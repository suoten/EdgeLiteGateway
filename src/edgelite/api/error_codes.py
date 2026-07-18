"""API错误码定义 — 向后兼容重新导出模块。

错误码定义已迁移到 `edgelite.error_codes`（中立模块），
消除了 drivers/engine/storage/security 层对 api 层的跨层依赖。

此文件保留以兼容现有 `from edgelite.api.error_codes import ...` 导入。
新代码应使用 `from edgelite.error_codes import ...`。
"""

from __future__ import annotations

# FIXED: 重新导出所有错误码，消除跨层依赖
# 所有错误码定义已移至 edgelite.error_codes 中立模块
from edgelite.error_codes import *  # noqa: F401,F403

# 显式导出常用类（确保 IDE 自动补全和静态分析可用）
from edgelite.error_codes import (  # noqa: F401
    AuthErrors,
    AuthzErrors,
    CommonErrors,
    DatabaseErrors,
    DeviceErrors,
    DriverErrors,
    ErrorCodeDetail,
    FinsDriverErrors,
    IntegrationErrors,
    McDriverErrors,
    ModbusDriverErrors,
    OpcDaDriverErrors,
    OpcUaDriverErrors,
    RepoErrors,
    S7DriverErrors,
    ServiceErrors,
    SimulatorDriverErrors,
    SystemErrors,
)
