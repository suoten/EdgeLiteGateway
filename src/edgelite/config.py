"""EdgeLiteGateway 配置加载模块"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from edgelite.security.secret_manager import get_secret_manager

logger = logging.getLogger(__name__)

# 关键配置项路径定义：变更时需要记录日志并通知相关服务
_SENSITIVE_CONFIG_PATHS: dict[str, str] = {
    "mqtt.broker": "MQTT Broker地址",
    "mqtt.port": "MQTT Broker端口",
    "mqtt.username": "MQTT用户名",
    "mqtt.password": "MQTT密码",
    "influxdb.url": "InfluxDB地址",
    "influxdb.token": "InfluxDB Token",
    "influxdb.org": "InfluxDB组织",
    "influxdb.bucket": "InfluxDB Bucket",
    "security.secret_key": "安全密钥",
    "security.algorithm": "JWT算法",
    "security.access_token_expire_minutes": "AccessToken过期时间（分钟）",
    "security.refresh_token_expire_days": "RefreshToken过期时间（天）",
    "security.max_token_ttl_days": "Token TTL上限（天），防止过长过期时间",
    "server.webhook_api_key": "Webhook API Key",
    "database.host": "数据库主机",
    "database.port": "数据库端口",
    "database.username": "数据库用户名",
    "database.password": "数据库密码",
}

# 需要在持久化时加密、加载时解密的真正敏感字段（密码/Token/密钥）路径
# 仅包含字符串型敏感字段，避免对端口/地址等非敏感字段加密
_ENCRYPTED_SECRET_PATHS: list[str] = [
    "mqtt.password",
    "influxdb.token",
    "security.secret_key",
    "database.password",
    "notify.dingtalk.secret",
    "notify.email.smtp_password",
    "server.webhook_api_key",
    "video.pygbsentry.api_key",
    "mqtt_server.password",
    "webhook_auth.token",
    "webhook_auth.password",
    "grafana.api_key",
]

# 脱敏兜底路径（当 SecretManager.mask_config 不可用时使用）
_SENSITIVE_MASK_PATHS: list[str] = [
    "mqtt.password",
    "mqtt.username",
    "influxdb.token",
    "security.secret_key",
    "database.password",
    "database.username",
    "notify.dingtalk.secret",
    "notify.email.smtp_password",
    "notify.email.smtp_user",
    "server.webhook_api_key",
    "video.pygbsentry.api_key",
    "mqtt_server.password",
    "mqtt_server.username",
    "webhook_auth.token",
    "webhook_auth.password",
    "webhook_auth.username",
    "grafana.api_key",
]

# 配置变更回调列表
_config_change_callbacks: list[Callable[[dict[str, Any]], Any]] = []
_callbacks_lock = threading.Lock()  # FIXED-P1: 原问题-_config_change_callbacks无并发保护，追加与遍历并发可RuntimeError

# FIXED(安全): 已知不安全默认值黑名单——生产环境下密钥/Token/CSRF密钥不得使用这些值
# 用于 SecurityConfig.secret_key 校验与生产环境 CSRF_SECRET 校验
_INSECURE_DEFAULT_VALUES: set[str] = {
    "changeme",
    "change-me",
    "change_me",
    "please-change-me",
    "please_change_me",
    "pleasechangethis",
    "pleasechangemebeforedeploy_2024!",
    "secret",
    "secret-key",
    "secretkey",
    "your-secret-key",
    "your_secret_key",
    "your-secret-key-here",
    "your-csrf-secret",
    "your_csrf_secret",
    "your-csrf-secret-here",
    "example",
    "default",
    "test",
    "placeholder",
    "admin",
    "password",
    "123456",
    "edgelite",
    "edgelite-secret",
    "admin@2026",
}


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"  # FIXED-P0: 默认仅监听localhost，Docker部署通过环境变量覆盖为0.0.0.0
    port: int = Field(default=8080, ge=1, le=65535)
    # FIXED-H04: 收紧 CORS 配置
    # 通过 EDGELITE_SERVER__CORS_ALLOWED_ORIGINS 环境变量配置，逗号分隔
    # 示例: http://localhost:3000,http://localhost:5173,https://app.example.com
    # 生产环境必须显式配置，不允许内网 IP 段通配
    cors_allowed_origins: list[str] = Field(default_factory=list)
    cors_origins: list[str] = ["http://localhost:3000"]  # 保留用于兼容
    webhook_api_key: str = ""
    debug_api_enabled: bool = False
    debug_api_allowed_ips: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "::1"]
    )  # FIXED(安全): 默认仅允许本机访问 Debug API，空列表=拒绝所有
    # FIXED-P0: 可信代理 IP 列表，用于防止 X-Forwarded-For 头伪造绕过速率限制
    # 只有直接客户端 IP 在此列表中时，才信任 X-Forwarded-For/X-Real-IP 头
    # 支持单个 IP（如 "127.0.0.1"）或 CIDR 格式（如 "10.0.0.0/8"）
    # 默认空列表表示不信任任何代理，直接使用 request.client.host
    trusted_proxies: list[str] = Field(default_factory=list)
    # FIXED(安全): TrustedHostMiddleware 允许的 Host 头列表
    # 通过 EDGELITE_SERVER__ALLOWED_HOSTS 配置，分号分隔（如 example.com;api.example.com）
    # 生产环境（DEV_MODE=false）下配置后启用 TrustedHostMiddleware，防止 Host 头伪造攻击
    allowed_hosts: list[str] = Field(default_factory=list)


class DatabaseConfig(BaseModel):
    backend: str = "sqlite"
    sqlite_path: str = "data/edgelite.db"
    host: str = "localhost"
    port: int = Field(default=3306, ge=1, le=65535)  # FIXED-P1: 原问题-port无范围约束，可配置非法端口号
    username: str = ""
    password: str = ""
    database: str = "edgelite"
    backup_dir: str = "data/backups"
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    echo: bool = False
    optimistic_lock_retries: int = Field(default=3, ge=1, le=20)  # FIXED-P4: 原问题-乐观锁重试次数硬编码3；改为可配置
    trust_server_certificate: bool = False  # FIXED-P4: MSSQL TrustServerCertificate，默认False（不跳过证书验证）
    # A-04: 慢查询阈值（秒），超过此值的 SQL 查询将被记录到 db_monitor 与日志中。
    # 可通过 EDGELITE_DATABASE__SLOW_QUERY_THRESHOLD_S 环境变量或配置文件覆盖。
    slow_query_threshold_s: float = Field(default=1.0, gt=0.0, description="慢查询阈值（秒）")


class DownsampleConfig(BaseModel):
    """Data downsample configuration"""

    enabled: bool = True
    tier1_age_days: int = Field(default=7, ge=1, description="Age threshold for 1min->5min downsample")
    tier2_age_days: int = Field(default=30, ge=1, description="Age threshold for 5min->1hour downsample")
    tier3_age_days: int = Field(default=90, ge=1, description="Age threshold for 1hour->1day downsample")
    auto_run: bool = False
    run_interval_hours: int = Field(default=24, ge=1, le=168)


class InfluxDBConfig(BaseModel):
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "edgelite"
    bucket: str = "edgelite"
    batch_size: int = 1000
    flush_interval: int = 5000
    retention_days: int = Field(default=30, ge=1, le=3650)
    fallback_backend: str = "sqlite"
    sqlite_ts_path: str = "data/edgelite_ts.db"
    auto_sync_on_recovery: bool = True
    sync_batch_size: int = Field(default=500, ge=10, le=5000)
    sync_interval: int = Field(default=30, ge=5, le=300)
    downsample: DownsampleConfig = Field(default_factory=lambda: DownsampleConfig())
    # R11-DRV-13: 网络探测URL，原代码硬编码 baidu.com 海外部署不可用，改为可配置
    network_probe_url: str = "https://www.baidu.com"


class CacheConfig(BaseModel):
    """环形缓冲区与增量同步配置"""

    ring_buffer_capacity: int = Field(default=100000, ge=1000)
    ring_buffer_compress: bool = True
    incremental_sync_enabled: bool = True
    high_watermark_pct: float = Field(default=0.8, ge=0.5, le=1.0)
    critical_watermark_pct: float = Field(default=0.9, ge=0.6, le=1.0)


class MQTTConfig(BaseModel):
    broker: str = "localhost"
    port: int = Field(default=1883, ge=1, le=65535)
    username: str = ""
    password: str = ""
    topic_prefix: str = "edgelite"
    offline_cache_enabled: bool = True
    offline_db_path: str = "data/mqtt_offline_queue.db"
    max_queue_size: int = Field(default=10000, ge=100)
    max_retries: int = Field(default=100, ge=1)
    retry_interval: float = Field(default=5.0, ge=1.0)
    ring_buffer_capacity: int = Field(default=50000, ge=1000)
    ring_buffer_compress: bool = True


class PyGBSentryConfig(BaseModel):
    endpoint: str = ""
    api_key: str = ""
    timeout: int = 10


class VideoConfig(BaseModel):
    pygbsentry: PyGBSentryConfig = PyGBSentryConfig()


class SecurityConfig(BaseModel):
    secret_key: str = ""
    access_token_expire_minutes: int = Field(default=30, ge=1)
    refresh_token_expire_days: int = Field(default=7, ge=1)
    algorithm: str = "HS256"
    # FIXED: Token TTL 上限，防止过长过期时间
    max_token_ttl_days: int = Field(default=30, ge=1)
    # FIXED: JWT 密钥轮换支持（kid header）[2026-06-29]
    # secret_key_previous 用于轮换过渡期验证旧 token；key_id 标识当前密钥
    # 轮换流程：1.将旧 secret_key 移到 secret_key_previous 2.设置新 secret_key+key_id
    # 过渡期后清除 secret_key_previous
    secret_key_previous: str = ""
    key_id: str = "default"
    previous_key_id: str = ""
    rate_limit_requests_per_minute: int = Field(default=120, ge=1)
    rate_limit_login_per_minute: int = Field(default=5, ge=1)
    login_lockout_threshold: int = Field(default=5, ge=1)
    login_lockout_minutes: int = Field(default=15, ge=1)
    # FIXED-M03: Global login protection
    # 用户名级别全局锁定阈值（15分钟内）
    global_lockout_threshold: int = Field(default=10, ge=1)
    # 用户名级别全局锁定时长（分钟）
    global_lockout_duration: int = Field(default=30, ge=1)
    # 全局失败率阈值（每分钟）
    global_failure_rate_threshold: int = Field(default=50, ge=1)
    # 全局锁定窗口（分钟）
    global_lockout_window: int = Field(default=15, ge=1)
    # FIXED-M04: 受保护的角色的敏感字段修改保护
    # 这些角色的用户无法通过 API 修改敏感字段（password, enabled, role, must_change_password）
    protected_roles: list[str] = Field(default_factory=lambda: ["admin"])
    # FIXED(F5): CSRF 签名密钥（独立于 JWT secret_key，避免密钥复用）。
    # 为空时回退到 secret_key（向后兼容，不拒绝启动）；显式配置即与 JWT 密钥分离。
    csrf_secret: str = ""
    # FIXED(F2): CSRF cookie 的 Secure 标记。生产环境（HTTPS 部署）应置 True，
    # 确保 cookie 仅通过加密通道传输。默认 False（开发/反向代理终结 TLS 场景）。
    cookie_secure: bool = False

    # 复用模块级不安全默认值黑名单，并补充带尖括号的占位符形式
    _PLACEHOLDER_VALUES = _INSECURE_DEFAULT_VALUES | {
        "<your-secret-key-here>",
        "<your-csrf-secret-here>",
        "<your-secret-key>",
        "<changeme>",
    }

    @model_validator(mode="after")
    def _validate_secret_key(self):
        if not self.secret_key:
            raise ValueError(
                "security.secret_key is empty — JWT tokens can be forged! "
                "Set EDGELITE_SECURITY__SECRET_KEY environment variable."
            )
        if self.secret_key.startswith("${") and self.secret_key.endswith("}"):
            raise ValueError(
                f"security.secret_key contains unresolved env placeholder '{self.secret_key}' — "
                f"JWT tokens can be forged with a known placeholder! "
                f"Set EDGELITE_SECURITY__SECRET_KEY environment variable."
            )
        if (
            self.secret_key.strip("<>") in self._PLACEHOLDER_VALUES
            or self.secret_key.lower() in self._PLACEHOLDER_VALUES
        ):
            raise ValueError(
                f"security.secret_key is set to a placeholder value '{self.secret_key}' — "
                f"This is insecure! Generate a random key: "
                f'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if len(self.secret_key) < 32:
            raise ValueError(
                f"security.secret_key is too short ({len(self.secret_key)} chars, minimum 32) — "
                f"JWT tokens can be brute-forced! Generate a random key: "
                f'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        # FIXED(F5): csrf_secret 仅在非空时校验强度（空值回退 secret_key，不拒绝启动）。
        # 防止用户显式配置了弱 CSRF 密钥却未被发现。
        if self.csrf_secret:
            if self.csrf_secret.startswith("${") and self.csrf_secret.endswith("}"):
                raise ValueError(
                    f"security.csrf_secret contains unresolved env placeholder '{self.csrf_secret}' — "
                    f"CSRF tokens can be forged with a known placeholder! "
                    f"Set EDGELITE_SECURITY__CSRF_SECRET environment variable."
                )
            if (
                self.csrf_secret.strip("<>") in self._PLACEHOLDER_VALUES
                or self.csrf_secret.lower() in self._PLACEHOLDER_VALUES
            ):
                raise ValueError(
                    f"security.csrf_secret is set to a placeholder value '{self.csrf_secret}' — "
                    f"This is insecure! Generate a random key: "
                    f'python -c "import secrets; print(secrets.token_urlsafe(32))"'
                )
            if len(self.csrf_secret) < 32:
                raise ValueError(
                    f"security.csrf_secret is too short ({len(self.csrf_secret)} chars, minimum 32) — "
                    f"CSRF tokens can be brute-forced! Generate a random key: "
                    f'python -c "import secrets; print(secrets.token_urlsafe(32))"'
                )
        return self


class LoggingConfig(BaseModel):
    level: str = "INFO"
    # FIXED(G-02): 日志格式包含 request_id 字段，配合 RequestIdFilter 实现请求级日志串联
    format: str = "%(asctime)s | %(levelname)-8s | %(name)s | req_id=%(request_id)s | %(message)s"
    json_format: bool = False
    log_dir: str = "data/logs"
    max_bytes: int = 52428800
    backup_count: int = 10


class SimulatorPointConfig(BaseModel):
    name: str
    data_type: str = "float32"
    unit: str = ""
    address: str = "0"
    access_mode: str = "r"
    min: float = 0.0
    max: float = 100.0
    mode: str = "random"


class SimulatorDeviceConfig(BaseModel):
    device_id: str
    name: str
    points: list[SimulatorPointConfig]
    collect_interval: int = 5


class SimulatorConfig(BaseModel):
    auto_create: bool = (
        False  # FIXED-P1: 默认关闭，生产环境不应自动创建模拟设备；开发环境通过EDGELITE_SIMULATOR__AUTO_CREATE=true开启
    )
    default_devices: list[SimulatorDeviceConfig] = []


class NotifyDingtalkConfig(BaseModel):
    enabled: bool = True
    name: str = "钉钉通知"
    webhook_url: str = ""
    secret: str = ""
    at_mobiles: list[str] = []
    is_at_all: bool = False
    max_per_minute: int = 10
    cooldown_seconds: float = 60.0


class NotifyEmailConfig(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    use_tls: bool = True
    use_starttls: bool = False
    from_addr: str = ""
    to_addrs: list[str] = []
    max_per_minute: int = 60
    cooldown_seconds: float = 60.0


class NotifyWechatConfig(BaseModel):
    webhook_url: str = ""


class NotifyWebhookConfig(BaseModel):
    enabled: bool = True
    name: str = "自定义Webhook"
    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = {}
    auth_type: str = "none"  # none/basic/bearer/api_key
    auth_token: str = ""
    auth_username: str = ""
    auth_password: str = ""
    max_per_minute: int = 10
    cooldown_seconds: float = 60.0


class NotifyConfig(BaseModel):
    dingtalk: NotifyDingtalkConfig = NotifyDingtalkConfig()
    email: NotifyEmailConfig = NotifyEmailConfig()
    wechat: NotifyWechatConfig = NotifyWechatConfig()
    webhook: NotifyWebhookConfig = NotifyWebhookConfig()


class MqttServerConfig(BaseModel):
    """内置MQTT Server配置"""

    enabled: bool = False
    host: str = "127.0.0.1"  # FIXED-P0: 默认仅监听localhost
    port: int = Field(default=1888, ge=1, le=65535)
    ws_port: int | None = Field(default=None, ge=1, le=65535)
    username: str = ""
    password: str = ""
    allow_no_auth: bool = False  # FIXED-P2: 无认证时默认拒绝启动，需显式设置为true才允许


class ModbusSlaveConfig(BaseModel):
    """内置Modbus Slave配置"""

    enabled: bool = False
    host: str = "127.0.0.1"  # FIXED-P0: 默认仅监听localhost
    port: int = Field(default=5020, ge=1, le=65535)
    holding_size: int = Field(default=1000, ge=1)
    input_size: int = Field(default=1000, ge=1)
    coil_size: int = Field(default=1000, ge=1)
    discrete_size: int = Field(default=1000, ge=1)


class SerialBridgeConfig(BaseModel):
    """串口TCP透传配置"""

    enabled: bool = False
    serial_port: str = "COM1" if __import__("sys").platform == "win32" else "/dev/ttyUSB0"
    baud_rate: int = 9600
    tcp_port: int = 9000
    ip_whitelist: list[str] = Field(default_factory=list)
    max_clients: int = Field(default=5, ge=1, le=50)


class PreprocessGlobalConfig(BaseModel):
    """边缘数据预处理全局配置"""

    enabled: bool = False
    default_deadband: float = Field(default=0.0, ge=0.0)
    default_filter_window: int = Field(default=3, ge=1, le=21)
    default_aggregate_window_sec: int = Field(default=0, ge=0)


class WebhookAuthConfig(BaseModel):
    """HTTP Webhook 安全认证配置"""

    mode: Literal["none", "bearer", "basic"] = "none"
    token: str = ""
    username: str = ""
    password: str = ""


class MqttTlsConfigModel(BaseModel):
    """MQTT TLS/SSL 配置"""

    enabled: bool = False
    ca_cert: str = ""
    client_cert: str = ""
    client_key: str = ""
    cert_reqs: Literal["none", "optional", "required"] = "required"


class McpServerConfig(BaseModel):
    """MCP Server配置"""

    enabled: bool = False


class GrafanaConfig(BaseModel):
    """Grafana集成配置"""

    enabled: bool = False
    url: str = "http://localhost:3001"
    api_key: str = ""
    datasource: str = "InfluxDB"


class AiInferenceConfig(BaseModel):
    """边缘AI推理引擎配置"""

    enabled: bool = True
    models_dir: str = ""
    hot_reload_timeout: int = Field(default=30, ge=5, le=300)
    inference_timeout: int = Field(default=10, ge=1, le=60)
    max_concurrent_inferences: int = Field(default=4, ge=1, le=32)
    stats_retention_days: int = Field(default=7, ge=1, le=365)


class DriversConfig(BaseModel):
    """驱动配置"""

    custom_dir: str = ""
    auto_reload: bool = False


class BackupConfig(BaseModel):
    """自动备份调度器配置"""

    enabled: bool = Field(default=True, description="是否启用自动备份")
    interval_hours: int = Field(default=24, ge=1, le=168, description="备份间隔（小时）")
    retain_days: int = Field(default=7, ge=1, le=365, description="备份文件保留天数")
    backup_dir: str = Field(default="data/backups", description="备份文件存储目录")
    min_free_mb: int = Field(default=100, ge=10, description="备份所需最小磁盘空间（MB）")


class SchedulerConfig(BaseModel):
    """采集调度配置"""

    max_concurrent_collects: int = Field(default=50, ge=1, le=500)
    error_rate_threshold: float = Field(default=0.1, ge=0.0, le=1.0)
    watchdog_interval: int = Field(default=30, ge=5, le=300)
    watchdog_stale_cycles: int = Field(default=3, ge=1, le=20)
    watchdog_restart_cycles: int = Field(default=10, ge=3, le=100)


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig = DatabaseConfig()
    influxdb: InfluxDBConfig = InfluxDBConfig()
    mqtt: MQTTConfig = MQTTConfig()
    mqtt_server: MqttServerConfig = MqttServerConfig()
    modbus_slave: ModbusSlaveConfig = ModbusSlaveConfig()
    video: VideoConfig = VideoConfig()
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    logging: LoggingConfig = LoggingConfig()
    simulator: SimulatorConfig = SimulatorConfig()
    notify: NotifyConfig = NotifyConfig()
    platforms: dict[str, Any] = {}
    serial_bridge: SerialBridgeConfig = Field(default_factory=SerialBridgeConfig)
    preprocess: PreprocessGlobalConfig = Field(default_factory=PreprocessGlobalConfig)
    webhook_auth: WebhookAuthConfig = Field(default_factory=WebhookAuthConfig)
    mqtt_tls: MqttTlsConfigModel = Field(default_factory=MqttTlsConfigModel)
    mcp_server: McpServerConfig = Field(default_factory=McpServerConfig)
    grafana: GrafanaConfig = Field(default_factory=GrafanaConfig)
    drivers: DriversConfig = Field(default_factory=DriversConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    ai_inference: AiInferenceConfig = Field(default_factory=AiInferenceConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    ota_update_url: str = ""

    _config_version: int = 0

    def reload_from_dict(self, data: dict) -> list[str]:
        """从字典热更新配置，返回变更的关键配置项路径列表

        Args:
            data: 新的配置数据（部分或全部），将深度合并到当前配置

        Returns:
            变更的关键配置项路径列表（仅包含 _SENSITIVE_CONFIG_PATHS 中的项）
        """
        current_dict = self.model_dump()
        old_dict = _deep_copy_dict(current_dict)
        merged = _deep_merge(current_dict, data)

        # 检测关键配置项变更
        changed_sensitive = _detect_sensitive_changes(old_dict, merged)

        # 验证新配置合法性
        try:
            new_config = AppConfig(**merged)
        except Exception as e:
            logger.error("reload_from_dict validation failed: %s", e)
            raise

        # FIXED-P1: 原子替换所有字段，先构建完整字段字典再一次性赋值
        # FIXED-P1: 使用_reload_lock保护并发更新，防止看到部分更新的配置状态
        with _reload_lock:
            new_fields = {}
            for field_name in new_config.model_fields:
                new_fields[field_name] = getattr(new_config, field_name)
            self.__dict__.update(new_fields)
            # 递增版本号
            self.__dict__["_config_version"] = self._config_version + 1

        # 记录关键配置变更日志
        for path in changed_sensitive:
            label = _SENSITIVE_CONFIG_PATHS.get(path, path)
            logger.warning("Config changed: %s (version=%d)", label, self._config_version)

        if changed_sensitive:
            logger.info(
                "Config hot-reloaded: version=%d, sensitive_changes=%s",
                self._config_version,
                changed_sensitive,
            )
        else:
            logger.info(
                "Config hot-reloaded: version=%d, no sensitive changes",
                self._config_version,
            )

        # 触发配置变更回调
        _notify_config_change({"version": self._config_version, "changed_keys": changed_sensitive})

        return changed_sensitive


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override覆盖base"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _deep_copy_dict(d: Any) -> Any:
    """深度拷贝字典（仅处理dict/list/基本类型）

    FIXED-P1: 原问题-列表使用list(v)浅拷贝，列表内嵌套dict为共享引用，
    导致old_dict与current_dict的列表内dict互相影响，破坏变更检测隔离性。
    改为递归深拷贝列表元素。
    """
    if isinstance(d, dict):
        return {k: _deep_copy_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_deep_copy_dict(item) for item in d]
    return d


def _get_nested_value(d: dict, path: str) -> Any:
    """通过点分隔路径获取嵌套字典值，如 'mqtt.broker' -> d['mqtt']['broker']"""
    keys = path.split(".")
    current = d
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _detect_sensitive_changes(old_dict: dict, new_dict: dict) -> list[str]:
    """检测关键配置项变更，返回变更的路径列表"""
    changed = []
    for path in _SENSITIVE_CONFIG_PATHS:
        old_val = _get_nested_value(old_dict, path)
        new_val = _get_nested_value(new_dict, path)
        if old_val != new_val:
            changed.append(path)
    return changed


def register_config_change_callback(callback: Callable[[dict[str, Any]], Any]) -> None:
    """注册配置变更回调函数

    回调函数接收一个字典参数: {"version": int, "changed_keys": list[str]}
    """
    with _callbacks_lock:  # FIXED-P1: 原问题-追加回调与遍历并发可RuntimeError
        _config_change_callbacks.append(callback)


def _notify_config_change(change_info: dict[str, Any]) -> None:
    """通知所有注册的配置变更回调"""
    with _callbacks_lock:  # FIXED-P1: 原问题-遍历回调与追加并发可RuntimeError
        callbacks = list(_config_change_callbacks)
    for callback in callbacks:
        try:
            callback(change_info)
        except Exception as e:
            logger.error("Config change callback failed: %s", e)


def _resolve_env_vars(obj: Any) -> Any:  # FIXED-P0: 添加环境变量插值解析
    """递归遍历配置字典，将 ${VAR_NAME} 格式的字符串替换为环境变量值"""
    _ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")
    if isinstance(obj, dict):
        for key in obj:
            obj[key] = _resolve_env_vars(obj[key])
    elif isinstance(obj, list):
        for i in range(len(obj)):
            obj[i] = _resolve_env_vars(obj[i])
        # FIXED-P2: Remove unresolved ${...} placeholders from list items (e.g. CORS origins)
        obj = [item for item in obj if not (isinstance(item, str) and _ENV_VAR_PATTERN.search(item))]
    elif isinstance(obj, str):

        def _replace_match(m):
            var_expr = m.group(1)
            # 支持 ${VAR:default} 语法，冒号后为默认值
            if ":" in var_expr:
                var_name, default_val = var_expr.split(":", 1)
            else:
                var_name, default_val = var_expr, None
            val = os.environ.get(var_name)
            if val is None:
                if default_val is not None:
                    return default_val
                # FIXED(P2): 原问题-未设置的env var保留${VAR}占位符，密码可能以字面量认证;
                # 修复-敏感字段未设置时返回空字符串而非占位符
                if any(kw in var_name.lower() for kw in ("password", "secret", "token", "key")):
                    logger.error("Required environment variable %s not set, using empty value", var_name)
                    return ""
                logger.warning(
                    "Environment variable %s referenced in config but not set, keeping ${%s} placeholder",
                    var_name,
                    var_name,
                )
                return m.group(0)
            return val

        obj = _ENV_VAR_PATTERN.sub(_replace_match, obj)
    return obj


def _load_env_overrides() -> dict[str, Any]:
    """从环境变量加载配置覆盖（EDGELITE_前缀，双下划线分隔层级）

    格式：EDGELITE_<SECTION>__<KEY>  或  EDGELITE_<SECTION>_<SUB>__<KEY>
    分号分隔的值会自动转为列表（如 CORS_ORIGINS=http://a;http://b -> ["http://a","http://b"]）
    示例：EDGELITE_SERVER__HOST -> server.host
          EDGELITE_SERVER__CORS_ALLOWED_ORIGINS=http://a;http://b -> server.cors_allowed_origins = ["http://a","http://b"]
          EDGELITE_VIDEO__PYGBSENTRY__ENDPOINT -> video.pygbsentry.endpoint
    """
    overrides: dict[str, Any] = {}
    prefix = "EDGELITE_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        raw = key[len(prefix) :]
        parsed_value: str | list[str] = value
        if ";" in value:
            parsed_value = [v.strip() for v in value.split(";") if v.strip()]
        if "__" in raw:
            parts = raw.lower().split("__")
            d = overrides
            for part in parts[:-1]:
                if part not in d or not isinstance(d[part], dict):
                    d[part] = {}
                d = d[part]
            d[parts[-1]] = parsed_value
        else:
            overrides[raw.lower()] = parsed_value
    return overrides


def _encrypt_sensitive_config(config_dict: dict[str, Any]) -> dict[str, Any]:
    """加密配置字典中的敏感字段（就地修改并返回）。

    若 SecretManager 未初始化（未配置 master key / EDGELITE_MASTER_KEY），
    则降级为明文存储，不进行加密，避免破坏现有功能。
    已是加密格式（以 '{"algorithm":' 开头）的值不会被重复加密。
    """
    sm = get_secret_manager()
    if not sm.is_initialized():
        return config_dict
    for path in _ENCRYPTED_SECRET_PATHS:
        val = _get_nested_value(config_dict, path)
        if isinstance(val, str) and val and not val.startswith('{"algorithm":'):
            try:
                _set_nested_value(config_dict, path, sm.encrypt(val))
            except Exception as e:
                logger.warning("加密配置字段 %s 失败: %s（保留明文）", path, e)
    return config_dict


def _decrypt_sensitive_config(config_dict: dict[str, Any]) -> dict[str, Any]:
    """解密配置字典中的敏感字段（就地修改并返回）。

    若 SecretManager 未初始化，则跳过解密（持久化值应为明文）。
    非加密格式的值经 decrypt_or_plain 原样返回，不会受影响。
    """
    sm = get_secret_manager()
    if not sm.is_initialized():
        return config_dict
    for path in _ENCRYPTED_SECRET_PATHS:
        val = _get_nested_value(config_dict, path)
        if isinstance(val, str) and val:
            try:
                decrypted = sm.decrypt_or_plain(val)
                if decrypted != val:
                    _set_nested_value(config_dict, path, decrypted)
            except Exception as e:
                logger.warning("解密配置字段 %s 失败: %s（保留原值）", path, e)
    return config_dict


def load_config(config_path: str | Path = "configs/config.yaml") -> AppConfig:
    """加载配置文件，支持 .env 文件覆盖"""
    # 自动加载 .env 文件（如果存在）
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)

    config_data: dict[str, Any] = {}
    config_file_ok = True

    path = Path(config_path)
    if path.exists():
        # FIXED: 原问题-load_config文件读取无try-except保护，文件权限不足或损坏导致启动失败
        try:
            with open(path, encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            logger.error("load_config failed to read %s: %s", path, e)
            config_data = {}
            config_file_ok = False

    # FIXED-P1: 原问题-配置文件损坏时静默回退到默认配置，不尝试从版本快照恢复
    # 改为：配置文件为空或损坏时，尝试从config_versions.db最新快照恢复
    if not config_data and not config_file_ok:
        try:
            import json as _json
            import sqlite3 as _sqlite3

            cv_db = Path("data/config_versions.db")
            if cv_db.exists():
                conn = _sqlite3.connect(str(cv_db), timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                try:
                    row = conn.execute(
                        "SELECT config_json FROM config_versions ORDER BY version DESC LIMIT 1"
                    ).fetchone()
                    if row and row[0]:
                        recovered = _json.loads(row[0])
                        if isinstance(recovered, dict) and recovered:
                            config_data = recovered
                            logger.warning("Config file damaged, recovered from config_versions.db snapshot")
                finally:
                    conn.close()
        except Exception as e:
            logger.warning("Config recovery from version snapshot failed: %s", e)

    # 解密持久化中的敏感字段（YAML 或版本快照中可能以加密形式存储）
    # 在环境变量解析/覆盖之前进行，确保后续合并基于明文
    _decrypt_sensitive_config(config_data)

    # 环境变量插值解析 ${VAR_NAME} 格式
    _resolve_env_vars(config_data)  # FIXED-P0: 添加环境变量插值解析

    # 环境变量覆盖（优先级：环境变量 > .env > config.yaml）
    env_overrides = _load_env_overrides()
    if env_overrides:
        config_data = _deep_merge(config_data, env_overrides)

    # FIXED-P0: 开发环境（DEV_MODE=true）下自动生成临时 secret_key 和 csrf_secret
    # 允许 config.yaml 中 secret_key/csrf_secret 为空，开发时自动生成随机密钥
    # 生产环境（DEV_MODE=false）下空值仍会被 AppConfig 验证器拒绝，强制要求显式配置
    dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
    if dev_mode:
        import secrets as _secrets

        _sec = config_data.get("security") or {}
        if not _sec.get("secret_key"):
            _sec["secret_key"] = _secrets.token_urlsafe(32)
            config_data["security"] = _sec
            logger.info("DEV_MODE: auto-generated temporary secret_key (regenerated on each restart)")
        if not _sec.get("csrf_secret") and not config_data.get("csrf_secret"):
            _sec["csrf_secret"] = _secrets.token_urlsafe(32)
            config_data["security"] = _sec
            logger.info("DEV_MODE: auto-generated temporary csrf_secret (regenerated on each restart)")

    config = AppConfig(**config_data)

    # FIXED(安全): 生产环境（DEV_MODE=false）下强制拒绝 debug_api_enabled=true
    # debug_api_enabled=true 会注册 /debug/simulate、/debug/read、/debug/write 等端点
    # （可直接操控工业设备）并暴露 /docs、/redoc、/openapi.json，存在严重安全风险
    if not dev_mode and config.server.debug_api_enabled:
        logger.warning(
            "安全校验: 生产环境（DEV_MODE=false）下禁止开启 debug_api_enabled，已强制关闭。如需调试请设置 DEV_MODE=true"
        )
        config.server.debug_api_enabled = False

    # FIXED(安全): 生产环境（DEV_MODE=false）下强制启用 cookie_secure
    # cookie_secure=False 时 Cookie 可通过 HTTP 传输被中间人截获
    # 生产环境必须通过 HTTPS 部署，Cookie 的 Secure 标记必须为 True
    if not dev_mode and not config.security.cookie_secure:
        logger.info("安全校验: 生产环境（DEV_MODE=false）下自动启用 cookie_secure=True，确保 Cookie 仅通过 HTTPS 传输")
        config.security.cookie_secure = True

    # FIXED(安全): 生产环境（DEV_MODE=false）下校验 CSRF_SECRET 不得为已知不安全默认值/占位符
    # CSRF_SECRET 为空时由中间件自动生成随机值（安全，但重启后 token 失效），仅记录提示
    if not dev_mode:
        _csrf_secret = os.environ.get("EDGELITE_CSRF_SECRET", "")
        if _csrf_secret:
            _csrf_norm = _csrf_secret.strip("<>").lower()
            if _csrf_norm in _INSECURE_DEFAULT_VALUES or _csrf_secret.lower() in _INSECURE_DEFAULT_VALUES:
                raise ValueError(
                    "EDGELITE_CSRF_SECRET 被设置为已知不安全默认值/占位符，"
                    "生产环境禁止使用！请生成强随机值："
                    'python -c "import secrets; print(secrets.token_urlsafe(32))"'
                )
            if len(_csrf_secret) < 32:
                logger.warning(
                    "安全校验: EDGELITE_CSRF_SECRET 长度仅 %d 字符（建议至少 32 字符），"
                    '可能被暴力破解。生成命令: python -c "import secrets; print(secrets.token_urlsafe(32))"',
                    len(_csrf_secret),
                )
        else:
            logger.info(
                "安全提示: 未配置 EDGELITE_CSRF_SECRET，将自动生成随机值。"
                "生产环境建议显式配置以保证重启后 CSRF token 不失效。"
            )

    return config


# 全局配置实例（延迟初始化）
_config: AppConfig | None = None
# R11-DRV-09: 原 _config_lock = asyncio.Lock() if hasattr(...) else None 是死代码
# （asyncio.Lock 恒存在，条件永真），且模块级创建 Lock 会绑定到导入时的（可能不存在的）事件循环，
# 多事件循环场景下复用会报错。改为懒初始化，首次 get_config_async 时按当前运行循环创建。
_config_async_lock: asyncio.Lock | None = None
_config_sync_lock = (
    threading.Lock()
)  # FIXED-P1: 原问题-get_config()同步版本无线程安全保护，多线程并发首次调用可创建多个实例
_reload_lock = threading.Lock()  # FIXED-P1: reload_from_dict配置热更新并发保护


async def get_config_async() -> AppConfig:
    """获取全局配置实例（异步安全版本）"""
    # R11-DRV-09: 懒初始化 asyncio.Lock，避免模块级创建绑定到错误事件循环
    global _config, _config_async_lock
    if _config_async_lock is None:
        _config_async_lock = asyncio.Lock()
    async with _config_async_lock:
        if _config is None:
            config_path = os.environ.get("EDGELITE_CONFIG", "configs/config.yaml")
            _config = load_config(config_path)
        return _config


def get_config() -> AppConfig:
    """获取全局配置实例"""
    global _config
    if _config is None:
        with _config_sync_lock:  # FIXED-P1: 原问题-get_config()无线程安全保护，多线程并发首次调用可创建多个实例
            if _config is None:
                config_path = os.environ.get("EDGELITE_CONFIG", "configs/config.yaml")
                _config = load_config(config_path)
    return _config


def reset_config() -> None:
    """重置全局配置（测试用）"""
    global _config
    _config = None


def save_config(config: AppConfig, config_path: str | Path | None = None) -> None:
    """将配置持久化到YAML文件

    Args:
        config: AppConfig实例
        config_path: 配置文件路径，默认使用当前全局配置的路径
    """
    import os
    import tempfile

    if config_path is None:
        config_path = os.environ.get("EDGELITE_CONFIG", "configs/config.yaml")

    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    config_dict = config.model_dump()

    env_overrides = _load_env_overrides()
    if env_overrides:
        config_dict = _deep_merge(config_dict, env_overrides)

    # 持久化前加密敏感字段（密码/Token/密钥），确保 YAML 与版本快照均存储密文
    # 内存中的 config 对象保持明文，仅落盘时加密
    _encrypt_sensitive_config(config_dict)

    # FIXED-P0: 原问题-YAML写入成功后版本快照写入可能失败(non-fatal)，文件已更新但版本历史缺失
    # 改为：先写版本快照再原子替换YAML文件，快照失败则中止保存，确保版本历史始终覆盖当前文件
    import json as _json

    config_json = _json.dumps(config_dict, ensure_ascii=False, default=str)
    try:
        from edgelite.services.config_version import get_config_version_manager

        cvm = get_config_version_manager()
        cvm.save_version_sync(config_json)
    except Exception as cv_err:
        logger.error("save_config: version snapshot failed, aborting save: %s", cv_err)
        raise

    # FIXED-P0: 原问题-save_config直接覆写文件，断电/磁盘满导致配置截断丢失
    # 改为写临时文件+os.replace原子替换，确保配置文件要么完整更新要么保持原状
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("删除临时文件失败: %s", e)
            raise
    except OSError as e:
        logger.error("save_config failed to write %s: %s", path, e)
        raise

    global _config
    _config = config


def update_config_section(section: str, values: dict, config_path: str | Path | None = None) -> AppConfig:
    """更新配置的某个段落并持久化

    Args:
        section: 配置段名称 (如 'mqtt_server', 'modbus_slave')
        values: 要更新的键值对
        config_path: 配置文件路径

    Returns:
        更新后的AppConfig实例
    """
    config = get_config()

    current_section = getattr(config, section, None)
    if current_section is None:
        raise ValueError(f"Unknown config section: {section}")

    # FIXED-P0: 使用Pydantic验证而非直接setattr，防止绕过Field约束和model_validator
    section_data = current_section.model_dump()
    for key, value in values.items():
        if key in section_data:
            section_data[key] = value
    validated_section = current_section.__class__.model_validate(section_data)

    # FIXED-P0: 原问题-先setattr修改内存再save_config，写入失败时内存与文件不一致
    # 改为先保存文件再修改内存，保存失败时内存配置不变
    old_section = current_section
    try:
        setattr(config, section, validated_section)
        save_config(config, config_path)
    except Exception:
        setattr(config, section, old_section)
        raise
    return config


def reload_config(config_path: str | Path | None = None) -> tuple[AppConfig, list[str]]:
    """从文件重新加载配置并热更新到全局实例

    Args:
        config_path: 配置文件路径，默认使用当前全局配置的路径

    Returns:
        (更新后的AppConfig, 变更的关键配置项路径列表)
    """
    if config_path is None:
        config_path = os.environ.get("EDGELITE_CONFIG", "configs/config.yaml")

    new_config_data = load_config(config_path)
    config = get_config()
    new_data = new_config_data.model_dump()

    changed_sensitive = config.reload_from_dict(new_data)

    # 同步更新全局实例引用
    global _config
    _config = config

    return config, changed_sensitive


def get_sanitized_config() -> dict[str, Any]:
    """获取脱敏后的当前运行配置

    使用 SecretManager.mask_config() 对密码、密钥、Token等敏感字段进行脱敏：
    - SecretManager 已初始化（配置了 master key）时，敏感字段返回加密密文（完全不可读）；
    - 未初始化时，敏感字段返回字符掩码（如 "ab****cd"）。
    若 mask_config 异常，回退到按 _SENSITIVE_MASK_PATHS 手动掩码。

    Returns:
        脱敏后的配置字典
    """
    config = get_config()
    data = config.model_dump()

    try:
        sm = get_secret_manager()
        data = sm.mask_config(data)
    except Exception as e:
        logger.warning("mask_config 脱敏失败，回退到手动掩码: %s", e)
        for path in _SENSITIVE_MASK_PATHS:
            val = _get_nested_value(data, path)
            if val and isinstance(val, str) and len(val) > 2:
                _set_nested_value(data, path, val[0] + "***" + val[-1])
            elif val and isinstance(val, str) and len(val) > 0:
                _set_nested_value(data, path, "***")

    data["_config_version"] = config._config_version
    return data


def _set_nested_value(d: dict, path: str, value: Any) -> None:
    """通过点分隔路径设置嵌套字典值"""
    keys = path.split(".")
    current = d
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


async def watch_config_file(
    file_path: str | Path,
    callback: Callable[[dict[str, Any]], Any] | None = None,
    poll_interval: float = 2.0,
) -> None:
    """异步监控配置文件变更，变更时触发回调

    使用轮询方式检测文件修改（基于 mtime 和内容哈希），兼容所有平台。

    Args:
        file_path: 要监控的配置文件路径
        callback: 文件变更时的回调函数，接收变更信息字典
            {"version": int, "changed_keys": list[str], "source": "file_watch"}
        poll_interval: 轮询间隔秒数，默认2秒
    """
    path = Path(file_path)
    last_mtime: float = 0.0
    last_hash: str = ""

    if callback is not None:
        register_config_change_callback(callback)

    logger.info("Config file watcher started: %s (poll_interval=%.1fs)", path, poll_interval)

    while True:
        try:
            await asyncio.sleep(poll_interval)

            if not path.exists():
                continue

            current_mtime = path.stat().st_mtime
            if current_mtime == last_mtime:
                continue

            # mtime 变了，计算内容哈希确认是否真的变了
            try:
                content = path.read_bytes()
            except OSError:
                continue

            current_hash = hashlib.sha256(content).hexdigest()  # FIXED-P2: MD5→SHA256防碰撞
            if current_hash == last_hash:
                last_mtime = current_mtime
                continue

            # 文件内容确实变了，执行热加载
            logger.info("Config file changed detected: %s (mtime=%.0f)", path, current_mtime)
            last_mtime = current_mtime
            last_hash = current_hash

            try:
                _, changed_keys = reload_config(file_path)
                logger.info(
                    "Config auto-reloaded from file: version=%d, changed=%s",
                    get_config()._config_version,
                    changed_keys,
                )
            except Exception as e:
                logger.error("Config auto-reload failed: %s", e)

        except asyncio.CancelledError:
            logger.info("Config file watcher stopped: %s", path)
            break
        except Exception as e:
            logger.error("Config file watcher error: %s", e)
