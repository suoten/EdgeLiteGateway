# EdgeLite Gateway — 端到端冒烟测试脚本 (PowerShell)
# 在 CI/CD 流水线或本地环境中执行，验证部署后核心功能可用
# 用法: powershell -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 [-BaseUrl <url>]
# 默认 BaseUrl=http://127.0.0.1:8080

param(
    [string]$BaseUrl = $env:EDGELITE_TEST_BASE
)

if (-not $BaseUrl) {
    $BaseUrl = "http://127.0.0.1:8080"
}

$TestUser = if ($env:EDGELITE_TEST_USER) { $env:EDGELITE_TEST_USER } else { "admin" }
$TestPass = $env:EDGELITE_TEST_PASS

$script:Pass = 0
$script:Fail = 0
$script:Skip = 0

function Log-Pass($msg) {
    Write-Host "✅ PASS: $msg" -ForegroundColor Green
    $script:Pass++
}

function Log-Fail($msg, $detail) {
    Write-Host "❌ FAIL: $msg — $detail" -ForegroundColor Red
    $script:Fail++
}

function Log-Skip($msg, $detail) {
    Write-Host "⏭️ SKIP: $msg — $detail" -ForegroundColor Yellow
    $script:Skip++
}

Write-Host "════════════════════════════════════════════"
Write-Host "  EdgeLite 冒烟测试"
Write-Host "  目标: $BaseUrl"
Write-Host "════════════════════════════════════════════"
Write-Host ""

# ── 1. 健康检查 ──────────────────────────────────────────────
Write-Host "── 健康检查 ──"

# 1a. Liveness probe
try {
    $response = Invoke-RestMethod -Uri "$BaseUrl/health/live" -Method Get -TimeoutSec 5 -ErrorAction Stop
    if ($response.status) {
        Log-Pass "GET /health/live"
    } else {
        Log-Fail "GET /health/live" "响应格式错误"
        exit 1
    }
} catch {
    Log-Fail "GET /health/live" "端点不可达: $_"
    exit 1
}

# 1b. Readiness probe
try {
    $null = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec 10 -ErrorAction Stop
    Log-Pass "GET /health"
} catch {
    Log-Skip "GET /health" "完整健康检查超时（非阻塞）"
}

# ── 2. 认证流程 ──────────────────────────────────────────────
Write-Host ""
Write-Host "── 认证流程 ──"

$token = $null

if (-not $TestPass) {
    Log-Skip "POST /api/auth/login" "未设置 EDGELITE_TEST_PASS"
} else {
    try {
        $body = @{ username = $TestUser; password = $TestPass } | ConvertTo-Json
        $loginResponse = Invoke-RestMethod -Uri "$BaseUrl/api/auth/login" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10 -ErrorAction Stop
        if ($loginResponse.data.access_token) {
            Log-Pass "POST /api/auth/login"
            $token = $loginResponse.data.access_token
        } else {
            Log-Fail "POST /api/auth/login" "响应中无 access_token"
        }
    } catch {
        Log-Fail "POST /api/auth/login" "登录失败: $_"
    }
}

# ── 3. 核心 API 端点 ─────────────────────────────────────────
Write-Host ""
Write-Host "── 核心 API 端点 ──"

$headers = @{}
if ($token) {
    $headers["Authorization"] = "Bearer $token"
}

$endpoints = @(
    @{ Name = "GET /api/devices"; Path = "/api/devices" },
    @{ Name = "GET /api/drivers"; Path = "/api/drivers" },
    @{ Name = "GET /api/rules"; Path = "/api/rules" }
)

foreach ($ep in $endpoints) {
    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl$($ep.Path)" -Method Get -Headers $headers -TimeoutSec 5 -ErrorAction Stop
        Log-Pass $ep.Name
    } catch {
        if (-not $token) {
            Log-Skip $ep.Name "无认证 token"
        } else {
            Log-Fail $ep.Name "端点不可达: $_"
        }
    }
}

# ── 4. 监控指标 ──────────────────────────────────────────────
Write-Host ""
Write-Host "── 监控指标 ──"

try {
    $metrics = Invoke-WebRequest -Uri "$BaseUrl/metrics" -Method Get -TimeoutSec 5 -ErrorAction Stop
    if ($metrics.Content -match "edgelite|http_requests|python_info") {
        Log-Pass "GET /metrics (Prometheus)"
    } else {
        Log-Fail "GET /metrics" "指标格式不符合 Prometheus 规范"
    }
} catch {
    Log-Fail "GET /metrics" "Prometheus 指标端点不可达: $_"
}

# ── 5. API 文档 ──────────────────────────────────────────────
Write-Host ""
Write-Host "── API 文档 ──"

try {
    $docs = Invoke-WebRequest -Uri "$BaseUrl/docs" -Method Get -TimeoutSec 5 -ErrorAction Stop
    if ($docs.Content -match "swagger|openapi") {
        Log-Pass "GET /docs (Swagger UI)"
    } else {
        Log-Skip "GET /docs" "Swagger UI 不可达（可能生产环境已禁用）"
    }
} catch {
    Log-Skip "GET /docs" "Swagger UI 不可达（可能生产环境已禁用）"
}

try {
    $null = Invoke-RestMethod -Uri "$BaseUrl/openapi.json" -Method Get -TimeoutSec 5 -ErrorAction Stop
    Log-Pass "GET /openapi.json"
} catch {
    Log-Fail "GET /openapi.json" "OpenAPI 规范不可达: $_"
}

# ── 结果汇总 ──────────────────────────────────────────────────
Write-Host ""
Write-Host "════════════════════════════════════════════"
Write-Host "  冒烟测试结果: $script:Pass passed, $script:Fail failed, $script:Skip skipped"
Write-Host "════════════════════════════════════════════"

if ($script:Fail -gt 0) {
    Write-Host "❌ 冒烟测试失败" -ForegroundColor Red
    exit 1
} else {
    Write-Host "✅ 冒烟测试通过" -ForegroundColor Green
    exit 0
}
