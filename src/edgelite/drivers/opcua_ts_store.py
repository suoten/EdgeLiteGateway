"""OPC UA time-series data storage and offline sync manager.

FIX-DRV-005: File was corrupted (13 bytes, unterminated docstring causing SyntaxError),
which prevented the OPC UA driver from loading. Restored as a minimal stub implementation.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class OpcUaTsStore:
    """Local time-series data cache for OPC UA driver.

    Minimal stub implementation to unblock driver loading.
    """

    def __init__(self, retention_days: int = 30) -> None:
        self._retention_days = retention_days
        self._buffer: deque = deque(maxlen=10000)

    def append(self, point_data: dict[str, Any]) -> None:
        """Append a data point to the local store."""
        self._buffer.append(point_data)

    def get_pending(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Get pending data points for sync."""
        return list(self._buffer)[:limit]

    def mark_synced(self, count: int) -> None:
        """Mark data points as synced, removing from buffer."""
        for _ in range(min(count, len(self._buffer))):
            self._buffer.popleft()

    def cleanup_expired(self) -> int:
        """Remove expired data points. Returns count of removed items."""
        return 0


class OpcUaOfflineSyncManager:
    """Manages offline data synchronization for OPC UA driver.

    Minimal stub implementation to unblock driver loading.
    """

    def __init__(self, ts_store: OpcUaTsStore | None = None, sync_interval: float = 30.0) -> None:
        self._ts_store = ts_store
        self._sync_interval = sync_interval
        self._running = False

    async def start(self) -> None:
        """Start the offline sync manager."""
        self._running = True
        logger.info("OpcUaOfflineSyncManager started (stub)")

    async def stop(self) -> None:
        """Stop the offline sync manager."""
        self._running = False

    async def sync_now(self) -> int:
        """Trigger an immediate sync. Returns number of synced records."""
        return 0
