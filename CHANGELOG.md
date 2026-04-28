# Changelog

本项目的所有重要变更均记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Changed

- **数据库架构重构**：引入 SQLAlchemy 2.0 + Alembic 替代手写 DDL，支持多数据库后端
  - 元数据存储后端：SQLite（默认）/ MySQL / PostgreSQL / MSSQL
  - ORM 模型：`models/db.py`（DeviceORM, RuleORM, AlarmORM, UserORM, AuditLogORM, CacheQueueORM）
  - 数据库迁移：Alembic 框架（`alembic/` 目录）
  - 多后端工厂模式：`storage/database.py` 根据 `database.backend` 配置自动选择驱动
  - Repository 层：`storage/sqlite_repo.py` 改为基于 SQLAlchemy AsyncSession 的 ORM 操作
- **环境变量格式统一**：`EDGELITE_<SECTION>__<KEY>` 双下划线分隔（Django 风格），避免歧义
- **恢复 python-dotenv 支持**：自动加载 `.env` 文件
- **安全修复**：`database_source.py` 的 `write_point` 方法从字符串拼接改为参数化查询
- **配置增强**：`.env.example` 覆盖所有配置项，`config.example.yaml` 添加 `.env` 替代说明
- **TDengine 配置完善**：添加 `TDengineConfig` Pydantic 模型 + YAML 配置段 + app.py 初始化逻辑
- **Bug 修复**：`audit_logs` 索引引用不存在的 `timestamp` 列 → 改为 `created_at`
- **Bug 修复**：`devices` 表 protocol CHECK 约束补全缺失的 14 个协议

## [1.0.0] - 2025-04-25

### Added

- 全异步架构的边缘计算物联网网关
- 17 种协议驱动：Modbus TCP、OPC UA、OPC DA、MQTT Client、HTTP Webhook、Simulator、西门子 S7、三菱 MC、欧姆龙 FINS、Allen Bradley、Fanuc CNC、MTConnect、托利多、BACnet、串口设备、数据库接入、扫码枪
- 事件总线（EventBus）实现模块间解耦
- 异步采集调度器，每设备独立 Task
- 规则引擎：多条件 AND/OR、持续时间窗口、告警收敛、自动恢复
- 告警通知：钉钉(加签)、企业微信、邮件(SMTP)、自定义 Webhook
- InfluxDB 时序存储 + SQLite 配置存储
- 断网缓存：InfluxDB 不可用时自动降级到 SQLite 队列
- WebSocket 实时推送：realtime/alarm/device 三个频道
- MQTT 北向数据转发
- 北向平台对接：IoTSharp、ThingsBoard、华为云 IoTDA、ThingsCloud
- 内置服务：MQTT Server、OPC UA Server、Modbus Slave
- 视频接入：PyGBSentry (GB28181) 适配器
- 计算表达式引擎：算术/比较/逻辑运算、数学函数、变量引用 `${device.point}`
- 审计日志：防篡改哈希链、异常登录检测、CSV导出、完整性校验
- 结构化日志：JSON格式输出、日志轮转归档、上下文注入
- 3D 数字孪生：基于 Three.js 的3D可视化展示
- Web 组态 (SCADA)：拖拽式组态编辑器，支持仪表盘/图表/开关/指示灯组件
- JWT 认证 + RBAC 权限（admin/operator/viewer 三角色 21 权限）
- Cython 加速模块（可选，纯 Python 回退保证兼容性）
- Vue 3 + Naive UI Web 管理界面
- Docker Compose 部署（生产 + 开发模式，含前端 Nginx 服务）
- Nginx 反向代理配置 + 宝塔面板部署指南
- 环境变量覆盖配置（EDGELITE_ 前缀）
- 系统备份/恢复功能
- 设备发现（Modbus 扫描）
- 模拟器设备自动创建
- 驱动配置管理页面（DriverConfig.vue）
- 平台对接配置页面（PlatformConfig.vue）
- 表达式编辑器页面（ExpressionConfig.vue）
- 审计日志页面（AuditLog.vue）

### Changed

- 项目名称从 EdgeLite 更名为 EdgeLiteGateway
