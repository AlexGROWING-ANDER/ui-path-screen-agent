"""Device-only input acceptance test in a manually prepared EMPTY test field.

No model, search submission, account changes, or benchmark task. Refuses to
write unless --run-in-empty-test-field is explicitly supplied and UI hierarchy
shows exactly one focused, editable, empty field. Stops at the first mismatch.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).parent))
import run_mobile_uipath as adapter
from android_text_input import read_focused_field, FocusedField


def focused_text(adb: Path) -> tuple[dict,str]:
    node=read_focused_field(adb,adapter.adb_run)
    identity={'resource-id':node.resource_id,'class':node.class_name,'package':node.package}
    if not node.resource_id:
        identity['bounds']=node.bounds
    if node.password:
        raise RuntimeError('Password fields cannot be used for input testing')
    return identity,node.text


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb',type=Path,default=adapter.DEFAULT_ADB)
    parser.add_argument('--run-in-empty-test-field',action='store_true')
    parser.add_argument('--empty-field-hint',default=None,
                        help='Explicitly verified hint exposed as text by an OEM empty editor')
    args=parser.parse_args()
    if not args.run_in_empty_test_field:
        parser.error('Prepare an empty neutral test editor, then explicitly pass --run-in-empty-test-field')
    adapter.adb_device(args.adb)
    identity,text=focused_text(args.adb)
    if text and text != args.empty_field_hint:
        raise RuntimeError('Test field must be empty; existing text will not be replaced')
    checks=[]
    def expect(expected: str):
        time.sleep(.3)
        now,actual=focused_text(args.adb)
        if expected == '' and actual == args.empty_field_hint:
            actual = ''
        def as_field(value):
            return FocusedField(value['package'],value['resource-id'],value['class'],value.get('bounds',''),'')
        same=as_field(now).same_target(as_field(identity))
        checks.append({'expected':expected,'actual':actual,'same_field':same,'passed':same and actual==expected})
        print(json.dumps({'check':len(checks),'passed':checks[-1]['passed'],
                          'expected_length':len(expected),'actual_length':len(actual)},ensure_ascii=False),flush=True)
        if not same or actual!=expected:
            raise RuntimeError('Visible field did not match input; no further test actions will run')
    try:
        for value in ['hello','中文输入','Taylor Swift 123', "a'b & 123", 'my love']:
            adapter.type_text(args.adb,value);expect(value)
            stop,reason=adapter.execute_keypress(args.adb,'Ctrl+A')
            if stop:raise RuntimeError(reason)
            stop,reason=adapter.execute_keypress(args.adb,'Backspace')
            if stop:raise RuntimeError(reason)
            expect('')
        adapter.type_text(args.adb,'abc');expect('abc')
        # Select only the known three characters and replace them in the same field.
        stop,reason=adapter.execute_keypress(args.adb,'Ctrl+A')
        if stop:raise RuntimeError(reason)
        adapter.type_text(args.adb,'my love');expect('my love')
        for key in ['Ctrl+A','Backspace']:
            stop,reason=adapter.execute_keypress(args.adb,key)
            if stop:raise RuntimeError(reason)
        expect('')
        passed=True;error=None
    except (RuntimeError,OSError) as exc:
        passed=False;error=str(exc)
    out=Path(__file__).parent/'confirmation_repair_20261026'/('input_device_check_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S')+'.json')
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps({'passed':passed,'error':error,'adapter_version':adapter.ADAPTER_VERSION,
        'test_field':identity,'checks':checks,'no_search_or_submission':True},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':passed,'checks':len(checks),'report':str(out)},ensure_ascii=False))
    return 0 if passed else 2


if __name__=='__main__':raise SystemExit(main())
