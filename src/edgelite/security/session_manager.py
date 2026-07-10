"""用户会话管理 — 持久化会话状态消除重启 fail-open 窗口。

FIXED(严重): 原 session_manager 重启后内存状态丢失，导致并发登录控制
(LP-09) 和 token 撤销机制双重失效；修复-会话状态持久化到 SQLite
user_sessions 表，启动时 restore_sessions() 恢复活跃会话到内存 [2026-06-29]

设计:
- 内存: dict[user_id -> set[jti]] + threading.Lock
- 持久化: SQLite user_sessions 表 (通过 db.db_path 直写, 避免与 aiosqlite 冲突)
- fail-open: is_session_active 在用户无记录时返回 True (避免重启后全失效)
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

# 内存中的活跃会话: user_id -> set of jti
_active_sessions: dict[str, set[str]] = {}
_lock = threading.Lock()

# 默认会话 TTL (24h)
_DEFAULT_SESSION_TTL = 86400.0


def _get_db_path() -> str | None:
    """获取 SQLite 数据库路径 (best-effort)。"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        return getattr(db, "db_path", None) if db else None
    except Exception as e:  # noqa: BLE001 - 启动早期/测试环境无 app_state 时降级内存模式
        # FIXED: [P0 异常静默吞没] 会话持久化降级时需可观测，便于排查会话丢失 [2026-06-30]
        logger.debug("SessionManager _get_db_path fallback to memory mode: %s", e)
        return None


def _ensure_table(conn: sqlite3.Connection) -> None:
    """确保 user_sessions 表存在。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id VARCHAR(64) NOT NULL,
            jti VARCHAR(64) PRIMARY KEY,
            expires_at REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at)")


def register_session(user_id: str, jti: str, expires_at: float | None = None) -> None:
    """注册新会话到内存和 SQLite。

    FIXED-P0 (并发安全#5): SQLite 先 → 内存后，确保持久化成功后才更新内存。
    原问题: 内存先 add → SQLite 后 INSERT，SQLite 失败则内存已有 session 但 DB 没有，
            进程崩溃后 restore_sessions 从 SQLite 恢复 → session 丢失 →
            is_session_active fail-open 返回 True (会话绕过并发登录控制)。
    修复: 先执行 SQLite INSERT，成功后才更新内存；失败则 return 不更新内存。

    Args:
        user_id: 用户 ID
        jti: JWT Token ID
        expires_at: 过期时间戳 (默认 24h 后)
    """
    if not user_id or not jti:
        return
    exp = expires_at or (time.time() + _DEFAULT_SESSION_TTL)

    # SQLite 先: 持久化成功后才更新内存
    db_path = _get_db_path()
    if db_path:
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            try:
                _ensure_table(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO user_sessions (user_id, jti, expires_at) VALUES (?, ?, ?)",
                    (user_id, jti, exp),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to persist session for user %s: %s", user_id, e)
            return  # SQLite 失败则不更新内存，保持内存与 SQLite 一致

    # 内存后: SQLite 成功后才更新内存
    with _lock:
        if user_id not in _active_sessions:
            _active_sessions[user_id] = set()
        _active_sessions[user_id].add(jti)


def remove_session(user_id: str, jti: str) -> None:
    """移除指定会话。

    FIXED-P0 (并发安全#5): SQLite 先 → 内存后，确保持久化成功后才更新内存。
    原问题: 内存先 discard → SQLite 后 DELETE，SQLite 失败则内存已删除但 DB 仍有，
            进程崩溃后 restore_sessions 从 SQLite 恢复 → zombie session 复活 (安全漏洞)。
    修复: 先执行 SQLite DELETE，成功后才更新内存；失败则 return 不更新内存
          (session 仍活跃，用户需重试 logout，但内存与 SQLite 保持一致)。

    Args:
        user_id: 用户 ID
        jti: JWT Token ID
    """
    if not user_id or not jti:
        return

    # SQLite 先: 持久化成功后才更新内存
    db_path = _get_db_path()
    if db_path:
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            try:
                _ensure_table(conn)
                conn.execute("DELETE FROM user_sessions WHERE jti = ?", (jti,))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to remove session jti=%s: %s", jti, e)
            return  # SQLite 失败则不更新内存，保持 session 活跃 (一致性优先)

    # 内存后: SQLite 成功后才更新内存
    with _lock:
        sessions = _active_sessions.get(user_id)
        if sessions is not None:
            sessions.discard(jti)
            if not sessions:
                _active_sessions.pop(user_id, None)


def is_session_active(user_id: str, jti: str) -> bool:
    """检查会话是否活跃。

    fail-open 策略: 用户无活跃 session 记录 (如重启后未恢复) 时返回 True，
    避免重启后所有 token 失效。restore_sessions() 可消除此窗口。

    Args:
        user_id: 用户 ID
        jti: JWT Token ID

    Returns:
        会话是否活跃 (无记录时返回 True)
    """
    with _lock:
        sessions = _active_sessions.get(user_id)
        if sessions is None:
            return True  # fail-open
        return jti in sessions


def clear_user_sessions(user_id: str) -> list[str]:
    """清除用户所有会话。

    FIXED-P0 (并发安全#5): SQLite 先 → 内存后，确保持久化成功后才更新内存。
    原问题: 内存先 pop → SQLite 后 DELETE，SQLite 失败则内存已清空但 DB 仍有，
            进程崩溃后 restore_sessions 从 SQLite 恢复 → zombie sessions 复活 (安全漏洞)。
    修复: 先读取 jti 快照 → SQLite DELETE → 成功后才从内存移除；失败则返回空列表。
          内存移除时只 discard 已清除的 jti，保留窗口期内新注册的 session。

    Args:
        user_id: 用户 ID

    Returns:
        被清除的 jti 列表 (SQLite 失败时返回空列表)
    """
    if not user_id:
        return []

    # 读取 jti 快照 (仅读不删，保持锁内最小操作)
    with _lock:
        sessions = _active_sessions.get(user_id)
        jtis = list(sessions) if sessions else []

    if not jtis:
        return []

    # SQLite 先: 持久化成功后才更新内存
    db_path = _get_db_path()
    if db_path:
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            try:
                _ensure_table(conn)
                conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to clear sessions for user %s: %s", user_id, e)
            return []  # SQLite 失败则不更新内存，返回空列表

    # 内存后: SQLite 成功后才移除 (只 discard 快照中的 jti，保留窗口期新注册的 session)
    with _lock:
        sessions = _active_sessions.get(user_id)
        if sessions is not None:
            for jti in jtis:
                sessions.discard(jti)
            if not sessions:
                _active_sessions.pop(user_id, None)

    return jtis


async def revoke_old_sessions(user_id: str, new_jtis: list[str]) -> None:
    """撤销用户旧会话，保留 new_jtis 中的新会话。

    LP-09 并发登录控制: 新登录时撤销旧会话，防止并发会话。

    FIXED-P0 (并发安全#5): SQLite 先 → 内存后，确保持久化成功后才更新内存。
    原问题 1: 内存先替换 → SQLite 后 DELETE，SQLite 失败则内存已替换但 DB 仍有旧 session，
              进程崩溃后 restore_sessions 从 SQLite 恢复 → zombie sessions 复活 (安全漏洞)。
    原问题 2: 旧代码仅 DELETE 旧 session 到 SQLite，不 INSERT 新 session 到 SQLite，
              导致新 session 仅存在于内存，进程崩溃后 restore_sessions 从 SQLite 恢复 →
              新 session 丢失 → is_session_active fail-open 返回 True (绕过并发登录控制)。
    修复: SQLite 内原子完成 DELETE 旧 + INSERT 新 → 成功后才更新内存；
          失败则 return 不更新内存 (旧 session 仍活跃，用户需重试登录)。

    Args:
        user_id: 用户 ID
        new_jtis: 需要保留的新 jti 列表
    """
    if not user_id:
        return

    new_set = set(new_jtis)

    # 读取旧会话快照 (仅读不删，计算 to_revoke)
    with _lock:
        old_sessions = set(_active_sessions.get(user_id, set()))
        to_revoke = old_sessions - new_set

    # SQLite 先: 原子完成 DELETE 旧 + INSERT 新
    db_path = _get_db_path()
    if db_path:
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            try:
                _ensure_table(conn)
                # DELETE 旧会话
                for jti in to_revoke:
                    conn.execute("DELETE FROM user_sessions WHERE jti = ?", (jti,))
                # INSERT 新会话 (修复原问题2: 新 session 也需持久化)
                exp = time.time() + _DEFAULT_SESSION_TTL
                for jti in new_set:
                    conn.execute(
                        "INSERT OR REPLACE INTO user_sessions (user_id, jti, expires_at) VALUES (?, ?, ?)",
                        (user_id, jti, exp),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to revoke old sessions for user %s: %s", user_id, e)
            return  # SQLite 失败则不更新内存，保持旧 session 活跃 (一致性优先)

    # 内存后: SQLite 成功后才更新 (用新会话集替换旧会话集)
    with _lock:
        if new_set:
            _active_sessions[user_id] = new_set
        else:
            _active_sessions.pop(user_id, None)


def restore_sessions() -> int:
    """从 SQLite 恢复活跃会话到内存 (启动时调用)。

    清理过期会话并加载未过期会话到内存。

    Returns:
        恢复的活跃会话数量
    """
    db_path = _get_db_path()
    if not db_path:
        logger.warning("Database not available, cannot restore sessions")
        return 0

    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            _ensure_table(conn)
            now = time.time()
            # 清理过期会话
            conn.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now,))
            # 加载活跃会话
            rows = conn.execute(
                "SELECT user_id, jti FROM user_sessions WHERE expires_at > ?",
                (now,),
            ).fetchall()
            conn.commit()
        finally:
            conn.close()

        with _lock:
            for user_id, jti in rows:
                if user_id not in _active_sessions:
                    _active_sessions[user_id] = set()
                _active_sessions[user_id].add(jti)

        logger.info("Restored %d active sessions from SQLite", len(rows))
        return len(rows)
    except Exception as e:
        logger.warning("Failed to restore sessions: %s", e)
        return 0


def get_active_session_count() -> int:
    """获取当前活跃会话总数 (监控用)。"""
    with _lock:
        return sum(len(sessions) for sessions in _active_sessions.values())
