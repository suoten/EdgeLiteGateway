# EdgeLiteGateway v1.0 Community 部署指南

## 目录

1. [Docker 部署](#docker-部署)
2. [源码部署](#源码部署)
3. [前端构建与部署](#前端构建与部署)
4. [Nginx 反向代理配置](#nginx-反向代理配置)
5. [生产环境配置](#生产环境配置)
6. [监控与运维](#监控与运维)
7. [故障排查](#故障排查)

---

## Docker 部署

### 前置条件

- Docker 20.10+
- Docker Compose 2.0+

### 快速启动

```bash
cd docker
docker compose up -d
```

### 服务说明

| 服务 | 容器名 | 端口 | 说明 |
|------|--------|------|------|
| edgelite | edgelite-gateway | 8080 | 主服务（API + WebSocket） |
| influxdb | edgelite-influxdb | 8086 | 时序数据库 |
| mosquitto | edgelite-mosquitto | 1883 | MQTT Broker |

### 环境变量配置

在 `docker/` 目录下创建 `.env` 文件：

```env
# InfluxDB 配置
INFLUXDB_TOKEN=your-secure-influxdb-token
INFLUXDB_PASSWORD=your-influxdb-admin-password

# EdgeLiteGateway 安全配置
SECRET_KEY=your-jwt-secret-key-at-least-32-chars
```

**生成安全密钥**：

```bash
# 生成 SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 生成 INFLUXDB_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### InfluxDB 初始化

Docker Compose 首次启动时自动初始化 InfluxDB，默认配置：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| DOCKER_INFLUXDB_INIT_USERNAME | admin | 管理员用户名 |
| DOCKER_INFLUXDB_INIT_PASSWORD | admin123 | 管理员密码（**生产环境必须修改**） |
| DOCKER_INFLUXDB_INIT_ORG | edgelite | 组织名 |
| DOCKER_INFLUXDB_INIT_BUCKET | edgelite | 默认 Bucket |
| DOCKER_INFLUXDB_INIT_ADMIN_TOKEN | edgelite-token-change-me | API Token（**生产环境必须修改**） |

初始化完成后，可访问 `http://localhost:8086` 进入 InfluxDB UI 确认配置。

### 开发模式

```bash
docker compose -f docker-compose.dev.yml up -d
```

开发模式特性：
- 挂载本地源码，代码修改自动生效
- 使用 `--reload` 参数，uvicorn 自动重载
- 使用固定开发密钥（**仅用于开发，不可用于生产**）

---

## 源码部署

### 1. 安装依赖

```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# 安装依赖
pip install -e .
```

### 2. 配置文件

```bash
# 复制示例配置
cp configs/config.example.yaml configs/config.yaml

# 编辑配置
vim configs/config.yaml
```

也可通过 `EDGELITE_CONFIG` 环境变量指定配置文件路径：

```bash
export EDGELITE_CONFIG=/path/to/config.yaml
```

### 3. 初始化数据库

```bash
python scripts/init_db.py
```

此命令创建 SQLite 数据库和默认 admin 用户（密码 admin123）。

### 4. 启动服务

```bash
# 直接启动
python -m edgelite --host 0.0.0.0 --port 8080

# 指定配置文件
python -m edgelite --host 0.0.0.0 --port 8080 --config /path/to/config.yaml

# 开发模式（热重载）
python -m edgelite --host 0.0.0.0 --port 8080 --reload

# 或使用 uvicorn
uvicorn edgelite.app:create_app --host 0.0.0.0 --port 8080 --factory
```

### 5. Systemd 服务（Linux）

创建 `/etc/systemd/system/edgelite.service`：

```ini
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
ExecStart=/opt/edgelite/venv/bin/python -m edgelite --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
LimitNOFILE=65536

# 日志输出到 journald
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
```

创建环境变量文件 `/opt/edgelite/.env`：

```env
EDGELITE_INFLUXDB_URL=http://localhost:8086
EDGELITE_INFLUXDB_TOKEN=your-influxdb-token
EDGELITE_MQTT_BROKER=localhost
EDGELITE_SECURITY_SECRET_KEY=your-jwt-secret-key
```

```bash
# 创建用户
useradd -r -s /bin/false edgelite

# 设置权限
chown -R edgelite:edgelite /opt/edgelite

# 启动服务
systemctl daemon-reload
systemctl enable edgelite
systemctl start edgelite

# 查看日志
journalctl -u edgelite -f
```

---

## 前端构建与部署

### 开发模式

```bash
cd web
npm install
npm run dev
# 访问 http://localhost:3000（Vite 开发服务器，自动代理 API 到 8080）
```

### 生产构建

```bash
cd web
npm install
npm run build
# 产物生成在 web/dist/ 目录
```

### 部署前端静态文件

将 `web/dist/` 目录部署到 Nginx 作为静态文件服务：

```bash
# 复制构建产物到 Nginx 目录
cp -r web/dist/ /var/www/edgelite/
```

---

## Nginx 反向代理配置

### 完整配置示例

以下配置将前端静态文件、后端 API、WebSocket 统一通过 Nginx 提供服务：

```nginx
# HTTP -> HTTPS 重定向
server {
    listen 80;
    server_name edgelite.example.com;
    return 301 https://$server_name$request_uri;
}

# HTTPS 主配置
server {
    listen 443 ssl http2;
    server_name edgelite.example.com;

    # SSL 证书
    ssl_certificate /etc/ssl/certs/edgelite.example.com.pem;
    ssl_certificate_key /etc/ssl/private/edgelite.example.com.key;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;

    # --- 前端静态文件 ---
    location / {
        root /var/www/edgelite;
        index index.html;
        try_files $uri $uri/ /index.html;  # SPA 路由回退
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

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
        proxy_read_timeout 86400s;  # WebSocket 长连接
        proxy_send_timeout 86400s;
    }

    # --- 静态资源缓存 ---
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
        root /var/www/edgelite;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

### 仅后端代理（前端由其他方式提供）

如果前端和后端部署在不同域名/端口：

```nginx
server {
    listen 443 ssl;
    server_name api.edgelite.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # API 代理
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket 代理
    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400s;
    }
}
```

### CORS 配置

如果前端和后端部署在不同域名，需在 `configs/config.yaml` 中配置 CORS：

```yaml
server:
  cors_origins:
    - "https://edgelite.example.com"
    - "http://localhost:3000"  # 开发环境
```

**生产环境切勿使用 `["*"]`，应限制为实际域名。**

---

## 生产环境配置

### 生产环境配置清单

| 序号 | 检查项 | 说明 |
|------|--------|------|
| 1 | 修改 admin 默认密码 | 首次登录后立即修改 |
| 2 | 配置 security.secret_key | 至少 32 字符随机字符串 |
| 3 | 配置 InfluxDB Token | 通过环境变量 `EDGELITE_INFLUXDB_TOKEN` 设置 |
| 4 | 限制 CORS 源 | 配置为实际前端域名 |
| 5 | 配置 HTTPS | 使用 Nginx + SSL 证书 |
| 6 | 配置防火墙 | 仅开放 80/443 端口 |
| 7 | 配置日志级别 | 生产环境设为 WARNING 或 ERROR |
| 8 | 配置日志轮转 | 避免日志文件无限增长 |
| 9 | 禁用模拟器 | `simulator.auto_create: false` |
| 10 | 配置备份策略 | 定期备份 SQLite 和 InfluxDB |

### 安全加固

#### 1. 修改默认密码

```bash
# 1. 登录获取 Token
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json; print(json.load(sys.stdin)['data']['access_token'])")

# 2. 修改密码
curl -X PUT http://localhost:8080/api/v1/users/admin \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"password": "new-secure-password"}'
```

#### 2. 生成安全密钥

```bash
# secret_key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# InfluxDB token
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

#### 3. 配置防火墙

```bash
# 仅开放 HTTP/HTTPS
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# 如果需要直接访问 InfluxDB（仅内网）
ufw allow from 10.0.0.0/8 to any port 8086
```

### 性能优化

#### 1. InfluxDB 调优

```yaml
influxdb:
  batch_size: 5000       # 增大批大小（默认 1000）
  flush_interval: 10000  # 增大刷新间隔毫秒（默认 5000）
```

#### 2. SQLite 优化

已在代码中默认启用：
- WAL 模式（`PRAGMA journal_mode=WAL`）—— 支持并发读写
- 外键约束（`PRAGMA foreign_keys=ON`）

#### 3. 日志级别

```yaml
logging:
  level: "WARNING"  # 生产环境：WARNING 或 ERROR
  format: "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
```

#### 4. 日志轮转

代码默认输出到控制台（stdout），推荐通过 Systemd + journald 或 logrotate 管理日志：

**logrotate 配置** `/etc/logrotate.d/edgelite`：

```
/var/log/edgelite/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

如需文件日志，在启动命令中添加重定向：

```bash
python -m edgelite 2>&1 | tee -a /var/log/edgelite/app.log
```

#### 5. Uvicorn Workers

单进程架构（asyncio 事件循环），不建议使用多 worker。如需更高吞吐量，可通过 Nginx 负载均衡多个实例。

#### 6. 事件总线队列大小

默认 `MAX_QUEUE_SIZE=10000`，队列满时丢弃最旧事件。高负载场景可在 `EventBus` 初始化时调大。

---

## 监控与运维

### 健康检查

```bash
# API 健康检查
curl http://localhost:8080/api/v1/system/status

# InfluxDB 健康检查
curl http://localhost:8086/health

# MQTT Broker 健康检查（mosquitto）
mosquitto_sub -t '$SYS/broker/uptime' -C 1
```

### 日志查看

```bash
# Docker 日志
docker compose logs -f edgelite

# Systemd 日志
journalctl -u edgelite -f

# 按时间过滤
journalctl -u edgelite --since "2024-01-01" --until "2024-01-02"
```

### 数据备份

#### SQLite 备份

```bash
# 通过 API 备份
curl -X POST http://localhost:8080/api/v1/system/backup \
  -H "Authorization: Bearer {token}"

# 手动备份（需先触发 WAL checkpoint）
sqlite3 data/edgelite.db "PRAGMA wal_checkpoint=TRUNCATE"
cp data/edgelite.db "data/backups/edgelite-$(date +%Y%m%d).db"
```

#### InfluxDB 备份

```bash
# InfluxDB 备份
influx backup /path/to/backup-dir -t your-influxdb-token

# InfluxDB 恢复
influx restore /path/to/backup-dir -t your-influxdb-token
```

### 数据恢复

```bash
# 通过 API 恢复 SQLite（恢复后需重启服务）
curl -X POST http://localhost:8080/api/v1/system/restore \
  -H "Authorization: Bearer {token}" \
  -H "Content-Type: application/json" \
  -d '{"backup_id": "xxx"}'
```

**注意**：SQLite 恢复后需要重启 EdgeLiteGateway 服务才能生效。

### 定时备份脚本

```bash
# /etc/cron.d/edgelite-backup
# 每天凌晨 2 点备份
0 2 * * * edgelite /opt/edgelite/venv/bin/python -c "
import sqlite3, shutil, datetime
conn = sqlite3.connect('/opt/edgelite/data/edgelite.db')
conn.execute('PRAGMA wal_checkpoint=TRUNCATE')
conn.close()
date = datetime.date.today().isoformat()
shutil.copy2('/opt/edgelite/data/edgelite.db', f'/opt/edgelite/data/backups/edgelite-{date}.db')
"
```

### 监控指标

系统状态 API（`GET /api/v1/system/status`）返回以下指标：

| 指标 | 说明 |
|------|------|
| cpu_percent | CPU 使用率 |
| memory_percent | 内存使用率 |
| disk_percent | 磁盘使用率 |
| device_total / device_online | 设备总数/在线数 |
| rule_total | 规则总数 |
| alarm_active | 活跃告警数 |
| collect_tasks | 采集任务数 |
| uptime_seconds | 运行时长（秒） |

可对接 Prometheus/Grafana 进行可视化监控（通过 Node Exporter 暴露系统指标，通过 InfluxDB Telegraf 采集应用指标）。

---

## 故障排查

### 服务无法启动

1. 检查端口占用：`netstat -tlnp | grep 8080` 或 `ss -tlnp | grep 8080`
2. 检查配置文件：`python -c "from edgelite.config import get_config; print(get_config())"`
3. 检查依赖：`pip list | grep -E "fastapi|uvicorn|aiosqlite|influxdb"`
4. 检查 Python 版本：`python --version`（需 3.11+）
5. 查看详细错误：`python -m edgelite --host 0.0.0.0 --port 8080`（前台运行查看输出）

### InfluxDB 连接失败

1. 检查 InfluxDB 服务：`curl http://localhost:8086/health`
2. 检查 token 配置：确认 `EDGELITE_INFLUXDB_TOKEN` 或 `config.yaml` 中 token 正确
3. 检查 org/bucket 是否存在：访问 InfluxDB UI 确认
4. **自动降级**：系统会自动降级到缓存模式，数据暂存 SQLite 队列，联网后自动续传

### MQTT 连接失败

1. 检查 MQTT Broker 服务：`mosquitto_sub -t 'test' -C 1`
2. 检查 broker 地址和端口配置
3. 检查用户名/密码认证配置
4. 查看日志中的 MQTT 连接错误

### 设备离线

1. 检查网络连通性：`ping <device_ip>`
2. 检查设备配置（IP、端口、协议参数）
3. 检查防火墙是否阻止设备端口
4. 查看日志：`journalctl -u edgelite | grep "device"`

### 告警未触发

1. 检查规则是否启用（`enabled: true`）
2. 检查条件配置（操作符、阈值是否正确）
3. 检查数据是否正常采集（查看设备实时数据）
4. 检查持续时间窗口（`duration > 0` 时需条件持续满足 N 秒）
5. 检查告警收敛（同一规则不会重复触发 firing）

### WebSocket 断连

1. 检查 Nginx WebSocket 代理配置（`proxy_read_timeout` 是否足够大）
2. 检查 Token 是否过期（access_token 默认 30 分钟）
3. 客户端应实现自动重连机制

### 通知发送失败

1. 钉钉：检查 webhook_url 和 secret 配置，确认加签方式正确
2. 邮件：检查 SMTP 服务器、端口、认证信息，测试 `telnet smtp.example.com 465`
3. 企业微信：检查 webhook_url 是否有效
4. 自定义 Webhook：检查 URL 可达性和请求格式

### 缓存队列溢出

1. 检查 InfluxDB 是否可达
2. 查看缓存队列大小（`GET /api/v1/system/status`）
3. 缓存满时（10 万条）自动丢弃最旧 10% 数据
4. 恢复 InfluxDB 连接后自动续传缓存数据
