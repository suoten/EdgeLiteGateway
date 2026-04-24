"""EventBus单元测试"""

import asyncio
import pytest
import pytest_asyncio

from edgelite.engine.event_bus import EventBus, PointUpdateEvent, AlarmEvent, DeviceStatusEvent


@pytest_asyncio.fixture
async def event_bus():
    bus = EventBus()
    yield bus
    bus._handlers.clear()


@pytest.mark.asyncio
async def test_event_bus_subscribe_and_publish(event_bus):
    """测试事件订阅和发布"""
    received = []

    async def handler(event):
        received.append(event)

    event_bus.register_handler("PointUpdateEvent", handler)

    event = PointUpdateEvent(device_id="test-01", point_name="temperature", value=25.0)
    await event_bus.publish(event)

    await asyncio.sleep(0.1)
    assert len(received) == 1
    assert received[0].device_id == "test-01"


@pytest.mark.asyncio
async def test_event_bus_multiple_handlers(event_bus):
    """测试多个处理器"""
    results_a = []
    results_b = []

    async def handler_a(event):
        results_a.append(event)

    async def handler_b(event):
        results_b.append(event)

    event_bus.register_handler("AlarmEvent", handler_a)
    event_bus.register_handler("AlarmEvent", handler_b)

    event = AlarmEvent(alarm_id="a1", rule_id="r1", device_id="d1", severity="warning", action="firing")
    await event_bus.publish(event)
    await asyncio.sleep(0.1)

    assert len(results_a) == 1
    assert len(results_b) == 1


@pytest.mark.asyncio
async def test_event_bus_no_handler(event_bus):
    """测试没有处理器时不报错"""
    event = DeviceStatusEvent(device_id="d1", old_status="offline", new_status="online")
    await event_bus.publish(event)


@pytest.mark.asyncio
async def test_point_update_event():
    """测试PointUpdateEvent数据类"""
    event = PointUpdateEvent(device_id="sensor-01", point_name="temp", value=22.5)
    assert event.device_id == "sensor-01"
    assert event.value == 22.5


@pytest.mark.asyncio
async def test_alarm_event():
    """测试AlarmEvent数据类"""
    event = AlarmEvent(alarm_id="a1", rule_id="r1", device_id="d1", severity="critical", action="firing")
    assert event.severity == "critical"
    assert event.action == "firing"
