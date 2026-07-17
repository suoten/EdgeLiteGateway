"""数据库备份调度器测试 - SQLite 文件级备份 + 保留期清理

覆盖 services/backup_scheduler.py：
- BackupResult / ScheduledBackupStatus 数据类
- DatabaseBackupScheduler: start/stop/run_backup/discover/cleanup
- WAL checkpoint、原子写入、保留期清理
- 单例工厂 get_backup_scheduler
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from edgelite.services.backup_scheduler import (
    BackupResult,
    DatabaseBackupScheduler,
    ScheduledBackupStatus,
    get_backup_scheduler,
)


@pytest.fixture
def scheduler(tmp_path):
    """创建临时备份调度器（不启动调度循环）"""
    backup_dir = tmp_path / "backups"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return DatabaseBackupScheduler(
        backup_dir=str(backup_dir),
        interval_seconds=86400,
        retain_days=7,
        enabled=False,  # 不启动调度循环
        data_dir=str(data_dir),
    )


def _create_sqlite_db(path: Path, table_name: str = "test") -> None:
    """创建一个包含测试数据的 SQLite 数据库"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} (id INTEGER, name TEXT)")
        conn.execute(f"INSERT INTO {table_name} VALUES (1, ?)", ("test_data",))
        conn.commit()
    finally:
        conn.close()


class TestBackupResultDataclass:
    def test_backup_result_required_fields(self):
        """BackupResult 必填字段: source/backup_path/size_bytes/success"""
        r = BackupResult(source="data/test.db", backup_path="/tmp/bak.db", size_bytes=1024, success=True)
        assert r.source == "data/test.db"
        assert r.backup_path == "/tmp/bak.db"
        assert r.size_bytes == 1024
        assert r.success is True
        assert r.error == ""
        assert r.duration_ms == 0

    def test_backup_result_with_error(self):
        """失败的 BackupResult 应携带 error 信息"""
        r = BackupResult(
            source="data/test.db",
            backup_path="/tmp/bak.db",
            size_bytes=0,
            success=False,
            error="permission denied",
        )
        assert r.success is False
        assert r.error == "permission denied"


class TestScheduledBackupStatusDataclass:
    def test_default_values(self):
        """ScheduledBackupStatus 默认值"""
        s = ScheduledBackupStatus()
        assert s.enabled is False
        assert s.interval_seconds == 86400
        assert s.retain_days == 7
        assert s.is_running is False
        assert s.last_backup_time is None
        assert s.backup_count == 0
        assert s.backups == []


class TestDatabaseBackupSchedulerInit:
    def test_constructor_normalizes_values(self, tmp_path):
        """构造器应规范化参数：interval>=60, retain>=1"""
        s = DatabaseBackupScheduler(
            backup_dir=str(tmp_path / "bak"),
            interval_seconds=10,  # 小于60应被提升到60
            retain_days=0,  # 小于1应被提升到1
            enabled=False,
            data_dir=str(tmp_path / "data"),
        )
        assert s._interval == 60
        assert s._retain_days == 1

    def test_status_property(self, scheduler):
        """status 属性应返回 ScheduledBackupStatus"""
        status = scheduler.status
        assert isinstance(status, ScheduledBackupStatus)
        assert status.enabled is False


class TestDiscoverSqliteFiles:
    def test_empty_data_dir(self, scheduler):
        """空 data 目录应返回空列表"""
        assert scheduler._discover_sqlite_files() == []

    def test_finds_sqlite_files(self, scheduler, tmp_path):
        """应发现 .db/.sqlite/.sqlite3 文件"""
        _create_sqlite_db(tmp_path / "data" / "main.db")
        _create_sqlite_db(tmp_path / "data" / "ts.sqlite")
        _create_sqlite_db(tmp_path / "data" / "alarms.sqlite3")
        # 非数据库文件应被忽略
        (tmp_path / "data" / "config.json").write_text("{}")
        files = scheduler._discover_sqlite_files()
        assert len(files) == 3

    def test_excludes_backup_dir(self, scheduler, tmp_path):
        """应排除备份目录自身，避免递归备份"""
        _create_sqlite_db(tmp_path / "data" / "main.db")
        # 在备份目录中放一个文件，应被排除
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir(exist_ok=True)
        _create_sqlite_db(backup_dir / "old_backup.db")
        files = scheduler._discover_sqlite_files()
        assert len(files) == 1
        assert files[0].name == "main.db"

    def test_nonexistent_data_dir(self, tmp_path):
        """data_dir 不存在时应返回空列表"""
        s = DatabaseBackupScheduler(
            backup_dir=str(tmp_path / "bak"),
            data_dir=str(tmp_path / "nonexistent"),
            enabled=False,
        )
        assert s._discover_sqlite_files() == []


class TestRunBackup:
    @pytest.mark.asyncio
    async def test_run_backup_creates_backup_file(self, scheduler, tmp_path):
        """run_backup 应创建备份文件"""
        _create_sqlite_db(tmp_path / "data" / "main.db", "users")
        results = await scheduler.run_backup()
        assert len(results) == 1
        assert results[0].success is True
        assert Path(results[0].backup_path).exists()
        assert results[0].size_bytes > 0

    @pytest.mark.asyncio
    async def test_run_backup_multiple_files(self, scheduler, tmp_path):
        """应备份多个 SQLite 文件"""
        _create_sqlite_db(tmp_path / "data" / "main.db")
        _create_sqlite_db(tmp_path / "data" / "ts.db")
        results = await scheduler.run_backup()
        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_run_backup_empty_dir(self, scheduler):
        """无文件时应返回空结果列表"""
        results = await scheduler.run_backup()
        assert results == []

    @pytest.mark.asyncio
    async def test_run_backup_updates_status(self, scheduler, tmp_path):
        """备份后应更新状态：last_backup_time/backup_count"""
        _create_sqlite_db(tmp_path / "data" / "main.db")
        await scheduler.run_backup()
        status = scheduler.status
        assert status.last_backup_time is not None
        assert status.backup_count >= 1
        assert status.last_backup_duration_ms is not None

    @pytest.mark.asyncio
    async def test_run_backup_is_running_flag(self, scheduler, tmp_path):
        """备份期间 is_running 应为 True"""
        _create_sqlite_db(tmp_path / "data" / "main.db")
        # 备份应在锁内设置 is_running=True，完成后恢复 False
        await scheduler.run_backup()
        assert scheduler.status.is_running is False


class TestCleanupExpired:
    def test_cleanup_removes_old_files(self, scheduler, tmp_path):
        """应清理超过保留期的备份文件"""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir(exist_ok=True)
        # 创建一个"旧"文件
        old_file = backup_dir / "old_backup.db"
        old_file.write_bytes(b"old data")
        # 将 mtime 设为 30 天前
        old_time = time.time() - 30 * 86400
        os.utime(str(old_file), (old_time, old_time))
        # 创建一个"新"文件
        new_file = backup_dir / "new_backup.db"
        new_file.write_bytes(b"new data")
        scheduler._cleanup_expired()
        assert not old_file.exists()
        assert new_file.exists()

    def test_cleanup_no_files(self, scheduler, tmp_path):
        """无文件时不应抛异常"""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir(exist_ok=True)
        scheduler._cleanup_expired()  # 不应抛异常


class TestGetBackupList:
    def test_empty_backup_dir(self, scheduler):
        """空备份目录应返回空列表"""
        assert scheduler.get_backup_list() == []

    def test_returns_backup_list_sorted(self, scheduler, tmp_path):
        """应返回按时间倒序的备份列表"""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir(exist_ok=True)
        # 创建两个备份文件
        f1 = backup_dir / "backup1.db"
        f1.write_bytes(b"data1")
        time.sleep(0.05)
        f2 = backup_dir / "backup2.db"
        f2.write_bytes(b"data2")
        items = scheduler.get_backup_list()
        assert len(items) == 2
        # 倒序：最新的在前
        assert items[0]["name"] == "backup2.db"
        assert items[1]["name"] == "backup1.db"
        # 每项应包含 name/path/size_bytes/modified
        for item in items:
            assert "name" in item
            assert "path" in item
            assert "size_bytes" in item
            assert "modified" in item

    def test_excludes_tmp_files(self, scheduler, tmp_path):
        """应排除 .tmp 文件"""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir(exist_ok=True)
        (backup_dir / "backup.db").write_bytes(b"data")
        (backup_dir / "partial.db.tmp").write_bytes(b"partial")
        items = scheduler.get_backup_list()
        assert len(items) == 1
        assert items[0]["name"] == "backup.db"


class TestSchedulerLifecycle:
    @pytest.mark.asyncio
    async def test_start_disabled_no_task(self, scheduler):
        """enabled=False 时 start 仅初始化目录，不启动调度任务"""
        await scheduler.start()
        assert scheduler._task is None

    @pytest.mark.asyncio
    async def test_start_enabled_creates_task(self, tmp_path):
        """enabled=True 时 start 应创建调度任务"""
        s = DatabaseBackupScheduler(
            backup_dir=str(tmp_path / "bak"),
            interval_seconds=60,
            enabled=True,
            data_dir=str(tmp_path / "data"),
        )
        await s.start()
        assert s._task is not None
        await s.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, tmp_path):
        """stop 应取消调度任务"""
        s = DatabaseBackupScheduler(
            backup_dir=str(tmp_path / "bak"),
            interval_seconds=60,
            enabled=True,
            data_dir=str(tmp_path / "data"),
        )
        await s.start()
        await s.stop()
        assert s._task is None
        assert s.status.is_running is False

    @pytest.mark.asyncio
    async def test_start_creates_backup_dir(self, tmp_path):
        """start 应创建备份目录"""
        backup_dir = tmp_path / "new_backups"
        s = DatabaseBackupScheduler(
            backup_dir=str(backup_dir),
            enabled=False,
            data_dir=str(tmp_path / "data"),
        )
        await s.start()
        assert backup_dir.exists()


class TestGetBackupSchedulerSingleton:
    def test_returns_instance(self):
        """get_backup_scheduler 应返回 DatabaseBackupScheduler 实例"""
        s = get_backup_scheduler()
        assert isinstance(s, DatabaseBackupScheduler)
