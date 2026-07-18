#!/usr/bin/env bash
# EdgeLite Gateway — 端到端冒烟测试脚本
# 在 CI/CD 流水线中执行，验证部署后核心功能可用
# 用法: bash scripts/smoke_test.sh [BASE_URL]
# 默认 BASE_URL=http://127.0.0.1:8080

set -euo pipefail

BASE_URL="${1:-${EDGELITE_TEST_BASE:-http://127.0.0.1:8080}}"
TEST_USER="${EDGELITE_TEST_USER:-admin}"
TEST_PASS="${EDGELITE_TEST_PASS:-}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PASS=0
FAIL=0
SKIP=0

log_pass() { echo -e "${GREEN}✅ PASS${NC}: $1"; ((PASS++)); }
log_fail() { echo -e "${RED}❌ FAIL${NC}: $1 — $2"; ((FAIL++)); }
log_skip() { echo -e "${YELLOW}⏭️ SKIP${NC}: $1 — $2"; ((SKIP++)); }

echo "════════════════════════════════════════════"
echo "  EdgeLite 冒烟测试"
echo "  目标: $BASE_URL"
echo "════════════════════════════════════════════"
echo ""

# ── 1. 健康检查端点 ──────────────────────────────────────────────────────
echo "── 健康检查 ──"

# 1a. Liveness probe (轻量)
if curl -sf --max-time 5 "$BASE_URL/health/live" | grep -q '"status"'; then
    log_pass "GET /health/live"
else
    log_fail "GET /health/live" "端点不可达或响应格式错误"
    exit 1
fi

# 1b. Readiness probe (完整检查)
HEALTH_RESPONSE=$(curl -sf --max-time 10 "$BASE_URL/health" 2>/dev/null || echo "")
if [ -n "$HEALTH_RESPONSE" ]; then
    log_pass "GET /health"
else
    log_skip "GET /health" "完整健康检查超时或不可达（非阻塞）"
fi

# ── 2. 认证流程 ──────────────────────────────────────────────────────────
echo ""
echo "── 认证流程 ──"

TOKEN=""

if [ -z "$TEST_PASS" ]; then
    log_skip "POST /api/auth/login" "未设置 EDGELITE_TEST_PASS"
else
    LOGIN_RESPONSE=$(curl -sf --max-time 10 -X POST "$BASE_URL/api/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"$TEST_USER\",\"password\":\"$TEST_PASS\"}" 2>/dev/null || echo "")

    if echo "$LOGIN_RESPONSE" | grep -q "access_token"; then
        log_pass "POST /api/auth/login"
        TOKEN=$(echo "$LOGIN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('access_token',''))" 2>/dev/null || echo "")
    else
        log_fail "POST /api/auth/login" "登录失败或响应格式错误"
    fi
fi

# ── 3. 核心 API 端点可达性 ───────────────────────────────────────────────
echo ""
echo "── 核心 API 端点 ──"

AUTH_HEADER=""
if [ -n "$TOKEN" ]; then
    AUTH_HEADER="-H \"Authorization: Bearer $TOKEN\""
fi

# 设备列表
if curl -sf --max-time 5 $AUTH_HEADER "$BASE_URL/api/devices" >/dev/null 2>&1; then
    log_pass "GET /api/devices"
else
    if [ -z "$TOKEN" ]; then
        log_skip "GET /api/devices" "无认证 token"
    else
        log_fail "GET /api/devices" "端点不可达"
    fi
fi

# 驱动列表
if curl -sf --max-time 5 $AUTH_HEADER "$BASE_URL/api/drivers" >/dev/null 2>&1; then
    log_pass "GET /api/drivers"
else
    if [ -z "$TOKEN" ]; then
        log_skip "GET /api/drivers" "无认证 token"
    else
        log_fail "GET /api/drivers" "端点不可达"
    fi
fi

# 规则列表
if curl -sf --max-time 5 $AUTH_HEADER "$BASE_URL/api/rules" >/dev/null 2>&1; then
    log_pass "GET /api/rules"
else
    if [ -z "$TOKEN" ]; then
        log_skip "GET /api/rules" "无认证 token"
    else
        log_fail "GET /api/rules" "端点不可达"
    fi
fi

# ── 4. 指标端点 ──────────────────────────────────────────────────────────
echo ""
echo "── 监控指标 ──"

if curl -sf --max-time 5 "$BASE_URL/metrics" | grep -q "edgelite\|http_requests\|python_info"; then
    log_pass "GET /metrics (Prometheus)"
else
    log_fail "GET /metrics" "Prometheus 指标端点不可达"
fi

# ── 5. API 文档 ──────────────────────────────────────────────────────────
echo ""
echo "── API 文档 ──"

if curl -sf --max-time 5 "$BASE_URL/docs" | grep -q "swagger\|openapi"; then
    log_pass "GET /docs (Swagger UI)"
else
    log_skip "GET /docs" "Swagger UI 不可达（可能生产环境已禁用）"
fi

if curl -sf --max-time 5 "$BASE_URL/openapi.json" | grep -q "openapi"; then
    log_pass "GET /openapi.json"
else
    log_fail "GET /openapi.json" "OpenAPI 规范不可达"
fi

# ── 结果汇总 ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo -e "  冒烟测试结果: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, ${YELLOW}$SKIP skipped${NC}"
echo "════════════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}❌ 冒烟测试失败${NC}"
    exit 1
else
    echo -e "${GREEN}✅ 冒烟测试通过${NC}"
    exit 0
fi
