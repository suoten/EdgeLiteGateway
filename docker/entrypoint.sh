#!/bin/sh
set -e

if [ ! -f configs/config.yaml ] && [ -f configs/config.example.yaml ]; then
    # 7#修复: configs 在生产 compose 中已改为只读挂载（:ro），cp 会失败导致 set -e 退出。
    # 仅在 configs 可写时（如 dev compose）自动复制；只读场景要求宿主机侧预先准备好 config.yaml
    if [ -w configs ]; then
        cp configs/config.example.yaml configs/config.yaml
        echo "[entrypoint] configs/config.yaml created from config.example.yaml"
    else
        echo "[entrypoint] WARNING: configs/config.yaml missing and configs is read-only; please create it on the host (e.g. cp configs/config.example.yaml configs/config.yaml) before starting the container"
    fi
fi

# Ensure data and logs directories are writable
mkdir -p data/backups data/ota logs

# 6#修复: Dockerfile 第42行已 `USER appuser`，容器以非 root 用户启动，
# 此处 `id -u == 0` 判断永远为 false，原 chown 逻辑失效。
# 由于 docker-compose 通过 bind mount 将宿主机 ../data、../logs 挂载到 /app/data、/app/logs，
# 容器内 chown 也无法改变宿主机目录属主（即便有 root）。
# 正确做法：宿主机侧预先修正挂载目录权限，例如：
#   sudo chown -R 1000:1000 data logs    # 1000 为镜像内 appuser 的默认 uid:gid
#   sudo chmod -R u+rwX data logs
# 镜像内 /app/data、/app/logs 已在 Dockerfile 构建阶段由 root 完成 chown（见 Dockerfile 第35-36行）。

# FIXED-P2: 启动前执行数据库迁移，失败时终止启动而非静默跳过
if command -v alembic >/dev/null 2>&1; then
    if ! alembic upgrade head 2>&1; then
        echo "[entrypoint] FATAL: alembic migration failed, aborting startup"
        exit 1
    fi
fi

# Check for graceful restart marker
if [ -f data/ota/graceful_restart.json ]; then
    echo "[entrypoint] Graceful restart marker detected, upgrade was applied"
    echo "[entrypoint] Starting with updated code..."
fi

exec "$@"
