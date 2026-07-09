# EdgeLiteGateway v1.0 Community Edition 用户指南

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+ (前端开发)
- Docker & Docker Compose (推荐部署方式)

### 方式一：Docker Compose 一键启动（推荐）

```bash
cd docker
docker compose up -d

# 访问服务
# API 文档: http://localhost:8080/docs
# Web 管理: http://localhost:3000 (开发模式)
```

### 方式二：本地开发启动

```bash
# 1. 安装 Python 依赖
pip install -e .

# 2. 启动后端
python -m edgelite --host 0.0.0.0 --port 8080

# 3. 启动前端（另一个终端）
cd web && npm install && npm run dev
```

### 默认账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| admin | admin123 | 管理员 |

**生产环境请立即修改默认密码！**

---

## 认证流程

### 1. 登录获取 Token

```bash
curl -X POST http://localhost:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'
```

响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
    "token_type": "bearer",
    "expires_in": 1800
  }
}
```

### 2. 使用 Token 访问 API

在所有需要认证的请求头中携带 Token：

```bash
curl http://localhost:8080/api/v1/devices \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..."
```

### 3. Token 过期后刷新

access_token 默认 30 分钟过期，使用 refresh_token 刷新：

```bash
curl -X POST http://localhost:8080/api/v1/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "eyJhbGciOiJIUzI1NiIs..."}'
```

### Token 生命周期

| Token 类型 | 默认过期时间 | 用途 |
|-----------|-------------|------|
| access_token | 30 分钟 | API 请求认证 |
| refresh_token | 7 天 | 刷新 access_token |

可通过配置修改：

```yaml
security:
  access_token_expire_minutes: 60    # access_token 过期时间
  refresh_token_expire_days: 30      # refresh_token 过期时间
```

---

## 功能概览

### 设备接入

| 协议 | 说明 |
|------|------|
| Modbus TCP | 工业标准协议，支持线圈/寄存器读写、设备发现 |
| MQTT Client | 订阅设备数据主题，支持 JSON 解析 |
| OPC-UA | 工业互联协议，支持节点浏览和订阅 |
| HTTP Webhook | 设备主动 POST 数据，无需认证 |
| Simulator | 内置模拟器，支持 fixed/sine/random_walk/random 模式 |

### 核心功能

- **数据采集**：异步并发采集，可配置间隔
- **规则引擎**：多条件 AND/OR、持续时间窗口、自动恢复
- **告警管理**：告警收敛、确认、恢复通知
- **通知渠道**：钉钉、企业微信、邮件、自定义 Webhook
- **数据存储**：SQLite(配置) + InfluxDB(时序)
- **断网缓存**：离线数据缓存，联网自动续传
- **北向转发**：MQTT 数据转发到云端 Broker
- **内置服务**：MQTT Server、OPC UA Server、Modbus Slave
- **视频接入**：通过 PyGBSentry 适配器接入 GB28181 视频流

### Web 管理界面

- 仪表盘：系统资源、设备统计、告警概览
- 设备管理：CRUD、实时数据、模拟器创建
- 规则管理：CRUD、启用/禁用、规则测试
- 告警中心：列表、筛选、确认
- 系统管理：状态监控、备份/恢复
- 用户管理：CRUD、角色分配

---

## API 接口

### 通用响应格式

**成功响应**：

```json
{
  "code": 0,
  "message": "success",
  "data": { ... }
}
```

**分页响应**：

```json
{
  "code": 0,
  "message": "success",
  "data": [ ... ],
  "total": 100,
  "page": 1,
  "size": 20
}
```

**错误响应**：

```json
{
  "code": 401,
  "message": "未认证",
  "data": null
}
```

### 分页参数

所有列表接口支持分页：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| page | int | 1 | 页码（从 1 开始） |
| size | int | 20 | 每页条数（1-100） |

示例：`GET /api/v1/devices?page=2&size=50`

### 常见错误码

| HTTP 状态码 | code | 说明 |
|-------------|------|------|
| 200 | 0 | 成功 |
| 401 | 401 | 未认证（Token 缺失或过期） |
| 403 | 403 | 权限不足 |
| 404 | 404 | 资源不存在 |
| 409 | 409 | 资源冲突（如重复创建） |
| 422 | 422 | 请求参数校验失败 |
| 500 | 500 | 服务器内部错误 |

### 认证

```
POST /api/v1/auth/login     # 登录获取 Token
POST /api/v1/auth/refresh   # 刷新 Token
```

**登录请求**：

```json
{ "username": "admin", "password": "admin123" }
```

### 设备管理

```
GET    /api/v1/devices                    # 设备列表
POST   /api/v1/devices                    # 创建设备
GET    /api/v1/devices/{id}               # 设备详情
PUT    /api/v1/devices/{id}               # 更新设备
DELETE /api/v1/devices/{id}               # 删除设备
GET    /api/v1/devices/{id}/points        # 读取测点
POST   /api/v1/devices/{id}/points        # 写入测点
POST   /api/v1/devices/{id}/push          # HTTP 数据推送（无需认证）
POST   /api/v1/devices/simulator          # 创建模拟设备
POST   /api/v1/devices/discover           # 设备发现
```

**创建设备示例**：

```json
{
  "device_id": "modbus-plc-01",
  "name": "1号产线PLC",
  "protocol": "modbus_tcp",
  "config": {
    "host": "192.168.1.100",
    "port": 502,
    "unit_id": 1
  },
  "points": [
    { "name": "temperature", "address": "0", "data_type": "float32", "unit": "°C" },
    { "name": "pressure", "address": "2", "data_type": "float32", "unit": "MPa" }
  ],
  "collect_interval": 5
}
```

**HTTP Webhook 推送**（无需认证，适合设备主动上报）：

```bash
curl -X POST http://localhost:8080/api/v1/devices/my-device/push \
  -H "Content-Type: application/json" \
  -d '{"temperature": 25.6, "humidity": 65.3}'
```

### 规则管理

```
GET    /api/v1/rules          # 规则列表
POST   /api/v1/rules          # 创建规则
PUT    /api/v1/rules/{id}     # 更新规则
DELETE /api/v1/rules/{id}     # 删除规则
POST   /api/v1/rules/{id}/enable   # 启用规则
POST   /api/v1/rules/{id}/disable  # 禁用规则
POST   /api/v1/rules/{id}/test     # 测试规则
```

**创建规则示例**：

```json
{
  "rule_id": "temp-alarm-01",
  "name": "温度超限告警",
  "device_id": "modbus-plc-01",
  "conditions": [
    { "point": "temperature", "operator": ">", "value": 80 }
  ],
  "logic": "AND",
  "duration": 10,
  "severity": "critical",
  "notify_channels": ["dingtalk", "email"]
}
```

**条件操作符**：`>`, `>=`, `<`, `<=`, `==`

**告警严重级别**：`critical`（严重）、`warning`（警告）、`info`（信息）

**告警状态流转**：`firing` → `acknowledged` → `recovered`

### 告警管理

```
GET /api/v1/alarms           # 告警列表（支持 page/size/status/severity 过滤）
GET /api/v1/alarms/{id}      # 告警详情
PUT /api/v1/alarms/{id}/ack  # 确认告警
```

### 数据查询

```
GET /api/v1/data/query   # 时序数据查询
GET /api/v1/data/export  # 数据导出（CSV/JSON）
```

**查询参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| device_id | string | 设备 ID |
| point | string | 测点名称 |
| start | string | 开始时间（RFC3339 或相对时间如 `-1h`、`-24h`） |
| end | string | 结束时间 |
| aggregate | string | 聚合窗口（如 `10m`、`1h`） |

示例：`GET /api/v1/data/query?device_id=modbus-plc-01&point=temperature&start=-1h`

### 视频接入

```
GET  /api/v1/video/devices              # 视频设备列表
GET  /api/v1/video/stream/{device_id}   # 获取视频流地址
POST /api/v1/video/ptz/{device_id}      # 云台控制
POST /api/v1/video/webhook              # PyGBSentry Webhook 回调
```

### 系统管理

```
GET  /api/v1/system/status   # 系统状态
GET  /api/v1/system/backup   # 备份列表
POST /api/v1/system/backup   # 创建备份
POST /api/v1/system/restore  # 恢复备份（需重启生效）
```

### 用户管理

```
GET    /api/v1/users       # 用户列表
POST   /api/v1/users       # 创建用户
PUT    /api/v1/users/{id}  # 更新用户
DELETE /api/v1/users/{id}  # 删除用户
```

---

## WebSocket 实时推送

### 连接方式

```
ws://host:8080/ws/v1/realtime?token=<access_token>  # 实时数据
ws://host:8080/ws/v1/alarm?token=<access_token>     # 告警事件
ws://host:8080/ws/v1/device?token=<access_token>    # 设备状态
```

### 消息格式

**realtime 频道**（测点值更新）：

```json
{
  "type": "point_update",
  "device_id": "modbus-plc-01",
  "point_name": "temperature",
  "value": 25.6,
  "quality": "good",
  "timestamp": "2024-01-15T10:30:00+00:00"
}
```

**alarm 频道**（告警事件）：

```json
{
  "type": "alarm",
  "alarm_id": "alarm-001",
  "rule_id": "temp-alarm-01",
  "device_id": "modbus-plc-01",
  "severity": "critical",
  "action": "firing",
  "timestamp": "2024-01-15T10:30:00+00:00"
}
```

**device 频道**（设备状态变更）：

```json
{
  "type": "device_status",
  "device_id": "modbus-plc-01",
  "old_status": "offline",
  "new_status": "online",
  "timestamp": "2024-01-15T10:30:00+00:00"
}
```

### 认证失败

Token 无效时，WebSocket 连接将被关闭，close code 为 `4001`。

### 客户端重连建议

```javascript
function connectWS(url, token) {
  const ws = new WebSocket(`${url}?token=${token}`);
  ws.onclose = (event) => {
    if (event.code === 4001) {
      // Token 无效，需要重新登录
      refreshToken().then(() => connectWS(url, newToken));
    } else {
      // 其他断连，延迟重连
      setTimeout(() => connectWS(url, token), 3000);
    }
  };
}
```

---

## RBAC 权限矩阵

### 角色与权限

| 权限 | admin | operator | viewer |
|------|:-----:|:--------:|:------:|
| device:create | ✅ | ❌ | ❌ |
| device:read | ✅ | ✅ | ✅ |
| device:update | ✅ | ❌ | ❌ |
| device:delete | ✅ | ❌ | ❌ |
| device:write_point | ✅ | ❌ | ❌ |
| rule:create | ✅ | ❌ | ❌ |
| rule:read | ✅ | ✅ | ✅ |
| rule:update | ✅ | ❌ | ❌ |
| rule:delete | ✅ | ❌ | ❌ |
| rule:toggle | ✅ | ✅ | ❌ |
| alarm:read | ✅ | ✅ | ✅ |
| alarm:ack | ✅ | ✅ | ❌ |
| data:read | ✅ | ✅ | ✅ |
| data:export | ✅ | ✅ | ✅ |
| video:read | ✅ | ✅ | ✅ |
| video:control | ✅ | ✅ | ❌ |
| system:read | ✅ | ✅ | ✅ |
| system:manage | ✅ | ❌ | ❌ |
| user:create | ✅ | ❌ | ❌ |
| user:read | ✅ | ❌ | ❌ |
| user:update | ✅ | ❌ | ❌ |
| user:delete | ✅ | ❌ | ❌ |

---

## 配置说明

配置文件：`configs/config.yaml`（从 `config.example.yaml` 复制）

### 完整配置项

```yaml
# 服务配置
server:
  host: "0.0.0.0"           # 监听地址
  port: 8080                 # 监听端口
  cors_origins:              # CORS 允许源（生产环境限制为实际域名）
    - "http://localhost:3000"

# 数据库配置
database:
  sqlite_path: "data/edgelite.db"  # SQLite 数据库路径
  backup_dir: "data/backups"       # 备份目录

# InfluxDB 时序数据库
influxdb:
  url: "http://localhost:8086"     # InfluxDB 地址
  token: "your-token"              # API Token（建议通过环境变量设置）
  org: "edgelite"                  # 组织名
  bucket: "edgelite"               # Bucket 名
  batch_size: 1000                 # 批量写入大小
  flush_interval: 5000             # 刷新间隔（毫秒）

# MQTT 北向转发
mqtt:
  broker: "localhost"              # MQTT Broker 地址
  port: 1883                       # Broker 端口
  username: ""                     # 认证用户名
  password: ""                     # 认证密码
  topic_prefix: "edgelite"         # 主题前缀

# 内置 MQTT Server（南向仿真）
mqtt_server:
  enabled: false                   # 是否启用
  host: "0.0.0.0"                  # 监听地址
  port: 1888                       # 监听端口
  ws_port: null                    # WebSocket 端口（null 不启用）
  username: ""                     # 认证用户名
  password: ""                     # 认证密码

# 内置 Modbus Slave（南向仿真）
modbus_slave:
  enabled: false                   # 是否启用
  host: "0.0.0.0"                  # 监听地址
  port: 502                        # 监听端口
  holding_size: 1000               # 保持寄存器数量
  input_size: 1000                 # 输入寄存器数量

# 视频接入（PyGBSentry）
video:
  pygbsentry:
    endpoint: ""                   # PyGBSentry 服务地址
    api_key: ""                    # API Key
    timeout: 10                    # 请求超时（秒）

# 安全配置
security:
  secret_key: "change-me"          # JWT 签名密钥（至少 32 字符）
  access_token_expire_minutes: 30  # access_token 过期时间
  refresh_token_expire_days: 7     # refresh_token 过期时间
  algorithm: "HS256"               # JWT 算法（HS256/HS384/HS512）

# 日志配置
logging:
  level: "INFO"                    # 日志级别：DEBUG/INFO/WARNING/ERROR/CRITICAL
  format: "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# 模拟器配置
simulator:
  auto_create: true                # 启动时自动创建模拟设备
  default_devices: []              # 默认模拟设备列表

# 告警通知渠道
notify:
  dingtalk:
    webhook_url: ""                # 钉钉机器人 Webhook URL
    secret: ""                     # 加签密钥
  email:
    smtp_host: ""                  # SMTP 服务器
    smtp_port: 465                 # SMTP 端口
    smtp_user: ""                  # SMTP 用户名
    smtp_password: ""              # SMTP 密码
    use_tls: true                  # 使用 TLS
    from_addr: ""                  # 发件地址（空则使用 smtp_user）
    to_addrs: []                   # 收件地址列表
  wechat:
    webhook_url: ""                # 企业微信机器人 Webhook URL
  webhook:
    url: ""                        # 自定义 Webhook URL
    headers: {}                    # 自定义请求头

# 北向平台对接
platforms: {}
```

### 环境变量覆盖

支持 `EDGELITE_` 前缀的环境变量覆盖配置（两级嵌套）：

| 环境变量 | 对应配置项 |
|---------|-----------|
| `EDGELITE_CONFIG` | 配置文件路径 |
| `EDGELITE_SERVER_HOST` | server.host |
| `EDGELITE_SERVER_PORT` | server.port |
| `EDGELITE_DATABASE_SQLITE_PATH` | database.sqlite_path |
| `EDGELITE_INFLUXDB_URL` | influxdb.url |
| `EDGELITE_INFLUXDB_TOKEN` | influxdb.token |
| `EDGELITE_INFLUXDB_ORG` | influxdb.org |
| `EDGELITE_INFLUXDB_BUCKET` | influxdb.bucket |
| `EDGELITE_MQTT_BROKER` | mqtt.broker |
| `EDGELITE_MQTT_PORT` | mqtt.port |
| `EDGELITE_SECURITY_SECRET_KEY` | security.secret_key |
| `EDGELITE_SECURITY_ALGORITHM` | security.algorithm |
| `EDGELITE_LOGGING_LEVEL` | logging.level |

---

## 常见问题

### Q: InfluxDB 连接失败怎么办？

A: 系统会自动降级到缓存模式，数据暂存本地 SQLite 队列（最大 10 万条），联网后自动续传。

### Q: 如何添加新设备？

A: 三种方式：
1. Web 界面：设备管理 → 创建设备
2. API：`POST /api/v1/devices`
3. 模拟器：`POST /api/v1/devices/simulator`

### Q: 如何配置告警通知？

A: 在 `configs/config.yaml` 中配置 `notify` 段，然后在创建规则时指定 `notify_channels`（如 `["dingtalk", "email"]`）。

### Q: 支持哪些数据类型？

A: Modbus 支持 INT16/UINT16/FLOAT32/BOOL，其他协议自动识别。

### Q: 如何修改默认密码？

A:
```bash
# 登录获取 Token
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json; print(json.load(sys.stdin)['data']['access_token'])")

# 修改密码
curl -X PUT http://localhost:8080/api/v1/users/admin \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"password": "new-secure-password"}'
```

### Q: 数据恢复后需要重启吗？

A: 是的，SQLite 恢复后需要重启 EdgeLiteGateway 服务才能生效。

---

## 技术支持

- 问题反馈：https://github.com/suoten/EdgeLiteGateway/issues
- 作者：suoten (suoten@163.com)

---

## 许可证

GPL-3.0 License
