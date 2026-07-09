"""OPC UA driver configuration version manager.

FIX-DRV-003: File was corrupted (unterminated triple-quoted string causing SyntaxError),
which prevented the OPC UA driver from loading. Restored as a minimal stub implementation
to unblock driver loading. Full functionality can be added later if needed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class OpcUaConfigVersionManager:
    """Manages configuration version snapshots for OPC UA driver.

    This is a minimal stub implementation that provides the interface
    expected by OpcUaDriver without requiring external storage.
    """

    def __init__(self) -> None:
        self._versions: dict[str, dict[str, Any]] = {}

    async def save_version(self, device_id: str, config: dict, change_summary: str = "", operator: str = "") -> int:
        """Save a configuration version snapshot.

        Returns an integer version number for audit trail compatibility.
        """
        version_no = len(self._versions) + 1
        version_id = f"{device_id}_v{version_no}"
        self._versions[version_id] = {
            "device_id": device_id,
            "config": config,
            "change_summary": change_summary,
            "operator": operator,
            "version": version_no,
        }
        logger.debug("Saved config version %s for device %s (v%d)", version_id, device_id, version_no)
        return version_no

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        """Retrieve a configuration version by ID."""
        return self._versions.get(version_id)

    def list_versions(self, device_id: str | None = None) -> list[dict[str, Any]]:
        """List configuration versions, optionally filtered by device_id."""
        if device_id:
            return [v for v in self._versions.values() if v.get("device_id") == device_id]
        return list(self._versions.values())

    def restore_version(self, version_id: str) -> dict[str, Any] | None:
        """Restore a configuration version by ID."""
        version = self._versions.get(version_id)
        if version:
            return version.get("config")
        return None

    async def stop(self) -> None:
        """Stop the config version manager and release resources."""
        self._versions.clear()
