# EdgeLiteGateway 架构设计

## 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                    北向接口层 (Northbound)                    │
│   REST API  │  WebSocket  │  MQTT Forwarder  │  Platform    │
├─────────────────────────────────────────────────────────────┤
│                    业务服务层 (Services)                      │
│  DeviceService │ RuleService │ AlarmService │ DataService    │
│  VideoService  │ NotifyService │ SystemService               │
├─────────────────────────────────────────────────────────────┤
│                    核心引擎层 (Engine)                        │
│  EventBus │ CollectScheduler │ RuleEvaluator │ Lifecycle     │
│  MqttServer │ OpcUaServer │ ModbusSlave                    │
├─────────────────────────────────────────────────────────────┤
│                    驱动接入层 (Drivers)                       │
│  ModbusTCP │ OPC-UA │ MQTT │ HTTP │ Simulator │ Video       │
├─────────────────────────────────────────────────────────────┤
│                    存储层 (Storage)                           │
│  SQLite (配置) │ InfluxDB (时序) │ CacheManager (断网缓存)   │
├─────────────────────────────────────────────────────────────┤
│                    安全层 (Security)                          │
│  JWT Token │ RBAC (3角色17权限) │ bcrypt 密码哈希            │
└─────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. EventBus（事件总线）

基于 `asyncio.Queue` 的进程内异步事件总线，实现模块间完全解耦。

**事件类型**：
- `PointUpdateEvent` - 测点数据更新
- `DeviceStatusEvent` - 设备上下线状态变更
- `AlarmEvent` - 告警触发/恢复

**数据流**：
```
驱动采集 → EventBus.publish(PointUpdateEvent)
         → RuleEvaluator 订阅并评估
         → 触发 AlarmEvent
         → NotifyService 推送通知
         → WebSocket 广播到前端
         → MqttForwarder 转发到云端
```

### 2. CollectScheduler（采集调度器）

为每个设备创建独立的 `asyncio.Task` 采集协程：

- 支持可配置采集间隔
- 超时自动处理（避免阻塞事件循环）
- 批量 InfluxDB 写入（减少网络开销）
- 断网时自动缓存到 SQLite 队列

### 3. RuleEvaluator（规则评估器）

订阅 `PointUpdateEvent`，评估规则条件：

- 支持 AND/OR 逻辑组合
- 支持持续时间窗口（条件持续 N 秒才触发）
- 告警收敛（同一规则不重复触发）
- 自动恢复（条件不满足时自动恢复告警）

### 4. DriverRegistry（驱动注册表）

自动发现并注册 `src/edgelite/drivers/` 目录下所有驱动：

- 每个驱动继承 `DriverPlugin` 基类
- 实现 `start()`, `stop()`, `read_points()`, `write_point()` 接口
- 支持 `discover_devices()` 设备发现

### 5. Storage（存储层）

| 存储 | 用途 | 实现 |
|------|------|------|
| SQLite | 配置数据（设备/规则/告警/用户） | aiosqlite + WAL 模式 |
| InfluxDB | 时序数据（测点值） | influxdb-client[async] |
| CacheManager | 断网缓存队列 | SQLite 队列，最大 10 万条 |

**降级策略**：InfluxDB 不可用时，数据自动缓存到 SQLite 队列，联网后批量续传。

### 6. Security（安全层）

- **JWT**：access token (30min) + refresh token (7d)
- **RBAC**：3 角色 × 17 权限的权限矩阵
  - `admin`：全部权限
  - `operator`：设备读取、规则切换、告警确认、数据查询
  - `viewer`：只读权限
- **密码**：bcrypt 哈希（12 rounds）

## 数据流示例

### 场景：Modbus 设备温度超限告警

```
[Modbus TCP 驱动]
    ↓ 每隔5秒采集
[CollectScheduler]
    ↓ 读取寄存器值
[EventBus.publish(PointUpdateEvent)]
    ↓
[RuleEvaluator] → 评估规则: temperature > 80 持续 10s
    ↓ 条件满足
[EventBus.publish(AlarmEvent)]
    ↓
    ├── [NotifyService] → 钉钉/邮件/企微/Webhook 通知
    ├── [WebSocket] → 前端实时告警弹窗
    ├── [MqttForwarder] → 转发到云端 MQTT Broker
    └── [AlarmRepo] → 写入 SQLite 告警记录
```

### 场景：HTTP Webhook 设备数据接入

```
[设备] → POST /api/v1/devices/{id}/push
    ↓
[DeviceService] → 解析数据
    ↓
[EventBus.publish(PointUpdateEvent)]
    ↓
[InfluxDB] → 写入时序数据
[RuleEvaluator] → 评估规则
[WebSocket] → 推送实时数据到前端
```

## 扩展性设计

### 添加新协议驱动

```python
# src/edgelite/drivers/my_driver.py
from edgelite.drivers.base import DriverPlugin

class MyDriver(DriverPlugin):
    plugin_name = "my_protocol"
    plugin_version = "1.0.0"
    supported_protocols = ["my_protocol"]

    async def start(self, config: dict) -> None:
        """初始化连接"""

    async def read_points(self, device_id: str, points: list[str]) -> dict:
        """读取测点值"""

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""
```

驱动放在 `src/edgelite/drivers/` 目录下即可自动发现注册。

### 添加北向平台对接

继承 `PlatformHandler` 基类，实现 `connect()` 和 `disconnect()` 接口。

## 性能优化

### Cython 加速

可选编译 `modbus_mapper.pyx` 和 `rule_compare.pyx` 为 C 扩展：

```bash
pip install cython
python setup.py build_ext --inplace
```

未编译时自动回退到纯 Python 实现（`_cython/*_py.py`），功能完全相同。

### InfluxDB 批量写入

- `batch_size`: 单次写入数据点数（默认 1000）
- `flush_interval`: 刷新间隔毫秒（默认 5000）

### SQLite WAL 模式

已默认启用 WAL 模式，支持并发读写。
