"""运行 mypy 并保存输出到文件。"""
import subprocess

result = subprocess.run(
    ["python", "-m", "mypy", "--config-file", "mypy.ini", "src/edgelite"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    cwd=r"e:\硕腾网络\PyGBSentry\EdgeLite\EdgeLite-v1.0-Community",
)

output = result.stdout + result.stderr
with open("mypy_current.txt", "w", encoding="utf-8") as f:
    f.write(output)

lines = output.splitlines()
print(f"Exit code: {result.returncode}")
print(f"Total lines: {len(lines)}")
if lines:
    print(f"Last 3 lines:")
    for line in lines[-3:]:
        print(f"  {line}")
