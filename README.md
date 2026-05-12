<div align="center">

# ⚡ EdgeLiteGateway

### 轻量级边缘计算物联网网关 —— 让设备接入像插U盘一样简单

[![License](https://img.shields.io/github/license/suoten/EdgeLiteGateway?color=blue&label=license)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vue](https://img.shields.io/badge/Vue-3.4%2B-4FC08D?logo=vue.js&logoColor=white)](https://vuejs.org/)
[![Version](https://img.shields.io/badge/version-1.0.0--community-brightgreen)](https://github.com/suoten/EdgeLiteGateway)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

**🇨🇳 国内首个开源 Python 边缘计算网关 | 🎯 22 种工业协议开箱即用 | 📹 视频物联网一体化 | 🚀 10 分钟 Docker 部署**

[快速开始](#-快速开始) · [功能特性](#-功能特性) · [安装部署](#-安装部署) · [技术架构](#-技术架构) · [版本对比](#-版本与路线图) · [技术支持](#-技术支持)

</div>

***

## 🌟 为什么选择 EdgeLiteGateway？

> **如果你正在寻找一个轻量、易用、功能完整的边缘计算网关，EdgeLiteGateway 就是为你而生的。**

| 特性          | EdgeLiteGateway | 传统 C/Java 网关 |
| :---------- | :-------------: | :----------: |
| **开发新驱动**   | 🐍 Python 30 分钟 |   C/Java 数天  |
| **内存占用**    |     < 512MB     |     1-4GB    |
| **部署方式**    |    单容器 / 树莓派    |     专用服务器    |
| **视频接入**    |  ✅ GB28181 一体化  |    ❌ 需额外平台   |
| **Web 管理**  |  ✅ 现代化 Vue 3 界面 |  ❌ 命令行或简陋 UI |
| **规则引擎**    |    ✅ 内置，可视化配置   |    ❌ 需外部引擎   |
| **AI/数据分析** | ✅ Python 生态天然支持 |    ❌ 集成困难    |
| **学习曲线**    |  🟢 低，Python 全栈 |  🔴 高，多语言混合  |

### 四大核心优势

#### 🐍 Python 生态 — 开发者群体最大

Python 是全球开发者最多的语言之一。30 分钟即可开发新驱动，丰富的 AI/数据分析库（NumPy、Pandas、ONNX Runtime）天然可用，无需跨语言桥接。

#### 📹 视频物联网一体化 — 国内首创

通过 PyGBSentry 适配器接入 GB28181 视频流，实现传感器数据与视频监控的**统一管理**。在一个界面中同时查看设备运行数据和现场视频画面，将传统割裂的安防监控与设备监控合二为一。

#### 🪶 极致轻量 — 树莓派即可运行

单容器运行，内存 < 512MB。无需专用服务器，树莓派、工控机、边缘盒子均可部署。Docker Compose 一键启动，10 分钟上线。

#### 🧩 开箱即用 — 22 种协议驱动

工业协议全覆盖：Modbus TCP/RTU、OPC UA、OPC DA、西门子 S7、三菱 MC、欧姆龙 FINS、Allen Bradley、FANUC CNC、MTConnect、托利多称重、DL/T 645、IEC 104、Sparkplug B、ABB/KUKA 机器人、ONVIF、串口设备、数据库接入、扫码枪、HTTP Webhook、MQTT Client、模拟器。

***

## 🚀 快速开始

```bash
# 1️⃣ 克隆仓库
git clone https://github.com/suoten/EdgeLiteGateway.git
cd EdgeLiteGateway

# 2️⃣ 构建前端
cd web && npm install && npm run build && cd ..

# 3️⃣ 一键启动
cd docker && docker compose up -d

# 4️⃣ 打开浏览器
# 👉 http://localhost:3000
# 🔑 首次登录请使用管理员账号（详见下方"默认账号"章节）
```

> 💡 **就这么简单！** Docker Compose 会自动启动后端、前端(Nginx)、InfluxDB、Mosquitto 所有服务。

***

## ✨ 功能特性

### 📡 协议驱动（22 种开箱即用）

| 协议                 | 说明                                         | 适用场景                      |
| ------------------ | ------------------------------------------ | ------------------------- |
| **Modbus TCP/RTU** | 工业标准协议，支持线圈/寄存器读写、设备发现、自动重连                | PLC、仪表、变频器                |
| **OPC UA**         | 工业互联协议，支持节点浏览和订阅                           | SCADA/MES 对接              |
| **OPC DA**         | 经典 OPC 协议，支持 COM 接口数据访问                    | 传统 Windows 工控             |
| **西门子 S7**         | S7 系列 PLC 通信协议（snap7）                      | S7-200/300/400/1200/1500  |
| **三菱 MC**          | 三菱 PLC MC 协议（pymcprotocol）                 | Q/L/FX 系列                 |
| **欧姆龙 FINS**       | 欧姆龙 PLC FINS 协议                            | CJ/CP/NJ 系列               |
| **Allen Bradley**  | AB PLC 通信协议（pylogix），支持 CIP/PCCC           | ControlLogix/CompactLogix |
| **FANUC CNC**      | 发那科数控系统（pyfanuc）                           | 数控机床                      |
| **MTConnect**      | 数控设备标准协议                                   | CNC 设备监控                  |
| **托利多 (Toledo)**   | 称重仪表协议，支持 TCP/Serial/MT-SICS               | 称重系统                      |
| **DL/T 645**       | 电力部电表协议（2007版）                             | 智能电表                      |
| **IEC 104**        | 电力远动协议                                     | 电力系统                      |
| **Sparkplug B**    | 工业物联网 MQTT 规范                              | IIoT 场景                   |
| **KUKA 机器人**       | EKRL 协议                                    | 工业机器人                     |
| **ABB 机器人**        | RWS REST API                               | 工业机器人                     |
| **ONVIF**          | 视频设备发现/RTSP/PTZ                            | 视频监控                      |
| **MQTT Client**    | 订阅设备数据主题，支持 JSON 解析                        | IoT 设备                    |
| **HTTP Webhook**   | 设备主动 POST 数据，支持 Bearer/Basic 认证            | 第三方集成                     |
| **串口设备**           | RS232/RS485 串口通信，支持 Modbus RTU             | 串口仪表                      |
| **数据库接入**          | MySQL/PostgreSQL/SQLite/MSSQL，SQL 查询作为测点   | 数据库数据源                    |
| **扫码枪**            | USB 串口扫码枪，自动帧解析                            | 产线追溯                      |
| **模拟器**            | 内置模拟器，支持 fixed/sine/random\_walk/random 模式 | 开发测试                      |

### 🧠 核心能力

| 能力               | 说明                                                                         |
| ---------------- | -------------------------------------------------------------------------- |
| **事件总线架构**       | 基于 asyncio.Queue 的进程内事件总线，模块间完全解耦                                          |
| **异步采集调度**       | 每设备独立 asyncio.Task，支持超时处理和批量写入，单核 1000+ 设备并发                               |
| **规则引擎**         | 多条件 AND/OR 逻辑、持续时间窗口、告警收敛、自动恢复                                             |
| **告警通知**         | 钉钉(加签)、企业微信、邮件(SMTP)、自定义 Webhook 四种通知渠道                                    |
| **时序存储**         | InfluxDB 异步写入 + SQLite 配置存储（SQLAlchemy 2.0 + Alembic 迁移）                   |
| **断网缓存**         | InfluxDB 不可用时自动降级到 SQLite 队列（最大 10 万条），联网后续传                               |
| **WebSocket 推送** | 实时数据、告警事件、设备状态三个频道，前端实时刷新                                                  |
| **北向对接**         | MQTT 转发、IoTSharp/ThingsBoard/华为云 IoTDA/ThingsCloud/ThingsPanel/自定义MQTT平台对接 |
| **内置服务**         | MQTT Server（设备接入）、Modbus Slave（南向仿真）                                       |
| **视频接入**         | 通过 PyGBSentry 适配器接入 GB28181 视频流，支持云台控制                                     |
| **计算表达式**        | 支持算术/逻辑/数学函数运算，变量引用 `${device.point}`，安全沙箱                                 |
| **数据预处理**        | 死区滤波、滑动平均、时间聚合窗口，减少无效数据上报                                                  |
| **MCP 协议**       | Model Context Protocol 支持，提供工具/资源/提示模板三大能力                                 |
| **OTA 升级**       | 远程检查更新、下载、SHA256 校验、应用、回滚全流程                                               |
| **Grafana 集成**   | 自动创建数据源和仪表板，iframe 嵌入监控视图                                                  |
| **审计日志**         | 完整操作记录、完整性校验、CSV 导出、日志清理                                                   |
| **多网关级联**        | WebSocket 集成端点，支持子网关数据上报和反控                                                |
| **串口透传**         | RS232/RS485 串口到 TCP 桥接，支持 IP 白名单和最大客户端数                                    |
| **插件热加载**        | 自动发现插件目录，支持加载/重载/卸载，自定义驱动扩展                                                |
| **3D 数字孪生**      | 基于 Three.js 的 3D 可视化展示，支持工厂车间/智慧园区/能源站场景                                   |
| **Web 组态**       | 拖拽式组态编辑器，支持仪表盘/趋势图/开关/指示灯/文本组件                                             |
| **安全体系**         | JWT 认证 + RBAC 权限（admin/operator/viewer 三角色）+ Token 刷新与吊销                   |
| **Cython 加速**    | 可选 Cython 编译 Modbus 映射和规则比较，3-10x 加速                                       |
| **结构化日志**        | JSON 格式输出、日志轮转归档、上下文注入                                                     |

### 🖥️ Web 管理界面

现代化 Vue 3 + Naive UI 界面，提供完整的可视化管理：

| 页面               | 功能                                 |
| ---------------- | ---------------------------------- |
| **仪表盘**          | 设备/规则/告警统计、设备状态分布、协议分布、告警趋势、资源使用趋势 |
| **设备管理**         | 设备 CRUD、测点读写、模拟器创建、设备发现、批量操作       |
| **设备详情**         | 实时测点值、时序图表、视频监控+云台控制               |
| **规则管理**         | 规则 CRUD、多条件组合、持续时间窗口、启用/禁用、规则测试    |
| **告警管理**         | 告警列表、状态筛选、级别筛选、告警确认、实时推送           |
| **数据查询**         | 时序数据查询、ECharts 图表、统计信息、CSV 导出      |
| **驱动配置**         | 驱动列表、配置模板查看、设备发现                   |
| **平台对接**         | 平台列表、两步式添加、连接/断开、配置管理              |
| **表达式编辑器**       | 表达式计算/验证/批量测试、函数列表、运算符参考           |
| **数据预处理**        | 全局配置、测点级配置（死区/滤波/聚合）               |
| **审计日志**         | 日志查询、完整性校验、CSV 导出、日志清理             |
| **服务管理**         | 服务总览、启用/停用、依赖安装、配置更新               |
| **MQTT Server**  | 启停管理、连接数监控、配置更新                    |
| **Modbus Slave** | 启停管理、寄存器映射、配置更新                    |
| **串口透传**         | 启停管理、流量统计                          |
| **OTA 升级**       | 检查更新、应用更新、版本回滚                     |
| **Grafana 集成**   | 服务管理、仪表板列表、iframe 嵌入               |
| **MCP Server**   | 工具/资源/提示模板管理、API Key 管理            |
| **用户管理**         | 用户 CRUD、角色分配、密码管理                  |
| **3D 数字孪生**      | Three.js 3D 场景、设备可视化、实时数据          |
| **Web 组态**       | 拖拽编辑器、组件绑定、预览模式、项目保存               |

***

## 📦 安装部署

本项目为**前后端分离**架构：

- **后端**（`src/edgelite/`）：FastAPI 提供 REST API + WebSocket
- **前端**（`web/`）：Vue 3 单页应用，需构建为静态文件

***

### 方式一：Docker Compose（推荐，10 分钟上线）

一键启动后端、前端(Nginx)、InfluxDB、Mosquitto 所有服务。

#### 前置条件

- Docker 20.10+
- Docker Compose 2.0+
- Node.js 18+（仅构建前端时需要）

**最低硬件要求**：

| 资源   | 最低配置                | 推荐配置                  |
| ---- | ------------------- | --------------------- |
| CPU  | 1 核                 | 2 核+                  |
| 内存   | 1 GB                | 2 GB+                 |
| 磁盘   | 2 GB                | 10 GB+                |
| 操作系统 | Linux/Windows/macOS | Linux (Ubuntu 20.04+) |

> 💡 树莓派 4B (4GB) 即可流畅运行！

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

> 首次登录请使用管理员账号，详见下方"默认账号"章节

> ⚠️ **首次启动注意事项**：
>
> 1. InfluxDB 首次启动需要约 30 秒完成初始化（创建 org/bucket/token），期间后端日志可能出现 `InfluxDB connection refused`，这是正常的，后端会自动降级到 SQLite 缓存模式
> 2. 等待约 1 分钟后所有服务就绪，刷新页面即可正常使用
> 3. 如果 1 分钟后仍有问题，检查 InfluxDB 状态：`docker compose logs influxdb | tail -20`
> 4. Mosquitto MQTT Broker 默认允许匿名连接（开发用途），生产环境请配置认证

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

# 1.4 复制环境变量模板并编辑
cp .env.example .env
# 编辑 .env 文件，重点修改：
# - EDGELITE_SECURITY__SECRET_KEY（至少 32 位随机字符串，生产环境必须修改！）
# - EDGELITE_INFLUXDB__TOKEN（InfluxDB API Token）
# 快速生成密钥: python -c "import secrets; print(secrets.token_urlsafe(32))"

# 1.5 复制并编辑配置文件
cp configs/config.example.yaml configs/config.yaml
# 编辑 configs/config.yaml，重点修改：
# - security.secret_key（至少 32 位随机字符串，也可通过 .env 设置）
# - influxdb.token（InfluxDB API Token，也可通过 .env 设置）

# 1.6 初始化数据库（创建 SQLite 表结构和默认 admin 用户）
python scripts/init_db.py

# 1.7 启动后端服务（默认端口 8080，热重载模式）
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
# VITE_WS_BASE_URL=  （留空，WebSocket 路径由代码自动拼接）

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

|  序号 | 检查项               | 命令/操作                                                          |
| :-: | ----------------- | -------------------------------------------------------------- |
|  1  | 修改 admin 默认密码     | Web 界面 → 用户管理 → 修改密码                                           |
|  2  | 配置强 secret\_key   | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
|  3  | 配置 InfluxDB Token | 通过环境变量 `EDGELITE_INFLUXDB__TOKEN` 设置                           |
|  4  | 限制 CORS 源         | 修改 `configs/config.yaml` 中 `server.cors_origins`               |
|  5  | 启用 HTTPS          | Certbot 自动配置                                                   |
|  6  | 配置防火墙             | `ufw allow 80/tcp && ufw allow 443/tcp && ufw enable`          |
|  7  | 设置日志级别为 WARNING   | 修改 `configs/config.yaml` 中 `logging.level`                     |
|  8  | 禁用模拟器             | `simulator.auto_create: false`                                 |
|  9  | 配置定时备份            | 参考下方备份脚本                                                       |
|  10 | 禁用 root 运行        | Systemd 配置中指定 `User=edgelite`                                  |

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

**⚠️ 生产环境请立即修改默认密码！密码策略：至少8位，必须包含字母和数字。**

***

## 🏗️ 技术架构

### 项目结构

```
EdgeLiteGateway/
├── src/edgelite/           # 后端源码
│   ├── api/                # REST API 路由（22 个模块，96 个端点）
│   ├── drivers/            # 协议驱动（22 种）
│   ├── engine/             # 核心引擎（事件总线/调度/规则/表达式/OTA/插件）
│   ├── models/             # Pydantic 数据模型 + SQLAlchemy ORM
│   ├── security/           # 安全模块（JWT/RBAC/密码/Token吊销）
│   ├── services/           # 业务服务层（9 个服务）
│   ├── storage/            # 存储层（SQLite/InfluxDB/缓存）
│   ├── platform/           # 北向平台对接（6 种平台）
│   └── _cython/            # Cython 加速模块（可选）
├── web/                    # 前端源码（Vue 3 + Naive UI + TypeScript）
│   └── src/
│       ├── api/            #   API 封装层（HTTP + WebSocket）
│       ├── views/          #   24 个页面组件
│       ├── components/     #   通用组件
│       ├── layouts/        #   布局组件
│       ├── stores/         #   Pinia 状态管理
│       └── router/         #   路由配置
├── configs/                # 配置文件
├── docker/                 # Docker 配置（Dockerfile + docker-compose.yml）
├── nginx/                  # Nginx 反向代理配置 + 宝塔部署指南
├── tests/                  # 测试用例（pytest + pytest-asyncio）
├── scripts/                # 工具脚本（init_db.py 等）
└── docs/                   # 文档（架构/部署/开发/用户指南）
```

### 技术栈

#### 后端

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

#### 前端

| 技术         | 用途       | 版本   |
| ---------- | -------- | ---- |
| Vue 3.4    | UI 框架    | ≥3.4 |
| TypeScript | 类型安全     | ≥5.0 |
| Naive UI   | 组件库      | ≥2.0 |
| Pinia      | 状态管理     | ≥2.0 |
| ECharts    | 图表       | ≥5.0 |
| Three.js   | 3D 可视化   | -    |
| Axios      | HTTP 客户端 | ≥1.6 |
| Vite       | 构建工具     | ≥5.0 |

***

## ⚙️ 配置说明

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
| `EDGELITE_DRIVERS__CUSTOM_DIR`   | drivers.custom\_dir   | 自定义驱动目录         |
| `EDGELITE_DRIVERS__AUTO_RELOAD`  | drivers.auto\_reload  | 驱动自动重载          |
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

## 📡 API 文档

启动服务后访问 FastAPI 自动文档：

- Swagger UI: `http://localhost:8080/docs`
- ReDoc: `http://localhost:8080/redoc`

### 主要端点

| 路由前缀                    | 功能                           |
| ----------------------- | ---------------------------- |
| `/api/v1/auth`          | 认证（登录/刷新Token/登出/修改密码）       |
| `/api/v1/devices`       | 设备管理（CRUD/测点读写/发现/推送）        |
| `/api/v1/rules`         | 规则管理（CRUD/启用禁用/测试）           |
| `/api/v1/alarms`        | 告警管理（列表/确认）                  |
| `/api/v1/data`          | 数据查询（时序查询/导出）                |
| `/api/v1/video`         | 视频接入（流获取/云台控制）               |
| `/api/v1/system`        | 系统管理（状态/备份/恢复）               |
| `/api/v1/users`         | 用户管理（CRUD）                   |
| `/api/v1/drivers`       | 驱动配置（列表/配置模板/设备发现）           |
| `/api/v1/platforms`     | 平台对接（列表/连接/断开/状态）            |
| `/api/v1/expressions`   | 表达式管理（计算/验证/批量/函数列表）         |
| `/api/v1/mcp`           | MCP协议（工具调用/资源/提示模板/认证密钥CRUD） |
| `/api/v1/scada`         | 组态管理（项目保存/加载/删除，服务端持久化）      |
| `/api/v1/mqtt-server`   | 内置MQTT Server（启停/状态/配置）      |
| `/api/v1/modbus-slave`  | 内置Modbus Slave（启停/状态/配置）     |
| `/api/v1/preprocess`    | 数据预处理（配置/死区滤波/滑动平均/聚合）       |
| `/api/v1/serial-bridge` | 串口透传（启停/状态）                  |
| `/api/v1/ota`           | OTA升级（检查/应用/回滚/备份）           |
| `/api/v1/grafana`       | Grafana集成（配置/仪表板/嵌入URL）      |
| `/api/v1/audit`         | 审计日志（查询/完整性校验/CSV导出/清理）      |
| `/api/v1/integration`   | 多网关级联（握手/状态）                 |

### WebSocket

```
ws://host:8080/ws/v1/realtime?token=xxx  # 实时数据推送
ws://host:8080/ws/v1/alarm?token=xxx     # 告警事件推送
ws://host:8080/ws/v1/device?token=xxx    # 设备状态推送
ws://host:8080/ws/v1/integration?token=xxx # 级联数据推送
```

***

## 🔧 开发

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

## 🗺️ 版本与路线图

EdgeLiteGateway 采用三级版本体系，满足不同场景需求：

### 🆓 社区版 (v1.0) — 你正在这里

面向个人开发者、教育机构和中小型企业，提供完整的边缘计算网关基础能力。

| 模块         | 功能说明                                                             |
| ---------- | ---------------------------------------------------------------- |
| **协议驱动**   | 22 种工业协议开箱即用                                                     |
| **内置服务**   | MQTT Server（设备接入）、Modbus Slave（南向仿真）                             |
| **规则引擎**   | 阈值告警、多条件 AND/OR 逻辑、持续时间窗口、告警收敛、自动恢复                              |
| **告警通知**   | 钉钉（加签）、企业微信、邮件 SMTP、自定义 Webhook                                  |
| **时序存储**   | InfluxDB 异步写入 + SQLite 断网缓存队列（最大 10 万条）                          |
| **视频接入**   | 通过 PyGBSentry 适配器接入 GB28181 视频流，支持云台控制                           |
| **北向对接**   | IoTSharp、ThingsBoard、华为云 IoTDA、ThingsCloud、ThingsPanel、自定义MQTT平台 |
| **安全体系**   | JWT 认证 + RBAC 三角色权限、bcrypt 密码哈希、Token 刷新与吊销                      |
| **Web 管理** | Vue 3 + Naive UI 现代化界面，24 个功能页面                                  |
| **扩展能力**   | 计算表达式引擎、Cython 可选加速、MCP 协议支持                                     |

### 🏢 企业版 (v1.2)

在社区版基础上，增加企业级特性和高阶协议支持，满足生产环境严苛要求。

| 新增模块      | 功能说明                                              |
| --------- | ------------------------------------------------- |
| **协议扩展**  | BACnet 楼宇自控协议、OPC UA Server（暴露网关数据供 SCADA/MES 订阅） |
| **高可用**   | 主备集群、健康检查、故障自动切换                                  |
| **审计日志**  | 防篡改哈希链、异常登录检测、CSV 导出、完整性校验                        |
| **身份集成**  | LDAP / Active Directory 统一认证                      |
| **国密安全**  | SM2/SM4 国密算法加密，符合等保 2.0 要求                        |
| **存储升级**  | TDengine 国产时序数据库（更高压缩率、更低查询延迟）                    |
| **消息队列**  | Kafka 北向对接，支持大规模数据流处理                             |
| **数据库扩展** | 达梦数据库支持                                           |

### ☁️ 云边协同版 (v2.0)

面向集团型企业和智慧城市场景，实现多节点统一管理、边云协同和 AI 增强。

| 新增模块             | 功能说明                                 |
| ---------------- | ------------------------------------ |
| **K8s Operator** | Kubernetes 原生部署，自动扩缩容、滚动升级、配置管理      |
| **边云同步**         | 边缘节点与云端平台双向数据同步，断网自治、联网续传            |
| **边缘 AI 推理**     | 集成 ONNX Runtime，支持工业异常检测、预测性维护模型本地推理 |
| **集中管控**         | 云端统一监控所有边缘节点状态、版本、配置                 |

***

## 📄 许可证

[GPL-3.0](LICENSE) License

***

## ❓ 常见问题

### 安装相关

**Q:** **`npm install`** **卡住或报错怎么办？**

使用国内镜像源：

```bash
npm install --registry https://registry.npmmirror.com
```

**Q:** **`pip install`** **安装依赖失败？**

确保 Python 版本 >= 3.11，并尝试：

```bash
pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**Q: Windows 上 Cython 编译失败？**

Cython 是可选加速模块，编译失败不影响运行。系统会自动回退到纯 Python 实现。如需编译，请安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)。

**Q:** **`ModuleNotFoundError: No module named 'sqlalchemy'`？**

确保使用 `pip install -e ".[dev]"` 安装（而非仅 `pip install -r requirements.txt`），`pyproject.toml` 中已包含所有核心依赖。

**Q: Docker Compose 启动后前端页面空白？**

确保先构建前端再启动：

```bash
cd web && npm install && npm run build && cd ..
cd docker && docker compose up -d --build
```

### 运行相关

**Q: 后端启动报** **`InfluxDB connection failed`？**

InfluxDB 服务未就绪，后端会自动降级到 SQLite 缓存模式。检查 InfluxDB 是否运行：

```bash
docker compose ps influxdb
curl http://localhost:8086/health
```

**Q: 后端启动报** **`JWT密钥未配置，已自动生成随机密钥`？**

系统会自动生成随机密钥并保存到 `.env` 文件，重启后密钥不会丢失。但生产环境强烈建议手动设置固定密钥：

```bash
export EDGELITE_SECURITY__SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

或在 `.env` 文件中设置 `EDGELITE_SECURITY__SECRET_KEY=你的随机密钥`（至少32位）。

**Q: 前端** **`npm run build`** **报** **`require is not defined`？**

确保 Node.js 版本 >= 18，且已安装最新依赖（`npm install`）。vite.config.ts 已使用 `createRequire` 兼容 ESM 模式。

**Q: WebSocket 连接失败？**

1. 确保 Nginx 配置了 `/ws/` 的 WebSocket 代理（参考上方 Nginx 配置）
2. 检查 Token 是否有效（WebSocket 连接需要 `?token=xxx` 参数）
3. 检查防火墙是否放行了对应端口

**Q: 设备创建后状态一直是"离线"？**

1. 检查设备连接配置（IP/端口/从站ID）是否正确
2. 确保网络可达：`ping <设备IP>` 或 `telnet <设备IP> <端口>`
3. 查看后端日志：`docker compose logs -f edgelite | grep error`

**Q: 告警通知不发送？**

1. 确认规则已启用
2. 确认通知渠道配置正确（钉钉 Webhook URL / 邮件 SMTP 服务器等）
3. 在 `configs/config.yaml` 中配置通知参数

**Q: MCP API Key 重启后丢失？**

v1.0 已支持密钥文件持久化（`data/mcp_keys.json`），确保 `data/` 目录有写入权限。

### 数据相关

**Q: 时序数据查询为空？**

1. 确认 InfluxDB 服务正常运行
2. 确认设备有数据写入：在设备详情页查看实时测点值
3. 检查查询时间范围是否正确

**Q: 如何备份数据？**

```bash
# 通过 API 创建备份
curl -X POST http://localhost:8080/api/v1/system/backup -H "Authorization: Bearer <token>"

# 手动备份关键文件
cp -r data/ data_backup_$(date +%Y%m%d)/
```

**Q: 如何升级到新版本？**

```bash
# 1. 备份数据
cp -r data/ data_backup_$(date +%Y%m%d)/

# 2. 拉取最新代码
git pull origin master

# 3. 重新构建前端
cd web && npm install && npm run build && cd ..

# 4. 重启服务
cd docker && docker compose up -d --build
```

**Q: 如何添加自定义协议驱动？**

**方式一：使用自定义驱动目录（推荐，无需修改核心代码）**

1. 创建驱动文件，继承 `DriverPlugin` 类并实现必要方法
2. 在配置文件中指定自定义驱动目录：

```yaml
# configs/config.yaml
drivers:
  custom_dir: /path/to/your/custom/drivers
```

或通过环境变量：

```bash
export EDGELITE_DRIVERS__CUSTOM_DIR=/path/to/your/custom/drivers
```

1. 重启服务，驱动将被自动发现和加载

**方式二：在内置驱动目录中添加**

1. 在 `src/edgelite/drivers/` 目录下创建新的驱动文件
2. 继承 `DriverPlugin` 类并实现 `start()`、`stop()`、`read_points()`、`write_point()` 方法
3. 在 `registry.py` 的 `_driver_modules` 列表中注册驱动
4. 重启服务即可自动加载

详见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 中的驱动开发指南。

**Q: 如何配置 HTTPS？**

生产环境推荐使用 Nginx + Certbot 自动配置 SSL 证书，详见上方"方式三：生产部署"中的 HTTPS 配置步骤。

**Q: 支持哪些数据库？**

- 配置存储：SQLite（内置，零配置）
- 时序数据：InfluxDB 2.x（社区版）/ TDengine（企业版）
- 未来计划：DuckDB 嵌入式分析（企业版可选）

**Q: 如何查看系统日志？**

```bash
# Docker 部署
docker compose logs -f edgelite

# Systemd 部署
sudo journalctl -u edgelite -f

# 本地开发
# 日志输出到控制台，级别可在 configs/config.yaml 中调整
```

### 安全相关

**Q: 默认密码安全吗？**

默认账号 `admin/admin123` 仅用于初次登录，系统会提示修改密码。生产环境请务必修改：

1. Web 界面登录后，点击右上角头像 → 修改密码
2. 修改 `configs/config.yaml` 中的 `security.secret_key`
3. 通过环境变量 `EDGELITE_SECURITY__SECRET_KEY` 设置强密钥

**Q: 如何限制 CORS 访问？**

在 `configs/config.yaml` 中修改 `server.cors_origins`，仅允许指定域名访问：

```yaml
server:
  cors_origins:
    - "https://your-domain.com"
```

***

## 🔗 相关项目

| 项目                                                 | 说明                         | 仓库地址                                                                                          |
| -------------------------------------------------- | -------------------------- | --------------------------------------------------------------------------------------------- |
| [ProtoForge](https://github.com/suoten/ProtoForge) | 物联网协议仿真与测试平台，17 种工业协议全链路仿真 | [Gitee](https://gitee.com/suoten/ProtoForge) · [GitHub](https://github.com/suoten/ProtoForge) |

***

## 💬 技术支持

- GitHub Issues: <https://github.com/suoten/EdgeLiteGateway/issues>
- Gitee Issues: <https://gitee.com/suoten/EdgeLiteGateway/issues>
- 作者邮箱: <suoten@163.com>

<div align="center">

**如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！**

**你的 Star 是我们持续开发的动力！**

</div>
