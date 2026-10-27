"""Exercise transport and readback as a device state machine, without a phone."""
import base64
import shlex
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parent))
import android_text_input as text_input
import run_mobile_uipath as adapter


def field(text='', *, rid='search', package='app', password=False, bounds='[0,0][100,60]'):
    return text_input.FocusedField(package,rid,'android.widget.EditText',bounds,text,password)


class FakeDevice:
    def __init__(self, states, receipts=None):
        self.states=iter(states);self.receipts=iter(receipts or [])
        self.commands=[]
    def run(self, *args):
        self.commands.append(args)
        class R:
            returncode=0;stdout='Broadcast completed: result=0';stderr=''
        return next(self.receipts,R())
    def read(self, *args):return next(self.states)


class VerifiedInputTests(unittest.TestCase):
    def test_idless_search_clear_icon_resize_is_same_field(self):
        a=field(rid='',bounds='[363,117][860,225]')
        self.assertTrue(a.same_target(field(rid='',bounds='[363,117][812,225]')))
        self.assertFalse(a.same_target(field(rid='',bounds='[363,300][812,408]')))
        self.assertFalse(a.same_target(field(rid='',bounds='[363,117][400,225]')))

    def test_cli_dump_failure_uses_service_without_stale_file(self):
        from types import SimpleNamespace
        xml='<hierarchy><node focused="true" class="android.widget.EditText" text="new" enabled="true" /></hierarchy>'
        def run(*args):
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(text_input,'_FILE_DUMP_ADB',set()), patch.object(text_input,'_SERVICE_DUMP_ADB',set()), patch.object(text_input,'read_service_hierarchy',return_value=xml) as service:
            self.assertEqual(text_input.read_focused_field(Path('adb'),run).text,'new')
            self.assertEqual(text_input.read_focused_field(Path('adb'),run).text,'new')
            self.assertEqual(service.call_count,2)

    def test_oem_dump_without_stdout_uses_unique_file_and_cleans_it(self):
        from types import SimpleNamespace
        xml='<hierarchy><node focused="true" class="android.widget.EditText" text="hello" enabled="true" /></hierarchy>'
        calls=[]
        def run(*args):
            calls.append(args)
            return SimpleNamespace(returncode=0,stdout=xml if 'cat' in args else 'UI hierarchy dumped',stderr='')
        with patch.object(text_input,'_FILE_DUMP_ADB',set()):
            self.assertEqual(text_input.read_focused_field(Path('adb'),run).text,'hello')
        self.assertEqual(calls[-1][2:4],('rm','-f'))
        self.assertTrue(calls[-1][-1].startswith('/sdcard/unipath_ui_'))
        self.assertEqual(calls[1][-1],calls[2][-1])

    def test_uiautomator_keyboard_uses_its_own_protocol(self):
        class Receipt:
            returncode=0;stdout='Broadcast completed: result=-1';stderr=''
        d=FakeDevice([field(),field(),field('中文 hello')],[Receipt()])
        info=self.write(d,'中文 hello',lambda a:'com.github.uiautomator/.AdbKeyboard')
        self.assertTrue(info['verified'])
        self.assertEqual(info['channel'],'ADB_KEYBOARD_INPUT_TEXT')
        self.assertEqual(d.commands[0][-2],'text')
        self.assertEqual(base64.b64decode(shlex.split(d.commands[0][-1])[0]).decode(),'中文 hello')

    def write(self, device, value, ensure=None):
        with patch.object(text_input,'read_focused_field',side_effect=device.read):
            return text_input.write_verified_text(Path('adb'), value,device.run,ensure or (lambda a:None),lambda t:None)

    def test_chinese_spaces_symbols_and_multiline_round_trip_in_b64(self):
        for value in ['你好','Taylor Swift 123',"' $HOME; & hello",'hello\nworld','😀 café']:
            with self.subTest(value=value):
                d=FakeDevice([field(),field(),field(value)])
                info=self.write(d,value)
                self.assertTrue(info['verified'])
                self.assertEqual(len(d.commands),1)
                self.assertEqual(d.commands[0][5],'ADB_INPUT_B64')
                payload=shlex.split(d.commands[0][-1])[0]
                self.assertEqual(base64.b64decode(payload).decode('utf-8'),value)

    def test_broadcast_receipt_without_field_change_is_not_success(self):
        d=FakeDevice([field()]*6)
        with self.assertRaisesRegex(text_input.TextInputError,'input_no_visible_change'):
            self.write(d,'hello')
        self.assertEqual(len(d.commands),2)
        self.assertEqual(d.commands[1][5],'ADB_INPUT_TEXT')

    def test_legacy_keyboard_falls_back_only_when_same_field_stayed_unchanged(self):
        d=FakeDevice([field(),field(),field(),field(),field('hello')])
        info=self.write(d,'hello')
        self.assertEqual(info['channel'],'ADB_INPUT_TEXT')
        self.assertEqual(len(d.commands),2)

    def test_delayed_update_is_not_sent_twice(self):
        d=FakeDevice([field(),field(),field(),field('hello')])
        self.assertTrue(self.write(d,'hello')['verified'])
        self.assertEqual(len(d.commands),1)

    def test_partial_write_never_retries_and_duplicates_text(self):
        d=FakeDevice([field(),field(),field('hel')])
        with self.assertRaisesRegex(text_input.TextInputError,'input_text_mismatch'):
            self.write(d,'hello')
        self.assertEqual(len(d.commands),1)

    def test_focus_switch_after_send_never_retries_into_another_field(self):
        d=FakeDevice([field(),field(),field(rid='recipient')])
        with self.assertRaisesRegex(text_input.TextInputError,'input_focus_changed_after_send'):
            self.write(d,'hello')
        # A diagnostic dump may run, but the payload must never be sent twice.
        self.assertEqual(sum('broadcast' in c for c in d.commands),1)

    def test_ime_switch_must_not_change_target_or_text(self):
        d=FakeDevice([field(),field(rid='other')])
        with self.assertRaisesRegex(text_input.TextInputError,'input_target_changed'):
            self.write(d,'hello')
        self.assertEqual(d.commands,[])

    def test_existing_text_does_not_prove_new_input_was_delivered(self):
        d=FakeDevice([field('hello')]*6)
        with self.assertRaisesRegex(text_input.TextInputError,'input_no_visible_change'):
            self.write(d,'hello')

    def test_insert_and_replace_keep_original_action_semantics(self):
        for before,after in [('prefix','prefixhello'),('old','hello')]:
            d=FakeDevice([field(before),field(before),field(after)])
            self.assertTrue(self.write(d,'hello')['verified'])
            self.assertEqual(len(d.commands),1)
            self.assertNotIn('keyevent',str(d.commands))

    def test_password_or_no_focus_never_receives_text(self):
        d=FakeDevice([field(password=True)])
        with self.assertRaisesRegex(text_input.TextInputError,'password'):
            self.write(d,'hello')
        self.assertEqual(d.commands,[])

    def test_missing_readback_after_send_is_not_success(self):
        d=FakeDevice([field(),field()])
        with patch.object(text_input,'read_focused_field',side_effect=[field(),field(),text_input.TextInputError('input_hierarchy_unavailable')]):
            with self.assertRaisesRegex(text_input.TextInputError,'input_readback_unavailable'):
                text_input.write_verified_text(Path('adb'),'hello',d.run,lambda a:None,lambda t:None)
        # A diagnostic dump may run, but the payload must never be sent twice.
        self.assertEqual(sum('broadcast' in c for c in d.commands),1)

    def test_visible_change_with_lost_receipt_is_not_duplicated(self):
        class Bad:
            returncode=1;stdout='';stderr='device transport error'
        d=FakeDevice([field(),field(),field('hello')],[Bad()])
        self.assertTrue(self.write(d,'hello')['verified'])
        self.assertEqual(len(d.commands),1)

    def test_input_errors_get_their_own_status_and_execution_evidence(self):
        action={'method_type':'type_into','parameters':{'value':'hello'}}
        with patch.object(adapter,'type_text',side_effect=text_input.TextInputError('input_no_visible_change')):
            stop,reason=adapter.execute_action(Path('adb'),action,True)
        self.assertTrue(stop)
        self.assertEqual(adapter.result_status(reason),'input_error')
        self.assertFalse(action['adapter_execution']['verified'])

    def test_xml_requires_exactly_one_editable_focus(self):
        xml='''<?xml version="1.0"?><hierarchy><node class="android.widget.EditText" focused="true" enabled="true" password="false" package="app" resource-id="search" bounds="[0,0][100,60]" text="中文"/></hierarchy>'''
        self.assertEqual(text_input.parse_focused_field(xml).text,'中文')
        for s in ['not xml',xml.replace('focused="true"','focused="false"'),xml.replace('enabled="true"','enabled="false"')]:
            with self.assertRaises(text_input.TextInputError):text_input.parse_focused_field(s)

    def test_field_resize_is_ok_when_unique_id_is_stable(self):
        d=FakeDevice([field(),field(bounds='[0,0][100,50]'),field('hello',bounds='[0,0][100,50]')])
        self.assertTrue(self.write(d,'hello')['verified'])

    def test_opaque_focused_view_is_accepted_without_readback(self):
        # A browser search bar painted by a plain View: focused+focusable but
        # no editable attribute, no text and no children.
        xml=('<?xml version="1.0"?><hierarchy><node class="android.view.View" '
             'resource-id="app:id/search_frame_input_bar" package="app" text="" '
             'focused="true" focusable="true" clickable="true" enabled="true" '
             'bounds="[291,195][774,266]"/></hierarchy>')
        parsed=text_input.parse_focused_field(xml)
        self.assertTrue(parsed.opaque)
        self.assertEqual(parsed.resource_id,'app:id/search_frame_input_bar')
        with self.assertRaisesRegex(text_input.TextInputError,'input_focus_missing_or_ambiguous'):
            text_input.parse_focused_field(xml.replace('focusable="true"','focusable="false"'))

    def test_focused_webview_is_never_an_opaque_text_target(self):
        xml=('<?xml version="1.0"?><hierarchy><node class="android.webkit.WebView" '
             'package="app" text="" focused="true" focusable="true" clickable="true" '
             'enabled="true" bounds="[0,114][1080,2268]"/></hierarchy>')
        with self.assertRaisesRegex(text_input.TextInputError,'input_focus_missing_or_ambiguous'):
            text_input.parse_focused_field(xml)


    def test_opaque_target_reports_sent_not_verified_text(self):
        opaque=text_input.FocusedField('app','app:id/bar','android.view.View','[0,0][100,60]','',False,True)
        d=FakeDevice([opaque,opaque,opaque])
        with patch.object(text_input,'read_focused_field',side_effect=[opaque,opaque,opaque]):
            info=text_input.write_verified_text(Path('adb'),'豆包',d.run,lambda a:'com.github.uiautomator/.AdbKeyboard',lambda t:None)
        self.assertTrue(info['verified'])
        self.assertEqual(info['verification'],'sent_opaque_target')
        self.assertFalse(info['text_observable'])
        self.assertEqual(sum('broadcast' in c for c in d.commands),1)

    def test_auto_submit_navigation_verifies_payload_on_new_screen(self):
        d=FakeDevice([field(),field()])
        with patch.object(text_input,'read_focused_field',side_effect=[field(),field(),text_input.TextInputError('input_focus_missing_or_ambiguous')]), \
             patch.object(text_input,'hierarchy_contains',return_value=True) as probe:
            info=text_input.write_verified_text(Path('adb'),'极锐数码',d.run,lambda a:'com.github.uiautomator/.AdbKeyboard',lambda t:None)
        self.assertTrue(info['verified'])
        self.assertEqual(info['verification'],'text_visible_in_hierarchy')
        probe.assert_called_once()
        self.assertEqual(sum('broadcast' in c for c in d.commands),1)

    def test_autocomplete_text_view_is_a_recognised_input(self):
        # Mail clients use MultiAutoCompleteTextView for the recipient line;
        # its class never contains the literal "EditText".
        xml=('<?xml version="1.0"?><hierarchy>'
             '<node class="android.widget.MultiAutoCompleteTextView" package="app" '
             'text="" focused="true" focusable="true" enabled="true" bounds="[237,299][972,351]"/>'
             '</hierarchy>')
        parsed=text_input.parse_focused_field(xml)
        self.assertFalse(parsed.opaque)
        self.assertEqual(parsed.class_name,'android.widget.MultiAutoCompleteTextView')

    def test_two_focused_inputs_prefer_the_populated_one(self):
        xml=('<?xml version="1.0"?><hierarchy>'
             '<node class="android.widget.MultiAutoCompleteTextView" package="app" text="donglaixu99@qq.com" '
             'focused="true" focusable="true" enabled="true" bounds="[237,299][972,351]"/>'
             '<node class="android.widget.EditText" package="app" text="" '
             'focused="true" focusable="true" enabled="true" bounds="[60,801][1044,858]"/>'
             '</hierarchy>')
        parsed=text_input.parse_focused_field(xml)
        self.assertEqual(parsed.class_name,'android.widget.MultiAutoCompleteTextView')
        self.assertEqual(parsed.text,'donglaixu99@qq.com')


if __name__=='__main__':unittest.main()
