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
- **安全体系** - JWT 认证 + RBAC 权限（admin/operator/viewer 三角色 21 权限）
- **Cython 加速** - 可选 Cython 编译 Modbus 映射和规则比较，3-10x 加速

## 安装部署

本项目为**前后端分离**架构：
- **后端**（`src/edgelite/`）：FastAPI 提供 REST API + WebSocket
- **前端**（`web/`）：Vue 3 单页应用，需构建为静态文件

以下每种方式都包含**后端部署**和**前端部署**两个步骤。

---

### 方式一：Docker Compose（推荐）

一键启动后端、前端、InfluxDB、Mosquitto 所有服务。

```bash
# 1. 克隆仓库
git clone https://github.com/suoten/EdgeLiteGateway.git
cd EdgeLiteGateway

# 2. 复制并编辑配置
cp configs/config.example.yaml configs/config.yaml
cp .env.example .env

# 3. 启动所有服务
cd docker
docker compose up -d

# 4. 查看日志
docker compose logs -f edgelite
```

**访问地址**：
- Web 管理界面：`http://localhost:3000`
- API 文档：`http://localhost:8080/docs`

---

### 方式二：本地开发（前后端分离运行）

适合二次开发，前后端独立运行，前端通过代理访问后端。

**步骤 1：启动后端**

```bash
# 进入项目根目录
cd EdgeLiteGateway

# 安装后端依赖
pip install -e ".[dev]"

# 复制配置
cp configs/config.example.yaml configs/config.yaml

# 启动后端服务（默认端口 8080）
python -m edgelite --host 0.0.0.0 --port 8080
```

**步骤 2：启动前端**

```bash
# 另开一个终端，进入前端目录
cd EdgeLiteGateway/web

# 安装前端依赖
npm install

# 启动开发服务器（默认端口 3000，带代理）
npm run dev
```

**访问地址**：
- Web 管理界面：`http://localhost:3000`
- API 文档：`http://localhost:8080/docs`

---

### 方式三：生产部署（宝塔 / Nginx）

生产环境推荐用 Nginx 反向代理，前后端同域部署。

**步骤 1：部署后端**

```bash
# 1.1 创建 Python 虚拟环境并安装依赖
cd /opt/edgelite
python3.11 -m venv venv
source venv/bin/activate
pip install -e .

# 1.2 复制并编辑配置
cp configs/config.example.yaml configs/config.yaml
cp .env.example .env

# 1.3 编辑 configs/config.yaml，修改安全密钥和数据库配置
#     重点修改：security.secret_key（至少32位随机字符串）

# 1.4 启动后端（测试用）
python -m edgelite --host 127.0.0.1 --port 8080
```

**步骤 2：构建并部署前端**

```bash
# 2.1 进入前端目录
cd /opt/edgelite/web

# 2.2 确认前端环境变量使用相对路径（同域部署）
#     文件：web/.env
#     VITE_API_BASE_URL=/api/v1
#     VITE_WS_BASE_URL=/ws/v1

# 2.3 安装依赖并构建
npm install
npm run build

# 2.4 复制构建产物到 Nginx 网站目录
mkdir -p /var/www/edgelite
cp -r dist/* /var/www/edgelite/
```

**步骤 3：配置 Nginx**

宝塔面板 → 网站 → 添加站点 → 根目录选 `/var/www/edgelite` → PHP 版本选**纯静态** → 设置 → 配置文件，在原有配置中**添加**以下内容：

```nginx
    # --- 后端 API 代理 ---
    location /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }

    # --- FastAPI 自动文档 ---
    location /docs {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /redoc {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /openapi.json {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
    }

    # --- WebSocket 代理 ---
    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
```

> 完整 Nginx 配置参考 [nginx/edgelite.conf](nginx/edgelite.conf)，宝塔详细步骤参考 [nginx/bt-panel.md](nginx/bt-panel.md)

**步骤 4：配置 Systemd 守护后端（可选但推荐）**

```bash
# 创建服务文件
cat > /etc/systemd/system/edgelite.service << 'EOF'
[Unit]
Description=EdgeLiteGateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/edgelite
EnvironmentFile=/opt/edgelite/.env
ExecStart=/opt/edgelite/venv/bin/python -m edgelite --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 启动并启用开机自启
systemctl daemon-reload
systemctl enable edgelite
systemctl start edgelite
```

**访问地址**：
- Web 管理界面：`http://你的域名` 或 `https://你的域名`
- API 文档：`http://你的域名/docs`

---

### 默认账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| admin | admin123 | 管理员 |

**生产环境请立即修改默认密码！**

## 项目结构

```
EdgeLiteGateway/
├── src/edgelite/           # 后端源码
│   ├── routers/            # REST API 路由（auth/devices/rules/alarms/data/video/system/users）
│   ├── drivers/            # 协议驱动
│   │   ├── base.py         #   DriverPlugin 抽象基类
│   │   ├── registry.py     #   驱动注册表（显式注册）
│   │   ├── modbus_tcp.py   #   Modbus TCP
│   │   ├── opcua.py        #   OPC UA
│   │   ├── mqtt_client.py  #   MQTT Client
│   │   ├── http_webhook.py #   HTTP Webhook
│   │   ├── simulator.py    #   模拟器
│   │   └── video/          #   视频接入（PyGBSentry 适配器）
│   ├── engine/             # 核心引擎
│   │   ├── event_bus.py    #   事件总线
│   │   ├── collector.py    #   采集调度器
│   │   ├── rule_evaluator.py # 规则评估器
│   │   ├── lifecycle.py    #   设备生命周期管理
│   │   ├── mqtt_forwarder.py # MQTT 北向转发
│   │   ├── mqtt_server.py  #   内置 MQTT Server
│   │   ├── opcua_server.py #   内置 OPC UA Server
│   │   └── modbus_slave.py #   内置 Modbus Slave
│   ├── models/             # Pydantic 数据模型
│   ├── security/           # 安全模块（JWT/RBAC/密码）
│   ├── services/           # 业务服务层
│   ├── storage/            # 存储层（SQLite/InfluxDB/缓存）
│   ├── platforms/          # 北向平台对接（IoTSharp/ThingsBoard）
│   ├── notify/             # 告警通知渠道（钉钉/邮件/企微/Webhook）
│   └── _cython/            # Cython 加速模块（可选）
├── web/                    # 前端源码（Vue 3 + Naive UI）
├── configs/                # 配置文件
├── docker/                 # Docker 配置
├── nginx/                  # Nginx 反向代理配置 + 宝塔部署指南
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

### 1. 复制配置文件

```bash
cp configs/config.example.yaml configs/config.yaml
```

### 2. 环境变量（可选）

复制环境变量示例文件：

```bash
# 后端环境变量
cp .env.example .env

# Docker 环境变量
cd docker && cp .env.example .env && cd ..

# 前端环境变量
cd web && cp .env.example .env && cd ..
```

支持 `EDGELITE_` 前缀的环境变量覆盖配置（两级嵌套用 `_` 连接）：

| 环境变量 | 对应配置项 | 说明 |
|---------|-----------|------|
| `EDGELITE_CONFIG` | - | 配置文件路径 |
| `EDGELITE_SERVER_HOST` | server.host | 监听地址 |
| `EDGELITE_SERVER_PORT` | server.port | 监听端口 |
| `EDGELITE_DATABASE_SQLITE_PATH` | database.sqlite_path | SQLite 路径 |
| `EDGELITE_INFLUXDB_URL` | influxdb.url | InfluxDB 地址 |
| `EDGELITE_INFLUXDB_TOKEN` | influxdb.token | InfluxDB Token |
| `EDGELITE_INFLUXDB_ORG` | influxdb.org | InfluxDB 组织 |
| `EDGELITE_INFLUXDB_BUCKET` | influxdb.bucket | InfluxDB Bucket |
| `EDGELITE_MQTT_BROKER` | mqtt.broker | MQTT Broker 地址 |
| `EDGELITE_MQTT_PORT` | mqtt.port | MQTT Broker 端口 |
| `EDGELITE_SECURITY_SECRET_KEY` | security.secret_key | JWT 签名密钥 |
| `EDGELITE_SECURITY_ALGORITHM` | security.algorithm | JWT 算法 |
| `EDGELITE_LOGGING_LEVEL` | logging.level | 日志级别 |

示例：

```bash
export EDGELITE_CONFIG=/opt/edgelite/config.yaml
export EDGELITE_INFLUXDB_URL=http://influxdb:8086
export EDGELITE_INFLUXDB_TOKEN=your-secure-token
export EDGELITE_SECURITY_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

详细配置项参见 [configs/config.example.yaml](configs/config.example.yaml) 和 [.env.example](.env.example)。

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
