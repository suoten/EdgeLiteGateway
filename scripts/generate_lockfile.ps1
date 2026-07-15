# 生成依赖锁定文件（requirements.lock）- PowerShell 版本
# 使用 pip-compile 生成带哈希值的锁定文件，确保构建可复现
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts/generate_lockfile.ps1
#
# 生成后使用:
#   pip install --require-hashes -r requirements.lock
#
# 前置条件:
#   pip install pip-tools

$ErrorActionPreference = "Stop"

Write-Host "==> 安装 pip-tools..." -ForegroundColor Cyan
pip install --quiet pip-tools

Write-Host "==> 生成 requirements.lock（带哈希值）..." -ForegroundColor Cyan
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt

Write-Host "==> 生成 requirements-dev.lock（含开发依赖）..." -ForegroundColor Cyan
pip-compile --generate-hashes --output-file=requirements-dev.lock requirements.txt --extra dev

Write-Host "✅ 锁定文件生成完成" -ForegroundColor Green
Write-Host "   - requirements.lock: 生产依赖（带哈希）" -ForegroundColor Gray
Write-Host "   - requirements-dev.lock: 开发依赖（带哈希）" -ForegroundColor Gray
Write-Host ""
Write-Host "安装锁定依赖:" -ForegroundColor Yellow
Write-Host "  pip install --require-hashes -r requirements.lock"