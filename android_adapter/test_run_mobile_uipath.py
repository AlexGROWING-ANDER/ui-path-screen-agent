import sys
import argparse
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import run_mobile_uipath as adapter
import watch_unipath_resume as watcher


class Completed:
    returncode = 0
    stdout = "keyevent keycombination"
    stderr = ""


class AndroidAdapterTests(unittest.TestCase):
    def test_keypress_supports_combination_and_page_down(self):
        with patch.object(adapter, "adb_run", return_value=Completed()) as adb_run:
            self.assertEqual(adapter.execute_keypress(Path("adb"), "Ctrl+A"), (False, "executed"))
            self.assertEqual(adapter.execute_keypress(Path("adb"), ["PgDn"]), (False, "executed"))
        self.assertEqual(adb_run.call_count, 3)

    def test_status_distinguishes_safety_gate_from_adapter_limitations(self):
        self.assertEqual(adapter.result_status("human_confirmation_required:send"), "human_confirmation")
        self.assertEqual(adapter.result_status("unsupported_key:pgdn"), "unsupported_action")
        self.assertEqual(adapter.result_status("unsupported_keycombination:device_input_command"), "unsupported_action")
        self.assertEqual(adapter.result_status("finished"), "completed")

    def test_watcher_forwards_explicit_side_effect_permission(self):
        task = {"package": "example.app", "intent_file": "intent.json", "test_id": "test_1"}
        args = argparse.Namespace(max_steps=30,allow_external_side_effects=True,allow_email_tasks=False,
                                  rerun_selection=None,rerun_gated=False,rerun_keypress=False)
        command = watcher.build_runner_command(args, task, Path('results'))
        self.assertIn("--allow-external-side-effects", command)
        args.allow_external_side_effects=False
        self.assertNotIn("--allow-external-side-effects", watcher.build_runner_command(args, task, Path('results')))


if __name__ == "__main__":
    unittest.main()
