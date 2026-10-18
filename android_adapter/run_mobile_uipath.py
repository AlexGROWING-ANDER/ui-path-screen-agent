"""Run the public UiPath Screen Agent loop against one Android KG-RAG task.

This is an adapter around the upstream planner/grounder. It does not modify
their implementation. Secrets are loaded from the existing Mobile-Agent .env
and are never written to the output directory or command line.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
UIPATH_REPO = Path("H:/GUI_AGENT_OSWorld/OSWorld-v1")
MOBILE_AGENT = WORKSPACE / "mobile_agent_v3/mobile_v3"
DEFAULT_ADB = Path("D:/Android/platform-tools/adb.exe")
DEFAULT_OUTPUT = Path("H:/GUI_AGENT_runs/ui_path_screen_agent/kg_rag_v2_hard_intents/gpt56sol_medium")
SIDE_EFFECT_TERMS = (
    "发送", "分享", "发布", "评论", "下单", "购买", "付款", "支付", "转账",
    "删除", "注销", "提交订单", "确认收货",
)


def dotenv_values(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE pairs without adding a runtime dependency."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--intent-file", type=Path, required=True)
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--adb", type=Path, default=DEFAULT_ADB)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-external-side-effects", action="store_true")
    parser.add_argument("--runtime-only", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "task"


def load_intent(path: Path, test_id: str) -> str:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    value = data[test_id]
    if isinstance(value, str):
        return value
    intent = value.get("intention") or value.get("intent")
    if not intent:
        raise ValueError(f"No intention text for {test_id}")
    return str(intent)


def adb_run(adb: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(adb), *args], capture_output=True, check=False,
        text=not binary, timeout=30,
    )


def adb_device(adb: Path) -> str:
    result = adb_run(adb, "devices")
    devices = [
        line.split()[0] for line in result.stdout.splitlines()[1:]
        if line.strip().endswith("\tdevice")
    ]
    if len(devices) != 1:
        raise RuntimeError(f"Expected exactly one ADB device, found {len(devices)}")
    return devices[0]


def screenshot(adb: Path, path: Path) -> bytes:
    result = adb_run(adb, "exec-out", "screencap", "-p", binary=True)
    if result.returncode or not result.stdout:
        raise RuntimeError("ADB screenshot failed")
    path.write_bytes(result.stdout)
    from PIL import Image
    with Image.open(path) as image:
        image.verify()
    return result.stdout


def tap(adb: Path, x: int, y: int, count: int = 1) -> None:
    for _ in range(count):
        adb_run(adb, "shell", "input", "tap", str(x), str(y))
        time.sleep(0.12)


def type_text(adb: Path, value: str) -> None:
    # ADB Keyboard is already installed for the GUI-agent experiments and its
    # broadcast path supports Chinese/Unicode without a uiautomator2 dependency.
    result = adb_run(adb, "shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", value)
    if result.returncode != 0:
        raise RuntimeError("ADB Keyboard text broadcast failed")
    time.sleep(0.5)


def execute_action(adb: Path, action: dict, allow_side_effects: bool) -> tuple[bool, str]:
    name = str(action.get("method_type", ""))
    params = action.get("parameters") or {}
    description = str(action.get("description", ""))
    if not allow_side_effects and any(term in description for term in SIDE_EFFECT_TERMS):
        return True, f"human_confirmation_required:{description}"

    if name == "finish":
        return True, "finished"
    if name == "click":
        x, y = map(int, params["position"])
        click_type = params.get("click_type", "single")
        tap(adb, x, y, {"double": 2, "triple": 3}.get(click_type, 1))
    elif name == "type_into":
        type_text(adb, str(params.get("value", "")))
    elif name == "scroll":
        x, y = map(int, params.get("position", [500, 1000]))
        direction = str(params.get("direction", "down")).lower()
        distance = 650
        ends = {
            "down": (x, max(50, y - distance)),
            "up": (x, y + distance),
            "left": (x + distance, y),
            "right": (max(50, x - distance), y),
        }
        x2, y2 = ends.get(direction, ends["down"])
        adb_run(adb, "shell", "input", "swipe", str(x), str(y), str(x2), str(y2), "500")
    elif name == "drag":
        path = params["path"]
        start, end = path[0], path[-1]
        adb_run(adb, "shell", "input", "swipe", str(start["x"]), str(start["y"]), str(end["x"]), str(end["y"]), "700")
    elif name == "keypress":
        keys = [str(k).lower() for k in params.get("keys", [])]
        mapping = {"back": "4", "enter": "66", "home": "3", "esc": "4", "del": "67"}
        for key in keys:
            if key in mapping:
                adb_run(adb, "shell", "input", "keyevent", mapping[key])
            else:
                return True, f"unsupported_key:{key}"
    elif name in {"wait_load_completed", "mouse_move"}:
        time.sleep(2)
    else:
        return True, f"unsupported_action:{name}"
    return False, "executed"


async def run(args: argparse.Namespace) -> int:
    if not UIPATH_REPO.is_dir():
        raise RuntimeError(f"Missing UiPath source: {UIPATH_REPO}")
    if not args.adb.is_file():
        raise RuntimeError(f"Missing adb: {args.adb}")

    intent = load_intent(args.intent_file, args.test_id)
    mobile = dotenv_values(MOBILE_AGENT / ".env")
    base_url = mobile.get("MOBILE_AGENT_BASE_URL")
    api_key = mobile.get("MOBILE_AGENT_API_KEY")
    model = mobile.get("MOBILE_AGENT_MODEL") or "gpt-5.6-sol"
    if not base_url or not api_key:
        raise RuntimeError("Missing local Mobile-Agent API configuration")
    if model != "gpt-5.6-sol":
        raise RuntimeError(f"Model contract mismatch: {model}")

    sys.path.insert(0, str(UIPATH_REPO))
    from mm_agents.uipath.agent import UiPathComputerUseV1
    if args.runtime_only:
        print(f"runtime_ok package={args.package} test={args.test_id} model={model} effort=medium")
        return 0

    serial = adb_device(args.adb)
    installed = adb_run(args.adb, "shell", "pm", "list", "packages", args.package)
    if f"package:{args.package}" not in installed.stdout:
        raise RuntimeError(f"Package is not installed: {args.package}")

    print(f"preflight_ok device={serial} package={args.package} model={model} effort=medium")
    if args.preflight_only:
        return 0

    os.environ.update({
        "UIPATH_LLM_BASE_URL": str(base_url),
        "UIPATH_LLM_API_KEY": str(api_key),
        "UIPATH_GROUND_BASE_URL": str(base_url),
        "UIPATH_GROUND_API_KEY": str(api_key),
        "UIPATH_GROUND_MODEL": str(model),
    })
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.output_root / run_id / args.package / args.test_id
    out.mkdir(parents=True, exist_ok=False)
    meta = {
        "dataset": "kg_rag_v2_hard_intents",
        "architecture": "ui_path_screen_agent",
        "package": args.package,
        "test_id": args.test_id,
        "intent": intent,
        "model_contract": {"planner": model, "grounder": model, "reasoning_effort": "medium"},
        "device": serial,
        "max_steps": args.max_steps,
        "started_at": dt.datetime.now().isoformat(),
    }
    (out / "task_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    adb_run(args.adb, "shell", "am", "force-stop", args.package)
    adb_run(args.adb, "shell", "monkey", "-p", args.package, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(4)
    agent = UiPathComputerUseV1()
    previous_steps: list[dict] = []
    status, reason = "max_steps", "max_steps"
    trajectory = out / "trajectory.jsonl"

    for index in range(1, args.max_steps + 1):
        image_path = out / f"step_{index:02d}.png"
        image_bytes = screenshot(args.adb, image_path)
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        request = {"previousSteps": previous_steps, "userTask": intent, "image": image_b64}
        response = await agent.predict_request(request, model)
        step = response["step"]
        step["image"] = image_b64
        record = {"step": index, "timestamp": dt.datetime.now().isoformat(), "response": step}
        with trajectory.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        stop = False
        for action in step.get("actions", []):
            stop, reason = execute_action(args.adb, action, args.allow_external_side_effects)
            if stop:
                break
        previous_steps.append(step)
        if stop:
            status = "completed" if reason == "finished" else "human_confirmation"
            break
        time.sleep(2)

    result = {**meta, "status": status, "reason": reason, "steps": len(previous_steps), "finished_at": dt.datetime.now().isoformat()}
    (out / "task_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"result status={status} steps={len(previous_steps)} output={out}")
    return 0 if status == "completed" else 2


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
