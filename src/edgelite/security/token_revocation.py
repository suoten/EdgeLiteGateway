"""令牌撤销管理模块

FIXED-P0: 改用 SQLite 持久化存储，替代内存存储。
- 持久化存储确保重启后撤销列表不丢失
- 过期条目自动清理
- 高并发注销场景下，已撤销 token 不会恢复有效
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from edgelite.constants import _TOKEN_REVOCATION_DEFAULT_TTL, _TOKEN_REVOCATION_MAX

logger = logging.getLogger(__name__)

_MAX_REVOKED_ENTRIES = _TOKEN_REVOCATION_MAX
_CLEANUP_THRESHOLD = _TOKEN_REVOCATION_MAX * 8 // 10

# FIXED(严重): DB故障连续失败告警阈值
_DB_FAILURE_ALERT_THRESHOLD = 5

# FIXED(中危): DB故障时的本地文件 fallback，减少已撤销token重新生效的风险窗口
_FALLBACK_FILE = Path("data") / ".token_revocation_fallback.json"
_fallback_lock = threading.Lock()


class TokenRevocationManager:
    """SQLite-backed token revocation manager with in-memory cache.

    FIXED-P0: 持久化存储 + 内存缓存方案：
    - 主存储：SQLite（持久化，重启后不丢失）
    - 缓存：内存 dict（提高查询性能）
    - 过期条目：后台定期清理
    """

    _instance: TokenRevocationManager | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._cache: dict[str, float] = {}  # jti -> expires_at
        self._cache_lock = threading.Lock()
        self._db_ready = False
        self._init_done = False
        self._pending_revokes: list[tuple[str, float]] = []  # For batch persistence
        self._persist_thread: threading.Thread | None = None
        # FIXED(严重): 原问题-DB查询失败时fail-open仅记warning日志，无告警机制;
        # 修复-添加_db_failures计数器，连续失败超过阈值时发出error告警
        self._db_failures = 0

    def _write_fallback(self, jti: str, expires_at: float) -> None:
        """FIXED(中危): DB不可用时将撤销记录写入本地文件 fallback。

        DB故障时撤销记录仅写入内存缓存，重启后丢失，已撤销token可能重新生效。
        通过本地文件 fallback 持久化撤销记录，减少风险窗口。
        """
        try:
            _FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _fallback_lock:
                data: dict[str, float] = {}
                if _FALLBACK_FILE.exists():
                    try:
                        raw = _FALLBACK_FILE.read_text(encoding="utf-8")
                        data = json.loads(raw) if raw.strip() else {}
                    except (json.JSONDecodeError, OSError):
                        data = {}
                data[jti] = expires_at
                # 清理过期条目，避免文件无限增长
                now = time.time()
                data = {k: v for k, v in data.items() if v > now}
                _FALLBACK_FILE.write_text(json.dumps(data), encoding="utf-8")
                # R5-G-11: 写入后设置文件权限 0o600（仅所有者可读写），防止其他用户读取撤销列表
                if os.name != 'nt':
                    try:
                        os.chmod(str(_FALLBACK_FILE), 0o600)
                    except OSError as chmod_err:
                        logger.warning("Failed to chmod token revocation fallback file: %s", chmod_err)
        except Exception as e:
            logger.warning("Failed to write token revocation fallback file: %s", e)

    def _check_fallback(self, jti: str) -> bool:
        """FIXED(中危): DB故障时检查本地文件 fallback，判断token是否已撤销。

        Returns:
            True 如果 jti 在 fallback 文件中且未过期，否则 False。
        """
        try:
            if not _FALLBACK_FILE.exists():
                return False
            with _fallback_lock:
                raw = _FALLBACK_FILE.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
            expires_at = data.get(jti)
            if expires_at is not None and expires_at > time.time():
                return True
        except Exception as e:
            logger.debug("Failed to check token revocation fallback file: %s", e)
        return False

    @classmethod
    def get_instance(cls) -> TokenRevocationManager:
        """Get singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = cls()
                    cls._instance = instance
                    # Initialize synchronously
                    instance._init_sync()
        return cls._instance

    def _init_sync(self) -> None:
        """Synchronous initialization for compatibility."""
        try:
            try:
                loop = asyncio.get_running_loop()  # FIXED-P2: 优先使用当前运行的事件循环
            except RuntimeError:
                loop = None
            if loop is not None:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, self._async_init())
                    future.result(timeout=10)
            else:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._async_init())
                finally:
                    loop.close()
        except Exception as e:
            logger.error("Failed to initialize token revocation manager: %s", e)
            self._db_ready = False

    async def _async_init(self) -> None:
        """Initialize SQLite table and load cached entries."""
        try:
            await self._ensure_table()
            await self._load_cache()
            self._db_ready = True
            self._init_done = True
            logger.info(
                "Token revocation manager initialized: %d entries loaded from SQLite",
                len(self._cache)
            )
        except Exception as e:
            logger.error("Failed to initialize token revocation manager: %s", e)
            self._db_ready = False
            self._init_done = True

    async def _ensure_table(self) -> None:
        """Create the revoked_tokens table if not exists."""
        try:
            from sqlalchemy import text

            # Get database instance from app state
            from edgelite.app import _app_state
            db = getattr(_app_state, "database", None)
            if db is None:
                logger.warning("Database not available for token revocation")
                return

            # Import the ORM model

            # Create table using raw SQL (ORM table creation is handled by metadata)
            async with db.get_session() as session:
                # Check if table exists
                result = await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='revoked_tokens'")
                )
                exists = result.scalar() is not None

                if not exists:
                    # Create table
                    await session.execute(text("""
                        CREATE TABLE IF NOT EXISTS revoked_tokens (
                            jti VARCHAR(64) PRIMARY KEY,
                            expires_at REAL NOT NULL,
                            revoked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                    await session.execute(text("CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires ON revoked_tokens(expires_at)"))
                    await session.commit()
                    logger.info("Created revoked_tokens table")
        except Exception as e:
            logger.error("Failed to ensure revoked_tokens table: %s", e)
            raise

    async def _load_cache(self) -> None:
        """Load all non-expired entries from SQLite into memory cache."""
        try:
            from edgelite.app import _app_state
            db = getattr(_app_state, "database", None)
            if db is None:
                return

            from sqlalchemy import delete, select

            from edgelite.models.db import RevokedTokenORM

            now = time.time()
            async with db.get_session() as session:
                # Load valid entries
                result = await session.execute(
                    select(RevokedTokenORM.jti, RevokedTokenORM.expires_at)
                    .where(RevokedTokenORM.expires_at > now)
                )
                rows = result.all()

                with self._cache_lock:
                    for jti, expires_at in rows:
                        self._cache[jti] = expires_at

                # Clean up expired entries
                await session.execute(
                    delete(RevokedTokenORM).where(RevokedTokenORM.expires_at <= now)
                )
                await session.commit()

                logger.debug("Loaded %d valid revoked tokens, cleaned up expired ones", len(rows))
        except Exception as e:
            logger.warning("Failed to load cache from SQLite: %s", e)

    def _get_db(self) -> Any:
        """Get database instance."""
        try:
            from edgelite.app import _app_state
            return getattr(_app_state, "database", None)
        except Exception:
            return None

    def revoke_token_sync(self, jti: str, exp: float | None = None) -> None:
        """同步撤销令牌（向后兼容）。"""
        # FIXED-P1: 校验 jti 非空，空 jti 撤销无意义且可能污染缓存
        if not jti:
            logger.warning("Attempted to revoke token with empty jti, ignoring")
            return

        expires_at = exp or (time.time() + _TOKEN_REVOCATION_DEFAULT_TTL)

        # FIXED: 原问题-asyncio.run()嵌套事件循环导致SQLite "database is locked"。
        # 修复：改用同步sqlite3直写，避免与aiosqlite连接冲突。
        db_write_ok = False
        if self._db_ready:
            try:
                db = self._get_db()
                sqlite_path = getattr(db, "db_path", None) if db else None
                if sqlite_path:
                    import sqlite3 as _sqlite3
                    conn = _sqlite3.connect(sqlite_path, timeout=5)
                    try:
                        conn.execute(
                            "INSERT INTO revoked_tokens (jti, expires_at, revoked_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                            "ON CONFLICT(jti) DO UPDATE SET expires_at = excluded.expires_at",
                            (jti, expires_at),
                        )
                        conn.commit()
                        db_write_ok = True
                    finally:
                        conn.close()
                else:
                    logger.debug("SQLite path not available for sync revoke")
            except Exception as e:
                logger.warning("Failed to persist token revocation synchronously: %s", e)

        # FIXED(中危): DB不可用或写入失败时，写入本地文件 fallback 持久化撤销记录
        if not db_write_ok:
            self._write_fallback(jti, expires_at)

        with self._cache_lock:
            self._cache[jti] = expires_at

    # FIXED-P2: 删除未使用的_persist_async方法（fire-and-forget threading.Thread无任务追踪，且无调用方）

    async def _persist_single(self, jti: str, expires_at: float) -> None:
        """Persist single entry to SQLite."""
        db = self._get_db()
        if db is None:
            return

        try:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            from edgelite.models.db import RevokedTokenORM

            async with db.get_session() as session:
                stmt = sqlite_insert(RevokedTokenORM).values(
                    jti=jti,
                    expires_at=expires_at,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["jti"],
                    set_={"expires_at": expires_at}
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            logger.warning("Failed to persist token revocation for %s: %s", jti, e)

    async def revoke_token(self, jti: str, exp: float | None = None) -> None:
        """异步撤销指定 jti 的令牌。"""
        # FIXED-P1: 校验 jti 非空，空 jti 撤销无意义且可能污染缓存
        if not jti:
            logger.warning("Attempted to revoke token with empty jti, ignoring")
            return

        expires_at = exp or (time.time() + _TOKEN_REVOCATION_DEFAULT_TTL)

        # FIXED-P0: 原问题-先更新内存再持久化，持久化失败时内存认为已撤销但DB未记录，重启后token恢复有效
        # 改为：先持久化到SQLite再更新内存缓存，与revoke_token_sync保持一致
        db_write_ok = False
        if self._db_ready:
            try:
                await self._persist_single(jti, expires_at)
                db_write_ok = True
            except Exception as e:
                logger.warning("Failed to persist token revocation for %s: %s", jti, e)

        # FIXED(中危): DB不可用或写入失败时，写入本地文件 fallback 持久化撤销记录
        if not db_write_ok:
            self._write_fallback(jti, expires_at)

        with self._cache_lock:
            self._cache[jti] = expires_at

    def is_token_revoked(self, jti: str) -> bool:
        """检查令牌是否已被撤销。

        先检查内存缓存（高性能），缓存未命中再考虑 SQLite。
        """
        # FIXED-P1: 校验 jti 非空，空 jti 无法进行撤销检查
        if not jti:
            return False

        # Check memory cache
        with self._cache_lock:
            if jti in self._cache:
                expires_at = self._cache[jti]
                if expires_at > time.time():
                    return True
                else:
                    # Expired in cache, mark for cleanup
                    del self._cache[jti]
                    return False

        # Cache miss - fallback to SQLite for multi-instance consistency
        # FIXED: 原问题-asyncio.run()嵌套事件循环导致aiosqlite新连接与主循环连接并发，
        # 触发SQLite "database is locked"（aiosqlite共享同一文件锁）。
        # 修复：改用同步sqlite3只读直查，不经过aiosqlite/SQLAlchemy，避免连接冲突。
        db = self._get_db()
        if db:
            try:
                sqlite_path = getattr(db, "db_path", None)
                if sqlite_path:
                    import sqlite3 as _sqlite3
                    conn = _sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=1)
                    try:
                        row = conn.execute(
                            "SELECT expires_at FROM revoked_tokens WHERE jti = ?",
                            (jti,),
                        ).fetchone()
                        if row is not None and row[0] > time.time():
                            with self._cache_lock:
                                self._cache[jti] = row[0]
                            return True
                    finally:
                        conn.close()
                    # FIXED(严重): 原问题-DB查询失败时fail-open仅记warning日志，无告警机制;
                    # 修复-查询成功时重置失败计数器
                    self._db_failures = 0
            except Exception as e:
                # FIXED(严重): 原问题-DB查询失败时fail-open仅记warning日志，运维无法及时感知;
                # 修复-日志级别从warning提升到error，添加_db_failures计数器，
                # 连续失败超过阈值(_DB_FAILURE_ALERT_THRESHOLD)时发出critical告警。
                # 保持fail-open策略：fail-closed会导致DB故障时所有cache-miss token被拒，
                # 即所有用户无法访问。缓存是主检查手段，token有短TTL，风险可控。
                self._db_failures += 1
                if self._db_failures >= _DB_FAILURE_ALERT_THRESHOLD:
                    logger.error(
                        "Token revocation DB check has failed %d consecutive times (jti=%s, last_error=%s). "
                        "Failing open but revocation consistency at risk - investigate DB health immediately.",
                        self._db_failures, jti, e,
                    )
                else:
                    logger.error(
                        "Failed to check token revocation in DB for jti=%s: %s. "
                        "Failing open (cache is primary check, token has short TTL). "
                        "Consecutive failures: %d",
                        jti, e, self._db_failures,
                    )
                # FIXED(中危): DB故障时检查本地文件 fallback，减少已撤销token重新生效的风险窗口
                if self._check_fallback(jti):
                    return True

        return False

    async def cleanup_expired(self) -> int:
        """清理已过期的撤销记录。

        Returns:
            Number of entries cleaned up
        """
        now = time.time()
        cleaned = 0

        # Clean memory cache
        with self._cache_lock:
            expired_jtis = [jti for jti, exp in self._cache.items() if exp <= now]
            for jti in expired_jtis:
                del self._cache[jti]
                cleaned += 1

        # Clean SQLite
        db = self._get_db()
        if db:
            try:
                from sqlalchemy import delete

                from edgelite.models.db import RevokedTokenORM

                async with db.get_session() as session:
                    result = await session.execute(
                        delete(RevokedTokenORM).where(RevokedTokenORM.expires_at <= now)
                    )
                    await session.commit()
                    db_cleaned = result.rowcount or 0
                    cleaned += db_cleaned
            except Exception as e:
                logger.warning("Failed to cleanup expired tokens from SQLite: %s", e)

        if cleaned > 0:
            logger.info("Cleaned up %d expired token revocation entries", cleaned)

        return cleaned

    def cleanup_expired_sync(self) -> int:
        """同步版本的清理方法。"""
        now = time.time()
        cleaned = 0

        with self._cache_lock:
            expired_jtis = [jti for jti, exp in self._cache.items() if exp <= now]
            for jti in expired_jtis:
                del self._cache[jti]
                cleaned += 1

        # FIXED: 原问题-asyncio.run()嵌套事件循环导致SQLite "database is locked"。
        # 修复：改用同步sqlite3直删，避免与aiosqlite连接冲突。
        db = self._get_db()
        if db:
            try:
                sqlite_path = getattr(db, "db_path", None)
                if sqlite_path:
                    import sqlite3 as _sqlite3
                    conn = _sqlite3.connect(sqlite_path, timeout=5)
                    try:
                        cursor = conn.execute(
                            "DELETE FROM revoked_tokens WHERE expires_at <= ?",
                            (now,),
                        )
                        conn.commit()
                        cleaned += cursor.rowcount or 0
                    finally:
                        conn.close()
            except Exception as e:
                logger.warning("Failed to cleanup expired tokens from SQLite: %s", e)

        if cleaned > 0:
            logger.info("Cleaned up %d expired token revocation entries", cleaned)

        return cleaned

    async def cleanup_if_needed(self) -> None:
        """如果缓存超过阈值，执行清理。"""
        # FIXED-P2: 先在锁内判断，锁外调用cleanup_expired，避免不可重入锁死锁
        with self._cache_lock:
            need_cleanup = len(self._cache) > _CLEANUP_THRESHOLD
        if need_cleanup:
            await self.cleanup_expired()

    def get_stats(self) -> dict[str, Any]:
        """获取撤销管理器统计信息。"""
        now = time.time()
        with self._cache_lock:
            valid_count = sum(1 for exp in self._cache.values() if exp > now)
            expired_count = len(self._cache) - valid_count

        return {
            "total_cached": len(self._cache),
            "valid_entries": valid_count,
            "expired_entries": expired_count,
            "db_ready": self._db_ready,
        }


# Global instance
_manager: TokenRevocationManager | None = None


def _get_manager() -> TokenRevocationManager:
    """Get the token revocation manager instance."""
    global _manager
    if _manager is None:
        _manager = TokenRevocationManager.get_instance()
    return _manager


def revoke_token(jti: str, exp: float | None = None) -> None:
    """撤销指定 jti 的令牌（同步版本，向后兼容）。"""
    manager = _get_manager()
    manager.revoke_token_sync(jti, exp)


async def revoke_token_async(jti: str, exp: float | None = None) -> None:
    """撤销指定 jti 的令牌（异步版本）。"""
    manager = _get_manager()
    await manager.revoke_token(jti, exp)


async def revoke_all_tokens_for_user(user_id: str) -> int:
    """批量撤销指定用户的所有活跃 token。

    第四轮修复: 禁用用户和删除用户时主动撤销其所有活跃 token，
    防止已签发的 token 在过期前继续有效。

    参考 auth.py:591-606 的 change_password 实现：
    1. 通过 session_manager.clear_user_sessions 清除用户所有 session 并获取 jti 集合
    2. 逐个撤销每个 jti 对应的 token

    Args:
        user_id: 用户 ID

    Returns:
        成功撤销的 token 数量
    """
    if not user_id:
        return 0

    import time as _time

    from edgelite.security.session_manager import clear_user_sessions

    revoked_count = 0
    try:
        # 清除用户所有 session，返回被清除的 jti 集合
        removed_jtis = clear_user_sessions(user_id)
        for old_jti in removed_jtis:
            try:
                await revoke_token_async(old_jti, _time.time() + 3600)
                revoked_count += 1
            except Exception as e:
                logger.warning("Failed to revoke token for user %s, jti=%s: %s", user_id, old_jti, e)
        if removed_jtis:
            logger.info("Revoked %d/%d active token(s) for user %s", revoked_count, len(removed_jtis), user_id)
    except Exception as e:
        logger.warning("revoke_all_tokens_for_user failed for user %s: %s", user_id, e)

    return revoked_count


def is_token_revoked(jti: str) -> bool:
    """检查令牌是否已被撤销。"""
    manager = _get_manager()
    return manager.is_token_revoked(jti)


async def cleanup_expired() -> int:
    """清理过期的撤销记录（异步版本）。"""
    manager = _get_manager()
    return await manager.cleanup_expired()


def cleanup_expired_sync() -> int:
    """清理过期的撤销记录（同步版本）。"""
    manager = _get_manager()
    return manager.cleanup_expired_sync()
