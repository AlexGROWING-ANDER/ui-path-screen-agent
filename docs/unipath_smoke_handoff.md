# UniPath Smoke Test Handoff

## Current state

代码侧 runtime-only 已通过；实际手机 smoke 尚未执行，因为 ADB 当前无设备。

## Resume command

先确认 `adb devices -l` 恢复一台 `device`，再运行：

```powershell
& 'H:/GUI_AGENT_OSWorld/venv-v1/Scripts/python.exe' -X utf8 `
  'C:/Users/ASUS/Documents/Codex/2026-09-19/new-chat/unipath_mobile/run_mobile_uipath.py' `
  --package com.baidu.baidutranslate `
  --intent-file 'E:/GUI_AGENT架构归因/hard_intents/com.baidu.baidutranslate.json' `
  --test-id test_1 `
  --preflight-only
```

preflight 通过且物理内存安全后，移除 `--preflight-only` 执行单条 smoke。不要加 `--allow-external-side-effects`。

## Continuous run

- Supervisor: `unipath_mobile/watch_unipath_resume.py`
- Deadline: `2026-10-17T23:00:00`
- Status root: `H:/GUI_AGENT_runs/ui_path_screen_agent/kg_rag_v2_hard_intents/gpt56sol_medium/watch/`
- Stop: 在最新 watch 时间戳目录中创建空文件 `STOP`。
- Recovery: Windows 计划任务 `GUIAgent-Continuous-Monitor` 每 5 分钟检查并恢复 Supervisor。

