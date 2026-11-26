"""OPC UA 驱动配置版本管理 - 复用 FINS 的 aiosqlite 持久化实现

FIX: 本文件原为"为让驱动能加载而恢复的最小桩"，与 OpcUaDriver 的实际调用接口不符——
驱动调用 get_current / get_versions / get_version_config / rollback / get_audit_trail /
diff_versions，而桩只有 get_version / list_versions / restore_version。任何一次调用都会
AttributeError，OPC UA 设备的配置历史 / 回滚 / 审计接口因此恒 500。

实现直接继承 FINS 的配置版本管理器（两张表：config_versions / config_audit_trail，
惰性初始化连接，每台设备独立的版本号序列），仅按 OPC UA 驱动的调用约定适配：
- get_versions 只接收 device_id 一个参数（limit 取默认值）。
"""

from __future__ import annotations

import logging
from typing import Any

from edgelite.drivers.fins_config_version import FinsConfigVersionManager

logger = logging.getLogger(__name__)

_DB_PATH = "data/opcua_config_versions.db"


class OpcUaConfigVersionManager(FinsConfigVersionManager):
    """OPC UA 配置版本管理器

    接口（与 OpcUaDriver 的调用方式一一对应）：
    - ``await save_version(device_id, config, change_summary, operator) -> int``：返回新版本号
    - ``await get_current(device_id) -> dict | None``：最新版本记录（含 ``config``）
    - ``await get_versions(device_id) -> list[dict]``：版本列表（倒序，不含配置正文）
    - ``await get_version_config(device_id, version) -> dict | None``：指定版本记录（含 ``config``）
    - ``await rollback(device_id, target_version, operator) -> dict | None``：回滚到目标版本，
      返回该版本记录；目标版本不存在返回 None，并在审计轨迹中记录一条 rollback
    - ``await get_audit_trail(device_id, limit) -> list[dict]``
    - ``await diff_versions(device_id, version_a, version_b) -> dict``：含 ``changes`` 列表
    - ``await stop()``：关闭数据库连接
    """

    def __init__(self, db_path: str = _DB_PATH) -> None:
        super().__init__()
        self._db_path = db_path

    async def get_versions(self, device_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """获取设备历史版本列表（倒序，最多 limit 条）

        注意：OpcUaDriver 只传 device_id 一个参数，limit 必须有默认值。
        """
        return await super().get_versions(device_id, limit)
