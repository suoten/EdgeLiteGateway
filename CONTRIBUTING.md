# 贡献指南

感谢你对 EdgeLiteGateway 的关注！欢迎提交 Issue 和 Pull Request。

## 开发环境

```bash
# 1. Fork 并克隆
git clone https://github.com/<your-username>/EdgeLiteGateway.git
cd EdgeLiteGateway

# 2. 后端环境
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# 3. 前端环境
cd web && npm install && cd ..

# 4. 启动开发服务
python -m edgelite --reload   # 后端
cd web && npm run dev          # 前端
```

## 代码规范

### Python 后端

- **格式化 & Lint**：Ruff
  ```bash
  ruff check src/ tests/
  ruff format src/ tests/
  ```
- **类型注解**：Python 3.11+ 风格（`list[str]`、`dict[str, Any]`）
- **异步**：全部 `async/await`，禁止同步阻塞
- **日志**：`logging.getLogger(__name__)`，不使用 `print()`
- **导入**：`from __future__ import annotations` 置顶

### Vue 前端

- **组件**：`<script setup lang="ts">` + SFC
- **状态**：Pinia stores
- **样式**：Scoped CSS + Naive UI 主题变量
- **Lint**：ESLint + Prettier
  ```bash
  cd web && npm run lint
  ```

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>
```

| type | 说明 |
|------|------|
| feat | 新功能 |
| fix | 修复 Bug |
| docs | 文档变更 |
| style | 代码格式（不影响逻辑） |
| refactor | 重构 |
| perf | 性能优化 |
| test | 测试 |
| chore | 构建/工具 |

| scope | 说明 |
|-------|------|
| driver | 协议驱动 |
| engine | 核心引擎 |
| api | API 路由 |
| ui | 前端界面 |
| config | 配置 |
| storage | 存储层 |
| security | 安全 |
| notify | 通知 |

## 分支策略

- `main`：稳定版本
- `dev`：开发分支
- `feat/<name>`：功能分支
- `fix/<name>`：修复分支

## PR 流程

1. 从 `dev` 创建功能分支：`git checkout -b feat/my-feature dev`
2. 开发并提交（遵循提交规范）
3. 确保测试通过：`pytest tests/ -v`
4. 确保代码检查通过：`ruff check src/`
5. 推送分支：`git push origin feat/my-feature`
6. 创建 Pull Request 到 `dev` 分支

### PR 检查清单

- [ ] 代码通过 Ruff 检查（`ruff check src/`）
- [ ] 代码通过格式化（`ruff format --check src/`）
- [ ] 测试通过（`pytest tests/ -v`）
- [ ] 新功能有对应测试
- [ ] 提交信息遵循 Conventional Commits
- [ ] 无硬编码密钥或敏感信息
- [ ] 文档已更新（如涉及 API 变更）

## 驱动开发

### 创建新协议驱动

1. 在 `src/edgelite/drivers/` 创建驱动文件，继承 `DriverPlugin`
2. 实现必要方法：`start()`, `stop()`, `read_points()`, `write_point()`
3. 可选实现：`discover_devices()`, `on_data()`
4. 在 `registry.py` 中注册驱动
5. 编写测试（`tests/`）
6. 更新文档

### DriverPlugin 接口

```python
class DriverPlugin(ABC):
    plugin_name: str           # 驱动标识
    plugin_version: str        # 版本号
    supported_protocols: list  # 支持的协议列表

    async def start(self, config: dict) -> None
    async def stop(self) -> None
    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]
    async def write_point(self, device_id: str, point: str, value: Any) -> bool
    async def discover_devices(self, config: dict) -> list[dict]  # 可选
    def on_data(self, callback: Callable) -> None                  # 可选
    @property
    def is_running(self) -> bool
```

### 驱动测试模板

```python
import pytest
from edgelite.drivers.my_driver import MyDriver

@pytest.fixture
def driver():
    return MyDriver()

@pytest.mark.asyncio
async def test_lifecycle(driver):
    await driver.start({"host": "localhost", "port": 9000})
    assert driver.is_running
    await driver.stop()
    assert not driver.is_running

@pytest.mark.asyncio
async def test_read_points(driver):
    await driver.start({"host": "localhost", "port": 9000})
    result = await driver.read_points("dev-001", ["point1"])
    assert isinstance(result, dict)
    await driver.stop()
```

## Cython 加速模块

如需修改 Cython 加速模块：

1. 修改 `.pyx` 文件
2. 同步修改 `_py.py` 纯 Python 回退
3. 编译测试：`pip install cython && python setup.py build_ext --inplace`
4. 确保纯 Python 回退也能正常工作

## Issue 模板

提交 Issue 时请包含：

- **Bug 报告**：环境信息、复现步骤、期望行为、实际行为、日志
- **功能请求**：使用场景、期望行为、替代方案

## 许可证

贡献的代码遵循 GPL-3.0 许可证。
