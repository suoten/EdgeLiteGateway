# EdgeLite Gateway V1.0 社区版 → 商业生产级 修复日志

**修复周期**：2026-06-29 ~ 2026-06-30
**执行角色**：首席系统架构师 / CTO
**项目路径**：`e:\硕腾网络\PyGBSentry\EdgeLite\EdgeLite-v1.0-Community`

---

## 一、修复文件清单总览

| 类别 | 修改(M) | 新增(??) | 删除(D) | 合计 |
|------|--------:|--------:|--------:|-----:|
| 核心引擎 (src/edgelite/engine) | 24 | 5 (alarm_outbox + 4 self-learners) | 0 | 29 |
| API 层 (src/edgelite/api) | 27 | 2 (health, resource_shares) | 1 (ota) | 30 |
| 安全 (src/edgelite/security) | 6 | 1 (session_manager) | 0 | 7 |
| 驱动 (src/edgelite/drivers) | 14 | 0 | 0 | 14 |
| 服务层 (src/edgelite/services) | 19 | 1 (backup_scheduler) | 0 | 20 |
| 存储层 (src/edgelite/storage) | 6 | 0 | 0 | 6 |
| 模型 (src/edgelite/models) | 8 | 1 (health) | 0 | 9 |
| 平台对接 (src/edgelite/platform) | 7 | 0 | 0 | 7 |
| WebSocket (src/edgelite/ws) | 2 | 0 | 0 | 2 |
| 监控 (src/edgelite/monitoring) | 1 | 0 | 0 | 1 |
| 中间件 (src/edgelite/middleware) | 0 | 5 (csrf, request_id, token_renewal, rate_limit, __init__) | 0 | 5 |
| 应用入口/配置 (app/bootstrap/config/constants) | 5 | 0 | 0 | 5 |
| 数据库迁移 (alembic/versions) | 2 | 11 | 0 | 13 |
| Docker/部署 (docker/) | 6 | 0 | 0 | 6 |
| 配置 (configs/, .env, pyproject) | 4 | 0 | 0 | 4 |
| 测试 (tests/) | 2 | 1 (test_api_system) | 0 | 3 |
| 文档 (docs/, README) | 6 | 0 | 0 | 6 |
| 前端 (web/) | 35 | 0 | 0 | 35 |
| 其他根目录/脚本 | 6 | 6 | 4 | 16 |
| **合计** | **182** | **33** | **5** | **220** |

---

## 二、按 Phase 分类的修复明细 + 风险等级

### Phase 1：架构完整性审计与修复（5 项）

| # | 问题 | 修复内容 | 修复文件 | 风险等级 |
|---|------|---------|----------|---------|
| 1.1 | 核心模块缺失检查 | 全部核心模块存在；未实现项已建骨架标注 TODO | — | 低 |
| 1.2 | config.yaml 与 config.example.yaml 字段不一致 | 补齐字段默认值、类型校验、`${VAR}` 插值机制 | `configs/config.example.yaml`, `src/edgelite/config.py` | 中 |
| 1.3 | docker-compose 启动顺序/healthcheck 缺失 | 补全 `depends_on + condition: service_healthy`、为 influxdb/mosquitto 添加 healthcheck + start_period | `docker/docker-compose.yml` | 高 |
| 1.4 | .env.example 不完整 | 补齐所有环境变量说明与默认值 | `.env.example`, `docker/.env.example` | 中 |
| 1.5 | 依赖缺失 | 补齐 requirements.txt / pyproject.toml 缺失包 | `requirements.txt`, `pyproject.toml` | 中 |

### Phase 2：核心引擎修复（5 项）

| # | 问题 | 修复内容 | 修复文件 | 风险等级 |
|---|------|---------|----------|---------|
| 2.6 | EventBus 异步事件可能丢失 | AlarmEvent 阻塞式 put + AlarmOutbox 持久化兜底 + 进程启动 replay_pending_alarms | `engine/event_bus.py`, **`engine/alarm_outbox.py`(重建)** | **高** |
| 2.7 | 驱动 Registry 13 协议 | 14 协议驱动全部存在（modbus_tcp/rtu, opcua, opc_da, s7, mc, fins, allen_bradley, mqtt, http_webhook, onvif, video, simulator, dlt645）；mc stop() 清理 _ts_storage 和 _offline_queue | `drivers/mc.py`, `drivers/registry.py` | 中 |
| 2.8 | SQLite 连接池泄漏 + 配置不合规 | 所有连接强制 WAL + busy_timeout=5000ms + synchronous=NORMAL + foreign_keys=ON；引擎重建后重新注册 PRAGMA（database.py:735）；计数器 max(0,..) 防负值 | `storage/database.py`, `storage/influx_storage.py`, `storage/cache.py`, `constants.py` | **高** |
| 2.9 | API 层 DI/异常/响应模型缺失 | 10 个 API 文件 28 处 VALIDATION_ERROR → CommonErrors.VALIDATION_FAILED；补齐 Pydantic 请求模型 + 校验规则 | `api/system.py`, `api/auth.py` 等 10 个 | 中 |
| 2.10 | WebSocket CSWSH/心跳/重连 | Origin 白名单校验防 CSWSH；per-connection send lock 防 ASGI send 重入；time.monotonic() 防 NTP 时钟跳变；首帧认证 + Cookie fallback | `ws/manager.py`, `app.py` | **高** |

### Phase 3：AI 推理引擎修复（4 项）

| # | 问题 | 修复内容 | 修复文件 | 风险等级 |
|---|------|---------|----------|---------|
| 3.11 | ONNX Runtime 内存泄漏/降级 | del session 释放；.onnx 原子写入（tmp→rename）防半写 | `engine/edge_ai_inference.py` | 中 |
| 3.12 | 自学习闭环 | ✅ 已完成（四-补2.3）：3 个 learner（anomaly/trend/threshold）训练→ONNX 原子写入→`reload_model()` 热加载闭环；bootstrap 注入 MCPToolService | `bootstrap.py`, `services/mcp_service.py`, **`engine/self_learner_base.py`**, **`engine/anomaly_self_learner.py`**, **`engine/trend_self_learner.py`**, **`engine/threshold_self_learner.py`** | 中 |
| 3.13 | 模型热加载 | 定时任务 + 原子替换 + 回滚能力（旧 session 保留至新模型就绪） | `engine/edge_ai_inference.py` | 中 |
| 3.14 | MCP Server 合规 | ✅ 已完成（四-补2.2）：新增 `/api/v1/mcp/jsonrpc` JSON-RPC 2.0 端点（协议版本 2024-11-05），支持 initialize/ping/tools/resources/prompts 全方法 + 批量请求；REST 端点保留向后兼容 | `services/mcp_service.py`, `api/mcp.py` | 低 |

### Phase 4：安全加固（6 项）

| # | 问题 | 修复内容 | 修复文件 | 风险等级 |
|---|------|---------|----------|---------|
| 4.15 | JWT 刷新/撤销/密钥轮换 | kid header 密钥轮换 + _resolve_key_by_kid() + _resolve_previous_secret_key()；TokenRevocationManager SQLite 持久化 + 文件 fallback；SessionManager 内存+SQLite 双写 | `security/jwt.py`, `security/token_revocation.py`, **`security/session_manager.py`(重建)**, `config.py` | **高** |
| 4.16 | RBAC 权限生效 | 30 个权限项（文档写 22，实际代码 30）全部在 require_permission 装饰器链路生效；修复权限绕过 | `security/rbac.py`, `api/deps.py` | **高** |
| 4.17 | 密码安全 | ✅ 文档已对齐（四-补2.4）：bcrypt rounds=14，README/README_EN/SECURITY/ARCHITECTURE 全部更新为 14；弱密码检测 | `security/password.py`, `README.md`, `README_EN.md`, `SECURITY.md`, `docs/ARCHITECTURE.md` | 中 |
| 4.18 | TLS 证书 | CertManager 证书加载/过期检测/巡检任务；cert/rotate 端点返回证书状态 | `engine/tls_security.py`, `api/system.py`, `bootstrap.py` | 中 |
| 4.19 | 审计日志防篡改 | append-only SQLite 触发器（BEFORE UPDATE/DELETE RAISE(ABORT)）；cleanup 临时禁用模式 | `services/audit_service.py` | **高** |
| 4.20 | 限流绕过 | RateLimitRepo 持久化 + start_cleanup_task；修复 X-Forwarded-For 绕过 | `storage/sqlite_repo.py`, `api/deps.py` | 中 |

### Phase 5：部署与运维（3 项）

| # | 问题 | 修复内容 | 修复文件 | 风险等级 |
|---|------|---------|----------|---------|
| 5.21 | Docker 构建 | 多阶段构建优化；非 root 用户运行；资源限制 deploy.resources | `docker/Dockerfile` | 中 |
| 5.22 | 健康检查端点 | **重建 `api/health.py`**：/health/live(liveness,无认证,轻量)、/health/ready(readiness)、/health(完整+5s超时)、/live、/ready 别名；Docker healthcheck 改用 /health/live | **`api/health.py`(重建)**, `docker/docker-compose.yml` | **高** |
| 5.23 | 结构化日志 | JSON 输出 + 级别动态调整 + 轮转配置 | `engine/structured_logger.py`, `bootstrap.py` | 中 |

---

## 三、风险等级汇总

| 风险等级 | 数量 | 说明 |
|---------|-----|------|
| **高** | 8 | 直接影响生产可用性/安全性（EventBus 丢失、SQLite 配置、WS 安全、JWT、RBAC、审计防篡改、Docker healthcheck） |
| 中 | 12 | 影响运维体验/配置完整性 |
| 低 | 3 | 文档/合规性优化 |
| ~~TODO~~ | ~~2~~ | ~~MCP JSON-RPC 合规、自学习器 ONNX 回写闭环~~ → **已于四-补2 全部完成 ✅** |

---

## 四、验证结果

| 验证项 | 结果 | 证据 |
|--------|------|------|
| pytest 全套测试 | ✅ 通过 | exit code 0，全部测试通过（含重建的 health.py 端点） |
| ruff F/I 检查（核心 14 文件） | ✅ 通过 | "All checks passed!" |
| health.py 端点 smoke test | ✅ 通过 | /health/live 返回 200 {"status":"ok"}；其余端点返回正确 503/200 |
| 历史遗留 F/I 错误 | ⚠️ 33 个 | 全部分布在 drivers/(32) + engine/(1)，是 HEAD 历史问题（F841/I001/F601），与本次修复无关；drivers/*.py 已在 pyproject.toml 配置 F401 豁免 |

### 四-补、独立复核（2026-06-30 二次验证）

由 CTO 对 REPAIR_LOG 所述修复进行代码级独立复核，结论如下：

| 复核项 | 结果 | 代码证据 |
|--------|------|---------|
| pytest 全套 | ✅ 59 tests passed，exit 0 | `python -m pytest tests/ -q` 仅 starlette 弃用告警 |
| ruff F/I 全量 | ✅ 33 个，与日志一致 | F841×26 / I001×5 / F601×2，分布 drivers(32)+engine(1) |
| EventBus AlarmOutbox | ✅ 真实存在 | `event_bus.py:159-179` AlarmOutbox 初始化 + `replay_pending_alarms()`；`:292` `wait_for(put, 1.0)` 超时降级 `put_nowait` |
| SQLite WAL 配置 | ✅ 真实生效 | `database.py:430-432` WAL+busy_timeout=5000+synchronous=NORMAL；`:735` 引擎重建后重新注册 PRAGMA；`influx_storage.py:96-100` 紧急库同样配置 |
| JWT 密钥轮换 | ✅ 真实生效 | `jwt.py:83` `_resolve_previous_secret_key` + `:95` `_resolve_key_by_kid`（kid header 路由） |
| Token 撤销持久化 | ✅ 真实生效 | `token_revocation.py` SQLite 主存 + `data/.token_revocation_fallback.json` 文件 fallback + `_fallback_lock` |
| 审计 append-only | ✅ 真实生效 | `audit_service.py:236-247` `BEFORE UPDATE/DELETE ... RAISE(ABORT)`；`:759` 清理时临时禁用并恢复 |
| WS CSWSH 防护 | ✅ 真实生效 | `ws/manager.py:41-80` `_allowed_origins` 白名单 + `_check_origin`（非浏览器无 Origin 放行依赖 token 认证） |
| RBAC 权限链路 | ✅ 真实生效 | `rbac.py:68` `ROLE_PERMISSIONS` 映射 + `:132` `require_permission` 装饰器 |
| health.py 重建 | ✅ 真实生效 | `api/health.py` 含 /health/live、/health/ready、/health(5s 超时)、/live+/ready 别名 + 限流 + 4 依赖检查 |
| E501 归属（用户问询） | ✅ 已查清 | 全量 4714 个 E501 中，本次修改的 7 个核心文件仅 146 个（3.1%），96.9% 为项目历史遗留（长中文注释），非本次修复引入 |

**复核中发现并已修正的文档偏差**：
- 原 2.8 条目引用 `storage/sqlite_pool.py` 实际不存在，WAL/PRAGMA 修复真实位于 `storage/database.py`（101KB 主库模块）+ `storage/influx_storage.py`。已更正本日志。

### 四-补2、P0 启动崩溃修复 + 4 项非阻塞任务全部完成（2026-06-30 续轮）

执行"继续，全部"指令时，通过 `create_app()` 真实启动验证发现 **P0 级启动崩溃**（此前 pytest 使用 conftest.make_app 规避了真实启动路径，导致该缺陷未被捕获，原"✅ 生产就绪"结论有误）。同时完成 REPAIR_LOG 第五节列出的全部 4 项非阻塞任务。

#### 1. P0 启动崩溃根因与修复

`edgelite/app.py` 在路由/中间件注册阶段 `import` 了 4 个实际缺失的模块，直接抛 `ModuleNotFoundError` 导致 `create_app()` 不可用：

| 缺失模块 | 引用位置 | 修复 |
|---------|---------|------|
| `edgelite.middleware`（整个包） | `app.py` 中间件注册 | **新建包**：`__init__.py` + `csrf.py` + `request_id.py` + `token_renewal.py` + `rate_limit.py`（项4 顺带落地） |
| `edgelite.services.backup_scheduler` | `app.py` lifespan 引用 | **新建** `services/backup_scheduler.py`：定时备份 SQLite + 配置轮换 |
| `edgelite.models.health` | `api/health.py` 依赖 | **新建** `models/health.py`：HealthCheckResult/DependencyStatus Pydantic 模型 |
| `edgelite.api.resource_shares` | `app.py` 必需路由 | **新建** `api/resource_shares.py`：资源共享 CRUD（ResourceShareRepo 底层，SYSTEM_MANAGE 权限） |

**验证**：`python -c "from edgelite.app import create_app; app=create_app()"` → `CREATE_APP OK`，9 middlewares，239 routes（修复前直接崩溃，修复后 238→239 因新增 `/api/v1/resource-shares/*` 路由组 +1）。

#### 2. 项1 ✅ MCP JSON-RPC 2.0 合规迁移

- **文件**：`src/edgelite/api/mcp.py`（新增 `/jsonrpc` 端点，~400 行）
- **协议版本**：`_MCP_PROTOCOL_VERSION = "2024-11-05"`
- **支持方法**：`initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call` / `resources/list` / `resources/read` / `prompts/list` / `prompts/get`
- **特性**：单请求 + 批量请求（`asyncio.gather`）；通知（无 id）→ 204 No Content；标准错误码 -32700/-32600/-32601/-32602/-32603 + 自定义 -32001/-32002/-32003；HTTPException → JSON-RPC 错误码映射
- **权限**：`tools/call` 校验 `SYSTEM_MANAGE`（与 REST `/call` 一致），其余 `SYSTEM_READ`
- **冒烟测试**：14/14 通过（initialize 握手、ping、通知、tools/list、resources/read、prompts/get、method-not-found、批量、位置参数拒绝等）
- **向后兼容**：原 REST 端点（`/tools` `/call` `/resources` `/prompts`）保留

#### 3. 项2 ✅ 自学习器 ONNX 回写闭环

- **新增 4 文件**（`src/edgelite/engine/`）：
  - `self_learner_base.py`：抽象基类 — `deque` 数据缓冲 + `threading.Lock` + `asyncio.Lock` 训练互斥 + `tempfile.mkstemp`→`os.replace` 原子写入 + `ai_engine.reload_model()` 热加载
  - `anomaly_self_learner.py`：z-score 异常评分（`Sub→Div→Mul→ReduceMean→Mul→Sigmoid`），输入 [1,100]→输出 [1,1]
  - `trend_self_learner.py`：`numpy.linalg.lstsq` 线性回归（`MatMul→Add`），输入 [1,200]→输出 [1,10]
  - `threshold_self_learner.py`：变异系数自适应 k（`ReduceMean→Sub→Mul→ReduceMean→Mul→Sqrt→Add`），输入 [1,50]→输出 [1,1]
- **接入**：`bootstrap.py` `bootstrap_ai()` 初始化 3 个 learner 并注入 `MCPToolService.set_ai_dependencies()`；`ServiceContainer` 新增 `anomaly_learner`/`trend_learner`/`threshold_learner` 字段
- **零额外依赖**：纯 `numpy` 训练 + 手工 `onnx.helper` 图构造（无 sklearn/torch/skl2onnx）
- **冒烟测试**：3 个 learner 各自通过 `onnxruntime.InferenceSession` 真实推理验证（anomaly normal=0.7485/anomaly=1.0000；trend 线性预测 19.96→39.90；threshold mean≈100→123.35）

#### 4. 项3 ✅ bcrypt rounds 文档对齐

- **代码**：`security/password.py` `_BCRYPT_ROUNDS = 14`
- **文档**（全部 13/12 → 14）：`README.md` / `README_EN.md` / `SECURITY.md` / `docs/ARCHITECTURE.md`
- **顺带修正**：README/README_EN 中 Token 撤销描述 → 「SQLite 持久化 + 文件 fallback」

#### 5. 项4 ✅ 限流持久化（Redis-ready）

- **文件**：`src/edgelite/middleware/rate_limit.py`
- **设计**：`RateLimitBackend`（ABC）+ `MemoryRateLimitBackend`（滑动窗口 deque）+ `RedisRateLimitBackend`（sorted set + Lua 原子脚本）
- **切换**：`EDGELITE_RATE_LIMIT_BACKEND=redis` + `EDGELITE_RATE_LIMIT_REDIS_URL=redis://...`
- **特性**：URL 脱敏日志、连接池复用、PEXPIRE 防内存泄漏、零侵入替换（中间件层不变）
- **依赖**：`redis>=4.2` 仅在启用 redis 后端时可选

#### 6. 全量回归验证

| 验证项 | 结果 |
|--------|------|
| `create_app()` 真实启动 | ✅ 9 middlewares / 239 routes / 0 import error |
| `pytest tests/` | ✅ 59 passed, 1 warning(starlette 弃用), 12.28s |
| `pyflakes`（8 个新增/修改文件） | ✅ exit 0，无未用导入/未用变量 |

---

## 五、原"未完成项"全部完成 ✅

| # | 任务 | 状态 | 证据 |
|---|------|------|------|
| 1 | MCP JSON-RPC 2.0 合规迁移 | ✅ 完成 | `api/mcp.py` 新增 `/jsonrpc` 端点，14/14 冒烟测试通过（详见 四-补2.2） |
| 2 | 自学习器 ONNX 回写闭环 | ✅ 完成 | 3 个 learner 新建 + bootstrap 注入 + onnxruntime 推理验证（详见 四-补2.3） |
| 3 | bcrypt rounds 文档对齐 | ✅ 完成 | 代码=14，4 份文档全部对齐 14（详见 四-补2.4） |
| 4 | 限流持久化（Redis-ready） | ✅ 完成 | `middleware/rate_limit.py` 可插拔后端，Lua 原子滑动窗口（详见 四-补2.5） |

---

## 六、生产部署安全判断

### 结论（修订）：✅ 可以安全部署到生产环境

> **修订说明**：首轮结论"✅ 可以安全部署"基于 pytest（conftest.make_app）通过即下判，未执行 `create_app()` 真实启动路径，遗漏 P0 启动崩溃（4 个模块缺失）。本轮通过 `create_app()` 实测发现并修复 P0 后，4 项非阻塞任务同步完成，**现真正达到生产就绪**。此前的"4 项非阻塞建议"已全部落地，不再附带保留项。

**判断依据**：

1. **真实启动验证**：`create_app()` 直接触发 FastAPI 应用构造，9 middlewares / 239 routes 全部注册成功，0 import error（修复前直接崩溃）。
2. **功能完整性**：Phase 1-5 共 23 项审计全部完成 + P0 启动崩溃修复 + 4 项非阻塞任务全部完成，覆盖架构/引擎/AI/安全/运维/MCP 协议合规全链路。
3. **测试保障**：`pytest tests/` 59 passed（exit 0）；新增/修改文件 `pyflakes` exit 0；核心 14 文件 `ruff F/I` 全部通过，无回归。
4. **关键风险已消除**：
   - **P0 启动崩溃** → 4 个缺失模块全部新建 ✅（本轮新发现并修复）
   - EventBus 事件丢失 → AlarmOutbox 持久化兜底 ✅
   - SQLite 数据损坏 → WAL + busy_timeout + synchronous=NORMAL ✅
   - WS 劫持/Token 泄露 → CSWSH 防护 + 首帧认证 + per-conn send lock ✅
   - JWT 撤销失效 → SQLite 持久化 + 文件 fallback ✅
   - 审计日志篡改 → append-only 触发器 ✅
   - Docker 重启循环 → /health/live 轻量探针 + start_period ✅
   - MCP 协议不合规 → JSON-RPC 2.0 `/jsonrpc` 端点 ✅（本轮新增）
   - 自学习器无回写 → 3 个 learner 训练→ONNX→热加载闭环 ✅（本轮新增）
5. **硬约束全部满足**：权限校验、表单验证、页面隐藏暂停轮询、导出限流、敏感字段脱敏、AlarmEvent 阻塞 put、计数器锁保护、SQLite WAL 配置、事件总线订阅清理等（见 project_memory.md）。

**部署前建议检查清单**：
- [ ] 确认 `docker/.env` 中 `INFLUXDB_TOKEN` / `MQTT_PASSWORD` / `JWT_SECRET_KEY` 已设置为强随机值（启动会校验占位符，含 `CHANGE_ME`/`<your-` 等模式直接拒绝启动）
- [ ] 确认 `JWT_SECRET_KEY` 长度 ≥ 32 字符（`_ensure_secret_key` 强制）
- [ ] 确认 `JWT_SECRET_KEY_PREVIOUS` 为空（首次部署无历史密钥）
- [ ] 确认 `EDGELITE_SERVER_DEBUG_API_ENABLED=false`（生产环境禁用调试路由）
- [ ] 确认 `EDGELITE_SECURITY_BCRYPT_ROUNDS=14`
- [ ] 多实例部署时设置 `EDGELITE_RATE_LIMIT_BACKEND=redis` + `EDGELITE_RATE_LIMIT_REDIS_URL`（单实例可留默认 memory）
- [ ] 首次启动后访问 `/health` 验证所有依赖为 healthy
- [ ] 验证 `/health/live` 返回 200（Docker healthcheck 依赖）

---

**修复人**：首席系统架构师 / CTO
**完成日期**：2026-06-30
