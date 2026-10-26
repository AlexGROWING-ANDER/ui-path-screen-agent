"""Verified Android text transport, independent of planner decisions.

No field selection, history clicking, text clearing, Enter, or search submission.
Only the current focused editable field may receive the model's exact payload.
"""
from __future__ import annotations

import base64
import re
import shlex
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_FILE_DUMP_ADB: set[str] = set()
_SERVICE_DUMP_ADB: set[str] = set()


def read_service_hierarchy(adb: Path, run: Callable) -> str:
    """Use the already installed UIAutomator service when CLI dumps fail."""
    serial = run(adb, 'get-serialno')
    if serial.returncode or not (serial.stdout or '').strip():
        raise TextInputError('input_hierarchy_unavailable')
    try:
        import uiautomator2
        device = uiautomator2.connect_usb(serial.stdout.strip())
        return device.dump_hierarchy(compressed=True)
    except Exception as exc:
        raise TextInputError('input_hierarchy_service_unavailable') from exc


class TextInputError(RuntimeError):
    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = {"verified": False, "error": code, **(details or {})}
        super().__init__(code)


@dataclass(frozen=True)
class FocusedField:
    package: str
    resource_id: str
    class_name: str
    bounds: str
    text: str
    password: bool = False

    def same_target(self, other: 'FocusedField') -> bool:
        if (self.package, self.class_name) != (other.package, other.class_name):
            return False
        if self.resource_id or other.resource_id:
            return bool(self.resource_id) and self.resource_id == other.resource_id
        if self.bounds and self.bounds == other.bounds:
            return True
        # Search fields without IDs shrink when their clear button appears.
        # Require identical left/top/bottom and at least 75% width overlap.
        a = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', self.bounds)
        b = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', other.bounds)
        if not a or not b:
            return False
        x1,y1,x2,y2 = map(int,a.groups())
        z1,t1,z2,t2 = map(int,b.groups())
        widths = (x2-x1,z2-z1)
        return (x1,y1,y2)==(z1,t1,t2) and min(widths)>0 and min(widths)/max(widths)>=.75

    def target_details(self) -> dict:
        return {"package": self.package, "resource_id": self.resource_id,
                "class": self.class_name, "bounds": self.bounds}


def parse_focused_field(output: str) -> FocusedField:
    start = output.find('<?xml')
    if start < 0:
        start = output.find('<hierarchy')
    end = output.rfind('</hierarchy>')
    if start < 0 or end < 0:
        raise TextInputError('input_hierarchy_unavailable')
    try:
        root = ET.fromstring(output[start:end + len('</hierarchy>')])
    except ET.ParseError as exc:
        raise TextInputError('input_hierarchy_invalid') from exc
    fields = [n for n in root.iter('node') if n.get('focused') == 'true' and
              (n.get('editable') == 'true' or 'edittext' in n.get('class', '').lower())]
    if len(fields) != 1:
        raise TextInputError('input_focus_missing_or_ambiguous')
    n = fields[0]
    if n.get('enabled', 'true') != 'true':
        raise TextInputError('input_field_disabled')
    return FocusedField(n.get('package', ''), n.get('resource-id', ''),
                        n.get('class', ''), n.get('bounds', ''), n.get('text', ''),
                        n.get('password') == 'true')


def read_focused_field(adb: Path, run: Callable) -> FocusedField:
    if str(adb) in _SERVICE_DUMP_ADB:
        return parse_focused_field(read_service_hierarchy(adb, run))
    if str(adb) not in _FILE_DUMP_ADB:
        result = run(adb, 'shell', 'uiautomator', 'dump', '/dev/tty')
        output = result.stdout or ''
        if not result.returncode and '</hierarchy>' in output:
            return parse_focused_field(output)
        _FILE_DUMP_ADB.add(str(adb))
    # Some OEM builds report success for /dev/tty without returning XML.
    # A unique file prevents stale dumps from being mistaken for current state.
    remote = '/sdcard/unipath_ui_' + uuid.uuid4().hex + '.xml'
    try:
        dumped = run(adb, 'shell', 'uiautomator', 'dump', remote)
        contents = run(adb, 'shell', 'cat', remote) if not dumped.returncode else None
        if contents is not None and not contents.returncode and '</hierarchy>' in (contents.stdout or ''):
            return parse_focused_field(contents.stdout)
        output = read_service_hierarchy(adb, run)
        parsed = parse_focused_field(output)
        _SERVICE_DUMP_ADB.add(str(adb))
        return parsed
    finally:
        run(adb, 'shell', 'rm', '-f', remote)


def broadcast(adb: Path, run: Callable, channel: str, value: str) -> None:
    uia = channel == 'ADB_KEYBOARD_INPUT_TEXT'
    payload = base64.b64encode(value.encode('utf-8')).decode('ascii') if channel == 'ADB_INPUT_B64' or uia else value
    result = run(adb, 'shell', 'am', 'broadcast', '-a', channel, '--es', 'text' if uia else 'msg', shlex.quote(payload))
    output = (result.stdout or '') + '\n' + (result.stderr or '')
    code = re.search(r'Broadcast completed:\s*result=(-?\d+)', output)
    if result.returncode or not code or int(code.group(1)) != (-1 if uia else 0) or re.search(r'Exception|Permission Denial|Error:', output):
        raise TextInputError('input_broadcast_failed', {"channel": channel})


def write_verified_text(adb: Path, value: str, run: Callable,
                        ensure_ime: Callable, sleep: Callable = time.sleep) -> dict:
    if not isinstance(value, str) or not value:
        raise TextInputError('input_payload_empty')
    before = read_focused_field(adb, run)
    if before.password:
        raise TextInputError('input_password_not_observable')
    try:
        ime = ensure_ime(adb)
    except (RuntimeError, OSError) as exc:
        raise TextInputError('input_ime_unavailable') from exc
    ready = read_focused_field(adb, run)
    if not before.same_target(ready) or ready.password or before.text != ready.text:
        raise TextInputError('input_target_changed_during_ime_activation')
    details = {"verified": False, "target": ready.target_details(),
               "requested_length": len(value), "before_length": len(ready.text), "transports": []}
    channels = ('ADB_KEYBOARD_INPUT_TEXT',) if isinstance(ime, str) and ime.startswith('com.github.uiautomator/') else ('ADB_INPUT_B64', 'ADB_INPUT_TEXT')
    for channel in channels:
        details['transports'].append(channel)
        # Even a failing command might have reached the receiver; observe first,
        # and do not blindly resend text after a transport exception.
        transport_error = None
        try:
            broadcast(adb, run, channel, value)
        except (RuntimeError, OSError) as exc:
            transport_error = type(exc).__name__
        unchanged = True
        for _ in range(2):
            sleep(.25)
            try:
                after = read_focused_field(adb, run)
            except (TextInputError, OSError) as exc:
                raise TextInputError('input_readback_unavailable_after_send', details) from exc
            if not ready.same_target(after) or after.password:
                raise TextInputError('input_focus_changed_after_send', details)
            details['after_length'] = len(after.text)
            if after.text != ready.text:
                unchanged = False
                if value in after.text:
                    # The payload reached this field, whether inserted or replacing
                    # a preselected range. Position/goal semantics remain model work.
                    return {**details, "verified": True, "verification": "same_field_text_changed",
                            "channel": channel, "transport_warning": transport_error}
                # Partial, corrupted or normalized text must not be appended again.
                raise TextInputError('input_text_mismatch', details)
        if not unchanged:
            raise TextInputError('input_text_mismatch', details)
        # Two unchanged observations in the exact same field allow one alternate
        # transport for old keyboards without B64. No clearing/replacement is added.
    raise TextInputError('input_no_visible_change', details)
