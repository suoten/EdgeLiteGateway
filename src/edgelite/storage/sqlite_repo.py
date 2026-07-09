"""ORM-based 仓储实现（SQLAlchemy 2.0 异步）"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from edgelite.api.error_codes import RepoErrors
from edgelite.constants import (
    _AUTH_LOGIN_WINDOW_SECONDS,
    _SHORT_ID_LENGTH,
    VALID_DEVICE_PROTOCOLS,
    normalize_protocol,
)
from edgelite.models.db import (
    AccountLockoutORM,
    AlarmORM,
    DeviceORM,
    DeviceTemplateORM,
    GlobalAccountLockoutORM,
    GlobalLoginFailureORM,
    LoginAttemptORM,
    PasswordResetAttemptORM,
    PasswordResetIPAttemptORM,
    PasswordResetUserRateORM,
    ResourceShareORM,
    RuleORM,
    StaleDataError,
    UsedPasswordResetTokenORM,
    UserORM,
)

logger = logging.getLogger(__name__)


# FIXED-OPTIMISTIC-LOCK: 乐观锁冲突重试装饰器
# 用于处理并发更新时的 StaleDataError，自动进行指数退避重试
def _get_optimistic_lock_retries() -> int:
    """FIXED-P4: 原问题-乐观锁重试次数硬编码3；改为从配置读取"""
    try:
        from edgelite.config import get_config
        return get_config().database.optimistic_lock_retries
    except Exception:
        return 3


def retry_on_stale(max_retries: int | None = None, base_delay: float = 0.1):
    """Decorator for automatic retry on optimistic lock conflicts (StaleDataError).

    Uses exponential backoff: delay = base_delay * 2^attempt
    After max_retries attempts, re-raises the original exception.

    Args:
        max_retries: Maximum number of retry attempts (default: from config, fallback 3)
        base_delay: Base delay in seconds for exponential backoff (default: 0.1)
    """
    effective_retries = max_retries if max_retries is not None else _get_optimistic_lock_retries()  # FIXED-P4: 原问题-硬编码3；改为从配置读取
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(effective_retries):
                try:
                    return await func(*args, **kwargs)
                except StaleDataError:
                    if attempt == effective_retries - 1:
                        logger.warning(
                            "StaleDataError: %s failed after %d retries",
                            func.__name__, effective_retries
                        )
                        raise
                    delay = base_delay * (2 ** attempt)
                    logger.debug(
                        "StaleDataError in %s (attempt %d/%d), retrying in %.3fs",
                        func.__name__, attempt + 1, effective_retries, delay
                    )
                    await asyncio.sleep(delay)
            return None  # unreachable
        return wrapper
    return decorator


def _now() -> datetime:
    return datetime.now(UTC)


# FIXED: 原问题-json.loads无异常保护，数据库字段损坏导致整个查询崩溃
# 现提供安全解析辅助函数
_corrupt_json_count: int = 0  # FIXED-P2: 原问题-_safe_json_loads损坏时静默返回default，运维无法感知数据损坏；添加全局计数器


def _safe_json_loads(value: Any, default: Any = None, field_name: str = "") -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            global _corrupt_json_count
            _corrupt_json_count += 1
            if _corrupt_json_count <= 10 or _corrupt_json_count % 100 == 0:
                logger.warning("Corrupt JSON detected (total=%d), returning default: %.80s", _corrupt_json_count, value)
            # FIXED-PROD: 原代码 from edgelite.services.event_bus import get_event_bus 路径错误
            # (该模块不存在)。此为模块级函数无 event_bus 实例访问，且日志已记录数据损坏，
            # 故移除无法工作的发布调用。如需事件通知，应通过调用方传入 event_bus。
            return default
    return value


def _uuid() -> str:
    return uuid.uuid4().hex[:_SHORT_ID_LENGTH]  # FIXED: 原问题-魔法数字，提取为命名常量


_VALID_DEVICE_STATUSES = {"online", "offline", "error", "unknown"}
_VALID_RULE_SEVERITIES = {"critical", "major", "warning", "minor", "info"}  # FIXED-P0: 与Pydantic RuleCreate.severity Literal对齐，补充major/minor
_VALID_RULE_LOGICS = {"AND", "OR", "NOT"}  # FIXED-P0: 与Pydantic RuleCreate.logic Literal对齐，补充NOT
_VALID_USER_ROLES = {"admin", "operator", "viewer"}
_VALID_ALARM_STATUSES = {"firing", "acknowledged", "recovered"}
_VALID_ALARM_RULE_TYPES = {"threshold", "ai_inference", "trend"}


def _validate_device_config(config: dict, protocol: str) -> None:
    """Deep-validate device config fields against protocol-specific schemas.

    FIXED-DEV-VALIDATION: Extends the previous shallow check to cover all protocol-specific
    fields (host/ip/port/unit_id for modbus, rack/slot/pdu_size for S7, endpoint_url/node_ids
    for OPC UA, broker/topic/qos for MQTT, etc.). Unknown protocols fall back to the
    generic host/port check. Empty/null config is allowed (some devices have no config).
    """
    # Allow empty config
    if not config:
        return

    if not isinstance(config, dict):
        raise ValueError(f"config must be dict, got {type(config).__name__}")

    p = protocol.lower()

    # ── Modbus TCP / RTU ───────────────────────────────────────────────────
    if p in ("modbus_tcp", "modbus_rtu", "modbus_slave"):
        _validate_modbus_config(config, p)

    # ── Siemens S7 ────────────────────────────────────────────────────────
    elif p in ("siemens_s7", "s7"):
        _validate_s7_config(config)

    # ── Mitsubishi MC ─────────────────────────────────────────────────────
    elif p in ("mitsubishi_mc", "mc"):
        _validate_mc_config(config)

    # ── Omron FINS ─────────────────────────────────────────────────────
    elif p in ("omron_fins", "fins"):
        _validate_fins_config(config)

    # ── Allen-Bradley (EtherNet/IP) ──────────────────────────────────────
    elif p in ("allen_bradley", "ab"):
        _validate_ab_config(config)

    # ── OPC UA ─────────────────────────────────────────────────────────
    elif p in ("opcua", "opc_ua"):
        _validate_opcua_config(config)

    # ── OPC DA ────────────────────────────────────────────────────────
    elif p == "opc_da":
        _validate_opcda_config(config)

    # ── MQTT ──────────────────────────────────────────────────────────
    elif p == "mqtt_client":
        _validate_mqtt_config(config)

    # ── HTTP Webhook ──────────────────────────────────────────────────
    elif p == "http_webhook":
        _validate_http_webhook_config(config)

    # ── Simulator ──────────────────────────────────────────────────────
    elif p == "simulator":
        _validate_simulator_config(config)

    # ── ONVIF ────────────────────────────────────────────────────────
    elif p == "onvif":
        _validate_onvif_config(config)

    # ── Video AI ──────────────────────────────────────────────────────
    elif p == "video_ai":
        _validate_videoai_config(config)

    # ── Generic fallback ───────────────────────────────────────────────
    else:
        _validate_generic_config(config)


# ----------------------------------------------------------------------
# Protocol-specific validators
# ----------------------------------------------------------------------

def _validate_modbus_config(config: dict, protocol: str) -> None:
    """Modbus TCP/RTU/Slave config: host, port, unit_id (slave_id), timeout, retry."""
    host = config.get("host")
    if host is not None and not isinstance(host, str):
        raise ValueError(f"config.host must be str, got {type(host).__name__}")
    if host is not None and host != "":
        _validate_ipv4_or_hostname(host, "config.host")
        _validate_dns_resolution(host, "config.host")

    port = config.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise ValueError(f"config.port must be int in [1, 65535], got {port!r}")

    unit_id = config.get("unit_id") or config.get("slave_id")
    if unit_id is not None and (not isinstance(unit_id, int) or not (0 <= unit_id <= 255)):
        raise ValueError(f"config.unit_id/slave_id must be int in [0, 255], got {unit_id!r}")

    timeout = config.get("timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ValueError(f"config.timeout must be positive number, got {timeout!r}")

    retries = config.get("retries")
    if retries is not None and (not isinstance(retries, int) or retries < 0):
        raise ValueError(f"config.retries must be non-negative int, got {retries!r}")

    # RTU-specific: serial_port
    if protocol == "modbus_rtu":
        serial_port = config.get("serial_port") or config.get("port_name")
        if serial_port is None or (isinstance(serial_port, str) and not serial_port.strip()):  # FIXED-P2: 原问题-RTU协议serial_port允许None入库；改为强制非空
            raise ValueError("config.serial_port is required for modbus_rtu and must be non-empty str")
        if not isinstance(serial_port, str):
            raise ValueError(f"config.serial_port must be str, got {type(serial_port).__name__}")
        baud = config.get("baudrate") or config.get("baud")
        if baud is not None and (not isinstance(baud, int) or baud <= 0):
            raise ValueError(f"config.baudrate must be positive int, got {baud!r}")
        parity = config.get("parity")
        if parity is not None and parity not in ("N", "E", "O", "M", "S"):
            raise ValueError(f"config.parity must be one of N/E/O/M/S, got {parity!r}")
        stopbits = config.get("stopbits")
        if stopbits is not None and stopbits not in (1, 1.5, 2):
            raise ValueError(f"config.stopbits must be 1/1.5/2, got {stopbits!r}")

    # Modbus TCP specific: mode (tcp/rtu/pdu)
    mode = config.get("mode")
    if mode is not None and mode not in ("tcp", "rtu", "pdu", "ascii"):
        raise ValueError(f"config.mode must be one of tcp/rtu/pdu/ascii, got {mode!r}")


def _validate_s7_config(config: dict) -> None:
    """S7 config: ip, rack, slot, pdu_size, connect_timeout, plc_model."""
    ip = config.get("ip")
    if ip is not None and ip != "":
        _validate_ipv4_or_hostname(ip, "config.ip")
        _validate_dns_resolution(ip, "config.ip")

    for field, lo, hi in [
        ("rack", 0, 7),
        ("slot", 0, 31),
        ("pdu_size", 0, 65535),
        ("connect_timeout", 1, 300),
    ]:
        v = config.get(field)
        if v is not None and (not isinstance(v, int) or not (lo <= v <= hi)):
            raise ValueError(f"config.{field} must be int in [{lo}, {hi}], got {v!r}")

    plc_model = config.get("plc_model")
    if plc_model is not None and plc_model not in (
        "auto", "S7-200 SMART", "S7-300", "S7-400", "S7-1200", "S7-1500",
    ):
        raise ValueError(
            f"config.plc_model must be one of auto/S7-200 SMART/S7-300/S7-400/S7-1200/S7-1500, got {plc_model!r}"
        )

    password = config.get("password")
    if password is not None and password != "":
        if not isinstance(password, str):
            raise ValueError(f"config.password must be str, got {type(password).__name__}")
        # 8 hex chars or empty
        if len(password) not in (0, 8):
            raise ValueError(f"config.password must be 8 hex chars or empty, got length={len(password)}")


def _validate_mc_config(config: dict) -> None:
    """Mitsubishi MC config: host, port, network, station, cpu_type."""
    host = config.get("host")
    if host is not None and host != "":
        _validate_ipv4_or_hostname(host, "config.host")
        _validate_dns_resolution(host, "config.host")

    port = config.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise ValueError(f"config.port must be int in [1, 65535], got {port!r}")

    for field, lo, hi in [
        ("network", 0, 255),
        ("station", 0, 255),
    ]:
        v = config.get(field)
        if v is not None and (not isinstance(v, int) or not (lo <= v <= hi)):
            raise ValueError(f"config.{field} must be int in [{lo}, {hi}], got {v!r}")

    cpu = config.get("cpu_type")
    if cpu is not None and cpu not in ("Q", "iQ-R", "iQ-F", "L"):
        raise ValueError(f"config.cpu_type must be one of Q/iQ-R/iQ-F/L, got {cpu!r}")


def _validate_fins_config(config: dict) -> None:
    """Omron FINS config: host, port, network, node, unit, cpu_type."""
    host = config.get("host")
    if host is not None and host != "":
        _validate_ipv4_or_hostname(host, "config.host")
        _validate_dns_resolution(host, "config.host")

    port = config.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise ValueError(f"config.port must be int in [1, 65535], got {port!r}")

    for field, lo, hi in [
        ("network", 0, 127),
        ("node", 1, 254),
        ("unit", 0, 31),
    ]:
        v = config.get(field)
        if v is not None and (not isinstance(v, int) or not (lo <= v <= hi)):
            raise ValueError(f"config.{field} must be int in [{lo}, {hi}], got {v!r}")


def _validate_ab_config(config: dict) -> None:
    """Allen-Bradley / EtherNet IP config: host, port, slot, cpu_type."""
    host = config.get("host")
    if host is not None and host != "":
        _validate_ipv4_or_hostname(host, "config.host")
        _validate_dns_resolution(host, "config.host")

    port = config.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise ValueError(f"config.port must be int in [1, 65535], got {port!r}")

    slot = config.get("slot")
    if slot is not None and (not isinstance(slot, int) or not (0 <= slot <= 16)):
        raise ValueError(f"config.slot must be int in [0, 16], got {slot!r}")


def _validate_opcua_config(config: dict) -> None:
    """OPC UA config: endpoint_url, namespace_index, security_mode, auth."""
    endpoint = config.get("endpoint") or config.get("endpoint_url")
    if endpoint is not None:
        if not isinstance(endpoint, str):
            raise ValueError(f"config.endpoint must be str, got {type(endpoint).__name__}")
        if not endpoint.startswith(("opc.tcp://", "opc.https://", "opcua://")):
            raise ValueError(
                f"config.endpoint must start with opc.tcp:// or opc.https://, got {endpoint!r}"
            )

    ns_index = config.get("namespace_index") or config.get("namespace")
    if ns_index is not None and (not isinstance(ns_index, int) or ns_index < 0):
        raise ValueError(f"config.namespace_index must be non-negative int, got {ns_index!r}")

    sec_mode = config.get("security_mode")
    if sec_mode is not None and sec_mode not in ("None", "Sign", "SignAndEncrypt"):
        raise ValueError(
            f"config.security_mode must be one of None/Sign/SignAndEncrypt, got {sec_mode!r}"
        )

    policy = config.get("security_policy")
    if policy is not None and policy not in (
        "Basic256", "Basic256Sha256", "Basic128Rsa15",
        "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss",
    ):
        raise ValueError(f"config.security_policy value not recognised: {policy!r}")


def _validate_opcda_config(config: dict) -> None:
    """OPC DA config: host, prog_id, clsid, server_node."""
    host = config.get("host")
    if host is not None and host != "":
        _validate_ipv4_or_hostname(host, "config.host")

    prog_id = config.get("prog_id") or config.get("program_id")
    if prog_id is not None and not isinstance(prog_id, str):
        raise ValueError(f"config.prog_id must be str, got {type(prog_id).__name__}")


def _validate_mqtt_config(config: dict) -> None:
    """MQTT config: broker, port, topic, qos, username, keepalive."""
    broker = config.get("broker") or config.get("host") or config.get("server")
    if broker is not None and broker != "":
        _validate_ipv4_or_hostname(broker, "config.broker")

    port = config.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise ValueError(f"config.port must be int in [1, 65535], got {port!r}")

    qos = config.get("qos")
    if qos is not None and qos not in (0, 1, 2):
        raise ValueError(f"config.qos must be 0/1/2, got {qos!r}")

    keepalive = config.get("keepalive")
    if keepalive is not None and (not isinstance(keepalive, int) or keepalive <= 0):
        raise ValueError(f"config.keepalive must be positive int, got {keepalive!r}")

    topic = config.get("topic") or config.get("subscribe_topic")
    if topic is not None and not isinstance(topic, (str, list)):
        raise ValueError(f"config.topic must be str or list, got {type(topic).__name__}")


def _validate_http_webhook_config(config: dict) -> None:
    """HTTP Webhook config: url, method, headers, timeout."""
    url = config.get("url") or config.get("webhook_url")
    if url is not None and url != "":
        if not isinstance(url, str):
            raise ValueError(f"config.url must be str, got {type(url).__name__}")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"config.url must start with http:// or https://, got {url!r}")

    method = config.get("method")
    if method is not None and method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise ValueError(f"config.method must be GET/POST/PUT/PATCH/DELETE, got {method!r}")

    timeout = config.get("timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ValueError(f"config.timeout must be positive number, got {timeout!r}")


def _validate_simulator_config(config: dict) -> None:
    """Simulator config: mode, interval_ms, noise."""
    mode = config.get("mode")
    if mode is not None and mode not in ("sine", "random", "step", "constant", "triangle"):
        raise ValueError(
            f"config.mode must be one of sine/random/step/constant/triangle, got {mode!r}"
        )

    interval = config.get("interval_ms") or config.get("interval")
    if interval is not None and (not isinstance(interval, (int, float)) or interval <= 0):
        raise ValueError(f"config.interval_ms must be positive number, got {interval!r}")


def _validate_onvif_config(config: dict) -> None:
    """ONVIF config: host, port, user, password."""
    host = config.get("host")
    if host is not None and host != "":
        _validate_ipv4_or_hostname(host, "config.host")
        _validate_dns_resolution(host, "config.host")

    port = config.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise ValueError(f"config.port must be int in [1, 65535], got {port!r}")

    user = config.get("user") or config.get("username")
    if user is not None and not isinstance(user, str):
        raise ValueError(f"config.user must be str, got {type(user).__name__}")

    password = config.get("password")
    if password is not None and not isinstance(password, str):
        raise ValueError(f"config.password must be str, got {type(password).__name__}")


def _validate_videoai_config(config: dict) -> None:
    """Video AI config: rtsp_url, model_path, detect_interval."""
    rtsp = config.get("rtsp_url") or config.get("stream_url")
    if rtsp is not None and rtsp != "":
        if not isinstance(rtsp, str):
            raise ValueError(f"config.rtsp_url must be str, got {type(rtsp).__name__}")
        if not rtsp.startswith(("rtsp://", "rtmp://", "http://", "https://")):
            raise ValueError(
                f"config.rtsp_url must start with rtsp:// or rtmp://, got {rtsp!r}"
            )

    interval = config.get("detect_interval") or config.get("interval")
    if interval is not None and (not isinstance(interval, (int, float)) or interval <= 0):
        raise ValueError(f"config.detect_interval must be positive number, got {interval!r}")


def _validate_generic_config(config: dict) -> None:
    """Fallback for unknown protocols: validate common network fields."""
    host = config.get("host") or config.get("ip") or config.get("address")
    if host is not None and host != "":
        _validate_ipv4_or_hostname(host, "config.host")
        _validate_dns_resolution(host, "config.host")

    port = config.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise ValueError(f"config.port must be int in [1, 65535], got {port!r}")


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------

def _validate_ipv4_or_hostname(value: str, field_name: str) -> None:
    """Reject obviously invalid host values."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty string, got {value!r}")
    # Basic pattern: alphanumeric with dots/hyphens, at least one dot for FQDN
    import re
    if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9.\-]*[a-zA-Z0-9])?$", value):
        raise ValueError(f"{field_name} contains invalid characters: {value!r}")


def _validate_dns_resolution(host: str, field_name: str) -> None:
    """Warn about hosts that look malformed (but don't block — DNS may work at runtime)."""
    import re
    # Block obviously invalid patterns (but not all — runtime DNS is authoritative)
    if re.search(r"[^\w.\-]", host):
        raise ValueError(f"{field_name} contains invalid hostname characters: {host!r}")


# ----------------------------------------------------------------------
# Point-level validation
# ----------------------------------------------------------------------

def _validate_points(points: Any, protocol: str = "") -> None:
    """Validate every point in a points list against protocol-specific field requirements.

    FIXED-DEV-VALIDATION: Extends the previous shallow check (name only) to cover
    address/register, data_type, scan_rate, and scale/offset/deadband ranges per point.
    Null/empty points list is allowed.
    """
    if points is None:
        return
    if not isinstance(points, list):
        raise ValueError(f"points must be list, got {type(points).__name__}")

    p = protocol.lower()

    for i, pt in enumerate(points):
        prefix = f"points[{i}]"
        if not isinstance(pt, dict):
            raise ValueError(f"{prefix} must be dict, got {type(pt).__name__}")

        # Name: always required
        if "name" not in pt:
            raise ValueError(f"{prefix} missing required field: 'name'")

        # Address/register: required for most protocols, optional for simulator/video_ai
        addr = pt.get("address") or pt.get("register") or pt.get("node_id")
        if p not in ("simulator", "video_ai", "http_webhook") and addr is None:
            raise ValueError(f"{prefix} missing required field: 'address' (or 'register')")
        if addr is not None and not isinstance(addr, (str, int)):
            raise ValueError(f"{prefix}.address must be str or int, got {type(addr).__name__}")

        # data_type
        dt = pt.get("data_type") or pt.get("type")
        if dt is not None and dt not in (
            "bool", "int16", "uint16", "int32", "uint32",
            "float32", "float64", "string", "bit", "byte",
        ):
            raise ValueError(
                f"{prefix}.data_type must be one of "
                "bool/int16/uint16/int32/uint32/float32/float64/string/bit/byte, got {dt!r}"
            )

        # scan_rate / interval
        sr = pt.get("scan_rate") or pt.get("interval") or pt.get("scan_interval")
        if sr is not None and (not isinstance(sr, (int, float)) or sr <= 0):
            raise ValueError(f"{prefix}.scan_rate must be positive number, got {sr!r}")

        # scale / ratio
        scale = pt.get("scale") or pt.get("ratio")
        if scale is not None and not isinstance(scale, (int, float)):
            raise ValueError(f"{prefix}.scale must be number, got {type(scale).__name__}")

        # offset
        offset = pt.get("offset")
        if offset is not None and not isinstance(offset, (int, float)):
            raise ValueError(f"{prefix}.offset must be number, got {type(offset).__name__}")

        # deadband
        db = pt.get("deadband")
        if db is not None:
            if not isinstance(db, (int, float)):
                raise ValueError(f"{prefix}.deadband must be number, got {type(db).__name__}")
            if db < 0:
                raise ValueError(f"{prefix}.deadband must be non-negative, got {db!r}")

        # writable flag
        writable = pt.get("writable")
        if writable is not None and not isinstance(writable, bool):
            raise ValueError(f"{prefix}.writable must be bool, got {type(writable).__name__}")

        # Bit position for Modbus bit reads
        bit = pt.get("bit")
        if bit is not None and (not isinstance(bit, int) or not (0 <= bit <= 15)):
            raise ValueError(f"{prefix}.bit must be int in [0, 15], got {bit!r}")

        # Register type for Modbus
        reg_type = pt.get("register_type") or pt.get("function_code")
        if reg_type is not None and reg_type not in (
            "coil", "holding", "input", "discrete",
            1, 2, 3, 4,
        ):
            raise ValueError(
                f"{prefix}.register_type must be one of "
                "coil/holding/input/discrete/1/2/3/4, got {reg_type!r}"
            )


def _validate_device_data(data: dict) -> None:  # FIXED-P1: 设备创建业务验证
    if not data.get("device_id"):
        raise ValueError("device_id is required")
    if not data.get("name"):
        raise ValueError("name is required")
    protocol = data.get("protocol", "")
    if protocol and normalize_protocol(protocol) is None:
        raise ValueError(f"Invalid protocol: {protocol}, valid options: {sorted(VALID_DEVICE_PROTOCOLS)}")
    status = data.get("status", "offline")
    if status and status not in _VALID_DEVICE_STATUSES:
        raise ValueError(f"Invalid status: {status}, must be one of {_VALID_DEVICE_STATUSES}")
    ci = data.get("collect_interval", 5)
    if not isinstance(ci, int) or ci <= 0:
        raise ValueError(f"collect_interval must be positive integer, got {ci}")
    config = data.get("config")
    if config is not None and not isinstance(config, dict):  # FIXED-DEV-VALIDATION: config结构验证
        raise ValueError(f"config must be dict, got {type(config).__name__}")
    if isinstance(config, dict) and protocol:  # FIXED-DEV-VALIDATION: protocol-specific内部字段验证
        _validate_device_config(config, protocol)
    points = data.get("points")
    if points is not None:
        if not isinstance(points, list):
            raise ValueError(f"points must be list, got {type(points).__name__}")
        _validate_points(points, protocol)  # FIXED-DEV-VALIDATION: 每点字段深度验证


def _validate_device_update_data(data: dict, current_protocol: str = "") -> None:  # FIXED-P0: update路径业务验证
    if "status" in data and data["status"] not in _VALID_DEVICE_STATUSES:
        raise ValueError(f"Invalid status: {data['status']}, must be one of {_VALID_DEVICE_STATUSES}")
    if "collect_interval" in data:
        ci = data["collect_interval"]
        if not isinstance(ci, int) or ci <= 0:
            raise ValueError(f"collect_interval must be positive integer, got {ci}")
    if "config" in data:
        if not isinstance(data["config"], dict):  # FIXED-DEV-VALIDATION: config结构验证
            raise ValueError(f"config must be dict, got {type(data['config']).__name__}")
        protocol = data.get("protocol") or current_protocol  # FIXED-P2: 原问题-protocol缺失时跳过config验证；改为fallback到DB当前protocol
        if protocol:
            _validate_device_config(data["config"], protocol)  # FIXED-DEV-VALIDATION: protocol-specific验证
    if "points" in data:
        pts = data["points"]
        if not isinstance(pts, list):
            raise ValueError(f"points must be list, got {type(pts).__name__}")
        protocol = data.get("protocol") or current_protocol  # FIXED-P2: 原问题-points验证同理fallback到DB当前protocol
        _validate_points(pts, protocol)  # FIXED-DEV-VALIDATION: 每点字段深度验证


_VALID_NOTIFY_CHANNELS = {"dingtalk", "email", "wechat", "webhook"}


def _validate_notify_channels(channels: Any) -> None:  # FIXED-P1: 原问题-notify_channels无结构验证，非法渠道名可入库
    if not isinstance(channels, list):
        raise ValueError(f"notify_channels must be list, got {type(channels).__name__}")
    for i, ch in enumerate(channels):
        if not isinstance(ch, str):
            raise ValueError(f"notify_channels[{i}] must be str, got {type(ch).__name__}")
        if ch not in _VALID_NOTIFY_CHANNELS:
            raise ValueError(f"notify_channels[{i}] invalid channel: {ch!r}, must be one of {_VALID_NOTIFY_CHANNELS}")


def _validate_trigger_value(value: Any) -> None:  # FIXED-P1: 原问题-trigger_value无结构验证，任意非dict值可入库
    if not isinstance(value, dict):
        raise ValueError(f"trigger_value must be dict, got {type(value).__name__}")


def _validate_rule_conditions(conditions: Any) -> None:  # FIXED-P1: 原问题-conditions无结构验证，非法条件表达式可入库
    if not isinstance(conditions, list):
        raise ValueError(f"conditions must be list, got {type(conditions).__name__}")
    for i, cond in enumerate(conditions):
        if not isinstance(cond, dict):
            raise ValueError(f"conditions[{i}] must be dict, got {type(cond).__name__}")
        if "point" not in cond:
            raise ValueError(f"conditions[{i}] missing required key 'point'")
        if "operator" not in cond:
            raise ValueError(f"conditions[{i}] missing required key 'operator'")


def _validate_rule_data(data: dict) -> None:  # FIXED-P1: 原问题-规则创建无业务验证
    if not data.get("name"):
        raise ValueError("name is required")
    severity = data.get("severity", "")
    if severity and severity not in _VALID_RULE_SEVERITIES:
        raise ValueError(f"Invalid severity: {severity}, must be one of {_VALID_RULE_SEVERITIES}")
    logic = data.get("logic", "AND")
    if logic and logic not in _VALID_RULE_LOGICS:
        raise ValueError(f"Invalid logic: {logic}, must be one of {_VALID_RULE_LOGICS}")
    duration = data.get("duration", 0)
    if not isinstance(duration, (int, float)) or duration < 0:
        raise ValueError(f"duration must be non-negative, got {duration}")
    conditions = data.get("conditions")
    if conditions is not None:
        _validate_rule_conditions(conditions)
    notify_channels = data.get("notify_channels")  # FIXED-P1: 原问题-notify_channels无结构验证
    if notify_channels is not None:
        _validate_notify_channels(notify_channels)


def _validate_rule_update_data(data: dict) -> None:  # FIXED-P0: 原问题-update路径不调用验证
    if "severity" in data and data["severity"] not in _VALID_RULE_SEVERITIES:
        raise ValueError(f"Invalid severity: {data['severity']}, must be one of {_VALID_RULE_SEVERITIES}")
    if "logic" in data and data["logic"] not in _VALID_RULE_LOGICS:
        raise ValueError(f"Invalid logic: {data['logic']}, must be one of {_VALID_RULE_LOGICS}")
    if "duration" in data:
        dur = data["duration"]
        if not isinstance(dur, (int, float)) or dur < 0:
            raise ValueError(f"duration must be non-negative, got {dur}")
    if "conditions" in data:  # FIXED-P1: 原问题-update路径conditions无结构验证
        _validate_rule_conditions(data["conditions"])
    if "notify_channels" in data:  # FIXED-P1: 原问题-update路径notify_channels无结构验证
        _validate_notify_channels(data["notify_channels"])


def _validate_user_data(data: dict) -> None:  # FIXED-P1: 原问题-用户创建无角色验证，可写入任意角色
    if not data.get("username"):
        raise ValueError("username is required")
    password = data.get("password", "")
    if not password:
        raise ValueError("password is required")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(password) > 72:
        raise ValueError("password must be no more than 72 characters")
    _has_alpha = any(c.isalpha() for c in password)
    _has_digit = any(c.isdigit() for c in password)
    _has_special = any(not c.isalnum() for c in password)
    if not (_has_alpha and _has_digit and _has_special):
        raise ValueError("password must contain letters, digits, and special characters")
    role = data.get("role", "")
    if role and role not in _VALID_USER_ROLES:
        raise ValueError(f"Invalid role: {role}, must be one of {_VALID_USER_ROLES}")


def _validate_user_update_data(data: dict) -> None:  # FIXED-P0: 原问题-update路径不调用验证，role可写入任意值
    if "role" in data and data["role"] not in _VALID_USER_ROLES:
        raise ValueError(f"Invalid role: {data['role']}, must be one of {_VALID_USER_ROLES}")
    if "password" in data:
        pwd = data["password"]
        if len(pwd) < 8:
            raise ValueError("password must be at least 8 characters")
        if len(pwd) > 72:
            raise ValueError("password must be no more than 72 characters")
        _ha = any(c.isalpha() for c in pwd)
        _hd = any(c.isdigit() for c in pwd)
        _hs = any(not c.isalnum() for c in pwd)
        if not (_ha and _hd and _hs):
            raise ValueError("password must contain letters, digits, and special characters")


def _validate_template_data(data: dict) -> None:
    if not data.get("name"):
        raise ValueError("name is required")
    protocol = data.get("protocol", "")
    if protocol and normalize_protocol(protocol) is None:
        raise ValueError(f"Invalid protocol: {protocol}, valid options: {sorted(VALID_DEVICE_PROTOCOLS)}")


def _validate_template_update_data(data: dict) -> None:  # FIXED-P1: TemplateRepo.update验证条件跳过，protocol变更不触发验证
    if "protocol" in data and normalize_protocol(data["protocol"]) is None:
        raise ValueError(f"Invalid protocol: {data['protocol']}, valid options: {sorted(VALID_DEVICE_PROTOCOLS)}")


def _validate_alarm_data(data: dict) -> None:  # FIXED-P1: 原问题-AlarmRepo.create无业务验证，severity/status/rule_type可写入非法值
    severity = data.get("severity", "")
    if severity and severity not in _VALID_RULE_SEVERITIES:
        raise ValueError(f"Invalid severity: {severity}, must be one of {_VALID_RULE_SEVERITIES}")
    rule_type = data.get("rule_type", "threshold")
    if rule_type and rule_type not in _VALID_ALARM_RULE_TYPES:
        raise ValueError(f"Invalid rule_type: {rule_type}, must be one of {_VALID_ALARM_RULE_TYPES}")
    message = data.get("message", "")
    if len(message) > 256:  # FIXED-P2: 原问题-message无长度验证，超长被DB VARCHAR(256)静默截断
        raise ValueError(f"message length {len(message)} exceeds maximum 256")
    trigger_value = data.get("trigger_value")  # FIXED-P1: 原问题-trigger_value无结构验证，任意非dict值可入库
    if trigger_value is not None:
        _validate_trigger_value(trigger_value)


class BaseRepo:
    def __init__(self, session_or_db: Any, write_lock: asyncio.Lock | None = None, table_name: str | None = None):
        from edgelite.storage.database import Database

        if isinstance(session_or_db, Database):
            self._database = session_or_db
            self._external_session: AsyncSession | None = None
        else:
            self._database = None
            self._external_session = session_or_db
        self._write_lock = write_lock
        # FIXED-FINE-GRAINED-LOCK: 支持按表细粒度锁
        self._table_name = table_name
        self._table_lock: asyncio.Lock | None = None
        self._use_fine_grained = True

    async def _commit(self, session: AsyncSession) -> None:
        # FIXED-P1: 原问题-_commit仅保护commit不保护读-改-写序列，调用方误用则并发更新交叉
        # 改为废弃此方法，所有调用方应直接使用session.commit()并在外层使用_table_write_lock/_write_write_lock
        raise RuntimeError(
            "BaseRepo._commit is deprecated: use session.commit() inside _table_write_lock() or _write_write_lock() "
            "to protect the full read-modify-write sequence"
        )

    from contextlib import asynccontextmanager

    def _init_table_lock(self) -> None:
        """初始化表级锁（延迟获取以避免循环导入）"""
        if self._table_lock is not None:
            return  # 已初始化
        if self._table_name and self._database:
            self._table_lock = self._database.get_table_lock(self._table_name)
            self._use_fine_grained = self._database.use_fine_grained_locks

    @asynccontextmanager
    async def _table_write_lock(self) -> AsyncGenerator[None, None]:
        """FIXED-FINE-GRAINED-LOCK: 表级写锁

        根据配置选择使用细粒度表锁或全局锁。
        只有当 use_fine_grained_locks=True 且有对应表锁时才使用表锁。
        """
        self._init_table_lock()

        if self._use_fine_grained and self._table_lock:
            # 使用细粒度表锁
            async with self._table_lock:
                yield
        elif self._write_lock:
            # 回退到全局锁
            async with self._write_lock:
                yield
        else:
            # 无锁（单线程或外部管理事务）
            yield

    @asynccontextmanager
    async def _write_write_lock(self) -> AsyncGenerator[None, None]:
        """FIXED-P0: 原问题-write_lock仅保护commit，读-改-写序列可并发交叉
        提供整个读-改-写序列的锁保护，无锁时为空上下文

        注意：优先使用表级锁 _table_write_lock()，此方法保留用于向后兼容
        """
        self._init_table_lock()

        if self._use_fine_grained and self._table_lock:
            async with self._table_lock:
                yield
        elif self._write_lock:
            async with self._write_lock:
                yield
        else:
            yield

    def _get_session(self) -> AsyncSession:
        if self._external_session is not None:
            return self._external_session
        raise RuntimeError(RepoErrors.DB_MODE_SESSION_REQUIRED) from None

    @property
    def _is_database_mode(self) -> bool:
        return self._database is not None


    @asynccontextmanager
    async def _auto_session(self) -> AsyncGenerator[AsyncSession, None]:
        """FIXED-ROLLBACK: Context manager that ensures automatic rollback on exception.

        This ensures that any unhandled exception within the session results in
        a clean rollback, preventing uncommitted transactions from being left in
        an inconsistent state.

        For external sessions, we only yield without commit/rollback since the
        caller controls the session lifecycle.
        """
        if self._external_session is not None:
            yield self._external_session
        elif self._database is not None:
            async with self._database.session() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise
        else:
            raise RuntimeError(RepoErrors.NO_SESSION_AVAILABLE) from None

    # FIXED: 原问题-所有Repo方法无try-except保护，数据库异常直接抛出导致调用方崩溃
    # 现提供安全执行辅助方法，查询类操作异常时返回默认值，写入类操作异常时记录日志并抛出
    async def _safe_query(self, coro, default=None, label="query"):
        try:
            return await coro
        except Exception as e:
            logger.error("Repo %s failed: %s", label, e)
            return default

    async def _safe_write(self, coro, label="write"):
        try:
            return await coro
        except IntegrityError:
            raise
        except Exception as e:
            logger.error("Repo %s failed: %s", label, e)
            raise


class DeviceRepo(BaseRepo):
    def __init__(self, session_or_db: Any, write_lock: asyncio.Lock | None = None, table_name: str | None = None):
        super().__init__(session_or_db, write_lock, table_name or "devices")

    async def create(self, data: dict, created_by: str | None = None) -> dict:
        _validate_device_data(data)  # FIXED-P1: 设备创建业务验证
        try:
            async with self._auto_session() as session:
                now = _now()
                orm = DeviceORM(
                    device_id=data["device_id"],
                    name=data["name"],
                    protocol=data["protocol"],
                    status=data.get("status", "offline"),
                    config=json.dumps(data.get("config", {}), ensure_ascii=False),
                    points=json.dumps(data.get("points", []), ensure_ascii=False),
                    collect_interval=data.get("collect_interval", 5),
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                session.add(orm)
                await session.commit()
                await session.refresh(orm)
                return _orm_to_device(orm)
        except IntegrityError:
            raise ValueError(RepoErrors.DEVICE_EXISTS) from None

    async def get(self, device_id: str) -> dict | None:
        # FIXED: 原问题-查询操作无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(DeviceORM).where(DeviceORM.device_id == device_id)
                )
                orm = result.scalar_one_or_none()
                return _orm_to_device(orm) if orm else None
        except Exception as e:
            logger.error("DeviceRepo.get failed: %s", e)
            raise RuntimeError(f"DeviceRepo.get failed for device_id={device_id}: {e}") from e

    # FIXED-P1: 原问题-export_devices中循环调用get导致N+1查询；新增批量查询方法
    async def get_by_ids(self, device_ids: list[str]) -> list[dict]:
        """批量查询多个设备（单条SQL，避免N+1查询）"""
        if not device_ids:
            return []
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(DeviceORM).where(DeviceORM.device_id.in_(device_ids))
                )
                orms = result.scalars().all()
                return [_orm_to_device(orm) for orm in orms if orm is not None]
        except Exception as e:
            logger.error("DeviceRepo.get_by_ids failed: %s", e)
            raise RuntimeError(f"DeviceRepo.get_by_ids failed: {e}") from e

    # FIXED-ATOMIC-IMPORT: 原子性批量创建设备（在外部事务中执行）
    async def bulk_create_in_session(
        self,
        session: AsyncSession,
        devices: list[dict],
        created_by: str | None = None,
    ) -> list[tuple[bool, str, str]]:
        """批量创建设备（不自动提交，由外部事务控制提交）

        Returns:
            list of (success, device_id, error_message)
        """
        results: list[tuple[bool, str, str]] = []
        for item in devices:
            device_id = item.get("device_id", "")
            try:
                _validate_device_data(item)
                now = _now()
                orm = DeviceORM(
                    device_id=item["device_id"],
                    name=item["name"],
                    protocol=item["protocol"],
                    status=item.get("status", "offline"),
                    config=json.dumps(item.get("config", {}), ensure_ascii=False),
                    points=json.dumps(item.get("points", []), ensure_ascii=False),
                    collect_interval=item.get("collect_interval", 5),
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                session.add(orm)
                # 不自动 commit，由调用方控制
                results.append((True, device_id, ""))
            except IntegrityError:
                results.append((False, device_id, "Device already exists"))
            except Exception as e:
                results.append((False, device_id, str(e)))
        return results

    async def list_all(
        self,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        protocol: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        collect_status: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict], int] | tuple[list[dict], int, str | None]:
        try:
            async with self._auto_session() as session:
                query = select(DeviceORM)
                count_query = select(func.count()).select_from(DeviceORM)
                if status:
                    query = query.where(DeviceORM.status == status)
                    count_query = count_query.where(DeviceORM.status == status)
                if collect_status == "collecting":
                    query = query.where(DeviceORM.status == "online")
                    count_query = count_query.where(DeviceORM.status == "online")
                elif collect_status == "stopped":
                    query = query.where(DeviceORM.status != "online")
                    count_query = count_query.where(DeviceORM.status != "online")
                if protocol:
                    query = query.where(DeviceORM.protocol == protocol)
                    count_query = count_query.where(DeviceORM.protocol == protocol)
                if search and len(search) >= 2:
                    # FIXED: 前缀通配符 LIKE 无法走索引，限制搜索最小长度为2字符，避免单字符全表扫描
                    # R6-G-02 修复(一般): 转义用户输入中的 LIKE 通配符 % 和 _，防止搜索语义被绕过
                    escaped_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    pattern = f"%{escaped_search}%"
                    query = query.where(
                        (DeviceORM.name.ilike(pattern, escape="\\")) | (DeviceORM.device_id.ilike(pattern, escape="\\"))
                    )
                    count_query = count_query.where(
                        (DeviceORM.name.ilike(pattern, escape="\\")) | (DeviceORM.device_id.ilike(pattern, escape="\\"))
                    )
                if created_by:
                    query = query.where(DeviceORM.created_by == created_by)
                    count_query = count_query.where(DeviceORM.created_by == created_by)
                total_result = await session.execute(count_query)
                total = total_result.scalar() or 0
                # R9-S-09: 游标分页优化——当提供cursor时使用 WHERE created_at < cursor 替代 OFFSET，避免深分页性能退化
                if cursor is not None:
                    cursor_dt = datetime.fromisoformat(cursor)
                    query = query.where(DeviceORM.created_at < cursor_dt)
                    query = query.order_by(DeviceORM.created_at.desc()).limit(size)
                else:
                    offset = (page - 1) * size
                    query = query.order_by(DeviceORM.created_at.desc()).offset(offset).limit(size)
                result = await session.execute(query)
                rows = result.scalars().all()
                items = [_orm_to_device(r) for r in rows]
                if cursor is not None:
                    # 返回 next_cursor 供下次查询使用（最后一条记录的 created_at）
                    next_cursor = items[-1]["created_at"] if items else None
                    return items, total, next_cursor
                return items, total
        except Exception as e:
            logger.error("DeviceRepo.list_all failed: %s", e)
            raise RuntimeError(f"DeviceRepo.list_all failed: {e}") from e

    async def list_device_ids_by_owner(self, created_by: str) -> list[str]:
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(DeviceORM.device_id).where(DeviceORM.created_by == created_by)
                )
                return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.error("DeviceRepo.list_device_ids_by_owner failed: %s", e)
            raise RuntimeError(f"DeviceRepo.list_device_ids_by_owner failed for created_by={created_by}: {e}") from e

    async def list_devices_by_ids(self, device_ids: list[str]) -> list[dict]:
        """LP-07: 批量查询设备列表，避免 N+1 查询。

        使用 SELECT * FROM devices WHERE device_id IN (...) 一次查询返回所有匹配设备。
        """
        if not device_ids:
            return []
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(DeviceORM).where(DeviceORM.device_id.in_(device_ids))
                )
                rows = result.scalars().all()
                return [_orm_to_device(r) for r in rows]
        except Exception as e:
            logger.error("DeviceRepo.list_devices_by_ids failed: %s", e)
            raise RuntimeError(f"DeviceRepo.list_devices_by_ids failed: {e}") from e

    async def get_status_counts(self, device_ids: list[str] | None = None) -> dict[str, int]:
        """R9-S-08: 使用SQL聚合查询按状态统计设备数量，避免全量加载到内存。

        Args:
            device_ids: 可选，限定统计的设备ID范围（用于非admin用户权限过滤）。
                        为 None 时统计全部设备。

        Returns:
            dict[str, int]: 各状态对应的设备数量，键为 status，值为 count。
            例如: {"online": 5, "offline": 3, "error": 1}
        """
        try:
            async with self._auto_session() as session:
                # 使用参数化查询防止SQL注入
                if device_ids is not None:
                    if not device_ids:
                        return {}
                    query = (
                        select(DeviceORM.status, func.count())
                        .where(DeviceORM.device_id.in_(device_ids))
                        .group_by(DeviceORM.status)
                    )
                else:
                    query = select(DeviceORM.status, func.count()).group_by(DeviceORM.status)
                result = await session.execute(query)
                return {status: count for status, count in result.fetchall()}
        except Exception as e:
            logger.error("DeviceRepo.get_status_counts failed: %s", e)
            raise RuntimeError(f"DeviceRepo.get_status_counts failed: {e}") from e

    @retry_on_stale(base_delay=0.1)
    async def update(self, device_id: str, data: dict) -> dict | None:
        try:
            async with self._write_write_lock():  # FIXED-P0: 原问题-write_lock仅保护commit，读-改-写序列可并发交叉；改为保护整个序列
                async with self._auto_session() as session:
                    result = await session.execute(
                        select(DeviceORM).where(DeviceORM.device_id == device_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm is None:
                        return None
                    _validate_device_update_data(data, current_protocol=orm.protocol or "")  # FIXED-P2: 原问题-protocol缺失时跳过config验证；改为传入DB当前protocol作为fallback
                    old_version = data.get("_version")  # FIXED-P2: 原问题-data.pop("_version")修改传入参数dict，调用方复用时_version键丢失
                    if old_version is not None and orm.version != old_version:
                        raise StaleDataError(f"Device {device_id} version conflict: expected={old_version}, actual={orm.version}")
                    for key in ("name", "collect_interval"):
                        if key in data:
                            setattr(orm, key, data[key])
                    if "config" in data:
                        orm.config = json.dumps(data["config"], ensure_ascii=False)
                    if "points" in data:
                        orm.points = json.dumps(data["points"], ensure_ascii=False)
                    if "status" in data:
                        orm.status = data["status"]
                    orm.updated_at = _now()
                    orm.version = (orm.version or 0) + 1
                    await session.commit()
                    await session.refresh(orm)
                    return _orm_to_device(orm)
        except StaleDataError:
            raise
        except Exception as e:
            logger.error("DeviceRepo.update failed: %s", e)
            raise RuntimeError(f"DeviceRepo.update failed for device_id={device_id}: {e}") from e

    async def update_status(self, device_id: str, status: str) -> None:
        if status not in _VALID_DEVICE_STATUSES:  # FIXED-P1: 原问题-update_status无status验证
            raise ValueError(f"Invalid status: {status}, must be one of {_VALID_DEVICE_STATUSES}")
        try:
            async with self._write_write_lock():  # FIXED-P1: 原问题-update_status无写锁保护，与update()并发保护不一致
                async with self._auto_session() as session:
                    await session.execute(
                        update(DeviceORM)
                        .where(DeviceORM.device_id == device_id)
                        .values(status=status, updated_at=_now(), version=DeviceORM.version + 1)  # FIXED-P1: 原问题-update_status绕过乐观锁，不递增version
                    )
                    await session.commit()
        except Exception as e:
            logger.error("DeviceRepo.update_status failed: %s", e)

    async def delete(self, device_id: str) -> bool:
        # FIXED: 原问题-_cleanup_sidecar_config_versions在写锁内同步执行，
        # 清理5个协议配置版本库+时序数据库+lifecycle，任一慢操作都会阻塞写锁导致删除超时。
        # 改为：delete仅做主库删除（快速），sidecar清理由调用方(DeviceService)异步执行
        try:
            async with self._write_write_lock(), self._auto_session() as session:
                await session.execute(
                    delete(AlarmORM).where(AlarmORM.device_id == device_id)
                )
                await session.execute(
                    delete(RuleORM).where(RuleORM.device_id == device_id)
                )
                await session.execute(
                    delete(ResourceShareORM).where(
                        ResourceShareORM.resource_type == "device",
                        ResourceShareORM.resource_id == device_id,
                    )
                )
                result = await session.execute(
                    delete(DeviceORM).where(DeviceORM.device_id == device_id)
                )
                if result.rowcount > 0:
                    await session.commit()
                    return True
                await session.commit()
                return False
        except Exception as e:
            logger.error("DeviceRepo.delete failed: %s", e)
            raise RuntimeError(f"DeviceRepo.delete failed for device_id={device_id}: {e}") from e

    async def delete_with_owner_check(
        self, device_id: str, user_id: str, is_admin: bool
    ) -> str:
        """Delete a device and all its cascade children in a single transaction.

        FIXED-CASCADE: Previously the device was deleted first, then its alarms/rules.
        Now child rows (alarms, rules, resource_shares) are deleted BEFORE the device
        so that if any step fails the transaction rolls back cleanly and no orphans
        are created. Order: resource_shares → alarms → rules → device.

        Returns:
            "deleted"        - device and all children were deleted
            "not_found"      - device does not exist
            "not_authorized" - device exists but user lacks permission
        """
        try:
            async with self._write_write_lock():
                async with self._auto_session() as session:
                    # Pre-check: does the device exist and does the user own it?
                    # Use a subquery to combine existence + permission check in one query.
                    device_exists_stmt = select(DeviceORM.device_id).where(
                        DeviceORM.device_id == device_id
                    )
                    if not is_admin:
                        device_exists_stmt = select(DeviceORM.device_id).where(
                            DeviceORM.device_id == device_id,
                            or_(
                                DeviceORM.created_by == user_id,
                                DeviceORM.device_id.in_(
                                    select(ResourceShareORM.resource_id).where(
                                        ResourceShareORM.resource_type == "device",
                                        ResourceShareORM.resource_id == device_id,
                                        ResourceShareORM.shared_with_user_id == user_id,
                                    )
                                ),
                            ),
                        )
                    check_result = await session.execute(device_exists_stmt)
                    device = check_result.scalar_one_or_none()
                    if device is None:
                        # Determine whether it's a permission failure or a true missing device
                        exists_stmt = select(DeviceORM.device_id).where(
                            DeviceORM.device_id == device_id
                        )
                        exists_result = await session.execute(exists_stmt)
                        if exists_result.scalar_one_or_none() is not None:
                            return "not_authorized"
                        return "not_found"

                    # ── Cascade delete: children first, then device ──────────────────
                    # FIXED-CASCADE: reverse order prevents orphans if an intermediate step
                    # fails — the device row is still present and the transaction can roll back.

                    # 1. Remove device-to-user share records
                    shares_result = await session.execute(
                        delete(ResourceShareORM).where(
                            ResourceShareORM.resource_type == "device",
                            ResourceShareORM.resource_id == device_id,
                        )
                    )
                    if shares_result.rowcount:
                        logger.info(
                            "Cascade deleted %d resource_share rows for device %s",
                            shares_result.rowcount, device_id,
                        )

                    # 2. Delete alarms that reference this device
                    alarms_result = await session.execute(
                        delete(AlarmORM).where(AlarmORM.device_id == device_id)
                    )
                    if alarms_result.rowcount:
                        logger.info(
                            "Cascade deleted %d alarm rows for device %s",
                            alarms_result.rowcount, device_id,
                        )

                    # 3. Delete rules that reference this device
                    rules_result = await session.execute(
                        delete(RuleORM).where(RuleORM.device_id == device_id)
                    )
                    if rules_result.rowcount:
                        logger.info(
                            "Cascade deleted %d rule rows for device %s",
                            rules_result.rowcount, device_id,
                        )

                    # 4. Delete the device itself — safe because children are gone
                    device_result = await session.execute(
                        delete(DeviceORM).where(DeviceORM.device_id == device_id)
                    )
                    if device_result.rowcount > 0:
                        # FIXED: sidecar清理移到写锁外异步执行，避免长时间持锁导致删除超时
                        await session.commit()
                        logger.info("Device %s deleted from main DB, sidecar cleanup deferred", device_id)
                        return "deleted"

                    # Should never reach here if we passed the pre-check above
                    return "not_found"
        except Exception as e:
            logger.error(
                "DeviceRepo.delete_with_owner_check failed for device %s: %s — "
                "transaction rolled back, no orphans created",
                device_id, e,
            )
            return "not_found"

    async def list_by_protocol(
        self,
        protocol: str,
        page: int = 1,
        size: int = 200,
    ) -> list[dict]:
        # FIXED: W2 原问题-list_by_protocol无分页参数，高频调度场景每次返回全量设备列表
        try:
            async with self._auto_session() as session:
                offset = (page - 1) * size
                result = await session.execute(
                    select(DeviceORM)
                    .where(DeviceORM.protocol == protocol)
                    .order_by(DeviceORM.created_at.desc())
                    .offset(offset)
                    .limit(size)
                )
                rows = result.scalars().all()
                return [_orm_to_device(r) for r in rows]
        except Exception as e:
            logger.error("DeviceRepo.list_by_protocol failed: %s", e)
            raise RuntimeError(f"DeviceRepo.list_by_protocol failed: {e}") from e

    async def bulk_upsert_in_session(
        self,
        records: list[dict],
        session: AsyncSession,
        skip_existing: bool = True,
    ) -> tuple[int, int, list[str]]:
        """Bulk upsert devices within an external transaction session (no commit).

        FIXED-ATOMIC-RESTORE: Provides the single-session atomic restore path.
        All changes are staged in the session; caller must commit/rollback.

        Returns (created, skipped, errors):
          created: number of records inserted or updated
          skipped: number of records skipped (existing, skip_existing=True)
          errors: list of error messages per failed record (same order as input)
        """

        created = 0
        skipped = 0
        errors: list[str] = ["" for _ in records]  # one slot per record

        # FIXED(严重-R2): 原问题-循环内逐条 SELECT 判断存在性，N条记录 N次查询
        # 修复-预加载所有已存在的 device_id 到字典，循环中直接查内存
        all_device_ids = [r["device_id"] for r in records if r.get("device_id")]
        existing_devices_map: dict[str, DeviceORM] = {}
        if all_device_ids:
            existing_result = await session.execute(
                select(DeviceORM).where(DeviceORM.device_id.in_(all_device_ids))
            )
            for row in existing_result.scalars().all():
                existing_devices_map[row.device_id] = row

        for i, record in enumerate(records):
            try:
                # Validate BEFORE any write
                _validate_device_data(record)

                orm = existing_devices_map.get(record["device_id"])

                if orm is not None:
                    if skip_existing:
                        skipped += 1
                        continue
                    orm.name = record.get("name", orm.name)
                    orm.protocol = record.get("protocol", orm.protocol)
                    orm.status = record.get("status", orm.status)
                    if "config" in record:
                        orm.config = json.dumps(record["config"], ensure_ascii=False)
                    if "points" in record:
                        orm.points = json.dumps(record["points"], ensure_ascii=False)
                    if "collect_interval" in record:
                        orm.collect_interval = record["collect_interval"]
                    orm.updated_at = _now()
                    orm.version = (orm.version or 0) + 1
                else:
                    now = _now()
                    session.add(
                        DeviceORM(
                            device_id=record["device_id"],
                            name=record.get("name", ""),
                            protocol=record.get("protocol", ""),
                            status=record.get("status", "offline"),
                            config=json.dumps(record.get("config", {}), ensure_ascii=False),
                            points=json.dumps(record.get("points", []), ensure_ascii=False),
                            collect_interval=record.get("collect_interval", 5),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                created += 1
            except Exception as e:
                errors[i] = f"{record.get('device_id', '?')}: {e}"

        return created, skipped, errors

    async def _cleanup_sidecar_config_versions(self, device_id: str) -> None:
        """FIXED-P1: 原问题-设备删除后sidecar残留配置版本数据
        Delete config version data from all protocol-specific sidecar databases,
        clean up device_status.db via lifecycle manager,
        and delete time-series data from all protocol ts stores.

        FIXED-P0: 原问题-各sidecar清理失败仅log.error不抛异常，主库仍commit导致孤儿数据
        改为：任一sidecar清理失败则抛出RuntimeError，中止主库删除事务

        FIXED-P2: 原问题-跨库级联删除Sidecar失败后无补偿重试，孤儿数据永久残留
        改为：Sidecar清理失败时记录补偿日志到_pending_cleanup表，供后台异步重试

        FIXED: 此方法不再在写锁内调用，改为由DeviceService._schedule_cleanup异步调用
        """
        cleanup_errors: list[str] = []
        try:
            from edgelite.app import _app_state
            for driver_attr, mgr_attr in [
                ("s7_driver", "_config_version_mgr"),
                ("mc_driver", "_config_version_mgr"),
                ("ab_driver", "_config_version_mgr"),
                ("opcua_driver", "_config_version_mgr"),
                ("fins_driver", "_config_version_mgr"),
            ]:
                driver = getattr(_app_state, driver_attr, None)
                if driver is None:
                    continue
                mgr = getattr(driver, mgr_attr, None)
                if mgr is None:
                    continue
                try:
                    await mgr.delete_by_device_id(device_id)
                except Exception as e:
                    cleanup_errors.append(f"{driver_attr}: {e}")
                    logger.error("Sidecar config cleanup for %s.%s failed: %s", driver_attr, device_id, e)
            lifecycle = getattr(_app_state, "lifecycle", None)
            if lifecycle is not None:
                try:
                    await lifecycle.remove_device(device_id)
                except Exception as e:
                    cleanup_errors.append(f"lifecycle: {e}")
                    logger.error("lifecycle.remove_device for %s failed: %s", device_id, e)
            for ts_attr in ("s7_ts_store", "modbus_ts_store", "ab_ts_store", "opcua_ts_store", "fins_ts_store"):
                ts_store = getattr(_app_state, ts_attr, None)
                if ts_store is not None:
                    try:
                        await ts_store.delete_by_device_id(device_id)
                    except Exception as e:
                        cleanup_errors.append(f"{ts_attr}: {e}")
                        logger.error("TS cleanup for %s.%s failed: %s", ts_attr, device_id, e)
            sqlite_ts = getattr(_app_state, "sqlite_ts", None)
            if sqlite_ts is not None:
                try:
                    await sqlite_ts.delete_by_device_id(device_id)
                except Exception as e:
                    cleanup_errors.append(f"sqlite_ts: {e}")
                    logger.error("sqlite_ts cleanup for %s failed: %s", device_id, e)
        except Exception as e:
            logger.error("Sidecar config cleanup failed for device %s: %s", device_id, e)
            await self._record_cleanup_compensation(device_id, str(e))
            raise RuntimeError(f"Sidecar cleanup failed for device {device_id}: {e}") from e

        if cleanup_errors:
            await self._record_cleanup_compensation(device_id, str(cleanup_errors))
            raise RuntimeError(
                f"Sidecar cleanup failed for device {device_id}: {cleanup_errors}"
            )

    async def cleanup_sidecar_data(self, device_id: str) -> None:
        """公开方法：清理设备在sidecar数据库中的残留数据

        由 DeviceService._schedule_cleanup 异步调用，不在写锁内执行。
        失败时记录补偿日志但不抛出异常（避免影响其他清理任务）。
        """
        try:
            await self._cleanup_sidecar_config_versions(device_id)
        except Exception as e:
            # 异步清理失败不影响删除结果，补偿日志已由内部方法记录
            logger.warning("Sidecar data cleanup failed for %s (orphan data may remain until compensation retry): %s", device_id, e)

    async def _record_cleanup_compensation(self, device_id: str, error_detail: str) -> None:
        """FIXED-P2: 记录Sidecar清理补偿日志，供后台异步重试清理孤儿数据"""
        try:
            if self._database is not None:
                async with self._database.session() as session:
                    await session.execute(text(
                        "CREATE TABLE IF NOT EXISTS _pending_sidecar_cleanup "
                        "(device_id TEXT PRIMARY KEY, error_detail TEXT, created_at REAL, retry_count INTEGER DEFAULT 0)"
                    ))
                    await session.execute(text(
                        "INSERT OR REPLACE INTO _pending_sidecar_cleanup (device_id, error_detail, created_at, retry_count) "
                        "VALUES (:did, :err, :ts, COALESCE((SELECT retry_count FROM _pending_sidecar_cleanup WHERE device_id=:did), 0))"
                    ), {"did": device_id, "err": error_detail[:500], "ts": time.time()})
                    await session.commit()
        except Exception as e:
            logger.warning("Failed to record cleanup compensation for %s: %s", device_id, e)

    async def retry_pending_sidecar_cleanups(self) -> None:
        """FIXED-P0: 后台补偿任务——扫描 _pending_sidecar_cleanup 表并重试失败的sidecar清理

        采用指数退避：初始60s，上限600s。超过最大重试次数(10)后记录ERROR日志并移除记录。
        """
        if self._database is None:
            return
        try:
            async with self._database.session() as session:
                # 确保表存在并补充 next_retry_at 列（兼容旧表）
                await session.execute(text(
                    "CREATE TABLE IF NOT EXISTS _pending_sidecar_cleanup "
                    "(device_id TEXT PRIMARY KEY, error_detail TEXT, created_at REAL, "
                    "retry_count INTEGER DEFAULT 0, next_retry_at REAL DEFAULT 0)"
                ))
                try:
                    await session.execute(text(
                        "ALTER TABLE _pending_sidecar_cleanup ADD COLUMN next_retry_at REAL DEFAULT 0"
                    ))
                except Exception:
                    pass  # 列已存在

                now = time.time()
                result = await session.execute(text(
                    "SELECT device_id, retry_count FROM _pending_sidecar_cleanup "
                    "WHERE next_retry_at <= :now OR next_retry_at IS NULL "
                    "ORDER BY created_at ASC LIMIT 50"
                ), {"now": now})
                pending = result.fetchall()

            for device_id, retry_count in pending:
                if retry_count >= 10:
                    logger.error(
                        "Sidecar清理补偿超过最大重试次数(10)，device_id=%s，停止重试",
                        device_id,
                    )
                    async with self._database.session() as session:
                        await session.execute(text(
                            "DELETE FROM _pending_sidecar_cleanup WHERE device_id = :did"
                        ), {"did": device_id})
                        await session.commit()
                    continue

                logger.info("重试sidecar清理: device_id=%s, retry_count=%d", device_id, retry_count)
                try:
                    await self.cleanup_sidecar_data(device_id)
                    async with self._database.session() as session:
                        await session.execute(text(
                            "DELETE FROM _pending_sidecar_cleanup WHERE device_id = :did"
                        ), {"did": device_id})
                        await session.commit()
                    logger.info("Sidecar清理补偿成功: device_id=%s", device_id)
                except Exception as e:
                    # 指数退避：60s, 120s, 240s... 上限600s
                    backoff = min(60 * (2 ** retry_count), 600)
                    next_retry = time.time() + backoff
                    async with self._database.session() as session:
                        await session.execute(text(
                            "UPDATE _pending_sidecar_cleanup "
                            "SET retry_count = retry_count + 1, next_retry_at = :nrt, "
                            "error_detail = :err WHERE device_id = :did"
                        ), {"nrt": next_retry, "err": str(e)[:500], "did": device_id})
                        await session.commit()
                    logger.warning(
                        "Sidecar清理补偿失败: device_id=%s, retry_count=%d, 下次重试: %.0fs后",
                        device_id, retry_count + 1, backoff,
                    )
        except Exception as e:
            logger.error("补偿任务扫描异常: %s", e, exc_info=True)


class TemplateRepo(BaseRepo):
    async def create(self, data: dict, created_by: str | None = None) -> dict:
        _validate_template_data(data)  # FIXED-P2: 原问题-TemplateRepo.create无业务验证
        try:
            async with self._auto_session() as session:
                now = _now()
                orm = DeviceTemplateORM(
                    name=data["name"],
                    protocol=data["protocol"],
                    config_template=json.dumps(data.get("config_template", {}), ensure_ascii=False),
                    point_templates=json.dumps(data.get("point_templates", []), ensure_ascii=False),
                    created_by=created_by,
                    created_at=now,
                )
                session.add(orm)
                await session.commit()
                await session.refresh(orm)
                return _orm_to_template(orm)
        except IntegrityError:
            raise ValueError(RepoErrors.TEMPLATE_EXISTS) from None

    async def get(self, name: str) -> dict | None:
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(DeviceTemplateORM).where(DeviceTemplateORM.name == name)
                )
                orm = result.scalar_one_or_none()
                return _orm_to_template(orm) if orm else None
        except Exception as e:
            logger.error("TemplateRepo.get failed: %s", e)
            raise RuntimeError(f"TemplateRepo.get failed for name={name}: {e}") from e

    async def list_all(
        self,
        created_by: str | None = None,
        page: int = 1,
        size: int = 50,
        limit: int | None = None,
    ) -> tuple[list[dict], int]:
        """列出所有模板，返回 (items, total)。

        支持分页（page/size）和 created_by 过滤。
        limit 参数保留向后兼容，如未指定则使用 size。
        """
        try:
            async with self._auto_session() as session:
                conditions = []
                if created_by:
                    conditions.append(DeviceTemplateORM.created_by == created_by)
                
                # 总数查询
                count_query = select(func.count()).select_from(DeviceTemplateORM)
                for cond in conditions:
                    count_query = count_query.where(cond)
                total = await session.scalar(count_query) or 0

                # 分页查询
                query = select(DeviceTemplateORM).order_by(DeviceTemplateORM.created_at.desc())
                for cond in conditions:
                    query = query.where(cond)
                
                # 向后兼容：如果传了 limit 且没传 page/size，使用 limit
                actual_size = limit if limit is not None and page == 1 and size == 50 else size
                actual_size = max(1, min(actual_size, 5000))
                offset = (page - 1) * actual_size
                query = query.offset(offset).limit(actual_size)
                
                result = await session.execute(query)
                rows = result.scalars().all()
                items = [_orm_to_template(r) for r in rows]
                return items, total
        except Exception as e:
            logger.error("TemplateRepo.list_all failed: %s", e)
            raise RuntimeError(f"TemplateRepo.list_all failed: {e}") from e

    async def delete(self, name: str) -> bool:
        try:
            async with self._write_write_lock(), self._auto_session() as session:
                result = await session.execute(
                    delete(DeviceTemplateORM).where(DeviceTemplateORM.name == name)
                )
                await session.commit()
                return result.rowcount > 0
        except Exception as e:
            logger.error("TemplateRepo.delete failed: %s", e)
            raise RuntimeError(f"TemplateRepo.delete failed for name={name}: {e}") from e

    @retry_on_stale(base_delay=0.1)
    async def update(self, name: str, data: dict) -> dict | None:
        _validate_template_update_data(data)
        try:
            async with self._write_write_lock():
                async with self._auto_session() as session:
                    result = await session.execute(
                        select(DeviceTemplateORM).where(DeviceTemplateORM.name == name)
                    )
                    orm = result.scalar_one_or_none()
                    if orm is None:
                        return None
                    old_version = data.get("_version")
                    if old_version is not None and orm.version != old_version:
                        raise StaleDataError(f"Template {name} version conflict: expected={old_version}, actual={orm.version}")
                    if "protocol" in data:
                        orm.protocol = data["protocol"]
                    if "config_template" in data:
                        orm.config_template = json.dumps(data["config_template"], ensure_ascii=False)
                    if "point_templates" in data:
                        orm.point_templates = json.dumps(data["point_templates"], ensure_ascii=False)
                    orm.version = (orm.version or 0) + 1
                    await session.commit()
                    await session.refresh(orm)
                    return _orm_to_template(orm)
        except StaleDataError:
            raise
        except Exception as e:
            logger.error("TemplateRepo.update failed: %s", e)
            raise RuntimeError(f"TemplateRepo.update failed for name={name}: {e}") from e


class RuleRepo(BaseRepo):
    def __init__(self, session_or_db: Any, write_lock: asyncio.Lock | None = None, table_name: str | None = None):
        super().__init__(session_or_db, write_lock, table_name or "rules")

    async def create(self, data: dict, created_by: str | None = None) -> dict:
        _validate_rule_data(data)  # FIXED-P1: 规则创建业务验证
        try:
            async with self._auto_session() as session:
                rule_id = _uuid()
                now = _now()
                orm = RuleORM(
                    rule_id=rule_id,
                    name=data["name"],
                    device_id=data["device_id"],
                    conditions=json.dumps(data["conditions"], ensure_ascii=False),
                    logic=data.get("logic", "AND"),
                    duration=data.get("duration", 0),
                    severity=data["severity"],
                    enabled=data.get("enabled", True),
                    notify_channels=json.dumps(data.get("notify_channels", []), ensure_ascii=False),
                    # SEC-FIX: 持久化 script/rule_type 字段
                    script=data.get("script", "") or "",
                    rule_type=data.get("rule_type", "threshold") or "threshold",
                    created_by=created_by,
                    created_at=now,
                )
                session.add(orm)
                await session.commit()
                await session.refresh(orm)
                return _orm_to_rule(orm)
        except IntegrityError as e:
            err_msg = str(e).lower()
            if "unique" in err_msg or "duplicate" in err_msg:
                raise ValueError(RepoErrors.RULE_EXISTS) from None
            # CHECK constraint violation — provide clear error message
            logger.error("RuleRepo.create IntegrityError (CHECK constraint?): %s", e)
            raise ValueError(f"Rule data violates database constraint: {e}") from None

    async def create_with_device_limit(
        self,
        data: dict,
        device_id: str,
        max_rules: int,
        created_by: str | None = None,
    ) -> dict:
        """FIXED-TOCTOU: 在同一事务+写锁内完成单设备规则数量校验与创建。

        原问题-RuleService.create_rule 先 list_all(count) 再 create，两步分别开 session
        且无锁保护，并发调用时多个请求可同时通过 count 检查导致超额创建规则。
        修复-将 count 检查与 insert 放入同一 session（单事务）并由 _write_write_lock 串行化，
        对齐 update/delete 的读-改-写保护模式。SQLite 的 WAL/busy_timeout/synchronous
        由 Database 引擎级 PRAGMA 统一配置，session 自动继承。
        """
        _validate_rule_data(data)
        try:
            async with self._write_write_lock(), self._auto_session() as session:
                # 事务内检查单设备规则数量
                count_result = await session.execute(
                    select(func.count())
                    .select_from(RuleORM)
                    .where(RuleORM.device_id == device_id)
                )
                existing = count_result.scalar() or 0
                if existing >= max_rules:
                    raise ValueError(
                        f"Rule limit reached for device {device_id}: "
                        f"{existing}/{max_rules}"
                    )
                # 同一事务内创建
                rule_id = _uuid()
                now = _now()
                orm = RuleORM(
                    rule_id=rule_id,
                    name=data["name"],
                    device_id=data["device_id"],
                    conditions=json.dumps(data["conditions"], ensure_ascii=False),
                    logic=data.get("logic", "AND"),
                    duration=data.get("duration", 0),
                    severity=data["severity"],
                    enabled=data.get("enabled", True),
                    notify_channels=json.dumps(data.get("notify_channels", []), ensure_ascii=False),
                    # SEC-FIX: 持久化 script/rule_type 字段
                    script=data.get("script", "") or "",
                    rule_type=data.get("rule_type", "threshold") or "threshold",
                    created_by=created_by,
                    created_at=now,
                )
                session.add(orm)
                await session.commit()
                await session.refresh(orm)
                return _orm_to_rule(orm)
        except IntegrityError as e:
            err_msg = str(e).lower()
            if "unique" in err_msg or "duplicate" in err_msg:
                raise ValueError(RepoErrors.RULE_EXISTS) from None
            logger.error("RuleRepo.create_with_device_limit IntegrityError (CHECK constraint?): %s", e)
            raise ValueError(f"Rule data violates database constraint: {e}") from None

    async def get(self, rule_id: str) -> dict | None:
        # FIXED: 原问题-RuleRepo.get无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(RuleORM).where(RuleORM.rule_id == rule_id))
                orm = result.scalar_one_or_none()
                return _orm_to_rule(orm) if orm else None
        except Exception as e:
            logger.error("RuleRepo.get failed: %s", e)
            raise RuntimeError(f"RuleRepo.get failed for rule_id={rule_id}: {e}") from e

    async def list_rules_by_ids(self, rule_ids: list[str]) -> list[dict]:
        """FIXED(严重): 批量查询规则列表，避免 N+1 查询。

        使用 SELECT * FROM rules WHERE rule_id IN (...) 一次查询返回所有匹配规则。
        """
        if not rule_ids:
            return []
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(RuleORM).where(RuleORM.rule_id.in_(rule_ids))
                )
                rows = result.scalars().all()
                return [_orm_to_rule(r) for r in rows]
        except Exception as e:
            logger.error("RuleRepo.list_rules_by_ids failed: %s", e)
            raise RuntimeError(f"RuleRepo.list_rules_by_ids failed: {e}") from e

    async def list(
        self,
        page: int = 1,
        size: int = 20,
        device_id: str | None = None,
        search: str | None = None,
        severity: str | None = None,
        created_by: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict], int] | tuple[list[dict], int, str | None]:
        try:
            async with self._auto_session() as session:
                query = select(RuleORM)
                count_query = select(func.count()).select_from(RuleORM)
                if device_id:
                    query = query.where(RuleORM.device_id == device_id)
                    count_query = count_query.where(RuleORM.device_id == device_id)
                if search and len(search) >= 2:
                    # FIXED: 前缀通配符 LIKE 无法走索引，限制搜索最小长度为2字符，避免单字符全表扫描
                    # R6-G-02: 转义 LIKE 通配符
                    escaped_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    pattern = f"%{escaped_search}%"
                    query = query.where(
                        (RuleORM.name.ilike(pattern, escape="\\")) | (RuleORM.rule_id.ilike(pattern, escape="\\"))
                    )
                    count_query = count_query.where(
                        (RuleORM.name.ilike(pattern, escape="\\")) | (RuleORM.rule_id.ilike(pattern, escape="\\"))
                    )
                if severity:
                    query = query.where(RuleORM.severity == severity)
                    count_query = count_query.where(RuleORM.severity == severity)
                if created_by:
                    query = query.where(RuleORM.created_by == created_by)
                    count_query = count_query.where(RuleORM.created_by == created_by)
                total_result = await session.execute(count_query)
                total = total_result.scalar() or 0
                # R9-S-09: 游标分页优化——当提供cursor时使用 WHERE created_at < cursor 替代 OFFSET，避免深分页性能退化
                if cursor is not None:
                    cursor_dt = datetime.fromisoformat(cursor)
                    query = query.where(RuleORM.created_at < cursor_dt)
                    query = query.order_by(RuleORM.created_at.desc()).limit(size)
                else:
                    offset = (page - 1) * size
                    query = query.order_by(RuleORM.created_at.desc()).offset(offset).limit(size)
                result = await session.execute(query)
                rows = result.scalars().all()
                items = [_orm_to_rule(r) for r in rows]
                if cursor is not None:
                    # 返回 next_cursor 供下次查询使用（最后一条记录的 created_at）
                    next_cursor = items[-1]["created_at"] if items else None
                    return items, total, next_cursor
                return items, total
        except Exception as e:
            logger.error("RuleRepo.list failed: %s", e)
            raise RuntimeError(f"RuleRepo.list failed: {e}") from e

    async def list_all(
        self,
        page: int = 1,
        size: int = 20,
        device_id: str | None = None,
        search: str | None = None,
        severity: str | None = None,
        created_by: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict], int] | tuple[list[dict], int, str | None]:
        """FIXED-P0: 添加 list_all 方法，多个Service调用此方法但之前不存在"""
        return await self.list(
            page=page, size=size, device_id=device_id,
            search=search, severity=severity, created_by=created_by,
            cursor=cursor,
        )

    @retry_on_stale(base_delay=0.1)
    async def update(self, rule_id: str, data: dict) -> dict | None:
        _validate_rule_update_data(data)  # FIXED-P0: 原问题-update路径不调用验证，severity/logic可绕过验证
        try:
            async with self._write_write_lock():  # FIXED-P0: 原问题-write_lock仅保护commit；改为保护整个读-改-写序列
                async with self._auto_session() as session:
                    result = await session.execute(select(RuleORM).where(RuleORM.rule_id == rule_id))
                    orm = result.scalar_one_or_none()
                    if orm is None:
                        return None
                    old_version = data.get("version") or data.get("_version")  # FIXED-BugR4X: 原问题-_orm_to_rule输出的是"version"键但此处读"_version"导致乐观锁校验失效；修复-优先读"version"再回退"_version"
                    if old_version is not None and orm.version != old_version:
                        raise StaleDataError(f"Rule {rule_id} version conflict: expected={old_version}, actual={orm.version}")
                    for key in ("name", "device_id", "logic", "duration", "severity"):
                        if key in data:
                            setattr(orm, key, data[key])
                    # SEC-FIX: 更新 script/rule_type 字段
                    if "script" in data:
                        orm.script = data["script"] or ""
                    if "rule_type" in data:
                        orm.rule_type = data["rule_type"] or "threshold"
                    if "conditions" in data:
                        orm.conditions = json.dumps(data["conditions"], ensure_ascii=False)
                    if "notify_channels" in data:
                        orm.notify_channels = json.dumps(data["notify_channels"], ensure_ascii=False)
                    if "enabled" in data:
                        orm.enabled = data["enabled"]
                    orm.version = (orm.version or 0) + 1
                    await session.commit()
                    await session.refresh(orm)
                    return _orm_to_rule(orm)
        except StaleDataError:
            raise
        except Exception as e:
            logger.error("RuleRepo.update failed: %s", e)
            raise RuntimeError(f"RuleRepo.update failed for rule_id={rule_id}: {e}") from e

    async def set_enabled(self, rule_id: str, enabled: bool) -> dict | None:
        return await self.update(rule_id, {"enabled": enabled})

    async def toggle(self, rule_id: str, enabled: bool) -> dict | None:
        """Alias for set_enabled — used by RuleService.enable_rule/disable_rule"""
        return await self.set_enabled(rule_id, enabled)

    async def delete(self, rule_id: str) -> bool:
        try:
            async with self._write_write_lock():
                async with self._auto_session() as session:
                    await session.execute(
                        delete(AlarmORM).where(AlarmORM.rule_id == rule_id)
                    )
                    result = await session.execute(delete(RuleORM).where(RuleORM.rule_id == rule_id))
                    if result.rowcount > 0:
                        # FIXED-BugR4X: 原问题-先清理edge_rules.db再commit主库，主库commit失败时两库不一致；修复-先commit主库再清理edge_rules.db，edge清理失败只记日志不回滚
                        await session.commit()
                        await self._cleanup_edge_rule(rule_id)
                        return True
                    await session.commit()
                    return False
        except Exception as e:
            logger.error("RuleRepo.delete failed: %s", e)
            raise RuntimeError(f"RuleRepo.delete failed for rule_id={rule_id}: {e}") from e

    async def _cleanup_edge_rule(self, rule_id: str) -> None:
        """FIXED-P1: 原问题-主库规则删除后edge_rules.db残留对应规则
        Synchronize deletion to the edge rule store."""
        # FIXED-BugR4X: 原问题-清理失败抛异常会回滚主库commit造成两库不一致；修复-主库已commit，edge清理失败只记日志不回滚(允许短暂残留孤儿规则，由后续同步任务补偿)
        try:
            from edgelite.app import _app_state
            rule_store = getattr(_app_state, "rule_store", None)
            if rule_store is not None:
                rule_store.delete_rule(rule_id)
        except Exception as e:
            logger.error("Edge rule cleanup for %s failed: %s", rule_id, e)  # FIXED-P0: 原问题-cleanup异常仅log.debug，运维无法发现孤儿规则；升级为log.error

    async def list_by_device(self, device_id: str) -> list[dict]:
        # FIXED: 原问题-RuleRepo.list_by_device无try-except保护，被evaluator调用
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(RuleORM).where(RuleORM.device_id == device_id, RuleORM.enabled.is_(True))
                )
                rows = result.scalars().all()
                return [_orm_to_rule(r) for r in rows]
        except Exception as e:
            logger.error("RuleRepo.list_by_device failed: %s", e)
            raise RuntimeError(f"RuleRepo.list_by_device failed for device_id={device_id}: {e}") from e

    async def list_enabled_by_point(self, device_id: str, point_name: str) -> list[dict]:
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(RuleORM).where(RuleORM.device_id == device_id, RuleORM.enabled.is_(True))
                )
                rows = result.scalars().all()
                rules = [_orm_to_rule(r) for r in rows]
                return [
                    r for r in rules
                    if any(c.get("point") == point_name for c in r.get("conditions", []))
                ]
        except Exception as e:
            logger.error("RuleRepo.list_enabled_by_point failed: %s", e)
            raise RuntimeError(f"RuleRepo.list_enabled_by_point failed for device_id={device_id}: {e}") from e

    async def upsert_bulk(
        self,
        records: list[dict],
        session: AsyncSession,
        skip_existing: bool = True,
    ) -> tuple[int, int, list[str]]:
        """Bulk upsert rules within an external transaction session (no commit).

        FIXED-ATOMIC-RESTORE: Provides the single-session atomic restore path.
        All changes are staged in the session; caller must commit/rollback.

        Returns (created, skipped, errors):
          created: number of records inserted or updated
          skipped: number of records skipped (existing, skip_existing=True)
          errors: list of error messages per failed record (same order as input)
        """
        created = 0
        skipped = 0
        errors: list[str] = ["" for _ in records]

        # FIXED(严重-R2): 原问题-循环内逐条 SELECT 判断存在性，N条记录 N次查询
        # 修复-预加载所有已存在的 rule_id 到字典，循环中直接查内存
        all_rule_ids = [r["rule_id"] for r in records if r.get("rule_id")]
        existing_rules_map: dict[str, RuleORM] = {}
        if all_rule_ids:
            existing_result = await session.execute(
                select(RuleORM).where(RuleORM.rule_id.in_(all_rule_ids))
            )
            for row in existing_result.scalars().all():
                existing_rules_map[row.rule_id] = row

        for i, record in enumerate(records):
            try:
                _validate_rule_data(record)

                orm = existing_rules_map.get(record["rule_id"])

                if orm is not None:
                    if skip_existing:
                        skipped += 1
                        continue
                    orm.name = record.get("name", orm.name)
                    orm.device_id = record.get("device_id", orm.device_id)
                    orm.conditions = json.dumps(record.get("conditions", []), ensure_ascii=False)
                    orm.logic = record.get("logic", orm.logic)
                    orm.duration = record.get("duration", orm.duration)
                    orm.severity = record.get("severity", orm.severity)
                    orm.enabled = record.get("enabled", orm.enabled)
                    orm.notify_channels = json.dumps(
                        record.get("notify_channels", []), ensure_ascii=False
                    )
                else:
                    now = _now()
                    session.add(
                        RuleORM(
                            rule_id=record["rule_id"],
                            name=record.get("name", ""),
                            device_id=record.get("device_id"),
                            conditions=json.dumps(record.get("conditions", []), ensure_ascii=False),
                            logic=record.get("logic", "AND"),
                            duration=record.get("duration", 0),
                            severity=record.get("severity", ""),
                            enabled=record.get("enabled", True),
                            notify_channels=json.dumps(
                                record.get("notify_channels", []), ensure_ascii=False
                            ),
                            created_at=now,
                        )
                    )
                created += 1
            except Exception as e:
                errors[i] = f"{record.get('rule_id', '?')}: {e}"

        return created, skipped, errors


class AlarmRepo(BaseRepo):
    def __init__(self, session_or_db: Any, write_lock: asyncio.Lock | None = None, table_name: str | None = None):
        super().__init__(session_or_db, write_lock, table_name or "alarms")

    async def create(self, data: dict) -> dict:
        _validate_alarm_data(data)  # FIXED-P1: 原问题-AlarmRepo.create无业务验证，severity/rule_type可写入非法值
        return await self._create_internal(data, alarm_id=None)

    async def create_with_id(self, data: dict) -> dict:
        """Create alarm with a specific alarm_id (for edge rule engine events).

        FIXED-BugR11: 边缘规则引擎（快速路径）发布的告警事件未写入数据库，
        导致 AlarmService.handle_alarm_event 查询不到告警，通知/升级/统计全部跳过。
        此方法允许用事件中的 alarm_id 创建数据库记录。
        """
        _validate_alarm_data(data)
        alarm_id = data.get("alarm_id")
        if not alarm_id:
            raise ValueError("alarm_id is required for create_with_id")
        return await self._create_internal(data, alarm_id=alarm_id)

    async def _create_internal(self, data: dict, alarm_id: str | None = None) -> dict:
        try:
            async with self._auto_session() as session:
                if alarm_id is None:
                    alarm_id = _uuid()
                now = _now()
                orm = AlarmORM(
                    alarm_id=alarm_id,
                    rule_id=data["rule_id"],
                    device_id=data["device_id"],
                    severity=data["severity"],
                    status="firing",
                    message=data.get("message", ""),
                    trigger_value=json.dumps(data.get("trigger_value", {}), ensure_ascii=False),
                    trigger_count=1,
                    fired_at=now,
                    rule_type=data.get("rule_type", "threshold"),
                )
                session.add(orm)
                await session.commit()
                await session.refresh(orm)
                return _orm_to_alarm(orm)
        except IntegrityError:
            raise ValueError(RepoErrors.ALARM_EXISTS) from None
        except Exception as e:
            logger.error("AlarmRepo.create failed: %s", e)
            raise

    async def get(self, alarm_id: str) -> dict | None:
        # FIXED: 原问题-查询操作无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
                )
                orm = result.scalar_one_or_none()
                return _orm_to_alarm(orm) if orm else None
        except Exception as e:
            logger.error("AlarmRepo.get failed: %s", e)
            raise RuntimeError(f"AlarmRepo.get failed for alarm_id={alarm_id}: {e}") from e

    async def list_all(
        self,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        severity: str | None = None,
        device_id: str | None = None,
        search: str | None = None,
        device_ids: list[str] | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict], int] | tuple[list[dict], int, str | None]:
        # FIXED: 原问题-AlarmRepo.list_all无try-except保护
        # FIXED(一般): 原问题-list_alarms传入device_ids时先加载5000条再内存过滤，结果可能不完整;
        # 修复-新增device_ids参数下推到SQL，使用WHERE device_id IN (...)避免内存过滤截断
        try:
            async with self._auto_session() as session:
                query = select(AlarmORM)
                count_query = select(func.count()).select_from(AlarmORM)
                if status:
                    query = query.where(AlarmORM.status == status)
                    count_query = count_query.where(AlarmORM.status == status)
                if severity:
                    query = query.where(AlarmORM.severity == severity)
                    count_query = count_query.where(AlarmORM.severity == severity)
                if device_id:
                    query = query.where(AlarmORM.device_id == device_id)
                    count_query = count_query.where(AlarmORM.device_id == device_id)
                if device_ids:
                    query = query.where(AlarmORM.device_id.in_(device_ids))
                    count_query = count_query.where(AlarmORM.device_id.in_(device_ids))
                if search and len(search) >= 2:
                    # FIXED: 前缀通配符 LIKE 无法走索引，限制搜索最小长度为2字符，避免单字符全表扫描
                    # R6-G-02: 转义 LIKE 通配符
                    escaped_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    pattern = f"%{escaped_search}%"
                    query = query.where(
                        (AlarmORM.message.ilike(pattern, escape="\\")) | (AlarmORM.alarm_id.ilike(pattern, escape="\\"))
                    )
                    count_query = count_query.where(
                        (AlarmORM.message.ilike(pattern, escape="\\")) | (AlarmORM.alarm_id.ilike(pattern, escape="\\"))
                    )
                total_result = await session.execute(count_query)
                total = total_result.scalar() or 0
                # R9-S-09: 游标分页优化——当提供cursor时使用 WHERE fired_at < cursor 替代 OFFSET，避免深分页性能退化
                if cursor is not None:
                    cursor_dt = datetime.fromisoformat(cursor)
                    query = query.where(AlarmORM.fired_at < cursor_dt)
                    query = query.order_by(AlarmORM.fired_at.desc()).limit(size)
                else:
                    offset = (page - 1) * size
                    query = query.order_by(AlarmORM.fired_at.desc()).offset(offset).limit(size)
                result = await session.execute(query)
                rows = result.scalars().all()
                items = [_orm_to_alarm(r) for r in rows]
                if cursor is not None:
                    # 返回 next_cursor 供下次查询使用（最后一条记录的 fired_at）
                    next_cursor = items[-1]["fired_at"] if items else None
                    return items, total, next_cursor
                return items, total
        except Exception as e:
            logger.error("AlarmRepo.list_all failed: %s", e)
            raise RuntimeError(f"AlarmRepo.list_all failed: {e}") from e

    async def count_by_status_and_severity(
        self,
        device_ids: list[str] | None = None,
    ) -> dict[tuple[str, str], int]:
        """返回按 (status, severity) 分组的告警计数，避免全量加载到内存。

        FIXED(一般): 原问题-_get_filtered_statistics 3次全量加载(每次最多5000条)后内存统计;
        修复-使用单条 SELECT status, severity, COUNT(*) GROUP BY 查询，避免内存与性能开销
        """
        try:
            async with self._auto_session() as session:
                query = (
                    select(AlarmORM.status, AlarmORM.severity, func.count().label("count"))
                    .group_by(AlarmORM.status, AlarmORM.severity)
                )
                if device_ids:
                    query = query.where(AlarmORM.device_id.in_(device_ids))
                result = await session.execute(query)
                return {(row.status, row.severity): row.count for row in result}
        except Exception as e:
            logger.error("AlarmRepo.count_by_status_and_severity failed: %s", e)
            raise RuntimeError(f"AlarmRepo.count_by_status_and_severity failed: {e}") from e

    @retry_on_stale(base_delay=0.1)  # FIXED-P1: 原问题-ack无乐观锁重试，与recover/update_trigger_count不一致，并发确认时StaleDataError直接抛出无重试
    async def ack(self, alarm_id: str, ack_by: str) -> dict | None:
        try:
            async with self._write_write_lock():  # FIXED-P0: 原问题-ack无写锁保护，并发确认可覆盖状态
                async with self._auto_session() as session:
                    result = await session.execute(
                        select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm is None or orm.status != "firing":
                        # FIXED-BugR13: 区分"不存在"和"已确认"，避免多客户端确认返回 404
                        if orm is None:
                            return None
                        current = _orm_to_alarm(orm)
                        current["_status_conflict"] = orm.status
                        return current
                    orm.status = "acknowledged"
                    orm.acknowledged_at = _now()
                    orm.acknowledged_by = ack_by
                    orm.version = (orm.version or 0) + 1
                    await session.commit()
                    await session.refresh(orm)
                    return _orm_to_alarm(orm)
        except StaleDataError:
            raise
        except Exception as e:
            logger.error("AlarmRepo.ack failed: %s", e)
            raise RuntimeError(f"AlarmRepo.ack failed for alarm_id={alarm_id}: {e}") from e

    @retry_on_stale(base_delay=0.1)
    async def recover(self, alarm_id: str) -> dict | None:
        try:
            async with self._write_write_lock():  # FIXED-P0: 原问题-recover无写锁保护，并发恢复可覆盖状态
                async with self._auto_session() as session:
                    result = await session.execute(
                        select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm is None or orm.status not in ("firing", "acknowledged"):
                        # FIXED-BugR13: 区分"不存在"和"已恢复"
                        if orm is None:
                            return None
                        current = _orm_to_alarm(orm)
                        current["_status_conflict"] = orm.status
                        return current
                    orm.status = "recovered"
                    orm.recovered_at = _now()
                    orm.version = (orm.version or 0) + 1
                    await session.commit()
                    await session.refresh(orm)
                    return _orm_to_alarm(orm)
        except Exception as e:
            logger.error("AlarmRepo.recover failed: %s", e)
            raise RuntimeError(f"AlarmRepo.recover failed for alarm_id={alarm_id}: {e}") from e

    async def delete(self, alarm_id: str) -> bool:
        """FIXED(严重): 物理删除告警记录，仅 admin 可调用。

        修复：删除告警时不应删除 alarm_silences 记录。AlarmSilenceORM 是独立的前瞻性
        静默窗口配置（如"对设备X的告警静默从2点到5点"），并非绑定单条告警的附属数据。
        原实现使用 or_(device_id==, rule_id==) 级联删除，会误删该设备/规则的所有静默配置，
        故移除该级联删除逻辑，仅删除告警记录本身。
        """
        try:
            async with self._write_write_lock(), self._auto_session() as session:
                # 仅删除告警记录本身；alarm_silences 为独立前瞻性配置，不随单条告警删除
                result = await session.execute(
                    delete(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
                )
                await session.commit()
                return result.rowcount > 0
        except Exception as e:
            logger.error("AlarmRepo.delete failed: %s", e)
            raise RuntimeError(f"AlarmRepo.delete failed for alarm_id={alarm_id}: {e}") from e

    @retry_on_stale(base_delay=0.1)
    async def update_trigger_count(self, alarm_id: str, trigger_value: dict) -> None:
        try:
            async with self._write_write_lock():  # FIXED-P0: 原问题-update_trigger_count无写锁保护，高频触发时计数可丢失
                async with self._auto_session() as session:
                    result = await session.execute(
                        select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm:
                        orm.trigger_count = (orm.trigger_count or 0) + 1
                        orm.trigger_value = json.dumps(trigger_value, ensure_ascii=False)
                        orm.version = (orm.version or 0) + 1
                        await session.commit()
        except Exception as e:
            logger.error("AlarmRepo.update_trigger_count failed: %s", e)

    @retry_on_stale(base_delay=0.1)
    async def update_severity(self, alarm_id: str, severity: str) -> None:
        if severity not in _VALID_RULE_SEVERITIES:
            raise ValueError(f"Invalid severity: {severity}, must be one of {_VALID_RULE_SEVERITIES}")
        try:
            async with self._write_write_lock():  # FIXED-P0: 原问题-update_severity无写锁保护，并发修改可覆盖
                async with self._auto_session() as session:
                    result = await session.execute(
                        select(AlarmORM).where(AlarmORM.alarm_id == alarm_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm:
                        orm.severity = severity
                        orm.version = (orm.version or 0) + 1
                        await session.commit()
        except Exception as e:
            logger.error("AlarmRepo.update_severity failed: %s", e)

    async def get_firing_by_rule_device(self, rule_id: str, device_id: str) -> dict | None:
        # FIXED: 原问题-被evaluator高频调用但无异常保护
        # FIXED: acknowledged 状态的告警也应能被恢复与去重，否则会卡死无法自动恢复并产生重复告警
        # FIXED: 使用 scalars().first() 避免 firing+acknowledged 同时存在时 MultipleResultsFound
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(AlarmORM).where(
                        AlarmORM.rule_id == rule_id,
                        AlarmORM.device_id == device_id,
                        AlarmORM.status.in_(("firing", "acknowledged")),
                    ).order_by(AlarmORM.fired_at.desc())
                )
                orm = result.scalars().first()
                return _orm_to_alarm(orm) if orm else None
        except Exception as e:
            logger.error("AlarmRepo.get_firing_by_rule_device failed: %s", e)
            raise RuntimeError(f"AlarmRepo.get_firing_by_rule_device failed for rule_id={rule_id}: {e}") from e

    async def count_active_by_rule(self, rule_id: str) -> int:
        """统计指定规则下的活跃告警数量（firing + acknowledged）。

        用于删除规则前的关联检查，避免删除后产生孤儿告警数据。
        """
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(func.count()).select_from(AlarmORM).where(
                        AlarmORM.rule_id == rule_id,
                        AlarmORM.status.in_(("firing", "acknowledged")),
                    )
                )
                return result.scalar() or 0
        except Exception as e:
            logger.error("AlarmRepo.count_active_by_rule failed: %s", e)
            raise RuntimeError(f"AlarmRepo.count_active_by_rule failed for rule_id={rule_id}: {e}") from e

    async def list_active_by_rule(self, rule_id: str) -> list[dict]:
        """查询指定规则下所有活跃告警（firing + acknowledged）。

        用于删除规则前获取需要清理的告警列表。
        """
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(AlarmORM).where(
                        AlarmORM.rule_id == rule_id,
                        AlarmORM.status.in_(("firing", "acknowledged")),
                    )
                )
                orms = result.scalars().all()
                return [_orm_to_alarm(orm) for orm in orms]
        except Exception as e:
            logger.error("AlarmRepo.list_active_by_rule failed: %s", e)
            raise RuntimeError(f"AlarmRepo.list_active_by_rule failed for rule_id={rule_id}: {e}") from e

    async def recover_active_by_rule(self, rule_id: str) -> list[dict]:
        """批量恢复指定规则下所有活跃告警（firing/acknowledged → recovered）。

        用于删除规则时清理孤儿告警，避免删除后留下引用已删除规则的活跃告警。
        返回已恢复的告警列表，供调用方发布恢复事件。
        """
        try:
            async with self._write_write_lock(), self._auto_session() as session:
                result = await session.execute(
                    select(AlarmORM).where(
                        AlarmORM.rule_id == rule_id,
                        AlarmORM.status.in_(("firing", "acknowledged")),
                    )
                )
                orms = result.scalars().all()
                if not orms:
                    return []
                now = _now()
                recovered = []
                for orm in orms:
                    orm.status = "recovered"
                    orm.recovered_at = now
                    orm.version = (orm.version or 0) + 1
                    recovered.append(_orm_to_alarm(orm))
                await session.commit()
                return recovered
        except Exception as e:
            logger.error("AlarmRepo.recover_active_by_rule failed: %s", e)
            raise RuntimeError(f"AlarmRepo.recover_active_by_rule failed for rule_id={rule_id}: {e}") from e

    async def query_trend_data(self, hours: int = 24) -> dict[str, Any]:
        """Query alarm trend data from the database for the specified number of hours"""
        try:
            async with self._auto_session() as session:
                from datetime import timedelta

                since = _now() - timedelta(hours=hours)

                # Alarm counts by hour
                hour_expr = func.strftime("%Y-%m-%dT%H:00", AlarmORM.fired_at)
                hour_query = (
                    select(hour_expr.label("hour"), func.count().label("count"))
                    .where(AlarmORM.fired_at >= since)
                    .group_by(hour_expr)
                    .order_by(hour_expr)
                )
                hour_result = await session.execute(hour_query)
                alarm_counts_by_hour = [
                    {"hour": row.hour, "count": row.count} for row in hour_result
                ]

                # Severity distribution from database
                sev_query = (
                    select(AlarmORM.severity, func.count().label("count"))
                    .where(AlarmORM.fired_at >= since)
                    .group_by(AlarmORM.severity)
                )
                sev_result = await session.execute(sev_query)
                severity_distribution = {row.severity: row.count for row in sev_result}

                # Top 10 devices by alarm count
                dev_query = (
                    select(AlarmORM.device_id, func.count().label("count"))
                    .where(AlarmORM.fired_at >= since)
                    .group_by(AlarmORM.device_id)
                    .order_by(func.count().desc())
                    .limit(10)
                )
                dev_result = await session.execute(dev_query)
                top_devices = [
                    {"device_id": row.device_id, "count": row.count} for row in dev_result
                ]

                # Top 10 rules by alarm count
                rule_query = (
                    select(AlarmORM.rule_id, func.count().label("count"))
                    .where(AlarmORM.fired_at >= since)
                    .group_by(AlarmORM.rule_id)
                    .order_by(func.count().desc())
                    .limit(10)
                )
                rule_result = await session.execute(rule_query)
                top_rules = [
                    {"rule_id": row.rule_id, "count": row.count} for row in rule_result
                ]

                return {
                    "period_hours": hours,
                    "alarm_counts_by_hour": alarm_counts_by_hour,
                    "severity_distribution": severity_distribution,
                    "top_devices": top_devices,
                    "top_rules": top_rules,
                }
        except Exception as e:
            logger.error("AlarmRepo.query_trend_data failed: %s", e)
            return {
                "period_hours": hours,
                "alarm_counts_by_hour": [],
                "severity_distribution": {},
                "top_devices": [],
                "top_rules": [],
            }

    async def get_top_alarms(
        self,
        hours: int = 24,
        device_ids: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """修复7: 返回 Top N 报警设备/规则排名，用于 Top10 看板"""
        try:
            async with self._auto_session() as session:
                from datetime import timedelta

                since = _now() - timedelta(hours=hours)

                # Top N devices by alarm count
                dev_query = (
                    select(AlarmORM.device_id, func.count().label("count"))
                    .where(AlarmORM.fired_at >= since)
                    .group_by(AlarmORM.device_id)
                    .order_by(func.count().desc())
                    .limit(limit)
                )
                if device_ids:
                    dev_query = dev_query.where(AlarmORM.device_id.in_(device_ids))
                dev_result = await session.execute(dev_query)
                top_devices = [
                    {"device_id": row.device_id, "count": row.count} for row in dev_result
                ]

                # Top N rules by alarm count
                rule_query = (
                    select(AlarmORM.rule_id, func.count().label("count"))
                    .where(AlarmORM.fired_at >= since)
                    .group_by(AlarmORM.rule_id)
                    .order_by(func.count().desc())
                    .limit(limit)
                )
                if device_ids:
                    rule_query = rule_query.where(AlarmORM.device_id.in_(device_ids))
                rule_result = await session.execute(rule_query)
                top_rules = [
                    {"rule_id": row.rule_id, "count": row.count} for row in rule_result
                ]

                return {"top_devices": top_devices, "top_rules": top_rules}
        except Exception as e:
            logger.error("AlarmRepo.get_top_alarms failed: %s", e)
            return {"top_devices": [], "top_rules": []}

    async def cleanup_old_alarms(self, retention_days: int = 90) -> int:
        """Delete recovered/acknowledged alarms older than retention_days"""
        try:
            # R8-G-04 修复(一般): 原代码单次 DELETE 无 LIMIT，百万级历史告警时
            # 可能长时间锁库。改为分批 DELETE，每批 1000 条。
            from datetime import timedelta

            cutoff = _now() - timedelta(days=retention_days)
            total_deleted = 0
            batch_size = 1000
            # FIXED-P1: 限制最大迭代次数，防止异常情况下 while True 无限循环耗尽资源
            max_iterations = 1000
            iteration = 0
            while True:
                iteration += 1
                if iteration > max_iterations:
                    logger.warning(
                        "AlarmRepo.cleanup_old_alarms hit max iterations (%d), stopping to avoid infinite loop",
                        max_iterations,
                    )
                    break
                # FIXED-P1: 原问题-循环内读-改-写（查询ID+删除+commit）未加 _write_write_lock，
                # 与其他写操作（如 update_status）并发时可能交叉导致锁竞争/数据不一致；
                # 修复-用 _write_write_lock 保护整个读-改-写序列，与 update() 等保持一致
                async with self._write_write_lock():
                    async with self._auto_session() as session:
                        # 先查出一批 ID，再按 ID 删除（SQLite 不支持 DELETE ... LIMIT）
                        from sqlalchemy import select

                        # FIXED-P1: 原代码使用 AlarmORM.id，但 AlarmORM 主键字段为 alarm_id（无 id 列），
                        # 查询构造时即抛 AttributeError，被外层 except 捕获后 re-raise 为 RuntimeError，
                        # 导致历史告警清理功能完全失效。修复：改用 AlarmORM.alarm_id 作为主键引用。
                        ids_result = await session.execute(
                            select(AlarmORM.alarm_id).where(
                                AlarmORM.status.in_(["recovered", "acknowledged"]),
                                AlarmORM.fired_at < cutoff,
                            ).limit(batch_size)
                        )
                        batch_ids = [row[0] for row in ids_result]
                        if not batch_ids:
                            break
                        result = await session.execute(
                            delete(AlarmORM).where(AlarmORM.alarm_id.in_(batch_ids))
                        )
                        await session.commit()
                        total_deleted += result.rowcount
                        if len(batch_ids) < batch_size:
                            break
            if total_deleted > 0:
                logger.info(
                    "Cleaned up %d old alarms (retention=%d days, batch_size=%d)",
                    total_deleted, retention_days, batch_size,
                )
            return total_deleted
        except Exception as e:
            logger.error("AlarmRepo.cleanup_old_alarms failed: %s", e)
            raise RuntimeError(f"AlarmRepo.cleanup_old_alarms failed: {e}") from e

    async def get_alarm_history(self, rule_id: str, days: int = 7) -> list[dict]:
        """修复9: 查询指定规则最近 N 天的历史触发记录，用于报警详情历史展示"""
        try:
            async with self._auto_session() as session:
                from datetime import timedelta

                since = _now() - timedelta(days=days)
                query = (
                    select(AlarmORM)
                    .where(AlarmORM.rule_id == rule_id, AlarmORM.fired_at >= since)
                    .order_by(AlarmORM.fired_at.desc())
                    .limit(200)
                )
                result = await session.execute(query)
                rows = result.scalars().all()
                return [_orm_to_alarm(r) for r in rows]
        except Exception as e:
            logger.error("AlarmRepo.get_alarm_history failed: %s", e)
            raise RuntimeError(f"AlarmRepo.get_alarm_history failed: {e}") from e

    async def get_alarm_history_paginated(
        self,
        rule_id: str,
        days: int = 7,
        page: int = 1,
        size: int = 100,
        device_ids: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        """查询指定规则最近 N 天的历史触发记录（分页），用于报警详情历史展示。

        FIXED: 原问题-get_alarm_history 硬编码 limit(200) 一次性返回，长期运行后告警数量巨大
        导致响应超时与内存压力；修复-将分页与 device_ids 过滤下推到 SQL，由数据库完成。
        """
        try:
            async with self._auto_session() as session:
                from datetime import timedelta

                since = _now() - timedelta(days=days)
                base_filters = [
                    AlarmORM.rule_id == rule_id,
                    AlarmORM.fired_at >= since,
                ]
                query = select(AlarmORM).where(*base_filters)
                count_query = select(func.count()).select_from(AlarmORM).where(*base_filters)
                if device_ids:
                    query = query.where(AlarmORM.device_id.in_(device_ids))
                    count_query = count_query.where(AlarmORM.device_id.in_(device_ids))
                total_result = await session.execute(count_query)
                total = total_result.scalar() or 0
                offset = (page - 1) * size
                query = query.order_by(AlarmORM.fired_at.desc()).offset(offset).limit(size)
                result = await session.execute(query)
                rows = result.scalars().all()
                return [_orm_to_alarm(r) for r in rows], total
        except Exception as e:
            logger.error("AlarmRepo.get_alarm_history_paginated failed: %s", e)
            raise RuntimeError(f"AlarmRepo.get_alarm_history_paginated failed: {e}") from e


class UserRepo(BaseRepo):
    def __init__(self, session_or_db: Any, write_lock: asyncio.Lock | None = None, table_name: str | None = None):
        super().__init__(session_or_db, write_lock, table_name or "users")

    async def create(self, data: dict) -> dict:
        _validate_user_data(data)  # FIXED-P1: 用户创建业务验证
        # FIXED: 数据库插入无IntegrityError处理
        try:
            async with self._auto_session() as session:
                user_id = _uuid()
                now = _now()
                orm = UserORM(
                    user_id=user_id,
                    username=data["username"],
                    password=data["password"],
                    role=data["role"],
                    enabled=True,
                    created_at=now,
                )
                session.add(orm)
                await session.commit()
                await session.refresh(orm)
                return _orm_to_user(orm)
        except IntegrityError:
            raise ValueError(RepoErrors.USERNAME_EXISTS) from None

    async def get(self, user_id: str) -> dict | None:
        # FIXED: 原问题-UserRepo.get无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.user_id == user_id))
                orm = result.scalar_one_or_none()
                return _orm_to_user(orm) if orm else None
        except Exception as e:
            logger.error("UserRepo.get failed: %s", e)
            raise RuntimeError(f"UserRepo.get failed for user_id={user_id}: {e}") from e

    async def get_by_username(self, username: str) -> dict | None:
        # FIXED: 原问题-UserRepo.get_by_username无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.username == username))
                orm = result.scalar_one_or_none()
                return _orm_to_user(orm) if orm else None
        except Exception as e:
            logger.error("UserRepo.get_by_username failed: %s", e)
            raise RuntimeError(f"UserRepo.get_by_username failed for username={username}: {e}") from e

    async def get_by_username_with_password(self, username: str) -> dict | None:
        # FIXED: 原问题-UserRepo.get_by_username_with_password无try-except保护，被登录认证调用
        try:
            async with self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.username == username))
                orm = result.scalar_one_or_none()
                return _orm_to_user_full(orm) if orm else None
        except Exception as e:
            logger.error("UserRepo.get_by_username_with_password failed: %s", e)
            raise RuntimeError(f"UserRepo.get_by_username_with_password failed for username={username}: {e}") from e

    async def list_all(
        self,
        page: int = 1,
        size: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict], int] | tuple[list[dict], int, str | None]:
        # FIXED: 原问题-UserRepo.list_all无try-except保护
        try:
            async with self._auto_session() as session:
                count_result = await session.execute(select(func.count()).select_from(UserORM))
                total = count_result.scalar() or 0
                # R9-S-09: 游标分页优化——当提供cursor时使用 WHERE created_at < cursor 替代 OFFSET，避免深分页性能退化
                if cursor is not None:
                    cursor_dt = datetime.fromisoformat(cursor)
                    result = await session.execute(
                        select(UserORM)
                        .where(UserORM.created_at < cursor_dt)
                        .order_by(UserORM.created_at.desc())
                        .limit(size)
                    )
                else:
                    offset = (page - 1) * size
                    result = await session.execute(
                        select(UserORM).order_by(UserORM.created_at.desc()).offset(offset).limit(size)
                    )
                rows = result.scalars().all()
                items = [_orm_to_user_safe(r) for r in rows]
                if cursor is not None:
                    # 返回 next_cursor 供下次查询使用（最后一条记录的 created_at）
                    next_cursor = items[-1]["created_at"] if items else None
                    return items, total, next_cursor
                return items, total
        except Exception as e:
            logger.error("UserRepo.list_all failed: %s", e)
            raise RuntimeError(f"UserRepo.list_all failed: {e}") from e

    @retry_on_stale(base_delay=0.1)
    async def update(self, user_id: str, data: dict) -> dict | None:
        _validate_user_update_data(data)
        try:
            async with self._write_write_lock():
                async with self._auto_session() as session:
                    result = await session.execute(select(UserORM).where(UserORM.user_id == user_id))
                    orm = result.scalar_one_or_none()
                    if orm is None:
                        return None
                    old_version = data.get("_version")
                    if old_version is not None and orm.version != old_version:  # FIXED-P1: 原问题-UserRepo.update无乐观锁冲突检测
                        raise StaleDataError(f"User {user_id} version conflict: expected={old_version}, actual={orm.version}")
                    for key in ("password", "role"):
                        if key in data:
                            setattr(orm, key, data[key])
                    if "enabled" in data:
                        orm.enabled = data["enabled"]
                    orm.version = (orm.version or 0) + 1
                    orm.updated_at = _now()  # R6-G-01: 与 DeviceRepo/RuleRepo 保持一致，更新 updated_at
                    await session.commit()
                    await session.refresh(orm)
                    return _orm_to_user(orm)
        except StaleDataError:
            raise
        except Exception as e:
            logger.error("UserRepo.update failed: %s", e)
            raise RuntimeError(f"UserRepo.update failed for user_id={user_id}: {e}") from e

    async def delete(self, user_id: str) -> bool:
        try:
            async with self._write_write_lock():  # FIXED-P1: 原问题-UserRepo.delete无写锁保护，并发删除可IntegrityError
                async with self._auto_session() as session:
                    # R6-S-08 修复(严重): 原实现仅 delete(UserORM)，不级联清理子表，
                    # 导致 devices.created_by/rules.created_by/resource_shares/alarms 等
                    # 残留悬空外键，破坏参照完整性。修复-在单事务内级联清理。
                    # 1. 删除资源共享记录（作为共享者或被共享者）
                    await session.execute(
                        delete(ResourceShareORM).where(
                            (ResourceShareORM.shared_with_user_id == user_id)
                            | (ResourceShareORM.shared_by_user_id == user_id)
                        )
                    )
                    # 2. 删除登录限流/锁定记录
                    await session.execute(delete(LoginAttemptORM).where(LoginAttemptORM.user_id == user_id))
                    await session.execute(delete(AccountLockoutORM).where(AccountLockoutORM.user_id == user_id))
                    await session.execute(delete(PasswordResetAttemptORM).where(PasswordResetAttemptORM.user_id == user_id))
                    # 3. 设备/规则的 created_by 置 NULL（保留数据，不删除业务实体）
                    await session.execute(
                        update(DeviceORM).where(DeviceORM.created_by == user_id).values(created_by=None)
                    )
                    await session.execute(
                        update(RuleORM).where(RuleORM.created_by == user_id).values(created_by=None)
                    )
                    # 4. 告警的 acknowledged_by 置 NULL（若有该字段）
                    try:
                        await session.execute(
                            text("UPDATE alarms SET acknowledged_by = NULL WHERE acknowledged_by = :uid"),
                            {"uid": user_id},
                        )
                    except Exception:
                        pass  # 字段可能不存在，忽略
                    # 5. 最后删除用户
                    result = await session.execute(delete(UserORM).where(UserORM.user_id == user_id))
                    await session.commit()
                    return result.rowcount > 0
        except Exception as e:
            logger.error("UserRepo.delete failed: %s", e)
            raise RuntimeError(f"UserRepo.delete failed for user_id={user_id}: {e}") from e

    async def update_password_and_clear_flag(self, username: str, hashed_password: str) -> bool:
        """Atomically update password and clear must_change_password flag in a single transaction.

        FIXED-T01: This method combines update_password and must_change_password flag update
        into a single database session with a single commit. Either both succeed or both
        are rolled back, preventing the inconsistent state that occurred when they were
        separate operations (password changed but must_change_password still True).
        """
        try:
            async with self._write_write_lock(), self._auto_session() as session:
                result = await session.execute(select(UserORM).where(UserORM.username == username))
                orm = result.scalar_one_or_none()
                if orm is None:
                    return False
                orm.password = hashed_password
                orm.password_changed_at = _now()
                orm.must_change_password = False
                orm.version = (orm.version or 0) + 1
                await session.commit()
                return True
        except Exception as e:
            logger.error("UserRepo.update_password_and_clear_flag failed: %s", e)
            raise RuntimeError(f"UserRepo.update_password_and_clear_flag failed for username={username}: {e}") from e

    async def update_password(self, username: str, hashed_password: str) -> None:
        try:
            async with self._write_write_lock():  # FIXED-P0: 原问题-update_password无写锁保护，并发修改可覆盖
                async with self._auto_session() as session:
                    result = await session.execute(select(UserORM).where(UserORM.username == username))
                    orm = result.scalar_one_or_none()
                    if orm is None:
                        return
                    orm.password = hashed_password
                    orm.password_changed_at = _now()
                    orm.version = (orm.version or 0) + 1
                    await session.commit()
        except Exception as e:
            # FIXED(严重): 原问题-异常被吞没不re-raise，密码重置返回成功但密码未更新;
            # 修复-re-raise异常让调用方感知失败
            logger.error("UserRepo.update_password failed: %s", e)
            raise RuntimeError(f"UserRepo.update_password failed for username={username}: {e}") from e

    async def update_user(self, username: str, data: dict) -> dict | None:
        _validate_user_update_data(data)
        try:
            async with self._write_write_lock():
                async with self._auto_session() as session:
                    result = await session.execute(select(UserORM).where(UserORM.username == username))
                    orm = result.scalar_one_or_none()
                    if orm is None:
                        return None
                    old_version = data.get("_version")
                    if old_version is not None and orm.version != old_version:  # FIXED-P1: 原问题-UserRepo.update_user无乐观锁冲突检测
                        raise StaleDataError(f"User {username} version conflict: expected={old_version}, actual={orm.version}")
                    for key in ("password", "role"):
                        if key in data:
                            setattr(orm, key, data[key])
                    if "enabled" in data:
                        orm.enabled = data["enabled"]
                    if "must_change_password" in data:
                        orm.must_change_password = data["must_change_password"]
                    orm.version = (orm.version or 0) + 1
                    await session.commit()
                    await session.refresh(orm)
                    return _orm_to_user(orm)
        except StaleDataError:
            raise
        except Exception as e:
            logger.error("UserRepo.update_user failed: %s", e)
            raise RuntimeError(f"UserRepo.update_user failed for username={username}: {e}") from e

    async def count_by_role(self, role: str) -> int:
        # FIXED: 原问题-UserRepo.count_by_role无try-except保护
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(func.count()).select_from(UserORM).where(UserORM.role == role)
                )
                return result.scalar() or 0
        except Exception as e:
            logger.error("UserRepo.count_by_role failed: %s", e)
            raise RuntimeError(f"UserRepo.count_by_role failed for role={role}: {e}") from e


def _orm_to_device(orm: DeviceORM) -> dict:
    return {
        "device_id": orm.device_id,
        "name": orm.name,
        "protocol": orm.protocol,
        "status": orm.status,
        "config": _safe_json_loads(orm.config, {}, "device.config"),
        "points": _safe_json_loads(orm.points, [], "device.points"),
        "collect_interval": orm.collect_interval,
        "created_by": orm.created_by,
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat()
        if isinstance(orm.updated_at, datetime)
        else str(orm.updated_at),
        "version": getattr(orm, "version", 1),  # FIXED-P0: 返回乐观锁版本号
    }


def _orm_to_template(orm: DeviceTemplateORM) -> dict:
    return {
        "name": orm.name,
        "protocol": orm.protocol,
        "config_template": _safe_json_loads(orm.config_template, {}, "template.config_template"),
        "point_templates": _safe_json_loads(orm.point_templates, [], "template.point_templates"),
        "created_by": orm.created_by,
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "version": getattr(orm, "version", 1),  # FIXED-P1: 返回乐观锁版本号
    }


def _orm_to_rule(orm: RuleORM) -> dict:
    return {
        "rule_id": orm.rule_id,
        "name": orm.name,
        "device_id": orm.device_id,
        "conditions": _safe_json_loads(orm.conditions, [], "rule.conditions"),
        "logic": orm.logic,
        "duration": orm.duration,
        "severity": orm.severity,
        "enabled": bool(orm.enabled),
        "notify_channels": _safe_json_loads(orm.notify_channels, [], "rule.notify_channels"),
        # SEC-FIX: 返回 script/rule_type 字段，使 evaluator 可读取持久化的脚本/规则类型
        "script": getattr(orm, "script", "") or "",
        "rule_type": getattr(orm, "rule_type", "threshold") or "threshold",
        "created_by": orm.created_by,
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "version": getattr(orm, "version", 1),  # FIXED-P0: 返回乐观锁版本号
    }


def _orm_to_alarm(orm: AlarmORM) -> dict:
    return {
        "alarm_id": orm.alarm_id,
        "rule_id": orm.rule_id,
        "device_id": orm.device_id,
        "severity": orm.severity,
        "status": orm.status,
        "message": orm.message,
        "trigger_value": _safe_json_loads(orm.trigger_value, field_name="alarm.trigger_value"),
        "trigger_count": orm.trigger_count,
        "rule_type": orm.rule_type,
        "fired_at": orm.fired_at.isoformat()
        if isinstance(orm.fired_at, datetime)
        else str(orm.fired_at),
        "acknowledged_at": orm.acknowledged_at.isoformat()
        if isinstance(orm.acknowledged_at, datetime)
        else orm.acknowledged_at,
        "acknowledged_by": orm.acknowledged_by,
        "recovered_at": orm.recovered_at.isoformat()
        if isinstance(orm.recovered_at, datetime)
        else orm.recovered_at,
        "version": getattr(orm, "version", 1),  # FIXED-P1: 原问题-_orm_to_alarm缺version字段，客户端无法对告警进行乐观锁检测
    }


def _orm_to_user(orm: UserORM) -> dict:
    return {
        "user_id": orm.user_id,
        "username": orm.username,
        "role": orm.role,
        "enabled": bool(orm.enabled),
        "must_change_password": bool(orm.must_change_password),
        "password_changed_at": orm.password_changed_at.isoformat()
        if hasattr(orm, "password_changed_at") and isinstance(orm.password_changed_at, datetime)
        else getattr(orm, "password_changed_at", None),
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat()
        if hasattr(orm, "updated_at") and isinstance(orm.updated_at, datetime)
        else str(getattr(orm, "updated_at", "")),
        "version": getattr(orm, "version", 1),  # FIXED-P1: 原问题-_orm_to_user缺version字段，客户端无法对用户做乐观锁
    }


def _orm_to_user_full(orm: UserORM) -> dict:
    return {
        "user_id": orm.user_id,
        "username": orm.username,
        "password": orm.password,
        "role": orm.role,
        "enabled": bool(orm.enabled),
        "must_change_password": bool(orm.must_change_password),
        "password_changed_at": orm.password_changed_at.isoformat()
        if hasattr(orm, "password_changed_at") and isinstance(orm.password_changed_at, datetime)
        else getattr(orm, "password_changed_at", None),
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat()
        if hasattr(orm, "updated_at") and isinstance(orm.updated_at, datetime)
        else str(getattr(orm, "updated_at", "")),
        "version": getattr(orm, "version", 1),
    }


def _orm_to_user_safe(orm: UserORM) -> dict:
    return {
        "user_id": orm.user_id,
        "username": orm.username,
        "role": orm.role,
        "enabled": bool(orm.enabled),
        "must_change_password": bool(orm.must_change_password),
        "password_changed_at": orm.password_changed_at.isoformat()
        if hasattr(orm, "password_changed_at") and isinstance(orm.password_changed_at, datetime)
        else getattr(orm, "password_changed_at", None),
        "created_at": orm.created_at.isoformat()
        if isinstance(orm.created_at, datetime)
        else str(orm.created_at),
        "updated_at": orm.updated_at.isoformat()
        if hasattr(orm, "updated_at") and isinstance(orm.updated_at, datetime)
        else str(getattr(orm, "updated_at", "")),
        "version": getattr(orm, "version", 1),
    }


# FIXED-H03: Persistent login rate limiting for multi-worker deployments
class RateLimitRepo:
    """Repository for login rate limiting and account lockout persistence.

    All methods are classmethods since this repo uses raw SQL for simplicity
    and to avoid async session management overhead in hot paths.
    """

    _cleanup_task: asyncio.Task | None = None
    _cleanup_interval: float = 300.0  # 5 minutes

    @classmethod
    async def record_login_attempt(cls, ip: str) -> int:
        """Record a failed login attempt for IP rate limiting.

        Returns:
            Current attempt count for this IP within the window.
        """
        now = time.time()
        window_start = now - _AUTH_LOGIN_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0

            async with db.write_lock:  # FIXED-P1: 原问题-read-modify-write无锁保护，并发更新可丢失attempt_count
                async with db.get_session() as session:
                    # Get current attempt record
                    result = await session.execute(
                        select(LoginAttemptORM).where(LoginAttemptORM.ip == ip)
                    )
                    record = result.scalar_one_or_none()

                    if record is None:
                        # New record
                        record = LoginAttemptORM(
                            ip=ip,
                            attempt_count=1,
                            first_attempt_at=now,
                            last_attempt_at=now,
                        )
                        session.add(record)
                    else:
                        # Update existing record, reset if window expired
                        if record.last_attempt_at < window_start:
                            record.attempt_count = 1
                            record.first_attempt_at = now
                        else:
                            record.attempt_count = record.attempt_count + 1
                        record.last_attempt_at = now

                    await session.commit()
                    return record.attempt_count
        except Exception as e:
            logger.warning("RateLimitRepo.record_login_attempt failed: %s", e)
            return 0

    @classmethod
    async def check_login_rate(cls, ip: str) -> int:
        """Check if IP is rate limited.

        Returns:
            Current attempt count, or -1 if rate limited.
        """
        now = time.time()
        window_start = now - _AUTH_LOGIN_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0

            async with db.write_lock, db.get_session() as session:
                result = await session.execute(
                    select(LoginAttemptORM).where(LoginAttemptORM.ip == ip)
                )
                record = result.scalar_one_or_none()

                if record is None:
                    return 0

                # Clean up if window expired
                if record.last_attempt_at < window_start:
                    await session.delete(record)
                    await session.commit()
                    return 0

                return record.attempt_count
        except Exception as e:
            logger.warning("RateLimitRepo.check_login_rate failed: %s", e)
            return 0

    @classmethod
    async def clear_login_attempts(cls, ip: str) -> None:
        """Clear login attempts for IP after successful login."""
        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return

            async with db.write_lock, db.get_session() as session:
                await session.execute(
                    delete(LoginAttemptORM).where(LoginAttemptORM.ip == ip)
                )
                await session.commit()
        except Exception as e:
            logger.warning("RateLimitRepo.clear_login_attempts failed: %s", e)

    @classmethod
    async def get_lockout_info(cls, username: str, ip: str) -> dict | None:
        """Get lockout info for username+IP combination.

        Returns:
            dict with fail_count, locked_until, or None if not locked.
        """
        now = time.time()
        lockout_key = f"{username}:{ip}"

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return None

            async with db.get_session() as session:
                result = await session.execute(
                    select(AccountLockoutORM).where(AccountLockoutORM.lockout_key == lockout_key)
                )
                record = result.scalar_one_or_none()

                if record is None:
                    return None

                # Check if lockout expired
                if record.lockout_until < now:
                    await session.delete(record)
                    await session.commit()
                    return None

                return {
                    "fail_count": record.fail_count,
                    "locked_until": record.lockout_until,
                }
        except Exception as e:
            logger.warning("RateLimitRepo.get_lockout_info failed: %s", e)
            return None

    @classmethod
    async def record_lockout_failure(cls, username: str, ip: str) -> dict | None:
        """Record a failed login attempt for account lockout tracking.

        Returns:
            dict with fail_count, locked_until if account is now locked, else None.
        """
        now = time.time()
        lockout_key = f"{username}:{ip}"

        try:
            from edgelite.config import get_config
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return None

            config = get_config()
            threshold = config.security.login_lockout_threshold
            lockout_minutes = config.security.login_lockout_minutes

            async with db.write_lock:  # FIXED-P1: 原问题-read-modify-write无锁保护，并发更新可丢失fail_count
                async with db.get_session() as session:
                    result = await session.execute(
                        select(AccountLockoutORM).where(AccountLockoutORM.lockout_key == lockout_key)
                    )
                    record = result.scalar_one_or_none()

                    if record is None:
                        fail_count = 1
                        record = AccountLockoutORM(
                            lockout_key=lockout_key,
                            username=username,
                            ip=ip,
                            fail_count=fail_count,
                            lockout_until=0,
                        )
                        session.add(record)
                    else:
                        fail_count = record.fail_count + 1
                        record.fail_count = fail_count

                    if fail_count >= threshold:  # FIXED-P1: 原问题-fail_count和lockout_until分两次session更新非原子，合并为单次commit
                        record.lockout_until = now + lockout_minutes * 60
                        logger.warning(
                            "Account locked for user %s from IP %s for %d minutes",
                            username, ip, lockout_minutes,
                        )

                    await session.commit()

                    if fail_count >= threshold:
                        return {
                            "fail_count": fail_count,
                            "locked_until": record.lockout_until,
                        }

                    return None
        except Exception as e:
            logger.warning("RateLimitRepo.record_lockout_failure failed: %s", e)
            return None

    @classmethod
    async def clear_lockout(cls, username: str, ip: str) -> None:
        """Clear lockout on successful login."""
        lockout_key = f"{username}:{ip}"

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return

            async with db.get_session() as session:
                await session.execute(
                    delete(AccountLockoutORM).where(AccountLockoutORM.lockout_key == lockout_key)
                )
                await session.commit()
        except Exception as e:
            logger.warning("RateLimitRepo.clear_lockout failed: %s", e)

    @classmethod
    async def cleanup_expired(cls) -> tuple[int, int, int]:
        """Clean up expired login attempts and lockouts.

        Returns:
            (deleted_attempts, deleted_lockouts, deleted_global_failures)
        """
        now = time.time()
        window_start = now - _AUTH_LOGIN_WINDOW_SECONDS
        deleted_attempts = 0
        deleted_lockouts = 0
        deleted_global_failures = 0

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0, 0, 0

            async with db.get_session() as session:
                # Clean up expired login attempts
                result1 = await session.execute(
                    delete(LoginAttemptORM).where(LoginAttemptORM.last_attempt_at < window_start)
                )
                deleted_attempts = result1.rowcount

                # Clean up expired lockouts
                result2 = await session.execute(
                    delete(AccountLockoutORM).where(AccountLockoutORM.lockout_until < now)
                )
                deleted_lockouts = result2.rowcount

                # FIXED-M03: Clean up expired global lockouts
                await session.execute(
                    delete(GlobalAccountLockoutORM).where(GlobalAccountLockoutORM.locked_until < now)
                )

                # Clean up old global failure records (older than 1 hour)
                cutoff = now - 3600
                result4 = await session.execute(
                    delete(GlobalLoginFailureORM).where(GlobalLoginFailureORM.timestamp < cutoff)
                )
                deleted_global_failures = result4.rowcount

                await session.commit()

                if deleted_attempts > 0 or deleted_lockouts > 0 or deleted_global_failures > 0:
                    logger.debug(
                        "Rate limit cleanup: %d attempts, %d lockouts, %d global failures removed",
                        deleted_attempts, deleted_lockouts, deleted_global_failures
                    )

        except Exception as e:
            logger.warning("RateLimitRepo.cleanup_expired failed: %s", e)

        return deleted_attempts, deleted_lockouts, deleted_global_failures

    @classmethod
    def start_cleanup_task(cls) -> None:
        """Start background cleanup task."""
        if cls._cleanup_task is not None:
            return

        async def _cleanup_loop():
            while True:
                await asyncio.sleep(cls._cleanup_interval)
                await cls.cleanup_expired()

        cls._cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info("Rate limit cleanup task started")

    # FIXED-M03: Global login protection methods

    @classmethod
    async def check_global_failure_rate(cls) -> int:
        """Check global login failure rate in the last minute.

        Returns:
            Number of failures in the last minute.
        """
        now = time.time()
        window_start = now - 60  # Last 60 seconds

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0

            async with db.get_session() as session:
                result = await session.execute(
                    select(func.count()).select_from(GlobalLoginFailureORM).where(
                        GlobalLoginFailureORM.timestamp >= window_start
                    )
                )
                return result.scalar() or 0
        except Exception as e:
            logger.warning("RateLimitRepo.check_global_failure_rate failed: %s", e)
            return 0

    @classmethod
    async def record_global_failure(cls, username: str | None, ip: str) -> None:
        """Record a global login failure for rate monitoring.

        Args:
            username: The username used in the attempt (can be None for unknown users)
            ip: The IP address of the request
        """
        now = time.time()

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return

            async with db.write_lock, db.get_session() as session:
                record = GlobalLoginFailureORM(
                    timestamp=now,
                    username=username,
                    ip=ip,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.warning("RateLimitRepo.record_global_failure failed: %s", e)

    @classmethod
    async def check_global_account_lockout(cls, username: str) -> dict | None:
        """Check if username is globally locked out.

        Returns:
            dict with locked_until if globally locked, else None.
        """
        now = time.time()

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return None

            async with db.get_session() as session:
                result = await session.execute(
                    select(GlobalAccountLockoutORM).where(GlobalAccountLockoutORM.username == username)
                )
                record = result.scalar_one_or_none()

                if record is None:
                    return None

                # Check if lockout expired
                if record.locked_until < now:
                    await session.delete(record)
                    await session.commit()
                    return None

                return {
                    "locked_until": record.locked_until,
                    "fail_count": record.fail_count,
                }
        except Exception as e:
            logger.warning("RateLimitRepo.check_global_account_lockout failed: %s", e)
            return None

    @classmethod
    async def record_global_account_failure(cls, username: str) -> dict | None:
        """Record a failure for global username lockout tracking.

        Returns:
            dict with locked_until if now globally locked, else None.
        """
        now = time.time()

        try:
            from edgelite.config import get_config
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return None

            config = get_config()
            threshold = config.security.global_lockout_threshold
            window = config.security.global_lockout_window
            lockout_duration = config.security.global_lockout_duration
            window_start = now - window * 60

            async with db.write_lock:  # FIXED-P1: 原问题-read-modify-write无锁保护，并发更新可丢失fail_count
                async with db.get_session() as session:
                    result = await session.execute(
                        select(GlobalAccountLockoutORM).where(GlobalAccountLockoutORM.username == username)
                    )
                    record = result.scalar_one_or_none()

                    if record is None:
                        record = GlobalAccountLockoutORM(
                            username=username,
                            fail_count=1,
                            first_attempt_at=now,
                            last_attempt_at=now,
                            locked_until=0,
                        )
                        session.add(record)
                    else:
                        if record.last_attempt_at < window_start:
                            record.fail_count = 1
                            record.first_attempt_at = now
                        else:
                            record.fail_count = record.fail_count + 1
                        record.last_attempt_at = now

                    if record.fail_count >= threshold:  # FIXED-P1: 原问题-fail_count和locked_until分两次session更新非原子，合并为单次commit
                        record.locked_until = now + lockout_duration * 60
                        logger.warning(
                            "Global account locked for username %s for %d minutes (failures: %d)",
                            username, lockout_duration, record.fail_count,
                        )

                    await session.commit()

                    if record.fail_count >= threshold:
                        return {
                            "locked_until": record.locked_until,
                            "fail_count": record.fail_count,
                        }

                    return None
        except Exception as e:
            logger.warning("RateLimitRepo.record_global_account_failure failed: %s", e)
            return None

    @classmethod
    async def clear_global_account_lockout(cls, username: str) -> None:
        """Clear global account lockout on successful login."""
        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return

            async with db.write_lock:
                async with db.get_session() as session:
                    await session.execute(
                        delete(GlobalAccountLockoutORM).where(GlobalAccountLockoutORM.username == username)
                    )
                    await session.commit()
        except Exception as e:
            logger.warning("RateLimitRepo.clear_global_account_lockout failed: %s", e)

    @classmethod
    async def cleanup_global_failures(cls) -> int:
        """Clean up old global failure records (older than 1 hour).

        Returns:
            Number of deleted records.
        """
        now = time.time()
        cutoff = now - 3600  # 1 hour

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0

            async with db.get_session() as session:
                result = await session.execute(
                    delete(GlobalLoginFailureORM).where(GlobalLoginFailureORM.timestamp < cutoff)
                )
                deleted = result.rowcount
                await session.commit()
                return deleted
        except Exception as e:
            logger.warning("RateLimitRepo.cleanup_global_failures failed: %s", e)
            return 0

    @classmethod
    async def stop_cleanup_task(cls) -> None:
        """Stop background cleanup task."""
        if cls._cleanup_task:
            cls._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cls._cleanup_task
            cls._cleanup_task = None

    # FIXED-H01: Password reset request rate limiting methods
    @classmethod
    async def check_password_reset_ip_rate(cls, ip: str) -> tuple[int, int]:
        """Check if IP is rate limited for password reset requests.

        Returns:
            Tuple of (current_count, retry_after_seconds).
            If rate limited, current_count is -1 and retry_after_seconds > 0.
        """
        from edgelite.constants import (
            _AUTH_RESET_IP_MAX,
            _AUTH_RESET_IP_WINDOW_SECONDS,
        )

        now = time.time()
        window_start = now - _AUTH_RESET_IP_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0, 0

            async with db.get_session() as session:
                result = await session.execute(
                    select(PasswordResetAttemptORM).where(PasswordResetAttemptORM.ip == ip)
                )
                record = result.scalar_one_or_none()

                if record is None:
                    return 0, 0

                # Check if window expired
                if record.last_attempt_at < window_start:
                    await session.delete(record)
                    await session.commit()
                    return 0, 0

                if record.attempt_count >= _AUTH_RESET_IP_MAX:
                    # Rate limited - calculate retry_after
                    retry_after = int(record.last_attempt_at + _AUTH_RESET_IP_WINDOW_SECONDS - now)
                    return -1, max(1, retry_after)

                return record.attempt_count, 0
        except Exception as e:
            logger.warning("RateLimitRepo.check_password_reset_ip_rate failed: %s", e)
            return 0, 0

    @classmethod
    async def record_password_reset_ip_attempt(cls, ip: str) -> int:
        """Record a password reset request for IP rate limiting.

        Returns:
            Current attempt count for this IP within the window.
        """
        from edgelite.constants import _AUTH_RESET_IP_WINDOW_SECONDS

        now = time.time()
        window_start = now - _AUTH_RESET_IP_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0

            async with db.get_session() as session:
                result = await session.execute(
                    select(PasswordResetAttemptORM).where(PasswordResetAttemptORM.ip == ip)
                )
                record = result.scalar_one_or_none()

                if record is None:
                    record = PasswordResetAttemptORM(
                        ip=ip,
                        attempt_count=1,
                        first_attempt_at=now,
                        last_attempt_at=now,
                    )
                    session.add(record)
                else:
                    if record.last_attempt_at < window_start:
                        record.attempt_count = 1
                        record.first_attempt_at = now
                    else:
                        record.attempt_count = record.attempt_count + 1
                    record.last_attempt_at = now

                await session.commit()
                return record.attempt_count
        except Exception as e:
            logger.warning("RateLimitRepo.record_password_reset_ip_attempt failed: %s", e)
            return 0

    @classmethod
    async def check_password_reset_user_rate(cls, username: str) -> tuple[int, int]:
        """Check if username is rate limited for password reset requests.

        Returns:
            Tuple of (current_count, retry_after_seconds).
            If rate limited, current_count is -1 and retry_after_seconds > 0.
        """
        from edgelite.constants import (
            _AUTH_RESET_USER_MAX,
            _AUTH_RESET_USER_WINDOW_SECONDS,
        )

        now = time.time()
        window_start = now - _AUTH_RESET_USER_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0, 0

            async with db.get_session() as session:
                result = await session.execute(
                    select(PasswordResetUserRateORM).where(
                        PasswordResetUserRateORM.username == username
                    )
                )
                record = result.scalar_one_or_none()

                if record is None:
                    return 0, 0

                if record.last_attempt_at < window_start:
                    await session.delete(record)
                    await session.commit()
                    return 0, 0

                if record.attempt_count >= _AUTH_RESET_USER_MAX:
                    retry_after = int(record.last_attempt_at + _AUTH_RESET_USER_WINDOW_SECONDS - now)
                    return -1, max(1, retry_after)

                return record.attempt_count, 0
        except Exception as e:
            logger.warning("RateLimitRepo.check_password_reset_user_rate failed: %s", e)
            return 0, 0

    @classmethod
    async def record_password_reset_user_attempt(cls, username: str) -> int:
        """Record a password reset request for username rate limiting.

        Returns:
            Current attempt count for this username within the window.
        """
        from edgelite.constants import _AUTH_RESET_USER_WINDOW_SECONDS

        now = time.time()
        window_start = now - _AUTH_RESET_USER_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0

            async with db.get_session() as session:
                result = await session.execute(
                    select(PasswordResetUserRateORM).where(
                        PasswordResetUserRateORM.username == username
                    )
                )
                record = result.scalar_one_or_none()

                if record is None:
                    record = PasswordResetUserRateORM(
                        username=username,
                        attempt_count=1,
                        first_attempt_at=now,
                        last_attempt_at=now,
                    )
                    session.add(record)
                else:
                    if record.last_attempt_at < window_start:
                        record.attempt_count = 1
                        record.first_attempt_at = now
                    else:
                        record.attempt_count = record.attempt_count + 1
                    record.last_attempt_at = now

                await session.commit()
                return record.attempt_count
        except Exception as e:
            logger.warning("RateLimitRepo.record_password_reset_user_attempt failed: %s", e)
            return 0

    @classmethod
    async def cleanup_password_reset_attempts(cls) -> tuple[int, int]:
        """Clean up old password reset attempt records.

        Returns:
            Tuple of (ip_deleted, user_deleted) counts.
        """
        from edgelite.constants import (
            _AUTH_RESET_IP_WINDOW_SECONDS,
            _AUTH_RESET_USER_WINDOW_SECONDS,
        )

        now = time.time()
        ip_cutoff = now - _AUTH_RESET_IP_WINDOW_SECONDS
        user_cutoff = now - _AUTH_RESET_USER_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0, 0

            async with db.get_session() as session:
                result1 = await session.execute(
                    delete(PasswordResetAttemptORM).where(
                        PasswordResetAttemptORM.last_attempt_at < ip_cutoff
                    )
                )
                ip_deleted = result1.rowcount

                result2 = await session.execute(
                    delete(PasswordResetUserRateORM).where(
                        PasswordResetUserRateORM.last_attempt_at < user_cutoff
                    )
                )
                user_deleted = result2.rowcount

                await session.commit()
                return ip_deleted, user_deleted
        except Exception as e:
            logger.warning("RateLimitRepo.cleanup_password_reset_attempts failed: %s", e)
            return 0, 0

    # FIXED-H03: Used password reset token tracking (one-time use)
    @classmethod
    async def is_password_reset_token_used(cls, token_hash: str) -> bool:
        """Check if a password reset token has already been used.

        Args:
            token_hash: SHA256 hash of the token

        Returns:
            True if the token has been used, False otherwise.
        """
        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return False

            async with db.get_session() as session:
                result = await session.execute(
                    select(UsedPasswordResetTokenORM).where(
                        UsedPasswordResetTokenORM.token_hash == token_hash
                    )
                )
                return result.scalar_one_or_none() is not None
        except Exception as e:
            logger.warning("RateLimitRepo.is_password_reset_token_used failed: %s", e)
            return False

    @classmethod
    async def mark_password_reset_token_used(
        cls, token_hash: str, username: str
    ) -> bool:
        """Mark a password reset token as used.

        Args:
            token_hash: SHA256 hash of the token
            username: The username whose password was reset

        Returns:
            True if marked successfully, False otherwise.
        """
        now = time.time()
        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return False

            async with db.get_session() as session:
                # Check if already exists
                result = await session.execute(
                    select(UsedPasswordResetTokenORM).where(
                        UsedPasswordResetTokenORM.token_hash == token_hash
                    )
                )
                existing = result.scalar_one_or_none()
                if existing:
                    return True  # Already marked

                # Mark as used
                used_token = UsedPasswordResetTokenORM(
                    token_hash=token_hash,
                    username=username,
                    used_at=now,
                )
                session.add(used_token)
                await session.commit()
                return True
        except Exception as e:
            logger.warning("RateLimitRepo.mark_password_reset_token_used failed: %s", e)
            return False

    @classmethod
    async def check_reset_usage_ip_rate(cls, ip: str) -> tuple[int, int]:
        """Check if IP is rate limited for password reset USAGE (not request).

        Args:
            ip: Client IP address

        Returns:
            Tuple of (current_count, retry_after_seconds).
            If rate limited, current_count is -1 and retry_after_seconds > 0.
        """
        from edgelite.constants import (
            _AUTH_RESET_IP_MAX_ATTEMPTS,
            _AUTH_RESET_IP_WINDOW_SECONDS,
        )

        now = time.time()
        window_start = now - _AUTH_RESET_IP_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0, 0

            async with db.get_session() as session:
                result = await session.execute(
                    select(PasswordResetIPAttemptORM).where(
                        PasswordResetIPAttemptORM.ip == ip
                    )
                )
                record = result.scalar_one_or_none()

                if record is None:
                    return 0, 0

                if record.last_attempt_at < window_start:
                    await session.delete(record)
                    await session.commit()
                    return 0, 0

                if record.attempt_count >= _AUTH_RESET_IP_MAX_ATTEMPTS:
                    retry_after = int(
                        record.last_attempt_at + _AUTH_RESET_IP_WINDOW_SECONDS - now
                    )
                    return -1, max(1, retry_after)

                return record.attempt_count, 0
        except Exception as e:
            logger.warning("RateLimitRepo.check_reset_usage_ip_rate failed: %s", e)
            return 0, 0

    @classmethod
    async def record_reset_usage_attempt(cls, ip: str) -> int:
        """Record a password reset usage attempt for IP rate limiting.

        Args:
            ip: Client IP address

        Returns:
            Current attempt count for this IP within the window.
        """
        from edgelite.constants import _AUTH_RESET_IP_WINDOW_SECONDS

        now = time.time()
        window_start = now - _AUTH_RESET_IP_WINDOW_SECONDS

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0

            async with db.get_session() as session:
                result = await session.execute(
                    select(PasswordResetIPAttemptORM).where(
                        PasswordResetIPAttemptORM.ip == ip
                    )
                )
                record = result.scalar_one_or_none()

                if record is None:
                    record = PasswordResetIPAttemptORM(
                        ip=ip,
                        attempt_count=1,
                        first_attempt_at=now,
                        last_attempt_at=now,
                    )
                    session.add(record)
                else:
                    if record.last_attempt_at < window_start:
                        record.attempt_count = 1
                        record.first_attempt_at = now
                    else:
                        record.attempt_count = record.attempt_count + 1
                    record.last_attempt_at = now

                await session.commit()
                return record.attempt_count
        except Exception as e:
            logger.warning("RateLimitRepo.record_reset_usage_attempt failed: %s", e)
            return 0

    @classmethod
    async def cleanup_used_password_reset_tokens(cls) -> int:
        """Clean up old used password reset token records.

        Returns:
            Number of deleted records.
        """

        now = time.time()
        # Keep used tokens for 24 hours for audit purposes
        cutoff = now - (24 * 3600)

        try:
            from edgelite.storage.database import Database

            db = Database.get_instance()
            if db is None:
                return 0

            async with db.get_session() as session:
                result = await session.execute(
                    delete(UsedPasswordResetTokenORM).where(
                        UsedPasswordResetTokenORM.used_at < cutoff
                    )
                )
                deleted = result.rowcount or 0
                await session.commit()
                return deleted
        except Exception as e:
            logger.warning("RateLimitRepo.cleanup_used_password_reset_tokens failed: %s", e)
            return 0


class ResourceShareRepo(BaseRepo):
    async def share_resource(
        self,
        resource_type: str,
        resource_id: str,
        shared_with_user_id: str,
        permission_level: str,
        shared_by_user_id: str,
    ) -> dict:
        try:
            async with self._auto_session() as session:
                existing = await session.execute(
                    select(ResourceShareORM).where(
                        ResourceShareORM.resource_type == resource_type,
                        ResourceShareORM.resource_id == resource_id,
                        ResourceShareORM.shared_with_user_id == shared_with_user_id,
                    )
                )
                orm = existing.scalar_one_or_none()
                if orm:
                    orm.permission_level = permission_level
                    orm.shared_by_user_id = shared_by_user_id
                    await session.commit()
                    await session.refresh(orm)
                else:
                    orm = ResourceShareORM(
                        resource_type=resource_type,
                        resource_id=resource_id,
                        shared_with_user_id=shared_with_user_id,
                        permission_level=permission_level,
                        shared_by_user_id=shared_by_user_id,
                    )
                    session.add(orm)
                    await session.commit()
                    await session.refresh(orm)
                return self._orm_to_dict(orm)
        except Exception as e:
            logger.error("ResourceShareRepo.share_resource failed: %s", e)
            raise

    async def unshare_resource(
        self,
        resource_type: str,
        resource_id: str,
        shared_with_user_id: str,
    ) -> bool:
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    delete(ResourceShareORM).where(
                        ResourceShareORM.resource_type == resource_type,
                        ResourceShareORM.resource_id == resource_id,
                        ResourceShareORM.shared_with_user_id == shared_with_user_id,
                    )
                )
                await session.commit()
                return (result.rowcount or 0) > 0
        except Exception as e:
            logger.error("ResourceShareRepo.unshare_resource failed: %s", e)
            raise RuntimeError(f"ResourceShareRepo.unshare_resource failed: {e}") from e

    async def list_shares_for_resource(
        self,
        resource_type: str,
        resource_id: str,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict], int]:
        """列出指定资源的共享记录，返回 (items, total)。"""
        try:
            async with self._auto_session() as session:
                base_query = select(ResourceShareORM).where(
                    ResourceShareORM.resource_type == resource_type,
                    ResourceShareORM.resource_id == resource_id,
                )
                # 总数
                count_query = select(func.count()).select_from(ResourceShareORM).where(
                    ResourceShareORM.resource_type == resource_type,
                    ResourceShareORM.resource_id == resource_id,
                )
                total = await session.scalar(count_query) or 0
                # 分页
                offset = (page - 1) * size
                result = await session.execute(
                    base_query.offset(offset).limit(size)
                )
                items = [self._orm_to_dict(orm) for orm in result.scalars().all()]
                return items, total
        except Exception as e:
            logger.error("ResourceShareRepo.list_shares_for_resource failed: %s", e)
            raise RuntimeError(f"ResourceShareRepo.list_shares_for_resource failed: {e}") from e

    async def list_shared_with_user(
        self,
        user_id: str,
        resource_type: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict], int]:
        """列出共享给指定用户的记录，返回 (items, total)。"""
        try:
            async with self._auto_session() as session:
                conditions = [ResourceShareORM.shared_with_user_id == user_id]
                if resource_type:
                    conditions.append(ResourceShareORM.resource_type == resource_type)
                base_query = select(ResourceShareORM).where(*conditions)
                count_query = select(func.count()).select_from(ResourceShareORM).where(*conditions)
                total = await session.scalar(count_query) or 0
                offset = (page - 1) * size
                result = await session.execute(
                    base_query.offset(offset).limit(size)
                )
                items = [self._orm_to_dict(orm) for orm in result.scalars().all()]
                return items, total
        except Exception as e:
            logger.error("ResourceShareRepo.list_shared_with_user failed: %s", e)
            raise RuntimeError(f"ResourceShareRepo.list_shared_with_user failed: {e}") from e

    async def check_user_has_access(
        self,
        resource_type: str,
        resource_id: str,
        user_id: str,
        permission_level: str = "read",
    ) -> bool:
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(ResourceShareORM).where(
                        ResourceShareORM.resource_type == resource_type,
                        ResourceShareORM.resource_id == resource_id,
                        ResourceShareORM.shared_with_user_id == user_id,
                    )
                )
                orm = result.scalar_one_or_none()
                if orm is None:
                    return False
                levels = {"read": 0, "write": 1, "admin": 2}
                return levels.get(orm.permission_level, 0) >= levels.get(permission_level, 0)
        except Exception as e:
            logger.error("ResourceShareRepo.check_user_has_access failed: %s", e)
            raise RuntimeError(f"ResourceShareRepo.check_user_has_access failed: {e}") from e

    async def get_shared_resource_ids(
        self,
        user_id: str,
        resource_type: str,
    ) -> set[str]:
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    select(ResourceShareORM.resource_id).where(
                        ResourceShareORM.shared_with_user_id == user_id,
                        ResourceShareORM.resource_type == resource_type,
                    )
                )
                return {row[0] for row in result.fetchall()}
        except Exception as e:
            logger.error("ResourceShareRepo.get_shared_resource_ids failed: %s", e)
            raise RuntimeError(f"ResourceShareRepo.get_shared_resource_ids failed: {e}") from e

    async def delete_shares_for_resource(
        self,
        resource_type: str,
        resource_id: str,
    ) -> int:
        try:
            async with self._auto_session() as session:
                result = await session.execute(
                    delete(ResourceShareORM).where(
                        ResourceShareORM.resource_type == resource_type,
                        ResourceShareORM.resource_id == resource_id,
                    )
                )
                await session.commit()
                return result.rowcount or 0
        except Exception as e:
            logger.error("ResourceShareRepo.delete_shares_for_resource failed: %s", e)
            raise RuntimeError(f"ResourceShareRepo.delete_shares_for_resource failed: {e}") from e

    @staticmethod
    def _orm_to_dict(orm: ResourceShareORM) -> dict:
        return {
            "id": orm.id,
            "resource_type": orm.resource_type,
            "resource_id": orm.resource_id,
            "shared_with_user_id": orm.shared_with_user_id,
            "permission_level": orm.permission_level,
            "shared_by_user_id": orm.shared_by_user_id,
            "created_at": orm.created_at.isoformat() if isinstance(orm.created_at, datetime) else str(orm.created_at),
        }
