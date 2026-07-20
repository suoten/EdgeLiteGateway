"""分析 mypy 错误分布，按 error_code 和 file 统计。"""
import re
from collections import Counter, defaultdict
from pathlib import Path

content = Path("mypy_current.txt").read_text(encoding="utf-8")
lines = content.splitlines()

# 匹配 "file:line: error: message  [error-code]"
pattern = re.compile(r"^(.+?):\d+: error: .+\[(.+?)\]\s*$")

by_code = Counter()
by_file = defaultdict(list)

for line in lines:
    m = pattern.match(line)
    if m:
        file_path = m.group(1).strip()
        code = m.group(2).strip()
        by_code[code] += 1
        by_file[file_path].append(code)

print("=== BY ERROR CODE ===")
for code, n in by_code.most_common():
    print(f"{n:4d}  {code}")

print(f"\n=== TOTAL: {sum(by_code.values())} errors in {len(by_file)} files ===")

print("\n=== ALL FILES WITH ERRORS ===")
for file_path, codes in sorted(by_file.items(), key=lambda x: -len(x[1])):
    codes_str = ", ".join(codes)
    print(f"{len(codes):2d}  {file_path}  [{codes_str}]")
