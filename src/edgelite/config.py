"""EdgeLiteGateway 配置加载模块"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    cors_origins: list[str] = ["http://localhost:3000"]
    webhook_api_key: str = ""


class DatabaseConfig(BaseModel):
    backend: str = "sqlite"
    sqlite_path: str = "data/edgelite.db"
    host: str = "localhost"
    port: int = 3306
    username: str = ""
    password: str = ""
    database: str = "edgelite"
    backup_dir: str = "data/backups"
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False


class InfluxDBConfig(BaseModel):
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "edgelite"
    bucket: str = "edgelite"
    batch_size: int = 1000
    flush_interval: int = 5000
    retention_days: int = Field(default=30, ge=1, le=3650)


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


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
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
    auto_create: bool = True
    default_devices: list[SimulatorDeviceConfig] = []


class NotifyDingtalkConfig(BaseModel):
    webhook_url: str = ""
    secret: str = ""


class NotifyEmailConfig(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    use_tls: bool = True
    use_starttls: bool = False
    from_addr: str = ""
    to_addrs: list[str] = []


class NotifyWechatConfig(BaseModel):
    webhook_url: str = ""


class NotifyWebhookConfig(BaseModel):
    url: str = ""
    headers: dict[str, str] = {}


class NotifyConfig(BaseModel):
    dingtalk: NotifyDingtalkConfig = NotifyDingtalkConfig()
    email: NotifyEmailConfig = NotifyEmailConfig()
    wechat: NotifyWechatConfig = NotifyWechatConfig()
    webhook: NotifyWebhookConfig = NotifyWebhookConfig()


class MqttServerConfig(BaseModel):
    """内置MQTT Server配置"""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=1888, ge=1, le=65535)
    ws_port: int | None = Field(default=None, ge=1, le=65535)
    username: str = ""
    password: str = ""


class ModbusSlaveConfig(BaseModel):
    """内置Modbus Slave配置"""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)
    holding_size: int = Field(default=1000, ge=1)
    input_size: int = Field(default=1000, ge=1)
    coil_size: int = Field(default=1000, ge=1)
    discrete_size: int = Field(default=1000, ge=1)


class SparkplugBConfig(BaseModel):
    """MQTT Sparkplug B 配置"""

    group_id: str = "group1"
    edge_node_id: str = "edgelite_node"
    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    birth_debounce_ms: int = Field(default=1000, ge=0, le=10000)
    tls_enabled: bool = False
    tls_ca_cert: str = ""
    tls_client_cert: str = ""
    tls_client_key: str = ""


class Dlt645DefaultsConfig(BaseModel):
    """DL/T 645 电表规约默认配置"""

    baud_rate: int = Field(default=2400, ge=300, le=115200)
    data_bits: int = 8
    parity: str = "E"
    stop_bits: int = 1
    timeout: float = Field(default=5.0, ge=1.0, le=30.0)


class Iec104Config(BaseModel):
    """IEC 104 电力远动规约配置"""

    default_port: int = 2404
    asdu_addr_length: int = Field(default=2, ge=1, le=2)
    cause_of_tx_length: int = Field(default=2, ge=1, le=2)
    max_apdu_length: int = Field(default=249, ge=25, le=65535)
    clock_sync: bool = True
    heartbeat_interval: float = Field(default=30.0, ge=5.0, le=300.0)
    t1_timeout: float = Field(default=15.0, ge=1.0, le=60.0)
    t2_timeout: float = Field(default=10.0, ge=1.0, le=60.0)
    t3_timeout: float = Field(default=20.0, ge=1.0, le=120.0)


class SerialBridgeConfig(BaseModel):
    """串口TCP透传配置"""

    enabled: bool = False
    serial_port: str = "/dev/ttyUSB0"
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

    enabled: bool = False
    models_dir: str = "models"
    hot_reload_timeout: int = Field(default=30, ge=5, le=300)
    inference_timeout: int = Field(default=10, ge=1, le=60)
    max_concurrent_inferences: int = Field(default=4, ge=1, le=32)
    stats_retention_days: int = Field(default=7, ge=1, le=365)


class DriversConfig(BaseModel):
    """驱动配置"""

    custom_dir: str = ""
    auto_reload: bool = False


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
    security: SecurityConfig = SecurityConfig()
    logging: LoggingConfig = LoggingConfig()
    simulator: SimulatorConfig = SimulatorConfig()
    notify: NotifyConfig = NotifyConfig()
    platforms: dict[str, Any] = {}
    sparkplug_b: SparkplugBConfig = Field(default_factory=SparkplugBConfig)
    dlt645_defaults: Dlt645DefaultsConfig = Field(default_factory=Dlt645DefaultsConfig)
    iec104: Iec104Config = Field(default_factory=Iec104Config)
    serial_bridge: SerialBridgeConfig = Field(default_factory=SerialBridgeConfig)
    preprocess: PreprocessGlobalConfig = Field(default_factory=PreprocessGlobalConfig)
    webhook_auth: WebhookAuthConfig = Field(default_factory=WebhookAuthConfig)
    mqtt_tls: MqttTlsConfigModel = Field(default_factory=MqttTlsConfigModel)
    mcp_server: McpServerConfig = Field(default_factory=McpServerConfig)
    grafana: GrafanaConfig = Field(default_factory=GrafanaConfig)
    drivers: DriversConfig = Field(default_factory=DriversConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    ai_inference: AiInferenceConfig = Field(default_factory=AiInferenceConfig)
    ota_update_url: str = ""


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override覆盖base"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_env_overrides() -> dict[str, Any]:
    """从环境变量加载配置覆盖（EDGELITE_前缀，双下划线分隔层级）

    格式：EDGELITE_<SECTION>__<KEY>  或  EDGELITE_<SECTION>__<SUB>__<KEY>
    分号分隔的值会自动转为列表（如 CORS_ORIGINS=http://a;http://b -> ["http://a","http://b"]）
    示例：EDGELITE_SERVER__HOST -> server.host
          EDGELITE_SERVER__CORS_ORIGINS=http://a;http://b -> server.cors_origins = ["http://a","http://b"]
          EDGELITE_VIDEO__PYGBSENTRY__ENDPOINT -> video.pygbsentry.endpoint
    """
    overrides: dict[str, Any] = {}
    prefix = "EDGELITE_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        raw = key[len(prefix) :]
        if ";" in value:
            value = [v.strip() for v in value.split(";") if v.strip()]
        if "__" in raw:
            parts = raw.lower().split("__")
            d = overrides
            for part in parts[:-1]:
                if part not in d or not isinstance(d[part], dict):
                    d[part] = {}
                d = d[part]
            d[parts[-1]] = value
        else:
            overrides[raw.lower()] = value
    return overrides


def load_config(config_path: str | Path = "configs/config.yaml") -> AppConfig:
    """加载配置文件，支持 .env 文件和环境变量覆盖"""
    # 自动加载 .env 文件（如果存在）
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)

    config_data: dict[str, Any] = {}

    path = Path(config_path)
    if path.exists():
        # FIXED: 原问题-load_config文件读取无try-except保护，文件权限不足或损坏导致启动失败
        try:
            with open(path, encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            logger.error("load_config failed to read %s: %s", path, e)
            config_data = {}

    # 环境变量覆盖（优先级：环境变量 > .env > config.yaml）
    env_overrides = _load_env_overrides()
    if env_overrides:
        config_data = _deep_merge(config_data, env_overrides)

    return AppConfig(**config_data)


# 全局配置实例（延迟初始化）
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """获取全局配置实例"""
    global _config
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

    if config_path is None:
        config_path = os.environ.get("EDGELITE_CONFIG", "configs/config.yaml")

    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    config_dict = config.model_dump()

    env_overrides = _load_env_overrides()
    if env_overrides:
        config_dict = _deep_merge(config_dict, env_overrides)

    # FIXED: 原问题-save_config文件写入无try-except保护，磁盘满或权限不足导致未处理异常
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except OSError as e:
        logger.error("save_config failed to write %s: %s", path, e)
        raise

    global _config
    _config = config


def update_config_section(
    section: str, values: dict, config_path: str | Path | None = None
) -> AppConfig:
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
        raise ValueError(f"未知的配置段: {section}")

    for key, value in values.items():
        if hasattr(current_section, key):
            setattr(current_section, key, value)

    save_config(config, config_path)
    return config
