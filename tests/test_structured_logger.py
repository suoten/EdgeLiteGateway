"""结构化日志测试 - JSON 格式/上下文注入/异常处理

覆盖 engine/structured_logger.py：
- StructuredFormatter: JSON 输出/context 注入/traceback 控制/extra_data
- ContextFilter: set_context/clear_context/filter 线程安全
- StructuredLogger: setup/set_context/clear_context/get_logger
- log_with_data: 带 data 字段的日志
"""

from __future__ import annotations

import json
import logging

import pytest

from edgelite.engine.structured_logger import (
    ContextFilter,
    StructuredFormatter,
    StructuredLogger,
    log_with_data,
)


def _make_record(msg: str = "test", level: int = logging.INFO, **extra) -> logging.LogRecord:
    """构造 LogRecord，可附加额外属性"""
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname=__file__,
        lineno=42,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


class TestStructuredFormatter:
    def test_basic_json_output(self):
        fmt = StructuredFormatter()
        record = _make_record("hello world")
        output = fmt.format(record)
        data = json.loads(output)
        assert data["message"] == "hello world"
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["module"] == "test_structured_logger"
        assert data["line"] == 42
        assert "timestamp" in data

    def test_include_context_true(self):
        fmt = StructuredFormatter(include_context=True)
        record = _make_record("msg", request_id="req-123", user_id="user-456")
        data = json.loads(fmt.format(record))
        assert data["request_id"] == "req-123"
        assert data["user_id"] == "user-456"

    def test_include_context_false(self):
        fmt = StructuredFormatter(include_context=False)
        record = _make_record("msg", request_id="req-123")
        data = json.loads(fmt.format(record))
        assert "request_id" not in data

    def test_context_empty_values_skipped(self):
        """context 值为空时不写入（if value: 判断）"""
        fmt = StructuredFormatter(include_context=True)
        record = _make_record("msg", request_id="", user_id=None)
        data = json.loads(fmt.format(record))
        assert "request_id" not in data
        assert "user_id" not in data

    def test_all_context_keys(self):
        fmt = StructuredFormatter(include_context=True)
        record = _make_record(
            "msg",
            request_id="r",
            user_id="u",
            device_id="d",
            trace_id="t",
            span_id="s",
        )
        data = json.loads(fmt.format(record))
        assert data["request_id"] == "r"
        assert data["user_id"] == "u"
        assert data["device_id"] == "d"
        assert data["trace_id"] == "t"
        assert data["span_id"] == "s"

    def test_exception_without_traceback(self):
        """include_traceback=False → 异常信息有 type/message 但无 traceback"""
        fmt = StructuredFormatter(include_traceback=False)
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="err",
            args=(),
            exc_info=exc_info,
        )
        data = json.loads(fmt.format(record))
        assert "exception" in data
        assert data["exception"]["type"] == "ValueError"
        assert data["exception"]["message"] == "test error"
        assert "traceback" not in data["exception"]

    def test_exception_with_traceback(self):
        fmt = StructuredFormatter(include_traceback=True)
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="err",
            args=(),
            exc_info=exc_info,
        )
        data = json.loads(fmt.format(record))
        assert "traceback" in data["exception"]

    def test_extra_data(self):
        fmt = StructuredFormatter()
        record = _make_record("msg")
        record.extra_data = {"key": "value", "count": 42}
        data = json.loads(fmt.format(record))
        assert data["data"]["key"] == "value"
        assert data["data"]["count"] == 42

    def test_no_extra_data_omitted(self):
        fmt = StructuredFormatter()
        record = _make_record("msg")
        data = json.loads(fmt.format(record))
        assert "data" not in data

    def test_ensure_ascii_false(self):
        """JSON 输出应支持中文（ensure_ascii=False）"""
        fmt = StructuredFormatter()
        record = _make_record("中文消息")
        output = fmt.format(record)
        assert "中文消息" in output  # 未被转义为 \uXXXX


class TestContextFilter:
    def test_set_and_filter(self):
        cf = ContextFilter()
        cf.set_context(request_id="req-1", user_id="user-1")
        record = _make_record("msg")
        assert cf.filter(record) is True
        assert record.request_id == "req-1"
        assert record.user_id == "user-1"

    def test_clear_context(self):
        cf = ContextFilter()
        cf.set_context(request_id="req-1")
        cf.clear_context()
        record = _make_record("msg")
        cf.filter(record)
        assert not hasattr(record, "request_id") or getattr(record, "request_id", None) is None

    def test_filter_returns_true_always(self):
        cf = ContextFilter()
        record = _make_record("msg")
        assert cf.filter(record) is True

    def test_set_context_multiple_calls_merge(self):
        cf = ContextFilter()
        cf.set_context(request_id="r1")
        cf.set_context(user_id="u1")
        record = _make_record("msg")
        cf.filter(record)
        assert record.request_id == "r1"
        assert record.user_id == "u1"

    def test_set_context_overwrite(self):
        cf = ContextFilter()
        cf.set_context(request_id="r1")
        cf.set_context(request_id="r2")
        record = _make_record("msg")
        cf.filter(record)
        assert record.request_id == "r2"


class TestStructuredLogger:
    def test_init_defaults(self, tmp_path):
        sl = StructuredLogger(log_dir=str(tmp_path / "logs"))
        assert sl._level == logging.INFO
        assert sl._json_format is True
        assert sl._max_bytes == 50 * 1024 * 1024
        assert sl._backup_count == 10

    def test_init_custom_level(self, tmp_path):
        sl = StructuredLogger(log_dir=str(tmp_path / "logs"), level="debug")
        assert sl._level == logging.DEBUG

    def test_init_invalid_level_falls_back_info(self, tmp_path):
        sl = StructuredLogger(log_dir=str(tmp_path / "logs"), level="NOSUCHLEVEL")
        assert sl._level == logging.INFO

    def test_setup_creates_log_dir(self, tmp_path):
        log_dir = tmp_path / "mylogs"
        sl = StructuredLogger(log_dir=str(log_dir))
        sl.setup()
        assert log_dir.exists()
        # 清理 handlers
        _cleanup_edgelite_handlers()

    def test_setup_adds_handlers(self, tmp_path):
        sl = StructuredLogger(log_dir=str(tmp_path / "logs"))
        sl.setup()
        root = logging.getLogger()
        edgelite_handlers = [h for h in root.handlers if getattr(h, "_edgelite_handler", False)]
        assert len(edgelite_handlers) >= 2  # console + file + error
        _cleanup_edgelite_handlers()

    def test_setup_removes_old_edgelite_handlers(self, tmp_path):
        """重复 setup 只保留最新 edgelite handlers"""
        sl1 = StructuredLogger(log_dir=str(tmp_path / "logs1"))
        sl1.setup()
        sl2 = StructuredLogger(log_dir=str(tmp_path / "logs2"))
        sl2.setup()
        root = logging.getLogger()
        edgelite_handlers = [h for h in root.handlers if getattr(h, "_edgelite_handler", False)]
        assert len(edgelite_handlers) <= 4  # 不重复堆积
        _cleanup_edgelite_handlers()

    def test_set_context_delegates(self, tmp_path):
        sl = StructuredLogger(log_dir=str(tmp_path / "logs"))
        sl.set_context(request_id="req-x")
        record = _make_record("msg")
        sl._context_filter.filter(record)
        assert record.request_id == "req-x"

    def test_clear_context_delegates(self, tmp_path):
        sl = StructuredLogger(log_dir=str(tmp_path / "logs"))
        sl.set_context(request_id="req-x")
        sl.clear_context()
        record = _make_record("msg")
        sl._context_filter.filter(record)
        assert getattr(record, "request_id", None) is None

    def test_get_logger_static(self):
        logger = StructuredLogger.get_logger("my.logger")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "my.logger"


class TestLogWithData:
    def test_log_with_data_attaches_extra(self):
        """log_with_data 应将 data 附加到 record.extra_data 并被 formatter 输出"""
        logger = logging.getLogger("test.log_with_data")
        # 用 capture handler 收集 record
        records: list[logging.LogRecord] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _CaptureHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            log_with_data(logger, logging.INFO, "test message", key1="val1", key2=42)
            assert len(records) == 1
            assert records[0].extra_data == {"key1": "val1", "key2": 42}
            assert records[0].getMessage() == "test message"
        finally:
            logger.removeHandler(handler)


def _cleanup_edgelite_handlers():
    """清理 root logger 上的 edgelite handler"""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_edgelite_handler", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _cleanup_handlers():
    """每个测试后清理 edgelite handler"""
    yield
    _cleanup_edgelite_handlers()
