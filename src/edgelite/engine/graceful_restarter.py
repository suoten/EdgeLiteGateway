"""Graceful restarter — 优雅重启标记管理。

在 EdgeLite 升级/重启时，旧进程可写入重启标记文件（含版本信息），
新进程启动时通过 ``check_and_cleanup_marker`` 读取并清理标记，
从而在日志中记录 "旧版本 → 新版本" 的重启结果。

正常首次启动（无标记文件）时 ``check_and_cleanup_marker`` 返回 ``None``。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# 重启标记文件默认路径（相对于 data 目录或工作目录）
_DEFAULT_MARKER_FILENAME = ".restart_marker"


def _get_marker_path() -> str:
    """返回重启标记文件的完整路径。"""
    data_dir = os.environ.get("EDGELITE_DATA_DIR", "data")
    return os.path.join(data_dir, _DEFAULT_MARKER_FILENAME)


class GracefulRestarter:
    """优雅重启管理器。"""

    @staticmethod
    def write_marker(old_version: str = "", new_version: str = "", success: bool = True) -> None:
        """写入重启标记文件（旧进程退出前调用）。

        :param old_version: 旧版本号
        :param new_version: 新版本号
        :param success: 重启是否成功
        """
        marker_path = _get_marker_path()
        info: dict[str, Any] = {
            "old_version": old_version,
            "new_version": new_version,
            "success": success,
            "timestamp": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(marker_path), exist_ok=True)
            with open(marker_path, "w", encoding="utf-8") as f:
                json.dump(info, f)
            logger.info("Restart marker written: %s -> %s", old_version, new_version)
        except Exception as e:
            logger.warning("Failed to write restart marker: %s", e)

    @staticmethod
    def check_and_cleanup_marker() -> dict[str, Any] | None:
        """检查并清理重启标记文件（新进程启动时调用）。

        :return: 重启信息 dict（含 old_version/new_version/success/timestamp），
                 无标记文件时返回 None。
        """
        marker_path = _get_marker_path()
        if not os.path.isfile(marker_path):
            return None
        try:
            with open(marker_path, encoding="utf-8") as f:
                info = json.load(f)
            # 清理标记文件
            os.remove(marker_path)
            logger.info("Restart marker cleaned up: %s", marker_path)
            return info
        except Exception as e:
            logger.warning("Failed to read/cleanup restart marker: %s", e)
            # 即使读取失败也尝试清理
            try:
                os.remove(marker_path)
            except Exception:
                pass
            return None
