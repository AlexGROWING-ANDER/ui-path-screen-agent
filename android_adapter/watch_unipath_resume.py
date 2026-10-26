"""Continuously resume KG-RAG × UiPath mobile tasks until a fixed deadline."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path


WORKSPACE = Path(os.environ.get('UIPATH_WORKSPACE', str(Path(__file__).resolve().parents[1])))
PYTHON = Path("H:/GUI_AGENT_OSWorld/venv-v1/Scripts/python.exe")
RUNNER = Path(__file__).resolve().parent / "run_mobile_uipath.py"
CONFIG = WORKSPACE / "agent_s/config/experiments_hard_intents.json"
ADB = Path("D:/Android/platform-tools/adb.exe")
RESULT_ROOT = Path("H:/GUI_AGENT_runs/ui_path_screen_agent/kg_rag_v2_hard_intents/gpt56sol_medium")
WATCH_ROOT = RESULT_ROOT / "watch"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TERMINAL = {"completed", "human_confirmation", "max_steps", "input_error", "unsupported_action", "device_execution_error"}
PRIVACY_SKIP_TERMS = ("发送邮件", "发邮件", "邮件发送", "发送一封邮件", "send email", "send an email", "send mail")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", required=True, help="Local ISO timestamp")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--task-timeout-seconds", type=int, default=2700)
    parser.add_argument("--rerun-gated", action="store_true", help="Rerun only recorded safety-gate interruptions in a separate output root")
    parser.add_argument("--rerun-keypress", action="store_true", help="Rerun unsupported-key results in a separate root")
    parser.add_argument("--rerun-selection", type=Path, help="Rerun a frozen task selection in its own output root")
    parser.add_argument("--allow-external-side-effects", action="store_true", help="Explicitly allow external side effects in a frozen selection rerun")
    parser.add_argument('--allow-email-tasks',action='store_true',help='User explicitly withdrew the former email privacy pause')
    return parser.parse_args()


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def events(path: Path, **value: object) -> None:
    value["time"] = dt.datetime.now().isoformat()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def task_list() -> list[dict]:
    config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    tasks: list[dict] = []
    for app in config["apps"]["all"]:
        intent_file = Path(app["intent_json"])
        intents = json.loads(intent_file.read_text(encoding="utf-8-sig"))
        for test_id in intents:
            if str(test_id).startswith("test_"):
                tasks.append({
                    "package": app["app_package"],
                    "app_name": app.get("app_name"),
                    "test_id": test_id,
                    "intent_file": str(intent_file),
                    "intent": str((intents[test_id].get("intention") if isinstance(intents[test_id], dict) else intents[test_id]) or ""),
                })
    return tasks


def is_privacy_skip(task: dict) -> bool:
    text = task.get("intent", "").lower()
    package = task.get("package", "")
    email_apps = {"com.corp21cn.mail189", "com.tencent.androidqqmail"}
    return package in email_apps or any(term.lower() in text for term in PRIVACY_SKIP_TERMS)


def terminal_tasks(max_steps: int) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(RESULT_ROOT.glob("20*/**/task_result.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8-sig"))
            status = str(result.get("status", ""))
            configured_steps = int(result.get("max_steps", 0))
            # A short smoke that finishes is valid; a short smoke that merely
            # reaches its limit must be rerun with the formal 30-step ceiling.
            if status in TERMINAL and not (status == "max_steps" and configured_steps < max_steps):
                key = f"{result['package']}/{result['test_id']}"
                found[key] = status
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


def adb_online() -> bool:
    try:
        result = subprocess.run([str(ADB), "devices"], capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
        return any(line.strip().endswith("\tdevice") for line in result.stdout.splitlines()[1:])
    except (OSError, subprocess.SubprocessError):
        return False


def free_physical_mb() -> int:
    command = "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW,
    )
    return round(int(result.stdout.strip()) / 1024) if result.returncode == 0 else 0


def external_runner_alive() -> bool:
    command = (
        "@(Get-CimInstance Win32_Process | Where-Object {"
        "$_.Name -eq 'python.exe' -and $_.CommandLine -match 'run_mobile_uipath.py'"
        "}).Count"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW,
        )
        if result.returncode:
            return True
        return int(result.stdout.strip()) > 0
    except (ValueError, OSError, subprocess.SubprocessError):
        # If discovery fails, wait instead of racing an existing phone runner.
        return True


def stop_tree(pid: int) -> None:
    subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)


def build_runner_command(args: argparse.Namespace, task: dict, output_root: Path) -> list[str]:
    """All queue modes share one permission path; selection never drops flags."""
    command = [str(PYTHON), "-u", "-X", "utf8", str(RUNNER),
               "--package", task["package"], "--intent-file", task["intent_file"],
               "--test-id", task["test_id"], "--max-steps", str(args.max_steps),
               "--output-root", str(output_root)]
    if args.allow_external_side_effects or args.rerun_gated or args.rerun_keypress:
        command.append("--allow-external-side-effects")
    if args.allow_email_tasks:
        command.append("--allow-communications")
    return command


def consume_initial_state_review(selection_path: Path, key: str) -> bool:
    """A manual reset is valid for one attempt, not all future retries."""
    selection = json.loads(selection_path.read_text(encoding="utf-8-sig"))
    if not selection.get("requires_initial_state_review"):
        return True
    ready = selection.get("initial_state_ready", [])
    if key not in ready:
        return False
    selection["initial_state_ready"] = [item for item in ready if item != key]
    selection.setdefault("initial_state_consumed", {})[key] = dt.datetime.now().isoformat()
    atomic_json(selection_path, selection)
    return True


def main() -> int:
    global RESULT_ROOT, WATCH_ROOT
    args = parse_args()
    if sum((args.rerun_keypress, args.rerun_gated, args.rerun_selection is not None)) > 1:
        raise ValueError("Select only one rerun mode")
    deadline = dt.datetime.fromisoformat(args.until)
    tasks = task_list()
    if len(tasks) != 130:
        raise RuntimeError(f"Expected 130 tasks, found {len(tasks)}")
    gate_sources = {}
    if args.rerun_keypress:
        source_root = RESULT_ROOT.with_name(RESULT_ROOT.name + "_gate_rerun")
        for source in sorted(source_root.glob("20*/**/task_result.json")):
            data = json.loads(source.read_text(encoding="utf-8-sig"))
            if str(data.get("reason", "")).startswith("unsupported_key:"):
                gate_sources[f"{data['package']}/{data['test_id']}"] = str(source)
        tasks = [t for t in tasks if f"{t['package']}/{t['test_id']}" in gate_sources]
        if len(tasks) != 13:
            raise ValueError(f"Expected 13 keypress reruns, found {len(tasks)}")
        RESULT_ROOT = RESULT_ROOT.with_name(RESULT_ROOT.name + "_keypress_rerun")
        WATCH_ROOT = RESULT_ROOT / "watch"
    if args.rerun_selection:
        selection = json.loads(args.rerun_selection.read_text(encoding="utf-8-sig"))
        selected = set(selection.get("selected", []))
        tasks = [task for task in tasks if f"{task['package']}/{task['test_id']}" in selected]
        if len(tasks) != len(selected):
            known = {f"{task['package']}/{task['test_id']}" for task in tasks}
            raise ValueError(f"Selection contains unknown task keys: {sorted(selected - known)}")
        RESULT_ROOT = Path(selection["output_root"])
        WATCH_ROOT = RESULT_ROOT / "watch"
        if args.allow_external_side_effects and not selection.get("allow_external_side_effects"):
            raise ValueError("CLI permission conflicts with frozen selection; update the selection explicitly")
        args.allow_external_side_effects = bool(args.allow_external_side_effects and selection.get("allow_external_side_effects"))
    if args.rerun_gated:
        for source in sorted(RESULT_ROOT.glob("20*/**/task_result.json")):
            data = json.loads(source.read_text(encoding="utf-8-sig"))
            if str(data.get("reason", "")).startswith("human_confirmation_required:"):
                key = f"{data['package']}/{data['test_id']}"
                gate_sources[key] = str(source)
        tasks = [t for t in tasks if f"{t['package']}/{t['test_id']}" in gate_sources]
        RESULT_ROOT = RESULT_ROOT.with_name(RESULT_ROOT.name + "_gate_rerun")
        WATCH_ROOT = RESULT_ROOT / "watch"
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    watch = WATCH_ROOT / run_id
    watch.mkdir(parents=True, exist_ok=False)
    stop_path = watch / "STOP"
    status_path = watch / "status.json"
    events_path = watch / "events.jsonl"
    if args.rerun_gated or args.rerun_keypress:
        atomic_json(watch / "selection.json", {"source_results": gate_sources,
                    "selected": tasks, "allow_external_side_effects": True,
                    "privacy_excluded": [t for t in tasks if is_privacy_skip(t) and not args.allow_email_tasks],
                    "allow_email_tasks":args.allow_email_tasks})
    elif args.rerun_selection:
        atomic_json(watch / "selection.json", {**selection, "allow_external_side_effects": args.allow_external_side_effects})
    attempts: dict[str, int] = {}
    skipped = {f"{task['package']}/{task['test_id']}": "privacy_email_send" for task in tasks if is_privacy_skip(task) and not args.allow_email_tasks}
    started = dt.datetime.now()
    events(events_path, kind="watch_started", deadline=deadline.isoformat(), total=len(tasks))

    while dt.datetime.now() < deadline and not stop_path.exists():
        completed = terminal_tasks(args.max_steps)
        pending = [task for task in tasks if f"{task['package']}/{task['test_id']}" not in completed and f"{task['package']}/{task['test_id']}" not in skipped]
        exhausted = {key for key, count in attempts.items() if count >= args.max_attempts}
        pending = [task for task in pending if f"{task['package']}/{task['test_id']}" not in exhausted]
        blocked = {key: status for key, status in completed.items()
                   if status in {"human_confirmation", "input_error", "unsupported_action", "device_execution_error"}}
        unresolved = sorted(exhausted - set(completed))
        state = {
            "pid": os.getpid(), "started": started.isoformat(), "deadline": deadline.isoformat(),
            "status": ("needs_attention" if blocked or unresolved else "complete") if not pending else "waiting",
            "terminal": len(completed), "skipped": len(skipped), "total": len(tasks), "remaining": len(pending),
            "task": None, "child_pid": None, "heartbeat": dt.datetime.now().isoformat(),
            "attempts": attempts, "result_root": str(RESULT_ROOT),
            "blocked_tasks": blocked, "exhausted_without_result": unresolved,
        }
        atomic_json(status_path, state)
        if not pending:
            events(events_path, kind="all_terminal", terminal=len(completed), skipped=len(skipped),
                   skipped_tasks=sorted(skipped), blocked_tasks=blocked, exhausted_without_result=unresolved)
            return 2 if blocked or unresolved else 0
        if external_runner_alive():
            state["status"] = "waiting_for_existing_smoke"
            state["heartbeat"] = dt.datetime.now().isoformat()
            atomic_json(status_path, state)
            time.sleep(15)
            continue
        if not adb_online():
            state["status"] = "waiting_for_adb"
            state["heartbeat"] = dt.datetime.now().isoformat()
            atomic_json(status_path, state)
            time.sleep(30)
            continue
        memory = free_physical_mb()
        if memory and memory < 700:
            state["status"] = "waiting_for_memory"
            state["free_physical_mb"] = memory
            state["heartbeat"] = dt.datetime.now().isoformat()
            atomic_json(status_path, state)
            time.sleep(30)
            continue

        task = pending[0]
        key = f"{task['package']}/{task['test_id']}"
        attempts[key] = attempts.get(key, 0) + 1
        log_base = watch / f"{task['package']}_{task['test_id']}_attempt_{attempts[key]}"
        if args.rerun_selection:
            if not consume_initial_state_review(args.rerun_selection, key):
                attempts[key] -= 1
                state.update({"status": "waiting_for_initial_state", "task": key, "attempts": attempts})
                atomic_json(status_path, state)
                time.sleep(15)
                continue
        command = build_runner_command(args, task, RESULT_ROOT)
        with log_base.with_suffix(".stdout.log").open("w", encoding="utf-8") as stdout, \
             log_base.with_suffix(".stderr.log").open("w", encoding="utf-8") as stderr:
            child = subprocess.Popen(command, cwd=WORKSPACE, stdout=stdout, stderr=stderr, creationflags=NO_WINDOW)
            state.update({"status": "running_task", "task": key, "child_pid": child.pid,
                          "heartbeat": dt.datetime.now().isoformat(), "attempts": attempts})
            atomic_json(status_path, state)
            events(events_path, kind="task_started", task=key, attempt=attempts[key], pid=child.pid,
                   allow_external_side_effects="--allow-external-side-effects" in command,
                   allow_communications="--allow-communications" in command)
            began = time.monotonic()
            while child.poll() is None and dt.datetime.now() < deadline and not stop_path.exists():
                state["heartbeat"] = dt.datetime.now().isoformat()
                state["elapsed_seconds"] = round(time.monotonic() - began)
                atomic_json(status_path, state)
                if time.monotonic() - began > args.task_timeout_seconds:
                    stop_tree(child.pid)
                    events(events_path, kind="task_timeout", task=key, attempt=attempts[key])
                    break
                time.sleep(10)
            if child.poll() is None:
                stop_tree(child.pid)
            return_code = child.poll()
            events(events_path, kind="task_exited", task=key, attempt=attempts[key], return_code=return_code)
        time.sleep(3)

    final = {
        "pid": os.getpid(), "started": started.isoformat(), "deadline": deadline.isoformat(),
        "status": "stopped" if stop_path.exists() else "deadline_reached",
        "terminal": len(terminal_tasks(args.max_steps)), "total": len(tasks),
        "heartbeat": dt.datetime.now().isoformat(), "attempts": attempts,
    }
    atomic_json(status_path, final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
