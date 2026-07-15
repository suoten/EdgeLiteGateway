#!/bin/bash
# 生成依赖锁定文件（requirements.lock）
# 使用 pip-compile 生成带哈希值的锁定文件，确保构建可复现
#
# 用法:
#   bash scripts/generate_lockfile.sh
#
# 生成后使用:
#   pip install --require-hashes -r requirements.lock
#
# 前置条件:
#   pip install pip-tools

set -e

echo "==> 安装 pip-tools..."
pip install --quiet pip-tools

echo "==> 生成 requirements.lock（带哈希值）..."
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt

echo "==> 生成 requirements-dev.lock（含开发依赖）..."
pip-compile --generate-hashes --output-file=requirements-dev.lock requirements.txt --extra dev

echo "✅ 锁定文件生成完成"
echo "   - requirements.lock: 生产依赖（带哈希）"
echo "   - requirements-dev.lock: 开发依赖（带哈希）"
echo ""
echo "安装锁定依赖:"
echo "  pip install --require-hashes -r requirements.lock"
