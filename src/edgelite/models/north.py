"""北向平台对接数据模型

定义北向平台连接、MQTT配置、主题模板、负载格式、QoS策略等配置模型。
用于 PlatformService 和 BaseNorthAdapter 之间的配置传递。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MqttTlsConfig(BaseModel):
    """MQTT TLS 加密配置"""

    enabled: bool = False
    ca_cert: str = ""
    client_cert: str = ""
    client_key: str = ""
    verify_server: bool = True


class MqttWillConfig(BaseModel):
    """MQTT Will 遗嘱消息配置"""

    enabled: bool = False
    topic: str = ""
    payload: str = ""
    qos: int = 1
    retain: bool = True


class Mqtt5Properties(BaseModel):
    """MQTT 5.0 协议扩展属性"""

    enabled: bool = False
    message_expiry_interval: int | None = None
    content_type: str = ""
    response_topic: str = ""
    correlation_data: str = ""
    user_properties: dict[str, str] = Field(default_factory=dict)


class MqttConnectionConfig(BaseModel):
    """MQTT 连接配置"""

    broker_host: str = ""
    broker_port: int = 1883
    client_id: str = ""
    username: str = ""
    password: str = ""
    keepalive: int = 60
    clean_session: bool = True
    protocol_version: int = 4  # 4=MQTT 3.1.1, 5=MQTT 5.0
    transport: str = "tcp"  # tcp | websockets
    tls: MqttTlsConfig = Field(default_factory=MqttTlsConfig)
    will: MqttWillConfig = Field(default_factory=MqttWillConfig)
    mqtt5_props: Mqtt5Properties = Field(default_factory=Mqtt5Properties)


class TopicTemplateConfig(BaseModel):
    """主题模板配置"""

    prefix: str = "edgelite"
    template: str = "{prefix}/{device_id}/{point_id}"
    status_template: str = "{prefix}/{device_id}/status"


class PayloadConfig(BaseModel):
    """负载格式配置"""

    format: str = "json"  # json | protobuf | custom
    custom_template: str = ""
    compress_threshold: int = 1024
    enable_compression: bool = True


class QosPolicy(BaseModel):
    """QoS 服务质量策略"""

    default_qos: int = 0
    alarm_qos: int = 1
    rules: list[dict[str, Any]] = Field(default_factory=list)


class NorthConfig(BaseModel):
    """北向平台完整配置

    封装平台类型、连接参数、MQTT设置、主题模板、负载格式和QoS策略，
    供 BaseNorthAdapter 使用。
    """

    platform_type: str = ""
    connection_params: dict[str, Any] = Field(default_factory=dict)
    publish_mode: str = "realtime"  # realtime | batch
    batch_size: int = 100
    timeout: float = 10.0
    enable_qos: bool = True
    mqtt: MqttConnectionConfig = Field(default_factory=MqttConnectionConfig)
    topic: TopicTemplateConfig = Field(default_factory=TopicTemplateConfig)
    payload: PayloadConfig = Field(default_factory=PayloadConfig)
    qos_policy: QosPolicy = Field(default_factory=QosPolicy)
    dedup_window_seconds: int = 300
