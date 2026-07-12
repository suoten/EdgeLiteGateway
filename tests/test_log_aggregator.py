"""日志聚合器测试 - 内存日志收集与查询

覆盖 engine/log_aggregator.py：
- _LogAggregator: add_entry/get_entries/clear/install/uninstall
- get_entries: level 过滤/limit 截断/since 时间过滤/倒序返回
- _AggregatorHandler: emit 转发
- get_log_aggregator: 单例
"""

from __future__ import annotations

import logging
import time

import pytest

from edgelite.engine.log_aggregator import (
    _AggregatorHandler,
    _LogAggregator,
    _aggregator,
    get_log_aggregator,
)


@pytest.fixture(autouse=True)
def _reset_aggregator_singleton():
    """每个测试重置模块级单例，避免相互干扰"""
    import edgelite.engine.log_aggregator as mod

    orig = mod._aggregator
    mod._aggregator = None
    yield
    # 清理：卸载 handler
    if orig is not None:
        try:
            orig.uninstall()
        except Exception:
            pass
    mod._aggregator = orig


def _make_record(level: int, msg: str, name: str = "test") -> logging.LogRecord:
    """构造 LogRecord"""
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=(),
        exc_info=None,
    )


class TestAddEntry:
    def test_add_single_entry(self):
        agg = _LogAggregator(max_entries=100)
        agg.add_entry(_make_record(logging.INFO, "hello"))
        entries = agg.get_entries(limit=10)
        assert len(entries) == 1
        assert entries[0]["message"] == "hello"

    def test_entry_fields(self):
        agg = _LogAggregator()
        agg.add_entry(_make_record(logging.WARNING, "warn msg", "mylogger"))
        entries = agg.get_entries()
        e = entries[0]
        assert e["level"] == "WARNING"
        assert e["logger"] == "mylogger"
        assert e["message"] == "warn msg"
        assert "timestamp" in e
        assert "module" in e
        assert "line" in e

    def test_max_entries_cap(self):
        agg = _LogAggregator(max_entries=3)
        for i in range(5):
            agg.add_entry(_make_record(logging.INFO, f"msg{i}"))
        entries = agg.get_entries(limit=100)
        assert len(entries) == 3  # deque maxlen=3


class TestGetEntries:
    def test_level_filter(self):
        agg = _LogAggregator()
        agg.add_entry(_make_record(logging.INFO, "info msg"))
        agg.add_entry(_make_record(logging.ERROR, "error msg"))
        agg.add_entry(_make_record(logging.WARNING, "warn msg"))

        errors = agg.get_entries(level="ERROR")
        assert len(errors) == 1
        assert errors[0]["message"] == "error msg"

    def test_limit_truncation(self):
        agg = _LogAggregator(max_entries=100)
        for i in range(10):
            agg.add_entry(_make_record(logging.INFO, f"msg{i}"))
        entries = agg.get_entries(limit=3)
        assert len(entries) == 3

    def test_reverse_order(self):
        """get_entries 返回倒序（最新在前）"""
        agg = _LogAggregator(max_entries=100)
        agg.add_entry(_make_record(logging.INFO, "first"))
        agg.add_entry(_make_record(logging.INFO, "second"))
        agg.add_entry(_make_record(logging.INFO, "third"))
        entries = agg.get_entries(limit=10)
        assert entries[0]["message"] == "third"
        assert entries[2]["message"] == "first"

    def test_since_filter(self):
        agg = _LogAggregator()
        agg.add_entry(_make_record(logging.INFO, "old"))
        cutoff = time.time() + 0.01
        time.sleep(0.02)
        agg.add_entry(_make_record(logging.INFO, "new"))
        entries = agg.get_entries(since=cutoff)
        assert len(entries) == 1
        assert entries[0]["message"] == "new"

    def test_empty_returns_empty_list(self):
        agg = _LogAggregator()
        assert agg.get_entries() == []

    def test_no_level_returns_all(self):
        agg = _LogAggregator()
        agg.add_entry(_make_record(logging.INFO, "a"))
        agg.add_entry(_make_record(logging.ERROR, "b"))
        assert len(agg.get_entries()) == 2


class TestClear:
    def test_clear_empties_entries(self):
        agg = _LogAggregator()
        agg.add_entry(_make_record(logging.INFO, "msg"))
        agg.clear()
        assert agg.get_entries() == []


class TestInstallUninstall:
    def test_install_adds_handler(self):
        agg = _LogAggregator()
        agg.install()
        assert agg._handler is not None
        root = logging.getLogger()
        assert agg._handler in root.handlers
        agg.uninstall()

    def test_uninstall_removes_handler(self):
        agg = _LogAggregator()
        agg.install()
        agg.uninstall()
        assert agg._handler is None
        root = logging.getLogger()
        # handler 不再在 root handlers 中
        assert all(not isinstance(h, _AggregatorHandler) or h._aggregator is not agg for h in root.handlers)

    def test_install_idempotent(self):
        agg = _LogAggregator()
        agg.install()
        handler1 = agg._handler
        agg.install()  # 再次 install 不创建新 handler
        assert agg._handler is handler1
        agg.uninstall()

    def test_uninstall_without_install_noop(self):
        agg = _LogAggregator()
        agg.uninstall()  # 不抛异常
        assert agg._handler is None


class TestAggregatorHandler:
    def test_emit_forwards_to_aggregator(self):
        agg = _LogAggregator()
        handler = _AggregatorHandler(agg)
        record = _make_record(logging.INFO, "via handler")
        handler.emit(record)
        entries = agg.get_entries()
        assert len(entries) == 1
        assert entries[0]["message"] == "via handler"

    def test_emit_swallows_exceptions(self):
        """emit 内部异常不应传播"""
        agg = _LogAggregator()

        def _bad_add(record):
            raise ValueError("boom")

        agg.add_entry = _bad_add  # type: ignore[method-assign]
        handler = _AggregatorHandler(agg)
        handler.emit(_make_record(logging.INFO, "msg"))  # 不抛异常


class TestGetLogAggregator:
    def test_singleton_returns_same_instance(self):
        a1 = get_log_aggregator()
        a2 = get_log_aggregator()
        assert a1 is a2
        a1.uninstall()

    def test_singleton_installs_handler(self):
        agg = get_log_aggregator()
        assert agg._handler is not None
        agg.uninstall()
