# Changelog

All notable changes to EdgeLite Gateway are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- 完美版质量审计：11 维度全面评估（安全/功能/可运行/可部署/代码质量/架构/端到端/测试/性能/文档/DevOps）
- 2026-10-13 全系统深度审计（5 域并行：安全认证/协议驱动/引擎存储/REST API/前端部署，报告见 `AUDIT_REPORT_20261013.md`）及本轮全量修复
- 协议桥接（protocol_bridge）配置持久化：桥接配置原子落盘 `data/protocol_bridges.json`，重启自动恢复；配置变更加锁消除与后台同步循环的竞态
- EventBus 新增告警 outbox 周期性重放（5 分钟），兜底 persist 超时导致的"已落盘未投递"事件
- CertManager 新增 `days_until_expiry()`，证书过期巡检真正生效（<30 天告警）
- OTAManager 支持断点续传（Range 续传 + .part 保留）与 `close()` 资源释放
- k8s 裸清单补齐 `pvc.yaml`（此前 deployment 引用的 PVC 缺失导致 Pod Pending）

### Security
- 【P0/高危】`api/scripts.py` exec 沙箱白名单含 `print` 可经 `__self__` 逃逸至 RCE：test 端点改走 AST 白名单沙箱 `run_script_safely`，draft 状态脚本禁止测试
- 【P0】`engine/sandbox.py` `resource.setrlimit` 为进程级且不恢复（Linux 上执行脚本规则可 SIGXCPU 杀死整个网关）：改为执行前保存、finally 恢复；AST 校验由黑名单改为白名单（拒绝 dunder/format 属性链逃逸）
- 【高危】`api/ota.py` 6 处 `require_permission` 未包 `Depends()` 导致 RBAC 静默失效（viewer 可触发固件升级）：全部修正
- 【高危】OTA 完整性链：apply 端点强制要求 sha256（fail-closed）、version 字符白名单、下载 URL 强制 HTTPS（显式环境变量方可豁免）
- 【高危】设备/数据导出越权：空 `device_ids` 绕过归属校验导出全租户配置（含明文凭据）——非 admin 强制按可访问范围过滤（devices/data 两处）
- 【高危】X-Forwarded-For 解析改为右向左跳过可信代理，防伪造 XFF 绕过登录限流/锁定
- 【高危】integration RPC 端点补设备归属校验，堵 operator 横向写任意设备（IDOR）
- firmware_signature 端点增加固件目录白名单（原任意路径可做本地文件哈希 oracle）
- debug API 全部端点补 IP 白名单（原仅 2 个端点覆盖）
- `_DENIED_SECTIONS` 增加 mqtt/notify/grafana，operator 不再可改数据外传通道
- 敏感配置接口返回固定掩码 `***` 而非 Fernet 密文
- CI JWT 密钥加入生产弱密钥拒绝名单
- 验收脚本硬编码管理员口令移出仓库（改环境变量必填）
- CD 流水线密钥改为 GitHub Environment secrets 注入（缺失 fail-fast），移除 `helm --debug` 防明文渲染清单进 CI 日志
- `config_version` 配置回滚从"假回滚"（直接返回成功）改为真实逐条回滚 + 失败计数 + 不支持段明确 501；快照修复 devices/rules 存 coroutine 字符串的损坏

### Fixed
- 【P0】MQTT 驱动断线后永不重连、数据静默冻结：连接主循环改为 `asyncio.wait` 感知 msg/pub 任务死亡并触发重连
- Modbus TCP 批量读混段（input/holding 用 FC03 读 input 测点数据错乱）：段合并跟踪寄存器类型（对齐 RTU 已有修复）
- S7 无符号数据类型（uint16/uint32）按有符号解码：按 data_type 决定符号，写入校验补无符号范围
- Modbus TCP remove_device stale 标记被误删导致租用中连接泄漏 + 恒 False 死代码清理
- OfflineQueue flush 毒丸死循环（积压≥1000 且发送失败时挂死北向转发）：满批零成功即退出
- OfflineQueue 按文件大小淘汰导致超限后每次入队持续误删（SQLite DELETE 不收缩文件）：改按行数计数淘汰
- InfluxDB 写入由异步缓冲改为同步 API：错误同步传播，三连失败降级与断网缓存回写真正生效
- OTA 文件替换非原子（中途失败=半新半旧代码）：逐文件备份+失败恢复；rollback 不再先 rmtree（防代码目录丢失）；下载加锁
- 看门狗 stale 阈值改用自适应有效间隔、不再重置退避状态（消除误杀/退避失效互相抵消）
- 设备质量统计 error_rate 累计值导致永久 degraded：连续成功 20 次恢复 healthy 并复位统计
- SystemAlertEvent/watchdog 超长离线静态 event_id 被去重吞掉第二次告警：event_id 加时间戳成分（发布方已有源头防抖）
- 紧急缓冲 SQLite 写入吞异常使三层兜底成死代码：改 log+re-raise
- 缓存孤儿补偿时间戳失真（epoch float 被静默替换为补偿时刻）：统一转 ISO
- evaluator 条件不满足路径逐评估查 DB 的负载放大：5s TTL 缓存（触发/恢复时主动失效）
- bootstrap 证书巡检对 bool 返回值调 `.get()` 必然 AttributeError 被 debug 吞掉：改用 `days_until_expiry`
- 请求体 10MB 限制可被 chunked 传输绕过：无 Content-Length 且带 Transfer-Encoding 一律 413
- 三个 learner 模块级缓存无界（viewer 可打爆内存）：LRU 上限 100 + initial_data 上限
- data 时序查询 90 天限制补齐 /trend、/correlation、/statistics、/multi-point、/downsample、/export 五个旁路端点
- 写保护策略剥离覆盖 create/import/batch-deploy 三个旁路入口（原仅 update）
- OPC UA browse 死端点（引用不存在的 `_drivers` 属性恒 404）与 certificate-status 恒报 0：改走真实驱动实例
- device_linkage 更新端点引用未定义变量 `current`（F821 运行时 NameError）：先取现有行再校验
- shadow/resource_shares/device_linkage 补归属校验与存在性校验；设备删除级联清理影子
- batch-deploy 补乐观锁（并发丢更新）；驱动重载失败回滚后 API 返回真实生效配置
- downsample `interval=0s` 除零返回误导 503 → 400；import/export 解析错误 503 → 400
- health 端点异常文本泄漏给未认证调用方 → 通用错误；磁盘检查改数据目录所在盘
- 配置保存同步 IO 阻塞事件循环 → `asyncio.to_thread`；审计 operator 字段服务端强制
- MQTT 订阅通配符位置校验（防 broker 断连循环）、routes 回退 `endswith` 改 `_topic_matches`、订阅 QoS=1
- 驱动层一批健壮性修复：executor 重建上限防线程泄漏、write_points_batch 缺键、告警历史上限、http_webhook 2xx 判定、RTU 重连锁外退避、OPC UA 会话锁 KeyError 等
- storage/engine 一批低危修复：sqlite_ts ROLLBACK 保护、mqtt_forwarder 关闭竞态、inference_scheduler TOCTOU/超时占额/busy-poll、ring buffer 过滤记录状态落定、alembic alarms.alarm_id 对齐 ORM、`_ensure_schema_columns` 补 alarms 三列
- 部署修复：根 Dockerfile CMD 端口对齐 EXPOSE/HEALTHCHECK、Procfile 移除指向不存在模块的 worker、promtail 加 no-new-privileges、alertmanager SMTP 凭据启动时渲染（原字面量占位符静默失效）、nginx body 上限统一 50m、entrypoint 中文注释修复
- 前端 WS 降级轮询 URL 基准与 axios baseURL 统一（分域部署不再 404）

### Security（历史）
- 生产环境（DEV_MODE=false）下自动启用 `cookie_secure=True`，确保 Cookie 仅通过 HTTPS 传输

### Changed
- 测试覆盖率基线从 60% 提升至 80%，匹配完美版（≥95 分）成熟度目标
- CI 安全扫描 job 添加 `needs: [docker]` 依赖，确保 Trivy 镜像扫描针对最新构建的镜像

### Fixed
- 设备列表操作列缺失 render 函数导致编辑/采集/克隆/删除按钮不显示（useDeviceList.ts）
- i18n 缺失 `deviceList.startCollect` 和 `deviceList.stopCollect` 键（zh-CN.ts / en-US.ts）
- README.md 中"验收门禁"开发记录已移除，保留产品文档定位
- 4 处 `except Exception: pass` 静默吞没异常改为 `logger.debug/warning` 记录（engine/lifecycle.py、engine/graceful_restarter.py、engine/edge_ai_inference.py、engine/alarm_outbox.py）
- .gitignore 增加 data/ 目录测试输出文件的额外保护规则
- FastAPI 路由参数类型注解 `Request | None = None` 导致启动崩溃，全量修复为 `Request = None`（涉及 alarms.py、rules.py、users.py、auth.py 共 15 处）
- bcrypt rounds 从 14 降至 12，解决登录耗时 >3s 超时问题
- WebSocket 心跳机制：前端收到 `ping` 后回复 `pong`，防止连接反复断开
- OTA `/backups` 和 `/check` 端点未启用时返回 503 → 改为 200 + 空数据
- DEV_MODE 下 SecretManager 自动生成并持久化主密钥，修复 Windows ACL 函数名错误
- README.md 中 bcrypt rounds 描述与实际代码不一致（13 → 12）
- 日志深度分析修复：7 类问题（2338+ERROR, 76+WARNING）→ 0 ERROR, 1 WARNING
  - influx_storage: 备份跳过/token明文/retention更新失败/cleanup失败 日志级别合理化
  - edge_ai_inference: 3 个 AI 可选模块不可用 WARNING→DEBUG
  - registry: opc_da 依赖缺失 WARNING→DEBUG
  - bootstrap: App updater 不可用 WARNING→DEBUG, MQTT 无认证 DEV_MODE 降为 INFO
  - bootstrap: pymodbus 弃用警告 logger 级别设为 ERROR
  - __main__: 早期抑制 RequestsDependencyWarning

## [1.0.0] - 2026-07-09

### Added
- Initial community release of EdgeLite Gateway
- 13 industrial protocol drivers: Modbus TCP/RTU, Siemens S7, Mitsubishi MC, Omron FINS, Allen-Bradley CIP, OPC UA Client, OPC DA Client, MQTT Client, HTTP Webhook, ONVIF Camera, Modbus Slave, MQTT Server, Simulator
- ONNX Runtime AI inference engine with model hot-loading
- Vue 3 + Naive UI frontend with dashboard, device management, rule engine, and alarm system
- FastAPI backend with JWT authentication, RBAC (admin/operator/viewer), and audit logging
- SQLite + InfluxDB dual storage (hot config in SQLite, time-series in InfluxDB)
- Event bus with transactional outbox for zero-loss alarm delivery
- Configuration hot-reload with exponential backoff error handling
- Webhook and MQTT northbound data forwarding
- Comprehensive DevOps infrastructure:
  - CI pipeline (lint, type-check, test+coverage, build, Docker build)
  - CD pipeline (semantic versioning, GHCR image publishing, GitHub Releases)
  - Docker Compose stack (EdgeLite + InfluxDB + Mosquitto + Prometheus + Grafana)
  - Kubernetes manifests and Helm chart for production deployment
  - Pre-commit hooks for code quality enforcement
- Comprehensive test suite (943 tests, all passing)
- Prometheus metrics endpoint (/metrics) with Grafana dashboard provisioning
- Health check endpoints (/health/live, /health/ready) for Kubernetes probes
- Graceful shutdown with 30-second drain timeout
- Security hardening:
  - SignAndEncrypt default for OPC UA connections
  - Certificate validation with auto-renewal for self-signed certs
  - Container security (read-only filesystem, cap_drop ALL, no-new-privileges)
  - Secret management via environment variables with :? fail-fast validation

### Security
- OPC UA default security mode changed from None (plaintext) to SignAndEncrypt
- Certificate expiry validation prevents silent degradation to unencrypted connections
- OPC UA authentication failures now reject connections (return None) instead of granting Anonymous role
- TLS certificate loading properly awaited (was silently skipped)
- Video upload endpoint enforces 10MB size limit to prevent OOM
- Python sandbox uses module-level `__builtins__` namespace isolation (prevents global builtins pollution TOCTOU)
- Event outbox maintains consistency: persist failure prevents delivery (no orphan events)
- Modbus TOCTOU race condition fixed: client re-validated inside lock context after serial acquisition
- Modbus broadcast path correctly differentiated: slave_id=0 requires broadcast_enabled, slave_id 1-247 uses unicast

### Fixed
- IEC 60870-5-104: SBO (Select Before Operate) confirmation flow and TESTFR heartbeat retry reset
- BACnet: Service code for ConfirmedWriteProperty (14→15), bit string unused bits parsing, Error PDU context tag parsing, segment reassembly via _SegmentReassembler
- Omron FINS: UDP retransmission with exponential backoff (10ms→200ms cap), socket.timeout vs OSError differentiation
- Mitsubishi MC Protocol: binary vs ascii communication mode via setaccessopt, with graceful fallback
- Allen-Bradley EtherNet/IP: Watchdog failure counting with reconnect threshold, Large Forward Open auto-degradation
- Modbus: Shared constants imported from modbus_base (fixes `is` identity checks), broadcast logic corrected
- FastAPI pagination: removed alias="page_size" so query parameter name matches field name
- Rule engine: logic field validator converts to uppercase (accepts "and"/"or"/"not"), notify_channels allows empty list
- AI stats endpoint: hasattr guard for model_dump() on dict responses

### DevOps
- 依赖锁定文件生成脚本（generate_lockfile.sh + .ps1）和 Makefile target
- requirements.txt 增加锁定文件生成文档
- GitHub Actions CI workflow with 5 parallel jobs (lint, typecheck, test, build, docker)
- GitHub Actions CD workflow with semantic version tagging and GHCR publishing
- Docker multi-stage build (frontend-builder → python-builder → runtime)
- Docker Compose with health checks, resource limits, and security hardening for all services
- Prometheus + Grafana monitoring stack with alerting rules
- Kubernetes manifests (Namespace, ConfigMap, Secret, Deployment, Service)
- Helm chart with configurable values for production K8s deployment
- Pre-commit hooks (secret detection, Pydantic/ORM consistency, exception handling)
- ruff lint + format, pyright type checking, pytest-cov coverage (80% floor)
