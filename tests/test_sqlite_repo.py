"""SQLite Repository单元测试"""

import asyncio
import pytest
import pytest_asyncio

from edgelite.storage.database import Database
from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo, AlarmRepo, UserRepo


@pytest_asyncio.fixture
async def db():
    """测试用数据库"""
    database = Database(db_path=":memory:")
    conn = await database.connect()
    await database.init_tables()
    yield conn
    await database.close()


@pytest_asyncio.fixture
async def device_repo(db):
    return DeviceRepo(db)


@pytest_asyncio.fixture
async def rule_repo(db):
    return RuleRepo(db)


@pytest_asyncio.fixture
async def alarm_repo(db):
    return AlarmRepo(db)


@pytest_asyncio.fixture
async def user_repo(db):
    return UserRepo(db)


# ─── DeviceRepo ───

@pytest.mark.asyncio
async def test_device_create_and_get(device_repo):
    """测试设备创建和查询"""
    device = await device_repo.create({
        "device_id": "test-01",
        "name": "测试设备",
        "protocol": "modbus_tcp",
        "status": "offline",
        "config": "{}",
        "points": '[{"name": "temp", "data_type": "float32", "unit": "C", "address": "0", "access_mode": "r"}]',
        "collect_interval": 5,
    })
    assert device is not None
    assert device["device_id"] == "test-01"

    result = await device_repo.get("test-01")
    assert result is not None
    assert result["name"] == "测试设备"


@pytest.mark.asyncio
async def test_device_list(device_repo):
    """测试设备列表"""
    for i in range(3):
        await device_repo.create({
            "device_id": f"dev-{i}",
            "name": f"设备{i}",
            "protocol": "simulator",
            "status": "online",
            "config": "{}",
            "points": "[]",
            "collect_interval": 5,
        })

    devices, total = await device_repo.list_all(page=1, size=10)
    assert total == 3
    assert len(devices) == 3


@pytest.mark.asyncio
async def test_device_delete(device_repo):
    """测试设备删除"""
    await device_repo.create({
        "device_id": "dev-del",
        "name": "待删除",
        "protocol": "simulator",
        "status": "offline",
        "config": "{}",
        "points": "[]",
        "collect_interval": 5,
    })

    success = await device_repo.delete("dev-del")
    assert success is True

    result = await device_repo.get("dev-del")
    assert result is None


# ─── RuleRepo ───

@pytest.mark.asyncio
async def test_rule_create_and_get(rule_repo, device_repo):
    """测试规则创建和查询（需要先创建设备）"""
    await device_repo.create({
        "device_id": "dev-01",
        "name": "测试设备",
        "protocol": "simulator",
        "status": "online",
        "config": "{}",
        "points": "[]",
        "collect_interval": 5,
    })

    rule = await rule_repo.create({
        "name": "温度告警",
        "device_id": "dev-01",
        "conditions": [{"point": "temp", "operator": ">", "threshold": 30}],
        "logic": "AND",
        "duration": 0,
        "severity": "warning",
        "enabled": True,
        "notify_channels": ["dingtalk"],
    })
    assert rule is not None
    assert rule["name"] == "温度告警"

    # 用create返回的rule_id查询
    result = await rule_repo.get(rule["rule_id"])
    assert result is not None
    assert result["name"] == "温度告警"


# ─── AlarmRepo ───

@pytest.mark.asyncio
async def test_alarm_create_and_list(alarm_repo, device_repo, rule_repo):
    """测试告警创建和列表"""
    await device_repo.create({
        "device_id": "dev-01",
        "name": "测试设备",
        "protocol": "simulator",
        "status": "online",
        "config": "{}",
        "points": "[]",
        "collect_interval": 5,
    })
    rule = await rule_repo.create({
        "name": "测试规则",
        "device_id": "dev-01",
        "conditions": [],
        "logic": "AND",
        "duration": 0,
        "severity": "warning",
        "enabled": True,
        "notify_channels": [],
    })

    alarm = await alarm_repo.create({
        "rule_id": rule["rule_id"],
        "device_id": "dev-01",
        "severity": "critical",
        "trigger_value": {"temp": 35.0},
    })

    alarms, total = await alarm_repo.list_all(page=1, size=10)
    assert total == 1
    assert alarms[0]["severity"] == "critical"


@pytest.mark.asyncio
async def test_alarm_ack(alarm_repo, device_repo, rule_repo):
    """测试告警确认"""
    await device_repo.create({
        "device_id": "dev-01",
        "name": "测试设备",
        "protocol": "simulator",
        "status": "online",
        "config": "{}",
        "points": "[]",
        "collect_interval": 5,
    })
    rule = await rule_repo.create({
        "name": "测试规则",
        "device_id": "dev-01",
        "conditions": [],
        "logic": "AND",
        "duration": 0,
        "severity": "warning",
        "enabled": True,
        "notify_channels": [],
    })

    alarm = await alarm_repo.create({
        "rule_id": rule["rule_id"],
        "device_id": "dev-01",
        "severity": "warning",
        "trigger_value": {},
    })

    result = await alarm_repo.ack(alarm["alarm_id"], "admin")
    assert result is not None
    assert result["status"] == "acknowledged"
    assert result["acknowledged_by"] == "admin"


# ─── UserRepo ───

@pytest.mark.asyncio
async def test_user_get_by_username(user_repo):
    """测试按用户名查询"""
    result = await user_repo.get_by_username("admin")
    assert result is not None
    assert result["username"] == "admin"
    assert result["role"] == "admin"
