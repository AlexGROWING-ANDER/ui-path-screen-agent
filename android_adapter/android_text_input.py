"""Verified Android text transport, independent of planner decisions.

No field selection, history clicking, text clearing, Enter, or search submission.
Only the current focused editable field may receive the model's exact payload.

Two opt-in capabilities were added for opaque/navigating inputs, without
weakening the existing path:

* A focused, enabled, focusable node that exposes no ``editable`` attribute and
  no text (a browser search bar drawn by a plain ``View``) is treated as an
  *opaque* target. Text can be delivered but never read back, so delivery is
  reported as ``sent_opaque_target`` instead of a false failure.
* If the read-back disappears after a send because the app auto-submitted and
  navigated, the whole hierarchy is searched for the payload. Success is only
  reported when the exact payload is visible somewhere in the tree.
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

# Widget classes that accept text. AutoCompleteTextView (and its
# MultiAutoCompleteTextView subclass) is what mail clients use for the
# recipient line, and it never carries the literal "EditText" suffix.
_INPUT_CLASS_MARKERS = ('edittext', 'autocompletetextview')
# A custom input bar still advertises itself somewhere. Without such a hint a
# focused WebView/SurfaceView is just a page, not a text target.
_INPUT_HINTS = ('input', 'search', 'edit', 'text', 'query', 'comment', 'reply')
_NOT_INPUT_CLASSES = ('webview', 'surfaceview')


def _is_input_class(class_name: str) -> bool:
    lowered = (class_name or '').lower()
    return any(marker in lowered for marker in _INPUT_CLASS_MARKERS)


def _looks_like_input_node(node: ET.Element) -> bool:
    lowered_class = (node.get('class') or '').lower()
    if any(marker in lowered_class for marker in _NOT_INPUT_CLASSES):
        return False
    hint = ((node.get('resource-id') or '') + ' ' + (node.get('content-desc') or '')).lower()
    return any(marker in hint for marker in _INPUT_HINTS)


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
    opaque: bool = False

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
                "class": self.class_name, "bounds": self.bounds,
                "opaque": self.opaque}


def _root_of(output: str) -> ET.Element:
    start = output.find('<?xml')
    if start < 0:
        start = output.find('<hierarchy')
    end = output.rfind('</hierarchy>')
    if start < 0 or end < 0:
        raise TextInputError('input_hierarchy_unavailable')
    try:
        return ET.fromstring(output[start:end + len('</hierarchy>')])
    except ET.ParseError as exc:
        raise TextInputError('input_hierarchy_invalid') from exc


def parse_focused_field(output: str) -> FocusedField:
    root = _root_of(output)
    fields = [n for n in root.iter('node') if n.get('focused') == 'true' and
              (n.get('editable') == 'true' or _is_input_class(n.get('class', '')))]
    if len(fields) > 1:
        # Some apps keep several inputs marked focused. Only a single one can
        # hold the IME connection, and a populated one is the active target.
        populated = [n for n in fields if (n.get('text') or '')]
        if len(populated) == 1:
            fields = populated
    if len(fields) != 1:
        # Opaque custom inputs expose neither an editable attribute nor text.
        # A single focused, enabled, focusable node is still the only place the
        # keyboard can deliver characters.
        candidates = [n for n in root.iter('node') if n.get('focused') == 'true'
                      and n.get('enabled', 'true') == 'true'
                      and n.get('focusable') == 'true'
                      and _looks_like_input_node(n)]
        if len(candidates) != 1:
            raise TextInputError('input_focus_missing_or_ambiguous')
        n = candidates[0]
        return FocusedField(n.get('package', ''), n.get('resource-id', ''),
                            n.get('class', ''), n.get('bounds', ''), n.get('text', ''),
                            n.get('password') == 'true', opaque=True)
    n = fields[0]
    if n.get('enabled', 'true') != 'true':
        raise TextInputError('input_field_disabled')
    return FocusedField(n.get('package', ''), n.get('resource-id', ''),
                        n.get('class', ''), n.get('bounds', ''), n.get('text', ''),
                        n.get('password') == 'true')


def dump_hierarchy(adb: Path, run: Callable) -> str:
    """Return the raw UI hierarchy XML, preferring the cheapest working dump."""
    if str(adb) in _SERVICE_DUMP_ADB:
        return read_service_hierarchy(adb, run)
    if str(adb) not in _FILE_DUMP_ADB:
        result = run(adb, 'shell', 'uiautomator', 'dump', '/dev/tty')
        output = result.stdout or ''
        if not result.returncode and '</hierarchy>' in output:
            return output
        _FILE_DUMP_ADB.add(str(adb))
    # Some OEM builds report success for /dev/tty without returning XML.
    # A unique file prevents stale dumps from being mistaken for current state.
    remote = '/sdcard/unipath_ui_' + uuid.uuid4().hex + '.xml'
    try:
        dumped = run(adb, 'shell', 'uiautomator', 'dump', remote)
        contents = run(adb, 'shell', 'cat', remote) if not dumped.returncode else None
        if contents is not None and not contents.returncode and '</hierarchy>' in (contents.stdout or ''):
            return contents.stdout
        output = read_service_hierarchy(adb, run)
        _SERVICE_DUMP_ADB.add(str(adb))
        return output
    finally:
        run(adb, 'shell', 'rm', '-f', remote)


def read_focused_field(adb: Path, run: Callable) -> FocusedField:
    return parse_focused_field(dump_hierarchy(adb, run))


def hierarchy_contains(adb: Path, run: Callable, value: str) -> bool:
    """True when the exact payload is visible somewhere in the current tree.

    Diagnostic only: it never sends text, so a failed read-back cannot be
    mistaken for a delivered payload unless the characters are actually on
    screen after an app-driven navigation.
    """
    if not value:
        return False
    try:
        root = _root_of(dump_hierarchy(adb, run))
    except (TextInputError, OSError):
        return False
    return any(value in (n.get('text') or '') or value in (n.get('content-desc') or '')
               for n in root.iter('node'))


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
        if ready.opaque:
            # The focused control paints its own text and exposes none of it.
            # Delivery is the best observable outcome; never claim read-back.
            try:
                after = read_focused_field(adb, run)
            except (TextInputError, OSError):
                after = None
            if after is not None and (not ready.same_target(after) or after.password):
                raise TextInputError('input_focus_changed_after_send', details)
            return {**details, "verified": True, "verification": "sent_opaque_target",
                    "channel": channel, "text_observable": False,
                    "transport_warning": transport_error}
        unchanged = True
        for _ in range(2):
            sleep(.25)
            try:
                after = read_focused_field(adb, run)
            except (TextInputError, OSError) as exc:
                # An app may auto-submit on input and tear the field down. Look
                # for the exact payload in the new screen before failing.
                if hierarchy_contains(adb, run, value):
                    return {**details, "verified": True,
                            "verification": "text_visible_in_hierarchy",
                            "channel": channel, "transport_warning": transport_error}
                raise TextInputError('input_readback_unavailable_after_send', details) from exc
            if not ready.same_target(after) or after.password:
                if after.text != ready.text and value in after.text:
                    return {**details, "verified": True, "verification": "same_field_text_changed",
                            "channel": channel, "transport_warning": transport_error}
                if hierarchy_contains(adb, run, value):
                    return {**details, "verified": True,
                            "verification": "text_visible_in_hierarchy",
                            "channel": channel, "transport_warning": transport_error}
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
