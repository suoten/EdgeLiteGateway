# 为什么我用 Python 重写了一个边缘网关，而不是继续用 C

> 本文从架构设计角度剖析 EdgeLiteGateway 的技术选型与实现，适合对 IoT 网关、边缘计算、Python 异步编程感兴趣的开发者。

## 背景：传统网关的痛点

工业物联网网关领域，C/Java 是绝对主流。Kepware、Matrikon、IGSS——这些名字统治了 OPC 领域二十年。但它们有一个共同的问题：**开发一个新协议驱动，你需要 C 指针级别的功力，或者 Java 企业级的耐心。**

我在实际项目中遇到过一个典型场景：客户有一批托利多称重仪表，协议文档只有 3 页纸，但现有网关不支持。用 C 写驱动？光搭环境就一天。用 Java？Spring Boot 启动就要 10 秒，跑在工控机上内存直接拉满。

于是我开始思考：**能不能用 Python 做一个真正能跑在产线上的边缘网关？**

## 核心挑战：Python 能扛住工业级并发吗？

这是所有人问的第一个问题。答案是：**用 asyncio 可以。**

### 每设备一协程的调度模型

传统网关用线程池轮询设备，1000 个设备 = 1000 个线程，内存直接爆炸。EdgeLiteGateway 的做法完全不同：

```python
async def _collect_loop(self, device_id, driver, points, interval):
    while True:
        values = await asyncio.wait_for(
            driver.read_points(device_id, point_names),
            timeout=5.0,
        )
        # 预处理 -> 发布事件 -> 批量写入
        await asyncio.sleep(interval)
```

每个设备一个 `asyncio.Task`，**单线程调度，零线程切换开销**。1000 个设备 = 1000 个协程，内存占用不到 200MB。

关键设计点：
- `asyncio.wait_for` 硬超时保护，单个设备卡死不影响全局
- 超时时发布 `quality="timeout"` 事件，下游规则引擎和前端都能感知
- `task.cancel()` + `CancelledError` 实现优雅停止

### 事件总线：asyncio.Queue 的正确用法

整个系统的"神经中枢"是一个基于 `asyncio.Queue` 的事件总线：

```python
class EventBus:
    def __init__(self, max_queue_size=10000):
        self._subscribers: dict[str, asyncio.Queue] = {}  # 频道式订阅
        self._handlers: dict[str, list[Callable]] = {}    # 回调式订阅
```

**双通道设计**：
- **Queue 通道**：每个消费者独立队列，互不阻塞。规则引擎、WebSocket 推送、MQTT 转发各自消费，慢消费者不影响快消费者
- **Handler 通道**：轻量级即时回调，适合通知发送这种 fire-and-forget 场景

**背压策略——尾部丢弃**：

```python
async def publish(self, event):
    for name, queue in self._subscribers.items():
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            queue.get_nowait()  # 丢弃最旧的
            queue.put_nowait(event)  # 保证最新事件进入
```

有界队列 + 尾部丢弃，流量洪峰下系统不会 OOM，且保证最新数据优先。这在工业场景中很重要——你永远想知道传感器**现在**的值，而不是 5 秒前的。

## 数据流：从驱动到前端的全链路

一条数据从 PLC 寄存器到前端图表，经历以下路径：

```
PLC → Driver.read_points() → Preprocessor(死区/滤波) → EventBus.publish()
  → InfluxDB批量写入(1000条/批)
  → WebSocket频道广播 → 前端ECharts实时刷新
  → RuleEvaluator(条件评估) → AlarmEvent → 钉钉/企微/邮件通知
```

### 死区过滤：最被低估的优化

工业场景中，传感器 90% 的采样值是相同的（温度变化 0.01°C）。如果全写入 InfluxDB，存储和查询都会爆炸。

EdgeLiteGateway 的预处理管道：

```python
def _apply_deadband(self, point_key, value, config):
    last = self._last_values.get(point_key)
    if abs(value - last) < deadband:
        return False  # 死区内，不上报
    return True
```

实测效果：一个 200 测点的温控系统，原始采样 10次/秒 = 2000 TPS，死区过滤后降到 50 TPS，**写入量减少 97.5%**。

### InfluxDB 不可用时的降级策略

边缘场景网络不稳定是常态。InfluxDB 断连时：

```python
success = await self._influx.write_points_batch(records)
if not success:
    for rec in records:
        await self._cache.add_to_cache(...)  # 降级到 SQLite
```

SQLite 缓存队列上限 10 万条，满了丢弃最旧 10%。连续 3 次写入失败自动熔断，不再尝试 InfluxDB，避免雪崩。网络恢复后缓存数据自动回传。

## 规则引擎：从阈值比较到 Python 沙箱

### 三层规则能力

1. **阈值规则**：`温度 > 80`，Cython 加速，C 层执行
2. **多条件逻辑**：`温度 > 80 AND 湿度 < 30 AND 持续 60秒`
3. **脚本规则**：`result = point_values["temp"] * 1.5 > point_values["threshold"]`，RestrictedPython 沙箱执行

### 持续时间窗口——避免瞬时毛刺告警

```python
if matched:
    if duration > 0:
        first_time = self._duration_tracker.get(tracker_key)
        if first_time is None:
            self._duration_tracker[tracker_key] = now  # 首次满足
        elif (now - first_time).total_seconds() >= duration:
            await self._fire_alarm(...)  # 持续窗口到期才告警
    else:
        await self._fire_alarm(...)  # 立即触发
```

### 告警收敛——同一故障不重复告警

```python
existing = await self._alarm_repo.get_firing_by_rule_device(rule_id, device_id)
if existing:
    await self._alarm_repo.update_trigger_count(...)  # 只更新触发次数
    return  # 不创建新告警
```

这在实际运维中极其重要——一个温度传感器超限，你不想收到 100 条告警，只想知道"第 1 次触发时间"和"已触发 100 次"。

## 22 种协议驱动：30 分钟开发一个新驱动

这是 Python 生态最大的优势。继承 `DriverPlugin` 基类，实现 4 个方法：

```python
class MyCustomDriver(DriverPlugin):
    plugin_name = "my_protocol"
    supported_protocols = ["my_protocol"]

    async def start(self, config): ...
    async def stop(self): ...
    async def read_points(self, device_id, points): ...
    async def write_point(self, device_id, point, value): ...
```

放到自定义驱动目录，重启即生效。不需要编译，不需要 JNI 桥接，不需要跨语言序列化。

### 内置驱动一览

| 协议 | Python 库 | 典型场景 |
|------|----------|---------|
| Modbus TCP/RTU | pymodbus | PLC、仪表、变频器 |
| OPC UA | asyncua | SCADA/MES 对接 |
| 西门子 S7 | snap7 | S7-1200/1500 |
| 三菱 MC | pymcprotocol | Q/L/FX 系列 |
| 欧姆龙 FINS | pylogix | CJ/CP/NJ 系列 |
| FANUC CNC | pyfanuc | 数控机床 |
| DL/T 645 | 自研 | 智能电表 |
| IEC 104 | 自研 | 电力远动 |
| BACnet | bacpypes | 楼宇自控 |

## 安全：不是事后补丁，是设计约束

- **JWT + RBAC**：admin/operator/viewer 三角色，Token 刷新与吊销
- **RestrictedPython 沙箱**：脚本规则只能访问白名单函数，不能 `import os` 或 `open()`
- **路径白名单**：自定义驱动只能从配置目录加载，防止任意代码执行
- **Flux 注入防护**：InfluxDB 查询参数全部转义
- **密码策略**：bcrypt 哈希 + 8位最小长度 + 字母数字强制

## 性能数据

在树莓派 4B (4GB) 上的实测数据：

| 指标 | 数值 |
|------|------|
| 并发设备数 | 500+ |
| 内存占用 | < 300MB |
| 采集延迟 (Modbus TCP) | < 50ms |
| 规则评估延迟 | < 5ms |
| WebSocket 推送延迟 | < 10ms |
| InfluxDB 批量写入吞吐 | 5000 TPS |

## 开源地址

- GitHub: https://github.com/suoten/EdgeLiteGateway
- Gitee: https://gitee.com/suoten/EdgeLiteGateway

**GPL-3.0 开源，欢迎 Star 和 PR。**

---

*作者注：如果你正在评估边缘网关方案，或者对 Python 异步架构在工业场景的应用有疑问，欢迎在 GitHub Issues 交流。*
