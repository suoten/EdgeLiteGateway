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

---

## 🚀 快速开始

> **请先确保你的电脑有 Docker**：打开终端输入 `docker --version` 有版本号即为已安装。
> 没有安装？去 [Docker 官网](https://docs.docker.com/get-docker/) 下载 Docker Desktop，安装后重启电脑即可。

### 方式一：Docker 一键部署（推荐小白使用）

```bash
# 1. 克隆仓库（二选一）
git clone https://github.com/suoten/EdgeLiteGateway.git    # GitHub（海外）
git clone https://gitee.com/suoten/EdgeLiteGateway.git      # Gitee（国内推荐）

# 2. 进入项目目录
cd EdgeLiteGateway

# 3. 一键部署（自动完成前端构建 + 启动全部服务）
#    Windows PowerShell 请复制下面第一行，Linux/Mac 终端复制第二行
powershell -Command "cd web; npm install; npm run build; cd ..; Copy-Item docker\.env.example docker\.env; cd docker; docker compose up -d"

# Linux/Mac 终端用这一行：
cd web && npm install && npm run build && cd .. && cp docker/.env.example docker/.env && cd docker && docker compose up -d

# 4. 等待约 2 分钟（首次需下载 Docker 镜像），然后浏览器打开：
open http://localhost:3000    # Linux
start http://localhost:3000   # Windows
```

**首次登录**：
- 地址：`http://localhost:3000`
- 账号：`admin`
- 密码：`admin123`
- ⚠️ 登录后请立即修改密码！

<details>
<summary>🎯 一键部署做了什么？（点击展开）</summary>

| 步骤 | 操作 | 耗时 | 说明 |
|------|------|------|------|
| 1 | 安装前端依赖 | 1-2 分钟 | `npm install` 安装 Vue 项目依赖 |
| 2 | 构建前端页面 | 1-2 分钟 | `npm run build` 编译生产版静态文件 |
| 3 | 生成 `.env` 配置 | 瞬间 | 从模板创建 Docker 环境变量文件 |
| 4 | 启动 4 个容器 | 1-2 分钟 | `docker compose up -d` 后台启动网关/前端/InfluxDB/MQTT |
</details>

---

## 🛠️ 前提条件（必看）

在部署之前，请确认你的环境满足以下要求。**如果不满足，下面的步骤会报错。**

| 软件 | 最低版本 | 检查命令 | 安装方法（点击对应链接） |
|------|---------|---------|---------------------|
| **Docker** | 20.10+ | `docker --version` | [Docker Desktop](https://docs.docker.com/get-docker/) |
| **Node.js** | 18+ | `node --version` | [Node.js 官网](https://nodejs.org/zh-cn/download/) 下载 LTS 版本 |
| **Git** | 2.30+ | `git --version` | [Git 下载](https://git-scm.com/downloads) |
| **Python** (Python开发模式需要) | 3.11+ | `python --version` | [Python 官网](https://www.python.org/downloads/) 下载 3.11 或 3.12 |

> **💡 Windows 用户特别注意**：Windows 自带 CMD 不支持 `&&` 连接命令，请使用 **PowerShell**（右键开始菜单 -> Windows PowerShell）或安装 [Git Bash](https://git-scm.com/downloads)。

---

## ⚠️ 常见问题速查（撞墙自救指南）

遇到错误不要慌，按下面的对照表处理：

| 报错信息 | 可能原因 | 解决办法 |
|---------|---------|---------|
| `docker: command not found` | 没装 Docker | 去 Docker 官网下载安装 |
| `Docker Desktop is not running` | Docker 没启动 | 双击桌面 Docker 图标启动，等鲸鱼图标稳定后再执行命令 |
| `INFLUXDB_TOKEN is not set` | 没复制 `.env` 文件 | 执行 `cp docker/.env.example docker/.env` |
| `node: command not found` | 没装 Node.js | 去 Node.js 官网下载安装 |
| `npm ERR! code EACCES` | 没权限 | Windows 用管理员运行 PowerShell，Linux 加 `sudo` |
| `port 3000 is already in use` | 端口被占用 | 关闭占用端口的程序，或修改 `docker/docker-compose.yml` 中的端口 |
| `port 8080 is already in use` | 后端端口被占用 | 同上，Tomcat/Jenkins 通常占用 8080 |
| `Error: ENOSPC: System limit` | Linux 文件监听限制 | 执行 `echo fs.inotify.max_user_watches=524288 \| sudo tee -a /etc/sysctl.conf && sudo sysctl -p` |
| 页面打开白屏/一直在加载 | 前端没构建 | 先执行 `cd web && npm install && npm run build && cd ..` |
| `npm run build` 报内存不足 | Node.js 内存限制 | 执行 `set NODE_OPTIONS=--max-old-space-size=4096 && npm run build` |
| 登录时提示"用户名或密码错误" | 忘了密码 | 默认 admin/admin123，如修改过请删除 `data/edgelite.db` 重新启动 |

> 如果上面没有你的错误，请去 [GitHub Issues](https://github.com/suoten/EdgeLiteGateway/issues) 搜索或提交新问题。

---

## 📋 功能特性

### 设备接入 / 协议适配

| 类别 | 协议 | 说明 |
|------|------|------|
| **通用工业** | Modbus TCP/RTU | 最广泛使用的工业协议，几乎兼容所有 PLC/传感器 |
| | Siemens S7 (S7-200/300/400/1200/1500) | 西门子 PLC 全系列 |
| | Mitsubishi MC (iQ-R/Q/L/FX) | 三菱 PLC 全系列 |
| | Omron FINS (CJ/CP/NJ) | 欧姆龙 PLC |
| | Allen-Bradley CIP/PCCC | 罗克韦尔 AB PLC |
| | OPC-UA Client | 跨平台工业互操作标准 |
| | OPC-DA Client | 传统 Windows OPC 兼容 |
| | MQTT Client (Sparkplug B) | 工业物联网 MQTT 标准 |
| **电力/能源** | IEC 60870-5-104 | 电力远动规约，变电站/配电自动化 |
| | DL/T 645-2007 | 国家电能表通信规约 |
| **机器人/CNC** | ABB RWS (Web Services) | ABB 机器人 REST API |
| | FANUC FOCAS | 发那科 CNC 数控系统 |
| | KUKA Ethernet KRL | 库卡机器人 XML |
| **称重/仪表** | Toledo MT-SICS | 梅特勒-托利多称重仪表 |
| **视频** | ONVIF / PyGBSentry / HTTP | IP 摄像头 / 视频边缘分析（企业版支持） |
| **扩展** | HTTP Webhook / Serial / Simulator | 自定义拉取、串口原始数据、虚拟设备调试 |

<details>
<summary>📡 查看完整通信架构图</summary>

```mermaid
flowchart LR
    subgraph 南向设备["设备 & 传感器"]
        A1["PLC (S7/MC/FINS)"]
        A2["Modbus 设备"]
        A3["CNC / Robot"]
        A4["电力仪表 IEC104/DLT645"]
        A5["摄像头 ONVIF"]
        A6["MQTT 子设备"]
    end

    subgraph EdgeLite["EdgeLiteGateway"]
            direction LR
            B1[协议驱动层 22 plugins]
            B2[规则引擎]
            B3[预处理管线]
            B4[时序数据缓存]
    end

    subgraph 北向对接["北向平台与存储"]
        direction TB
        C1["IoTSharp"]
        C2["ThingsBoard"]
        C3["ThingsCloud"]
        C4["ThingsPanel"]
        C5["Huawei IoTDA"]
        C6["Custom MQTT"]
        D[("InfluxDB 时序库")]
    end

    A1 & A2 & A3 & A4 & A5 & A6 --> B1
    B1 --> B3 --> B4
    B4 --> D
    B4 --> C1 & C2 & C3 & C4 & C5 & C6
    B2 <-.-> B4
```
</details>

---

### 边缘计算引擎
- **规则引擎**：阈值告警 / 死区过滤 / 变化检测 / 条件动作（P1）
- **数据预处理**：缩放 / 死区 / 限幅 / 开方 / 累积（P1）
- **告警服务**：`钉钉 / 邮件 (SMTP) / 企业微信 / Webhook` 多渠道通知
- **断网续传**：离线缓存 + 排序回放（P1）

### 平台与系统
- **认证鉴权**：JWT (Access + Refresh) + RBAC `admin / operator / viewer`
- **审计日志**：全操作留痕，`设备/规则/告警/登录` 全维度
- **南向**：MQTT Broker (内置 `amqtt`) / Modbus Slave / Serial Bridge（P2）
- **北向**：自定义 MQTT Broker 把 EdgeLite 变成协议转换中台（P2）
- **MCP Server**：Model Context Protocol 把实时数据暴露给 AI Agent（P2）

> 💡 优先级划分：**P0 = v1.0 必需** · **P1 = v1.0 目标** · **P2 = v1.1+**

### 可视化与交互
- **看板**：设备/点位总数、在线率、今日数据量（P0）
- **SCADA 编辑器**：拖拽绑定测点 + 实时数据（P2）
- **数字孪生**：`Three.js 3D` 模型绑定 / 测点映射 / 视角同步（⚠️ 实验性）
- **数据查询**：多维度图表 / 自定义时间范围（P1）
- **PWA 离线**：Service Worker 离线可用 / 推送通知（P2）

---

## 📦 安装部署

四种部署方式分别适合不同场景，**对号入座**：

| 方式 | 适合谁 | 一句话说明 |
|------|-------|-----------|
| [Docker 一键](#-快速开始) | 🟢 **新手推荐** | 克隆 → 一键命令 → 浏览器打开 |
| [Docker Compose 手动](#方式一docker-compose-手动控制) | 🟡 想看清每步做了什么 | 分步骤操作，可自定义参数 |
| [Python 本地部署](#方式二python-本地部署开发模式) | 🔵 开发者/二次开发 | 需要 Python 3.11 + Node.js，启动开发服务 |
| [Docker 纯容器](#方式三docker-纯容器模式无前端构建) | 🟠 只有 Docker，没有 Node.js | Docker 全自动构建前后端，无需本地 Node.js |

---

### 方式一：Docker Compose（手动控制）

适用于需要自定义端口、查看每步日志的场景。

```bash
# 1. 克隆仓库
git clone https://gitee.com/suoten/EdgeLiteGateway.git && cd EdgeLiteGateway

# 2. 构建前端页面（必须！web/dist 会被 nginx 挂载）
cd web && npm install && npm run build && cd ..

# 3. 配置环境变量
cp docker/.env.example docker/.env

# 4. 启动全部服务（-d = 后台运行）
cd docker && docker compose up -d

# 5. 查看日志（确认启动成功）
docker compose logs -f edgelite    # 后端日志
docker compose logs -f frontend    # 前端日志

# 6. 浏览器打开 http://localhost:3000，账号 admin，密码 admin123
```

| 端口 | 服务 | 说明 |
|------|------|------|
| `3000` | 前端 (Nginx) | Web UI |
| `8080` | 后端 (FastAPI) | REST API + WebSocket |
| `8086` | InfluxDB | 时序数据库（仅 localhost） |
| `1883` | Mosquitto MQTT | MQTT Broker |

**停止服务**：`docker compose down`  
**完全清除（含数据）**：`docker compose down -v`

---

### 方式二：Python 本地部署（开发模式）

适用于二次开发、调试驱动、修改源码。

```bash
# 前置：必须 Python 3.11+ 且 Node.js 18+

# 1. 克隆
git clone https://gitee.com/suoten/EdgeLiteGateway.git && cd EdgeLiteGateway

# 2. 创建 Python 虚拟环境（重要！避免污染系统 Python）
python -m venv .venv

# 3. 激活虚拟环境
.venv\Scripts\activate        # Windows PowerShell
source .venv/bin/activate     # Linux / Mac

# 4. 安装后端依赖
pip install -e ".[dev]"

# 5. 准备配置文件
cp configs/config.example.yaml configs/config.yaml

# 6. 启动后端（新开一个终端）
python main.py --port 8080

# 7. 新终端启动前端开发服务器（新开一个终端）
cd web
cp .env.example .env          # 前端环境变量
npm install
npm run dev                    # Vite dev server, 默认 http://localhost:5173

# 8. 浏览器打开 http://localhost:5173
#    首次登录：admin / admin123
```

> **💡 为什么需要虚拟环境？** 隔离项目依赖，避免和系统Python其他项目冲突。如果你已激活虚拟环境，终端前面会显示 `(.venv)`。

<details>
<summary>📦 可选：安装 InfluxDB 和 Mosquitto（点击展开）</summary>

时序数据和 MQTT 功能需要额外安装：
```bash
# Ubuntu/Debian
sudo apt install influxdb mosquitto

# 或用 Docker 单独启动：
docker run -d --name influxdb -p 8086:8086 influxdb:2.7
docker run -d --name mosquitto -p 1883:1883 eclipse-mosquitto:2
```

不安装也能跑——系统会自动降级为缓存模式。
</details>

---

### 方式三：Docker 纯容器模式（无前端构建）

适用于只有 Docker、没有 Node.js 的场景。Dockerfile 内置了前端构建步骤。

```bash
# 1. 克隆仓库
git clone https://gitee.com/suoten/EdgeLiteGateway.git && cd EdgeLiteGateway

# 2. 配置环境变量
cp docker/.env.example docker/.env

# 3. 构建并启动
cd docker && docker compose build edgelite && docker compose up -d

# 4. 前端由 edgelite 容器内置服务提供
#    浏览器打开 http://localhost:8080
```

> **注意**：这种方式下 Nginx 前端服务不启动（因为没有 `web/dist`），前端由 edgelite 后端直接提供。

---

### 服务管理命令（备忘）

```bash
# 查看容器状态
docker compose -f docker/docker-compose.yml ps

# 查看所有日志
docker compose -f docker/docker-compose.yml logs -f

# 重启网关
docker compose -f docker/docker-compose.yml restart edgelite

# 删除所有数据（慎用！不可恢复）
docker compose -f docker/docker-compose.yml down -v
rm -rf data/ logs/
```

---

## 🏛️ 技术架构

```

┌──────────────────────────────────────────────────────────┐
│                      北向平台对接                          │
│  ThingsBoard  IoTSharp  ThingsCloud  ThingsPanel          │
│  Huawei IoTDA  Custom MQTT  ↑ MQTT/HTTP/REST              │
├──────────────────────────────────────────────────────────┤
│                    核心引擎 (EventBus)                     │
│  ┌─────────────────┐  ┌──────────────────┐                │
│  │  MQTT Forwarder │  │   规则引擎        │                │
│  │  预处理管线      │  │  告警/通知服务    │                │
│  └─────────────────┘  └──────────────────┘                │
├──────────────────────────────────────────────────────────┤
│                     数据抽象层 (SOR)                       │
│  ┌──────────────────────────────────────────────┐        │
│  │   SQLite ORM  │  InfluxDB 2.x Client       │        │
│  │   离线Cache   │  Tags: device,tenant,asset  │        │
│  └──────────────────────────────────────────────┘        │
├──────────────────────────────────────────────────────────┤
│                     API & WebSocket                        │
│  REST /api/v1/*  │  WS /ws/v1/{realtime,alarm,device}     │
├──────────────────────────────────────────────────────────┤
│                     驱动管理层 (Registry)                  │
│  22 Protocols: S7 / MC / FINS / AB / IEC104 / DLT645     │
│  Modbus TCP/RTU / OPC UA / OPC DA / MQTT / Fanuc / ...  │
├──────────────────────────────────────────────────────────┤
│                  视频接入层 (VideoProvider)                │
│  RTSP → PyGBSentry Analytics → MQ Events                  │
│  ONVIF Camera (PTZ, Preset, Snapshot URI)                 │
└──────────────────────────────────────────────────────────┘

```

---

## 📊 版本与路线图

### 版本差异

| 特性 | Community v1.0 | Enterprise v1.5 |
|------|:---:|:---:|
| **驱动协议** | 22 | 26+ (新增 Omron NJ EtherNet/IP, GE SRTP, BACnet, KNX) |
| **传感器模板** | 手动 | 模板向导 50+ |
| **北向平台** | 4 (IoTSharp/ThingsBoard/ThingsCloud/ThingsPanel) | 9+ (新增 AWS IoT Core, Azure IoT Hub, Cumulocity, DMP, OneNET) |
| **视频模块** | ONVIF 基础 | `PyGBSentry` 视频边缘分析引擎完整版 |
| **扩展能力** | 有限 | 全 SDK (Go/JS/Python 二次开发) + Cluster 集群 |
| **技术支持** | Community (Issue / QQ) | 7×24 Priority + 远程实施 |
| **开源协议** | GPL-3.0 | 需商业授权 |

### Roadmap

```
v1.0 Community    ← 项目开源  (2025 Q1) ✅
v1.1 Community    ← C 扩展 GCC/Clang/MSVC, 传感器向导  (2025 Q3) 🚧
v1.5 Enterprise   ← SDK 全面开放 (2025 Q4) 💼
v2.0 EdgeMesh     ← 分布式集群网, 跨网关协同  (2026/27远景)
```

---

## 🙋 技术支持

| 渠道 | 说明 |
|------|------|
| [GitHub Issues](https://github.com/suoten/EdgeLiteGateway/issues) | 提交 bug / 功能建议（中英文均可） |
| QQ 群: 738699357 | 技术交流与解答（加群请注明 "EdgeLite"） |
| 📧 suoten@163.com | 商业授权、企业版、定制开发咨询 |

### 文档索引

| 文档 | 内容 |
|------|------|
| [EdgeLiteGateway 规划文档](https://github.com/suoten/EdgeLiteGateway/blob/master/EdgeLite-Gateway-%E8%A7%84%E5%88%92%E6%96%87%E6%A1%A3.md) | 完整架构设计、API 设计、数据库设计、安全设计 |
| [Docker 部署指南](#-快速开始) | Docker Compose 一键部署 |
| [Python 本地部署](#方式二python-本地部署开发模式) | 开发环境搭建 |

---

## 📄 许可证

EdgeLiteGateway V1.0 Community 采用 [GPL-3.0](LICENSE) 协议开源。简单来说：
- ✅ 你可以自由使用、修改、分发源码
- ✅ 你可以用于商业项目
- ⚠️ 修改后的代码必须保留 `GPL-3.0` 协议并开源
- 💼 对 GPL 有限制的商业场景（如嵌入式 SDK）请联系 `suoten@163.com` 获取双授权

---

## ✨ 贡献者

感谢以下贡献者对 EdgeLiteGateway 项目做出的重要贡献：

<a href="https://github.com/suoten/EdgeLiteGateway/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=suoten/EdgeLiteGateway&max=10" />
</a>

---

## 🌟 Stargazers over time

[![Star History Chart](https://api.star-history.com/svg?repos=suoten/EdgeLiteGateway&type=Date)](https://star-history.com/#suoten/EdgeLiteGateway&Date)

---

***Made with ❤️ for the Industrial IoT Community***

</parameter>