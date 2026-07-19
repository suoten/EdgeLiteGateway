"""InfluxDB时序存储

性能修复：所有influxdb-client同步API通过asyncio.to_thread调用，
避免阻塞事件循环。influxdb-client的write_api/query_api均为同步API，
直接在async函数中调用会阻塞整个事件循环。

降级方案：InfluxDB不可用时自动降级到SQLite时序存储，
InfluxDB恢复后SQLite中的数据增量同步回InfluxDB。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from influxdb_client.client.influxdb_client import InfluxDBClient
from influxdb_client.client.write.point import Point
from influxdb_client.client.write_api import SYNCHRONOUS, WriteOptions

from edgelite.config import get_config
from edgelite.constants import _INFLUX_CONNECT_TIMEOUT_MS, _INFLUX_WRITE_TIMEOUT_S
from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

logger = logging.getLogger(__name__)


class InfluxDBStorage:
    """InfluxDB时序数据存储，支持SQLite降级"""

    def __init__(self):
        config = get_config()
        self._url = config.influxdb.url
        self._token = config.influxdb.token
        self._org = config.influxdb.org
        self._bucket = config.influxdb.bucket
        self._retention_days = config.influxdb.retention_days
        self._client: InfluxDBClient | None = None
        self._write_api = None
        # R5-G-12: 同步写入专用 write_api，在 connect() 中创建一次复用，避免 _sync_batch 每次创建新实例
        self._sync_write_api = None
        self._query_api = None
        self._buckets_api = None
        self._available = False
        self._fail_count = 0
        # SQLite降级存储
        self._sqlite_ts: SqliteTimeSeriesStorage | None = None
        self._sync_task: asyncio.Task | None = None
        self._sync_running = False
        # R11-DRV-08: 复用网络探测 HTTP client，避免每次 check_network_status 新建连接
        self._probe_client: Any = None
        # 降级状态跟踪
        self._fallback_mode = False
        # R7-S-02: 记录上次 cleanup_expired_data 执行时间，用于在 _sync_loop 中低频调用
        self._last_cleanup_time: float = 0.0
        # FIXED-P1: 记录已上传但sync_completed失败的max_id，防止重复上传
        self._last_uploaded_max_id = 0
        # 降级统计
        self._stats_fallback_count = 0  # 降级次数
        self._stats_cached_count = 0  # 缓存数据量（写入SQLite的总条数）
        self._stats_sync_success = 0  # 同步成功数
        self._stats_sync_fail = 0  # 同步失败数
        self._emergency_buffer: deque = deque(maxlen=10000)
        self._emergency_db_path: str = os.path.join(os.environ.get("EDGELITE_DATA_DIR", "data"), "emergency_buffer.db")
        self._emergency_db: sqlite3.Connection | None = None
        # FIXED-EMERGENCY-RETRY: Metrics counter for monitoring SQLite write failures
        self._emergency_sqlite_failures: int = 0
        # FIXED-EMERGENCY-RACE: Two separate locks to avoid deadlock.
        # Lock acquisition order (fixed to prevent circular deadlock):
        #   1. _emergency_buffer_lock  (must be acquired first)
        #   2. _emergency_db_lock      (acquired after buffer lock)
        self._emergency_buffer_lock = asyncio.Lock()
        self._emergency_db_lock = threading.Lock()
        self._state_lock = asyncio.Lock()  # FIXED-P2: _available/_fallback_mode并发保护
        # R5-G-04: 同步互斥锁，防止force_sync与_sync_loop并发调用_sync_batch导致重复写入
        self._sync_lock = asyncio.Lock()
        self._init_emergency_db()
        # EventBus引用（延迟设置）
        self._event_bus = None

    def _init_emergency_db(self) -> None:  # FIXED-P2: 紧急缓冲添加SQLite持久化
        try:
            os.makedirs(os.path.dirname(self._emergency_db_path), exist_ok=True)
            self._emergency_db = sqlite3.connect(
                self._emergency_db_path, check_same_thread=False
            )  # FIXED-P1: 原问题-未设check_same_thread=False，asyncio.to_thread工作线程中抛ProgrammingError
            self._emergency_db.execute("PRAGMA journal_mode=WAL")
            # R8-S-DB1 修复(严重): 补全 PRAGMA 配置，与主库保持一致，
            # 避免高并发下 "database is locked" 和写入性能问题
            self._emergency_db.execute("PRAGMA busy_timeout=5000")
            self._emergency_db.execute("PRAGMA synchronous=NORMAL")
            # FIXED-P0: 原问题-紧急缓冲DB无完整性检查，损坏文件导致后续写入全部失败
            integrity = self._emergency_db.execute("PRAGMA integrity_check").fetchone()
            if integrity and integrity[0] != "ok":
                logger.warning("Emergency DB integrity check failed: %s, rebuilding", integrity[0])
                self._emergency_db.close()
                corrupt_backup = f"{self._emergency_db_path}.corrupt.{int(time.time())}"
                try:
                    import shutil

                    shutil.move(self._emergency_db_path, corrupt_backup)
                    logger.warning("Corrupt emergency DB moved to: %s", corrupt_backup)
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("备份损坏紧急缓冲DB失败: %s", e)
                self._emergency_db = sqlite3.connect(self._emergency_db_path, check_same_thread=False)
                self._emergency_db.execute("PRAGMA journal_mode=WAL")
            # R8-S-DB1 修复(严重): 补全 PRAGMA 配置，与主库保持一致，
            # 避免高并发下 "database is locked" 和写入性能问题
            self._emergency_db.execute("PRAGMA busy_timeout=5000")
            self._emergency_db.execute("PRAGMA synchronous=NORMAL")
            self._emergency_db.execute("""
                CREATE TABLE IF NOT EXISTS emergency_buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT NOT NULL,
                    created_at REAL NOT NULL
            )
        """)
            self._emergency_db.commit()
        except Exception as e:  # FIXED-P2: 数据库路径不可写时不崩溃，回退到纯内存模式
            logger.warning("Emergency DB init failed (%s), falling back to memory-only buffer", e)
            self._emergency_db = None

    # ------------------------------------------------------------------
    # FIXED-EMERGENCY-RACE: Buffer operations must be protected by
    # _emergency_buffer_lock.  All reads/writes of the deque go through these
    # helpers to prevent data races under concurrent coroutines.
    # Lock order when both locks are needed:
    #   1. async with _emergency_buffer_lock
    #   2. with _emergency_db_lock
    # ------------------------------------------------------------------

    async def _buffer_append(self, item: dict) -> None:
        """Append one item to the emergency buffer (lock-guarded).

        When the deque is full, the oldest item is silently dropped — this is
        preserved from the original behaviour.  The corresponding SQLite
        overflow is trimmed in _buffer_append_with_db().
        """
        async with self._emergency_buffer_lock:
            self._emergency_buffer.append(item)

    async def _buffer_append_with_db(self, item: dict) -> None:
        """Append to buffer then persist to SQLite (buffer lock first, then DB lock).

        FIXED-EMERGENCY-RETRY: Added retry logic for SQLite write failures.
        FIXED-P1: 原问题-SQLite写入在buffer lock外，失败时deque有数据但SQLite无，重启后丢失；
        改为SQLite写入在buffer lock内，失败时回滚deque append保持同步。

        Lock order: _emergency_buffer_lock → _emergency_db_lock
        """
        async with self._emergency_buffer_lock:
            # FIXED-P0: 原问题-len(deque)>maxlen永远为False(deque maxlen自动淘汰)，overflow永远为0。
            # 改为append前检查是否已满，满时append会淘汰1条旧数据。
            overflow = 1 if len(self._emergency_buffer) >= self._emergency_buffer.maxlen else 0
            self._emergency_buffer.append(item)
            if overflow > 0:
                logger.warning(
                    "紧急缓冲区已满(maxlen=%d)，%d条旧数据从内存deque淘汰（SQLite保留）",
                    self._emergency_buffer.maxlen,
                    overflow,
                )
                # FIXED-PROD: 原代码 from edgelite.services.event_bus import get_event_bus 路径错误
                # (该模块不存在) 且 publish(string, dict) API 用法错误 (EventBus.publish 需要 Event 对象)。
                # 改用 self._event_bus + InfluxDBFallbackEvent，与 _publish_fallback_event 保持一致。
                if self._event_bus is not None:
                    try:
                        from edgelite.engine.event_bus import InfluxDBFallbackEvent

                        await self._event_bus.publish(
                            InfluxDBFallbackEvent(
                                action="buffer_overflow",
                                reason=f"emergency buffer full, maxlen={self._emergency_buffer.maxlen}",
                                cached_count=overflow,
                            )
                        )
                    except Exception as e:
                        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                        logger.warning("发布缓冲区溢出事件失败: %s", e)

            if self._emergency_db is not None:
                write_success = False
                last_error = None
                for attempt in range(3):
                    try:
                        await asyncio.to_thread(self._emergency_db_write, item)
                        write_success = True
                        break
                    except (sqlite3.OperationalError, sqlite3.DatabaseError) as db_err:
                        last_error = db_err
                        if attempt < 2:
                            await asyncio.sleep(attempt * 2 + 1)
                        else:
                            logger.error("紧急缓冲SQLite写入失败(3次重试后): %s", db_err)
                            self._emergency_sqlite_failures += 1
                if not write_success:
                    self._emergency_buffer.pop()
                    await self._emergency_fallback_file_write(
                        item
                    )  # FIXED-P4: 原问题-SQLite写入全失败时数据仅存内存，崩溃即丢失；改为追加写入fallback文件
                    logger.error(
                        "紧急缓冲SQLite写入失败，已回滚deque append，数据写入fallback文件: %s",
                        last_error,
                    )

    async def _buffer_extend_with_db(self, items: list[dict]) -> None:
        """Extend buffer with multiple items and persist to SQLite (lock-guarded).

        FIXED-EMERGENCY-RETRY: Added retry logic for SQLite batch write failures.
        FIXED-P1: 原问题-SQLite写入在buffer_lock外，批量写入失败时deque有数据但SQLite无，重启后丢失
        改为SQLite写入在buffer_lock内，失败时回滚deque extend保持同步。
        """
        if not items:
            return
        async with self._emergency_buffer_lock:
            # FIXED-P0: 原问题-extend后失败回滚pop len(items)次，但deque maxlen可能已自动淘汰旧数据，
            # 导致多pop原有数据。改为记录extend前后的长度差，仅回滚实际新增数量。
            len_before = len(self._emergency_buffer)
            self._emergency_buffer.extend(items)
            len_after = len(self._emergency_buffer)
            actual_added = len_after - len_before
            is_full = len(self._emergency_buffer) >= self._emergency_buffer.maxlen
            if is_full:
                logger.warning(
                    "紧急缓冲区已满(maxlen=%d)，数据可能已溢出",
                    self._emergency_buffer.maxlen,
                )

            if self._emergency_db is not None:
                write_success = False
                last_error = None
                for attempt in range(3):
                    try:
                        await asyncio.to_thread(self._emergency_db_write_batch, items)
                        write_success = True
                        break
                    except (sqlite3.OperationalError, sqlite3.DatabaseError) as db_err:
                        last_error = db_err
                        if attempt < 2:
                            await asyncio.sleep(attempt * 2 + 1)
                        else:
                            logger.error("紧急缓冲批量SQLite写入失败(3次重试后): %s", db_err)
                            self._emergency_sqlite_failures += 1
                if not write_success:
                    # FIXED-P0: 仅回滚实际新增的数量，避免多pop原有数据
                    for _ in range(actual_added):
                        if self._emergency_buffer:
                            self._emergency_buffer.pop()
                    for item in items:  # FIXED-P4: 原问题-批量写入全失败时数据仅存内存；改为追加写入fallback文件
                        await self._emergency_fallback_file_write(item)
                    logger.error(
                        "紧急缓冲批量SQLite写入失败，已回滚deque extend(%d条)，数据写入fallback文件: %s",
                        actual_added,
                        last_error,
                    )

    async def _buffer_drain_all(self) -> list[dict]:
        """Atomically drain the entire buffer and return its contents."""
        async with self._emergency_buffer_lock:
            drained = list(self._emergency_buffer)
            self._emergency_buffer.clear()
            return drained

    async def _emergency_fallback_file_write(self, item: dict) -> None:
        """FIXED-P4: 原问题-SQLite写入全失败时数据仅存内存，崩溃即丢失
        改为追加写入fallback JSONL文件，确保进程崩溃后仍可恢复
        """
        try:
            fallback_path = self._emergency_db_path.replace(".db", ".fallback.jsonl")
            line = json.dumps(item, ensure_ascii=False, default=str) + "\n"
            await asyncio.to_thread(self._append_line, fallback_path, line)
        except Exception as e:
            logger.warning("Emergency fallback file write failed: %s", e)

    @staticmethod
    def _append_line(path: str, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def _emergency_db_delete_restored(self, max_id: int) -> None:
        """FIXED-BugR4X: 删除已恢复的紧急缓冲数据（带DB锁保护）"""
        with self._emergency_db_lock:
            if self._emergency_db is None:
                return
            try:
                self._emergency_db.execute(
                    "DELETE FROM emergency_buffer WHERE id <= ?",
                    (max_id,),
                )
                self._emergency_db.commit()
            except Exception as e:
                logger.warning("删除已恢复紧急缓冲数据失败: %s", e)

    async def restore_emergency_buffer(self) -> None:  # FIXED-P2: 紧急缓冲添加SQLite持久化
        """Restore buffered items from SQLite on startup (async-safe)."""
        if self._emergency_db is None:
            return
        try:
            rows = await asyncio.to_thread(  # FIXED-P2: 仅SQLite读取在线程池，deque操作在事件循环线程
                self._emergency_db.execute,
                # FIXED-BugR4X: 原问题-SELECT不获取id，恢复后无法删除已恢复行；修复-获取id用于删除
                "SELECT id, data FROM emergency_buffer ORDER BY id ASC",
            )
            row_list = rows.fetchall()
            if not row_list:
                return

            max_id = 0  # FIXED-BugR4X: 记录已恢复的最大id，用于删除
            async with self._emergency_buffer_lock:  # FIXED-P2: deque操作在asyncio.Lock保护下
                for row_id, data_str in row_list:  # FIXED-BugR4X: 解包id和data
                    if row_id is not None and row_id > max_id:
                        max_id = row_id
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        self._emergency_buffer.append(json.loads(data_str))

            count = len(self._emergency_buffer)
            if count > 0:
                logger.info("从SQLite恢复了%d条紧急缓冲数据", count)
            # FIXED-BugR4X: 原问题-恢复后不删除已恢复行，导致重复恢复和表膨胀；修复-恢复成功后删除已恢复行
            if max_id > 0:
                await asyncio.to_thread(self._emergency_db_delete_restored, max_id)
        except Exception as e:
            logger.error("恢复紧急缓冲数据失败: %s", e)

    @staticmethod
    def _read_fallback_file(path: str) -> list[str]:
        """FIXED-BugR4X: 读取fallback JSONL文件内容"""
        try:
            with open(path, encoding="utf-8") as f:
                return f.readlines()
        except Exception as e:
            logger.warning("读取fallback文件失败: %s", e)
            return []

    async def _replay_fallback_file(self) -> None:
        """FIXED-BugR4X: 原问题-_emergency_fallback_file_write写入的fallback JSONL文件无读取逻辑，数据永久丢失
        修复-读取fallback JSONL文件内容回放到紧急缓冲区，并删除文件
        """
        fallback_path = self._emergency_db_path.replace(".db", ".fallback.jsonl")
        if not os.path.exists(fallback_path):
            return
        try:
            lines = await asyncio.to_thread(self._read_fallback_file, fallback_path)
            if not lines:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(os.remove, fallback_path)
                return
            items = []
            for line in lines:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    items.append(json.loads(line))
            # 先删除文件再回放，若回放失败数据会重新写入fallback文件，避免数据丢失
            await asyncio.to_thread(os.remove, fallback_path)
            if items:
                await self._buffer_extend_with_db(items)
                logger.info("从fallback文件回放%d条紧急缓冲数据", len(items))
        except Exception as e:
            logger.error("回放fallback文件失败: %s", e)

    async def connect(self) -> None:
        """建立InfluxDB连接，失败时初始化SQLite降级存储

        P0-4.4: 增强连接验证 — 连接建立后执行ping验证，
        失败时降级到SQLite时序存储并记录warning日志。
        """
        # FIXED(安全) R5-S-10: 校验 URL 协议，若配置了 token 但使用 HTTP 明文传输则记录警告
        # InfluxDB token 相当于长期凭据，明文 HTTP 传输可被中间人嗅探
        if self._token and not str(self._url).startswith("https://"):
            logger.warning(
                "InfluxDB token sent over plaintext HTTP - use HTTPS in production (url=%s)",
                str(self._url).split("?")[0],
            )
        try:
            self._client = InfluxDBClient(
                url=self._url,
                token=self._token,
                org=self._org,
                timeout=_INFLUX_CONNECT_TIMEOUT_MS,  # FIXED: 原问题-timeout=5000魔法数字
            )
            # P0-4.4: 增强验证 — 先执行health()再执行ping()双重验证
            try:
                health = await asyncio.wait_for(
                    asyncio.to_thread(self._client.health),
                    timeout=_INFLUX_WRITE_TIMEOUT_S,  # FIXED: 原问题-timeout=5.0魔法数字
                )
                self._available = health.status == "pass"
            except (TimeoutError, Exception) as health_err:
                self._available = False
                logger.warning("InfluxDB健康检查超时或失败: %s", health_err)

            # P0-4.4: health通过后执行ping验证确认数据面可用
            if self._available:
                try:
                    ping_result = await asyncio.wait_for(
                        asyncio.to_thread(self._client.ping),
                        timeout=_INFLUX_WRITE_TIMEOUT_S,
                    )
                    if not ping_result:
                        logger.warning("InfluxDB ping returned False, server may be degraded")
                        self._available = False
                except (TimeoutError, Exception) as ping_err:
                    self._available = False
                    logger.warning("InfluxDB ping verification failed: %s", ping_err)

            if self._available:
                config = get_config()
                # flush_interval=0 会导致缓冲区永不刷新；flush_interval 极大值会导致
                # 数据在内存中无限积压。限制在合理范围 [100, 60000] ms
                raw_flush = config.influxdb.flush_interval
                flush_interval = max(100, min(60000, raw_flush))
                if flush_interval != raw_flush:
                    logger.warning(
                        "InfluxDB flush_interval=%d (原始配置%d) 超出安全范围[100,60000]，已调整为%d",
                        raw_flush,
                        flush_interval,
                        flush_interval,
                    )
                self._write_api = self._client.write_api(
                    write_options=WriteOptions(
                        batch_size=config.influxdb.batch_size,
                        flush_interval=flush_interval,
                    )
                )
                # R5-G-12: 创建同步写入专用 write_api 一次并复用，避免 _sync_batch 每次创建新实例
                self._sync_write_api = self._client.write_api(write_options=SYNCHRONOUS)
                self._query_api = self._client.query_api()
                self._buckets_api = self._client.buckets_api()
                logger.info(
                    "InfluxDB连接成功(health+ping验证通过): %s", self._url.split("?")[0]
                )  # FIXED-P1: 脱敏URL，移除查询参数
                await self._ensure_retention_policy()
            else:
                logger.warning("InfluxDB不可用(验证未通过)，将使用SQLite降级模式")
                await self._enter_fallback_mode("health/ping验证失败")
        except Exception as e:
            self._available = False
            logger.warning("InfluxDB连接失败: %s，将使用SQLite降级模式", e)
            await self._enter_fallback_mode(str(e))

        # FIXED-BugR4X: 原问题-restore_emergency_buffer和_replay_fallback_file从未被调用，紧急缓冲区数据和fallback文件数据永久丢失
        # 修复-在启动同步循环前恢复紧急缓冲区SQLite数据和fallback JSONL文件数据
        await self.restore_emergency_buffer()
        await self._replay_fallback_file()

        # 启动同步循环
        config = get_config()
        if config.influxdb.auto_sync_on_recovery:
            await self.start_sync()

    async def _init_sqlite_fallback(self) -> None:
        """初始化SQLite降级存储"""
        if self._sqlite_ts is not None:
            return
        config = get_config()
        if config.influxdb.fallback_backend == "sqlite":
            # FIXED-BugR4X: 原问题-先赋值self._sqlite_ts再start()，start()失败后_sqlite_ts指向损坏实例，_ensure_sqlite_started不会重试
            # 修复-先用临时变量candidate创建和start()，成功后才赋值给self._sqlite_ts；失败时cleanup candidate并保持self._sqlite_ts = None
            candidate = SqliteTimeSeriesStorage(db_path=config.influxdb.sqlite_ts_path)
            try:
                await candidate.start()
            except Exception:
                await candidate.stop()
                raise
            self._sqlite_ts = candidate
            logger.info("SQLite降级存储已初始化: %s", config.influxdb.sqlite_ts_path)

    async def _enter_fallback_mode(self, reason: str = "") -> None:
        """进入降级模式：标记状态、初始化SQLite、发布事件"""
        async with self._state_lock:  # FIXED-P2: _fallback_mode并发保护
            if self._fallback_mode:
                return
            self._fallback_mode = True
            self._stats_fallback_count += 1
        await self._init_sqlite_fallback()
        logger.warning(
            "InfluxDB降级到SQLite模式 (原因: %s, 第%d次降级)",
            reason,
            self._stats_fallback_count,
        )
        await self._publish_fallback_event("degraded", reason)

    async def _exit_fallback_mode(self) -> None:
        """退出降级模式：恢复状态、发布事件"""
        async with self._state_lock:  # FIXED-P2: _fallback_mode并发保护
            if not self._fallback_mode:
                return
            self._fallback_mode = False
        cached = 0
        if self._sqlite_ts:
            cached = await self._sqlite_ts.get_unsynced_count()
        logger.info("InfluxDB已恢复，退出降级模式 (待同步缓存: %d条)", cached)
        await self._publish_fallback_event("recovered", "", cached)

    async def _publish_fallback_event(
        self,
        action: str,
        reason: str = "",
        cached_count: int = 0,
    ) -> None:
        """发布降级/恢复事件到EventBus"""
        if self._event_bus is None:
            return
        try:
            from edgelite.engine.event_bus import InfluxDBFallbackEvent

            event = InfluxDBFallbackEvent(
                action=action,
                reason=reason,
                cached_count=cached_count,
            )
            await self._event_bus.publish(event)
        except Exception as e:
            # FIXED-P2: 原问题-降级事件发布失败仅debug日志，运维无法感知监控系统盲区；改为warning
            logger.warning("发布InfluxDB降级事件失败: %s", e)

    async def _ensure_sqlite_started(self) -> None:
        """确保SQLite降级存储已启动"""
        if self._sqlite_ts is None:
            await self._init_sqlite_fallback()

    async def _ensure_retention_policy(self) -> None:
        """Ensure InfluxDB bucket has the configured retention policy, apply if mismatch"""
        if not self._client or not self._buckets_api:
            return
        try:
            bucket = await asyncio.to_thread(self._buckets_api.find_bucket_by_name, self._bucket)
            if bucket:
                current_rp = getattr(bucket, "retention_rules", None)
                expected_secs = self._retention_days * 86400
                if current_rp and hasattr(current_rp, "retention_secs"):
                    secs = current_rp.retention_secs
                    if secs > 0 and secs != expected_secs:
                        logger.info(
                            "InfluxDB retention: %s current=%d days, configured=%d days, applying update",
                            self._bucket,
                            secs // 86400,
                            self._retention_days,
                        )
                        await self._apply_retention_policy(bucket, expected_secs)
                    elif secs == 0:
                        logger.info(
                            "InfluxDB retention: %s unlimited, configured=%d days, applying update",
                            self._bucket,
                            self._retention_days,
                        )
                        await self._apply_retention_policy(bucket, expected_secs)
                    else:
                        logger.debug(
                            "InfluxDB retention policy already correct: %s (%d days)",
                            self._bucket,
                            self._retention_days,
                        )
                else:
                    # No retention rules set, apply configured policy
                    logger.info(
                        "InfluxDB retention: %s no rules set, applying %d days",
                        self._bucket,
                        self._retention_days,
                    )
                    await self._apply_retention_policy(bucket, expected_secs)
            else:
                logger.warning("InfluxDB bucket not found: %s", self._bucket)
        except Exception as e:
            logger.debug("InfluxDB retention policy check failed: %s", e)

    async def _apply_retention_policy(self, bucket: Any, expected_secs: int) -> None:
        """Apply retention policy update to the InfluxDB bucket"""
        try:
            # #[AUDIT-FIX] 使用 PatchRetentionRule（更新模型）+ PatchBucketRequest 直接调用
            # patch_buckets_id。原代码使用 BucketRetentionRules（创建模型）并修改完整 Bucket
            # 对象后传给 update_bucket()，导致 InfluxDB v2 PATCH /buckets/{id} 返回 400:
            # cannot unmarshal object into Go struct field bucketUpdate.retentionRules
            # of type []tenant.retentionRuleUpdate
            from influxdb_client.domain.patch_bucket_request import PatchBucketRequest
            from influxdb_client.domain.patch_retention_rule import PatchRetentionRule

            patch_request = PatchBucketRequest(
                retention_rules=[
                    PatchRetentionRule(
                        type="expire",
                        every_seconds=expected_secs,
                    )
                ]
            )
            await asyncio.to_thread(
                self._buckets_api._buckets_service.patch_buckets_id,
                bucket.id,
                patch_request,
            )
            logger.info(
                "InfluxDB retention policy updated: %s -> %d days",
                self._bucket,
                expected_secs // 86400,
            )
        except Exception as e:
            logger.error("Failed to update InfluxDB retention policy: %s", e)

    async def cleanup_expired_data(self) -> int:
        """Clean up expired data older than retention_days via Flux query + delete"""
        # FIXED-P0: _available读取通过await available()加锁保护
        if not await self.available() or not self._query_api:
            # InfluxDB不可用时，清理SQLite中的旧数据
            if self._sqlite_ts:
                return await self._sqlite_ts.cleanup_old_data(self._retention_days)
            return 0
        return await self._delete_old_data()

    async def _delete_old_data(self) -> int:
        """Delete old data from InfluxDB using DeletePredicateRequest

        修复：拆分为两个删除请求，按 measurement 过滤，避免误删降采样数据。
        1. device_points（原始数据）按 retention_days 删除
        2. device_points_downsampled（降采样数据）按更长保留期（90天）删除
        """
        # FIXED-P0: _available读取通过await available()加锁保护
        if not await self.available() or not self._query_api or not self._client:
            return 0
        try:
            from datetime import timedelta

            from influxdb_client import DeletePredicateRequest

            delete_api = self._client.delete_api()
            # 降采样数据保留期更长（至少90天），避免误删降采样数据
            downsample_retention_days = max(self._retention_days, 90)

            # 1. 删除原始点位数据（device_points），按 retention_days 保留
            cutoff_raw = datetime.now(UTC) - timedelta(days=self._retention_days)
            predicate_raw = DeletePredicateRequest(
                start="1970-01-01T00:00:00Z",
                stop=cutoff_raw.isoformat(),
                predicate='_measurement="device_points"',
            )
            await asyncio.to_thread(
                lambda: delete_api.delete(predicate_raw, bucket=self._bucket, org=self._org)
            )

            # 2. 删除降采样数据（device_points_downsampled），按更长保留期删除
            cutoff_downsampled = datetime.now(UTC) - timedelta(days=downsample_retention_days)
            predicate_downsampled = DeletePredicateRequest(
                start="1970-01-01T00:00:00Z",
                stop=cutoff_downsampled.isoformat(),
                predicate='_measurement="device_points_downsampled"',
            )
            await asyncio.to_thread(
                lambda: delete_api.delete(predicate_downsampled, bucket=self._bucket, org=self._org)
            )

            logger.info(
                "InfluxDB expired data cleaned up (raw retention=%d days, downsampled retention=%d days)",
                self._retention_days,
                downsample_retention_days,
            )
            return 1
        except Exception as e:
            logger.error("InfluxDB expired data cleanup failed: %s", e)
            return 0

    async def close(self) -> None:
        """关闭连接"""
        await self.stop_sync()
        # FIX-EL-R2-SEVERE: 每个 close 步骤独立 try/except，防止单步异常导致
        # 后续资源（_client/_sqlite_ts/_emergency_db）未关闭造成泄漏。
        if self._write_api:
            try:
                await asyncio.to_thread(self._write_api.close)
            except Exception as e:
                logger.warning("关闭 InfluxDB write_api 失败: %s", e)
        # R5-G-12: 关闭同步写入专用 write_api，防止资源泄漏
        if self._sync_write_api:
            try:
                await asyncio.to_thread(self._sync_write_api.close)
            except Exception as e:
                logger.warning("关闭 InfluxDB sync_write_api 失败: %s", e)
            self._sync_write_api = None
        if self._client:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception as e:
                logger.warning("关闭 InfluxDB client 失败: %s", e)
            self._client = None
        if self._sqlite_ts:
            try:
                await self._sqlite_ts.stop()
            except Exception as e:
                logger.warning("停止 sqlite_ts 失败: %s", e)
            self._sqlite_ts = None
        if self._emergency_db is not None:  # FIXED-P2: 紧急缓冲添加SQLite持久化
            try:
                # FIX-EL-R2-SEVERE: 先获取锁防止与工作线程的 _emergency_db_write 竞态
                with self._emergency_db_lock:
                    self._emergency_db.close()
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("关闭紧急缓冲DB失败: %s", e)
            self._emergency_db = None
        async with self._state_lock:  # FIXED-P2: _available/_fallback_mode修改移入_state_lock
            self._available = False
            self._fallback_mode = False

    async def backup(self, backup_dir: str = "data/backups") -> None:
        """备份时序数据 — 委托给内部 SqliteTimeSeriesStorage"""
        if self._sqlite_ts and hasattr(self._sqlite_ts, "backup"):
            await self._sqlite_ts.backup(backup_dir)
        else:
            logger.warning("时序存储备份跳过：SQLite降级存储未初始化")

    async def available(self) -> bool:
        """返回 InfluxDB 当前是否可用（通过状态锁保护读取）。"""
        # FIXED-P0: _available读取通过_state_lock保护，防止读到不一致状态
        async with self._state_lock:
            return self._available

    async def fallback_mode(self) -> bool:
        """是否处于降级模式"""
        # FIXED-P0: _fallback_mode读取通过_state_lock保护
        async with self._state_lock:
            return self._fallback_mode

    async def using_fallback(self) -> bool:
        """是否正在使用SQLite降级模式"""
        # FIXED-P0: _fallback_mode读取通过_state_lock保护
        async with self._state_lock:
            return self._fallback_mode and self._sqlite_ts is not None

    def set_event_bus(self, event_bus: Any) -> None:
        """设置EventBus引用，用于发布降级/恢复事件"""
        self._event_bus = event_bus

    async def check_health(self) -> bool:
        """检查InfluxDB可用性，失败后尝试自动恢复"""
        if not self._client:
            return False
        try:
            health = await asyncio.to_thread(self._client.health)
            is_healthy = health.status == "pass"
            if is_healthy:
                was_unavailable = False
                async with self._state_lock:  # FIXED-P2: _available并发保护
                    self._fail_count = 0
                    if not self._available:
                        self._available = True
                        was_unavailable = True
                        from influxdb_client.client.write_api import WriteOptions

                        cfg = get_config()
                        # FIXED-BugR4X: 原问题-重建_write_api时不关闭旧的，导致资源泄漏；修复-重建前先关闭旧的write_api
                        old_write_api = self._write_api
                        if old_write_api is not None:
                            await asyncio.to_thread(old_write_api.close)
                        self._write_api = self._client.write_api(
                            write_options=WriteOptions(
                                batch_size=cfg.influxdb.batch_size,
                                flush_interval=max(100, min(60000, cfg.influxdb.flush_interval)),
                            )
                        )
                        # R5-G-12: 连接恢复时同步重建 _sync_write_api 并复用
                        old_sync_write_api = self._sync_write_api
                        if old_sync_write_api is not None:
                            await asyncio.to_thread(old_sync_write_api.close)
                        self._sync_write_api = self._client.write_api(write_options=SYNCHRONOUS)
                        self._query_api = self._client.query_api()
                        self._buckets_api = self._client.buckets_api()
                        logger.info("InfluxDB connection recovered")
                # FIXED-P0: 在锁外判断was_unavailable，避免长时间持锁
                if was_unavailable:
                    await self._exit_fallback_mode()
            else:
                async with self._state_lock:  # FIXED-P2: _available并发保护
                    self._available = False
            return is_healthy
        except Exception as e:
            async with self._state_lock:  # FIXED-P2: _available并发保护
                self._available = False
            logger.debug("InfluxDB health check exception: %s", e)
            return False

    async def _write_with_retry(self, record=None, max_retries: int = 3) -> None:
        """带重试的写入方法 - 瞬时网络错误重试，数据校验错误不重试。

        Args:
            record: 要写入的记录（可为None）
            max_retries: 最大重试次数（默认3次）

        Raises:
            ConnectionError: 重试耗尽后仍失败
            ValueError: 数据校验错误（不重试，立即抛出）
        """
        last_exc = None
        for attempt in range(max_retries):
            try:
                self._write_api.write(record)
                return
            except (ConnectionError, TimeoutError, OSError) as e:
                last_exc = e
                if attempt < max_retries - 1:
                    logger.warning(
                        "InfluxDB write attempt %d/%d failed (transient): %s",
                        attempt + 1,
                        max_retries,
                        e,
                    )
                    await asyncio.sleep(min(2**attempt, 10))  # 指数退避
                else:
                    logger.error(
                        "InfluxDB write failed after %d attempts: %s",
                        max_retries,
                        e,
                    )
            except ValueError:
                # 数据校验错误不重试
                raise
        raise last_exc  # type: ignore[misc]

    async def write_point(
        self,
        device_id: str,
        point_name: str,
        value: float,
        timestamp: datetime | None = None,
        quality: str = "good",
    ) -> bool:
        """写入单条测点数据

        当InfluxDB不可用时，将数据写入SQLite降级存储以便后续同步恢复。
        """
        # FIXED-P0: _available读取通过await available()加锁保护
        if not await self.available() or not self._write_api:
            # FIXED(严重): 原代码直接读 self._fallback_mode 未加锁，高并发下多协程
            # 同时通过检查导致重复调用 _enter_fallback_mode 和 _init_sqlite_fallback
            # 修复-使用 await self.fallback_mode() 加锁读取
            if not await self.fallback_mode():
                await self._enter_fallback_mode("InfluxDB不可用")
            await self._fallback_write(device_id, point_name, value, timestamp, quality)
            return False

        try:
            import math

            float_val = float(value)
            if math.isnan(float_val) or math.isinf(float_val):
                logger.warning(
                    "InfluxDB写入跳过: 值为NaN/Infinity (device=%s, point=%s)",
                    device_id,
                    point_name,
                )
                return False

            point = (
                Point("device_points")
                .tag("device_id", device_id)
                .tag("point_name", point_name)
                .tag("quality", quality)
                .field("value", float_val)
            )
            if timestamp:
                point = point.time(timestamp)

            await asyncio.to_thread(self._write_api.write, bucket=self._bucket, record=point)
            async with (
                self._state_lock
            ):  # FIXED-P2: _fail_count修改移入_state_lock，与available()/check_health()锁保护读取一致
                self._fail_count = 0
            return True
        except Exception as e:
            logger.error("InfluxDB写入失败: %s，降级到SQLite", e)
            await self._fallback_write(device_id, point_name, value, timestamp, quality)
            # FIXED-P3: 原问题-_fail_count和_fallback_mode在锁外读取；改为锁内判断并设置标志
            need_fallback = False
            async with self._state_lock:
                self._fail_count += 1
                if self._fail_count >= 3:
                    self._available = False
                    if not self._fallback_mode:
                        need_fallback = True
            if need_fallback:
                await self._enter_fallback_mode(str(e))
            return False

    async def _fallback_write(
        self,
        device_id: str,
        point_name: str,
        value: float | Any,
        timestamp: datetime | None,
        quality: str,
    ) -> None:
        """InfluxDB不可用时将数据写入SQLite降级存储"""
        try:
            await self._ensure_sqlite_started()
            if self._sqlite_ts is None:
                return

            timestamp_ns = None
            if timestamp:
                ts_epoch = timestamp.timestamp()
                timestamp_ns = int(ts_epoch * 1e9)
            await self._sqlite_ts.write_point(
                measurement="device_points",
                device_id=device_id,
                point_name=point_name,
                value=value,
                quality=quality,
                timestamp_ns=timestamp_ns,
                tags={"device_id": device_id, "point_name": point_name, "quality": quality},
            )
            self._stats_cached_count += 1
        except Exception as e:
            # FIXED(严重): 原问题-except Exception 未捕获异常变量，logger.error 不带异常信息与堆栈
            # 修复：捕获异常变量 e，日志中输出异常消息并带 exc_info 保留完整堆栈
            logger.error("SQLite降级写入失败: 数据可能在InfluxDB中断期间丢失: %s", e, exc_info=True)
            buf_item = {
                "device_id": device_id,
                "point_name": point_name,
                "value": value,
                "quality": quality,
                "timestamp": timestamp.isoformat() if timestamp else None,
            }
            await self._buffer_append_with_db(buf_item)  # FIXED-EMERGENCY-RACE

    def _emergency_db_write(self, buf_item: dict) -> None:  # FIXED-P2: 原问题-emergency_db同步操作无锁保护
        with self._emergency_db_lock:
            if self._emergency_db is None:
                return
            try:
                self._emergency_db.execute(
                    "INSERT INTO emergency_buffer (data, created_at) VALUES (?, ?)",
                    (json.dumps(buf_item, default=str), time.time()),
                )
                self._emergency_db.commit()
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("紧急缓冲SQLite写入失败: %s", e)

    def _emergency_db_write_batch(
        self, batch_items: list[dict]
    ) -> None:  # FIXED-P2: 原问题-批量emergency_db同步操作无锁保护
        with self._emergency_db_lock:
            if self._emergency_db is None:
                return
            try:
                # FIXED(严重): 原问题-循环 execute 单条 INSERT 导致 N 次调用
                # 修复：使用 executemany 一次提交批量数据
                now_ts = time.time()
                rows = [(json.dumps(item, default=str), now_ts) for item in batch_items]
                self._emergency_db.executemany(
                    "INSERT INTO emergency_buffer (data, created_at) VALUES (?, ?)",
                    rows,
                )
                self._emergency_db.commit()
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("紧急缓冲批量SQLite写入失败: %s", e, exc_info=True)

    async def _fallback_to_cache(
        self,
        device_id: str,
        point_name: str,
        value: float | Any,
        timestamp: datetime | None,
        quality: str,
    ) -> bool:
        """InfluxDB不可用时将数据写入本地缓存，防止断网丢数据"""
        try:
            from edgelite.app import _app_state

            cache_manager = getattr(_app_state, "cache_manager", None)
            if cache_manager is None:
                logger.debug("CacheManager not available, data will be lost during outage")
                return False
            ts_str = timestamp.isoformat() if timestamp else datetime.now(UTC).isoformat()
            return await cache_manager.add_to_cache(
                measurement="device_points",
                tags={"device_id": device_id, "point_name": point_name, "quality": quality},
                fields={"value": float(value)},
                timestamp=ts_str,
            )
        except Exception as e:
            # FIXED(严重): 原问题-except Exception 未捕获异常变量，logger.error 不带异常信息与堆栈
            logger.error(
                "Cache fallback failed: data may be lost during InfluxDB outage: %s",
                e,
                exc_info=True,
            )
            return False

    async def write_points_batch(self, records: list[dict]) -> bool:
        """批量写入测点数据

        当InfluxDB不可用时，将数据写入SQLite降级存储以便后续同步恢复。
        """
        # R5-G-06: 硬上限防止超大批次导致 OOM，超出部分截断并告警
        MAX_WRITE_BATCH = 10000
        if len(records) > MAX_WRITE_BATCH:
            logger.warning("write_points_batch truncated from %d to %d", len(records), MAX_WRITE_BATCH)
            records = records[:MAX_WRITE_BATCH]
        # FIXED-P0: _available读取通过await available()加锁保护
        if not await self.available() or not self._write_api:
            # FIXED(严重): 原代码直接读 self._fallback_mode 未加锁
            if not await self.fallback_mode():
                await self._enter_fallback_mode("InfluxDB不可用")
            await self._fallback_batch_write(records)
            return False

        try:
            import math

            points = []
            for rec in records:
                device_id = rec.get("device_id")
                point_name = rec.get("point_name")
                raw_value = rec.get("value")
                if not device_id or not point_name or raw_value is None:
                    logger.warning("批量写入跳过: 缺少必填字段 (device_id/point_name/value)")
                    continue
                float_val = float(raw_value)
                if math.isnan(float_val) or math.isinf(float_val):
                    logger.warning(
                        "批量写入跳过: 值为NaN/Infinity (device=%s, point=%s)",
                        device_id,
                        point_name,
                    )
                    continue
                p = (
                    Point("device_points")
                    .tag("device_id", device_id)
                    .tag("point_name", point_name)
                    .tag("quality", rec.get("quality", "good"))
                    .field("value", float_val)
                )
                if rec.get("timestamp"):
                    p = p.time(rec["timestamp"])
                points.append(p)

            if not points:
                return True
            await asyncio.to_thread(self._write_api.write, bucket=self._bucket, record=points)
            async with self._state_lock:  # FIXED-P2: _fail_count修改移入_state_lock
                self._fail_count = 0
            return True
        except Exception as e:
            logger.error("InfluxDB批量写入失败: %s，降级到SQLite", e)
            await self._fallback_batch_write(records)
            need_fallback = False
            async with self._state_lock:  # FIXED-P2: _fail_count/_available修改移入_state_lock
                self._fail_count += 1
                if self._fail_count >= 3:
                    self._available = False
                    if not self._fallback_mode:
                        need_fallback = True
            if need_fallback:
                await self._enter_fallback_mode(str(e))
            return False

    async def _fallback_batch_write(self, records: list[dict]) -> None:
        """InfluxDB不可用时批量写入SQLite降级存储"""
        try:
            await self._ensure_sqlite_started()
            if self._sqlite_ts is None:
                return

            sqlite_points = []
            for rec in records:
                timestamp_ns = None
                ts = rec.get("timestamp")
                if ts:
                    if isinstance(ts, datetime):
                        timestamp_ns = int(ts.timestamp() * 1e9)
                    elif isinstance(ts, (int, float)):
                        timestamp_ns = int(ts)
                sqlite_points.append(
                    {
                        "measurement": "device_points",
                        "device_id": rec.get("device_id", ""),
                        "point_name": rec.get("point_name", ""),
                        "value": rec.get("value"),
                        "quality": rec.get("quality", "good"),
                        "timestamp_ns": timestamp_ns,
                        "tags": {
                            "device_id": rec.get("device_id", ""),
                            "point_name": rec.get("point_name", ""),
                            "quality": rec.get("quality", "good"),
                        },
                    }
                )
            await self._sqlite_ts.write_points_batch(sqlite_points)
            self._stats_cached_count += len(sqlite_points)
        except Exception as e:
            # FIXED(严重): 原问题-except Exception 未捕获异常变量，logger.error 不带异常信息与堆栈
            logger.error(
                "SQLite降级批量写入失败: 数据可能在InfluxDB中断期间丢失: %s",
                e,
                exc_info=True,
            )
            batch_items = []
            for rec in records:
                try:
                    buf_item = {
                        "measurement": rec.get("measurement", "device_points"),
                        "device_id": rec.get("device_id", ""),
                        "point_name": rec.get("point_name", ""),
                        "value": rec.get("value"),
                        "timestamp_ns": int(rec["timestamp"].timestamp() * 1e9)
                        if isinstance(rec.get("timestamp"), datetime)
                        else rec.get("timestamp"),
                    }
                    batch_items.append(buf_item)
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("构造紧急缓冲条目失败: %s", e)
            if batch_items:
                await self._buffer_extend_with_db(batch_items)  # FIXED-EMERGENCY-RACE

    @staticmethod
    def _escape_flux_value(value: str) -> str:
        """转义 Flux 查询中的字符串值，使用单引号（Flux 要求单引号包裹字符串字面量）"""
        escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
        return f"'{escaped}'"

    async def query_points(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        aggregate: str | None = None,
        max_points: int = 10000,
        agg_fn: str | None = None,
        offset: int = 0,  # R9-S-16: 新增 offset 参数支持流式分批查询
    ) -> list[dict]:
        """查询时序数据，InfluxDB不可用时从SQLite查询"""
        # FIXED(严重): 新增 agg_fn 参数，支持用户指定的聚合函数(max/min/sum 等)，
        # 原代码硬编码 mean 导致用户请求 max/min 时始终返回 mean 结果
        # FIXED: 强制校验 max_points 为整数且在合理范围内，防止 Flux 注入
        try:
            max_points = int(max_points)
        except (TypeError, ValueError):
            max_points = 10000
        if max_points < 1:
            max_points = 1
        elif max_points > 50000:
            max_points = 50000

        # R9-S-16: 校验 offset 为非负整数
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            offset = 0

        # FIXED-P0: _available读取通过await available()加锁保护
        if not await self.available() or not self._query_api:
            return await self._fallback_query_points(
                device_id, point_name, start, stop, aggregate, max_points, agg_fn=agg_fn, offset=offset
            )

        time_range_re = re.compile(r"^-?\d+[smhdwMy]$|^\d{4}-\d{2}-\d{2}")
        for param_name, param_val in [("start", start), ("stop", stop)]:
            if param_val and not (
                time_range_re.match(param_val) or param_val.lstrip("-").replace(".", "", 1).isdigit()
            ):
                logger.error("非法的时间范围参数 %s: %s", param_name, param_val)
                return []

        safe_device_id = self._escape_flux_value(device_id)
        safe_point_name = self._escape_flux_value(point_name)
        safe_start = self._escape_flux_value(start)
        safe_stop = self._escape_flux_value(stop) if stop else ""

        safe_bucket = self._escape_flux_value(self._bucket)  # FIXED-P1: 原问题-bucket名称f-string无转义，可注入Flux查询
        stop_clause = f", stop: {safe_stop}" if stop else ""
        flux = f"""
from(bucket: {safe_bucket})
  |> range(start: {safe_start}{stop_clause})
  |> filter(fn: (r) => r._measurement == "device_points")
  |> filter(fn: (r) => r.device_id == {safe_device_id})
  |> filter(fn: (r) => r.point_name == {safe_point_name})
"""
        if aggregate:
            if not re.match(r"^\d+[smh]$", aggregate):
                logger.error("非法的聚合窗口参数: %s", aggregate)
                return []
            # FIXED(严重): 原代码硬编码 fn: mean，用户请求 max/min/sum 等时始终返回 mean
            # 修复-使用用户指定的 agg_fn，默认 mean
            _flux_fn = (agg_fn or "mean").lower()
            # Flux aggregateWindow 支持的函数名白名单校验，防止注入
            _valid_flux_fns = {"mean", "max", "min", "last", "first", "sum", "count", "median", "stddev"}
            if _flux_fn not in _valid_flux_fns:
                _flux_fn = "mean"
            flux += f"  |> aggregateWindow(every: {aggregate}, fn: {_flux_fn}, createEmpty: false)\n"
            # R9-S-16: Flux limit 支持 offset 参数，用于流式分批查询
            flux += f"  |> limit(n: {max_points}, offset: {offset})\n"
            flux += '  |> yield(name: "result")'
        else:
            # R9-S-16: Flux limit 支持 offset 参数，用于流式分批查询
            flux += f"  |> limit(n: {max_points}, offset: {offset})\n"
            flux += '  |> yield(name: "result")'

        try:
            # FIXED-Bug30: 添加 30 秒超时保护，避免大范围查询永久挂起导致连接池耗尽
            # 对比 query_latest 已有 10 秒超时，query_points 缺失超时保护
            tables = await asyncio.wait_for(
                asyncio.to_thread(self._query_api.query, flux, self._org),
                timeout=30.0,
            )
            results = []
            for table in tables:
                for record in table.records:
                    results.append(
                        {
                            "time": record.get_time().isoformat() if record.get_time() else None,
                            "value": record.get_value(),
                            "device_id": record.values.get("device_id"),
                            "point_name": record.values.get("point_name"),
                            "quality": record.values.get("quality"),
                        }
                    )
            return results
        except TimeoutError:
            logger.warning(
                "InfluxDB查询超时(30s): device=%s point=%s start=%s",
                device_id,
                point_name,
                start,
            )
            return []
        except Exception as e:
            logger.error("InfluxDB查询失败: %s", e)
            return []

    async def _fallback_query_points(
        self,
        device_id: str,
        point_name: str,
        start: str,
        stop: str | None = None,
        aggregate: str | None = None,
        max_points: int = 10000,
        agg_fn: str | None = None,
        offset: int = 0,  # R9-S-16: 新增 offset 参数支持流式分批查询
    ) -> list[dict]:
        """InfluxDB不可用时从SQLite查询时序数据"""
        if not self._sqlite_ts:
            return []

        try:
            start_ns = self._parse_time_to_ns(start)
            stop_ns = self._parse_time_to_ns(stop) if stop else None

            window_seconds = None
            sqlite_agg_fn = None
            if aggregate:
                # FIXED(严重): 原代码硬编码 agg_fn = "mean"，用户请求 max/min 等时始终返回 mean
                # 修复-使用传入的 agg_fn，默认 mean
                sqlite_agg_fn = (agg_fn or "mean").lower()
                window_seconds = self._parse_aggregate_to_seconds(aggregate)

            return await self._sqlite_ts.query_points(
                device_id=device_id,
                point_name=point_name,
                start_ns=start_ns,
                stop_ns=stop_ns,
                aggregate=sqlite_agg_fn,
                window_seconds=window_seconds,
                limit=max_points,
                offset=offset,  # R9-S-16: 传递 offset 到 SQLite 查询
            )
        except Exception as e:
            logger.error("SQLite降级查询失败: %s", e)
            return []

    async def query_latest(self, device_id: str, point_names: list[str] | None = None) -> dict[str, Any]:
        """查询设备最新测点值，InfluxDB不可用时从SQLite查询"""
        # FIXED-P0: _available读取通过await available()加锁保护
        if not await self.available() or not self._query_api:
            return await self._fallback_query_latest(device_id, point_names)

        safe_device_id = self._escape_flux_value(device_id)
        point_filter = ""
        if point_names:
            safe_names = ", ".join(self._escape_flux_value(n) for n in point_names)
            # FIXED: P2-1 Flux point_name filter now uses single-quoted escaped values
            point_filter = f"  |> filter(fn: (r) => contains(value: r.point_name, set: [{safe_names}]))\n"

        safe_bucket = self._escape_flux_value(
            self._bucket
        )  # FIXED-P1: 原问题-query_latest中bucket未转义，与query_points不一致
        flux = f"""
from(bucket: {safe_bucket})
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "device_points")
  |> filter(fn: (r) => r.device_id == {safe_device_id})
  {point_filter}
  |> last()
"""

        try:
            tables = await asyncio.wait_for(
                asyncio.to_thread(self._query_api.query, flux, self._org),
                timeout=10.0,
            )
            result = {}
            for table in tables:
                for record in table.records:
                    pn = record.values.get("point_name")
                    if pn:
                        result[pn] = {
                            "value": record.get_value(),
                            "time": record.get_time().isoformat() if record.get_time() else None,
                            "quality": record.values.get("quality", "good"),
                        }
            return result
        except TimeoutError:
            logger.warning(
                "InfluxDB最新值查询超时(10s): device=%s",
                device_id,
            )
            return {}
        except Exception as e:
            logger.error("InfluxDB最新值查询失败: %s", e)
            return {}

    async def _fallback_query_latest(self, device_id: str, point_names: list[str] | None = None) -> dict[str, Any]:
        """InfluxDB不可用时从SQLite查询最新测点值"""
        if not self._sqlite_ts or not point_names:
            return {}
        try:
            return await self._sqlite_ts.query_latest(device_id, point_names)
        except Exception as e:
            logger.error("SQLite降级最新值查询失败: %s", e)
            return {}

    @staticmethod
    def _parse_time_to_ns(time_str: str) -> int:
        """将Flux风格的时间字符串转换为纳秒时间戳"""
        now_ns = time.time_ns()

        # 相对时间: -1h, -30m, -2d 等
        rel_match = re.match(r"^-(\d+)([smhdwMy])$", time_str)
        if rel_match:
            amount = int(rel_match.group(1))
            unit = rel_match.group(2)
            multipliers = {
                "s": 1,
                "m": 60,
                "h": 3600,
                "d": 86400,
                "w": 604800,
                "M": 2592000,
                "y": 31536000,
            }
            seconds = amount * multipliers.get(unit, 1)
            return now_ns - int(seconds * 1e9)

        # 绝对时间: 2024-01-01T00:00:00Z
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1e9)
        except (ValueError, TypeError):
            pass

        # 纯数字（纳秒）
        try:
            return int(time_str)
        except (ValueError, TypeError):
            pass

        return now_ns - 3600_000_000_000  # 默认1小时前

    @staticmethod
    def _parse_aggregate_to_seconds(aggregate: str) -> int:
        """将Flux风格的聚合窗口转换为秒数"""
        match = re.match(r"^(\d+)([smh])$", aggregate)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            multipliers = {"s": 1, "m": 60, "h": 3600}
            return amount * multipliers.get(unit, 1)
        return 60  # 默认1分钟

    # ---- 增量同步 ----

    async def start_sync(self) -> None:
        """启动SQLite到InfluxDB的增量同步循环"""
        # R11-DRV-03: 用 _sync_lock 包裹检查-设置-创建全流程，防止并发调用创建多个同步任务（TOCTOU 竞态）
        async with self._sync_lock:
            if self._sync_running:
                return
            self._sync_running = True
            config = get_config()
            self._sync_task = asyncio.create_task(self._sync_loop(interval=config.influxdb.sync_interval))
        logger.info(
            "SQLite->InfluxDB增量同步已启动 (间隔=%ds)",
            config.influxdb.sync_interval,
        )

    async def stop_sync(self) -> None:
        """停止增量同步循环"""
        # R11-DRV-03: 用 _sync_lock 包裹检查-清理-设置全流程；await task 必须在锁外执行，
        # 避免持锁等待任务结束造成死锁（_sync_loop 内部 _sync_batch 也会获取 _sync_lock）
        async with self._sync_lock:
            self._sync_running = False
            task = self._sync_task
            self._sync_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("SQLite->InfluxDB增量同步已停止")

        # R11-DRV-08: 关闭复用的网络探测 HTTP client，避免连接泄漏
        if self._probe_client is not None:
            with contextlib.suppress(Exception):
                await self._probe_client.aclose()
            self._probe_client = None

    async def _sync_loop(self, interval: int = 30) -> None:
        """增量同步循环：定期检查InfluxDB恢复状态，恢复后从SQLite读取未同步数据增量写入InfluxDB"""
        while self._sync_running:
            try:
                # 降级模式下定期检查InfluxDB是否恢复
                # FIXED-P0: _available读取通过await available()加锁保护
                if self._fallback_mode and not await self.available():
                    await self.check_health()

                if await self.available() and self._sqlite_ts:
                    # FIXED-BugR4X: 原问题-_buffer_drain_all从未被调用，紧急缓冲区数据永久丢失
                    # 修复-InfluxDB可用时先将紧急缓冲区数据回灌到_sqlite_ts，再执行正常同步
                    drained = await self._buffer_drain_all()
                    if drained:
                        # FIXED(严重): 原问题-循环 await write_point 逐条写入导致 N+1
                        # 修复：转换为 points 列表后调用 write_points_batch 批量写入
                        batch_points = []
                        for item in drained:
                            try:
                                timestamp_ns = None
                                ts_str = item.get("timestamp")
                                if ts_str:
                                    try:
                                        ts = datetime.fromisoformat(ts_str)
                                        timestamp_ns = int(ts.timestamp() * 1e9)
                                    except (ValueError, TypeError):
                                        timestamp_ns = None
                                batch_points.append(
                                    {
                                        "measurement": "device_points",
                                        "device_id": item.get("device_id", ""),
                                        "point_name": item.get("point_name", ""),
                                        "value": item.get("value"),
                                        "quality": item.get("quality", "good"),
                                        "timestamp_ns": timestamp_ns,
                                        "tags": {
                                            "device_id": item.get("device_id", ""),
                                            "point_name": item.get("point_name", ""),
                                            "quality": item.get("quality", "good"),
                                        },
                                    }
                                )
                            except Exception as conv_err:
                                logger.error("紧急缓冲数据转换失败: %s", conv_err)
                        if batch_points:
                            try:
                                await self._sqlite_ts.write_points_batch(batch_points)
                            except Exception as batch_err:
                                logger.error("紧急缓冲数据批量回灌SQLite失败: %s", batch_err, exc_info=True)
                    unsynced = await self._sqlite_ts.get_unsynced_count()
                    if unsynced > 0:
                        await self._sync_batch()

                # R7-S-02: 低频调用 cleanup_expired_data()（每24小时一次），
                # 清理 SQLite 降级存储和 InfluxDB 中的过期数据，防止无限增长
                now_ts = time.time()
                if now_ts - self._last_cleanup_time >= 86400:
                    self._last_cleanup_time = now_ts
                    try:
                        deleted = await self.cleanup_expired_data()
                        if deleted:
                            logger.info("InfluxDB cleanup_expired_data executed: deleted=%s", deleted)
                    except Exception as cleanup_err:
                        logger.error("InfluxDB cleanup_expired_data failed in _sync_loop: %s", cleanup_err)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("增量同步循环异常: %s", e)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _sync_batch(self) -> int:
        """执行一批增量同步"""
        # FIXED-P0: _available读取通过await available()加锁保护
        if not self._sqlite_ts or not await self.available() or not self._write_api:
            return 0

        # R5-G-04: 使用同步锁防止force_sync与_sync_loop并发读取同一批未同步记录导致重复写入
        async with self._sync_lock:
            config = get_config()
            batch_size = config.influxdb.sync_batch_size

            try:
                # FIXED-P1: 传入_last_uploaded_max_id作为偏移，跳过已上传但sync_completed失败的记录，防止重复上传
                min_id = self._last_uploaded_max_id + 1 if self._last_uploaded_max_id > 0 else None
                records = await self._sqlite_ts.get_unsynced_records(limit=batch_size, min_id=min_id)
                if not records:
                    return 0

                points = []
                max_id = 0
                for rec in records:
                    max_id = max(max_id, rec["id"])
                    value = rec.get("value")
                    if value is None:
                        continue

                    import math

                    try:
                        float_val = float(value)
                        if math.isnan(float_val) or math.isinf(float_val):
                            continue
                    except (ValueError, TypeError):
                        continue

                    p = (
                        Point(rec.get("measurement", "device_points"))
                        .tag("device_id", rec.get("device_id", ""))
                        .tag("point_name", rec.get("point_name", ""))
                        .tag("quality", rec.get("quality", "good"))
                        .field("value", float_val)
                    )

                    timestamp_ns = rec.get("timestamp_ns")
                    if timestamp_ns:
                        ts = datetime.fromtimestamp(timestamp_ns / 1e9, tz=UTC)
                        p = p.time(ts)

                    points.append(p)

                if points:
                    # R5-G-12: 复用 connect() 中创建的 _sync_write_api，避免每次同步批次创建新实例
                    if self._sync_write_api is None:
                        # 防御性兜底：连接恢复路径未重建时按需创建
                        self._sync_write_api = self._client.write_api(write_options=SYNCHRONOUS)
                    try:
                        await asyncio.to_thread(self._sync_write_api.write, bucket=self._bucket, record=points)
                    except Exception as write_err:
                        logger.error("增量同步写入InfluxDB失败，保留SQLite源数据: %s", write_err)
                        self._last_uploaded_max_id = max(self._last_uploaded_max_id, max_id)
                        return 0
                    # FIXED-ATOMIC-SYNC: Use atomic sync_completed() instead of separate
                    # mark_synced() and delete_synced_records() calls
                    sync_ok = await self._sqlite_ts.sync_completed(max_id)
                    if sync_ok:
                        self._last_uploaded_max_id = 0  # FIXED-P1: 同步成功后重置偏移保护
                        self._stats_sync_success += len(points)
                        logger.info(
                            "增量同步: %d条数据已同步到InfluxDB (max_id=%d)",
                            len(points),
                            max_id,
                        )
                    else:
                        # FIXED-P1: sync_completed失败时记录已上传max_id，防止下次重复上传
                        self._last_uploaded_max_id = max(self._last_uploaded_max_id, max_id)
                        logger.warning(
                            "增量同步: SQLite原子同步失败，数据将在下次重试 (max_id=%d, last_uploaded_max_id=%d)",
                            max_id,
                            self._last_uploaded_max_id,
                        )
                    return len(points)

                # 即使没有有效points也标记已同步，避免重复处理
                # FIXED-ATOMIC-SYNC: Use atomic sync_completed() for consistency
                await self._sqlite_ts.sync_completed(max_id)
                self._last_uploaded_max_id = 0
                return 0
            except Exception as e:
                self._stats_sync_fail += 1
                logger.error("增量同步批次失败: %s", e)
                return 0

    async def get_fallback_stats(self) -> dict:
        """获取降级存储统计信息"""
        base_stats = {
            "total_records": 0,
            "db_size_bytes": 0,
            "oldest_record": None,
            "newest_record": None,
            "unsynced_count": 0,
        }
        if self._sqlite_ts:
            base_stats = await self._sqlite_ts.get_stats()
        # R11-DRV-04: _fallback_mode 通过 await fallback_mode() 加锁读取，避免无锁读取竞态
        fallback_mode = await self.fallback_mode()
        base_stats.update(
            {
                "fallback_mode": fallback_mode,
                "fallback_count": self._stats_fallback_count,
                "cached_count": self._stats_cached_count,
                "sync_success_count": self._stats_sync_success,
                "sync_fail_count": self._stats_sync_fail,
            }
        )
        return base_stats

    async def _get_probe_client(self):
        """R11-DRV-08: 懒初始化并复用网络探测 HTTP client，避免每次探测新建连接"""
        import httpx

        if self._probe_client is None or self._probe_client.is_closed:
            self._probe_client = httpx.AsyncClient(timeout=5)
        return self._probe_client

    async def check_network_status(self) -> str:
        """检测网络状态: online/offline/weak

        通过多DNS探测 + HTTP心跳区分真实离线与弱网状态。
        """
        # FIXED(一般): 原问题-同步socket.create_connection阻塞事件循环;
        # 修复-用asyncio.to_thread包装同步DNS探测
        dns_servers = ["8.8.8.8", "114.114.114.114", "1.1.1.1"]
        success_count = await asyncio.to_thread(self._check_dns_connectivity_sync, dns_servers)

        if success_count >= 2:
            # Also check HTTP heartbeat
            try:
                # R11-DRV-08: 复用 probe client，避免每次新建 httpx.AsyncClient
                # R11-DRV-13: 探测 URL 改为从配置读取，原硬编码 baidu.com 海外部署不可用
                probe_url = get_config().influxdb.network_probe_url
                client = await self._get_probe_client()
                resp = await client.get(probe_url)
                if resp.status_code == 200:
                    return "online"
            except Exception as e:
                logger.warning(
                    "Network check failed, returning weak: %s", e
                )  # FIXED-P1: 原问题-网络检查异常返回weak无日志
                return "weak"
            return "weak"
        elif success_count == 1:
            return "weak"
        else:
            return "offline"

    @staticmethod
    def _check_dns_connectivity_sync(dns_servers: list[str]) -> int:
        """Synchronous DNS connectivity probe (runs in worker thread)."""
        import socket

        success_count = 0
        for dns in dns_servers:
            try:
                sock = socket.create_connection((dns, 53), timeout=3)
                sock.close()
                success_count += 1
            except (TimeoutError, OSError):
                pass
        return success_count

    async def get_cache_stats(self) -> dict:
        """获取缓存统计"""
        # R11-DRV-04: _fallback_mode 通过 await fallback_mode() 加锁读取，避免无锁读取竞态
        fallback_mode = await self.fallback_mode()
        if not self._sqlite_ts:
            return {
                "count": 0,
                "size_mb": 0,
                "last_sync": None,
                "pending": 0,
                "fallback_mode": fallback_mode,
                "fallback_count": self._stats_fallback_count,
            }
        stats = await self._sqlite_ts.get_stats()
        return {
            "count": stats.get("total_records", 0),
            "size_mb": round(stats.get("db_size_bytes", 0) / (1024 * 1024), 2),
            "oldest_record": stats.get("oldest_record"),
            "newest_record": stats.get("newest_record"),
            "last_sync": None,
            "pending": stats.get("unsynced_count", 0),
            "fallback_mode": fallback_mode,
            "fallback_count": self._stats_fallback_count,
            "cached_count": self._stats_cached_count,
            "sync_success_count": self._stats_sync_success,
            "sync_fail_count": self._stats_sync_fail,
        }

    async def force_sync(self) -> int:
        """强制同步所有离线数据到InfluxDB"""
        total_synced = 0
        while True:
            count = await self._sync_from_sqlite()
            total_synced += count
            if count == 0:
                break
        logger.info("强制离线同步完成: 共同步%d条数据", total_synced)
        return total_synced

    async def clear_cache(self) -> int:
        """清空本地缓存"""
        if self._sqlite_ts:
            return await self._sqlite_ts.clear_all()
        return 0

    async def _sync_from_sqlite(self) -> int:
        """从SQLite同步未同步数据到InfluxDB（供force_sync调用）"""
        return await self._sync_batch()
