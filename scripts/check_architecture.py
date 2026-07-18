#!/usr/bin/env python3
"""检查项目分层架构依赖合规性。

验证规则：
  - drivers/ 不得导入 api/
  - engine/ 不得导入 api/
  - storage/ 不得导入 api/ 或 services/
  - security/ 不得导入 api/

用法: python scripts/check_architecture.py
退出码: 0=合规, 1=违规
"""

from __future__ import annotations

import sys
from pathlib import Path

# Fix Windows GBK encoding issues
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 分层依赖规则：key 不得导入 value 中的任何模块
FORBIDDEN_IMPORTS = {
    "drivers": ["edgelite.api"],
    "engine": ["edgelite.api"],
    "storage": ["edgelite.api", "edgelite.services"],
    "security": ["edgelite.api"],
}


def check_layer(layer: str, forbidden: list[str]) -> list[str]:
    """检查指定层的所有文件是否违规导入了禁止的模块。"""
    violations = []
    src_root = Path(__file__).parent.parent / "src" / "edgelite" / layer
    if not src_root.exists():
        return violations

    for py_file in src_root.rglob("*.py"):
        rel_path = py_file.relative_to(Path(__file__).parent.parent)
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        for line_num, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            # 跳过注释和空行
            if not stripped or stripped.startswith("#"):
                continue
            # 检查 import 和 from ... import 语句
            if "import" not in stripped:
                continue
            for forbidden_mod in forbidden:
                if forbidden_mod in stripped:
                    violations.append(f"  ❌ {rel_path}:{line_num} -> {stripped} (forbidden: {forbidden_mod})")
    return violations


def main() -> int:
    print("=" * 60)
    print("  EdgeLite Architecture Dependency Check")
    print("=" * 60)

    all_violations = []
    for layer, forbidden in FORBIDDEN_IMPORTS.items():
        print(f"\n🔍 Checking layer: {layer}/")
        print(f"   Forbidden imports: {forbidden}")
        violations = check_layer(layer, forbidden)
        if violations:
            print(f"   ❌ Found {len(violations)} violation(s):")
            for v in violations:
                print(v)
            all_violations.extend(violations)
        else:
            print("   ✅ No violations found")

    print("\n" + "=" * 60)
    if all_violations:
        print(f"  ❌ FAIL: {len(all_violations)} architecture violation(s) found")
        print("=" * 60)
        return 1
    else:
        print("  ✅ PASS: All layers comply with dependency rules")
        print("=" * 60)
        return 0


if __name__ == "__main__":
    sys.exit(main())
