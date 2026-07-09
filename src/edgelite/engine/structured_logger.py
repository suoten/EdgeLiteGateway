"""结构化日志系统 - 支持JSON格式日志输出和日志归档

支持：
- JSON结构化日志输出
- 日志轮转和归档
- 日志级别动态调整
- 上下文信息注入（请求ID、用户ID等）
- 日志过滤和搜索
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading  # FIXED-P2: ContextFilter的_context字典多线程读写需同步保护
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StructuredFormatter(logging.Formatter):
    """结构化日志格式器 - 输出JSON格式"""

    def __init__(self, include_context: bool = True, include_traceback: bool = False):
        super().__init__()
        self.include_context = include_context
        self.include_traceback = include_traceback  # FIXED-P0: 默认不包含traceback，防止生产环境泄露内部路径

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        if self.include_context:
            for key in ("request_id", "user_id", "device_id", "trace_id", "span_id"):
                value = getattr(record, key, None)
                if value:
                    log_entry[key] = value

        if record.exc_info and record.exc_info[0]:
            exc_entry = {
                "type": record.exc_info[0].__name__
                if hasattr(record.exc_info[0], "__name__")
                else str(record.exc_info[0]),
                "message": str(record.exc_info[1]),
            }
            if self.include_traceback:  # FIXED-P0: 仅在include_traceback=True时包含完整堆栈
                exc_entry["traceback"] = self.formatException(record.exc_info)
            log_entry["exception"] = exc_entry

        extra = getattr(record, "extra_data", None)
        if extra and isinstance(extra, dict):
            log_entry["data"] = extra

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class ContextFilter(logging.Filter):
    """上下文信息过滤器 - 注入请求ID、用户ID等"""

    def __init__(self):
        super().__init__()
        self._context: dict[str, Any] = {}
        self._lock = threading.Lock()  # FIXED-P2: 多线程并发调用set_context/filter时保护_context字典

    def set_context(self, **kwargs: Any) -> None:
        with self._lock:
            self._context.update(kwargs)

    def clear_context(self) -> None:
        with self._lock:
            self._context.clear()

    def filter(self, record: logging.LogRecord) -> bool:
        # FIXED-P2: 复制context快照，避免迭代期间其他线程修改字典导致RuntimeError
        with self._lock:
            context_snapshot = dict(self._context)
        for key, value in context_snapshot.items():
            setattr(record, key, value)
        return True


class StructuredLogger:
    """结构化日志管理器"""

    def __init__(
        self,
        log_dir: str = "data/logs",
        level: str = "INFO",
        json_format: bool = True,
        max_bytes: int = 50 * 1024 * 1024,
        backup_count: int = 10,
    ):
        self._log_dir = Path(log_dir)
        self._level = getattr(logging, level.upper(), logging.INFO)
        self._json_format = json_format
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._context_filter = ContextFilter()

    def setup(self) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)

        root_logger = logging.getLogger()
        root_logger.setLevel(self._level)

        # FIXED-P2: 无条件清除所有handler可能移除第三方库(如uvicorn)的handler，改为只移除自己添加的
        for h in list(root_logger.handlers):
            if getattr(h, '_edgelite_handler', False):
                root_logger.removeHandler(h)

        console_handler = logging.StreamHandler()
        if self._json_format:
            console_handler.setFormatter(StructuredFormatter())
        else:
            console_handler.setFormatter(
                logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
            )
        console_handler.addFilter(self._context_filter)
        console_handler._edgelite_handler = True  # FIXED-P2: 标记edgelite自己的handler，便于后续只移除自己的
        root_logger.addHandler(console_handler)

        app_log = self._log_dir / "edgelite.log"
        file_handler = logging.handlers.RotatingFileHandler(
            str(app_log),
            maxBytes=self._max_bytes,
            backupCount=self._backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(StructuredFormatter())
        file_handler.addFilter(self._context_filter)
        file_handler._edgelite_handler = True
        root_logger.addHandler(file_handler)

        error_log = self._log_dir / "edgelite-error.log"
        error_handler = logging.handlers.RotatingFileHandler(
            str(error_log),
            maxBytes=self._max_bytes,
            backupCount=self._backup_count,
            encoding="utf-8",
        )
        error_handler.setFormatter(StructuredFormatter())
        error_handler.setLevel(logging.ERROR)
        error_handler.addFilter(self._context_filter)
        error_handler._edgelite_handler = True
        root_logger.addHandler(error_handler)

    def set_context(self, **kwargs: Any) -> None:
        self._context_filter.set_context(**kwargs)

    def clear_context(self) -> None:
        self._context_filter.clear_context()

    @staticmethod
    def get_logger(name: str) -> logging.Logger:
        return logging.getLogger(name)


def log_with_data(logger: logging.Logger, level: int, msg: str, **data: Any) -> None:
    record = logger.makeRecord(logger.name, level, "", 0, msg, (), None)
    record.extra_data = data
    logger.handle(record)
