<div align="center">
  <img src="logo.svg" width="128" height="128" alt="EdgeLiteGateway Logo" />
</div>

# EdgeLiteGateway Community Edition

**轻量级边缘计算物联网网关 —— 让设备接入像插U盘一样简单**

!\[License]\(https\://img.shields.io/badge/license-GPL--3.0-blue.svg null)
!\[Python]\(https\://img.shields.io/badge/python-3.11%2B-blue null)
!\[Version]\(https\://img.shields.io/badge/version-1.0.0-green null)

***

## 为什么选择 EdgeLiteGateway？

**EdgeLiteGateway 社区版** 专为中小企业边缘计算场景设计：

- **Python 生态** — 开发者群体最大，30分钟开发新驱动，AI/数据分析库天然丰富
- **视频物联网一体化** — 国内首个开源融合网关，无缝对接 GB28181 视频平台
- **极致轻量** — 单容器运行，内存 < 512MB，树莓派/工控机即可部署
- **开箱即用** — 内置 17 种协议驱动、规则引擎、告警系统、时序存储、Web 管理界面

***

## 核心优势

### 1. 全异步高性能架构

基于 Python asyncio 全链路异步设计，单核支持 **1000+ 设备并发采集**。每设备独立协程，事件总线解耦，避免传统多线程模型的上下文切换开销。

### 2. 视频物联网一体化（独有）

通过 PyGBSentry 适配器接入 GB28181 视频流，实现传感器数据与视频监控的统一管理。将传统割裂的安防监控与设备监控合二为一，在一个界面中同时查看设备运行数据和现场视频画面。

### 3. 完整规则引擎 + 告警系统

内置规则引擎支持阈值告警、多条件组合、持续时间窗口、告警收敛和自动恢复通知，无需依赖外部平台即可实现边缘侧智能告警。支持钉钉（加签）、企业微信、邮件 SMTP、自定义 Webhook 四种通知渠道。

### 4. 22 种协议驱动开箱即用

工业协议全覆盖：Modbus TCP/RTU、OPC UA、OPC DA、西门子 S7、三菱 MC、欧姆龙 FINS、Allen Bradley、FANUC CNC、MTConnect、托利多称重、DL/T 645、IEC 104、Sparkplug B、ABB/KUKA 机器人、ONVIF、串口设备、数据库接入、扫码枪。

### 5. 企业级安全体系

- JWT 认证 + RBAC 三角色权限（admin/operator/viewer）
- bcrypt 密码哈希、Token 刷新与吊销
- 结构化日志：JSON 格式、日志轮转、上下文注入

### 6. 断网续传与高可用

InfluxDB 不可用时自动降级到 SQLite 缓存队列（最大 10 万条），网络恢复后自动断点续传，确保边缘场景数据零丢失。

***

## 功能特性

### 协议驱动

| 协议             | 说明                                         |
| -------------- | ------------------------------------------ |
| Modbus TCP/RTU | 工业标准协议，支持线圈/寄存器读写、设备发现、自动重连                |
| OPC UA         | 工业互联协议，支持节点浏览和订阅                           |
| OPC DA         | 经典 OPC 协议，支持 COM 接口数据访问                    |
| MQTT Client    | 订阅设备数据主题，支持 JSON 解析                        |
| HTTP Webhook   | 设备主动 POST 数据，支持 Bearer/Basic 认证            |
| Simulator      | 内置模拟器，支持 fixed/sine/random\_walk/random 模式 |
| 西门子 S7         | S7 系列 PLC 通信协议（snap7）                      |
| 三菱 MC          | 三菱 PLC MC 协议（pymcprotocol）                 |
| 欧姆龙 FINS       | 欧姆龙 PLC FINS 协议                            |
| Allen Bradley  | AB PLC 通信协议（pylogix）                       |
| Fanuc CNC      | 发那科数控系统（pyfanuc）                           |
| MTConnect      | 数控设备标准协议                                   |
| 托利多 (Toledo)   | 称重仪表协议                                     |
| 串口设备           | RS232/RS485 串口通信，支持 Modbus RTU             |
| 数据库接入          | MySQL/PostgreSQL/SQLite/MSSQL，SQL 查询作为测点   |
| 扫码枪            | USB 串口扫码枪，自动帧解析                            |
| 视频接入           | 通过 PyGBSentry 适配器接入 GB28181 视频流            |

### 核心能力

- **事件总线架构** — 基于 asyncio.Queue 的进程内事件总线，模块间完全解耦
- **异步采集调度** — 每设备独立 asyncio.Task，支持超时处理和批量写入
- **规则引擎** — 多条件 AND/OR 逻辑、持续时间窗口、告警收敛、自动恢复
- **告警通知** — 钉钉(加签)、企业微信、邮件(SMTP)、自定义 Webhook
- **时序存储** — InfluxDB 异步写入 + SQLite 配置存储（SQLAlchemy 2.0 + Alembic 迁移）
- **断网缓存** — InfluxDB 不可用时自动降级到 SQLite 队列，联网后续传
- **WebSocket 推送** — 实时数据、告警事件、设备状态三个频道
- **北向对接** — MQTT 转发、IoTSharp/ThingsBoard/华为云 IoTDA/ThingsCloud 平台对接
- **内置服务** — MQTT Server、Modbus Slave（南向仿真）
- **视频接入** — 通过 PyGBSentry 适配器接入 GB28181 视频流，支持云台控制
- **计算表达式** — 支持算术/逻辑/数学函数运算，变量引用 `${device.point}`
- **结构化日志** — JSON 格式输出、日志轮转归档、上下文注入
- **3D 数字孪生** — 基于 Three.js 的 3D 可视化展示
- **Web 组态** — 拖拽式组态编辑器，支持仪表盘/图表/开关/指示灯组件
- **安全体系** — JWT 认证 + RBAC 权限（admin/operator/viewer 三角色）
- **Cython 加速** — 可选 Cython 编译 Modbus 映射和规则比较，3-10x 加速

***

## 安装部署

本项目为**前后端分离**架构：

- **后端**（`src/edgelite/`）：FastAPI 提供 REST API + WebSocket
- **前端**（`web/`）：Vue 3 单页应用，需构建为静态文件

以下每种方式都包含**后端部署**和**前端部署**两个步骤。

***

### 方式一：Docker Compose（推荐，10 分钟上线）

一键启动后端、前端(Nginx)、InfluxDB、Mosquitto 所有服务。

#### 前置条件

- Docker 20.10+
- Docker Compose 2.0+
- Node.js 18+（仅构建前端时需要）

#### 步骤 1：克隆仓库并构建前端

```bash
# 1.1 克隆仓库
git clone https://github.com/suoten/EdgeLiteGateway.git
cd EdgeLiteGateway

# 1.2 安装前端依赖并构建
cd web
npm install
npm run build
cd ..

# 构建完成后，web/dist/ 目录即为前端静态文件
```

> **Windows 用户注意**：如果 `npm install` 卡住，可以尝试 `npm install --registry https://registry.npmmirror.com`

#### 步骤 2：配置环境变量

```bash
# 2.1 复制环境变量示例文件
cp .env.example .env

# 2.2 编辑 .env 文件，修改以下关键配置（生产环境必须修改！）
# INFLUXDB_TOKEN=your-secure-influxdb-token        # InfluxDB API Token，建议 48 位以上随机字符串
# INFLUXDB_PASSWORD=your-influxdb-admin-password   # InfluxDB 管理员密码
# SECRET_KEY=your-jwt-secret-key-at-least-32-chars # JWT 签名密钥，至少 32 位随机字符串

# 快速生成安全密钥（可选）
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 用于 SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"   # 用于 INFLUXDB_TOKEN
```

#### 步骤 3：启动所有服务

```bash
# 3.1 进入 docker 目录并启动
cd docker
docker compose up -d

# 3.2 查看服务状态
docker compose ps

# 3.3 查看后端日志（排查问题用）
docker compose logs -f edgelite

# 3.4 查看 InfluxDB 日志
docker compose logs -f influxdb
```

#### 步骤 4：验证部署

| 服务          | 访问地址                         | 说明                 |
| ----------- | ---------------------------- | ------------------ |
| Web 管理界面    | <http://localhost:3000>      | 前端 Nginx 服务        |
| API 文档      | <http://localhost:8080/docs> | FastAPI Swagger UI |
| InfluxDB UI | <http://localhost:8086>      | 时序数据库管理界面          |

> 首次登录使用默认账号：`admin` / `admin123`

#### Docker Compose 服务说明

| 服务        | 容器名                | 内部端口      | 说明                   |
| --------- | ------------------ | --------- | -------------------- |
| edgelite  | edgelite-gateway   | 8080      | 主服务（API + WebSocket） |
| frontend  | edgelite-frontend  | 80 → 3000 | Nginx 前端静态文件服务       |
| influxdb  | edgelite-influxdb  | 8086      | 时序数据库                |
| mosquitto | edgelite-mosquitto | 1883      | MQTT Broker          |

#### 常用 Docker 命令

```bash
# 停止所有服务
docker compose down

# 停止并删除数据卷（谨慎！会清空时序数据）
docker compose down -v

# 重启单个服务
docker compose restart edgelite

# 进入后端容器调试
docker compose exec edgelite sh

# 查看资源占用
docker stats
```

***

### 方式二：本地开发（前后端分离运行）

适合二次开发，前后端独立运行，前端通过代理访问后端。

#### 前置条件

- Python 3.11+
- Node.js 18+
- InfluxDB 2.x（本地或 Docker 运行）
- Mosquitto（可选，用于 MQTT 功能测试）

#### 步骤 1：启动后端

```bash
# 1.1 进入项目根目录
cd EdgeLiteGateway

# 1.2 创建 Python 虚拟环境（强烈推荐）
python -m venv venv

# Linux/macOS:
source venv/bin/activate
# Windows:
# venv\Scripts\activate

# 1.3 安装后端依赖（含开发依赖）
pip install -e ".[dev]"

# 1.4 复制并编辑配置文件
cp configs/config.example.yaml configs/config.yaml
# 编辑 configs/config.yaml，重点修改：
# - security.secret_key（至少 32 位随机字符串）
# - influxdb.token（InfluxDB API Token）

# 1.5 初始化数据库（创建 SQLite 表结构和默认 admin 用户）
python scripts/init_db.py

# 1.6 启动后端服务（默认端口 8080，热重载模式）
python -m edgelite --host 0.0.0.0 --port 8080 --reload
```

#### 步骤 2：启动前端

```bash
# 2.1 另开一个终端，进入前端目录
cd EdgeLiteGateway/web

# 2.2 安装前端依赖
npm install

# 2.3 启动开发服务器（默认端口 3000，带 API 代理）
npm run dev
```

#### 访问地址

- Web 管理界面：`http://localhost:3000`
- API 文档：`http://localhost:8080/docs`

***

### 方式三：生产部署（宝塔面板 / Nginx）

生产环境推荐用 Nginx 反向代理，前后端同域部署，支持 HTTPS。

#### 前置条件

- Linux 服务器（Ubuntu 20.04+ / CentOS 7+ / Debian 11+）
- Python 3.11+
- Nginx 1.18+
- InfluxDB 2.x
- Mosquitto（可选）

#### 步骤 1：部署后端

```bash
# 1.1 创建应用目录
sudo mkdir -p /opt/edgelite
sudo chown $USER:$USER /opt/edgelite
cd /opt/edgelite

# 1.2 克隆代码
git clone https://github.com/suoten/EdgeLiteGateway.git .

# 1.3 创建 Python 虚拟环境并安装依赖
python3.11 -m venv venv
source venv/bin/activate
pip install -e .

# 1.4 复制并编辑配置
cp configs/config.example.yaml configs/config.yaml
cp .env.example .env

# 编辑 configs/config.yaml，重点修改：
# - security.secret_key: "your-32-char-random-string-here"
# - influxdb.url: "http://localhost:8086"
# - influxdb.token: "your-influxdb-token"
# - logging.level: "WARNING"  # 生产环境建议 WARNING

# 编辑 .env 文件，设置环境变量（可选，优先级高于 config.yaml）
```

#### 步骤 2：初始化数据库

```bash
# 2.1 初始化 SQLite 数据库和默认用户
python scripts/init_db.py

# 输出示例：
# Database initialized at data/edgelite.db
# Default user created: admin / admin123
```

#### 步骤 3：构建并部署前端

```bash
# 3.1 进入前端目录
cd /opt/edgelite/web

# 3.2 确认前端环境变量使用相对路径（同域部署）
# 文件：web/.env
# VITE_API_BASE_URL=/api/v1
# VITE_WS_BASE_URL=/ws/v1

# 3.3 安装依赖并构建
npm install
npm run build

# 3.4 复制构建产物到 Nginx 网站目录
sudo mkdir -p /var/www/edgelite
sudo cp -r dist/* /var/www/edgelite/
sudo chown -R www-data:www-data /var/www/edgelite
```

#### 步骤 4：配置 Nginx

```bash
# 4.1 复制 Nginx 配置
sudo cp nginx/edgelite.conf /etc/nginx/sites-available/edgelite

# 4.2 编辑配置，修改 server_name 为你的域名
sudo nano /etc/nginx/sites-available/edgelite

# 4.3 启用站点
sudo ln -s /etc/nginx/sites-available/edgelite /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

**Nginx 核心配置说明**：

```nginx
server {
    listen 80;
    server_name your-domain.com;
    
    # 前端静态文件
    location / {
        root /var/www/edgelite;
        index index.html;
        try_files $uri $uri/ /index.html;  # SPA 路由回退
    }
    
    # 后端 API 代理
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
    
    # WebSocket 代理（必须！用于实时数据推送）
    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
    
    # FastAPI 自动文档
    location /docs {
        proxy_pass http://127.0.0.1:8080;
    }
    location /redoc {
        proxy_pass http://127.0.0.1:8080;
    }
    location /openapi.json {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

> 完整 Nginx 配置参考 [nginx/edgelite.conf](nginx/edgelite.conf)，宝塔面板详细步骤参考 [nginx/bt-panel.md](nginx/bt-panel.md)

#### 步骤 5：配置 Systemd 守护进程（强烈推荐）

```bash
# 5.1 创建 Systemd 服务文件
sudo tee /etc/systemd/system/edgelite.service > /dev/null << 'EOF'
[Unit]
Description=EdgeLiteGateway - 轻量级边缘计算物联网网关
After=network.target influxdb.service mosquitto.service
Wants=influxdb.service mosquitto.service

[Service]
Type=simple
User=edgelite
Group=edgelite
WorkingDirectory=/opt/edgelite
EnvironmentFile=/opt/edgelite/.env
ExecStart=/opt/edgelite/venv/bin/python -m edgelite --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal
SyslogIdentifier=edgelite

# 安全加固
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/edgelite/data /opt/edgelite/logs /opt/edgelite/backups

[Install]
WantedBy=multi-user.target
EOF

# 5.2 创建专用用户
sudo useradd -r -s /bin/false edgelite

# 5.3 设置目录权限
sudo chown -R edgelite:edgelite /opt/edgelite

# 5.4 创建必要目录
sudo mkdir -p /opt/edgelite/data /opt/edgelite/logs /opt/edgelite/backups
sudo chown -R edgelite:edgelite /opt/edgelite/data /opt/edgelite/logs /opt/edgelite/backups

# 5.5 启动并启用开机自启
sudo systemctl daemon-reload
sudo systemctl enable edgelite
sudo systemctl start edgelite

# 5.6 查看状态
sudo systemctl status edgelite

# 5.7 查看日志
sudo journalctl -u edgelite -f
```

#### 步骤 6：配置 HTTPS（强烈推荐）

```bash
# 6.1 安装 Certbot
sudo apt install certbot python3-certbot-nginx  # Ubuntu/Debian
# sudo yum install certbot python3-certbot-nginx  # CentOS

# 6.2 申请并自动配置 SSL 证书
sudo certbot --nginx -d your-domain.com

# 6.3 证书自动续期已默认启用，可手动测试
sudo certbot renew --dry-run
```

#### 步骤 7：生产环境安全检查清单

| 序号 | 检查项               | 命令/操作                                                          |
| -- | ----------------- | -------------------------------------------------------------- |
| 1  | 修改 admin 默认密码     | Web 界面 → 用户管理 → 修改密码                                           |
| 2  | 配置强 secret\_key   | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| 3  | 配置 InfluxDB Token | 通过环境变量 `EDGELITE_INFLUXDB__TOKEN` 设置                           |
| 4  | 限制 CORS 源         | 修改 `configs/config.yaml` 中 `server.cors_origins`               |
| 5  | 启用 HTTPS          | Certbot 自动配置                                                   |
| 6  | 配置防火墙             | `ufw allow 80/tcp && ufw allow 443/tcp && ufw enable`          |
| 7  | 设置日志级别为 WARNING   | 修改 `configs/config.yaml` 中 `logging.level`                     |
| 8  | 禁用模拟器             | `simulator.auto_create: false`                                 |
| 9  | 配置定时备份            | 参考下方备份脚本                                                       |
| 10 | 禁用 root 运行        | Systemd 配置中指定 `User=edgelite`                                  |

**访问地址**：

- Web 管理界面：`https://your-domain.com`
- API 文档：`https://your-domain.com/docs`

***

### 方式四：Windows 本地部署

```powershell
# 1. 克隆仓库
git clone https://github.com/suoten/EdgeLiteGateway.git
cd EdgeLiteGateway

# 2. 创建虚拟环境
python -m venv venv
venv\Scripts\activate

# 3. 安装依赖
pip install -e ".[dev]"

# 4. 复制配置
copy configs\config.example.yaml configs\config.yaml

# 5. 初始化数据库
python scripts\init_db.py

# 6. 启动后端
python -m edgelite --host 0.0.0.0 --port 8080

# 7. 另开终端启动前端
cd web
npm install
npm run dev
```

> Windows 生产部署建议使用 WSL2 + Docker Compose 方式，或部署到 Linux 服务器。

***

### 默认账号

| 用户名   | 密码       | 角色  | 权限   |
| ----- | -------- | --- | ---- |
| admin | admin123 | 管理员 | 全部权限 |

**生产环境请立即修改默认密码！**

***

## 项目结构

```
EdgeLiteGateway/
├── src/edgelite/           # 后端源码
│   ├── api/                # REST API 路由
│   │   ├── auth.py         #   认证（登录/刷新Token）
│   │   ├── devices.py      #   设备管理（CRUD/测点读写/发现/推送）
│   │   ├── rules.py        #   规则管理（CRUD/启用禁用/测试）
│   │   ├── alarms.py       #   告警管理（列表/确认）
│   │   ├── data.py         #   数据查询（时序查询/导出）
│   │   ├── video.py        #   视频接入（流获取/云台控制）
│   │   ├── system.py       #   系统管理（状态/备份/恢复）
│   │   ├── users.py        #   用户管理（CRUD）
│   │   ├── drivers.py      #   驱动配置（列表/配置模板/设备发现）
│   │   ├── platforms.py    #   平台对接（列表/连接/断开/状态）
│   │   └── expressions.py  #   表达式管理（计算/验证/批量/函数列表）
│   ├── drivers/            # 协议驱动
│   │   ├── base.py         #   DriverPlugin 抽象基类
│   │   ├── registry.py     #   驱动注册表（显式注册）
│   │   ├── modbus_tcp.py   #   Modbus TCP/RTU
│   │   ├── opcua.py        #   OPC UA
│   │   ├── opc_da.py       #   OPC DA
│   │   ├── mqtt_client.py  #   MQTT Client
│   │   ├── http_webhook.py #   HTTP Webhook
│   │   ├── simulator.py    #   模拟器
│   │   ├── s7.py           #   西门子 S7
│   │   ├── mc.py           #   三菱 MC
│   │   ├── fins.py         #   欧姆龙 FINS
│   │   ├── allen_bradley.py #  Allen Bradley
│   │   ├── fanuc.py        #   Fanuc CNC
│   │   ├── mtconnect.py    #   MTConnect
│   │   ├── toledo.py       #   托利多称重
│   │   ├── serial_port.py  #   串口设备
│   │   ├── database_source.py # 数据库接入
│   │   ├── barcode_scanner.py # 扫码枪
│   │   └── video/          #   视频接入（PyGBSentry 适配器）
│   ├── engine/             # 核心引擎
│   │   ├── event_bus.py    #   事件总线
│   │   ├── scheduler.py    #   采集调度器
│   │   ├── evaluator.py    #   规则评估器
│   │   ├── lifecycle.py    #   设备生命周期管理
│   │   ├── mqtt_forwarder.py # MQTT 北向转发
│   │   ├── mqtt_server.py  #   内置 MQTT Server
│   │   ├── modbus_slave.py #   内置 Modbus Slave
│   │   ├── expression_engine.py # 计算表达式引擎
│   │   └── structured_logger.py # 结构化日志系统
│   ├── models/             # Pydantic 数据模型 + SQLAlchemy ORM
│   ├── security/           # 安全模块（JWT/RBAC/密码/Token吊销）
│   ├── services/           # 业务服务层
│   ├── storage/            # 存储层（SQLite/InfluxDB/缓存）
│   ├── platform/           # 北向平台对接（IoTSharp/ThingsBoard/华为云IoTDA/ThingsCloud）
│   └── _cython/            # Cython 加速模块（可选）
├── web/                    # 前端源码（Vue 3 + Naive UI + TypeScript）
│   └── src/
│       ├── api/index.ts    #   API 封装层
│       ├── views/
│       │   ├── system/
│       │   │   ├── DriverConfig.vue     #   驱动配置页
│       │   │   ├── PlatformConfig.vue   #   平台对接配置页
│       │   │   └── ExpressionConfig.vue #   表达式编辑器页
│       │   ├── digital-twin/DigitalTwin.vue # 3D数字孪生页
│       │   └── scada/ScadaEditor.vue    #   Web组态编辑器页
│       └── ...
├── configs/                # 配置文件
├── docker/                 # Docker 配置（Dockerfile + docker-compose.yml）
├── nginx/                  # Nginx 反向代理配置 + 宝塔部署指南
├── tests/                  # 测试用例（pytest + pytest-asyncio）
├── scripts/                # 工具脚本（init_db.py 等）
└── docs/                   # 文档（架构/部署/开发/用户指南）
```

***

## 技术栈

### 后端

| 技术                         | 用途                | 版本     |
| -------------------------- | ----------------- | ------ |
| Python 3.11+               | 运行时               | ≥3.11  |
| FastAPI                    | Web 框架（异步原生，自动文档） | ≥0.110 |
| Pydantic                   | 数据校验与配置管理         | ≥2.6   |
| SQLAlchemy 2.0 + aiosqlite | SQLite 异步 ORM     | ≥2.0   |
| Alembic                    | 数据库迁移             | -      |
| InfluxDB Client            | 时序数据库             | ≥1.44  |
| pymodbus                   | Modbus 协议         | ≥3.7   |
| aiomqtt                    | MQTT 客户端          | ≥2.1   |
| asyncua                    | OPC UA 客户端/服务端    | ≥1.1   |
| python-jose                | JWT Token         | ≥3.3   |
| passlib + bcrypt           | 密码哈希              | ≥1.7   |

### 前端

| 技术         | 用途       | 版本   |
| ---------- | -------- | ---- |
| Vue 3.4    | UI 框架    | ≥3.4 |
| TypeScript | 类型安全     | ≥5.0 |
| Naive UI   | 组件库      | ≥2.0 |
| Pinia      | 状态管理     | ≥2.0 |
| ECharts    | 图表       | ≥5.0 |
| Axios      | HTTP 客户端 | ≥1.6 |
| Vite       | 构建工具     | ≥5.0 |

***

## 配置说明

### 1. 复制配置文件

```bash
cp configs/config.example.yaml configs/config.yaml
```

### 2. 环境变量（推荐用于生产环境）

复制环境变量示例文件：

```bash
# 后端环境变量
cp .env.example .env

# Docker 环境变量
cd docker && cp .env.example .env && cd ..

# 前端环境变量
cd web && cp .env.example .env && cd ..
```

支持 `EDGELITE_` 前缀的环境变量覆盖配置（两级嵌套用双下划线 `__` 分隔）：

| 环境变量                             | 对应配置项                 | 说明              |
| -------------------------------- | --------------------- | --------------- |
| `EDGELITE_CONFIG`                | -                     | 配置文件路径          |
| `EDGELITE_SERVER__HOST`          | server.host           | 监听地址            |
| `EDGELITE_SERVER__PORT`          | server.port           | 监听端口            |
| `EDGELITE_DATABASE__SQLITE_PATH` | database.sqlite\_path | SQLite 路径       |
| `EDGELITE_INFLUXDB__URL`         | influxdb.url          | InfluxDB 地址     |
| `EDGELITE_INFLUXDB__TOKEN`       | influxdb.token        | InfluxDB Token  |
| `EDGELITE_INFLUXDB__ORG`         | influxdb.org          | InfluxDB 组织     |
| `EDGELITE_INFLUXDB__BUCKET`      | influxdb.bucket       | InfluxDB Bucket |
| `EDGELITE_MQTT__BROKER`          | mqtt.broker           | MQTT Broker 地址  |
| `EDGELITE_MQTT__PORT`            | mqtt.port             | MQTT Broker 端口  |
| `EDGELITE_SECURITY__SECRET_KEY`  | security.secret\_key  | JWT 签名密钥        |
| `EDGELITE_SECURITY__ALGORITHM`   | security.algorithm    | JWT 算法          |
| `EDGELITE_LOGGING__LEVEL`        | logging.level         | 日志级别            |

示例：

```bash
export EDGELITE_CONFIG=/opt/edgelite/config.yaml
export EDGELITE_INFLUXDB__URL=http://influxdb:8086
export EDGELITE_INFLUXDB__TOKEN=your-secure-token
export EDGELITE_SECURITY__SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

详细配置项参见 [configs/config.example.yaml](configs/config.example.yaml) 和 [.env.example](.env.example)。

***

## API 文档

启动服务后访问 FastAPI 自动文档：

- Swagger UI: `http://localhost:8080/docs`
- ReDoc: `http://localhost:8080/redoc`

### 主要端点

| 路由前缀              | 功能                    |
| ----------------- | --------------------- |
| `/api/v1/auth`    | 认证（登录/刷新Token）        |
| `/api/v1/devices` | 设备管理（CRUD/测点读写/发现/推送） |
| `/api/v1/rules`   | 规则管理（CRUD/启用禁用/测试）    |
| `/api/v1/alarms`  | 告警管理（列表/确认）           |
| `/api/v1/data`    | 数据查询（时序查询/导出）         |
| `/api/v1/video`   | 视频接入（流获取/云台控制）        |
| `/api/v1/system`  | 系统管理（状态/备份/恢复）        |
| `/api/v1/users`   | 用户管理（CRUD）            |

\| `/api/v1/drivers` | 驱动配置（列表/配置模板/设备发现） |
\| `/api/v1/platforms` | 平台对接（列表/连接/断开/状态） |
\| `/api/v1/expressions` | 表达式管理（计算/验证/批量/函数列表） |

### WebSocket

```
ws://host:8080/ws/v1/realtime?token=xxx  # 实时数据推送
ws://host:8080/ws/v1/alarm?token=xxx     # 告警事件推送
ws://host:8080/ws/v1/device?token=xxx    # 设备状态推送
```

***

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

***

## 版本与路线图

EdgeLiteGateway 采用三级版本体系，满足不同场景需求：

### 社区版

面向个人开发者、教育机构和中小型企业，提供完整的边缘计算网关基础能力。

| 模块 | 功能说明 |
|------|----------|
| **协议驱动** | 22 种工业协议开箱即用（Modbus TCP/RTU、OPC UA、OPC DA、西门子 S7、三菱 MC、欧姆龙 FINS、Allen Bradley、FANUC CNC、MTConnect、托利多、DL/T 645、IEC 104、Sparkplug B、ABB/KUKA、ONVIF、串口、数据库、扫码枪） |
| **内置服务** | MQTT Server（设备接入）、Modbus Slave（南向仿真） |
| **规则引擎** | 阈值告警、多条件 AND/OR 逻辑、持续时间窗口、告警收敛、自动恢复 |
| **告警通知** | 钉钉（加签）、企业微信、邮件 SMTP、自定义 Webhook |
| **时序存储** | InfluxDB 异步写入 + SQLite 断网缓存队列（最大 10 万条） |
| **视频接入** | 通过 PyGBSentry 适配器接入 GB28181 视频流，支持云台控制 |
| **北向对接** | IoTSharp、ThingsBoard、华为云 IoTDA、ThingsCloud |
| **安全体系** | JWT 认证 + RBAC 三角色权限、bcrypt 密码哈希、Token 刷新与吊销 |
| **Web 管理** | Vue 3 + Naive UI 现代化界面，支持实时数据、告警、设备状态监控 |
| **扩展能力** | 计算表达式引擎、Cython 可选加速、MCP 协议支持 |

### 企业版

在社区版基础上，增加企业级特性和高阶协议支持，满足生产环境严苛要求。

| 新增模块 | 功能说明 |
|----------|----------|
| **协议扩展** | BACnet 楼宇自控协议、OPC UA Server（暴露网关数据供 SCADA/MES 订阅） |
| **高可用** | 主备集群、健康检查、故障自动切换 |
| **审计日志** | 防篡改哈希链、异常登录检测、CSV 导出、完整性校验 |
| **身份集成** | LDAP / Active Directory 统一认证 |
| **国密安全** | SM2/SM4 国密算法加密，符合等保 2.0 要求 |
| **存储升级** | TDengine 国产时序数据库（更高压缩率、更低查询延迟） |
| **消息队列** | Kafka 北向对接，支持大规模数据流处理 |
| **数据库扩展** | 达梦数据库支持 |

### 云边协同版

面向集团型企业和智慧城市场景，实现多节点统一管理、边云协同和 AI 增强。

| 新增模块 | 功能说明 |
|----------|----------|
| **K8s Operator** | Kubernetes 原生部署，自动扩缩容、滚动升级、配置管理 |
| **边云同步** | 边缘节点与云端平台双向数据同步，断网自治、联网续传 |
| **边缘 AI 推理** | 集成 ONNX Runtime，支持工业异常检测、预测性维护模型本地推理 |
| **集中管控** | 云端统一监控所有边缘节点状态、版本、配置 |

---

## 许可证

[GPL-3.0](LICENSE) License

***

## 技术支持

- GitHub Issues: <https://github.com/suoten/EdgeLiteGateway/issues>
- Gitee Issues: <https://gitee.com/suoten/EdgeLiteGateway/issues>
- 作者邮箱: <suoten@163.com>

**欢迎 Star ⭐ 和 Fork，您的支持是我们持续开发的动力！**
