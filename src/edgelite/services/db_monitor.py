"""Database monitor — tracks connection pool stats and slow queries.

Provides a lightweight monitoring layer that collects database connection
pool statistics and slow query counts for metrics export.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class DatabaseMonitor:
    """Monitors database connection pool and slow queries."""

    def __init__(self) -> None:
        self._database: Any = None
        self._running = False
        self._lock = threading.Lock()
        self._slow_queries: int = 0
        self._active_connections: int = 0
        self._idle_connections: int = 0
        self._waiting_count: int = 0
        self._monitor_task: Any = None

    def set_database(self, database: Any) -> None:
        """Set the database instance to monitor."""
        self._database = database

    async def start(self) -> None:
        """Start the database monitor."""
        self._running = True
        logger.info("Database monitor started")

    async def stop(self) -> None:
        """Stop the database monitor."""
        self._running = False
        logger.info("Database monitor stopped")

    def record_slow_query(self) -> None:
        """Record a slow query occurrence."""
        with self._lock:
            self._slow_queries += 1

    def get_slow_query_count(self) -> int:
        """Get total slow query count."""
        with self._lock:
            return self._slow_queries

    def get_pool_stats(self) -> dict[str, int]:
        """Get connection pool statistics."""
        with self._lock:
            return {
                "active_connections": self._active_connections,
                "idle_connections": self._idle_connections,
                "waiting_count": self._waiting_count,
            }

    def update_pool_stats(self, active: int, idle: int, waiting: int) -> None:
        """Update pool statistics (called by pool listeners)."""
        with self._lock:
            self._active_connections = active
            self._idle_connections = idle
            self._waiting_count = waiting


_monitor: DatabaseMonitor | None = None


def get_db_monitor() -> DatabaseMonitor:
    """Get the singleton database monitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = DatabaseMonitor()
    return _monitor
