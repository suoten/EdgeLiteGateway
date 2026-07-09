"""Log aggregator stub — collects recent log entries in memory for debugging.

This module provides a lightweight in-memory log handler that captures
the most recent log entries for potential inspection via API or debug tools.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any


class _LogAggregator:
    """In-memory log aggregator that captures recent log records."""

    def __init__(self, max_entries: int = 1000) -> None:
        self._max_entries = max_entries
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._handler: _AggregatorHandler | None = None

    def install(self) -> None:
        """Install the log handler on the root logger."""
        if self._handler is not None:
            return
        self._handler = _AggregatorHandler(self)
        root_logger = logging.getLogger()
        root_logger.addHandler(self._handler)

    def uninstall(self) -> None:
        """Remove the log handler from the root logger."""
        if self._handler is not None:
            root_logger = logging.getLogger()
            root_logger.removeHandler(self._handler)
            self._handler = None

    def add_entry(self, record: logging.LogRecord) -> None:
        """Add a log entry to the aggregator."""
        entry = {
            "timestamp": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        with self._lock:
            self._entries.append(entry)

    def get_entries(
        self, level: str | None = None, limit: int = 100, since: float | None = None
    ) -> list[dict[str, Any]]:
        """Get recent log entries, optionally filtered by level."""
        with self._lock:
            entries = list(self._entries)
        if level:
            entries = [e for e in entries if e["level"] == level]
        if since:
            entries = [e for e in entries if e["timestamp"] >= since]
        entries.reverse()
        return entries[:limit]

    def clear(self) -> None:
        """Clear all stored log entries."""
        with self._lock:
            self._entries.clear()


class _AggregatorHandler(logging.Handler):
    """Logging handler that forwards records to the aggregator."""

    def __init__(self, aggregator: _LogAggregator) -> None:
        super().__init__()
        self._aggregator = aggregator

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._aggregator.add_entry(record)
        except Exception:
            pass  # Never let logging handler errors propagate


_aggregator: _LogAggregator | None = None


def get_log_aggregator() -> _LogAggregator:
    """Get the singleton log aggregator instance."""
    global _aggregator
    if _aggregator is None:
        _aggregator = _LogAggregator(max_entries=1000)
        _aggregator.install()
    return _aggregator
