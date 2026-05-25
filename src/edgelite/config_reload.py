"""配置热加载管理器 - 支持运行时动态更新配置"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class ConfigChange:
    """配置变更记录"""

    config_type: str  # device/rule/alarm/driver/system
    config_id: str  # 配置ID
    old_value: Any
    new_value: Any
    changed_by: str = "system"
    changed_at: datetime | None = None
    change_reason: str = ""


@dataclass
class HotReloadConfig:
    """热加载配置"""

    enabled: bool = True
    watch_interval: float = 5.0  # 文件检查间隔（秒）
    max_history: int = 100  # 最大历史记录数
    auto_backup: bool = True  # 自动备份
    backup_dir: str = "data/config_backups"  # 备份目录


class ConfigHotReloader:
    """配置热加载管理器

    支持热加载：
    - 驱动配置
    - 设备配置
    - 规则配置
    - 告警配置
    - 系统配置

    特性：
    - 变更检测：监控配置文件变化
    - 原子更新：确保配置更新原子性
    - 回滚支持：支持配置回滚
    - 变更历史：记录所有配置变更
    - 回调通知：变更后触发回调
    """

    def __init__(self, config: HotReloadConfig | None = None):
        self._config = config or HotReloadConfig()
        self._running = False
        self._task: asyncio.Task | None = None
        self._watched_files: dict[str, str] = {}  # path -> content_hash
        self._change_history: list[ConfigChange] = []
        self._lock = asyncio.Lock()
        self._change_callbacks: dict[str, list[Callable]] = {
            "device": [],
            "rule": [],
            "alarm": [],
            "driver": [],
            "system": [],
            "*": [],  # 所有变更
        }
        self._backup_dir = Path(self._config.backup_dir)

    async def start(self) -> None:
        """启动热加载监控"""
        if self._running:
            return

        self._running = True
        if self._config.auto_backup:
            self._backup_dir.mkdir(parents=True, exist_ok=True)

        # 扫描现有配置文件
        await self._scan_config_files()

        # 启动监控任务
        self._task = asyncio.create_task(self._watchdog_loop())
        logger.info(
            "Config hot reloader started (interval=%.1fs)",
            self._config.watch_interval,
        )

    async def stop(self) -> None:
        """停止热加载监控"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Config hot reloader stopped")

    async def _scan_config_files(self) -> None:
        """扫描配置文件"""
        config_dir = Path("configs")
        if not config_dir.exists():
            return

        for config_file in config_dir.glob("*.yaml"):
            await self._watch_file(str(config_file))
        for config_file in config_dir.glob("*.json"):
            await self._watch_file(str(config_file))

    async def _watch_file(self, file_path: str) -> None:
        """监控单个配置文件"""
        path = Path(file_path)
        if not path.exists():
            return

        try:
            content = path.read_text(encoding="utf-8")
            content_hash = hashlib.md5(content.encode()).hexdigest()
            self._watched_files[file_path] = content_hash
            logger.debug("Watching config file: %s", file_path)
        except Exception as e:
            logger.warning("Failed to watch file %s: %s", file_path, e)

    async def _watchdog_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                await asyncio.sleep(self._config.watch_interval)
                await self._check_changes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Config watch loop error: %s", e)

    async def _check_changes(self) -> None:
        """检查配置变更"""
        for file_path, old_hash in list(self._watched_files.items()):
            path = Path(file_path)
            if not path.exists():
                continue

            try:
                content = path.read_text(encoding="utf-8")
                new_hash = hashlib.md5(content.encode()).hexdigest()

                if new_hash != old_hash:
                    logger.info("Config file changed: %s", file_path)
                    await self._on_config_changed(file_path, content)
                    self._watched_files[file_path] = new_hash

            except Exception as e:
                logger.warning("Failed to check config %s: %s", file_path, e)

    async def _on_config_changed(self, file_path: str, content: str) -> None:
        """配置变更处理"""
        # 解析配置类型
        config_type = self._detect_config_type(file_path)

        # 备份旧配置
        if self._config.auto_backup:
            await self._backup_config(file_path, content)

        # 解析配置内容
        try:
            if file_path.endswith(".json"):
                new_value = json.loads(content)
            else:
                import yaml

                new_value = yaml.safe_load(content)
        except Exception as e:
            logger.error("Failed to parse config %s: %s", file_path, e)
            return

        # 创建变更记录
        change = ConfigChange(
            config_type=config_type,
            config_id=file_path,
            old_value=self._watched_files.get(f"_old_{file_path}"),
            new_value=new_value,
            changed_at=datetime.now(UTC),
        )
        await self._record_change(change)

        # 触发回调
        await self._notify_callbacks(config_type, change)

    async def _backup_config(self, file_path: str, content: str) -> None:
        """备份配置"""
        try:
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            backup_name = f"{Path(file_path).stem}_{timestamp}{Path(file_path).suffix}"
            backup_path = self._backup_dir / backup_name
            backup_path.write_text(content, encoding="utf-8")
            logger.debug("Config backed up: %s", backup_path)

            # 清理旧备份
            await self._cleanup_old_backups(Path(file_path).stem)
        except Exception as e:
            logger.warning("Backup failed for %s: %s", file_path, e)

    async def _cleanup_old_backups(self, stem: str) -> None:
        """清理旧备份"""
        try:
            pattern = f"{stem}_*.yaml"
            backups = sorted(
                self._backup_dir.glob(pattern),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            # 只保留最近的 10 个
            for old_backup in backups[10:]:
                old_backup.unlink()
        except Exception as e:
            logger.debug("Backup cleanup error: %s", e)

    async def _record_change(self, change: ConfigChange) -> None:
        """记录变更"""
        async with self._lock:
            self._change_history.append(change)
            # 限制历史记录数量
            if len(self._change_history) > self._config.max_history:
                self._change_history = self._change_history[-self._config.max_history :]

    async def _notify_callbacks(self, config_type: str, change: ConfigChange) -> None:
        """通知回调"""
        # 先触发特定类型的回调
        callbacks = self._change_callbacks.get(config_type, [])
        for callback in callbacks:
            try:
                if asyncio.iscoroutine_function(callback):
                    await callback(change)
                else:
                    callback(change)
            except Exception as e:
                logger.warning("Change callback error: %s", e)

        # 再触发通配符回调
        for callback in self._change_callbacks.get("*", []):
            try:
                if asyncio.iscoroutine_function(callback):
                    await callback(change)
                else:
                    callback(change)
            except Exception as e:
                logger.warning("Global change callback error: %s", e)

    @staticmethod
    def _detect_config_type(file_path: str) -> str:
        """检测配置类型"""
        filename = Path(file_path).stem.lower()
        if "device" in filename:
            return "device"
        elif "rule" in filename:
            return "rule"
        elif "alarm" in filename:
            return "alarm"
        elif "driver" in filename:
            return "driver"
        else:
            return "system"

    def register_callback(
        self, config_type: str, callback: Callable[[ConfigChange], None]
    ) -> None:
        """注册配置变更回调

        Args:
            config_type: 配置类型 (device/rule/alarm/driver/system/*)
            callback: 回调函数
        """
        if config_type not in self._change_callbacks:
            self._change_callbacks[config_type] = []
        self._change_callbacks[config_type].append(callback)
        logger.debug("Registered callback for config type: %s", config_type)

    def unregister_callback(
        self, config_type: str, callback: Callable[[ConfigChange], None]
    ) -> None:
        """取消注册回调"""
        if config_type in self._change_callbacks:
            try:
                self._change_callbacks[config_type].remove(callback)
            except ValueError:
                pass

    async def reload_device_config(
        self, device_id: str, new_config: dict
    ) -> ConfigChange:
        """手动触发设备配置热更新

        Args:
            device_id: 设备ID
            new_config: 新的配置

        Returns:
            ConfigChange 变更记录
        """
        change = ConfigChange(
            config_type="device",
            config_id=device_id,
            old_value=None,
            new_value=new_config,
            changed_by="manual",
            changed_at=datetime.now(UTC),
            change_reason="Manual reload",
        )
        await self._record_change(change)
        await self._notify_callbacks("device", change)
        logger.info("Device config hot reloaded: %s", device_id)
        return change

    async def reload_rule_config(
        self, rule_id: str, new_config: dict
    ) -> ConfigChange:
        """手动触发规则配置热更新"""
        change = ConfigChange(
            config_type="rule",
            config_id=rule_id,
            old_value=None,
            new_value=new_config,
            changed_by="manual",
            changed_at=datetime.now(UTC),
            change_reason="Manual reload",
        )
        await self._record_change(change)
        await self._notify_callbacks("rule", change)
        logger.info("Rule config hot reloaded: %s", rule_id)
        return change

    def get_change_history(
        self, config_type: str | None = None, limit: int = 50
    ) -> list[dict]:
        """获取配置变更历史

        Args:
            config_type: 过滤配置类型
            limit: 返回数量限制

        Returns:
            变更历史列表
        """
        history = self._change_history
        if config_type:
            history = [h for h in history if h.config_type == config_type]

        return [
            {
                "config_type": h.config_type,
                "config_id": h.config_id,
                "changed_by": h.changed_by,
                "changed_at": h.changed_at.isoformat() if h.changed_at else None,
                "change_reason": h.change_reason,
            }
            for h in history[-limit:]
        ]

    async def rollback(self, config_type: str, config_id: str) -> bool:
        """回滚配置到前一个版本

        Args:
            config_type: 配置类型
            config_id: 配置ID

        Returns:
            True 表示回滚成功
        """
        # 查找变更记录
        for i in range(len(self._change_history) - 1, -1, -1):
            change = self._change_history[i]
            if change.config_type == config_type and change.config_id == config_id:
                if change.old_value is not None:
                    # 触发回滚
                    rollback_change = ConfigChange(
                        config_type=config_type,
                        config_id=config_id,
                        old_value=change.new_value,
                        new_value=change.old_value,
                        changed_by="rollback",
                        changed_at=datetime.now(UTC),
                        change_reason=f"Rollback to version at {change.changed_at}",
                    )
                    await self._record_change(rollback_change)
                    await self._notify_callbacks(config_type, rollback_change)
                    logger.info(
                        "Config rolled back: %s/%s",
                        config_type,
                        config_id,
                    )
                    return True

        logger.warning("No previous version found for rollback: %s/%s", config_type, config_id)
        return False

    def watch_file(self, file_path: str) -> None:
        """手动添加监控文件"""
        asyncio.create_task(self._watch_file(file_path))

    def unwatch_file(self, file_path: str) -> None:
        """取消监控文件"""
        self._watched_files.pop(file_path, None)
        self._watched_files.pop(f"_old_{file_path}", None)


# 全局热加载器实例
_config_hot_reloader: ConfigHotReloader | None = None


def get_config_hot_reloader() -> ConfigHotReloader:
    """获取全局配置热加载器"""
    global _config_hot_reloader
    if _config_hot_reloader is None:
        _config_hot_reloader = ConfigHotReloader()
    return _config_hot_reloader
