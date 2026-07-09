# EdgeLite V1.0 社区版 — 深度系统分析报告

> **分析范围**：后端引擎、驱动层、安全模块、API 路由、前端状态管理、部署架构  
> **分析方法**：源码逐文件审计 + 架构模式推演 + 边界条件分析  
> **分析日期**：2026-07-02  
> **注意**：本报告仅作分析，不修改任何源码

---

## 目录

- [一、架构级问题](#一架构级问题)
- [二、驱动层问题](#二驱动层问题)
- [三、引擎层问题](#三引擎层问题)
- [四、安全模块问题](#四安全模块问题)
- [五、API 路由与错误码问题](#五api-路由与错误码问题)
- [六、前端状态管理与安全问题](#六前端状态管理与安全问题)
- [七、存储与数据一致性问题](#七存储与数据一致性问题)
- [八、部署与运维问题](#八部署与运维问题)
- [九、问题严重度汇总矩阵](#九问题严重度汇总矩阵)

---

## 一、架构级问题

### ARCH-001：锁层级体系过于复杂，死锁风险持续存在 【严重】

**现象**：整个系统混合使用 `asyncio.Lock`、`threading.Lock`、`threading.RLock` 三种锁类型，仅 `DriverPlugin` 基类就持有 **9 把锁**：

| 锁名称 | 类型 | 保护对象 |
|--------|------|----------|
| `_stats_lock` | `RLock` | `_health_stats`, `_offline_since` |
| `_conn_state_lock` | `RLock` | `_connection_statuses` |
| `_circuit_lock` | `Lock` | `_circuit_states`, `_circuit_open_sinces` |
| `_executor_lock` | `asyncio.Lock` | `_executor`, `_executor_futures` |
| `_reconnect_lock` | `asyncio.Lock` | `_reconnect_state` |
| `_pool_lock` | `asyncio.Lock` | `_connection_pool` (ModbusTcpDriver) |
| `_lease_lock` | `asyncio.Lock` | `_leased_clients` (ModbusTcpDriver) |
| `_retry_lock` | `asyncio.Lock` | `_retry_count` (ModbusTcpDriver) |
| `_role_lock` | `asyncio.Lock` | `_current_user_role` (ModbusTcpDriver) |

**问题细节**：
- 代码中大量 `FIXED-P0` 注释记录了已修复的 ABBA 死锁，但这表明锁获取顺序在设计阶段未明确定义，后续修复均为被动式打补丁。
- `_record_read_failure` 是同步方法，内部通过 `loop.create_task()` 调度异步的 `_record_circuit_failure`，同时有同步回退路径。两条路径获取锁的顺序不同（异步路径：`_stats_lock` → `_circuit_lock`；同步回退路径也是 `_stats_lock` → `_circuit_lock`，但中间释放了 `_stats_lock`），在极端时序下仍可能产生竞态。
- `_evaluate_degradation` 在 `_stats_lock` 内读取数据后释放锁，然后通过 `loop.create_task` 调度 `_set_connection_state`（获取 `_conn_state_lock`），如果此时另一个协程持有 `_conn_state_lock` 并尝试获取 `_stats_lock`，则形成 ABBA。

**影响**：高并发场景下偶发死锁，导致采集任务或规则评估永久卡住。

**建议方向**：引入统一的锁层级文档（Lock Hierarchy Document），定义所有锁的全局获取顺序；考虑使用单一 `asyncio.Lock` 保护关联状态组，减少锁碎片。

---

### ARCH-002：同步/异步混合调用模式脆弱 【高】

**现象**：系统频繁在异步上下文中调用同步方法，或在同步方法中调度异步任务：

```python
# base.py: _record_read_failure (同步方法)
def _record_read_failure(self, device_id: str) -> None:
    with self._stats_lock:
        # ... 更新统计 ...
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._record_circuit_failure(device_id))
        # ...
    except RuntimeError:
        # 无事件循环时的同步回退路径
        with self._circuit_lock:
            # ...
```

**问题细节**：
- 双路径（异步调度 + 同步回退）需要维护两套等价逻辑，极易出现行为不一致。
- `loop.create_task` 创建的任务是 fire-and-forget 的，虽然添加了 `done_callback`，但如果回调本身抛异常，异常会被静默吞没。
- `_check_circuit_breaker` 使用 `asyncio.to_thread(_check_sync)` 来避免 `threading.Lock` 阻塞事件循环，但这引入了线程切换开销，且 `to_thread` 本身在高频调用时可能成为瓶颈。

**影响**：熔断器在无事件循环的上下文中行为可能与正常路径不一致，导致保护机制失效。

---

### ARCH-003：状态字典膨胀，缺乏统一生命周期管理 【高】

**现象**：`ModbusTcpDriver` 在 `__init__` 中初始化了 **30+ 个状态字典**：

```python
self._clients = {}
self._connection_pool = {}
self._stale_clients = {}
self._leased_clients = {}
self._device_pool_key = {}
self._device_configs = {}
self._device_points = {}
self._retry_count = {}
self._read_fail_tracker = OrderedDict()
self._circuit_open = set()
self._watchdog_fail_count = {}
self._device_watchdog_tasks = {}
self._reconnect_tasks = {}
self._single_point_cache = OrderedDict()
self._point_retry_tasks = {}
self._last_values = OrderedDict()
self._pool_backoff = {}
self._conn_state = {}
self._primary_fail_count = {}
self._active_host = {}
self._point_health = OrderedDict()
self._point_quality_history = {}
self._device_latency_history = {}
self._degrade_level = {}
self._frozen_count = {}
self._last_timestamp = {}
self._last_write_time = {}
self._device_point_keys = {}
self._write_audit_log = deque(maxlen=1000)
# ... 还有更多
```

**问题细节**：
- `stop()` 方法需要手动清理所有这些字典（约 25 行 `.clear()` 调用），任何遗漏都会导致重启后状态残留。
- `reset_health_stats` 方法需要跨 3 把锁（`_stats_lock`, `_circuit_lock`, `_conn_state_lock`）清理设备级状态，如果新增状态字典忘记在此方法中清理，设备移除后会产生内存泄漏。
- `_conn_state`（ModbusTcpDriver 自有）与基类的 `_connection_statuses` 语义重叠，存在状态不一致风险。

**影响**：设备频繁增删场景下内存泄漏；驱动重启后状态不一致导致行为异常。

---

## 二、驱动层问题

### DRV-001：Modbus TCP 连接池 TOCTOU 竞态窗口仍存在 【高】

**现象**：`add_device` 方法中两次获取 `_pool_lock`，中间执行连接创建：

```python
# 第一次获取锁
async with self._pool_lock:
    if pool_key in self._connection_pool:
        # 复用连接 ...
        return

# 锁外创建连接（可能耗时数秒）
client = AsyncModbusTcpClient(host=host, port=port, ...)
connected = await asyncio.wait_for(client.connect(), timeout=timeout)

# 第二次获取锁
async with self._pool_lock:
    if pool_key in self._connection_pool:
        # 另一个协程可能已添加同 pool_key 的连接
        existing_client, ref_count = self._connection_pool[pool_key]
        # ...
```

**问题细节**：
- 在两次锁之间，另一个协程可能已经为同一个 `host:port` 创建并添加了连接到池中。代码通过 "二次检查" 处理这种情况，但逻辑复杂且分支众多。
- 当 `connected=False` 且 `pool_key` 已存在时，代码设置 `reused_existing=True` 但不绑定 client 到设备，导致设备处于"已注册但无连接"的悬空状态，后续读取会失败但错误信息不明确。
- `remove_device` 中的引用计数递减逻辑有两条分支（`ref_count <= 1` 和 `ref_count > 1`），其中 `ref_count > 1` 分支内部又检查 `_stale_clients`，逻辑嵌套过深。

**影响**：多设备同时连接同一网关时，可能出现引用计数错误，导致连接泄漏或过早关闭。

---

### DRV-002：Stale Client 清理机制存在资源泄漏窗口 【中】

**现象**：当 client 正在被使用（在 `_leased_clients` 中）时，`remove_device` 不会立即关闭它，而是标记为 stale：

```python
if pooled_client not in self._leased_clients:
    pooled_client.close()
else:
    self._stale_clients[pooled_client] = time.monotonic()
```

**问题细节**：
- `_stale_client_cleanup_loop` 定期检查 stale clients，但 `_STALE_CLIENT_TIMEOUT` 为 300 秒（5分钟）。在这 5 分钟内，如果 lease 未释放，client 持续占用 TCP 连接。
- `_leased_clients` 的添加和移除逻辑分散在多个方法中（`_get_leased_client`, `_release_leased_client` 等），如果 `_release_leased_client` 因异常未执行，lease 永远不会释放。
- stale client 超过 5 分钟后"强制关闭"，但代码中仅调用 `client.close()`，没有从 `_leased_clients` 中移除，可能导致 use-after-close。

**影响**：长期运行后 TCP 连接数持续增长，最终达到系统限制。

---

### DRV-003：自定义驱动沙箱的全局 import 劫持有残留风险 【严重】

**现象**：`registry.py` 的 `_discover_custom_drivers` 方法通过替换 `builtins.__import__` 来限制自定义驱动的导入能力：

```python
builtins.__import__ = _restricted_import
importlib.import_module = _restricted_import_module
importlib.util.spec_from_file_location = _restricted_spec_from_file
# ...
try:
    spec.loader.exec_module(module)
finally:
    builtins.__import__ = _orig_import
    importlib.import_module = _orig_import_module
    # ...
```

**问题细节**：
- 如果 `exec_module` 抛出 `SystemExit` 或 `KeyboardInterrupt`（不被 `except Exception` 捕获），`finally` 块会执行，但如果 `finally` 块内部也抛异常（如恢复 `sys.modules` 时），则全局 `__import__` 永远不会被恢复。
- 在 `exec_module` 执行期间，所有其他协程的 `import` 语句也会被拦截（因为是全局替换），虽然时间窗口很短，但在高并发启动场景下可能导致其他模块导入失败。
- 挂起 `sys.modules` 的逻辑（`_SUSPENDED_MODULES`）会 pop 非白名单模块，如果自定义驱动在此期间触发了某个内部模块的延迟导入（如 `json.decoder`），可能导致 `ImportError`。

**影响**：自定义驱动加载失败可能影响整个应用的 import 系统；精心构造的驱动文件可能绕过沙箱。

---

### DRV-004：pymodbus 版本检测的懒初始化竞态 【中】

**现象**：`_SLAVE_KWARG_NAME` 是全局变量，通过懒初始化设置：

```python
_SLAVE_KWARG_NAME: str | None = None

def _slave_kwarg(slave_id: int) -> dict:
    global _SLAVE_KWARG_NAME
    if _SLAVE_KWARG_NAME is None:
        _SLAVE_KWARG_NAME = _detect_slave_kwarg_name()
    # ...
```

**问题细节**：
- 虽然 Python 的 GIL 保证了单个字节码操作的原子性，但 `_detect_slave_kwarg_name()` 内部有多个条件判断和属性访问，理论上可以在多线程环境下被多次调用（虽然结果相同，但浪费资源）。
- 更重要的是，这个检测发生在首次使用时而非导入时，如果 pymodbus 在运行时被升级（虽然不常见），检测结果可能不准确。

**影响**：低概率的初始化竞态，实际影响较小但设计不够健壮。

---

### DRV-005：线程池资源上限可能成为采集瓶颈 【高】

**现象**：`DriverPlugin` 基类的线程池默认只有 4 个 worker：

```python
self._executor_max_workers: int = 4
```

**问题细节**：
- 当多个设备使用同步库（如某些 OPC DA 实现）时，所有同步 I/O 操作都通过 `_run_in_executor` 提交到这个 4-worker 线程池。
- 如果 4 个操作都在等待 I/O（如网络超时 30 秒），后续所有设备的采集都会被阻塞。
- `_run_in_executor` 的默认超时是 10 秒，超时后会重建 executor，但重建期间其他正在执行的线程会被 `shutdown(wait=False, cancel_futures=True)` 取消，可能导致数据丢失。
- `_executor_futures_warn_threshold` 设为 64，但这是告警阈值而非硬限制，futures 集合可以继续增长。

**影响**：设备数量超过 4 个且使用同步驱动时，采集延迟显著增加。

---

### DRV-006：DriverHealthStats 的 effective_state 逻辑有矛盾 【中】

**现象**：`DriverHealthStats.effective_state` 属性的逻辑：

```python
@property
def effective_state(self) -> str:
    if self.consecutive_failures == 0 and self.read_error_rate < 0.1:
        return ConnectionState.CONNECTED.value
    if self.consecutive_failures >= 5:
        return ConnectionState.OFFLINE.value
    if self.connection_quality_score < 50:
        return ConnectionState.DEGRADED.value
    return ConnectionState.DEGRADED.value  # 兜底也是 DEGRADED
```

**问题细节**：
- 最后两个分支都返回 `DEGRADED`，`connection_quality_score < 50` 的检查是多余的。
- `is_healthy` 属性使用 `consecutive_failures < 5 and read_error_rate < 0.1`，而 `effective_state` 使用 `consecutive_failures == 0`，两者不一致：一个设备可以有 `consecutive_failures=3`（不健康但不是 OFFLINE），`effective_state` 返回 `DEGRADED`，但 `is_healthy` 返回 `False`。
- `health_score` 和 `connection_quality_score` 是两个独立的评分系统，前者基于 `consecutive_failures`、`read_error_rate`、`avg_latency` 和 `total_reconnects`，后者仅基于前两者，容易混淆。

**影响**：UI 和监控系统可能显示矛盾的健康状态信息。

---

## 三、引擎层问题

### ENG-001：规则评估器 Duration Tracker 的清理窗口可能丢失告警 【高】

**现象**：`_prune_duration_tracker` 使用 24 小时作为 cutoff：

```python
def _prune_duration_tracker(self) -> None:
    cutoff = datetime.now(UTC).timestamp() - 86400
    for (rule_id, device_id), first_match_time in list(self._duration_tracker.items()):
        if first_match_time.timestamp() < cutoff:
            keys_to_remove.append((rule_id, device_id))
```

**问题细节**：
- 如果规则的 `duration` 配置超过 86400 秒（24小时），tracker 会在条件满足 24 小时后被清理，导致规则永远不会触发。虽然 24 小时的 duration 在实际中不常见，但系统没有对此做校验。
- `max_entries` 基于 `active_rule_count * 2`，如果所有规则都在等待 duration，`active_rule_count` 等于总规则数，`max_entries` 等于 `总规则数 * 2`。但如果某些规则跨设备关联（一个规则关联多个设备），tracker 条目数可能超过此限制，导致正常等待的条目被清理。
- 清理操作不在 `_state_lock` 保护下执行（`_prune_duration_tracker` 是同步方法直接操作字典），可能与 `_evaluate_rule` 中的 `_duration_tracker` 访问产生竞态。

**影响**：长时间持续条件规则可能丢失告警；高并发评估时 tracker 可能被意外清理。

---

### ENG-002：规则评估超时后状态不一致 【高】

**现象**：`_evaluate` 方法使用 `asyncio.wait_for` 包装 `_evaluate_inner`：

```python
async def _evaluate(self, event: PointUpdateEvent) -> None:
    try:
        await asyncio.wait_for(self._evaluate_inner(event), timeout=5.0)
    except TimeoutError:
        logger.warning("Rule evaluation timed out after 5.0s ...")
```

**问题细节**：
- 超时后，`_evaluate_inner` 内部的 `_state_lock` 可能仍然被持有（如果超时发生在锁内 await 期间），导致后续所有评估被阻塞。
- 超时时，`_fire_alarm` 可能已经创建了数据库记录但未发布事件（如果超时发生在 `alarm_repo.create` 之后、`event_bus.publish` 之前），形成孤儿告警。
- 超时不会清理 `_duration_tracker`，如果超时发生在 `matched=True` 且 `duration > 0` 的分支中，tracker 可能处于不一致状态。
- `_evaluate_inner` 内部的 `_get_rules_for_point` 获取 `_state_lock`，如果超时发生在此处，锁会被 `asyncio.wait_for` 的取消机制释放（`async with` 会处理 `CancelledError`），但如果超时发生在 `_fire_alarm` 的 `async with self._state_lock` 内部，回滚逻辑可能不完整。

**影响**：评估超时后规则评估循环可能永久阻塞，或产生孤儿告警。

---

### ENG-003：事件总线去重缓存在高吞吐场景下可能误删 【中】

**现象**：EventBus 使用 `OrderedDict` 实现 event_id 去重，上限 10000：

```python
self._seen_event_ids: OrderedDict[str, None] = OrderedDict()
self._max_dedup_size = 10000

# publish 中
if event_id in self._seen_event_ids:
    return  # 去重
self._seen_event_ids[event_id] = None
if len(self._seen_event_ids) > self._max_dedup_size:
    self._seen_event_ids.popitem(last=False)  # FIFO 淘汰
```

**问题细节**：
- 当事件吞吐量超过 10000/秒（工业场景下多设备高频采集可能达到），最早的 event_id 会在 1 秒内被淘汰。如果同一逻辑事件因网络重试等原因在 1 秒后再次发布，去重会失效，导致重复处理。
- `AlarmEvent` 的 `event_id` 格式为 `alarm:{alarm_id}:{action}`，如果告警恢复后同一 alarm_id 再次触发（`firing` → `recovered` → `firing`），第三次 `firing` 的 event_id 与第一次相同，会被错误去重。
- 去重缓存在 `_subscribers_lock` 外部访问，虽然 `OrderedDict` 的基本操作是线程安全的（受 GIL 保护），但 `in` 检查和后续的 `[]=` 赋值不是原子的，两个协程可能同时通过 `in` 检查并各自添加。

**影响**：告警可能被错误去重而丢失；高吞吐场景下去重失效。

---

### ENG-004：采集调度器的并发门控迁移可能丢失采集周期 【中】

**现象**：`set_max_concurrent` 方法重建优先级信号量并唤醒旧等待者：

```python
# 重建信号量
old_semaphores = dict(self._priority_semaphores)
self._priority_semaphores.clear()
for p in DevicePriority:
    self._priority_semaphores[p] = _ConcurrencyGate(capacity)
# 唤醒旧信号量上的等待者
for p, old_gate in old_semaphores.items():
    await old_gate.wake_all_waiters()
```

**问题细节**：
- `wake_all_waiters` 将旧 gate 的 limit 设为 `active + 1` 并 `notify_all`，但被唤醒的协程使用旧 gate 完成 `acquire`，下次循环时从字典获取新 gate。如果协程在 `acquire` 和 `release` 之间被取消，旧 gate 的 `_active` 计数不会递减，但旧 gate 已不在字典中，无法被清理。
- 重建期间（`clear()` 到新 gate 创建完成），如果有新的 `_collect_loop` 尝试获取信号量，会从新字典中获取新 gate，但旧 gate 上正在等待的协程可能还未被唤醒，导致短时间内采集并发超过限制。
- `_ConcurrencyGate.acquire` 使用 `Condition.wait()`，如果 `set_limit` 的 `notify` 丢失（理论上不会，但 `Condition` 在 asyncio 中的实现有已知边界情况），等待者会永久阻塞。

**影响**：动态调整并发数时可能出现短暂的并发超限或采集任务卡住。

---

### ENG-005：DeviceLifecycleManager 持久化失败后的状态回滚不完整 【中】

**现象**：`on_device_online` 在持久化失败时回滚 `_status_map`：

```python
async def on_device_online(self, device_id: str) -> None:
    async with self._db_lock:
        old_status = self._status_map.get(device_id, "offline")
        if old_status == "online":
            return
        self._status_map[device_id] = "online"
        event = DeviceStatusEvent(...)
    try:
        await self._persist_status(device_id, "online")
    except Exception as e:
        async with self._db_lock:
            self._status_map[device_id] = old_status
        raise
    if event:
        await self._event_bus.publish(event)
```

**问题细节**：
- 持久化失败后 `raise`，调用方（通常是驱动层）可能不处理此异常，导致设备实际已连接但 lifecycle 管理器认为未连接。
- 如果 `_persist_status` 成功但 `_event_bus.publish` 失败（事件总线队列满），内存状态已更新、DB 已持久化，但订阅者未收到事件，形成状态不一致。
- `close()` 方法获取 `_db_lock` 后在 `_sqlite_lock` 内关闭连接，但如果此时有 `_persist_status` 正在 `to_thread` 中执行且持有 `_sqlite_lock`，`close()` 会阻塞在 `_sqlite_lock` 上，而 `_persist_status` 需要 `_db_lock`（已被 `close` 持有），形成跨锁嵌套。

**影响**：设备状态在内存、数据库和事件订阅者之间不一致。

---

## 四、安全模块问题

### SEC-001：表达式引擎沙箱对计算式下标访问防护不足 【高】

**现象**：AST 访问器对 `Subscript` 节点的检查仅覆盖字符串常量键：

```python
def visit_Subscript(self, node: ast.Subscript) -> None:
    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        if node.slice.value.startswith("__") and node.slice.value.endswith("__"):
            raise ValueError(...)
    self.generic_visit(node)
```

**问题细节**：
- 如果下标是计算表达式（如 `obj[chr(95)*2 + "class" + chr(95)*2]`），不会被检测到。虽然 `chr` 不在安全内置函数列表中，但用户可以通过表达式构造出 `__class__` 字符串。
- 更危险的是，如果用户注册了自定义函数（通过 `register_function`），且该函数返回一个包含 `__class__` 的字符串，表达式可以通过 `obj[my_func()]` 访问 dunder 属性。
- `visit_BinOp` 对 `Pow` 的检查仅限制常量指数：`if isinstance(right, ast.Constant) and isinstance(right.value, int) and right.value > 1000`。计算式指数如 `10 ** (500 * 2)` 不受限制，可导致整数爆炸。
- `ast.Subscript` 在 Python 3.9+ 中 `node.slice` 直接是表达式节点（不再是 `ast.Index`），代码同时检查 `ast.Index`（Python 3.8 兼容），但 Python 3.12+ 中 `ast.Index` 已被移除，可能导致 `NameError`。

**影响**：精心构造的表达式可能突破沙箱访问 Python 内部对象，或通过整数爆炸导致 DoS。

---

### SEC-002：自定义函数注册的闭包检查可被间接绕过 【中】

**现象**：`register_function` 检查函数的 `co_names` 和 `co_freevars`：

```python
code_obj = getattr(func, "__code__", None)
checked_names = set(getattr(code_obj, "co_names", ())) | set(getattr(code_obj, "co_freevars", ()))
for dangerous in _DANGEROUS_CODE_NAMES:
    if dangerous in checked_names:
        raise ValueError(...)
```

**问题细节**：
- `co_names` 仅包含函数直接引用的全局名称。如果函数 A 调用函数 B（已注册），函数 B 内部使用了 `getattr`，则函数 A 的 `co_names` 不包含 `getattr`，检查通过。
- 闭包检查（`__closure__`）仅检查 `cell_contents` 是否为模块对象，但不检查深层嵌套闭包（闭包中的闭包）。
- 使用 `functools.partial` 或 `types.MethodType` 包装的函数可能没有 `__code__` 属性（检查会抛 `ValueError`），但代码对此情况的处理是直接拒绝，这可能导致合法函数被误拒。
- `callable(func)` 检查在后，如果 `func` 是一个类的实例（实现了 `__call__`），`__code__` 属性存在于 `__call__` 方法上而非实例上，检查逻辑可能不正确。

**影响**：通过已注册函数的间接调用链可能访问危险功能。

---

### SEC-003：RBAC 权限矩阵中 OPERATOR 缺少部分写入权限 【中】

**现象**：`ROLE_PERMISSIONS` 中 `OPERATOR` 角色不包含 `DEVICE_CREATE`、`DEVICE_UPDATE`、`DEVICE_DELETE`：

```python
Role.OPERATOR: frozenset({
    Permission.DEVICE_READ,
    Permission.DRIVER_READ,
    Permission.RULE_READ,
    Permission.RULE_TOGGLE,  # 可启停规则但不能创建/修改
    # ... 缺少 DEVICE_CREATE/UPDATE/DELETE, RULE_CREATE/UPDATE/DELETE
})
```

**问题细节**：
- `OPERATOR` 可以 `RULE_TOGGLE`（启停规则）但不能 `RULE_CREATE`/`RULE_UPDATE`，这意味着操作员无法调整告警阈值，只能启停。在实际工业场景中，操作员通常需要根据现场情况调整阈值。
- `OPERATOR` 有 `CONFIG_EDIT` 权限可以编辑系统配置，但不能创建/删除设备，权限粒度不一致。
- `VIEWER` 有 `DATA_EXPORT` 权限，这可能允许低权限用户导出大量敏感数据。在工业场景中，数据导出通常需要更严格的控制。

**影响**：角色权限设计可能不符合实际工业场景的操作流程。

---

### SEC-004：WebSocket 首帧认证窗口允许未认证连接接收广播 【中】

**现象**：`connect(token=None)` 路径在 accept 后将连接加入 `_connections`，但标记 `authenticated=False`：

```python
self._conn_meta[websocket] = {
    "authenticated": False,
}
```

**问题细节**：
- 在 `connect` 返回到 `authenticate` 被调用之间的窗口期内，连接已在 `_connections` 中。如果 `broadcast` 方法未检查 `authenticated` 标志（需要验证 `broadcast` 的实现），未认证连接可能收到广播数据。
- 从代码注释看 `broadcast` 会跳过 `authenticated!=True` 的连接，但 `connect` 方法的 token=None 路径注释说"由调用方从首帧消息提取 token 后调用 authenticate()"。如果调用方未正确实现首帧读取逻辑（如 WebSocket handler 中的 `await websocket.receive_text()` 超时），连接会永久停留在未认证状态。
- 未认证连接占用 `max_connections` 配额，恶意客户端可以通过建立大量未认证连接来 DoS。

**影响**：未认证连接可能短暂接收广播数据；连接数 DoS。

---

### SEC-005：JWT 密钥轮换期间旧令牌验证可能产生竞态 【低】

**现象**：系统支持通过 `kid` 头部进行密钥轮换。在轮换期间，验证方需要根据 `kid` 选择对应的密钥。

**问题细节**：
- 如果密钥配置在运行时热加载（`config_reload.py`），在旧密钥被移除新密钥生效的瞬间，使用旧 `kid` 的令牌可能验证失败。
- 密钥轮换通常需要重叠期（两种密钥同时有效），但代码中未见明确的重叠期管理逻辑。
- 如果密钥配置存储在加密的配置文件中，`SecretManager` 的解密失败会导致所有令牌验证失败。

**影响**：密钥轮换期间可能出现短暂的认证失败。

---

## 五、API 路由与错误码问题

### API-001：DriverExceptionMapper 与结构化错误码体系不一致 【中】

**现象**：`base.py` 中的 `DriverExceptionMapper` 使用自己的错误码映射：

```python
class DriverExceptionMapper:
    _EXCEPTION_MAP: dict[type[Exception], str] = {
        ConnectionRefusedError: "ERR_NETWORK_CONNECTION_REFUSED",
        TimeoutError: "ERR_NETWORK_TIMEOUT",
        # ...
    }
```

而 `error_codes.py` 定义了更细粒度的错误码（如 `ModbusDriverErrors.READ_TIMEOUT`、`ModbusDriverErrors.READ_EXCEPTION` 等）。

**问题细节**：
- `DriverExceptionMapper` 的错误码（如 `ERR_NETWORK_CONNECTION_REFUSED`）不在 `error_codes.py` 中定义，前端 i18n 可能缺少对应翻译。
- `map_exception` 方法将所有 `ValueError` 映射为 `ERR_DEVICE_CONFIG_INVALID`，但 `ValueError` 可能由非配置原因引起（如数据解析错误）。
- `OSError` 的子类 `ConnectionRefusedError` 和 `ConnectionResetError` 被映射为相同的错误码，丢失了故障诊断所需的差异化信息。

**影响**：前端错误提示可能不准确；运维排障信息丢失。

---

### API-002：错误码数量过多，i18n 维护负担重 【低】

**现象**：`error_codes.py` 定义了超过 **200 个错误码**，分布在 15+ 个错误码类中：

- `AuthErrors`: 31 个
- `UserErrors`: 12 个
- `DeviceErrors`: 33 个
- `ServiceErrors`: 12 个
- 等

**问题细节**：
- 前端需要为每个错误码提供中英文翻译（`zh-CN.ts` 和 `en-US.ts`），200+ 个错误码的翻译维护成本很高。
- 错误码使用字符串常量（非枚举），拼写错误不会被编译器检测到。
- 部分错误码命名不一致：有的使用 `ERR_{MODULE}_{ACTION}_FAILED`（如 `ERR_DEVICE_CREATE_FAILED`），有的使用 `ERR_{MODULE}_{STATE}`（如 `ERR_DEVICE_OFFLINE`），有的使用 `ERR_{MODULE}_{CONDITION}`（如 `ERR_CIRCUIT_BREAKER_OPEN`）。

**影响**：前端可能缺少部分错误码的翻译，用户看到原始错误码字符串。

---

### API-003：HTTP 拦截器对 409 Conflict 的处理过于简单 【低】

**现象**：前端 HTTP 拦截器将所有 409 响应标记为 `isConflictError`：

```typescript
if (status === 409) {
    error.isConflictError = true
    error.conflictMessage = errorMessage || t('http.dataConflict')
    return Promise.reject(error)
}
```

**问题细节**：
- 409 可能由多种原因引起：乐观锁冲突、资源已存在、状态冲突等。统一标记为冲突错误丢失了细分原因。
- 调用方需要检查 `isConflictError` 并自行处理重试或提示，但没有任何自动重试逻辑（如乐观锁冲突后重新获取数据并让用户合并）。
- `conflictMessage` 使用 `errorMessage` 或通用翻译，不包含冲突的具体字段或当前值与期望值。

**影响**：用户体验不佳，冲突时缺乏有用的诊断信息。

---

## 六、前端状态管理与安全问题

### FE-001：isAuthenticated 基于 sessionStorage 中的 username，刷新后可能误判 【中】

**现象**：

```typescript
const isAuthenticated = computed(() => !!username.value)
const username = ref<string>(_getItem('edgelite_username'))
```

**问题细节**：
- 用户关闭浏览器标签页后重新打开，`sessionStorage` 被清除，`username` 为空，`isAuthenticated` 为 `false`，行为正确。
- 但用户在同一标签页中刷新页面，`sessionStorage` 保留 `username`，`isAuthenticated` 为 `true`。此时如果后端 session 已过期（HttpOnly Cookie 过期），首次 API 调用会返回 401，触发 `fetchUserInfo`，但 `fetchUserInfo` 的 401 处理会清空 `username` 并将 `isAuthenticated` 设为 `false`。
- 问题在于：在 `fetchUserInfo` 完成之前，UI 可能已经基于 `isAuthenticated=true` 渲染了需要认证的页面，导致短暂的未授权数据展示（虽然数据请求会失败）。

**影响**：页面刷新后短暂展示认证内容但数据加载失败。

---

### FE-002：Token 刷新失败后的重试窗口可能产生竞争 【低】

**现象**：

```typescript
if (refreshFailed && Date.now() - refreshFailedTime > 10000) {
    refreshFailed = false
    // 不 return，继续走正常 refresh 流程
} else {
    refreshFailed = false
    auth.logout()
    redirectToLogin()
    return Promise.reject(error)
}
```

**问题细节**：
- `refreshFailed` 被重置为 `false` 后，如果有多个 401 请求同时到达，它们可能同时通过 `refreshFailed` 检查并各自发起 refresh 请求。
- 虽然 `isRefreshing` 标志会阻止部分竞争（第二个 401 会进入 `addRefreshSubscriber` 队列），但 `isRefreshing` 的设置在 `refreshFailed` 检查之后，存在微小的时间窗口。
- `refreshSubscribers` 队列的超时为 10 秒，与 `refreshFailed` 的重置时间相同。如果 refresh 请求耗时接近 10 秒，订阅者超时和 `refreshFailed` 重置可能同时发生，导致行为不可预测。

**影响**：极小概率下可能发起多个 refresh 请求，但 `isRefreshing` 保护通常会阻止这种情况。

---

### FE-003：空闲会话超时监听的事件处理可能影响性能 【低】

**现象**：

```typescript
function startIdleWatch() {
    window.addEventListener('mousemove', resetIdleTimer)
    window.addEventListener('keydown', resetIdleTimer)
    window.addEventListener('click', resetIdleTimer)
    window.addEventListener('scroll', resetIdleTimer)
    resetIdleTimer()
}
```

**问题细节**：
- `mousemove` 和 `scroll` 事件在用户操作时高频触发，每次调用 `resetIdleTimer` 都会 `clearTimeout` 和 `setTimeout`。在 SCADA 画面或数字孪生等有大量 DOM 交互的页面中，可能造成可感知的性能下降。
- 建议使用 `passive: true` 选项或节流（throttle）来减少 `resetIdleTimer` 的调用频率。
- `startIdleWatch` 在 `login` 和 `fetchUserInfo` 中都被调用，虽然 `addEventListener` 对相同函数引用会去重，但这种模式不够健壮——如果 `resetIdleTimer` 被重新赋值（虽然当前代码不会），去重会失效。

**影响**：高频交互页面可能感受到轻微卡顿。

---

### FE-004：CSRF Token 的 base64 编码提供虚假安全感 【低】

**现象**：

```typescript
function _encode(value: string): string {
    const bytes = new TextEncoder().encode(value)
    return btoa(String.fromCharCode(...bytes))
}
```

**问题细节**：
- CSRF Token 存储在 `sessionStorage` 中并使用 base64 编码。base64 不是加密，只是编码，任何人都可以 `atob` 解码。
- 这种编码可能让开发者误以为 token 被保护了，实际上 XSS 攻击可以轻易读取 CSRF token。
- 不过，CSRF token 的安全模型本身就是"同源可读"——它只需要防止跨站请求伪造（CSRF），不需要防止同源 XSS。如果 XSS 已经发生，攻击者可以直接调用 API（不需要 CSRF token），所以 base64 编码在这里没有实际的安全影响，但也不提供任何额外保护。

**影响**：无实际安全影响，但代码可能误导维护者。

---

## 七、存储与数据一致性问题

### STORE-001：InfluxDB 降级到 SQLite 的增量同步可能丢数据 【高】

**现象**：系统支持 InfluxDB 不可用时自动降级到 SQLite 存储时序数据，InfluxDB 恢复后进行增量同步。

**问题细节**：
- 降级期间数据写入 SQLite，但如果进程在降级期间崩溃，SQLite 中的未提交数据可能丢失（取决于事务隔离级别和 WAL 配置）。
- 增量同步通常基于时间戳（最后同步时间），但如果降级期间设备时钟与服务器时钟有偏差，同步可能遗漏或重复数据。
- 恢复 InfluxDB 后的同步是批量操作，如果同步过程中网络再次中断，已同步的数据可能被重复写入（InfluxDB 的幂等性取决于 retention policy 和 measurement 设计）。
- 降级检测通常基于写入失败次数的阈值，在阈值达到之前的数据写入会失败并可能被丢弃。

**影响**：InfluxDB 短暂不可用期间可能丢失部分时序数据。

---

### STORE-002：SQLite WAL 模式下的检查点 (checkpoint) 策略不明确 【中】

**现象**：系统使用 `check_and_convert_to_wal` 将 SQLite 切换到 WAL 模式，并应用标准 PRAGMAs。

**问题细节**：
- WAL 模式下，写入操作追加到 WAL 文件，读取操作可以并发进行。但如果 WAL 文件增长过大（没有定期 checkpoint），读取性能会下降。
- 代码中未见显式的 checkpoint 策略（如 `PRAGMA wal_autocheckpoint`），SQLite 默认在 WAL 达到 1000 页时自动 checkpoint，但在高频写入场景下可能不够。
- 如果进程崩溃，WAL 文件中的未 checkpoint 数据需要在下次启动时恢复。虽然 SQLite 会自动恢复，但恢复过程中数据库被锁定，可能导致启动延迟。
- 多个模块（`DeviceLifecycleManager`, `sqlite_repo`, `alarm_outbox` 等）各自管理独立的 SQLite 数据库文件，文件数量多，维护复杂。

**影响**：长期运行后 SQLite 读取性能可能下降；启动恢复可能延迟。

---

### STORE-003：设备级状态字典清理分散在多处，容易遗漏 【中】

**现象**：设备移除时需要清理的状态分布在多个组件中：

- `DriverPlugin.reset_health_stats`: 清理 `_health_stats`, `_offline_since`, `_circuit_states`, `_circuit_open_sinces`, `_half_open_calls`, `_connection_statuses`, `_reconnect_state`, `_device_configs`
- `ModbusTcpDriver.remove_device`: 清理 `_clients`, `_device_pool_key`, `_device_configs`, `_device_points`, `_retry_count` 等
- `CollectScheduler.stop_collect`: 清理 `_tasks`, `_device_info`, `_device_priorities`, `_adaptive_state`, `_last_values`, `_collect_stats`, `_device_quality_stats`, `_last_collect_time`, `_last_ai_inference_time`
- `DeviceLifecycleManager.remove_device`: 清理 `_status_map` 和 DB 记录

**问题细节**：
- 每个组件独立维护设备级状态，没有统一的"设备移除"事件来触发所有组件的清理。
- 如果新增一个管理设备状态的组件，开发者需要记住在设备移除时添加清理逻辑，否则会产生内存泄漏。
- `EventBus` 的 `DeviceStatusEvent` 不包含 "device_removed" 动作，无法用于通知各组件清理。

**影响**：设备频繁增删场景下，某些组件中的设备状态可能不被清理，导致内存缓慢增长。

---

## 八、部署与运维问题

### OPS-001：日志中混用中英文，不利于国际化和日志分析 【低】

**现象**：虽然代码中有大量 `FIXED-P3: 中文日志→英文` 注释，但仍有部分日志使用中文：

```python
# lifecycle.py
logger.warning("解析数据库路径配置失败: %s", e)
logger.warning("关闭SQLite连接失败: %s", e)

# scheduler.py
logger.warning("信号量迁移: 唤醒旧信号量等待者失败 (priority=%s): %s", p.name, migrate_e)
logger.info("优先级信号量重建完成: old_max=%d, new_max=%d, 已迁移 %d 个旧信号量", ...)
```

**问题细节**：
- 混合中英文日志使 ELK/Loki 等日志分析系统的模式匹配变得困难。
- 中文日志在非 UTF-8 终端中可能显示乱码。
- 国际用户需要额外配置日志翻译。

**影响**：日志分析效率降低。

---

### OPS-002：Docker 容器中只读文件系统与 SQLite WAL 模式可能冲突 【中】

**现象**：Docker 安全加固使用只读文件系统（`read_only: true`），SQLite WAL 模式需要创建 `-wal` 和 `-shm` 文件。

**问题细节**：
- 如果 SQLite 数据库文件位于只读文件系统上，WAL 模式无法创建 WAL 文件，会回退到 DELETE 模式，写入性能下降。
- 通常通过 `tmpfs` 或 volume mount 来解决，但需要确保所有 SQLite 数据库路径都在可写区域。系统有多个独立的 SQLite 数据库（`device_status.db`, `alarm_outbox.db`, 主数据库等），每个都需要单独配置。
- `check_and_convert_to_wal` 在只读文件系统上会失败，但代码仅记录 warning 日志，不影响启动。这意味着在 Docker 生产环境中，SQLite 可能以非 WAL 模式运行，写入并发性受限。

**影响**：Docker 生产环境中 SQLite 写入性能可能不如预期。

---

### OPS-003：内存使用缺乏全局监控和限制 【高】

**现象**：系统中存在多个无统一管理的内存缓存：

| 缓存 | 位置 | 上限 | 估算内存 |
|------|------|------|----------|
| `_last_values` | ModbusTcpDriver | 10000 | ~800KB |
| `_single_point_cache` | ModbusTcpDriver | 10000 | ~1MB |
| `_read_fail_tracker` | ModbusTcpDriver | 20000 | ~2MB |
| `_point_health` | ModbusTcpDriver | 无明确上限 | 可变 |
| `_point_value_cache` | RuleEvaluator | `_POINT_VALUE_CACHE_MAX` | ~800KB |
| `_seen_event_ids` | EventBus | 10000 | ~1MB |
| `_recent_firings` | RuleEvaluator | 10000 | ~800KB |
| `_duration_tracker` | RuleEvaluator | `active_rules * 2` | 可变 |
| `_write_audit_log` | ModbusTcpDriver | 1000 (deque) | ~200KB |

**问题细节**：
- 100 个设备 × 50 个测点 = 5000 个测点，每个测点在多个缓存中都有条目。仅 ModbusTcpDriver 的 3 个 LRU 缓存就可能占用 ~4MB。
- `_point_quality_history` 和 `_device_latency_history` 使用 deque 但没有设置 maxlen，如果设备运行时间长且采集频率高，这些 deque 会无限增长。
- `_point_health` 使用 OrderedDict 但没有明确的容量上限设置代码（虽然注释提到 LRU 淘汰，但代码中未见 `popitem` 调用）。
- 缺乏全局内存监控端点来报告各缓存的当前大小和总内存使用量。

**影响**：长期运行后内存使用持续增长，在资源受限的边缘设备上可能触发 OOM。

---

### OPS-004：配置热加载期间的服务行为不一致 【中】

**现象**：`config_reload.py` 支持配置热加载，但各组件对配置变更的响应不一致。

**问题细节**：
- `CollectScheduler` 的 `set_max_concurrent` 会重建信号量，但采集间隔的变更（`collect_interval`）需要停止并重新启动采集任务，代码中未见自动触发。
- 驱动配置变更（如 Modbus 从站地址修改）需要调用 `add_device` 重新初始化，但如果设备正在采集中，重新初始化可能导致短暂的数据中断。
- 安全配置变更（如 JWT 密钥轮换）可能导致正在使用的令牌验证失败。
- 配置热加载没有版本号或 epoch 机制，无法确保所有组件都应用了最新配置。如果加载过程中某个组件失败，部分组件使用新配置、部分使用旧配置。

**影响**：配置变更后系统行为不一致，可能短暂中断数据采集或认证。

---

## 九、问题严重度汇总矩阵

| ID | 模块 | 严重度 | 标题 | 影响范围 |
|----|------|--------|------|----------|
| ARCH-001 | 架构 | 🔴严重 | 锁层级体系过于复杂，死锁风险 | 全系统 |
| ARCH-002 | 架构 | 🟠高 | 同步/异步混合调用模式脆弱 | 驱动层+引擎层 |
| ARCH-003 | 架构 | 🟠高 | 状态字典膨胀，缺乏统一生命周期管理 | 驱动层 |
| DRV-001 | 驱动 | 🟠高 | Modbus TCP 连接池 TOCTOU 竞态 | Modbus TCP |
| DRV-002 | 驱动 | 🟡中 | Stale Client 清理机制资源泄漏 | Modbus TCP |
| DRV-003 | 驱动 | 🔴严重 | 自定义驱动沙箱全局 import 劫持风险 | 驱动注册 |
| DRV-004 | 驱动 | 🟡中 | pymodbus 版本检测懒初始化竞态 | Modbus 驱动 |
| DRV-005 | 驱动 | 🟠高 | 线程池资源上限成为采集瓶颈 | 所有同步驱动 |
| DRV-006 | 驱动 | 🟡中 | DriverHealthStats 状态逻辑矛盾 | 所有驱动 |
| ENG-001 | 引擎 | 🟠高 | Duration Tracker 清理窗口丢告警 | 规则评估 |
| ENG-002 | 引擎 | 🟠高 | 规则评估超时后状态不一致 | 规则评估 |
| ENG-003 | 引擎 | 🟡中 | 事件总线去重缓存高吞吐误删 | 事件总线 |
| ENG-004 | 引擎 | 🟡中 | 并发门控迁移丢失采集周期 | 采集调度 |
| ENG-005 | 引擎 | 🟡中 | Lifecycle 持久化失败状态回滚不完整 | 设备状态 |
| SEC-001 | 安全 | 🟠高 | 表达式沙箱计算式下标防护不足 | 表达式引擎 |
| SEC-002 | 安全 | 🟡中 | 自定义函数闭包检查可被绕过 | 表达式引擎 |
| SEC-003 | 安全 | 🟡中 | RBAC 权限矩阵设计不合理 | 权限控制 |
| SEC-004 | 安全 | 🟡中 | WebSocket 首帧认证窗口风险 | WebSocket |
| SEC-005 | 安全 | 🟢低 | JWT 密钥轮换竞态 | 认证 |
| API-001 | API | 🟡中 | DriverExceptionMapper 与错误码不一致 | API 层 |
| API-002 | API | 🟢低 | 错误码过多，i18n 维护负担 | API 层 |
| API-003 | API | 🟢低 | 409 Conflict 处理过于简单 | 前端 API |
| FE-001 | 前端 | 🟡中 | isAuthenticated 刷新后误判 | 前端认证 |
| FE-002 | 前端 | 🟢低 | Token 刷新失败重试窗口竞争 | 前端认证 |
| FE-003 | 前端 | 🟢低 | 空闲监听事件性能影响 | 前端性能 |
| FE-004 | 前端 | 🟢低 | CSRF Token base64 虚假安全感 | 前端安全 |
| STORE-001 | 存储 | 🟠高 | InfluxDB 降级同步可能丢数据 | 时序存储 |
| STORE-002 | 存储 | 🟡中 | SQLite WAL checkpoint 策略不明确 | SQLite |
| STORE-003 | 存储 | 🟡中 | 设备级状态清理分散 | 多组件 |
| OPS-001 | 运维 | 🟢低 | 日志混用中英文 | 日志分析 |
| OPS-002 | 运维 | 🟡中 | Docker 只读 FS 与 SQLite WAL 冲突 | Docker 部署 |
| OPS-003 | 运维 | 🟠高 | 内存使用缺乏全局监控和限制 | 全系统 |
| OPS-004 | 运维 | 🟡中 | 配置热加载服务行为不一致 | 全系统 |

### 严重度统计

| 严重度 | 数量 | 占比 |
|--------|------|------|
| 🔴 严重 | 2 | 6.3% |
| 🟠 高 | 8 | 25.0% |
| 🟡 中 | 14 | 43.8% |
| 🟢 低 | 8 | 25.0% |
| **合计** | **32** | 100% |

---

## 十、总结与建议优先级

### 第一优先级（立即处理）

1. **ARCH-001 锁层级治理**：定义全局锁获取顺序文档，将关联状态合并到单一锁保护下，消除跨锁嵌套。
2. **DRV-003 沙箱安全**：将自定义驱动加载改为子进程模式或使用 `ast` 模块预解析驱动代码，避免全局 `builtins` 劫持。
3. **SEC-001 表达式沙箱**：禁止所有非字符串常量的 Subscript 访问，对计算式 Pow 指数做运行时值检查。

### 第二优先级（近期处理）

4. **DRV-005 线程池扩容**：将默认线程池大小从 4 提升到 `max(8, device_count // 4)`，或改为每个驱动实例独立线程池。
5. **ENG-001/ENG-002 评估器健壮性**：Duration Tracker 清理改为基于规则 `duration` 配置的动态 cutoff；评估超时后强制清理所有锁和 tracker 状态。
6. **OPS-003 内存监控**：添加 `/api/v1/system/memory-stats` 端点，报告所有缓存的当前大小；为 `_point_quality_history` 和 `_device_latency_history` 添加 maxlen。
7. **STORE-001 数据同步**：在降级写入 SQLite 时使用显式事务，并在同步到 InfluxDB 后标记已同步记录（而非依赖时间戳）。

### 第三优先级（中期优化）

8. **ARCH-003 状态管理重构**：引入 `DeviceStateContext` 类封装所有设备级状态，提供统一的 `cleanup(device_id)` 方法。
9. **DRV-001 连接池重写**：使用 `asyncio.ConnectionPool` 模式重写连接池，消除两次获取锁的模式。
10. **SEC-003 RBAC 权限调整**：根据实际工业场景操作流程，为 OPERATOR 角色添加 DEVICE_UPDATE 和 RULE_UPDATE 权限。

---

> **声明**：本报告基于源码静态分析，未修改任何源代码。部分问题的影响评估基于代码模式推断，实际影响可能因运行时环境和负载特征而异。建议结合动态测试和压力测试验证关键问题。
