"""令牌撤销管理单元测试。

覆盖 src/edgelite/security/token_revocation.py：revoke_token / is_token_revoked /
cleanup_expired / revoke_all_tokens_for_user，含内存缓存、空 jti 校验、
过期清理、统计信息、fallback 文件。
"""

from __future__ import annotations

import time

import pytest

from edgelite.security.token_revocation import (
    TokenRevocationManager,
    cleanup_expired,
    cleanup_expired_sync,
    is_token_revoked,
    revoke_all_tokens_for_user,
    revoke_token,
    revoke_token_async,
)


@pytest.fixture(autouse=True)
def _reset_manager():
    """每个测试前后重置单例和全局管理器。"""
    TokenRevocationManager._instance = None
    import edgelite.security.token_revocation as tr

    tr._manager = None
    yield
    TokenRevocationManager._instance = None
    tr._manager = None


# ─── is_token_revoked / revoke_token ───


def test_revoke_and_check():
    """撤销后 is_token_revoked 返回 True。"""
    revoke_token("jti-1", exp=time.time() + 3600)
    assert is_token_revoked("jti-1") is True


def test_is_token_revoked_not_revoked():
    """未撤销的 jti 返回 False。"""
    assert is_token_revoked("nonexistent-jti") is False


def test_is_token_revoked_empty_jti():
    """空 jti 返回 False。"""
    assert is_token_revoked("") is False


def test_is_token_revoked_none_jti():
    """None jti 返回 False。"""
    assert is_token_revoked(None) is False


def test_revoke_token_empty_jti_ignored():
    """空 jti 撤销被忽略。"""
    revoke_token("", exp=time.time() + 3600)
    # 不应抛异常，不应影响其他 jti


def test_revoke_token_default_exp():
    """不提供 exp 时使用默认 TTL。"""
    revoke_token("jti-default")
    assert is_token_revoked("jti-default") is True


# ─── 过期清理 ───


def test_revoked_token_expired():
    """已过期的撤销记录不视为已撤销。"""
    # 撤销时设置过期时间为过去
    revoke_token("jti-expired", exp=time.time() - 10)
    assert is_token_revoked("jti-expired") is False


def test_cleanup_expired_sync():
    """同步清理过期记录。"""
    # 添加一个已过期的记录
    revoke_token("jti-expired-1", exp=time.time() - 10)
    revoke_token("jti-valid-1", exp=time.time() + 3600)

    cleaned = cleanup_expired_sync()
    assert cleaned >= 1
    # 有效记录仍存在
    assert is_token_revoked("jti-valid-1") is True


@pytest.mark.asyncio
async def test_cleanup_expired_async():
    """异步清理过期记录。"""
    revoke_token("jti-expired-2", exp=time.time() - 10)
    revoke_token("jti-valid-2", exp=time.time() + 3600)

    cleaned = await cleanup_expired()
    assert cleaned >= 1
    assert is_token_revoked("jti-valid-2") is True


# ─── revoke_token_async ───


@pytest.mark.asyncio
async def test_revoke_token_async():
    """异步撤销令牌。"""
    await revoke_token_async("jti-async", exp=time.time() + 3600)
    assert is_token_revoked("jti-async") is True


@pytest.mark.asyncio
async def test_revoke_token_async_empty_jti():
    """异步撤销空 jti 被忽略。"""
    await revoke_token_async("", exp=time.time() + 3600)
    # 不应抛异常


# ─── revoke_all_tokens_for_user ───


@pytest.mark.asyncio
async def test_revoke_all_tokens_for_user_no_sessions():
    """无会话用户返回 0。"""
    count = await revoke_all_tokens_for_user("nonexistent-user")
    assert count == 0


@pytest.mark.asyncio
async def test_revoke_all_tokens_for_user_empty():
    """空 user_id 返回 0。"""
    count = await revoke_all_tokens_for_user("")
    assert count == 0


@pytest.mark.asyncio
async def test_revoke_all_tokens_for_user_with_sessions():
    """有会话用户撤销所有 token。"""
    from edgelite.security.session_manager import register_session

    register_session("user-test", "jti-a")
    register_session("user-test", "jti-b")

    count = await revoke_all_tokens_for_user("user-test")
    assert count == 2
    # 撤销后 token 应标记为已撤销
    assert is_token_revoked("jti-a") is True
    assert is_token_revoked("jti-b") is True


# ─── TokenRevocationManager 单例 ───


def test_manager_singleton():
    """get_instance 返回单例。"""
    m1 = TokenRevocationManager.get_instance()
    m2 = TokenRevocationManager.get_instance()
    assert m1 is m2


# ─── get_stats ───


def test_get_stats():
    """get_stats 返回统计信息。"""
    manager = TokenRevocationManager.get_instance()
    revoke_token("jti-stats", exp=time.time() + 3600)
    stats = manager.get_stats()
    assert "total_cached" in stats
    assert "valid_entries" in stats
    assert "expired_entries" in stats
    assert "db_ready" in stats
    assert stats["total_cached"] >= 1


def test_get_stats_empty():
    """无记录时 stats 正确。"""
    manager = TokenRevocationManager.get_instance()
    stats = manager.get_stats()
    assert stats["total_cached"] == 0
    assert stats["valid_entries"] == 0


# ─── _write_fallback / _check_fallback ───


def test_fallback_write_and_check(tmp_path, monkeypatch):
    """fallback 文件写入和检查。"""
    import edgelite.security.token_revocation as tr

    fallback_file = tmp_path / ".fallback.json"
    monkeypatch.setattr(tr, "_FALLBACK_FILE", fallback_file)

    manager = TokenRevocationManager.get_instance()
    manager._write_fallback("jti-fb", time.time() + 3600)
    assert manager._check_fallback("jti-fb") is True
    assert manager._check_fallback("nonexistent") is False


def test_fallback_expired_not_revoked(tmp_path, monkeypatch):
    """fallback 中过期的记录不视为已撤销。"""
    import edgelite.security.token_revocation as tr

    fallback_file = tmp_path / ".fallback.json"
    monkeypatch.setattr(tr, "_FALLBACK_FILE", fallback_file)

    manager = TokenRevocationManager.get_instance()
    manager._write_fallback("jti-fb-expired", time.time() - 10)
    assert manager._check_fallback("jti-fb-expired") is False


# ─── cleanup_if_needed ───


@pytest.mark.asyncio
async def test_cleanup_if_needed_below_threshold():
    """缓存未超阈值时不清理。"""
    manager = TokenRevocationManager.get_instance()
    revoke_token("jti-cin", exp=time.time() + 3600)
    # 不应抛异常
    await manager.cleanup_if_needed()
    assert is_token_revoked("jti-cin") is True
