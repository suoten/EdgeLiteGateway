# Changelog

本项目的所有重要变更均记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2025-04-25

### Added

- 全异步架构的边缘计算物联网网关
- 5 种协议驱动：Modbus TCP、OPC UA、MQTT Client、HTTP Webhook、Simulator
- 事件总线（EventBus）实现模块间解耦
- 异步采集调度器，每设备独立 Task
- 规则引擎：多条件 AND/OR、持续时间窗口、告警收敛、自动恢复
- 告警通知：钉钉(加签)、企业微信、邮件(SMTP)、自定义 Webhook
- InfluxDB 时序存储 + SQLite 配置存储
- 断网缓存：InfluxDB 不可用时自动降级到 SQLite 队列
- WebSocket 实时推送：realtime/alarm/device 三个频道
- MQTT 北向数据转发
- 北向平台对接：IoTSharp、ThingsBoard
- 内置服务：MQTT Server、OPC UA Server、Modbus Slave
- 视频接入：PyGBSentry (GB28181) 适配器
- JWT 认证 + RBAC 权限（admin/operator/viewer 三角色 17 权限）
- Cython 加速模块（可选，纯 Python 回退保证兼容性）
- Vue 3 + Naive UI Web 管理界面
- Docker Compose 部署（生产 + 开发模式）
- 系统备份/恢复功能
- 设备发现（Modbus 扫描）
- 模拟器设备自动创建

### Changed

- 项目名称从 EdgeLite 更名为 EdgeLiteGateway
