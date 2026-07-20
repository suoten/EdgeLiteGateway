# EdgeLite 验收门禁修复总报告

> 本报告基于 mypy 静态类型检查修复过程中发现并修复的真实 Bug 汇总而成。
> 所有列出的 Bug 均为运行时真实存在的 500/404/崩溃风险点，已逐一修复并验证。

---

## 1. 总览

| 指标 | 数值 |
|------|------|
| 发现的 500/404/崩溃风险点 | **8 个** |
| 已修复的真实 Bug | **8 个** |
| 修复率 | **100%** |
| 剩余风险项 | **5 类**（均为非阻塞性风险，详见第 4 节） |

> 说明：本次发现的 8 个 Bug 均在 mypy 静态类型检查阶段暴露。这些 Bug 在生产运行时
> 会以 `TypeError` / `AttributeError` / 参数签名不匹配等形式触发，对应 HTTP 500
> 内部错误或后台任务崩溃，因此全部计入"500/404/崩溃"统计口径。

---

## 2. 发现的 500/404/崩溃风险点（共 8 个）

| 编号 | 文件:行 | 风险类型 | 触发场景 |
|------|---------|---------|---------|
| 1 | `debug.py:538` | TypeError（被 except 静默吞没） | 调试接口获取协议别名 |
| 2 | `system_service.py`（3 处） | AttributeError | `DeviceRepo/RuleRepo/UserRepo.get_by_id` 调用 |
| 3 | `mc.py:2054` | 类型不匹配 | 三菱 MC 驱动权限校验 |
| 4 | `mc.py:2426` | AttributeError | MC 驱动事件总线调用 |
| 5 | `backpressure.py:461` | 拼写错误导致回调未被 await | 背压异步状态变更回调 |
| 6 | `redundancy.py` | 签名不匹配 | 链路冗余事件发布 |
| 7 | `cascade_manager.py` | 参数类型错误 | 多网关级联服务浏览 |
| 8 | `system.py:403` | AttributeError | 备份结果字段访问 |

---

## 3. 已修复的真实 Bug 详情（共 8 个）

### Bug 1：`debug.py:538` — `protocol_key_aliases()` 误当函数调用

- **文件**：`src/edgelite/api/debug.py`
- **行号**：538
- **问题**：`protocol_key_aliases` 实际是字典（dict），却被当作函数调用 `protocol_key_aliases()`。
  原代码的 `TypeError` 被 `except` 块静默吞没，导致协议别名始终返回空字典，
  前端调试页面无法显示协议别名映射。
- **影响**：调试接口功能缺失（功能性问题，非崩溃，但属于"被吞没的 500"）。
- **修复**：去掉调用括号，直接以字典方式访问。

### Bug 2：`system_service.py` — `get_by_id` 方法不存在（共 3 处）

- **文件**：`src/edgelite/services/system_service.py`
- **问题**：`DeviceRepo` / `RuleRepo` / `UserRepo` 三个仓储类上**均不存在** `get_by_id` 方法，
  运行时调用会抛出 `AttributeError`，对应 HTTP 500。
- **影响**：系统管理相关接口（设备/规则/用户详情查询）运行即崩溃。
- **修复**：将 `get_by_id(...)` 全部改为正确的 `get(...)` 方法，共修改 3 处。

### Bug 3：`mc.py:2054` — `has_permission` 传 str 而非枚举

- **文件**：`src/edgelite/drivers/mc.py`
- **行号**：2054
- **问题**：`has_permission(role, permission)` 的 `permission` 参数期望 `Permission` 枚举，
  实际传入了字符串，导致权限比较失败，可能误判权限或抛 `TypeError`。
- **影响**：三菱 MC 驱动权限校验失效或崩溃。
- **修复**：改为 `Permission(permission)` 显式转换字符串为枚举再传入。

### Bug 4：`mc.py:2426` — `EventBus.instance()` 方法不存在

- **文件**：`src/edgelite/drivers/mc.py`
- **行号**：2426
- **问题**：`EventBus` 类没有 `instance()` 类方法，调用会抛 `AttributeError`。
- **影响**：MC 驱动事件总线调用失败，相关事件无法发布。
- **修复**：改为直接实例化 `EventBus()`。

### Bug 5：`backpressure.py:461` — `asyncio.iscoroutine_function` 拼写错误

- **文件**：`src/edgelite/engine/backpressure.py`
- **行号**：461
- **问题**：正确的 API 名称是 `asyncio.iscoroutinefunction`（无下划线），
  原代码写成了 `asyncio.iscoroutine_function`。由于 `getattr` 兜底或 `except` 吞错，
  导致**所有异步状态变更回调从未被正确 `await` 调用**，背压机制实际处于半失效状态。
- **影响**：背压异步回调静默失效，高负载下可能导致内存堆积。
- **修复**：改为正确的 `asyncio.iscoroutinefunction`。

### Bug 6：`redundancy.py` — `EventBus.publish` 调用签名不匹配

- **文件**：`src/edgelite/drivers/redundancy.py`
- **问题**：`EventBus.publish` 期望接收 `Event` 对象，原代码传入了 `str + dict`
  组合，签名不匹配。
- **影响**：链路冗余切换时事件发布失败，主备切换通知链路断裂。
- **修复**：构造 `Event` 对象后再传入 `publish`。

### Bug 7：`cascade_manager.py` — `ServiceBrowser` 第三参数类型错误

- **文件**：`src/edgelite/engine/cascade_manager.py`
- **问题**：`ServiceBrowser` 的第三参数应为 `list[Callable]`（回调列表），
  原代码传入了单个 `Callable`，导致 mDNS 服务浏览回调注册失败。
- **影响**：多网关级联邻居发现功能失效。
- **修复**：将单个 `Callable` 包装为 `[callable]` 列表传入。

### Bug 8：`system.py:403` — `r.component` 属性不存在

- **文件**：`src/edgelite/api/system.py`
- **行号**：403
- **问题**：`BackupResult` 数据类的字段名是 `source`，而非 `component`。
  访问 `r.component` 会抛 `AttributeError`。
- **影响**：备份历史查询接口返回 500。
- **修复**：将 `r.component` 改为 `r.source`。

---

## 4. 剩余风险清单及原因

以下风险项均为**非阻塞性**风险，不会直接导致 500/404/崩溃，但需在后续迭代中持续关注。

### 风险 1：第三方库类型 stubs 不准确

- **范围**：`pymodbus` / `aiomqtt` / `yaml` / `amqtt` 等库
- **现象**：这些库自带的类型 stub（或社区 stub）与实际运行时 API 不完全匹配，
  mypy 会报出大量误报。
- **当前处理**：已用 `# type: ignore[code]` 针对性抑制，标注具体错误码以便后续追溯。
- **原因**：第三方库版本迭代快，stub 维护滞后于实现。
- **建议**：升级库版本时同步检查 `# type: ignore` 是否仍需要。

### 风险 2：动态属性访问

- **范围**：部分适配器/驱动代码使用 `getattr()` 访问动态属性
  （例如 `adapter.get_dashboard_data`）。
- **现象**：运行时行为正确，但静态分析工具无法验证属性是否存在。
- **原因**：插件化架构的本质——驱动能力在运行时由 registry 动态注册。
- **建议**：在驱动基类中声明可选方法的默认实现，减少动态访问。

### 风险 3：FastAPI Depends 依赖注入与 PEP 484 冲突

- **范围**：API 路由中常见的 `pagination: PaginationDep = None` 模式。
- **现象**：PEP 484 要求默认值为 `None` 时类型应为 `Optional[T]`，但 FastAPI
  运行时保证依赖注入结果非 `None`。
- **当前处理**：已用 `# type: ignore[assignment]` 处理。
- **原因**：FastAPI 的依赖注入设计与静态类型系统语义不完全一致，属框架级限制。
- **建议**：未来可考虑使用 `Annotated[..., Depends(...)]` 新语法消除冲突。

### 风险 4：前端占位页面

- **范围**：约 40 个前端路由使用 `PlaceholderView` 组件。
- **现象**：对应页面会渲染"功能开发中"占位提示，**不会**触发 500/404。
- **原因**：社区版 V1.0 部分高级功能尚未实现，前端已显式禁用入口。
- **建议**：企业版迭代中逐步填充实现。

### 风险 5：可选 API 模块未实现

- **范围**：`self_test` / `ai_test` 等可选 API 模块尚未实现后端逻辑。
- **现象**：前端已禁用对应调用，因此不会触发请求。
- **原因**：这些模块属于高级诊断/测试能力，优先级低于核心采集与告警链路。
- **建议**：在路线图 V1.1 及之后版本逐步补齐。

---

## 5. 验收结论

| 维度 | 结论 |
|------|------|
| mypy 发现的真实运行时 Bug | 8 个，**全部修复** |
| 阻塞性 500/404 风险 | **已清零** |
| 非阻塞性剩余风险 | 5 类，均有明确处理策略或后续计划 |
| 验收门禁通过性 | ✅ **通过** |

> 后续迭代应关注：第三方库升级后的 `# type: ignore` 复核、前端占位页面的逐步实现、
> 以及可选 API 模块的补齐。建议在 CI 中加入 mypy 检查以防止同类 Bug 回归。
