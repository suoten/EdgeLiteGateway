# Contributing to EdgeLiteGateway

感谢你对 EdgeLiteGateway 的关注！本文档描述如何参与项目贡献。

## 开发环境搭建

### 后端

```bash
# 克隆仓库
git clone https://github.com/suoten/EdgeLiteGateway.git
cd EdgeLiteGateway

# 创建虚拟环境
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

# 安装开发依赖
pip install -e ".[dev]"
```

### 前端

```bash
cd web
npm install
```

## 代码规范

### Python（Ruff）

项目使用 [Ruff](https://docs.astral.sh/ruff/) 进行代码检查和格式化：

```bash
# 检查
ruff check src/

# 格式化
ruff format src/
```

配置（`pyproject.toml`）：
- `target-version = "py311"`
- `line-length = 100`
- 规则集：E, F, W, I, N, UP, B, A, SIM

### TypeScript / Vue

前端使用 TypeScript 严格模式，构建时自动检查类型：

```bash
cd web
npx vue-tsc --noEmit
```

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Type

| 类型 | 说明 |
|------|------|
| feat | 新功能 |
| fix | 修复 Bug |
| docs | 文档变更 |
| style | 代码格式（不影响逻辑） |
| refactor | 重构（非新功能/非修复） |
| test | 测试相关 |
| chore | 构建/工具/依赖 |

### 示例

```
feat(drivers): 添加 BACnet 协议驱动
fix(scheduler): 修复采集超时后任务未清理的问题
docs: 更新部署指南
```

## 分支策略

- `main` - 稳定发布分支
- `develop` - 开发集成分支
- `feature/*` - 功能分支
- `fix/*` - 修复分支

## PR 提交流程

1. Fork 仓库
2. 从 `develop` 创建功能分支：`git checkout -b feature/my-feature develop`
3. 编写代码并添加测试
4. 确保所有测试通过：`pytest tests/ -v`
5. 确保代码检查通过：`ruff check src/`
6. 提交并推送到 Fork 仓库
7. 创建 Pull Request 到 `develop` 分支
8. 等待代码审查

## 测试要求

### 运行测试

```bash
# 全部测试
pytest tests/ -v

# 带覆盖率
pytest tests/ --cov=edgelite --cov-report=html

# 特定测试文件
pytest tests/test_event_bus.py -v
```

### 编写测试

- 测试文件放在 `tests/` 目录
- 文件名以 `test_` 开头
- 异步测试使用 `pytest.mark.asyncio`
- 使用 `conftest.py` 共享 fixture

### 测试覆盖

- 新功能必须附带测试
- Bug 修复必须附带回归测试
- 目标覆盖率：核心模块 > 80%

## 驱动开发

添加新协议驱动：

1. 在 `src/edgelite/drivers/` 创建驱动文件
2. 继承 `DriverPlugin` 基类
3. 实现必要接口：`start()`, `stop()`, `read_points()`, `write_point()`
4. 驱动注册表会自动发现并注册

详见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。
