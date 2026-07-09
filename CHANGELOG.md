# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [1.1.2] - 2026-05-25

### 驱动增强

| 驱动 | 增强功能 | 说明 |
|------|----------|------|
| **BACnet/IP** | SubscribeCOV订阅模式 | 增加COV变化订阅、批量读取优化 |
| **KNXnet/IP** | 事件订阅模式 | 增加组值变化事件回调、批量读取优化 |
| **Mitsubishi MC** | 批量读取 | 增加并发批量读取、batch_size配置 |
| **Omron FINS** | 批量读取 | 增加并发批量读取、batch_size配置 |
| **StreamCompute** | Scheduler集成 | 增加EventBus订阅、事件总线集成 |

---

## [1.1.1] - 2026-05-25

### 驱动增强

| 驱动 | 增强功能 | 说明 |
|------|----------|------|
| **MTConnect** | 数据推送模式 | 增加on_data回调、连接统计、路径浏览 |
| **Sparkplug B** | 命令响应+批量优化 | DCMD命令响应、批量发布间隔可配 |
| **OPC DA** | 订阅模式+重连 | OPC组订阅、自动重连机制、服务器列表 |

---

## [1.1.0] - 2026-05-25

### 新增功能

| 类别 | 新增功能 | 说明 |
|------|----------|------|
| **楼宇自动化** | BACnet/IP | ASHRAE 135标准协议，支持HVAC/照明/门禁设备接入 |
| **楼宇自动化** | KNXnet/IP | EN 50090欧洲标准协议，支持楼宇自控设备 |
| **运动控制** | Profinet/DCP | 工业以太网协议，支持运动控制设备发现与配置 |
| **运动控制** | EtherCAT | Beckhoff高速以太网协议，支持亚毫秒运动控制 |
| **电力/水务** | DNP3 | IEEE 1815标准协议，支持配电自动化/水务SCADA |
| **边缘计算** | 流计算引擎 (CEP) | 实时流处理、窗口聚合、模式检测、异常检测 |
| **协议转换** | 协议转换网关 | 多协议桥接、Modbus↔OPC UA映射、数据类型转换 |
| **安全增强** | TLS/mTLS | 双向认证、证书管理、自签名CA、设备证书 |
| **监控集成** | Prometheus指标端点 | /metrics端点，支持Prometheus采集 |
| **性能优化** | 协议驱动数量 22→28 | 新增6种工业协议驱动 |
