# EdgeLite Gateway — 架构设计文档

## 1. 系统概览

EdgeLite 是一个边缘计算网关平台，采用分层微服务架构，支持多种工业协议接入、数据处理和北向转发。

```
┌─────────────────────────────────────────────────────────────┐
│                    Frontend (Vue 3 + Vite)                   │
├─────────────────────────────────────────────────────────────┤
│                    API Layer (FastAPI)                       │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐          │
│  │  Auth   │ │ Devices │ │  Data   │ │ System  │  ...      │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘          │
├───────┼──────────┼──────────┼──────────┼──────────────────┤
│       │   Service Layer (Business Logic)                    │
│  ┌────┴────┐ ┌───┴─────┐ ┌──┴────┐ ┌───┴─────┐            │
│  │ Device  │ │  Rule   │ │ Alarm │ │ Notify  │  ...       │
│  │ Service │ │ Service │ │Service│ │ Service │            │
│  └────┬────┘ └────┬────┘ └───┬───┘ └────┬────┘            │
├───────┼──────────┼──────────┼─────┼────────┼───────────────┤
│       │   Driver Layer (Protocol Adapters)                  │
│  ┌────┴────────────────────────────────────┐               │
│  │ Modbus TCP/RTU │ OPC UA │ MQTT │ BACnet │  ...         │
│  └─────────────────────────────────────────┘               │
├─────────────────────────────────────────────────────────────┤
│                   Storage Layer                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐      │
│  │  SQLite  │ │ InfluxDB │ │  Cache   │ │  Logs    │      │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘      │
└─────────────────────────────────────────────────────────────┘
```

## 2. 分层架构

### 2.1 API Layer (`src/edgelite/api/`)
- **职责**: HTTP/REST 端点定义、请求验证、响应序列化
- **依赖**: Service Layer（通过依赖注入）
- **规则**: API 层不直接访问数据库或驱动，必须通过 Service Layer

### 2.2 Service Layer (`src/edgelite/services/`)
- **职责**: 业务逻辑编排、事务管理、跨驱动协调
- **依赖**: Storage Layer、Driver Layer
- **规则**: Service 层不导入 API 层模块（禁止跨层依赖）

### 2.3 Driver Layer (`src/edgelite/drivers/`)
- **职责**: 工业协议适配（Modbus、OPC UA、BACnet、MQTT 等）
- **依赖**: Engine Layer（采集调度）、Storage Layer（数据写入）
- **规则**: 驱动通过 `registry.py` 注册，支持热插拔

### 2.4 Engine Layer (`src/edgelite/engine/`)
- **职责**: 采集调度、AI 推理、规则引擎、事件总线
- **依赖**: Driver Layer（通过抽象接口）、Storage Layer
- **规则**: 引擎层不依赖 API 层（已通过 `packet_recorder.py` 中立模块消除跨层依赖）

### 2.5 Storage Layer (`src/edgelite/storage/`)
- **职责**: 数据持久化（SQLite、InfluxDB）、缓存管理
- **依赖**: 无上层依赖
- **规则**: 存储层是最底层，不依赖任何上层模块

### 2.6 Security Layer (`src/edgelite/security/`)
- **职责**: JWT 认证、RBAC 授权、CSRF 防护、密码策略
- **依赖**: 独立模块，被 API Layer 和 Service Layer 引用

## 3. 依赖规则

```
API → Service → Storage
              → Driver → Engine → Storage
Security (独立，被各层引用)
Config (独立，被各层引用)
```

**禁止的依赖方向**:
- Driver/Engine → API（违反分层架构）
- Storage → API/Service（反向依赖）
- Security → API（安全层不应依赖 API 定义）

## 4. 关键设计决策

### 4.1 中立模块模式
- `packet_recorder.py`: 消除 Engine→API 跨层依赖的中立模块
- `protocol_keys.py`: 协议键名标准化，避免各层硬编码

### 4.2 依赖注入
- FastAPI `Depends()` 用于 API→Service 依赖注入
- `app.state` 作为 ServiceContainer，运行时注入服务实例

### 4.3 配置管理
- 优先级: 环境变量 > .env 文件 > config.yaml
- Pydantic 模型校验，生产环境强制校验敏感配置

## 5. 模块清单

| 目录 | 职责 | 关键文件 |
|------|------|----------|
| `src/edgelite/api/` | REST API 端点 | `auth.py`, `devices.py`, `health.py` |
| `src/edgelite/services/` | 业务逻辑 | `device_service.py`, `rule_service.py` |
| `src/edgelite/drivers/` | 协议驱动 | `modbus_tcp.py`, `opcua.py`, `mqtt_client_driver.py` |
| `src/edgelite/engine/` | 采集引擎 | `scheduler.py`, `edge_ai_inference.py` |
| `src/edgelite/storage/` | 数据存储 | `sqlite_repo.py`, `influx_storage.py`, `sqlite_ts.py` |
| `src/edgelite/security/` | 安全模块 | `jwt.py`, `csrf.py`, `password.py`, `rbac.py` |
| `src/edgelite/config.py` | 配置管理 | `AppConfig`, `load_config()` |
| `src/edgelite/app.py` | 应用入口 | `create_app()`, ServiceContainer |
| `src/edgelite/packet_recorder.py` | 中立模块 | `record_packet()` |
