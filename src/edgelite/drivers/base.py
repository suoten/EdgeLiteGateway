"""驱动插件抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any


@dataclass
class DriverHealthStats:
    """驱动健康状态统计"""

    device_id: str = ""
    total_reads: int = 0
    failed_reads: int = 0
    total_writes: int = 0
    failed_writes: int = 0
    last_success_read: datetime | None = None
    last_failed_read: datetime | None = None
    consecutive_failures: int = 0
    total_downtime_seconds: float = 0.0
    last_online_at: datetime | None = None
    last_offline_at: datetime | None = None
    connection_quality_score: float = 100.0  # 0-100, 100=最佳

    @property
    def read_error_rate(self) -> float:
        """读取错误率"""
        if self.total_reads == 0:
            return 0.0
        return self.failed_reads / self.total_reads

    @property
    def is_healthy(self) -> bool:
        """驱动是否健康（连续失败<5次且错误率<10%）"""
        return self.consecutive_failures < 5 and self.read_error_rate < 0.1


class DriverPlugin(ABC):
    """协议驱动插件基类"""

    plugin_name: str = ""
    plugin_version: str = "0.1.0"
    supported_protocols: list[str] = []
    config_schema: dict = {}

    def __init__(self) -> None:
        self._running: bool = False  # FIXED-P2: 基类初始化_running和_data_callback，避免子类访问未定义属性抛AttributeError
        self._data_callback: Callable | None = None
        self._health_stats: dict[str, DriverHealthStats] = {}
        self._offline_since: dict[str, datetime] = {}

    @abstractmethod
    async def start(self, config: dict) -> None:
        """启动驱动"""

    @abstractmethod
    async def stop(self) -> None:
        """停止驱动"""

    @abstractmethod
    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值，返回 {point_name: value}"""

    @abstractmethod
    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""

    async def write_points_batch(
        self, device_id: str, points: dict[str, Any]
    ) -> dict[str, bool]:
        """批量写入多个测点（可选实现）。

        默认实现：逐点调用 write_point()，子类可覆盖优化。

        Args:
            device_id: 设备ID
            points: 测点名到值的字典 {point_name: value}

        Returns:
            {point_name: success} - 每个测点的写入结果
        """
        results = {}
        for point_name, value in points.items():
            try:
                results[point_name] = await self.write_point(device_id, point_name, value)
            except Exception:
                results[point_name] = False
        return results

    async def discover_devices(self, config: dict) -> list[dict]:
        """发现设备（可选实现）"""
        return []

    # FIXED: 原问题-add_device使用NotImplementedError而非@abstractmethod，子类未实现时不在实例化阶段报错
    # add_device 保持为可选方法（非 abstractmethod），但改用更明确的文档说明
    async def add_device(
        self, device_id: str, config: dict, points: list[dict] | None = None
    ) -> None:
        """添加设备到驱动实例（可选实现）。未实现时抛出 NotImplementedError。"""
        raise NotImplementedError(f"{self.__class__.__name__} does not implement add_device")

    def is_device_connected(self, device_id: str) -> bool:
        """检查设备是否已连接（可选实现）。
        默认返回 True：驱动 start() 成功即视为设备在线，
        子类可覆写此方法实现精确的连通性检测。
        """
        return True

    def on_data(self, callback: Callable) -> None:
        """注册数据回调（可选，用于推送型协议如MQTT）。子类如需支持推送，应覆盖此方法保存callback。"""
        self._data_callback = callback

    @property
    def is_running(self) -> bool:
        """驱动是否运行中"""
        return getattr(self, "_running", False)

    # ─── 心跳检测与健康状态 ───

    def get_health_stats(self, device_id: str) -> DriverHealthStats | None:
        """获取设备健康状态统计"""
        return self._health_stats.get(device_id)

    def get_all_health_stats(self) -> dict[str, DriverHealthStats]:
        """获取所有设备的健康状态统计"""
        return dict(self._health_stats)

    def reset_health_stats(self, device_id: str) -> None:
        """重置设备健康统计"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)

    def _record_read_success(self, device_id: str) -> None:
        """记录读取成功"""
        stats = self._health_stats.setdefault(device_id, DriverHealthStats(device_id=device_id))
        stats.total_reads += 1
        stats.last_success_read = datetime.now(UTC)
        stats.consecutive_failures = 0
        # 恢复在线状态
        if device_id in self._offline_since:
            offline_duration = (datetime.now(UTC) - self._offline_since.pop(device_id)).total_seconds()
            stats.total_downtime_seconds += offline_duration
            stats.last_offline_at = None
        self._update_connection_quality(stats)

    def _record_read_failure(self, device_id: str) -> None:
        """记录读取失败"""
        stats = self._health_stats.setdefault(device_id, DriverHealthStats(device_id=device_id))
        stats.total_reads += 1
        stats.failed_reads += 1
        stats.last_failed_read = datetime.now(UTC)
        stats.consecutive_failures += 1
        # 记录离线开始时间
        if device_id not in self._offline_since:
            self._offline_since[device_id] = datetime.now(UTC)
            stats.last_offline_at = datetime.now(UTC)
        self._update_connection_quality(stats)

    def _record_write_success(self, device_id: str) -> None:
        """记录写入成功"""
        stats = self._health_stats.setdefault(device_id, DriverHealthStats(device_id=device_id))
        stats.total_writes += 1

    def _record_write_failure(self, device_id: str) -> None:
        """记录写入失败"""
        stats = self._health_stats.setdefault(device_id, DriverHealthStats(device_id=device_id))
        stats.total_writes += 1
        stats.failed_writes += 1

    def _update_connection_quality(self, stats: DriverHealthStats) -> None:
        """更新连接质量评分

        评分算法：
        - 基础分: 100
        - 每次连续失败: -10
        - 错误率每增加1%: -2
        - 最低0分
        """
        score = 100.0
        score -= min(stats.consecutive_failures * 10, 50)  # 连续失败最多扣50分
        score -= min(stats.read_error_rate * 200, 50)  # 错误率最多扣50分
        stats.connection_quality_score = max(0.0, score)

    def get_connection_quality(self, device_id: str) -> float:
        """获取设备连接质量评分 (0-100)"""
        stats = self._health_stats.get(device_id)
        return stats.connection_quality_score if stats else 100.0

    async def health_check(self, device_id: str) -> bool:
        """执行设备健康检查（可选实现）。

        子类可覆盖此方法实现自定义的健康检查逻辑，
        例如发送心跳包、读取测试寄存器等。

        默认实现：检查 is_device_connected() 状态。

        Returns:
            True 表示设备健康，False 表示设备异常
        """
        return self.is_device_connected(device_id)
