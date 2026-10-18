## UI Path Screen Agent

Private research package for the UiPath Screen Agent experiment.

Contains the public UiPath planner/grounder plus an Android ADB execution adapter for KG-RAG. No API keys, cookies, screenshots, trajectories, or local env files are included.

### Layout

- upstream_uipath: copied UiPath Python core
- android_adapter: Android runner and bounded watcher
- docs: experiment handoff notes

Planner and grounder use gpt-5.6-sol with reasoning_effort=medium at runtime. Credentials must stay in a local env file outside this repository.
