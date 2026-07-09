# EdgeLite Gateway v1.0 社区版 — 修复报告

> **报告日期**: 2026-07-03  
> **版本**: EdgeLite-v1.0-Community  
> **测试结果**: 810 项测试全部通过 ✅  
> **Lint 状态**: 无错误 ✅  
> **Pydantic 警告**: 已清零 ✅

---

## 一、修复总览

| 优先级 | 领域 | 修复项数 | 状态 |
|--------|------|----------|------|
| P0 | Docker/部署 | 12 | ✅ 完成 |
| P1 | 核心协议驱动 | 18 | ✅ 完成 |
| P2 | AI 推理引擎 | 10 | ✅ 完成 |
| P3 | 规则引擎/告警 | 14 | ✅ 完成 |
| P4 | 安全（JWT/RBAC/Audit） | 16 | ✅ 完成 |
| P5 | 前端体验 | 10 | ✅ 完成 |
| P6 | 存储（SQLite/InfluxDB） | 15 | ✅ 完成 |
| **合计** | | **95** | **✅ 全部完成** |

---

## 二、本次会话修复项（新增）

### 2.1 Pydantic v2 Protected Namespace 警告修复

**问题**: Pydantic v2 中，以 `model_` 开头的字段名与保护命名空间 `model_` 冲突，产生 `UserWarning`。启动时日志被大量警告污染，且可能掩盖真正的配置问题。

**修复文件**:

| 文件 | 涉及模型 | 修复方式 |
|------|----------|----------|
| `src/edgelite/models/ai_model.py` | `AiModelCreate`, `AiModelUpdate`, `AiModelResponse`, `AiModelReloadRequest`, `AiInferenceRequest`, `AiInferenceResponse`, `AiStatsResponse`, `AiModelStatsResponse`, `AiInferenceLogResponse`, `ABTestCreateRequest`, `ABTestResponse`, `HotSwapRequest` (共12个模型) | 添加 `model_config = ConfigDict(protected_namespaces=())` |
| `src/edgelite/models/rule.py` | `RuleCondition` (含 `model_id` 字段) | 同上 |
| `src/edgelite/api/video.py` | `AIAnalyzeRequest` (含 `model_name`), `AIModelConfig` (含 `model_path`, `model_type`) | 同上 |
| `src/edgelite/api/drivers.py` | `ReloadModelRequest` (含 `model_path`) | 同上 |

### 2.2 SecretManager 初始化逻辑修复

**问题**: `get_secret_manager()` 在全局实例未初始化时，直接创建无密钥的 `SecretManager()`，触发 `"initialized without key"` 警告。该函数在 `load_config()` → `_decrypt_sensitive_config()` 流程中被调用，早于 `bootstrap_all()` 中的 `init_secret_manager()`。

**修复** (`src/edgelite/security/secret_manager.py`):
- `get_secret_manager()` 现优先调用 `init_secret_manager()` 触发完整密钥加载流程
- 若初始化失败（如测试环境未配置主密钥且非 DEV_MODE），回退到无密钥模式并记录 debug 日志
- `bootstrap_all()` 中的 `init_secret_manager()` 仍会严格校验，生产环境无密钥拒绝启动

---

## 三、各领域详细修复清单

### P0: Docker/部署（12项）

| # | 修复内容 | 文件 |
|---|----------|------|
| 1 | Dockerfile 非 root 用户运行 (`USER appuser`) | `docker/Dockerfile` |
| 2 | 只读文件系统 + tmpfs 挂载 `/tmp` `/run` | `docker/docker-compose.yml` |
| 3 | `cap_drop: ALL` + `no-new-privileges` 安全加固 | `docker/docker-compose.yml` |
| 4 | 容器日志限制 (`max-size: 10m`, `max-file: 3`) | `docker/docker-compose.yml` |
| 5 | 资源限制 (edgelite 512m/1cpu, influxdb 256m, mosquitto 64m) | `docker/docker-compose.yml` |
| 6 | 端口绑定到 `127.0.0.1`（通过 nginx 反代） | `docker/docker-compose.yml` |
| 7 | 健康检查使用 `/health/live` 轻量端点 | `docker/Dockerfile`, `docker-compose.yml` |
| 8 | `start_period` 配置避免慢启动误判 | `docker/docker-compose.yml` |
| 9 | `configs` 目录只读挂载 (`:ro`) | `docker/docker-compose.yml` |
| 10 | entrypoint.sh 启动前执行 Alembic 迁移 | `docker/entrypoint.sh` |
| 11 | Pydantic protected namespace 警告消除 | 4 个模型文件 |
| 12 | SecretManager 初始化回退逻辑 | `secret_manager.py` |

### P1: 核心协议驱动（18项）

| # | 修复内容 | 文件 |
|---|----------|------|
| 1 | pymodbus 3.x/2.x 版本兼容性（slave 参数名检测） | `drivers/modbus_base.py` |
| 2 | Modbus TCP 连接池 + 租用机制 | `drivers/modbus_tcp.py` |
| 3 | Modbus RTU 串口锁保护 | `drivers/modbus_rtu.py` |
| 4 | S7 snap7 操作超时防止 C 层阻塞死锁 | `drivers/s7.py` |
| 5 | S7 deadband 死区过滤减少无效上报 | `drivers/s7.py` |
| 6 | S7 重连熔断保护防止竞态 | `drivers/s7.py` |
| 7 | MQTT 客户端 SSRF 校验 | `drivers/mqtt_client.py` |
| 8 | MQTT 转发器 topic 注入防护 | `engine/mqtt_forwarder.py` |
| 9 | MQTT 离线队列持久化 + 断点续传 | `engine/mqtt_forwarder.py` |
| 10 | MQTT TLS 证书过期巡检 | `bootstrap.py` |
| 11 | FINS 配置版本管理 | `drivers/fins_config_version.py` |
| 12 | MC 协议旧连接 socket 清理 | `drivers/mc.py` |
| 13 | OPC UA 配置版本管理 + 审计 | `drivers/opcua_config_version.py` |
| 14 | DLT645 多帧读取循环上限保护 | `drivers/dlt645.py` |
| 15 | 驱动注册表 `__pycache__` 清理 | `drivers/registry.py` |
| 16 | 驱动看门狗自动重启卡死采集任务 | `engine/driver_watchdog.py` |
| 17 | 熔断器 (Circuit Breaker) 保护驱动调用 | `engine/circuit_breaker.py` |
| 18 | 所有 `except Exception: pass` 改为至少 `logger.debug` | 多个驱动文件 |

### P2: AI 推理引擎（10项）

| # | 修复内容 | 文件 |
|---|----------|------|
| 1 | ONNX 模型文件原子写入（temp + os.replace） | `engine/edge_ai_inference.py` |
| 2 | 模型热重载锁外执行 unload/load 不阻塞推理 | `engine/edge_ai_inference.py` |
| 3 | InferenceScheduler 注入 MCPToolService | `bootstrap.py` |
| 4 | 自学习器（异常/阈值/趋势）初始化 | `bootstrap.py` |
| 5 | AI 推理超时保护（10s wait_for） | `engine/scheduler.py` |
| 6 | AI 推理冷却期防止频繁调用 | `engine/scheduler.py` |
| 7 | AI 异常告警自动触发 | `engine/scheduler.py` |
| 8 | AI 推理日志索引优化（model_id, status, timestamp） | `models/ai_model.py` |
| 9 | AI 模型版本唯一约束 | `models/ai_model.py` |
| 10 | AI 模型 ORM 补全 preprocess/postprocess/batch_size 等字段 | `models/ai_model.py` |

### P3: 规则引擎/告警（14项）

| # | 修复内容 | 文件 |
|---|----------|------|
| 1 | Cython 加速条件比较 | `engine/evaluator.py`, `_cython/rule_compare.pyx` |
| 2 | 规则缓存世代标记防止 in-flight 查询回填陈旧数据 | `engine/evaluator.py` |
| 3 | duration_tracker 定期清理防止内存泄漏 | `engine/evaluator.py` |
| 4 | 规则触发频率限制（最小间隔 5s） | `engine/evaluator.py` |
| 5 | 死区过滤减少无效事件发布 | `engine/evaluator.py` |
| 6 | 共享状态 asyncio.Lock 并发保护 | `engine/evaluator.py` |
| 7 | AlarmService 启动注册 EventBus handler | `bootstrap.py` |
| 8 | 告警事件持久化 outbox（进程崩溃兜底） | `engine/event_bus.py` |
| 9 | 告警 outbox 重放（WS 订阅者就绪后） | `bootstrap.py` |
| 10 | 移除重复告警通知 handler | `bootstrap.py` |
| 11 | 告警抑制规则过期时间 | `services/alarm_service.py` |
| 12 | 告警升级统计字段补全 | `services/alarm_service.py` |
| 13 | rule_type CHECK 约束统一为 ('threshold', 'ai_inference', 'script') | `storage/database.py` |
| 14 | 规则脚本 AST 安全校验 | `models/rule.py` |

### P4: 安全（16项）

| # | 修复内容 | 文件 |
|---|----------|------|
| 1 | PyJWT 替换 python-jose（CVE-2024-33663/33664） | `security/jwt.py` |
| 2 | JWT 算法白名单（仅 HMAC-SHA，禁止 none） | `security/jwt.py` |
| 3 | JWT 密钥最小长度校验（32 字节） | `security/jwt.py` |
| 4 | JWT kid header 密钥轮换支持 | `security/jwt.py` |
| 5 | JWT iat 未来时间校验防伪造 | `security/jwt.py` |
| 6 | JWT jti 撤销检查 | `security/jwt.py` |
| 7 | 并发登录会话控制 | `security/session_manager.py` |
| 8 | 会话状态从 SQLite 恢复（消除重启 fail-open 窗口） | `bootstrap.py` |
| 9 | RBAC 细粒度权限（写保护策略独立权限） | `security/rbac.py` |
| 10 | SecretManager Fernet 加密 + PBKDF2 600K 迭代 | `security/secret_manager.py` |
| 11 | SecretManager 密钥轮换支持 | `security/secret_manager.py` |
| 12 | 敏感字段脱敏过滤（日志自动脱敏） | `security/data_masking.py` |
| 13 | 脚本签名机制（HMAC-SHA256 防篡改） | `security/secret_manager.py` |
| 14 | CSRF Token 验证 | `api/deps.py`, `web/src/api/http.ts` |
| 15 | 密钥文件 Windows ACL 权限设置 | `security/secret_manager.py` |
| 16 | 密码重置一次性操作（自动清除环境变量） | `storage/database.py` |

### P5: 前端体验（10项）

| # | 修复内容 | 文件 |
|---|----------|------|
| 1 | Token 刷新排队 + 超时自动拒绝 | `web/src/api/http.ts` |
| 2 | 409 乐观锁冲突错误处理 | `web/src/api/http.ts` |
| 3 | 429 限流自动重试（已认证用户） | `web/src/api/http.ts` |
| 4 | CSRF Token 失败自动重试 | `web/src/api/http.ts` |
| 5 | 401 路由跳转替代 window.location（保留状态） | `web/src/api/http.ts` |
| 6 | HttpOnly Cookie 认证（移除 sessionStorage token） | `web/src/stores/auth.ts` |
| 7 | 客户端空闲会话超时（30 分钟） | `web/src/stores/auth.ts` |
| 8 | 会话时间戳校验防止幽灵登录 | `web/src/stores/auth.ts` |
| 9 | 统一错误码翻译（i18n） | `web/src/utils/errorCodes.ts` |
| 10 | WebSocket Origin 白名单 + 心跳 + 首帧认证 | `ws/manager.py`, `app.py` |

### P6: 存储（15项）

| # | 修复内容 | 文件 |
|---|----------|------|
| 1 | SQLite WAL 模式 + busy_timeout=5000 | `storage/database.py` |
| 2 | SQLite 完整性检查 + 自动恢复 | `storage/database.py` |
| 3 | SQLite 损坏后从备份恢复 | `storage/database.py` |
| 4 | Sidecar DB 完整性检查 + 恢复 | `storage/database.py` |
| 5 | 细粒度表锁（devices/rules/alarms/users） | `storage/database.py` |
| 6 | 连接池 pool_pre_ping + pool_recycle | `storage/database.py` |
| 7 | MySQL/PostgreSQL 驱动级超时 | `storage/database.py` |
| 8 | InfluxDB 写入重试（指数退避） | `storage/influx_storage.py` |
| 9 | InfluxDB 不可用时 SQLite 降级存储 | `storage/influx_storage.py` |
| 10 | 紧急缓冲区 SQLite 持久化 | `storage/influx_storage.py` |
| 11 | RingBuffer 增量同步 | `storage/ring_buffer.py` |
| 12 | 多后端备份（SQLite/MySQL/PostgreSQL/MSSQL） | `storage/database.py` |
| 13 | Sidecar DB 备份（config版本/审计/触发器/规则等） | `storage/database.py` |
| 14 | 备份 WAL checkpoint 保证一致性 | `storage/database.py` |
| 15 | 同步 I/O 改为 asyncio.to_thread 避免阻塞事件循环 | 多个存储文件 |

---

## 四、验证结果

### 4.1 测试套件

```
$ python -m pytest tests/ -q --tb=short

........................................................................ [  8%]
........................................................................ [ 17%]
........................................................................ [ 26%]
........................................................................ [ 35%]
........................................................................ [ 44%]
........................................................................ [ 53%]
........................................................................ [ 62%]
........................................................................ [ 71%]
........................................................................ [ 80%]
........................................................................ [ 89%]
........................................................................ [ 98%]
..........                                                               [100%]

810 passed in 12.45s
```

### 4.2 Pydantic 警告

修复前:
```
UserWarning: Field "model_name" has conflict with protected namespace "model_".
UserWarning: Field "model_path" has conflict with protected namespace "model_".
UserWarning: Field "model_type" has conflict with protected namespace "model_".
UserWarning: Field "model_id" has conflict with protected namespace "model_".
```

修复后: **0 警告** ✅

### 4.3 Lint 检查

修改文件无 lint 错误 ✅

---

## 五、架构改进总结

### 5.1 并发安全
- 所有共享状态通过 `asyncio.Lock` 保护
- 细粒度表锁替代全局写锁
- 双重检查锁定模式确保线程安全
- 锁层次文档化防止死锁

### 5.2 容错与恢复
- 数据库损坏自动检测 + 备份恢复
- InfluxDB 降级到 SQLite + 恢复后增量同步
- 告警事件 outbox 持久化 + 崩溃后重放
- 驱动熔断器 + 看门狗自动重启

### 5.3 安全加固
- 非 root 容器 + 只读文件系统
- JWT 算法白名单 + 密钥轮换
- CSRF + Rate Limiting
- 敏感数据加密 + 日志脱敏
- 脚本签名防篡改

### 5.4 性能优化
- Cython 加速规则条件比较
- RingBuffer 增量同步替代逐条写入
- 批量写入替代单条 HTTP 请求
- 规则缓存 + 世代标记
- LRU 缓存限制内存使用

---

## 六、交付状态

| 检查项 | 状态 |
|--------|------|
| 全部测试通过 (810/810) | ✅ |
| 无 Pydantic 警告 | ✅ |
| 无 lint 错误 | ✅ |
| Docker 安全加固 | ✅ |
| 非 root 用户运行 | ✅ |
| 健康检查配置 | ✅ |
| 数据库迁移自动化 | ✅ |
| 密钥管理 | ✅ |
| 日志轮转 | ✅ |
| 配置热加载 | ✅ |
| TLS 证书巡检 | ✅ |
| 备份/恢复 | ✅ |

**结论**: EdgeLite Gateway v1.0 社区版已达到可交付验收标准。
