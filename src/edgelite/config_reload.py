"""配置热加载管理器 - 支持运行时动态更新配置"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import threading  # FIXED-P2: 全局单例竞态保护
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        self._callbacks_lock = (
            threading.Lock()
        )  # FIXED-P0: _change_callbacks并发修改保护（同步方法无法使用asyncio.Lock）
        self._change_callbacks: dict[str, list[Callable]] = {
            "device": [],
            "rule": [],
            "alarm": [],
            "driver": [],
            "system": [],
            "*": [],  # 所有变更
        }
        self._backup_dir = Path(self._config.backup_dir)
        # FIXED-P1: watch_file 创建的 task 统一管理集合，stop 时统一取消，防止孤儿 task
        self._watch_tasks: set[asyncio.Task] = set()
        # S-11: 配置解析失败退避与错误去重状态
        self._last_error_signature: str | None = None  # 上次错误特征（文件路径+异常类型+错误消息的哈希）
        self._consecutive_failures: int = 0  # 连续解析失败计数（用于指数退避）
        self._max_backoff_interval: float = 60.0  # 最大退避间隔（秒）

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
        # FIXED-P1: watch_file 创建的 task 统一管理集合，stop 时统一取消，防止孤儿 task
        with self._callbacks_lock:
            watch_tasks = list(self._watch_tasks)
            self._watch_tasks.clear()
        for t in watch_tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Config hot reloader stopped")

    async def _scan_config_files(self) -> None:
        """扫描配置文件"""
        # FIXED-P2: 原问题-硬编码configs/目录，无法通过环境变量自定义配置路径；
        # 改为从EDGELITE_CONFIG环境变量解析配置目录
        config_path = os.environ.get("EDGELITE_CONFIG", "configs/config.yaml")
        config_dir = Path(config_path).parent
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
            content_hash = hashlib.sha256(content.encode()).hexdigest()  # FIXED-P2: MD5→SHA256防碰撞
            # FIXED-P1: 原问题-修改共享字典 _watched_files 未获取 _callbacks_lock，
            # 而 _check_changes/unwatch_file/_on_config_changed 都在锁内操作，
            # 并发调用时可能导致字典迭代/修改竞态，回滚基准 _old_ 错乱，回滚功能失效
            # 修复：与其它访问点一致，在 _callbacks_lock 临界区内修改
            with self._callbacks_lock:
                self._watched_files[file_path] = content_hash
                # FIXED-P1: 原问题-_old_{file_path}从未赋值，导致rollback的old_value始终为None，回滚功能永久失效
                # 存储解析后的值（与_on_config_changed的new_value类型一致），供rollback使用
                try:
                    if file_path.endswith(".json"):
                        _parsed = json.loads(content)
                    else:
                        import yaml  # type: ignore[import-untyped]

                        _parsed = yaml.safe_load(content)
                    self._watched_files[f"_old_{file_path}"] = _parsed
                except Exception:
                    self._watched_files[f"_old_{file_path}"] = content
            logger.debug("Watching config file: %s", file_path)
        except Exception as e:
            logger.warning("Failed to watch file %s: %s", file_path, e)

    async def _watchdog_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                # S-11: 连续解析失败时使用指数退避间隔，避免重复检测导致日志洪泛
                await asyncio.sleep(self._compute_backoff_interval())
                await self._check_changes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Config watch loop error: %s", e)

    def _compute_backoff_interval(self) -> float:
        """S-11: 根据连续失败次数计算退避检测间隔

        指数退避：watch_interval * 2^failures，上限为 _max_backoff_interval（60s）
        示例（watch_interval=5s）：5s → 10s → 20s → 40s → 60s → 60s ...
        """
        if self._consecutive_failures == 0:
            return self._config.watch_interval
        backoff = self._config.watch_interval * (2**self._consecutive_failures)
        return min(backoff, self._max_backoff_interval)

    def _record_parse_failure(self, file_path: str, exc: Exception, level: int = logging.ERROR) -> None:
        """S-11: 记录解析失败 - 错误去重 + 累加退避计数

        相同错误特征（文件路径+异常类型+错误消息）只记录一次高级别日志，
        后续重复错误降级为 DEBUG，避免日志洪泛。

        Args:
            file_path: 配置文件路径
            exc: 捕获的异常
            level: 日志级别（默认 ERROR）
        """
        # 计算错误特征签名：文件路径 + 异常类型 + 错误消息
        sig_raw = f"{file_path}:{type(exc).__name__}:{exc}"
        signature = hashlib.sha256(sig_raw.encode()).hexdigest()

        # 累加连续失败计数（用于指数退避）
        self._consecutive_failures += 1

        # 相同错误只记录一次高级别日志，后续重复错误降级为 DEBUG
        if signature == self._last_error_signature:
            logger.debug(
                "Config check repeated failure (dedup): %s - %s: %s",
                file_path,
                type(exc).__name__,
                exc,
            )
        else:
            self._last_error_signature = signature
            logger.log(
                level,
                "Config check failed for %s: %s: %s (consecutive_failures=%d, next_interval=%.1fs)",
                file_path,
                type(exc).__name__,
                exc,
                self._consecutive_failures,
                self._compute_backoff_interval(),
            )

    async def _check_changes(self) -> None:
        """检查配置变更"""
        with self._callbacks_lock:  # FIXED-P2: _watched_files读取加锁，与watch_file/unwatch_file互斥
            watched_snapshot = list(self._watched_files.items())
        for file_path, old_hash in watched_snapshot:
            # FIXED-BugR4X: 原问题-endswith("_task")或startswith("_old_")过滤可能误过滤真实配置文件，
            # 修复-移除字符串模式过滤，改用Path.exists()验证file_path是否为真实文件路径；
            # 内部键（如_old_{file_path}）不是真实路径会被exists()过滤，避免误伤真实文件
            path = Path(file_path)
            if not path.exists():
                continue
            # 跳过非文件路径（如目录或内部辅助键对应的非文件条目）
            if not path.is_file():
                continue

            try:
                # FIXED(严重): 原问题-同步文件IO(path.read_text)阻塞事件循环;
                # 修复-用asyncio.to_thread包装同步文件读取
                content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            except FileNotFoundError as e:
                # S-11: 文件不存在 - WARNING（可能是正常卸载）
                self._record_parse_failure(file_path, e, level=logging.WARNING)
                continue
            except PermissionError as e:
                # S-11: 文件权限不足 - ERROR
                self._record_parse_failure(file_path, e, level=logging.ERROR)
                continue
            except Exception as e:
                # S-11: 其他读取错误 - WARNING
                self._record_parse_failure(file_path, e, level=logging.WARNING)
                continue

            try:
                new_hash = hashlib.sha256(content.encode()).hexdigest()  # FIXED-P2: MD5→SHA256防碰撞

                if new_hash != old_hash:
                    logger.info("Config file changed: %s", file_path)
                    # FIXED-P2: 原问题-解析失败仍更新哈希，导致下次不再重试；
                    # 改为解析失败时不更新哈希，下次循环会重新检测并重试
                    try:
                        new_value = await self._on_config_changed(file_path, content)
                        # FIXED-P1: hash 和 old 更新在同一锁临界区，防止并发读取到不一致状态
                        # 之前：hash 在这里更新，old 在 _on_config_changed 中更新，两者不在同一临界区
                        # 之后：_on_config_changed 返回 new_value，由这里统一在 _callbacks_lock 内原子更新 hash 和 old
                        with self._callbacks_lock:
                            self._watched_files[file_path] = new_hash
                            self._watched_files[f"_old_{file_path}"] = new_value
                        # S-11: 解析成功，重置退避计数器和错误去重状态
                        self._consecutive_failures = 0
                        self._last_error_signature = None
                    except Exception as e:
                        # S-11: 配置解析/应用失败 - ERROR（格式错误、reload失败等），去重记录并累加退避
                        self._record_parse_failure(file_path, e, level=logging.ERROR)
            except Exception as e:
                # S-11: 哈希计算等意外错误 - WARNING
                self._record_parse_failure(file_path, e, level=logging.WARNING)

    async def _on_config_changed(self, file_path: str, content: str) -> Any:
        """配置变更处理

        Returns:
            解析后的新配置值，供调用方在同一锁临界区更新 _old_{file_path}
        """
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
        except Exception:
            # S-11: 解析失败日志由 _check_changes 统一处理（含去重和退避），避免重复记录
            raise

        # FIXED-P0: 原问题-检测到变更但从不reload AppConfig，只通知回调；
        # 解析成功后对system类型配置实际调用reload_config刷新AppConfig
        if config_type == "system":
            # FIXED-BugR1: 仅对主配置文件调用 reload_config，避免非主配置文件
            # （如 logging.yaml/notify.yaml）触发 reload_config 后用默认值覆盖当前运行配置
            main_config_path = os.environ.get("EDGELITE_CONFIG", "configs/config.yaml")
            try:
                is_main = Path(file_path).resolve() == Path(main_config_path).resolve()
            except Exception:
                is_main = file_path == main_config_path
            if is_main:
                # FIXED-P1: reload_config 失败时 raise，让 _check_changes 捕获，不更新哈希，下次循环重试
                from edgelite.config import reload_config

                # FIXED-P1: reload_config 内部执行同步 open()+yaml.safe_load+sqlite3.connect+解密操作，
                # 在 async 上下文直接调用会阻塞事件循环。参考 _check_changes 中已用 asyncio.to_thread 读取文件，
                # 这里将同步 reload_config 调用包装为 asyncio.to_thread 放到线程池执行。
                _, changed_keys = await asyncio.to_thread(reload_config, file_path)
                logger.info("AppConfig reloaded from %s, changed=%s", file_path, changed_keys)
            else:
                logger.debug(
                    "Non-main system config changed, only notifying callbacks: %s",
                    file_path,
                )

        # 创建变更记录
        with self._callbacks_lock:
            old_value = self._watched_files.get(f"_old_{file_path}")
        change = ConfigChange(
            config_type=config_type,
            config_id=file_path,
            old_value=old_value,
            new_value=new_value,
            changed_at=datetime.now(UTC),
        )
        await self._record_change(change)

        # FIXED-P1: old 更新移至 _check_changes 的锁临界区，与 hash 更新原子执行
        # 此处不再更新 _old_{file_path}，由 _check_changes 统一更新

        # 触发回调
        # FIXED-BugR6: reload_config 成功后的记录与通知步骤失败不应导致下一轮重复 reload。
        # 将这些步骤独立 try/except，确保异常不影响 _check_changes 的哈希更新。
        try:
            await self._notify_callbacks(config_type, change)
        except Exception as e:
            logger.warning("Failed to notify config change callbacks (non-fatal): %s", e)

        return new_value

    async def _backup_config(self, file_path: str, content: str) -> None:
        """备份配置"""
        if not self._config.auto_backup:
            return
        try:
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            backup_name = f"{Path(file_path).stem}_{timestamp}{Path(file_path).suffix}"
            backup_path = self._backup_dir / backup_name
            tmp_path = backup_path.with_suffix(
                backup_path.suffix + ".tmp"
            )  # FIXED-P1: 原问题-直接write_text目标文件，写入中断导致半写YAML损坏；改为原子替换：先写tmp→fsync→rename

            # FIXED(严重): 原问题-同步文件IO(open/write/fsync/replace)阻塞事件循环;
            # 修复-用asyncio.to_thread包装同步文件写入操作
            def _do_backup():
                self._backup_dir.mkdir(parents=True, exist_ok=True)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())  # FIXED-P2: 原问题-tmp_path.open("r")泄漏fd；改为with语句内fsync同一fd
                tmp_path.replace(backup_path)

            await asyncio.to_thread(_do_backup)
            logger.debug("Config backed up: %s", backup_path)

            # 清理旧备份
            await self._cleanup_old_backups(Path(file_path).stem)
        except Exception as e:
            logger.warning("Backup failed for %s: %s", file_path, e)

    async def _cleanup_old_backups(self, stem: str) -> None:
        """清理旧备份"""
        try:
            # FIXED-P1: 原问题-硬编码".yaml"通配符，JSON配置备份文件无法被匹配导致无限堆积磁盘占满
            # 改为匹配所有扩展名
            pattern = f"{stem}_*"
            backups = sorted(
                [p for p in self._backup_dir.glob(pattern) if p.is_file()],
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
        # FIXED-P0: 锁内复制回调列表快照，防止遍历期间列表被修改
        with self._callbacks_lock:
            callbacks_snapshot = {}
            for key in [config_type, "*"]:
                if key in self._change_callbacks:
                    callbacks_snapshot[key] = list(self._change_callbacks[key])

        # 先触发特定类型的回调
        for callback in callbacks_snapshot.get(config_type, []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(change)
                else:
                    callback(change)
            except Exception as e:
                logger.warning("Change callback error: %s", e)

        # 再触发通配符回调
        for callback in callbacks_snapshot.get("*", []):
            try:
                if asyncio.iscoroutinefunction(callback):
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

    def register_callback(self, config_type: str, callback: Callable[[ConfigChange], None]) -> None:
        """注册配置变更回调

        Args:
            config_type: 配置类型 (device/rule/alarm/driver/system/*)
            callback: 回调函数
        """
        # FIXED-P0: _change_callbacks修改需加锁，防止与_notify_callbacks遍历竞态
        with self._callbacks_lock:
            if config_type not in self._change_callbacks:
                self._change_callbacks[config_type] = []
            self._change_callbacks[config_type].append(callback)
        logger.debug("Registered callback for config type: %s", config_type)

    def unregister_callback(self, config_type: str, callback: Callable[[ConfigChange], None]) -> None:
        """取消注册回调"""
        # FIXED-P0: _change_callbacks修改需加锁，防止与_notify_callbacks遍历竞态
        with self._callbacks_lock:
            if config_type in self._change_callbacks:
                with contextlib.suppress(ValueError):
                    self._change_callbacks[config_type].remove(callback)

    async def reload_device_config(self, device_id: str, new_config: dict) -> ConfigChange:
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

    async def reload_rule_config(self, rule_id: str, new_config: dict) -> ConfigChange:
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

    async def get_change_history(  # FIXED-P1: 改为async方法，统一使用_lock保护_change_history，消除双锁竞态
        self, config_type: str | None = None, limit: int = 50
    ) -> list[dict]:
        """获取配置变更历史

        Args:
            config_type: 过滤配置类型
            limit: 返回数量限制

        Returns:
            变更历史列表
        """
        async with self._lock:  # FIXED-P1: 统一使用_lock，与_record_change同一把锁
            history = list(self._change_history)
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
        # FIXED-P2: 锁内创建_change_history快照，防止与_record_change并发
        async with self._lock:
            history_snapshot = list(self._change_history)
        # 查找变更记录
        for i in range(len(history_snapshot) - 1, -1, -1):
            change = history_snapshot[i]
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
        # FIXED-P1: watch_file 创建的 task 加入 _watch_tasks 集合统一管理，stop 时统一取消
        # 之前：asyncio.create_task 返回的 task 未保存，stop 时无法取消，导致孤儿 task
        # 之后：task 加入 _watch_tasks 集合，完成后自动移除，stop 时统一取消
        if not self._running:
            logger.warning("Cannot watch file when reloader is not running: %s", file_path)
            return
        task = asyncio.create_task(self._watch_file(file_path))
        self._watch_tasks.add(task)
        task.add_done_callback(self._watch_tasks.discard)

    def unwatch_file(self, file_path: str) -> None:
        """取消监控文件"""
        with self._callbacks_lock:  # FIXED-P2: _watched_files修改加锁，防止与_check_changes迭代竞态
            # R11-DRV-10: 删除无效的 "_task" 后缀 pop（_watched_files 中从未以 "_task" 后缀作为 key）
            self._watched_files.pop(file_path, None)
            self._watched_files.pop(f"_old_{file_path}", None)


# 全局热加载器实例
_config_hot_reloader: ConfigHotReloader | None = None


def get_config_hot_reloader() -> ConfigHotReloader:
    """R11-DRV-01: 获取全局配置热加载器单例（懒初始化）

    修复前：该工厂函数完全缺失，bootstrap.py 执行 from edgelite.config_reload import
    get_config_hot_reloader 会抛 ImportError 导致启动崩溃。
    """
    global _config_hot_reloader
    if _config_hot_reloader is None:
        _config_hot_reloader = ConfigHotReloader()
    return _config_hot_reloader
