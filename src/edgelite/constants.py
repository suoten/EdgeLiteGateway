"""EdgeLite 全局常量定义模块。

集中管理 MQTT、HTTP、超时、协议别名等跨模块共享的常量，
避免魔法数字分散在各业务代码中。
"""

_MQTT_KEEPALIVE = 60
_MQTT_QUEUE_MAXSIZE = 10000
_MQTT_RECONNECT_DELAY = 5
_MQTT_HEARTBEAT_INTERVAL = 1
_MQTT_QUEUE_POLL_INTERVAL = 0.1
_QUEUE_POLL_TIMEOUT = 1.0  # FIXED: 原问题-17处timeout=1.0魔法数字跨模块重复，提取为常量

_HTTP_TIMEOUT = 10.0
_OTA_DOWNLOAD_TIMEOUT = 60.0
_NOTIFY_HTTP_TIMEOUT = 10.0
_NOTIFY_SMTP_TIMEOUT = 15

_DEVICE_CONNECT_TIMEOUT = 2.0
_SERIAL_READ_TIMEOUT = 0.1
_SERIAL_WRITE_WAIT = 0.1  # FIXED: P2-3 串口写入后等待时间
_SERIAL_POLL_INTERVAL = 0.05  # FIXED: P2-3 原魔法数字 0.05，串口轮询间隔
_SERIAL_RETRY_DELAY = 0.5
_SERIAL_BRIDGE_RAW_POLL_INTERVAL = 0.01  # FIXED: P2-3 串口读取循环轮询间隔
_SERIAL_BRIDGE_ERROR_RECOVERY_DELAY = 0.1  # FIXED: P2-3 串口TCP转发错误恢复延迟

_INFLUX_CONNECT_TIMEOUT_MS = 5000
_INFLUX_WRITE_TIMEOUT_S = 5.0

_CACHE_MAX_SIZE = 100_000
_CACHE_BATCH_LIMIT = 500
_CACHE_FLUSH_MAX_RETRIES = 3  # FIXED: P2-3 缓存回写最大重试次数，防止单次失败导致永久停止
_EVENT_BUS_MAX_QUEUE = 10000
_PREPROCESSOR_MAX_POINTS = 10000
_TOKEN_REVOCATION_MAX = 100000
_INTEGRATION_SESSION_TTL = 300

_RULE_CACHE_TTL = 5.0
_POINT_VALUE_CACHE_TTL = 300.0
_POINT_VALUE_CACHE_MAX = 10000

_EXPRESSION_EVAL_MAX_WORKERS = 4  # FIXED: 表达式引擎线程池大小 (原硬编码 2，扩容至 4)
_EXPRESSION_EVAL_TIMEOUT = 5.0  # FIXED: 表达式引擎 eval 超时时间 (秒)

_AUTH_MAX_ATTEMPTS = 5
_AUTH_ATTEMPTS_LIMIT = 10000
_AUTH_PASSWORD_MAX_LENGTH = 128
_AUTH_LOGIN_WINDOW_SECONDS = 300  # FIXED-H03: Rate limiting window for login attempts
# FIXED-H01: Rate limiting windows for password reset requests
_AUTH_RESET_IP_WINDOW_SECONDS = 3600  # 1 hour: max 5 requests per IP
_AUTH_RESET_USER_WINDOW_SECONDS = 3600  # 1 hour: max 3 requests per username
_AUTH_RESET_IP_MAX = 5  # Max reset requests per IP per window
_AUTH_RESET_USER_MAX = 3  # Max reset requests per username per window
# FIXED-H03: Rate limiting for password reset usage endpoint
# FIXED-P2: 原问题-_AUTH_RESET_IP_WINDOW_SECONDS在第41行和第46行重复定义，第二次覆盖第一次；
# 值相同(3600)无功能影响但注释语义冲突，属于复制粘贴错误。移除重复定义，usage端点复用同一窗口常量。
_AUTH_RESET_IP_MAX_ATTEMPTS = 3  # Max password reset attempts per IP per window
_EXPRESSION_MAX_LENGTH = 2048
_EXPRESSION_BATCH_LIMIT = 50

_DEFAULT_PAGE_SIZE = 20
_MAX_QUERY_SIZE = 5000  # FIXED: 原问题-1000过小，前端下拉选择场景需加载全量数据，放宽至5000
_EXPORT_QUERY_SIZE = 10000
_EXPORT_MAX_RECORDS = 100_000  # FIXED-P2: 数据导出最大记录数，防止OOM
_MCP_QUERY_SIZE = 200

_SCHEDULER_INTERVAL = 30
_MQTT_FORWARDER_RECONNECT = 30
_MQTT_DRIVER_RECONNECT = 30
_SPARKPLUG_RECONNECT_MAX_DELAY = 30.0
_PLATFORM_RECONNECT_MAX_BACKOFF = 60
_INTEGRATION_MAX_SESSIONS = 10
_TOKEN_REVOCATION_DEFAULT_TTL = 86400
_CACHE_EVICTION_RATIO = 10
_SHORT_ID_LENGTH = 16

# FIXED-AUTO-BACKUP: Auto backup scheduling constants
_AUTO_BACKUP_INTERVAL_SECONDS = 86400  # daily (24 hours)
_AUTO_BACKUP_MAX_RETENTION = 50  # max backup files to keep
_AUTO_BACKUP_HOUR = 2  # 02:00 AM for daily backups

_OTA_DOWNLOAD_CHUNK = 8192
_ONVIF_MULTICAST_PORT = 3702
_ONVIF_MULTICAST_TTL = 4
_ALLEN_BRADLEY_DEFAULT_PORT = 2222

# P4-47 Architectural constants
_NORTH_QUEUE_MAX_SIZE = 10000
_NORTH_RETRY_INITIAL_BACKOFF = 1.0
_NORTH_RETRY_MAX_BACKOFF = 60.0
_NORTH_RETRY_MAX_ATTEMPTS = 10
_NORTH_POOL_MAX_CONNECTIONS = 5
_NORTH_POOL_IDLE_TIMEOUT = 300
_NORTH_POOL_PROBE_INTERVAL = 60
_NORTH_BATCH_DEFAULT_SIZE = 100
_NORTH_PERIODIC_DEFAULT_INTERVAL = 10.0
_NORTH_COMPRESS_THRESHOLD = 1024
_NORTH_DEDUP_WINDOW_SECONDS = 300
_NORTH_MESSAGE_PREVIEW_MAX = 50
_NORTH_MQTT5_PROTOCOL = 5
_NORTH_MQTT311_PROTOCOL = 4

_NORTH_PERSIST_DB_NAME = "north_queue.db"
_NORTH_PERSIST_FLUSH_INTERVAL = 5.0
_NORTH_PERSIST_FLUSH_BATCH_SIZE = 100
_NORTH_PERSIST_DEQUEUE_BATCH_SIZE = 50
_NORTH_PERSIST_MEMORY_THRESHOLD = 1000
_NORTH_PERSIST_MEMORY_MAX = 100_000
_NORTH_PERSIST_DISK_MAX = 1_000_000
_NORTH_PERSIST_SENT_TTL_DAYS = 7
_NORTH_PERSIST_FAILED_TTL_DAYS = 30
_NORTH_PERSIST_STUCK_SENT_TIMEOUT = 300.0  # FIXED-P1#13: 'sent' 状态超过此秒数视为卡住，初始化时恢复为 'pending'

_ACK_TIMEOUT_SECONDS = 30.0
_ACK_MAX_RETRIES = 10
_ACK_MAX_INFLIGHT = 10000  # FIXED-P0: AckTracker _inflight 上限，防止北向平台不可达时OOM
_ACK_DEDUP_WINDOW_SECONDS = 86400
_ACK_TRACKER_FLUSH_INTERVAL = 5.0
_DLQ_ALERT_RATE_PER_MINUTE = 10
_DLQ_MAX_ENTRIES = 100_000

_BP_LEVEL_NORMAL = "normal"
_BP_LEVEL_WARNING = "warning"
_BP_LEVEL_DANGER = "danger"
_BP_LEVEL_CRITICAL = "critical"
_BP_THRESHOLD_WARNING = 0.6
_BP_THRESHOLD_DANGER = 0.8
_BP_THRESHOLD_CRITICAL = 0.95
_BP_RESUME_THRESHOLD = 0.5
_BP_RESUME_HOLD_SECONDS = 30.0
_BP_SLOW_DOWN_WARNING_FACTOR = 0.8
_BP_SLOW_DOWN_DANGER_FACTOR = 0.5
_BP_TOKEN_BUCKET_RATE = 1000.0
_BP_TOKEN_BUCKET_BURST = 2000.0
_BP_HISTORY_MAX_EVENTS = 1000

_CB_STATE_CLOSED = "closed"
_CB_STATE_OPEN = "open"
_CB_STATE_HALF_OPEN = "half_open"
_CB_FAILURE_THRESHOLD = 5
_CB_ERROR_RATE_THRESHOLD = 0.5
_CB_ERROR_RATE_WINDOW_SECONDS = 60.0
_CB_INITIAL_OPEN_DURATION = 30.0
_CB_MAX_OPEN_DURATION = 600.0
_CB_OPEN_DURATION_MULTIPLIER = 2.0
_CB_HALF_OPEN_PROBE_INTERVAL = 30.0
_CB_HALF_OPEN_SUCCESS_THRESHOLD = 3
_HP_NORMAL_INTERVAL = 30.0
_HP_ABNORMAL_INTERVAL = 5.0
_HP_MQTT_PING_TIMEOUT = 10.0
_HP_HTTP_TIMEOUT = 5.0
_RC_INITIAL_BACKOFF = 1.0
_RC_MAX_BACKOFF = 60.0
_RC_BACKOFF_MULTIPLIER = 1.5
_RC_JITTER_FACTOR = 0.5
_RC_GLOBAL_MAX_CONCURRENT = 3

_OBS_LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_OBS_LATENCY_MAX_SAMPLES = 10000
_OBS_TRACE_MAX_TRACES = 100000
_OBS_TRACE_DB_NAME = "message_traces.db"
_OBS_ALERT_DB_NAME = "alert_events.db"
_OBS_ALERT_COOLDOWN_SECONDS = 300
_OBS_ALERT_MAX_EVENTS = 10000
_OBS_OTEL_SAMPLE_RATE = 0.01
_OBS_OTEL_SERVICE_NAME = "edgelite-gateway"
_OBS_K8S_STARTUP_DELAY_SECONDS = 30
_OBS_K8S_LIVE_TIMEOUT_SECONDS = 5
_OBS_K8S_READY_TIMEOUT_SECONDS = 10

_TB_HTTP_POOL_SIZE = 50
_TB_RPC_TIMEOUT = 30.0
_TB_DEVICE_CACHE_TTL = 300
_TB_ALARM_SEVERITY_MAP = {
    "critical": "CRITICAL",
    "major": "MAJOR",
    "minor": "MINOR",
    "warning": "WARNING",
    "indeterminate": "INDETERMINATE",
}

_SQLITE_BUSY_TIMEOUT = 5000
_SQLITE_WAL_MODE = "wal"
# FIXED: 补充 synchronous 常量，确保所有 SQLite 连接统一配置 synchronous=NORMAL [2026-06-29]
# WAL+synchronous=NORMAL 是性能与数据安全的平衡点：WAL 提供并发读，NORMAL 避免每次 commit fsync
_SQLITE_SYNCHRONOUS = "NORMAL"

_KDF_SALT_ENV = "EDGELITE_KDF_SALT"
_ENCRYPTION_KEY_ENV = "EDGELITE_ENCRYPTION_KEY"

_SCRIPT_SANDBOX_TIMEOUT = 5.0
_SCRIPT_SANDBOX_MEMORY_MB = 64
_CUSTOM_MQTT_MAX_BROKERS = 10

# FIXED(编号6/7/8): 级联转发安全认证常量
_CASCADE_TOKEN_ENV = "EDGELITE_CASCADE_TOKEN"  # 级联共享密钥环境变量名
_CASCADE_HOP_LIMIT = 16  # 拓扑环检测最大跳数，超过则丢弃并告警
_CASCADE_TOKEN_TTL = 300  # 级联签名时间戳容差窗口(秒)，防止重放攻击
_CASCADE_TOKEN_HASH_LEN = 16  # mDNS 广播中 token_hash 的截取长度

_LOG_DIR = "logs"  # FIXED: P4-47 log directory hardcoded, now extracted to constant
_LOG_MAX_BYTES = 50 * 1024 * 1024  # FIXED: P4-47 magic number 50MB
_LOG_BACKUP_COUNT = 10  # FIXED: P4-47 magic number 10

# FIXED-PROTOCOL: Unified device protocol definitions — single source of truth for all
# protocol validation across sqlite_repo, system_services, and Alembic migrations.
# Naming convention: underscore (modbus_tcp, opc_ua, ethernet_ip).
# Backward compatibility: old hyphen-style names (modbus-tcp, opcua) are mapped on read.
VALID_DEVICE_PROTOCOLS = frozenset(
    {
        "modbus_tcp",
        "modbus_rtu",
        "simulator",
        "mqtt_client",
        "http_webhook",
        "opc_ua",
        "siemens_s7",
        "mitsubishi_mc",
        "omron_fins",
        "allen_bradley",
        "opc_da",
        "onvif",
        "modbus_slave",
    }
)

# Legacy name → canonical name mapping (hyphen-style → underscore-style)
# R11-DRV-07: 作为协议别名的单一数据源，protocol_keys.py 直接复用本表，避免两套并行系统不一致
_PROTOCOL_ALIASES: dict[str, str] = {
    # hyphen-style (old)
    "modbus-tcp": "modbus_tcp",
    "modbus-rtu": "modbus_rtu",
    "opcua": "opc_ua",
    "ethernet-ip": "allen_bradley",
    "mqtt": "mqtt_client",
    "opc-da": "opc_da",
    # short-form (old)
    "s7": "siemens_s7",
    "mc": "mitsubishi_mc",
    "fins": "omron_fins",
    "ab": "allen_bradley",
    "http": "http_webhook",
    "opc_da_client": "opc_da",
}


def normalize_protocol(protocol: str) -> str | None:
    """Normalize a protocol name to the canonical form.

    Returns the canonical protocol name if valid, or None if unknown.
    Handles both legacy hyphen-style and current underscore-style names.
    """
    if protocol in VALID_DEVICE_PROTOCOLS:
        return protocol
    return _PROTOCOL_ALIASES.get(protocol)
