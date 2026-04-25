# 宝塔面板部署指南

## 概述

本文档介绍如何在**宝塔面板**中部署 EdgeLiteGateway，包含：
1. 后端服务部署（Python 环境 + Systemd 守护）
2. 前端构建与部署
3. Nginx 反向代理配置（同域部署，无需处理 CORS）

---

## 目录

1. [环境准备](#环境准备)
2. [后端部署](#后端部署)
3. [前端构建](#前端构建)
4. [Nginx 配置](#nginx配置)
5. [SSL 证书](#ssl-证书)
6. [启动与验证](#启动与验证)

---

## 环境准备

### 1. 安装基础软件

在宝塔面板中安装：
- **Python 项目管理器**（或手动安装 Python 3.11+）
- **Nginx**（宝塔面板 -> 软件商店 -> 安装 Nginx）

### 2. 上传代码

将 `EdgeLite-v1.0-Community` 代码上传到服务器，例如：
```
/opt/edgelite/
```

---

## 后端部署

### 1. 创建 Python 虚拟环境

```bash
cd /opt/edgelite
python3.11 -m venv venv
source venv/bin/activate
pip install -e .
```

### 2. 复制配置文件

```bash
cp configs/config.example.yaml configs/config.yaml
```

编辑 `configs/config.yaml`，重点修改：
```yaml
server:
  host: "127.0.0.1"   # 改为 127.0.0.1，只监听本地（Nginx 反向代理）
  port: 8080
  cors_origins: []      # 同域部署，CORS 可留空

security:
  secret_key: "你的至少32位随机密钥"
```

### 3. 初始化数据库

```bash
python scripts/init_db.py
```

### 4. 创建 Systemd 服务

创建 `/etc/systemd/system/edgelite.service`：

```ini
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
```

创建 `/opt/edgelite/.env`：
```env
EDGELITE_SECURITY_SECRET_KEY=你的至少32位随机密钥
EDGELITE_INFLUXDB_TOKEN=你的influxdb-token
```

启动服务：
```bash
systemctl daemon-reload
systemctl enable edgelite
systemctl start edgelite
```

---

## 前端构建

### 1. 安装依赖并构建

```bash
cd /opt/edgelite/web
npm install
npm run build
```

构建产物在 `web/dist/` 目录。

### 2. 复制到网站目录

```bash
mkdir -p /var/www/edgelite
cp -r /opt/edgelite/web/dist/* /var/www/edgelite/
```

---

## Nginx 配置

### 方式一：宝塔面板图形界面配置（推荐）

1. 宝塔面板 -> 网站 -> 添加站点
2. 填写域名（如 `edgelite.yourdomain.com`）
3. 根目录选择 `/var/www/edgelite`
4. 选择 PHP 版本为 **纯静态**
5. 点击"设置" -> "配置文件"

在原有配置中 **添加** 以下 `location` 块（不要删除原有内容）：

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

**注意**：
- 如果宝塔已有 `location /` 配置，保留它（用于前端静态文件）
- 确保 `try_files $uri $uri/ /index.html;` 已存在（SPA 路由支持）

### 方式二：直接编辑 Nginx 配置文件

配置文件路径通常在：
```
/www/server/panel/vhost/nginx/edgelite.yourdomain.com.conf
```

完整配置参考项目 `nginx/edgelite.conf` 文件。

---

## SSL 证书

宝塔面板 -> 网站 -> 设置 -> SSL -> Let's Encrypt / 其他证书

申请并开启强制 HTTPS。

---

## 启动与验证

### 1. 检查后端状态

```bash
systemctl status edgelite
curl http://127.0.0.1:8080/api/v1/system/status
```

### 2. 检查 Nginx 配置

```bash
nginx -t
```

### 3. 访问验证

- Web 管理界面：`https://edgelite.yourdomain.com`
- API 文档：`https://edgelite.yourdomain.com/docs`
- 默认账号：`admin` / `admin123`

---

## 常见问题

### Q: 前端页面空白或 404？

A: 检查 Nginx 是否配置了 SPA 回退：
```nginx
location / {
    try_files $uri $uri/ /index.html;
}
```

### Q: API 请求 502？

A: 检查后端服务是否启动：
```bash
systemctl status edgelite
```

### Q: WebSocket 连接失败？

A: 检查 Nginx WebSocket 代理配置，确保包含：
```nginx
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

### Q: 登录后提示 CORS 错误？

A: 同域部署不应该出现 CORS。检查前端 `web/.env` 是否使用了相对路径：
```env
VITE_API_BASE_URL=/api/v1
VITE_WS_BASE_URL=/ws/v1
```

---

## 目录结构（部署后）

```
/opt/edgelite/              # 后端源码
├── venv/                   # Python 虚拟环境
├── configs/config.yaml     # 配置文件
├── .env                    # 环境变量
├── data/                   # SQLite 数据库
└── web/dist/               # 前端构建产物（可单独部署）

/var/www/edgelite/          # Nginx 静态文件目录（前端）
├── index.html
├── assets/
└── ...
```
