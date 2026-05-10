# 22 种工业协议，一个 Python 网关全搞定——EdgeLiteGateway 驱动层深度拆解

> 本文从协议实现角度，逐一拆解 EdgeLiteGateway 内置的 22 种工业协议驱动，分析每种协议的 Python 库选型、踩坑经验和实际性能数据。适合工业自动化、IoT 平台开发者参考。

## 为什么协议驱动是边缘网关的核心壁垒？

边缘网关的本质是**协议翻译器**——把 PLC 的 Modbus 寄存器翻译成云平台的 MQTT 消息。协议支持的数量和质量，直接决定网关能覆盖多少场景。

传统 C/Java 网关的协议支持通常停留在 5-8 种（Modbus + OPC UA + 几种私有协议），因为每新增一种协议的开发成本极高。EdgeLiteGateway 用 Python 生态做到了 22 种，核心原因是 **PyPI 上几乎每种工业协议都有现成的 Python 库**。

## 22 种协议驱动全景

### 第一梯队：工业通信三大件

#### 1. Modbus TCP/RTU — pymodbus 3.7+

工业领域使用最广泛的协议，没有之一。90% 的 PLC、仪表、变频器都支持。

```python
from pymodbus.client import AsyncModbusTcpClient

class ModbusTcpDriver(DriverPlugin):
    plugin_name = "modbus_tcp"
    supported_protocols = ["modbus_tcp"]

    async def read_points(self, device_id, points):
        client = AsyncModbusTcpClient(host, port)
        await client.connect()
        result = await client.read_holding_registers(address, count, slave=slave_id)
        return {point_name: result.registers[i] for i, point_name in enumerate(points)}
```

**踩坑经验**：
- pymodbus 3.x 重写了整个 API，2.x 的代码全部不兼容。迁移时注意 `client.read_holding_registers` 返回的是 `ReadRegistersResponse`，不是直接的数据
- RTU 模式下串口超时是硬伤，`timeout` 参数建议设为 3 秒，低于 1 秒在长线缆场景下大量超时
- 从站 ID（slave_id）范围 1-247，很多开发者忘记这个限制

#### 2. OPC UA — asyncua 1.1+

OPC UA 是工业 4.0 的"统一语言"，SCADA/MES 系统的标准接口。

```python
from asyncua import Client

class OpcUaDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        async with Client(endpoint) as client:
            values = {}
            for point in points:
                node = client.get_node(f"ns={ns};s={point}")
                values[point] = await node.read_value()
            return values
```

**踩坑经验**：
- OPC UA 服务器经常用自签名证书，`Client` 需要设置 `security_mode=MessageSecurityMode.None_` 才能连接开发环境
- 节点浏览（browse）是递归操作，大型服务器上可能返回几千个节点，必须做分页
- 订阅模式（Subscription）比轮询模式效率高 10 倍以上，但实现复杂度也高

#### 3. 西门子 S7 — snap7

S7 协议是西门子 PLC 的原生通信协议，覆盖 S7-200/300/400/1200/1500 全系列。

```python
import snap7

class S7Driver(DriverPlugin):
    async def read_points(self, device_id, points):
        client = snap7.client.Client()
        client.connect(ip, rack, slot)
        # DB块读取
        data = client.db_read(db_number, start, size)
        # 按偏移解析
        values = {}
        for point in points:
            values[point.name] = self._parse_s7_data(data, point.offset, point.data_type)
        return values
```

**踩坑经验**：
- snap7 底层是 C 库，Windows 上需要把 `snap7.dll` 放到 PATH 中。Docker 部署时用 `apt install libsnap7-dev`
- Rack/Slot 参数：S7-1200/1500 一般是 `rack=0, slot=1`，S7-300 是 `rack=0, slot=2`
- S7 数据类型解析是最大的坑：BOOL 是 1 位，BYTE 是 8 位，REAL 是 32 位浮点，偏移量必须精确到位

### 第二梯队：日系 PLC 三巨头

#### 4. 三菱 MC — pymcprotocol

```python
from pymcprotocol import Type3E

class McDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        mc = Type3E(ip, port)
        mc.setaccessopt(commtype='binary')
        # 批量读取：D100-D109
        values = mc.batchread_wordunits(headdevice="D100", size=10)
        return values
```

**注意**：三菱 Q/L 系列用 Type3E 帧，FX 系列用 Type4E 帧，协议不同。

#### 5. 欧姆龙 FINS — pylogix（非官方）

欧姆龙 FINS 协议的 Python 实现较少，EdgeLiteGateway 使用自研的 FINS 帧封装：

```python
class OmronFinsDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        # FINS 命令帧：ICF+RSV+GCT+DNA+DA1+DA2+SNA+SA1+SA2+SID
        frame = self._build_fins_frame(command_area_read, area_code, start, count)
        response = await self._transport.send_receive(frame)
        return self._parse_fins_response(response, points)
```

**踩坑经验**：FINS 的内存区域编码（CIO/WR/HR/DM）各不相同，DM 区是 `0x82`，CIO 区是 `0xB0`。

#### 6. Allen Bradley — pylogix

AB PLC 在北美市场占有率极高：

```python
from pylogix import PLC

class AbDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        with PLC() as comm:
            comm.IPAddress = ip
            comm.ProcessorSlot = slot  # ControlLogix 通常 slot=0
            response = comm.Read(tag_name)
            return {tag_name: response.Value}
```

**注意**：AB 的 CIP 协议支持批量读取，但 tag 名必须连续排列才有效。

### 第三梯队：数控与机器人

#### 7. FANUC CNC — pyfanuc

发那科数控系统的数据采集，主要用于机床监控：

```python
class FanucCncDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        # FANUC FOCAS2 库封装
        handle = fanuc.open(ip, port)
        spindle_speed = fanuc.read_spindle_speed(handle)
        feed_rate = fanuc.read_feed_rate(handle)
        return {"spindle_speed": spindle_speed, "feed_rate": feed_rate}
```

#### 8-9. KUKA EKRL / ABB RWS

工业机器人通信：
- **KUKA**：通过 EKRL (Ethernet KRL) 协议，XML 格式报文
- **ABB**：通过 RWS (Robot Web Services) REST API，JSON 格式

```python
# ABB 机器人 - REST API 风格
class AbbRobotDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://{ip}/rw/rapid/symbol/data/RAPID/{module}/{variable}")
            return self._parse_rws_response(resp.json())
```

### 第四梯队：电力与能源

#### 10. DL/T 645 — 自研

中国电力行业标准，用于智能电表数据采集：

```python
class Dlt645Driver(DriverPlugin):
    async def read_points(self, device_id, points):
        # DL/T 645 帧格式：68H + 地址 + 68H + 控制 + 数据长度 + 数据 + CS + 16H
        frame = self._build_645_frame(address, command, data)
        response = await self._serial.write_read(frame)
        # 数据域加33H解密
        return self._decrypt_645_data(response, points)
```

**关键点**：DL/T 645-2007 的数据域每个字节 +0x33 加密，这是很多人踩的坑。

#### 11. IEC 104 — 自研

电力远动协议，用于变电站自动化：

```python
class Iec104Driver(DriverPlugin):
    async def start(self, config):
        # IEC 104 使用 APDU 帧，支持平衡/非平衡传输
        self._connection = Iec104Connection(
            ip=config["host"],
            port=config.get("port", 2404),
            asdu_address=config.get("asdu_address", 1),
            cot_size=config.get("cot_size", 2),
        )
        await self._connection.connect()
```

#### 12. BACnet — bacpypes

楼宇自控协议，用于 HVAC、照明、门禁：

```python
from bacpypes.object import AnalogInputObject
from bacpypes.primitivedata import Real

class BacnetDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        # BACnet ReadPropertyMultiple
        request = ReadPropertyMultipleRequest(
            objectIdentifier=(objectType, instance),
            propertyIdentifier=property_id,
        )
        response = await self._bbmd.request(request)
        return self._parse_bacnet_response(response)
```

### 第五梯队：IoT 与 Web

#### 13. MQTT Client — aiomqtt 2.1+

```python
class MqttClientDriver(DriverPlugin):
    async def start(self, config):
        async with aiomqtt.Client(broker, port) as client:
            await client.subscribe(topic)
            async for message in client.messages:
                data = json.loads(message.payload)
                await self._on_data_callback(data)
```

**注意**：这是推送型协议，不用轮询，通过 `on_data` 回调上报数据。

#### 14. HTTP Webhook — FastAPI 内置

```python
class WebhookDriver(DriverPlugin):
    async def start(self, config):
        # 设备主动 POST 数据到网关
        self._endpoint = f"/webhook/{self.plugin_name}"
        # FastAPI 路由自动注册
```

#### 15. Sparkplug B — 自研

Eclipse Tahu 规范的 MQTT 子协议，为工业 IoT 场景定义了标准主题命名和数据模型。

### 第六梯队：特殊场景

#### 16. 串口设备 — pyserial

RS232/RS485 串口通信，支持上层协议嵌套（如 Modbus RTU over Serial）。

#### 17. 数据库接入 — SQLAlchemy

把 SQL 查询结果作为测点值，支持 MySQL/PostgreSQL/SQLite/MSSQL：

```python
class DatabaseSourceDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        async with self._engine.connect() as conn:
            for point in points:
                result = await conn.execute(text(point.sql_query))
                values[point.name] = result.scalar()
        return values
```

#### 18. 扫码枪 — pyserial

USB 串口扫码枪，自动帧解析（按 `\r` 或 `\n` 分帧）。

#### 19. 托利多称重 — 自研

支持 TCP/Serial/MT-SICS 三种通信方式。

#### 20. ONVIF — onvif-zeep

视频设备发现和 RTSP 流获取。

#### 21. OPC DA — OpenOPC-Python3x

经典 OPC 协议，仅 Windows 平台可用（依赖 COM 接口）。

#### 22. 模拟器 — 内置

```python
class SimulatorDriver(DriverPlugin):
    async def read_points(self, device_id, points):
        values = {}
        for point in points:
            if point.mode == "sine":
                values[point.name] = point.amplitude * math.sin(time.time() * point.frequency)
            elif point.mode == "random_walk":
                values[point.name] = self._last_values[point.name] + random.gauss(0, point.sigma)
            elif point.mode == "fixed":
                values[point.name] = point.value
        return values
```

## 驱动对比总表

| 协议 | Python 库 | 通信方式 | 轮询/推送 | 典型延迟 | 踩坑难度 |
|------|----------|---------|---------|---------|---------|
| Modbus TCP | pymodbus | TCP | 轮询 | 10-50ms | ⭐⭐ |
| Modbus RTU | pymodbus | Serial | 轮询 | 50-200ms | ⭐⭐⭐ |
| OPC UA | asyncua | TCP | 轮询/订阅 | 20-100ms | ⭐⭐⭐⭐ |
| S7 | snap7 | TCP | 轮询 | 10-30ms | ⭐⭐⭐ |
| MC | pymcprotocol | TCP | 轮询 | 10-50ms | ⭐⭐ |
| FINS | 自研 | TCP/UDP | 轮询 | 20-80ms | ⭐⭐⭐⭐ |
| AB | pylogix | TCP (CIP) | 轮询 | 30-100ms | ⭐⭐⭐ |
| FANUC | pyfanuc | TCP (FOCAS2) | 轮询 | 50-200ms | ⭐⭐⭐ |
| KUKA | 自研 | TCP (EKRL) | 轮询 | 50-200ms | ⭐⭐⭐⭐ |
| ABB | httpx | HTTP (RWS) | 轮询 | 100-500ms | ⭐⭐ |
| DL/T 645 | 自研 | Serial | 轮询 | 100-500ms | ⭐⭐⭐⭐ |
| IEC 104 | 自研 | TCP | 推送 | 10-50ms | ⭐⭐⭐⭐⭐ |
| BACnet | bacpypes | UDP | 轮询 | 50-200ms | ⭐⭐⭐⭐ |
| MQTT | aiomqtt | TCP | 推送 | 5-20ms | ⭐ |
| HTTP | httpx | HTTP | 推送 | 50-200ms | ⭐ |
| Sparkplug B | 自研 | MQTT | 推送 | 10-50ms | ⭐⭐⭐ |
| 串口 | pyserial | Serial | 轮询 | 50-500ms | ⭐⭐ |
| 数据库 | SQLAlchemy | TCP | 轮询 | 100-1000ms | ⭐⭐ |
| 扫码枪 | pyserial | Serial | 推送 | 10-50ms | ⭐ |
| 称重 | 自研 | TCP/Serial | 轮询 | 50-200ms | ⭐⭐⭐ |
| ONVIF | onvif-zeep | HTTP/SOAP | 轮询 | 100-500ms | ⭐⭐⭐ |
| OPC DA | OpenOPC | COM (Windows) | 轮询 | 50-200ms | ⭐⭐⭐⭐⭐ |
| 模拟器 | 内置 | 内存 | 轮询 | <1ms | ⭐ |

## 自定义驱动开发：30 分钟实战

假设你有一个私有协议，帧格式是 `0xAA + 长度 + 命令 + 数据 + CRC16`：

```python
# custom_drivers/my_protocol.py
from edgelite.drivers.base import DriverPlugin

class MyProtocolDriver(DriverPlugin):
    plugin_name = "my_protocol"
    plugin_version = "1.0.0"
    supported_protocols = ["my_protocol"]

    async def start(self, config):
        import asyncio
        self._reader, self._writer = await asyncio.open_connection(
            config["host"], config.get("port", 9000)
        )

    async def stop(self):
        if self._writer:
            self._writer.close()

    async def read_points(self, device_id, points):
        results = {}
        for point in points:
            frame = self._build_frame(command=0x01, data=point.encode())
            self._writer.write(frame)
            await self._writer.drain()
            response = await self._reader.read(256)
            results[point] = self._parse_response(response)
        return results

    async def write_point(self, device_id, point, value):
        frame = self._build_frame(command=0x02, data=self._encode_value(point, value))
        self._writer.write(frame)
        await self._writer.drain()
        return True
```

配置 `config.yaml`：

```yaml
drivers:
  custom_dir: /path/to/custom_drivers
```

重启服务，驱动自动发现并注册。**零编译，零配置，30 分钟上线。**

## 开源地址

- GitHub: https://github.com/suoten/EdgeLiteGateway
- Gitee: https://gitee.com/suoten/EdgeLiteGateway

**如果你在项目中遇到了协议对接的难题，欢迎提 Issue，我们持续扩展驱动库。**
