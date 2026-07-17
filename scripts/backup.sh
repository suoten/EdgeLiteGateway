#!/bin/bash
# EdgeLite Gateway — 数据备份脚本
# 用法: bash scripts/backup.sh [backup_dir]
# 默认备份目录: data/backups/
#
# 备份内容:
#   1. SQLite 数据库 (edgelite.db)
#   2. SQLite 时序数据库 (edgelite_ts.db)
#   3. 配置文件 (configs/)
#   4. 证书文件 (data/certs/)
#
# 恢复: bash scripts/restore.sh <backup_file>

set -euo pipefail

BACKUP_DIR="${1:-data/backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="edgelite_backup_${TIMESTAMP}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"

echo "📦 EdgeLite 数据备份"
echo "   备份目录: ${BACKUP_PATH}"
echo ""

mkdir -p "${BACKUP_PATH}"

# ── 1. SQLite 数据库 ──────────────────────────────────────────────────
for db_file in data/edgelite.db data/edgelite_ts.db; do
    if [ -f "$db_file" ]; then
        echo "📋 备份 ${db_file}..."
        # 使用 SQLite 的 .backup 命令确保一致性
        sqlite3 "$db_file" ".backup '${BACKUP_PATH}/$(basename $db_file)'" 2>/dev/null || \
            cp "$db_file" "${BACKUP_PATH}/$(basename $db_file)"
        echo "   ✅ 完成"
    fi
done

# ── 2. 配置文件 ───────────────────────────────────────────────────────
if [ -d "configs" ]; then
    echo "📋 备份 configs/..."
    cp -r configs "${BACKUP_PATH}/configs"
    echo "   ✅ 完成"
fi

# ── 3. 证书文件 ───────────────────────────────────────────────────────
if [ -d "data/certs" ]; then
    echo "📋 备份 data/certs/..."
    cp -r data/certs "${BACKUP_PATH}/certs"
    echo "   ✅ 完成"
fi

# ── 4. 创建压缩包 ─────────────────────────────────────────────────────
ARCHIVE="${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
echo "📦 压缩备份..."
tar -czf "$ARCHIVE" -C "$BACKUP_DIR" "$BACKUP_NAME"
rm -rf "${BACKUP_PATH}"
echo "   ✅ 完成: ${ARCHIVE}"

# ── 5. 清理旧备份（保留最近 7 个）────────────────────────────────────
echo "🧹 清理旧备份..."
cd "$BACKUP_DIR"
ls -t edgelite_backup_*.tar.gz 2>/dev/null | tail -n +8 | while read old_file; do
    rm -f "$old_file"
    echo "   🗑️  删除: $old_file"
done

echo ""
echo "✅ 备份完成: ${ARCHIVE}"
echo "   大小: $(du -h "$ARCHIVE" | cut -f1)"
