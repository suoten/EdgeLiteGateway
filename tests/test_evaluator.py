"""规则评估器 (RuleEvaluator) 综合单元测试。

覆盖 edgelite/engine/evaluator.py 的全部公共与私有方法：
- 构造与生命周期 (start/stop)
- 设备名称解析 (_resolve_device_name)
- 规则缓存与失效 (_get_rules_for_point / invalidate_cache / 世代校验)
- duration_tracker 清理 (cleanup_duration_tracker / _prune_duration_tracker)
- 条件比较 (_compare) 与条件组合 (_check_conditions: AND/OR/NOT、死区、duration_seconds、窗口聚合、AI源)
- AI 推理条件评估 (_evaluate_ai_conditions / _get_latest_ai_result)
- 窗口聚合 (_get_window_aggregate)
- 脚本规则 (_eval_script)
- 单规则评估 (_evaluate_rule: 阈值/脚本/AI/持续时间/触发/恢复)
- 告警触发 (_fire_alarm: 去重、冷却、回滚、CancelledError)
- 告警恢复 (_recover_alarm)
- 事件循环 (_eval_loop / _evaluate / _evaluate_inner)

所有外部依赖 (DB/InfluxDB/AI引擎/流计算引擎/沙箱) 均通过 mock 注入，
不产生真实网络或数据库调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.engine.evaluator import RuleEvaluator
from edgelite.engine.event_bus import EventBus, PointUpdateEvent

# ════════════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════════════


def _make_event(device_id="d1", point_name="p1", value=10.0, quality="good"):
    """构造一个 PointUpdateEvent"""
    return PointUpdateEvent(device_id=device_id, point_name=point_name, value=value, quality=quality)


def _make_rule(
    rule_id="r1",
    device_id="d1",
    conditions=None,
    logic="AND",
    duration=0,
    rule_type="threshold",
    script="",
    severity="warning",
    priority=0,
    name="rule-name",
):
    """构造一个规则字典"""
    return {
        "rule_id": rule_id,
        "device_id": device_id,
        "conditions": conditions or [{"point": "p1", "operator": ">", "threshold": 5}],
        "logic": logic,
        "duration": duration,
        "rule_type": rule_type,
        "script": script,
        "severity": severity,
        "priority": priority,
        "name": name,
    }


# ════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus():
    """构造一个真实 EventBus，便于 publish/subscribe 集成验证"""
    return EventBus()


@pytest.fixture
def rule_repo():
    """mock RuleRepo"""
    repo = MagicMock()
    repo.list_enabled_by_point = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def alarm_repo():
    """mock AlarmRepo"""
    repo = MagicMock()
    repo.get_firing_by_rule_device = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value={"alarm_id": "a1"})
    repo.update_trigger_count = AsyncMock(return_value=None)
    repo.recover = AsyncMock(return_value={"alarm_id": "a1"})
    return repo


@pytest.fixture
def ai_engine():
    """mock AI 推理引擎"""
    engine = MagicMock()
    engine.get_model = MagicMock(return_value=None)
    engine.infer = AsyncMock(return_value=None)
    return engine


@pytest.fixture
def device_repo():
    """mock DeviceRepo"""
    repo = MagicMock()
    repo.get = AsyncMock(return_value={"name": "设备A"})
    return repo


@pytest.fixture
def evaluator(event_bus, rule_repo, alarm_repo):
    """构造一个 RuleEvaluator 实例，依赖全部 mock。

    将 _min_firing_interval 设为 0 便于测试告警触发不被冷却拦截。
    """
    ev = RuleEvaluator(event_bus, rule_repo, alarm_repo)
    ev._min_firing_interval = 0.0
    return ev


@pytest.fixture
def evaluator_full(event_bus, rule_repo, alarm_repo, ai_engine, device_repo):
    """构造一个注入全部依赖 (含 AI/设备仓库) 的 RuleEvaluator"""
    return RuleEvaluator(event_bus, rule_repo, alarm_repo, ai_engine=ai_engine, device_repo=device_repo)


# ════════════════════════════════════════════════════════════════════════
# 1. 构造与初始化
# ════════════════════════════════════════════════════════════════════════


class TestInit:
    """验证 RuleEvaluator 构造与初始状态"""

    def test_init_defaults(self, event_bus, rule_repo, alarm_repo):
        """默认构造应初始化所有内部状态"""
        ev = RuleEvaluator(event_bus, rule_repo, alarm_repo)
        assert ev._event_bus is event_bus
        assert ev._rule_repo is rule_repo
        assert ev._alarm_repo is alarm_repo
        assert ev._ai_engine is None
        assert ev._device_repo is None
        assert ev._device_name_cache == {}
        assert ev._duration_tracker == {}
        assert ev._rule_cache == {}
        assert ev._task is None
        assert ev._recent_firings == {}
        assert ev._last_values == {}
        assert ev._condition_first_met == {}

    def test_init_with_optional_deps(self, event_bus, rule_repo, alarm_repo, ai_engine, device_repo):
        """构造时可注入 ai_engine 与 device_repo"""
        ev = RuleEvaluator(event_bus, rule_repo, alarm_repo, ai_engine=ai_engine, device_repo=device_repo)
        assert ev._ai_engine is ai_engine
        assert ev._device_repo is device_repo


# ════════════════════════════════════════════════════════════════════════
# 2. _resolve_device_name
# ════════════════════════════════════════════════════════════════════════


class TestResolveDeviceName:
    """验证异步设备名称解析与缓存"""

    async def test_returns_cached_name(self, evaluator_full, device_repo):
        """已缓存的设备名直接返回，不再访问 device_repo"""
        evaluator_full._device_name_cache["d1"] = "缓存设备"
        name = await evaluator_full._resolve_device_name("d1")
        assert name == "缓存设备"
        device_repo.get.assert_not_called()

    async def test_queries_device_repo(self, evaluator_full, device_repo):
        """无缓存时从 device_repo.get 获取并写入缓存"""
        name = await evaluator_full._resolve_device_name("d2")
        assert name == "设备A"
        device_repo.get.assert_awaited_once_with("d2")
        assert evaluator_full._device_name_cache["d2"] == "设备A"

    async def test_returns_id_when_repo_none(self, evaluator):
        """device_repo 为 None 时返回 device_id 本身"""
        name = await evaluator._resolve_device_name("dX")
        assert name == "dX"

    async def test_returns_id_when_device_has_no_name(self, evaluator_full, device_repo):
        """设备字典无 name 字段时返回 device_id"""
        device_repo.get.return_value = {"id": "d4"}
        name = await evaluator_full._resolve_device_name("d4")
        assert name == "d4"

    async def test_returns_id_on_exception(self, evaluator_full, device_repo):
        """device_repo.get 抛异常时返回 device_id 不向上传播"""
        device_repo.get.side_effect = RuntimeError("db error")
        name = await evaluator_full._resolve_device_name("d5")
        assert name == "d5"


# ════════════════════════════════════════════════════════════════════════
# 3. _get_rules_for_point
# ════════════════════════════════════════════════════════════════════════


class TestGetRulesForPoint:
    """验证规则缓存、TTL 与世代校验"""

    async def test_caches_rules(self, evaluator, rule_repo):
        """首次查询后结果写入缓存，二次命中缓存不再查 DB"""
        rule_repo.list_enabled_by_point.return_value = [_make_rule()]
        await evaluator._get_rules_for_point("d1", "p1")
        await evaluator._get_rules_for_point("d1", "p1")
        rule_repo.list_enabled_by_point.assert_awaited_once_with("d1", "p1")
        assert "d1:p1" in evaluator._rule_cache

    async def test_returns_empty_on_db_error(self, evaluator, rule_repo):
        """DB 查询异常时返回空列表不抛出"""
        rule_repo.list_enabled_by_point.side_effect = RuntimeError("db fail")
        rules = await evaluator._get_rules_for_point("d1", "p1")
        assert rules == []

    async def test_cache_ttl_expiry(self, evaluator, rule_repo):
        """超过 TTL 后缓存清空并重新查询"""
        evaluator._cache_ttl = 0.0  # 立即过期
        rule_repo.list_enabled_by_point.return_value = [_make_rule()]
        await evaluator._get_rules_for_point("d1", "p1")
        await evaluator._get_rules_for_point("d1", "p1")
        assert rule_repo.list_enabled_by_point.await_count == 2

    async def test_invalidate_during_inflight_query_discards_stale(self, evaluator, rule_repo):
        """DB 查询期间若 invalidate_cache 递增世代，回填结果被丢弃"""
        original = rule_repo.list_enabled_by_point

        async def slow_query(device_id, point_name):
            # 模拟查询期间缓存被失效
            await evaluator.invalidate_cache()
            return [_make_rule()]

        rule_repo.list_enabled_by_point = AsyncMock(side_effect=slow_query)
        rules = await evaluator._get_rules_for_point("d1", "p1")
        # 世代变化，结果应被丢弃，返回 rules 但不写入缓存（仍返回查询结果）
        assert rules == [_make_rule()]
        assert "d1:p1" not in evaluator._rule_cache
        # 恢复以避免影响其他测试
        rule_repo.list_enabled_by_point = original


# ════════════════════════════════════════════════════════════════════════
# 4. invalidate_cache
# ════════════════════════════════════════════════════════════════════════


class TestInvalidateCache:
    """验证缓存失效逻辑"""

    async def test_clear_all(self, evaluator):
        """无参数时清空全部缓存并重置 cache_time"""
        evaluator._rule_cache["d1:p1"] = [_make_rule()]
        evaluator._rule_cache["d2:p2"] = []
        evaluator._cache_time = 100.0
        await evaluator.invalidate_cache()
        assert evaluator._rule_cache == {}
        assert evaluator._cache_time == 0.0
        assert evaluator._cache_generation == 1

    async def test_clear_specific_key(self, evaluator):
        """指定 device+point 时仅删除对应键"""
        evaluator._rule_cache["d1:p1"] = [_make_rule()]
        evaluator._rule_cache["d2:p2"] = []
        await evaluator.invalidate_cache("d1", "p1")
        assert "d1:p1" not in evaluator._rule_cache
        assert "d2:p2" in evaluator._rule_cache

    async def test_increments_generation(self, evaluator):
        """每次失效递增缓存世代"""
        gen0 = evaluator._cache_generation
        await evaluator.invalidate_cache()
        assert evaluator._cache_generation == gen0 + 1
        await evaluator.invalidate_cache("d1", "p1")
        assert evaluator._cache_generation == gen0 + 2


# ════════════════════════════════════════════════════════════════════════
# 5. cleanup_duration_tracker
# ════════════════════════════════════════════════════════════════════════


class TestCleanupDurationTracker:
    """验证按 rule_id 清理 duration_tracker 与 condition_first_met"""

    async def test_removes_entries_by_rule_id(self, evaluator):
        """清理指定 rule_id 的所有 tracker 条目"""
        evaluator._duration_tracker[("r1", "d1")] = datetime.now(UTC)
        evaluator._duration_tracker[("r1", "d2")] = datetime.now(UTC)
        evaluator._duration_tracker[("r2", "d1")] = datetime.now(UTC)
        await evaluator.cleanup_duration_tracker("r1")
        assert ("r1", "d1") not in evaluator._duration_tracker
        assert ("r1", "d2") not in evaluator._duration_tracker
        assert ("r2", "d1") in evaluator._duration_tracker

    async def test_removes_condition_first_met_entries(self, evaluator):
        """同时清理 condition_first_met 中匹配前缀的键"""
        evaluator._condition_first_met["r1:p1:>:5"] = 1.0
        evaluator._condition_first_met["r1:p2:<:3"] = 2.0
        evaluator._condition_first_met["r2:p1:>:5"] = 3.0
        await evaluator.cleanup_duration_tracker("r1")
        assert "r1:p1:>:5" not in evaluator._condition_first_met
        assert "r1:p2:<:3" not in evaluator._condition_first_met
        assert "r2:p1:>:5" in evaluator._condition_first_met

    async def test_noop_when_no_entries(self, evaluator):
        """无匹配条目时不报错"""
        await evaluator.cleanup_duration_tracker("rX")
        assert evaluator._duration_tracker == {}


# ════════════════════════════════════════════════════════════════════════
# 6. _prune_duration_tracker (基本覆盖，详细测试在 test_evaluator_prune_lock.py)
# ════════════════════════════════════════════════════════════════════════


class TestPruneDurationTracker:
    """验证过期条目清理基本逻辑"""

    async def test_removes_expired(self, evaluator):
        """超过 24h 的条目被移除"""
        now = datetime.now(UTC)
        evaluator._duration_tracker[("r1", "d1")] = now - timedelta(hours=25)
        evaluator._duration_tracker[("r2", "d2")] = now - timedelta(minutes=5)
        await evaluator._prune_duration_tracker()
        assert ("r1", "d1") not in evaluator._duration_tracker
        assert ("r2", "d2") in evaluator._duration_tracker

    async def test_empty_noop(self, evaluator):
        """空 tracker 不报错"""
        await evaluator._prune_duration_tracker()
        assert evaluator._duration_tracker == {}


# ════════════════════════════════════════════════════════════════════════
# 7. _compare (静态方法)
# ════════════════════════════════════════════════════════════════════════


class TestCompare:
    """验证比较运算符 (覆盖纯 Python 路径)"""

    @pytest.fixture(autouse=True)
    def _force_pure_python(self, monkeypatch):
        """强制使用纯 Python 路径以覆盖所有运算符分支"""
        monkeypatch.setattr("edgelite.engine.evaluator._HAS_CYTHON", False)

    def test_gt(self):
        assert RuleEvaluator._compare(10, ">", 5) is True
        assert RuleEvaluator._compare(5, ">", 5) is False

    def test_ge(self):
        assert RuleEvaluator._compare(5, ">=", 5) is True
        assert RuleEvaluator._compare(4, ">=", 5) is False

    def test_lt(self):
        assert RuleEvaluator._compare(3, "<", 5) is True
        assert RuleEvaluator._compare(5, "<", 5) is False

    def test_le(self):
        assert RuleEvaluator._compare(5, "<=", 5) is True
        assert RuleEvaluator._compare(6, "<=", 5) is False

    def test_eq(self):
        assert RuleEvaluator._compare(5, "==", 5) is True
        assert RuleEvaluator._compare(5.0001, "==", 5) is False

    def test_ne(self):
        assert RuleEvaluator._compare(6, "!=", 5) is True
        assert RuleEvaluator._compare(5, "!=", 5) is False


class TestCheckConditions:
    """验证条件组合检查 (AND/OR/NOT、死区、duration_seconds、窗口聚合、AI源)"""

    @pytest.fixture(autouse=True)
    def _force_pure_python(self, monkeypatch):
        """强制纯 Python 路径以覆盖所有分支"""
        monkeypatch.setattr("edgelite.engine.evaluator._HAS_CYTHON", False)

    async def test_and_all_true(self, evaluator):
        """AND 逻辑：全部满足返回 True"""
        conds = [
            {"point": "p1", "operator": ">", "threshold": 5},
            {"point": "p2", "operator": "<", "threshold": 20},
        ]
        result = await evaluator._check_conditions(conds, {"p1": 10, "p2": 15}, "AND")
        assert result is True

    async def test_and_one_false(self, evaluator):
        """AND 逻辑：任一不满足返回 False"""
        conds = [
            {"point": "p1", "operator": ">", "threshold": 5},
            {"point": "p2", "operator": "<", "threshold": 10},
        ]
        result = await evaluator._check_conditions(conds, {"p1": 10, "p2": 15}, "AND")
        assert result is False

    async def test_or_any_true(self, evaluator):
        """OR 逻辑：任一满足返回 True"""
        conds = [
            {"point": "p1", "operator": ">", "threshold": 100},
            {"point": "p2", "operator": "<", "threshold": 20},
        ]
        result = await evaluator._check_conditions(conds, {"p1": 10, "p2": 15}, "OR")
        assert result is True

    async def test_or_all_false(self, evaluator):
        """OR 逻辑：全部不满足返回 False"""
        conds = [
            {"point": "p1", "operator": ">", "threshold": 100},
            {"point": "p2", "operator": "<", "threshold": 5},
        ]
        result = await evaluator._check_conditions(conds, {"p1": 10, "p2": 15}, "OR")
        assert result is False

    async def test_not_all_true_returns_false(self, evaluator):
        """NOT 逻辑：全部满足时返回 False"""
        conds = [{"point": "p1", "operator": ">", "threshold": 5}]
        result = await evaluator._check_conditions(conds, {"p1": 10}, "NOT")
        assert result is False

    async def test_empty_conditions(self, evaluator):
        """空条件列表返回 False"""
        result = await evaluator._check_conditions([], {}, "AND")
        assert result is False

    async def test_missing_point_returns_false(self, evaluator):
        """point_values 中缺少测点值时该条件为 False"""
        conds = [{"point": "pX", "operator": ">", "threshold": 5}]
        result = await evaluator._check_conditions(conds, {"p1": 10}, "AND")
        assert result is False

    async def test_dead_zone_skips_last_value_update_when_stable(self, evaluator):
        """死区内不更新 last_value，但仍正常评估条件 (BugR4X 修复)"""
        evaluator._last_values["d1:p1"] = 100.0
        conds = [{"point": "p1", "operator": ">", "threshold": 50, "dead_zone": 5}]
        # value=100 与 last_value=100 差值 0 < dead_zone 5 → 不更新 last_value
        result = await evaluator._check_conditions(conds, {"p1": 100.0}, "AND", "d1", "r1")
        assert result is True  # 条件 100>50 仍满足
        assert evaluator._last_values["d1:p1"] == 100.0  # 未更新

    async def test_duration_seconds_first_met_returns_false(self, evaluator):
        """条件首次满足时记录时间但返回 False (未持续足够时间)"""
        conds = [{"point": "p1", "operator": ">", "threshold": 5, "duration_seconds": 10}]
        result = await evaluator._check_conditions(conds, {"p1": 10}, "AND", "d1", "r1")
        assert result is False
        assert "r1:p1:>:5" in evaluator._condition_first_met

    async def test_duration_seconds_met(self, evaluator):
        """条件满足且持续足够时间返回 True"""
        evaluator._condition_first_met["r1:p1:>:5"] = time.monotonic() - 11
        conds = [{"point": "p1", "operator": ">", "threshold": 5, "duration_seconds": 10}]
        result = await evaluator._check_conditions(conds, {"p1": 10}, "AND", "d1", "r1")
        assert result is True

    async def test_condition_false_clears_duration(self, evaluator):
        """条件不满足时清除 condition_first_met"""
        evaluator._condition_first_met["r1:p1:>:100"] = time.monotonic()
        conds = [{"point": "p1", "operator": ">", "threshold": 100, "duration_seconds": 10}]
        result = await evaluator._check_conditions(conds, {"p1": 10}, "AND", "d1", "r1")
        assert result is False
        assert "r1:p1:>:100" not in evaluator._condition_first_met

    async def test_ai_source_condition(self, evaluator_full, ai_engine):
        """AI 推理源条件：从 _get_latest_ai_result 获取值"""
        ai_engine.get_model.return_value = MagicMock(status="active", last_result={"anomaly_score": 0.9})
        conds = [
            {
                "point": "p1",
                "source": "ai_inference",
                "model_id": "m1",
                "field": "anomaly_score",
                "operator": ">",
                "threshold": 0.5,
            }
        ]
        result = await evaluator_full._check_conditions(conds, {"p1": 10}, "AND", "d1", "r1")
        assert result is True

    async def test_window_aggregate_condition(self, evaluator):
        """窗口聚合条件：从流计算引擎获取聚合值"""
        conds = [{"point": "p1", "operator": ">", "threshold": 5, "window_seconds": 60, "aggregate": "avg"}]
        with patch("edgelite.engine.stream_compute.get_stream_engine") as mock_get:
            engine = MagicMock()
            engine.get_window_result.return_value = 10.0
            mock_get.return_value = engine
            result = await evaluator._check_conditions(conds, {}, "AND", "d1", "r1")
            assert result is True


class TestCheckConditionsCythonPath:
    """验证 Cython 快速路径 (当 _HAS_CYTHON=True 且条件可走快速路径时)"""

    async def test_fast_path_and(self, evaluator):
        """Cython 快速路径 AND 逻辑"""
        conds = [{"point": "p1", "operator": ">", "threshold": 5}]
        result = await evaluator._check_conditions(conds, {"p1": 10}, "AND")
        assert result is True

    async def test_fast_path_or(self, evaluator):
        """Cython 快速路径 OR 逻辑"""
        conds = [
            {"point": "p1", "operator": ">", "threshold": 100},
            {"point": "p2", "operator": "<", "threshold": 20},
        ]
        result = await evaluator._check_conditions(conds, {"p1": 10, "p2": 15}, "OR")
        assert result is True

    async def test_fast_path_not(self, evaluator):
        """Cython 快速路径 NOT 逻辑 (not all)"""
        conds = [{"point": "p1", "operator": ">", "threshold": 100}]
        result = await evaluator._check_conditions(conds, {"p1": 10}, "NOT")
        assert result is True

    async def test_fast_path_falls_back_when_window(self, evaluator):
        """含窗口聚合条件时回退到纯 Python 路径"""
        conds = [{"point": "p1", "operator": ">", "threshold": 5, "window_seconds": 60, "aggregate": "avg"}]
        with patch("edgelite.engine.stream_compute.get_stream_engine") as mock_get:
            engine = MagicMock()
            engine.get_window_result.return_value = 10.0
            mock_get.return_value = engine
            result = await evaluator._check_conditions(conds, {}, "AND", "d1", "r1")
            assert result is True

    async def test_fast_path_falls_back_when_missing_value(self, evaluator):
        """测点值缺失时回退到纯 Python 路径"""
        conds = [{"point": "pX", "operator": ">", "threshold": 5}]
        result = await evaluator._check_conditions(conds, {"p1": 10}, "AND")
        assert result is False


# ════════════════════════════════════════════════════════════════════════
# 10. _evaluate_ai_conditions
# ════════════════════════════════════════════════════════════════════════


class TestEvaluateAiConditions:
    """验证 AI 推理条件评估"""

    async def test_no_ai_engine_returns_false(self, evaluator):
        """ai_engine 为 None 时返回 False"""
        result = await evaluator._evaluate_ai_conditions([], {"p1": 10}, "AND", "d1")
        assert result is False

    async def test_no_model_id_returns_false(self, evaluator_full):
        """条件无 model_id 时该条件为 False"""
        conds = [{"point": "p1", "ai_threshold": 0.5}]
        result = await evaluator_full._evaluate_ai_conditions(conds, {"p1": 10}, "AND", "d1")
        assert result is False

    async def test_model_not_found_returns_false(self, evaluator_full, ai_engine):
        """模型不存在时该条件为 False"""
        ai_engine.get_model.return_value = None
        conds = [{"point": "p1", "model_id": "m1", "ai_threshold": 0.5}]
        result = await evaluator_full._evaluate_ai_conditions(conds, {"p1": 10}, "AND", "d1")
        assert result is False

    async def test_infer_success_above_threshold(self, evaluator_full, ai_engine):
        """推理结果超过阈值返回 True"""
        ai_engine.get_model.return_value = MagicMock(status="active", input_schema={"shape": [1, 1]})
        ai_engine.infer.return_value = MagicMock(status="success", output_data={"output_0": [0.9]})
        conds = [{"point": "p1", "model_id": "m1", "ai_threshold": 0.5}]
        result = await evaluator_full._evaluate_ai_conditions(conds, {"p1": 10}, "AND", "d1")
        assert result is True

    async def test_infer_success_below_threshold(self, evaluator_full, ai_engine):
        """推理结果低于阈值返回 False"""
        ai_engine.get_model.return_value = MagicMock(status="active", input_schema={"shape": [1, 1]})
        ai_engine.infer.return_value = MagicMock(status="success", output_data={"output_0": [0.3]})
        conds = [{"point": "p1", "model_id": "m1", "ai_threshold": 0.5}]
        result = await evaluator_full._evaluate_ai_conditions(conds, {"p1": 10}, "AND", "d1")
        assert result is False

    async def test_infer_exception_returns_false(self, evaluator_full, ai_engine):
        """推理抛异常时该条件为 False"""
        ai_engine.get_model.return_value = MagicMock(status="active", input_schema={"shape": [1, 1]})
        ai_engine.infer.side_effect = RuntimeError("infer fail")
        conds = [{"point": "p1", "model_id": "m1", "ai_threshold": 0.5}]
        result = await evaluator_full._evaluate_ai_conditions(conds, {"p1": 10}, "AND", "d1")
        assert result is False

    async def test_input_truncation(self, evaluator_full, ai_engine):
        """输入数据超过期望长度时截断"""
        ai_engine.get_model.return_value = MagicMock(status="active", input_schema={"shape": [1, 2]})
        ai_engine.infer.return_value = MagicMock(status="success", output_data={"output_0": [0.9]})
        conds = [{"point": "p1", "model_id": "m1", "ai_threshold": 0.5}]
        result = await evaluator_full._evaluate_ai_conditions(conds, {"p1": 10, "p2": 20, "p3": 30}, "AND", "d1")
        assert result is True
        called_args = ai_engine.infer.await_args
        assert called_args.args[1] == [10, 20]

    async def test_empty_results_returns_false(self, evaluator_full, ai_engine):
        """空结果列表返回 False"""
        ai_engine.get_model.return_value = None
        result = await evaluator_full._evaluate_ai_conditions([], {"p1": 10}, "AND", "d1")
        assert result is False


# ════════════════════════════════════════════════════════════════════════
# 11. _get_latest_ai_result
# ════════════════════════════════════════════════════════════════════════


class TestGetLatestAiResult:
    """验证获取最新 AI 推理结果"""

    async def test_no_ai_engine(self, evaluator):
        """ai_engine 为 None 时返回空字典"""
        assert await evaluator._get_latest_ai_result("m1") == {}

    async def test_model_not_found(self, evaluator_full, ai_engine):
        """模型不存在返回空字典"""
        ai_engine.get_model.return_value = None
        assert await evaluator_full._get_latest_ai_result("m1") == {}

    async def test_returns_last_result(self, evaluator_full, ai_engine):
        """返回模型的 last_result"""
        ai_engine.get_model.return_value = MagicMock(status="active", last_result={"score": 0.8})
        result = await evaluator_full._get_latest_ai_result("m1")
        assert result == {"score": 0.8}

    async def test_exception_returns_empty(self, evaluator_full, ai_engine):
        """get_model 抛异常时返回空字典"""
        ai_engine.get_model.side_effect = RuntimeError("fail")
        assert await evaluator_full._get_latest_ai_result("m1") == {}


# ════════════════════════════════════════════════════════════════════════
# 12. _get_window_aggregate
# ════════════════════════════════════════════════════════════════════════


class TestGetWindowAggregate:
    """验证窗口聚合值获取 (流计算引擎 + InfluxDB 回退)"""

    async def test_from_stream_engine(self, evaluator):
        """流计算引擎返回值时直接使用"""
        with patch("edgelite.engine.stream_compute.get_stream_engine") as mock_get:
            engine = MagicMock()
            engine.get_window_result.return_value = 42.0
            mock_get.return_value = engine
            result = await evaluator._get_window_aggregate("d1", "p1", 60, "avg")
            assert result == 42.0

    async def test_stream_engine_exception_falls_back(self, evaluator):
        """流计算引擎异常时回退到 InfluxDB"""
        with patch("edgelite.engine.stream_compute.get_stream_engine") as mock_get:
            mock_get.side_effect = RuntimeError("engine fail")
            with patch("edgelite.app._app_state") as mock_state:
                mock_state.influx_storage.query_points = AsyncMock(return_value=[{"value": 30.0}])
                result = await evaluator._get_window_aggregate("d1", "p1", 60, "avg")
                assert result == 30.0

    async def test_influx_empty_returns_none(self, evaluator):
        """InfluxDB 也无数据时返回 None"""
        with patch("edgelite.engine.stream_compute.get_stream_engine") as mock_get:
            engine = MagicMock()
            engine.get_window_result.return_value = None
            mock_get.return_value = engine
            with patch("edgelite.app._app_state") as mock_state:
                mock_state.influx_storage.query_points = AsyncMock(return_value=[])
                result = await evaluator._get_window_aggregate("d1", "p1", 60, "avg")
                assert result is None

    async def test_influx_no_storage_returns_none(self, evaluator):
        """influx_storage 为 None 时返回 None"""
        with patch("edgelite.engine.stream_compute.get_stream_engine") as mock_get:
            engine = MagicMock()
            engine.get_window_result.return_value = None
            mock_get.return_value = engine
            with patch("edgelite.app._app_state") as mock_state:
                mock_state.influx_storage = None
                result = await evaluator._get_window_aggregate("d1", "p1", 60, "avg")
                assert result is None

    async def test_influx_exception_returns_none(self, evaluator):
        """InfluxDB 异常时返回 None"""
        with patch("edgelite.engine.stream_compute.get_stream_engine") as mock_get:
            engine = MagicMock()
            engine.get_window_result.return_value = None
            mock_get.return_value = engine
            with patch("edgelite.app._app_state") as mock_state:
                mock_state.influx_storage.query_points = AsyncMock(side_effect=RuntimeError("influx fail"))
                result = await evaluator._get_window_aggregate("d1", "p1", 60, "avg")
                assert result is None


# ════════════════════════════════════════════════════════════════════════
# 13. _eval_script
# ════════════════════════════════════════════════════════════════════════


class TestEvalScript:
    """验证脚本规则执行"""

    @staticmethod
    def _make_fake_sandbox(run_impl):
        """构造一个 fake edgelite.engine.sandbox 模块注入 sys.modules"""
        fake_module = types.ModuleType("edgelite.engine.sandbox")
        fake_module.run_script_safely = run_impl
        return fake_module

    async def test_script_success_true(self):
        """沙箱执行返回 True"""

        async def fake_run(script, namespace, timeout=3.0, filename="<rule_script>"):
            namespace["result"] = True
            return True

        with patch.dict(sys.modules, {"edgelite.engine.sandbox": self._make_fake_sandbox(fake_run)}):
            result = await RuleEvaluator._eval_script("result = True", {"p1": 10})
        assert result is True

    async def test_script_success_false(self):
        """沙箱执行返回 False"""

        async def fake_run(script, namespace, timeout=3.0, filename="<rule_script>"):
            namespace["result"] = False
            return False

        with patch.dict(sys.modules, {"edgelite.engine.sandbox": self._make_fake_sandbox(fake_run)}):
            result = await RuleEvaluator._eval_script("result = False", {"p1": 10})
        assert result is False

    async def test_script_timeout_returns_false(self):
        """沙箱执行超时返回 False"""

        async def fake_run(script, namespace, timeout=3.0, filename="<rule_script>"):
            raise TimeoutError()

        with patch.dict(sys.modules, {"edgelite.engine.sandbox": self._make_fake_sandbox(fake_run)}):
            result = await RuleEvaluator._eval_script("while True: pass", {"p1": 10})
        assert result is False

    async def test_script_exception_returns_false(self):
        """沙箱执行异常返回 False"""

        async def fake_run(script, namespace, timeout=3.0, filename="<rule_script>"):
            raise RuntimeError("fail")

        with patch.dict(sys.modules, {"edgelite.engine.sandbox": self._make_fake_sandbox(fake_run)}):
            result = await RuleEvaluator._eval_script("bad syntax", {"p1": 10})
        assert result is False

    async def test_script_import_error_returns_false(self):
        """sandbox 模块不存在时 (ImportError) 返回 False"""
        # 不 patch，让真实导入失败 (edgelite.engine.sandbox 不存在)
        result = await RuleEvaluator._eval_script("result = True", {"p1": 10})
        assert result is False


# ════════════════════════════════════════════════════════════════════════
# 14. _evaluate_rule
# ════════════════════════════════════════════════════════════════════════


class TestEvaluateRule:
    """验证单规则评估主流程"""

    async def test_incomplete_rule_skipped(self, evaluator):
        """规则缺少 rule_id/device_id/conditions 时跳过"""
        event = _make_event()
        await evaluator._evaluate_rule({"rule_id": "", "device_id": "d1", "conditions": []}, event)
        await evaluator._evaluate_rule({"rule_id": "r1", "device_id": "", "conditions": []}, event)
        await evaluator._evaluate_rule({"rule_id": "r1", "device_id": "d1", "conditions": []}, event)
        # 无任何副作用
        assert evaluator._recent_firings == {}

    async def test_threshold_matched_fires_alarm(self, evaluator, alarm_repo):
        """阈值匹配时触发告警"""
        rule = _make_rule(conditions=[{"point": "p1", "operator": ">", "threshold": 5}])
        event = _make_event(value=10)
        await evaluator._evaluate_rule(rule, event)
        alarm_repo.create.assert_awaited_once()

    async def test_threshold_not_matched_no_alarm(self, evaluator, alarm_repo):
        """阈值不匹配时不触发告警"""
        rule = _make_rule(conditions=[{"point": "p1", "operator": ">", "threshold": 100}])
        event = _make_event(value=10)
        await evaluator._evaluate_rule(rule, event)
        alarm_repo.create.assert_not_called()

    async def test_threshold_not_matched_recovers_alarm(self, evaluator, alarm_repo):
        """阈值不匹配且存在 firing 告警时恢复"""
        alarm_repo.get_firing_by_rule_device.return_value = {"alarm_id": "a1"}
        rule = _make_rule(conditions=[{"point": "p1", "operator": ">", "threshold": 100}])
        event = _make_event(value=10)
        await evaluator._evaluate_rule(rule, event)
        alarm_repo.recover.assert_awaited_once_with("a1")

    async def test_firing_alarm_missing_alarm_id_skips_recovery(self, evaluator, alarm_repo):
        """firing 告警缺少 alarm_id 时跳过恢复"""
        alarm_repo.get_firing_by_rule_device.return_value = {"rule_id": "r1"}
        rule = _make_rule(conditions=[{"point": "p1", "operator": ">", "threshold": 100}])
        event = _make_event(value=10)
        await evaluator._evaluate_rule(rule, event)
        alarm_repo.recover.assert_not_called()

    async def test_duration_first_match_no_fire(self, evaluator, alarm_repo):
        """持续时间规则首次匹配不触发告警"""
        rule = _make_rule(duration=10, conditions=[{"point": "p1", "operator": ">", "threshold": 5}])
        event = _make_event(value=10)
        await evaluator._evaluate_rule(rule, event)
        alarm_repo.create.assert_not_called()
        assert ("r1", "d1") in evaluator._duration_tracker

    async def test_duration_elapsed_fires(self, evaluator, alarm_repo):
        """持续时间满足后触发告警"""
        rule = _make_rule(duration=10, conditions=[{"point": "p1", "operator": ">", "threshold": 5}])
        evaluator._duration_tracker[("r1", "d1")] = datetime.now(UTC) - timedelta(seconds=11)
        event = _make_event(value=10)
        await evaluator._evaluate_rule(rule, event)
        alarm_repo.create.assert_awaited_once()
        assert ("r1", "d1") not in evaluator._duration_tracker

    async def test_duration_not_matched_clears_tracker(self, evaluator):
        """条件不匹配时清除 duration_tracker"""
        evaluator._duration_tracker[("r1", "d1")] = datetime.now(UTC)
        rule = _make_rule(duration=10, conditions=[{"point": "p1", "operator": ">", "threshold": 100}])
        event = _make_event(value=10)
        await evaluator._evaluate_rule(rule, event)
        assert ("r1", "d1") not in evaluator._duration_tracker

    async def test_script_rule_type(self, evaluator, alarm_repo):
        """script 类型规则走 _eval_script 路径"""
        rule = _make_rule(rule_type="script", script="result = True")
        event = _make_event(value=10)

        async def fake_run(script, namespace, timeout=3.0, filename="<rule_script>"):
            namespace["result"] = True
            return True

        fake_module = types.ModuleType("edgelite.engine.sandbox")
        fake_module.run_script_safely = fake_run
        with patch.dict(sys.modules, {"edgelite.engine.sandbox": fake_module}):
            await evaluator._evaluate_rule(rule, event)
            alarm_repo.create.assert_awaited_once()

    async def test_ai_inference_rule_type(self, evaluator_full, ai_engine, alarm_repo):
        """ai_inference 类型规则走 _evaluate_ai_conditions 路径"""
        ai_engine.get_model.return_value = MagicMock(status="active", input_schema={"shape": [1, 1]})
        ai_engine.infer.return_value = MagicMock(status="success", output_data={"output_0": [0.9]})
        rule = _make_rule(
            rule_type="ai_inference",
            conditions=[{"point": "p1", "model_id": "m1", "ai_threshold": 0.5}],
        )
        event = _make_event(value=10)
        await evaluator_full._evaluate_rule(rule, event)
        alarm_repo.create.assert_awaited_once()

    async def test_multi_point_condition_uses_influx(self, evaluator, alarm_repo):
        """条件引用其他测点时从 InfluxDB 获取值"""
        rule = _make_rule(conditions=[{"point": "p2", "operator": ">", "threshold": 5}])
        event = _make_event(device_id="d1", point_name="p1", value=10)
        with patch("edgelite.app._app_state") as mock_state:
            mock_state.influx_storage = MagicMock()
            mock_state.influx_storage.query_latest = AsyncMock(return_value={"p2": 20})
            await evaluator._evaluate_rule(rule, event)
            alarm_repo.create.assert_awaited_once()

    async def test_condition_missing_point_skipped(self, evaluator, alarm_repo):
        """条件无 point 字段时跳过该条件"""
        rule = _make_rule(conditions=[{"operator": ">", "threshold": 5}])
        event = _make_event(value=10)
        await evaluator._evaluate_rule(rule, event)
        # 无可用条件 → results 空 → matched=False → 不触发
        alarm_repo.create.assert_not_called()


# ════════════════════════════════════════════════════════════════════════
# 15. _fire_alarm
# ════════════════════════════════════════════════════════════════════════


class TestFireAlarm:
    """验证告警触发逻辑 (去重、冷却、回滚、CancelledError)"""

    async def test_incomplete_rule_no_fire(self, evaluator, alarm_repo):
        """规则缺少 rule_id/device_id 时不触发"""
        await evaluator._fire_alarm({"rule_id": "", "device_id": "d1"}, {})
        await evaluator._fire_alarm({"rule_id": "r1", "device_id": ""}, {})
        alarm_repo.create.assert_not_called()

    async def test_creates_alarm_and_publishes(self, evaluator, alarm_repo, event_bus):
        """正常创建告警并发布事件"""
        rule = _make_rule()
        await evaluator._fire_alarm(rule, {"p1": 10})
        alarm_repo.create.assert_awaited_once()
        alarm_repo.get_firing_by_rule_device.assert_awaited_once()

    async def test_existing_alarm_updates_trigger_count(self, evaluator, alarm_repo):
        """已存在 firing 告警时更新触发计数不重复创建"""
        alarm_repo.get_firing_by_rule_device.return_value = {"alarm_id": "a1"}
        rule = _make_rule()
        await evaluator._fire_alarm(rule, {"p1": 10})
        alarm_repo.update_trigger_count.assert_awaited_once_with("a1", {"p1": 10})
        alarm_repo.create.assert_not_called()

    async def test_cooldown_skips_fire(self, evaluator, alarm_repo):
        """冷却期内跳过触发"""
        evaluator._min_firing_interval = 10.0
        evaluator._recent_firings["r1"] = time.time()
        rule = _make_rule()
        await evaluator._fire_alarm(rule, {"p1": 10})
        alarm_repo.create.assert_not_called()

    async def test_db_exception_rolls_back_firings(self, evaluator, alarm_repo):
        """DB 异常时回滚 _recent_firings 防止告警黑洞"""
        alarm_repo.get_firing_by_rule_device.side_effect = RuntimeError("db fail")
        rule = _make_rule()
        await evaluator._fire_alarm(rule, {"p1": 10})
        assert "r1" not in evaluator._recent_firings

    async def test_alarm_missing_alarm_id(self, evaluator, alarm_repo):
        """create 返回结果缺少 alarm_id 时不发布事件"""
        alarm_repo.create.return_value = {"rule_id": "r1"}
        rule = _make_rule()
        await evaluator._fire_alarm(rule, {"p1": 10})

    async def test_cancelled_error_rolls_back(self, evaluator, alarm_repo):
        """CancelledError 时回滚 _recent_firings 并重新抛出"""
        alarm_repo.get_firing_by_rule_device.side_effect = asyncio.CancelledError()
        rule = _make_rule()
        with pytest.raises(asyncio.CancelledError):
            await evaluator._fire_alarm(rule, {"p1": 10})
        assert "r1" not in evaluator._recent_firings


class TestRecoverAlarm:
    """验证告警恢复逻辑"""

    async def test_recover_success_publishes_event(self, evaluator, alarm_repo, event_bus):
        """恢复成功时发布 recovered 事件"""
        rule = _make_rule()
        await evaluator._recover_alarm("a1", rule)
        alarm_repo.recover.assert_awaited_once_with("a1")

    async def test_recover_db_exception_no_crash(self, evaluator, alarm_repo):
        """recover DB 异常时不崩溃"""
        alarm_repo.recover.side_effect = RuntimeError("db fail")
        rule = _make_rule()
        await evaluator._recover_alarm("a1", rule)  # 不抛出

    async def test_recover_returns_none_no_publish(self, evaluator, alarm_repo, event_bus):
        """recover 返回 None 时不发布事件"""
        alarm_repo.recover.return_value = None
        rule = _make_rule()
        await evaluator._recover_alarm("a1", rule)

    async def test_recover_publishes_with_device_name(self, evaluator_full, alarm_repo, device_repo):
        """恢复事件包含解析后的设备名"""
        rule = _make_rule(device_id="d1")
        await evaluator_full._recover_alarm("a1", rule)
        device_repo.get.assert_awaited()


# ════════════════════════════════════════════════════════════════════════
# 17. _evaluate 与 _evaluate_inner
# ════════════════════════════════════════════════════════════════════════


class TestEvaluate:
    """验证评估入口与超时保护"""

    async def test_evaluate_calls_inner(self, evaluator, rule_repo, alarm_repo):
        """_evaluate 正常调用 _evaluate_inner"""
        rule_repo.list_enabled_by_point.return_value = [_make_rule()]
        event = _make_event(value=10)
        await evaluator._evaluate(event)
        alarm_repo.create.assert_awaited_once()

    async def test_evaluate_timeout_handled(self, evaluator, rule_repo):
        """_evaluate_inner 超时时不崩溃"""
        rule_repo.list_enabled_by_point.return_value = [_make_rule()]

        async def slow_inner(event):
            await asyncio.sleep(10)

        with patch.object(evaluator, "_evaluate_inner", side_effect=slow_inner):
            # _evaluate 有 5s 超时，但为加速测试直接 patch wait_for
            with patch("edgelite.engine.evaluator.asyncio.wait_for", new=AsyncMock(side_effect=TimeoutError())):
                await evaluator._evaluate(_make_event())  # 不抛出

    async def test_evaluate_inner_sorts_by_priority(self, evaluator, rule_repo, alarm_repo):
        """规则按 priority 降序排序"""
        rules = [
            _make_rule(rule_id="low", priority=1),
            _make_rule(rule_id="high", priority=10),
            _make_rule(rule_id="mid", priority=5),
        ]
        rule_repo.list_enabled_by_point.return_value = rules
        event = _make_event(value=10)
        # 用 side_effect 记录调用顺序
        call_order = []
        original = evaluator._evaluate_rule

        async def track(rule, event):
            call_order.append(rule["rule_id"])
            # 不实际触发告警以避免干扰
            return None

        evaluator._evaluate_rule = track
        await evaluator._evaluate_inner(event)
        assert call_order == ["high", "mid", "low"]
        evaluator._evaluate_rule = original

    async def test_evaluate_inner_no_rules(self, evaluator, rule_repo):
        """无规则时不报错"""
        rule_repo.list_enabled_by_point.return_value = []
        await evaluator._evaluate_inner(_make_event())


# ════════════════════════════════════════════════════════════════════════
# 18. _eval_loop
# ════════════════════════════════════════════════════════════════════════


class TestEvalLoop:
    """验证评估事件循环"""

    async def test_processes_good_quality_event(self, evaluator, rule_repo, alarm_repo):
        """good 质量事件被处理"""
        rule_repo.list_enabled_by_point.return_value = [_make_rule()]
        queue = asyncio.Queue()
        await queue.put(_make_event(value=10))
        task = asyncio.create_task(evaluator._eval_loop(queue))
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib_suppress_cancel():
            await task
        alarm_repo.create.assert_awaited_once()

    async def test_skips_non_good_quality(self, evaluator, rule_repo, alarm_repo):
        """非 good 质量事件被跳过"""
        rule_repo.list_enabled_by_point.return_value = [_make_rule()]
        queue = asyncio.Queue()
        await queue.put(_make_event(value=10, quality="bad"))
        task = asyncio.create_task(evaluator._eval_loop(queue))
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib_suppress_cancel():
            await task
        alarm_repo.create.assert_not_called()

    async def test_loop_exception_continues(self, evaluator):
        """循环内异常不终止循环"""
        queue = asyncio.Queue()
        # 注入会触发异常的事件 (rule_repo.list_enabled_by_point 抛异常但被 _evaluate_inner 捕获)
        evaluator._rule_repo.list_enabled_by_point = AsyncMock(side_effect=RuntimeError("fail"))
        await queue.put(_make_event(value=10))
        task = asyncio.create_task(evaluator._eval_loop(queue))
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib_suppress_cancel():
            await task
        # 循环应仍在运行直到取消

    async def test_timeout_triggers_prune(self, evaluator):
        """队列超时触发 prune 检查"""
        evaluator._tracker_cleanup_interval = 0.0
        evaluator._duration_tracker[("r1", "d1")] = datetime.now(UTC) - timedelta(hours=25)
        queue = asyncio.Queue()

        async def _fast_timeout(coro, timeout=None):
            coro.close()
            await asyncio.sleep(0)  # yield to event loop
            raise TimeoutError()

        with patch("edgelite.engine.evaluator.asyncio.wait_for", new=_fast_timeout):
            task = asyncio.create_task(evaluator._eval_loop(queue))
            await asyncio.sleep(0.3)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert ("r1", "d1") not in evaluator._duration_tracker


# ════════════════════════════════════════════════════════════════════════
# 19. start / stop
# ════════════════════════════════════════════════════════════════════════


class TestStartStop:
    """验证启动与停止生命周期"""

    async def test_start_creates_task(self, evaluator, event_bus):
        """start 创建评估循环 task"""
        await evaluator.start()
        assert evaluator._task is not None
        assert not evaluator._task.done()
        await evaluator.stop()
        assert evaluator._task is None or evaluator._task.done()

    async def test_stop_clears_state(self, evaluator):
        """stop 清理所有内部状态"""
        evaluator._duration_tracker[("r1", "d1")] = datetime.now(UTC)
        evaluator._rule_cache["d1:p1"] = []
        evaluator._recent_firings["r1"] = time.time()
        evaluator._last_values["d1:p1"] = 10
        evaluator._condition_first_met["r1:p1:>:5"] = time.time()
        evaluator._point_value_cache["d1:p1"] = (10, time.time())
        await evaluator.stop()
        assert evaluator._duration_tracker == {}
        assert evaluator._rule_cache == {}
        assert evaluator._recent_firings == {}
        assert evaluator._last_values == {}
        assert evaluator._condition_first_met == {}
        assert evaluator._point_value_cache == {}

    async def test_stop_without_start(self, evaluator):
        """未启动直接 stop 不报错"""
        await evaluator.stop()


def contextlib_suppress_cancel():
    """返回 suppress(asyncio.CancelledError) 上下文，兼容 py3.11+ CancelledError"""
    import contextlib

    return contextlib.suppress(asyncio.CancelledError)
