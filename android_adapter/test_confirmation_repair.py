"""Regression checks for the real Android gate failures; no phone/API calls."""
import argparse
import copy
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import run_mobile_uipath as adapter
import watch_unipath_resume as watcher


def result(stdout='', returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, '')


def step(key, grounded):
    return {'additional_parameters': {'plan_action': json.dumps({
        'type': 'key_press', 'parameters': {'key': key}})},
        'actions': [{'method_type': 'keypress', 'description': '删除搜索框文字',
                     'parameters': {'keys': grounded}}]}


class ConfirmationRepairTests(unittest.TestCase):
    def test_upstream_backspace_and_delete_preserve_original_key(self):
        for original, grounded, expected in [('Backspace', ['Back'], '67'), ('Delete', ['Del'], '112'), ('Back', ['Back'], '4')]:
            with self.subTest(original=original):
                source = step(original, grounded);before = copy.deepcopy(source)
                action = adapter.execution_actions(source)[0]
                with patch.object(adapter, 'adb_run', return_value=result()) as run:
                    self.assertEqual(adapter.execute_action(Path('adb'), action, True), (False, 'executed'))
                    self.assertEqual(run.call_args.args[-1], expected)
                self.assertEqual(source, before)

    def test_various_key_encodings(self):
        for raw in ['Ctrl+A', 'Ctrl A', ['Ctrl', 'A'], "['Ctrl', 'A']", '["Ctrl", "A"]']:
            self.assertEqual(adapter.normalize_keys(raw), ['ctrl', 'a'])
        for raw in ['[]', "['Ctrl'", {}, 1]:
            self.assertEqual(adapter.normalize_keys(raw), [])

    def test_unknown_key_validated_before_sending(self):
        with patch.object(adapter, 'adb_run') as run:
            self.assertEqual(adapter.execute_keypress(Path('adb'), ['Ctrl', 'badkey']), (True, 'unsupported_key:badkey'))
            run.assert_not_called()
        self.assertEqual(adapter.result_status('unsupported_key:badkey'), 'unsupported_action')

    def test_local_actions_do_not_require_side_effect_authorization(self):
        cases = [
            {'method_type': 'finish', 'description': '已删除商品，任务完成'},
            {'method_type': 'scroll', 'description': '横向滑动分享渠道', 'parameters': {}},
            {'method_type': 'type_into', 'description': '输入要发送的邮件正文', 'parameters': {'value': 'hello'}},
            {'method_type': 'keypress', 'description': '删除搜索框中的文字', 'parameters': {'keys': ['Backspace']}},
            {'method_type': 'keypress', 'description': '按Delete删除输入框字符', 'parameters': {'keys': ['Delete']}},
            {'method_type': 'click', 'description': '点击搜索框X清除文字', 'parameters': {'position': [1, 2]}},
        ]
        for action in cases:
            with self.subTest(action=action):
                self.assertFalse(adapter.requires_confirmation(action, False))
        self.assertEqual(adapter.execute_action(Path('adb'), cases[0], False), (True, 'finished'))

    def test_actual_external_writes_remain_gated_without_authorization(self):
        for desc in ['点击发送邮件按钮', '发布评论', '删除账号', '删除购物车商品', 'Delete selected file', '点击支付']:
            action = {'method_type': 'click', 'description': desc}
            self.assertTrue(adapter.requires_confirmation(action, False), desc)
            self.assertFalse(adapter.requires_confirmation(action, True), desc)
        self.assertTrue(adapter.requires_confirmation({'method_type': 'drag', 'description': '拖动文件到删除区域'}, False))
        self.assertTrue(adapter.requires_confirmation({'method_type': 'keypress', 'description': '按Delete删除所选文件', 'parameters': {'keys': ['Delete']}}, False))

    def test_communications_do_not_grant_payment_permission(self):
        self.assertFalse(adapter.requires_confirmation({'method_type': 'click', 'description': '发送邮件'}, False, True))
        self.assertTrue(adapter.requires_confirmation({'method_type': 'click', 'description': '点击付款'}, False, True))

    def test_selection_mode_carries_permission_flags(self):
        task = {'package': 'example', 'test_id': 'test_1', 'intent_file': 'intent.json'}
        for mode in ['normal', 'selection', 'gated', 'keypress']:
            args = argparse.Namespace(max_steps=30, rerun_selection=mode == 'selection',
                rerun_gated=mode == 'gated', rerun_keypress=mode == 'keypress',
                allow_external_side_effects=True, allow_email_tasks=True)
            command = watcher.build_runner_command(args, task, Path('separate-results'))
            self.assertIn('--allow-external-side-effects', command, mode)
            self.assertIn('--allow-communications', command, mode)
            self.assertEqual(command[command.index('--output-root') + 1], 'separate-results')
        args.rerun_keypress=False;args.allow_external_side_effects=False;args.allow_email_tasks=False
        command = watcher.build_runner_command(args, task, Path('separate-results'))
        self.assertNotIn('--allow-external-side-effects', command)
        self.assertNotIn('--allow-communications', command)

    def test_process_probe_timeout_waits_instead_of_crashing_or_starting(self):
        with patch.object(watcher.subprocess, 'run', side_effect=subprocess.TimeoutExpired('powershell', 20)):
            self.assertTrue(watcher.external_runner_alive())

    def test_manual_reset_is_required_again_for_each_retry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            p = Path(folder)/'selection.json'
            p.write_text(json.dumps({'requires_initial_state_review':True, 'initial_state_ready':['a/test_1']}), encoding='utf-8')
            self.assertFalse(watcher.consume_initial_state_review(p, 'b/test_2'))
            self.assertTrue(watcher.consume_initial_state_review(p, 'a/test_1'))
            self.assertFalse(watcher.consume_initial_state_review(p, 'a/test_1'))

    def test_text_activates_ime_and_quotes_payload(self):
        responses = [result('com.vivo/.IME'), result('com.android.adbkeyboard/.AdbIME'),
                     result(), result(), result('com.android.adbkeyboard/.AdbIME'),
                     result('Broadcast completed: result=0')]
        with patch.object(adapter, 'adb_run', side_effect=responses) as run, patch.object(adapter.time, 'sleep'):
            adapter.ensure_adb_keyboard(Path('adb'))
        self.assertEqual(run.call_args_list[3].args[3], 'set')

    def test_missing_ime_does_not_silently_report_input_success(self):
        with patch.object(adapter, 'adb_run', side_effect=[result('com.vivo/.IME'), result('com.vivo/.IME')]) as run:
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                adapter.ensure_adb_keyboard(Path('adb'))
            self.assertEqual(run.call_count, 2)

    def test_failed_broadcast_does_not_report_success(self):
        from android_text_input import broadcast, TextInputError
        with patch.object(adapter, 'adb_run', return_value=result('Error: receiver failed')):
            with self.assertRaises(TextInputError):
                broadcast(Path('adb'), adapter.adb_run, 'ADB_INPUT_B64', 'hello')

    def test_current_website_confirmation_stops_are_replayed_read_only(self):
        snapshot = adapter.WORKSPACE/'attribution_viewer/unipath_review_20261024/snapshot.json'
        if not snapshot.exists():
            self.skipTest('No frozen website snapshot')
        checked = 0
        for case in json.loads(snapshot.read_text(encoding='utf-8'))['cases']:
            if case['result'].get('status') != 'human_confirmation':
                continue
            trajectory = Path(case['source'])/'trajectory.jsonl'
            source = json.loads(trajectory.read_text(encoding='utf-8').splitlines()[-1])['response']
            for action in adapter.execution_actions(source):
                self.assertFalse(adapter.requires_confirmation(action, True, True), (case['number'], action))
                if action['method_type']=='keypress':
                    plan=json.loads(source['additional_parameters']['plan_action'])
                    self.assertEqual(adapter.normalize_keys(action['parameters']['keys']), adapter.normalize_keys(plan['parameters']['key']))
            checked+=1
        self.assertGreater(checked, 0)


if __name__ == '__main__':
    unittest.main()
