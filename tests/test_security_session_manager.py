"""用户会话管理单元测试。

覆盖 src/edgelite/security/session_manager.py：register_session / remove_session /
is_session_active / clear_user_sessions / revoke_old_sessions / restore_sessions /
get_active_session_count，含 fail-open 策略和内存模式降级。
"""

from __future__ import annotations

import time

import pytest

from edgelite.security import session_manager as sm


@pytest.fixture(autouse=True)
def _reset_sessions():
    """每个测试前后清理内存会话状态。"""
    sm._active_sessions.clear()
    yield
    sm._active_sessions.clear()


# ─── register_session / is_session_active ───


def test_register_and_check_session():
    """注册会话后 is_session_active 返回 True。"""
    sm.register_session("user1", "jti-1")
    assert sm.is_session_active("user1", "jti-1") is True


def test_is_session_active_no_user_fail_open():
    """用户无记录时 fail-open 返回 True。"""
    assert sm.is_session_active("nonexistent", "any-jti") is True


def test_is_session_active_wrong_jti():
    """用户有记录但 jti 不匹配返回 False。"""
    sm.register_session("user1", "jti-1")
    assert sm.is_session_active("user1", "wrong-jti") is False


def test_register_session_empty_user_id():
    """空 user_id 不注册。"""
    sm.register_session("", "jti-1")
    assert "" not in sm._active_sessions


def test_register_session_empty_jti():
    """空 jti 不注册。"""
    sm.register_session("user1", "")
    assert "user1" not in sm._active_sessions


def test_register_multiple_sessions_same_user():
    """同一用户注册多个 jti。"""
    sm.register_session("user1", "jti-1")
    sm.register_session("user1", "jti-2")
    assert sm.is_session_active("user1", "jti-1") is True
    assert sm.is_session_active("user1", "jti-2") is True


# ─── remove_session ───


def test_remove_session():
    """移除会话后 is_session_active 返回 False。"""
    sm.register_session("user1", "jti-1")
    sm.remove_session("user1", "jti-1")
    # 移除后用户无记录 → fail-open → True
    # 但如果用户还有其他 session，则返回 False
    assert sm.is_session_active("user1", "jti-1") is True  # fail-open（用户记录已被清除）


def test_remove_session_keeps_others():
    """移除一个会话保留其他会话。"""
    sm.register_session("user1", "jti-1")
    sm.register_session("user1", "jti-2")
    sm.remove_session("user1", "jti-1")
    assert sm.is_session_active("user1", "jti-1") is False
    assert sm.is_session_active("user1", "jti-2") is True


def test_remove_session_empty_user():
    """空 user_id 移除不抛错。"""
    sm.remove_session("", "jti-1")  # 不应抛异常


def test_remove_session_empty_jti():
    """空 jti 移除不抛错。"""
    sm.remove_session("user1", "")  # 不应抛异常


def test_remove_nonexistent_session():
    """移除不存在的会话不抛错。"""
    sm.remove_session("nonexistent", "no-jti")


# ─── clear_user_sessions ───


def test_clear_user_sessions():
    """清除用户所有会话。"""
    sm.register_session("user1", "jti-1")
    sm.register_session("user1", "jti-2")
    cleared = sm.clear_user_sessions("user1")
    assert len(cleared) == 2
    assert "jti-1" in cleared
    assert "jti-2" in cleared
    # 清除后 fail-open
    assert sm.is_session_active("user1", "jti-1") is True


def test_clear_user_sessions_no_sessions():
    """无会话的用户清除返回空列表。"""
    cleared = sm.clear_user_sessions("nonexistent")
    assert cleared == []


def test_clear_user_sessions_empty_user():
    """空 user_id 返回空列表。"""
    assert sm.clear_user_sessions("") == []


def test_clear_does_not_affect_other_users():
    """清除一个用户的会话不影响其他用户。"""
    sm.register_session("user1", "jti-1")
    sm.register_session("user2", "jti-2")
    sm.clear_user_sessions("user1")
    assert sm.is_session_active("user2", "jti-2") is True


# ─── revoke_old_sessions ───


@pytest.mark.asyncio
async def test_revoke_old_sessions():
    """撤销旧会话保留新会话。"""
    sm.register_session("user1", "old-jti-1")
    sm.register_session("user1", "old-jti-2")

    await sm.revoke_old_sessions("user1", ["new-jti-1"])

    # 旧会话应被撤销
    assert sm.is_session_active("user1", "old-jti-1") is False
    assert sm.is_session_active("user1", "old-jti-2") is False
    # 新会话应活跃
    assert sm.is_session_active("user1", "new-jti-1") is True


@pytest.mark.asyncio
async def test_revoke_old_sessions_empty_user():
    """空 user_id 不操作。"""
    await sm.revoke_old_sessions("", ["jti-1"])
    # 不应抛异常


@pytest.mark.asyncio
async def test_revoke_old_sessions_no_old_sessions():
    """无旧会话时仅设置新会话。"""
    await sm.revoke_old_sessions("user1", ["new-jti-1"])
    assert sm.is_session_active("user1", "new-jti-1") is True


@pytest.mark.asyncio
async def test_revoke_old_sessions_empty_new():
    """空新会话列表清除所有旧会话。"""
    sm.register_session("user1", "old-jti-1")
    await sm.revoke_old_sessions("user1", [])
    # 用户记录被清除 → fail-open
    assert sm.is_session_active("user1", "old-jti-1") is True


# ─── get_active_session_count ───


def test_get_active_session_count_empty():
    """无会话时计数为 0。"""
    assert sm.get_active_session_count() == 0


def test_get_active_session_count_multiple():
    """多会话计数正确。"""
    sm.register_session("user1", "jti-1")
    sm.register_session("user1", "jti-2")
    sm.register_session("user2", "jti-3")
    assert sm.get_active_session_count() == 3


def test_get_active_session_count_after_removal():
    """移除会话后计数减少。"""
    sm.register_session("user1", "jti-1")
    sm.register_session("user1", "jti-2")
    sm.remove_session("user1", "jti-1")
    assert sm.get_active_session_count() == 1


# ─── restore_sessions ───


def test_restore_sessions_no_db():
    """无数据库时返回 0。"""
    # 测试环境无 _app_state.database，_get_db_path 返回 None
    count = sm.restore_sessions()
    assert count == 0


# ─── _get_db_path ───


def test_get_db_path_no_app_state():
    """无 app_state 时返回 None（内存模式）。"""
    path = sm._get_db_path()
    assert path is None


# ─── register_session with expires_at ───


def test_register_session_custom_expires():
    """自定义过期时间。"""
    exp = time.time() + 3600
    sm.register_session("user1", "jti-1", expires_at=exp)
    assert sm.is_session_active("user1", "jti-1") is True
