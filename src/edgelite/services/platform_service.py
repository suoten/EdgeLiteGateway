from __future__ import annotations

import asyncio
import importlib
import logging
import re
import time
from typing import Any

from edgelite.models.north import (
    Mqtt5Properties,
    MqttConnectionConfig,
    MqttTlsConfig,
    MqttWillConfig,
    NorthConfig,
    PayloadConfig,
    QosPolicy,
    TopicTemplateConfig,
)
from edgelite.platform.base import PlatformHandler
from edgelite.platform.north_base import BaseNorthAdapter

logger = logging.getLogger(__name__)

_PLATFORM_REGISTRY: dict[str, dict] = {}

# FIXED(安全): 平台导出配置泄露凭据防护 - 导出配置中的敏感字段名，匹配则替换为 "***"
# 包含 MQTT broker 密码、ThingsBoard token、Huawei secret 等明文凭据
_SENSITIVE_FIELD_NAMES = frozenset({"password", "secret", "api_key", "token", "access_secret"})


def _mask_sensitive_config_fields(config: Any) -> Any:
    """递归脱敏配置中的敏感字段（password, secret, api_key, token, access_secret）。

    FIXED(安全): GET /api/v1/platforms/export/{platform_name} 仅需 SYSTEM_READ 权限（VIEWER 即可），
    但返回的配置包含 MQTT broker 密码、ThingsBoard token 等明文凭据。
    递归遍历配置字典/列表，将敏感字段值替换为 "***"。
    """
    if isinstance(config, dict):
        return {
            k: ("***" if k in _SENSITIVE_FIELD_NAMES and v else _mask_sensitive_config_fields(v))
            for k, v in config.items()
        }
    if isinstance(config, list):
        return [_mask_sensitive_config_fields(item) for item in config]
    return config


_BROKER_PATTERN = re.compile(
    r"^(mqtt[s]?://)?"
    r"([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"$|"
    r"^(\d{1,3}\.){3}\d{1,3}$"
)

_MQTT_COMMON_FIELDS = [
    {
        "name": "broker",
        "type": "string",
        "label": "MQTT Broker Address",
        "placeholder": "e.g. 192.168.1.100 or mqtt.example.com",
        "required": True,
    },
    {"name": "port", "type": "integer", "label": "Port", "default": 1883, "required": True},
    {"name": "username", "type": "string", "label": "Username"},
    {"name": "password", "type": "string", "label": "Password", "secret": True},
    {"name": "client_id", "type": "string", "label": "Client ID", "placeholder": "Auto-generated if empty"},
    {"name": "keepalive", "type": "integer", "label": "Keepalive (s)", "default": 60},
    {
        "name": "clean_session",
        "type": "string",
        "label": "Clean Session",
        "default": "true",
        "placeholder": "true/false",
    },
    {
        "name": "protocol_version",
        "type": "integer",
        "label": "Protocol Version",
        "default": 4,
        "placeholder": "4=MQTT 3.1.1, 5=MQTT 5.0",
    },
    {"name": "transport", "type": "string", "label": "Transport", "default": "tcp", "placeholder": "tcp / websockets"},
]

_MQTT_TLS_FIELDS = [
    {
        "name": "tls_enabled",
        "type": "string",
        "label": "Enable TLS/SSL",
        "default": "false",
        "placeholder": "true/false",
    },
    {"name": "ca_cert", "type": "string", "label": "CA Certificate (PEM)", "placeholder": "Path or paste PEM content"},
    {"name": "client_cert", "type": "string", "label": "Client Certificate (PEM)", "placeholder": "For mTLS"},
    {"name": "client_key", "type": "string", "label": "Client Key (PEM)", "secret": True, "placeholder": "For mTLS"},
    {
        "name": "verify_server",
        "type": "string",
        "label": "Verify Server Cert",
        "default": "true",
        "placeholder": "true/false",
    },
]

_MQTT_WILL_FIELDS = [
    {
        "name": "will_enabled",
        "type": "string",
        "label": "Enable Last Will",
        "default": "false",
        "placeholder": "true/false",
    },
    {"name": "will_topic", "type": "string", "label": "Will Topic", "placeholder": "e.g. edgelite/status"},
    {"name": "will_payload", "type": "string", "label": "Will Payload", "default": "offline"},
    {"name": "will_qos", "type": "integer", "label": "Will QoS", "default": 1},
    {"name": "will_retain", "type": "string", "label": "Will Retain", "default": "true", "placeholder": "true/false"},
]

_MQTT5_FIELDS = [
    {
        "name": "message_expiry_interval",
        "type": "integer",
        "label": "Message Expiry (s)",
        "placeholder": "Seconds, empty=never",
    },
    {"name": "content_type", "type": "string", "label": "Content Type", "placeholder": "e.g. application/json"},
    {"name": "response_topic", "type": "string", "label": "Response Topic"},
    {"name": "correlation_data", "type": "string", "label": "Correlation Data"},
]

_TOPIC_TEMPLATE_FIELDS = [
    {"name": "topic_prefix", "type": "string", "label": "Topic Prefix", "default": "edgelite"},
    {
        "name": "topic_template",
        "type": "string",
        "label": "Topic Template",
        "default": "{prefix}/{device_id}/{point_id}",
        "placeholder": "Variables: {prefix}, {device_id}, {point_id}, {quality}",
    },
    {
        "name": "status_topic_template",
        "type": "string",
        "label": "Status Topic Template",
        "default": "{prefix}/{device_id}/status",
    },
]

_PAYLOAD_FIELDS = [
    {
        "name": "payload_format",
        "type": "string",
        "label": "Payload Format",
        "default": "json",
        "placeholder": "json / cbor / protobuf / custom",
    },
    {"name": "custom_template", "type": "string", "label": "Custom Template", "placeholder": "Only for custom format"},
    {"name": "compress_threshold", "type": "integer", "label": "Compression Threshold (bytes)", "default": 1024},
    {
        "name": "enable_compression",
        "type": "string",
        "label": "Enable Compression",
        "default": "true",
        "placeholder": "true/false",
    },
]

_QOS_FIELDS = [
    {"name": "default_qos", "type": "integer", "label": "Default QoS", "default": 0, "placeholder": "0 / 1 / 2"},
    {"name": "alarm_qos", "type": "integer", "label": "Alarm QoS", "default": 1, "placeholder": "0 / 1 / 2"},
]

_DEDUP_FIELDS = [
    {"name": "dedup_window_seconds", "type": "integer", "label": "Dedup Window (s)", "default": 300},
]


def _build_full_schema(base_fields: list[dict]) -> dict:
    return {
        "fields": base_fields,
        "sections": [
            {"title": "MQTT Connection", "fields": _MQTT_COMMON_FIELDS},
            {"title": "TLS/SSL", "fields": _MQTT_TLS_FIELDS},
            {"title": "Last Will", "fields": _MQTT_WILL_FIELDS},
            {"title": "MQTT 5.0 Properties", "fields": _MQTT5_FIELDS, "condition": "protocol_version == 5"},
            {"title": "Topic Template", "fields": _TOPIC_TEMPLATE_FIELDS},
            {"title": "Payload Format", "fields": _PAYLOAD_FIELDS},
            {"title": "QoS Policy", "fields": _QOS_FIELDS},
            {"title": "Deduplication", "fields": _DEDUP_FIELDS},
        ],
    }


def _ensure_registry() -> dict[str, dict]:
    if _PLATFORM_REGISTRY:
        return _PLATFORM_REGISTRY
    _PLATFORM_REGISTRY.update(
        {
            "iotsharp": {
                "label": "IoTSharp",
                "description": "Open-source IoT platform with device shadow",
                "module": "edgelite.platform.iotsharp",
                "class": "IoTSharpHandler",
                "north_adapter": "edgelite.platform.north_adapters",
                "north_class": "IoTSharpNorthAdapter",
                "base_fields": [
                    {
                        "name": "broker",
                        "type": "string",
                        "label": "MQTT Broker Address",
                        "placeholder": "e.g. iotsharp.example.com",
                        "required": True,
                    },
                    {"name": "port", "type": "integer", "label": "MQTT Port", "default": 1883, "required": True},
                    {"name": "device_token", "type": "string", "label": "Device Token", "required": True},
                    {"name": "username", "type": "string", "label": "MQTT Username"},
                    {"name": "password", "type": "string", "label": "MQTT Password", "secret": True},
                    {
                        "name": "shadow_enabled",
                        "type": "string",
                        "label": "Enable Device Shadow",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "attribute_publish",
                        "type": "string",
                        "label": "Attribute Publish",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                ],
            },
            "thingsboard": {
                "label": "ThingsBoard",
                "description": "Open-source IoT platform (PE/CE)",
                "module": "edgelite.platform.thingsboard",
                "class": "ThingsBoardHandler",
                "north_adapter": "edgelite.platform.north_adapters",
                "north_class": "ThingsBoardNorthAdapter",
                "base_fields": [
                    {
                        "name": "broker",
                        "type": "string",
                        "label": "MQTT Broker Address",
                        "placeholder": "e.g. thingsboard.example.com",
                        "required": True,
                    },
                    {"name": "port", "type": "integer", "label": "MQTT Port", "default": 1883, "required": True},
                    {"name": "token", "type": "string", "label": "Gateway Access Token", "required": True},
                    {
                        "name": "server_url",
                        "type": "string",
                        "label": "ThingsBoard HTTP URL",
                        "placeholder": "e.g. http://thingsboard.example.com:8080",
                    },
                    {
                        "name": "auth_mode",
                        "type": "string",
                        "label": "Auth Mode",
                        "default": "access_token",
                        "placeholder": "access_token / basic",
                    },
                    {
                        "name": "tb_admin_token",
                        "type": "string",
                        "label": "Admin JWT Token (for HTTP API)",
                        "placeholder": "For device auto-registration",
                    },
                    {"name": "tb_username", "type": "string", "label": "TB Username (Basic Auth)"},
                    {"name": "tb_password", "type": "string", "label": "TB Password (Basic Auth)", "secret": True},
                    {
                        "name": "transport_mode",
                        "type": "string",
                        "label": "Transport Mode",
                        "default": "mqtt",
                        "placeholder": "mqtt / http / both",
                    },
                    {"name": "http_pool_size", "type": "integer", "label": "HTTP Pool Size", "default": 50},
                    {"name": "http_timeout", "type": "integer", "label": "HTTP Timeout (s)", "default": 10},
                    {
                        "name": "auto_register",
                        "type": "string",
                        "label": "Auto Register Devices",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "name_template",
                        "type": "string",
                        "label": "Device Name Template",
                        "default": "EdgeLite-{device_id}",
                    },
                    {
                        "name": "attr_sync_enabled",
                        "type": "string",
                        "label": "Attribute Sync",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "server_attributes",
                        "type": "string",
                        "label": "Server Attributes",
                        "default": "model,location,firmware_version",
                        "placeholder": "Comma-separated",
                    },
                    {
                        "name": "shared_attributes",
                        "type": "string",
                        "label": "Shared Attributes",
                        "default": "report_interval,collect_interval",
                        "placeholder": "Comma-separated",
                    },
                    {
                        "name": "rpc_enabled",
                        "type": "string",
                        "label": "RPC Enabled",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "alarm_sync_enabled",
                        "type": "string",
                        "label": "Alarm Sync",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                ],
            },
            "huawei_iotda": {
                "label": "Huawei IoTDA",
                "description": "Huawei Cloud Device Access Service",
                "module": "edgelite.platform.huawei_iotda",
                "class": "HuaweiIoTDAHandler",
                "north_adapter": "edgelite.platform.north_adapters",
                "north_class": "HuaweiIoTDANorthAdapter",
                "base_fields": [
                    {
                        "name": "broker",
                        "type": "string",
                        "label": "MQTT Broker",
                        "placeholder": "e.g. a123b456c.iot-mqtts.cn-north-4.myhuaweicloud.com",
                        "required": True,
                    },
                    {"name": "port", "type": "integer", "label": "Port", "default": 8883},
                    {"name": "hw_device_id", "type": "string", "label": "Huawei Device ID", "required": True},
                    {
                        "name": "node_id",
                        "type": "string",
                        "label": "Node ID",
                        "placeholder": "For ID mapping: {node_id}_{device_id}",
                    },
                    {"name": "secret", "type": "string", "label": "Device Secret (HMAC)", "secret": True},
                    {
                        "name": "auth_mode",
                        "type": "string",
                        "label": "Auth Mode",
                        "default": "secret",
                        "placeholder": "secret / certificate",
                    },
                    {
                        "name": "timestamp_check",
                        "type": "string",
                        "label": "Timestamp Check",
                        "default": "0",
                        "placeholder": "0=no verify / 1=verify",
                    },
                    {
                        "name": "ca_cert",
                        "type": "string",
                        "label": "CA Certificate (PEM)",
                        "placeholder": "For MQTTS/certificate auth",
                    },
                    {
                        "name": "client_cert",
                        "type": "string",
                        "label": "Client Certificate (PEM)",
                        "placeholder": "For X.509 certificate auth",
                    },
                    {
                        "name": "client_key",
                        "type": "string",
                        "label": "Client Key (PEM)",
                        "secret": True,
                        "placeholder": "For X.509 certificate auth",
                    },
                    {
                        "name": "verify_server",
                        "type": "string",
                        "label": "Verify Server Cert",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "shadow_enabled",
                        "type": "string",
                        "label": "Enable Device Shadow",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "device_id_mapping",
                        "type": "string",
                        "label": "Device ID Mapping (JSON)",
                        "placeholder": 'e.g. {"d1":"hw_d1"}',
                    },
                    {
                        "name": "node_id_mapping",
                        "type": "string",
                        "label": "Node ID Mapping (JSON)",
                        "placeholder": 'e.g. {"d1":"node1"}',
                    },
                ],
            },
            "thingscloud": {
                "label": "ThingsCloud",
                "description": "ThingsCloud IoT Platform",
                "module": "edgelite.platform.thingscloud",
                "class": "ThingsCloudHandler",
                "north_adapter": "edgelite.platform.north_adapters",
                "north_class": "ThingsCloudNorthAdapter",
                "base_fields": [
                    {
                        "name": "broker",
                        "type": "string",
                        "label": "MQTT Broker",
                        "placeholder": "e.g. mqtt.thingscloud.cn",
                        "required": True,
                    },
                    {"name": "port", "type": "integer", "label": "Port", "default": 1883},
                    {"name": "access_key", "type": "string", "label": "Access Key (ProjectId)", "required": True},
                    {
                        "name": "access_secret",
                        "type": "string",
                        "label": "Access Secret",
                        "secret": True,
                        "required": True,
                    },
                    {"name": "project_id", "type": "string", "label": "Project ID"},
                    {
                        "name": "command_enabled",
                        "type": "string",
                        "label": "Enable Command Receive",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "status_report",
                        "type": "string",
                        "label": "Device Status Report",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                ],
            },
            "thingspanel": {
                "label": "ThingsPanel",
                "description": "ThingsPanel Open-Source IoT Platform",
                "module": "edgelite.platform.thingspanel",
                "class": "ThingsPanelHandler",
                "north_adapter": "edgelite.platform.north_adapters",
                "north_class": "ThingsPanelNorthAdapter",
                "base_fields": [
                    {
                        "name": "broker",
                        "type": "string",
                        "label": "MQTT Broker",
                        "placeholder": "e.g. mqtt.thingspanel.cn",
                        "required": True,
                    },
                    {"name": "port", "type": "integer", "label": "Port", "default": 1883},
                    {"name": "device_token", "type": "string", "label": "Device Token", "required": True},
                    {
                        "name": "api_url",
                        "type": "string",
                        "label": "HTTP API URL",
                        "placeholder": "e.g. http://thingspanel:8080",
                    },
                    {"name": "api_key", "type": "string", "label": "API Key (Bearer Token)", "secret": True},
                    {"name": "username", "type": "string", "label": "MQTT Username"},
                    {"name": "password", "type": "string", "label": "MQTT Password", "secret": True},
                    {
                        "name": "alarm_push",
                        "type": "string",
                        "label": "Enable Alarm Push (HTTP)",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "auto_register",
                        "type": "string",
                        "label": "Auto Register Devices",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                ],
            },
            "custom": {
                "label": "Custom MQTT",
                "description": "Highly configurable custom MQTT adapter with template engine, script extension, multi-broker support",  # noqa: E501
                "module": "edgelite.platform.custom_mqtt",
                "class": "CustomMqttHandler",
                "north_adapter": "edgelite.platform.north_adapters",
                "north_class": "CustomMqttNorthAdapter",
                "base_fields": [
                    {
                        "name": "broker",
                        "type": "string",
                        "label": "MQTT Broker",
                        "placeholder": "e.g. 192.168.1.100 or mqtt.example.com",
                        "required": True,
                    },
                    {"name": "port", "type": "integer", "label": "Port", "default": 1883},
                    {"name": "username", "type": "string", "label": "Username"},
                    {"name": "password", "type": "string", "label": "Password", "secret": True},
                    {"name": "topic_prefix", "type": "string", "label": "Topic Prefix", "default": "edgelite"},
                    {
                        "name": "topic_template",
                        "type": "string",
                        "label": "Topic Template",
                        "default": "{prefix}/{device_id}/{point_id}",
                        "placeholder": "Variables: {device_id}, {point_id}, {value}, {quality}, etc.",
                    },
                    {
                        "name": "payload_template",
                        "type": "string",
                        "label": "Payload Template",
                        "placeholder": "JSON template with {device_id}, {value}, {timestamp}, etc.",
                    },
                    {
                        "name": "batch_payload_template",
                        "type": "string",
                        "label": "Batch Payload Template",
                        "placeholder": "{for point in points}...{/for}",
                    },
                    {
                        "name": "gateway_id",
                        "type": "string",
                        "label": "Gateway ID",
                        "placeholder": "Auto-generated if empty",
                    },
                    {
                        "name": "script_enabled",
                        "type": "string",
                        "label": "Enable Script Transform",
                        "default": "false",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "script_code",
                        "type": "string",
                        "label": "JavaScript Script",
                        "placeholder": "function transform(payload, context) { return payload; }",
                    },
                    {
                        "name": "payload_format",
                        "type": "string",
                        "label": "Payload Format",
                        "default": "custom",
                        "placeholder": "json / cbor / protobuf / custom",
                    },
                    {
                        "name": "enable_compression",
                        "type": "string",
                        "label": "Enable Compression",
                        "default": "true",
                        "placeholder": "true/false",
                    },
                    {
                        "name": "compress_threshold",
                        "type": "integer",
                        "label": "Compression Threshold (bytes)",
                        "default": 1024,
                    },
                    {"name": "dedup_window_seconds", "type": "integer", "label": "Dedup Window (s)", "default": 300},
                ],
            },
        }
    )
    return _PLATFORM_REGISTRY


def _build_north_config(platform_name: str, config: dict) -> NorthConfig:
    mqtt_cfg = MqttConnectionConfig(
        broker_host=config.get("broker", config.get("broker_host", "")),
        broker_port=int(config.get("port", config.get("broker_port", 1883))),
        client_id=config.get("client_id", ""),
        username=config.get("username", config.get("token", "")),
        password=config.get("password", config.get("secret", "")),
        keepalive=int(config.get("keepalive", 60)),
        clean_session=str(config.get("clean_session", "true")).lower() == "true",
        protocol_version=int(config.get("protocol_version", 4)),
        transport=config.get("transport", "tcp"),
        tls=MqttTlsConfig(
            enabled=str(config.get("tls_enabled", "false")).lower() == "true",
            ca_cert=config.get("ca_cert", ""),
            client_cert=config.get("client_cert", ""),
            client_key=config.get("client_key", ""),
            verify_server=str(config.get("verify_server", "true")).lower() == "true",
        ),
        will=MqttWillConfig(
            enabled=str(config.get("will_enabled", "false")).lower() == "true",
            topic=config.get("will_topic", ""),
            payload=config.get("will_payload", ""),
            qos=int(config.get("will_qos", 1)),
            retain=str(config.get("will_retain", "true")).lower() == "true",
        ),
        mqtt5_props=Mqtt5Properties(
            enabled=int(config.get("protocol_version", 4)) == 5,
            message_expiry_interval=config.get("message_expiry_interval"),
            content_type=config.get("content_type", ""),
            response_topic=config.get("response_topic", ""),
            correlation_data=config.get("correlation_data", ""),
            user_properties=config.get("user_properties", {}),
        ),
    )
    topic_cfg = TopicTemplateConfig(
        prefix=config.get("topic_prefix", "edgelite"),
        template=config.get("topic_template", "{prefix}/{device_id}/{point_id}"),
        status_template=config.get("status_topic_template", "{prefix}/{device_id}/status"),
    )
    payload_cfg = PayloadConfig(
        format=config.get("payload_format", "json"),
        custom_template=config.get("custom_template", ""),
        compress_threshold=int(config.get("compress_threshold", 1024)),
        enable_compression=str(config.get("enable_compression", "true")).lower() == "true",
    )
    qos_policy = QosPolicy(
        default_qos=int(config.get("default_qos", 0)),
        alarm_qos=int(config.get("alarm_qos", 1)),
        rules=config.get("qos_rules", []),
    )
    return NorthConfig(
        platform_type=platform_name,
        connection_params=config,
        publish_mode=config.get("publish_mode", "realtime"),
        batch_size=int(config.get("batch_size", 100)),
        timeout=float(config.get("timeout", 10.0)),
        enable_qos=str(config.get("enable_qos", "true")).lower() == "true",
        mqtt=mqtt_cfg,
        topic=topic_cfg,
        payload=payload_cfg,
        qos_policy=qos_policy,
        dedup_window_seconds=int(config.get("dedup_window_seconds", 300)),
    )


class PlatformService:
    def __init__(self, handlers: dict[str, PlatformHandler] | None = None):
        self._handlers: dict[str, PlatformHandler] = handlers if handlers is not None else {}
        self._adapters: dict[str, BaseNorthAdapter] = {}
        self._adapter_configs: dict[str, NorthConfig] = {}
        # R8-S-08 修复(严重): 保护 _adapters 的 check-then-act 竞态。
        # connect 检查 platform_name in _adapters 后 await adapter.start()，
        # 期间另一个并发 connect 可能通过同样的检查并重复创建适配器。
        self._lock = asyncio.Lock()

    @property
    def handlers(self) -> dict[str, PlatformHandler]:
        return self._handlers

    @property
    def adapters(self) -> dict[str, BaseNorthAdapter]:
        return self._adapters

    def list_platforms(self) -> list[dict[str, Any]]:
        result = []
        seen: set[str] = set()
        for name, h in self._handlers.items():
            seen.add(name)
            adapter = self._adapters.get(name)
            if adapter:
                result.append(
                    {
                        "name": getattr(adapter, "platform_name", name),
                        "version": getattr(adapter, "platform_version", "1.0.0"),
                        "connected": adapter._connected,
                        "state": adapter._state,
                    }
                )
            else:
                result.append(
                    {
                        "name": getattr(h, "platform_name", name),
                        "version": getattr(h, "platform_version", "1.0.0"),
                        "connected": getattr(h, "_connected", False),
                        "state": "unknown",
                    }
                )
        # FIXED: 原代码仅遍历 _handlers，遗漏仅有 adapter 而无对应 handler 的平台条目
        for name, adapter in self._adapters.items():
            if name in seen:
                continue
            result.append(
                {
                    "name": getattr(adapter, "platform_name", name),
                    "version": getattr(adapter, "platform_version", "1.0.0"),
                    "connected": adapter._connected,
                    "state": adapter._state,
                }
            )
        return result

    def list_supported(self) -> list[dict[str, str]]:
        registry = _ensure_registry()
        return [{"name": k, "label": v["label"], "description": v["description"]} for k, v in registry.items()]

    def get_config_schema(self, platform_name: str) -> dict | None:
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            return None
        base_fields = entry.get("base_fields", entry.get("schema", {}).get("fields", []))
        return _build_full_schema(base_fields)

    def validate_config(self, platform_name: str, config: dict) -> list[str]:
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            return ["ERR_PLATFORM_VALIDATION_UNSUPPORTED"]

        errors: list[str] = []
        base_fields = entry.get("base_fields", entry.get("schema", {}).get("fields", []))

        for f in base_fields:
            if f.get("required"):
                name = f["name"]
                val = config.get(name)
                if not val and val != 0:
                    errors.append(f"ERR_PLATFORM_VALIDATION_REQUIRED:{name}")

        broker_val = config.get("broker")
        if broker_val:
            clean_broker = re.sub(r"^mqtt[s]?://", "", str(broker_val))
            # FIXED-P1: 原代码仅检查首字符，未使用 _BROKER_PATTERN 进行完整验证
            # 导致非法 broker 地址（如含特殊字符）可通过验证
            if not _BROKER_PATTERN.match(clean_broker):
                errors.append("ERR_PLATFORM_VALIDATION_BROKER_FORMAT")

        port_val = config.get("port")
        if port_val is not None:
            try:
                port_int = int(port_val)
                if port_int < 1 or port_int > 65535:
                    errors.append("ERR_PLATFORM_VALIDATION_PORT_RANGE")
            except (ValueError, TypeError):
                errors.append("ERR_PLATFORM_VALIDATION_PORT_NUMBER")

        # FIXED: PEM 证书、脚本代码等长文本字段允许更大长度
        _LONG_TEXT_FIELDS = {
            "ca_cert",
            "client_cert",
            "client_key",
            "script_code",
            "custom_template",
            "password",
            "auth_token",
            "auth_key",
            "secret",
        }
        for f in base_fields:
            name = f["name"]
            val = config.get(name)
            if isinstance(val, str) and len(val) > 256:
                if name not in _LONG_TEXT_FIELDS:
                    errors.append(f"ERR_PLATFORM_VALIDATION_TOO_LONG:{name}")

        topic_template = config.get("topic_template")
        if topic_template:
            from edgelite.platform.mqtt_utils import TopicTemplateEngine

            valid, template_errors = TopicTemplateEngine.validate_template(topic_template)
            if not valid:
                for te in template_errors:
                    errors.append(f"ERR_PLATFORM_VALIDATION_TOPIC:{te}")

        return errors

    async def connect(self, platform_name: str, config: dict) -> dict[str, Any]:
        # R8-S-08: 持锁保护 check-then-act，避免并发 connect 重复创建适配器。
        # 平台连接频率低，持锁等待可接受。
        async with self._lock:
            if platform_name in self._adapters:
                return {"status": "already_connected"}

            registry = _ensure_registry()
            entry = registry.get(platform_name)
            if not entry:
                raise ValueError(f"Unsupported platform: {platform_name}")

            errors = self.validate_config(platform_name, config)
            if errors:
                raise ValueError(";".join(errors))

            north_module = entry.get("north_adapter")
            north_class = entry.get("north_class")
            if north_module and north_class:
                try:
                    mod = importlib.import_module(north_module)
                    adapter_cls = getattr(mod, north_class)
                except (ImportError, AttributeError):
                    adapter_cls = None
            else:
                adapter_cls = None

            if adapter_cls and issubclass(adapter_cls, BaseNorthAdapter):
                north_config = _build_north_config(platform_name, config)
                adapter = adapter_cls(north_config)
                try:
                    await adapter.start(north_config)
                    self._adapters[platform_name] = adapter
                    self._adapter_configs[platform_name] = north_config
                except Exception as e:
                    logger.error("North adapter start failed: %s - %s", platform_name, e)
                    raise RuntimeError(f"Platform connect failed: {platform_name} - {e}") from e
            else:
                # FIXED-P0: 原代码直接使用 entry["module"]/entry["class"]，缺少 KeyError 保护
                module_name = entry.get("module")
                class_name = entry.get("class")
                if not module_name or not class_name:
                    raise RuntimeError(f"Platform {platform_name} missing module/class definition")
                try:
                    module = importlib.import_module(module_name)
                    handler_cls = getattr(module, class_name)
                except (ImportError, AttributeError) as e:
                    logger.error("Platform handler load failed: %s - %s", platform_name, e)
                    raise RuntimeError(f"Platform handler load failed: {platform_name} - {e}") from e
                handler = handler_cls()
                try:
                    await handler.connect(config)
                except Exception as e:
                    logger.error("Platform connect failed: %s - %s", platform_name, e)
                    raise RuntimeError(f"Platform connect failed: {platform_name} - {e}") from e
                self._handlers[platform_name] = handler

            return {"status": "connecting"}

    async def disconnect(self, platform_name: str) -> dict[str, Any]:
        # R8-S-08: 锁内获取引用并移除，锁外执行 stop/disconnect（耗时操作）
        async with self._lock:
            adapter = self._adapters.pop(platform_name, None)
            self._adapter_configs.pop(platform_name, None)
            # FIXED: 原代码使用 .get() 未移除 handler，导致 disconnect 后 handler 仍残留在 _handlers 中
            handler = self._handlers.pop(platform_name, None) if adapter is None else None

        if adapter:
            try:
                await adapter.stop()
            except Exception as e:
                logger.error("North adapter stop failed: %s - %s", platform_name, e)
            return {"status": "disconnected"}

        if handler is None:
            # 再检查一次 _handlers（可能在获取锁前被 connect 添加）
            async with self._lock:
                handler = self._handlers.pop(platform_name, None)
        if not handler:
            raise KeyError(f"Platform {platform_name} not connected")
        try:
            await handler.disconnect()
        except Exception as e:
            logger.error("Platform disconnect failed: %s - %s", platform_name, e)
        return {"status": "disconnected"}

    def get_status(self, platform_name: str) -> dict[str, Any]:
        adapter = self._adapters.get(platform_name)
        if adapter:
            return {
                "connected": adapter._connected,
                "name": adapter.platform_name,
                "version": adapter.platform_version,
                "state": adapter._state,
                "queue_size": adapter._queue.size,
                "messages_total": adapter._metrics.messages_total,
                "errors_total": adapter._metrics.errors_total,
                "dedup_dropped": adapter._metrics.dedup_dropped,
                "compressed_total": adapter._metrics.compressed_total,
                "last_heartbeat": adapter._last_heartbeat,
            }
        handler = self._handlers.get(platform_name)
        if not handler:
            return {"connected": False, "state": "disconnected"}
        return {
            "connected": getattr(handler, "_connected", False),
            "name": getattr(handler, "platform_name", platform_name),
            "version": getattr(handler, "platform_version", "1.0.0"),
            "state": "unknown",
        }

    async def test_connection(self, platform_name: str, config: dict) -> dict[str, Any]:
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            raise ValueError(f"Unsupported platform: {platform_name}")
        errors = self.validate_config(platform_name, config)
        if errors:
            raise ValueError(";".join(errors))

        north_module = entry.get("north_adapter")
        north_class = entry.get("north_class")
        if north_module and north_class:
            try:
                mod = importlib.import_module(north_module)
                adapter_cls = getattr(mod, north_class)
                if issubclass(adapter_cls, BaseNorthAdapter):
                    return await self._test_north_adapter(adapter_cls, platform_name, config)
            except (ImportError, AttributeError):
                pass

        # FIXED-P0: 原代码直接使用 entry["module"]/entry["class"]，缺少 KeyError 保护
        module_name = entry.get("module")
        class_name = entry.get("class")
        if not module_name or not class_name:
            return {"success": False, "message": f"Platform {platform_name} missing module/class definition"}
        try:
            module = importlib.import_module(module_name)
            handler_cls = getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            # FIXED(一般): 原问题-str(e)泄漏内部信息;修复-记录日志并返回通用错误消息
            logger.error("Platform handler load failed: %s", e)
            return {"success": False, "message": "Platform handler load failed, check logs for details"}
        handler = handler_cls()
        try:
            await handler.connect(config)
            max_wait = 10.0
            interval = 0.5
            elapsed = 0.0
            while elapsed < max_wait:
                if getattr(handler, "_connected", False):
                    break
                await asyncio.sleep(interval)
                elapsed += interval
            connected = getattr(handler, "_connected", False)
            await handler.disconnect()
            if connected:
                return {"success": True, "message": "Connection test successful"}
            else:
                return {
                    "success": False,
                    "message": f"Connection timed out after {max_wait:.0f}s",
                }
        except ImportError as e:
            return {"success": False, "message": f"Missing dependency: {e}"}
        except Exception as e:
            # FIXED(一般): 原问题-str(e)泄漏内部信息;修复-记录日志并返回通用错误消息
            logger.error("Platform connection test failed: %s", e)
            return {"success": False, "message": "Connection test failed, check logs for details"}

    async def _test_north_adapter(
        self,
        adapter_cls: type,
        platform_name: str,
        config: dict,
    ) -> dict[str, Any]:
        north_config = _build_north_config(platform_name, config)
        adapter = adapter_cls(north_config)
        try:
            await adapter.start(north_config)
            connected = await adapter.is_connected()
            await adapter.stop()
            if connected:
                return {"success": True, "message": "Connection test successful"}
            return {"success": False, "message": "Connection test failed: not connected"}
        except Exception as e:
            # FIXED(一般): 原问题-str(e)泄漏内部信息;修复-记录日志并返回通用错误消息
            # 注意: logger.error 须在内层 try 之前调用，避免内层 except as e 删除外层 e
            logger.error("North adapter connection test failed: %s", e)
            try:
                await adapter.stop()
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("连接测试后停止适配器失败: %s", e)
            return {"success": False, "message": "Connection test failed, check logs for details"}

    def get_message_preview(self, platform_name: str) -> list[dict]:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_message_preview"):
            return adapter.get_message_preview()
        return []

    def get_broker_quality(self, platform_name: str) -> dict:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_broker_quality"):
            return adapter.get_broker_quality()
        return {
            "avg_latency_ms": 0.0,
            "max_latency_ms": 0.0,
            "min_latency_ms": 0.0,
            "packet_loss_count": 0,
            "samples": 0,
        }

    def validate_topic_template(self, template: str) -> dict:
        from edgelite.platform.mqtt_utils import TopicTemplateEngine

        valid, errors = TopicTemplateEngine.validate_template(template)
        variables = TopicTemplateEngine.extract_variables(template)
        return {
            "valid": valid,
            "errors": errors,
            "variables": variables,
        }

    def get_tb_devices(self, platform_name: str) -> list[dict]:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_device_list"):
            return adapter.get_device_list()
        return []

    def get_tb_rpc_logs(self, platform_name: str) -> list[dict]:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_rpc_logs"):
            return adapter.get_rpc_logs()
        return []

    def get_tb_alarm_records(self, platform_name: str) -> list[dict]:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_alarm_records"):
            return adapter.get_alarm_records()
        return []

    def get_tb_sync_status(self, platform_name: str) -> dict:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_sync_status"):
            return adapter.get_sync_status()
        return {
            "total_devices": 0,
            "registered_devices": 0,
            "pending_devices": 0,
            "last_sync_ts": 0.0,
            "rpc_pending": 0,
            "rpc_completed": 0,
            "alarms_synced": 0,
        }

    def get_platform_shadow(self, platform_name: str) -> dict:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_shadow_cache"):
            return adapter.get_shadow_cache()
        return {}

    def get_platform_command_logs(self, platform_name: str) -> list[dict]:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_command_logs"):
            return adapter.get_command_logs()
        return []

    def get_platform_alarm_records(self, platform_name: str) -> list[dict]:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_alarm_records"):
            return adapter.get_alarm_records()
        return []

    def get_platform_device_mapping(self, platform_name: str) -> list[dict]:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_device_mapping"):
            return adapter.get_device_mapping()
        return []

    async def get_dashboard_data(
        self,
    ) -> list[dict[str, Any]]:  # FIXED-P0: 适配BaseNorthAdapter.get_dashboard_data改为async
        registry = _ensure_registry()
        result = []
        all_names = set(list(self._adapters.keys()) + list(self._handlers.keys()))
        for name in all_names:
            adapter = self._adapters.get(name)
            if adapter:
                label = registry.get(name, {}).get("label", name)
                result.append(await adapter.get_dashboard_data(label))  # FIXED-P0: await async方法
            else:
                handler = self._handlers.get(name)
                if handler:
                    label = registry.get(name, {}).get("label", name)
                    result.append(
                        {
                            "platform_name": name,
                            "label": label,
                            "state": "unknown",
                            "connected": getattr(handler, "_connected", False),
                            "messages_today": 0,
                            "error_rate": 0.0,
                            "queue_backlog": 0,
                            "last_heartbeat": None,
                            "latency_ms": 0.0,
                        }
                    )
        for name, entry in registry.items():
            if name not in all_names:
                result.append(
                    {
                        "platform_name": name,
                        "label": entry["label"],
                        "state": "disconnected",
                        "connected": False,
                        "messages_today": 0,
                        "error_rate": 0.0,
                        "queue_backlog": 0,
                        "last_heartbeat": None,
                        "latency_ms": 0.0,
                    }
                )
        return result

    def export_config(self, platform_name: str) -> dict:
        # FIXED(安全): 对返回的 config 中的敏感字段（password, secret, api_key, token, access_secret）
        # 进行脱敏，防止 VIEWER 角色通过导出接口获取 MQTT broker 密码、ThingsBoard token 等明文凭据
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_custom_config"):
            custom_cfg = adapter.get_custom_config()
            if custom_cfg:
                return {
                    "platform_name": platform_name,
                    "version": getattr(adapter, "platform_version", "1.0.0"),
                    "config": _mask_sensitive_config_fields(custom_cfg),
                    "exported_at": time.time(),
                }
        config = self._adapter_configs.get(platform_name)
        if config:
            return {
                "platform_name": platform_name,
                "config": _mask_sensitive_config_fields(config.model_dump()),
                "exported_at": time.time(),
            }
        return {"platform_name": platform_name, "config": {}, "exported_at": time.time()}

    def import_config(self, platform_name: str, config_data: dict) -> dict:
        registry = _ensure_registry()
        entry = registry.get(platform_name)
        if not entry:
            raise ValueError(f"Unsupported platform: {platform_name}")
        imported_config = config_data.get("config", config_data)
        # FIXED: 原代码对所有平台都调用 _flatten_custom_config，导致非 custom 平台的
        # 有效配置字段（broker/port/device_token 等）被剥离。仅 custom 平台使用嵌套结构。
        if platform_name == "custom" and isinstance(imported_config, dict):
            flat_config = self._flatten_custom_config(imported_config)
        else:
            flat_config = imported_config
        errors = self.validate_config(platform_name, flat_config)
        if errors:
            raise ValueError(";".join(errors))
        return {"status": "imported", "config": flat_config}

    def _flatten_custom_config(self, nested: dict) -> dict:
        result = {}
        brokers = nested.get("brokers", [])
        if brokers and isinstance(brokers, list):
            result["brokers"] = brokers
            if brokers:
                primary = brokers[0]
                for key in (
                    "broker_host",
                    "broker_port",
                    "username",
                    "password",
                    "client_id",
                    "keepalive",
                    "clean_session",
                    "protocol_version",
                    "transport",
                ):
                    if key in primary:
                        # FIXED: 原代码仅映射 broker_host→broker，遗漏 broker_port→port，
                        # 导致导入的 custom 嵌套配置端口字段名不正确
                        mapped = {"broker_host": "broker", "broker_port": "port"}.get(key, key)
                        result[mapped] = primary[key]
        template = nested.get("template", {})
        if template:
            for key in (
                "topic_template",
                "payload_template",
                "batch_payload_template",
                "status_topic_template",
                "topic_prefix",
            ):
                if key in template:
                    result[key] = template[key]
        script = nested.get("script", {})
        if script:
            if "enabled" in script:
                result["script_enabled"] = str(script["enabled"]).lower()
            if "script" in script:
                result["script_code"] = script["script"]
            if "script_language" in script:
                result["script_language"] = script["script_language"]
        for key in ("gateway_id", "payload_format", "enable_compression", "compress_threshold", "dedup_window_seconds"):
            if key in nested:
                result[key] = nested[key]
        user_props = nested.get("user_properties", [])
        if user_props:
            result["mqtt5_user_properties"] = user_props
        qos = nested.get("qos_policy", {})
        if qos:
            if "default_qos" in qos:
                result["default_qos"] = qos["default_qos"]
            if "alarm_qos" in qos:
                result["alarm_qos"] = qos["alarm_qos"]
        return result

    def get_broker_status(self, platform_name: str) -> list[dict]:
        adapter = self._adapters.get(platform_name)
        if adapter and hasattr(adapter, "get_broker_status"):
            return adapter.get_broker_status()
        return []

    def validate_advanced_template(self, template: str, template_type: str = "payload") -> dict:
        from edgelite.platform.mqtt_utils import AdvancedTemplateEngine

        valid, errors = AdvancedTemplateEngine.validate_template(template)
        variables = AdvancedTemplateEngine.extract_variables(template)
        return {
            "valid": valid,
            "errors": errors,
            "variables": variables,
            "template_type": template_type,
        }

    def preview_template(self, template: str, test_data: dict, template_type: str = "payload") -> dict:
        from edgelite.platform.mqtt_utils import AdvancedTemplateEngine

        engine = AdvancedTemplateEngine(gateway_id=test_data.get("gateway_id", ""))
        try:
            if template_type == "topic":
                result = engine.render_topic(template, type("DP", (), test_data)(), test_data.get("gateway_id", ""))
            elif template_type == "batch_payload":
                points = test_data.get("points", [test_data])
                mock_dps = [type("DP", (), p)() for p in points]
                result = engine.render_batch_payload(template, mock_dps, test_data.get("gateway_id", ""))
            else:
                result = engine.render_payload(template, type("DP", (), test_data)(), test_data.get("gateway_id", ""))
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def validate_script(self, script: str) -> dict:
        from edgelite.platform.js_sandbox import JsSandbox

        sandbox = JsSandbox()
        valid, errors = sandbox.validate_script(script)
        return {"valid": valid, "errors": errors}

    async def test_script(self, script: str, test_payload: dict, test_context: dict | None = None) -> dict:
        from edgelite.platform.js_sandbox import JsSandbox

        sandbox = JsSandbox()
        result = await sandbox.execute(script, test_payload, test_context or {})
        return {
            "success": result.success,
            "data": result.data,
            "error": result.error,
            "execution_ms": result.execution_ms,
        }

    async def mqtt_test_publish(self, platform_name: str, topic: str, payload: str, qos: int = 0) -> dict:
        # FIXED(严重): 原问题-topic无校验可含MQTT通配符(#/+)，可能导致意外广播;
        # 修复-禁止MQTT通配符
        if "#" in topic or "+" in topic:
            return {"success": False, "message": "Topic cannot contain wildcards (# or +)"}
        adapter = self._adapters.get(platform_name)
        if not adapter or not getattr(adapter, "_connected", False):
            return {"success": False, "error": "Adapter not connected"}
        # FIXED-P1: 原代码直接访问 adapter._mqtt_client，应使用 hasattr 保护避免 AttributeError
        mqtt_client = getattr(adapter, "_mqtt_client", None)
        if not mqtt_client:
            return {"success": False, "error": "MQTT client not available"}
        try:
            await mqtt_client.publish(topic, payload.encode("utf-8"), qos=qos)
            return {"success": True, "message": f"Published to {topic}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_north_metrics(self) -> str:
        lines = []
        for adapter in self._adapters.values():
            lines.append(adapter.get_prometheus_metrics())
        return "\n".join(lines)

    async def reload_config(self, platform_name: str, config: dict) -> dict[str, Any]:
        # FIX-P1: 原代码未获取 self._lock，与 connect/disconnect 并发修改 _adapters/
        # _adapter_configs 时状态不一致。reload 频率低，持锁保护 check-then-act
        # 与 connect/disconnect 串行化，避免竞态。
        async with self._lock:
            if platform_name not in self._adapters and platform_name not in self._handlers:
                raise KeyError(f"Platform {platform_name} not found")

            errors = self.validate_config(platform_name, config)
            if errors:
                raise ValueError(";".join(errors))

            adapter = self._adapters.get(platform_name)
            if adapter:
                try:
                    await adapter.stop()
                except Exception as e:
                    logger.error("Adapter stop during reload failed: %s", e)
                north_config = _build_north_config(platform_name, config)
                try:
                    await adapter.start(north_config)
                    self._adapter_configs[platform_name] = north_config
                    return {"status": "reloaded", "connected": adapter._connected}
                except Exception as e:
                    # FIXED-P1: 原代码重启失败后 adapter 处于停止状态但配置已更新，
                    # 从 _adapters 中移除避免后续操作访问已停止的 adapter
                    logger.error("Adapter reload failed: %s - %s", platform_name, e)
                    self._adapters.pop(platform_name, None)
                    self._adapter_configs.pop(platform_name, None)
                    raise RuntimeError(f"Config reload failed: {platform_name} - {e}") from e

            return {"status": "reload_not_supported"}
