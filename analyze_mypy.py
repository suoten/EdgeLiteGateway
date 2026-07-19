"""分析 mypy 错误分布，按 error_code 和 file 统计。"""
import re
from collections import Counter, defaultdict
from pathlib import Path

content = Path("mypy_current.txt").read_text(encoding="utf-8")
lines = content.splitlines()

# 匹配 "file:line: error: message  [error-code]"
pattern = re.compile(r"^(.+?):\d+: error: .+\[(.+?)\]\s*$")

by_code = Counter()
by_file = Counter()

for line in lines:
    m = pattern.match(line)
    if m:
        file_path = m.group(1).strip()
        code = m.group(2).strip()
        by_code[code] += 1
        by_file[file_path] += 1

print("=== BY ERROR CODE ===")
for code, n in by_code.most_common():
    print(f"{n:4d}  {code}")

print(f"\n=== TOTAL: {sum(by_code.values())} errors in {len(by_file)} files ===")

print("\n=== TOP FILES (>=3 errors) ===")
for file_path, n in by_file.most_common():
    if n >= 3:
        print(f"{n:4d}  {file_path}")
