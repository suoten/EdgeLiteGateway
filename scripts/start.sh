#!/usr/bin/env bash
# EdgeLite Gateway 启动脚本 (Linux/macOS)
# 用法: ./scripts/start.sh [--dev|--prod]
#
# --dev:  开发模式 (DEV_MODE=true, 自动生成密钥, 启用调试)
# --prod: 生产模式 (DEV_MODE=false, 必须配置密钥)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

MODE="${1:---dev}"

case "$MODE" in
  --dev)
    export DEV_MODE="${DEV_MODE:-true}"
    echo "🚀 Starting EdgeLite in DEVELOPMENT mode..."
    ;;
  --prod)
    export DEV_MODE="${DEV_MODE:-false}"
    if [ -z "${EDGELITE_SECURITY__SECRET_KEY:-}" ]; then
      echo "❌ ERROR: EDGELITE_SECURITY__SECRET_KEY must be set in production mode"
      exit 1
    fi
    if [ -z "${EDGELITE_MASTER_KEY:-}" ]; then
      echo "❌ ERROR: EDGELITE_MASTER_KEY must be set in production mode"
      exit 1
    fi
    echo "🚀 Starting EdgeLite in PRODUCTION mode..."
    ;;
  *)
    echo "Usage: $0 [--dev|--prod]"
    echo "  --dev   Development mode (default)"
    echo "  --prod  Production mode (requires secret keys)"
    exit 1
    ;;
esac

# Load .env if exists
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
  echo "📄 Loaded .env"
fi

# Ensure data directories exist
mkdir -p data/logs data/backups data/ota data/scada

# Set defaults
export EDGELITE_SERVER__HOST="${EDGELITE_SERVER__HOST:-127.0.0.1}"
export EDGELITE_SERVER__PORT="${EDGELITE_SERVER__PORT:-8080}"

echo "   Host: $EDGELITE_SERVER__HOST"
echo "   Port: $EDGELITE_SERVER__PORT"
echo "   Mode: $DEV_MODE"
echo ""

# Start the application
exec python -m edgelite
