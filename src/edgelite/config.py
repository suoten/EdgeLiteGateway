"""EdgeLiteGateway 配置加载模块"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    cors_origins: list[str] = ["http://localhost:3000"]
    webhook_api_key: str = ""


class DatabaseConfig(BaseModel):
    sqlite_path: str = "data/edgelite.db"
    backup_dir: str = "data/backups"


class InfluxDBConfig(BaseModel):
    url: str = "http://localhost:8086"
    token: str = "edgelite-token-change-me"
    org: str = "edgelite"
    bucket: str = "edgelite"
    batch_size: int = 1000
    flush_interval: int = 5000


class MQTTConfig(BaseModel):
    broker: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    topic_prefix: str = "edgelite"


class PyGBSentryConfig(BaseModel):
    endpoint: str = ""
    api_key: str = ""
    timeout: int = 10


class VideoConfig(BaseModel):
    pygbsentry: PyGBSentryConfig = PyGBSentryConfig()


class SecurityConfig(BaseModel):
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    algorithm: str = "HS256"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    json_format: bool = False
    log_dir: str = "data/logs"
    max_bytes: int = 52428800
    backup_count: int = 10


class AuditConfig(BaseModel):
    enabled: bool = True
    tamper_proof: bool = True
    retention_days: int = 90


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
    port: int = 1888
    ws_port: int | None = None
    username: str = ""
    password: str = ""


class OpcUaServerConfig(BaseModel):
    """内置OPC UA Server配置"""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 4840
    namespace: str = "urn:edgelite:gateway"


class ModbusSlaveConfig(BaseModel):
    """内置Modbus Slave配置"""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 502
    holding_size: int = 1000
    input_size: int = 1000


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig = DatabaseConfig()
    influxdb: InfluxDBConfig = InfluxDBConfig()
    mqtt: MQTTConfig = MQTTConfig()
    mqtt_server: MqttServerConfig = MqttServerConfig()
    opcua_server: OpcUaServerConfig = OpcUaServerConfig()
    modbus_slave: ModbusSlaveConfig = ModbusSlaveConfig()
    video: VideoConfig = VideoConfig()
    security: SecurityConfig = SecurityConfig()
    logging: LoggingConfig = LoggingConfig()
    audit: AuditConfig = AuditConfig()
    simulator: SimulatorConfig = SimulatorConfig()
    notify: NotifyConfig = NotifyConfig()
    platforms: dict[str, Any] = {}


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
    """从环境变量加载配置覆盖（EDGELITE_前缀）"""
    overrides: dict[str, Any] = {}
    prefix = "EDGELITE_"
    for key, value in os.environ.items():
        if key.startswith(prefix):
            # EDGELITE_SERVER_PORT -> server.port
            config_path = key[len(prefix):].lower().split("__")
            # 支持两级嵌套，如 EDGELITE_INFLUXDB_URL
            if len(config_path) >= 2:
                section = config_path[0]
                item = "_".join(config_path[1:])
                if section not in overrides:
                    overrides[section] = {}
                overrides[section][item] = value
            elif len(config_path) == 1:
                overrides[config_path[0]] = value
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
        with open(path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

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
