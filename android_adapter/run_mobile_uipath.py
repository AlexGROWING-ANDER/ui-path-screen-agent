"""Run the public UiPath Screen Agent loop against one Android KG-RAG task.

This is an adapter around the upstream planner/grounder. It does not modify
their implementation. Secrets are loaded from the existing Mobile-Agent .env
and are never written to the output directory or command line.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from android_text_input import TextInputError, write_verified_text

WORKSPACE = Path(os.environ.get('UIPATH_WORKSPACE', str(Path(__file__).resolve().parents[1])))
UIPATH_REPO = Path(os.environ.get('UIPATH_REPO', 'H:/GUI_AGENT_OSWorld/OSWorld-v1'))
MOBILE_AGENT = WORKSPACE / "mobile_agent_v3/mobile_v3"
sys.path.insert(0,str(MOBILE_AGENT))
from communication_permissions import authorized_communication
DEFAULT_ADB = Path("D:/Android/platform-tools/adb.exe")
DEFAULT_OUTPUT = Path("H:/GUI_AGENT_runs/ui_path_screen_agent/kg_rag_v2_hard_intents/gpt56sol_medium")
ADAPTER_VERSION = "android-input-verified-v4-20261026"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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
    parser.add_argument('--allow-communications',action='store_true',help='Allow task-directed sending/publishing, without permitting payments')
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
        text=not binary, encoding=None if binary else "utf-8",
        errors=None if binary else "replace", timeout=30, creationflags=NO_WINDOW,
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
        result = adb_run(adb, "shell", "input", "tap", str(x), str(y))
        if result.returncode:
            raise RuntimeError("ADB tap failed")
        time.sleep(0.12)


def ensure_adb_keyboard(adb: Path) -> str:
    current = adb_run(adb, "shell", "settings", "get", "secure", "default_input_method")
    if current.returncode:
        raise RuntimeError("ADB input-method query failed")
    if "adbkeyboard" not in (current.stdout or "").lower():
        installed = adb_run(adb, "shell", "ime", "list", "-s")
        choices = [line.strip() for line in (installed.stdout or "").splitlines()
                   if "adbkeyboard" in line.lower() and "/" in line]
        if installed.returncode or len(choices) != 1:
            raise RuntimeError("ADB Keyboard IME unavailable; text was not sent")
        for command in ("enable", "set"):
            switched = adb_run(adb, "shell", "ime", command, choices[0])
            if switched.returncode:
                raise RuntimeError("ADB Keyboard IME activation failed")
        confirmed = adb_run(adb, "shell", "settings", "get", "secure", "default_input_method")
        if confirmed.returncode or "adbkeyboard" not in (confirmed.stdout or "").lower():
            raise RuntimeError("ADB Keyboard IME activation was not confirmed")
        current = confirmed
    return (current.stdout or '').strip()


def type_text(adb: Path, value: str) -> dict:
    return write_verified_text(adb, value, adb_run, ensure_adb_keyboard, time.sleep)


def normalize_keys(keys: object) -> list[str]:
    """Accept native lists, JSON/Python list strings, and Ctrl+A/Ctrl A."""
    if isinstance(keys, str) and keys.strip().startswith("["):
        try:
            keys = ast.literal_eval(keys.strip())
        except (ValueError, SyntaxError):
            return []
    if not isinstance(keys, (str, list, tuple)):
        return []
    aliases = {"control": "ctrl", "ctrlleft": "ctrl", "ctrlright": "ctrl",
               "return": "enter", "escape": "esc", "pagedown": "pgdn",
               "page_down": "pgdn", "pageup": "pgup", "page_up": "pgup",
               "arrowleft": "left", "arrowright": "right", "arrowup": "up",
               "arrowdown": "down", "backspace": "del"}
    values = [keys] if isinstance(keys, str) else keys
    normalized = []
    for raw in values:
        if not isinstance(raw, str):
            return []
        for token in re.split(r"\s*\+\s*|\s+", raw.strip()):
            if token:
                normalized.append(aliases.get(token.lower(), token.lower()))
    return normalized


def execution_actions(step: dict) -> list[dict]:
    """Preserve the model response; repair only upstream key-name conversion."""
    actions = copy.deepcopy(step.get("actions", []))
    raw_plan = (step.get("additional_parameters") or {}).get("plan_action", {})
    try:
        plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
    except (ValueError, TypeError):
        plan = {}
    if isinstance(plan, dict) and plan.get("type") == "key_press":
        raw_key = (plan.get("parameters") or {}).get("key")
        # Use the structured original key, never prose instructions that may
        # contain a second, unplanned operation. Backspace != Android Back.
        if raw_key is not None and len(actions) == 1 and actions[0].get("method_type") == "keypress":
            actions[0].setdefault("parameters", {})["keys"] = raw_key
    return actions


def requires_confirmation(action: dict, allow_side_effects: bool, allow_communications: bool = False) -> bool:
    name = str(action.get("method_type", ""))
    description = str(action.get("description", ""))
    # These action types cannot submit a remote write by themselves. In
    # particular a finish summary mentioning 'deleted' is not a delete action.
    if name in {"finish", "scroll", "mouse_move", "wait_load_completed", "type_into"}:
        return False
    if name == "keypress":
        keys = normalize_keys((action.get("parameters") or {}).get("keys", []))
        local_keys = {"back", "esc", "left", "right", "up", "down",
                      "home", "end", "move_home", "pgup", "pgdn", "tab"}
        if keys and (all(k in local_keys for k in keys) or keys == ["ctrl", "a"]):
            return False
        if keys and all(k in {"del", "delete"} for k in keys) and re.search(
                r"搜索框|输入框|文本框|编辑框|姓名|名称|文字|文本|字符|text|input|search", description, re.I):
            return False
    if name == "click" and re.search(r"搜索框|输入框|文本框|编辑框|名称框|姓名栏|text field|input field|search box", description, re.I):
        if re.search(r"清空|清除|删除|clear|erase|delete", description, re.I) and not re.search(r"账号|账户|account|历史记录|购物车", description, re.I):
            return False
    if allow_side_effects or (allow_communications and authorized_communication(description)):
        return False
    return any(term in description for term in SIDE_EFFECT_TERMS) or bool(
        re.search(r"\b(send|share|publish|post|comment|delete|purchase|pay|checkout)\b", description, re.I))


def execute_action(adb: Path, action: dict, allow_side_effects: bool, allow_communications: bool = False) -> tuple[bool, str]:
    name = str(action.get("method_type", ""))
    params = action.get("parameters") or {}
    if name == "finish":
        return True, "finished"
    supported = {"click", "type_into", "scroll", "drag", "keypress", "wait_load_completed", "mouse_move"}
    if name not in supported:
        return True, f"unsupported_action:{name}"
    if requires_confirmation(action, allow_side_effects, allow_communications):
        return True, f"human_confirmation_required:{action.get('description', '')}"
    if name == "click":
        x, y = map(int, params["position"])
        click_type = params.get("click_type", "single")
        tap(adb, x, y, {"double": 2, "triple": 3}.get(click_type, 1))
    elif name == "type_into":
        try:
            action["adapter_execution"] = type_text(adb, str(params.get("value", "")))
        except TextInputError as exc:
            action["adapter_execution"] = exc.details
            return True, f"input_execution_error:{exc.code}"
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
        result = adb_run(adb, "shell", "input", "swipe", str(x), str(y), str(x2), str(y2), "500")
        if result.returncode:
            raise RuntimeError("ADB scroll failed")
    elif name == "drag":
        path = params["path"]
        start, end = path[0], path[-1]
        result = adb_run(adb, "shell", "input", "swipe", str(start["x"]), str(start["y"]), str(end["x"]), str(end["y"]), "700")
        if result.returncode:
            raise RuntimeError("ADB drag failed")
    elif name == "keypress":
        return execute_keypress(adb, params.get("keys", []))
    elif name in {"wait_load_completed", "mouse_move"}:
        time.sleep(2)
    else:
        return True, f"unsupported_action:{name}"
    return False, "executed"


def execute_keypress(adb: Path, keys: list[str]) -> tuple[bool, str]:
    """Validate the entire action before sending Android key events."""
    normalized = normalize_keys(keys)
    mapping = {"back": 4, "enter": 66, "home": 3, "esc": 4, "del": 67,
               "delete": 112, "tab": 61, "space": 62, "left": 21, "right": 22,
               "up": 19, "down": 20, "pgup": 92, "pgdn": 93,
               "ctrl": 113, "shift": 59, "alt": 57, "meta": 117,
               "end": 123, "move_home": 122, "insert": 124,
               "@": 77}
    mapping.update({chr(97 + n): 29 + n for n in range(26)})
    mapping.update({str(n): 7 + n for n in range(10)})
    mapping.update({f"f{n}": 130 + n for n in range(1, 13)})
    if not normalized:
        return True, "invalid_keypress:empty"
    for key in normalized:
        if key not in mapping:
            return True, f"unsupported_key:{key}"
    modifiers = {"ctrl", "shift", "alt", "meta"}
    if any(k in modifiers for k in normalized):
        if not any(k not in modifiers for k in normalized):
            return True, "invalid_keypress:modifier_without_key"
        help_result = adb_run(adb, "shell", "input", "help")
        if "keycombination" not in ((help_result.stdout or "") + (help_result.stderr or "")):
            return True, "unsupported_keycombination:device_input_command"
        command = "keycombination"
    else:
        command = "keyevent"
    result = adb_run(adb, "shell", "input", command, *[str(mapping[k]) for k in normalized])
    if result.returncode:
        raise RuntimeError("ADB keypress failed")
    return False, "executed"


def result_status(reason: str) -> str:
    if reason == "finished":
        return "completed"
    if reason.startswith("human_confirmation_required:"):
        return "human_confirmation"
    if reason.startswith(("unsupported_key:", "unsupported_keycombination:",
                          "invalid_keypress:", "unsupported_action:")):
        return "unsupported_action"
    if reason.startswith("device_execution_error:"):
        return "device_execution_error"
    if reason.startswith("input_execution_error:"):
        return "input_error"
    return "max_steps"


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
        "adapter_version": ADAPTER_VERSION,
        "permissions": {"allow_external_side_effects": args.allow_external_side_effects,
                        "allow_communications": getattr(args, "allow_communications", False)},
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
        for action in execution_actions(step):
            try:
                stop, reason = execute_action(args.adb, action, args.allow_external_side_effects, getattr(args,'allow_communications',False))
            except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
                stop, reason = True, f"device_execution_error:{type(exc).__name__}"
            # Keep a compact execution journal apart from the untouched model
            # response, including before-execution stops and key corrections.
            with (out / "action_execution.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"step": index, "action": action, "stop": stop,
                                         "reason": reason, "adapter_version": ADAPTER_VERSION}, ensure_ascii=False) + "\n")
            if stop:
                break
        # Capture the final observation even when the last input failed, so it
        # is reviewable without a nonexistent next model step.
        try:
            screenshot(args.adb, out / f"after_step_{index:02d}.png")
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError):
            with (out / "action_execution.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"step": index, "after_screenshot_unavailable": True}) + "\n")
        previous_steps.append(step)
        if stop:
            status = result_status(reason)
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
