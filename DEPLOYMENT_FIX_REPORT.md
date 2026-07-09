# EdgeLite 部署修复报告

## 一、部署修复清单

### 1. Docker 配置修复

| # | 文件 | 修复内容 | 严重级别 |
|---|------|----------|----------|
| D1 | `docker/Dockerfile` | 添加 `PYTHONDONTWRITEBYTECODE=1` 和 `PYTHONUNBUFFERED=1` 环境变量，解决 `read_only: true` 文件系统下 `__pycache__` 写入失败和日志缓冲问题 | P0 |
| D2 | `docker/docker-compose.yml` | CORS 来源添加 `http://localhost:5173`，与 `.env.example` 和前端 Vite 开发服务器保持一致 | P2 |
| D3 | `docker/docker-compose.dev.yml` | 同步添加 `PYTHONDONTWRITEBYTECODE=1` 和 `PYTHONUNBUFFERED=1` 环境变量 | P2 |
| D4 | `.dockerignore` (新建) | 创建 Docker 构建上下文排除文件，排除 `.git/`、`node_modules/`、`data/`、`logs/`、测试文件等，减小构建上下文体积 | P2 |

### 2. 启动脚本修复

| # | 文件 | 修复内容 | 严重级别 |
|---|------|----------|----------|
| S1 | `docker/entrypoint.sh` | **添加 alembic 迁移重试机制**（3 次重试，每次间隔 5 秒），防止数据库短暂锁竞争导致启动失败 | P0 |
| S2 | `docker/entrypoint.sh` | **添加配置文件优雅降级**：只读挂载时自动回退到 `config.example.yaml`，设置 `EDGELITE_CONFIG` 环境变量 | P1 |
| S3 | `docker/entrypoint.sh` | **添加管理员密码位置提示**：首次启动时提示运维在 `data/.initial_admin_password` 查看密码，不输出密码本身 | P1 |
| S4 | `docker/entrypoint.sh` | 添加运行时目录创建的错误处理 | P2 |

### 3. 配置文件修复

| # | 文件 | 修复内容 | 严重级别 |
|---|------|----------|----------|
| C1 | `configs/config.example.yaml` | **补全日志配置字段**：添加 `log_dir`、`max_bytes`、`backup_count`、`json_format`，与代码中 `config.logging.*` 读取逻辑对齐 | P1 |
| C2 | `docker/.env.example` | **添加 `INFLUXDB_USERNAME` 变量**（默认 `edgelite`），与 docker-compose.yml 中的 `${INFLUXDB_USERNAME:-edgelite}` 对齐 | P1 |
| C3 | `docker/.env.example` | **添加一键生成所有密钥的命令**，方便运维快速生成安全随机值 | P2 |
| C4 | `.env.example` | **修复 CORS 来源不一致**：将 `5173;3000;8180` 改为 `8080;3000;5173`，与 docker-compose.yml 对齐 | P1 |
| C5 | `.env.example` | 补全日志配置环境变量文档（`LOG_DIR`、`MAX_BYTES`、`BACKUP_COUNT`、`JSON_FORMAT`） | P2 |

### 4. 日志系统修复

| # | 文件 | 修复内容 | 严重级别 |
|---|------|----------|----------|
| L1 | `src/edgelite/bootstrap.py` | **修复 SensitiveFilter 未生效 bug**：Python 日志框架中 logger 级别的 filter 不传播到子 logger 的记录。将 `SensitiveFilter` 同时添加到每个 `Handler` 上（`file_handler`、`error_handler`），确保所有日志记录（包括 uvicorn/aiomqtt 等第三方库）都经过脱敏处理 | P0 |

### 5. 文档一致性修复

| # | 文件 | 修复内容 | 严重级别 |
|---|------|----------|----------|
| W1 | `README.md` | **修复管理员密码描述**：原文说"打印到控制台日志"，实际代码写入 `data/.initial_admin_password` 文件（出于安全不输出到日志） | P0 |
| W2 | `README.md` | **修复默认密码引用**：移除 `admin / admin123` 的错误描述，改为引导用户查看 `data/.initial_admin_password` | P0 |
| W3 | `README.md` | 修复混合模式部署说明中的密码引用 | P1 |
| W4 | `README_EN.md` | 同步修复英文版 README 中的相同问题 | P0 |

---

## 二、一键部署验证步骤

### 步骤 1: 准备环境变量

```bash
cd docker
cp .env.example .env

# 生成所有必需密钥（在宿主机执行）
python -c "import secrets; print(f'INFLUXDB_TOKEN={secrets.token_urlsafe(48)}')" >> .env
python -c "import secrets; print(f'INFLUXDB_PASSWORD={secrets.token_urlsafe(16)}')" >> .env
python -c "import secrets; print(f'SECRET_KEY={secrets.token_urlsafe(32)}')" >> .env
python -c "import secrets; print(f'MQTT_PASSWORD={secrets.token_urlsafe(16)}')" >> .env
python -c "import secrets; print(f'ADMIN_PASSWORD={secrets.token_urlsafe(16)}')" >> .env
python -c "import secrets; print(f'EDGELITE_MASTER_KEY={secrets.token_hex(32)}')" >> .env
python -c "import base64,os; print(f'EDGELITE_KDF_SALT={base64.b64encode(os.urandom(16)).decode()}')" >> .env

# 编辑 .env，替换所有 <...> 占位符
vi .env
```

### 步骤 2: 构建并启动

```bash
cd docker
docker compose build edgelite
docker compose up -d
```

### 步骤 3: 验证服务健康

```bash
# 检查所有容器状态
docker compose ps
# 期望：edgelite/influxdb/mosquitto 状态均为 Up (healthy)

# 检查后端存活探针
curl -s http://localhost:8080/health/live
# 期望：{"status":"ok"}

# 检查后端就绪探针
curl -s http://localhost:8080/health/ready
# 期望：{"status":"ready","checks":{"sqlite":{"status":"healthy"},"influxdb":{"status":"healthy"}}}

# 检查完整健康状态
curl -s http://localhost:8080/health | python -m json.tool
# 期望：status=healthy，所有 checks 为 healthy

# 检查 InfluxDB
curl -s http://localhost:8086/health
# 期望：{"status":"pass"}
```

### 步骤 4: 获取初始密码并登录

```bash
# 如果未设置 ADMIN_PASSWORD，查看自动生成的密码
cat data/.initial_admin_password

# 浏览器打开
# http://localhost:8080
# 用 admin / <密码> 登录，首次登录需修改密码
```

### 步骤 5: 验证日志脱敏

```bash
# 查看日志文件，确认密码/Token 已脱敏
docker compose exec edgelite cat /app/logs/edgelite.log | grep -i "password\|token\|secret"
# 期望：所有敏感字段显示为 password=*** 或 token=***
```

### 步骤 6: 验证日志轮转

```bash
# 检查日志文件大小（应 < 50MB）
docker compose exec edgelite ls -lh /app/logs/
# 期望：edgelite.log < 50MB，有 .log.1/.log.2 等轮转备份

# 检查 Docker 日志限制
docker inspect edgelite-gateway --format '{{.HostConfig.LogConfig}}'
# 期望：{json-file map[max-file:3 max-size:10m]}
```

### 步骤 7: 验证配置热更新

```bash
# 修改宿主机 configs/config.yaml 中的某个配置项
# 等待 5 秒（热加载轮询间隔）
# 查看日志确认热加载生效
docker compose logs --tail=20 edgelite | grep -i "config.*reload\|config.*change"
```

### 步骤 8: 验证优雅降级

```bash
# 停止 InfluxDB，验证降级模式
docker compose stop influxdb
sleep 10
curl -s http://localhost:8080/health | python -m json.tool
# 期望：influxdb status=unhealthy，但整体服务仍存活

# 恢复 InfluxDB
docker compose start influxdb
sleep 10
curl -s http://localhost:8080/health | python -m json.tool
# 期望：所有服务恢复 healthy
```

---

## 三、修复文件清单

### 新建文件 (1)
1. `.dockerignore` — Docker 构建上下文排除文件

### 修改文件 (11)
1. `docker/Dockerfile` — 添加 `PYTHONDONTWRITEBYTECODE`/`PYTHONUNBUFFERED` 环境变量
2. `docker/docker-compose.yml` — CORS 来源对齐
3. `docker/docker-compose.dev.yml` — 同步环境变量
4. `docker/entrypoint.sh` — 重试机制/优雅降级/密码提示
5. `docker/.env.example` — 补全变量/生成命令
6. `.env.example` — CORS 来源修复/日志配置补全
7. `configs/config.example.yaml` — 补全日志配置字段
8. `src/edgelite/bootstrap.py` — 修复 SensitiveFilter handler 注册
9. `README.md` — 修复密码描述/命令一致性
10. `README_EN.md` — 同步修复英文版

---

## 四、验证结果

| 验证项 | 结果 |
|--------|------|
| YAML 语法验证 (docker-compose.yml) | ✅ valid |
| YAML 语法验证 (docker-compose.dev.yml) | ✅ valid |
| YAML 语法验证 (config.example.yaml) | ✅ valid |
| Ruff Lint (bootstrap.py) | ✅ All checks passed |
| 端口映射一致性 (README ↔ docker-compose) | ✅ 8080/3000/8086/1883 一致 |
| 环境变量一致性 (.env.example ↔ docker-compose ↔ config.example) | ✅ 一致 |
