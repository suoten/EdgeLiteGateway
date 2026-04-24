# EdgeLiteGateway v1.0 Community 部署指南

## 目录

1. [Docker 部署](#docker-部署)
2. [源码部署](#源码部署)
3. [生产环境配置](#生产环境配置)
4. [监控与运维](#监控与运维)
5. [故障排查](#故障排查)

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

| 服务 | 端口 | 说明 |
|------|------|------|
| edgelite | 8080 | 主服务 |
| influxdb | 8086 | 时序数据库 |
| mosquitto | 1883 | MQTT Broker |

### 环境变量配置

创建 `.env` 文件：

```env
INFLUXDB_TOKEN=your-secure-token
INFLUXDB_PASSWORD=your-admin-password
SECRET_KEY=your-jwt-secret
```

### 开发模式

```bash
docker compose -f docker-compose.dev.yml up -d
```

开发模式支持代码热重载，挂载本地源码。

---

## 源码部署

### 1. 安装依赖

```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux
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

### 3. 初始化数据库

```bash
python scripts/init_db.py
```

### 4. 启动服务

```bash
# 直接启动
python -m edgelite --host 0.0.0.0 --port 8080

# 或使用 uvicorn
uvicorn edgelite.app:create_app --host 0.0.0.0 --port 8080 --factory
```

### 5. Systemd 服务（Linux）

创建 `/etc/systemd/system/edgelite.service`：

```ini
[Unit]
Description=EdgeLiteGateway
After=network.target

[Service]
Type=simple
User=edgelite
WorkingDirectory=/opt/edgelite
ExecStart=/opt/edgelite/venv/bin/python -m edgelite
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable edgelite
systemctl start edgelite
```

---

## 生产环境配置

### 安全加固

#### 1. 修改默认密码

```bash
# 登录后立即修改 admin 密码
curl -X PUT http://localhost:8080/api/v1/users/{user_id} \
  -H "Authorization: Bearer {token}" \
  -H "Content-Type: application/json" \
  -d '{"password": "new-secure-password"}'
```

#### 2. 配置 HTTPS

使用 Nginx 反向代理：

```nginx
server {
    listen 443 ssl;
    server_name edgelite.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

#### 3. 配置防火墙

```bash
# 只开放必要端口
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

### 性能优化

#### 1. InfluxDB 调优

```yaml
influxdb:
  batch_size: 5000       # 增大批大小
  flush_interval: 10000  # 增大刷新间隔
```

#### 2. 数据库 WAL 模式

已在代码中默认启用 WAL 模式，无需额外配置。

#### 3. 日志级别

```yaml
logging:
  level: "WARNING"  # 生产环境降低日志级别
```

---

## 监控与运维

### 健康检查

```bash
# API 健康检查
curl http://localhost:8080/api/v1/system/status

# InfluxDB 健康检查
curl http://localhost:8086/health
```

### 日志查看

```bash
# Docker 日志
docker compose logs -f edgelite

# 本地日志
tail -f logs/edgelite.log
```

### 数据备份

```bash
# 通过 API 备份
curl -X POST http://localhost:8080/api/v1/system/backup \
  -H "Authorization: Bearer {token}"

# 手动备份
cp data/edgelite.db data/backups/edgelite-$(date +%Y%m%d).db
```

### 数据恢复

```bash
# 通过 API 恢复
curl -X POST http://localhost:8080/api/v1/system/restore \
  -H "Authorization: Bearer {token}" \
  -H "Content-Type: application/json" \
  -d '{"backup_id": "xxx"}'
```

### 监控指标

系统状态 API 返回以下指标：

- CPU 使用率
- 内存使用率
- 磁盘使用率
- 设备总数/在线数
- 规则总数
- 活跃告警数
- 采集任务数
- 运行时长

可对接 Prometheus/Grafana 进行可视化监控。

---

## 故障排查

### 服务无法启动

1. 检查端口占用：`netstat -tlnp | grep 8080`
2. 检查配置文件：`python -c "from edgelite.config import get_config; print(get_config())"`
3. 检查依赖：`pip list`

### InfluxDB 连接失败

1. 检查 InfluxDB 服务：`curl http://localhost:8086/health`
2. 检查 token 配置
3. 系统会自动降级到缓存模式

### 设备离线

1. 检查网络连通性
2. 检查设备配置（IP、端口等）
3. 查看日志：`grep "device" logs/edgelite.log`

### 告警未触发

1. 检查规则是否启用
2. 检查条件配置
3. 检查数据是否正常采集
