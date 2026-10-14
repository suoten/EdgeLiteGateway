#!/usr/bin/env python3
"""运行验收脚本的启动器（处理环境变量设置）。"""

import os
import subprocess
import sys

# 设置环境变量
env = os.environ.copy()
env["PF_URL"] = "http://127.0.0.1:8000/api/v1"
env["PF_USER"] = "admin"
env["PF_PASS"] = "admin"
env["EL_URL"] = "http://127.0.0.1:8180/api/v1"
env["EL_USER"] = "admin"
# FIXED(安全-P2): 口令不再硬编码，必须通过环境变量注入（缺省报错退出）
el_pass = os.environ.get("EL_PASS", "")
if not el_pass:
    print(
        "ERROR: environment variable EL_PASS is required (EdgeLite admin password for acceptance testing)",
        file=sys.stderr,
    )
    raise SystemExit(1)
env["EL_PASS"] = el_pass

script = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "joint_acceptance.py",
)

cmd = [sys.executable, script, "--report", "joint_acceptance_report.json"]
print(f"Running: {' '.join(cmd)}")
print(f"Environment: PF_URL={env['PF_URL']} EL_URL={env['EL_URL']}")
print()

result = subprocess.run(cmd, env=env, cwd=os.path.dirname(script))
print(f"\nExit code: {result.returncode}")
sys.exit(result.returncode)
