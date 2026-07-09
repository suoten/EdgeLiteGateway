"""Internationalization (i18n) module - Chinese/English support

This module provides bilingual (Chinese/English) support for the application.
Language can be switched via configuration or Accept-Language header.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Default language
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ["en", "zh"]


@dataclass
class TranslationStrings:
    """Container for all translatable strings"""
    # System
    APP_NAME: str = "EdgeLiteGateway"
    VERSION: str = "Version"

    # Device status
    STATUS_ONLINE: str = "Online"
    STATUS_OFFLINE: str = "Offline"
    STATUS_DEGRADED: str = "Degraded"

    # Alarm severity
    SEVERITY_CRITICAL: str = "Critical"
    SEVERITY_MAJOR: str = "Major"
    SEVERITY_MINOR: str = "Minor"
    SEVERITY_WARNING: str = "Warning"
    SEVERITY_INFO: str = "Info"

    # Alarm actions
    ACTION_FIRING: str = "Alarm Triggered"
    ACTION_RECOVERED: str = "Alarm Recovered"
    ACTION_ACKNOWLEDGED: str = "Alarm Acknowledged"
    ACTION_ESCALATED: str = "Alarm Escalated"

    # Common
    DEVICE: str = "Device"
    RULE: str = "Rule"
    ALARM: str = "Alarm"
    USER: str = "User"
    SETTINGS: str = "Settings"
    SAVE: str = "Save"
    CANCEL: str = "Cancel"
    DELETE: str = "Delete"
    EDIT: str = "Edit"
    CREATE: str = "Create"
    SEARCH: str = "Search"
    REFRESH: str = "Refresh"
    EXPORT: str = "Export"
    IMPORT: str = "Import"

    # Error messages
    ERR_CONNECTION_FAILED: str = "Connection failed"
    ERR_TIMEOUT: str = "Request timeout"
    ERR_AUTH_FAILED: str = "Authentication failed"
    ERR_PERMISSION_DENIED: str = "Permission denied"
    ERR_NOT_FOUND: str = "Resource not found"
    ERR_VALIDATION_FAILED: str = "Validation failed"
    ERR_INTERNAL_ERROR: str = "Internal server error"

    # Device management
    DEVICE_ID: str = "Device ID"
    DEVICE_NAME: str = "Device Name"
    PROTOCOL: str = "Protocol"
    COLLECT_INTERVAL: str = "Collect Interval"
    POINTS_COUNT: str = "Points Count"
    ADD_DEVICE: str = "Add Device"
    DELETE_DEVICE: str = "Delete Device"
    DEVICE_TEMPLATES: str = "Device Templates"
    DEVICE_GROUPS: str = "Device Groups"

    # Rule management
    RULE_ID: str = "Rule ID"
    RULE_NAME: str = "Rule Name"
    CONDITIONS: str = "Conditions"
    SEVERITY: str = "Severity"
    DURATION: str = "Duration"
    ADD_RULE: str = "Add Rule"
    DELETE_RULE: str = "Delete Rule"
    RULE_ENABLED: str = "Enabled"
    RULE_DISABLED: str = "Disabled"

    # Alarm management
    ALARM_ID: str = "Alarm ID"
    TRIGGER_VALUE: str = "Trigger Value"
    FIRED_AT: str = "Fired At"
    RECOVERED_AT: str = "Recovered At"
    ACKNOWLEDGED_BY: str = "Acknowledged By"
    ACK_ALARM: str = "Acknowledge Alarm"
    CLEAR_ALARM: str = "Clear Alarm"
    ALARM_STATISTICS: str = "Alarm Statistics"
    MTTR: str = "MTTR (Mean Time To Recovery)"
    MTBF: str = "MTBF (Mean Time Between Failures)"
    ALARM_COUNT: str = "Alarm Count"
    ALARM_TREND: str = "Alarm Trend"

    # Notification channels
    NOTIFICATION_CHANNELS: str = "Notification Channels"
    DINGTALK: str = "DingTalk"
    WECOM: str = "WeCom (Enterprise WeChat)"
    EMAIL: str = "Email"
    WEBHOOK: str = "Webhook"
    CHANNEL_ENABLED: str = "Channel Enabled"
    CHANNEL_DISABLED: str = "Channel Disabled"
    TEST_CHANNEL: str = "Test Channel"
    CONFIGURE_CHANNEL: str = "Configure Channel"

    # System management
    SYSTEM_CONFIG: str = "System Configuration"
    BACKUP_CONFIG: str = "Backup Configuration"
    RESTORE_CONFIG: str = "Restore Configuration"
    LOG_SETTINGS: str = "Log Settings"
    LOG_LEVEL: str = "Log Level"
    LOG_ROTATION: str = "Log Rotation"
    MAX_LOG_SIZE: str = "Max Log Size"
    BACKUP_COUNT: str = "Backup Count"

    # Data management
    DATA_QUERY: str = "Data Query"
    TIME_RANGE: str = "Time Range"
    AGGREGATION: str = "Aggregation"
    EXPORT_DATA: str = "Export Data"
    IMPORT_DATA: str = "Import Data"

    # Command control
    COMMAND_CONTROL: str = "Command Control"
    SEND_COMMAND: str = "Send Command"
    COMMAND_HISTORY: str = "Command History"
    APPROVAL_REQUIRED: str = "Approval Required"
    PENDING_APPROVAL: str = "Pending Approval"

    ERR_MODBUS_CONN_FAILED: str = "Modbus TCP connection failed"
    ERR_MODBUS_CONN_TIMEOUT: str = "Modbus TCP connection timeout"
    ERR_MODBUS_READ_FAILED: str = "Modbus TCP read failed"
    ERR_MODBUS_READ_TIMEOUT: str = "Modbus TCP read timeout"
    ERR_MODBUS_READ_EXCEPTION: str = "Modbus TCP protocol exception"
    ERR_MODBUS_WRITE_FAILED: str = "Modbus TCP write failed"
    ERR_MODBUS_WRITE_TIMEOUT: str = "Modbus TCP write timeout"
    ERR_MODBUS_BCAST_WRITE_FAILED: str = "Modbus TCP broadcast write failed"
    ERR_MODBUS_VALUE_OUT_OF_RANGE: str = "Value out of clamp range"
    ERR_MODBUS_DECODE_FAILED: str = "Modbus TCP value decode failed"
    ERR_MODBUS_CONFIG_INVALID: str = "Modbus TCP config invalid"
    ERR_MODBUS_RECONNECT_OK: str = "Modbus TCP reconnected"
    ERR_MODBUS_NAN_INF: str = "NaN/Inf value detected"
    ERR_MODBUS_RATE_OF_CHANGE: str = "Rate of change exceeded threshold"
    ERR_MODBUS_FROZEN_VALUE: str = "Frozen value detected"
    ERR_MODBUS_WRITE_VERIFY_FAILED: str = "Write verify failed (read-back mismatch)"
    ERR_MODBUS_WRITE_RATE_LIMITED: str = "Write rate limited"
    ERR_MODBUS_FAILOVER_TRIGGERED: str = "Link failover triggered (primary->backup)"
    ERR_MODBUS_FAILOVER_NO_BACKUP: str = "Failover failed: no backup link available"
    ERR_MODBUS_AUTO_REVERT_OK: str = "Auto-revert to primary link"
    ERR_MODBUS_RULE_EVAL_FAILED: str = "Edge rule evaluation failed"
    ERR_MODBUS_RULE_ACTION_FAILED: str = "Edge rule action execution failed"
    ERR_MODBUS_RULE_HOT_RELOAD_OK: str = "Edge rule hot-reloaded"
    ERR_MODBUS_RULE_ROLLBACK_OK: str = "Edge rule rolled back"
    ERR_MODBUS_TS_WRITE_FAILED: str = "Time series write failed"
    ERR_MODBUS_TS_QUERY_FAILED: str = "Time series query failed"
    ERR_MODBUS_OFFLINE_SYNC_FAILED: str = "Offline sync upload failed"
    ERR_MODBUS_OFFLINE_SYNC_OK: str = "Offline sync completed"
    ERR_MODBUS_CONFIG_VERSION_SAVE_OK: str = "Config version saved"
    ERR_MODBUS_CONFIG_VERSION_ROLLBACK_OK: str = "Config version rolled back"
    ERR_MODBUS_AUDIT_LOG_OK: str = "Audit log recorded"
    ERR_S7_CONN_FAILED: str = "S7 connection failed"
    ERR_S7_CONN_TIMEOUT: str = "S7 connection timeout"
    ERR_S7_READ_FAILED: str = "S7 read failed"
    ERR_S7_READ_TIMEOUT: str = "S7 read timeout"
    ERR_S7_READ_EXCEPTION: str = "S7 protocol exception"
    ERR_S7_WRITE_FAILED: str = "S7 write failed"
    ERR_S7_WRITE_TIMEOUT: str = "S7 write timeout"
    ERR_S7_DECODE_FAILED: str = "S7 value decode failed"
    ERR_S7_CONFIG_INVALID: str = "S7 config invalid"
    ERR_S7_RECONNECT_OK: str = "S7 reconnected"
    ERR_S7_RECONNECT_FAILED: str = "S7 reconnect failed"
    ERR_S7_PASSWORD_FAILED: str = "S7 password negotiation failed"
    ERR_S7_PDU_FAILED: str = "S7 PDU negotiation failed"
    ERR_S7_CIRCUIT_OPEN: str = "S7 circuit breaker open"
    ERR_S7_CONN_LOST: str = "S7 connection lost"
    ERR_S7_CONN_RECOVERED: str = "S7 connection recovered"
    ERR_S7_VALUE_OUT_OF_RANGE: str = "S7 value out of clamp range"
    ERR_S7_AUTH_LOCKED: str = "S7 auth locked (too many password failures)"
    ERR_S7_FAILOVER_TRIGGERED: str = "S7 link failover triggered (primary->backup)"
    ERR_S7_FAILOVER_NO_BACKUP: str = "S7 failover failed: no backup link available"
    ERR_S7_REDUNDANCY_SWITCH: str = "S7 redundancy link switched"
    ERR_S7_REDUNDANCY_REVERT: str = "S7 redundancy auto-reverted to primary"
    ERR_S7_REDUNDANCY_PROBE_OK: str = "S7 primary link probe succeeded"
    ERR_S7_REDUNDANCY_PROBE_FAIL: str = "S7 primary link probe failed"
    ERR_S7_PDU_NEGOTIATING: str = "S7 PDU negotiation in progress"
    ERR_S7_RATE_OF_CHANGE: str = "S7 rate of change exceeded threshold"
    ERR_S7_FROZEN_VALUE: str = "S7 frozen value detected"
    ERR_S7_NAN_INF: str = "S7 NaN/Inf value detected"
    ERR_S7_DEGRADE_ACTIVE: str = "S7 collection degraded"
    ERR_S7_DEGRADE_RECOVERED: str = "S7 collection recovered"
    ERR_S7_BATCH_RETRY: str = "S7 batch read retry with reduced size"
    ERR_S7_WRITE_VERIFY_FAILED: str = "S7 write verify failed (read-back mismatch)"
    ERR_S7_WRITE_RATE_LIMITED: str = "S7 write rate limited"
    ERR_S7_WRITE_VALUE_INVALID: str = "S7 write value out of valid range"
    ERR_S7_EDGE_RULE_FIRED: str = "S7 edge rule fired (alarm)"
    ERR_S7_EDGE_RULE_RECOVERED: str = "S7 edge rule recovered"
    ERR_S7_EDGE_RULE_ERROR: str = "S7 edge rule evaluation error"
    ERR_S7_EDGE_TRIGGER_EXECUTED: str = "S7 edge trigger executed"
    ERR_S7_EDGE_TRIGGER_FAILED: str = "S7 edge trigger execution failed"
    ERR_S7_TS_WRITE_FAILED: str = "S7 time-series write failed"
    ERR_S7_TS_QUERY_FAILED: str = "S7 time-series query failed"
    ERR_S7_TS_CLEANUP_OK: str = "S7 time-series cleanup completed"
    ERR_S7_TS_CLEANUP_FAILED: str = "S7 time-series cleanup failed"
    ERR_S7_TS_NOT_STARTED: str = "S7 time-series store not started"
    ERR_S7_OFFLINE_ENQUEUE_FAILED: str = "S7 offline queue enqueue failed"
    ERR_S7_OFFLINE_UPLOAD_OK: str = "S7 offline data uploaded"
    ERR_S7_OFFLINE_UPLOAD_FAILED: str = "S7 offline data upload failed"
    ERR_S7_OFFLINE_SYNC_RESTORD: str = "S7 offline sync restored (network back)"
    ERR_S7_OFFLINE_COMPRESS_FAILED: str = "S7 offline batch compression failed"
    ERR_S7_CONFIG_VERSION_SAVE_OK: str = "S7 config version saved"
    ERR_S7_CONFIG_VERSION_SAVE_FAILED: str = "S7 config version save failed"
    ERR_S7_CONFIG_VERSION_ROLLBACK_OK: str = "S7 config version rollback completed"
    ERR_S7_CONFIG_VERSION_ROLLBACK_FAILED: str = "S7 config version rollback failed"
    ERR_S7_CONFIG_VERSION_NOT_FOUND: str = "S7 config version not found"
    ERR_S7_OTA_START_OK: str = "S7 OTA update started"
    ERR_S7_OTA_START_FAILED: str = "S7 OTA update start failed"
    ERR_S7_OTA_VERIFY_FAILED: str = "S7 OTA firmware verification failed"
    ERR_S7_OTA_ROLLBACK_OK: str = "S7 OTA rollback completed"
    ERR_S7_OTA_ROLLBACK_FAILED: str = "S7 OTA rollback failed"
    ERR_S7_OTA_IN_PROGRESS: str = "S7 OTA update already in progress"
    # MQTT Client
    ERR_MQTT_CONN_TIMEOUT: str = "MQTT connection timeout"
    ERR_MQTT_CONN_FAILED: str = "MQTT connection failed"
    ERR_MQTT_CONN_LOST: str = "MQTT connection lost"
    ERR_MQTT_AUTH_FAILED: str = "MQTT authentication failed"
    ERR_MQTT_TLS_ERROR: str = "MQTT TLS configuration failed"
    ERR_MQTT_SUBSCRIBE_FAILED: str = "MQTT subscribe failed"
    ERR_MQTT_PUBLISH_FAILED: str = "MQTT publish failed"
    ERR_MQTT_MESSAGE_PARSE_ERROR: str = "MQTT message parse error"
    ERR_MQTT_QUEUE_FULL: str = "MQTT publish queue full"
    ERR_MQTT_OFFLINE_BAD_QUALITY: str = "MQTT offline, point marked bad quality"

    # OPC-UA driver errors
    ERR_OPCUA_CONN_FAILED: str = "OPC UA connection failed"
    ERR_OPCUA_CONN_TIMEOUT: str = "OPC UA connection timeout"
    ERR_OPCUA_READ_FAILED: str = "OPC UA read failed"
    ERR_OPCUA_READ_TIMEOUT: str = "OPC UA read timeout"
    ERR_OPCUA_WRITE_FAILED: str = "OPC UA write failed"
    ERR_OPCUA_WRITE_TIMEOUT: str = "OPC UA write timeout"
    ERR_OPCUA_SESSION_EXPIRED: str = "OPC UA session expired"
    ERR_OPCUA_CERT_EXPIRED: str = "OPC UA certificate expired"
    ERR_OPCUA_CERT_EXPIRING: str = "OPC UA certificate expiring soon"
    ERR_OPCUA_SUBSCRIPTION_FAILED: str = "OPC UA subscription failed"
    ERR_OPCUA_BATCH_READ_FAILED: str = "OPC UA batch read failed"
    ERR_OPCUA_BATCH_READ_TIMEOUT: str = "OPC UA batch read timeout"
    ERR_OPCUA_BATCH_WRITE_FAILED: str = "OPC UA batch write failed"
    ERR_OPCUA_BATCH_WRITE_TIMEOUT: str = "OPC UA batch write timeout"
    ERR_OPCUA_CALLBACK_ERROR: str = "OPC UA subscription callback error"
    ERR_OPCUA_RECONNECTING: str = "OPC UA reconnecting"
    ERR_OPCUA_SECURITY_SKIP: str = "OPC UA security skipped due to missing certificate"
    ERR_OPCUA_VALUE_OUT_OF_RANGE: str = "OPC UA value out of clamp range"
    ERR_OPCUA_QUALITY_BAD: str = "OPC UA variable quality is Bad"
    ERR_OPCUA_QUALITY_UNCERTAIN: str = "OPC UA variable quality is Uncertain"
    ERR_OPCUA_OFFLINE_BAD_QUALITY: str = "OPC UA offline, point marked bad quality"
    ERR_OPCUA_FAILOVER_TRIGGERED: str = "OPC UA failover triggered (primary->backup)"
    ERR_OPCUA_FAILOVER_NO_BACKUP: str = "OPC UA failover failed: no backup endpoint"
    ERR_OPCUA_FAILOVER_REVERT: str = "OPC UA reverted to primary endpoint"
    ERR_OPCUA_CERT_AUTO_RENEW_OK: str = "OPC UA self-signed certificate auto-renewed"
    ERR_OPCUA_CERT_AUTO_RENEW_FAILED: str = "OPC UA certificate auto-renew failed"
    ERR_OPCUA_SECURITY_DEGRADED: str = "OPC UA security degraded to None policy"
    ERR_OPCUA_SESSION_PRE_EXPIRY_REBUILD: str = "OPC UA session pre-expiry rebuild"
    ERR_OPCUA_KEEPALIVE_FAILED: str = "OPC UA keepalive failed"
    ERR_OPCUA_STATE_TRANSITION: str = "OPC UA connection state transition"
    ERR_OPCUA_RATE_OF_CHANGE: str = "OPC UA rate of change exceeded threshold"
    ERR_OPCUA_FROZEN_VALUE: str = "OPC UA frozen value detected"
    ERR_OPCUA_NAN_INF: str = "OPC UA NaN/Inf value detected"
    ERR_OPCUA_COMPLEX_TYPE_HEX_FALLBACK: str = "OPC UA complex type parsed as hex fallback"
    ERR_OPCUA_COMPLEX_TYPE_UNKNOWN: str = "OPC UA unknown type cannot be parsed"
    ERR_OPCUA_ARRAY_TRUNCATED: str = "OPC UA array truncated"
    ERR_OPCUA_SUB_DEGRADED_POLLING: str = "OPC UA subscription degraded to polling"
    ERR_OPCUA_SUB_RECOVERED: str = "OPC UA subscription recovered"
    ERR_OPCUA_STALE_DATA: str = "OPC UA stale data detected"
    ERR_OPCUA_DEADBAND_NATIVE_FAILED: str = "OPC UA native deadband failed, falling back to software"
    ERR_OPCUA_WRITE_VERIFY_FAILED: str = "OPC UA write verify failed (read-back mismatch)"
    ERR_OPCUA_WRITE_TYPE_MISMATCH: str = "OPC UA write type mismatch"
    ERR_OPCUA_WRITE_RATE_LIMITED: str = "OPC UA write rate limited"
    ERR_OPCUA_WRITE_VALUE_INVALID: str = "OPC UA write value invalid"
    ERR_OPCUA_WRITE_AUDIT_OK: str = "OPC UA write audit logged"
    ERR_OPCUA_FAILOVER_FAST_SWITCH: str = "OPC UA fast failover to backup endpoint"
    ERR_OPCUA_SESSION_RESTORED: str = "OPC UA session restored from persisted state"
    ERR_OPCUA_SESSION_PERSIST_FAILED: str = "OPC UA session persist failed"
    ERR_OPCUA_CERT_FAILOVER_TRIGGERED: str = "OPC UA switched to backup certificates"
    ERR_OPCUA_CERT_FAILOVER_REVERT: str = "OPC UA reverted to primary certificates"
    ERR_OPCUA_RULE_EVALUATED: str = "OPC UA edge rule evaluated"
    ERR_OPCUA_RULE_TRIGGERED: str = "OPC UA edge rule triggered alarm"
    ERR_OPCUA_RULE_HOT_RELOADED: str = "OPC UA edge rules hot reloaded"
    ERR_OPCUA_CROSS_SERVER_WRITE: str = "OPC UA cross-server write triggered"
    ERR_OPCUA_DATA_PERSIST_LOCAL: str = "OPC UA data persisted to local time-series store"
    ERR_OPCUA_DATA_SYNC_RECOVERED: str = "OPC UA network recovered, data sync enabled"
    ERR_OPCUA_DATA_COMPRESS_UPLOAD: str = "OPC UA compressed data batch uploaded"
    ERR_OPCUA_CONFIG_VERSION_SAVE_OK: str = "OPC UA config version saved"
    ERR_OPCUA_CONFIG_VERSION_ROLLBACK_OK: str = "OPC UA config version rolled back"
    ERR_OPCUA_OTA_START_OK: str = "OPC UA OTA started successfully"
    ERR_OPCUA_OTA_START_FAILED: str = "OPC UA OTA start failed"
    ERR_OPCUA_OTA_VERIFY_FAILED: str = "OPC UA OTA verification failed"
    ERR_OPCUA_OTA_ROLLBACK_OK: str = "OPC UA OTA rollback succeeded"
    ERR_OPCUA_OTA_ROLLBACK_FAILED: str = "OPC UA OTA rollback failed"
    ERR_OPCUA_OTA_IN_PROGRESS: str = "OPC UA OTA already in progress"
    ERR_OPCUA_RBAC_DENIED: str = "OPC UA RBAC permission denied"
    ERR_OPCUA_AUDIT_LOG_OK: str = "OPC UA audit log recorded"

    ERR_FINS_CONN_FAILED: str = "FINS connection failed"
    ERR_FINS_CONN_TIMEOUT: str = "FINS connection timeout"
    ERR_FINS_READ_FAILED: str = "FINS read failed"
    ERR_FINS_READ_TIMEOUT: str = "FINS read timeout"
    ERR_FINS_WRITE_FAILED: str = "FINS write failed"
    ERR_FINS_WRITE_TIMEOUT: str = "FINS write timeout"
    ERR_FINS_DECODE_FAILED: str = "FINS value decode failed"
    ERR_FINS_CONFIG_INVALID: str = "FINS config invalid"
    ERR_FINS_RECONNECT_OK: str = "FINS reconnected"
    ERR_FINS_RECONNECT_FAILED: str = "FINS reconnect failed"
    ERR_FINS_CONN_LOST: str = "FINS connection lost"
    ERR_FINS_CONN_RECOVERED: str = "FINS connection recovered"
    ERR_FINS_VALUE_OUT_OF_RANGE: str = "FINS value out of clamp range"
    ERR_FINS_OFFLINE_BAD_QUALITY: str = "FINS offline, point marked bad quality"
    ERR_FINS_DIRECT_MODE_FALLBACK: str = "FINS direct mode fallback to standard read"
    ERR_FINS_DISCOVER_FAILED: str = "FINS device discovery failed"
    ERR_FINS_FAILOVER_TRIGGERED: str = "FINS link failover triggered (primary->backup)"
    ERR_FINS_FAILOVER_NO_BACKUP: str = "FINS failover failed: no backup link available"
    ERR_FINS_FAILOVER_REVERT: str = "FINS reverted to primary link"
    ERR_FINS_NODE_INIT_FAILED: str = "FINS node address initialization failed"
    ERR_FINS_NODE_INIT_OK: str = "FINS node address initialized"
    ERR_FINS_ILLEGAL_AREA: str = "FINS illegal memory area"
    ERR_FINS_ILLEGAL_ADDRESS: str = "FINS illegal address offset"
    ERR_FINS_ILLEGAL_DATA: str = "FINS illegal data length or type"
    ERR_FINS_FROZEN_VALUE: str = "FINS frozen value detected"
    ERR_FINS_NAN_INF: str = "FINS NaN or Inf value filtered"
    ERR_FINS_RATE_OF_CHANGE: str = "FINS rate of change exceeded"
    ERR_FINS_BATCH_SPLIT: str = "FINS batch read split to smaller batches"
    ERR_FINS_DEGRADED_FREQ: str = "FINS collection frequency degraded"
    ERR_FINS_WRITE_VALUE_OUT_OF_RANGE: str = "FINS write value out of range"
    ERR_FINS_WRITE_RATE_LIMITED: str = "FINS write rate limited"
    ERR_FINS_WRITE_VERIFY_FAILED: str = "FINS write-back verification failed"
    ERR_FINS_WRITE_REJECTED: str = "FINS write rejected by PLC"
    ERR_FINS_WRITE_PROTECTED_AREA: str = "FINS write to protected area"
    ERR_FINS_WRITE_MERGE: str = "FINS batch write merged adjacent addresses"
    ERR_FINS_FAILOVER_FAST: str = "FINS fast failover to backup IP"
    ERR_FINS_STANDBY_READY: str = "FINS standby connection ready"
    ERR_FINS_STANDBY_FAILED: str = "FINS standby connection failed"
    ERR_FINS_STANDBY_TAKEOVER: str = "FINS standby takeover (instant failover)"
    ERR_FINS_EDGE_RULE_FIRED: str = "FINS edge rule fired (alarm)"
    ERR_FINS_EDGE_RULE_RECOVERED: str = "FINS edge rule recovered"
    ERR_FINS_EDGE_RULE_ERROR: str = "FINS edge rule evaluation error"
    ERR_FINS_EDGE_TRIGGER_EXECUTED: str = "FINS edge trigger executed"
    ERR_FINS_EDGE_TRIGGER_FAILED: str = "FINS edge trigger execution failed"
    ERR_FINS_RULE_HOT_RELOADED: str = "FINS edge rules hot reloaded"
    ERR_FINS_RULE_ROLLBACK_OK: str = "FINS edge rule rolled back"
    ERR_FINS_TS_STORE_WRITE: str = "FINS time-series data stored locally"
    ERR_FINS_TS_STORE_OFFLINE: str = "FINS offline queue enqueued"
    ERR_FINS_TS_SYNC_RESTORED: str = "FINS offline data synced after network restore"
    ERR_FINS_TS_SYNC_FAILED: str = "FINS offline data sync failed"
    ERR_FINS_TS_COMPRESS_UPLOAD: str = "FINS compressed batch uploaded"
    ERR_FINS_CONFIG_VERSION_SAVED: str = "FINS config version saved"
    ERR_FINS_CONFIG_VERSION_ROLLBACK: str = "FINS config version rolled back"
    ERR_FINS_CONFIG_CHANGE_DENIED: str = "FINS config change denied (RBAC)"
    ERR_FINS_RBAC_DENIED: str = "FINS RBAC permission denied"
    ERR_FINS_AUDIT_LOGGED: str = "FINS audit event logged"
    ERR_FINS_OTA_CHECK: str = "FINS OTA update check"
    ERR_FINS_OTA_STARTED: str = "FINS OTA upgrade started"
    ERR_FINS_OTA_COMPLETED: str = "FINS OTA upgrade completed"
    ERR_FINS_OTA_FAILED: str = "FINS OTA upgrade failed"
    ERR_FINS_OTA_ROLLBACK: str = "FINS OTA rollback"

    # MC driver errors
    ERR_MC_CONN_FAILED: str = "MC connection failed"
    ERR_MC_CONN_TIMEOUT: str = "MC connection timeout"
    ERR_MC_CONN_LOST: str = "MC connection lost"
    ERR_MC_CONN_RECOVERED: str = "MC connection recovered"
    ERR_MC_READ_FAILED: str = "MC read failed"
    ERR_MC_READ_TIMEOUT: str = "MC read timeout"
    ERR_MC_READ_EXCEPTION: str = "MC protocol exception"
    ERR_MC_WRITE_FAILED: str = "MC write failed"
    ERR_MC_WRITE_TIMEOUT: str = "MC write timeout"
    ERR_MC_DECODE_FAILED: str = "MC value decode failed"
    ERR_MC_CONFIG_INVALID: str = "MC config invalid"
    ERR_MC_RECONNECT_OK: str = "MC reconnected"
    ERR_MC_RECONNECT_FAILED: str = "MC reconnect failed"
    ERR_MC_VALUE_OUT_OF_RANGE: str = "MC value out of clamp range"
    ERR_MC_OFFLINE_BAD_QUALITY: str = "MC offline, point marked bad quality"
    ERR_MC_SLMP_DIRECT_FALLBACK: str = "MC SLMP direct mode fallback to standard read"
    ERR_MC_DISCOVER_FAILED: str = "MC device discovery failed"
    ERR_MC_FAILOVER_TRIGGERED: str = "MC failover triggered (primary->backup)"
    ERR_MC_FAILOVER_REVERT: str = "MC reverted to primary IP"
    ERR_MC_FAILOVER_NO_BACKUP: str = "MC failover failed: no backup IP"
    ERR_MC_RATE_OF_CHANGE: str = "MC rate of change exceeded threshold"
    ERR_MC_FROZEN_VALUE: str = "MC frozen value detected"
    ERR_MC_NAN_INF: str = "MC NaN/Inf value detected"
    ERR_MC_BATCH_RETRY: str = "MC batch read retry with reduced size"
    ERR_MC_DEGRADE_ACTIVE: str = "MC collection degraded"
    ERR_MC_DEGRADE_RECOVERED: str = "MC collection recovered"
    ERR_MC_WRITE_VERIFY_FAILED: str = "MC write verify failed (read-back mismatch)"
    ERR_MC_WRITE_RATE_LIMITED: str = "MC write rate limited"
    ERR_MC_WRITE_VALUE_INVALID: str = "MC write value invalid"
    ERR_MC_WRITE_AUDIT_OK: str = "MC write audit logged"
    ERR_MC_FAILOVER_FAST: str = "MC failover exceeded 3s target duration"
    ERR_MC_RULE_ENGINE_INIT_FAILED: str = "MC edge rule engine init failed"
    ERR_MC_RULE_EVAL_FAILED: str = "MC edge rule evaluation failed"
    ERR_MC_RULE_ADD_FAILED: str = "MC edge rule add failed"
    ERR_MC_RULE_RELOAD_OK: str = "MC edge rules reloaded"
    ERR_MC_RULE_RELOAD_FAILED: str = "MC edge rules reload failed"
    ERR_MC_TRIGGER_WRITE_FAILED: str = "MC trigger write to PLC failed"
    ERR_MC_TS_STORAGE_INIT_FAILED: str = "MC time-series storage init failed"
    ERR_MC_TS_PERSIST_FAILED: str = "MC time-series persist failed"
    ERR_MC_TS_UPLOAD_OK: str = "MC offline queue uploaded"
    ERR_MC_TS_UPLOAD_FAILED: str = "MC offline queue upload failed"
    ERR_MC_TS_OFFLINE_QUEUED: str = "MC data queued to offline storage"
    ERR_MC_OTA_INIT_FAILED: str = "MC OTA manager init failed"
    ERR_MC_CONFIG_VERSION_INIT_FAILED: str = "MC config version manager init failed"
    ERR_MC_AUDIT_INIT_FAILED: str = "MC audit init failed"
    ERR_MC_RBAC_DENIED: str = "MC RBAC permission denied"
    ERR_MC_CONFIG_ROLLBACK_OK: str = "MC config rollback completed"
    ERR_MC_CONFIG_ROLLBACK_FAILED: str = "MC config rollback failed"

    ERR_WEBHOOK_OFFLINE_BAD_QUALITY: str = "HTTP Webhook offline, point marked as bad quality"
    ERR_WEBHOOK_READ_TIMEOUT: str = "HTTP Webhook read timeout, no data available"
    ERR_WEBHOOK_WRITE_FAILED: str = "HTTP Webhook write/push failed"
    ERR_WEBHOOK_VALUE_OUT_OF_RANGE: str = "HTTP Webhook value out of clamp range"
    ERR_WEBHOOK_CONFIG_INVALID: str = "HTTP Webhook config invalid"
    ERR_WEBHOOK_CONN_LOST: str = "HTTP Webhook connection lost (offline timeout)"
    ERR_WEBHOOK_CONN_RECOVERED: str = "HTTP Webhook connection recovered"
    ERR_WEBHOOK_DEVICE_NOT_REGISTERED: str = "HTTP Webhook device not registered"
    ERR_WEBHOOK_DATA_PROCESS_FAILED: str = "HTTP Webhook data processing failed"
    ERR_WEBHOOK_HEALTH_SLOW: str = "HTTP Webhook health check slow response"
    ERR_WEBHOOK_HEALTH_TIMEOUT: str = "HTTP Webhook health check HEAD request timeout"
    ERR_WEBHOOK_HEALTH_SERVER_ERROR: str = "HTTP Webhook health check server error"
    ERR_WEBHOOK_HEALTH_FAILED: str = "HTTP Webhook health check failed"
    ERR_WEBHOOK_RATE_OF_CHANGE: str = "HTTP Webhook value rate of change exceeded"
    ERR_WEBHOOK_FROZEN_VALUE: str = "HTTP Webhook frozen value detected"
    ERR_WEBHOOK_NAN_INF: str = "HTTP Webhook NaN/Inf value rejected"
    ERR_WEBHOOK_PAYLOAD_PARSE_FAILED: str = "HTTP Webhook payload parse failed, fallback to raw string"
    ERR_WEBHOOK_WRITE_PAYLOAD_INVALID: str = "HTTP Webhook write payload validation failed"
    ERR_WEBHOOK_WRITE_CLIENT_ERROR: str = "HTTP Webhook write client error (4xx)"
    ERR_WEBHOOK_WRITE_RETRY: str = "HTTP Webhook write retrying"
    ERR_WEBHOOK_WRITE_SERVER_ERROR: str = "HTTP Webhook write server error (5xx)"

    ERR_AB_CONN_FAILED: str = "AB connection failed"
    ERR_AB_CONN_TIMEOUT: str = "AB connection timeout"
    ERR_AB_CONN_LOST: str = "AB connection lost"
    ERR_AB_CONN_RECOVERED: str = "AB connection recovered"
    ERR_AB_READ_FAILED: str = "AB read failed"
    ERR_AB_READ_TIMEOUT: str = "AB read timeout"
    ERR_AB_READ_BATCH_FAILED: str = "AB batch read failed"
    ERR_AB_WRITE_FAILED: str = "AB write failed"
    ERR_AB_WRITE_TIMEOUT: str = "AB write timeout"
    ERR_AB_DECODE_FAILED: str = "AB value decode failed"
    ERR_AB_CONFIG_INVALID: str = "AB config invalid"
    ERR_AB_RECONNECT_OK: str = "AB reconnected"
    ERR_AB_RECONNECT_FAILED: str = "AB reconnect failed"
    ERR_AB_CIRCUIT_OPEN: str = "AB circuit breaker open"
    ERR_AB_VALUE_OUT_OF_RANGE: str = "AB value out of clamp range"
    ERR_AB_OFFLINE_BAD_QUALITY: str = "AB offline, point marked bad quality"
    ERR_AB_CIP_SECURITY_FAILED: str = "AB CIP security setup failed"
    ERR_AB_WATCHDOG_FAILED: str = "AB watchdog check failed"
    ERR_AB_TAG_DISCOVERY_FAILED: str = "AB tag discovery failed"
    ERR_AB_STRUCT_BROWSE_FAILED: str = "AB struct browse failed"
    ERR_AB_DEGRADE_ACTIVE: str = "AB collection degraded"
    ERR_AB_DEGRADE_RECOVERED: str = "AB collection recovered"
    ERR_AB_FAILOVER_TRIGGERED: str = "AB link failover triggered (primary->backup)"
    ERR_AB_FAILOVER_NO_BACKUP: str = "AB failover failed: no backup link available"
    ERR_AB_FAILOVER_REVERT: str = "AB reverted to primary link"
    ERR_AB_FAILOVER_FAST: str = "AB failover exceeded 3s target duration"
    ERR_AB_NAN_INF: str = "AB NaN/Inf value detected"
    ERR_AB_FROZEN_VALUE: str = "AB frozen value detected"
    ERR_AB_RATE_OF_CHANGE: str = "AB rate of change exceeded threshold"
    ERR_AB_BATCH_RETRY: str = "AB batch read retry with reduced size"
    ERR_AB_WRITE_VERIFY_FAILED: str = "AB write verify failed (read-back mismatch)"
    ERR_AB_WRITE_RATE_LIMITED: str = "AB write rate limited"
    ERR_AB_WRITE_VALUE_INVALID: str = "AB write value out of valid range"
    ERR_AB_WRITE_AUDIT_OK: str = "AB write audit logged"
    ERR_AB_RULE_ENGINE_INIT_FAILED: str = "AB edge rule engine init failed"
    ERR_AB_RULE_ADD_FAILED: str = "AB edge rule add failed"
    ERR_AB_RULE_EVAL_FAILED: str = "AB edge rule eval failed"
    ERR_AB_RULE_RELOAD_OK: str = "AB edge rules reloaded"
    ERR_AB_TS_STORAGE_INIT_FAILED: str = "AB ts storage init failed"
    ERR_AB_TS_PERSIST_FAILED: str = "AB ts persist write failed"
    ERR_AB_CONFIG_VERSION_SAVED: str = "AB config version saved"
    ERR_AB_CONFIG_VERSION_ROLLBACK: str = "AB config version rollback"
    ERR_AB_CONFIG_CHANGE_DENIED: str = "AB config change denied by RBAC"
    ERR_AB_RBAC_DENIED: str = "AB RBAC permission denied"
    ERR_AB_OTA_CHECK: str = "AB OTA update check"
    ERR_AB_OTA_STARTED: str = "AB OTA started"
    ERR_AB_OTA_COMPLETED: str = "AB OTA completed"
    ERR_AB_OTA_FAILED: str = "AB OTA failed"
    ERR_AB_OTA_ROLLBACK: str = "AB OTA rollback"
    ERR_MODBUS_SLAVE_REG_OUT_OF_BOUNDS: str = "Modbus Slave register address out of bounds"
    ERR_MODBUS_SLAVE_POINT_UNDEFINED: str = "Modbus Slave point not defined"
    ERR_MODBUS_SLAVE_VALUE_OUT_OF_RANGE: str = "Modbus Slave value out of clamp range"
    ERR_MODBUS_SLAVE_SERVER_START_FAILED: str = "Modbus Slave server start failed"
    ERR_MODBUS_SLAVE_SERVER_STOP_FAILED: str = "Modbus Slave server stop failed"
    ERR_MODBUS_SLAVE_SYNC_FAILED: str = "Modbus Slave register sync to server failed"
    ERR_MODBUS_SLAVE_PYMODBUS_NOT_INSTALLED: str = "pymodbus not installed, run: pip install pymodbus>=3.0.0"
    ERR_MODBUS_SLAVE_DECODE_FAILED: str = "Modbus Slave value decode failed"
    ERR_MODBUS_SLAVE_CONFIG_INVALID: str = "Modbus Slave config invalid"
    ERR_MODBUS_SLAVE_CONN_REJECTED_MAX: str = "Modbus Slave connection rejected: max connections reached"
    ERR_MODBUS_SLAVE_CONN_REJECTED_WHITELIST: str = "Modbus Slave connection rejected: IP not in whitelist"
    ERR_MODBUS_SLAVE_CONN_REJECTED_BANNED: str = "Modbus Slave connection rejected: IP is banned"
    ERR_MODBUS_SLAVE_IP_BANNED: str = "Modbus Slave IP banned due to abuse"
    ERR_MODBUS_SLAVE_WRITE_READONLY: str = "Modbus Slave write to read-only register rejected"
    ERR_MODBUS_SLAVE_WRITE_VALUE_INVALID: str = "Modbus Slave write value out of valid range"
    ERR_SIM_READ_FAILED: str = "Simulator read failed"
    ERR_SIM_READ_TIMEOUT: str = "Simulator read timeout"
    ERR_SIM_WRITE_FAILED: str = "Simulator write failed"
    ERR_SIM_FAULT_TIMEOUT: str = "Simulator fault: timeout"
    ERR_SIM_FAULT_DISCONNECT: str = "Simulator fault: disconnect"
    ERR_SIM_FAULT_DATA_ERROR: str = "Simulator fault: data error"
    ERR_SIM_FAULT_RANDOM: str = "Simulator fault: random mixed"
    ERR_SIM_VALUE_OUT_OF_RANGE: str = "Simulator value out of clamp range"
    ERR_SIM_CONFIG_INVALID: str = "Simulator config invalid"
    ERR_SIM_FORMULA_EVAL_FAILED: str = "Simulator formula evaluation failed"

    ERR_OPCDA_CONN_FAILED: str = "OPC DA connection failed"
    ERR_OPCDA_CONN_TIMEOUT: str = "OPC DA connection timeout"
    ERR_OPCDA_CONN_LOST: str = "OPC DA connection lost"
    ERR_OPCDA_CONN_RECOVERED: str = "OPC DA connection recovered"
    ERR_OPCDA_READ_FAILED: str = "OPC DA read failed"
    ERR_OPCDA_READ_TIMEOUT: str = "OPC DA read timeout"
    ERR_OPCDA_WRITE_FAILED: str = "OPC DA write failed"
    ERR_OPCDA_WRITE_TIMEOUT: str = "OPC DA write timeout"
    ERR_OPCDA_CONFIG_INVALID: str = "OPC DA config invalid"
    ERR_OPCDA_RECONNECT_OK: str = "OPC DA reconnected"
    ERR_OPCDA_RECONNECT_FAILED: str = "OPC DA reconnect failed"
    ERR_OPCDA_QUALITY_BAD: str = "OPC DA variable quality is Bad"
    ERR_OPCDA_QUALITY_UNCERTAIN: str = "OPC DA variable quality is Uncertain"
    ERR_OPCDA_OFFLINE_BAD_QUALITY: str = "OPC DA offline, point marked bad quality"
    ERR_OPCDA_VALUE_OUT_OF_RANGE: str = "OPC DA value out of clamp range"
    ERR_OPCDA_DCOM_ACCESS_DENIED: str = "OPC DA DCOM access denied"
    ERR_OPCDA_DCOM_SERVER_UNAVAILABLE: str = "OPC DA DCOM server unavailable"
    ERR_OPCDA_DCOM_DISCONNECTED: str = "OPC DA DCOM object disconnected"
    ERR_OPCDA_DCOM_CALL_FAILED: str = "OPC DA DCOM call failed"
    ERR_OPCDA_SUBSCRIPTION_FAILED: str = "OPC DA subscription failed"
    ERR_OPCDA_BROWSE_FAILED: str = "OPC DA browse failed"
    ERR_OPCDA_IMPORT_ERROR: str = "OPC DA OpenOPC not installed"
    ERR_OPCDA_DCOM_CLASS_NOT_REGISTERED: str = "OPC DA DCOM class not registered, install OPC Core Components"
    ERR_OPCDA_DCOM_SERVER_BUSY: str = "OPC DA DCOM server busy, retry with backoff"
    ERR_OPCDA_RATE_OF_CHANGE_EXCEEDED: str = "OPC DA rate of change exceeded limit"
    ERR_OPCDA_FROZEN_VALUE_DETECTED: str = "OPC DA frozen value detected"
    ERR_OPCDA_WRITE_READ_ONLY: str = "OPC DA write to read-only item rejected"
    ERR_OPCDA_WRITE_TYPE_MISMATCH: str = "OPC DA write value type mismatch"
    ERR_VAI_MODEL_NOT_LOADED: str = "Video AI model not loaded"
    ERR_VAI_FRAME_CAPTURE_FAILED: str = "Video AI frame capture failed"
    ERR_VAI_INFERENCE_FAILED: str = "Video AI inference failed"
    ERR_VAI_INFERENCE_EMPTY: str = "Video AI inference output empty"
    ERR_VAI_ONNX_LOAD_FAILED: str = "Video AI ONNX model load failed"
    ERR_VAI_OFFLINE_BAD_QUALITY: str = "Video AI offline, point marked bad quality"
    ERR_VAI_CONFIG_INVALID: str = "Video AI config invalid"
    ERR_VAI_VALUE_OUT_OF_RANGE: str = "Video AI value out of clamp range"
    ERR_VAI_MODEL_HOT_RELOAD_OK: str = "Video AI model hot-reloaded successfully"
    ERR_VAI_MODEL_HOT_RELOAD_FAILED: str = "Video AI model hot-reload failed"
    ERR_VAI_MODEL_ROLLBACK_OK: str = "Video AI model rolled back to previous version"
    ERR_VAI_MODEL_VALIDATE_FAILED: str = "Video AI model validation failed"
    ERR_VAI_MODEL_VALIDATE_INPUT_MISMATCH: str = "Video AI model input dimensions mismatch"
    ERR_VAI_MODEL_VALIDATE_OUTPUT_MISMATCH: str = "Video AI model output dimensions mismatch"
    ERR_VAI_MODEL_VALIDATE_DTYPE_MISMATCH: str = "Video AI model data type mismatch"
    ERR_VAI_GPU_DEGRADED_TO_CPU: str = "Video AI GPU unavailable, degraded to CPU"
    ERR_VAI_INFERENCE_TIMEOUT: str = "Video AI inference timed out"
    ERR_VAI_PREPROCESS_FAILED: str = "Video AI image preprocessing failed"
    ERR_VAI_MODEL_PATH_TRAVERSAL: str = "Video AI model path traversal detected"
    ERR_VAI_MODEL_PATH_NOT_ALLOWED: str = "Video AI model path not in allowed directories"
    ERR_VAI_MODEL_FORMAT_INVALID: str = "Video AI model file format invalid, only .onnx allowed"
    ERR_VAI_MODEL_HEADER_INVALID: str = "Video AI model file header invalid"
    ERR_VAI_CONFIG_AUDIT_RECORDED: str = "Video AI config change audit recorded"
    ERR_ONVIF_START_FAILED: str = "ONVIF driver start failed"
    ERR_ONVIF_CAMERA_INIT_FAILED: str = "ONVIF camera init failed"
    ERR_ONVIF_DISCOVER_FAILED: str = "ONVIF device discovery failed"
    ERR_ONVIF_RTSP_FAILED: str = "ONVIF RTSP stream URI failed"
    ERR_ONVIF_PTZ_CONTINUOUS_FAILED: str = "ONVIF PTZ continuous move failed"
    ERR_ONVIF_PTZ_ABSOLUTE_FAILED: str = "ONVIF PTZ absolute move failed"
    ERR_ONVIF_PTZ_RELATIVE_FAILED: str = "ONVIF PTZ relative move failed"
    ERR_ONVIF_PTZ_STOP_FAILED: str = "ONVIF PTZ stop failed"
    ERR_ONVIF_PRESET_SET_FAILED: str = "ONVIF PTZ preset set failed"
    ERR_ONVIF_PRESET_GOTO_FAILED: str = "ONVIF PTZ preset goto failed"
    ERR_ONVIF_PRESET_REMOVE_FAILED: str = "ONVIF PTZ preset remove failed"
    ERR_ONVIF_PRESET_GET_FAILED: str = "ONVIF PTZ preset get failed"
    ERR_ONVIF_SNAPSHOT_URI_FAILED: str = "ONVIF snapshot URI failed"
    ERR_ONVIF_EVENT_SUBSCRIBE_FAILED: str = "ONVIF event subscribe failed"
    ERR_ONVIF_READ_FAILED: str = "ONVIF point read failed"
    ERR_ONVIF_WRITE_INVALID: str = "ONVIF write point invalid"
    ERR_ONVIF_WRITE_FAILED: str = "ONVIF write point failed"
    ERR_ONVIF_WATCHDOG_TRIGGER: str = "ONVIF watchdog triggered reconnect"
    ERR_ONVIF_RECONNECT_GIVEUP: str = "ONVIF reconnect give up"
    ERR_ONVIF_RECONNECT_FAILED: str = "ONVIF reconnect failed"
    ERR_ONVIF_AUTH_FAILED: str = "ONVIF authentication failed"
    ERR_ONVIF_NOT_SUPPORTED: str = "ONVIF action not supported"
    ERR_ONVIF_CONN_TIMEOUT: str = "ONVIF connection timeout"
    ERR_ONVIF_SOAP_ERROR: str = "ONVIF SOAP error"
    ERR_ONVIF_SNAPSHOT_DOWNLOAD_FAILED: str = "ONVIF snapshot download failed"
    ERR_ONVIF_PTZ_OUT_OF_RANGE: str = "ONVIF PTZ value out of range"
    ERR_ONVIF_PTZ_RATE_LIMITED: str = "ONVIF PTZ rate limited, min interval 500ms"
    ERR_ONVIF_PRESET_NOT_FOUND: str = "ONVIF preset not found"


# English translations
ENGLISH_STRINGS = TranslationStrings()


# Chinese translations
ZH_CHINESE_STRINGS = TranslationStrings(
    # System
    APP_NAME="EdgeLiteGateway",
    VERSION="版本",

    # Device status
    STATUS_ONLINE="在线",
    STATUS_OFFLINE="离线",
    STATUS_DEGRADED="降级",

    # Alarm severity
    SEVERITY_CRITICAL="紧急",
    SEVERITY_MAJOR="重要",
    SEVERITY_MINOR="次要",
    SEVERITY_WARNING="警告",
    SEVERITY_INFO="信息",

    # Alarm actions
    ACTION_FIRING="告警触发",
    ACTION_RECOVERED="告警恢复",
    ACTION_ACKNOWLEDGED="告警确认",
    ACTION_ESCALATED="告警升级",

    # Common
    DEVICE="设备",
    RULE="规则",
    ALARM="告警",
    USER="用户",
    SETTINGS="设置",
    SAVE="保存",
    CANCEL="取消",
    DELETE="删除",
    EDIT="编辑",
    CREATE="创建",
    SEARCH="搜索",
    REFRESH="刷新",
    EXPORT="导出",
    IMPORT="导入",

    # Error messages
    ERR_CONNECTION_FAILED="连接失败",
    ERR_TIMEOUT="请求超时",
    ERR_AUTH_FAILED="认证失败",
    ERR_PERMISSION_DENIED="权限不足",
    ERR_NOT_FOUND="资源未找到",
    ERR_VALIDATION_FAILED="验证失败",
    ERR_INTERNAL_ERROR="服务器内部错误",

    # Device management
    DEVICE_ID="设备ID",
    DEVICE_NAME="设备名称",
    PROTOCOL="协议",
    COLLECT_INTERVAL="采集间隔",
    POINTS_COUNT="测点数量",
    ADD_DEVICE="添加设备",
    DELETE_DEVICE="删除设备",
    DEVICE_TEMPLATES="设备模板",
    DEVICE_GROUPS="设备分组",

    # Rule management
    RULE_ID="规则ID",
    RULE_NAME="规则名称",
    CONDITIONS="条件",
    SEVERITY="严重程度",
    DURATION="持续时间",
    ADD_RULE="添加规则",
    DELETE_RULE="删除规则",
    RULE_ENABLED="已启用",
    RULE_DISABLED="已禁用",

    # Alarm management
    ALARM_ID="告警ID",
    TRIGGER_VALUE="触发值",
    FIRED_AT="触发时间",
    RECOVERED_AT="恢复时间",
    ACKNOWLEDGED_BY="确认人",
    ACK_ALARM="确认告警",
    CLEAR_ALARM="清除告警",
    ALARM_STATISTICS="告警统计",
    MTTR="平均恢复时间",
    MTBF="平均故障间隔时间",
    ALARM_COUNT="告警数量",
    ALARM_TREND="告警趋势",

    # Notification channels
    NOTIFICATION_CHANNELS="通知渠道",
    DINGTALK="钉钉",
    WECOM="企业微信",
    EMAIL="邮件",
    WEBHOOK="Webhook",
    CHANNEL_ENABLED="渠道已启用",
    CHANNEL_DISABLED="渠道已禁用",
    TEST_CHANNEL="测试渠道",
    CONFIGURE_CHANNEL="配置渠道",

    # System management
    SYSTEM_CONFIG="系统配置",
    BACKUP_CONFIG="备份配置",
    RESTORE_CONFIG="恢复配置",
    LOG_SETTINGS="日志设置",
    LOG_LEVEL="日志级别",
    LOG_ROTATION="日志轮转",
    MAX_LOG_SIZE="最大日志大小",
    BACKUP_COUNT="备份数量",

    # Data management
    DATA_QUERY="数据查询",
    TIME_RANGE="时间范围",
    AGGREGATION="聚合",
    EXPORT_DATA="导出数据",
    IMPORT_DATA="导入数据",

    # Command control
    COMMAND_CONTROL="指令控制",
    SEND_COMMAND="发送指令",
    COMMAND_HISTORY="指令历史",
    APPROVAL_REQUIRED="需要审批",
    PENDING_APPROVAL="待审批",

    ERR_MODBUS_CONN_FAILED="Modbus TCP连接失败",
    ERR_MODBUS_CONN_TIMEOUT="Modbus TCP连接超时",
    ERR_MODBUS_READ_FAILED="Modbus TCP读取失败",
    ERR_MODBUS_READ_TIMEOUT="Modbus TCP读取超时",
    ERR_MODBUS_READ_EXCEPTION="Modbus TCP协议异常",
    ERR_MODBUS_WRITE_FAILED="Modbus TCP写入失败",
    ERR_MODBUS_WRITE_TIMEOUT="Modbus TCP写入超时",
    ERR_MODBUS_BCAST_WRITE_FAILED="Modbus TCP广播写入失败",
    ERR_MODBUS_VALUE_OUT_OF_RANGE="数值超出限幅范围",
    ERR_MODBUS_DECODE_FAILED="Modbus TCP数值解码失败",
    ERR_MODBUS_CONFIG_INVALID="Modbus TCP配置无效",
    ERR_MODBUS_RECONNECT_OK="Modbus TCP重连成功",
    ERR_MODBUS_NAN_INF="检测到NaN/Inf异常值",
    ERR_MODBUS_RATE_OF_CHANGE="变化率超限",
    ERR_MODBUS_FROZEN_VALUE="检测到冻结值",
    ERR_MODBUS_WRITE_VERIFY_FAILED="写验证失败(回读不一致)",
    ERR_MODBUS_WRITE_RATE_LIMITED="写速率受限",
    ERR_MODBUS_FAILOVER_TRIGGERED="链路故障切换(主→备)",
    ERR_MODBUS_FAILOVER_NO_BACKUP="故障切换失败:无备用链路",
    ERR_MODBUS_AUTO_REVERT_OK="自动回切到主链路",
    ERR_MODBUS_RULE_EVAL_FAILED="边缘规则评估失败",
    ERR_MODBUS_RULE_ACTION_FAILED="边缘规则动作执行失败",
    ERR_MODBUS_RULE_HOT_RELOAD_OK="边缘规则热加载成功",
    ERR_MODBUS_RULE_ROLLBACK_OK="边缘规则回滚成功",
    ERR_MODBUS_TS_WRITE_FAILED="时序数据写入失败",
    ERR_MODBUS_TS_QUERY_FAILED="时序数据查询失败",
    ERR_MODBUS_OFFLINE_SYNC_FAILED="离线同步上传失败",
    ERR_MODBUS_OFFLINE_SYNC_OK="离线同步完成",
    ERR_MODBUS_CONFIG_VERSION_SAVE_OK="配置版本保存成功",
    ERR_MODBUS_CONFIG_VERSION_ROLLBACK_OK="配置版本回滚成功",
    ERR_MODBUS_AUDIT_LOG_OK="审计日志已记录",
    ERR_S7_CONN_FAILED="S7连接失败",
    ERR_S7_CONN_TIMEOUT="S7连接超时",
    ERR_S7_READ_FAILED="S7读取失败",
    ERR_S7_READ_TIMEOUT="S7读取超时",
    ERR_S7_READ_EXCEPTION="S7协议异常",
    ERR_S7_WRITE_FAILED="S7写入失败",
    ERR_S7_WRITE_TIMEOUT="S7写入超时",
    ERR_S7_DECODE_FAILED="S7数值解码失败",
    ERR_S7_CONFIG_INVALID="S7配置无效",
    ERR_S7_RECONNECT_OK="S7重连成功",
    ERR_S7_RECONNECT_FAILED="S7重连失败",
    ERR_S7_PASSWORD_FAILED="S7密码协商失败",
    ERR_S7_PDU_FAILED="S7 PDU协商失败",
    ERR_S7_CIRCUIT_OPEN="S7熔断器打开",
    ERR_S7_CONN_LOST="S7连接丢失",
    ERR_S7_CONN_RECOVERED="S7连接恢复",
    ERR_S7_VALUE_OUT_OF_RANGE="S7数值超出限幅范围",
    ERR_S7_AUTH_LOCKED="S7认证锁定(密码错误次数过多)",
    ERR_S7_FAILOVER_TRIGGERED="S7链路故障切换(主→备)",
    ERR_S7_FAILOVER_NO_BACKUP="S7故障切换失败:无备用链路",
    ERR_S7_REDUNDANCY_SWITCH="S7冗余链路切换",
    ERR_S7_REDUNDANCY_REVERT="S7冗余自动回切至主链路",
    ERR_S7_REDUNDANCY_PROBE_OK="S7主链路探测成功",
    ERR_S7_REDUNDANCY_PROBE_FAIL="S7主链路探测失败",
    ERR_S7_PDU_NEGOTIATING="S7 PDU协商进行中",
    ERR_S7_RATE_OF_CHANGE="S7变化率超限",
    ERR_S7_FROZEN_VALUE="S7检测到冻结值",
    ERR_S7_NAN_INF="S7检测到NaN/Inf异常值",
    ERR_S7_DEGRADE_ACTIVE="S7采集降级",
    ERR_S7_DEGRADE_RECOVERED="S7采集恢复",
    ERR_S7_BATCH_RETRY="S7批量读取减小重试",
    ERR_S7_WRITE_VERIFY_FAILED="S7写验证失败(回读不一致)",
    ERR_S7_WRITE_RATE_LIMITED="S7写速率受限",
    ERR_S7_WRITE_VALUE_INVALID="S7写入值超出有效范围",
    ERR_S7_EDGE_RULE_FIRED="S7边缘规则触发(告警)",
    ERR_S7_EDGE_RULE_RECOVERED="S7边缘规则恢复",
    ERR_S7_EDGE_RULE_ERROR="S7边缘规则评估错误",
    ERR_S7_EDGE_TRIGGER_EXECUTED="S7边缘触发器已执行",
    ERR_S7_EDGE_TRIGGER_FAILED="S7边缘触发器执行失败",
    ERR_S7_TS_WRITE_FAILED="S7时序数据写入失败",
    ERR_S7_TS_QUERY_FAILED="S7时序数据查询失败",
    ERR_S7_TS_CLEANUP_OK="S7时序数据清理完成",
    ERR_S7_TS_CLEANUP_FAILED="S7时序数据清理失败",
    ERR_S7_TS_NOT_STARTED="S7时序存储未启动",
    ERR_S7_OFFLINE_ENQUEUE_FAILED="S7离线队列入队失败",
    ERR_S7_OFFLINE_UPLOAD_OK="S7离线数据上传成功",
    ERR_S7_OFFLINE_UPLOAD_FAILED="S7离线数据上传失败",
    ERR_S7_OFFLINE_SYNC_RESTORD="S7离线同步恢复(网络恢复)",
    ERR_S7_OFFLINE_COMPRESS_FAILED="S7离线批次压缩失败",
    ERR_S7_CONFIG_VERSION_SAVE_OK="S7配置版本保存成功",
    ERR_S7_CONFIG_VERSION_SAVE_FAILED="S7配置版本保存失败",
    ERR_S7_CONFIG_VERSION_ROLLBACK_OK="S7配置版本回滚完成",
    ERR_S7_CONFIG_VERSION_ROLLBACK_FAILED="S7配置版本回滚失败",
    ERR_S7_CONFIG_VERSION_NOT_FOUND="S7配置版本未找到",
    ERR_S7_OTA_START_OK="S7 OTA升级已启动",
    ERR_S7_OTA_START_FAILED="S7 OTA升级启动失败",
    ERR_S7_OTA_VERIFY_FAILED="S7 OTA固件校验失败",
    ERR_S7_OTA_ROLLBACK_OK="S7 OTA回滚完成",
    ERR_S7_OTA_ROLLBACK_FAILED="S7 OTA回滚失败",
    ERR_S7_OTA_IN_PROGRESS="S7 OTA升级正在进行中",
    ERR_MQTT_CONN_TIMEOUT="MQTT连接超时",
    ERR_MQTT_CONN_FAILED="MQTT连接失败",
    ERR_MQTT_CONN_LOST="MQTT连接中断",
    ERR_MQTT_AUTH_FAILED="MQTT认证失败",
    ERR_MQTT_TLS_ERROR="MQTT TLS配置失败",
    ERR_MQTT_SUBSCRIBE_FAILED="MQTT订阅失败",
    ERR_MQTT_PUBLISH_FAILED="MQTT发布失败",
    ERR_MQTT_MESSAGE_PARSE_ERROR="MQTT消息解析失败",
    ERR_MQTT_QUEUE_FULL="MQTT发布队列满",
    ERR_MQTT_OFFLINE_BAD_QUALITY="MQTT离线，测点标记为坏质量",

    ERR_OPCUA_CONN_FAILED="OPC UA连接失败",
    ERR_OPCUA_CONN_TIMEOUT="OPC UA连接超时",
    ERR_OPCUA_READ_FAILED="OPC UA读取失败",
    ERR_OPCUA_READ_TIMEOUT="OPC UA读取超时",
    ERR_OPCUA_WRITE_FAILED="OPC UA写入失败",
    ERR_OPCUA_WRITE_TIMEOUT="OPC UA写入超时",
    ERR_OPCUA_SESSION_EXPIRED="OPC UA会话过期",
    ERR_OPCUA_CERT_EXPIRED="OPC UA证书已过期",
    ERR_OPCUA_CERT_EXPIRING="OPC UA证书即将过期",
    ERR_OPCUA_SUBSCRIPTION_FAILED="OPC UA订阅失败",
    ERR_OPCUA_BATCH_READ_FAILED="OPC UA批量读取失败",
    ERR_OPCUA_BATCH_READ_TIMEOUT="OPC UA批量读取超时",
    ERR_OPCUA_BATCH_WRITE_FAILED="OPC UA批量写入失败",
    ERR_OPCUA_BATCH_WRITE_TIMEOUT="OPC UA批量写入超时",
    ERR_OPCUA_CALLBACK_ERROR="OPC UA订阅回调异常",
    ERR_OPCUA_RECONNECTING="OPC UA正在重连",
    ERR_OPCUA_SECURITY_SKIP="OPC UA安全策略跳过(缺少证书)",
    ERR_OPCUA_VALUE_OUT_OF_RANGE="OPC UA数值超出限幅范围",
    ERR_OPCUA_QUALITY_BAD="OPC UA变量质量为坏",
    ERR_OPCUA_QUALITY_UNCERTAIN="OPC UA变量质量为不确定",
    ERR_OPCUA_OFFLINE_BAD_QUALITY="OPC UA离线，测点标记为坏质量",
    ERR_OPCUA_FAILOVER_TRIGGERED="OPC UA故障切换(主→备)",
    ERR_OPCUA_FAILOVER_NO_BACKUP="OPC UA故障切换失败:无备用端点",
    ERR_OPCUA_FAILOVER_REVERT="OPC UA回切至主端点",
    ERR_OPCUA_CERT_AUTO_RENEW_OK="OPC UA自签名证书自动续期成功",
    ERR_OPCUA_CERT_AUTO_RENEW_FAILED="OPC UA证书自动续期失败",
    ERR_OPCUA_SECURITY_DEGRADED="OPC UA安全降级至None策略",
    ERR_OPCUA_SESSION_PRE_EXPIRY_REBUILD="OPC UA会话过期前主动重建",
    ERR_OPCUA_KEEPALIVE_FAILED="OPC UA保活失败",
    ERR_OPCUA_STATE_TRANSITION="OPC UA连接状态转换",
    ERR_OPCUA_RATE_OF_CHANGE="OPC UA变化率超限",
    ERR_OPCUA_FROZEN_VALUE="OPC UA检测到冻结值",
    ERR_OPCUA_NAN_INF="OPC UA检测到NaN/Inf异常值",
    ERR_OPCUA_COMPLEX_TYPE_HEX_FALLBACK="OPC UA复杂类型回退为十六进制",
    ERR_OPCUA_COMPLEX_TYPE_UNKNOWN="OPC UA未知类型无法解析",
    ERR_OPCUA_ARRAY_TRUNCATED="OPC UA数组截断",
    ERR_OPCUA_SUB_DEGRADED_POLLING="OPC UA订阅降级为轮询",
    ERR_OPCUA_SUB_RECOVERED="OPC UA订阅恢复",
    ERR_OPCUA_STALE_DATA="OPC UA检测到陈旧数据",
    ERR_OPCUA_DEADBAND_NATIVE_FAILED="OPC UA原生死区失败,回退软件死区",
    ERR_OPCUA_WRITE_VERIFY_FAILED="OPC UA写验证失败(回读不一致)",
    ERR_OPCUA_WRITE_TYPE_MISMATCH="OPC UA写入类型不匹配",
    ERR_OPCUA_WRITE_RATE_LIMITED="OPC UA写速率受限",
    ERR_OPCUA_WRITE_VALUE_INVALID="OPC UA写入值无效",
    ERR_OPCUA_WRITE_AUDIT_OK="OPC UA写操作审计已记录",
    ERR_OPCUA_FAILOVER_FAST_SWITCH="OPC UA快速故障转移到备Endpoint",
    ERR_OPCUA_SESSION_RESTORED="OPC UA会话从持久化状态恢复",
    ERR_OPCUA_SESSION_PERSIST_FAILED="OPC UA会话持久化失败",
    ERR_OPCUA_CERT_FAILOVER_TRIGGERED="OPC UA切换到备用证书",
    ERR_OPCUA_CERT_FAILOVER_REVERT="OPC UA恢复到主证书",
    ERR_OPCUA_RULE_EVALUATED="OPC UA边缘规则已评估",
    ERR_OPCUA_RULE_TRIGGERED="OPC UA边缘规则触发报警",
    ERR_OPCUA_RULE_HOT_RELOADED="OPC UA边缘规则热加载",
    ERR_OPCUA_CROSS_SERVER_WRITE="OPC UA跨服务器写入触发",
    ERR_OPCUA_DATA_PERSIST_LOCAL="OPC UA数据已持久化到本地时序存储",
    ERR_OPCUA_DATA_SYNC_RECOVERED="OPC UA网络恢复，数据同步已启用",
    ERR_OPCUA_DATA_COMPRESS_UPLOAD="OPC UA压缩数据批量上传",
    ERR_OPCUA_CONFIG_VERSION_SAVE_OK="OPC UA配置版本已保存",
    ERR_OPCUA_CONFIG_VERSION_ROLLBACK_OK="OPC UA配置版本已回滚",
    ERR_OPCUA_OTA_START_OK="OPC UA OTA升级启动成功",
    ERR_OPCUA_OTA_START_FAILED="OPC UA OTA升级启动失败",
    ERR_OPCUA_OTA_VERIFY_FAILED="OPC UA OTA校验失败",
    ERR_OPCUA_OTA_ROLLBACK_OK="OPC UA OTA回滚成功",
    ERR_OPCUA_OTA_ROLLBACK_FAILED="OPC UA OTA回滚失败",
    ERR_OPCUA_OTA_IN_PROGRESS="OPC UA OTA升级正在进行",
    ERR_OPCUA_RBAC_DENIED="OPC UA RBAC权限被拒绝",
    ERR_OPCUA_AUDIT_LOG_OK="OPC UA审计日志已记录",

    ERR_FINS_CONN_FAILED="FINS连接失败",
    ERR_FINS_CONN_TIMEOUT="FINS连接超时",
    ERR_FINS_READ_FAILED="FINS读取失败",
    ERR_FINS_READ_TIMEOUT="FINS读取超时",
    ERR_FINS_WRITE_FAILED="FINS写入失败",
    ERR_FINS_WRITE_TIMEOUT="FINS写入超时",
    ERR_FINS_DECODE_FAILED="FINS数值解码失败",
    ERR_FINS_CONFIG_INVALID="FINS配置无效",
    ERR_FINS_RECONNECT_OK="FINS重连成功",
    ERR_FINS_RECONNECT_FAILED="FINS重连失败",
    ERR_FINS_CONN_LOST="FINS连接丢失",
    ERR_FINS_CONN_RECOVERED="FINS连接恢复",
    ERR_FINS_VALUE_OUT_OF_RANGE="FINS数值超出限幅范围",
    ERR_FINS_OFFLINE_BAD_QUALITY="FINS离线，测点标记为坏质量",
    ERR_FINS_DIRECT_MODE_FALLBACK="FINS直接模式回退至标准读取",
    ERR_FINS_DISCOVER_FAILED="FINS设备发现失败",
    ERR_FINS_FAILOVER_TRIGGERED="FINS链路故障切换(主→备)",
    ERR_FINS_FAILOVER_NO_BACKUP="FINS故障切换失败:无备用链路",
    ERR_FINS_FAILOVER_REVERT="FINS回切至主链路",
    ERR_FINS_NODE_INIT_FAILED="FINS节点地址初始化失败",
    ERR_FINS_NODE_INIT_OK="FINS节点地址初始化成功",
    ERR_FINS_ILLEGAL_AREA="FINS非法内存区域",
    ERR_FINS_ILLEGAL_ADDRESS="FINS非法地址偏移",
    ERR_FINS_ILLEGAL_DATA="FINS非法数据长度或类型",
    ERR_FINS_FROZEN_VALUE="FINS冻结值检测",
    ERR_FINS_NAN_INF="FINS NaN/Inf值已过滤",
    ERR_FINS_RATE_OF_CHANGE="FINS变化率超限",
    ERR_FINS_BATCH_SPLIT="FINS批量读取拆分为更小批次",
    ERR_FINS_DEGRADED_FREQ="FINS采集频率降级",
    ERR_FINS_WRITE_VALUE_OUT_OF_RANGE="FINS写入值超范围",
    ERR_FINS_WRITE_RATE_LIMITED="FINS写入速率受限",
    ERR_FINS_WRITE_VERIFY_FAILED="FINS写后回读验证失败",
    ERR_FINS_WRITE_REJECTED="FINS写入被PLC拒绝",
    ERR_FINS_WRITE_PROTECTED_AREA="FINS写入受保护区域",
    ERR_FINS_WRITE_MERGE="FINS批量写合并相邻地址",
    ERR_FINS_FAILOVER_FAST="FINS快速故障切换至备IP",
    ERR_FINS_STANDBY_READY="FINS备用连接就绪",
    ERR_FINS_STANDBY_FAILED="FINS备用连接失败",
    ERR_FINS_STANDBY_TAKEOVER="FINS备用接管(即时切换)",
    ERR_FINS_EDGE_RULE_FIRED="FINS边缘规则触发(报警)",
    ERR_FINS_EDGE_RULE_RECOVERED="FINS边缘规则恢复",
    ERR_FINS_EDGE_RULE_ERROR="FINS边缘规则评估错误",
    ERR_FINS_EDGE_TRIGGER_EXECUTED="FINS边缘触发器已执行",
    ERR_FINS_EDGE_TRIGGER_FAILED="FINS边缘触发器执行失败",
    ERR_FINS_RULE_HOT_RELOADED="FINS边缘规则热加载",
    ERR_FINS_RULE_ROLLBACK_OK="FINS边缘规则回滚成功",
    ERR_FINS_TS_STORE_WRITE="FINS时序数据本地存储",
    ERR_FINS_TS_STORE_OFFLINE="FINS离线队列已入队",
    ERR_FINS_TS_SYNC_RESTORED="FINS网络恢复后离线数据同步",
    ERR_FINS_TS_SYNC_FAILED="FINS离线数据同步失败",
    ERR_FINS_TS_COMPRESS_UPLOAD="FINS压缩批量上传",
    ERR_FINS_CONFIG_VERSION_SAVED="FINS配置版本已保存",
    ERR_FINS_CONFIG_VERSION_ROLLBACK="FINS配置版本已回滚",
    ERR_FINS_CONFIG_CHANGE_DENIED="FINS配置变更被拒绝(RBAC)",
    ERR_FINS_RBAC_DENIED="FINS RBAC权限被拒绝",
    ERR_FINS_AUDIT_LOGGED="FINS审计事件已记录",
    ERR_FINS_OTA_CHECK="FINS OTA更新检查",
    ERR_FINS_OTA_STARTED="FINS OTA升级已启动",
    ERR_FINS_OTA_COMPLETED="FINS OTA升级完成",
    ERR_FINS_OTA_FAILED="FINS OTA升级失败",
    ERR_FINS_OTA_ROLLBACK="FINS OTA回滚",

    ERR_MC_CONN_FAILED="MC连接失败",
    ERR_MC_CONN_TIMEOUT="MC连接超时",
    ERR_MC_CONN_LOST="MC连接丢失",
    ERR_MC_CONN_RECOVERED="MC连接恢复",
    ERR_MC_READ_FAILED="MC读取失败",
    ERR_MC_READ_TIMEOUT="MC读取超时",
    ERR_MC_READ_EXCEPTION="MC协议异常",
    ERR_MC_WRITE_FAILED="MC写入失败",
    ERR_MC_WRITE_TIMEOUT="MC写入超时",
    ERR_MC_DECODE_FAILED="MC数值解码失败",
    ERR_MC_CONFIG_INVALID="MC配置无效",
    ERR_MC_RECONNECT_OK="MC重连成功",
    ERR_MC_RECONNECT_FAILED="MC重连失败",
    ERR_MC_VALUE_OUT_OF_RANGE="MC数值超出限幅范围",
    ERR_MC_OFFLINE_BAD_QUALITY="MC离线，测点标记为坏质量",
    ERR_MC_SLMP_DIRECT_FALLBACK="MC SLMP直接模式回退至标准读取",
    ERR_MC_DISCOVER_FAILED="MC设备发现失败",
    ERR_MC_FAILOVER_TRIGGERED="MC链路故障切换(主→备)",
    ERR_MC_FAILOVER_REVERT="MC回切至主IP",
    ERR_MC_FAILOVER_NO_BACKUP="MC故障切换失败:无备用IP",
    ERR_MC_RATE_OF_CHANGE="MC变化率超限",
    ERR_MC_FROZEN_VALUE="MC检测到冻结值",
    ERR_MC_NAN_INF="MC检测到NaN/Inf异常值",
    ERR_MC_BATCH_RETRY="MC批量读取减小重试",
    ERR_MC_DEGRADE_ACTIVE="MC采集降级",
    ERR_MC_DEGRADE_RECOVERED="MC采集恢复",
    ERR_MC_WRITE_VERIFY_FAILED="MC写验证失败(回读不一致)",
    ERR_MC_WRITE_RATE_LIMITED="MC写速率受限",
    ERR_MC_WRITE_VALUE_INVALID="MC写入值超出有效范围",
    ERR_MC_WRITE_AUDIT_OK="MC写操作审计已记录",
    ERR_MC_FAILOVER_FAST="MC故障切换超过3秒目标时长",
    ERR_MC_RULE_ENGINE_INIT_FAILED="MC边缘规则引擎初始化失败",
    ERR_MC_RULE_EVAL_FAILED="MC边缘规则评估失败",
    ERR_MC_RULE_ADD_FAILED="MC边缘规则添加失败",
    ERR_MC_RULE_RELOAD_OK="MC边缘规则已热加载",
    ERR_MC_RULE_RELOAD_FAILED="MC边缘规则热加载失败",
    ERR_MC_TRIGGER_WRITE_FAILED="MC触发器写入PLC失败",
    ERR_MC_TS_STORAGE_INIT_FAILED="MC时序存储初始化失败",
    ERR_MC_TS_PERSIST_FAILED="MC时序数据持久化失败",
    ERR_MC_TS_UPLOAD_OK="MC离线队列已上传",
    ERR_MC_TS_UPLOAD_FAILED="MC离线队列上传失败",
    ERR_MC_TS_OFFLINE_QUEUED="MC数据已入离线队列",
    ERR_MC_OTA_INIT_FAILED="MC OTA管理器初始化失败",
    ERR_MC_CONFIG_VERSION_INIT_FAILED="MC配置版本管理器初始化失败",
    ERR_MC_AUDIT_INIT_FAILED="MC审计初始化失败",
    ERR_MC_RBAC_DENIED="MC RBAC权限拒绝",
    ERR_MC_CONFIG_ROLLBACK_OK="MC配置回滚完成",
    ERR_MC_CONFIG_ROLLBACK_FAILED="MC配置回滚失败",

    ERR_WEBHOOK_OFFLINE_BAD_QUALITY="HTTP Webhook离线，测点标记为坏质量",
    ERR_WEBHOOK_READ_TIMEOUT="HTTP Webhook读取超时，无数据可用",
    ERR_WEBHOOK_WRITE_FAILED="HTTP Webhook写入/推送失败",
    ERR_WEBHOOK_VALUE_OUT_OF_RANGE="HTTP Webhook数值超出限幅范围",
    ERR_WEBHOOK_CONFIG_INVALID="HTTP Webhook配置无效",
    ERR_WEBHOOK_CONN_LOST="HTTP Webhook连接丢失（离线超时）",
    ERR_WEBHOOK_CONN_RECOVERED="HTTP Webhook连接恢复",
    ERR_WEBHOOK_DEVICE_NOT_REGISTERED="HTTP Webhook设备未注册",
    ERR_WEBHOOK_DATA_PROCESS_FAILED="HTTP Webhook数据处理失败",
    ERR_WEBHOOK_HEALTH_SLOW="HTTP Webhook健康检查响应缓慢",
    ERR_WEBHOOK_HEALTH_TIMEOUT="HTTP Webhook健康检查HEAD请求超时",
    ERR_WEBHOOK_HEALTH_SERVER_ERROR="HTTP Webhook健康检查服务器错误",
    ERR_WEBHOOK_HEALTH_FAILED="HTTP Webhook健康检查失败",
    ERR_WEBHOOK_RATE_OF_CHANGE="HTTP Webhook数值变化率超限",
    ERR_WEBHOOK_FROZEN_VALUE="HTTP Webhook冻结值检测",
    ERR_WEBHOOK_NAN_INF="HTTP Webhook NaN/Inf值已拒绝",
    ERR_WEBHOOK_PAYLOAD_PARSE_FAILED="HTTP Webhook载荷解析失败，回退原始字符串",
    ERR_WEBHOOK_WRITE_PAYLOAD_INVALID="HTTP Webhook写入载荷验证失败",
    ERR_WEBHOOK_WRITE_CLIENT_ERROR="HTTP Webhook写入客户端错误（4xx）",
    ERR_WEBHOOK_WRITE_RETRY="HTTP Webhook写入重试中",
    ERR_WEBHOOK_WRITE_SERVER_ERROR="HTTP Webhook写入服务器错误（5xx）",

    ERR_AB_CONN_FAILED="AB连接失败",
    ERR_AB_CONN_TIMEOUT="AB连接超时",
    ERR_AB_CONN_LOST="AB连接丢失",
    ERR_AB_CONN_RECOVERED="AB连接恢复",
    ERR_AB_READ_FAILED="AB读取失败",
    ERR_AB_READ_TIMEOUT="AB读取超时",
    ERR_AB_READ_BATCH_FAILED="AB批量读取失败",
    ERR_AB_WRITE_FAILED="AB写入失败",
    ERR_AB_WRITE_TIMEOUT="AB写入超时",
    ERR_AB_DECODE_FAILED="AB数值解码失败",
    ERR_AB_CONFIG_INVALID="AB配置无效",
    ERR_AB_RECONNECT_OK="AB重连成功",
    ERR_AB_RECONNECT_FAILED="AB重连失败",
    ERR_AB_CIRCUIT_OPEN="AB熔断器打开",
    ERR_AB_VALUE_OUT_OF_RANGE="AB数值超出限幅范围",
    ERR_AB_OFFLINE_BAD_QUALITY="AB离线，测点标记为坏质量",
    ERR_AB_CIP_SECURITY_FAILED="AB CIP安全设置失败",
    ERR_AB_WATCHDOG_FAILED="AB看门狗检查失败",
    ERR_AB_TAG_DISCOVERY_FAILED="AB标签发现失败",
    ERR_AB_STRUCT_BROWSE_FAILED="AB结构体浏览失败",
    ERR_AB_DEGRADE_ACTIVE="AB采集降级",
    ERR_AB_DEGRADE_RECOVERED="AB采集恢复",
    ERR_AB_FAILOVER_TRIGGERED="AB链路故障切换(主→备)",
    ERR_AB_FAILOVER_NO_BACKUP="AB故障切换失败:无备用链路",
    ERR_AB_FAILOVER_REVERT="AB回切至主链路",
    ERR_AB_FAILOVER_FAST="AB故障切换超过3秒目标时长",
    ERR_AB_NAN_INF="AB检测到NaN/Inf异常值",
    ERR_AB_FROZEN_VALUE="AB检测到冻结值",
    ERR_AB_RATE_OF_CHANGE="AB变化率超限",
    ERR_AB_BATCH_RETRY="AB批量读取减小重试",
    ERR_AB_WRITE_VERIFY_FAILED="AB写验证失败(回读不一致)",
    ERR_AB_WRITE_RATE_LIMITED="AB写速率受限",
    ERR_AB_WRITE_VALUE_INVALID="AB写入值超出有效范围",
    ERR_AB_WRITE_AUDIT_OK="AB写操作审计已记录",
    ERR_AB_RULE_ENGINE_INIT_FAILED="AB边缘规则引擎初始化失败",
    ERR_AB_RULE_ADD_FAILED="AB边缘规则添加失败",
    ERR_AB_RULE_EVAL_FAILED="AB边缘规则评估失败",
    ERR_AB_RULE_RELOAD_OK="AB边缘规则已热加载",
    ERR_AB_TS_STORAGE_INIT_FAILED="AB时序存储初始化失败",
    ERR_AB_TS_PERSIST_FAILED="AB时序持久化写入失败",
    ERR_AB_CONFIG_VERSION_SAVED="AB配置版本已保存",
    ERR_AB_CONFIG_VERSION_ROLLBACK="AB配置版本已回滚",
    ERR_AB_CONFIG_CHANGE_DENIED="AB配置变更被RBAC拒绝",
    ERR_AB_RBAC_DENIED="AB RBAC权限拒绝",
    ERR_AB_OTA_CHECK="AB OTA更新检查",
    ERR_AB_OTA_STARTED="AB OTA已启动",
    ERR_AB_OTA_COMPLETED="AB OTA已完成",
    ERR_AB_OTA_FAILED="AB OTA失败",
    ERR_AB_OTA_ROLLBACK="AB OTA已回滚",
    ERR_MODBUS_SLAVE_REG_OUT_OF_BOUNDS="Modbus Slave寄存器地址越界",
    ERR_MODBUS_SLAVE_POINT_UNDEFINED="Modbus Slave测点未定义",
    ERR_MODBUS_SLAVE_VALUE_OUT_OF_RANGE="Modbus Slave数值超出限幅范围",
    ERR_MODBUS_SLAVE_SERVER_START_FAILED="Modbus Slave服务启动失败",
    ERR_MODBUS_SLAVE_SERVER_STOP_FAILED="Modbus Slave服务停止失败",
    ERR_MODBUS_SLAVE_SYNC_FAILED="Modbus Slave寄存器同步到服务器失败",
    ERR_MODBUS_SLAVE_PYMODBUS_NOT_INSTALLED="pymodbus未安装，请执行: pip install pymodbus>=3.0.0",
    ERR_MODBUS_SLAVE_DECODE_FAILED="Modbus Slave数值解码失败",
    ERR_MODBUS_SLAVE_CONFIG_INVALID="Modbus Slave配置无效",
    ERR_MODBUS_SLAVE_CONN_REJECTED_MAX="Modbus Slave连接被拒绝：已达最大连接数",
    ERR_MODBUS_SLAVE_CONN_REJECTED_WHITELIST="Modbus Slave连接被拒绝：IP不在白名单中",
    ERR_MODBUS_SLAVE_CONN_REJECTED_BANNED="Modbus Slave连接被拒绝：IP已被封禁",
    ERR_MODBUS_SLAVE_IP_BANNED="Modbus Slave IP因异常行为被封禁",
    ERR_MODBUS_SLAVE_WRITE_READONLY="Modbus Slave写入只读寄存器被拒绝",
    ERR_MODBUS_SLAVE_WRITE_VALUE_INVALID="Modbus Slave写入值超出有效范围",
    ERR_SIM_READ_FAILED="模拟器读取失败",
    ERR_SIM_READ_TIMEOUT="模拟器读取超时",
    ERR_SIM_WRITE_FAILED="模拟器写入失败",
    ERR_SIM_FAULT_TIMEOUT="模拟器故障:超时",
    ERR_SIM_FAULT_DISCONNECT="模拟器故障:断连",
    ERR_SIM_FAULT_DATA_ERROR="模拟器故障:数据错误",
    ERR_SIM_FAULT_RANDOM="模拟器故障:随机混合",
    ERR_SIM_VALUE_OUT_OF_RANGE="模拟器数值超出限幅范围",
    ERR_SIM_CONFIG_INVALID="模拟器配置无效",
    ERR_SIM_FORMULA_EVAL_FAILED="模拟器公式计算失败",

    ERR_OPCDA_CONN_FAILED="OPC DA连接失败",
    ERR_OPCDA_CONN_TIMEOUT="OPC DA连接超时",
    ERR_OPCDA_CONN_LOST="OPC DA连接丢失",
    ERR_OPCDA_CONN_RECOVERED="OPC DA连接恢复",
    ERR_OPCDA_READ_FAILED="OPC DA读取失败",
    ERR_OPCDA_READ_TIMEOUT="OPC DA读取超时",
    ERR_OPCDA_WRITE_FAILED="OPC DA写入失败",
    ERR_OPCDA_WRITE_TIMEOUT="OPC DA写入超时",
    ERR_OPCDA_CONFIG_INVALID="OPC DA配置无效",
    ERR_OPCDA_RECONNECT_OK="OPC DA重连成功",
    ERR_OPCDA_RECONNECT_FAILED="OPC DA重连失败",
    ERR_OPCDA_QUALITY_BAD="OPC DA变量质量为坏",
    ERR_OPCDA_QUALITY_UNCERTAIN="OPC DA变量质量为不确定",
    ERR_OPCDA_OFFLINE_BAD_QUALITY="OPC DA离线，测点标记为坏质量",
    ERR_OPCDA_VALUE_OUT_OF_RANGE="OPC DA数值超出限幅范围",
    ERR_OPCDA_DCOM_ACCESS_DENIED="OPC DA DCOM访问被拒绝",
    ERR_OPCDA_DCOM_SERVER_UNAVAILABLE="OPC DA DCOM服务器不可用",
    ERR_OPCDA_DCOM_DISCONNECTED="OPC DA DCOM对象已断开",
    ERR_OPCDA_DCOM_CALL_FAILED="OPC DA DCOM调用失败",
    ERR_OPCDA_SUBSCRIPTION_FAILED="OPC DA订阅失败",
    ERR_OPCDA_BROWSE_FAILED="OPC DA浏览失败",
    ERR_OPCDA_IMPORT_ERROR="OPC DA OpenOPC未安装",
    ERR_OPCDA_DCOM_CLASS_NOT_REGISTERED="OPC DA DCOM类未注册，请安装OPC Core Components",
    ERR_OPCDA_DCOM_SERVER_BUSY="OPC DA DCOM服务器繁忙，退避重试中",
    ERR_OPCDA_RATE_OF_CHANGE_EXCEEDED="OPC DA变化率超限",
    ERR_OPCDA_FROZEN_VALUE_DETECTED="OPC DA冻结值检测",
    ERR_OPCDA_WRITE_READ_ONLY="OPC DA写入只读项被拒绝",
    ERR_OPCDA_WRITE_TYPE_MISMATCH="OPC DA写入值类型不匹配",
    ERR_VAI_MODEL_NOT_LOADED="视频AI模型未加载",
    ERR_VAI_FRAME_CAPTURE_FAILED="视频AI图像采集失败",
    ERR_VAI_INFERENCE_FAILED="视频AI推理失败",
    ERR_VAI_INFERENCE_EMPTY="视频AI推理输出为空",
    ERR_VAI_ONNX_LOAD_FAILED="视频AI ONNX模型加载失败",
    ERR_VAI_OFFLINE_BAD_QUALITY="视频AI离线，测点标记为坏质量",
    ERR_VAI_CONFIG_INVALID="视频AI配置无效",
    ERR_VAI_VALUE_OUT_OF_RANGE="视频AI数值超出限幅范围",
    ERR_VAI_MODEL_HOT_RELOAD_OK="视频AI模型热加载成功",
    ERR_VAI_MODEL_HOT_RELOAD_FAILED="视频AI模型热加载失败",
    ERR_VAI_MODEL_ROLLBACK_OK="视频AI模型已回退至旧版本",
    ERR_VAI_MODEL_VALIDATE_FAILED="视频AI模型校验失败",
    ERR_VAI_MODEL_VALIDATE_INPUT_MISMATCH="视频AI模型输入维度不匹配",
    ERR_VAI_MODEL_VALIDATE_OUTPUT_MISMATCH="视频AI模型输出维度不匹配",
    ERR_VAI_MODEL_VALIDATE_DTYPE_MISMATCH="视频AI模型数据类型不匹配",
    ERR_VAI_GPU_DEGRADED_TO_CPU="视频AI GPU不可用，降级至CPU",
    ERR_VAI_INFERENCE_TIMEOUT="视频AI推理超时",
    ERR_VAI_PREPROCESS_FAILED="视频AI图像预处理失败",
    ERR_VAI_MODEL_PATH_TRAVERSAL="视频AI模型路径遍历攻击已拦截",
    ERR_VAI_MODEL_PATH_NOT_ALLOWED="视频AI模型路径不在允许目录内",
    ERR_VAI_MODEL_FORMAT_INVALID="视频AI模型文件格式无效，仅允许.onnx文件",
    ERR_VAI_MODEL_HEADER_INVALID="视频AI模型文件头校验失败",
    ERR_VAI_CONFIG_AUDIT_RECORDED="视频AI配置变更审计已记录",
    ERR_ONVIF_START_FAILED="ONVIF驱动启动失败",
    ERR_ONVIF_CAMERA_INIT_FAILED="ONVIF相机初始化失败",
    ERR_ONVIF_DISCOVER_FAILED="ONVIF设备发现失败",
    ERR_ONVIF_RTSP_FAILED="ONVIF RTSP流地址获取失败",
    ERR_ONVIF_PTZ_CONTINUOUS_FAILED="ONVIF PTZ连续移动失败",
    ERR_ONVIF_PTZ_ABSOLUTE_FAILED="ONVIF PTZ绝对定位失败",
    ERR_ONVIF_PTZ_RELATIVE_FAILED="ONVIF PTZ相对移动失败",
    ERR_ONVIF_PTZ_STOP_FAILED="ONVIF PTZ停止失败",
    ERR_ONVIF_PRESET_SET_FAILED="ONVIF预置位设置失败",
    ERR_ONVIF_PRESET_GOTO_FAILED="ONVIF预置位调用失败",
    ERR_ONVIF_PRESET_REMOVE_FAILED="ONVIF预置位删除失败",
    ERR_ONVIF_PRESET_GET_FAILED="ONVIF预置位获取失败",
    ERR_ONVIF_SNAPSHOT_URI_FAILED="ONVIF快照URI获取失败",
    ERR_ONVIF_EVENT_SUBSCRIBE_FAILED="ONVIF事件订阅失败",
    ERR_ONVIF_READ_FAILED="ONVIF测点读取失败",
    ERR_ONVIF_WRITE_INVALID="ONVIF写入地址无效",
    ERR_ONVIF_WRITE_FAILED="ONVIF写入失败",
    ERR_ONVIF_WATCHDOG_TRIGGER="ONVIF看门狗检测失败，触发重连",
    ERR_ONVIF_RECONNECT_GIVEUP="ONVIF重连放弃",
    ERR_ONVIF_RECONNECT_FAILED="ONVIF重连失败",
    ERR_ONVIF_AUTH_FAILED="ONVIF认证失败，不再重试",
    ERR_ONVIF_NOT_SUPPORTED="ONVIF操作不支持",
    ERR_ONVIF_CONN_TIMEOUT="ONVIF连接超时",
    ERR_ONVIF_SOAP_ERROR="ONVIF SOAP通信错误",
    ERR_ONVIF_SNAPSHOT_DOWNLOAD_FAILED="ONVIF快照下载失败",
    ERR_ONVIF_PTZ_OUT_OF_RANGE="ONVIF PTZ值超出设备范围",
    ERR_ONVIF_PTZ_RATE_LIMITED="ONVIF PTZ操作过快，最小间隔500ms",
    ERR_ONVIF_PRESET_NOT_FOUND="ONVIF预置位不存在",
)


# Translation map
TRANSLATIONS: dict[str, TranslationStrings] = {
    "en": ENGLISH_STRINGS,
    "zh": ZH_CHINESE_STRINGS,
}


class I18n:
    """Internationalization manager"""

    def __init__(self, language: str = DEFAULT_LANGUAGE):
        self._language = language
        self._callbacks: list[Callable[[str], None]] = []

    @property
    def language(self) -> str:
        return self._language

    @property
    def strings(self) -> TranslationStrings:
        return TRANSLATIONS.get(self._language, ENGLISH_STRINGS)

    def set_language(self, language: str) -> bool:
        """Set the current language"""
        if language not in SUPPORTED_LANGUAGES:
            return False
        self._language = language
        self._notify_change()
        return True

    def get_language_from_request(self, accept_language: str | None = None) -> str:
        """Determine language from Accept-Language header"""
        if not accept_language:
            return self._language

        # Parse Accept-Language header
        languages = []
        for part in accept_language.split(","):
            part = part.strip().split(";")[0]
            if part:
                languages.append(part.lower())

        # Match supported languages
        for lang in languages:
            if lang.startswith("zh"):
                return "zh"
            elif lang.startswith("en"):
                return "en"

        return self._language

    def register_change_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback for language change events"""
        self._callbacks.append(callback)

    def _notify_change(self) -> None:
        """Notify all registered callbacks of language change"""
        for callback in self._callbacks:
            try:
                callback(self._language)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("语言变更回调执行失败: %s", e)

    def t(self, key: str) -> str:
        """Get translation for a key"""
        try:
            return getattr(self.strings, key, key)
        except AttributeError:
            return key


# Global i18n instance
_i18n: I18n | None = None


def get_i18n() -> I18n:
    """Get the global i18n instance"""
    global _i18n
    if _i18n is None:
        _i18n = I18n()
    return _i18n


def set_language(language: str) -> bool:
    """Set the global language"""
    return get_i18n().set_language(language)


def get_strings() -> TranslationStrings:
    """Get the current translation strings"""
    return get_i18n().strings


def t(key: str) -> str:
    """Get translation for a key (shorthand)"""
    return get_i18n().t(key)


def get_translatable_keys() -> list[str]:
    """Get all available translation keys"""
    # R11-SVC-07: 基于 isinstance 类型过滤，仅返回值为 str 的属性，
    # 避免 dir() 返回的非翻译属性（方法、类变量等）混入翻译键列表
    cls = TranslationStrings
    return [
        attr for attr in dir(cls)
        if not attr.startswith("_")
        and isinstance(getattr(cls, attr), str)
    ]


class LanguageMiddleware:
    """ASGI middleware that parses Accept-Language header and sets accept_language in scope."""

    def __init__(self, app: ASGIApp, default_language: str = DEFAULT_LANGUAGE) -> None:
        self.app = app
        self.default_language = default_language

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(
                (k.decode("latin-1").lower(), v.decode("latin-1"))
                for k, v in scope.get("headers", [])
            )
            accept_language = headers.get("accept-language", "")
            resolved = self._resolve_language(accept_language)
            scope["accept_language"] = resolved
        await self.app(scope, receive, send)

    def _resolve_language(self, accept_language: str) -> str:
        if not accept_language:
            return self.default_language
        for part in accept_language.split(","):
            lang = part.strip().split(";")[0].lower()
            if lang.startswith("zh"):
                return "zh"
            if lang.startswith("en"):
                return "en"
        return self.default_language
