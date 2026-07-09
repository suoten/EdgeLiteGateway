"""SessionManager 原子持久化单元测试 (并发安全 #5)

覆盖 P0 修复: SQLite 先 → 内存后，确保持久化成功后才更新内存。

原问题:
  register_session / remove_session / clear_user_sessions / revoke_old_sessions
  均为 "内存先 → SQLite 后" 顺序，SQLite 失败则内存与 DB 不一致:
  - register: SQLite 失败 → 内存有 session, DB 没有 → 崩溃后 session 丢失
  - remove:   SQLite 失败 → 内存无 session, DB 有 → 崩溃后 zombie session 复活 (安全漏洞)
  - clear:    同 remove
  - revoke:   同 remove + 新 session 不持久化到 SQLite (原 bug, 仅内存有)

修复:
  1. SQLite 先执行 (INSERT/DELETE)
  2. SQLite 成功后才更新内存
  3. SQLite 失败则 return 不更新内存 (保持内存与 SQLite 一致)
  4. revoke_old_sessions 额外修复: 新 session 也 INSERT 到 SQLite
"""

from __future__ import annotations

import sqlite3
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, "src")

from edgelite.security import session_manager
from edgelite.security.session_manager import (
    clear_user_sessions,
    is_session_active,
    register_session,
    remove_session,
    restore_sessions,
    revoke_old_sessions,
)


# ════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_session_state():
    """每个测试前后清空模块级内存状态 (autouse)。"""
    with session_manager._lock:
        session_manager._active_sessions.clear()
    yield
    with session_manager._lock:
        session_manager._active_sessions.clear()


@pytest.fixture
def db_path(tmp_path):
    """临时 SQLite DB 路径，patch _get_db_path 返回该路径。

    预创建 user_sessions 表，确保测试 helper (_db_has_jti / _count_db_sessions)
    可在 sqlite3.connect 被 mock 失败时仍能查询 (mock 仅作用于 session_manager 模块)。
    """
    path = str(tmp_path / "test_sessions.db")
    conn = sqlite3.connect(path)
    try:
        session_manager._ensure_table(conn)
        conn.commit()
    finally:
        conn.close()
    with patch.object(session_manager, "_get_db_path", return_value=path):
        yield path


def _count_db_sessions(db_path: str, user_id: str | None = None) -> int:
    """查询 SQLite 中指定用户的 session 数量 (或全部)。"""
    conn = sqlite3.connect(db_path)
    try:
        if user_id:
            rows = conn.execute(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
        else:
            rows = conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()
        return rows[0]
    finally:
        conn.close()


def _db_has_jti(db_path: str, jti: str) -> bool:
    """查询 SQLite 中是否存在指定 jti。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE jti = ?", (jti,)
        ).fetchone()
        return rows[0] > 0
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════
# register_session: SQLite 先 → 内存后
# ════════════════════════════════════════════════════════════════════════


class TestRegisterSessionAtomicity:
    """register_session 原子持久化测试"""

    def test_register_persists_to_sqlite_then_memory(self, db_path):
        """正常路径: SQLite INSERT 成功 → 内存 add"""
        register_session("user1", "jti-1")

        # 内存有 session
        assert is_session_active("user1", "jti-1") is True
        # SQLite 也有 session
        assert _db_has_jti(db_path, "jti-1") is True

    def test_register_rollback_on_sqlite_failure(self, db_path):
        """SQLite 失败 → 内存不更新 (一致性优先)"""
        with patch(
            "edgelite.security.session_manager.sqlite3.connect",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            register_session("user1", "jti-1")

        # 内存不应有该 session (直接检查内存结构, 非 is_session_active 的 fail-open)
        with session_manager._lock:
            assert "user1" not in session_manager._active_sessions
        # SQLite 也不应有 (connect 失败)
        assert _db_has_jti(db_path, "jti-1") is False

    def test_register_memory_mode_no_db(self, tmp_path):
        """无 DB 路径 → 仅内存模式 (降级)"""
        with patch.object(session_manager, "_get_db_path", return_value=None):
            register_session("user1", "jti-1")

        assert is_session_active("user1", "jti-1") is True

    def test_register_empty_args_noop(self, db_path):
        """空 user_id/jti → 无操作"""
        register_session("", "jti-1")
        register_session("user1", "")
        with session_manager._lock:
            assert len(session_manager._active_sessions) == 0
        assert _count_db_sessions(db_path) == 0

    def test_register_multiple_sessions_same_user(self, db_path):
        """同一用户注册多个 session → 内存和 SQLite 都有"""
        register_session("user1", "jti-1")
        register_session("user1", "jti-2")

        assert is_session_active("user1", "jti-1") is True
        assert is_session_active("user1", "jti-2") is True
        assert _count_db_sessions(db_path, "user1") == 2


# ════════════════════════════════════════════════════════════════════════
# remove_session: SQLite 先 → 内存后
# ════════════════════════════════════════════════════════════════════════


class TestRemoveSessionAtomicity:
    """remove_session 原子持久化测试"""

    def test_remove_persists_to_sqlite_then_memory(self, db_path):
        """正常路径: register → remove → SQLite 和内存都不再有"""
        register_session("user1", "jti-1")
        remove_session("user1", "jti-1")

        # 内存不再有 (user1 无记录 → fail-open 返回 True, 需直接检查内存)
        with session_manager._lock:
            assert "user1" not in session_manager._active_sessions
        # SQLite 也不再有
        assert _db_has_jti(db_path, "jti-1") is False

    def test_remove_rollback_on_sqlite_failure(self, db_path):
        """SQLite DELETE 失败 → 内存不更新 (session 仍活跃, 一致性优先)"""
        register_session("user1", "jti-1")

        with patch(
            "edgelite.security.session_manager.sqlite3.connect",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            remove_session("user1", "jti-1")

        # 内存仍有该 session
        assert is_session_active("user1", "jti-1") is True
        # SQLite 也有 (未被删除)
        assert _db_has_jti(db_path, "jti-1") is True

    def test_remove_one_of_multiple_sessions(self, db_path):
        """移除多个 session 中的一个 → 其余仍在"""
        register_session("user1", "jti-1")
        register_session("user1", "jti-2")
        remove_session("user1", "jti-1")

        assert is_session_active("user1", "jti-1") is False
        assert is_session_active("user1", "jti-2") is True
        assert _count_db_sessions(db_path, "user1") == 1

    def test_remove_memory_mode_no_db(self, tmp_path):
        """无 DB 路径 → 仅内存模式"""
        with patch.object(session_manager, "_get_db_path", return_value=None):
            register_session("user1", "jti-1")
            remove_session("user1", "jti-1")

        with session_manager._lock:
            assert "user1" not in session_manager._active_sessions

    def test_remove_empty_args_noop(self, db_path):
        """空 user_id/jti → 无操作"""
        register_session("user1", "jti-1")
        remove_session("", "jti-1")
        remove_session("user1", "")

        assert is_session_active("user1", "jti-1") is True
        assert _count_db_sessions(db_path, "user1") == 1


# ════════════════════════════════════════════════════════════════════════
# clear_user_sessions: SQLite 先 → 内存后
# ════════════════════════════════════════════════════════════════════════


class TestClearUserSessionsAtomicity:
    """clear_user_sessions 原子持久化测试"""

    def test_clear_persists_to_sqlite_then_memory(self, db_path):
        """正常路径: SQLite DELETE 成功 → 内存移除, 返回 jti 列表"""
        register_session("user1", "jti-1")
        register_session("user1", "jti-2")

        jtis = clear_user_sessions("user1")

        assert set(jtis) == {"jti-1", "jti-2"}
        with session_manager._lock:
            assert "user1" not in session_manager._active_sessions
        assert _count_db_sessions(db_path, "user1") == 0

    def test_clear_rollback_on_sqlite_failure(self, db_path):
        """SQLite DELETE 失败 → 内存不更新, 返回空列表"""
        register_session("user1", "jti-1")

        with patch(
            "edgelite.security.session_manager.sqlite3.connect",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            jtis = clear_user_sessions("user1")

        assert jtis == []
        # 内存仍有 session
        assert is_session_active("user1", "jti-1") is True
        # SQLite 也有 (未被删除)
        assert _db_has_jti(db_path, "jti-1") is True

    def test_clear_empty_user_returns_empty(self, db_path):
        """空 user_id → 返回空列表"""
        assert clear_user_sessions("") == []

    def test_clear_no_sessions_returns_empty(self, db_path):
        """用户无 session → 返回空列表"""
        assert clear_user_sessions("nobody") == []

    def test_clear_does_not_affect_other_users(self, db_path):
        """清除用户 A 的 session 不影响用户 B"""
        register_session("userA", "jti-a")
        register_session("userB", "jti-b")

        jtis = clear_user_sessions("userA")

        assert jtis == ["jti-a"]
        assert is_session_active("userB", "jti-b") is True
        assert _count_db_sessions(db_path, "userB") == 1


# ════════════════════════════════════════════════════════════════════════
# revoke_old_sessions: SQLite 先 → 内存后 + 新 session 持久化
# ════════════════════════════════════════════════════════════════════════


class TestRevokeOldSessionsAtomicity:
    """revoke_old_sessions 原子持久化测试"""

    async def test_revoke_persists_to_sqlite_then_memory(self, db_path):
        """正常路径: SQLite DELETE 旧 + INSERT 新 → 内存替换"""
        register_session("user1", "old-jti")

        await revoke_old_sessions("user1", ["new-jti"])

        # 内存: 新 session 在, 旧 session 不在
        assert is_session_active("user1", "new-jti") is True
        with session_manager._lock:
            sessions = session_manager._active_sessions.get("user1", set())
            assert "old-jti" not in sessions
            assert "new-jti" in sessions

        # SQLite: 旧 session 已删除, 新 session 已插入
        assert _db_has_jti(db_path, "old-jti") is False
        assert _db_has_jti(db_path, "new-jti") is True

    async def test_revoke_rollback_on_sqlite_failure(self, db_path):
        """SQLite 失败 → 内存不更新 (旧 session 仍活跃, 一致性优先)"""
        register_session("user1", "old-jti")

        with patch(
            "edgelite.security.session_manager.sqlite3.connect",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            await revoke_old_sessions("user1", ["new-jti"])

        # 内存: 旧 session 仍在 (未替换)
        assert is_session_active("user1", "old-jti") is True
        with session_manager._lock:
            sessions = session_manager._active_sessions.get("user1", set())
            assert "old-jti" in sessions
            assert "new-jti" not in sessions

        # SQLite: 旧 session 仍在 (未删除), 新 session 未插入
        assert _db_has_jti(db_path, "old-jti") is True
        assert _db_has_jti(db_path, "new-jti") is False

    async def test_revoke_persists_new_jtis_to_sqlite(self, db_path):
        """FIXED bug 验证: 新 session 也 INSERT 到 SQLite (原 bug: 仅内存有)"""
        # 无旧 session, 仅注册新 session
        await revoke_old_sessions("user1", ["new-jti-1", "new-jti-2"])

        # SQLite 应有新 session (原 bug: SQLite 无新 session)
        assert _db_has_jti(db_path, "new-jti-1") is True
        assert _db_has_jti(db_path, "new-jti-2") is True

        # 内存也有
        assert is_session_active("user1", "new-jti-1") is True
        assert is_session_active("user1", "new-jti-2") is True

    async def test_revoke_no_old_sessions(self, db_path):
        """无旧 session → 仅 INSERT 新 session"""
        await revoke_old_sessions("user1", ["new-jti"])

        assert is_session_active("user1", "new-jti") is True
        assert _count_db_sessions(db_path, "user1") == 1

    async def test_revoke_empty_new_jtis_clears_all(self, db_path):
        """new_jtis 为空 → 撤销所有旧 session"""
        register_session("user1", "old-jti-1")
        register_session("user1", "old-jti-2")

        await revoke_old_sessions("user1", [])

        with session_manager._lock:
            assert "user1" not in session_manager._active_sessions
        assert _count_db_sessions(db_path, "user1") == 0

    async def test_revoke_empty_user_noop(self, db_path):
        """空 user_id → 无操作"""
        await revoke_old_sessions("", ["new-jti"])
        with session_manager._lock:
            assert len(session_manager._active_sessions) == 0


# ════════════════════════════════════════════════════════════════════════
# restore_sessions: 崩溃后恢复一致性
# ════════════════════════════════════════════════════════════════════════


class TestRestoreSessionsConsistency:
    """restore_sessions: 从 SQLite 恢复到内存的一致性验证

    模拟进程崩溃: 清空内存 → 从 SQLite 恢复 → 验证内存与 SQLite 一致。
    这是 "SQLite 先 → 内存后" 修复的核心价值: 崩溃后内存与 SQLite 保持一致。
    """

    def test_restore_after_register(self, db_path):
        """register 后崩溃 → 恢复后 session 在内存中"""
        register_session("user1", "jti-1")
        register_session("user2", "jti-2")

        # 模拟崩溃: 清空内存
        with session_manager._lock:
            session_manager._active_sessions.clear()

        count = restore_sessions()
        assert count == 2
        assert is_session_active("user1", "jti-1") is True
        assert is_session_active("user2", "jti-2") is True

    def test_restore_after_remove(self, db_path):
        """remove 后崩溃 → 被移除的 session 不恢复 (无 zombie)"""
        register_session("user1", "jti-1")
        register_session("user1", "jti-2")
        remove_session("user1", "jti-1")

        with session_manager._lock:
            session_manager._active_sessions.clear()

        count = restore_sessions()
        assert count == 1

        with session_manager._lock:
            sessions = session_manager._active_sessions.get("user1", set())
            assert "jti-1" not in sessions
            assert "jti-2" in sessions

    def test_restore_after_clear(self, db_path):
        """clear 后崩溃 → 所有 session 不恢复 (无 zombie)"""
        register_session("user1", "jti-1")
        register_session("user1", "jti-2")
        clear_user_sessions("user1")

        with session_manager._lock:
            session_manager._active_sessions.clear()

        count = restore_sessions()
        assert count == 0

    async def test_restore_after_revoke(self, db_path):
        """revoke 后崩溃 → 旧 session 不恢复, 新 session 恢复"""
        register_session("user1", "old-jti")
        await revoke_old_sessions("user1", ["new-jti"])

        with session_manager._lock:
            session_manager._active_sessions.clear()

        count = restore_sessions()
        assert count == 1

        with session_manager._lock:
            sessions = session_manager._active_sessions.get("user1", set())
            assert "old-jti" not in sessions
            assert "new-jti" in sessions

    async def test_restore_after_revoke_new_jtis_survive(self, db_path):
        """FIXED bug 验证: revoke 新 session 在崩溃后可恢复 (原 bug: 新 session 丢失)"""
        await revoke_old_sessions("user1", ["new-jti-1", "new-jti-2"])

        with session_manager._lock:
            session_manager._active_sessions.clear()

        count = restore_sessions()
        assert count == 2  # 原 bug: count == 0 (新 session 未持久化)
        assert is_session_active("user1", "new-jti-1") is True
        assert is_session_active("user1", "new-jti-2") is True

    def test_restore_memory_mode_no_db(self):
        """无 DB 路径 → restore 返回 0"""
        with patch.object(session_manager, "_get_db_path", return_value=None):
            count = restore_sessions()
        assert count == 0
