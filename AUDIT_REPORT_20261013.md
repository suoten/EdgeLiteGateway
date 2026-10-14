# EdgeLite v1.0-Community 全系统深度审计报告

- 日期：2026-10-13
- 审计基线：master @ 88fcf06（工作区干净）
- 方法：5 个并行分域审计（安全认证 / 协议驱动 / 引擎与存储 / REST API / 前端与部署），后端逐文件精读 + 关键结论下沉到 service/repo 层验证；审计者独立抽查复核了全部 P0/P1 级发现（11/11 属实，无误报）。
- 规模：src 209 文件 ≈ 11.9 万行；API 379 端点系统性权限比对；前端 web/src 全量正则扫描；git ls-files 638 跟踪文件逐项核查。

## 结论速览

| 维度 | 结果 |
|---|---|
| 测试 | 全套通过；唯一失败为负载敏感的延迟断言（53.9ms vs 50ms 阈值），单独跑即过，属计时抖动非回归 |
| Lint/一致性 | ruff check / 依赖一致性 / 配置漂移全部通过（本地 venv ruff 0.16.8 与 CI 钉的 0.15.11 有格式器版本漂移，2 个 md 文件差异为本地误报） |
| 发现总量 | P0 × 2，P1 × 14，P2 × 30，P3 × 60+ |

联调验收覆盖的协议层（Modbus/FINS/MC/AB/S7/OPC UA 帧解析、写映射、权限矩阵主体）质量良好；本轮发现的严重问题集中在**联调未覆盖的路径**：MQTT 驱动断线语义、Linux 沙箱、OTA 完整性链、离线队列、导出/集成端点的授权旁路、配置快照。

---

## P0（必须立即修复）

### P0-1 MQTT 驱动断线后永不重连，采集数据静默冻结
- `src/edgelite/drivers/mqtt_client.py:878-879`：连接主循环 `while self._running: await asyncio.sleep(1)`，从不检查 `msg_task`/`pub_task` 存活；`_message_loop`（:992-1022）断线抛 MqttError 后仅记 error 日志即退出。
- 后果：broker 重启/网络抖动后 TCP 断开 → 消息循环退出 → 主循环永远睡眠，不触发重连；`read_points` 继续返回 `_latest_values` 旧值且 quality="good"。**数据静默冻结、无 bad quality、无告警**。
- 修复：`asyncio.wait([msg_task, pub_task], return_when=FIRST_EXCEPTION)` 替代 sleep 循环，或在循环中检查 `task.done()` 唤醒重连。

### P0-2 沙箱 resource.setrlimit 是进程级限制且永不恢复——Linux 上执行一次脚本规则可杀死整个网关
- `src/edgelite/engine/sandbox.py:180-196`：在工作线程内 `setrlimit(RLIMIT_CPU,(2,2))`、`RLIMIT_AS 256MB`、`RLIMIT_FSIZE 0`，无保存/恢复。POSIX rlimit 是**进程属性**，docstring"线程级"说法错误。
- 后果（Linux/Docker 部署 + 配置了 script 类型规则）：运行几分钟后进程累计 CPU 超 2 秒 → SIGXCPU 杀死整个网关；`RLIMIT_FSIZE=0` 使 SQLite WAL/日志写入全面失败。Windows 上 `resource` 为 None 跳过，因此开发机发现不了。
- 修复：执行前 `getrlimit` 保存、finally 恢复；或改为 fork 子进程隔离。

---

## P1（高危，应尽快修复）

### 安全域
1. **scripts.py exec"沙箱"一行逃逸 → Web 管理员到 OS RCE**
   `api/scripts.py:57-81` 白名单含 `print`，`print.__self__` 即 builtins 模块 → `__import__('os')` 任意命令执行（已用 PoC 实测）。且 test 端点无超时（`while True: pass` 挂死服务）、不检查脚本 status（draft 可直接执行）、`secret_manager.py` 的脚本 HMAC 验签能力全项目无人调用。
2. **ota.py 全部 6 端点 RBAC 静默失效**
   `api/ota.py:25,43,94,113,134,179`：`user: CurrentUser = require_permission(...)` 未包 `Depends()`——FastAPI 采用注解里的 `get_current_user`，权限检查函数被当普通默认值忽略。viewer 可调用 `POST /ota/apply`、`/ota/rollback`。全仓 grep 确认仅此一处该写法；CI 应加 AST 规则禁止。
3. **OTA 更新链完整性校验缺失**
   `api/ota.py:72` 调 `apply_update(update_file)` 不传 `expected_sha256`；`engine/ota_manager.py:171-175` 为 None 时跳过全部校验直接解压覆盖应用目录；`version` 来自 OTA 服务器 JSON 拼路径（`../` 路径遍历，:76）；下载无 HTTPS 强制、`firmware_signature.py` 验签未接线。OTA 服务器被劫持/HTTP 中间人 → 持久化 RCE。
4. **反向代理下 XFF 取最左值：限流/锁定可绕过或被滥用为全局登录 DoS**
   `api/auth.py:241` `forwarded.split(",")[0]` 取 XFF 最左侧（客户端可伪造段）；nginx 配置用 `$proxy_add_x_forwarded_for` 追加。配了 `trusted_proxies` → 伪造 XFF 轮换绕过 IP 限流/锁定并污染审计 IP；不配（默认）→ 所有用户共享 nginx IP，攻击者故意失败可触发全局失败计数锁死全体登录。
5. **设备导出越权：空 device_ids 绕过归属校验，跨租户导出全部配置含明文凭据**
   `api/devices.py:947-966`：`body.device_ids or []` 为 None 时不做任何归属校验，`svc.export_devices(None)` 导出全表；驱动配置含 S7/OPC UA/AB/ONVIF 明文口令且响应不脱敏。`api/data.py:441-477` 的 data/export 同型。viewer 一个空 `{}` 请求即拿下全租户 PLC 凭据。

### 驱动域
6. **Modbus TCP 批量读混段用 FC03：input 测点静默读到错误数据**
   `drivers/modbus_tcp.py:1201-1227` 段合并只按地址排序，不区分寄存器类型；混合段落入 holding 分支用 FC03 读，input 测点值错乱且无报错。**RTU 已修同一问题**（`modbus_rtu.py:1103-1124` 的 `seg_reg_type` 跟踪），TCP 漏掉。
7. **S7 整数一律按有符号解码，uint16/uint32 语义缺失**
   `drivers/s7.py:1267-1280` `int.from_bytes(..., signed=True)` 硬编码；`data_type=uint16` 的 40000 解码为 -25536；写入侧校验也限有符号范围。
8. **Modbus TCP remove_device 的 stale 标记刚加即删，租用中连接泄漏**
   `drivers/modbus_tcp.py:806`（加入 `_stale_clients` 延迟关闭）被 `:813` 无条件 `pop` 抵消；5 分钟强关清理永远看不到该 client，在飞读完成后 socket/FD 泄漏。另 `:818` `ref_count - 1 == 0` 在 ref>1 分支恒 False，死代码。

### 引擎/存储域
9. **OfflineQueue.flush 毒丸死循环，挂死 MqttForwarder 连接循环**
   `storage/offline_queue.py:118-147`：回调持续失败 + 积压 ≥1000 条时，唯一退出条件 `len(batch) < 1000` 永不满足，`dequeue_batch` 反复取回同一批，重试无上限——`_connect_loop` 永久卡死，北向转发彻底瘫痪。
10. **OfflineQueue 按文件大小淘汰，但 DELETE 不收缩文件——超限后每次入队都误删数据**
    `storage/offline_queue.py:177-191`：`excess = 文件大小 - 200MB` 估算条数删最旧记录；SQLite 删除不回收页，一旦涨破 200MB，`excess` 永远 > 0，链路恢复后队列被反复清空，至少一次送达语义失效。
11. **InfluxDB 写入用异步缓冲 write_api，成功返回 ≠ 已持久化**
    `storage/influx_storage.py:437-442`：`WriteOptions(batch_size, flush_interval)` 为后台批量模式——网络失败发生在后台线程，不传播给调用方；三连失败降级判定（`_fail_count`/`_enter_fallback_mode`）与 `scheduler` 的断网缓存回写对网络故障永不触发；重试耗尽静默丢点。自带的同步 `_write_with_retry`（:803-838）是死代码。
12. **OTA 文件替换非原子 + 下载无锁并发**
    `engine/ota_manager.py:165-237`：apply_update 逐文件覆盖**正在运行**的应用目录，中途失败=半新半旧代码，下次重启可能起不来；rollback 先 `rmtree` 再复制，中途失败=代码目录丢失无自动恢复；`download_update` 不持锁，并发下载交错写同一 `.part` 文件。

### API 域
13. **config_version 快照对 devices/rules 存的是 coroutine 字符串，回滚是假回滚**
    `api/config_version.py:97-99` `await_list` 原样返回协程；`:62,71` 把 coroutine 经 `default=str` 序列化成对象地址——最关键的两类配置从未真正存过快照。`POST /config/rollback/{id}`（:272-304）只查库返回成功字段，不执行任何回滚——操作员以为已回滚，实际什么都没发生。
14. **integration RPC 端点缺设备归属校验（IDOR），operator 可写任意设备**
    `api/integration.py:207-293`：有 `DEVICE_WRITE_POINT` 权限但无 `_check_device_owner`；`backhaul.handle_rpc_command` 把 params 直传驱动写点。对比 `devices.py:1373` 写点第一步就查归属。横向越权可对非本人设备下设定值。

---

## P2（中危，按主题归类）

**安全**
- `api/firmware_signature.py:44,90` 接收任意 `firmware_path` → 本地文件哈希/大小 oracle（可探测 `/etc/shadow`、`data/.master_key` 等）；应复用 `ai_models.py:157-172` 的目录白名单。
- `api/debug.py` IP 白名单只盖 2 个端点；最危险的 `debug_read`(:1230)/`debug_write`(:1269，可直接写工业点位)/`get_recent_packets` 等无白名单。
- `engine/sandbox.py:33-56` AST 黑名单可被 `"{0.__class__...}".format(())` 字符串属性链绕过（实测）；应改用 `expression_engine.py` 的 AST 白名单范式并禁 `format`。
- `security/rbac.py:88` OPERATOR 含 `CONFIG_EDIT`，而 `api/system.py:796` `_DENIED_SECTIONS` 不含 `mqtt`/`notify`/`grafana` → operator 可把 MQTT broker/告警 webhook 改到攻击者服务器，数据持续外传且持久化。

**驱动**
- `drivers/base.py:465-483` + `mc.py:347-361`：executor 超时重建 + 驱动级 `threading.Lock` 组合，PLC 半开连接时线程线性泄漏（每 4 线程/次）。
- `drivers/mqtt_client.py:815-838` 订阅侧不校验通配符位置（`a/#/b` 原样发 broker → 断连循环）；`:1167-1173` routes 回退用 `endswith` 可能写错设备（主路径已改 `_topic_matches`，此处漏改）。
- `drivers/modbus_tcp.py:1705-1771` 批量写被拒点缺键返回（RTU 有兜底，TCP 无）。
- `drivers/edge_rule_engine.py:127,218,239` 告警历史 list 无上限，告警风暴下内存无界增长。

**引擎/存储**
- `engine/event_bus.py:283-307` 告警 outbox persist 超 0.5s 即放弃投递，但落盘线程仍在跑 → 事件"已落盘不投递也不重放"（重启才补发）。
- `engine/event_bus.py:100-107` + `driver_watchdog.py:633-650`：静态 event_id（`system_alert:{type}:{severity}`、`long_offline_{device}`）被去重缓存吞掉第二次及以后的同类告警。
- `storage/influx_storage.py:962-994` 紧急缓冲 SQLite 写入吞异常，三层兜底（重试/回滚/fallback 文件）全部死代码，磁盘满时缓冲仅存内存。
- `storage/cache.py:438-463` + `scheduler.py:1085-1092`：孤儿补偿回灌数据时间戳失真（原始 epoch 被静默替换为补偿时刻）。
- `engine/scheduler.py:955-1026`：watchdog 用基础间隔算 stale 阈值，与自适应退避互相抵消——断线边缘设备被反复重启采集任务，且重启清空退避状态；`_update_device_quality_stats`(:835-880) 的 error_rate 是累计值，设备长期恢复后仍持续发布 degraded。
- `bootstrap.py:666-711`：证书过期巡检调用 `validate_cert(path).get("days_remaining")`，而该函数返回 `bool`（`tls_security.py:79`）→ 必然 AttributeError 被 `logger.debug` 吞掉，**功能从未生效**（已独立复核确认）。
- `engine/ota_manager.py:69-112` 无断点续传（无 Range 头，失败即删 `.part`），弱网大固件反复从头下载。
- `engine/evaluator.py:329-343`：不满足条件路径每次评估逐规则查 DB + 窗口条件回源 InfluxDB——50 设备×10 点×5s ≈ 每秒 100 次 SQLite 查询的设计级负载放大。

**API**
- `api/data.py`：90 天范围限制只修了 `/query`；`/trend`(:277)/`/correlation`(:302)/`/statistics`(:324)/`/multi-point`(:350)/`/downsample`(:587)/`/export`(:177) 全部可 `start=-10y` 全表扫描——已打补丁的明显遗漏。
- `api/devices.py`：写保护策略只剥离 update 一条路径，create(:167)/import(:969)/batch-deploy(:677) 均旁路（当前矩阵下仅 admin 可达，权限下放即成洞）。
- `api/drivers.py:562` OPC UA browse 用不存在的 `_drivers` 属性 → 恒 404 死端点；`:588-605` certificate-status 用 `__new__` 构造假驱动 → 恒报 0 证书，掩盖真实证书告警。
- `trend_learner.py:33` / `anomaly_learner.py:38` / `threshold_learner.py`：模块级 `_learners` 按任意客户端 model_id 永久缓存，viewer 可脚本打爆内存。
- `services/device_service.py:289-355` + `api/devices.py:1164-1190`：驱动重载失败回滚后 API 仍返回"新配置成功"。
- `api/devices.py:759-776` batch-deploy 不注入 `_version`，无乐观锁，并发丢更新。
- `api/protocol_bridge.py:130-133,233-242` 纯内存态重启全丢、更新无锁。
- `api/devices.py:150-158` / `rules.py:74-83`：共享资源 append 到页尾，破坏分页/排序/total 语义。

**前端/部署**
- `scripts/run_acceptance.py:16`、`scripts/one_shot_acceptance.py:83,235` 硬编码验收环境真实管理员口令 `EdgeLite@2026`（git 已跟踪）。
- 根 `Dockerfile:73` CMD 无 `--port`（默认 8180）与 `EXPOSE/HEALTHCHECK` 的 8080 不符——裸 `docker run` 健康 检查必败（注释声称两 Dockerfile 同步，实际已漂移）。
- `Procfile:2` `python -m edgelite.scheduler` 指向不存在的模块，worker 启动即崩。
- `k8s/deployment.yaml:84-86` 引用不存在的 PVC `edgelite-data`，裸清单无法直接部署。
- `.github/workflows/deploy.yml` 部署出的 Helm Secret 全是 `CHANGE_ME`（后端 fail-closed 拒绝启动，CD 流水线实际不可用），且 `helm upgrade --debug` 失败时会把含明文 stringData 的渲染清单打进 CI 日志。
- `docker/docker-compose.yml:297` promtail 挂载 docker.sock（`:ro` 不限制 socket API 权限）且无 cap_drop/no-new-privileges。

---

## P3（低危/卫生，摘要）

- **安全**：CSRF token 与会话无绑定（防线实为 SameSite+自定义头）；`/auth/refresh` 豁免 CSRF；登录 `no_revoke=true` 可绕过会话互斥；`users.py:190,316` 调用不存在的 `_invalidate_user_cache`（`# type: ignore` 掩盖，角色变更后缓存失效静默失败，已复核）；`token_renewal.py:85` 续签未注册新 jti（滑动续签实际失效）；`secret_manager.py:258` 调用不存在的 `ConvertStringSecurityDescriptorToSDDL`（同文件 :198 已修正过同名错误，此处漏改，已复核）；WS 端点绕过限流/CSRF 中间件；`GET /system/config` 对 operator 返回 Fernet 密文而非掩码。
- **驱动**：registry 无锁 check-then-act（当前调用收敛在发现锁内）；TCP write_verify float 绝对容差对大数值误报（RTU 用相对容差）；RTU 写后回读 None 静默放行；MQTT 订阅 QoS 0 不可配；http_webhook 200 之外成功码误判 + 并发首建 client 泄漏；`modbus_ts_store.py:108` stop 在队列满时永久阻塞（应 `put_nowait`）；`base.py:194` p95 无锁遍历 deque；OPC UA 批量写回退未带 VariantType、`_session_locks` 并发 pop KeyError、批量回退串行长阻塞；RTU 重连在 port_lock 内 sleep 阻塞同口设备；S7 写超时丢统计、deadband 命中缺键；redundancy create_task 未存引用。
- **引擎/存储**：event_bus 串行 await handler 拖慢采集周期；`backpressure.py:515` rate_limiter 死代码 + None 哨兵截断；inference_scheduler TOCTOU 超限 + busy-poll + 超时请求仍占信号量；`sqlite_ts.py:697` BEGIN IMMEDIATE 失败路径 ROLLBACK 抛新异常；alembic 001 的 `alarms.alarm_id String(16)` vs ORM String(64)（MySQL/PG 路径有风险），`_ensure_schema_columns` 兜底清单缺列；mqtt_forwarder stop 关 DB 未持锁；scheduler 每点值抢全局 `_state_lock`；expression_engine 求值全局串行；`data_service.py:137` OFFSET 分页导出跳行/重复；`check_network_status` 硬编码 8.8.8.8（内网恒报 offline）；lifecycle 每次状态转换 2 次 to_thread 写放大。
- **API**：SSRF 校验 DNS rebinding TOCTOU（devices/drivers/grafana 三处，先 getaddrinfo 校验再重连）；shadow 全端点无归属校验 + 设备删除不清理影子；resource_shares 不校验资源/用户存在性；device_linkage 无所有权模型、可指向幽灵设备；downsample `interval="0s"` 除零返回误导性 503；health 端点把 `str(e)` 泄漏给未认证调用方；10MB body 限制可被 chunked 绕过；`test_device_connection` 把内部异常文本原样返回；retention 端点假更新；审计 operator 字段客户端可控；设备存在性 404/403 策略模块间不一致可差分枚举；`DeviceUpdate.force` 死字段；audit pending 异常路径悬挂；`get_device_metrics` 异常时返回伪造全零/100 分指标；配置保存同步 IO 阻塞事件循环且读改写无锁。
- **部署/前端**：web/dist 本地产物过期而 compose frontend 直接挂载（忘记 build 则上线旧前端）；WS 降级轮询打页面 origin 而非 `VITE_API_BASE_URL`（分域部署时静默失效）；alertmanager 配置的环境变量占位符不会被替换（SMTP 认证失效）；`.gitlab-ci.yml:17` CI JWT 密钥入仓（标注 ci-only，建议改注入）；`run.py` src 优先加载不覆盖 `python -m edgelite` 入口（建议在 `__init__.py` 加版本自检）；xlsx 依赖绑第三方 CDN tarball；`docker/entrypoint.sh` 中文注释 mojibake；nginx `client_max_body_size` 两处不一致（50m vs 10m，frontend 路径上传大固件 413）；`.gitignore` 忽略 `docs/images/` 但收款码图片已被跟踪；**根目录 `.env` 含真实随机密钥（secret_key/CSRF/InfluxDB token）——目录打包分发会随包泄露，发布务必走 `git archive`**。

---

## 已检查且确认良好的方面（覆盖面）

- **JWT/会话**：算法白名单、kid 白名单、过期/iat 未来拒绝、jti+SQLite 持久化、refresh 轮换、改密/禁用全撤销；登录爆破防护体系完整（IP 限流+多级锁定+等时防枚举）；XFF 仅信白名单代理（问题只在取最左值）。
- **密码/密钥**：bcrypt cost 12 + SHA-256 预哈希；无默认口令（初始 admin 随机口令落 `.initial_admin_password`，登录即删）；弱密钥黑名单 fail-closed；PBKDF2 600k 迭代。
- **注入面**：SQL 全参数化（f-string 仅拼硬编码常量表名）；命令注入无（固定 argv）；反序列化无 pickle、YAML safe_load；表达式引擎是 AST 白名单的正确范式。
- **RBAC 主体**：379 端点系统性比对，除 ota.py 一处写法缺陷与 2 个设计内免认证端点外无遗漏；video/data 有归属校验；权限矩阵整体纪律良好。
- **协议面**：FINS 帧解析质量高（魔数/残帧/流错位/事务锁）；MC 串行一致性 + 小端正确；AB 生命周期完整；OPC UA 连接循环/keepalive/asyncua 2.x 适配正确；Modbus 地址越界/字交换/广播写双条件放行正确；全部 IO 有超时；NaN/Inf 写前拒读后标 bad。
- **存储基座**：WAL+busy_timeout 统一注册、启动 integrity check、表级细粒度锁+乐观锁、时序批量刷盘+24h 清理、ring buffer 有界覆盖、cache 孤儿补偿（时间戳失真除外）、alarm outbox 先落盘后投递、teardown 顺序正确。
- **前端**：XSS 面干净（无 v-html/innerHTML/eval）；token 内存+HttpOnly Cookie、30 分钟空闲登出、401 刷新队列、WS 首帧认证/退避重连/心跳质量高；无硬编码密钥。
- **容器/K8s 主体**：非 root、cap_drop ALL、只读根文件系统、seccomp、compose 全绑定 127.0.0.1、探针齐全、依赖服务口令强制环境注入；git 跟踪文件里无证书私钥、无运行时数据；`web/dist` 从未入库。

## 修复优先级建议

1. **立即**：P0-1（MQTT 重连）、P0-2（setrlimit 进程级）——两者都是"静默失败/整进程崩溃"级。
2. **立即**：P1-1/2/5（沙箱逃逸、ota.py Depends、导出越权）——改动极小（一行级），收益大。
3. **尽快**：P1-3/4（OTA 完整性链、XFF 右向左解析）、P1-6/7/8（TCP 混段、S7 符号、stale 泄漏）、P1-9/10/11（offline flush、队列淘汰、InfluxDB 同步写）、P1-13/14（假回滚、RPC IDOR）。
4. **短期**：P2 全部，尤其是 90 天限制补齐（明显遗漏）、debug 全端点白名单、sandbox 改白名单 AST、`_DENIED_SECTIONS` 补 mqtt/notify、证书巡检修正、验收脚本口令移出仓库。
5. **工程**：CI 加 AST 规则禁止 `= require_permission(` 裸写法；统一 `run.py`/`-m` 入口的本地 src 加载；本地 venv 的 ruff 对齐 CI 钉住的 0.15.11。
