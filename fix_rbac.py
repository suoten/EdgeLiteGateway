"""RBAC 绕过漏洞修复脚本

将所有 = require_permission(...) 改为 = Depends(require_permission(...))
并确保 Depends 已从 fastapi 导入
"""
from __future__ import annotations

import os
import re

API_DIR = os.path.join(os.path.dirname(__file__), "src", "edgelite", "api")

# 匹配 = require_permission(参数)  其中参数不含嵌套括号
PATTERN = re.compile(r"= require_permission\(([^)]+)\)")


def ensure_depends_import(content: str) -> str:
    """确保文件导入了 Depends。返回修改后的内容。"""
    # 已有 Depends 导入（任意形式）
    if re.search(r"\bDepends\b", content.split("\n\n")[0]):
        # 进一步确认是 import 语境
        if re.search(r"from fastapi import[^\n]*\bDepends\b", content) or re.search(
            r"from fastapi import\s*\([^)]*\bDepends\b", content, re.DOTALL
        ):
            return content

    # 处理单行 import: from fastapi import A, B, C
    m = re.search(r"^from fastapi import ([^\n(]+)$", content, re.MULTILINE)
    if m:
        imports = m.group(1).strip()
        if "Depends" not in imports:
            # 添加 Depends，保持字母序不太重要，加在最前
            new_imports = f"Depends, {imports}"
            content = content.replace(
                f"from fastapi import {imports}",
                f"from fastapi import {new_imports}",
                1,
            )
            return content

    # 处理多行 import: from fastapi import (\n    A,\n    B,\n)
    m = re.search(r"from fastapi import \(([^)]+)\)", content, re.DOTALL)
    if m:
        block = m.group(1)
        if "Depends" not in block:
            # 在括号内第一行添加 Depends
            new_block = "    Depends,\n" + block
            content = content.replace(m.group(0), f"from fastapi import ({new_block})", 1)
            return content

    # 如果文件没有 from fastapi import 行，添加一个
    # 找到第一个 import 或 from 行
    lines = content.split("\n")
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.startswith("from ") or line.startswith("import "):
            insert_idx = i
            break
    lines.insert(insert_idx, "from fastapi import Depends")
    return "\n".join(lines)


def process_file(fpath: str) -> int:
    """处理单个文件，返回替换数量。"""
    with open(fpath, encoding="utf-8") as f:
        content = f.read()

    matches = PATTERN.findall(content)
    if not matches:
        return 0

    count = len(matches)
    new_content = PATTERN.sub(r"= Depends(require_permission(\1))", content)

    # 确保导入了 Depends
    new_content = ensure_depends_import(new_content)

    if new_content != content:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_content)

    return count


def main() -> None:
    total = 0
    file_count = 0
    for root, _dirs, files in os.walk(API_DIR):
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            n = process_file(fpath)
            if n > 0:
                rel = os.path.relpath(fpath, API_DIR)
                print(f"  {rel}: {n} replacements")
                total += n
                file_count += 1

    print(f"\nTotal: {total} replacements in {file_count} files")


if __name__ == "__main__":
    main()
