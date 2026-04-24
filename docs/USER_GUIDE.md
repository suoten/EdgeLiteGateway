# EdgeLiteGateway v1.0 Community Edition

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+ (前端开发)
- Docker & Docker Compose (推荐部署方式)

### 方式一：Docker Compose 一键启动（推荐）

```bash
# 进入 docker 目录
cd docker

# 启动所有服务（EdgeLiteGateway + InfluxDB + Mosquitto）
docker compose up -d

# 查看日志
docker compose logs -f edgelite

# 访问服务
# API 文档: http://localhost:8080/docs
# Web 管理: http://localhost:3000
```

### 方式二：本地开发启动

```bash
# 1. 安装 Python 依赖
pip install -e .

# 2. 启动后端
python -m edgelite --host 0.0.0.0 --port 8080

# 3. 启动前端（另一个终端）
cd web
npm install
npm run dev
```

### 默认账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| admin | admin123 | 管理员 |

**生产环境请立即修改默认密码！**

---

## 功能概览

### 设备接入

| 协议 | 说明 |
|------|------|
| Modbus TCP | 工业标准协议，支持线圈/寄存器读写 |
| MQTT Client | 订阅设备数据主题，支持 JSON 解析 |
| OPC-UA | 工业互联协议，支持节点订阅 |
| HTTP Webhook | 设备主动 POST 数据，无需认证 |
| Simulator | 内置模拟器，支持正弦/随机游走等模式 |

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

## 配置说明

配置文件：`configs/config.yaml`

### 核心配置项

```yaml
# 服务配置
server:
  host: "0.0.0.0"
  port: 8080

# InfluxDB 时序数据库
influxdb:
  url: "http://localhost:8086"
  token: "your-token"
  org: "edgelite"
  bucket: "edgelite"

# MQTT Broker（北向转发）
mqtt:
  broker: "localhost"
  port: 1883
  topic_prefix: "edgelite"

# 安全配置
security:
  secret_key: "change-me-in-production"
  access_token_expire_minutes: 30

# 通知渠道
notify:
  dingtalk:
    webhook_url: "https://oapi.dingtalk.com/robot/send?access_token=xxx"
  email:
    smtp_host: "smtp.example.com"
    smtp_port: 465
    smtp_user: "alert@example.com"
    smtp_password: "password"
    to_addrs: ["admin@example.com"]
```

### 环境变量覆盖

支持 `EDGELITE_` 前缀的环境变量覆盖配置：

```bash
export EDGELITE_INFLUXDB_URL=http://influxdb:8086
export EDGELITE_MQTT_BROKER=mosquitto
export EDGELITE_SECURITY_SECRET_KEY=your-secret
```

---

## API 接口

### 认证

```
POST /api/v1/auth/login     # 登录获取 Token
POST /api/v1/auth/refresh   # 刷新 Token
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

### 告警管理

```
GET /api/v1/alarms           # 告警列表
GET /api/v1/alarms/{id}      # 告警详情
PUT /api/v1/alarms/{id}/ack  # 确认告警
```

### 数据查询

```
GET /api/v1/data/query   # 时序数据查询
GET /api/v1/data/export  # 数据导出（CSV/JSON）
```

### 系统管理

```
GET  /api/v1/system/status   # 系统状态
GET  /api/v1/system/backup   # 备份列表
POST /api/v1/system/backup   # 创建备份
POST /api/v1/system/restore  # 恢复备份
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

```
ws://host:8080/ws/v1/realtime?token=xxx  # 实时数据
ws://host:8080/ws/v1/alarm?token=xxx     # 告警事件
ws://host:8080/ws/v1/device?token=xxx    # 设备状态
```

---

## 权限说明

| 角色 | 权限 |
|------|------|
| admin | 全部权限 |
| operator | 设备读取、规则切换、告警确认、数据查询 |
| viewer | 只读权限 |

---

## 常见问题

### Q: InfluxDB 连接失败怎么办？

A: 系统会自动降级到缓存模式，数据暂存本地 SQLite，联网后自动续传。

### Q: 如何添加新设备？

A:
1. Web 界面：设备管理 → 创建设备
2. API：POST /api/v1/devices
3. 模拟器：POST /api/v1/devices/simulator

### Q: 如何配置告警通知？

A: 在 `configs/config.yaml` 中配置 `notify` 段，然后在创建规则时指定 `notify_channels`。

### Q: 支持哪些数据类型？

A: Modbus 支持 INT16/UINT16/FLOAT32/BOOL，其他协议自动识别。

---

## 技术支持

- 问题反馈：https://github.com/suoten/EdgeLiteGateway/issues
- 作者：suoten (suoten@163.com)

---

## 许可证

GPL-3.0 License
