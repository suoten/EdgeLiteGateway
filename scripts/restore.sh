#!/bin/bash
# EdgeLite Gateway — 数据恢复脚本
# 用法: bash scripts/restore.sh <backup_file>
# 示例: bash scripts/restore.sh data/backups/edgelite_backup_20260717_120000.tar.gz

set -euo pipefail

BACKUP_FILE="${1:-}"
if [ -z "$BACKUP_FILE" ]; then
    echo "❌ 用法: bash scripts/restore.sh <backup_file>"
    echo "   示例: bash scripts/restore.sh data/backups/edgelite_backup_20260717_120000.tar.gz"
    exit 1
fi

if [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ 备份文件不存在: $BACKUP_FILE"
    exit 1
fi

TMP_DIR=$(mktemp -d)
echo "📦 EdgeLite 数据恢复"
echo "   备份文件: ${BACKUP_FILE}"
echo "   临时目录: ${TMP_DIR}"
echo ""

# ── 解压备份 ───────────────────────────────────────────────────────────
echo "📂 解压备份..."
tar -xzf "$BACKUP_FILE" -C "$TMP_DIR"
BACKUP_DIR=$(find "$TMP_DIR" -maxdepth 1 -type d -name "edgelite_backup_*" | head -1)
if [ -z "$BACKUP_DIR" ]; then
    echo "❌ 无法找到备份数据目录"
    rm -rf "$TMP_DIR"
    exit 1
fi
echo "   ✅ 完成"

# ── 1. 恢复 SQLite 数据库 ─────────────────────────────────────────────
for db_file in "$BACKUP_DIR"/*.db; do
    if [ -f "$db_file" ]; then
        db_name=$(basename "$db_file")
        target="data/$db_name"
        echo "📋 恢复 ${db_name} → ${target}..."
        # 备份当前数据库（如果存在）
        if [ -f "$target" ]; then
            mv "$target" "${target}.pre_restore.$(date +%s)"
        fi
        cp "$db_file" "$target"
        echo "   ✅ 完成"
    fi
done

# ── 2. 恢复配置文件 ───────────────────────────────────────────────────
if [ -d "$BACKUP_DIR/configs" ]; then
    echo "📋 恢复 configs/..."
    cp -r "$BACKUP_DIR/configs/"* configs/ 2>/dev/null || true
    echo "   ✅ 完成"
fi

# ── 3. 恢复证书文件 ───────────────────────────────────────────────────
if [ -d "$BACKUP_DIR/certs" ]; then
    echo "📋 恢复 data/certs/..."
    mkdir -p data/certs
    cp -r "$BACKUP_DIR/certs/"* data/certs/ 2>/dev/null || true
    echo "   ✅ 完成"
fi

# ── 清理 ──────────────────────────────────────────────────────────────
rm -rf "$TMP_DIR"

echo ""
echo "✅ 恢复完成！请重启 EdgeLite 服务使更改生效。"
echo "   docker compose -f docker/docker-compose.yml restart edgelite"
