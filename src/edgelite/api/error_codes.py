"""API错误码定义

后端返回error_code而非中文detail，前端根据error_code映射i18n文本。
格式: ERR_{模块}_{动作}_{原因}

每个错误码包含:
- code: 字符串标识 (如 ERR_DEVICE_NOT_FOUND)
- http_status: 对应的HTTP状态码
- message: 英文描述

FIXED: 原问题-后端API错误消息中文硬编码，无法国际化且前端依赖字符串匹配
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCodeDetail:
    """错误码详情"""

    code: str
    http_status: int
    message: str


class AuthErrors:
    RATE_LIMITED = "ERR_AUTH_RATE_LIMITED"
    INVALID_CREDENTIALS = "ERR_AUTH_INVALID_CREDENTIALS"
    USER_DISABLED = "ERR_AUTH_USER_DISABLED"
    LOGIN_FAILED = "ERR_AUTH_LOGIN_FAILED"
    REFRESH_TOKEN_INVALID = "ERR_AUTH_REFRESH_TOKEN_INVALID"
    USER_NOT_FOUND = "ERR_AUTH_USER_NOT_FOUND"
    OLD_PASSWORD_WRONG = "ERR_AUTH_OLD_PASSWORD_WRONG"
    PASSWORD_POLICY = "ERR_AUTH_PASSWORD_POLICY"
    PASSWORD_TOO_LONG = "ERR_AUTH_PASSWORD_TOO_LONG"
    PASSWORD_SAME_AS_OLD = "ERR_AUTH_PASSWORD_SAME_AS_OLD"
    PASSWORD_LETTER_AND_DIGIT = "ERR_AUTH_PASSWORD_LETTER_AND_DIGIT"
    PASSWORD_CHANGE_FAILED = "ERR_AUTH_PASSWORD_CHANGE_FAILED"
    TOKEN_INVALID = "ERR_AUTH_TOKEN_INVALID"
    TOKEN_EXPIRED = "ERR_AUTH_TOKEN_EXPIRED"
    TOKEN_REVOKED = "ERR_AUTH_TOKEN_REVOKED"
    PERMISSION_DENIED = "ERR_AUTH_PERMISSION_DENIED"
    ACCOUNT_DISABLED = "ERR_AUTH_ACCOUNT_DISABLED"
    PASSWORD_EXPIRED = "ERR_AUTH_PASSWORD_EXPIRED"
    LOGOUT_FAILED = "ERR_AUTH_LOGOUT_FAILED"


class UserErrors:
    LIST_FAILED = "ERR_USER_LIST_FAILED"
    USERNAME_EXISTS = "ERR_USER_USERNAME_EXISTS"
    CREATE_FAILED = "ERR_USER_CREATE_FAILED"
    CANNOT_REMOVE_LAST_ADMIN = "ERR_USER_CANNOT_REMOVE_LAST_ADMIN"
    USER_NOT_FOUND = "ERR_USER_USER_NOT_FOUND"
    UPDATE_FAILED = "ERR_USER_UPDATE_FAILED"
    CANNOT_DELETE_SELF = "ERR_USER_CANNOT_DELETE_SELF"
    CANNOT_DELETE_ADMIN = "ERR_USER_CANNOT_DELETE_ADMIN"
    CANNOT_DELETE_LAST_ADMIN = "ERR_USER_CANNOT_DELETE_LAST_ADMIN"
    DELETE_FAILED = "ERR_USER_DELETE_FAILED"


class ServiceErrors:
    LIST_FAILED = "ERR_SVC_LIST_FAILED"
    UNKNOWN_SERVICE = "ERR_SVC_UNKNOWN_SERVICE"
    NOT_REGISTERED = "ERR_SVC_NOT_REGISTERED"
    STATUS_FAILED = "ERR_SVC_STATUS_FAILED"
    ENABLE_FAILED = "ERR_SVC_ENABLE_FAILED"
    DISABLE_FAILED = "ERR_SVC_DISABLE_FAILED"
    START_FAILED = "ERR_SVC_START_FAILED"
    STOP_FAILED = "ERR_SVC_STOP_FAILED"
    INSTALL_FAILED = "ERR_SVC_INSTALL_FAILED"
    CONFIG_UPDATE_FAILED = "ERR_SVC_CONFIG_UPDATE_FAILED"
    DEPS_INSTALL_FAILED = "ERR_SVC_DEPS_INSTALL_FAILED"
    SERIAL_PORT_UNAVAILABLE = "ERR_SVC_SERIAL_PORT_UNAVAILABLE"


class DeviceErrors:
    NOT_FOUND = "ERR_DEVICE_NOT_FOUND"
    ALREADY_EXISTS = "ERR_DEVICE_ALREADY_EXISTS"
    OFFLINE = "ERR_DEVICE_OFFLINE"
    CONFIG_INVALID = "ERR_DEVICE_CONFIG_INVALID"
    DRIVER_UNAVAILABLE = "ERR_DEVICE_DRIVER_UNAVAILABLE"
    PUSH_EMPTY = "ERR_DEVICE_PUSH_EMPTY"
    PUSH_INVALID_ID = "ERR_DEVICE_PUSH_INVALID_ID"
    PUSH_INVALID_KEY = "ERR_DEVICE_PUSH_INVALID_KEY"  # FIXED: 原问题-push_data无key校验
    WEBHOOK_AUTH_FAILED = "ERR_DEVICE_WEBHOOK_AUTH_FAILED"
    PUSH_FAILED = "ERR_DEVICE_PUSH_FAILED"
    LIST_FAILED = "ERR_DEVICE_LIST_FAILED"
    CREATE_FAILED = "ERR_DEVICE_CREATE_FAILED"
    GET_FAILED = "ERR_DEVICE_GET_FAILED"
    UPDATE_FAILED = "ERR_DEVICE_UPDATE_FAILED"
    DELETE_FAILED = "ERR_DEVICE_DELETE_FAILED"
    POINTS_FAILED = "ERR_DEVICE_POINTS_FAILED"
    WRITE_FAILED = "ERR_DEVICE_WRITE_FAILED"
    API_KEY_INVALID = "ERR_DEVICE_API_KEY_INVALID"
    API_KEY_NOT_CONFIGURED = "ERR_DEVICE_API_KEY_NOT_CONFIGURED"
    SIMULATOR_FAILED = "ERR_DEVICE_SIMULATOR_FAILED"
    DISCOVER_FAILED = "ERR_DEVICE_DISCOVER_FAILED"
    PUSH_DRIVER_NOT_READY = "ERR_DEVICE_PUSH_DRIVER_NOT_READY"


class PreprocessErrors:
    GET_FAILED = "ERR_PREPROCESS_GET_FAILED"
    NOT_INITIALIZED = "ERR_PREPROCESS_NOT_INITIALIZED"
    UPDATE_FAILED = "ERR_PREPROCESS_UPDATE_FAILED"


class CommonErrors:
    SERVICE_NOT_READY = "ERR_COMMON_SERVICE_NOT_READY"
    DB_NOT_READY = "ERR_COMMON_DB_NOT_READY"
    INTERNAL_ERROR = "ERR_COMMON_INTERNAL_ERROR"
    NOT_FOUND = "ERR_COMMON_NOT_FOUND"


class RepoErrors:
    DEVICE_EXISTS = "ERR_REPO_DEVICE_EXISTS"
    RULE_EXISTS = "ERR_REPO_RULE_EXISTS"
    ALARM_EXISTS = "ERR_REPO_ALARM_EXISTS"
    USERNAME_EXISTS = "ERR_REPO_USERNAME_EXISTS"
    DB_MODE_SESSION_REQUIRED = "ERR_REPO_DB_MODE_SESSION_REQUIRED"
    NO_SESSION_AVAILABLE = "ERR_REPO_NO_SESSION_AVAILABLE"


class DatabaseErrors:
    UNSUPPORTED_BACKEND = "ERR_DB_UNSUPPORTED_BACKEND"
    DRIVER_REQUIRED = "ERR_DB_DRIVER_REQUIRED"
    NOT_CONNECTED = "ERR_DB_NOT_CONNECTED"
    SESSION_NOT_INIT = "ERR_DB_SESSION_NOT_INIT"


class ConfigErrors:
    LOAD_FAILED = "ERR_CONFIG_LOAD_FAILED"
    SAVE_FAILED = "ERR_CONFIG_SAVE_FAILED"


class DriverErrors:
    START_FAILED = "ERR_DRIVER_START_FAILED"
    NOT_FOUND = "ERR_DRIVER_NOT_FOUND"
    REGISTRY_NOT_INIT = "ERR_DRIVER_REGISTRY_NOT_INIT"
    LIST_FAILED = "ERR_DRIVER_LIST_FAILED"
    GET_FAILED = "ERR_DRIVER_GET_FAILED"
    DISCOVER_FAILED = "ERR_DRIVER_DISCOVER_FAILED"


class DataErrors:
    UNSUPPORTED_AGGREGATE = "ERR_DATA_UNSUPPORTED_AGGREGATE"
    QUERY_FAILED = "ERR_DATA_QUERY_FAILED"
    EXPORT_FAILED = "ERR_DATA_EXPORT_FAILED"
    NO_DATA = "ERR_DATA_NO_DATA"


class VideoErrors:
    PTZ_FAILED = "ERR_VIDEO_PTZ_FAILED"
    WEBHOOK_FAILED = "ERR_VIDEO_WEBHOOK_FAILED"
    API_KEY_INVALID = "ERR_VIDEO_API_KEY_INVALID"
    API_KEY_NOT_CONFIGURED = "ERR_VIDEO_API_KEY_NOT_CONFIGURED"


class AuditErrors:
    INVALID_ACTION = "ERR_AUDIT_INVALID_ACTION"
    INVALID_TIME_FORMAT = "ERR_AUDIT_INVALID_TIME_FORMAT"
    NOT_ENABLED = "ERR_AUDIT_NOT_ENABLED"
    CLEANUP_FAILED = "ERR_AUDIT_CLEANUP_FAILED"
    EXPORT_FAILED = "ERR_AUDIT_EXPORT_FAILED"
    INTEGRITY_FAILED = "ERR_AUDIT_INTEGRITY_FAILED"
    LIST_FAILED = "ERR_AUDIT_LIST_FAILED"


class ExpressionErrors:
    EVALUATE_FAILED = "ERR_EXPR_EVALUATE_FAILED"
    BATCH_EVALUATE_FAILED = "ERR_EXPR_BATCH_EVALUATE_FAILED"
    VALIDATE_FAILED = "ERR_EXPR_VALIDATE_FAILED"


class ScadaErrors:
    PROJECT_NOT_FOUND = "ERR_SCADA_PROJECT_NOT_FOUND"
    SAVE_FAILED = "ERR_SCADA_SAVE_FAILED"
    DELETE_FAILED = "ERR_SCADA_DELETE_FAILED"
    LOAD_FAILED = "ERR_SCADA_LOAD_FAILED"


class PlatformErrors:
    CONFIG_SCHEMA_NOT_FOUND = "ERR_PLATFORM_CONFIG_SCHEMA_NOT_FOUND"
    CONNECT_FAILED = "ERR_PLATFORM_CONNECT_FAILED"
    DISCONNECT_FAILED = "ERR_PLATFORM_DISCONNECT_FAILED"
    NOT_SUPPORTED = "ERR_PLATFORM_NOT_SUPPORTED"
    MISSING_CONFIG = "ERR_PLATFORM_MISSING_CONFIG"
    NOT_CONNECTED = "ERR_PLATFORM_NOT_CONNECTED"
    VALIDATION_REQUIRED = "ERR_PLATFORM_VALIDATION_REQUIRED"
    VALIDATION_BROKER_FORMAT = "ERR_PLATFORM_VALIDATION_BROKER_FORMAT"
    VALIDATION_PORT_RANGE = "ERR_PLATFORM_VALIDATION_PORT_RANGE"
    VALIDATION_PORT_NUMBER = "ERR_PLATFORM_VALIDATION_PORT_NUMBER"
    VALIDATION_TOO_LONG = "ERR_PLATFORM_VALIDATION_TOO_LONG"
    VALIDATION_UNSUPPORTED = "ERR_PLATFORM_VALIDATION_UNSUPPORTED"


class SystemErrors:
    STATUS_FAILED = "ERR_SYS_STATUS_FAILED"
    BACKUP_LIST_FAILED = "ERR_SYS_BACKUP_LIST_FAILED"
    BACKUP_CREATE_FAILED = "ERR_SYS_BACKUP_CREATE_FAILED"
    INVALID_BACKUP_ID = "ERR_SYS_INVALID_BACKUP_ID"
    BACKUP_NOT_FOUND = "ERR_SYS_BACKUP_NOT_FOUND"
    RESTORE_FAILED = "ERR_SYS_RESTORE_FAILED"


class RuleErrors:
    LIST_FAILED = "ERR_RULE_LIST_FAILED"
    CREATE_FAILED = "ERR_RULE_CREATE_FAILED"
    NOT_FOUND = "ERR_RULE_NOT_FOUND"
    GET_FAILED = "ERR_RULE_GET_FAILED"
    UPDATE_FAILED = "ERR_RULE_UPDATE_FAILED"
    DELETE_FAILED = "ERR_RULE_DELETE_FAILED"
    ENABLE_FAILED = "ERR_RULE_ENABLE_FAILED"
    DISABLE_FAILED = "ERR_RULE_DISABLE_FAILED"
    TEST_FAILED = "ERR_RULE_TEST_FAILED"
    CONDITION_INVALID = "ERR_RULE_CONDITION_INVALID"


class AlarmErrors:
    LIST_FAILED = "ERR_ALARM_LIST_FAILED"
    NOT_FOUND = "ERR_ALARM_NOT_FOUND"
    GET_FAILED = "ERR_ALARM_GET_FAILED"
    ACK_FAILED = "ERR_ALARM_ACK_FAILED"
    ALREADY_ACKNOWLEDGED = "ERR_ALARM_ALREADY_ACKNOWLEDGED"
    ALREADY_RECOVERED = "ERR_ALARM_ALREADY_RECOVERED"


class IntegrationErrors:
    HANDSHAKE_FAILED = "ERR_INTEG_HANDSHAKE_FAILED"
    STATUS_FAILED = "ERR_INTEG_STATUS_FAILED"
    RPC_EXECUTE_FAILED = "ERR_INTEG_RPC_EXECUTE_FAILED"
    RPC_HISTORY_FAILED = "ERR_INTEG_RPC_HISTORY_FAILED"
    BACKHAUL_NOT_READY = "ERR_INTEG_BACKHAUL_NOT_READY"
    RPC_DEVICE_SERVICE_UNAVAILABLE = "ERR_INTEG_RPC_DEVICE_SERVICE_UNAVAILABLE"
    RPC_MISSING_VALUE = "ERR_INTEG_RPC_MISSING_VALUE"
    RPC_WRITE_FAILED = "ERR_INTEG_RPC_WRITE_FAILED"


class CascadeErrors:
    INVALID_CONFIG = "ERR_CASCADE_INVALID_CONFIG"
    TOPOLOGY_FAILED = "ERR_CASCADE_TOPOLOGY_FAILED"
    NEIGHBORS_FAILED = "ERR_CASCADE_NEIGHBORS_FAILED"
    CONFIG_UPDATE_FAILED = "ERR_CASCADE_CONFIG_UPDATE_FAILED"
    NEIGHBOR_NOT_FOUND = "ERR_CASCADE_NEIGHBOR_NOT_FOUND"
    REMOVE_FAILED = "ERR_CASCADE_REMOVE_FAILED"
    NOT_ENABLED = "ERR_CASCADE_NOT_ENABLED"


class McpErrors:
    DEVICE_SERVICE_UNAVAILABLE = "ERR_MCP_DEVICE_SERVICE_UNAVAILABLE"
    MISSING_DEVICE_ID = "ERR_MCP_MISSING_DEVICE_ID"
    DEVICE_NOT_FOUND = "ERR_MCP_DEVICE_NOT_FOUND"
    MISSING_PARAMS = "ERR_MCP_MISSING_PARAMS"
    ALARM_SERVICE_UNAVAILABLE = "ERR_MCP_ALARM_SERVICE_UNAVAILABLE"
    SYSTEM_SERVICE_UNAVAILABLE = "ERR_MCP_SYSTEM_SERVICE_UNAVAILABLE"
    RULE_SERVICE_UNAVAILABLE = "ERR_MCP_RULE_SERVICE_UNAVAILABLE"
    UNKNOWN_TOOL = "ERR_MCP_UNKNOWN_TOOL"
    LIST_FAILED = "ERR_MCP_LIST_FAILED"
    CALL_FAILED = "ERR_MCP_CALL_FAILED"
    CREATE_KEY_FAILED = "ERR_MCP_CREATE_KEY_FAILED"
    KEY_NOT_FOUND = "ERR_MCP_KEY_NOT_FOUND"
    SSE_FAILED = "ERR_MCP_SSE_FAILED"


class OtaErrors:
    NOT_ENABLED = "ERR_OTA_NOT_ENABLED"
    CHECK_FAILED = "ERR_OTA_CHECK_FAILED"
    IN_PROGRESS = "ERR_OTA_IN_PROGRESS"
    NO_UPDATE = "ERR_OTA_NO_UPDATE"
    NO_DOWNLOAD_URL = "ERR_OTA_NO_DOWNLOAD_URL"
    DOWNLOAD_FAILED = "ERR_OTA_DOWNLOAD_FAILED"
    APPLY_FAILED = "ERR_OTA_APPLY_FAILED"
    ROLLBACK_FAILED = "ERR_OTA_ROLLBACK_FAILED"
    LIST_BACKUPS_FAILED = "ERR_OTA_LIST_BACKUPS_FAILED"


class AuthzErrors:
    NOT_AUTHENTICATED = "ERR_AUTHZ_NOT_AUTHENTICATED"
    PERMISSION_DENIED = "ERR_AUTHZ_PERMISSION_DENIED"


class AiErrors:
    ENGINE_NOT_INITIALIZED = "ERR_AI_ENGINE_NOT_INITIALIZED"
    MODEL_NOT_FOUND = "ERR_AI_MODEL_NOT_FOUND"
    MODEL_LOAD_FAILED = "ERR_AI_MODEL_LOAD_FAILED"
    MODEL_RELOAD_FAILED = "ERR_AI_MODEL_RELOAD_FAILED"
    MODEL_DELETE_PRESET = "ERR_AI_MODEL_DELETE_PRESET"
    MODEL_ALREADY_LOADED = "ERR_AI_MODEL_ALREADY_LOADED"
    MODEL_FILE_NOT_FOUND = "ERR_AI_MODEL_FILE_NOT_FOUND"
    MODEL_CANNOT_LOAD = "ERR_AI_MODEL_CANNOT_LOAD"
    MODEL_PREVIOUS_ERROR = "ERR_AI_MODEL_PREVIOUS_ERROR"
    MODEL_ENABLE_FAILED = "ERR_AI_MODEL_ENABLE_FAILED"
    INFERENCE_FAILED = "ERR_AI_INFERENCE_FAILED"
    INFERENCE_TIMEOUT = "ERR_AI_INFERENCE_TIMEOUT"
    STATS_FAILED = "ERR_AI_STATS_FAILED"
    LIST_FAILED = "ERR_AI_LIST_FAILED"
    GET_FAILED = "ERR_AI_GET_FAILED"
    UPDATE_FAILED = "ERR_AI_UPDATE_FAILED"
    DELETE_FAILED = "ERR_AI_DELETE_FAILED"
    ENABLE_FAILED = "ERR_AI_ENABLE_FAILED"
    DISABLE_FAILED = "ERR_AI_DISABLE_FAILED"
    ONNX_NOT_AVAILABLE = "ERR_AI_ONNX_NOT_AVAILABLE"
    ONNXRUNTIME_NOT_INSTALLED = "ERR_AI_ONNXRUNTIME_NOT_INSTALLED"
    MODEL_IS_LOADING = "ERR_AI_MODEL_IS_LOADING"
    INVALID_INPUT_DATA = "ERR_AI_INVALID_INPUT_DATA"
    INTERNAL_ERROR = "ERR_AI_INTERNAL_ERROR"
    SCHEDULE_ALREADY_EXISTS = "ERR_AI_SCHEDULE_ALREADY_EXISTS"
    SCHEDULE_NOT_FOUND = "ERR_AI_SCHEDULE_NOT_FOUND"
    SCHEDULE_START_FAILED = "ERR_AI_SCHEDULE_START_FAILED"
    SCHEDULE_NO_DATA = "ERR_AI_SCHEDULE_NO_DATA"


class GrafanaErrors:
    NOT_ENABLED = "ERR_GRAFANA_NOT_ENABLED"
    API_KEY_MISSING = "ERR_GRAFANA_API_KEY_MISSING"
    BAD_STATUS = "ERR_GRAFANA_BAD_STATUS"
    DEPS_MISSING = "ERR_GRAFANA_DEPS_MISSING"
    CONNECTION_FAILED = "ERR_GRAFANA_CONNECTION_FAILED"
    INVALID_UID = "ERR_GRAFANA_INVALID_UID"


class StorageErrors:
    INFLUXDB_UNAVAILABLE = "ERR_STORAGE_INFLUXDB_UNAVAILABLE"
    SQLITE_ERROR = "ERR_STORAGE_SQLITE_ERROR"
    CACHE_OVERFLOW = "ERR_STORAGE_CACHE_OVERFLOW"
    SYNC_FAILED = "ERR_STORAGE_SYNC_FAILED"


class NetworkErrors:
    TIMEOUT = "ERR_NETWORK_TIMEOUT"
    CONNECTION_REFUSED = "ERR_NETWORK_CONNECTION_REFUSED"
    DNS_FAILED = "ERR_NETWORK_DNS_FAILED"


class ProtocolErrors:
    MODBUS_CRC = "ERR_PROTOCOL_MODBUS_CRC"
    S7_PDU = "ERR_PROTOCOL_S7_PDU"
    OPCUA_SESSION = "ERR_PROTOCOL_OPCUA_SESSION"
    MQTT_AUTH = "ERR_PROTOCOL_MQTT_AUTH"


# ---------------------------------------------------------------------------
# ERROR_CODE_MAP: 统一错误码 -> (http_status, message) 映射
# ---------------------------------------------------------------------------
ERROR_CODE_MAP: dict[str, ErrorCodeDetail] = {
    # --- AUTH ---
    AuthErrors.RATE_LIMITED: ErrorCodeDetail(AuthErrors.RATE_LIMITED, 429, "Too many requests"),
    AuthErrors.INVALID_CREDENTIALS: ErrorCodeDetail(AuthErrors.INVALID_CREDENTIALS, 401, "Invalid credentials"),
    AuthErrors.USER_DISABLED: ErrorCodeDetail(AuthErrors.USER_DISABLED, 403, "User account is disabled"),
    AuthErrors.LOGIN_FAILED: ErrorCodeDetail(AuthErrors.LOGIN_FAILED, 401, "Login failed"),
    AuthErrors.REFRESH_TOKEN_INVALID: ErrorCodeDetail(AuthErrors.REFRESH_TOKEN_INVALID, 401, "Refresh token is invalid"),
    AuthErrors.USER_NOT_FOUND: ErrorCodeDetail(AuthErrors.USER_NOT_FOUND, 404, "User not found"),
    AuthErrors.OLD_PASSWORD_WRONG: ErrorCodeDetail(AuthErrors.OLD_PASSWORD_WRONG, 400, "Current password is incorrect"),
    AuthErrors.PASSWORD_POLICY: ErrorCodeDetail(AuthErrors.PASSWORD_POLICY, 400, "Password does not meet policy"),
    AuthErrors.PASSWORD_TOO_LONG: ErrorCodeDetail(AuthErrors.PASSWORD_TOO_LONG, 400, "Password is too long"),
    AuthErrors.PASSWORD_SAME_AS_OLD: ErrorCodeDetail(AuthErrors.PASSWORD_SAME_AS_OLD, 400, "New password must differ from old"),
    AuthErrors.PASSWORD_LETTER_AND_DIGIT: ErrorCodeDetail(AuthErrors.PASSWORD_LETTER_AND_DIGIT, 400, "Password must contain letters and digits"),
    AuthErrors.PASSWORD_CHANGE_FAILED: ErrorCodeDetail(AuthErrors.PASSWORD_CHANGE_FAILED, 500, "Password change failed"),
    AuthErrors.TOKEN_INVALID: ErrorCodeDetail(AuthErrors.TOKEN_INVALID, 401, "Token is invalid"),
    AuthErrors.TOKEN_EXPIRED: ErrorCodeDetail(AuthErrors.TOKEN_EXPIRED, 401, "Token has expired"),
    AuthErrors.TOKEN_REVOKED: ErrorCodeDetail(AuthErrors.TOKEN_REVOKED, 401, "Token has been revoked"),
    AuthErrors.PERMISSION_DENIED: ErrorCodeDetail(AuthErrors.PERMISSION_DENIED, 403, "Permission denied"),
    AuthErrors.ACCOUNT_DISABLED: ErrorCodeDetail(AuthErrors.ACCOUNT_DISABLED, 403, "Account is disabled"),
    AuthErrors.PASSWORD_EXPIRED: ErrorCodeDetail(AuthErrors.PASSWORD_EXPIRED, 403, "Password has expired"),
    AuthErrors.LOGOUT_FAILED: ErrorCodeDetail(AuthErrors.LOGOUT_FAILED, 500, "Logout failed"),
    # --- USER ---
    UserErrors.LIST_FAILED: ErrorCodeDetail(UserErrors.LIST_FAILED, 500, "Failed to list users"),
    UserErrors.USERNAME_EXISTS: ErrorCodeDetail(UserErrors.USERNAME_EXISTS, 409, "Username already exists"),
    UserErrors.CREATE_FAILED: ErrorCodeDetail(UserErrors.CREATE_FAILED, 500, "Failed to create user"),
    UserErrors.CANNOT_REMOVE_LAST_ADMIN: ErrorCodeDetail(UserErrors.CANNOT_REMOVE_LAST_ADMIN, 409, "Cannot remove last admin role"),
    UserErrors.USER_NOT_FOUND: ErrorCodeDetail(UserErrors.USER_NOT_FOUND, 404, "User not found"),
    UserErrors.UPDATE_FAILED: ErrorCodeDetail(UserErrors.UPDATE_FAILED, 500, "Failed to update user"),
    UserErrors.CANNOT_DELETE_SELF: ErrorCodeDetail(UserErrors.CANNOT_DELETE_SELF, 409, "Cannot delete yourself"),
    UserErrors.CANNOT_DELETE_ADMIN: ErrorCodeDetail(UserErrors.CANNOT_DELETE_ADMIN, 409, "Cannot delete admin user"),
    UserErrors.CANNOT_DELETE_LAST_ADMIN: ErrorCodeDetail(UserErrors.CANNOT_DELETE_LAST_ADMIN, 409, "Cannot delete last admin"),
    UserErrors.DELETE_FAILED: ErrorCodeDetail(UserErrors.DELETE_FAILED, 500, "Failed to delete user"),
    # --- SERVICE ---
    ServiceErrors.LIST_FAILED: ErrorCodeDetail(ServiceErrors.LIST_FAILED, 500, "Failed to list services"),
    ServiceErrors.UNKNOWN_SERVICE: ErrorCodeDetail(ServiceErrors.UNKNOWN_SERVICE, 404, "Unknown service"),
    ServiceErrors.NOT_REGISTERED: ErrorCodeDetail(ServiceErrors.NOT_REGISTERED, 404, "Service not registered"),
    ServiceErrors.STATUS_FAILED: ErrorCodeDetail(ServiceErrors.STATUS_FAILED, 500, "Failed to get service status"),
    ServiceErrors.ENABLE_FAILED: ErrorCodeDetail(ServiceErrors.ENABLE_FAILED, 500, "Failed to enable service"),
    ServiceErrors.DISABLE_FAILED: ErrorCodeDetail(ServiceErrors.DISABLE_FAILED, 500, "Failed to disable service"),
    ServiceErrors.START_FAILED: ErrorCodeDetail(ServiceErrors.START_FAILED, 500, "Failed to start service"),
    ServiceErrors.STOP_FAILED: ErrorCodeDetail(ServiceErrors.STOP_FAILED, 500, "Failed to stop service"),
    ServiceErrors.INSTALL_FAILED: ErrorCodeDetail(ServiceErrors.INSTALL_FAILED, 500, "Failed to install service"),
    ServiceErrors.CONFIG_UPDATE_FAILED: ErrorCodeDetail(ServiceErrors.CONFIG_UPDATE_FAILED, 500, "Failed to update service config"),
    ServiceErrors.DEPS_INSTALL_FAILED: ErrorCodeDetail(ServiceErrors.DEPS_INSTALL_FAILED, 500, "Failed to install dependencies"),
    ServiceErrors.SERIAL_PORT_UNAVAILABLE: ErrorCodeDetail(ServiceErrors.SERIAL_PORT_UNAVAILABLE, 400, "Serial port unavailable"),
    # --- DEVICE ---
    DeviceErrors.NOT_FOUND: ErrorCodeDetail(DeviceErrors.NOT_FOUND, 404, "Device not found"),
    DeviceErrors.ALREADY_EXISTS: ErrorCodeDetail(DeviceErrors.ALREADY_EXISTS, 409, "Device already exists"),
    DeviceErrors.OFFLINE: ErrorCodeDetail(DeviceErrors.OFFLINE, 503, "Device is offline"),
    DeviceErrors.CONFIG_INVALID: ErrorCodeDetail(DeviceErrors.CONFIG_INVALID, 400, "Device configuration is invalid"),
    DeviceErrors.DRIVER_UNAVAILABLE: ErrorCodeDetail(DeviceErrors.DRIVER_UNAVAILABLE, 503, "Device driver is unavailable"),
    DeviceErrors.PUSH_EMPTY: ErrorCodeDetail(DeviceErrors.PUSH_EMPTY, 400, "Push data is empty"),
    DeviceErrors.PUSH_INVALID_ID: ErrorCodeDetail(DeviceErrors.PUSH_INVALID_ID, 400, "Invalid device ID"),
    DeviceErrors.PUSH_INVALID_KEY: ErrorCodeDetail(DeviceErrors.PUSH_INVALID_KEY, 401, "Invalid API key"),
    DeviceErrors.WEBHOOK_AUTH_FAILED: ErrorCodeDetail(DeviceErrors.WEBHOOK_AUTH_FAILED, 401, "Webhook authentication failed"),
    DeviceErrors.PUSH_FAILED: ErrorCodeDetail(DeviceErrors.PUSH_FAILED, 500, "Device push failed"),
    DeviceErrors.LIST_FAILED: ErrorCodeDetail(DeviceErrors.LIST_FAILED, 500, "Failed to list devices"),
    DeviceErrors.CREATE_FAILED: ErrorCodeDetail(DeviceErrors.CREATE_FAILED, 500, "Failed to create device"),
    DeviceErrors.GET_FAILED: ErrorCodeDetail(DeviceErrors.GET_FAILED, 500, "Failed to get device"),
    DeviceErrors.UPDATE_FAILED: ErrorCodeDetail(DeviceErrors.UPDATE_FAILED, 500, "Failed to update device"),
    DeviceErrors.DELETE_FAILED: ErrorCodeDetail(DeviceErrors.DELETE_FAILED, 500, "Failed to delete device"),
    DeviceErrors.POINTS_FAILED: ErrorCodeDetail(DeviceErrors.POINTS_FAILED, 500, "Failed to get device points"),
    DeviceErrors.WRITE_FAILED: ErrorCodeDetail(DeviceErrors.WRITE_FAILED, 500, "Failed to write device point"),
    DeviceErrors.API_KEY_INVALID: ErrorCodeDetail(DeviceErrors.API_KEY_INVALID, 401, "Device API key is invalid"),
    DeviceErrors.API_KEY_NOT_CONFIGURED: ErrorCodeDetail(DeviceErrors.API_KEY_NOT_CONFIGURED, 401, "Device API key not configured"),
    DeviceErrors.SIMULATOR_FAILED: ErrorCodeDetail(DeviceErrors.SIMULATOR_FAILED, 500, "Simulator failed"),
    DeviceErrors.DISCOVER_FAILED: ErrorCodeDetail(DeviceErrors.DISCOVER_FAILED, 500, "Device discovery failed"),
    DeviceErrors.PUSH_DRIVER_NOT_READY: ErrorCodeDetail(DeviceErrors.PUSH_DRIVER_NOT_READY, 503, "Push driver not ready"),
    # --- PREPROCESS ---
    PreprocessErrors.GET_FAILED: ErrorCodeDetail(PreprocessErrors.GET_FAILED, 500, "Failed to get preprocess config"),
    PreprocessErrors.NOT_INITIALIZED: ErrorCodeDetail(PreprocessErrors.NOT_INITIALIZED, 503, "Preprocessor not initialized"),
    PreprocessErrors.UPDATE_FAILED: ErrorCodeDetail(PreprocessErrors.UPDATE_FAILED, 500, "Failed to update preprocess config"),
    # --- COMMON ---
    CommonErrors.SERVICE_NOT_READY: ErrorCodeDetail(CommonErrors.SERVICE_NOT_READY, 503, "Service not ready"),
    CommonErrors.DB_NOT_READY: ErrorCodeDetail(CommonErrors.DB_NOT_READY, 503, "Database not ready"),
    CommonErrors.INTERNAL_ERROR: ErrorCodeDetail(CommonErrors.INTERNAL_ERROR, 500, "Internal error"),
    CommonErrors.NOT_FOUND: ErrorCodeDetail(CommonErrors.NOT_FOUND, 404, "Resource not found"),
    # --- REPO ---
    RepoErrors.DEVICE_EXISTS: ErrorCodeDetail(RepoErrors.DEVICE_EXISTS, 409, "Device already exists"),
    RepoErrors.RULE_EXISTS: ErrorCodeDetail(RepoErrors.RULE_EXISTS, 409, "Rule already exists"),
    RepoErrors.ALARM_EXISTS: ErrorCodeDetail(RepoErrors.ALARM_EXISTS, 409, "Alarm already exists"),
    RepoErrors.USERNAME_EXISTS: ErrorCodeDetail(RepoErrors.USERNAME_EXISTS, 409, "Username already exists"),
    RepoErrors.DB_MODE_SESSION_REQUIRED: ErrorCodeDetail(RepoErrors.DB_MODE_SESSION_REQUIRED, 500, "DB mode session required"),
    RepoErrors.NO_SESSION_AVAILABLE: ErrorCodeDetail(RepoErrors.NO_SESSION_AVAILABLE, 500, "No session available"),
    # --- DATABASE ---
    DatabaseErrors.UNSUPPORTED_BACKEND: ErrorCodeDetail(DatabaseErrors.UNSUPPORTED_BACKEND, 500, "Unsupported database backend"),
    DatabaseErrors.DRIVER_REQUIRED: ErrorCodeDetail(DatabaseErrors.DRIVER_REQUIRED, 500, "Database driver required"),
    DatabaseErrors.NOT_CONNECTED: ErrorCodeDetail(DatabaseErrors.NOT_CONNECTED, 503, "Database not connected"),
    DatabaseErrors.SESSION_NOT_INIT: ErrorCodeDetail(DatabaseErrors.SESSION_NOT_INIT, 500, "Database session not initialized"),
    # --- CONFIG ---
    ConfigErrors.LOAD_FAILED: ErrorCodeDetail(ConfigErrors.LOAD_FAILED, 500, "Failed to load config"),
    ConfigErrors.SAVE_FAILED: ErrorCodeDetail(ConfigErrors.SAVE_FAILED, 500, "Failed to save config"),
    # --- DRIVER ---
    DriverErrors.START_FAILED: ErrorCodeDetail(DriverErrors.START_FAILED, 500, "Failed to start driver"),
    DriverErrors.NOT_FOUND: ErrorCodeDetail(DriverErrors.NOT_FOUND, 404, "Driver not found"),
    DriverErrors.REGISTRY_NOT_INIT: ErrorCodeDetail(DriverErrors.REGISTRY_NOT_INIT, 500, "Driver registry not initialized"),
    DriverErrors.LIST_FAILED: ErrorCodeDetail(DriverErrors.LIST_FAILED, 500, "Failed to list drivers"),
    DriverErrors.GET_FAILED: ErrorCodeDetail(DriverErrors.GET_FAILED, 500, "Failed to get driver"),
    DriverErrors.DISCOVER_FAILED: ErrorCodeDetail(DriverErrors.DISCOVER_FAILED, 500, "Device discovery failed"),
    # --- DATA ---
    DataErrors.UNSUPPORTED_AGGREGATE: ErrorCodeDetail(DataErrors.UNSUPPORTED_AGGREGATE, 400, "Unsupported aggregate type"),
    DataErrors.QUERY_FAILED: ErrorCodeDetail(DataErrors.QUERY_FAILED, 500, "Data query failed"),
    DataErrors.EXPORT_FAILED: ErrorCodeDetail(DataErrors.EXPORT_FAILED, 500, "Data export failed"),
    DataErrors.NO_DATA: ErrorCodeDetail(DataErrors.NO_DATA, 404, "No data available"),
    # --- VIDEO ---
    VideoErrors.PTZ_FAILED: ErrorCodeDetail(VideoErrors.PTZ_FAILED, 500, "PTZ control failed"),
    VideoErrors.WEBHOOK_FAILED: ErrorCodeDetail(VideoErrors.WEBHOOK_FAILED, 500, "Video webhook failed"),
    VideoErrors.API_KEY_INVALID: ErrorCodeDetail(VideoErrors.API_KEY_INVALID, 401, "Video API key is invalid"),
    VideoErrors.API_KEY_NOT_CONFIGURED: ErrorCodeDetail(VideoErrors.API_KEY_NOT_CONFIGURED, 401, "Video API key not configured"),
    # --- AUDIT ---
    AuditErrors.INVALID_ACTION: ErrorCodeDetail(AuditErrors.INVALID_ACTION, 400, "Invalid audit action"),
    AuditErrors.INVALID_TIME_FORMAT: ErrorCodeDetail(AuditErrors.INVALID_TIME_FORMAT, 400, "Invalid time format"),
    AuditErrors.NOT_ENABLED: ErrorCodeDetail(AuditErrors.NOT_ENABLED, 503, "Audit not enabled"),
    AuditErrors.CLEANUP_FAILED: ErrorCodeDetail(AuditErrors.CLEANUP_FAILED, 500, "Audit cleanup failed"),
    AuditErrors.EXPORT_FAILED: ErrorCodeDetail(AuditErrors.EXPORT_FAILED, 500, "Audit export failed"),
    AuditErrors.INTEGRITY_FAILED: ErrorCodeDetail(AuditErrors.INTEGRITY_FAILED, 500, "Audit integrity check failed"),
    AuditErrors.LIST_FAILED: ErrorCodeDetail(AuditErrors.LIST_FAILED, 500, "Failed to list audit logs"),
    # --- EXPRESSION ---
    ExpressionErrors.EVALUATE_FAILED: ErrorCodeDetail(ExpressionErrors.EVALUATE_FAILED, 500, "Expression evaluation failed"),
    ExpressionErrors.BATCH_EVALUATE_FAILED: ErrorCodeDetail(ExpressionErrors.BATCH_EVALUATE_FAILED, 500, "Batch expression evaluation failed"),
    ExpressionErrors.VALIDATE_FAILED: ErrorCodeDetail(ExpressionErrors.VALIDATE_FAILED, 400, "Expression validation failed"),
    # --- SCADA ---
    ScadaErrors.PROJECT_NOT_FOUND: ErrorCodeDetail(ScadaErrors.PROJECT_NOT_FOUND, 404, "SCADA project not found"),
    ScadaErrors.SAVE_FAILED: ErrorCodeDetail(ScadaErrors.SAVE_FAILED, 500, "Failed to save SCADA project"),
    ScadaErrors.DELETE_FAILED: ErrorCodeDetail(ScadaErrors.DELETE_FAILED, 500, "Failed to delete SCADA project"),
    ScadaErrors.LOAD_FAILED: ErrorCodeDetail(ScadaErrors.LOAD_FAILED, 500, "Failed to load SCADA project"),
    # --- PLATFORM ---
    PlatformErrors.CONFIG_SCHEMA_NOT_FOUND: ErrorCodeDetail(PlatformErrors.CONFIG_SCHEMA_NOT_FOUND, 404, "Platform config schema not found"),
    PlatformErrors.CONNECT_FAILED: ErrorCodeDetail(PlatformErrors.CONNECT_FAILED, 500, "Platform connection failed"),
    PlatformErrors.DISCONNECT_FAILED: ErrorCodeDetail(PlatformErrors.DISCONNECT_FAILED, 500, "Platform disconnection failed"),
    PlatformErrors.NOT_SUPPORTED: ErrorCodeDetail(PlatformErrors.NOT_SUPPORTED, 400, "Unsupported platform type"),
    PlatformErrors.MISSING_CONFIG: ErrorCodeDetail(PlatformErrors.MISSING_CONFIG, 400, "Missing platform config"),
    PlatformErrors.NOT_CONNECTED: ErrorCodeDetail(PlatformErrors.NOT_CONNECTED, 503, "Platform not connected"),
    PlatformErrors.VALIDATION_REQUIRED: ErrorCodeDetail(PlatformErrors.VALIDATION_REQUIRED, 400, "Required field is empty"),
    PlatformErrors.VALIDATION_BROKER_FORMAT: ErrorCodeDetail(PlatformErrors.VALIDATION_BROKER_FORMAT, 400, "Invalid broker format"),
    PlatformErrors.VALIDATION_PORT_RANGE: ErrorCodeDetail(PlatformErrors.VALIDATION_PORT_RANGE, 400, "Port out of range"),
    PlatformErrors.VALIDATION_PORT_NUMBER: ErrorCodeDetail(PlatformErrors.VALIDATION_PORT_NUMBER, 400, "Port must be a number"),
    PlatformErrors.VALIDATION_TOO_LONG: ErrorCodeDetail(PlatformErrors.VALIDATION_TOO_LONG, 400, "Field value too long"),
    PlatformErrors.VALIDATION_UNSUPPORTED: ErrorCodeDetail(PlatformErrors.VALIDATION_UNSUPPORTED, 400, "Unsupported field type"),
    # --- SYSTEM ---
    SystemErrors.STATUS_FAILED: ErrorCodeDetail(SystemErrors.STATUS_FAILED, 500, "Failed to get system status"),
    SystemErrors.BACKUP_LIST_FAILED: ErrorCodeDetail(SystemErrors.BACKUP_LIST_FAILED, 500, "Failed to list backups"),
    SystemErrors.BACKUP_CREATE_FAILED: ErrorCodeDetail(SystemErrors.BACKUP_CREATE_FAILED, 500, "Failed to create backup"),
    SystemErrors.INVALID_BACKUP_ID: ErrorCodeDetail(SystemErrors.INVALID_BACKUP_ID, 400, "Invalid backup ID"),
    SystemErrors.BACKUP_NOT_FOUND: ErrorCodeDetail(SystemErrors.BACKUP_NOT_FOUND, 404, "Backup not found"),
    SystemErrors.RESTORE_FAILED: ErrorCodeDetail(SystemErrors.RESTORE_FAILED, 500, "System restore failed"),
    # --- RULE ---
    RuleErrors.LIST_FAILED: ErrorCodeDetail(RuleErrors.LIST_FAILED, 500, "Failed to list rules"),
    RuleErrors.CREATE_FAILED: ErrorCodeDetail(RuleErrors.CREATE_FAILED, 500, "Failed to create rule"),
    RuleErrors.NOT_FOUND: ErrorCodeDetail(RuleErrors.NOT_FOUND, 404, "Rule not found"),
    RuleErrors.GET_FAILED: ErrorCodeDetail(RuleErrors.GET_FAILED, 500, "Failed to get rule"),
    RuleErrors.UPDATE_FAILED: ErrorCodeDetail(RuleErrors.UPDATE_FAILED, 500, "Failed to update rule"),
    RuleErrors.DELETE_FAILED: ErrorCodeDetail(RuleErrors.DELETE_FAILED, 500, "Failed to delete rule"),
    RuleErrors.ENABLE_FAILED: ErrorCodeDetail(RuleErrors.ENABLE_FAILED, 500, "Failed to enable rule"),
    RuleErrors.DISABLE_FAILED: ErrorCodeDetail(RuleErrors.DISABLE_FAILED, 500, "Failed to disable rule"),
    RuleErrors.TEST_FAILED: ErrorCodeDetail(RuleErrors.TEST_FAILED, 500, "Rule test failed"),
    RuleErrors.CONDITION_INVALID: ErrorCodeDetail(RuleErrors.CONDITION_INVALID, 400, "Rule condition is invalid"),
    # --- ALARM ---
    AlarmErrors.LIST_FAILED: ErrorCodeDetail(AlarmErrors.LIST_FAILED, 500, "Failed to list alarms"),
    AlarmErrors.NOT_FOUND: ErrorCodeDetail(AlarmErrors.NOT_FOUND, 404, "Alarm not found"),
    AlarmErrors.GET_FAILED: ErrorCodeDetail(AlarmErrors.GET_FAILED, 500, "Failed to get alarm"),
    AlarmErrors.ACK_FAILED: ErrorCodeDetail(AlarmErrors.ACK_FAILED, 500, "Failed to acknowledge alarm"),
    AlarmErrors.ALREADY_ACKNOWLEDGED: ErrorCodeDetail(AlarmErrors.ALREADY_ACKNOWLEDGED, 409, "Alarm already acknowledged"),
    AlarmErrors.ALREADY_RECOVERED: ErrorCodeDetail(AlarmErrors.ALREADY_RECOVERED, 409, "Alarm already recovered"),
    # --- INTEGRATION ---
    IntegrationErrors.HANDSHAKE_FAILED: ErrorCodeDetail(IntegrationErrors.HANDSHAKE_FAILED, 500, "Integration handshake failed"),
    IntegrationErrors.STATUS_FAILED: ErrorCodeDetail(IntegrationErrors.STATUS_FAILED, 500, "Failed to get integration status"),
    IntegrationErrors.RPC_EXECUTE_FAILED: ErrorCodeDetail(IntegrationErrors.RPC_EXECUTE_FAILED, 500, "RPC execution failed"),
    IntegrationErrors.RPC_HISTORY_FAILED: ErrorCodeDetail(IntegrationErrors.RPC_HISTORY_FAILED, 500, "RPC history query failed"),
    IntegrationErrors.BACKHAUL_NOT_READY: ErrorCodeDetail(IntegrationErrors.BACKHAUL_NOT_READY, 503, "Backhaul channel not ready"),
    IntegrationErrors.RPC_DEVICE_SERVICE_UNAVAILABLE: ErrorCodeDetail(IntegrationErrors.RPC_DEVICE_SERVICE_UNAVAILABLE, 503, "RPC device service unavailable"),
    IntegrationErrors.RPC_MISSING_VALUE: ErrorCodeDetail(IntegrationErrors.RPC_MISSING_VALUE, 400, "RPC missing value parameter"),
    IntegrationErrors.RPC_WRITE_FAILED: ErrorCodeDetail(IntegrationErrors.RPC_WRITE_FAILED, 500, "RPC write failed"),
    # --- CASCADE ---
    CascadeErrors.INVALID_CONFIG: ErrorCodeDetail(CascadeErrors.INVALID_CONFIG, 400, "Invalid cascade config"),
    CascadeErrors.TOPOLOGY_FAILED: ErrorCodeDetail(CascadeErrors.TOPOLOGY_FAILED, 500, "Failed to get topology"),
    CascadeErrors.NEIGHBORS_FAILED: ErrorCodeDetail(CascadeErrors.NEIGHBORS_FAILED, 500, "Failed to get neighbors"),
    CascadeErrors.CONFIG_UPDATE_FAILED: ErrorCodeDetail(CascadeErrors.CONFIG_UPDATE_FAILED, 500, "Failed to update cascade config"),
    CascadeErrors.NEIGHBOR_NOT_FOUND: ErrorCodeDetail(CascadeErrors.NEIGHBOR_NOT_FOUND, 404, "Neighbor not found"),
    CascadeErrors.REMOVE_FAILED: ErrorCodeDetail(CascadeErrors.REMOVE_FAILED, 500, "Failed to remove neighbor"),
    CascadeErrors.NOT_ENABLED: ErrorCodeDetail(CascadeErrors.NOT_ENABLED, 503, "Cascade not enabled"),
    # --- MCP ---
    McpErrors.DEVICE_SERVICE_UNAVAILABLE: ErrorCodeDetail(McpErrors.DEVICE_SERVICE_UNAVAILABLE, 503, "MCP device service unavailable"),
    McpErrors.MISSING_DEVICE_ID: ErrorCodeDetail(McpErrors.MISSING_DEVICE_ID, 400, "Missing device ID"),
    McpErrors.DEVICE_NOT_FOUND: ErrorCodeDetail(McpErrors.DEVICE_NOT_FOUND, 404, "MCP device not found"),
    McpErrors.MISSING_PARAMS: ErrorCodeDetail(McpErrors.MISSING_PARAMS, 400, "Missing required parameters"),
    McpErrors.ALARM_SERVICE_UNAVAILABLE: ErrorCodeDetail(McpErrors.ALARM_SERVICE_UNAVAILABLE, 503, "MCP alarm service unavailable"),
    McpErrors.SYSTEM_SERVICE_UNAVAILABLE: ErrorCodeDetail(McpErrors.SYSTEM_SERVICE_UNAVAILABLE, 503, "MCP system service unavailable"),
    McpErrors.RULE_SERVICE_UNAVAILABLE: ErrorCodeDetail(McpErrors.RULE_SERVICE_UNAVAILABLE, 503, "MCP rule service unavailable"),
    McpErrors.UNKNOWN_TOOL: ErrorCodeDetail(McpErrors.UNKNOWN_TOOL, 404, "Unknown MCP tool"),
    McpErrors.LIST_FAILED: ErrorCodeDetail(McpErrors.LIST_FAILED, 500, "Failed to list MCP tools"),
    McpErrors.CALL_FAILED: ErrorCodeDetail(McpErrors.CALL_FAILED, 500, "MCP tool call failed"),
    McpErrors.CREATE_KEY_FAILED: ErrorCodeDetail(McpErrors.CREATE_KEY_FAILED, 500, "Failed to create MCP key"),
    McpErrors.KEY_NOT_FOUND: ErrorCodeDetail(McpErrors.KEY_NOT_FOUND, 404, "MCP key not found"),
    McpErrors.SSE_FAILED: ErrorCodeDetail(McpErrors.SSE_FAILED, 500, "MCP SSE failed"),
    # --- OTA ---
    OtaErrors.NOT_ENABLED: ErrorCodeDetail(OtaErrors.NOT_ENABLED, 503, "OTA not enabled"),
    OtaErrors.CHECK_FAILED: ErrorCodeDetail(OtaErrors.CHECK_FAILED, 500, "OTA check failed"),
    OtaErrors.IN_PROGRESS: ErrorCodeDetail(OtaErrors.IN_PROGRESS, 409, "OTA upgrade in progress"),
    OtaErrors.NO_UPDATE: ErrorCodeDetail(OtaErrors.NO_UPDATE, 404, "No update available"),
    OtaErrors.NO_DOWNLOAD_URL: ErrorCodeDetail(OtaErrors.NO_DOWNLOAD_URL, 500, "No download URL"),
    OtaErrors.DOWNLOAD_FAILED: ErrorCodeDetail(OtaErrors.DOWNLOAD_FAILED, 500, "OTA download failed"),
    OtaErrors.APPLY_FAILED: ErrorCodeDetail(OtaErrors.APPLY_FAILED, 500, "OTA apply failed"),
    OtaErrors.ROLLBACK_FAILED: ErrorCodeDetail(OtaErrors.ROLLBACK_FAILED, 500, "OTA rollback failed"),
    OtaErrors.LIST_BACKUPS_FAILED: ErrorCodeDetail(OtaErrors.LIST_BACKUPS_FAILED, 500, "Failed to list OTA backups"),
    # --- AUTHZ ---
    AuthzErrors.NOT_AUTHENTICATED: ErrorCodeDetail(AuthzErrors.NOT_AUTHENTICATED, 401, "Not authenticated"),
    AuthzErrors.PERMISSION_DENIED: ErrorCodeDetail(AuthzErrors.PERMISSION_DENIED, 403, "Permission denied"),
    # --- AI ---
    AiErrors.ENGINE_NOT_INITIALIZED: ErrorCodeDetail(AiErrors.ENGINE_NOT_INITIALIZED, 503, "AI engine not initialized"),
    AiErrors.MODEL_NOT_FOUND: ErrorCodeDetail(AiErrors.MODEL_NOT_FOUND, 404, "AI model not found"),
    AiErrors.MODEL_LOAD_FAILED: ErrorCodeDetail(AiErrors.MODEL_LOAD_FAILED, 500, "AI model load failed"),
    AiErrors.MODEL_RELOAD_FAILED: ErrorCodeDetail(AiErrors.MODEL_RELOAD_FAILED, 500, "AI model reload failed"),
    AiErrors.MODEL_DELETE_PRESET: ErrorCodeDetail(AiErrors.MODEL_DELETE_PRESET, 409, "Cannot delete preset model"),
    AiErrors.MODEL_ALREADY_LOADED: ErrorCodeDetail(AiErrors.MODEL_ALREADY_LOADED, 409, "Model already loaded"),
    AiErrors.MODEL_FILE_NOT_FOUND: ErrorCodeDetail(AiErrors.MODEL_FILE_NOT_FOUND, 404, "Model file not found"),
    AiErrors.MODEL_CANNOT_LOAD: ErrorCodeDetail(AiErrors.MODEL_CANNOT_LOAD, 500, "Model cannot be loaded"),
    AiErrors.MODEL_PREVIOUS_ERROR: ErrorCodeDetail(AiErrors.MODEL_PREVIOUS_ERROR, 500, "Model has previous error"),
    AiErrors.MODEL_ENABLE_FAILED: ErrorCodeDetail(AiErrors.MODEL_ENABLE_FAILED, 500, "Model enable failed"),
    AiErrors.INFERENCE_FAILED: ErrorCodeDetail(AiErrors.INFERENCE_FAILED, 500, "Inference failed"),
    AiErrors.INFERENCE_TIMEOUT: ErrorCodeDetail(AiErrors.INFERENCE_TIMEOUT, 504, "Inference timeout"),
    AiErrors.STATS_FAILED: ErrorCodeDetail(AiErrors.STATS_FAILED, 500, "AI stats query failed"),
    AiErrors.LIST_FAILED: ErrorCodeDetail(AiErrors.LIST_FAILED, 500, "Failed to list AI models"),
    AiErrors.GET_FAILED: ErrorCodeDetail(AiErrors.GET_FAILED, 500, "Failed to get AI model"),
    AiErrors.UPDATE_FAILED: ErrorCodeDetail(AiErrors.UPDATE_FAILED, 500, "Failed to update AI model"),
    AiErrors.DELETE_FAILED: ErrorCodeDetail(AiErrors.DELETE_FAILED, 500, "Failed to delete AI model"),
    AiErrors.ENABLE_FAILED: ErrorCodeDetail(AiErrors.ENABLE_FAILED, 500, "Failed to enable AI model"),
    AiErrors.DISABLE_FAILED: ErrorCodeDetail(AiErrors.DISABLE_FAILED, 500, "Failed to disable AI model"),
    AiErrors.ONNX_NOT_AVAILABLE: ErrorCodeDetail(AiErrors.ONNX_NOT_AVAILABLE, 503, "ONNX Runtime not available"),
    AiErrors.ONNXRUNTIME_NOT_INSTALLED: ErrorCodeDetail(AiErrors.ONNXRUNTIME_NOT_INSTALLED, 503, "ONNX Runtime not installed"),
    AiErrors.MODEL_IS_LOADING: ErrorCodeDetail(AiErrors.MODEL_IS_LOADING, 409, "Model is loading"),
    AiErrors.INVALID_INPUT_DATA: ErrorCodeDetail(AiErrors.INVALID_INPUT_DATA, 400, "Invalid input data"),
    AiErrors.INTERNAL_ERROR: ErrorCodeDetail(AiErrors.INTERNAL_ERROR, 500, "AI internal error"),
    AiErrors.SCHEDULE_ALREADY_EXISTS: ErrorCodeDetail(AiErrors.SCHEDULE_ALREADY_EXISTS, 409, "AI schedule already exists"),
    AiErrors.SCHEDULE_NOT_FOUND: ErrorCodeDetail(AiErrors.SCHEDULE_NOT_FOUND, 404, "AI schedule not found"),
    AiErrors.SCHEDULE_START_FAILED: ErrorCodeDetail(AiErrors.SCHEDULE_START_FAILED, 500, "Failed to start AI schedule"),
    AiErrors.SCHEDULE_NO_DATA: ErrorCodeDetail(AiErrors.SCHEDULE_NO_DATA, 404, "No data for AI schedule"),
    # --- GRAFANA ---
    GrafanaErrors.NOT_ENABLED: ErrorCodeDetail(GrafanaErrors.NOT_ENABLED, 503, "Grafana not enabled"),
    GrafanaErrors.API_KEY_MISSING: ErrorCodeDetail(GrafanaErrors.API_KEY_MISSING, 401, "Grafana API key missing"),
    GrafanaErrors.BAD_STATUS: ErrorCodeDetail(GrafanaErrors.BAD_STATUS, 502, "Grafana returned bad status"),
    GrafanaErrors.DEPS_MISSING: ErrorCodeDetail(GrafanaErrors.DEPS_MISSING, 503, "Grafana dependencies missing"),
    GrafanaErrors.CONNECTION_FAILED: ErrorCodeDetail(GrafanaErrors.CONNECTION_FAILED, 502, "Grafana connection failed"),
    GrafanaErrors.INVALID_UID: ErrorCodeDetail(GrafanaErrors.INVALID_UID, 400, "Invalid Grafana UID"),
    # --- STORAGE ---
    StorageErrors.INFLUXDB_UNAVAILABLE: ErrorCodeDetail(StorageErrors.INFLUXDB_UNAVAILABLE, 503, "InfluxDB is unavailable"),
    StorageErrors.SQLITE_ERROR: ErrorCodeDetail(StorageErrors.SQLITE_ERROR, 500, "SQLite error"),
    StorageErrors.CACHE_OVERFLOW: ErrorCodeDetail(StorageErrors.CACHE_OVERFLOW, 507, "Cache overflow"),
    StorageErrors.SYNC_FAILED: ErrorCodeDetail(StorageErrors.SYNC_FAILED, 500, "Storage sync failed"),
    # --- NETWORK ---
    NetworkErrors.TIMEOUT: ErrorCodeDetail(NetworkErrors.TIMEOUT, 504, "Network timeout"),
    NetworkErrors.CONNECTION_REFUSED: ErrorCodeDetail(NetworkErrors.CONNECTION_REFUSED, 502, "Connection refused"),
    NetworkErrors.DNS_FAILED: ErrorCodeDetail(NetworkErrors.DNS_FAILED, 502, "DNS resolution failed"),
    # --- PROTOCOL ---
    ProtocolErrors.MODBUS_CRC: ErrorCodeDetail(ProtocolErrors.MODBUS_CRC, 400, "Modbus CRC check failed"),
    ProtocolErrors.S7_PDU: ErrorCodeDetail(ProtocolErrors.S7_PDU, 400, "S7 PDU error"),
    ProtocolErrors.OPCUA_SESSION: ErrorCodeDetail(ProtocolErrors.OPCUA_SESSION, 400, "OPC UA session error"),
    ProtocolErrors.MQTT_AUTH: ErrorCodeDetail(ProtocolErrors.MQTT_AUTH, 401, "MQTT authentication failed"),
}


def make_error_response(error_code: str, detail: str = "", http_status: int = 400) -> dict:
    """Create a standardized error response dict.

    Args:
        error_code: Business error code (e.g. ERR_DEVICE_NOT_FOUND)
        detail: Optional custom detail message, overrides default message if provided
        http_status: Fallback HTTP status code if error_code not found in map

    Returns:
        Standardized error response dict with code, message, data, error_code fields
    """
    err = ERROR_CODE_MAP.get(error_code)
    if err:
        return {
            "code": err.http_status,
            "message": detail or err.message,
            "data": None,
            "error_code": error_code,
        }
    return {
        "code": http_status,
        "message": detail or "Unknown error",
        "data": None,
        "error_code": error_code,
    }
