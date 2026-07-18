#!/usr/bin/env python3
"""检查 requirements.txt 与 pyproject.toml 依赖版本一致性。

用法: python scripts/check_deps_consistency.py
退出码: 0=一致, 1=存在不一致
"""

import re
import sys
import tomllib
from pathlib import Path


def parse_requirements_txt(filepath: str) -> dict[str, str]:
    """解析 requirements.txt 格式的依赖文件。"""
    deps = {}
    for line in Path(filepath).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or not line or line.startswith("["):
            continue
        # 去掉注释
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = re.match(r"^([a-zA-Z0-9_-]+)\s*(.*)", line)
        if m:
            name = m.group(1).lower()
            spec = m.group(2).strip()
            deps[name] = spec
    return deps


def parse_pyproject_toml(filepath: str) -> dict[str, str]:
    """解析 pyproject.toml 中的 dependencies。"""
    with open(filepath, "rb") as f:
        toml = tomllib.load(f)
    deps = {}
    for dep in toml["project"]["dependencies"]:
        m = re.match(r"^([a-zA-Z0-9_-]+)\s*(.*)", dep)
        if m:
            name = m.group(1).lower()
            spec = m.group(2).strip()
            deps[name] = spec
    return deps


def main() -> int:
    root = Path(__file__).parent.parent
    req_deps = parse_requirements_txt(str(root / "requirements.txt"))
    toml_deps = parse_pyproject_toml(str(root / "pyproject.toml"))

    print("=== Dependency consistency check ===")
    print(f"  requirements.txt: {len(req_deps)} packages")
    print(f"  pyproject.toml:   {len(toml_deps)} packages")
    print()

    all_names = set(req_deps.keys()) | set(toml_deps.keys())
    mismatches = []

    for name in sorted(all_names):
        req_spec = req_deps.get(name)
        toml_spec = toml_deps.get(name)

        if req_spec is None:
            print(f"  [ONLY-TOML] {name}: {toml_spec}")
            continue
        if toml_spec is None:
            print(f"  [ONLY-REQ]  {name}: {req_spec}")
            continue

        # 标准化比较：去掉空格
        req_norm = req_spec.replace(" ", "")
        toml_norm = toml_spec.replace(" ", "")

        if req_norm != toml_norm:
            mismatches.append((name, req_spec, toml_spec))
            print(f"  [MISMATCH]  {name}:")
            print(f"    requirements.txt: {req_spec}")
            print(f"    pyproject.toml:   {toml_spec}")

    print()
    if mismatches:
        print(f"[FAIL] Found {len(mismatches)} version mismatches!")
        return 1
    else:
        print("[OK] All shared dependencies are consistent!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
