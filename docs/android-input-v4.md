# Android input adapter v4

The old adapter could report a completed broadcast without entering text. The
installed uiautomator AdbKeyboard uses a different protocol from ADBKeyBoard.
The adapter now chooses the protocol from the activated input method and checks
visible text in the same focused field after sending the full payload.

Other fixes:

- Recover the original structured Backspace/Delete action before Android key
  mapping; do not confuse Backspace with Android Back.
- Exempt local field editing, text input, scrolling and finish summaries from
  keyword-based external-write confirmation. Real external writes remain gated
  unless explicitly authorized.
- Classify unsupported keys as unsupported_action and unsuccessful text input as
  input_error, rather than human_confirmation.
- Carry explicit authorization flags through watcher selection modes.
- Use unique temporary XML dumps when /dev/tty produces no XML. If CLI dumping
  fails, use the installed uiautomator2 service (requires the Python package and
  device service). Never accept an old dump file as current state.
- Allow bounded horizontal resizing of ID-less search fields when a clear icon
  appears. Changes of package, class, vertical position or substantial size are
  rejected.
- Save after-action screenshots and execution diagnostics separately from the
  original model response. Stop on unverified input instead of consuming further
  planner steps.
- Frozen rerun selections can require a fresh initial-state acknowledgement for
  each attempt. The watcher reports blocked tasks as needs_attention.

## Verification

On 2026-10-26, a vivo device passed 48 checkpoints across two system-settings
rounds, QQ Music search and NetEase Cloud Music search. Checks covered Chinese,
English, spaces, quotes, ampersands, Ctrl+A, Backspace and selection replacement.
No searches were submitted, no business data were saved, and no benchmark reruns
were launched. Screenshots, device identifiers and trajectories are excluded
from this repository.

Run offline checks:

```text
python -m unittest discover -s android_adapter -p "test_*.py"
```

The website-snapshot replay test skips when its local fixture is absent.
To run the device probe, prepare a neutral empty field and explicitly pass
`--run-in-empty-test-field`. An OEM hint exposed as text may be supplied using
`--empty-field-hint`. The probe stops on the first mismatch.

The runner still expects the existing KG-RAG experiment layout. Set
`UIPATH_WORKSPACE` to that workspace and `UIPATH_REPO` to its OSWorld checkout
when using this standalone package. ADB and output paths have CLI overrides;
watcher defaults remain specific to the experiment machine.

These checks do not establish support for every custom canvas, WebView, password
field or business workflow. An inaccessible field stops with an explicit error.
Initial app/account state must be restored before a benchmark rerun.
