#!/usr/bin/env python3
"""i18n Coverage Checker for EdgeLiteGateway.

Scans all .vue files under web/src/views/ and web/src/components/,
extracts t('xxx') / t("xxx") translation key references,
and compares them against keys defined in zh-CN.ts and en-US.ts.

Usage:
    python scripts/check_i18n_coverage.py
    python scripts/check_i18n_coverage.py --verbose
"""

import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VUE_DIRS = [
    BASE_DIR / "web" / "src" / "views",
    BASE_DIR / "web" / "src" / "components",
]
ZH_CN_FILE = BASE_DIR / "web" / "src" / "i18n" / "zh-CN.ts"
EN_US_FILE = BASE_DIR / "web" / "src" / "i18n" / "en-US.ts"

T_CALL_PATTERN = re.compile(r"""t\(\s*['"]([^'"]+)['"]\s*\)""")

INVALID_KEY_PATTERNS = [
    re.compile(r'^[0-9]'),
    re.compile(r'^/'),
    re.compile(r'^three/'),
    re.compile(r'^\.$'),
    re.compile(r'^[a-z]$'),
    re.compile(r'\.(js|ts|vue|css|html|json|md|py)$'),
]

IGNORED_KEYS = {'canvas', 'default', 'three'}


def is_valid_i18n_key(key: str) -> bool:
    if key in IGNORED_KEYS:
        return False
    for pat in INVALID_KEY_PATTERNS:
        if pat.search(key):
            return False
    return True


def extract_keys_from_vue_files() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for vue_dir in VUE_DIRS:
        if not vue_dir.exists():
            continue
        for vue_file in vue_dir.rglob("*.vue"):
            content = vue_file.read_text(encoding="utf-8")
            matches = T_CALL_PATTERN.findall(content)
            for key in matches:
                if is_valid_i18n_key(key):
                    used.setdefault(key, []).append(str(vue_file.relative_to(BASE_DIR)))
    return used


def extract_defined_keys(ts_file: Path) -> set[str]:
    content = ts_file.read_text(encoding="utf-8")
    content = re.sub(r'//.*', '', content)
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)

    keys: set[str] = set()
    path_stack: list[str] = []

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        m = re.match(r'^(\w+)\s*:\s*(.*)', stripped)
        if m:
            key_name = m.group(1)
            value_part = m.group(2).strip()
            current_path = '.'.join(path_stack + [key_name])
            keys.add(current_path)

            if '{' in value_part and '}' not in value_part:
                path_stack.append(key_name)
            elif value_part == '{':
                path_stack.append(key_name)

        opens = stripped.count('{')
        closes = stripped.count('}')
        net_closes = closes - opens
        for _ in range(max(0, net_closes)):
            if path_stack:
                path_stack.pop()

    return keys


def main():
    verbose = "--verbose" in sys.argv

    print("=" * 60)
    print("EdgeLiteGateway i18n Coverage Checker")
    print("=" * 60)

    used_keys = extract_keys_from_vue_files()
    print(f"\n  Vue files scanned: {len(VUE_DIRS)} directories")
    print(f"  Unique t() keys found: {len(used_keys)}")

    zh_keys = extract_defined_keys(ZH_CN_FILE)
    en_keys = extract_defined_keys(EN_US_FILE)
    print(f"  zh-CN keys defined: {len(zh_keys)}")
    print(f"  en-US keys defined: {len(en_keys)}")

    used_set = set(used_keys.keys())

    missing_zh = used_set - zh_keys
    missing_en = used_set - en_keys
    zh_only = zh_keys - en_keys
    en_only = en_keys - zh_keys

    print(f"\n{'=' * 60}")
    print(f"  Missing in zh-CN: {len(missing_zh)} keys")
    print(f"  Missing in en-US: {len(missing_en)} keys")
    print(f"  zh-CN only (not in en-US): {len(zh_only)} keys")
    print(f"  en-US only (not in zh-CN): {len(en_only)} keys")

    if missing_zh:
        print(f"\n--- Missing in zh-CN ---")
        for k in sorted(missing_zh):
            files = used_keys.get(k, [])
            file_info = f"  (used in: {files[0]})" if files and verbose else ""
            print(f"  {k}{file_info}")

    if missing_en:
        print(f"\n--- Missing in en-US ---")
        for k in sorted(missing_en):
            files = used_keys.get(k, [])
            file_info = f"  (used in: {files[0]})" if files and verbose else ""
            print(f"  {k}{file_info}")

    if zh_only:
        print(f"\n--- Defined only in zh-CN (missing en-US translation) ---")
        for k in sorted(zh_only):
            print(f"  {k}")

    if en_only:
        print(f"\n--- Defined only in en-US (missing zh-CN translation) ---")
        for k in sorted(en_only):
            print(f"  {k}")

    unused_zh = zh_keys - used_set
    unused_en = en_keys - used_set
    print(f"\n  Keys defined in zh-CN but not used in .vue: {len(unused_zh)}")
    print(f"  Keys defined in en-US but not used in .vue: {len(unused_en)}")

    has_issues = bool(missing_zh or missing_en or zh_only or en_only)
    print(f"\n{'=' * 60}")
    if has_issues:
        print("FAILED - missing keys detected")
    else:
        print("PASSED - all keys are covered")
    print("=" * 60)

    return 1 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
