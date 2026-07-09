# EdgeLiteGateway 开发指南

## 开发环境搭建

### 1. 克隆仓库

```bash
git clone https://github.com/suoten/EdgeLiteGateway.git
cd EdgeLiteGateway
```

### 2. 后端环境

```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

pip install -e ".[dev]"
```

### 3. 前端环境

```bash
cd web
npm install
```

### 4. 启动开发服务

```bash
# 后端（热重载）
python -m edgelite --host 0.0.0.0 --port 8080 --reload

# 前端（另一个终端）
cd web && npm run dev
# 访问 http://localhost:3000（Vite 自动代理 API 到 8080）
```

---

## 项目结构

```
EdgeLiteGateway/
├── src/edgelite/              # Python 后端
│   ├── app.py                 # FastAPI 应用工厂
│   ├── main.py                # 入口点
│   ├── config.py              # 配置加载（YAML + 环境变量覆盖）
│   ├── drivers/               # 协议驱动
│   │   ├── base.py            # DriverPlugin 抽象基类
│   │   ├── registry.py        # 驱动注册表
│   │   ├── modbus_tcp.py      # Modbus TCP 驱动
│   │   ├── opcua.py           # OPC UA 驱动
│   │   ├── mqtt_client.py     # MQTT 客户端驱动
│   │   ├── http_webhook.py    # HTTP Webhook 驱动
│   │   ├── simulator.py       # 模拟器驱动
│   │   └── video.py           # 视频驱动（PyGBSentry）
│   ├── engine/                # 核心引擎
│   │   ├── event_bus.py       # 事件总线
│   │   ├── collector.py       # 采集调度器
│   │   ├── rule_evaluator.py  # 规则评估器
│   │   └── lifecycle.py       # 生命周期管理
│   ├── routers/               # API 路由
│   │   ├── auth.py            # 认证
│   │   ├── devices.py         # 设备管理
│   │   ├── rules.py           # 规则管理
│   │   ├── alarms.py          # 告警管理
│   │   ├── data.py            # 数据查询
│   │   ├── video.py           # 视频接入
│   │   ├── system.py          # 系统管理
│   │   └── users.py           # 用户管理
│   ├── services/              # 业务服务
│   ├── storage/               # 存储层
│   │   ├── database.py        # SQLite 数据库
│   │   ├── influxdb.py        # InfluxDB 客户端
│   │   └── cache.py           # 断网缓存
│   ├── security/              # 安全层
│   │   ├── auth.py            # JWT 认证
│   │   └── rbac.py            # RBAC 权限
│   ├── notify/                # 通知渠道
│   │   ├── dingtalk.py        # 钉钉
│   │   ├── email.py           # 邮件
│   │   ├── wechat.py          # 企业微信
│   │   └── webhook.py         # 自定义 Webhook
│   ├── platforms/             # 北向平台对接
│   │   ├── base.py            # PlatformHandler 基类
│   │   ├── iotsharp.py        # IoTSharp
│   │   └── thingsboard.py     # ThingsBoard
│   ├── servers/               # 内置服务
│   │   ├── mqtt_server.py     # MQTT Server
│   │   └── modbus_slave.py    # Modbus Slave
│   └── _cython/               # Cython 加速（可选）
│       ├── modbus_mapper.pyx  # Modbus 映射加速
│       ├── rule_compare.pyx   # 规则比较加速
│       └── *_py.py            # 纯 Python 回退
├── web/                       # Vue 3 前端
│   ├── src/
│   │   ├── views/             # 页面组件
│   │   ├── components/        # 通用组件
│   │   ├── layouts/           # 布局组件
│   │   ├── stores/            # Pinia 状态
│   │   ├── api/               # API 请求
│   │   └── utils/             # 工具函数
│   └── public/                # 静态资源
├── configs/                   # 配置文件
├── docker/                    # Docker 部署
├── scripts/                   # 工具脚本
├── tests/                     # 测试
└── docs/                      # 文档
```

---

## 后端开发

### 代码规范

- **格式化**：Ruff（`ruff format`）
- **Lint**：Ruff（`ruff check`）
- **类型注解**：Python 3.11+ 风格（`list[str]` 而非 `List[str]`）
- **异步**：全部使用 `async/await`，禁止同步阻塞调用
- **日志**：使用 `logging.getLogger(__name__)`，不使用 `print()`

### 运行检查

```bash
ruff check src/
ruff format src/
```

### 测试

```bash
# 运行全部测试
pytest tests/ -v

# 运行指定模块测试
pytest tests/test_drivers.py -v

# 查看覆盖率
pytest tests/ --cov=edgelite --cov-report=html
```

### 驱动开发完整指南

#### 1. 创建驱动文件

在 `src/edgelite/drivers/` 下创建新文件，继承 `DriverPlugin`：

```python
# src/edgelite/drivers/my_protocol.py
from __future__ import annotations

import logging
from typing import Any, Callable

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class MyProtocolDriver(DriverPlugin):
    """MyProtocol 协议驱动"""

    plugin_name = "my_protocol"
    plugin_version = "1.0.0"
    supported_protocols = ["my_protocol"]

    def __init__(self):
        self._running = False
        self._connections: dict[str, Any] = {}
        self._data_callback: Callable | None = None

    async def start(self, config: dict) -> None:
        """启动驱动，config 来自设备的 config 字段"""
        host = config.get("host", "localhost")
        port = config.get("port", 9000)
        # 初始化连接...
        self._running = True
        logger.info("MyProtocol 驱动启动: %s:%s", host, port)

    async def stop(self) -> None:
        """停止驱动，关闭所有连接"""
        for conn in self._connections.values():
            await conn.close()
        self._connections.clear()
        self._running = False

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值，返回 {point_name: value}"""
        result = {}
        for point in points:
            # 读取测点值...
            result[point] = 0.0
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值，返回是否成功"""
        try:
            # 写入测点值...
            return True
        except Exception as e:
            logger.error("写入失败: %s.%s = %s - %s", device_id, point, value, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """发现设备（可选实现）"""
        # 扫描网络中的设备...
        return [
            {"device_id": "found-001", "name": "Device 1", "config": {...}}
        ]

    def on_data(self, callback: Callable) -> None:
        """注册数据回调（可选，用于推送型协议）"""
        self._data_callback = callback
```

#### 2. 注册驱动

在 `src/edgelite/drivers/registry.py` 中注册：

```python
from edgelite.drivers.my_protocol import MyProtocolDriver

# 在 _register_builtin_drivers 方法中添加
self._drivers["my_protocol"] = MyProtocolDriver
```

#### 3. 设备配置约定

设备 `config` 字段（JSON）应包含连接参数：

```json
{
  "host": "192.168.1.100",
  "port": 9000,
  "timeout": 5,
  "unit_id": 1
}
```

设备 `points` 字段（JSON）定义测点：

```json
[
  {"name": "temperature", "address": "0", "data_type": "float32", "unit": "°C"},
  {"name": "pressure", "address": "2", "data_type": "float32", "unit": "MPa"}
]
```

#### 4. 推送型协议（如 MQTT）

对于设备主动推送数据的协议，实现 `on_data()` 方法：

```python
def on_data(self, callback: Callable) -> None:
    self._data_callback = callback

# 在收到数据时调用回调
async def _on_message(self, topic: str, payload: bytes):
    if self._data_callback:
        await self._data_callback(device_id, point_name, value)
```

#### 5. 驱动测试

```python
# tests/test_my_protocol.py
import pytest
from edgelite.drivers.my_protocol import MyProtocolDriver

@pytest.fixture
def driver():
    return MyProtocolDriver()

@pytest.mark.asyncio
async def test_start_stop(driver):
    await driver.start({"host": "localhost", "port": 9000})
    assert driver.is_running
    await driver.stop()
    assert not driver.is_running

@pytest.mark.asyncio
async def test_read_points(driver):
    await driver.start({"host": "localhost", "port": 9000})
    result = await driver.read_points("dev-001", ["temperature"])
    assert "temperature" in result
    await driver.stop()
```

### 通知渠道开发

在 `src/edgelite/notify/` 下创建新文件：

```python
# src/edgelite/notify/my_channel.py
import logging
from typing import Any

logger = logging.getLogger(__name__)


class MyChannelNotifier:
    """自定义通知渠道"""

    def __init__(self, config: dict):
        self.url = config.get("url", "")
        self.token = config.get("token", "")

    async def send(self, title: str, message: str, severity: str, **kwargs: Any) -> bool:
        """发送通知，返回是否成功"""
        try:
            # 发送通知...
            return True
        except Exception as e:
            logger.error("通知发送失败: %s", e)
            return False
```

然后在 `NotifyService` 中注册新渠道。

### 北向平台对接开发

继承 `PlatformHandler` 基类：

```python
# src/edgelite/platforms/my_platform.py
from edgelite.platforms.base import PlatformHandler

class MyPlatformHandler(PlatformHandler):
    async def connect(self, config: dict) -> None:
        """连接平台"""

    async def disconnect(self) -> None:
        """断开连接"""

    async def publish_telemetry(self, device_id: str, data: dict) -> None:
        """发布遥测数据"""

    async def publish_attributes(self, device_id: str, attrs: dict) -> None:
        """发布设备属性"""

    async def on_rpc_request(self, callback: Callable) -> None:
        """注册 RPC 请求回调"""

    async def publish_device_status(self, device_id: str, status: str) -> None:
        """发布设备状态"""
```

### Cython 编译

```bash
# 安装 Cython
pip install cython

# 编译加速模块
python setup.py build_ext --inplace

# 验证编译成功
python -c "from edgelite._cython.modbus_mapper import map_registers; print('OK')"
```

未安装 Cython 时自动使用纯 Python 回退（`_cython/*_py.py`），功能完全相同。

---

## 前端开发

### 技术栈

- Vue 3.4 + Composition API
- Naive UI 组件库
- Pinia 状态管理
- Vue Router 4
- ECharts 图表
- Axios HTTP 客户端
- Vite 构建工具

### 代码规范

- **组件**：`<script setup lang="ts">` + SFC
- **状态**：Pinia stores（`web/src/stores/`）
- **API 请求**：`web/src/api/` 统一封装
- **样式**：Scoped CSS，使用 Naive UI 主题变量
- **类型**：TypeScript 类型注解

### 开发命令

```bash
cd web

# 开发服务器
npm run dev

# 生产构建
npm run build

# 预览构建产物
npm run preview

# 类型检查
npm run type-check

# Lint
npm run lint
```

### 新增页面示例

1. 在 `web/src/views/` 创建页面组件
2. 在 `web/src/router/` 注册路由
3. 在 `web/src/api/` 添加 API 请求
4. 在 `web/src/stores/` 添加状态管理（如需要）

---

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

```
<type>(<scope>): <subject>

<body>
```

**type**：`feat` | `fix` | `docs` | `style` | `refactor` | `perf` | `test` | `chore`

**scope**：`driver` | `engine` | `api` | `ui` | `config` | `storage` | `security` | `notify`

示例：

```
feat(driver): 添加 BACnet 协议驱动
fix(engine): 修复规则评估器持续时间窗口计算错误
docs(api): 补充设备推送接口文档
```
