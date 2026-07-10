"""数据库备份调度器（SQLite 文件级备份 + 定时调度 + 保留期清理）。

FIXED: 重建丢失的 backup_scheduler 模块（app.py:97 / api/system.py:360,404 引用但文件不存在，
导致 create_app() 崩溃）[2026-06-30]

职责:
- 定时（默认每 24h）备份 data 目录下的 SQLite 数据库文件（主库 + 时序库 + 告警 outbox 等）
- 备份前对源库执行 WAL checkpoint（PRAGMA wal_checkpoint=TRUNCATE）确保数据落盘一致
- 原子写入：先写 .tmp 再 os.replace，避免半写备份文件
- 保留期清理：超过 retain_days 的备份自动删除
- 并发安全：备份期间持锁，避免与手动触发并发执行

不在本模块职责（由 system_services.ConfigBackupService 负责）:
- JSON 配置文件备份（应用层配置，非数据库）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认扫描的 SQLite 文件扩展名
_SQLITE_EXTS = (".db", ".sqlite", ".sqlite3")
# 备份文件时间戳格式
_TS_FMT = "%Y%m%d_%H%M%S"


@dataclass
class BackupResult:
    """单次备份结果。"""

    source: str  # 源文件相对路径
    backup_path: str  # 备份文件绝对路径
    size_bytes: int
    success: bool
    error: str = ""
    duration_ms: int = 0


@dataclass
class ScheduledBackupStatus:
    """调度器运行状态。"""

    enabled: bool = False
    interval_seconds: int = 86400
    retain_days: int = 7
    is_running: bool = False
    last_backup_time: str | None = None
    last_backup_duration_ms: int | None = None
    backup_count: int = 0
    total_backup_size_bytes: int = 0
    backups: list[dict[str, Any]] = field(default_factory=list)


class DatabaseBackupScheduler:
    """SQLite 数据库定时备份调度器。

    线程/协程安全: 备份执行持 asyncio.Lock，防止定时任务与手动触发并发。
    """

    def __init__(
        self,
        backup_dir: str = "data/backups",
        interval_seconds: int = 86400,
        retain_days: int = 7,
        enabled: bool = True,
        data_dir: str = "data",
    ) -> None:
        self._backup_dir = Path(backup_dir)
        self._interval = max(60, int(interval_seconds))
        self._retain_days = max(1, int(retain_days))
        self._enabled = bool(enabled)
        self._data_dir = Path(data_dir)
        self._status = ScheduledBackupStatus(
            enabled=self._enabled,
            interval_seconds=self._interval,
            retain_days=self._retain_days,
        )
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    # ── 生命周期 ──
    async def start(self) -> None:
        """启动定时备份任务。enabled=False 时仅初始化目录不启动调度。"""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        if not self._enabled:
            logger.info("DatabaseBackupScheduler disabled, only manual backups available")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="db-backup-scheduler")
        logger.info(
            "DatabaseBackupScheduler started: interval=%ds retain=%dd dir=%s",
            self._interval,
            self._retain_days,
            self._backup_dir,
        )

    async def stop(self) -> None:
        """优雅停止调度任务。"""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._task = None
        self._status.is_running = False

    # ── 调度循环 ──
    async def _run_loop(self) -> None:
        # 启动后延迟一个周期再首次执行，避免与启动竞争资源
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                    return  # stop 信号到达
                except TimeoutError:
                    pass  # 超时即触发一次备份
                await self.run_backup()
        except asyncio.CancelledError:
            logger.info("DatabaseBackupScheduler cancelled")

    # ── 备份执行 ──
    async def run_backup(self) -> list[BackupResult]:
        """立即执行一次备份，返回每个源库的备份结果。"""
        async with self._lock:
            self._status.is_running = True
            start_ts = time.monotonic()
            results: list[BackupResult] = []
            try:
                sources = self._discover_sqlite_files()
                for src in sources:
                    results.append(await self._backup_one(src))
                self._cleanup_expired()
                self._refresh_status(results, time.monotonic() - start_ts)
                logger.info(
                    "Database backup completed: %d files, %d succeeded",
                    len(results),
                    sum(1 for r in results if r.success),
                )
                return results
            finally:
                self._status.is_running = False

    async def _backup_one(self, src: Path) -> BackupResult:
        """备份单个 SQLite 文件（含 WAL checkpoint）。"""
        t0 = time.monotonic()
        rel = src.relative_to(self._data_dir.parent) if src.is_relative_to(self._data_dir.parent) else src
        ts = datetime.now().strftime(_TS_FMT)
        dest = self._backup_dir / f"{src.stem}_{ts}{src.suffix}"
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            # WAL checkpoint 确保数据落盘后再复制（仅对 .db 主文件）
            if src.suffix == ".db" and src.exists():
                try:
                    conn = sqlite3.connect(str(src), timeout=5)
                    try:
                        conn.execute("PRAGMA wal_checkpoint=TRUNCATE")
                    finally:
                        conn.close()
                except sqlite3.Error as e:
                    logger.debug("WAL checkpoint skipped for %s: %s", src, e)

            await asyncio.to_thread(shutil.copy2, str(src), str(tmp))
            os.replace(str(tmp), str(dest))  # 原子替换，防半写
            size = dest.stat().st_size
            return BackupResult(
                source=str(rel),
                backup_path=str(dest),
                size_bytes=size,
                success=True,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:  # noqa: BLE001 - 单文件失败不阻断其余备份
            logger.error("Backup failed for %s: %s", src, e)
            # 清理可能的残留 tmp 文件
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
            return BackupResult(
                source=str(rel),
                backup_path=str(dest),
                size_bytes=0,
                success=False,
                error=str(e),
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

    def _discover_sqlite_files(self) -> list[Path]:
        """扫描 data 目录下的 SQLite 文件（排除备份目录自身）。"""
        if not self._data_dir.exists():
            return []
        files: list[Path] = []
        for f in self._data_dir.rglob("*"):
            if not f.is_file():
                continue
            # 跳过备份目录自身，避免递归备份
            if self._backup_dir in f.parents or f.parent == self._backup_dir:
                continue
            if f.suffix.lower() in _SQLITE_EXTS:
                files.append(f)
        return files

    def _cleanup_expired(self) -> None:
        """清理超过保留期的备份文件。"""
        cutoff = time.time() - self._retain_days * 86400
        if not self._backup_dir.exists():
            return
        for f in self._backup_dir.iterdir():
            if not f.is_file():
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.debug("Expired backup removed: %s", f)
            except OSError as e:
                logger.warning("Failed to remove expired backup %s: %s", f, e)

    def _refresh_status(self, results: list[BackupResult], duration_s: float) -> None:
        self._status.last_backup_time = datetime.now().isoformat(timespec="seconds")
        self._status.last_backup_duration_ms = int(duration_s * 1000)
        self._status.backups = self.get_backup_list()
        self._status.backup_count = len(self._status.backups)
        self._status.total_backup_size_bytes = sum(b.get("size_bytes", 0) for b in self._status.backups)

    # ── 查询 ──
    @property
    def status(self) -> ScheduledBackupStatus:
        return self._status

    def get_backup_list(self) -> list[dict[str, Any]]:
        """返回当前备份目录中的备份文件清单（按时间倒序）。"""
        if not self._backup_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for f in self._backup_dir.iterdir():
            if not f.is_file() or f.suffix == ".tmp":
                continue
            st = f.stat()
            items.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "size_bytes": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                }
            )
        items.sort(key=lambda x: x["modified"], reverse=True)
        return items


class _contextlib_suppress:
    """轻量 suppress，避免在热路径 import contextlib。"""

    def __enter__(self) -> _contextlib_suppress:
        return self

    def __exit__(self, *exc: object) -> bool:
        return True


# ── 单例工厂 ──
_scheduler_instance: DatabaseBackupScheduler | None = None
# NOTE: 模块级 asyncio.Lock 在无运行事件循环时不可用，改用线程锁保护单例创建
import threading as _threading

_singleton_lock = _threading.Lock()


def get_backup_scheduler(
    backup_dir: str | None = None,
    interval_seconds: int | None = None,
    retain_days: int | None = None,
    enabled: bool | None = None,
) -> DatabaseBackupScheduler:
    """获取/创建备份调度器单例。

    首次调用（startup 时）传入完整参数创建实例；后续调用（API 层）无参返回已建实例。
    若未先创建过则用默认值惰性创建（保证 API 调用不报错）。
    """
    global _scheduler_instance
    if _scheduler_instance is not None:
        # 已创建则忽略参数（避免运行时改配置）
        return _scheduler_instance
    with _singleton_lock:
        if _scheduler_instance is None:
            _scheduler_instance = DatabaseBackupScheduler(
                backup_dir=backup_dir or "data/backups",
                interval_seconds=interval_seconds or 86400,
                retain_days=retain_days or 7,
                enabled=enabled if enabled is not None else True,
            )
    return _scheduler_instance
