# EdgeLite V1.0 Community - 项目规则

## 项目概述

EdgeLiteGateway 是一个轻量级边缘计算物联网网关（社区版），采用 Python 后端 + Vue 3 前端架构，支持多种工业协议（Modbus、OPC UA、S7、FINS、MC、Allen-Bradley 等）的数据采集、规则引擎、告警、AI 推理和北向平台对接。

## 技术栈

### 后端
- **语言**: Python 3.11+
- **框架**: FastAPI + Uvicorn
- **ORM**: SQLAlchemy 2.x (async) + Alembic 迁移
- **数据库**: SQLite（元数据）+ InfluxDB（时序数据）
- **验证**: Pydantic 2.x
- **认证**: JWT (PyJWT) + bcrypt
- **协议驱动**: pymodbus, asyncua, python-snap7, pymcprotocol, fins, pylogix
- **消息**: aiomqtt (MQTT 5.0)
- **AI 推理**: ONNX Runtime
- **代码规范**: Ruff (lint + format), Pyright (类型检查)

### 前端
- **框架**: Vue 3 + TypeScript
- **UI 库**: Naive UI
- **状态管理**: Pinia
- **路由**: Vue Router 4
- **图表**: ECharts (vue-echarts)
- **3D**: Three.js
- **国际化**: vue-i18n (zh-CN / en-US)
- **构建**: Vite 5

## 项目结构

```
edgelite-v1.0-community/
├── src/edgelite/          # 后端源码
│   ├── api/               # FastAPI 路由层
│   ├── drivers/           # 协议驱动（Modbus, OPC UA, S7, FINS, MC 等）
│   ├── engine/            # 核心引擎（规则、调度、AI、表达式等）
│   ├── models/            # 数据模型（Pydantic + SQLAlchemy）
│   ├── services/          # 业务逻辑层
│   ├── storage/           # 存储层（SQLite, InfluxDB, 缓存）
│   ├── security/          # 安全模块（JWT, RBAC, 密码, 数据脱敏）
│   ├── platform/          # 北向平台对接（ThingsBoard, 华为 IoTDA 等）
│   ├── middleware/         # 中间件（CSRF, 限流, Token 续期）
│   ├── monitoring/        # 监控指标
│   ├── ws/                # WebSocket 管理
│   ├── _cython/           # Cython 加速模块
│   ├── ai_models/         # ONNX 模型文件
│   ├── config.py          # 配置加载
│   ├── app.py             # FastAPI 应用
│   └── bootstrap.py       # 启动引导
├── web/                   # 前端源码
│   └── src/
│       ├── api/           # HTTP/WebSocket 客户端
│       ├── components/    # Vue 组件
│       ├── composables/   # 组合式函数
│       ├── constants/     # 常量定义
│       ├── i18n/          # 国际化翻译
│       └── views/         # 页面视图
├── alembic/               # 数据库迁移
├── tests/                 # 测试用例
├── docker/                # Docker 部署配置
├── scripts/               # 工具脚本
├── configs/               # 配置文件模板
└── models/                # 预置模型生成
```

## 编码规范

### Python 后端

- **格式化**: Ruff，行长上限 100 字符
- **Lint 规则**: E, F, W, I, N, UP, B, A, SIM（忽略 B008, B027, N812, N813, N814）
- **类型检查**: Pyright (basic 模式)，Python 3.11
- **异步**: 全异步架构，使用 `async/await`，数据库操作使用 `aiosqlite` + `SQLAlchemy async`
- **导入**: 遵循 `from __future__ import annotations` 放置文件顶部
- **命名**: 模块/变量用 snake_case，类用 PascalCase，常量用 UPPER_SNAKE_CASE
- **驱动模块**: `src/edgelite/drivers/` 下的 F401（未使用导入）被忽略
- **引擎模块**: `src/edgelite/engine/` 下的 F401 被忽略
- **平台模块**: `src/edgelite/platform/` 下的 F401 被忽略

### Vue 前端

- **语言**: TypeScript（严格模式）
- **组件风格**: `<script setup>` + Composition API
- **路径别名**: `@` 映射到 `src/`
- **API 代理**: 开发时 `/api` 代理到 `http://localhost:8080`，`/ws` 代理 WebSocket

## 常用命令

### 后端
```bash
# 安装（开发模式）
pip install -e ".[dev]"

# 安装 AI 功能
pip install -e ".[ai]"

# 启动服务
python main.py

# 代码检查
ruff check src/

# 类型检查
pyright src/

# 运行测试
pytest

# 运行特定测试
pytest tests/test_modbus_rtu_driver.py -v

# 数据库迁移
alembic upgrade head
```

### 前端
```bash
cd web

# 安装依赖
npm install

# 开发服务器（端口 3000）
npm run dev

# 构建生产版本
npm run build

# 类型检查 + 构建
npm run build:check
```

### Docker
```bash
# 生产部署
docker compose -f docker/docker-compose.yml up -d

# 开发环境
docker compose -f docker/docker-compose.dev.yml up -d
```

## 环境变量

- 环境变量前缀: `EDGELITE_`
- Section 与 Key 用双下划线分隔: `EDGELITE_<SECTION>__<KEY>`
- 示例: `EDGELITE_SERVER__HOST=0.0.0.0`
- 配置模板: `.env.example`
- **禁止**在代码中硬编码密钥（pre-commit hook 会检查）

## 安全规范

- **密钥管理**: 使用 `secret_manager.py`，禁止硬编码 secret
- **认证**: JWT AccessToken + RefreshToken，支持 Token 撤销
- **授权**: RBAC 角色权限控制
- **密码**: bcrypt 哈希存储
- **数据脱敏**: 敏感数据展示前需脱敏处理
- **CORS**: 通过 `EDGELITE_SERVER__CORS_ORIGINS` 配置
- **TLS**: MQTT TLS 支持，证书验证

## 测试规范

- **框架**: pytest + pytest-asyncio（asyncio_mode = "auto"）
- **标记**:
  - `@pytest.mark.integration` - 集成测试（需外部服务）
  - `@pytest.mark.stress` - 压力测试
  - `@pytest.mark.stability` - 稳定性测试
  - `@pytest.mark.chaos` - 混沌工程测试
  - `@pytest.mark.e2e` - 端到端前端测试
- **测试路径**: `tests/`
- **北向平台测试**: `tests/north/`

## 国际化

- 后端: `src/edgelite/services/i18n.py`
- 前端: `web/src/i18n/` 下 `zh-CN.ts` 和 `en-US.ts`
- 检查脚本: `scripts/check_i18n_symmetry.py`, `scripts/check_i18n_coverage.py`

## 表单开发规范

### 新增表单的必须步骤

1. 先定义后端 Pydantic 模型（字段名、类型、验证规则）
2. 再定义前端表单结构（字段名与后端保持一致，类型对应）
3. 编写 API 对接层（web/src/api/xxx.ts）
4. 编写表单组件（使用 naive-ui 的 n-form）
5. 联调测试（提交 → 响应 → 回显）

### 字段命名约定

- 前端表单字段名 = 后端 Pydantic 字段名
- 禁止前端用 `device_name`，后端用 `deviceName` 这种混用
- 如果必须不一致，在 API 层做显式映射（不要隐式转换）

### 类型映射表

| 前端控件 | 前端值类型 | 后端 Pydantic 类型 | 数据库类型 |
|---------|-----------|-------------------|-----------|
| 输入框 | string | str | VARCHAR |
| 数字输入 | number | int / float | INTEGER / FLOAT |
| 开关 | boolean | bool | BOOLEAN |
| 选择器 | string / string[] | str / List[str] | VARCHAR / JSON |
| 日期时间 | number (timestamp) | datetime | DATETIME |
| 级联选择 | string[] | List[str] | JSON |

### n-input-number 空值防护

Naive UI 的 `n-input-number` 组件在用户清空输入框后，`v-model:value` 的值会变为 `null`，而非保留原值或变为 0。后端 Pydantic 的 `int`/`float` 类型会拒绝 `null` 值并返回 422 错误。

**必须**在提交函数中对所有 `n-input-number` 绑定的字段做空值防护：

```typescript
// 正确：使用 ?? 回退到默认值
const payload = {
  ...form,
  interval_seconds: form.interval_seconds ?? 60,
  timeout: form.timeout ?? 10,
}
await api.create(payload)

// 错误：直接提交，null 会触发后端 422
await api.create(form)
```

对于设备协议表单（`applyToConfig` 函数），同样需要防护：

```typescript
// 正确
c.port = config.port ?? 1883
c.timeout = config.timeout ?? 5

// 错误
c.port = config.port
c.timeout = config.timeout
```

### 错误显示规范

表单提交的 `catch` 块中，**禁止**使用 `e.message` 显示错误信息，必须使用 `extractError(e, fallback)`：

```typescript
import { extractError } from '@/utils/errorCodes'

// 正确
catch (e: any) {
  message.error(extractError(e, t('common.failed')))
}

// 错误 — 显示原始 axios 错误，用户无法理解
catch (e: any) {
  message.error(e.message)
}
```

### 数组/对象字段

- 前端多选框 → 后端 `List[str]`，空数组必须提交 `[]` 而不是 `null`/`undefined`
- 前端级联选择 → 后端嵌套 Pydantic 模型
- 提交前确保数组字段：`form.tags || []`

### 文件上传

- 必须使用 `multipart/form-data`
- 后端用 `UploadFile` / `File` 接收
- 前端不要 `JSON.stringify` 文件对象

## 注意事项

- 本项目在 Windows/GBK 环境下开发，注意编码问题
- `amqtt` 为 beta 版，仅作为可选依赖
- InfluxDB 为必需服务，未配置时序数据功能不可用
- `EDGELITE_SECURITY__SECRET_KEY` 必须配置，否则服务无法启动
- AI 功能需额外安装 `onnxruntime`：`pip install ".[ai]"`
- Cython 加速模块为可选，纯 Python 回退实现在 `_cython/*_py.py`
