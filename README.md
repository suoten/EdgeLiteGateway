<div align="center">
  <img src="logo.svg" width="128" height="128" alt="EdgeLiteGateway Logo" />
</div>

# EdgeLiteGateway Community Edition

**轻量级边缘计算物联网网关**

![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Version](https://img.shields.io/badge/version-1.0.0-green)

EdgeLiteGateway 是一个全异步架构的轻量级边缘计算物联网网关，支持多种工业协议接入、规则引擎、告警系统、时序数据存储和北向平台对接，适用于工业物联网、智慧园区、设备监控等边缘计算场景。

> Python 包名为 `edgelite`，通过 `pip install edgelite` 或 `python -m edgelite` 启动。

## 功能特性

### 协议驱动

| 协议 | 说明 |
|------|------|
| Modbus TCP | 工业标准协议，支持线圈/寄存器读写、设备发现 |
| OPC UA | 工业互联协议，支持节点浏览和订阅 |
| MQTT Client | 订阅设备数据主题，支持 JSON 解析 |
| HTTP Webhook | 设备主动 POST 数据，无需认证 |
| Simulator | 内置模拟器，支持 fixed/sine/random_walk/random 模式 |

### 核心能力

- **事件总线架构** - 基于 asyncio.Queue 的进程内事件总线，模块间完全解耦
- **异步采集调度** - 每设备独立 asyncio.Task，支持超时处理和批量写入
- **规则引擎** - 多条件 AND/OR 逻辑、持续时间窗口、告警收敛、自动恢复
- **告警通知** - 钉钉(加签)、企业微信、邮件(SMTP)、自定义 Webhook
- **时序存储** - InfluxDB 异步写入 + SQLite 配置存储
- **断网缓存** - InfluxDB 不可用时自动降级到 SQLite 队列，联网后续传
- **WebSocket 推送** - 实时数据、告警事件、设备状态三个频道
- **北向对接** - MQTT 转发、IoTSharp/ThingsBoard 平台对接
- **内置服务** - MQTT Server、OPC UA Server、Modbus Slave（南向仿真）
- **视频接入** - 通过 PyGBSentry 适配器接入 GB28181 视频流
- **安全体系** - JWT 认证 + RBAC 权限（admin/operator/viewer 三角色 17 权限）
- **Cython 加速** - 可选 Cython 编译 Modbus 映射和规则比较，3-10x 加速

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
# 克隆仓库
git clone https://github.com/suoten/EdgeLiteGateway.git
cd EdgeLiteGateway

# 复制并编辑配置
cp configs/config.example.yaml configs/config.yaml

# 启动所有服务（EdgeLiteGateway + InfluxDB + Mosquitto）
cd docker
docker compose up -d

# 查看日志
docker compose logs -f edgelite

# 访问服务
# API 文档: http://localhost:8080/docs
# Web 管理: http://localhost:3000
```

### 方式二：本地开发

```bash
# 克隆仓库
git clone https://github.com/suoten/EdgeLiteGateway.git
cd EdgeLiteGateway

# 安装后端依赖
pip install -e ".[dev]"

# 复制并编辑配置
cp configs/config.example.yaml configs/config.yaml

# 启动后端
python -m edgelite --host 0.0.0.0 --port 8080

# 启动前端（另一个终端）
cd web
npm install
npm run dev
```

### 默认账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| admin | admin123 | 管理员 |

**生产环境请立即修改默认密码！**

## 项目结构

```
EdgeLiteGateway/
├── src/edgelite/           # 后端源码
│   ├── api/                # REST API 路由（auth/devices/rules/alarms/data/video/system/users）
│   ├── drivers/            # 协议驱动
│   │   ├── base.py         #   DriverPlugin 抽象基类
│   │   ├── registry.py     #   驱动注册表（自动发现）
│   │   ├── modbus_tcp.py   #   Modbus TCP
│   │   ├── opcua.py        #   OPC UA
│   │   ├── mqtt_client.py  #   MQTT Client
│   │   ├── http_webhook.py #   HTTP Webhook
│   │   ├── simulator.py    #   模拟器
│   │   └── video/          #   视频接入（PyGBSentry 适配器）
│   ├── engine/             # 核心引擎
│   │   ├── event_bus.py    #   事件总线
│   │   ├── scheduler.py    #   采集调度器
│   │   ├── evaluator.py    #   规则评估器
│   │   ├── lifecycle.py    #   设备生命周期管理
│   │   ├── mqtt_forwarder.py # MQTT 北向转发
│   │   ├── mqtt_server.py  #   内置 MQTT Server
│   │   ├── opcua_server.py #   内置 OPC UA Server
│   │   └── modbus_slave.py #   内置 Modbus Slave
│   ├── models/             # Pydantic 数据模型
│   ├── security/           # 安全模块（JWT/RBAC/密码）
│   ├── services/           # 业务服务层
│   ├── storage/            # 存储层（SQLite/InfluxDB/缓存）
│   ├── platform/           # 北向平台对接（IoTSharp/ThingsBoard）
│   ├── ws/                 # WebSocket 实时推送
│   └── _cython/            # Cython 加速模块（可选）
├── web/                    # 前端源码（Vue 3 + Naive UI）
├── configs/                # 配置文件
├── docker/                 # Docker 配置
├── tests/                  # 测试用例
├── scripts/                # 工具脚本
└── docs/                   # 文档
```

## 技术栈

### 后端

| 技术 | 用途 |
|------|------|
| Python 3.11+ | 运行时 |
| FastAPI | Web 框架 |
| Pydantic | 数据校验 |
| SQLAlchemy + aiosqlite | SQLite 异步 ORM |
| InfluxDB Client | 时序数据库 |
| pymodbus | Modbus 协议 |
| aiomqtt | MQTT 客户端 |
| asyncua | OPC UA 客户端 |
| python-jose | JWT Token |
| passlib + bcrypt | 密码哈希 |

### 前端

| 技术 | 用途 |
|------|------|
| Vue 3.4 + TypeScript | UI 框架 |
| Naive UI | 组件库 |
| Pinia | 状态管理 |
| ECharts | 图表 |
| Axios | HTTP 客户端 |
| Vite | 构建工具 |

## 配置说明

复制 `configs/config.example.yaml` 为 `configs/config.yaml` 并修改。支持 `EDGELITE_` 前缀的环境变量覆盖：

```bash
export EDGELITE_INFLUXDB_URL=http://influxdb:8086
export EDGELITE_MQTT_BROKER=mosquitto
export EDGELITE_SECURITY_SECRET_KEY=your-secret-key
```

详细配置项参见 [configs/config.example.yaml](configs/config.example.yaml)。

## API 文档

启动服务后访问 FastAPI 自动文档：

- Swagger UI: `http://localhost:8080/docs`
- ReDoc: `http://localhost:8080/redoc`

### 主要端点

| 路由前缀 | 功能 |
|---------|------|
| `/api/v1/auth` | 认证（登录/刷新Token） |
| `/api/v1/devices` | 设备管理（CRUD/测点读写/发现/推送） |
| `/api/v1/rules` | 规则管理（CRUD/启用禁用/测试） |
| `/api/v1/alarms` | 告警管理（列表/确认） |
| `/api/v1/data` | 数据查询（时序查询/导出） |
| `/api/v1/video` | 视频接入（流获取/云台控制） |
| `/api/v1/system` | 系统管理（状态/备份/恢复） |
| `/api/v1/users` | 用户管理（CRUD） |

### WebSocket

```
ws://host:8080/ws/v1/realtime?token=xxx  # 实时数据推送
ws://host:8080/ws/v1/alarm?token=xxx     # 告警事件推送
ws://host:8080/ws/v1/device?token=xxx    # 设备状态推送
```

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest tests/ -v

# 代码检查
ruff check src/

# 格式化
ruff format src/

# 热重载开发
python -m edgelite --reload
```

详见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

## 许可证

[GPL-3.0](LICENSE) License
