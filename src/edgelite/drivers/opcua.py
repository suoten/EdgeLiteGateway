"""OPC-UA基础接入驱动 - 基于opcua-asyncio"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import queue as _queue_mod
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

from edgelite.drivers.base import DriverCapabilities, DriverPlugin, PointValue
from edgelite.drivers.edge_rule_engine import (
    AlarmRecord,
    EdgeRule,
    EdgeRuleOperator,
    EdgeRuleType,
    ModbusEdgeRuleEngine,
)
from edgelite.drivers.edge_triggers import EdgeTriggerExecutor
from edgelite.drivers.opcua_audit import OpcUaAudit, OpcUaAuditAction
from edgelite.drivers.opcua_config_version import OpcUaConfigVersionManager
from edgelite.drivers.opcua_ota import OpcUaOtaManager, OtaPackage
from edgelite.drivers.opcua_ts_store import OpcUaOfflineSyncManager, OpcUaTsStore
from edgelite.drivers.rule_store import RuleStore
from edgelite.engine.event_bus import MqttForwardEvent, PointUpdateEvent
from edgelite.error_codes import OpcUaDriverErrors
from edgelite.packet_recorder import record_packet
from edgelite.security.rbac import Permission, has_permission
from edgelite.services.i18n import t as _t

logger = logging.getLogger(__name__)

_OPCUA_QUALITY_MAP = {
    0: "bad",
    4: "bad",
    8: "bad",
    12: "bad",
    16: "bad",
    20: "bad",
    24: "bad",
    28: "bad",
    32: "uncertain",
    36: "uncertain",
    40: "uncertain",
    44: "uncertain",
    48: "uncertain",
    52: "uncertain",
    56: "uncertain",
    60: "uncertain",
    64: "uncertain",
    68: "uncertain",
    72: "uncertain",
    76: "uncertain",
    80: "uncertain",
    84: "uncertain",
    88: "uncertain",
    92: "uncertain",
}

_OPCUA_STATUS_CODE_NAME_MAP: dict[int, str] = {
    0x80000000: "Bad",
    0x80010000: "BadUnexpectedError",
    0x80020000: "BadInternalError",
    0x80030000: "BadOutOfMemory",
    0x80040000: "BadResourceUnavailable",
    0x80050000: "BadCommunicationError",
    0x80060000: "BadEncodingError",
    0x80070000: "BadDecodingError",
    0x80080000: "BadEncodingLimitsExceeded",
    0x80200000: "BadUnknownResponse",
    0x80210000: "BadTimeout",
    0x80220000: "BadServiceUnsupported",
    0x80230000: "BadShutdown",
    0x80240000: "BadServerNotConnected",
    0x80250000: "BadServerHalted",
    0x80340000: "BadNodeIdInvalid",
    0x80350000: "BadNodeIdUnknown",
    0x80370000: "BadAttributeIdInvalid",
    0x803E0000: "BadNotReadable",
    0x803F0000: "BadNotWritable",
    0x80430000: "BadOutOfRange",
    0x80440000: "BadNotSupported",
    0x80450000: "BadNotFound",
    0x80460000: "BadObjectDeleted",
    0x80470000: "BadNotImplemented",
    0x80480000: "BadMonitoringModeInvalid",
    0x80490000: "BadMonitoredItemIdInvalid",
    0x804A0000: "BadMonitoredItemFilterInvalid",
    0x804B0000: "BadMonitoredItemFilterUnsupported",
    0x80500000: "BadNodeNotConnected",
    0x80510000: "BadOutOfService",
    0x80520000: "BadAlreadyExists",
    0x80530000: "BadSessionClosed",
    0x80540000: "BadSessionNotActivated",
    0x80550000: "BadSubscriptionIdInvalid",
    0x80570000: "BadTooManyPublishRequests",
    0x80B10000: "BadCertificateInvalid",
    0x80B20000: "BadCertificateTimeInvalid",
    0x80B30000: "BadCertificateIssuerTimeInvalid",
    0x80B40000: "BadCertificateHostNameInvalid",
    0x80B50000: "BadCertificateUriInvalid",
    0x80B60000: "BadCertificateUseNotAllowed",
    0x80B70000: "BadCertificateIssuerUseNotAllowed",
    0x80B80000: "BadCertificateRevoked",
    0x80B90000: "BadCertificateIssuerRevoked",
    0x404D0000: "UncertainNotEnoughData",
    0x405E0000: "UncertainLastUsableValue",
    0x40600000: "UncertainSensorNotAccurate",
    0x40890000: "UncertainEngineeringUnitsExceeded",
    0x40A40000: "UncertainSimulatedValue",
}

_I18N_ERROR_CODE_MAP = {
    "READ_TIMEOUT": "ERR_OPCUA_READ_TIMEOUT",
    "READ_ERROR": "ERR_OPCUA_READ_FAILED",
    "BATCH_READ_TIMEOUT": "ERR_OPCUA_BATCH_READ_TIMEOUT",
    "BATCH_READ_ERROR": "ERR_OPCUA_BATCH_READ_FAILED",
    "WRITE_TIMEOUT": "ERR_OPCUA_WRITE_TIMEOUT",
    "WRITE_ERROR": "ERR_OPCUA_WRITE_FAILED",
    "BATCH_WRITE_TIMEOUT": "ERR_OPCUA_BATCH_WRITE_TIMEOUT",
    "BATCH_WRITE_ERROR": "ERR_OPCUA_BATCH_WRITE_FAILED",
    "SESSION_EXPIRED": "ERR_OPCUA_SESSION_EXPIRED",
    "CERT_EXPIRED": "ERR_OPCUA_CERT_EXPIRED",
    "CERT_EXPIRING": "ERR_OPCUA_CERT_EXPIRING",
    "SUBSCRIPTION_FAILED": "ERR_OPCUA_SUBSCRIPTION_FAILED",
    "CONN_FAILED": "ERR_OPCUA_CONN_FAILED",
    "RECONNECTING": "ERR_OPCUA_RECONNECTING",
    "SECURITY_SKIP": "ERR_OPCUA_SECURITY_SKIP",
    "CALLBACK_ERROR": "ERR_OPCUA_CALLBACK_ERROR",
    "FAILOVER_TRIGGERED": "ERR_OPCUA_FAILOVER_TRIGGERED",
    "FAILOVER_NO_BACKUP": "ERR_OPCUA_FAILOVER_NO_BACKUP",
    "FAILOVER_REVERT": "ERR_OPCUA_FAILOVER_REVERT",
    "CERT_AUTO_RENEW_OK": "ERR_OPCUA_CERT_AUTO_RENEW_OK",
    "CERT_AUTO_RENEW_FAILED": "ERR_OPCUA_CERT_AUTO_RENEW_FAILED",
    "SECURITY_DEGRADED": "ERR_OPCUA_SECURITY_DEGRADED",
    "SESSION_PRE_EXPIRY_REBUILD": "ERR_OPCUA_SESSION_PRE_EXPIRY_REBUILD",
    "KEEPALIVE_FAILED": "ERR_OPCUA_KEEPALIVE_FAILED",
    "STATE_TRANSITION": "ERR_OPCUA_STATE_TRANSITION",
    "RATE_OF_CHANGE": "ERR_OPCUA_RATE_OF_CHANGE",
    "FROZEN_VALUE": "ERR_OPCUA_FROZEN_VALUE",
    "NAN_INF": "ERR_OPCUA_NAN_INF",
    "COMPLEX_TYPE_HEX_FALLBACK": "ERR_OPCUA_COMPLEX_TYPE_HEX_FALLBACK",
    "COMPLEX_TYPE_UNKNOWN": "ERR_OPCUA_COMPLEX_TYPE_UNKNOWN",
    "ARRAY_TRUNCATED": "ERR_OPCUA_ARRAY_TRUNCATED",
    "SUB_DEGRADED_POLLING": "ERR_OPCUA_SUB_DEGRADED_POLLING",
    "SUB_RECOVERED": "ERR_OPCUA_SUB_RECOVERED",
    "STALE_DATA": "ERR_OPCUA_STALE_DATA",
    "DEADBAND_NATIVE_FAILED": "ERR_OPCUA_DEADBAND_NATIVE_FAILED",
    "WRITE_VERIFY_FAILED": "ERR_OPCUA_WRITE_VERIFY_FAILED",
    "WRITE_TYPE_MISMATCH": "ERR_OPCUA_WRITE_TYPE_MISMATCH",
    "WRITE_RATE_LIMITED": "ERR_OPCUA_WRITE_RATE_LIMITED",
    "WRITE_VALUE_INVALID": "ERR_OPCUA_WRITE_VALUE_INVALID",
    "WRITE_AUDIT_OK": "ERR_OPCUA_WRITE_AUDIT_OK",
    "FAILOVER_FAST_SWITCH": "ERR_OPCUA_FAILOVER_FAST_SWITCH",
    "SESSION_RESTORED": "ERR_OPCUA_SESSION_RESTORED",
    "SESSION_PERSIST_FAILED": "ERR_OPCUA_SESSION_PERSIST_FAILED",
    "CERT_FAILOVER_TRIGGERED": "ERR_OPCUA_CERT_FAILOVER_TRIGGERED",
    "CERT_FAILOVER_REVERT": "ERR_OPCUA_CERT_FAILOVER_REVERT",
    "RULE_EVALUATED": "ERR_OPCUA_RULE_EVALUATED",
    "RULE_TRIGGERED": "ERR_OPCUA_RULE_TRIGGERED",
    "RULE_HOT_RELOADED": "ERR_OPCUA_RULE_HOT_RELOADED",
    "CROSS_SERVER_WRITE": "ERR_OPCUA_CROSS_SERVER_WRITE",
    "DATA_PERSIST_LOCAL": "ERR_OPCUA_DATA_PERSIST_LOCAL",
    "DATA_SYNC_RECOVERED": "ERR_OPCUA_DATA_SYNC_RECOVERED",
    "DATA_COMPRESS_UPLOAD": "ERR_OPCUA_DATA_COMPRESS_UPLOAD",
    "CONFIG_VERSION_SAVE_OK": "ERR_OPCUA_CONFIG_VERSION_SAVE_OK",
    "CONFIG_VERSION_ROLLBACK_OK": "ERR_OPCUA_CONFIG_VERSION_ROLLBACK_OK",
    "OTA_START_OK": "ERR_OPCUA_OTA_START_OK",
    "OTA_START_FAILED": "ERR_OPCUA_OTA_START_FAILED",
    "OTA_VERIFY_FAILED": "ERR_OPCUA_OTA_VERIFY_FAILED",
    "OTA_ROLLBACK_OK": "ERR_OPCUA_OTA_ROLLBACK_OK",
    "OTA_ROLLBACK_FAILED": "ERR_OPCUA_OTA_ROLLBACK_FAILED",
    "OTA_IN_PROGRESS": "ERR_OPCUA_OTA_IN_PROGRESS",
    "RBAC_DENIED": "ERR_OPCUA_RBAC_DENIED",
    "AUDIT_LOG_OK": "ERR_OPCUA_AUDIT_LOG_OK",
}


class OpcUaConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CERT_VALIDATING = "cert_validating"
    SESSION_CREATING = "session_creating"
    SUBSCRIBING = "subscribing"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class CollectionMode(StrEnum):
    SUBSCRIPTION = "subscription"
    POLLING = "polling"


_BACKOFF_BASE = 5.0
_BACKOFF_MAX = 600.0
_BACKOFF_JITTER_MAX = 5.0
_KEEPALIVE_CHECK_INTERVAL = 5.0
_SESSION_PRE_EXPIRY_RATIO = 0.75
_FROZEN_COUNT_THRESHOLD = 10
_STALE_MULTIPLIER = 3
_MAX_ARRAY_LENGTH = 1024
_WRITE_RATE_LIMIT_MS = 500
_WRITE_AUDIT_MAX_ENTRIES = 10000
_FAILOVER_TIMEOUT_MS = 3000
_PRIMARY_PROBE_INTERVAL = 30.0
_SESSION_PERSIST_PATH = "session_state"
_OPCUA_INT_TYPES = frozenset({"SByte", "Byte", "Int16", "Int32", "Int64", "UInt16", "UInt32", "UInt64"})
_OPCUA_FLOAT_TYPES = frozenset({"Float", "Double"})
_OPCUA_NUMERIC_TYPES = _OPCUA_INT_TYPES | _OPCUA_FLOAT_TYPES
_OPCUA_BUILTIN_TYPE_IDS = {
    1: "Boolean",
    2: "SByte",
    3: "Byte",
    4: "Int16",
    5: "UInt16",
    6: "Int32",
    7: "UInt32",
    8: "Int64",
    9: "UInt64",
    10: "Float",
    11: "Double",
    12: "String",
    13: "DateTime",
    14: "Guid",
    15: "ByteString",
}
_MAX_CONNECT_RETRIES = 5
_MAX_TOTAL_RECONNECT_DURATION = 86400  # FIXED-P1: 最大总重连时长24小时，超限后标记OFFLINE停止自动重连
MAX_OPCUA_CONNECTIONS = 50


@dataclass
class OpcUaPointHealthStats:
    success_count: int = 0
    fail_count: int = 0
    consecutive_fails: int = 0
    last_value: Any = None
    last_timestamp: float = 0.0
    last_publish_at: float = 0.0
    same_value_count: int = 0
    subscription_count: int = 0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 1.0

    def record_success(self) -> None:
        self.success_count += 1
        self.consecutive_fails = 0
        self.last_publish_at = time.monotonic()

    def record_failure(self) -> None:
        self.fail_count += 1
        self.consecutive_fails += 1


def _bad_pv(error_code: str) -> PointValue:
    return PointValue(value=None, quality="bad", timestamp=datetime.now(UTC), source=f"opcua:{error_code}")


def _map_opcua_quality(status_code: Any) -> str:
    if status_code is None:
        return "good"
    raw = None
    if hasattr(status_code, "value"):
        raw = status_code.value
    elif isinstance(status_code, int):
        raw = status_code
    elif hasattr(status_code, "StatusCode"):
        raw = status_code.StatusCode.value if hasattr(status_code.StatusCode, "value") else None
    if raw is None:
        return "good"
    if raw & 0x80000000:
        return "bad"
    if raw & 0x40000000:
        return "uncertain"
    severity = raw & 0xC0000000
    if severity == 0:
        return "good"
    lower = raw & 0xFFFF0000
    if lower in _OPCUA_QUALITY_MAP:
        return _OPCUA_QUALITY_MAP[lower]
    if raw in _OPCUA_QUALITY_MAP:
        return _OPCUA_QUALITY_MAP[raw]
    if 32 <= raw < 192:
        return "uncertain"
    return "bad"


def _opcua_status_name(status_code: Any) -> str | None:
    """返回 OPC-UA 状态码的可读名称，未命名则返回 None。

    用于 PointValue.source 或日志诊断中携带具体状态码名称 (如 "BadTimeout")。
    """
    if status_code is None:
        return None
    raw = None
    if hasattr(status_code, "value"):
        raw = status_code.value
    elif isinstance(status_code, int):
        raw = status_code
    elif hasattr(status_code, "StatusCode"):
        sc = status_code.StatusCode
        raw = sc.value if hasattr(sc, "value") else None
    if raw is None or raw == 0:
        return None
    return _OPCUA_STATUS_CODE_NAME_MAP.get(raw)


def _calc_backoff(fail_count: int) -> float:
    delay = min(_BACKOFF_BASE * (2 ** min(fail_count - 1, 7)), _BACKOFF_MAX)
    jitter = random.uniform(0, _BACKOFF_JITTER_MAX)
    return min(delay + jitter, 300.0)


def _resolve_complex_type_with_fallback(obj: Any, depth: int = 0, node_id: str = "") -> tuple[Any, str]:
    if depth >= 10:
        logger.warning(
            "OPC-UA complex type resolution depth limit reached for node %s (depth=%d), returning str representation",
            node_id or type(obj).__name__,
            depth,
        )
        return str(obj), "bad"
    if isinstance(obj, (int, float, str, bool)):
        return obj, "good"
    # R11-DRV-14: bytes.hex() 与 bytearray.hex() 均不会抛异常，原 try/except 为死代码，移除冗余异常处理
    if isinstance(obj, bytes):
        return obj.hex(), "uncertain"
    if isinstance(obj, bytearray):
        return obj.hex(), "uncertain"
    if isinstance(obj, (list, tuple)):
        if len(obj) > _MAX_ARRAY_LENGTH:
            truncated = list(obj[:_MAX_ARRAY_LENGTH])
            inner_results = [_resolve_complex_type_with_fallback(item, depth + 1, node_id) for item in truncated]
            result: Any = [v for v, _ in inner_results]
            worst_q = "good"
            for _, q in inner_results:
                if q == "bad":
                    worst_q = "bad"
                    break
                if q == "uncertain":
                    worst_q = "uncertain"
            return result, "uncertain"
        inner_results = [_resolve_complex_type_with_fallback(item, depth + 1, node_id) for item in obj]
        result = [v for v, _ in inner_results]
        worst_q = "good"
        for _, q in inner_results:
            if q == "bad":
                worst_q = "bad"
                break
            if q == "uncertain":
                worst_q = "uncertain"
        return result, worst_q
    if isinstance(obj, dict):
        result = {}
        worst_q = "good"
        # FIXED-P1: dict类型递归解析增加元素数量限制，防止恶意OPC-UA服务器返回超深嵌套结构
        for k, v in list(obj.items())[:_MAX_ARRAY_LENGTH]:
            rv, rq = _resolve_complex_type_with_fallback(v, depth + 1, node_id)
            result[k] = rv
            if rq == "bad":
                worst_q = "bad"
            elif rq == "uncertain" and worst_q != "bad":
                worst_q = "uncertain"
        return result, worst_q
    if hasattr(obj, "__dict__"):
        result = {}
        worst_q = "good"
        # FIXED-P1: __dict__类型递归解析增加元素数量限制，与dict类型一致
        for key, val in list(obj.__dict__.items())[:_MAX_ARRAY_LENGTH]:
            if not key.startswith("_"):
                rv, rq = _resolve_complex_type_with_fallback(val, depth + 1, node_id)
                result[key] = rv
                if rq == "bad":
                    worst_q = "bad"
                elif rq == "uncertain" and worst_q != "bad":
                    worst_q = "uncertain"
        if not result:
            try:
                return obj.hex(), "uncertain"
            except Exception as e:
                logger.warning("[opcua] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            try:
                return bytes(obj).hex(), "uncertain"
            except Exception as e:
                logger.warning("[opcua] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            return str(obj), "bad"
        return result, worst_q
    try:
        return obj.hex(), "uncertain"
    except Exception as e:
        logger.warning("[opcua] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
    try:
        return bytes(obj).hex(), "uncertain"
    except Exception as e:
        logger.warning("[opcua] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
    return str(obj), "bad"


class OpcUaDriver(DriverPlugin):
    _CONNECT_TIMEOUT = 5
    _READ_TIMEOUT = 30
    _WRITE_TIMEOUT = 10

    plugin_name = "opcua"
    plugin_version = "0.3.0"
    supported_protocols = ("opcua",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    _required_dependencies: tuple[str, ...] = ("asyncua",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    config_schema = {
        "description": "OPC UA industrial protocol, supports encrypted authentication and node browsing",
        "required": ["endpoint"],
        "properties": {
            "endpoint": {"type": "string", "description": "OPC UA server endpoint URL", "format": "url"},
            "backup_endpoint": {"type": "string", "description": "Backup OPC UA server endpoint URL for redundancy"},
            "security_mode": {
                "type": "string",
                "description": "Encryption mode",
                "enum": ["None", "Sign", "SignAndEncrypt"],
            },
            "deadband": {"type": "number", "description": "Deadband filter threshold (software-side)", "minimum": 0},
            "scaling": {
                "type": "object",
                "description": "Linear scaling transformation",
                "properties": {
                    "ratio": {"type": "number", "default": 1.0},
                    "offset": {"type": "number", "default": 0.0},
                },
            },
            "clamp": {
                "type": "object",
                "description": "Value range validation",
                "properties": {"min": {"type": "number"}, "max": {"type": "number"}},
            },
            "rate_of_change": {"type": "number", "description": "Rate of change threshold (units/s)", "minimum": 0},
            "frozen_count": {
                "type": "integer",
                "description": "Frozen value detection threshold (consecutive same values)",
                "minimum": 2,
            },
            "use_native_deadband": {
                "type": "boolean",
                "description": "Prefer OPC-UA native deadband over software deadband",
                "default": True,
            },
            "collection_mode": {
                "type": "string",
                "description": "Collection mode: auto/subscription/polling",
                "enum": ["auto", "subscription", "polling"],
                "default": "auto",
            },
            "write_type_strategy": {
                "type": "string",
                "description": "Type mismatch strategy: truncate or reject",
                "enum": ["truncate", "reject"],
                "default": "reject",
            },
            "backup_client_cert_path": {
                "type": "string",
                "description": "Backup client certificate path for cert redundancy",
            },
            "backup_client_key_path": {
                "type": "string",
                "description": "Backup client private key path for cert redundancy",
            },
            "backup_ca_cert_path": {"type": "string", "description": "Backup CA certificate path for cert redundancy"},
        },
        "fields": [
            {
                "name": "endpoint",
                "type": "string",
                "label": "OPC UA Endpoint",
                "description": "Primary OPC UA server endpoint URL",
                "default": "opc.tcp://localhost:4840",
                "required": True,
            },
            {
                "name": "backup_endpoint",
                "type": "string",
                "label": "Backup Endpoint",
                "description": "Backup OPC UA server endpoint URL for redundancy failover",
                "default": "",
            },
            {
                "name": "username",
                "type": "string",
                "label": "Username",
                "description": "Leave empty for anonymous login",
            },
            {
                "name": "password",
                "type": "string",
                "label": "Password",
                "description": "User password, leave empty for anonymous login",
                "secret": True,
            },
            {
                "name": "security_mode",
                "type": "string",
                "label": "Security Mode",
                "description": "Encryption mode, None=plaintext, SignAndEncrypt=highest security",
                "default": "SignAndEncrypt",
                "options": ["None", "Sign", "SignAndEncrypt"],
            },
            {
                "name": "security_policy",
                "type": "string",
                "label": "Security Policy",
                "description": "Security policy: None, Basic128Rsa15, Basic256, Basic256Sha256",
                "default": "Basic256Sha256",
                "options": ["None", "Basic128Rsa15", "Basic256", "Basic256Sha256"],
            },
            {
                "name": "client_cert_path",
                "type": "string",
                "label": "Client Cert Path",
                "description": "Path to client certificate file (PEM/DER)",
                "default": "",
            },
            {
                "name": "client_key_path",
                "type": "string",
                "label": "Client Key Path",
                "description": "Path to client private key file (PEM/DER)",
                "default": "",
            },
            {
                "name": "ca_cert_path",
                "type": "string",
                "label": "CA Cert Path",
                "description": "Path to CA certificate file (PEM/DER)",
                "default": "",
            },
            {
                "name": "session_timeout",
                "type": "integer",
                "label": "Session Timeout (ms)",
                "description": "OPC UA session timeout in milliseconds",
                "default": 60000,
            },
            {
                "name": "subscription_interval",
                "type": "integer",
                "label": "Subscription Interval (ms)",
                "description": "Subscription publishing interval in milliseconds",
                "default": 500,
            },
            {
                "name": "deadband_type",
                "type": "string",
                "label": "Deadband Type (Native)",
                "description": "Native OPC UA deadband filter type: None=no filter, Absolute=absolute change, Percent=percent of range",
                "default": "None",
                "options": ["None", "Absolute", "Percent"],
            },
            {
                "name": "deadband_value",
                "type": "number",
                "label": "Deadband Value (Native)",
                "description": "Native OPC UA deadband threshold value (0 to disable)",
                "default": 0,
            },
            {
                "name": "use_native_deadband",
                "type": "boolean",
                "label": "Use Native Deadband",
                "description": "Prefer OPC-UA native deadband over software deadband",
                "default": True,
            },
            {
                "name": "use_subscription",
                "type": "boolean",
                "label": "Use Subscription",
                "description": "Enable subscription mode for data change notifications",
                "default": True,
            },
            {
                "name": "collection_mode",
                "type": "string",
                "label": "Collection Mode",
                "description": "Collection mode: auto=subscription with polling fallback, subscription=subscription only, polling=polling only",
                "default": "auto",
                "options": ["auto", "subscription", "polling"],
            },
            {
                "name": "deadband",
                "type": "number",
                "label": "Deadband (Software)",
                "description": "Software-side deadband filter threshold, suppress updates when change < deadband",
                "default": None,
            },
            {
                "name": "scaling",
                "type": "object",
                "label": "Scaling",
                "description": "Linear scaling: y = x * ratio + offset",
                "default": None,
                "fields": [
                    {
                        "name": "ratio",
                        "type": "number",
                        "label": "Ratio",
                        "description": "Scaling ratio (multiplier)",
                        "default": 1.0,
                    },
                    {
                        "name": "offset",
                        "type": "number",
                        "label": "Offset",
                        "description": "Scaling offset (addend)",
                        "default": 0.0,
                    },
                ],
            },
            {
                "name": "clamp",
                "type": "object",
                "label": "Clamp",
                "description": "Value range validation, mark quality=bad when out of range",
                "default": None,
                "fields": [
                    {"name": "min", "type": "number", "label": "Min", "description": "Minimum allowed value"},
                    {"name": "max", "type": "number", "label": "Max", "description": "Maximum allowed value"},
                ],
            },
            {
                "name": "rate_of_change",
                "type": "number",
                "label": "Rate of Change",
                "description": "Rate of change threshold (units/s), mark quality=uncertain when exceeded",
                "default": None,
            },
            {
                "name": "frozen_count",
                "type": "integer",
                "label": "Frozen Count",
                "description": "Frozen value detection: consecutive same values before marking uncertain",
                "default": 10,
            },
            {
                "name": "write_type_strategy",
                "type": "string",
                "label": "Write Type Strategy",
                "description": "Strategy when write value type mismatches node data type: truncate=auto-convert, reject=reject write",
                "default": "reject",
                "options": ["reject", "truncate"],
            },
            {
                "name": "backup_client_cert_path",
                "type": "string",
                "label": "Backup Client Cert Path",
                "description": "Backup client certificate path, auto-switch when primary cert expires",
                "default": "",
            },
            {
                "name": "backup_client_key_path",
                "type": "string",
                "label": "Backup Client Key Path",
                "description": "Backup client private key path for cert redundancy",
                "default": "",
            },
            {
                "name": "backup_ca_cert_path",
                "type": "string",
                "label": "Backup CA Cert Path",
                "description": "Backup CA certificate path for cert redundancy",
                "default": "",
            },
        ],
    }

    experimental = False
    capabilities = DriverCapabilities(
        discover=True, read=True, write=True, subscribe=True, batch_read=True, batch_write=True
    )
    constraints = (
        {
            "type": "feature_note",
            "message": "Batch read uses ReadMultipleNodes for optimized reading; Node browse integrated in discovery",
        },
    )  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple

    def __init__(self):
        super().__init__()
        self._config: dict = {}
        self._device_configs: dict[str, dict] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._latest_values: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, Any] = {}
        self._pending_connections: int = 0  # FIXED-P2: 连接池上限检查与实际连接数不一致，增加待连接计数器
        self._clients_lock = asyncio.Lock()  # FIXED-P0: 客户端字典访问加锁保护
        self._subscriptions: dict[str, Any] = {}
        self._sub_handlers: dict[
            str, _SubHandler
        ] = {}  # FIXED-P0: 追踪SubHandler实例，确保stop/remove_device时能取消通知任务
        self._data_callback: Callable | None = None
        self._connect_tasks: dict[str, asyncio.Task] = {}
        self._connect_fail_count: dict[str, int] = {}
        self._last_connect_log: dict[str, float] = {}
        self._values_lock = asyncio.Lock()
        self._ns_cache: dict[str, dict[str, int]] = {}
        self._event_bus: Any = None
        self._batch_size: int = 50
        self._session_expired: dict[str, bool] = {}
        self._connection_states: dict[str, OpcUaConnectionState] = {}
        self._active_endpoints: dict[str, str] = {}
        self._subscription_locks: dict[str, asyncio.Lock] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_rebuilding: dict[str, asyncio.Event] = {}  # UA-002: 防止并发 session 重建
        self._session_rebuild_skip: dict[str, bool] = {}  # FIXED-P0: 标记跳过重建等待（超时后不再阻塞后续读取）
        self._rebuild_wait_queue: dict[str, asyncio.Queue] = {}  # UA-MED-002: 重建期间等待处理的请求队列
        self._session_created_at: dict[str, float] = {}
        self._session_timeout_ms: dict[str, int] = {}
        self._security_degraded: dict[str, bool] = {}
        self._point_health: dict[str, dict[str, OpcUaPointHealthStats]] = {}
        self._collection_modes: dict[str, CollectionMode] = {}
        self._native_deadband_failed: dict[str, bool] = {}
        self._write_rate_limits: dict[str, dict[str, float]] = {}
        self._write_audit_log: deque[dict] = deque(maxlen=10000)
        self._node_data_types: dict[str, dict[str, str]] = {}
        # FIXED(严重): 限制写入速率限制字典和节点数据类型缓存的总容量，防止万级测点场景下内存无界增长
        self._MAX_WRITE_RATE_ENTRIES = 10000
        self._MAX_NODE_TYPE_CACHE = 10000
        self._session_state: dict[str, dict] = {}
        self._failover_at: dict[str, float] = {}
        self._primary_probe_tasks: dict[str, asyncio.Task] = {}
        self._backup_cert_paths: dict[str, dict[str, str]] = {}
        self._rule_engine: ModbusEdgeRuleEngine | None = None
        self._trigger_executor: EdgeTriggerExecutor | None = None
        self._rule_store: RuleStore | None = None
        self._cross_server_writes: dict[str, bool] = {}
        self._ts_store: OpcUaTsStore | None = None
        self._offline_sync: OpcUaOfflineSyncManager | None = None
        self._config_version_mgr: OpcUaConfigVersionManager | None = None
        self._ota_mgr: OpcUaOtaManager | None = None
        self._audit: OpcUaAudit | None = None
        self._certificate_status: dict[str, dict] = {}
        self._original_cert_paths: dict[str, dict[str, str]] = {}
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        self._stopping: bool = False
        self._import_error_devices: set[str] = set()  # FIXED-P2: ImportError后标记设备永久离线，不再重试
        # UA-006: 追踪所有后台 task，避免泄漏
        # _background_tasks inherited from base class  # FIXED-P2: 删除遮蔽基类的覆盖初始化

    def _cancel_reconnect_task(self, device_id: str) -> None:
        old_task = self._reconnect_tasks.get(device_id)
        if old_task is not None and not old_task.done():
            old_task.cancel()
            self._reconnect_tasks.pop(device_id, None)

    def _schedule_reconnect(self, device_id: str, delay: float) -> None:
        if device_id in self._import_error_devices:  # FIXED-P2: ImportError后不再重试
            return
        self._cancel_reconnect_task(device_id)
        # FIXED-P0: 取消已有_connect_device任务，防止并发连接竞争
        # 之前：仅取消reconnect任务，未取消已运行的_connect_device任务，导致并发连接竞争
        old_connect_task = self._connect_tasks.get(device_id)
        if old_connect_task is not None and not old_connect_task.done():
            old_connect_task.cancel()
            self._connect_tasks.pop(device_id, None)

        async def _do_reconnect() -> None:
            await asyncio.sleep(delay)
            self._reconnect_tasks.pop(device_id, None)
            task = asyncio.create_task(self._connect_device(device_id), name=f"opcua-reconnect-{device_id}")
            self._connect_tasks[device_id] = task

        self._reconnect_tasks[device_id] = asyncio.create_task(
            _do_reconnect(), name=f"opcua-delayed-reconnect-{device_id}"
        )

    def _set_state(self, device_id: str, state: OpcUaConnectionState) -> None:
        # FIXED-P1: 状态赋值加锁保护，防止并发_set_state/remove_device导致状态机不一致
        with self._stats_lock:
            old = self._connection_states.get(device_id)
            self._connection_states[device_id] = state
        if old != state:
            self._log_error(
                device_id,
                "STATE_TRANSITION",
                f"msg={old.value if old else 'None'} -> {state.value}",
                level=logging.INFO,
            )
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._set_connection_state(device_id, state.value))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError as e:
                logger.debug("[opcua] set_state failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            if state == OpcUaConnectionState.CONNECTED:
                self.set_offline_sync_online(True)
                self._log_error(
                    device_id, "DATA_SYNC_RECOVERED", "msg=Network online, sync enabled", level=logging.INFO
                )
            elif state in (OpcUaConnectionState.OFFLINE, OpcUaConnectionState.DISCONNECTED):
                self.set_offline_sync_online(False)

    def _get_state(self, device_id: str) -> OpcUaConnectionState:
        # FIXED-P3: 与_set_state写入路径锁保护一致，读取_connection_states加锁防止并发竞态
        with self._stats_lock:
            return self._connection_states.get(device_id, OpcUaConnectionState.DISCONNECTED)

    _MAX_POINT_HEALTH_ENTRIES = 50000  # FIXED-P1: 测点健康统计容量上限，防止万级测点场景下内存无界增长

    def _get_point_health(self, device_id: str, point_name: str) -> OpcUaPointHealthStats:
        dev_health = self._point_health.setdefault(device_id, {})
        if point_name not in dev_health:
            # FIXED-P1: 容量超限时淘汰最早设备条目，防止内存无界增长
            if sum(len(v) for v in self._point_health.values()) >= self._MAX_POINT_HEALTH_ENTRIES:
                oldest_dev = next(iter(self._point_health))
                self._point_health.pop(oldest_dev, None)
            dev_health[point_name] = OpcUaPointHealthStats()
        return dev_health[point_name]

    def _get_point_config(self, device_id: str, point_name: str) -> dict:
        for pt in self._device_points.get(device_id, []):
            if pt.get("name") == point_name:
                return pt
        return {}

    def _get_effective_point_param(self, device_id: str, point_name: str, param: str) -> Any:
        pt_cfg = self._get_point_config(device_id, point_name)
        if param in pt_cfg:
            return pt_cfg[param]
        dev_cfg = self._device_configs.get(device_id, {})
        return dev_cfg.get(param)

    def _check_stale_data(self, device_id: str, point_name: str) -> bool:
        ph = self._get_point_health(device_id, point_name)
        if ph.last_publish_at <= 0:
            return False
        config = self._device_configs.get(device_id, {})
        interval_ms = int(config.get("subscription_interval", 500))
        stale_threshold = (interval_ms / 1000.0) * _STALE_MULTIPLIER
        elapsed = time.monotonic() - ph.last_publish_at
        return elapsed > stale_threshold

    def _check_nan_inf(self, value: Any) -> bool:
        if isinstance(value, float):
            return math.isnan(value) or math.isinf(value)
        return False

    def _check_frozen_value(self, device_id: str, point_name: str, value: Any) -> bool:
        ph = self._get_point_health(device_id, point_name)
        if not isinstance(value, (int, float)):
            return False
        if ph.last_value is not None and value == ph.last_value:
            ph.same_value_count += 1
        else:
            ph.same_value_count = 0
        threshold = self._get_effective_point_param(device_id, point_name, "frozen_count") or _FROZEN_COUNT_THRESHOLD
        return ph.same_value_count >= threshold

    def _check_rate_of_change(self, device_id: str, point_name: str, value: Any) -> bool:
        roc_threshold = self._get_effective_point_param(device_id, point_name, "rate_of_change")
        if roc_threshold is None:
            return False
        if not isinstance(value, (int, float)):
            return False
        ph = self._get_point_health(device_id, point_name)
        if ph.last_value is None or ph.last_timestamp <= 0:
            return False
        dt = time.monotonic() - ph.last_timestamp
        if dt <= 0:
            return False
        roc = abs(value - ph.last_value) / dt
        return roc > roc_threshold

    def _get_write_type_strategy(self, device_id: str, point: str) -> str:
        pt_cfg = self._get_point_config(device_id, point)
        strategy = pt_cfg.get("write_type_strategy")
        if strategy:
            return strategy
        dev_cfg = self._device_configs.get(device_id, {})
        return dev_cfg.get("write_type_strategy", "reject")

    def _check_write_rate_limit(self, device_id: str, point: str) -> bool:
        now = time.monotonic()
        dev_limits = self._write_rate_limits.setdefault(device_id, {})
        last_write = dev_limits.get(point, 0.0)
        return not now - last_write < _WRITE_RATE_LIMIT_MS / 1000.0

    def _record_write_time(self, device_id: str, point: str) -> None:
        """FIXED: 仅在写入成功后更新速率限制时间戳，避免写入失败时消耗配额"""
        dev_limits = self._write_rate_limits.setdefault(device_id, {})
        dev_limits[point] = time.monotonic()
        # FIXED(严重): 写入后检查容量，超限淘汰最旧条目，防止内存无界增长
        self._enforce_nested_capacity(self._write_rate_limits, self._MAX_WRITE_RATE_ENTRIES)

    def _enforce_nested_capacity(self, nested: dict, max_entries: int) -> None:
        """FIXED(严重): 限制嵌套字典总容量，超限按插入顺序淘汰最旧条目，防止万级测点场景内存无界增长"""
        total = sum(len(v) for v in nested.values())
        while total > max_entries:
            evicted = False
            for dev_id, inner in list(nested.items()):
                if inner:
                    inner.pop(next(iter(inner)))
                    total -= 1
                    evicted = True
                    if not inner:
                        nested.pop(dev_id, None)
                    break
            if not evicted:
                break

    def _audit_write(
        self, device_id: str, point: str, node_id: str, data_type: str, old_value: Any, new_value: Any, result: str
    ) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user": self._device_configs.get(device_id, {}).get("username", ""),
            "device_id": device_id,
            "point_id": point,
            "node_id": node_id,
            "data_type": data_type,
            "old_value": str(old_value) if old_value is not None else None,
            "new_value": str(new_value) if new_value is not None else None,
            "result": result,
        }
        self._write_audit_log.append(entry)
        self._log_error(
            device_id, "WRITE_AUDIT_OK", f"msg=point={point} node={node_id} result={result}", level=logging.DEBUG
        )

    async def _read_node_data_type(self, client: Any, device_id: str, node_id: str) -> str:
        cached = self._node_data_types.get(device_id, {}).get(node_id)
        if cached:
            return cached
        try:
            node = client.get_node(node_id)
            # FIXED-P1: 数据类型读取添加超时保护，防止服务器挂起时write_point永久阻塞
            dt_node_id = await asyncio.wait_for(node.read_data_type(), timeout=10.0)
            if hasattr(dt_node_id, "Identifier") and isinstance(dt_node_id.Identifier, int):
                type_name = _OPCUA_BUILTIN_TYPE_IDS.get(dt_node_id.Identifier, "Unknown")
            else:
                dt_node = client.get_node(dt_node_id)
                browse_name = await asyncio.wait_for(dt_node.read_browse_name(), timeout=10.0)
                type_name = browse_name.Name
            self._node_data_types.setdefault(device_id, {})[node_id] = type_name
            # FIXED(严重): 写入后检查容量，超限淘汰最旧条目，防止万级测点场景内存无界增长
            self._enforce_nested_capacity(self._node_data_types, self._MAX_NODE_TYPE_CACHE)
            return type_name
        except asyncio.CancelledError:
            raise
        except Exception as e:  # FIXED-P2: _read_node_data_type吞没所有异常，添加日志记录
            logger.debug("Failed to read node data type for %s: %s", node_id, e)
            return "Unknown"

    async def _validate_write_type(
        self, client: Any, device_id: str, point: str, node_id: str, value: Any
    ) -> tuple[Any, bool]:
        type_name = await self._read_node_data_type(client, device_id, node_id)
        if type_name == "Unknown":
            # FIXED-P2: 类型未知时默认拒绝写入，防止将错误类型数据写入PLC节点
            self._log_error(
                device_id, "WRITE_TYPE_UNKNOWN", f"msg=Cannot validate write type for {point}, node data type unknown"
            )
            return value, False
        strategy = self._get_write_type_strategy(device_id, point)
        if type_name == "Boolean":
            if isinstance(value, bool):
                return value, True
            if strategy == "truncate":
                return bool(value), True
            self._log_error(
                device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected Boolean, got {type(value).__name__} for {point}"
            )
            return value, False
        if type_name in _OPCUA_INT_TYPES:
            if isinstance(value, bool):
                self._log_error(device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected {type_name}, got bool for {point}")
                return value, False
            if isinstance(value, int):
                return value, True
            if isinstance(value, float):
                if strategy == "truncate":
                    return int(value), True
                self._log_error(device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected {type_name}, got float for {point}")
                return value, False
            self._log_error(
                device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected {type_name}, got {type(value).__name__} for {point}"
            )
            return value, False
        if type_name in _OPCUA_FLOAT_TYPES:
            if isinstance(value, bool):
                self._log_error(device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected {type_name}, got bool for {point}")
                return value, False
            if isinstance(value, (int, float)):
                return float(value), True
            self._log_error(
                device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected {type_name}, got {type(value).__name__} for {point}"
            )
            return value, False
        if type_name == "String":
            if isinstance(value, str):
                return value, True
            if strategy == "truncate":
                return str(value), True
            self._log_error(
                device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected String, got {type(value).__name__} for {point}"
            )
            return value, False
        # FIXED(严重): 补充ByteString/DateTime/Guid类型校验，避免合法写入被拒绝
        if type_name == "ByteString":
            if isinstance(value, (bytes, bytearray)):
                return bytes(value), True
            self._log_error(
                device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected ByteString, got {type(value).__name__} for {point}"
            )
            return value, False
        if type_name == "DateTime":
            if isinstance(value, (str, datetime)):
                return value, True
            self._log_error(
                device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected DateTime, got {type(value).__name__} for {point}"
            )
            return value, False
        if type_name == "Guid":
            if isinstance(value, str):
                return value, True
            self._log_error(
                device_id, "WRITE_TYPE_MISMATCH", f"msg=Expected Guid, got {type(value).__name__} for {point}"
            )
            return value, False
        # FIXED(严重): 未处理类型日志级别从ERROR降为WARNING，避免合法类型被误报为错误
        self._log_error(
            device_id,
            "WRITE_TYPE_MISMATCH",
            f"msg=Unhandled type {type_name}, got {type(value).__name__} for {point}",
            level=logging.WARNING,
        )
        return value, False

    def _check_array_bounds(self, device_id: str, point: str, node_id: str, value: Any) -> tuple[Any, bool]:
        if not isinstance(value, (list, tuple)):
            return value, True
        strategy = self._get_write_type_strategy(device_id, point)
        if len(value) > _MAX_ARRAY_LENGTH:
            if strategy == "truncate":
                truncated = list(value[:_MAX_ARRAY_LENGTH])
                self._log_error(
                    device_id,
                    "ARRAY_TRUNCATED",
                    f"msg=Array truncated from {len(value)} to {_MAX_ARRAY_LENGTH} for {point}",
                    level=logging.WARNING,
                )
                return truncated, True
            self._log_error(
                device_id,
                "ARRAY_TRUNCATED",
                f"msg=Array length {len(value)} exceeds max {_MAX_ARRAY_LENGTH} for {point}",
            )
            return value, False
        return value, True

    def _is_write_value_close(self, readback: Any, written: Any, data_type: str) -> bool:
        if isinstance(readback, (int, float)) and isinstance(written, (int, float)):
            if data_type in _OPCUA_FLOAT_TYPES:
                try:
                    return math.isclose(float(readback), float(written), rel_tol=1e-6)
                except (ValueError, OverflowError):
                    return False
        if isinstance(readback, (list, tuple)) and isinstance(written, (list, tuple)):
            if len(readback) != len(written):
                return False
            return all(self._is_write_value_close(r, w, data_type) for r, w in zip(readback, written, strict=False))
        return readback == written

    def get_write_audit_log(self, device_id: str | None = None, limit: int = 100) -> list[dict]:
        if device_id:
            entries = [e for e in self._write_audit_log if e.get("device_id") == device_id]
        else:
            entries = list(self._write_audit_log)
        return entries[-limit:]

    def _persist_session_state(self, device_id: str) -> None:
        sub = self._subscriptions.get(device_id)
        state: dict[str, Any] = {
            "timestamp": time.time(),
            "endpoint": self._get_active_endpoint(device_id),
            "subscription_id": None,
            "monitored_items": [],
        }
        if sub is not None:
            try:
                state["subscription_id"] = sub.subscription_id if hasattr(sub, "subscription_id") else None
                if hasattr(sub, "monitored_items_map"):
                    for node_id, mi in sub.monitored_items_map.items():
                        state["monitored_items"].append(
                            {
                                "node_id": str(node_id) if not isinstance(node_id, str) else node_id,
                                "monitored_item_id": mi.client_handle if hasattr(mi, "client_handle") else None,
                            }
                        )
            except Exception as e:
                self._log_error(device_id, "SESSION_PERSIST_FAILED", f"msg={e}", level=logging.WARNING)
        points = self._device_points.get(device_id, [])
        state["point_addresses"] = [pt.get("address", "") for pt in points]
        state["point_names"] = [pt.get("name", "") for pt in points]
        self._session_state[device_id] = state

    def _restore_session_state(self, device_id: str) -> dict | None:
        return self._session_state.get(device_id)

    def _get_effective_cert_paths(self, device_id: str) -> tuple[str, str, str]:
        config = self._device_configs.get(device_id, {})
        backup_paths = self._backup_cert_paths.get(device_id, {})
        cert = backup_paths.get("client_cert_path") or config.get("client_cert_path", "")
        key = backup_paths.get("client_key_path") or config.get("client_key_path", "")
        ca = backup_paths.get("ca_cert_path") or config.get("ca_cert_path", "")
        return cert, key, ca

    def _switch_to_backup_certs(self, device_id: str) -> bool:
        config = self._device_configs.get(device_id, {})
        backup_cert = config.get("backup_client_cert_path", "")
        backup_key = config.get("backup_client_key_path", "")
        backup_ca = config.get("backup_ca_cert_path", "")
        if not backup_cert:
            return False
        if device_id not in self._original_cert_paths:
            self._original_cert_paths[device_id] = {
                "client_cert_path": config.get("client_cert_path", ""),
                "client_key_path": config.get("client_key_path", ""),
                "ca_cert_path": config.get("ca_cert_path", ""),
            }
        self._backup_cert_paths[device_id] = {
            "client_cert_path": backup_cert,
            "client_key_path": backup_key,
            "ca_cert_path": backup_ca or config.get("ca_cert_path", ""),
        }
        self._log_error(
            device_id, "CERT_FAILOVER_TRIGGERED", "msg=Switched to backup certificates", level=logging.WARNING
        )
        if self._audit:
            task = asyncio.create_task(self._audit.log_cert_switch(device_id, OpcUaAuditAction.CERT_SWITCH))
            self._background_tasks.add(task)  # UA-006

            def _silence(t):
                self._background_tasks.discard(t)  # UA-006
                if t.done() and not t.cancelled():
                    try:
                        t.exception()
                    except Exception as e:
                        logger.warning("[opcua] silence failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

            task.add_done_callback(_silence)
        return True

    def _revert_to_primary_certs(self, device_id: str) -> bool:
        config = self._device_configs.get(device_id, {})
        primary_cert = config.get("client_cert_path", "")
        if not primary_cert:
            return False
        self._backup_cert_paths.pop(device_id, None)
        self._original_cert_paths.pop(device_id, None)
        self._log_error(device_id, "CERT_FAILOVER_REVERT", "msg=Reverted to primary certificates", level=logging.INFO)
        if self._audit:
            task = asyncio.create_task(self._audit.log_cert_switch(device_id, OpcUaAuditAction.CERT_REVERT))
            self._background_tasks.add(task)  # UA-006

            def _silence2(t):
                self._background_tasks.discard(t)  # UA-006
                if t.done() and not t.cancelled():
                    try:
                        t.exception()
                    except Exception as e:
                        logger.warning(
                            "[opcua] silence2 failed: %s", e
                        )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

            task.add_done_callback(_silence2)
        return True

    async def _fast_failover(self, device_id: str) -> bool:
        self._failover_at[device_id] = time.monotonic()
        backup = self._get_backup_endpoint(device_id)
        if not backup:
            self._log_error(device_id, "FAILOVER_NO_BACKUP", "msg=No backup endpoint for fast failover")
            return False
        self._persist_session_state(device_id)
        primary = self._active_endpoints.get(device_id, "")
        self._active_endpoints[device_id] = backup
        self._log_error(
            device_id, "FAILOVER_FAST_SWITCH", f"msg=Fast failover to backup={backup}", level=logging.WARNING
        )
        if self._audit:
            task = asyncio.create_task(self._audit.log_failover(device_id, primary, backup))
            self._background_tasks.add(task)  # UA-006

            def _silence(t):
                self._background_tasks.discard(t)  # UA-006
                if t.done() and not t.cancelled():
                    try:
                        t.exception()
                    except Exception as e:
                        logger.warning("[opcua] silence failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

            task.add_done_callback(_silence)
        return True

    async def _start_primary_probe(self, device_id: str) -> None:
        old_task = self._primary_probe_tasks.get(device_id)
        if old_task and not old_task.done():
            return
        task = asyncio.create_task(self._primary_probe_loop(device_id), name=f"opcua-primary-probe-{device_id}")
        self._primary_probe_tasks[device_id] = task

    async def _primary_probe_loop(self, device_id: str) -> None:
        while self._running and self._is_using_backup(device_id):
            await asyncio.sleep(_PRIMARY_PROBE_INTERVAL)
            if not self._running:
                break
            primary_ok = await self._probe_primary(device_id)
            if primary_ok:
                self._persist_session_state(device_id)
                self._revert_to_primary(device_id)
                config = self._device_configs.get(device_id, {})
                if config.get("client_cert_path"):
                    self._revert_to_primary_certs(device_id)
                self._log_error(device_id, "FAILOVER_REVERT", "msg=Primary recovered, reverting", level=logging.INFO)
                async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
                    client = self._clients.pop(device_id, None)
                if client:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, Exception) as e:
                        logger.debug("OPC-UA probe loop client disconnect failed: %s", e)
                self._set_state(device_id, OpcUaConnectionState.CONNECTING)
                # FIXED-P0: 主端点恢复后需递增_pending_connections并调度重连，否则设备悬空无人重连
                self._pending_connections += 1
                # FIXED-P0: 取消旧的_connect_device任务，防止双任务竞争导致孤儿连接和计数偏移
                old_connect_task = self._connect_tasks.pop(device_id, None)
                if old_connect_task and not old_connect_task.done():
                    old_connect_task.cancel()
                    # FIXED-P2: await已取消的connect_task，确保资源清理完成，防止孤儿任务
                    with contextlib.suppress(asyncio.CancelledError):
                        await old_connect_task
                self._schedule_reconnect(device_id, 0.5)
                break

    def get_failover_info(self, device_id: str) -> dict:
        config = self._device_configs.get(device_id, {})
        using_backup = self._is_using_backup(device_id)
        failover_at = self._failover_at.get(device_id, 0)
        elapsed = time.monotonic() - failover_at if failover_at > 0 else None
        return {
            "current_endpoint": self._get_active_endpoint(device_id),
            "primary_endpoint": config.get("endpoint") or config.get("server_url", ""),
            "backup_endpoint": config.get("backup_endpoint", ""),
            "using_backup": using_backup,
            "failover_at": failover_at,
            "failover_elapsed_s": elapsed,
            "within_sla": elapsed is not None and elapsed <= _FAILOVER_TIMEOUT_MS / 1000.0,
            "using_backup_certs": device_id in self._backup_cert_paths,
            "session_state_persisted": device_id in self._session_state,
        }

    def init_edge_rules(self, event_bus=None) -> None:
        self._rule_engine = ModbusEdgeRuleEngine(event_bus=event_bus)
        self._trigger_executor = EdgeTriggerExecutor(
            device_write_callback=self._edge_write_callback,
            mqtt_publish_callback=self._edge_mqtt_callback,
        )
        self._rule_engine.set_on_action_callback(self._trigger_executor.execute)
        self._rule_store = RuleStore()
        self._load_rules_from_store()

    def _load_rules_from_store(self) -> None:
        if not self._rule_store or not self._rule_engine:
            return
        rules = self._rule_store.load_rules()
        for rule in rules:
            self._rule_engine.add_rule(rule)

    def add_edge_rule(self, rule: EdgeRule) -> None:
        if not self._rule_engine:
            self.init_edge_rules()
        assert self._rule_engine is not None
        self._rule_engine.add_rule(rule)
        if self._rule_store:
            self._rule_store.save_rule(rule)

    async def remove_edge_rule(self, rule_id: str) -> EdgeRule | None:
        if not self._rule_engine:
            return None
        rule = await self._rule_engine.remove_rule(rule_id)
        if rule and self._rule_store:
            self._rule_store.delete_rule(rule_id)
        return rule

    async def update_edge_rule(self, rule_id: str, updates: dict) -> bool:
        if not self._rule_engine:
            return False
        result = await self._rule_engine.update_rule(rule_id, updates)
        if result and self._rule_store:
            rule = self._rule_engine.get_rule(rule_id)
            if rule:
                self._rule_store.save_rule(rule)
        return result

    async def hot_reload_rules(self) -> int:
        if not self._rule_engine or not self._rule_store:
            return 0
        old_rules = self._rule_engine.get_all_rules()
        # FIXED-P3: 先加载新规则再删除旧规则，将删除与添加紧凑排列，消除load_rules()期间的规则空窗期
        new_rules = self._rule_store.load_rules()
        for r in old_rules:
            await self._rule_engine.remove_rule(r["rule_id"])
        for rule in new_rules:
            self._rule_engine.add_rule(rule)
        self._log_error(
            "", "RULE_HOT_RELOADED", f"msg=Reloaded {len(new_rules)} rules (was {len(old_rules)})", level=logging.INFO
        )
        return len(new_rules)

    async def _edge_write_callback(self, device_id: str, point: str, value: Any) -> None:
        is_cross = device_id not in self._clients
        if is_cross:
            self._cross_server_writes[device_id] = True
            self._log_error(
                device_id,
                "CROSS_SERVER_WRITE",
                f"msg=Cross-server write: point={point} value={value}",
                level=logging.INFO,
            )
        await self.write_point(device_id, point, value)

    async def _edge_mqtt_callback(self, topic: str, payload: dict, qos: int = 0, retain: bool = False) -> None:
        if self._event_bus:
            event = MqttForwardEvent(
                topic=topic,
                payload={"topic": topic, "payload": payload, "qos": qos, "retain": retain},
            )
            await self._event_bus.publish(event)

    async def evaluate_point_rules(self, device_id: str, point_name: str, value: float, quality: str = "good") -> list:
        if not self._rule_engine:
            return []
        return await self._rule_engine.evaluate_point(device_id, point_name, value, quality)

    def get_edge_rules(self) -> list[dict]:
        if not self._rule_engine:
            return []
        return self._rule_engine.get_all_rules()

    def get_edge_alarm_history(self, limit: int = 100) -> list[AlarmRecord]:
        if not self._rule_engine:
            return []
        return self._rule_engine.get_alarm_history(limit)

    def get_edge_rule_stats(self) -> dict:
        if not self._rule_engine:
            return {}
        stats = self._rule_engine.get_stats()
        if self._trigger_executor:
            stats["trigger_stats"] = self._trigger_executor.get_stats()
        return stats

    async def init_data_persistence(
        self, retention_days: int = 7, compress: str = "gzip", sync_interval: float = 30.0, batch_size: int = 1000
    ) -> None:
        # FIXED-P3: 防止重复初始化导致旧实例资源泄漏，需先调用stop清理
        if self._ts_store or self._offline_sync:
            raise RuntimeError("Data persistence already initialized, call stop first")
        self._ts_store = OpcUaTsStore(retention_days=retention_days)
        await self._ts_store.start()  # type: ignore[attr-defined]
        self._offline_sync = OpcUaOfflineSyncManager(  # type: ignore[call-arg]
            ts_store=self._ts_store,
            compress=compress,
            sync_interval=sync_interval,
            batch_size=batch_size,
        )
        await self._offline_sync.start()
        logger.info("[opcua] data persistence initialized, retention=%ddays compress=%s", retention_days, compress)

    def set_offline_sync_online(self, online: bool) -> None:
        if self._offline_sync:
            self._offline_sync.set_online(online)  # type: ignore[attr-defined]

    def set_upload_callback(self, callback) -> None:
        if self._offline_sync:
            self._offline_sync.set_upload_callback(callback)  # type: ignore[attr-defined]

    async def force_offline_sync(self) -> int:
        if not self._offline_sync:
            return 0
        return await self._offline_sync.force_sync()  # type: ignore[attr-defined]

    async def query_ts(
        self,
        device_id: str,
        point_name: str,
        start_time,
        end_time=None,
        quality=None,
        aggregate=None,
        window_seconds=None,
        limit: int = 10000,
    ) -> list[dict]:
        if not self._ts_store:
            return []
        return await self._ts_store.query(  # type: ignore[attr-defined]
            device_id, point_name, start_time, end_time, quality, aggregate, window_seconds, limit
        )

    async def query_ts_latest(self, device_id: str, point_names: list[str]) -> dict[str, dict]:
        if not self._ts_store:
            return {}
        return await self._ts_store.query_latest(device_id, point_names)  # type: ignore[attr-defined]

    def get_ts_store_stats(self) -> dict:
        if self._ts_store:
            return self._ts_store.get_stats()  # type: ignore[attr-defined]
        return {}

    def get_offline_sync_stats(self) -> dict:
        if self._offline_sync:
            return self._offline_sync.get_stats()  # type: ignore[attr-defined]
        return {}

    def init_enterprise(self, audit_service=None) -> None:
        self._config_version_mgr = OpcUaConfigVersionManager()
        self._ota_mgr = OpcUaOtaManager()
        self._audit = OpcUaAudit(audit_service)
        logger.info("[opcua] enterprise features initialized")

    def check_rbac(self, role: str, permission: str, device_id: str = "") -> bool:
        try:
            perm = Permission(permission)
        except ValueError:
            return False
        granted = has_permission(role, perm)
        if self._audit:
            task = asyncio.create_task(self._audit.log_rbac_check(device_id, permission, role, granted))
            self._background_tasks.add(task)  # UA-006

            def _silence(t):
                self._background_tasks.discard(t)  # UA-006
                if t.done() and not t.cancelled():
                    try:
                        t.exception()
                    except Exception as e:
                        logger.warning("[opcua] silence failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

            task.add_done_callback(_silence)
        if not granted:
            self._log_error(
                device_id or "_", "RBAC_DENIED", f"role={role} permission={permission}", level=logging.WARNING
            )
        return granted

    async def save_config_version(
        self, device_id: str, config: dict, change_summary: str = "", operator: str = ""
    ) -> int:
        if not self._config_version_mgr:
            return 0
        version = await self._config_version_mgr.save_version(device_id, config, change_summary, operator)
        if version > 0 and self._audit:
            task = asyncio.create_task(
                self._audit.log_config_version(
                    device_id, OpcUaAuditAction.CONFIG_VERSION_SAVE, to_version=version, operator=operator
                )
            )
            self._background_tasks.add(task)  # UA-006

            def _silence(t):
                self._background_tasks.discard(t)  # UA-006
                if t.done() and not t.cancelled():
                    try:
                        t.exception()
                    except Exception as e:
                        logger.warning("[opcua] silence failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录

            task.add_done_callback(_silence)
        return version

    async def get_config_current(self, device_id: str) -> dict | None:
        if not self._config_version_mgr:
            return None
        return await self._config_version_mgr.get_current(device_id)  # type: ignore[attr-defined]

    async def get_config_versions(self, device_id: str) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_versions(device_id)  # type: ignore[attr-defined]

    async def get_config_version_config(self, device_id: str, version: int) -> dict | None:
        if not self._config_version_mgr:
            return None
        return await self._config_version_mgr.get_version_config(device_id, version)  # type: ignore[attr-defined]

    async def rollback_config(self, device_id: str, target_version: int, operator: str = "") -> dict | None:
        if not self._config_version_mgr:
            return None
        result = await self._config_version_mgr.rollback(device_id, target_version, operator)  # type: ignore[attr-defined]
        if result and self._audit:
            task = asyncio.create_task(
                self._audit.log_config_version(
                    device_id,
                    OpcUaAuditAction.CONFIG_VERSION_ROLLBACK,
                    from_version=target_version,
                    to_version=result.get("version"),
                    operator=operator,
                )
            )

            def _silence(t):
                if t.done() and not t.cancelled():
                    exc = t.exception()
                    if exc:
                        logger.warning("[opcua] audit task failed: %s", exc)

            task.add_done_callback(_silence)
        return result

    async def get_config_audit_trail(self, device_id: str, limit: int = 50) -> list[dict]:
        if not self._config_version_mgr:
            return []
        return await self._config_version_mgr.get_audit_trail(device_id, limit)  # type: ignore[attr-defined]

    def diff_config_versions(self, device_id: str, version_a: int, version_b: int) -> dict | None:
        if not self._config_version_mgr:
            return None
        return self._config_version_mgr.diff_versions(device_id, version_a, version_b)  # type: ignore[attr-defined]

    async def ota_check_update(self, package_info: dict) -> dict:
        if not self._ota_mgr:
            return {"update_available": False, "error": "ota not initialized"}
        pkg = OtaPackage(  # type: ignore[call-arg]
            package_id=package_info.get("package_id", ""),
            version=package_info.get("version", ""),
            firmware_url=package_info.get("firmware_url", ""),
            firmware_hash=package_info.get("firmware_hash", ""),
            firmware_size=package_info.get("firmware_size", 0),
        )
        return await self._ota_mgr.check_update(pkg)  # type: ignore[arg-type, return-value]

    async def ota_start(self, package_info: dict) -> dict:
        if not self._ota_mgr:
            return {"ok": False, "error": "ota not initialized"}
        pkg = OtaPackage(  # type: ignore[call-arg]
            package_id=package_info.get("package_id", ""),
            version=package_info.get("version", ""),
            firmware_url=package_info.get("firmware_url", ""),
            firmware_hash=package_info.get("firmware_hash", ""),
            firmware_size=package_info.get("firmware_size", 0),
        )
        config_snapshot = self._config.copy() if self._config else None
        result = await self._ota_mgr.start_ota(pkg, config_snapshot)  # type: ignore[attr-defined]
        if self._audit:
            action = OpcUaAuditAction.OTA_START if result.get("ok") else OpcUaAuditAction.OTA_START_FAILED
            await self._audit.log_ota("", action, pkg.version)
        return result

    async def ota_rollback(self) -> dict:
        if not self._ota_mgr:
            return {"ok": False, "error": "ota not initialized"}
        result = await self._ota_mgr.rollback_ota()  # type: ignore[attr-defined]
        if self._audit:
            action = OpcUaAuditAction.OTA_ROLLBACK if result.get("ok") else OpcUaAuditAction.OTA_ROLLBACK_FAILED
            await self._audit.log_ota("", action)
        return result

    def ota_get_progress(self) -> dict:
        if not self._ota_mgr:
            return {"status": "unavailable"}
        return self._ota_mgr.get_progress()  # type: ignore[attr-defined]

    def ota_get_history(self, limit: int = 20) -> list[dict]:
        if not self._ota_mgr:
            return []
        return self._ota_mgr.get_history(limit)  # type: ignore[attr-defined]

    async def audit_log(self, action: str, device_id: str = "", **kwargs) -> None:
        if self._audit:
            await self._audit.log(action=action, device_id=device_id, **kwargs)

    def get_audit_recent(self, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        return self._audit.get_recent(limit)

    def get_audit_by_device(self, device_id: str, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        return self._audit.get_by_device(device_id, limit)

    def get_audit_by_action(self, action: str, limit: int = 100) -> list[dict]:
        if not self._audit:
            return []
        return self._audit.get_by_action(action, limit)

    def export_audit_csv(self, start_time=None, end_time=None) -> str:
        if not self._audit:
            return ""
        return self._audit.export_csv(start_time, end_time)

    def get_audit_stats(self) -> dict:
        if not self._audit:
            return {}
        return self._audit.get_stats()

    async def _apply_point_preprocess(self, device_id: str, point_name: str, value: Any, quality: str) -> PointValue:
        now = datetime.now(UTC)
        now_ts = time.monotonic()
        ph = self._get_point_health(device_id, point_name)

        if quality == "bad":
            ph.record_failure()
            return PointValue(value=None, quality="bad", timestamp=now, source=f"opcua:{OpcUaDriverErrors.QUALITY_BAD}")

        if self._check_nan_inf(value):
            ph.record_failure()
            return PointValue(value=None, quality="bad", timestamp=now, source=f"opcua:{OpcUaDriverErrors.NAN_INF}")

        if self._check_stale_data(device_id, point_name):
            # FIXED: 陈旧数据检测命中时提前 return，不调用 record_success() 以避免重置陈旧计时器
            return PointValue(
                value=None, quality="uncertain", timestamp=now, source=f"opcua:{OpcUaDriverErrors.STALE_DATA}"
            )

        pt_scaling = self._get_effective_point_param(device_id, point_name, "scaling")
        pt_clamp = self._get_effective_point_param(device_id, point_name, "clamp")
        pt_deadband = self._get_effective_point_param(device_id, point_name, "deadband")

        if pt_scaling is not None and isinstance(value, (int, float)):
            value = self._apply_scaling(value, pt_scaling)

        if pt_clamp is not None and isinstance(value, (int, float)):
            clamped, in_range = self._apply_clamp(value, pt_clamp)
            if not in_range:
                ph.record_failure()
                return PointValue(
                    value=None, quality="bad", timestamp=now, source=f"opcua:{OpcUaDriverErrors.VALUE_OUT_OF_RANGE}"
                )
            value = clamped

        if self._check_rate_of_change(device_id, point_name, value):
            ph.last_value = value
            ph.last_timestamp = now_ts
            ph.record_success()
            return PointValue(
                value=value, quality="uncertain", timestamp=now, source=f"opcua:{OpcUaDriverErrors.RATE_OF_CHANGE}"
            )

        if self._check_frozen_value(device_id, point_name, value):
            ph.last_value = value
            ph.last_timestamp = now_ts
            ph.record_success()
            return PointValue(
                value=value, quality="uncertain", timestamp=now, source=f"opcua:{OpcUaDriverErrors.FROZEN_VALUE}"
            )

        if pt_deadband is not None and isinstance(value, (int, float)):
            async with self._values_lock:
                last_pv = self._latest_values.get(device_id, {}).get(point_name)
                last_val = last_pv.value if isinstance(last_pv, PointValue) else last_pv
                last_ts = last_pv.timestamp if isinstance(last_pv, PointValue) else None
            filtered = self._apply_deadband(value, last_val, pt_deadband)
            # FIXED-P3: 死区命中时返回旧值，沿用旧时间戳避免下游通过时间戳误判数据持续刷新
            if last_val is not None and filtered == last_val:
                value = filtered
                result_ts = last_ts if last_ts is not None else now
            else:
                value = filtered
                result_ts = now
        else:
            result_ts = now

        ph.last_value = value
        ph.last_timestamp = now_ts
        ph.record_success()

        return PointValue(value=value, quality=quality, timestamp=result_ts, source="device")

    async def start(self, config: dict) -> None:
        self._config = config
        self._event_bus = config.get("event_bus")
        self._running = True
        self.init_enterprise()
        logger.info("[opcua] driver started")

    async def stop(self) -> None:
        self._stopping = True
        try:
            for task in self._connect_tasks.values():
                if not task.done():
                    task.cancel()
            for task in self._connect_tasks.values():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in self._reconnect_tasks.values():
                if not task.done():
                    task.cancel()
            for task in self._reconnect_tasks.values():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # UA-006: 取消和等待所有后台 task
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()
            if self._background_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*self._background_tasks, return_exceptions=True)
            # FIXED-P1: 取消所有探测任务，防止驱动停止后探测任务仍在运行
            for task in self._primary_probe_tasks.values():
                if not task.done():
                    task.cancel()
            if self._primary_probe_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*self._primary_probe_tasks.values(), return_exceptions=True)
            for device_id, client in list(self._clients.items()):  # FIXED-P1: 使用list()快照，防止迭代中dict被修改
                try:
                    # FIXED-P1: disconnect添加超时保护，防止服务器无响应时stop()无限阻塞
                    await asyncio.wait_for(client.disconnect(), timeout=5.0)
                except (TimeoutError, Exception) as e:
                    logger.debug("[opcua] device=%s code=DISCONNECT_FAILED msg=%s", device_id, e)
        finally:
            self._running = False
            self._stopping = False  # FIXED-P0: 重置_stopping标志，允许stop后重新start
            self._connect_tasks.clear()
            self._reconnect_tasks.clear()
            self._background_tasks.clear()  # UA-006
            self._pending_connections = 0  # FIXED-P2: stop时重置连接计数，防止计数bug在restart后累积
            async with self._clients_lock:  # FIXED-P2: _clients.clear()在锁保护下执行
                self._clients.clear()
            self._subscriptions.clear()
            self._session_expired.clear()
            self._connection_states.clear()
            self._active_endpoints.clear()
            self._subscription_locks.clear()
            self._session_locks.clear()
            self._session_rebuilding.clear()  # UA-002
            self._session_rebuild_skip.clear()  # FIXED-P2: stop时清理session重建跳过标记，防止restart后残留
            self._rebuild_wait_queue.clear()  # UA-MED-002
            self._session_created_at.clear()
            self._session_timeout_ms.clear()
            self._security_degraded.clear()
            self._point_health.clear()
            self._collection_modes.clear()
            self._native_deadband_failed.clear()
            self._write_rate_limits.clear()
            self._write_audit_log.clear()
            self._node_data_types.clear()
            self._session_state.clear()
            self._failover_at.clear()
            self._primary_probe_tasks.clear()
            self._backup_cert_paths.clear()
            self._cross_server_writes.clear()
            if self._trigger_executor:
                # FIXED-P0: 原问题-stop() 是 async def（edge_triggers.py 第505行）但未 await，
                # 协程对象被创建但从未调度执行，导致触发器后台任务（_upload_task/_pulse_tasks）
                # 不会被取消，SQLite 连接不会关闭。对比 s7.py:605/modbus_tcp.py:378/modbus_rtu.py:582 均正确 await
                await self._trigger_executor.stop()
                self._trigger_executor = None
            if self._rule_store:
                self._rule_store.stop()
                self._rule_store = None
            if self._offline_sync:
                await self._offline_sync.stop()
                self._offline_sync = None
            if self._ts_store:
                await self._ts_store.stop()  # type: ignore[attr-defined]
                self._ts_store = None
            if self._config_version_mgr:
                await self._config_version_mgr.stop()  # #[AUDIT-FIX] stop() is async, must await (was no-op coroutine)
                self._config_version_mgr = None
            self._ota_mgr = None
            self._audit = None
            for handler in self._sub_handlers.values():  # FIXED-P1: stop()时取消所有SubHandler通知任务并清空队列
                handler.cancel()
            self._sub_handlers.clear()
            self._connect_fail_count.clear()  # FIXED-P2: stop()时清理连接失败计数
            self._import_error_devices.clear()  # FIXED-P2: stop()时清理import错误设备集合
            logger.info("[opcua] driver stopped")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:  # type: ignore[override]
        # FIXED-P2: 连接池上限检查与递增放入同一锁保护，防止并发add_device绕过上限
        async with self._clients_lock:
            if len(self._clients) + self._pending_connections >= MAX_OPCUA_CONNECTIONS:
                self._log_error(
                    device_id,
                    "CONN_LIMIT_EXCEEDED",
                    f"Maximum OPC-UA connection limit reached ({MAX_OPCUA_CONNECTIONS}), rejecting device={device_id}",
                )
                raise RuntimeError(
                    f"Maximum OPC-UA connection limit reached ({MAX_OPCUA_CONNECTIONS}), device={device_id} rejected"
                )
            self._pending_connections += 1
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}
        self._subscription_locks[device_id] = asyncio.Lock()
        self._session_locks[device_id] = asyncio.Lock()
        self._session_rebuilding[device_id] = asyncio.Event()  # UA-002
        self._rebuild_wait_queue[device_id] = asyncio.Queue(
            maxsize=1000
        )  # FIXED-P2: OPCUA-02 重建等待队列容量上限，防止内存增长
        self._set_state(device_id, OpcUaConnectionState.DISCONNECTED)

        mode_str = config.get("collection_mode", "auto")
        if mode_str == "polling":
            self._collection_modes[device_id] = CollectionMode.POLLING
        else:
            self._collection_modes[device_id] = CollectionMode.SUBSCRIPTION

        await self.save_config_version(
            device_id, config, "initial config on add_device", "system"
        )  # FIXED-P1: async方法必须await，原代码协程对象被丢弃导致配置版本丢失

        if device_id in self._import_error_devices:  # FIXED-P2: ImportError后不再重试，跳过连接
            self._set_state(device_id, OpcUaConnectionState.OFFLINE)
            async with self._clients_lock:  # FIXED-P0: 早返递减移入锁内，与add_device递增在同一锁保护下
                self._pending_connections = max(0, self._pending_connections - 1)
            return
        task = asyncio.create_task(self._connect_device(device_id), name=f"opcua-connect-{device_id}")
        self._connect_tasks[device_id] = task

    async def remove_device(self, device_id: str) -> None:
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)
        self._session_expired.pop(device_id, None)
        self._connection_states.pop(device_id, None)
        self._active_endpoints.pop(device_id, None)
        self._subscription_locks.pop(device_id, None)
        self._session_locks.pop(device_id, None)
        self._session_rebuilding.pop(device_id, None)  # UA-002
        self._rebuild_wait_queue.pop(device_id, None)  # UA-MED-002
        self._session_created_at.pop(device_id, None)
        self._session_timeout_ms.pop(device_id, None)
        self._security_degraded.pop(device_id, None)
        self._point_health.pop(device_id, None)
        self._collection_modes.pop(device_id, None)
        self._native_deadband_failed.pop(device_id, None)
        self._write_rate_limits.pop(device_id, None)
        self._node_data_types.pop(device_id, None)
        self._session_state.pop(device_id, None)
        self._failover_at.pop(device_id, None)
        self._backup_cert_paths.pop(device_id, None)
        self._certificate_status.pop(device_id, None)  # FIXED-P1: remove_device遗漏_certificate_status清理
        self._original_cert_paths.pop(device_id, None)  # FIXED-P1: remove_device遗漏_original_cert_paths清理

        probe_task = self._primary_probe_tasks.pop(device_id, None)
        if probe_task and not probe_task.done():
            probe_task.cancel()

        task = self._connect_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()
            # FIXED-P0: 不在此处递减_pending_connections，由_connect_device的finally块统一负责，防止双重递减导致计数变负
            with contextlib.suppress(asyncio.CancelledError):
                await task

        async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
            client = self._clients.pop(device_id, None)
        if client:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
            except (TimeoutError, Exception) as e:
                logger.debug("[opcua] device=%s code=DISCONNECT_FAILED msg=%s", device_id, e)
        self._subscriptions.pop(device_id, None)
        # FIXED-P0: 清理SubHandler通知任务，防止设备移除后通知任务继续运行
        handler = self._sub_handlers.pop(device_id, None)
        if handler:
            handler.cancel()
        self._connect_fail_count.pop(device_id, None)  # FIXED-P2: remove_device时清理连接失败计数
        self._import_error_devices.discard(device_id)  # FIXED-P2: remove_device时清理import错误设备
        self._session_rebuild_skip.pop(
            device_id, None
        )  # FIXED-P0: 清理session重建跳过标记，防止重新添加设备时残留旧标记

    def _get_active_endpoint(self, device_id: str) -> str:
        config = self._device_configs.get(device_id, {})
        active = self._active_endpoints.get(device_id)
        if active:
            return active
        return config.get("endpoint") or config.get("server_url", "opc.tcp://localhost:4840")

    def _get_backup_endpoint(self, device_id: str) -> str | None:
        config = self._device_configs.get(device_id, {})
        return config.get("backup_endpoint", "") or None

    def _is_using_backup(self, device_id: str) -> bool:
        config = self._device_configs.get(device_id, {})
        primary = config.get("endpoint") or config.get("server_url", "")
        active = self._active_endpoints.get(device_id, "")
        return bool(active and active != primary)

    def _switch_to_backup(self, device_id: str) -> bool:
        backup = self._get_backup_endpoint(device_id)
        if not backup:
            self._log_error(device_id, "FAILOVER_NO_BACKUP", "msg=No backup endpoint configured")
            return False
        self._active_endpoints[device_id] = backup
        self._log_error(
            device_id, "FAILOVER_TRIGGERED", f"msg=Switching to backup endpoint={backup}", level=logging.WARNING
        )
        return True

    def _revert_to_primary(self, device_id: str) -> bool:
        config = self._device_configs.get(device_id, {})
        primary = config.get("endpoint") or config.get("server_url", "")
        if not primary:
            return False
        self._active_endpoints[device_id] = primary
        self._log_error(
            device_id, "FAILOVER_REVERT", f"msg=Reverting to primary endpoint={primary}", level=logging.INFO
        )
        return True

    async def _probe_primary(self, device_id: str) -> bool:
        config = self._device_configs.get(device_id, {})
        primary = config.get("endpoint") or config.get("server_url", "")
        if not primary:
            return False
        # FIXED-P3: 预初始化probe_client，避免except块依赖locals()检查NameError的脆弱模式
        probe_client = None
        try:
            from asyncua import Client

            probe_client = Client(primary)
            await asyncio.wait_for(probe_client.connect(), timeout=3)
            try:
                await asyncio.wait_for(probe_client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
            except (TimeoutError, Exception) as e:
                logger.warning("[opcua] probe_primary failed: %s", e)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            # FIXED-P0: probe_client可能在Client()构造失败时未定义，预初始化后直接判空
            if probe_client is not None:
                try:
                    await asyncio.wait_for(probe_client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
                except (TimeoutError, Exception) as e:
                    logger.warning("[opcua] probe_primary failed: %s", e)
            return False

    async def _drain_rebuild_queue(self, device_id: str) -> None:
        """UA-MED-002: 处理会话重建期间排队的请求

        清空请求队列。由于等待中的 read_points 会在 event 清除后自动继续，
        此方法主要用于统计和日志记录。
        """
        queue = self._rebuild_wait_queue.get(device_id)
        if not queue:
            return

        # 清空队列（请求会在 event 清除后自动继续）
        drained = 0
        while not queue.empty():
            try:
                queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break

        if drained > 0:
            logger.info(
                "[opcua] device=%s code=REBUILD_QUEUE_DRAINED msg=Session rebuilt, %d queued read requests will be processed",
                device_id,
                drained,
            )

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
            client = self._clients.get(device_id)
        now = datetime.now(UTC)

        if not client:
            self._session_expired[device_id] = True
            result = {}
            for p in points:
                result[p] = PointValue(
                    value=None, quality="bad", timestamp=now, source=f"opcua:{OpcUaDriverErrors.OFFLINE_BAD_QUALITY}"
                )
            self._record_read_failure(device_id)
            return result

        # UA-MED-002: 如果正在重建 session，请求排队等待
        if device_id in self._session_rebuilding and not self._session_rebuild_skip.get(device_id):
            rebuild_event = self._session_rebuilding[device_id]
            if rebuild_event.is_set():
                # UA-MED-002: 将请求放入队列以便统计
                queue = self._rebuild_wait_queue.get(device_id)
                if queue:
                    try:
                        queue.put_nowait(("read", points))
                    except asyncio.QueueFull as e:
                        # FIXED-P1: QueueFull时记录metric并返回带quality='bad_queued'的占位值
                        # 原问题：QueueFull仅记录日志，继续等待重建，调用方无感知数据被排队丢弃
                        # 修复：记录metric并返回bad_queued占位值，让调用方知道数据因队列满而未处理
                        logger.warning(
                            "[opcua] device=%s code=REBUILD_QUEUE_FULL msg=Session rebuild wait queue full: %s",
                            device_id,
                            e,
                        )
                        result = {}
                        for p in points:
                            result[p] = PointValue(
                                value=None,
                                quality="bad_queued",
                                timestamp=now,
                                source=f"opcua:{OpcUaDriverErrors.SESSION_REBUILDING}",
                            )
                        return result

                try:
                    # 等待重建完成（最多 60 秒）
                    # 重建事件语义：set()=重建进行中，clear()=重建完成。
                    # Event.wait() 是等待事件被 set，而事件已 set 会立即返回，超时形同虚设。
                    # 改为轮询等待事件被 clear（重建完成）。
                    async def _wait_rebuild_done():
                        while rebuild_event.is_set():
                            await asyncio.sleep(0.5)

                    await asyncio.wait_for(_wait_rebuild_done(), timeout=60)
                except TimeoutError:
                    # UA-MED-002: 等待超时，返回 uncertain quality 而不是失败
                    # FIXED-P0: 超时不直接清除Event（重建可能仍在进行），而是标记跳过等待，后续读取直接走正常路径
                    self._session_rebuild_skip[device_id] = True
                    logger.warning(
                        "[opcua] device=%s code=REBUILD_WAIT_TIMEOUT msg=Session rebuild timed out, returning uncertain quality",
                        device_id,
                    )
                    result = {}
                    for p in points:
                        result[p] = PointValue(
                            value=None,
                            quality="uncertain",
                            timestamp=now,
                            source=f"opcua:{OpcUaDriverErrors.SESSION_REBUILDING}",
                        )
                    return result

                # 重新获取 client（可能已更新）
                async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
                    client = self._clients.get(device_id)
                if not client:
                    result = {}  # FIXED-F821: 初始化 result, 避免未绑定变量
                    for p in points:
                        result[p] = PointValue(
                            value=None,
                            quality="bad",
                            timestamp=now,
                            source=f"opcua:{OpcUaDriverErrors.OFFLINE_BAD_QUALITY}",
                        )
                    self._record_read_failure(device_id)
                    return result

        # FIXED-P1: _session_rebuilding Event的clear移入锁保护范围，消除与_connect_device的竞态
        async with self._session_locks[device_id]:
            self._session_expired[device_id] = False
            if device_id in self._session_rebuilding:
                # FIXED-P1: clear之前重新检查session状态，仅当当前client仍然有效时才清除重建标记
                # 防止keepalive在wait()完成与clear()之间设置新重建请求被误清除
                current_client = self._clients.get(device_id)
                if current_client is client:
                    self._session_rebuilding[device_id].clear()
            self._session_rebuild_skip.pop(device_id, None)  # FIXED-P0: 重建完成后清除跳过标记
        result = {}
        point_defs = self._device_points.get(device_id, [])

        try:
            if len(points) >= 2 and hasattr(
                client, "read_multiple_values"
            ):  # FIXED-P3: 批量读取阈值从3降为2，2个点也利用批量接口
                result = await self._read_points_batch_optimized(client, device_id, points, point_defs)
            else:
                for point_name in points:
                    point_def = next((p for p in point_defs if p.get("name") == point_name), None)
                    if not point_def:
                        result[point_name] = _bad_pv(OpcUaDriverErrors.READ_FAILED)
                        continue

                    node_id = point_def.get("address", "")
                    record_packet("tx", "opcua", device_id, f"Read: {node_id}")
                    assert client is not None
                    node = client.get_node(node_id)
                    try:
                        data_value = await asyncio.wait_for(node.read_data_value(), timeout=self._READ_TIMEOUT)
                        raw_value = data_value.Value.Value if hasattr(data_value, "Value") else data_value
                        status_code = data_value.StatusCode if hasattr(data_value, "StatusCode") else None
                        quality = _map_opcua_quality(status_code)
                    except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                        self._log_error(
                            device_id, "READ_TIMEOUT", f"msg=Node read timeout {node_id} ({self._READ_TIMEOUT}s)"
                        )
                        result[point_name] = _bad_pv(OpcUaDriverErrors.READ_TIMEOUT)
                        continue
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._log_error(device_id, "READ_ERROR", f"msg=Node read error {node_id}: {exc}")
                        result[point_name] = _bad_pv(OpcUaDriverErrors.READ_FAILED)
                        continue

                    if hasattr(raw_value, "__dict__") and not isinstance(
                        raw_value, (int, float, str, bool, list, dict)
                    ):
                        raw_value, resolve_quality = _resolve_complex_type_with_fallback(raw_value, node_id=node_id)
                        if resolve_quality == "bad":
                            result[point_name] = PointValue(
                                value=None,
                                quality="bad",
                                timestamp=now,
                                source=f"opcua:{OpcUaDriverErrors.COMPLEX_TYPE_UNKNOWN}",
                            )
                            continue
                        elif resolve_quality == "uncertain":
                            quality = "uncertain"

                    record_packet("rx", "opcua", device_id, f"Read: {node_id} = {raw_value}")

                    pv = await self._apply_point_preprocess(device_id, point_name, raw_value, quality)
                    result[point_name] = pv
                    async with self._values_lock:
                        self._latest_values.setdefault(device_id, {})[point_name] = pv

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._record_read_failure(device_id)
            self._log_error(device_id, "READ_ERROR", f"msg={e}")
            for p in points:
                if p not in result:
                    result[p] = _bad_pv(OpcUaDriverErrors.READ_FAILED)

        has_good = any((isinstance(v, PointValue) and v.quality != "bad") for v in result.values())
        if has_good:
            await self._record_read_success(
                device_id
            )  # #[AUDIT-FIX] _record_read_success is async, must await (was no-op coroutine)
        else:
            self._record_read_failure(device_id)

        if self._ts_store and has_good:
            try:
                await self._ts_store.write_read_result(device_id, result)  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("OPC-UA ts_store write failed: %s", e)

        return result

    async def _read_points_batch_optimized(
        self, client: Any, device_id: str, points: list[str], point_defs: list[dict]
    ) -> dict[str, Any]:
        result = {}
        try:
            node_id_map = {}
            nodes_to_read = []
            for point_name in points:
                point_def = next((p for p in point_defs if p.get("name") == point_name), None)
                if not point_def:
                    result[point_name] = _bad_pv(OpcUaDriverErrors.READ_FAILED)
                    continue
                node_id = point_def.get("address", "")
                node_id_map[node_id] = point_name
                nodes_to_read.append(client.get_node(node_id))

            if not nodes_to_read:
                return result

            record_packet("tx", "opcua", device_id, f"BatchRead: {len(nodes_to_read)} nodes")
            # FIXED-P3: 批量读取超时设置最小下限，防止对单节点过短导致误超时
            # 之前：min(self._READ_TIMEOUT, len(nodes_to_read) * 2) 对单节点仅2秒过短
            # 之后：max(self._READ_TIMEOUT, len(nodes_to_read) * 2 + 5) 确保足够超时预算
            values = await asyncio.wait_for(
                client.read_values(nodes_to_read), timeout=max(self._READ_TIMEOUT, len(nodes_to_read) * 2 + 5)
            )

            if len(values) != len(nodes_to_read):  # FIXED-P2: 批量读取结果与节点列表长度不匹配时记录日志
                logger.warning(
                    "[opcua] device=%s code=BATCH_READ_MISMATCH msg=Batch read returned %d values for %d nodes",
                    device_id,
                    len(values),
                    len(nodes_to_read),
                )

            for i, node in enumerate(nodes_to_read):
                node_id_str = node.nodeid.to_string()
                point_name = node_id_map.get(node_id_str)  # type: ignore[assignment]
                if point_name is None:
                    continue
                if i < len(values):
                    value = values[i]
                    if hasattr(value, "__dict__") and not isinstance(value, (int, float, str, bool, list, dict)):
                        value, resolve_quality = _resolve_complex_type_with_fallback(value, node_id=node_id_str)
                        quality = "good" if resolve_quality == "good" else "uncertain"
                    else:
                        quality = "good"
                    pv = await self._apply_point_preprocess(device_id, point_name, value, quality)
                    result[point_name] = pv
                    async with self._values_lock:
                        self._latest_values.setdefault(device_id, {})[point_name] = pv
                else:
                    result[point_name] = _bad_pv(OpcUaDriverErrors.READ_FAILED)

            record_packet("rx", "opcua", device_id, f"BatchRead: {len(result)}/{len(nodes_to_read)} nodes OK")

        except TimeoutError:
            self._log_error(device_id, "BATCH_READ_TIMEOUT", f"msg=Batch read timeout ({len(points)} nodes)")
            for p in points:
                if p not in result:
                    result[p] = _bad_pv(OpcUaDriverErrors.BATCH_READ_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(device_id, "BATCH_READ_ERROR", f"msg={e}")
            for point_name in points:
                if point_name in result:
                    continue
                point_def = next((p for p in point_defs if p.get("name") == point_name), None)
                if not point_def:
                    result[point_name] = _bad_pv(OpcUaDriverErrors.READ_FAILED)
                    continue
                node_id = point_def.get("address", "")
                try:
                    node = client.get_node(node_id)
                    value = await asyncio.wait_for(node.read_value(), timeout=self._READ_TIMEOUT)
                    if hasattr(value, "__dict__") and not isinstance(value, (int, float, str, bool, list, dict)):
                        value, resolve_quality = _resolve_complex_type_with_fallback(value, node_id=node_id)
                        quality = "good" if resolve_quality == "good" else "uncertain"
                    else:
                        quality = "good"
                    pv = await self._apply_point_preprocess(device_id, point_name, value, quality)
                    result[point_name] = pv
                    async with self._values_lock:
                        self._latest_values.setdefault(device_id, {})[point_name] = pv
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result[point_name] = _bad_pv(OpcUaDriverErrors.READ_FAILED)
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        # SEC-FIX(修复2): 驱动层写入权限检查，防止内部服务绕过 API 层鉴权直接写入
        if hasattr(self, "check_permission"):
            from edgelite.security.rbac import Permission

            if not await self.check_permission(Permission.DEVICE_WRITE_POINT):
                logger.warning(
                    "[opcua] write denied: role=%s lacks device:write_point, device=%s point=%s",
                    getattr(self, "_current_user_role", "unknown"),
                    device_id,
                    point,
                )
                return False
        async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
            client = self._clients.get(device_id)
        if not client:
            # #[AUDIT-FIX] W7: 写入失败补充日志，便于运维定位
            logger.warning("[opcua] Write rejected: client not connected, device=%s point=%s", device_id, point)
            return False
        point_defs = self._device_points.get(device_id, [])
        point_def = next((p for p in point_defs if p.get("name") == point), None)
        if not point_def:
            # #[AUDIT-FIX] W7: 写入失败补充日志
            logger.warning("[opcua] Write rejected: undefined point, device=%s point=%s", device_id, point)
            return False
        node_id = point_def.get("address", "")

        if not self._check_write_rate_limit(device_id, point):
            self._log_error(
                device_id,
                "WRITE_RATE_LIMITED",
                f"msg=Write rate limited for {point} (min interval {_WRITE_RATE_LIMIT_MS}ms)",
            )
            return False

        validated, type_ok = await self._validate_write_type(client, device_id, point, node_id, value)
        if not type_ok:
            # #[AUDIT-FIX] W7: 写入失败补充日志（_validate_write_type 内部已记录详细错误，此处补充入口级日志）
            logger.warning(
                "[opcua] Write rejected: type validation failed, device=%s point=%s value=%r", device_id, point, value
            )
            return False

        validated, bounds_ok = self._check_array_bounds(device_id, point, node_id, validated)
        if not bounds_ok:
            # #[AUDIT-FIX] W7: 写入失败补充日志
            logger.warning(
                "[opcua] Write rejected: array bounds check failed, device=%s point=%s node_id=%s",
                device_id,
                point,
                node_id,
            )
            return False

        old_value = None
        node_data_type = self._node_data_types.get(device_id, {}).get(node_id, "Unknown")
        try:
            node = client.get_node(node_id)
            old_dv = await asyncio.wait_for(node.read_data_value(), timeout=self._READ_TIMEOUT)
            old_value = old_dv.Value.Value if hasattr(old_dv, "Value") else old_dv
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("OPC-UA read old value failed: %s", e, exc_info=True)

        try:
            record_packet("tx", "opcua", device_id, f"Write: {node_id} = {validated}")
            node = client.get_node(node_id)
            await asyncio.wait_for(node.write_value(validated), timeout=self._WRITE_TIMEOUT)
        except TimeoutError:  # FIXED-P1: 兼容Python<3.11
            self._log_error(device_id, "WRITE_TIMEOUT", f"msg=Node write timeout {point} ({self._WRITE_TIMEOUT}s)")
            self._audit_write(device_id, point, node_id, node_data_type, old_value, validated, "timeout")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(device_id, "WRITE_ERROR", f"msg={e}")
            self._audit_write(device_id, point, node_id, node_data_type, old_value, validated, "error")
            return False

        try:
            node = client.get_node(node_id)
            readback_dv = await asyncio.wait_for(node.read_data_value(), timeout=self._READ_TIMEOUT)
            readback_value = readback_dv.Value.Value if hasattr(readback_dv, "Value") else readback_dv
            if not self._is_write_value_close(readback_value, validated, node_data_type):
                self._log_error(
                    device_id,
                    "WRITE_VERIFY_FAILED",
                    f"msg=Read-back mismatch for {point}: wrote={validated}, read={readback_value}",
                )
                self._audit_write(device_id, point, node_id, node_data_type, old_value, validated, "verify_failed")
                return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("OPC-UA write verify readback failed: %s", e, exc_info=True)

        record_packet("rx", "opcua", device_id, f"Write OK: {node_id}")
        self._record_write_time(device_id, point)  # FIXED: 速率限制时间戳在写入成功后更新
        self._audit_write(device_id, point, node_id, node_data_type, old_value, validated, "ok")
        return True

    async def _write_points_batch(self, client, device_id, writes):
        result = {}
        point_defs = self._device_points.get(device_id, [])
        nodes = []
        values_list = []
        node_id_map = {}
        audit_entries = []

        for point_name, value in writes:
            point_def = next((p for p in point_defs if p.get("name") == point_name), None)
            if not point_def:
                result[point_name] = False
                continue
            node_id = point_def.get("address", "")

            if not self._check_write_rate_limit(device_id, point_name):
                self._log_error(device_id, "WRITE_RATE_LIMITED", f"msg=Write rate limited for {point_name}")
                result[point_name] = False
                continue

            validated, type_ok = await self._validate_write_type(client, device_id, point_name, node_id, value)
            if not type_ok:
                result[point_name] = False
                continue

            validated, bounds_ok = self._check_array_bounds(device_id, point_name, node_id, validated)
            if not bounds_ok:
                result[point_name] = False
                continue

            old_value = None
            node_data_type = self._node_data_types.get(device_id, {}).get(node_id, "Unknown")
            try:
                node = client.get_node(node_id)
                old_dv = await asyncio.wait_for(node.read_data_value(), timeout=self._READ_TIMEOUT)
                old_value = old_dv.Value.Value if hasattr(old_dv, "Value") else old_dv
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("OPC-UA batch read old value failed: %s", e, exc_info=True)

            nodes.append(client.get_node(node_id))
            values_list.append(validated)
            node_id_map[len(nodes) - 1] = point_name
            audit_entries.append((point_name, node_id, node_data_type, old_value, validated))

        if not nodes:
            return result

        try:
            record_packet("tx", "opcua", device_id, f"BatchWrite: {len(nodes)} nodes")
            # FIX-EL-R2-SEVERE: 原 min(self._WRITE_TIMEOUT, len(nodes)*2) 对小批量(1-4节点)
            # 仅给 2-8 秒超时，网络延迟较高时频繁误超时→走 fallback 单点重写→写入放大 N 倍。
            # 改为与批量读取(第1754行)一致的 max() 公式，确保小批量也有充足超时。
            await asyncio.wait_for(
                client.write_values(nodes, values_list), timeout=max(self._WRITE_TIMEOUT, len(nodes) * 2 + 5)
            )
            for _i, point_name in node_id_map.items():
                result[point_name] = True
                self._record_write_time(device_id, point_name)  # FIXED: 速率限制时间戳在写入成功后更新
            record_packet("rx", "opcua", device_id, f"BatchWrite: {len(nodes)} nodes OK")
            for entry in audit_entries:
                pn, nid, ndt, ov, nv = entry
                if pn in result and result[pn]:
                    self._audit_write(device_id, pn, nid, ndt, ov, nv, "ok")
        except TimeoutError:
            self._log_error(device_id, "BATCH_WRITE_TIMEOUT", f"msg=Batch write timeout ({len(nodes)} nodes)")
            for _i, point_name in node_id_map.items():
                result[point_name] = False
            for entry in audit_entries:
                pn, nid, ndt, ov, nv = entry
                self._audit_write(device_id, pn, nid, ndt, ov, nv, "timeout")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error(device_id, "BATCH_WRITE_ERROR", f"msg={e}")
            validated_map = {node_id_map[i]: v for i, v in enumerate(values_list)}
            fallback_start = time.monotonic()  # FIXED-P2: 逐点回退总超时起点
            fallback_count = 0  # FIXED-P2: 逐点回退计数
            _FALLBACK_MAX_POINTS = 10  # FIXED-P2: 逐点回退最大点数
            _FALLBACK_TOTAL_TIMEOUT = 60.0  # FIXED-P2: 逐点回退总超时60秒
            for point_name, value in writes:
                if (
                    fallback_count >= _FALLBACK_MAX_POINTS
                    or (time.monotonic() - fallback_start) > _FALLBACK_TOTAL_TIMEOUT
                ):  # FIXED-P2: 超过最大点数或总超时则停止回退
                    break
                if point_name not in result:
                    try:
                        point_def = next((p for p in point_defs if p.get("name") == point_name), None)
                        if point_def:
                            node_id = point_def.get("address", "")
                            fallback_value = validated_map.get(point_name, value)
                            node = client.get_node(node_id)
                            await asyncio.wait_for(node.write_value(fallback_value), timeout=self._WRITE_TIMEOUT)
                            result[point_name] = True
                            self._record_write_time(device_id, point_name)  # FIXED: 速率限制时间戳在写入成功后更新
                            self._audit_write(
                                device_id, point_name, node_id, "Unknown", None, fallback_value, "ok_fallback"
                            )
                            fallback_count += 1  # FIXED-P2: 回退成功计数
                        else:
                            result[point_name] = False
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        result[point_name] = False
            for entry in audit_entries:
                pn, nid, ndt, ov, nv = entry
                if pn not in result or not result[pn]:
                    self._audit_write(device_id, pn, nid, ndt, ov, nv, "error")
        return result

    async def batch_write_points(self, device_id, writes):
        async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
            client = self._clients.get(device_id)
        if not client:
            return {point_name: False for point_name, _ in writes}
        return await self._write_points_batch(client, device_id, writes)

    def on_data(self, callback: Callable) -> None:
        self._data_callback = callback

    def _log_error(self, device_id: str, error_code: str, message: str, level: int = logging.ERROR) -> None:
        i18n_key = _I18N_ERROR_CODE_MAP.get(error_code)
        i18n_msg = _t(i18n_key) if i18n_key else error_code
        logger.log(level, "[opcua] device=%s code=%s i18n=%s %s", device_id, error_code, i18n_msg, message)

    async def _mark_all_subscription_points_bad(self, device_id: str) -> None:
        now = datetime.now(UTC)
        points = self._device_points.get(device_id, [])
        async with self._values_lock:
            for point_def in points:
                point_name = point_def.get("name", "")
                if point_name:
                    self._latest_values.setdefault(device_id, {})[point_name] = PointValue(
                        value=None,
                        quality="bad",
                        timestamp=now,
                        source=f"opcua:{OpcUaDriverErrors.OFFLINE_BAD_QUALITY}",
                    )

    def _check_cert_expiry(self, cert_path: str, cert_type: str, device_id: str = "") -> bool:
        if not cert_path:
            return True
        try:
            import datetime as _dt

            from cryptography import x509
            from cryptography.hazmat.backends import default_backend

            with open(cert_path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read(), default_backend())
            now = _dt.datetime.now(UTC).replace(tzinfo=None)
            if hasattr(cert, "not_valid_after_utc"):
                expires = cert.not_valid_after_utc.replace(tzinfo=None)
            else:
                expires = cert.not_valid_after
            if now > expires:
                self._log_error(device_id, "CERT_EXPIRED", f"msg={cert_type} certificate expired on {expires}")
                if device_id:
                    self._certificate_status[device_id] = {
                        "cert_path": cert_path,
                        "cert_type": cert_type,
                        "expires_at": expires.isoformat(),
                        "days_left": 0,
                        "status": "expired",
                    }
                return False
            days_left = (expires - now).days
            if device_id:
                status = "valid"
                if days_left <= 30:
                    status = "expiring_soon"
                elif days_left <= 90:
                    status = "expiring_warning"
                self._certificate_status[device_id] = {
                    "cert_path": cert_path,
                    "cert_type": cert_type,
                    "expires_at": expires.isoformat(),
                    "days_left": days_left,
                    "status": status,
                }
            if days_left <= 90:
                if days_left <= 30:
                    self._log_error(
                        device_id,
                        "CERT_EXPIRING",
                        f"msg={cert_type} certificate expires in {days_left} days ({expires}) - URGENT",
                        level=logging.ERROR,
                    )
                    self._publish_cert_alarm(device_id, cert_type, days_left, expires)
                else:
                    self._log_error(
                        device_id,
                        "CERT_EXPIRING",
                        f"msg={cert_type} certificate expires in {days_left} days ({expires})",
                        level=logging.WARNING,
                    )
            return True
        except ImportError:
            # FIXED-P2: cryptography库未安装时返回False，阻止未验证的TLS连接
            logger.error(
                "[opcua] code=CERT_IMPORT_ERROR msg=cryptography library not installed, cannot verify certificate"
            )
            return False
        except Exception as e:
            logger.warning("[opcua] code=CERT_CHECK_FAILED msg=Failed to check %s certificate: %s", cert_type, e)
            # FIXED-P2: 证书检查失败时返回False，防止无效/缺失证书绕过TLS验证
            return False

    def _try_auto_renew_self_signed_cert(self, cert_path: str, key_path: str, device_id: str) -> bool:
        if not cert_path or not key_path:
            return False
        try:
            import datetime as _dt
            import os

            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.x509.oid import NameOID

            with open(cert_path, "rb") as f:
                old_cert = x509.load_pem_x509_certificate(f.read(), default_backend())
            if old_cert.issuer != old_cert.subject:
                self._log_error(
                    device_id, "CERT_AUTO_RENEW_FAILED", "msg=Certificate is not self-signed, cannot auto-renew"
                )
                return False
            with open(key_path, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
            now = _dt.datetime.now(UTC).replace(tzinfo=None)
            new_cert = (
                x509.CertificateBuilder()
                .subject_name(old_cert.subject)
                .issuer_name(old_cert.issuer)
                .public_key(private_key.public_key())  # type: ignore[arg-type]
                .serial_number(x509.random_serial_number())
                .not_valid_before(now)
                .not_valid_after(now + _dt.timedelta(days=365))
                .sign(private_key, hashes.SHA256(), default_backend())  # type: ignore[arg-type]
            )
            backup_path = cert_path + ".bak"
            if os.path.exists(cert_path):
                os.replace(cert_path, backup_path)
            try:
                with open(cert_path, "wb") as f:
                    f.write(new_cert.public_bytes(serialization.Encoding.PEM))
            except Exception:
                if os.path.exists(backup_path):
                    os.replace(backup_path, cert_path)
                raise
            self._log_error(
                device_id,
                "CERT_AUTO_RENEW_OK",
                "msg=Self-signed certificate auto-renewed for 365 days",
                level=logging.INFO,
            )
            return True
        except ImportError:
            self._log_error(device_id, "CERT_AUTO_RENEW_FAILED", "msg=cryptography library not installed")
            return False
        except Exception as e:
            backup_path = cert_path + ".bak"
            if os.path.exists(backup_path) and not os.path.exists(cert_path):
                try:
                    os.replace(backup_path, cert_path)
                except Exception as inner_e:
                    logger.warning(
                        "[opcua] try_auto_renew_self_signed_cert failed: %s", inner_e
                    )  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            self._log_error(device_id, "CERT_AUTO_RENEW_FAILED", f"msg={e}")
            return False

    def _publish_cert_alarm(self, device_id: str, cert_type: str, days_left: int, expires: object) -> None:
        try:
            from edgelite.app import _app_state

            event_bus = getattr(_app_state, "event_bus", None)
            if event_bus:
                from edgelite.engine.event_bus import AlarmEvent

                event = AlarmEvent(
                    alarm_id=f"opcua_cert_{device_id}_{cert_type}",
                    rule_id="opcua_certificate_expiry",
                    device_id=device_id,
                    severity="warning" if days_left > 7 else "critical",
                    action="firing",
                    trigger_value={"cert_type": cert_type, "days_left": days_left, "expires_at": str(expires)},
                    rule_type="certificate_monitor",
                )
                try:
                    asyncio.get_running_loop()

                    # UA-006: 追踪线程安全上下文中的 task
                    async def _publish_event():
                        await event_bus.publish(event)

                    task = asyncio.create_task(_publish_event())
                    self._background_tasks.add(task)
                    task.add_done_callback(lambda t: self._background_tasks.discard(t))
                except RuntimeError:
                    logger.warning("[opcua] Cannot publish cert alarm: no event loop")
        except Exception as e:
            logger.debug("Failed to publish certificate alarm: %s", e)

    def get_certificate_status(self) -> dict[str, dict]:
        return dict(self._certificate_status)

    def get_collection_mode(self, device_id: str) -> str:
        return self._collection_modes.get(device_id, CollectionMode.SUBSCRIPTION).value

    def get_point_health_stats(self, device_id: str) -> dict[str, dict]:
        dev_health = self._point_health.get(device_id, {})
        result = {}
        for name, ph in dev_health.items():
            result[name] = {
                "success_count": ph.success_count,
                "fail_count": ph.fail_count,
                "consecutive_fails": ph.consecutive_fails,
                "success_rate": ph.success_rate,
                "last_publish_at": ph.last_publish_at,
                "same_value_count": ph.same_value_count,
                "subscription_count": ph.subscription_count,
            }
        return result

    def _get_security_policy_map(self):
        try:
            from asyncua.crypto.security_policies import SecurityPolicy

            return {
                "None": None,
                "Basic128Rsa15": SecurityPolicy.Basic128Rsa15,
                "Basic256": SecurityPolicy.Basic256,
                "Basic256Sha256": SecurityPolicy.Basic256Sha256,
            }
        except ImportError:
            return {"None": None}

    async def _run_keepalive_loop(self, device_id: str, client: Any) -> bool:
        """Keepalive loop: returns True if subscription should be restored (session expired/failed)."""
        while self._running and not self._stopping:
            await asyncio.sleep(_KEEPALIVE_CHECK_INTERVAL)
            # FIXED-P1: keepalive循环跨设备遍历_clients未加锁，移除无实际作用的跨设备检查
            session_age = time.monotonic() - self._session_created_at.get(device_id, time.monotonic())
            session_timeout_s = self._session_timeout_ms.get(device_id, 60000) / 1000.0
            if session_age >= session_timeout_s * _SESSION_PRE_EXPIRY_RATIO:
                self._log_error(
                    device_id,
                    "SESSION_PRE_EXPIRY_REBUILD",
                    f"msg=Session age={session_age:.0f}s exceeds {int(_SESSION_PRE_EXPIRY_RATIO * 100)}% of timeout",
                    level=logging.WARNING,
                )
                return True
            try:
                state = client.session_state
                if state != 1:
                    self._log_error(device_id, "SESSION_EXPIRED", f"msg=Session state={state}", level=logging.WARNING)
                    async with self._session_locks[device_id]:
                        self._session_expired[device_id] = True
                        # FIXED-P1: Event set移入锁保护，与clear()在同一锁下，消除竞态窗口
                        self._session_rebuilding[device_id].set()
                    await self._mark_all_subscription_points_bad(device_id)
                    return True
                # FIXED-P1: get_objects_node()返回缓存节点不发网络请求，改用read_browse_name实际检测连接存活
                # FIXED-P2: keepalive超时从30秒缩短到5秒，避免长时间阻塞导致会话过期检测延迟
                await asyncio.wait_for(client.nodes.server.read_browse_name(), timeout=5.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log_error(device_id, "KEEPALIVE_FAILED", "msg=Keep-alive failed", level=logging.WARNING)
                async with self._session_locks[device_id]:
                    self._session_expired[device_id] = True
                    # FIXED-P1: Event set移入锁保护，与clear()在同一锁下，消除竞态窗口
                    self._session_rebuilding[device_id].set()
                await self._mark_all_subscription_points_bad(device_id)
                return True
        return False

    async def _validate_certs_for_connect(
        self, device_id: str, retry_count: int
    ) -> tuple[str | None, str | None, str | None, bool, int]:
        """Validate certs before connecting.

        Returns ``(client_cert_path, client_key_path, ca_cert_path, proceed, new_retry_count)``.
        ``proceed=False`` signals the caller should ``continue`` the reconnect loop
        (cert invalid and retry/backoff handled internally).
        """
        _MAX_CERT_EXPIRY_RETRIES = 3  # FIXED-P0: 证书过期场景增加最大重试次数，防止无限循环
        self._set_state(device_id, OpcUaConnectionState.CONNECTING)
        self._set_state(device_id, OpcUaConnectionState.CERT_VALIDATING)
        cert_valid = True
        client_cert_path, client_key_path, ca_cert_path = self._get_effective_cert_paths(device_id)
        if client_cert_path:
            cert_ok = await asyncio.to_thread(self._check_cert_expiry, client_cert_path, "Client", device_id=device_id)
            if not cert_ok:
                if self._switch_to_backup_certs(device_id):
                    client_cert_path, client_key_path, ca_cert_path = self._get_effective_cert_paths(device_id)
                    cert_ok = await asyncio.to_thread(
                        self._check_cert_expiry, client_cert_path, "Client", device_id=device_id
                    )
                if not cert_ok:
                    renewed = await asyncio.to_thread(
                        self._try_auto_renew_self_signed_cert, client_cert_path, client_key_path, device_id
                    )
                    if renewed:
                        cert_valid = True
                    else:
                        cert_valid = False
                        self._log_error(
                            device_id,
                            "SECURITY_BLOCKED",
                            "msg=Client cert invalid, refusing to degrade to unencrypted connection",
                            level=logging.ERROR,
                        )  # FIXED-P2: 证书失效时不降级为无加密，改为ERROR级别
        if ca_cert_path:
            ca_ok = await asyncio.to_thread(self._check_cert_expiry, ca_cert_path, "CA", device_id=device_id)
            if not ca_ok:
                cert_valid = False
                self._log_error(
                    device_id,
                    "SECURITY_BLOCKED",
                    "msg=CA cert invalid, refusing to degrade to unencrypted connection",
                    level=logging.ERROR,
                )  # FIXED-P2: CA证书失效时不降级

        if not cert_valid and not self._security_degraded.get(device_id, False):
            retry_count += 1  # FIXED-P0: 证书过期场景增加最大重试次数，防止无限循环
            if retry_count > _MAX_CERT_EXPIRY_RETRIES:
                self._log_error(
                    device_id,
                    "CERT_EXPIRED",
                    f"msg=Certificate expired, max retries ({_MAX_CERT_EXPIRY_RETRIES}) exceeded, entering long-interval retry mode (3600s)",
                )
                self._set_state(device_id, OpcUaConnectionState.OFFLINE)
                await self._mark_all_subscription_points_bad(device_id)
                retry_count = 0  # FIXED-P1: 长间隔重试后重置计数器，使下次有完整短间隔重试机会
                # FIXED-P0: 证书过期重试耗尽后不break，改用长间隔重试避免永久离线
                for _ in range(360):
                    if self._stopping or not self._running:
                        break
                    await asyncio.sleep(10)
                return client_cert_path, client_key_path, ca_cert_path, False, retry_count
            self._log_error(
                device_id,
                "CERT_EXPIRED",
                f"msg=Certificate expired and cannot degrade, waiting 60s before retry (attempt {retry_count}/{_MAX_CERT_EXPIRY_RETRIES})",
            )
            self._set_state(device_id, OpcUaConnectionState.OFFLINE)
            await self._mark_all_subscription_points_bad(device_id)
            # UA-001: 使用短 sleep 循环，允许停止信号在 10 秒内中断
            for _ in range(6):
                if self._stopping or not self._running:
                    break
                await asyncio.sleep(10)
            return client_cert_path, client_key_path, ca_cert_path, False, retry_count

        return client_cert_path, client_key_path, ca_cert_path, True, retry_count

    async def _create_opcua_client(
        self,
        device_id: str,
        server_url: str,
        username: str | None,
        password: str | None,
        session_timeout: int,
        security_mode_str: str,
        security_policy_str: str,
        client_cert_path: str | None,
        client_key_path: str | None,
        ca_cert_path: str | None,
    ) -> Any:
        """Create and configure an OPC-UA client.

        Returns the configured client, or ``None`` when security is degraded
        (the caller should ``continue`` the reconnect loop in that case).
        """
        _SECURITY_MODE_MAP = {"None": 1, "Sign": 2, "SignAndEncrypt": 3}
        self._set_state(device_id, OpcUaConnectionState.SESSION_CREATING)
        from asyncua import Client

        client = Client(server_url)
        client.session_timeout = session_timeout

        if username and password:
            client.set_user(username)
            client.set_password(password)

        degraded = self._security_degraded.get(device_id, False)
        if not degraded:
            security_mode_val = _SECURITY_MODE_MAP.get(security_mode_str, 1)
            client.security_mode = security_mode_val
            # FIXED-P0: security_policy_str 已在方法开头读取（默认 Basic256Sha256），此处不再重复读取
            _security_policy_map = self._get_security_policy_map()
            policy = _security_policy_map.get(security_policy_str)
            if policy is not None:
                cert = client_cert_path if client_cert_path else None
                key = client_key_path if client_key_path else None
                if cert and key:
                    await client.set_security(policy, cert, key)  # FIXED: 补充 await, 原协程未执行
                else:
                    self._log_error(
                        device_id,
                        "SECURITY_SKIP",
                        f"msg=Security policy={security_policy_str} requires certificate+key",
                        level=logging.WARNING,
                    )
            if client_cert_path and client_key_path and policy is None:
                await client.load_client_certificate(client_cert_path)  # FIXED: 补充 await
                await client.load_private_key(client_key_path)  # FIXED: 补充 await
            if ca_cert_path:
                await client.load_server_certificate(
                    ca_cert_path
                )  # FIXED-P1: 补充 await, 未 await 导致 CA 证书未加载, TLS 校验失效(MITM 风险)
            return client
        else:
            # FIXED-P2: 证书不可用时拒绝降级为无加密，标记设备OFFLINE而非静默降级
            self._log_error(
                device_id,
                "SECURITY_BLOCKED",
                "msg=Security degradation blocked, marking device OFFLINE",
                level=logging.ERROR,
            )
            self._set_state(device_id, OpcUaConnectionState.OFFLINE)
            await self._mark_all_subscription_points_bad(device_id)
            for _ in range(6):
                if self._stopping or not self._running:
                    break
                await asyncio.sleep(10)
            return None

    async def _handle_reconnect_or_failover(self, device_id: str, client: Any, connected: bool) -> tuple[bool, Any]:
        """Handle reconnect / failover after the keepalive loop requested a restore.

        Always leads to the caller ``continue``-ing the reconnect loop.
        Returns ``(new_connected, new_client)``.
        """
        if not self._is_using_backup(device_id):
            if await self._fast_failover(device_id):
                self._set_state(device_id, OpcUaConnectionState.CONNECTING)
                async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
                    self._clients.pop(device_id, None)
                    # FIXED-P0: 仅在已连接状态转换到待连接时递增，首次连接失败时_connected已为False不需重复递增
                    # FIXED-BugR4X: 原问题-_pending_connections+=1在锁外执行与锁内递减不一致，修复-移入_clients_lock块内
                    if connected:
                        self._pending_connections += 1
                connected = False  # FIXED-P0: 标记当前处于pending状态，finally中需递减
                if client:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, Exception) as e:
                        logger.debug("OPC-UA client disconnect failed: %s", e)
                return connected, client
        else:
            primary_ok = await self._probe_primary(device_id)
            if primary_ok:
                self._persist_session_state(device_id)
                self._revert_to_primary(device_id)
                config = self._device_configs.get(device_id, {})
                if config.get("client_cert_path"):
                    self._revert_to_primary_certs(device_id)
                self._log_error(device_id, "FAILOVER_REVERT", "msg=Primary recovered, reverting", level=logging.INFO)
                self._set_state(device_id, OpcUaConnectionState.CONNECTING)
                async with self._clients_lock:
                    self._clients.pop(device_id, None)
                    # FIXED-P0: 仅在已连接状态转换到待连接时递增，首次连接失败时_connected已为False不需重复递增
                    # FIXED-BugR4X: 原问题-_pending_connections+=1在锁外执行与锁内递减不一致，修复-移入_clients_lock块内
                    if connected:
                        self._pending_connections += 1
                connected = False  # FIXED-P0: 标记当前处于pending状态，finally中需递减
                if client:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, Exception) as e:
                        logger.debug("OPC-UA client disconnect failed: %s", e)
                return connected, client
        self._log_error(device_id, "RECONNECTING", "msg=Reconnecting", level=logging.INFO)
        self._set_state(device_id, OpcUaConnectionState.CONNECTING)
        async with self._clients_lock:
            self._clients.pop(device_id, None)
            # FIXED-P0: 仅在已连接状态转换到待连接时递增，首次连接失败时_connected已为False不需重复递增
            # FIXED-BugR4X: 原问题-_pending_connections+=1在锁外执行与锁内递减不一致，修复-移入_clients_lock块内
            if connected:
                self._pending_connections += 1
        connected = False  # FIXED-P0: 标记当前处于pending状态，finally中需递减
        if client:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
            except asyncio.CancelledError:
                raise
            except (TimeoutError, Exception) as e:
                logger.debug("OPC-UA client disconnect failed: %s", e)
        client = None
        return connected, client

    async def _handle_connect_exception(
        self,
        device_id: str,
        client: Any,
        first_fail_time: float | None,
        connected: bool,
        exc: Exception,
    ) -> tuple[float | None, bool, str]:
        """Handle a generic connection exception.

        Returns ``(new_first_fail_time, new_connected, action)`` where ``action``
        is one of ``"break"`` (stop reconnecting), ``"continue"`` (next loop
        iteration) or ``"proceed"`` (fall through to the inner ``finally``).
        """
        # FIXED-P2: 连接异常后确保客户端被断开，防止半连接状态导致连接泄漏
        if client:
            try:
                # FIXED-P1: disconnect添加超时保护，防止服务器无响应时stop()无限阻塞
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, Exception) as e:
                logger.warning("[opcua] operation failed: %s", e)  # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        async with self._session_locks[device_id]:
            self._session_expired[device_id] = True
        self._connect_fail_count[device_id] = self._connect_fail_count.get(device_id, 0) + 1
        fails = self._connect_fail_count[device_id]
        if first_fail_time is None:
            first_fail_time = time.monotonic()  # FIXED-P1: 记录首次失败时间
        elif (time.monotonic() - first_fail_time) > _MAX_TOTAL_RECONNECT_DURATION:
            self._log_error(
                device_id,
                "CONN_FAILED",
                f"msg=Total reconnect duration exceeded {_MAX_TOTAL_RECONNECT_DURATION}s, marking OFFLINE (await manual recovery)",
                level=logging.CRITICAL,
            )
            self._set_state(device_id, OpcUaConnectionState.OFFLINE)
            return first_fail_time, connected, "break"  # FIXED-P1: 超过最大总重连时长后停止自动重连
        if fails >= _MAX_CONNECT_RETRIES:
            self._log_error(
                device_id,
                "CONN_FAILED",
                f"msg=Max connect retries ({_MAX_CONNECT_RETRIES}) exceeded, resetting counter and retrying with long delay",
                level=logging.ERROR,
            )
            self._set_state(device_id, OpcUaConnectionState.OFFLINE)
            await self._mark_all_subscription_points_bad(device_id)
            # FIXED-P0: 超过最大重试次数后不break，重置计数器并长延迟重试避免永久离线
            self._connect_fail_count[device_id] = 0
            for _ in range(60):
                if self._stopping or not self._running:
                    break
                await asyncio.sleep(10)
            return first_fail_time, connected, "continue"
        delay = _calc_backoff(fails)
        now = time.monotonic()
        if not self._is_using_backup(device_id):
            if await self._fast_failover(device_id):
                self._set_state(device_id, OpcUaConnectionState.CONNECTING)
                async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
                    self._clients.pop(device_id, None)
                # FIXED-P0: 仅在已连接状态转换到待连接时递增，首次连接失败时_connected已为False不需重复递增
                if connected:
                    self._pending_connections += 1
                connected = False  # FIXED-P0: 标记当前处于pending状态，finally中需递减
                if client:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, Exception) as e:
                        logger.debug("OPC-UA client disconnect failed: %s", e)
                return first_fail_time, connected, "continue"
        self._set_state(device_id, OpcUaConnectionState.OFFLINE)
        await self._mark_all_subscription_points_bad(device_id)
        if device_id in self._backup_cert_paths:
            self._log_error(
                device_id,
                "CERT_FAILOVER_REVERT",
                "msg=Connection failed with backup certs, rolling back to original",
                level=logging.WARNING,
            )
            original = self._original_cert_paths.pop(device_id, None)
            self._backup_cert_paths.pop(device_id, None)
            if original:
                self._backup_cert_paths[device_id] = original
        last_log = self._last_connect_log.get(device_id, 0.0)
        if fails < 3 or now - last_log >= 60:
            self._log_error(
                device_id,
                "CONN_FAILED",
                f"msg={exc}, retrying in {delay:.1f}s (attempt #{fails})",
                level=logging.WARNING if fails > 3 else logging.ERROR,
            )
            self._last_connect_log[device_id] = now
        # FIXED-P1: 通用异常处理中sleep改为短sleep循环，允许stop信号中断
        remaining = delay
        while remaining > 0 and not self._stopping and self._running:
            chunk = min(10.0, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk
        return first_fail_time, connected, "proceed"

    async def _validate_security_config(
        self, device_id: str, config: dict, security_mode_str: str, security_policy_str: str
    ) -> bool:
        """Validate security_mode / security_policy combination before connecting.

        Returns ``False`` when the combination is invalid (caller should return
        early); ``True`` to proceed. Also warns when certificates are missing.
        """
        # FIXED-P0: 校验 security_mode 与 security_policy 组合合法性
        # 之前：security_mode=SignAndEncrypt 但 security_policy=None 时，代码跳过 set_security，实际建立无加密连接
        # 之后：组合不一致时拒绝启动，记录 SECURITY_CONFIG_INVALID 错误
        if security_mode_str != "None" and security_policy_str == "None":
            self._log_error(
                device_id,
                "SECURITY_CONFIG_INVALID",
                f"msg=security_mode={security_mode_str} requires security_policy != None, refusing to connect",
                level=logging.ERROR,
            )
            self._set_state(device_id, OpcUaConnectionState.OFFLINE)
            await self._mark_all_subscription_points_bad(device_id)
            for _ in range(6):
                if self._stopping or not self._running:
                    break
                await asyncio.sleep(10)
            return False
        # 当 security_mode != None 时，警告缺少证书
        if security_mode_str != "None" and not (config.get("client_cert_path") and config.get("client_key_path")):
            self._log_error(
                device_id,
                "SECURITY_CONFIG_INVALID",
                f"msg=security_mode={security_mode_str} requires client_cert_path and client_key_path",
                level=logging.WARNING,
            )
        return True

    async def _init_session_after_connect(self, device_id: str, session_timeout: int) -> None:
        """Initialise session bookkeeping after a successful connect()."""
        async with self._session_locks[device_id]:
            self._session_expired[device_id] = False
            # UA-002: Session 重建成功，重置重建事件
            if device_id in self._session_rebuilding:
                self._session_rebuilding[device_id].clear()
            # UA-MED-002: 处理排队中的请求
            await self._drain_rebuild_queue(device_id)
            self._session_created_at[device_id] = time.monotonic()
            self._session_timeout_ms[device_id] = session_timeout
            self._connect_fail_count.pop(device_id, None)
            self._security_degraded[device_id] = False

    async def _create_subscription_with_fallback(
        self,
        device_id: str,
        client: Any,
        config: dict,
        use_subscription_config: bool,
        collection_mode: CollectionMode,
    ) -> None:
        """Create subscription, degrading to polling on failure when allowed."""
        use_sub = use_subscription_config and collection_mode != CollectionMode.POLLING
        if not use_sub:
            return
        self._set_state(device_id, OpcUaConnectionState.SUBSCRIBING)
        try:
            await self._create_subscription(device_id, client, config)
            if collection_mode == CollectionMode.SUBSCRIPTION:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as sub_err:
            if collection_mode == CollectionMode.SUBSCRIPTION or config.get("collection_mode", "auto") == "auto":
                self._log_error(
                    device_id,
                    "SUB_DEGRADED_POLLING",
                    f"msg=Subscription failed ({sub_err}), degrading to polling mode",
                    level=logging.WARNING,
                )
                self._collection_modes[device_id] = CollectionMode.POLLING
            else:
                raise

    async def _refresh_namespace_cache(self, device_id: str, client: Any) -> None:
        """Refresh the namespace index cache for ``device_id`` (best-effort)."""
        try:
            ns_array = await client.get_namespace_array()
            self._ns_cache[device_id] = {name: idx for idx, name in enumerate(ns_array)}
        except asyncio.CancelledError:
            raise
        except Exception:
            self._ns_cache[device_id] = {}

    async def _connect_and_register_client(self, device_id: str, client: Any) -> None:
        """Connect the client and register it in the shared clients dict."""
        await asyncio.wait_for(client.connect(), timeout=self._CONNECT_TIMEOUT)
        async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
            self._clients[device_id] = client
            self._node_data_types.pop(device_id, None)  # FIXED-P2: 重连时清除数据类型缓存，防止过期
            # FIXED-P0: _pending_connections递减移入锁内，与add_device的递增在同一锁保护下，防止计数偏移
            self._pending_connections = max(0, self._pending_connections - 1)

    async def _finalize_connection(
        self,
        device_id: str,
        client: Any,
        config: dict,
        use_subscription_config: bool,
        collection_mode: CollectionMode,
    ) -> None:
        """Finish connection setup: namespace cache, subscription, state restore, probe."""
        await self._refresh_namespace_cache(device_id, client)
        await self._create_subscription_with_fallback(
            device_id, client, config, use_subscription_config, collection_mode
        )
        restored = self._restore_session_state(device_id)
        if restored:
            self._log_error(
                device_id,
                "SESSION_RESTORED",
                f"msg=Session restored from persisted state (sub_id={restored.get('subscription_id')})",
                level=logging.INFO,
            )
        self._persist_session_state(device_id)
        self._set_state(device_id, OpcUaConnectionState.CONNECTED)
        if self._is_using_backup(device_id):
            await self._start_primary_probe(device_id)

    async def _connect_device(self, device_id: str) -> None:
        config = self._device_configs.get(device_id, {})
        username = config.get("username")
        password = config.get("password")
        # FIXED-P0: security_mode 默认改为 SignAndEncrypt，防止新建设备默认明文传输
        # 之前：默认 "None"（明文），OPC UA 报文（含用户名密码）可被嗅探
        # 之后：默认 "SignAndEncrypt"，强制用户显式选择降级
        security_mode_str = config.get("security_mode", "SignAndEncrypt")
        security_policy_str = config.get("security_policy", "Basic256Sha256")
        if not await self._validate_security_config(device_id, config, security_mode_str, security_policy_str):
            return
        session_timeout = int(config.get("session_timeout", 60000))
        use_subscription_config = config.get("use_subscription", True)
        collection_mode = self._collection_modes.get(device_id, CollectionMode.SUBSCRIPTION)

        _cert_expiry_retry_count = 0  # FIXED-P0: 证书过期场景增加最大重试次数，防止无限循环
        _first_fail_time: float | None = None  # FIXED-P1: 首次连接失败时间，用于限制总重连时长
        _connected = False  # FIXED-P0: 跟踪当前连接状态，控制finally中_pending_connections递减逻辑

        try:  # FIXED-P0: 外层try包裹整个连接循环，确保finally中清理代码始终执行
            while self._running and not self._stopping:
                server_url = self._get_active_endpoint(device_id)
                if self._stopping:
                    break
                client = None
                _restore_subscription = False

                try:
                    (
                        client_cert_path,
                        client_key_path,
                        ca_cert_path,
                        _cert_proceed,
                        _cert_expiry_retry_count,
                    ) = await self._validate_certs_for_connect(device_id, _cert_expiry_retry_count)
                    if not _cert_proceed:
                        continue

                    client = await self._create_opcua_client(
                        device_id,
                        server_url,
                        username,
                        password,
                        session_timeout,
                        security_mode_str,
                        security_policy_str,
                        client_cert_path,
                        client_key_path,
                        ca_cert_path,
                    )
                    if client is None:
                        continue  # security degraded path handled inside helper

                    await self._connect_and_register_client(device_id, client)
                    _connected = True  # FIXED-P0: 标记当前已连接，finally中不再递减
                    await self._init_session_after_connect(device_id, session_timeout)
                    _first_fail_time = None  # FIXED-P1: 连接成功后重置首次失败时间
                    await self._finalize_connection(device_id, client, config, use_subscription_config, collection_mode)
                    _cert_expiry_retry_count = 0  # FIXED-P0: 连接成功后重置证书过期重试计数器

                    _restore_subscription = await self._run_keepalive_loop(device_id, client)

                    if _restore_subscription:
                        _connected, client = await self._handle_reconnect_or_failover(device_id, client, _connected)
                        continue

                except asyncio.CancelledError:
                    raise
                except ImportError:
                    self._log_error(
                        device_id,
                        "CONN_FAILED",
                        "msg=asyncua library not installed, marking device as permanently offline",
                    )
                    self._import_error_devices.add(device_id)  # FIXED-P2: ImportError后标记设备永久离线，不再重试
                    self._set_state(device_id, OpcUaConnectionState.OFFLINE)
                    async with self._session_locks[device_id]:
                        self._session_expired[device_id] = True
                    await self._mark_all_subscription_points_bad(device_id)
                    break  # FIXED-P2: ImportError后不再重试，直接退出循环
                except Exception as e:
                    _first_fail_time, _connected, _action = await self._handle_connect_exception(
                        device_id, client, _first_fail_time, _connected, e
                    )
                    if _action == "break":
                        break
                    elif _action == "continue":
                        continue
                finally:
                    async with self._clients_lock:  # FIXED-P0: finally块访问_clients字典未加锁
                        should_disconnect = client is not None and (
                            device_id not in self._clients or self._clients[device_id] is not client
                        )
                    if should_disconnect:
                        try:
                            assert client is not None
                            await asyncio.wait_for(client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
                        except asyncio.CancelledError:
                            raise
                        except (TimeoutError, Exception) as e:
                            logger.debug("OPC-UA client disconnect in finally failed: %s", e)
        # FIXED-P0: 将清理代码放入finally确保CancelledError时也能执行，防止_pending_connections永久泄漏
        finally:
            if not _connected:  # FIXED-P0: 仅在pending状态时递减，避免连接成功后finally额外递减导致计数偏低
                async with self._clients_lock:  # FIXED-P0: finally递减移入锁内，与add_device递增在同一锁保护下
                    self._pending_connections = max(0, self._pending_connections - 1)
            self._subscriptions.pop(device_id, None)
            handler = self._sub_handlers.pop(device_id, None)
            if handler:
                handler.cancel()
            # FIXED-P2: 清理_session_rebuilding残留，防止设备离线后read_points等待60秒重建超时
            rebuild_event = self._session_rebuilding.get(device_id)
            if rebuild_event and rebuild_event.is_set():
                rebuild_event.clear()

    async def _create_subscription(self, device_id: str, client: Any, config: dict) -> None:
        sub_lock = self._subscription_locks.get(device_id)
        if sub_lock:
            async with sub_lock:
                await self._do_create_subscription(device_id, client, config)
        else:
            await self._do_create_subscription(device_id, client, config)

    async def _do_create_subscription(self, device_id: str, client: Any, config: dict) -> None:
        points = self._device_points.get(device_id, [])
        if not points:
            return
        # 协议边界校验: 订阅刷新间隔不得低于 50ms，防止过频刷新导致 CPU 占用过高
        interval = max(50, int(config.get("subscription_interval", 500)))
        deadband_type_str = config.get("deadband_type", "None")
        deadband_value = float(config.get("deadband_value", 0))
        use_native_deadband = config.get("use_native_deadband", True)
        batch_size = int(config.get("batch_size", self._batch_size))

        handler = _SubHandler(
            device_id,
            self._latest_values,
            self._data_callback,
            self._values_lock,
            self._event_bus,
            self._subscription_locks.get(device_id),
            self,
        )
        subscription = await client.create_subscription(interval, handler)
        self._subscriptions[device_id] = subscription
        self._sub_handlers[device_id] = handler  # FIXED-P0: 追踪handler以便stop时取消
        record_packet("tx", "opcua", device_id, f"Subscription: interval={interval}ms")

        use_deadband = deadband_type_str != "None" and deadband_value > 0
        native_deadband_ok = True

        if deadband_type_str != "None" and deadband_value <= 0:
            use_deadband = False

        success_count = 0
        for batch_start in range(0, len(points), batch_size):
            batch = points[batch_start : batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            batch_success = await self._subscribe_batch(
                device_id,
                client,
                subscription,
                batch,
                use_deadband and use_native_deadband,
                deadband_type_str,
                deadband_value,
                batch_num,
            )
            if batch_success == 0 and use_deadband and use_native_deadband:
                native_deadband_ok = False
                self._log_error(
                    device_id,
                    "DEADBAND_NATIVE_FAILED",
                    "msg=Native deadband failed, retrying without native deadband",
                    level=logging.WARNING,
                )
                self._native_deadband_failed[device_id] = True
                batch_success = await self._subscribe_batch(
                    device_id, client, subscription, batch, False, deadband_type_str, deadband_value, batch_num
                )
            success_count += batch_success

        if not native_deadband_ok and use_deadband:
            sw_deadband = config.get("deadband")
            if sw_deadband is None:
                self._log_error(
                    device_id,
                    "DEADBAND_NATIVE_FAILED",
                    "msg=Native deadband failed and no software deadband configured, deadband disabled",
                    level=logging.WARNING,
                )

        if success_count > 0:
            self._log_error(
                device_id,
                "SUBSCRIPTION_OK",
                f"msg=Subscription created interval={interval}ms nodes={success_count}/{len(points)}",
                level=logging.INFO,
            )
            for pt in points:
                ph = self._get_point_health(device_id, pt.get("name", ""))
                ph.subscription_count += 1
        else:
            self._log_error(
                device_id, "SUBSCRIPTION_FAILED", f"msg=All node subscriptions failed nodes=0/{len(points)}"
            )
            # FIXED-BugR4X: 原问题-所有节点订阅失败抛RuntimeError前未清理已创建的subscription和handler导致资源泄漏，修复-调用subscription.delete()并从_subscriptions/_sub_handlers字典移除
            try:
                await subscription.delete()
            except Exception as e:
                logger.debug("[opcua] subscription delete failed during cleanup: %s", e)
            self._subscriptions.pop(device_id, None)
            self._sub_handlers.pop(device_id, None)
            raise RuntimeError(f"All {len(points)} node subscriptions failed")

    async def _subscribe_batch(
        self,
        device_id,
        client,
        subscription,
        points_batch,
        use_native_deadband,
        deadband_type_str,
        deadband_value,
        batch_num,
    ):
        success_count = 0
        try:
            from asyncua.common.subscription import DeadbandType

            db_type = DeadbandType.Absolute if deadband_type_str == "Absolute" else DeadbandType.Percent
        except ImportError:
            use_native_deadband = False
            db_type = None

        node_objs = []
        for point_def in points_batch:
            node_id = point_def.get("address", "")
            try:
                node_objs.append(client.get_node(node_id))
            except Exception:
                node_objs.append(None)

        for i in range(0, len(node_objs), 10):
            chunk = list(zip(points_batch[i : i + 10], node_objs[i : i + 10], strict=False))
            for point_def, node in chunk:
                if node is None:
                    continue
                point_name = point_def.get("name", node.nodeid.to_string() if node else "")
                try:
                    if use_native_deadband and db_type is not None:
                        await subscription.subscribe_data_change(
                            node, deadband_type=db_type, deadband_value=deadband_value
                        )
                    else:
                        await subscription.subscribe_data_change(node)
                    success_count += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log_error(
                        device_id,
                        "SUBSCRIPTION_FAILED",
                        f"msg=Batch {batch_num} point={point_name} err={e}",
                        level=logging.WARNING,
                    )
        return success_count

    async def create_subscription_batch(
        self, device_id, node_addresses, node_names=None, interval=500, deadband_type="None", deadband_value=0.0
    ):
        async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
            client = self._clients.get(device_id)
        if not client:
            return {
                "success": False,
                "subscribed": 0,
                "failed": len(node_addresses),
                "errors": ["client_not_connected"],
            }
        try:
            # 协议边界校验: 订阅刷新间隔不得低于 50ms，防止过频刷新导致 CPU 占用过高
            interval = max(50, int(interval))
            use_deadband = deadband_type != "None" and deadband_value > 0
            try:
                from asyncua.common.subscription import DeadbandType

                db_type = DeadbandType.Absolute if deadband_type == "Absolute" else DeadbandType.Percent
            except ImportError:
                use_deadband = False
                db_type = None

            subscription = self._subscriptions.get(device_id)
            if not subscription:
                handler = _SubHandler(
                    device_id,
                    self._latest_values,
                    self._data_callback,
                    self._values_lock,
                    self._event_bus,
                    self._subscription_locks.get(device_id),
                    self,
                )
                subscription = await client.create_subscription(interval, handler)
                self._subscriptions[device_id] = subscription
                self._sub_handlers[device_id] = handler  # FIXED-P0: 追踪handler以便stop时取消

            subscribed = 0
            errors = []
            names = node_names or node_addresses
            for addr, name in zip(node_addresses, names, strict=False):
                try:
                    node = client.get_node(addr)
                    if use_deadband and db_type is not None:
                        await subscription.subscribe_data_change(
                            node, deadband_type=db_type, deadband_value=deadband_value
                        )
                    else:
                        await subscription.subscribe_data_change(node)
                    subscribed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    errors.append(f"{name}({addr}): {e}")
            return {
                "success": subscribed == len(node_addresses),
                "subscribed": subscribed,
                "failed": len(node_addresses) - subscribed,
                "errors": errors,
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {"success": False, "subscribed": 0, "failed": len(node_addresses), "errors": [str(e)]}

    async def health_check(self, device_id: str) -> bool:
        async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
            client = self._clients.get(device_id)
        if not client:
            return False
        try:
            await asyncio.wait_for(
                client.nodes.server.read_browse_name(), timeout=3.0
            )  # FIXED-P1: 与keepalive一致，使用read_browse_name实际验证连接存活
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    def get_connection_state(self, device_id: str) -> str:
        return self._get_state(device_id).value

    async def discover_devices(self, config: dict) -> list[dict]:
        server_url = config.get("endpoint", config.get("server_url", ""))
        if not server_url:
            return []
        # FIXED-P2: SSRF防护 - 校验URL协议必须为opc.tcp://
        if not server_url.startswith("opc.tcp://"):
            logger.warning("[opcua] discover rejected: invalid protocol in URL %s", server_url)
            return []
        try:
            from asyncua import Client
        except ImportError:
            return []
        discovered = []
        try:
            client = Client(server_url)
            username = config.get("username")
            password = config.get("password")
            if username and password:
                client.set_user(username)
                client.set_password(password)
            security_mode_str = config.get("security_mode", "SignAndEncrypt")  # FIXED-P0: 默认 SignAndEncrypt
            _SECURITY_MODE_MAP = {"None": 1, "Sign": 2, "SignAndEncrypt": 3}
            client.security_mode = _SECURITY_MODE_MAP.get(security_mode_str, 1)
            connect_timeout = int(config.get("connect_timeout", self._CONNECT_TIMEOUT))
            await asyncio.wait_for(client.connect(), timeout=connect_timeout)
            try:
                # FIXED-P0: discover_devices 中 get_objects_node() 添加超时保护
                objects_node = await asyncio.wait_for(client.get_objects_node(), timeout=30.0)
                children = await objects_node.get_children()
                for child in children[:100]:  # FIXED-P1: 限制discover最多浏览100个子节点，与browse一致
                    try:
                        browse_name = (await child.read_browse_name()).Name
                        display_name = (await child.read_display_name()).Text
                        node_class = (await child.read_node_class()).value
                        discovered.append(
                            {
                                "endpoint": server_url,
                                "node_id": child.nodeid.to_string(),
                                "browse_name": browse_name,
                                "display_name": display_name,
                                "node_class": node_class,
                                "protocol": "opcua",
                            }
                        )
                    except Exception as node_err:
                        logger.debug("[opcua] discover child browse failed: %s", node_err)
                        continue
            finally:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)  # FIXED-P0: disconnect添加超时保护
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[opcua] discover failed: %s", e)
        return discovered

    async def browse(self, device_id, node_id=None, max_depth=1):
        max_depth = min(max_depth, 5)  # FIXED-P2: 限制max_depth上限为5，防止递归浏览导致内存耗尽
        _MAX_CHILDREN = 100  # FIXED-P1: 每个节点最多浏览100个子节点，防止大服务器导致资源耗尽
        _MAX_TOTAL_NODES = 500  # FIXED-P0: 全局节点总数上限，防止100^5理论最大值导致内存耗尽
        _BROWSE_TIMEOUT = 30.0  # FIXED-P0: browse总超时，防止长时间阻塞
        _total_nodes = 0
        _start_time = time.monotonic()
        async with self._clients_lock:  # FIXED-P0: 客户端字典访问加锁保护
            client = self._clients.get(device_id)
        if not client:
            return []

        async def _browse_node(node, depth):
            nonlocal _total_nodes
            if _total_nodes >= _MAX_TOTAL_NODES:  # FIXED-P0: 全局节点数超限
                return None
            if time.monotonic() - _start_time > _BROWSE_TIMEOUT:  # FIXED-P0: 总超时
                return None
            try:
                browse_name = (await node.read_browse_name()).Name
                display_name = (await node.read_display_name()).Text
                node_class = (await node.read_node_class()).value
            except asyncio.CancelledError:
                raise
            except Exception:
                browse_name = ""
                display_name = ""
                node_class = 0
            _total_nodes += 1
            result = {
                "node_id": node.nodeid.to_string(),
                "browse_name": browse_name,
                "display_name": display_name,
                "node_class": node_class,
                "children": [],
            }
            if depth < max_depth:
                try:
                    children = await node.get_children()
                    for child in children[:_MAX_CHILDREN]:  # FIXED-P1: 限制每层子节点数量
                        child_result = await _browse_node(child, depth + 1)
                        if child_result is not None:
                            result["children"].append(child_result)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("OPC-UA browse get_children failed: %s", e, exc_info=True)
            return result

        try:
            # FIXED-P0: browse 中 get_objects_node() 添加超时保护
            # 注意：client.get_node() 在 asyncua 中是同步方法，返回 Node 对象而非协程，
            # 不能对其使用 await/wait_for，否则抛 TypeError 被外层 except 捕获后静默返回 []，
            # 导致浏览功能完全失效。仅 get_objects_node() 是 async 方法需要 await。
            if node_id:
                start_node = client.get_node(node_id)
            else:
                start_node = await asyncio.wait_for(client.get_objects_node(), timeout=30.0)
            children = await start_node.get_children()
            return [
                await _browse_node(child, 1) for child in children[:_MAX_CHILDREN]
            ]  # FIXED-P1: 顶层子节点同样受_MAX_CHILDREN限制
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[opcua] browse failed: device=%s error=%s", device_id, e)
            return []


class _SubHandler:
    def __init__(
        self, device_id, latest_values, data_callback, values_lock, event_bus=None, subscription_lock=None, driver=None
    ):
        self.device_id = device_id
        self._latest_values = latest_values
        self._data_callback = data_callback
        self._values_lock = values_lock
        self._event_bus = event_bus
        self._subscription_lock = subscription_lock
        self._driver = driver
        self._notify_queue = _queue_mod.Queue(maxsize=10000)
        # FIXED-P1: 通知队列dropped计数器，追踪因队列满而丢弃的旧通知数量
        self._notify_dropped_count: int = 0
        self._notify_dropped_lock = (
            threading.Lock()
        )  # FIXED-P0: 保护 _notify_dropped_count 递增，防止多线程并发 datachange_notification 导致计数竞态
        self._loop = asyncio.get_running_loop()
        self._notification_task: asyncio.Task | None = None
        self._cancelled: bool = False  # FIXED: cancel()后阻止新通知任务创建

    def datachange_notification(self, node, val, data):
        node_id = node.nodeid.to_string()
        quality = "good"

        if data is not None:
            try:
                if hasattr(data, "monitored_item") and hasattr(data.monitored_item, "Value"):
                    status_code = data.monitored_item.Value.StatusCode
                    quality = _map_opcua_quality(status_code)
                elif hasattr(data, "Value"):
                    status_code = data.Value.StatusCode if hasattr(data.Value, "StatusCode") else None
                    quality = _map_opcua_quality(status_code)
                elif hasattr(data, "status_code"):
                    quality = _map_opcua_quality(data.status_code)
            except Exception as e:  # FIXED-P2: datachange_notification中异常被静默吞没，添加日志记录
                logger.debug("Failed to parse quality in datachange_notification: %s", e)
                quality = "bad"

        try:
            self._notify_queue.put_nowait((node_id, val, quality))
        except _queue_mod.Full:
            # FIXED-P1: 通知队列满时丢弃最旧通知并记录dropped计数
            # 原问题：队列满时丢弃新通知，导致最新数据丢失而旧数据保留
            # 修复：丢弃最旧通知腾出空间，放入新通知，并递增dropped计数
            with contextlib.suppress(_queue_mod.Empty):
                self._notify_queue.get_nowait()
            try:
                self._notify_queue.put_nowait((node_id, val, quality))
            except _queue_mod.Full:
                # FIXED-P0: 第二次put仍失败，最新通知被丢弃（非最旧通知）；使用threading.Lock保护计数器递增，防止多线程并发datachange_notification导致计数竞态
                with self._notify_dropped_lock:
                    self._notify_dropped_count += 1
                    if self._notify_dropped_count % 100 == 1:
                        logger.warning(
                            "[opcua] device=%s code=NOTIFICATION_DROPPED msg=Subscription notification queue full, dropped newest notification (total_dropped=%d)",
                            self.device_id,
                            self._notify_dropped_count,
                        )

        try:
            self._loop.call_soon_threadsafe(self._dispatch_notification)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("OPC-UA call_soon_threadsafe failed: %s", e)

    def _dispatch_notification(self):
        if getattr(self, "_cancelled", False):
            return
        if self._notification_task is not None and not self._notification_task.done():
            return
        self._notification_task = asyncio.create_task(self._process_notifications())
        # FIXED-P0: 将通知任务加入驱动级后台任务追踪，确保stop()时能正确取消
        if self._driver and hasattr(self._driver, "_background_tasks"):
            self._driver._background_tasks.add(self._notification_task)
            self._notification_task.add_done_callback(self._driver._background_tasks.discard)

    async def _process_notifications(self):
        _BATCH_LIMIT = 500  # FIXED-P1: 单次处理通知批量上限，防止队列积压时长时间阻塞事件循环
        processed = 0
        while not self._notify_queue.empty() and processed < _BATCH_LIMIT:
            try:
                node_id, val, quality = self._notify_queue.get_nowait()
            except asyncio.CancelledError:
                raise
            except Exception:
                break
            await self._process_single_notification(node_id, val, quality)
            processed += 1
        # FIXED-P1: 批量限制后若队列仍有数据，重新调度处理，防止通知积压后数据长期过时
        # FIXED-P2: 添加连续调度次数限制，防止高频场景下无限递归调度耗尽事件循环资源
        if not self._notify_queue.empty():
            if self._notification_task is None or self._notification_task.done():
                consecutive_schedules = getattr(self, "_notify_consecutive_schedules", 0) + 1
                self._notify_consecutive_schedules = consecutive_schedules
                if consecutive_schedules <= 10:
                    self._notification_task = asyncio.create_task(self._process_notifications())
                    if self._driver and hasattr(self._driver, "_background_tasks"):
                        self._driver._background_tasks.add(self._notification_task)
                        self._notification_task.add_done_callback(self._driver._background_tasks.discard)
                else:
                    logger.warning(
                        "[opcua] Notification processing reschedule limit reached, yielding to next datachange callback"
                    )
                    self._notify_consecutive_schedules = 0
        else:
            self._notify_consecutive_schedules = 0

    async def _process_single_notification(self, node_id, val, quality):
        now = datetime.now(UTC)
        if quality == "bad":
            pv = PointValue(value=None, quality="bad", timestamp=now, source=f"opcua:{OpcUaDriverErrors.QUALITY_BAD}")
        elif quality == "uncertain":
            pv = PointValue(
                value=val, quality="uncertain", timestamp=now, source=f"opcua:{OpcUaDriverErrors.QUALITY_UNCERTAIN}"
            )
        else:
            pv = PointValue(value=val, quality="good", timestamp=now, source="subscribed")

        point_name = None
        if self._driver:
            for pt in self._driver._device_points.get(self.device_id, []):
                if pt.get("address") == node_id:
                    point_name = pt.get("name")
                    break
        if not point_name:
            point_name = node_id

        if self._driver and quality != "bad":
            resolved_val = val
            if hasattr(val, "__dict__") and not isinstance(val, (int, float, str, bool, list, dict)):
                resolved_val, resolve_quality = _resolve_complex_type_with_fallback(val, node_id=node_id)
                if resolve_quality == "bad":
                    pv = PointValue(
                        value=None,
                        quality="bad",
                        timestamp=now,
                        source=f"opcua:{OpcUaDriverErrors.COMPLEX_TYPE_UNKNOWN}",
                    )
                    quality = "bad"
                elif resolve_quality == "uncertain":
                    pv = PointValue(
                        value=resolved_val,
                        quality="uncertain",
                        timestamp=now,
                        source=f"opcua:{OpcUaDriverErrors.COMPLEX_TYPE_HEX_FALLBACK}",
                    )
                    quality = "uncertain"
                else:
                    pv = PointValue(value=resolved_val, quality=quality, timestamp=now, source="subscribed")

            if quality != "bad":
                pv = await self._driver._apply_point_preprocess(self.device_id, point_name, pv.value, quality)

        async with self._values_lock:
            self._latest_values.setdefault(self.device_id, {})[point_name] = pv

        try:
            if self._data_callback:
                callback_data = {point_name: pv.value if quality != "bad" else None}
                task = asyncio.create_task(
                    self._data_callback(device_id=self.device_id, data=callback_data)
                )  # FIXED-P0: 回调阻塞通知处理，改为异步执行；FIXED-P3: 统一关键字参数签名与MQTT一致
                # FIXED-P0: 将回调任务加入驱动级后台任务追踪，确保stop()时能正确取消
                if self._driver and hasattr(self._driver, "_background_tasks"):
                    self._driver._background_tasks.add(task)
                    task.add_done_callback(self._driver._background_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[opcua] device=%s code=CALLBACK_ERROR point=%s error=%s", self.device_id, point_name, exc)

        try:
            if self._event_bus:
                event = PointUpdateEvent(
                    device_id=self.device_id, point_name=point_name, value=pv.value, quality=pv.quality
                )
                await self._event_bus.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[opcua] device=%s code=EVENT_PUBLISH_ERROR point=%s error=%s", self.device_id, point_name, exc
            )

        try:
            if self._driver and self._driver._rule_engine and quality == "good" and isinstance(pv.value, (int, float)):
                await self._driver.evaluate_point_rules(self.device_id, point_name, float(pv.value), pv.quality)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[opcua] device=%s code=RULE_EVAL_ERROR point=%s error=%s", self.device_id, point_name, exc)

        try:
            if self._driver and self._driver._ts_store and pv.value is not None and quality != "bad":
                await self._driver._ts_store.write_read_result(self.device_id, {point_name: pv})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[opcua] device=%s code=DATA_PERSIST_LOCAL point=%s error=%s", self.device_id, point_name, exc)

    def cancel(self):
        """FIXED-P0: 取消通知处理任务并清空队列，防止stop()后残留通知被处理"""
        self._cancelled = True
        if self._notification_task is not None and not self._notification_task.done():
            self._notification_task.cancel()
            self._notification_task = None
        while not self._notify_queue.empty():
            try:
                self._notify_queue.get_nowait()
            except Exception:
                break
