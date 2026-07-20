"""Prometheus 指标端点 - /metrics

FIXED: Added access control requiring either JWT token with SYSTEM_READ permission
or valid X-API-Key with METRICS_READ permission.
FIXED-H02: API Key 不再隐式授予 admin 角色，改为绑定最小必要权限。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

from edgelite.api.error_codes import AuthErrors, AuthzErrors, CommonErrors, DeviceErrors

# FIXED: Import Permission for role-based access control
from edgelite.security.rbac import (
    APIKeyPermission,
    Permission,
    has_permission,
)

_API_KEY_HEADER = "X-API-Key"

# FIXED(P1): 原问题-RUF006 create_task 返回值未保存，task 可能被 GC 回收;
#            修复-模块级 _background_tasks 集合保存引用，任务完成时自动移除
_background_tasks: set[asyncio.Task] = set()


def _log_api_key_usage(request: Request, key_name: str, success: bool, action: str = "metrics_access") -> None:
    """Log API Key usage for audit trail."""
    try:
        from edgelite.app import _app_state

        audit_svc = getattr(_app_state, "audit_service", None)
        if audit_svc:
            client_ip = request.client.host if request.client else "unknown"
            try:
                from edgelite.services.audit_service import AuditAction

                # FIXED(P1): 原问题-RUF006 create_task 返回值未保存，task 可能被 GC 回收;
                #            修复-保存到模块级 _background_tasks 集合
                task = asyncio.create_task(
                    audit_svc.log(
                        getattr(AuditAction, "API_KEY_USED", "api_key_used"),
                        resource_type="api_key",
                        resource_id=key_name,
                        ip_address=client_ip,
                        after_value={"success": success, "action": action},
                    )
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.debug("审计日志记录API Key使用失败: %s", e)
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("API Key使用审计日志记录失败: %s", e)


# FIXED-H02: Authentication dependency supporting both JWT and API Key with scoped permissions
async def _authenticate_metrics(request: Request) -> dict[str, str]:
    """Authenticate metrics endpoint access.

    Supports two authentication methods:
    1. JWT Bearer token with SYSTEM_READ permission
    2. Valid X-API-Key with METRICS_READ permission (not admin role)

    Returns:
        Dict with authentication info (user_id, username, role, or auth_type for API key)

    Raises:
        HTTPException: 401 if authentication fails, 403 if permission denied
    """
    # Check for Bearer token
    auth_header = request.headers.get("Authorization", "")
    # LP-02: Cookie fallback - 当 Authorization header 缺失时，从 HttpOnly Cookie 提取 token
    if not auth_header.startswith("Bearer "):
        cookie_token = request.cookies.get("edgelite_access", "")
        if cookie_token:
            auth_header = f"Bearer {cookie_token}"
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            # FIXED(严重): 原问题-使用decode_token不检查token撤销和session状态;
            # 修复-改用verify_token完整校验
            from edgelite.security.jwt import verify_token

            payload = verify_token(token, token_type="access")
            username = payload.get("username", "")
            user_role = payload.get("role", "")

            # FIXED(致命): 原问题-verify_token 成功后未检查 jti 是否已撤销，
            # 已登出用户的 token 在过期前仍可访问 metrics 端点;
            # 修复-添加 jti 撤销检查，与 health.py 第258-262行实现保持一致
            jti = payload.get("jti", "")
            if jti:
                from edgelite.security.token_revocation import is_token_revoked

                if is_token_revoked(jti):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail=AuthErrors.TOKEN_REVOKED,
                    )

            # Verify user still exists and is enabled
            try:
                from edgelite.app import _app_state
                from edgelite.storage.sqlite_repo import UserRepo

                db = _app_state.database
                if db:
                    async with db.get_session() as session:
                        repo = UserRepo(session, db.write_lock)
                        user = await repo.get_by_username(username)
                    if user is None or not user.get("enabled"):
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=AuthErrors.USER_DISABLED,
                        )
                    user_role = user.get("role", user_role)

                    # FIXED-P1: 检查Token是否在密码修改之前签发，与deps.py保持一致
                    # 之前：metrics端点不检查密码修改后token失效，已改密用户仍可用旧token访问
                    # 之后：与标准API端点一致，密码修改后旧token立即失效
                    token_iat = payload.get("iat")
                    password_changed_at = user.get("password_changed_at")
                    if token_iat and password_changed_at:
                        from datetime import datetime

                        if isinstance(password_changed_at, str):
                            try:
                                pwd_changed_ts = datetime.fromisoformat(password_changed_at).timestamp()
                            except (ValueError, TypeError):
                                pwd_changed_ts = 0
                        elif isinstance(password_changed_at, datetime):
                            pwd_changed_ts = password_changed_at.timestamp()
                        else:
                            pwd_changed_ts = 0
                        if token_iat < pwd_changed_ts:
                            raise HTTPException(
                                status_code=status.HTTP_401_UNAUTHORIZED,
                                detail=AuthErrors.TOKEN_PASSWORD_CHANGED,
                            )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning("Metrics auth: failed to verify user: %s", e)

            # Check SYSTEM_READ permission
            if not has_permission(user_role, Permission.SYSTEM_READ):
                logger.warning(
                    "Metrics access denied: user=%s role=%s ip=%s - insufficient permissions",
                    username,
                    user_role,
                    request.client.host if request.client else "unknown",
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=AuthzErrors.PERMISSION_DENIED,
                )

            return {
                "user_id": payload.get("sub", ""),
                "username": username,
                "role": user_role,
                "auth_type": "jwt",
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Metrics auth: JWT validation failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=AuthErrors.TOKEN_INVALID,
            ) from e  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e

    # Check for API Key with METRICS_READ permission (FIXED-H02: no longer grants admin role)
    api_key = request.headers.get(_API_KEY_HEADER)
    if api_key:
        try:
            from edgelite.config import get_config

            config = get_config()
            if config is None:
                logger.warning("Metrics auth: config not available")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=CommonErrors.SERVICE_NOT_READY,
                )

            # FIXED-H02: Check API Key with scoped permissions instead of admin role
            valid_key_info = None

            # Check Grafana API key with METRICS_READ permission
            if hasattr(config, "grafana") and config.grafana:
                grafana_api_key = getattr(config.grafana, "api_key", "") or ""
                if grafana_api_key and hmac.compare_digest(api_key, grafana_api_key):
                    valid_key_info = {
                        "auth_type": "api_key",
                        "username": "grafana",
                        "key_name": "grafana.api_key",
                        "scope": APIKeyPermission.METRICS_READ,
                    }

            # Check Video API key with METRICS_READ permission
            if valid_key_info is None and hasattr(config, "video") and hasattr(config.video, "pygbsentry"):
                video_api_key = getattr(config.video.pygbsentry, "api_key", "") or ""
                if video_api_key and hmac.compare_digest(api_key, video_api_key):
                    valid_key_info = {
                        "auth_type": "api_key",
                        "username": "video",
                        "key_name": "video.pygbsentry.api_key",
                        "scope": APIKeyPermission.METRICS_READ,
                    }

            # Check server webhook API key (FIXED-H02: should not have METRICS_READ by default)
            if valid_key_info is None and hasattr(config, "server"):
                server_api_key = getattr(config.server, "webhook_api_key", "") or ""
                if server_api_key and hmac.compare_digest(api_key, server_api_key):
                    # FIXED-H02: webhook API key 默认只有 DEVICE_PUSH 权限，不应访问 metrics
                    logger.warning(
                        "Metrics auth: webhook API key attempted to access metrics (ip=%s)",
                        request.client.host if request.client else "unknown",
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=AuthzErrors.PERMISSION_DENIED,
                    )

            if valid_key_info is None:
                logger.warning(
                    "Metrics auth: invalid API key from ip=%s", request.client.host if request.client else "unknown"
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=DeviceErrors.API_KEY_INVALID,
                )

            # Log successful API Key usage
            _log_api_key_usage(request, valid_key_info["key_name"], success=True, action="metrics_access")

            # FIXED-H02: Return with scoped permissions, not admin role
            return {
                "auth_type": "api_key",
                "username": valid_key_info["username"],
                "role": "api_key",  # Not "admin" anymore
                "api_key_scope": valid_key_info["scope"],
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Metrics auth: API key validation failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=AuthErrors.AUTH_FAILED,
            ) from e  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from e

    # No authentication provided
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=AuthErrors.AUTH_REQUIRED,
        headers={"WWW-Authenticate": "Bearer"},
    )


MetricsAuth = Depends(_authenticate_metrics)

# ── prometheus_client 优雅降级 ──
try:
    from prometheus_client import (  # noqa: I001
        Counter,
        Gauge,
        Histogram,
        CollectorRegistry,
        generate_latest,
    )

    _PROMETHEUS_CLIENT_AVAILABLE = True
except ImportError:
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]
    CollectorRegistry = None  # type: ignore[assignment,misc]
    generate_latest = None  # type: ignore[assignment,misc]
    _PROMETHEUS_CLIENT_AVAILABLE = False

router = APIRouter(prefix="/api/v1", tags=["Metrics"])


# ── prometheus_client 指标定义（仅当库可用时初始化） ──

# 类型标注为 Any，避免 pyright reportPossiblyUnboundVariable 误报
_registry: Any = None
_devices_total: Any = None
_devices_online: Any = None
_collect_total: Any = None
_collect_errors: Any = None
_collect_duration_seconds: Any = None
_rules_active: Any = None
_alarms_firing: Any = None
_mqtt_forward_total: Any = None
_mqtt_forward_errors: Any = None
_influxdb_fallback_mode: Any = None
_driver_read_total: Any = None
_driver_read_errors: Any = None
_driver_write_total: Any = None
_driver_write_errors: Any = None
_driver_reconnect_total: Any = None
# 可观测性-5xx 计数器：所有 HTTP 5xx 响应自动累加，便于 Prometheus 告警
_http_requests_total: Any = None
_http_requests_5xx_total: Any = None
_http_request_duration_seconds: Any = None


def _init_prometheus_client_metrics() -> None:
    """初始化 prometheus_client 指标（仅当库可用时调用）"""
    global _registry, _devices_total, _devices_online, _collect_total, _collect_errors
    global _collect_duration_seconds, _rules_active, _alarms_firing
    global _mqtt_forward_total, _mqtt_forward_errors, _influxdb_fallback_mode
    global _driver_read_total, _driver_read_errors, _driver_write_total, _driver_write_errors, _driver_reconnect_total
    global _http_requests_total, _http_requests_5xx_total, _http_request_duration_seconds

    assert _PROMETHEUS_CLIENT_AVAILABLE  # 仅当 prometheus_client 可用时调用
    assert CollectorRegistry is not None
    assert Gauge is not None
    assert Counter is not None
    assert Histogram is not None

    _registry = CollectorRegistry()

    # 1. 设备总数
    _devices_total = Gauge(
        "edgelite_devices_total",
        "Total number of registered devices",
        ["protocol"],
        registry=_registry,
    )
    # 2. 在线设备数
    _devices_online = Gauge(
        "edgelite_devices_online",
        "Number of online devices",
        registry=_registry,
    )
    # 3. 采集总次数
    _collect_total = Counter(
        "edgelite_collect_total",
        "Total number of data collections",
        [],  # FIXED-P1: 移除device_id高基数标签，改为全局聚合，防止Prometheus指标爆炸
        registry=_registry,
    )
    # 4. 采集错误次数
    _collect_errors = Counter(
        "edgelite_collect_errors",
        "Total number of collection errors",
        [],  # FIXED-P1: 移除device_id高基数标签，改为全局聚合
        registry=_registry,
    )
    # 5. 采集耗时
    _collect_duration_seconds = Histogram(
        "edgelite_collect_duration_seconds",
        "Data collection duration in seconds",
        ["device_id"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        registry=_registry,
    )
    # 6. 活跃规则数
    _rules_active = Gauge(
        "edgelite_rules_active",
        "Number of active alarm rules",
        registry=_registry,
    )
    # 7. 当前告警数
    _alarms_firing = Gauge(
        "edgelite_alarms_firing",
        "Number of currently firing alarms",
        ["severity"],
        registry=_registry,
    )
    # 8. MQTT 转发总次数
    _mqtt_forward_total = Counter(
        "edgelite_mqtt_forward_total",
        "Total number of MQTT message forwards",
        registry=_registry,
    )
    # 9. MQTT 转发错误次数
    _mqtt_forward_errors = Counter(
        "edgelite_mqtt_forward_errors",
        "Total number of MQTT forward errors",
        registry=_registry,
    )
    # 10. InfluxDB 降级模式
    _influxdb_fallback_mode = Gauge(
        "edgelite_influxdb_fallback_mode",
        "InfluxDB fallback mode (1=using fallback, 0=normal)",
        registry=_registry,
    )
    _driver_read_total = Counter(
        "edgelite_driver_read_total",
        "Total number of driver read operations",
        ["driver"],  # FIXED-P1: 移除device_id高基数标签，按驱动类型聚合
        registry=_registry,
    )
    _driver_read_errors = Counter(
        "edgelite_driver_read_errors",
        "Total number of driver read errors",
        ["driver"],  # FIXED-P1: 移除device_id高基数标签，按驱动类型聚合
        registry=_registry,
    )
    _driver_write_total = Counter(
        "edgelite_driver_write_total",
        "Total number of driver write operations",
        ["driver"],  # FIXED-P1: 移除device_id高基数标签，按驱动类型聚合
        registry=_registry,
    )
    _driver_write_errors = Counter(
        "edgelite_driver_write_errors",
        "Total number of driver write errors",
        ["driver"],  # FIXED-P1: 移除device_id高基数标签，按驱动类型聚合
        registry=_registry,
    )
    _driver_reconnect_total = Counter(
        "edgelite_driver_reconnect_total",
        "Total number of driver reconnection attempts",
        ["driver"],
        registry=_registry,
    )
    # 可观测性: HTTP 请求计数器（按方法/路径/状态码），路径已归一化避免高基数
    _http_requests_total = Counter(
        "edgelite_http_requests_total",
        "Total HTTP requests by method, path template and status code",
        ["method", "path", "status_code"],
        registry=_registry,
    )
    # 可观测性: 5xx 响应计数器（按方法/路径），方便 Prometheus 告警规则
    # 仅 5xx 单独计数，便于 alert: rate(edgelite_http_requests_5xx_total[5m]) > 0
    _http_requests_5xx_total = Counter(
        "edgelite_http_requests_5xx_total",
        "Total HTTP 5xx responses by method and path (server errors)",
        ["method", "path"],
        registry=_registry,
    )
    # 可观测性: HTTP 请求耗时直方图（按方法/路径），便于 P50/P95/P99 延迟监控
    _http_request_duration_seconds = Histogram(
        "edgelite_http_request_duration_seconds",
        "HTTP request duration in seconds by method and path",
        ["method", "path"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        registry=_registry,
    )


def record_http_request(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    """记录一次 HTTP 请求的指标（5xx 计数 + 总计数 + 延迟）。

    供 app.py 中的中间件 / 异常处理器调用。对路径做归一化：
    - 路径参数替换为 {param}（避免高基数）
    - 查询字符串剥离
    - 长度截断到 200 字符以内

    若 prometheus_client 不可用，则记到自定义 PrometheusExporter。
    """
    # 路径归一化：剥离查询字符串 + 路径参数替换
    normalized = path.split("?", 1)[0].split("#", 1)[0]
    # 将 /api/v1/devices/123 转为 /api/v1/devices/{id}
    import re

    normalized = re.sub(r"/\d+(?=/|$)", "/{id}", normalized)
    # UUID 路径参数
    normalized = re.sub(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]+(?=/|$)", "/{uuid}", normalized)
    # 长度截断
    if len(normalized) > 200:
        normalized = normalized[:197] + "..."

    try:
        if _PROMETHEUS_CLIENT_AVAILABLE and _http_requests_total is not None:
            _http_requests_total.labels(method=method, path=normalized, status_code=str(status_code)).inc()
            if status_code >= 500:
                _http_requests_5xx_total.labels(method=method, path=normalized).inc()
            if _http_request_duration_seconds is not None:
                _http_request_duration_seconds.labels(method=method, path=normalized).observe(duration_seconds)
        else:
            # 降级到自定义导出器
            exporter = get_exporter()
            exporter.counter(
                "edgelite_http_requests_total",
                1,
                {"method": method, "path": normalized, "status_code": str(status_code)},
                "Total HTTP requests by method, path and status code",
            )
            if status_code >= 500:
                exporter.counter(
                    "edgelite_http_requests_5xx_total",
                    1,
                    {"method": method, "path": normalized},
                    "Total HTTP 5xx responses by method and path",
                )
            exporter.histogram(
                "edgelite_http_request_duration_seconds",
                duration_seconds,
                {"method": method, "path": normalized},
                "HTTP request duration in seconds",
            )
    except Exception as e:
        # 指标采集失败不应影响请求本身，仅 debug 日志
        logger.debug("record_http_request failed: %s", e)


if _PROMETHEUS_CLIENT_AVAILABLE:
    _init_prometheus_client_metrics()


def _escape_label(s: str) -> str:
    """转义Prometheus标签值"""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_metric(
    name: str,
    labels: dict[str, str],
    value: float,
    timestamp: float | None = None,
    metric_type: str = "gauge",
) -> str:
    """格式化Prometheus指标"""
    label_str = ""
    if labels:
        label_parts = [f'{k}="{_escape_label(str(v))}"' for k, v in labels.items()]
        label_str = "{" + ",".join(label_parts) + "}"

    line = f"{name}{label_str} {value}"
    if timestamp is not None:
        line += f" {int(timestamp * 1000)}"
    return line


class PrometheusExporter:
    """Prometheus指标导出器"""

    def __init__(self):
        self._metrics: dict[str, dict[str, Any]] = {}
        # FIXED(严重): 添加线程锁保护 _metrics，避免后台采集循环与请求处理并发调用时
        # counter/gauge 的读-改-写非原子操作导致数据错乱，以及 render() 遍历时
        # 并发修改抛出 RuntimeError
        self._lock = threading.Lock()

    def gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        description: str = "",
    ) -> None:
        """设置Gauge指标"""
        # FIXED(严重): 加锁保护读写操作，防止并发修改
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = {"type": "gauge", "description": description, "values": {}}
            self._metrics[name]["values"][self._labels_key(labels or {})] = value

    def counter(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        description: str = "",
    ) -> None:
        """增加Counter指标"""
        # R8-S-08: Prometheus Counter 语义要求单调递增，拒绝负值避免计数器回退
        if value < 0:
            value = 0
        # FIXED(严重): 加锁保护读-改-写操作，防止并发累加导致计数丢失或错乱
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = {"type": "counter", "description": description, "values": {}}
            key = self._labels_key(labels or {})
            current = self._metrics[name]["values"].get(key, 0.0)
            self._metrics[name]["values"][key] = current + value

    def histogram(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        description: str = "",
        buckets: tuple[float, ...] = (
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1.0,
            2.5,
            5.0,
            10.0,
        ),
    ) -> None:
        """记录Histogram指标"""
        # FIXED(严重): 加锁保护读写操作，防止并发修改导致统计错乱
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = {
                    "type": "histogram",
                    "description": description,
                    "values": {},
                    "buckets": buckets,
                }

            key = self._labels_key(labels or {})
            if key not in self._metrics[name]["values"]:
                self._metrics[name]["values"][key] = {
                    "sum": 0.0,
                    "count": 0,
                    "buckets": {b: 0 for b in buckets},
                }

            entry = self._metrics[name]["values"][key]
            entry["sum"] += value
            entry["count"] += 1
            for b in buckets:
                if value <= b:
                    entry["buckets"][b] += 1

    def summary(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        description: str = "",
        quantiles: tuple[float, ...] = (0.5, 0.9, 0.95, 0.99),
    ) -> None:
        """记录Summary指标"""
        # FIXED(严重): 加锁保护读写操作，防止并发修改导致统计错乱
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = {
                    "type": "summary",
                    "description": description,
                    "values": {},
                    "quantiles": quantiles,
                }

            key = self._labels_key(labels or {})
            if key not in self._metrics[name]["values"]:
                self._metrics[name]["values"][key] = {"sum": 0.0, "count": 0, "values": []}

            entry = self._metrics[name]["values"][key]
            entry["sum"] += value
            entry["count"] += 1
            entry["values"].append(value)

    def reset_gauge(self, name: str, labels: dict[str, str] | None = None) -> None:
        """重置指定 Gauge 指标（用于后台采集时清理旧值）"""
        if name in self._metrics:
            key = self._labels_key(labels or {})
            self._metrics[name]["values"].pop(key, None)

    def reset_gauge_all(self, name: str) -> None:
        """重置指定 Gauge 指标的所有标签组合"""
        if name in self._metrics:
            self._metrics[name]["values"].clear()

    @staticmethod
    def _labels_key(labels: dict[str, str]) -> str:
        """生成标签组合的唯一键"""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def render(self) -> str:
        """渲染Prometheus格式文本"""
        # FIXED(严重): 加锁保护遍历操作，防止并发修改 _metrics 导致 RuntimeError
        with self._lock:
            output = []
            timestamp = time.time()

            for name, metric in self._metrics.items():
                metric_type = metric["type"]
                description = metric.get("description", "")

                # HELP行
                if description:
                    output.append(f"# HELP {name} {description}")

                # TYPE行
                output.append(f"# TYPE {name} {metric_type}")

                if metric_type == "gauge" or metric_type == "counter":
                    for labels_key, value in metric["values"].items():
                        labels = self._parse_labels(labels_key)
                        output.append(_format_metric(name, labels, value, timestamp))

                elif metric_type == "histogram":
                    buckets = metric.get("buckets", ())
                    for labels_key, data in metric["values"].items():
                        labels = self._parse_labels(labels_key)
                        for b in buckets:
                            output.append(
                                _format_metric(
                                    f"{name}_bucket",
                                    {**labels, "le": str(b)},
                                    data["buckets"].get(b, 0),
                                    timestamp,
                                )
                            )
                        output.append(
                            _format_metric(
                                f"{name}_bucket",
                                {**labels, "le": "+Inf"},
                                data["count"],
                                timestamp,
                            )
                        )
                        output.append(_format_metric(f"{name}_sum", labels, data["sum"], timestamp))
                        output.append(_format_metric(f"{name}_count", labels, data["count"], timestamp))

                elif metric_type == "summary":
                    quantiles = metric.get("quantiles", ())
                    for labels_key, data in metric["values"].items():
                        labels = self._parse_labels(labels_key)
                        sorted_values = sorted(data["values"])
                        count = len(sorted_values)
                        for q in quantiles:
                            idx = int(q * count) if count > 0 else 0
                            idx = min(idx, count - 1) if count > 0 else 0
                            quantile_value = sorted_values[idx] if sorted_values else 0.0
                            output.append(
                                _format_metric(
                                    f"{name}",
                                    {**labels, "quantile": str(q)},
                                    quantile_value,
                                    timestamp,
                                )
                            )
                        output.append(_format_metric(f"{name}_sum", labels, data["sum"], timestamp))
                        output.append(_format_metric(f"{name}_count", labels, data["count"], timestamp))

            return "\n".join(output) + "\n"

    @staticmethod
    def _parse_labels(labels_key: str) -> dict[str, str]:
        """解析标签键为字典"""
        labels: dict[str, str] = {}
        if not labels_key:
            return labels
        for pair in labels_key.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels[k.strip()] = v.strip()
        return labels


# 全局指标导出器实例
_exporter: PrometheusExporter | None = None


def get_exporter() -> PrometheusExporter:
    """获取全局指标导出器"""
    global _exporter
    if _exporter is None:
        _exporter = PrometheusExporter()
    return _exporter


# ── 指标收集函数 ──


def _collect_system_metrics(exporter: PrometheusExporter) -> None:
    """收集系统指标"""
    import psutil

    # CPU指标
    # FIXED-P0: 原问题-psutil.cpu_percent(interval=0.1)是阻塞调用(100ms)，在async端点中直接调用会阻塞事件循环
    # 改为interval=None返回上次调用以来的瞬时CPU使用率，避免阻塞
    cpu_percent = psutil.cpu_percent(interval=None)
    exporter.gauge(
        "edgelite_system_cpu_usage_percent",
        cpu_percent,
        {"core": "total"},
        "System CPU usage percent",
    )

    for i, percent in enumerate(psutil.cpu_percent(interval=None, percpu=True)):
        exporter.gauge("edgelite_system_cpu_usage_percent", percent, {"core": str(i)})

    # 内存指标
    mem = psutil.virtual_memory()
    exporter.gauge(
        "edgelite_system_memory_total_bytes",
        mem.total,
        description="System memory total in bytes",
    )
    exporter.gauge("edgelite_system_memory_available_bytes", mem.available)
    exporter.gauge("edgelite_system_memory_used_bytes", mem.used)
    exporter.gauge("edgelite_system_memory_usage_percent", mem.percent)

    # 磁盘指标
    try:
        # FIXED-P1: 原问题-Windows上psutil.disk_usage("/")可能失败，使用系统盘根目录
        import os

        disk_path = os.path.abspath(os.sep)  # Windows: "C:\", Linux: "/"
        disk = psutil.disk_usage(disk_path)
        exporter.gauge(
            "edgelite_system_disk_total_bytes",
            disk.total,
            description="System disk total in bytes",
        )
        exporter.gauge("edgelite_system_disk_free_bytes", disk.free)
        exporter.gauge("edgelite_system_disk_used_bytes", disk.used)
        exporter.gauge("edgelite_system_disk_usage_percent", disk.percent)
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("Disk metrics collection failed: %s", e)

    # 网络指标
    net = psutil.net_io_counters()
    exporter.gauge(
        "edgelite_system_network_bytes_total",
        net.bytes_recv + net.bytes_sent,
        description="Total network bytes transferred",
    )
    exporter.gauge(
        "edgelite_system_network_packets_total",
        net.packets_recv + net.packets_sent,
    )

    # Uptime
    try:
        from edgelite.app import _app_state

        start_time = getattr(_app_state, "start_time", None)
        if start_time:
            exporter.gauge(
                "edgelite_system_uptime_seconds",
                time.time() - start_time,
                description="System uptime in seconds",
            )
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("系统运行时间指标采集失败: %s", e)


async def _collect_device_metrics(exporter: PrometheusExporter) -> None:  # FIXED-P0: 改为async以支持await
    """收集设备指标 - edgelite_devices_total, edgelite_devices_online"""
    try:
        from edgelite.app import _app_state

        if not hasattr(_app_state, "scheduler") or not _app_state.scheduler:
            return

        scheduler = _app_state.scheduler
        stats = await scheduler.get_collect_stats()
        # FIXED-P0: get_active_devices改为async，需await
        active_devices = await scheduler.get_active_devices()

        # 按协议统计设备
        protocol_counts: dict[str, int] = {}
        online_count = 0
        offline_count = 0

        driver_registry = getattr(_app_state, "driver_registry", None)
        if driver_registry and hasattr(driver_registry, "list_drivers"):
            try:
                for driver_name in driver_registry.list_drivers():
                    driver = driver_registry.get_driver(driver_name)
                    if driver and hasattr(driver, "get_all_health_stats"):
                        health_stats = driver.get_all_health_stats()
                        protocol_counts[driver_name] = len(health_stats)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.debug("驱动健康统计获取失败: %s", e)

        # 设备总数（按协议分label）
        total_devices = len(active_devices)
        for protocol, count in protocol_counts.items():
            exporter.gauge(
                "edgelite_devices_total",
                count,
                {"protocol": protocol},
                "Total devices by protocol",
            )
        if not protocol_counts:
            exporter.gauge(
                "edgelite_devices_total",
                total_devices,
                {"protocol": "unknown"},
                "Total devices by protocol",
            )

        # 在线/离线设备数
        for device_id in active_devices:
            stat = stats.get(device_id)
            if stat and getattr(stat, "timeout_count", 0) == 0:
                online_count += 1
            else:
                offline_count += 1

        exporter.gauge(
            "edgelite_devices_online",
            online_count,
            description="Number of online devices",
        )
        exporter.gauge(
            "edgelite_devices_offline",
            offline_count,
            description="Number of offline devices",
        )

        # 设备采集统计
        for device_id, stat in stats.items():
            labels = {"device_id": device_id}
            exporter.gauge("edgelite_collect_avg_latency_ms", stat.avg_latency_ms, labels)
            exporter.gauge("edgelite_collect_max_latency_ms", stat.max_latency_ms, labels)
            exporter.gauge("edgelite_collect_total_calls", stat.total_calls, labels)
            exporter.gauge("edgelite_collect_timeout_count", stat.timeout_count, labels)

    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("设备指标采集失败: %s", e)


async def _collect_points_metrics(exporter: PrometheusExporter) -> None:  # FIXED-P0: 改为async以支持await
    """收集数据点指标 - edgelite_points_collected_total, edgelite_collection_errors_total"""
    try:
        from edgelite.app import _app_state

        scheduler = getattr(_app_state, "scheduler", None)
        if not scheduler:
            return

        stats = await scheduler.get_collect_stats()
        total_points = 0
        total_errors = 0

        for _device_id, stat in stats.items():
            total_calls = getattr(stat, "total_calls", 0)
            timeout_count = getattr(stat, "timeout_count", 0)
            total_points += total_calls
            total_errors += timeout_count

        exporter.gauge(
            "edgelite_points_collected_total",
            total_points,
            description="Total data points collected",
        )
        exporter.gauge(
            "edgelite_collection_errors_total",
            total_errors,
            description="Total collection errors",
        )
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("数据点指标采集失败: %s", e)


def _collect_alarm_metrics(exporter: PrometheusExporter) -> None:
    """收集告警指标 - edgelite_alarms_active"""
    try:
        from edgelite.app import _app_state

        alarm_service = getattr(_app_state, "alarm_service", None)
        if not alarm_service:
            return

        alarm_repo = getattr(alarm_service, "_repo", None)
        if not alarm_repo:
            return

        # 尝试获取活跃告警统计
        try:
            # 通过 alarm_repo 获取活跃告警
            if hasattr(alarm_repo, "count_active_by_severity"):
                counts = alarm_repo.count_active_by_severity()
            else:
                # 回退：使用 evaluator 的统计
                evaluator = getattr(_app_state, "evaluator", None)
                counts = (
                    getattr(evaluator, "_alarm_counts", {}) if evaluator and hasattr(evaluator, "_alarm_counts") else {}
                )
            total_active = 0
            for severity, count in counts.items():
                exporter.gauge(
                    "edgelite_alarms_active",
                    count,
                    {"severity": severity},
                    "Active alarms by severity",
                )
                total_active += count
            exporter.gauge(
                "edgelite_alarms_active",
                total_active,
                {"severity": "all"},
                "Active alarms by severity",
            )
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.debug("活跃告警统计获取失败: %s", e)

    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("告警指标采集失败: %s", e)


# FIXED(严重): 记录上次采集的 AI 累计统计值，用于计算 delta
# inference_count/error_count 是模型启动以来的累计值，而 exporter.counter() 是累加操作，
# 若每次采集都把累计值加到计数器上会导致指标值虚高 N 倍
_last_ai_stats: dict[str, dict[str, int]] = {}

# R8-F-01 修复: 记录上次采集的协议累计统计值，用于计算 delta
# 驱动 get_all_health_stats 返回的 total_reads/failed_reads 等是累计值，
# 而 exporter.counter() 是累加操作，直接传累计值会导致协议指标虚高 N 倍
_last_protocol_stats: dict[str, dict[str, float]] = {}


def _collect_ai_metrics(exporter: PrometheusExporter) -> None:
    """收集 AI 推理指标 - 推理计数、延迟、错误率"""
    try:
        from edgelite.app import _app_state

        ai_engine = getattr(_app_state, "ai_engine", None)
        if not ai_engine:
            return

        models = ai_engine.get_loaded_models() if hasattr(ai_engine, "get_loaded_models") else {}
        active_count = 0
        for model_id, wrapper in models.items():
            status = getattr(wrapper, "status", "unknown")
            if status == "active":
                active_count += 1

            # 通过 ai_engine.get_model_stats 获取详细统计
            model_stats = ai_engine.get_model_stats(model_id) if hasattr(ai_engine, "get_model_stats") else None
            inference_count = model_stats.get("call_count", 0) if model_stats else 0
            error_count = model_stats.get("error_count", 0) if model_stats else 0
            avg_latency_ms = model_stats.get("avg_latency_ms", 0) if model_stats else 0

            # FIXED(严重): 计算本次采集相对于上次的增量(delta)，仅将 delta 传给 counter()，
            # 避免每次采集都把累计值累加到计数器导致指标虚高 N 倍
            last_stats = _last_ai_stats.get(model_id, {})
            inference_delta = max(0, inference_count - last_stats.get("inference_count", 0))
            error_delta = max(0, error_count - last_stats.get("error_count", 0))
            _last_ai_stats[model_id] = {
                "inference_count": inference_count,
                "error_count": error_count,
            }

            exporter.counter(
                "edgelite_ai_inferences_total",
                inference_delta,
                {"model": model_id, "status": status},
                "AI inference count by model",
            )
            exporter.counter(
                "edgelite_ai_errors_total",
                error_delta,
                {"model": model_id},
                "AI inference error count by model",
            )
            exporter.histogram(
                "edgelite_ai_inference_latency_ms",
                avg_latency_ms,
                {"model": model_id},
                "AI inference latency in milliseconds",
            )

        # FIXED-P1: 清理已卸载模型的统计条目，防止_last_ai_stats无界增长(内存泄漏)
        stale_models = [m for m in _last_ai_stats if m not in models]
        for m in stale_models:
            _last_ai_stats.pop(m, None)

        exporter.gauge(
            "edgelite_ai_active_models",
            active_count,
            description="Number of active AI models",
        )
        exporter.gauge(
            "edgelite_ai_total_models",
            len(models),
            description="Total number of AI models",
        )
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("AI推理指标采集失败: %s", e)


async def _collect_cache_metrics(
    exporter: PrometheusExporter,
) -> None:  # FIXED-P0: 改为async以支持await influx_storage.available()/using_fallback()
    """收集缓存指标 - edgelite_cache_size, edgelite_ring_buffer_usage"""
    try:
        from edgelite.app import _app_state

        # Cache manager
        cache_manager = getattr(_app_state, "cache_manager", None)
        if cache_manager:
            cache_size = 0
            if hasattr(cache_manager, "size"):
                cache_size = cache_manager.size()
            elif hasattr(cache_manager, "_cache"):
                cache_size = len(cache_manager._cache)
            exporter.gauge(
                "edgelite_cache_size",
                cache_size,
                description="Cache queue size",
            )

        # InfluxDB ring buffer usage
        influx_storage = getattr(_app_state, "influx_storage", None)
        if influx_storage:
            # 检查 SQLite fallback 的缓冲区
            sqlite_ts = getattr(influx_storage, "_sqlite_ts", None)
            if sqlite_ts:
                buffer_size = 0
                if hasattr(sqlite_ts, "_buffer"):
                    buffer_size = len(sqlite_ts._buffer)
                elif hasattr(sqlite_ts, "pending_count"):
                    buffer_size = sqlite_ts.pending_count()
                exporter.gauge(
                    "edgelite_ring_buffer_usage",
                    buffer_size,
                    description="Ring buffer pending items count",
                )

            # InfluxDB 可用性作为 gauge
            # FIXED-P0: available是async方法，原getattr获取绑定方法对象(始终truthy)导致指标失真，改为await调用
            available = await influx_storage.available() if hasattr(influx_storage, "available") else False
            exporter.gauge(
                "edgelite_influxdb_available",
                1 if available else 0,
                description="InfluxDB availability (1=available, 0=unavailable)",
            )
            # FIXED-P0: using_fallback同为async方法，改为await调用
            using_fallback = (
                await influx_storage.using_fallback() if hasattr(influx_storage, "using_fallback") else False
            )
            exporter.gauge(
                "edgelite_influxdb_using_fallback",
                1 if using_fallback else 0,
                description="InfluxDB using SQLite fallback (1=yes, 0=no)",
            )

    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("缓存指标采集失败: %s", e)


async def _collect_protocol_metrics(exporter: PrometheusExporter) -> None:  # FIXED-P0: 改为async以支持await
    """收集协议指标 - 按协议统计请求计数、错误计数、延迟"""
    try:
        from edgelite.app import _app_state

        scheduler = getattr(_app_state, "scheduler", None)
        if not scheduler:
            return

        # 从 scheduler 的 _device_info 获取驱动实例
        device_info = getattr(scheduler, "_device_info", {})
        # 按协议聚合统计
        protocol_stats: dict[str, dict[str, float]] = {}

        for _device_id, info in device_info.items():
            if not isinstance(info, tuple) or len(info) < 1:
                continue
            driver = info[0]
            # 获取协议名称
            protocol = getattr(driver, "plugin_name", None) or getattr(driver, "__class__", type).__name__

            if protocol not in protocol_stats:
                protocol_stats[protocol] = {
                    "total_reads": 0,
                    "failed_reads": 0,
                    "total_writes": 0,
                    "failed_writes": 0,
                }

            # 从驱动实例获取健康统计
            if hasattr(driver, "get_all_health_stats"):
                try:
                    all_stats = driver.get_all_health_stats()
                    for _dev_id, health in all_stats.items():
                        protocol_stats[protocol]["total_reads"] += getattr(health, "total_reads", 0)
                        protocol_stats[protocol]["failed_reads"] += getattr(health, "failed_reads", 0)
                        protocol_stats[protocol]["total_writes"] += getattr(health, "total_writes", 0)
                        protocol_stats[protocol]["failed_writes"] += getattr(health, "failed_writes", 0)
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.debug("驱动健康统计获取失败(protocol=%s): %s", protocol, e)

        # 导出协议指标
        for protocol, stats in protocol_stats.items():
            labels = {"protocol": protocol}
            # R8-F-01 修复: 驱动返回的 total_reads/failed_reads 等是累计值，
            # 而 exporter.counter() 是累加操作，直接传累计值会导致指标虚高 N 倍。
            # 参照 _collect_ai_metrics 的 delta 模式：仅将本次采集相对上次的增量传给 counter()。
            requests_total = stats["total_reads"] + stats["total_writes"]
            errors_total = stats["failed_reads"] + stats["failed_writes"]
            last_stats = _last_protocol_stats.get(protocol, {})
            requests_delta = max(0, requests_total - last_stats.get("requests_total", 0))
            errors_delta = max(0, errors_total - last_stats.get("errors_total", 0))
            _last_protocol_stats[protocol] = {
                "requests_total": requests_total,
                "errors_total": errors_total,
            }
            exporter.counter(
                "edgelite_protocol_requests_total",
                requests_delta,
                labels,
                "Protocol total requests (reads + writes)",
            )
            exporter.counter(
                "edgelite_protocol_errors_total",
                errors_delta,
                labels,
                "Protocol total errors (failed reads + failed writes)",
            )
            exporter.gauge(
                "edgelite_protocol_reads_total",
                stats["total_reads"],
                labels,
                "Protocol total read requests",
            )
            exporter.gauge(
                "edgelite_protocol_read_errors_total",
                stats["failed_reads"],
                labels,
                "Protocol total read errors",
            )

        # FIXED-P1: 清理已移除协议的统计条目，防止_last_protocol_stats无界增长(内存泄漏)
        stale_protocols = [p for p in _last_protocol_stats if p not in protocol_stats]
        for p in stale_protocols:
            _last_protocol_stats.pop(p, None)

        # 从 scheduler 的采集统计中获取延迟数据
        collect_stats = await scheduler.get_collect_stats() if hasattr(scheduler, "get_collect_stats") else {}
        for device_id, stat in collect_stats.items():
            labels = {"device_id": device_id}
            exporter.histogram(
                "edgelite_protocol_latency_ms",
                stat.avg_latency_ms,
                labels,
                "Protocol request latency in milliseconds",
            )

    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("协议指标采集失败: %s", e)


async def _collect_all_metrics(exporter: PrometheusExporter) -> None:  # FIXED-P0: 改为async以支持await子函数
    """收集所有指标（用于后台定时采集和请求时采集）"""
    # 系统指标
    try:
        _collect_system_metrics(exporter)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("System metrics collection failed: %s", e)

    # 业务指标
    await _collect_device_metrics(exporter)
    await _collect_points_metrics(exporter)
    _collect_alarm_metrics(exporter)
    _collect_ai_metrics(exporter)
    await _collect_cache_metrics(exporter)
    await _collect_protocol_metrics(exporter)
    _collect_db_monitor_metrics(exporter)
    _collect_event_bus_metrics(exporter)


def _collect_event_bus_metrics(exporter: PrometheusExporter) -> None:
    """S-02修复: 收集事件总线指标 - 丢弃事件数、活跃 handler_loop 数、订阅者数"""
    try:
        from edgelite.app import _app_state

        event_bus = getattr(_app_state, "event_bus", None)
        if event_bus is None:
            return

        # 因队列满被丢弃的事件总数（背压监控指标）
        dropped = event_bus.get_dropped_count() if hasattr(event_bus, "get_dropped_count") else 0
        exporter.gauge(
            "edgelite_event_bus_dropped_total",
            dropped,
            description="Total events dropped due to full queue (backpressure)",
        )

        # 活跃 handler_loop 协程数量（协程泄漏监控指标）
        handler_loop_count = event_bus.get_handler_loop_count() if hasattr(event_bus, "get_handler_loop_count") else 0
        exporter.gauge(
            "edgelite_event_bus_handler_loops",
            handler_loop_count,
            description="Active event handler loop coroutines",
        )

        # 订阅者数量
        subscriber_count = len(getattr(event_bus, "_subscribers", {}))
        exporter.gauge(
            "edgelite_event_bus_subscribers",
            subscriber_count,
            description="Event bus subscriber count",
        )
    except Exception as e:
        logger.debug("事件总线指标采集失败: %s", e)


def _collect_db_monitor_metrics(exporter: PrometheusExporter) -> None:
    """Collect database monitor metrics"""
    try:
        from edgelite.services.db_monitor import get_db_monitor

        monitor = get_db_monitor()
        stats = monitor.get_pool_stats()
        exporter.gauge(
            "edgelite_db_pool_active_connections",
            stats.get("active_connections", 0),
            description="Active database connections",
        )
        exporter.gauge(
            "edgelite_db_pool_idle_connections",
            stats.get("idle_connections", 0),
            description="Idle database connections",
        )
        exporter.gauge(
            "edgelite_db_pool_waiting_count",
            stats.get("waiting_count", 0),
            description="Waiting for database connection",
        )
        exporter.gauge(
            "edgelite_db_slow_queries_total",
            monitor.get_slow_query_count(),
            description="Total slow queries detected",
        )  # FIXED(严重): 改用公共方法get_slow_query_count()，避免直接访问私有属性_slow_queries
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("数据库监控指标采集失败: %s", e)


# ── prometheus_client 指标收集 ──

# 上次采集的累积值，用于计算 Counter 增量
_last_collect_stats: dict[str, int] = {}
_last_mqtt_forward_count: int = 0
_last_mqtt_error_count: int = 0


async def _collect_prometheus_client_metrics() -> None:  # FIXED-P0: 改为async以支持await
    """使用 prometheus_client 收集业务指标（仅当库可用时调用）"""
    global _last_mqtt_forward_count, _last_mqtt_error_count

    if not _PROMETHEUS_CLIENT_AVAILABLE:
        return

    try:
        from edgelite.app import _app_state
    except Exception:
        return

    # ── 设备指标 ──
    try:
        scheduler = getattr(_app_state, "scheduler", None)
        if scheduler:
            # FIXED-P0: get_active_devices改为async，需await
            active_devices = await scheduler.get_active_devices() if hasattr(scheduler, "get_active_devices") else []
            stats = await scheduler.get_collect_stats() if hasattr(scheduler, "get_collect_stats") else {}

            # 按协议统计设备总数
            protocol_counts: dict[str, int] = {}
            driver_registry = getattr(_app_state, "driver_registry", None)
            if driver_registry and hasattr(driver_registry, "list_drivers"):
                try:
                    for driver_name in driver_registry.list_drivers():
                        driver = driver_registry.get_driver(driver_name)
                        if driver and hasattr(driver, "get_all_health_stats"):
                            health_stats = driver.get_all_health_stats()
                            protocol_counts[driver_name] = len(health_stats)
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.debug("prometheus_client驱动健康统计获取失败: %s", e)

            # 先清除旧的 label 组合，再重新设置
            _devices_total.clear()
            if protocol_counts:
                for protocol, count in protocol_counts.items():
                    _devices_total.labels(protocol=protocol).set(count)
            else:
                _devices_total.labels(protocol="unknown").set(len(active_devices))

            # 在线设备数
            online_count = 0
            for device_id in active_devices:
                stat = stats.get(device_id)
                if stat and getattr(stat, "timeout_count", 0) == 0:
                    online_count += 1
            _devices_online.set(online_count)

            # 采集总次数 & 错误次数 & 耗时（Counter 用增量，Histogram 用 observe）
            for device_id, stat in stats.items():
                total_calls = getattr(stat, "total_calls", 0)
                timeout_count = getattr(stat, "timeout_count", 0)
                avg_latency_ms = getattr(stat, "avg_latency_ms", 0)

                # 计算 Counter 增量
                stat_key = f"collect:{device_id}"
                prev_calls = _last_collect_stats.get(f"{stat_key}:calls", 0)
                prev_errors = _last_collect_stats.get(f"{stat_key}:errors", 0)
                delta_calls = max(0, total_calls - prev_calls)
                delta_errors = max(0, timeout_count - prev_errors)
                _last_collect_stats[f"{stat_key}:calls"] = total_calls
                _last_collect_stats[f"{stat_key}:errors"] = timeout_count

                if delta_calls > 0:
                    _collect_total.inc(delta_calls)  # FIXED-P1: 已移除device_id标签，直接inc
                if delta_errors > 0:
                    _collect_errors.inc(delta_errors)  # FIXED-P1: 已移除device_id标签，直接inc
                if avg_latency_ms > 0:
                    _collect_duration_seconds.labels(device_id=device_id).observe(avg_latency_ms / 1000.0)

            # FIXED-P1: 清理已删除设备的统计条目，防止_last_collect_stats无界增长(内存泄漏)
            valid_keys = set()
            for _did in stats:
                valid_keys.add(f"collect:{_did}:calls")
                valid_keys.add(f"collect:{_did}:errors")
            for _stale in [k for k in _last_collect_stats if k not in valid_keys]:
                _last_collect_stats.pop(_stale, None)
    except Exception as e:
        logger.debug("prometheus_client device metrics collection failed: %s", e)

    # ── 规则指标 ──
    try:
        evaluator = getattr(_app_state, "evaluator", None)
        if evaluator:
            rules = getattr(evaluator, "_rules", {}) or {}
            active_rule_count = sum(1 for r in rules.values() if getattr(r, "enabled", True))
            _rules_active.set(active_rule_count)
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("规则指标采集失败: %s", e)

    # ── 告警指标 ──
    try:
        alarm_service = getattr(_app_state, "alarm_service", None)
        if alarm_service:
            alarm_repo = getattr(alarm_service, "_repo", None)
            if alarm_repo and hasattr(alarm_repo, "count_active_by_severity"):
                counts = alarm_repo.count_active_by_severity()
            else:
                evaluator = getattr(_app_state, "evaluator", None)
                counts = (
                    getattr(evaluator, "_alarm_counts", {}) if evaluator and hasattr(evaluator, "_alarm_counts") else {}
                )
            _alarms_firing.clear()
            total = 0
            for severity, count in counts.items():
                _alarms_firing.labels(severity=severity).set(count)
                total += count
            _alarms_firing.labels(severity="all").set(total)
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("告警指标采集失败: %s", e)

    # ── MQTT 转发指标 ──
    try:
        mqtt_forwarder = getattr(_app_state, "mqtt_forwarder", None)
        if mqtt_forwarder:
            forward_count = getattr(mqtt_forwarder, "_forward_count", 0)
            error_count = getattr(mqtt_forwarder, "_error_count", 0)

            # 计算 Counter 增量
            delta_forward = max(0, forward_count - _last_mqtt_forward_count)
            delta_errors = max(0, error_count - _last_mqtt_error_count)
            _last_mqtt_forward_count = forward_count
            _last_mqtt_error_count = error_count

            if delta_forward > 0:
                _mqtt_forward_total.inc(delta_forward)
            if delta_errors > 0:
                _mqtt_forward_errors.inc(delta_errors)
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("MQTT转发指标采集失败: %s", e)

    # ── InfluxDB 降级模式 ──
    try:
        influx_storage = getattr(_app_state, "influx_storage", None)
        if influx_storage:
            # FIXED-P0: using_fallback是async方法，原getattr获取绑定方法对象(始终truthy)导致降级模式指标失真，改为await调用  # noqa: E501
            using_fallback = (
                await influx_storage.using_fallback() if hasattr(influx_storage, "using_fallback") else False
            )
            _influxdb_fallback_mode.set(1 if using_fallback else 0)
        else:
            _influxdb_fallback_mode.set(0)
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.debug("InfluxDB降级模式指标采集失败: %s", e)


# ── 后台定时采集 ──

_background_task: asyncio.Task | None = None
_COLLECT_INTERVAL = 30  # 每30秒采集一次


async def _background_collect_loop() -> None:
    """后台定时采集指标"""
    while True:
        try:
            await asyncio.sleep(_COLLECT_INTERVAL)
            exporter = get_exporter()
            await _collect_all_metrics(exporter)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Background metrics collection failed: %s", e)


def start_background_collection() -> None:
    """启动后台指标采集任务"""
    global _background_task
    if _background_task is not None and not _background_task.done():
        return

    try:
        loop = asyncio.get_running_loop()
        _background_task = loop.create_task(_background_collect_loop())
        logger.info(
            "Background metrics collection started (interval=%ds)",
            _COLLECT_INTERVAL,
        )
    except RuntimeError:
        # 没有运行中的事件循环，延迟到首次请求时采集
        pass


def stop_background_collection() -> None:
    """停止后台指标采集任务"""
    global _background_task
    if _background_task is not None and not _background_task.done():
        _background_task.cancel()
        _background_task = None


# ── API 端点 ──


@router.get("/metrics")
async def prometheus_metrics(request: Request, auth_info: dict = MetricsAuth):
    """Prometheus指标端点

    FIXED: Requires authentication via JWT Bearer token with SYSTEM_READ permission
    or valid X-API-Key header.

    返回格式符合Prometheus exposition format。
    优先使用 prometheus_client 库输出指标；若未安装则优雅降级至自定义导出器。
    支持与Prometheus server集成进行指标采集。
    包含系统指标、设备指标、告警指标、AI推理指标、缓存指标等。
    """
    logger.debug("Metrics accessed: auth_type=%s username=%s", auth_info.get("auth_type"), auth_info.get("username"))

    if _PROMETHEUS_CLIENT_AVAILABLE:
        # 使用 prometheus_client 收集并输出指标
        await _collect_prometheus_client_metrics()

        # 同时收集系统指标到自定义导出器（补充 prometheus_client 未覆盖的指标）
        exporter = get_exporter()
        try:
            _collect_system_metrics(exporter)
        except ImportError:
            pass
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.debug("系统指标采集失败(补充导出器): %s", e)
        _collect_ai_metrics(exporter)
        await _collect_cache_metrics(exporter)
        await _collect_protocol_metrics(exporter)
        _collect_event_bus_metrics(exporter)

        # 合并输出：prometheus_client 指标 + 自定义导出器指标
        assert generate_latest is not None  # _PROMETHEUS_CLIENT_AVAILABLE 已保证
        pc_content = generate_latest(_registry).decode("utf-8")
        custom_content = exporter.render()
        content = pc_content + "\n" + custom_content if custom_content.strip() else pc_content
    else:
        # 降级：使用自定义导出器
        exporter = get_exporter()
        await _collect_all_metrics(exporter)
        content = exporter.render()

    # 确保后台采集已启动
    start_background_collection()

    return PlainTextResponse(
        content=content,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/metrics.json")
async def prometheus_metrics_json(request: Request, auth_info: dict = MetricsAuth):
    """Prometheus指标端点 (JSON格式)

    FIXED: Requires authentication via JWT Bearer token with SYSTEM_READ permission
    or valid X-API-Key header.

    返回OpenMetrics JSON格式，用于自定义采集。
    """
    logger.debug(
        "Metrics JSON accessed: auth_type=%s username=%s", auth_info.get("auth_type"), auth_info.get("username")
    )

    exporter = get_exporter()
    metrics = []

    # R8-S-06 修复: 直接遍历 exporter._metrics 未持有 _lock，与后台采集循环的
    # counter/gauge 写入并发时会抛出 RuntimeError(dictionary changed size during iteration)。
    # 用 with exporter._lock: 包裹遍历逻辑，与 render() 的加锁方式保持一致。
    with exporter._lock:
        for name, metric in exporter._metrics.items():
            for labels_key, value in metric["values"].items():
                labels = exporter._parse_labels(labels_key)
                metrics.append(
                    {
                        "name": name,
                        "type": metric["type"],
                        "description": metric.get("description", ""),
                        "labels": labels,
                        "value": value,
                    }
                )

    return {"metrics": metrics, "timestamp": time.time()}


# Root-level /metrics for Prometheus scraping (no /api prefix)
_root_metrics_router = APIRouter()


@_root_metrics_router.get("/metrics")
async def root_prometheus_metrics(request: Request):
    """Root /metrics endpoint for Prometheus scraping compatibility

    FIXED: Requires authentication via JWT Bearer token with SYSTEM_READ permission
    or valid X-API-Key header (same as /api/metrics).
    """
    # Reuse the same authentication logic
    auth_info = await _authenticate_metrics(request)

    logger.debug(
        "Root metrics accessed: auth_type=%s username=%s", auth_info.get("auth_type"), auth_info.get("username")
    )

    if _PROMETHEUS_CLIENT_AVAILABLE:
        await _collect_prometheus_client_metrics()
        exporter = get_exporter()
        try:
            _collect_system_metrics(exporter)
        except ImportError:
            pass
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.debug("系统指标采集失败(root端点): %s", e)
        _collect_ai_metrics(exporter)
        await _collect_cache_metrics(exporter)
        await _collect_protocol_metrics(exporter)
        assert generate_latest is not None
        pc_content = generate_latest(_registry).decode("utf-8")
        custom_content = exporter.render()
        content = pc_content + "\n" + custom_content if custom_content.strip() else pc_content
    else:
        exporter = get_exporter()
        await _collect_all_metrics(exporter)
        content = exporter.render()

    start_background_collection()

    return PlainTextResponse(
        content=content,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
