"""500/404 歼灭修复验证测试

覆盖：
1. 500 修复：helper 函数 DB 异常返回 503 而非 500；驱动/AI RuntimeError 转 503
2. 404 修复：15 个新后端模块可导入且 router prefix 正确
3. 404 修复：追加端点存在（alarms/batch-ack, ota/status, ota/cancel, data/downsample, data/export POST, data/import POST）
4. 404 修复：前端路径对齐（resource-shares, ota）
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


# ═══════════════════════════════════════════════════════════════════════════
# Part 1: 500 修复 — helper 函数 DB 异常返回 503
# ═══════════════════════════════════════════════════════════════════════════


def _make_user(role="admin", user_id="u1", username="admin"):
    return {"user_id": user_id, "username": username, "role": role}


@pytest.mark.asyncio
async def test_check_device_owner_db_error_returns_503():
    """500修复: data.py _check_device_owner DB 异常应返回 503 而非 500"""
    from edgelite.api.data import _check_device_owner

    user = _make_user(role="viewer", user_id="u2")
    device = {"device_id": "d1", "created_by": "other"}

    # _check_device_owner 内部通过 `from edgelite.app import _app_state` 访问
    # 必须 patch edgelite.app._app_state 而非 edgelite.api.data._app_state
    fake_state = MagicMock()
    fake_state.device_service.get_device = AsyncMock(return_value=device)
    fake_state.database = MagicMock()
    fake_state.database.write_lock = MagicMock()
    with patch("edgelite.app._app_state", fake_state), \
         patch("edgelite.storage.sqlite_repo.ResourceShareRepo") as MockRepo:
        mock_repo = MockRepo.return_value
        mock_repo.check_user_has_access = AsyncMock(side_effect=RuntimeError("DB locked"))
        with pytest.raises(HTTPException) as exc:
            await _check_device_owner("d1", user)
        assert exc.value.status_code == 503
        assert exc.value.detail == "ERR_COMMON_DB_NOT_READY"


@pytest.mark.asyncio
async def test_check_alarm_device_access_db_error_returns_503():
    """500修复: alarms.py _check_alarm_device_access DB 异常应返回 503"""
    from edgelite.api.alarms import _check_alarm_device_access

    user = _make_user(role="viewer", user_id="u2")

    fake_state = MagicMock()
    fake_state.device_service.get_device = AsyncMock(side_effect=RuntimeError("DB error"))
    fake_state.database = MagicMock()
    fake_state.database.write_lock = MagicMock()
    with patch("edgelite.app._app_state", fake_state):
        with pytest.raises(HTTPException) as exc:
            await _check_alarm_device_access("d1", user)
        assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_get_accessible_device_ids_db_error_returns_503():
    """500修复: alarms.py _get_accessible_device_ids_for_alarms DB 异常应返回 503"""
    from edgelite.api.alarms import _get_accessible_device_ids_for_alarms

    user = _make_user(role="viewer", user_id="u2")

    fake_state = MagicMock()
    fake_state.device_service.list_device_ids_by_owner = AsyncMock(
        side_effect=RuntimeError("DB locked")
    )
    with patch("edgelite.app._app_state", fake_state):
        with pytest.raises(HTTPException) as exc:
            await _get_accessible_device_ids_for_alarms(user)
        assert exc.value.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════
# Part 2: 404 修复 — 15 个新后端模块可导入且 router prefix 正确
# ═══════════════════════════════════════════════════════════════════════════

NEW_MODULES = [
    ("edgelite.api.shadow", "/api/v1/shadows"),
    ("edgelite.api.db_monitor", "/api/v1/db-monitor"),
    ("edgelite.api.log_aggregation", "/api/v1/logs"),
    ("edgelite.api.anomaly_learner", "/api/v1/anomaly-learner"),
    ("edgelite.api.trend_learner", "/api/v1/trend-learner"),
    ("edgelite.api.threshold_learner", "/api/v1/threshold-learner"),
    ("edgelite.api.protocol_bridge", "/api/v1/bridge"),
    ("edgelite.api.profiler", "/api/v1/profiler"),
    ("edgelite.api.observability", "/api/v1/observability"),
    ("edgelite.api.config_version", "/api/v1/config"),
    ("edgelite.api.scripts", "/api/v1/scripts"),
    ("edgelite.api.simulation", "/api/v1/simulation"),
    ("edgelite.api.data_quality", "/api/v1/data-quality"),
    ("edgelite.api.firmware_signature", "/api/v1/firmware"),
    ("edgelite.api.device_linkage", "/api/v1/linkage"),
]


@pytest.mark.parametrize("module_path,expected_prefix", NEW_MODULES)
def test_new_module_importable_and_prefix_correct(module_path, expected_prefix):
    """404修复: 每个新模块可导入且 router prefix 与前端调用对齐"""
    import importlib

    mod = importlib.import_module(module_path)
    assert hasattr(mod, "router"), f"{module_path} 缺少 router 属性"
    assert mod.router.prefix == expected_prefix, (
        f"{module_path} prefix={mod.router.prefix!r} 期望 {expected_prefix!r}"
    )
    route_paths = [r.path for r in mod.router.routes]
    assert len(route_paths) > 0, f"{module_path} 无任何路由"


# ═══════════════════════════════════════════════════════════════════════════
# Part 3: 404 修复 — 追加端点存在性验证
# ═══════════════════════════════════════════════════════════════════════════


def _router_path_methods(router):
    """提取 router 中所有 (path, method) 对，path 含完整 prefix"""
    result = {}
    for r in router.routes:
        if hasattr(r, "methods") and hasattr(r, "path"):
            result.setdefault(r.path, set()).update(r.methods)
    return result


def test_alarms_batch_ack_endpoint_exists():
    """404修复: alarms.py 包含 POST /batch-ack 端点"""
    from edgelite.api.alarms import router

    pm = _router_path_methods(router)
    assert "/api/v1/alarms/batch-ack" in pm, "alarms 缺少 /batch-ack"
    assert "POST" in pm["/api/v1/alarms/batch-ack"], "alarms /batch-ack 不是 POST"


def test_ota_status_and_cancel_endpoints_exist():
    """404修复: ota.py 包含 GET /status 和 POST /cancel 端点"""
    from edgelite.api.ota import router

    pm = _router_path_methods(router)
    assert "/api/v1/ota/status" in pm, "ota 缺少 /status"
    assert "GET" in pm["/api/v1/ota/status"], "ota /status 不是 GET"
    assert "/api/v1/ota/cancel" in pm, "ota 缺少 /cancel"
    assert "POST" in pm["/api/v1/ota/cancel"], "ota /cancel 不是 POST"


def test_data_downsample_endpoint_exists():
    """404修复: data.py 包含 POST /downsample 端点"""
    from edgelite.api.data import router

    pm = _router_path_methods(router)
    assert "/api/v1/data/downsample" in pm, "data 缺少 /downsample"
    assert "POST" in pm["/api/v1/data/downsample"], "data /downsample 不是 POST"


def test_data_export_import_post_endpoints_exist():
    """404修复: data.py 包含 POST /export 和 POST /import 端点（与原 GET /export 共存）"""
    from edgelite.api.data import router

    pm = _router_path_methods(router)
    assert "POST" in pm.get("/api/v1/data/export", set()), "data 缺少 POST /export"
    assert "POST" in pm.get("/api/v1/data/import", set()), "data 缺少 POST /import"


def test_ai_models_enhanced_endpoints_exist():
    """404修复: ai_models.py 包含 ai_enhanced 端点（ab-test, hot-swap, cache 等）"""
    from edgelite.api.ai_models import router

    pm = _router_path_methods(router)
    expected = [
        "/api/v1/ai/ab-test",
        "/api/v1/ai/hot-swap",
        "/api/v1/ai/cache/stats",
        "/api/v1/ai/cache/clear",
        "/api/v1/ai/resources",
        "/api/v1/ai/devices",
        "/api/v1/ai/batch/stats",
        "/api/v1/ai/preprocess/steps",
        "/api/v1/ai/postprocess/steps",
    ]
    for p in expected:
        assert p in pm, f"ai_models 缺少 {p}"


# ═══════════════════════════════════════════════════════════════════════════
# Part 4: 应用整体路由注册验证
# ═══════════════════════════════════════════════════════════════════════════


def test_all_new_modules_registered_in_app():
    """404修复: create_app() 后所有新模块路由可访问

    FastAPI 0.110+ 中 include_router 会将子 router 包成 _IncludedRouter 实例，
    需通过 original_router 属性解包才能拿到子 APIRouter 的 routes。
    """
    from edgelite.app import create_app

    app = create_app()
    all_paths = set()
    for route in app.routes:
        # 解包 _IncludedRouter → 拿到原始 APIRouter
        inner = getattr(route, "original_router", None) or route
        # 直接挂在 app 上的 APIRoute/APIWebSocketRoute/Mount 有 path
        if hasattr(route, "path"):
            all_paths.add(route.path)
        # 解包后的 APIRouter.routes 含具体子路由
        if hasattr(inner, "routes"):
            for sub in inner.routes:
                if hasattr(sub, "path"):
                    all_paths.add(sub.path)

    key_paths = [
        "/api/v1/shadows",
        "/api/v1/db-monitor/pool-stats",
        "/api/v1/logs/query",
        "/api/v1/anomaly-learner/initialize",
        "/api/v1/bridge/list",
        "/api/v1/profiler/stats",
        "/api/v1/observability/overview",
        "/api/v1/config/versions",
        "/api/v1/scripts/list",
        "/api/v1/simulation/types",
        "/api/v1/data-quality/trend",
        "/api/v1/firmware/verify/signature",
        "/api/v1/linkage/rules",
        "/api/v1/alarms/batch-ack",
        "/api/v1/ota/status",
        "/api/v1/ota/cancel",
        "/api/v1/data/downsample",
    ]
    missing = [p for p in key_paths if p not in all_paths]
    assert not missing, f"以下关键路径未注册: {missing}"


# ═══════════════════════════════════════════════════════════════════════════
# Part 5: 全局异常处理器验证（通过源码检查）
# ═══════════════════════════════════════════════════════════════════════════


def test_global_exception_handler_exists_and_unified():
    """500修复: app.py 包含全局异常处理器，返回统一格式且不泄露堆栈"""
    import inspect
    from edgelite import app as app_module

    # 验证 create_app 函数源码中包含全局异常处理器
    source = inspect.getsource(app_module.create_app)
    assert "global_exception_handler" in source, "create_app 缺少 global_exception_handler"
    assert "CommonErrors.INTERNAL_ERROR" in source, "全局处理器未使用 INTERNAL_ERROR 错误码"
    assert "status_code=500" in source, "全局处理器未返回 500"
    # 验证 ApiResponse 格式
    assert "ApiResponse(code=500" in source or "code=500" in source, "全局处理器未使用统一 ApiResponse"


def test_validation_handler_returns_422_unified():
    """500修复: app.py 包含 Pydantic 校验异常处理器，返回 422 统一格式"""
    import inspect
    from edgelite import app as app_module

    source = inspect.getsource(app_module.create_app)
    assert "validation_exception_handler" in source, "缺少 validation_exception_handler"
    assert "RequestValidationError" in source, "未捕获 RequestValidationError"
    assert "422" in source, "校验异常未返回 422"


# ═══════════════════════════════════════════════════════════════════════════
# Part 6: 500 修复 — 设备写入/AI 推理驱动异常转 503
# ═══════════════════════════════════════════════════════════════════════════


def test_device_write_runtime_error_returns_503():
    """500修复: 设备写入时驱动 RuntimeError 应转为 503"""
    import inspect
    from edgelite.api.devices import write_device_point

    source = inspect.getsource(write_device_point)
    assert "RuntimeError" in source, "write_device_point 未捕获 RuntimeError"
    assert "503" in source, "write_device_point 未将 RuntimeError 转为 503"


def test_device_probe_runtime_error_returns_503():
    """500修复: 设备探测时驱动 RuntimeError 应转为 503"""
    import inspect
    from edgelite.api.devices import probe_primary_link

    source = inspect.getsource(probe_primary_link)
    assert "RuntimeError" in source, "probe_primary_link 未捕获 RuntimeError"
    assert "503" in source, "probe_primary_link 未将 RuntimeError 转为 503"


def test_ai_inference_runtime_error_returns_503():
    """500修复: AI 推理 RuntimeError 应转为 503"""
    import inspect
    from edgelite.api.ai_models import inference

    source = inspect.getsource(inference)
    assert "RuntimeError" in source, "inference 未捕获 RuntimeError"
    assert "503" in source, "inference 未将 RuntimeError 转为 503"


def test_alarms_silence_import_error_returns_503():
    """500修复: alarms /silence 端点在 alarm_silence 模块未加载时返回 503 而非 500"""
    import inspect
    from edgelite.api.alarms import list_alarm_silences

    source = inspect.getsource(list_alarm_silences)
    assert "ImportError" in source, "list_alarm_silences 未捕获 ImportError"
    assert "AttributeError" in source, "list_alarm_silences 未捕获 AttributeError"
    assert "503" in source, "list_alarm_silences 未将 ImportError 转为 503"


def test_alarms_correlation_import_error_returns_503():
    """500修复: alarms /correlation 端点在 alarm_correlation 模块未加载时返回 503"""
    import inspect
    from edgelite.api.alarms import get_alarm_correlations

    source = inspect.getsource(get_alarm_correlations)
    assert "ImportError" in source, "get_alarm_correlations 未捕获 ImportError"
    assert "AttributeError" in source, "get_alarm_correlations 未捕获 AttributeError"
    assert "503" in source, "get_alarm_correlations 未将 ImportError 转为 503"


def test_data_stats_service_not_ready_returns_503():
    """500修复: data /stats 端点在设备服务未就绪时返回 503 而非 500"""
    import inspect
    from edgelite.api.data import get_collect_stats

    source = inspect.getsource(get_collect_stats)
    assert "AttributeError" in source, "get_collect_stats 未捕获 AttributeError"
    assert "RuntimeError" in source, "get_collect_stats 未捕获 RuntimeError"
    assert "503" in source, "get_collect_stats 未将服务未就绪异常转为 503"


# ═══════════════════════════════════════════════════════════════════════════
# Part 7: 前端路径对齐验证
# ═══════════════════════════════════════════════════════════════════════════


def _read_frontend_index():
    """读取前端 index.ts 内容"""
    import os
    frontend_index = os.path.join(
        os.path.dirname(__file__), "..", "web", "src", "api", "index.ts"
    )
    if not os.path.exists(frontend_index):
        pytest.skip("前端 index.ts 不存在")
    with open(frontend_index, encoding="utf-8") as f:
        return f.read()


def test_frontend_no_app_update_api_call():
    """404修复: 前端 index.ts 不再调用 /app-update/* 路径（注释除外）"""
    import re
    content = _read_frontend_index()
    # 实际调用形式：http.get('/app-update/...') 或 http.post('/app-update/...')
    # 兼容 TypeScript 泛型：http.get<ApiResponse<any>>('/app-update/...')
    api_call_pattern = r"http\.(get|post|put|delete)(?:<[^>]*>)?\(\s*['\"`]/app-update/"
    matches = re.findall(api_call_pattern, content)
    assert not matches, f"前端仍存在 /app-update/ API 调用: {matches}"
    # 验证已对齐 /ota/（直接 /ota/ 或 URL.OTA.xxx）
    ota_pattern = r"http\.(get|post)(?:<[^>]*>)?\(\s*(?:URL\.OTA\.|['\"`]/ota/)"
    assert re.search(ota_pattern, content), "前端未对齐 /ota/ 路径"


def test_frontend_no_resources_api_call():
    """404修复: 前端 index.ts 不再调用 /resources/share 等错误路径"""
    import re
    content = _read_frontend_index()
    api_call_pattern = r"http\.(get|post|put|delete)\(\s*['\"`]/resources/(share|unshare|shares|transfer)"
    matches = re.findall(api_call_pattern, content)
    assert not matches, f"前端仍存在 /resources/* API 调用: {matches}"


def test_frontend_self_test_disabled():
    """404修复: 前端 selfTest/acceptanceReport 不再调用后端"""
    import re
    content = _read_frontend_index()
    api_call_pattern = r"http\.(get|post)\(\s*['\"`]/self-test/"
    matches = re.findall(api_call_pattern, content)
    assert not matches, f"前端仍调用 /self-test/ API: {matches}"
