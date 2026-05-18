<div align="center">

# ⚡ EdgeLiteGateway

### Lightweight Edge Computing IoT Gateway — Device Connectivity as Simple as Plug & Play

[![License](https://img.shields.io/github/license/suoten/EdgeLiteGateway?color=blue\&label=license)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python\&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi\&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vue](https://img.shields.io/badge/Vue-3.4%2B-4FC08D?logo=vue.js\&logoColor=white)](https://vuejs.org/)
[![Version](https://img.shields.io/badge/version-1.0.0--community-brightgreen)](https://github.com/suoten/EdgeLiteGateway)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker\&logoColor=white)](https://www.docker.com/)

**🇨🇳 China's First Open-Source Python Edge Gateway | 🎯 22 Industrial Protocols Out-of-the-Box | 📹 Video-IoT Unified | 🚀 10-Min Docker Deploy**

[Quick Start](#-quick-start) · [Features](#-features) · [Deployment](#-deployment) · [Architecture](#-architecture) · [Versions](#-versions--roadmap) · [Support](#-support)

</div>

***

## 🚀 Quick Start

> **Only [Docker](https://docs.docker.com/get-docker/) required. No Node.js / Python needed.**

> ⚠️ **Windows users**: Use **PowerShell** (not CMD). Right-click Start → Windows PowerShell.

```bash
# 1. Clone the repository
git clone https://gitee.com/suoten/EdgeLiteGateway.git && cd EdgeLiteGateway

# 2. Generate configuration (Windows PowerShell: use "Copy-Item" instead of "cp")
cp docker/.env.example docker/.env

# 3. Build and start (first build ~3-5 min, instant thereafter)
#    Users in China: if build hangs/times out, configure a registry mirror
#    Docker Desktop → Settings → Docker Engine → add "registry-mirrors": ["https://docker.1ms.run"]
cd docker && docker compose build edgelite && docker compose up -d

# 4. Watch startup logs
docker compose logs -f edgelite     # "Uvicorn running" = success, Ctrl+C to exit
```

Open http://localhost:8080 in your browser. Username: `admin` / Password: `admin123` (password change required on first login).

<details>
<summary>🖥️ Have Node.js? Hybrid mode (local frontend build + Docker backend)</summary>

If you have Node.js 18+ locally, build the frontend first then use Docker for the backend. Access via http://localhost:3000 (Nginx serves frontend, faster):

```bash
git clone https://gitee.com/suoten/EdgeLiteGateway.git && cd EdgeLiteGateway
cd web && npm install && npm run build && cd ..
cp docker/.env.example docker/.env
cd docker && docker compose --profile nginx up -d
```

</details>

---

<details>
<summary>🎯 What does the one-click deploy do? (click to expand)</summary>

| Step | Operation | Time | Description |
|------|-----------|------|-------------|
| 1 | Clone code | seconds | `git clone` downloads the project |
| 2 | Generate config | instant | `cp .env.example .env` creates env vars |
| 3 | Build image | 3-5 min | Docker auto-installs dependencies, builds frontend & backend |
| 4 | Start containers | 30 sec | `docker compose up -d` starts gateway/InfluxDB/MQTT |

</details>

***

## 🛠️ Prerequisites (Read Before Deploying)

Verify your environment meets these requirements before proceeding. **If not met, the commands below will fail.**

| Software                     | Minimum | Check command       | Install                                                                                                         |
| ---------------------------- | ------- | ------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Docker**                   | 20.10+  | `docker --version`  | Windows/Mac: [Docker Desktop](https://docs.docker.com/get-docker/); Linux: `curl -fsSL https://get.docker.com \| sudo sh` |
| **Git**                      | 2.30+   | `git --version`     | [Git Download](https://git-scm.com/downloads)                                                                    |
| **Node.js** (hybrid mode only) | 18+   | `node --version`    | [Node.js](https://nodejs.org/en/download/) download LTS                                                          |
| **Python** (dev mode only)   | 3.11+   | `python --version`  | [Python](https://www.python.org/downloads/) download 3.11 or 3.12                                                |

> **💡 Windows users**: Windows CMD does not support `&&` for chaining commands. Use **PowerShell** (right-click Start → Windows PowerShell) or install [Git Bash](https