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
│  JWT Token │ RBAC (3角色21权限) │ bcrypt 密码哈希            │
└─────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. EventBus（事件总线）

基于 `asyncio.Queue` 的进程内异步事件总线，实现模块间完全解耦。

**队列策略**：每个订阅者独立队列，最大 10000 条（`MAX_QUEUE_SIZE`），队列满时丢弃最旧事件。

**事件类型**：

| 事件 | 字段 | 说明 |
|------|------|------|
| `PointUpdateEvent` | device_id, point_name, value, quality, timestamp | 测点值更新 |
| `DeviceStatusEvent` | device_id, old_status, new_status, timestamp | 设备上下线 |
| `AlarmEvent` | alarm_id, rule_id, device_id, severity, action, trigger_value, timestamp | 告警触发/恢复 |

**事件处理链**：

```
PointUpdateEvent → RuleEvaluator（规则评估）
                  → WebSocket realtime 频道（前端实时数据）
                  → MqttForwarder（北向转发）

AlarmEvent       → NotifyService（钉钉/邮件/企微/Webhook 通知）
                  → WebSocket alarm 频道（前端告警弹窗）
                  → MqttForwarder（北向转发）

DeviceStatusEvent → WebSocket device 频道（前端设备状态）
                   → MqttForwarder（北向转发）
```

### 2. CollectScheduler（采集调度器）

为每个设备创建独立的 `asyncio.Task` 采集协程：

- 支持可配置采集间隔（秒）
- 超时自动处理（避免阻塞事件循环）
- 批量 InfluxDB 写入（减少网络开销）
- 断网时自动缓存到 SQLite 队列

### 3. RuleEvaluator（规则评估器）

订阅 `PointUpdateEvent`，评估规则条件：

- **条件操作符**：`>`, `>=`, `<`, `<=`, `==`
- **逻辑组合**：AND（所有条件满足）/ OR（任一条件满足）
- **持续时间窗口**：`duration > 0` 时，条件需持续满足 N 秒才触发
- **告警收敛**：同一规则不重复触发 firing
- **自动恢复**：条件不满足时自动将告警状态改为 recovered
- **告警严重级别**：`critical`（严重）、`warning`（警告）、`info`（信息）
- **告警状态流转**：`firing` → `acknowledged` → `recovered`

### 4. DriverRegistry（驱动注册表）

内置驱动在 `registry.py` 中显式注册，启动时自动实例化。

**社区版驱动**：

| 驱动 | 协议 | 说明 |
|------|------|------|
| ModbusTCPDriver | modbus_tcp | Modbus TCP，支持多数据类型、自动重连（指数退避）、设备发现 |
| OpcUaDriver | opcua | OPC UA 客户端 |
| MqttClientDriver | mqtt | MQTT 客户端，订阅主题 |
| HttpWebhookDriver | http | HTTP Webhook，设备主动推送 |
| SimulatorDriver | simulator | 模拟器，4种模式：fixed/sine/random_walk/random |

**Pro 版驱动**（代码存在但需 Pro 版依赖）：

| 驱动 | 协议 | 依赖 |
|------|------|------|
| S7Driver | s7 | python-snap7 |
| MCDriver | mc | pymcprotocol |
| FinsDriver | fins | pyfins |
| AllenBradleyDriver | allen_bradley | pylogix |
| OpcDaDriver | opc_da | OpenOPC-Python3 |

### 5. DriverPlugin 接口

所有驱动继承 `DriverPlugin` 抽象基类：

```python
class DriverPlugin(ABC):
    plugin_name: str           # 驱动标识
    plugin_version: str        # 版本号
    supported_protocols: list  # 支持的协议列表

    async def start(self, config: dict) -> None
        """启动驱动，config 为设备配置"""

    async def stop(self) -> None
        """停止驱动"""

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]
        """读取测点值，返回 {point_name: value}"""

    async def write_point(self, device_id: str, point: str, value: Any) -> bool
        """写入测点值"""

    async def discover_devices(self, config: dict) -> list[dict]
        """发现设备（可选实现），返回设备列表"""

    def on_data(self, callback: Callable) -> None
        """注册数据回调（可选，用于推送型协议如 MQTT）"""

    @property
    def is_running(self) -> bool
        """驱动是否运行中"""
```

### 6. Storage（存储层）

| 存储 | 用途 | 实现 |
|------|------|------|
| SQLite | 配置数据（设备/规则/告警/用户/审计日志） | aiosqlite + WAL 模式 + 外键约束 |
| InfluxDB | 时序数据（测点值） | influxdb-client[async]，同步 API 通过 `asyncio.to_thread` 调用 |
| CacheManager | 断网缓存队列 | SQLite 队列，最大 100000 条，满时丢弃最旧 10% |

**InfluxDB 数据模型**：

| 组件 | 值 | 说明 |
|------|-----|------|
| measurement | `device_points` | 固定值 |
| tags | `device_id`, `point_name`, `quality` | 索引字段 |
| field | `value` (float) | 实际数据值 |
| timestamp | 采集时间 | 纳秒精度 |

### 7. Security（安全层）

- **JWT**：access_token (默认30min) + refresh_token (默认7d)，算法 HS256/HS384/HS512
- **RBAC**：3 角色 × 21 权限的权限矩阵（详见 USER_GUIDE.md）
- **密码**：bcrypt 哈希（12 rounds）

### 8. NotifyService（通知服务）

支持 4 种告警通知渠道：

| 渠道 | 配置 | 说明 |
|------|------|------|
| 钉钉 | webhook_url + secret | 加签安全模式 |
| 邮件 | smtp_host/port/user/password | SMTP 发送 |
| 企业微信 | webhook_url | 群机器人 Webhook |
| 自定义 Webhook | url + headers | 通用 HTTP 回调 |

### 9. PlatformHandler（北向平台对接）

```python
class PlatformHandler(ABC):
    async def connect(self, config: dict) -> None
        """连接平台"""

    async def disconnect(self) -> None
        """断开连接"""

    async def publish_telemetry(self, device_id: str, data: dict) -> None
        """发布遥测数据"""

    async def publish_attributes(self, device_id: str, attrs: dict) -> None
        """发布设备属性"""

    async def on_rpc_request(self, callback: Callable) -> None
        """注册 RPC 请求回调"""

    async def publish_device_status(self, device_id: str, status: str) -> None
        """发布设备状态"""
```

内置实现：IoTSharp、ThingsBoard

## 数据库表结构

### devices 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| device_id | TEXT | PRIMARY KEY | 设备唯一标识 |
| name | TEXT | NOT NULL | 设备名称 |
| protocol | TEXT | NOT NULL, CHECK | 协议类型 |
| status | TEXT | NOT NULL, DEFAULT 'offline', CHECK | 设备状态 |
| config | TEXT | NOT NULL | 设备配置（JSON） |
| points | TEXT | NOT NULL | 测点定义（JSON） |
| collect_interval | INTEGER | NOT NULL, DEFAULT 5 | 采集间隔（秒） |
| created_at | TEXT | NOT NULL, DEFAULT now | 创建时间 |
| updated_at | TEXT | NOT NULL, DEFAULT now | 更新时间 |

### rules 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| rule_id | TEXT | PRIMARY KEY | 规则唯一标识 |
| name | TEXT | NOT NULL | 规则名称 |
| device_id | TEXT | FK → devices | 关联设备 |
| conditions | TEXT | NOT NULL | 条件列表（JSON） |
| logic | TEXT | NOT NULL, DEFAULT 'AND', CHECK | 逻辑组合 |
| duration | INTEGER | NOT NULL, DEFAULT 0 | 持续时间窗口（秒） |
| severity | TEXT | NOT NULL, CHECK | 严重级别 |
| enabled | INTEGER | NOT NULL, DEFAULT 1 | 是否启用 |
| notify_channels | TEXT | NOT NULL | 通知渠道（JSON） |
| created_at | TEXT | NOT NULL, DEFAULT now | 创建时间 |

### alarms 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| alarm_id | TEXT | PRIMARY KEY | 告警唯一标识 |
| rule_id | TEXT | NOT NULL | 关联规则 |
| device_id | TEXT | NOT NULL | 关联设备 |
| severity | TEXT | NOT NULL | 严重级别 |
| status | TEXT | NOT NULL, DEFAULT 'firing', CHECK | 告警状态 |
| trigger_value | TEXT | NOT NULL | 触发值（JSON） |
| trigger_count | INTEGER | NOT NULL, DEFAULT 1 | 触发次数 |
| fired_at | TEXT | NOT NULL, DEFAULT now | 触发时间 |
| acknowledged_at | TEXT | | 确认时间 |
| acknowledged_by | TEXT | | 确认人 |
| recovered_at | TEXT | | 恢复时间 |

索引：`idx_alarms_status`, `idx_alarms_device`

### users 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| user_id | TEXT | PRIMARY KEY | 用户唯一标识 |
| username | TEXT | NOT NULL, UNIQUE | 用户名 |
| password | TEXT | NOT NULL | bcrypt 密码哈希 |
| role | TEXT | NOT NULL, CHECK | 角色 |
| enabled | INTEGER | NOT NULL, DEFAULT 1 | 是否启用 |
| created_at | TEXT | NOT NULL, DEFAULT now | 创建时间 |

### audit_logs 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK AUTOINCREMENT | 自增主键 |
| user_id | TEXT | NOT NULL | 操作用户 |
| action | TEXT | NOT NULL | 操作类型 |
| resource | TEXT | NOT NULL | 资源类型 |
| resource_id | TEXT | | 资源 ID |
| detail | TEXT | | 操作详情 |
| result | TEXT | NOT NULL | 操作结果 |
| created_at | TEXT | NOT NULL, DEFAULT now | 操作时间 |

索引：`idx_audit_time`

### cache_queue 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PK AUTOINCREMENT | 自增主键 |
| measurement | TEXT | NOT NULL | InfluxDB measurement |
| tags | TEXT | NOT NULL | 标签（JSON） |
| fields | TEXT | NOT NULL | 字段（JSON） |
| timestamp | TEXT | NOT NULL | 时间戳 |
| retry_count | INTEGER | NOT NULL, DEFAULT 0 | 重试次数 |
| created_at | TEXT | NOT NULL, DEFAULT now | 创建时间 |

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

    async def stop(self) -> None:
        """关闭连接"""

    async def read_points(self, device_id: str, points: list[str]) -> dict:
        """读取测点值"""

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""
```

然后在 `registry.py` 中注册驱动。

### 添加北向平台对接

继承 `PlatformHandler` 基类，实现 `connect()`, `disconnect()`, `publish_telemetry()`, `publish_attributes()`, `on_rpc_request()`, `publish_device_status()` 接口。

## 性能优化

### Cython 加速

可选编译 `modbus_mapper.pyx` 和 `rule_compare.pyx` 为 C 扩展：

```bash
pip install cython
python setup.py build_ext --inplace
```

- `modbus_mapper.pyx`：Modbus 寄存器映射加速
- `rule_compare.pyx`：规则条件比较加速

未编译时自动回退到纯 Python 实现（`_cython/*_py.py`），功能完全相同。

### InfluxDB 批量写入

- `batch_size`：单次写入数据点数（默认 1000）
- `flush_interval`：刷新间隔毫秒（默认 5000）

### SQLite WAL 模式

已默认启用 WAL 模式（`PRAGMA journal_mode=WAL`），支持并发读写。外键约束已启用（`PRAGMA foreign_keys=ON`）。
