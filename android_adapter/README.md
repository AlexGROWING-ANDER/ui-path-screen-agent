# KG-RAG × UiPath Screen Agent mobile adapter

This adapter preserves the public UiPath planner and grounder and adds only:

- Android screenshot and action execution through ADB;
- KG-RAG intent loading;
- isolated trajectory/result persistence;
- a default stop before external side effects.

Preflight without model calls or phone actions:

```powershell
& 'H:/GUI_AGENT_OSWorld/venv-v1/Scripts/python.exe' -X utf8 `
  'C:/Users/ASUS/Documents/Codex/2026-09-19/new-chat/unipath_mobile/run_mobile_uipath.py' `
  --package com.baidu.baidutranslate `
  --intent-file 'E:/GUI_AGENT架构归因/hard_intents/com.baidu.baidutranslate.json' `
  --test-id test_1 `
  --preflight-only
```

Remove `--preflight-only` for the isolated smoke run. It writes only under:

`H:/GUI_AGENT_runs/ui_path_screen_agent/kg_rag_v2_hard_intents/gpt56sol_medium/<timestamp>/`

## Android adapter update

The adapter executes UiPath actions through Android ADB. Key presses support
letters, digits, arrows, `PgUp`, `PgDn`, and combinations such as `Ctrl+A`
when the connected device exposes `adb shell input keycombination`.

`type_into` now matches the active keyboard protocol and verifies visible text
after whole-string input; see [v4 details](../docs/android-input-v4.md).
Unsupported key input is recorded as `unsupported_action`,
while `human_confirmation` is reserved for the external-side-effect safety gate.

For the approved rerun that intentionally allows external effects, pass the
flag explicitly to the runner:

```powershell
python -X utf8 android_adapter/run_mobile_uipath.py `
  --package <package> `
  --intent-file <intent-file> `
  --test-id <test-id> `
  --max-steps 30 `
  --allow-external-side-effects
```

For a watcher-driven rerun, pass the same flag to the watcher:

```powershell
python -X utf8 android_adapter/watch_unipath_resume.py `
  --until '<local ISO timestamp>' `
  --allow-external-side-effects
```

The flag remains disabled by default and must be supplied explicitly.

