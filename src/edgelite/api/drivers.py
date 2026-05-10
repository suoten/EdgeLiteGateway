"""驱动配置管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/drivers", tags=["驱动配置"])


class DriverInfo(BaseModel):
    name: str
    version: str = "1.0.0"
    protocols: list[str] = []
    description: str = ""


class DriverListResponse(BaseModel):
    drivers: list[DriverInfo]
    total: int


class DriverProtocolsResponse(BaseModel):
    protocols: list[str]


class DriverConfigSchemaResponse(BaseModel):
    driver_name: str
    schema: dict


class DriverStatusInfo(BaseModel):
    name: str
    class_: str = Field(alias="class")
    module: str
    custom: bool | None = None
    loaded: bool | None = None
    error: str | None = None

    model_config = {"populate_by_name": True}


class DriverDiscoverResponse(BaseModel):
    devices: list[dict]


def _get_registry():
    from edgelite.app import _app_state

    return getattr(_app_state, "driver_registry", None)


@router.get("/list", response_model=ApiResponse[DriverListResponse])
async def list_drivers(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    registry = _get_registry()
    try:
        if not registry:
            raise HTTPException(status_code=501, detail="驱动注册表未初始化")

        drivers = []
        for name, driver_cls in registry.items():
            instance = driver_cls() if isinstance(driver_cls, type) else driver_cls
            drivers.append(
                {
                    "name": getattr(instance, "plugin_name", name),
                    "version": getattr(instance, "plugin_version", "1.0.0"),
                    "protocols": getattr(instance, "supported_protocols", []),
                    "description": getattr(instance, "__doc__", "") or "",
                }
            )

        return ApiResponse(data={"drivers": drivers, "total": len(drivers)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


@router.get("/protocols", response_model=ApiResponse[DriverProtocolsResponse])
async def list_protocols(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    registry = _get_registry()
    try:
        if not registry:
            raise HTTPException(status_code=501, detail="驱动注册表未初始化")

        protocols = registry.get_supported_protocols()
        return ApiResponse(data={"protocols": protocols})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


@router.get("/{driver_name}/config-schema", response_model=ApiResponse[DriverConfigSchemaResponse])
async def get_driver_config_schema(
    driver_name: str,
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    registry = _get_registry()
    try:
        if not registry:
            raise HTTPException(status_code=501, detail="驱动注册表未初始化")

        driver_cls = registry.get_driver_class(driver_name)
        if not driver_cls:
            raise HTTPException(status_code=404, detail=f"驱动 {driver_name} 不存在")

        if getattr(driver_cls, "config_schema", None):
            return ApiResponse(data={"driver_name": driver_name, "schema": driver_cls.config_schema})

        schemas = {
            "modbus_tcp": {
                "description": "Modbus TCP 工业标准协议，用于读写PLC/仪表的线圈和寄存器",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "PLC或网关的IP地址，如 192.168.1.100",
                        "default": "192.168.1.100",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "Modbus TCP服务端口号，默认502",
                        "default": 502,
                        "required": True,
                    },
                    {
                        "name": "slave_id",
                        "type": "integer",
                        "label": "从站ID",
                        "description": "设备从站地址（Unit ID），通常为1",
                        "default": 1,
                        "required": True,
                    },
                    {
                        "name": "timeout",
                        "type": "number",
                        "label": "超时(秒)",
                        "description": "连接和读取超时时间",
                        "default": 3.0,
                    },
                ],
            },
            "opcua": {
                "description": "OPC UA 工业互联协议，支持加密认证和节点浏览",
                "fields": [
                    {
                        "name": "endpoint",
                        "type": "string",
                        "label": "OPC UA端点",
                        "description": "OPC UA服务器端点URL",
                        "default": "opc.tcp://localhost:4840",
                        "required": True,
                    },
                    {
                        "name": "username",
                        "type": "string",
                        "label": "用户名",
                        "description": "匿名登录可留空",
                    },
                    {
                        "name": "password",
                        "type": "string",
                        "label": "密码",
                        "description": "用户密码，匿名登录可留空",
                        "secret": True,
                    },
                    {
                        "name": "security_mode",
                        "type": "string",
                        "label": "安全模式",
                        "description": "通信加密方式，None为明文，SignAndEncrypt为最高安全",
                        "default": "None",
                        "options": ["None", "Sign", "SignAndEncrypt"],
                    },
                ],
            },
            "s7": {
                "description": "西门子S7系列PLC通信协议（S7-200/300/400/1200/1500）",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "PLC的IP地址",
                        "default": "192.168.1.1",
                        "required": True,
                    },
                    {
                        "name": "rack",
                        "type": "integer",
                        "label": "机架号",
                        "description": "硬件机架号，通常为0",
                        "default": 0,
                    },
                    {
                        "name": "slot",
                        "type": "integer",
                        "label": "槽号",
                        "description": "CPU插槽号，S7-300通常为2，S7-1200/1500通常为0或1",
                        "default": 1,
                    },
                ],
            },
            "serial_port": {
                "description": "串口通信（RS232/RS485），支持Modbus RTU等协议",
                "fields": [
                    {
                        "name": "port",
                        "type": "string",
                        "label": "串口设备",
                        "description": "串口设备路径，Windows如 COM1/COM2，Linux如 /dev/ttyUSB0",
                        "default": "COM1",
                        "required": True,
                    },
                    {
                        "name": "baudrate",
                        "type": "integer",
                        "label": "波特率",
                        "description": "串口通信速率，需与设备一致",
                        "default": 9600,
                        "options": [9600, 19200, 38400, 57600, 115200],
                    },
                    {
                        "name": "bytesize",
                        "type": "integer",
                        "label": "数据位",
                        "description": "每个字节的数据位数",
                        "default": 8,
                        "options": [5, 6, 7, 8],
                    },
                    {
                        "name": "parity",
                        "type": "string",
                        "label": "校验位",
                        "description": "N=无校验，E=偶校验，O=奇校验",
                        "default": "N",
                        "options": ["N", "E", "O"],
                    },
                    {
                        "name": "stopbits",
                        "type": "number",
                        "label": "停止位",
                        "description": "停止位数量",
                        "default": 1,
                        "options": [1, 1.5, 2],
                    },
                    {
                        "name": "protocol",
                        "type": "string",
                        "label": "上层协议",
                        "description": "raw=原始数据透传，modbus_rtu=Modbus RTU协议",
                        "default": "raw",
                        "options": ["raw", "modbus_rtu"],
                    },
                ],
            },
            "database_source": {
                "description": "数据库接入，通过SQL查询将数据库表字段映射为测点",
                "fields": [
                    {
                        "name": "db_type",
                        "type": "string",
                        "label": "数据库类型",
                        "description": "目标数据库类型",
                        "default": "mysql",
                        "required": True,
                        "options": ["mysql", "postgresql", "sqlite", "mssql"],
                    },
                    {
                        "name": "host",
                        "type": "string",
                        "label": "主机地址",
                        "description": "数据库服务器IP或域名",
                        "default": "localhost",
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "数据库服务端口，MySQL默认3306，PostgreSQL默认5432",
                        "default": 3306,
                    },
                    {
                        "name": "database",
                        "type": "string",
                        "label": "数据库名",
                        "description": "要连接的数据库名称",
                        "required": True,
                    },
                    {
                        "name": "username",
                        "type": "string",
                        "label": "用户名",
                        "description": "数据库登录用户名",
                    },
                    {
                        "name": "password",
                        "type": "string",
                        "label": "密码",
                        "description": "数据库登录密码",
                        "secret": True,
                    },
                    {
                        "name": "pool_size",
                        "type": "integer",
                        "label": "连接池大小",
                        "description": "数据库连接池最大连接数",
                        "default": 5,
                    },
                ],
            },
            "barcode_scanner": {
                "description": "USB/串口扫码枪，自动解析条码数据",
                "fields": [
                    {
                        "name": "port",
                        "type": "string",
                        "label": "串口设备",
                        "description": "扫码枪连接的串口，如 COM1 或 /dev/ttyUSB0",
                        "default": "COM1",
                        "required": True,
                    },
                    {
                        "name": "baudrate",
                        "type": "integer",
                        "label": "波特率",
                        "description": "扫码枪串口波特率",
                        "default": 9600,
                    },
                    {
                        "name": "prefix",
                        "type": "string",
                        "label": "条码前缀",
                        "description": "条码数据前缀标识，用于过滤特定条码",
                    },
                    {
                        "name": "suffix",
                        "type": "string",
                        "label": "条码后缀",
                        "description": "条码结束符，通常为回车符 \\r",
                        "default": "\\r",
                    },
                ],
            },
            "mqtt_client": {
                "description": "MQTT客户端，订阅设备数据主题，支持JSON解析",
                "fields": [
                    {
                        "name": "broker",
                        "type": "string",
                        "label": "Broker地址",
                        "description": "MQTT服务器地址，如 localhost 或 broker.emqx.io",
                        "default": "localhost",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "MQTT服务端口，默认1883（非加密）或8883（TLS）",
                        "default": 1883,
                    },
                    {
                        "name": "username",
                        "type": "string",
                        "label": "用户名",
                        "description": "MQTT认证用户名，无认证可留空",
                    },
                    {
                        "name": "password",
                        "type": "string",
                        "label": "密码",
                        "description": "MQTT认证密码",
                        "secret": True,
                    },
                    {
                        "name": "topic",
                        "type": "string",
                        "label": "订阅主题",
                        "description": "要订阅的MQTT主题，支持通配符如 device/+/data",
                        "required": True,
                    },
                ],
            },
            "http_webhook": {
                "description": "HTTP Webhook，设备通过HTTP POST主动推送数据到EdgeLite",
                "fields": [
                    {
                        "name": "path",
                        "type": "string",
                        "label": "Webhook路径",
                        "description": "数据接收URL路径，如 /webhook/data",
                        "default": "/webhook/data",
                        "required": True,
                    },
                    {
                        "name": "secret",
                        "type": "string",
                        "label": "签名密钥",
                        "description": "用于验证请求签名的密钥，留空则不验证",
                    },
                    {
                        "name": "method",
                        "type": "string",
                        "label": "HTTP方法",
                        "description": "接收数据使用的HTTP方法",
                        "default": "POST",
                        "options": ["POST", "PUT"],
                    },
                ],
            },
            "sparkplug_b": {
                "description": "MQTT Sparkplug B工业物联网协议，标准化设备数据发布/订阅",
                "fields": [
                    {
                        "name": "group_id",
                        "type": "string",
                        "label": "组ID",
                        "description": "Sparkplug B逻辑组ID",
                        "default": "group1",
                        "required": True,
                    },
                    {
                        "name": "edge_node_id",
                        "type": "string",
                        "label": "边缘节点ID",
                        "description": "本网关在Sparkplug B中的节点ID",
                        "default": "edgelite_node",
                        "required": True,
                    },
                    {
                        "name": "mqtt_broker",
                        "type": "string",
                        "label": "Broker地址",
                        "description": "MQTT Broker地址",
                        "default": "localhost",
                        "required": True,
                    },
                    {
                        "name": "mqtt_port",
                        "type": "integer",
                        "label": "端口",
                        "description": "MQTT Broker端口",
                        "default": 1883,
                    },
                ],
            },
            "dlt645": {
                "description": "DL/T 645-2007 多功能电能表通信协议，通过RS485串口采集电表数据",
                "fields": [
                    {
                        "name": "port",
                        "type": "string",
                        "label": "串口设备",
                        "description": "RS485串口设备路径",
                        "default": "COM1",
                        "required": True,
                    },
                    {
                        "name": "baud_rate",
                        "type": "integer",
                        "label": "波特率",
                        "description": "电表通信波特率，默认2400",
                        "default": 2400,
                    },
                    {
                        "name": "parity",
                        "type": "string",
                        "label": "校验位",
                        "description": "E=偶校验（默认）",
                        "default": "E",
                        "options": ["E", "N", "O"],
                    },
                    {
                        "name": "timeout",
                        "type": "number",
                        "label": "超时(秒)",
                        "description": "通信超时时间",
                        "default": 5.0,
                    },
                ],
            },
            "iec104": {
                "description": "IEC 60870-5-104 电力远动规约，用于与电力SCADA系统通信",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "SCADA或保护装置IP地址",
                        "default": "127.0.0.1",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "IEC 104默认端口2404",
                        "default": 2404,
                    },
                    {
                        "name": "asdu_addr",
                        "type": "integer",
                        "label": "ASDU地址",
                        "description": "ASDU公共地址",
                        "default": 1,
                    },
                    {
                        "name": "heartbeat_interval",
                        "type": "number",
                        "label": "心跳间隔(秒)",
                        "description": "T3超时时间，心跳发送间隔",
                        "default": 30.0,
                    },
                ],
            },
            "kuka_ekrl": {
                "description": "KUKA机器人Ethernet KRL XML协议，用于读写机器人变量",
                "fields": [
                    {
                        "name": "ip",
                        "type": "string",
                        "label": "IP地址",
                        "description": "KUKA控制器IP地址",
                        "default": "192.168.1.100",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "EKRL端口，默认54600",
                        "default": 54600,
                    },
                ],
            },
            "abb_rws": {
                "description": "ABB机器人Robot Web Services协议，通过REST API读写机器人数据",
                "fields": [
                    {
                        "name": "ip",
                        "type": "string",
                        "label": "IP地址",
                        "description": "ABB控制器IP地址",
                        "default": "192.168.1.100",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "RWS端口，默认80",
                        "default": 80,
                    },
                ],
            },
            "onvif": {
                "description": "ONVIF视频设备协议，支持设备发现/RTSP流/PTZ云台控制",
                "fields": [
                    {
                        "name": "ip",
                        "type": "string",
                        "label": "IP地址",
                        "description": "ONVIF设备IP地址",
                        "default": "",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "ONVIF服务端口，默认80",
                        "default": 80,
                    },
                    {
                        "name": "username",
                        "type": "string",
                        "label": "用户名",
                        "description": "设备认证用户名",
                        "default": "admin",
                    },
                    {
                        "name": "password",
                        "type": "string",
                        "label": "密码",
                        "description": "设备认证密码",
                        "secret": True,
                    },
                ],
            },
            "mc": {
                "description": "三菱MC协议（MELSEC Communication），支持Q/L/FX系列PLC",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "PLC的IP地址",
                        "default": "192.168.1.1",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "MC协议端口，默认5007",
                        "default": 5007,
                    },
                    {
                        "name": "plc_type",
                        "type": "string",
                        "label": "PLC型号",
                        "description": "Q系列=Q，L系列=L，FX系列=iQ-R",
                        "default": "Q",
                        "options": ["Q", "L", "iQ-R"],
                    },
                ],
            },
            "fins": {
                "description": "欧姆龙FINS协议，支持CJ/CP/NJ系列PLC",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "PLC的IP地址",
                        "default": "192.168.1.1",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "FINS UDP端口，默认9600",
                        "default": 9600,
                    },
                    {
                        "name": "source_node",
                        "type": "integer",
                        "label": "源节点号",
                        "description": "本机FINS节点号",
                        "default": 0,
                    },
                    {
                        "name": "dest_node",
                        "type": "integer",
                        "label": "目标节点号",
                        "description": "PLC的FINS节点号",
                        "default": 1,
                    },
                ],
            },
            "allen_bradley": {
                "description": "Allen-Bradley PLC协议（pylogix），支持ControlLogix/CompactLogix",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "AB PLC的IP地址",
                        "default": "192.168.1.1",
                        "required": True,
                    },
                    {
                        "name": "slot",
                        "type": "integer",
                        "label": "槽号",
                        "description": "CPU所在槽位，ControlLogix默认0，CompactLogix默认0",
                        "default": 0,
                    },
                ],
            },
            "fanuc": {
                "description": "FANUC CNC数控系统FOCAS协议，支持读取机床状态和坐标",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "CNC控制器IP地址",
                        "default": "192.168.1.1",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "FOCAS端口，默认8193",
                        "default": 8193,
                    },
                    {
                        "name": "timeout",
                        "type": "integer",
                        "label": "超时(秒)",
                        "description": "连接超时时间",
                        "default": 5,
                    },
                ],
            },
            "mtconnect": {
                "description": "MTConnect数控设备标准协议，通过HTTP获取CNC运行数据",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "MTConnect代理地址",
                        "default": "127.0.0.1",
                        "required": True,
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "HTTP端口，默认5000",
                        "default": 5000,
                    },
                ],
            },
            "toledo": {
                "description": "托利多称重仪表协议，支持TCP/Serial/MT-SICS通信",
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "IP地址",
                        "description": "称重仪表IP地址（TCP模式）",
                        "default": "192.168.1.1",
                    },
                    {
                        "name": "port",
                        "type": "integer",
                        "label": "端口",
                        "description": "TCP端口，默认1701",
                        "default": 1701,
                    },
                    {
                        "name": "mode",
                        "type": "string",
                        "label": "通信模式",
                        "description": "TCP或Serial",
                        "default": "tcp",
                        "options": ["tcp", "serial"],
                    },
                ],
            },
            "opc_da": {
                "description": "OPC DA经典协议（Windows COM），读取传统OPC服务器数据",
                "fields": [
                    {
                        "name": "prog_id",
                        "type": "string",
                        "label": "ProgID",
                        "description": "OPC DA服务器的ProgID，如 Matrikon.OPC.Simulation",
                        "required": True,
                    },
                    {
                        "name": "host",
                        "type": "string",
                        "label": "主机",
                        "description": "OPC服务器所在主机，本机留空",
                        "default": "",
                    },
                ],
            },
            "video": {
                "description": "GB28181视频监控协议，通过PyGBSentry接入视频流和云台控制",
                "fields": [
                    {
                        "name": "pygbsentry_url",
                        "type": "string",
                        "label": "PyGBSentry地址",
                        "description": "PyGBSentry平台API地址",
                        "default": "http://127.0.0.1:8080",
                        "required": True,
                    },
                    {
                        "name": "username",
                        "type": "string",
                        "label": "用户名",
                        "description": "PyGBSentry登录用户名",
                        "default": "admin",
                    },
                    {
                        "name": "password",
                        "type": "string",
                        "label": "密码",
                        "description": "PyGBSentry登录密码",
                        "secret": True,
                    },
                ],
            },
            "modbus_rtu": {
                "description": "Modbus RTU串口协议，通过RS485/RS232连接Modbus从站设备",
                "fields": [
                    {
                        "name": "port",
                        "type": "string",
                        "label": "串口设备",
                        "description": "串口路径，如COM1或/dev/ttyUSB0",
                        "default": "COM1",
                        "required": True,
                    },
                    {
                        "name": "baudrate",
                        "type": "integer",
                        "label": "波特率",
                        "description": "通信速率",
                        "default": 9600,
                        "options": [9600, 19200, 38400, 57600, 115200],
                    },
                    {
                        "name": "parity",
                        "type": "string",
                        "label": "校验位",
                        "description": "N=无校验，E=偶校验，O=奇校验",
                        "default": "N",
                        "options": ["N", "E", "O"],
                    },
                    {
                        "name": "stopbits",
                        "type": "integer",
                        "label": "停止位",
                        "description": "停止位数量",
                        "default": 1,
                    },
                    {
                        "name": "slave_id",
                        "type": "integer",
                        "label": "从站地址",
                        "description": "Modbus从站地址",
                        "default": 1,
                        "required": True,
                    },
                ],
            },
        }

        _schema_aliases = {
            "siemens_s7": "s7",
            "mitsubishi_mc": "mc",
            "omron_fins": "fins",
            "fanuc_cnc": "fanuc",
            "kuka_ekrl": "kuka",
            "abb_rws": "abb_robot",
            "serial_port": "modbus_rtu",
            "serial_modbus_rtu": "modbus_rtu",
            "serial_raw": "serial_port",
        }

        lookup_key = _schema_aliases.get(driver_name, driver_name)
        schema = schemas.get(
            lookup_key,
            {
                "fields": [
                    {
                        "name": "host",
                        "type": "string",
                        "label": "主机地址",
                        "default": "localhost",
                        "required": True,
                    },
                    {"name": "port", "type": "integer", "label": "端口", "default": 0},
                ]
            },
        )

        return ApiResponse(data={"driver_name": driver_name, "schema": schema})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e


@router.get("", response_model=ApiResponse[list[DriverStatusInfo]])
async def list_all_drivers(user: CurrentUser = require_permission(Permission.SYSTEM_READ)):
    """查询所有驱动状态"""
    try:
        from edgelite.app import _app_state

        drivers_info = []
        registry = _app_state.driver_registry
        if registry:
            for name, cls in registry.items():
                drivers_info.append(
                    {
                        "name": name,
                        "class": cls.__name__,
                        "module": cls.__module__,
                    }
                )
        if hasattr(_app_state, "plugin_manager") and _app_state.plugin_manager:
            for info in _app_state.plugin_manager.list_plugins():
                drivers_info.append(
                    {
                        "name": info.name,
                        "class": info.class_name,
                        "module": info.module_path,
                        "custom": info.is_custom,
                        "loaded": info.is_loaded,
                        "error": info.error,
                    }
                )
        return ApiResponse(data=drivers_info)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


class DriverDiscoverRequest(BaseModel):
    config: dict = {}


@router.post("/{driver_name}/discover", response_model=ApiResponse[DriverDiscoverResponse])
async def discover_devices(
    driver_name: str,
    req: DriverDiscoverRequest = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=501, detail="驱动注册表未初始化")

    driver_cls = registry.get_driver_class(driver_name)
    if not driver_cls:
        raise HTTPException(status_code=404, detail=f"驱动 {driver_name} 不存在")

    try:
        driver = driver_cls()
        driver_config = req.config if req else {}
        await driver.start(driver_config)
        devices = await driver.discover_devices(driver_config)
        try:
            await driver.stop()
        except Exception as e:
            logger.warning("驱动停止失败: %s", e)
        return ApiResponse(data={"devices": devices})
    except Exception as e:
        raise HTTPException(status_code=500, detail="设备发现失败") from e
