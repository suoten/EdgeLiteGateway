"""链路冗余管理 - 为协议驱动提供主备链路自动切换能力。

管理主站(primary)与备站(backup)之间的链路状态:
- record_failure 累计失败次数，达到 failover_threshold 后自动切换到备站
- record_success 重置失败计数；若当前在备站且 auto_revert=True，标记主站健康
- mark_primary_healthy 显式标记主站恢复，触发自动回切(auto_revert)
- 切换时通过 on_switch_callback 通知驱动层更新 active_ip

设计要点:
- 线程安全: 所有状态变更在 _lock 保护下进行 (同步 threading.Lock，适配驱动层调用)
- 无后台任务: recovery probe 由驱动层心跳触发 (record_success/mark_primary_healthy)
  避免管理器内嵌 asyncio 任务导致生命周期复杂化
- 事件通知: 链路切换时发布 RedundancySwitchEvent 到 EventBus (如有)
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from edgelite.engine.event_bus import EventBus

logger = logging.getLogger(__name__)


class LinkRole(StrEnum):
    """链路角色枚举"""

    PRIMARY = "primary"
    BACKUP = "backup"


@dataclass
class RedundancyConfig:
    """单设备的链路冗余配置

    Attributes:
        primary_host: 主站 IP 地址
        primary_port: 主站端口
        backup_host: 备站 IP 地址 (空字符串表示无备站)
        backup_port: 备站端口
        failover_threshold: 主站连续失败多少次后切换到备站
        recovery_probe_interval: 主站恢复探测间隔 (秒)，驱动层心跳按此间隔探测
        auto_revert: 备站工作期间主站恢复后是否自动回切到主站
        auto_revert_stable_count: 主站连续成功多少次后触发回切
        link_timeout: 链路连接超时 (秒)，传递给驱动层用于 connect 超时
    """

    primary_host: str
    primary_port: int
    backup_host: str = ""
    backup_port: int = 0
    failover_threshold: int = 3
    recovery_probe_interval: float = 30.0
    auto_revert: bool = True
    auto_revert_stable_count: int = 3
    link_timeout: float = 5.0


@dataclass
class _DeviceState:
    """单设备冗余运行态 (内部使用)"""

    config: RedundancyConfig
    active_role: LinkRole = LinkRole.PRIMARY
    fail_count: int = 0
    primary_stable_count: int = 0  # 备站期间主站连续探测成功次数
    last_switch_time: float = 0.0
    last_fail_time: float = 0.0
    total_failovers: int = 0


class LinkRedundancyManager:
    """链路冗余管理器 — 管理多设备的主备链路切换

    线程安全: 使用 threading.Lock 保护内部状态，可被同步/异步驱动层安全调用。
    事件通知: 链路切换时调用 on_switch_callback(device_id, old_host, new_host)。
    """

    def __init__(self, event_bus: EventBus | None = None, config: RedundancyConfig | None = None) -> None:
        """初始化冗余管理器

        Args:
            event_bus: 事件总线实例 (可选)，链路切换时发布事件
            config: 全局默认冗余配置 (可选)，register_device 时可覆盖
        """
        self._event_bus = event_bus
        self._default_config = config
        self._devices: dict[str, _DeviceState] = {}
        self._lock = threading.Lock()
        self._on_switch_callback: Callable[[str, str, str], None] | None = None

    def set_on_switch_callback(self, callback: Callable[[str, str, str], None]) -> None:
        """设置链路切换回调函数

        Args:
            callback: 回调函数签名 (device_id, old_host, new_host) -> None
        """
        self._on_switch_callback = callback

    def register_device(self, device_id: str, config: RedundancyConfig) -> None:
        """注册设备到冗余管理器

        Args:
            device_id: 设备唯一标识
            config: 该设备的冗余配置
        """
        with self._lock:
            self._devices[device_id] = _DeviceState(config=config)
        logger.info(
            "[redundancy] device=%s code=REGISTERED msg=primary=%s:%d backup=%s:%d threshold=%d",
            device_id, config.primary_host, config.primary_port,
            config.backup_host or "(none)", config.backup_port,
            config.failover_threshold,
        )

    def unregister_device(self, device_id: str) -> None:
        """注销设备

        Args:
            device_id: 设备唯一标识
        """
        with self._lock:
            self._devices.pop(device_id, None)

    def record_success(self, device_id: str) -> None:
        """记录设备通信成功

        - 当前在主站: 重置失败计数
        - 当前在备站且 auto_revert=True: 累计主站稳定计数，达到阈值后回切

        Args:
            device_id: 设备唯一标识
        """
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                return
            state.fail_count = 0
            # 备站期间记录主站探测成功 (驱动层心跳探测主站恢复时调用)
            if state.active_role == LinkRole.BACKUP and state.config.auto_revert:
                state.primary_stable_count += 1
                if state.primary_stable_count >= state.config.auto_revert_stable_count:
                    self._switch_to_primary_locked(device_id, state)

    def record_failure(self, device_id: str) -> None:
        """记录设备通信失败

        - 累计失败次数，达到 failover_threshold 且有备站时切换到备站

        Args:
            device_id: 设备唯一标识
        """
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                return
            state.fail_count += 1
            state.last_fail_time = time.monotonic()
            if (state.active_role == LinkRole.PRIMARY
                    and state.fail_count >= state.config.failover_threshold
                    and state.config.backup_host):
                self._switch_to_backup_locked(device_id, state)

    def get_active_role(self, device_id: str) -> LinkRole:
        """获取设备当前活跃链路角色

        Args:
            device_id: 设备唯一标识

        Returns:
            LinkRole.PRIMARY 或 LinkRole.BACKUP；未注册设备返回 PRIMARY
        """
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                return LinkRole.PRIMARY
            return state.active_role

    def get_active_host(self, device_id: str) -> str:
        """获取设备当前活跃主机地址

        Args:
            device_id: 设备唯一标识

        Returns:
            当前活跃主机 IP；未注册设备返回空字符串
        """
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                return ""
            if state.active_role == LinkRole.BACKUP:
                return state.config.backup_host
            return state.config.primary_host

    def get_status(self, device_id: str) -> dict[str, Any]:
        """获取设备冗余状态摘要

        Args:
            device_id: 设备唯一标识

        Returns:
            状态字典，包含 role/active_host/fail_count/total_failovers 等；
            未注册设备返回空字典
        """
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                return {}
            cfg = state.config
            active_host = cfg.backup_host if state.active_role == LinkRole.BACKUP else cfg.primary_host
            return {
                "role": state.active_role.value,
                "active_host": active_host,
                "primary_host": cfg.primary_host,
                "backup_host": cfg.backup_host,
                "fail_count": state.fail_count,
                "primary_stable_count": state.primary_stable_count,
                "total_failovers": state.total_failovers,
                "last_switch_time": state.last_switch_time,
                "auto_revert": cfg.auto_revert,
            }

    def mark_primary_healthy(self, device_id: str) -> None:
        """显式标记主站健康 (驱动层探测到主站恢复时调用)

        若当前在备站且 auto_revert=True，立即触发回切到主站。

        Args:
            device_id: 设备唯一标识
        """
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                return
            if state.active_role == LinkRole.BACKUP and state.config.auto_revert:
                self._switch_to_primary_locked(device_id, state)

    def stop(self) -> None:
        """停止管理器，清理所有设备状态"""
        with self._lock:
            count = len(self._devices)
            self._devices.clear()
        if count:
            logger.info("[redundancy] code=STOPPED msg=Cleared %d device(s)", count)

    def _switch_to_backup_locked(self, device_id: str, state: _DeviceState) -> None:
        """切换到备站 (调用方已持有 _lock)"""
        old_host = state.config.primary_host
        new_host = state.config.backup_host
        state.active_role = LinkRole.BACKUP
        state.fail_count = 0
        state.primary_stable_count = 0
        state.last_switch_time = time.monotonic()
        state.total_failovers += 1
        logger.warning(
            "[redundancy] device=%s code=FAILOVER msg=%s -> %s (failover #%d)",
            device_id, old_host, new_host, state.total_failovers,
        )
        self._notify_switch(device_id, old_host, new_host)

    def _switch_to_primary_locked(self, device_id: str, state: _DeviceState) -> None:
        """回切到主站 (调用方已持有 _lock)"""
        old_host = state.config.backup_host
        new_host = state.config.primary_host
        state.active_role = LinkRole.PRIMARY
        state.fail_count = 0
        state.primary_stable_count = 0
        state.last_switch_time = time.monotonic()
        logger.info(
            "[redundancy] device=%s code=REVERT msg=%s -> %s (auto_revert)",
            device_id, old_host, new_host,
        )
        self._notify_switch(device_id, old_host, new_host)

    def _notify_switch(self, device_id: str, old_host: str, new_host: str) -> None:
        """通知驱动层链路切换 (不持有 _lock)"""
        if self._on_switch_callback is not None:
            try:
                self._on_switch_callback(device_id, old_host, new_host)
            except Exception as e:
                logger.warning("[redundancy] on_switch_callback failed: %s", e)
        # 发布事件到 EventBus (如有)
        if self._event_bus is not None:
            try:
                self._event_bus.publish("redundancy.switch", {
                    "device_id": device_id,
                    "old_host": old_host,
                    "new_host": new_host,
                })
            except Exception as e:
                logger.debug("[redundancy] event publish failed: %s", e)
