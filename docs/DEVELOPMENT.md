# EdgeLiteGateway v1.0 Community 开发指南

## 项目结构

```
EdgeLiteGateway/
├── src/edgelite/          # 后端源码
│   ├── api/               # REST API 路由
│   │   ├── auth.py        #   认证
│   │   ├── devices.py     #   设备管理
│   │   ├── rules.py       #   规则管理
│   │   ├── alarms.py      #   告警管理
│   │   ├── data.py        #   数据查询
│   │   ├── video.py       #   视频接入
│   │   ├── system.py      #   系统管理
│   │   ├── users.py       #   用户管理
│   │   └── deps.py        #   依赖注入（认证/权限）
│   ├── drivers/           # 协议驱动
│   │   ├── base.py        #   DriverPlugin 抽象基类
│   │   ├── registry.py    #   驱动注册表（自动发现）
│   │   ├── modbus_tcp.py  #   Modbus TCP
│   │   ├── opcua.py       #   OPC UA
│   │   ├── mqtt_client.py #   MQTT Client
│   │   ├── http_webhook.py#   HTTP Webhook
│   │   ├── simulator.py   #   模拟器
│   │   └── video/         #   视频接入
│   ├── engine/            # 核心引擎
│   │   ├── event_bus.py   #   事件总线
│   │   ├── scheduler.py   #   采集调度器
│   │   ├── evaluator.py   #   规则评估器
│   │   ├── lifecycle.py   #   设备生命周期
│   │   ├── mqtt_forwarder.py # MQTT 北向转发
│   │   ├── mqtt_server.py #   内置 MQTT Server
│   │   ├── opcua_server.py#   内置 OPC UA Server
│   │   └── modbus_slave.py#   内置 Modbus Slave
│   ├── models/            # Pydantic 数据模型
│   ├── security/          # 安全模块（JWT/RBAC/密码）
│   ├── services/          # 业务服务层
│   ├── storage/           # 存储层（SQLite/InfluxDB/缓存）
│   ├── platform/          # 北向平台对接
│   ├── ws/                # WebSocket 实时推送
│   └── _cython/           # Cython 加速模块
├── web/                   # 前端源码（Vue 3 + Naive UI）
│   └── src/
│       ├── api/           # API 封装
│       ├── views/         # 页面组件
│       ├── stores/        # Pinia 状态管理
│       └── layouts/       # 布局组件
├── configs/               # 配置文件
├── docker/                # Docker 配置
├── tests/                 # 测试用例
├── scripts/               # 工具脚本
└── docs/                  # 文档
```

---

## 开发环境搭建

### 后端

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest tests/ -v

# 代码检查
ruff check src/

# 格式化
ruff format src/
```

### 前端

```bash
cd web
npm install

# 开发模式
npm run dev

# 构建
npm run build

# 类型检查
npx vue-tsc --noEmit
```

---

## 核心模块说明

### 事件总线 (EventBus)

基于 `asyncio.Queue` 实现的异步事件总线：

```python
from edgelite.engine.event_bus import EventBus, PointUpdateEvent

bus = EventBus()

# 注册处理器
async def handler(event):
    print(f"收到数据: {event.points}")

bus.register_handler("PointUpdateEvent", handler)

# 发布事件
await bus.publish(PointUpdateEvent(device_id="dev-01", points={"temp": 25.0}))
```

### 采集调度器 (CollectScheduler)

管理设备采集任务：

```python
from edgelite.engine.scheduler import CollectScheduler

scheduler = CollectScheduler(event_bus, influx_storage, cache_manager)

# 启动采集
await scheduler.start_collect(device_id, driver, points, interval=5)

# 停止采集
await scheduler.stop_collect(device_id)
```

### 规则评估器 (RuleEvaluator)

评估规则并触发告警：

```python
from edgelite.engine.evaluator import RuleEvaluator

evaluator = RuleEvaluator(event_bus, rule_repo, alarm_repo)
await evaluator.start()
```

---

## 驱动开发

### 驱动接口

所有驱动继承 `DriverPlugin` 基类：

```python
from edgelite.drivers.base import DriverPlugin

class MyDriver(DriverPlugin):
    plugin_name = "my_driver"
    plugin_version = "1.0.0"
    supported_protocols = ["my_protocol"]

    async def start(self, config: dict) -> None:
        """启动驱动"""

    async def stop(self) -> None:
        """停止驱动"""

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点"""

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点"""
```

### 注册驱动

驱动会自动发现，只需放在 `src/edgelite/drivers/` 目录下。

---

## API 开发

### 添加新路由

1. 在 `src/edgelite/api/` 创建路由文件：

```python
# src/edgelite/api/my_route.py
from fastapi import APIRouter
from edgelite.models.common import ApiResponse

router = APIRouter(prefix="/api/v1/my", tags=["我的模块"])

@router.get("", response_model=ApiResponse)
async def my_endpoint():
    return ApiResponse(data={"message": "Hello"})
```

2. 在 `src/edgelite/app.py` 注册路由：

```python
from edgelite.api import my_route
app.include_router(my_route.router)
```

### 权限控制

使用 `require_permission` 依赖：

```python
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

@router.post("")
async def create_item(
    user: CurrentUser = require_permission(Permission.DEVICE_CREATE),
):
    # 只有拥有 device:create 权限的用户可访问
    pass
```

---

## 测试

### 单元测试

```python
# tests/test_my_module.py
import pytest

@pytest.mark.asyncio
async def test_my_function():
    result = await my_function()
    assert result == expected
```

### 运行测试

```bash
# 全部测试
pytest tests/ -v

# 覆盖率
pytest tests/ --cov=edgelite --cov-report=html
```

---

## 调试

### 日志

```python
import logging
logger = logging.getLogger(__name__)

logger.info("信息日志")
logger.warning("警告日志")
logger.error("错误日志")
```

### 开发模式热重载

```bash
python -m edgelite --reload
```

---

## 发布

### 版本号规则

- 主版本.次版本.修订号
- 社区版：v1.0.0

### 发布步骤

1. 更新版本号：`src/edgelite/__init__.py`
2. 更新 CHANGELOG
3. 构建前端：`cd web && npm run build`
4. 构建Docker镜像
5. 发布到 GitHub/Gitee
