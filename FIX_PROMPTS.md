# EdgeLite V1.0 社区版 — 修复提示语文档

> **用途**：将以下提示语逐条输入 AI 编程助手（如 CatPaw），引导其按照分析报告 `SYSTEM_ANALYSIS_REPORT.md` 的优先级顺序修复系统问题。  
> **项目路径**：`e:/硕腾网络/PyGBSentry/EdgeLite/EdgeLite-v1.0-Community`  
> **技术栈**：Python 3.12+ / FastAPI / SQLAlchemy 2.0 Async / Vue 3 + Pinia / SQLite / InfluxDB  
> **约束**：每次修复后运行 `pytest` 和 `ruff check` 验证，确保不引入回归。

---

## 全局上下文提示（每次对话开头粘贴）

```
你正在修复 EdgeLite V1.0 社区版的系统性问题。项目根目录为 e:/硕腾网络/PyGBSentry/EdgeLite/EdgeLite-v1.0-Community。

项目是一个工业边缘网关系统，后端基于 FastAPI 异步架构，前端使用 Vue 3 + Pinia。
系统涉及多种工业协议驱动（Modbus TCP/RTU, OPC UA, S7 等）、规则引擎、告警系统、时序数据存储。

修复要求：
1. 每次只修复一个问题，修复后运行 ruff check 和相关测试验证
2. 保持代码风格与现有代码一致（类型注解、docstring、日志风格）
3. 修复注释使用 # FIX-{ISSUE_ID}: 格式标注
4. 不要破坏现有 API 接口和数据库 schema
5. 涉及并发的修改需要仔细分析锁获取顺序，避免引入新的死锁
6. 所有日志使用英文，不使用中文日志
```

---

## 第一优先级：立即处理（🔴严重 + 🟠高）

---

### 提示语 1：ARCH-001 锁层级治理

```
修复 ARCH-001：DriverPlugin 基类锁层级体系过于复杂，存在死锁风险。

问题位置：src/edgelite/drivers/base.py

当前问题：
- DriverPlugin 基类持有 9 把锁（_stats_lock, _conn_state_lock, _circuit_lock, _executor_lock, _reconnect_lock 等）
- _evaluate_degradation 方法在 _stats_lock 内读取数据后释放锁，再通过 create_task 调度 _set_connection_state（获取 _conn_state_lock），与另一方向获取锁的路径形成 ABBA
- _record_read_failure 同步方法中通过 create_task 调度异步 _record_circuit_failure，两条路径锁顺序不一致

修复方案：
1. 在 base.py 文件头部添加锁层级文档注释，定义全局锁获取顺序：
   # Lock Hierarchy (must acquire in this order, top-to-bottom):
   # 1. _executor_lock (asyncio.Lock)
   # 2. _reconnect_lock (asyncio.Lock)
   # 3. _stats_lock (threading.RLock)
   # 4. _circuit_lock (threading.Lock)
   # 5. _conn_state_lock (threading.RLock)
2. 审计 _evaluate_degradation 方法，确保 _set_connection_state 的调用完全在 _stats_lock 释放之后
3. 确保 _record_read_failure 的同步回退路径与异步路径获取锁的顺序完全一致
4. 在 _evaluate_degradation 中，将 create_task 调度改为在锁外直接 await（如果可能），或确保不存在反向锁获取

验证方法：编写一个并发测试，同时触发 _evaluate_degradation 和 _set_connection_state，确保不会死锁。
```

---

### 提示语 2：DRV-003 自定义驱动沙箱安全加固

```
修复 DRV-003：自定义驱动加载机制通过全局替换 builtins.__import__ 实现沙箱，存在全局 import 系统损坏风险。

问题位置：src/edgelite/drivers/registry.py 的 _discover_custom_drivers 方法

当前问题：
- 全局替换 builtins.__import__、importlib.import_module、importlib.util.spec_from_file_location
- 如果 exec_module 抛出 SystemExit 或 KeyboardInterrupt，finally 块内部的恢复操作可能失败
- 挂起 sys.modules 的逻辑可能影响其他协程的导入

修复方案：
1. 不再全局替换 builtins.__import__，改为仅在自定义驱动模块的 __builtins__ 上设置受限的 __import__
2. 不再 pop sys.modules 中的非白名单模块（移除 _SUSPENDED_MODULES 逻辑）
3. 在 spec.loader.exec_module 之前，为 module.__builtins__ 设置独立的受限字典：
   module.__builtins__ = {"__import__": _restricted_import, "__name__": module_name, ...}
4. 在 finally 块中仅恢复 importlib.import_module 和 importlib.util.spec_from_file_location
5. 添加 SystemExit 和 KeyboardInterrupt 的捕获，确保 finally 块一定能执行恢复
6. 为恢复操作添加异常捕获，即使恢复失败也记录 error 日志

验证方法：编写测试加载一个包含 import os 的恶意自定义驱动，确认被拦截；加载正常驱动确认功能正常；模拟 exec_module 抛出 SystemExit，确认全局 import 不受影响。
```

---

### 提示语 3：SEC-001 表达式沙箱加固

```
修复 SEC-001：表达式引擎的 AST 访问器对计算式下标访问和幂运算防护不足。

问题位置：src/edgelite/engine/expression_engine.py 的 SafeExpressionVisitor 类

当前问题：
1. visit_Subscript 仅检查字符串常量键的 dunder 访问，计算式下标（如 obj[chr(95)*2+"class"+chr(95)*2]）可绕过
2. visit_BinOp 对 Pow 的检查仅限制常量指数，计算式指数（如 10**(500*2)）不受限制
3. ast.Index 在 Python 3.12+ 已被移除，代码中引用可能导致 NameError

修复方案：
1. visit_Subscript 方法修改：
   - 完全禁止非常量下标访问（仅允许 ast.Constant 类型的 slice）
   - 对所有字符串常量下标检查 dunder 模式（已有的逻辑保留）
   - 对数字常量下标允许通过
2. visit_BinOp 方法修改：
   - 对 Pow 操作，无论左右操作数是否为常量，都添加运行时值检查
   - 在 ExpressionEngine.evaluate 方法中，实际执行 pow(a, b) 前检查 b 的值不超过 1000
   - 或者：在 visit_BinOp 中禁止 Pow 的右操作数为非常量表达式（更严格但更安全）
3. 兼容性修复：
   - 移除对 ast.Index 的直接引用（Python 3.9+ 中 node.slice 直接是表达式节点）
   - 使用 hasattr 检查或 try/except 处理 Python 版本差异

验证方法：编写测试验证以下表达式被拒绝：
- obj["__class__"]（已有，确保仍被拒绝）
- obj[some_variable]（新增：非常量下标应被拒绝）
- 10 ** (500 * 2)（新增：计算式指数应被拒绝）
- 10 ** 50（正常：常量指数小于 1000 应通过）
```

---

### 提示语 4：DRV-005 线程池资源扩容

```
修复 DRV-005：DriverPlugin 基类线程池默认仅 4 个 worker，多设备同步驱动场景下成为采集瓶颈。

问题位置：src/edgelite/drivers/base.py

当前问题：
- _executor_max_workers 默认为 4
- 多个使用同步库的设备（如 OPC DA）共享这个 4-worker 线程池
- 如果 4 个操作都在等待 I/O 超时，后续所有设备采集被阻塞

修复方案：
1. 将 _executor_max_workers 默认值从 4 提升到 16
2. 在 start() 方法中读取配置，允许通过配置文件覆盖：
   max_workers = config.get("driver_executor_workers", 16)
   self._executor_max_workers = max(4, min(64, max_workers))
3. 在 _run_in_executor 的超时重建逻辑中，确保新 executor 使用配置的 max_workers
4. 添加 _executor 活跃任务数的监控指标到 get_observability_metrics 方法中：
   "executor_active_futures": len(self._executor_futures),
   "executor_max_workers": self._executor_max_workers,
5. 当 _executor_futures 超过 _executor_futures_warn_threshold 时，不仅记录 warning，还应触发降级（如跳过非关键采集）

验证方法：编写测试模拟 8 个设备同时通过 _run_in_executor 执行耗时 5 秒的同步操作，验证所有操作能在 10 秒内完成（而非 10 秒×2）。
```

---

### 提示语 5：ENG-001 Duration Tracker 清理逻辑修复

```
修复 ENG-001：规则评估器的 Duration Tracker 清理窗口可能丢失长时间持续条件告警。

问题位置：src/edgelite/engine/evaluator.py 的 _prune_duration_tracker 方法

当前问题：
- cutoff 固定为 86400 秒（24小时），规则 duration 超过此值时 tracker 会被提前清理
- max_entries 基于 active_rule_count * 2，跨设备规则可能超限
- _prune_duration_tracker 是同步方法直接操作 _duration_tracker，未加 _state_lock 保护

修复方案：
1. 将 cutoff 改为动态值：
   - 遍历所有缓存的规则，获取最大 duration 值
   - cutoff = max(86400, max_duration * 2)
   - 如果无法获取规则列表，使用默认值 86400
2. max_entries 计算：
   - 考虑每个规则可能关联多个设备，使用 _duration_tracker 中不同 rule_id 的数量作为 active_rule_count
   - max_entries = max(active_rule_count * 50, 500)  # 每个规则最多 50 个设备
3. 将 _prune_duration_tracker 改为 async 方法，使用 async with self._state_lock 保护
4. 在 _eval_loop 中调用时改为 await self._prune_duration_tracker()

验证方法：编写测试创建一个 duration=90000（25小时）的规则，模拟条件持续满足 25 小时，验证告警正常触发而非被清理。
```

---

### 提示语 6：ENG-002 规则评估超时后状态清理

```
修复 ENG-002：规则评估超时后内部状态（锁、tracker）可能不一致。

问题位置：src/edgelite/engine/evaluator.py 的 _evaluate 和 _evaluate_inner 方法

当前问题：
- _evaluate 使用 asyncio.wait_for(timeout=5.0) 包装 _evaluate_inner
- 超时后 CancelledError 会在 _evaluate_inner 的 await 点抛出
- 如果超时发生在 _state_lock 持有期间，async with 会释放锁，但 _duration_tracker 和 _recent_firings 可能处于中间状态
- 如果超时发生在 _fire_alarm 的 alarm_repo.create 之后、event_bus.publish 之前，形成孤儿告警

修复方案：
1. 在 _evaluate 方法中，超时后添加状态清理逻辑：
   - 清理当前事件相关的 _duration_tracker 条目（如果超时发生在 duration 评估期间）
   - 清理 _recent_firings 中可能错误设置的条目
   - 记录详细的超时上下文日志（device_id, point_name, rule_id）
2. 在 _fire_alarm 方法中：
   - 将 alarm_repo.create 和 event_bus.publish 放在同一个 try 块中
   - 如果 publish 失败，记录 error 日志并标记 alarm 为"孤儿告警"（添加到 retry 队列或补偿机制）
   - 保留现有的 CancelledError shield 逻辑，但确保 shield 内的 publish 有超时
3. 在 _evaluate_inner 中，为每个 rule 的评估添加 try/finally：
   - finally 中检查是否有未完成的 _duration_tracker 更新，确保状态一致

验证方法：编写测试模拟 _evaluate_inner 在 _fire_alarm 阶段超时（mock alarm_repo.create 返回后 sleep），验证后续评估循环不阻塞、_recent_firings 被正确回滚。
```

---

### 提示语 7：OPS-003 内存监控与缓存限制

```
修复 OPS-003：系统存在多个无统一管理的内存缓存，缺乏全局监控和硬限制。

问题位置：多文件
- src/edgelite/drivers/modbus_tcp.py: _point_quality_history, _device_latency_history, _point_health
- src/edgelite/engine/evaluator.py: _point_value_cache, _duration_tracker
- src/edgelite/engine/event_bus.py: _seen_event_ids

当前问题：
- _point_quality_history 和 _device_latency_history 使用 deque 但未设置 maxlen
- _point_health 使用 OrderedDict 但代码中未见 popitem LRU 淘汰逻辑
- 缺乏全局内存监控端点

修复方案：
1. 在 ModbusTcpDriver.__init__ 中为所有 deque 设置 maxlen：
   self._point_quality_history: dict[tuple[str, str], deque[str]] = {} 
   # 改为在 add_device 中初始化时设置 maxlen=100
2. 在 _point_health 中添加 LRU 淘汰逻辑：
   在每次 _point_health[key] = value 后检查 len，超过 _MAX_POINT_HEALTH (10000) 时 popitem(last=False)
3. 添加 /api/v1/system/memory-stats 端点到 src/edgelite/api/system.py：
   返回各主要缓存的当前大小：
   {
     "modbus_tcp": {
       "last_values": len(driver._last_values),
       "single_point_cache": len(driver._single_point_cache),
       "read_fail_tracker": len(driver._read_fail_tracker),
       "point_health": len(driver._point_health),
       "point_quality_history": sum(len(d) for d in driver._point_quality_history.values()),
       "write_audit_log": len(driver._write_audit_log),
       "stale_clients": len(driver._stale_clients)
     },
     "evaluator": {
       "rule_cache": len(evaluator._rule_cache),
       "duration_tracker": len(evaluator._duration_tracker),
       "point_value_cache": len(evaluator._point_value_cache),
       "recent_firings": len(evaluator._recent_firings)
     },
     "event_bus": {
       "seen_event_ids": len(event_bus._seen_event_ids),
       "subscribers": len(event_bus._subscribers),
       "retry_tasks": len(event_bus._retry_tasks)
     },
     "executor_futures": len(driver._executor_futures) if hasattr(driver, '_executor_futures') else 0
   }
4. 在 CollectScheduler 中添加缓存大小统计到 get_collect_stats 返回值中

验证方法：添加测试验证 deque maxlen 生效；调用 /api/v1/system/memory-stats 验证返回数据结构正确。
```

---

### 提示语 8：STORE-001 InfluxDB 降级同步数据一致性

```
修复 STORE-001：InfluxDB 降级到 SQLite 的增量同步可能丢数据。

问题位置：src/edgelite/storage/influx_storage.py

当前问题：
- 降级期间数据写入 SQLite，进程崩溃可能丢失未提交数据
- 增量同步基于时间戳，时钟偏差可能导致遗漏或重复
- 降级检测阈值达到前的写入会失败并可能被丢弃

修复方案：
1. 降级写入 SQLite 时使用显式事务：
   async with sqlite_conn.begin():  # 显式事务
       await sqlite_conn.execute(insert_sql, params)
   确保每批数据原子性写入
2. 在 SQLite 降级表中添加 synced 字段（默认 0），同步成功后更新为 1：
   ALTER TABLE degraded_points ADD COLUMN synced INTEGER DEFAULT 0;
   CREATE INDEX idx_degraded_synced ON degraded_points(synced) WHERE synced = 0;
3. 增量同步改为基于 synced 字段而非时间戳：
   SELECT * FROM degraded_points WHERE synced = 0 ORDER BY timestamp ASC LIMIT batch_size
   写入 InfluxDB 成功后：UPDATE degraded_points SET synced = 1 WHERE id IN (...)
4. 定期清理已同步且超过保留期的记录：
   DELETE FROM degraded_points WHERE synced = 1 AND timestamp < datetime('now', '-7 days')
5. 降级检测优化：
   - 连续 3 次写入失败才切换到降级模式（而非单次失败）
   - 降级前的失败数据放入内存 ring buffer（容量 1000），降级后先写入 buffer 中的数据
6. 恢复检测优化：
   - 每 30 秒尝试一次 InfluxDB ping，连续 3 次成功才切换回正常模式
   - 切换回正常模式后，异步执行增量同步（不阻塞正常写入）

验证方法：编写测试模拟 InfluxDB 不可用 → 降级写入 → 进程重启 → InfluxDB 恢复 → 增量同步，验证数据零丢失。
```

---

### 提示语 9：ARCH-002 同步/异步混合调用模式规范化

```
修复 ARCH-002：系统频繁在异步上下文中调用同步方法或反向调度，行为脆弱。

问题位置：src/edgelite/drivers/base.py 的 _record_read_failure 和 _evaluate_degradation

当前问题：
- _record_read_failure 是同步方法，内部通过 loop.create_task() 调度异步的 _record_circuit_failure
- 存在同步回退路径，两条路径需要维护等价逻辑
- _evaluate_degradation 也是同步方法，内部 create_task 调度异步的 _set_connection_state

修复方案：
1. 将 _record_read_failure 改为 async 方法：
   async def _record_read_failure(self, device_id: str) -> None:
   所有调用方需要 await（审计所有调用点，确保调用方是 async 方法）
2. 将 _evaluate_degradation 改为 async 方法：
   async def _evaluate_degradation(self, device_id: str) -> None:
   _record_read_success 和 _record_read_failure 中的调用改为 await self._evaluate_degradation(device_id)
3. 移除 _record_read_failure 中的同步回退路径（_check_circuit_breaker 已使用 asyncio.to_thread，无需同步回退）
4. 将 _evaluate_degradation 中的 create_task 改为直接 await self._set_connection_state(...)
5. 审计所有调用 _record_read_failure 和 _evaluate_degradation 的位置，确保调用方是 async 上下文

注意：某些同步调用方（如 watchdog 循环中的同步回调）可能需要重构为 async，如果无法重构，使用 asyncio.to_thread 包装同步调用。

验证方法：运行全部测试确保无回归；使用 asyncio debug 模式验证无 "coroutine was never awaited" 警告。
```

---

### 提示语 10：DRV-001 Modbus TCP 连接池 TOCTOU 修复

```
修复 DRV-001：ModbusTcpDriver.add_device 方法中连接池存在 TOCTOU 竞态窗口。

问题位置：src/edgelite/drivers/modbus_tcp.py 的 add_device 方法

当前问题：
- 两次获取 _pool_lock，中间执行连接创建（可能耗时数秒）
- 二次检查逻辑复杂，分支众多
- 连接失败且 pool_key 已存在时，设备处于悬空状态

修复方案：
1. 重构 add_device 的连接池逻辑，使用"乐观创建 + 原子注册"模式：
   - 锁外创建连接并 connect()
   - 获取锁后一次性检查并注册：
     async with self._pool_lock:
         if pool_key in self._connection_pool:
             existing_client, ref_count = self._connection_pool[pool_key]
             if connected and existing_client.connected:
                 self._connection_pool[pool_key] = (existing_client, ref_count + 1)
                 self._clients[device_id] = existing_client
             else:
                 # 池中连接不可用，替换为新连接
                 if connected:
                     self._connection_pool[pool_key] = (client, ref_count + 1)
                     self._clients[device_id] = client
                 else:
                     # 新连接也失败，仅记录 pool_key 不绑定 client
                     self._device_pool_key[device_id] = pool_key
         elif connected:
             self._connection_pool[pool_key] = (client, 1)
             self._clients[device_id] = client
         else:
             self._device_pool_key[device_id] = pool_key
   - 如果连接失败且池中没有可用连接，关闭新创建的 client
2. 简化 remove_device 的引用计数逻辑，统一为一个分支：
   - 获取锁后检查 ref_count
   - ref_count <= 1 且 client 不在 _leased_clients 中：关闭并删除
   - ref_count <= 1 且 client 在 _leased_clients 中：标记 stale 并删除池条目
   - ref_count > 1：递减引用计数
3. 为连接失败但 pool_key 已存在的情况添加明确的错误日志和状态标记

验证方法：编写并发测试，10 个设备同时 add_device 到同一 host:port，验证连接池引用计数正确、无连接泄漏。
```

---

## 第二优先级：近期处理（🟡中）

---

### 提示语 11：DRV-002 Stale Client 清理加固

```
修复 DRV-002：Stale Client 清理机制存在资源泄漏窗口。

问题位置：src/edgelite/drivers/modbus_tcp.py

修复方案：
1. 将 _STALE_CLIENT_TIMEOUT 从 300 秒缩短到 120 秒
2. 在 _stale_client_cleanup_loop 中，强制关闭 stale client 时同时从 _leased_clients 中移除
3. 为 _leased_clients 的添加和移除添加 try/finally 确保 release 一定执行
4. 在 stale client 强制关闭后，记录 warning 日志包含 device_id 和 lease 持有时长

验证方法：模拟 client 被 lease 后 release 未调用的场景，验证 120 秒后 stale client 被正确清理。
```

---

### 提示语 12：DRV-006 DriverHealthStats 状态逻辑统一

```
修复 DRV-006：DriverHealthStats 的 effective_state、is_healthy 和 health_score 三个指标逻辑不一致。

问题位置：src/edgelite/drivers/base.py 的 DriverHealthStats 类

修复方案：
1. 统一 effective_state 和 is_healthy 的判断条件：
   is_healthy: consecutive_failures < 5 and read_error_rate < 0.1
   effective_state:
     - CONNECTED: consecutive_failures == 0 and read_error_rate < 0.05
     - DEGRADED: consecutive_failures >= 1 and < 5, or read_error_rate >= 0.05 and < 0.1
     - OFFLINE: consecutive_failures >= 5
2. 移除 effective_state 中多余的 connection_quality_score < 50 检查（与兜底分支重复）
3. 在 health_score 属性注释中说明与 connection_quality_score 的区别：
   - health_score: 综合评分（含延迟和重连次数），用于 UI 展示
   - connection_quality_score: 连接质量评分（仅失败率和连续失败），用于状态判定
4. 添加文档注释说明三者的关系和使用场景

验证方法：编写参数化测试覆盖各种 consecutive_failures 和 read_error_rate 组合。
```

---

### 提示语 13：DRV-004 pymodbus 版本检测改为导入时初始化

```
修复 DRV-004：pymodbus 版本检测的懒初始化存在竞态。

问题位置：src/edgelite/drivers/modbus_tcp.py

修复方案：
1. 将 _SLAVE_KWARG_NAME 的初始化从懒加载改为模块级初始化：
   在模块导入时直接调用 _detect_slave_kwarg_name()
   _SLAVE_KWARG_NAME: str = _detect_slave_kwarg_name()
2. 移除 _slave_kwarg 函数中的懒初始化逻辑
3. 如果担心 pymodbus 在运行时升级（极罕见），添加一个 _refresh_slave_kwarg() 函数供手动调用

验证方法：在多线程环境下并发调用 _slave_kwarg，验证结果一致且无竞态。
```

---

### 提示语 14：ENG-003 事件总线去重缓存改进

```
修复 ENG-003：EventBus 的 event_id 去重缓存在高吞吐场景下可能误删，且 AlarmEvent 的 event_id 可能被错误去重。

问题位置：src/edgelite/engine/event_bus.py

修复方案：
1. 将 _max_dedup_size 从 10000 提升到 50000
2. 修改 AlarmEvent 的 event_id 生成逻辑，加入时间戳避免循环触发被去重：
   在 AlarmEvent.__post_init__ 中：
   if not self.event_id:
       self.event_id = f"alarm:{self.alarm_id}:{self.action}:{int(datetime.now(UTC).timestamp())}"
   注意：这会改变去重语义，仅对同一秒内的重复发布去重
3. 或者：在 EventBus.publish 中对 AlarmEvent 使用独立的去重策略：
   - 仅对 action=firing 的 AlarmEvent 去重（同一 alarm_id 的 firing 只发布一次）
   - action=recovered 不去重（总是发送）
4. 为去重缓存的访问添加 _subscribers_lock 保护（或使用单独的 _dedup_lock）

验证方法：编写测试模拟同一 alarm_id 的 firing → recovered → firing 循环，验证第三次 firing 不被去重。
```

---

### 提示语 15：ENG-004 并发门控迁移逻辑加固

```
修复 ENG-004：CollectScheduler 的并发门控迁移可能丢失采集周期。

问题位置：src/edgelite/engine/scheduler.py 的 set_max_concurrent 方法

修复方案：
1. 在 wake_all_waiters 中，除了设置 limit 和 notify_all 外，记录被唤醒的等待者数量
2. 在旧 gate 上添加 _retired 标志，release 时检查并记录 warning（表示有协程在 gate 退役后仍未释放）
3. 在 _collect_loop 获取信号量后，检查当前 gate 是否已退役（与字典中的 gate 比较），如果不同则使用新 gate 重新获取
4. 为 _ConcurrencyGate 添加 acquire_timeout 参数，防止永久阻塞

验证方法：编写测试在采集高峰期调用 set_max_concurrent，验证无采集任务永久阻塞。
```

---

### 提示语 16：ENG-005 Lifecycle 持久化失败回滚完善

```
修复 ENG-005：DeviceLifecycleManager 持久化失败后的状态回滚不完整。

问题位置：src/edgelite/engine/lifecycle.py

修复方案：
1. 在 on_device_online/on_device_offline 中，如果 _persist_status 失败：
   - 回滚 _status_map（已有）
   - 记录 error 日志包含 device_id 和原始异常
   - 不 re-raise，改为返回 False（调用方不处理此异常）
   - 添加重试机制：最多重试 3 次，间隔 1 秒
2. 如果 _event_bus.publish 失败：
   - 记录 error 日志（已有）
   - 不影响主流程（内存和 DB 已更新）
   - 添加补偿机制：将未发布的事件放入内存队列，下次 publish 时重试
3. close() 方法优化：
   - 添加超时机制：等待 _sqlite_lock 最多 5 秒
   - 超时后强制关闭连接（记录 warning）
   - 避免与 to_thread 操作的跨锁嵌套

验证方法：模拟 SQLite 磁盘满，验证持久化失败后内存状态正确回滚、应用不崩溃。
```

---

### 提示语 17：SEC-002 自定义函数注册安全检查加固

```
修复 SEC-002：表达式引擎的 register_function 闭包检查可被间接调用绕过。

问题位置：src/edgelite/engine/expression_engine.py 的 register_function 方法

修复方案：
1. 添加递归检查：对已注册函数的 co_names 也进行检查，确保不调用已注册的危险函数
2. 对 functools.partial 包装的函数：检查 func 属性的 __code__
3. 对实现了 __call__ 的类实例：检查 __call__ 方法的 __code__
4. 添加注册后的运行时沙箱限制：自定义函数执行时使用与表达式相同的 _SAFE_BUILTINS 命名空间
5. 在 _eval_pool 执行自定义函数时添加超时（3 秒）和内存限制

验证方法：注册一个间接调用 getattr 的函数，验证被拒绝。
```

---

### 提示语 18：SEC-004 WebSocket 首帧认证窗口加固

```
修复 SEC-004：WebSocket 首帧认证窗口允许未认证连接接收广播，且可被用于 DoS。

问题位置：src/edgelite/ws/manager.py

修复方案：
1. 为未认证连接添加超时清理机制：
   - connect(token=None) 后启动 10 秒超时定时器
   - 10 秒内未调用 authenticate()，自动 disconnect 并关闭连接
2. 未认证连接不计入 max_connections 配额：
   - connect 时检查：total = sum(len(conns) for conns in self._connections.values())
   - 分开统计已认证和未认证连接数
   - 仅已认证连接数受 max_connections 限制
   - 未认证连接数限制为 max_connections 的 10%（防 DoS）
3. 在 broadcast 方法中添加 authenticated 检查（如果已有则确认覆盖所有广播路径）

验证方法：建立 100 个未认证 WebSocket 连接，验证 10 秒后被清理；验证已认证连接不受未认证连接影响。
```

---

### 提示语 19：SEC-003 RBAC 权限矩阵调整

```
修复 SEC-003：RBAC 权限矩阵中 OPERATOR 角色缺少部分实际需要的写入权限。

问题位置：src/edgelite/security/rbac.py

修复方案：
1. 为 OPERATOR 角色添加以下权限：
   - Permission.DEVICE_CREATE
   - Permission.DEVICE_UPDATE
   - Permission.RULE_CREATE
   - Permission.RULE_UPDATE
   - Permission.ALARM_ACK  （已有）
2. 不添加以下权限（保持 SoD）：
   - Permission.DEVICE_DELETE （仅 ADMIN）
   - Permission.RULE_DELETE （仅 ADMIN）
   - Permission.USER_CREATE/UPDATE/DELETE （仅 ADMIN）
   - Permission.DEVICE_WRITE_POLICY_EDIT （仅 ADMIN）
3. 将 VIEWER 的 DATA_EXPORT 权限移除，改为需要显式分配（最小权限原则）
4. 更新 RBAC 相关测试和文档

验证方法：运行 RBAC 测试，验证权限矩阵变更后各角色的可操作范围正确。
```

---

### 提示语 20：API-001 错误码体系统一

```
修复 API-001：DriverExceptionMapper 的错误码与 error_codes.py 体系不一致。

问题位置：src/edgelite/drivers/base.py 的 DriverExceptionMapper 类

修复方案：
1. 将 DriverExceptionMapper._EXCEPTION_MAP 中的错误码替换为 error_codes.py 中定义的标准错误码：
   - ERR_NETWORK_CONNECTION_REFUSED → 新增到 error_codes.py 的 NetworkErrors 类
   - ERR_NETWORK_TIMEOUT → 新增到 NetworkErrors 类
   - ERR_NETWORK_DNS_FAILED → 新增到 NetworkErrors 类
   - ERR_NETWORK_HOST_UNREACHABLE → 新增到 NetworkErrors 类
2. 在 error_codes.py 中添加 NetworkErrors 类：
   class NetworkErrors:
       CONNECTION_REFUSED = "ERR_NETWORK_CONNECTION_REFUSED"
       CONNECTION_RESET = "ERR_NETWORK_CONNECTION_RESET"
       TIMEOUT = "ERR_NETWORK_TIMEOUT"
       DNS_FAILED = "ERR_NETWORK_DNS_FAILED"
       HOST_UNREACHABLE = "ERR_NETWORK_HOST_UNREACHABLE"
3. 更新 DriverExceptionMapper.map_exception 使用 NetworkErrors 常量
4. 更新前端 i18n 文件添加对应翻译
5. 区分 ConnectionRefusedError 和 ConnectionResetError（当前映射为相同错误码）

验证方法：触发各种网络异常，验证前端显示正确的 i18n 错误消息。
```

---

### 提示语 21：FE-001 前端 isAuthenticated 判断加固

```
修复 FE-001：isAuthenticated 基于 sessionStorage 中的 username，刷新后可能误判。

问题位置：web/src/stores/auth.ts

修复方案：
1. 添加 isAuthenticatedLoading 标志：
   const isAuthenticatedLoading = ref(true)
   在 fetchUserInfo 完成后设为 false
2. 修改路由守卫：
   - 如果 isAuthenticatedLoading 为 true，显示 loading 页面并等待
   - fetchUserInfo 完成后根据结果决定跳转
3. 修改 isAuthenticated 计算属性：
   - 同时检查 username 和一个运行时认证标志
   - const isRuntimeAuthenticated = ref(false)
   - login 成功后设为 true，logout 或 401 后设为 false
   - 刷新时初始为 false，fetchUserInfo 成功后设为 true
4. 页面刷新时的流程：
   - isAuthenticatedLoading = true
   - 尝试调用 /auth/me 验证 Cookie 有效性
   - 成功：isRuntimeAuthenticated = true, isAuthenticatedLoading = false
   - 失败（401）：isRuntimeAuthenticated = false, isAuthenticatedLoading = false, 清空 username

验证方法：刷新页面时不应短暂显示已认证内容；Cookie 过期时直接跳转登录页。
```

---

### 提示语 22：STORE-002 SQLite WAL Checkpoint 策略

```
修复 STORE-002：SQLite WAL 模式下的 checkpoint 策略不明确。

问题位置：src/edgelite/storage/sqlite_pragmas.py

修复方案：
1. 在 apply_standard_pragmas 中显式设置 wal_autocheckpoint：
   conn.execute("PRAGMA wal_autocheckpoint = 500")  # 每 500 页自动 checkpoint
2. 添加定期手动 checkpoint 机制：
   - 在应用启动时创建后台任务，每 5 分钟执行一次 PRAGMA wal_checkpoint(TRUNCATE)
   - 检查 WAL 文件大小，超过 50MB 时触发强制 checkpoint
3. 在 check_and_convert_to_wal 中添加失败处理：
   - 如果 WAL 模式切换失败（如只读文件系统），记录 warning 并回退到 DELETE 模式
   - 在日志中明确说明降级原因和影响
4. 为所有 SQLite 数据库文件添加统一的 PRAGMA 配置文档

验证方法：高频写入后检查 WAL 文件大小不超过 50MB；Docker 只读文件系统下验证降级行为。
```

---

### 提示语 23：STORE-003 设备级状态统一清理

```
修复 STORE-003：设备级状态字典清理分散在多处，容易遗漏。

问题位置：多文件

修复方案：
1. 在 EventBus 中添加 "device_removed" 事件类型：
   @dataclass
   class DeviceRemovedEvent(Event):
       device_id: str = ""
2. 在设备移除流程中发布 DeviceRemovedEvent：
   device_service.remove_device → 发布 DeviceRemovedEvent
3. 各组件注册 DeviceRemovedEvent handler，在 handler 中清理自身状态：
   - CollectScheduler: 停止采集任务
   - RuleEvaluator: 清理相关缓存
   - DriverPlugin: 调用 reset_health_stats
   - DeviceLifecycleManager: 删除状态记录
4. 这样新增组件时只需注册 handler，不需要修改设备移除流程

验证方法：添加一个 mock 组件注册 handler，移除设备后验证 handler 被调用且状态被清理。
```

---

### 提示语 24：ARCH-003 设备状态管理重构

```
修复 ARCH-003：驱动层状态字典膨胀，缺乏统一生命周期管理。

问题位置：src/edgelite/drivers/modbus_tcp.py 和 src/edgelite/drivers/base.py

修复方案：
1. 创建 DeviceStateContext 数据类，封装设备级运行时状态：
   @dataclass
   class DeviceStateContext:
       device_id: str
       config: dict
       points: list[dict]
       pool_key: str | None = None
       retry_count: int = 0
       conn_state: str = "disconnected"
       active_host: str = ""
       primary_fail_count: int = 0
       degrade_level: int = 0
       watchdog_fail_count: int = 0
       # ... 其他设备级状态
2. 在 ModbusTcpDriver 中用 dict[str, DeviceStateContext] 替代 30+ 个独立字典
3. cleanup(device_id) 方法只需删除一个字典条目
4. reset_health_stats 调用 ctx.cleanup() 即可完成所有状态清理
5. 逐步迁移，先创建 DeviceStateContext 类，然后逐个替换字典引用

注意：这是一个较大的重构，建议分多个 PR 进行。先创建类和基础设施，再逐步迁移。

验证方法：确保所有现有测试通过；设备增删 1000 次后内存不增长。
```

---

## 第三优先级：中期优化（🟢低 + 其他改进）

---

### 提示语 25：OPS-001 日志统一英文

```
修复 OPS-001：系统中仍有部分日志使用中文。

问题位置：全项目搜索

修复方案：
1. 搜索所有 logger 调用中的中文字符串：
   grep -rn 'logger\.\(info\|warning\|error\|debug\|critical\).*[\x{4e00}-\x{9fff}]' src/edgelite/
2. 将所有中文日志翻译为英文
3. 重点检查以下文件（已知有中文日志）：
   - src/edgelite/engine/lifecycle.py
   - src/edgelite/engine/scheduler.py
   - src/edgelite/ws/manager.py
4. 保持日志格式一致：logger.level("[module] message: %s", value)

验证方法：grep 搜索确认无中文日志残留。
```

---

### 提示语 26：OPS-002 Docker 只读 FS 与 SQLite 兼容

```
修复 OPS-002：Docker 容器中只读文件系统与 SQLite WAL 模式可能冲突。

问题位置：docker/docker-compose.yml, docker/entrypoint.sh

修复方案：
1. 在 docker-compose.yml 中为所有 SQLite 数据库路径添加 tmpfs 或 volume：
   volumes:
     - edgelite_data:/app/data
   或
   tmpfs:
     - /app/data:mode=1777,size=100M
2. 在 entrypoint.sh 中检查 data 目录是否可写：
   if ! touch data/.write_test 2>/dev/null; then
       echo "Warning: data directory is not writable, SQLite will use in-memory mode"
       export EDGELITE_SQLITE_PATH=:memory:
   fi
   rm -f data/.write_test
3. 在 check_and_convert_to_wal 中添加只读检测：
   try:
       conn.execute("PRAGMA journal_mode=WAL")
   except sqlite3.OperationalError:
       logger.warning("Cannot enable WAL mode (read-only filesystem?), falling back to DELETE mode")
       conn.execute("PRAGMA journal_mode=DELETE")
4. 在文档中说明 Docker 部署时数据目录的配置要求

验证方法：在只读 Docker 容器中启动应用，验证不崩溃且日志有降级提示。
```

---

### 提示语 27：OPS-004 配置热加载一致性

```
修复 OPS-004：配置热加载期间各组件行为不一致。

问题位置：src/edgelite/config_reload.py 及各组件

修复方案：
1. 为配置引入版本号（epoch）机制：
   config_epoch = 0  # 每次热加载递增
2. 各组件在 start 时记录当前 config_epoch
3. 定期检查 config_epoch 是否变化，变化时触发重新初始化
4. 添加配置变更通知事件：
   ConfigChangedEvent(old_epoch, new_epoch, changed_keys: list[str])
5. 各组件注册 ConfigChangedEvent handler，仅处理与自己相关的配置变更
6. 添加配置加载状态端点 /api/v1/system/config-status：
   返回当前 epoch、各组件是否已应用最新配置

验证方法：修改配置文件，验证所有组件在 10 秒内应用新配置。
```

---

### 提示语 28：FE-002 Token 刷新竞争修复

```
修复 FE-002：Token 刷新失败后的重试窗口可能产生竞争。

问题位置：web/src/api/http.ts

修复方案：
1. 将 refreshFailed 的重置与 isRefreshing 检查合并为原子操作：
   if (refreshFailed) {
       if (Date.now() - refreshFailedTime > 10000) {
           refreshFailed = false
           refreshFailedTime = 0
       } else {
           refreshFailed = false
           auth.logout()
           redirectToLogin()
           return Promise.reject(error)
       }
   }
   // 紧接着检查 isRefreshing，中间不 await
   if (isRefreshing) {
       return new Promise(...)
   }
2. 将 refreshSubscribers 超时从 10 秒缩短到 5 秒（与 refreshFailed 重置时间错开）
3. 添加 isRefreshing 的超时保护：如果 isRefreshing 超过 15 秒未重置，强制重置

验证方法：模拟 refresh 请求耗时 8 秒，验证期间排队的请求不会重复触发 refresh。
```

---

### 提示语 29：FE-003 空闲监听性能优化

```
修复 FE-003：空闲会话超时监听的事件处理可能影响性能。

问题位置：web/src/stores/auth.ts

修复方案：
1. 为 mousemove 和 scroll 事件添加节流：
   let lastResetTime = 0
   const THROTTLE_MS = 5000  // 5 秒内最多触发一次 resetIdleTimer
   
   function throttledReset() {
       const now = Date.now()
       if (now - lastResetTime >= THROTTLE_MS) {
           lastResetTime = now
           resetIdleTimer()
       }
   }
   
   // mousemove 和 scroll 使用 throttledReset
   // keydown 和 click 仍然使用 resetIdleTimer（低频事件）
2. 使用 passive 事件监听器：
   window.addEventListener('mousemove', throttledReset, { passive: true })
   window.addEventListener('scroll', throttledReset, { passive: true })
3. 添加 startIdleWatch 的幂等性检查：
   if (idleWatchStarted) return
   idleWatchStarted = true

验证方法：在 SCADA 页面快速移动鼠标，验证 CPU 使用率不显著升高。
```

---

### 提示语 30：FE-004 CSRF Token 安全说明

```
修复 FE-004：CSRF Token 的 base64 编码提供虚假安全感。

问题位置：web/src/stores/auth.ts

修复方案：
1. 移除 _encode 和 _decode 函数，直接存储 CSRF token 原文
2. 添加注释说明 CSRF token 的安全模型：
   // CSRF token 安全说明：
   // CSRF token 存储在 sessionStorage 中，同源 JavaScript 可读。
   // 这是 CSRF 保护的预期行为——CSRF token 仅防止跨站请求伪造（CSRF），
   // 不防止同源 XSS。如果 XSS 已发生，攻击者可直接调用 API，无需 CSRF token。
   // XSS 防护由 Content-Security-Policy 头和输入消毒负责。
3. 简化 _getItem 和 _setItem 为直接调用 sessionStorage

验证方法：验证 CSRF token 正常读写、CSRF 保护机制正常工作。
```

---

### 提示语 31：API-002 错误码整理与 i18n 补全

```
修复 API-002：错误码数量过多，命名不一致，i18n 可能不完整。

问题位置：src/edgelite/api/error_codes.py, web/src/i18n/zh-CN.ts, web/src/i18n/en-US.ts

修复方案：
1. 编写脚本扫描 error_codes.py 中所有错误码常量
2. 与前端 i18n 文件对比，找出缺失的翻译
3. 为所有缺失的错误码添加中英文翻译
4. 统一错误码命名规范为 ERR_{MODULE}_{ACTION}_{REASON}：
   - 状态类使用 ERR_{MODULE}_{STATE}（如 ERR_DEVICE_OFFLINE）
   - 失败类使用 ERR_{MODULE}_{ACTION}_FAILED（如 ERR_DEVICE_CREATE_FAILED）
   - 在 error_codes.py 文件头部添加命名规范文档
5. 考虑将字符串常量改为 StrEnum（Python 3.11+），获得类型安全和 IDE 自动补全

验证方法：编写测试确保所有错误码在 i18n 文件中都有对应翻译。
```

---

### 提示语 32：API-003 409 Conflict 处理增强

```
修复 API-003：前端 409 Conflict 处理过于简单。

问题位置：web/src/api/http.ts

修复方案：
1. 从 409 响应体中提取冲突详情：
   - 冲突字段名
   - 当前值与期望值
   - 冲突类型（乐观锁/资源已存在/状态冲突）
2. 扩展 conflictMessage 为结构化对象：
   error.conflictDetail = {
     type: respData?.error_code || 'unknown',
     field: respData?.detail?.field,
     expected: respData?.detail?.expected,
     current: respData?.detail?.current,
     message: errorMessage
   }
3. 为乐观锁冲突添加自动重试逻辑（最多 1 次）：
   - 收到 409 后重新 GET 最新数据
   - 提示用户数据已被修改，是否要覆盖
4. 在 UI 层提供友好的冲突解决组件

验证方法：模拟乐观锁冲突场景，验证前端展示冲突详情而非通用错误消息。
```

---

## 使用指南

### 如何使用本文档

1. **按优先级顺序处理**：从第一优先级开始，每完成一个提示语后运行测试验证
2. **每次只处理一个提示语**：不要并行处理多个修复，避免冲突
3. **粘贴全局上下文提示**：每次新对话开头粘贴"全局上下文提示"段落
4. **验证后继续**：每个修复完成后，运行 `pytest` 和 `ruff check` 确保无回归
5. **记录修复状态**：在提示语前添加 ✅ 或 ❌ 标记完成/失败状态

### 预估工作量

| 优先级 | 提示语数量 | 预估总工时 | 建议完成周期 |
|--------|-----------|-----------|-------------|
| 第一优先级 | 10 | 40-60 小时 | 2 周 |
| 第二优先级 | 14 | 30-50 小时 | 3 周 |
| 第三优先级 | 8 | 15-25 小时 | 2 周 |
| **合计** | **32** | **85-135 小时** | **5-7 周** |

### 注意事项

- 提示语 24（ARCH-003 设备状态管理重构）是最大的重构，建议拆分为 3-5 个 PR
- 提示语 8（STORE-001 InfluxDB 降级同步）涉及数据库 schema 变更，需要编写 Alembic 迁移脚本
- 提示语 1（ARCH-001 锁层级治理）影响面最广，建议在专门的分支上开发并充分测试
- 所有涉及安全模块的修复（SEC-001 到 SEC-005）建议进行安全代码审计

---

> **生成日期**：2026-07-02  
> **基于报告**：SYSTEM_ANALYSIS_REPORT.md  
> **文档版本**：1.0
