import sys
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding='utf-8')
tree = ET.parse('test_reports_test/acceptance_junit.xml')
root = tree.getroot()

failed = []
passed = []
for tc in root.iter('testcase'):
    name = tc.get('name', '')
    classname = tc.get('classname', '')
    if tc.find('failure') is not None:
        failed.append(f"{classname}::{name}")
    else:
        passed.append(f"{classname}::{name}")

print(f"=== PASSED ({len(passed)}) ===")
for p in passed:
    print(f"  PASS: {p}")

print(f"\n=== FAILED ({len(failed)}) ===")
for f in failed:
    print(f"  FAIL: {f}")

print(f"\nTotal: {len(passed) + len(failed)}, Passed: {len(passed)}, Failed: {len(failed)}")
