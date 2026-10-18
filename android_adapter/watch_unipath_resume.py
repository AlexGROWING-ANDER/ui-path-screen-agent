"""Continuously resume KG-RAG × UiPath mobile tasks until a fixed deadline."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
PYTHON = Path("H:/GUI_AGENT_OSWorld/venv-v1/Scripts/python.exe")
RUNNER = Path(__file__).resolve().parent / "run_mobile_uipath.py"
CONFIG = WORKSPACE / "agent_s/config/experiments_hard_intents.json"
ADB = Path("D:/Android/platform-tools/adb.exe")
RESULT_ROOT = Path("H:/GUI_AGENT_runs/ui_path_screen_agent/kg_rag_v2_hard_intents/gpt56sol_medium")
WATCH_ROOT = RESULT_ROOT / "watch"
TERMINAL = {"completed", "human_confirmation", "max_steps"}
PRIVACY_SKIP_TERMS = ("发送邮件", "发邮件", "邮件发送", "发送一封邮件", "send email", "send an email", "send mail")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", required=True, help="Local ISO timestamp")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--task-timeout-seconds", type=int, default=2700)
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
    for path in RESULT_ROOT.glob("20*/**/task_result.json"):
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
        result = subprocess.run([str(ADB), "devices"], capture_output=True, text=True, timeout=20)
        return any(line.strip().endswith("\tdevice") for line in result.stdout.splitlines()[1:])
    except (OSError, subprocess.SubprocessError):
        return False


def free_physical_mb() -> int:
    command = "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True, timeout=20,
    )
    return round(int(result.stdout.strip()) / 1024) if result.returncode == 0 else 0


def external_runner_alive() -> bool:
    command = (
        "@(Get-CimInstance Win32_Process | Where-Object {"
        "$_.Name -eq 'python.exe' -and $_.CommandLine -match 'run_mobile_uipath.py'"
        "}).Count"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True, timeout=20,
    )
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def stop_tree(pid: int) -> None:
    subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], capture_output=True)


def main() -> int:
    args = parse_args()
    deadline = dt.datetime.fromisoformat(args.until)
    tasks = task_list()
    if len(tasks) != 130:
        raise RuntimeError(f"Expected 130 tasks, found {len(tasks)}")
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    watch = WATCH_ROOT / run_id
    watch.mkdir(parents=True, exist_ok=False)
    stop_path = watch / "STOP"
    status_path = watch / "status.json"
    events_path = watch / "events.jsonl"
    attempts: dict[str, int] = {}
    skipped = {f"{task['package']}/{task['test_id']}": "privacy_email_send" for task in tasks if is_privacy_skip(task)}
    started = dt.datetime.now()
    events(events_path, kind="watch_started", deadline=deadline.isoformat(), total=len(tasks))

    while dt.datetime.now() < deadline and not stop_path.exists():
        completed = terminal_tasks(args.max_steps)
        pending = [task for task in tasks if f"{task['package']}/{task['test_id']}" not in completed and f"{task['package']}/{task['test_id']}" not in skipped]
        exhausted = {key for key, count in attempts.items() if count >= args.max_attempts}
        pending = [task for task in pending if f"{task['package']}/{task['test_id']}" not in exhausted]
        state = {
            "pid": os.getpid(), "started": started.isoformat(), "deadline": deadline.isoformat(),
            "status": "complete" if not pending else "waiting",
            "terminal": len(completed), "skipped": len(skipped), "total": len(tasks), "remaining": len(pending),
            "task": None, "child_pid": None, "heartbeat": dt.datetime.now().isoformat(),
            "attempts": attempts, "result_root": str(RESULT_ROOT),
        }
        atomic_json(status_path, state)
        if not pending:
            events(events_path, kind="all_terminal", terminal=len(completed), skipped=len(skipped), skipped_tasks=sorted(skipped))
            return 0
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
        command = [
            str(PYTHON), "-u", "-X", "utf8", str(RUNNER),
            "--package", task["package"], "--intent-file", task["intent_file"],
            "--test-id", task["test_id"], "--max-steps", str(args.max_steps),
        ]
        with log_base.with_suffix(".stdout.log").open("w", encoding="utf-8") as stdout, \
             log_base.with_suffix(".stderr.log").open("w", encoding="utf-8") as stderr:
            child = subprocess.Popen(command, cwd=WORKSPACE, stdout=stdout, stderr=stderr)
            state.update({"status": "running_task", "task": key, "child_pid": child.pid,
                          "heartbeat": dt.datetime.now().isoformat(), "attempts": attempts})
            atomic_json(status_path, state)
            events(events_path, kind="task_started", task=key, attempt=attempts[key], pid=child.pid)
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
