"""sqlite_repo 扩展单元测试（文件2）：AlarmRepo、UserRepo、ResourceShareRepo、RateLimitRepo。"""

import asyncio
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, "src")

from edgelite.models.db import (  # noqa: E402
    Base,
    StaleDataError,
)
from edgelite.storage.sqlite_repo import (  # noqa: E402
    AlarmRepo,
    RateLimitRepo,
    ResourceShareRepo,
    UserRepo,
)


# ──────────────────────────────── 夹具 ────────────────────────────────


@pytest_asyncio.fixture
async def db_session():
    """内存 SQLite 会话，每测试独立。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    yield session
    await session.close()
    await engine.dispose()


@pytest_asyncio.fixture
async def alarm_repo(db_session):
    return AlarmRepo(db_session)


@pytest_asyncio.fixture
async def user_repo(db_session):
    return UserRepo(db_session)


@pytest_asyncio.fixture
async def share_repo(db_session):
    return ResourceShareRepo(db_session)


# ──────────────────────────────── 辅助 ────────────────────────────────


def _alarm(**kw):
    a = {
        "rule_id": "r1",
        "device_id": "d1",
        "severity": "critical",
        "message": "alarm",
        "trigger_value": {"v": 1},
        "rule_type": "threshold",
    }
    a.update(kw)
    return a


def _user(**kw):
    u = {"username": "u1", "password": "Abcd1234!", "role": "viewer"}
    u.update(kw)
    return u


def _setup_rule_device(db_session):
    """插入设备+规则，满足外键约束。"""
    from edgelite.models.db import DeviceORM, RuleORM
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    db_session.add(DeviceORM(device_id="d1", name="dev", protocol="simulator", status="online", config="{}", points="[]", collect_interval=5, created_at=now, updated_at=now, version=1))
    db_session.add(RuleORM(rule_id="r1", name="rule", device_id="d1", conditions="[]", logic="AND", duration=0, severity="warning", enabled=True, notify_channels="[]", script="", rule_type="threshold", created_at=now, updated_at=now, version=1))


# ════════════════════ AlarmRepo ════════════════════


class TestAlarmRepo:
    """AlarmRepo 完整 CRUD 与查询。"""

    async def test_create_and_get(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        assert a["severity"] == "critical"
        got = await alarm_repo.get(a["alarm_id"])
        assert got["status"] == "firing"

    async def test_create_invalid_severity(self, alarm_repo):
        with pytest.raises(ValueError):
            await alarm_repo.create(_alarm(severity="bogus"))

    async def test_create_with_id(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create_with_id({**_alarm(), "alarm_id": "custom-id"})
        assert a["alarm_id"] == "custom-id"

    async def test_create_with_id_missing_id(self, alarm_repo):
        with pytest.raises(ValueError, match="alarm_id"):
            await alarm_repo.create_with_id(_alarm())

    async def test_get_missing(self, alarm_repo):
        assert await alarm_repo.get("nope") is None

    async def test_list_all_filters(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm(severity="critical", device_id="d1"))
        await alarm_repo.create(_alarm(severity="warning", device_id="d1"))
        items, total = await alarm_repo.list_all(severity="critical")
        assert total == 1
        items2, total2 = await alarm_repo.list_all(device_id="d1")
        assert total2 == 2

    async def test_list_all_status_filter(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        items, total = await alarm_repo.list_all(status="firing")
        assert total == 1

    async def test_list_all_device_ids(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm(device_id="d1"))
        items, total = await alarm_repo.list_all(device_ids=["d1"])
        assert total == 1
        items2, total2 = await alarm_repo.list_all(device_ids=["d2"])
        assert total2 == 0

    async def test_list_all_search(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm(message="TemperatureHigh"))
        items, total = await alarm_repo.list_all(search="Temp")
        assert total == 1

    async def test_list_all_cursor(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        await alarm_repo.create(_alarm())
        items, total, cursor = await alarm_repo.list_all(cursor="2099-01-01T00:00:00+00:00", size=10)
        assert total == 2
        assert len(items) == 2

    async def test_count_by_status_and_severity(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm(severity="critical"))
        await alarm_repo.create(_alarm(severity="warning"))
        counts = await alarm_repo.count_by_status_and_severity()
        assert counts.get(("firing", "critical")) == 1
        assert counts.get(("firing", "warning")) == 1

    async def test_count_by_status_and_severity_with_ids(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm(device_id="d1"))
        counts = await alarm_repo.count_by_status_and_severity(device_ids=["d1"])
        assert counts.get(("firing", "critical")) == 1
        counts2 = await alarm_repo.count_by_status_and_severity(device_ids=["d2"])
        assert counts2 == {}

    async def test_ack_success(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        result = await alarm_repo.ack(a["alarm_id"], "admin")
        assert result["status"] == "acknowledged"
        assert result["acknowledged_by"] == "admin"

    async def test_ack_missing(self, alarm_repo):
        assert await alarm_repo.ack("nope", "admin") is None

    async def test_ack_status_conflict(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        await alarm_repo.ack(a["alarm_id"], "u1")
        result = await alarm_repo.ack(a["alarm_id"], "u2")
        assert result is not None
        assert result.get("_status_conflict") == "acknowledged"

    async def test_recover_success(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        result = await alarm_repo.recover(a["alarm_id"])
        assert result["status"] == "recovered"

    async def test_recover_missing(self, alarm_repo):
        assert await alarm_repo.recover("nope") is None

    async def test_recover_status_conflict(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        await alarm_repo.recover(a["alarm_id"])
        result = await alarm_repo.recover(a["alarm_id"])
        assert result is not None
        assert result.get("_status_conflict") == "recovered"

    async def test_delete(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        assert await alarm_repo.delete(a["alarm_id"]) is True
        assert await alarm_repo.get(a["alarm_id"]) is None

    async def test_delete_missing(self, alarm_repo):
        assert await alarm_repo.delete("nope") is False

    async def test_update_trigger_count(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        await alarm_repo.update_trigger_count(a["alarm_id"], {"v": 2})
        got = await alarm_repo.get(a["alarm_id"])
        assert got["trigger_count"] == 2

    async def test_update_severity(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        await alarm_repo.update_severity(a["alarm_id"], "warning")
        got = await alarm_repo.get(a["alarm_id"])
        assert got["severity"] == "warning"

    async def test_update_severity_invalid(self, alarm_repo):
        with pytest.raises(ValueError):
            await alarm_repo.update_severity("a1", "bogus")

    async def test_get_firing_by_rule_device(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        result = await alarm_repo.get_firing_by_rule_device("r1", "d1")
        assert result is not None
        assert result["alarm_id"] == a["alarm_id"]

    async def test_get_firing_by_rule_device_none(self, alarm_repo):
        assert await alarm_repo.get_firing_by_rule_device("nope", "nope") is None

    async def test_count_active_by_rule(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        await alarm_repo.create(_alarm())
        count = await alarm_repo.count_active_by_rule("r1")
        assert count == 2

    async def test_list_active_by_rule(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        result = await alarm_repo.list_active_by_rule("r1")
        assert len(result) == 1

    async def test_recover_active_by_rule(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        await alarm_repo.create(_alarm())
        recovered = await alarm_repo.recover_active_by_rule("r1")
        assert len(recovered) == 2
        assert all(r["status"] == "recovered" for r in recovered)

    async def test_recover_active_by_rule_none(self, alarm_repo):
        result = await alarm_repo.recover_active_by_rule("nope")
        assert result == []

    async def test_query_trend_data(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        data = await alarm_repo.query_trend_data(hours=24)
        assert data["period_hours"] == 24
        assert "alarm_counts_by_hour" in data
        assert "severity_distribution" in data
        assert "top_devices" in data
        assert "top_rules" in data

    async def test_get_top_alarms(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        data = await alarm_repo.get_top_alarms(hours=24)
        assert "top_devices" in data
        assert "top_rules" in data

    async def test_get_top_alarms_with_device_ids(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        data = await alarm_repo.get_top_alarms(hours=24, device_ids=["d1"])
        assert len(data["top_devices"]) == 1

    async def test_cleanup_old_alarms(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        a = await alarm_repo.create(_alarm())
        await alarm_repo.ack(a["alarm_id"], "u1")
        deleted = await alarm_repo.cleanup_old_alarms(retention_days=0)
        assert deleted >= 1

    async def test_cleanup_old_alarms_none(self, alarm_repo):
        deleted = await alarm_repo.cleanup_old_alarms(retention_days=90)
        assert deleted == 0

    async def test_get_alarm_history(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        history = await alarm_repo.get_alarm_history("r1", days=7)
        assert len(history) == 1

    async def test_get_alarm_history_paginated(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm())
        await alarm_repo.create(_alarm())
        items, total = await alarm_repo.get_alarm_history_paginated("r1", days=7, page=1, size=10)
        assert total == 2
        assert len(items) == 2

    async def test_get_alarm_history_paginated_with_device_ids(self, alarm_repo, db_session):
        _setup_rule_device(db_session)
        await db_session.commit()
        await alarm_repo.create(_alarm(device_id="d1"))
        items, total = await alarm_repo.get_alarm_history_paginated("r1", days=7, device_ids=["d1"])
        assert total == 1
        items2, total2 = await alarm_repo.get_alarm_history_paginated("r1", days=7, device_ids=["d2"])
        assert total2 == 0


# ════════════════════ UserRepo ════════════════════


class TestUserRepo:
    """UserRepo 完整 CRUD。"""

    async def test_create_and_get(self, user_repo):
        u = await user_repo.create(_user())
        assert u["username"] == "u1"
        got = await user_repo.get(u["user_id"])
        assert got["role"] == "viewer"

    async def test_create_duplicate(self, user_repo):
        await user_repo.create(_user())
        with pytest.raises(ValueError, match="USERNAME_EXISTS"):
            await user_repo.create(_user())

    async def test_create_invalid_role(self, user_repo):
        with pytest.raises(ValueError):
            await user_repo.create(_user(role="super"))

    async def test_get_missing(self, user_repo):
        assert await user_repo.get("nope") is None

    async def test_get_by_username(self, user_repo):
        await user_repo.create(_user())
        got = await user_repo.get_by_username("u1")
        assert got is not None

    async def test_get_by_username_missing(self, user_repo):
        assert await user_repo.get_by_username("nope") is None

    async def test_get_by_username_with_password(self, user_repo):
        await user_repo.create(_user())
        got = await user_repo.get_by_username_with_password("u1")
        assert got is not None
        assert "password" in got

    async def test_get_by_username_with_password_missing(self, user_repo):
        assert await user_repo.get_by_username_with_password("nope") is None

    async def test_list_all(self, user_repo):
        await user_repo.create(_user(username="u1"))
        await user_repo.create(_user(username="u2", password="Defg5678!"))
        items, total = await user_repo.list_all()
        assert total == 2

    async def test_list_all_cursor(self, user_repo):
        await user_repo.create(_user(username="u1"))
        await user_repo.create(_user(username="u2", password="Defg5678!"))
        items, total, cursor = await user_repo.list_all(cursor="2099-01-01T00:00:00+00:00", size=10)
        assert total == 2
        assert len(items) == 2

    async def test_update(self, user_repo):
        u = await user_repo.create(_user())
        updated = await user_repo.update(u["user_id"], {"role": "admin"})
        assert updated["role"] == "admin"

    async def test_update_missing(self, user_repo):
        assert await user_repo.update("nope", {"role": "admin"}) is None

    async def test_update_version_conflict(self, user_repo):
        u = await user_repo.create(_user())
        with pytest.raises(StaleDataError):
            await user_repo.update(u["user_id"], {"role": "admin", "_version": 999})

    async def test_update_invalid_role(self, user_repo):
        u = await user_repo.create(_user())
        with pytest.raises(ValueError):
            await user_repo.update(u["user_id"], {"role": "super"})

    async def test_delete(self, user_repo):
        u = await user_repo.create(_user())
        with pytest.raises(RuntimeError):
            await user_repo.delete(u["user_id"])

    async def test_delete_missing(self, user_repo):
        with pytest.raises(RuntimeError):
            await user_repo.delete("nope")

    async def test_update_password_and_clear_flag(self, user_repo):
        u = await user_repo.create(_user())
        result = await user_repo.update_password_and_clear_flag("u1", "newhash123")
        assert result is True

    async def test_update_password_and_clear_flag_missing(self, user_repo):
        result = await user_repo.update_password_and_clear_flag("nope", "newhash")
        assert result is False

    async def test_update_password(self, user_repo):
        await user_repo.create(_user())
        await user_repo.update_password("u1", "newhash123")
        got = await user_repo.get_by_username_with_password("u1")
        assert got["password"] == "newhash123"

    async def test_update_password_missing(self, user_repo):
        await user_repo.update_password("nope", "newhash")

    async def test_update_user(self, user_repo):
        await user_repo.create(_user())
        updated = await user_repo.update_user("u1", {"role": "operator"})
        assert updated["role"] == "operator"

    async def test_update_user_missing(self, user_repo):
        assert await user_repo.update_user("nope", {"role": "admin"}) is None

    async def test_update_user_version_conflict(self, user_repo):
        await user_repo.create(_user())
        with pytest.raises(StaleDataError):
            await user_repo.update_user("u1", {"role": "admin", "_version": 999})

    async def test_count_by_role(self, user_repo):
        await user_repo.create(_user(role="admin"))
        await user_repo.create(_user(username="u2", password="Defg5678!", role="viewer"))
        assert await user_repo.count_by_role("admin") == 1
        assert await user_repo.count_by_role("viewer") == 1


# ════════════════════ ResourceShareRepo ════════════════════


class TestResourceShareRepo:
    """ResourceShareRepo 完整 CRUD。"""

    async def test_share_new(self, share_repo):
        s = await share_repo.share_resource("device", "d1", "user1", "read", "admin1")
        assert s["resource_type"] == "device"
        assert s["resource_id"] == "d1"
        assert s["permission_level"] == "read"

    async def test_share_update_existing(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "read", "admin1")
        s = await share_repo.share_resource("device", "d1", "user1", "write", "admin2")
        assert s["permission_level"] == "write"
        assert s["shared_by_user_id"] == "admin2"

    async def test_unshare(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "read", "admin1")
        assert await share_repo.unshare_resource("device", "d1", "user1") is True

    async def test_unshare_missing(self, share_repo):
        assert await share_repo.unshare_resource("device", "nope", "user1") is False

    async def test_list_shares_for_resource(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "read", "admin1")
        await share_repo.share_resource("device", "d1", "user2", "write", "admin1")
        items, total = await share_repo.list_shares_for_resource("device", "d1")
        assert total == 2

    async def test_list_shares_for_resource_empty(self, share_repo):
        items, total = await share_repo.list_shares_for_resource("device", "nope")
        assert total == 0

    async def test_list_shared_with_user(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "read", "admin1")
        await share_repo.share_resource("rule", "r1", "user1", "write", "admin1")
        items, total = await share_repo.list_shared_with_user("user1")
        assert total == 2
        items2, total2 = await share_repo.list_shared_with_user("user1", resource_type="device")
        assert total2 == 1

    async def test_check_user_has_access_read(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "read", "admin1")
        assert await share_repo.check_user_has_access("device", "d1", "user1", "read") is True
        assert await share_repo.check_user_has_access("device", "d1", "user1", "write") is False

    async def test_check_user_has_access_write(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "write", "admin1")
        assert await share_repo.check_user_has_access("device", "d1", "user1", "read") is True
        assert await share_repo.check_user_has_access("device", "d1", "user1", "write") is True
        assert await share_repo.check_user_has_access("device", "d1", "user1", "admin") is False

    async def test_check_user_has_access_admin(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "admin", "admin1")
        assert await share_repo.check_user_has_access("device", "d1", "user1", "admin") is True

    async def test_check_user_has_access_no_share(self, share_repo):
        assert await share_repo.check_user_has_access("device", "d1", "user1") is False

    async def test_get_shared_resource_ids(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "read", "admin1")
        await share_repo.share_resource("device", "d2", "user1", "read", "admin1")
        ids = await share_repo.get_shared_resource_ids("user1", "device")
        assert ids == {"d1", "d2"}

    async def test_get_shared_resource_ids_empty(self, share_repo):
        ids = await share_repo.get_shared_resource_ids("user1", "device")
        assert ids == set()

    async def test_delete_shares_for_resource(self, share_repo):
        await share_repo.share_resource("device", "d1", "user1", "read", "admin1")
        await share_repo.share_resource("device", "d1", "user2", "read", "admin1")
        count = await share_repo.delete_shares_for_resource("device", "d1")
        assert count == 2

    async def test_delete_shares_for_resource_none(self, share_repo):
        count = await share_repo.delete_shares_for_resource("device", "nope")
        assert count == 0


# ════════════════════ RateLimitRepo ════════════════════


class _FakeDB:
    """模拟 Database 单例，提供 write_lock 和 get_session()，使用 StaticPool 共享内存库。"""

    def __init__(self, engine, factory):
        self._engine = engine
        self._factory = factory
        self.write_lock = asyncio.Lock()

    def get_session(self):
        return self._factory()


@pytest_asyncio.fixture
async def fake_db():
    """构建 StaticPool 内存库 + _FakeDB，patch Database.get_instance 返回它。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    db = _FakeDB(engine, factory)

    with patch("edgelite.storage.database.Database.get_instance", return_value=db):
        yield db

    await engine.dispose()


@pytest.fixture
def fake_config(monkeypatch):
    """patch get_config 返回带 security 配置的对象。"""
    security = SimpleNamespace(
        login_lockout_threshold=3,
        login_lockout_minutes=15,
        global_lockout_threshold=5,
        global_lockout_window=15,
        global_lockout_duration=30,
    )
    config = SimpleNamespace(security=security)
    monkeypatch.setattr("edgelite.config.get_config", lambda: config)
    return config


class TestRateLimitRepo:
    """RateLimitRepo 登录限流与锁定。"""

    async def test_record_and_check_login_attempt(self, fake_db):
        count = await RateLimitRepo.record_login_attempt("1.2.3.4")
        assert count == 1
        count2 = await RateLimitRepo.record_login_attempt("1.2.3.4")
        assert count2 == 2
        checked = await RateLimitRepo.check_login_rate("1.2.3.4")
        assert checked == 2

    async def test_check_login_rate_no_record(self, fake_db):
        assert await RateLimitRepo.check_login_rate("9.9.9.9") == 0

    async def test_clear_login_attempts(self, fake_db):
        await RateLimitRepo.record_login_attempt("1.2.3.4")
        await RateLimitRepo.clear_login_attempts("1.2.3.4")
        assert await RateLimitRepo.check_login_rate("1.2.3.4") == 0

    async def test_get_lockout_info_none(self, fake_db):
        assert await RateLimitRepo.get_lockout_info("user", "1.2.3.4") is None

    async def test_record_lockout_no_lock(self, fake_db, fake_config):
        result = await RateLimitRepo.record_lockout_failure("user", "1.2.3.4")
        assert result is None  # below threshold

    async def test_record_lockout_triggers_lock(self, fake_db, fake_config):
        await RateLimitRepo.record_lockout_failure("user", "1.2.3.4")
        await RateLimitRepo.record_lockout_failure("user", "1.2.3.4")
        result = await RateLimitRepo.record_lockout_failure("user", "1.2.3.4")
        assert result is not None
        assert result["fail_count"] == 3
        assert result["locked_until"] > 0

    async def test_get_lockout_info_active(self, fake_db, fake_config):
        await RateLimitRepo.record_lockout_failure("u", "1.2.3.4")
        await RateLimitRepo.record_lockout_failure("u", "1.2.3.4")
        await RateLimitRepo.record_lockout_failure("u", "1.2.3.4")
        info = await RateLimitRepo.get_lockout_info("u", "1.2.3.4")
        assert info is not None
        assert info["fail_count"] == 3

    async def test_clear_lockout(self, fake_db, fake_config):
        await RateLimitRepo.record_lockout_failure("u", "1.2.3.4")
        await RateLimitRepo.record_lockout_failure("u", "1.2.3.4")
        await RateLimitRepo.record_lockout_failure("u", "1.2.3.4")
        await RateLimitRepo.clear_lockout("u", "1.2.3.4")
        assert await RateLimitRepo.get_lockout_info("u", "1.2.3.4") is None

    async def test_cleanup_expired(self, fake_db):
        await RateLimitRepo.record_login_attempt("1.2.3.4")
        deleted = await RateLimitRepo.cleanup_expired()
        assert isinstance(deleted, tuple)
        assert len(deleted) == 3

    async def test_start_and_stop_cleanup_task(self, fake_db):
        RateLimitRepo.start_cleanup_task()
        assert RateLimitRepo._cleanup_task is not None
        await RateLimitRepo.stop_cleanup_task()
        assert RateLimitRepo._cleanup_task is None

    async def test_check_global_failure_rate(self, fake_db):
        assert await RateLimitRepo.check_global_failure_rate() == 0

    async def test_record_global_failure(self, fake_db):
        await RateLimitRepo.record_global_failure("user", "1.2.3.4")
        count = await RateLimitRepo.check_global_failure_rate()
        assert count == 1

    async def test_check_global_account_lockout_none(self, fake_db):
        assert await RateLimitRepo.check_global_account_lockout("user") is None

    async def test_record_global_account_failure_no_lock(self, fake_db, fake_config):
        result = await RateLimitRepo.record_global_account_failure("user")
        assert result is None

    async def test_record_global_account_failure_lock(self, fake_db, fake_config):
        for _ in range(5):
            result = await RateLimitRepo.record_global_account_failure("user")
        assert result is not None
        assert result["fail_count"] == 5

    async def test_check_global_account_lockout_active(self, fake_db, fake_config):
        for _ in range(5):
            await RateLimitRepo.record_global_account_failure("user")
        info = await RateLimitRepo.check_global_account_lockout("user")
        assert info is not None
        assert info["fail_count"] == 5

    async def test_clear_global_account_lockout(self, fake_db, fake_config):
        for _ in range(5):
            await RateLimitRepo.record_global_account_failure("user")
        await RateLimitRepo.clear_global_account_lockout("user")
        assert await RateLimitRepo.check_global_account_lockout("user") is None

    async def test_cleanup_global_failures(self, fake_db):
        from edgelite.models.db import GlobalLoginFailureORM
        old_ts = time.time() - 7200
        async with fake_db.get_session() as session:
            session.add(GlobalLoginFailureORM(timestamp=old_ts, username="old", ip="1.2.3.4"))
            await session.commit()
        deleted = await RateLimitRepo.cleanup_global_failures()
        assert deleted >= 1

    async def test_password_reset_ip_rate(self, fake_db):
        count = await RateLimitRepo.record_password_reset_ip_attempt("1.2.3.4")
        assert count == 1
        checked, retry = await RateLimitRepo.check_password_reset_ip_rate("1.2.3.4")
        assert checked == 1
        assert retry == 0

    async def test_password_reset_ip_rate_no_record(self, fake_db):
        checked, retry = await RateLimitRepo.check_password_reset_ip_rate("9.9.9.9")
        assert checked == 0

    async def test_password_reset_ip_rate_limited(self, fake_db):
        for _ in range(5):
            await RateLimitRepo.record_password_reset_ip_attempt("1.2.3.4")
        checked, retry = await RateLimitRepo.check_password_reset_ip_rate("1.2.3.4")
        assert checked == -1
        assert retry > 0

    async def test_password_reset_user_rate(self, fake_db):
        count = await RateLimitRepo.record_password_reset_user_attempt("user")
        assert count == 1
        checked, retry = await RateLimitRepo.check_password_reset_user_rate("user")
        assert checked == 1

    async def test_password_reset_user_rate_no_record(self, fake_db):
        checked, retry = await RateLimitRepo.check_password_reset_user_rate("nope")
        assert checked == 0

    async def test_password_reset_user_rate_limited(self, fake_db):
        for _ in range(3):
            await RateLimitRepo.record_password_reset_user_attempt("user")
        checked, retry = await RateLimitRepo.check_password_reset_user_rate("user")
        assert checked == -1
        assert retry > 0

    async def test_cleanup_password_reset_attempts(self, fake_db):
        await RateLimitRepo.record_password_reset_ip_attempt("1.2.3.4")
        await RateLimitRepo.record_password_reset_user_attempt("user")
        ip_del, user_del = await RateLimitRepo.cleanup_password_reset_attempts()
        assert isinstance(ip_del, int)
        assert isinstance(user_del, int)

    async def test_is_password_reset_token_used(self, fake_db):
        assert await RateLimitRepo.is_password_reset_token_used("tokhash") is False

    async def test_mark_and_check_token_used(self, fake_db):
        result = await RateLimitRepo.mark_password_reset_token_used("tokhash", "user")
        assert result is True
        assert await RateLimitRepo.is_password_reset_token_used("tokhash") is True

    async def test_mark_token_used_idempotent(self, fake_db):
        await RateLimitRepo.mark_password_reset_token_used("tokhash", "user")
        result = await RateLimitRepo.mark_password_reset_token_used("tokhash", "user")
        assert result is True

    async def test_check_reset_usage_ip_rate(self, fake_db):
        count = await RateLimitRepo.record_reset_usage_attempt("1.2.3.4")
        assert count == 1
        checked, retry = await RateLimitRepo.check_reset_usage_ip_rate("1.2.3.4")
        assert checked == 1

    async def test_check_reset_usage_ip_rate_no_record(self, fake_db):
        checked, retry = await RateLimitRepo.check_reset_usage_ip_rate("9.9.9.9")
        assert checked == 0

    async def test_check_reset_usage_ip_rate_limited(self, fake_db):
        for _ in range(3):
            await RateLimitRepo.record_reset_usage_attempt("1.2.3.4")
        checked, retry = await RateLimitRepo.check_reset_usage_ip_rate("1.2.3.4")
        assert checked == -1
        assert retry > 0

    async def test_cleanup_used_password_reset_tokens(self, fake_db):
        await RateLimitRepo.mark_password_reset_token_used("tokhash", "user")
        deleted = await RateLimitRepo.cleanup_used_password_reset_tokens()
        assert deleted == 0  # token is recent, not older than 24h

    async def test_rate_limit_no_db(self, monkeypatch):
        """Database.get_instance 返回 None 时安全降级。"""
        monkeypatch.setattr("edgelite.storage.database.Database.get_instance", staticmethod(lambda: None))
        assert await RateLimitRepo.record_login_attempt("1.2.3.4") == 0
        assert await RateLimitRepo.check_login_rate("1.2.3.4") == 0
        assert await RateLimitRepo.get_lockout_info("u", "1.2.3.4") is None
