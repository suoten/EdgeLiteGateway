"""Data import/export service for configuration management

Supports:
- Export devices, rules, and alarm configurations to JSON/CSV
- Import configurations with validation and conflict resolution
- Template management for devices and rules
- Batch operations
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo, AlarmRepo

logger = logging.getLogger(__name__)


class ExportFormat(Enum):
    """Export file format"""
    JSON = "json"
    CSV = "csv"


class ImportMode(Enum):
    """Import conflict resolution mode"""
    SKIP = "skip"  # Skip existing items
    OVERWRITE = "overwrite"  # Overwrite existing items
    RENAME = "rename"  # Rename with suffix
    ERROR = "error"  # Raise error on conflict


class ImportResult:
    """Result of an import operation"""
    success: bool = True
    total_count: int = 0
    imported_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    imported_items: list[dict] = field(default_factory=list)
    skipped_items: list[dict] = field(default_factory=list)


@dataclass
class DeviceTemplate:
    """Device configuration template"""
    template_id: str = ""
    name: str = ""
    protocol: str = ""
    description: str = ""
    default_config: dict = field(default_factory=dict)
    default_points: list[dict] = field(default_factory=list)
    collect_interval: int = 5
    tags: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass
class RuleTemplate:
    """Rule configuration template"""
    template_id: str = ""
    name: str = ""
    description: str = ""
    rule_type: str = "threshold"
    default_conditions: list[dict] = field(default_factory=list)
    default_severity: str = "warning"
    default_duration: int = 0
    notify_channels: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass
class DeviceGroup:
    """Device grouping for organization"""
    group_id: str = ""
    name: str = ""
    description: str = ""
    parent_id: str = ""  # For hierarchical groups
    device_ids: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class DataExportService:
    """Service for exporting configuration data"""

    def __init__(
        self,
        device_repo: DeviceRepo,
        rule_repo: RuleRepo,
        alarm_repo: AlarmRepo,
    ):
        self._device_repo = device_repo
        self._rule_repo = rule_repo
        self._alarm_repo = alarm_repo

    async def export_devices(
        self,
        device_ids: list[str] | None = None,
        format: ExportFormat = ExportFormat.JSON,
    ) -> str:
        """Export devices to specified format"""
        if device_ids:
            devices = []
            for did in device_ids:
                device = await self._device_repo.get(did)
                if device:
                    devices.append(device)
        else:
            devices, _ = await self._device_repo.list_all(size=10000)

        if format == ExportFormat.JSON:
            return self._export_devices_json(devices)
        else:
            return self._export_devices_csv(devices)

    def _export_devices_json(self, devices: list[dict]) -> str:
        """Export devices to JSON format"""
        export_data = {
            "version": "1.0",
            "type": "devices",
            "exported_at": datetime.now(UTC).isoformat(),
            "count": len(devices),
            "devices": devices,
        }
        return json.dumps(export_data, indent=2, ensure_ascii=False)

    def _export_devices_csv(self, devices: list[dict]) -> str:
        """Export devices to CSV format"""
        if not devices:
            return ""

        output = io.StringIO()
        fieldnames = ["device_id", "name", "protocol", "status", "collect_interval"]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(devices)
        return output.getvalue()

    async def export_rules(
        self,
        rule_ids: list[str] | None = None,
        format: ExportFormat = ExportFormat.JSON,
    ) -> str:
        """Export rules to specified format"""
        if rule_ids:
            rules = []
            for rid in rule_ids:
                rule = await self._rule_repo.get(rid)
                if rule:
                    rules.append(rule)
        else:
            rules, _ = await self._rule_repo.list_all(size=10000)

        if format == ExportFormat.JSON:
            return self._export_rules_json(rules)
        else:
            return self._export_rules_csv(rules)

    def _export_rules_json(self, rules: list[dict]) -> str:
        """Export rules to JSON format"""
        export_data = {
            "version": "1.0",
            "type": "rules",
            "exported_at": datetime.now(UTC).isoformat(),
            "count": len(rules),
            "rules": rules,
        }
        return json.dumps(export_data, indent=2, ensure_ascii=False)

    def _export_rules_csv(self, rules: list[dict]) -> str:
        """Export rules to CSV format"""
        if not rules:
            return ""

        output = io.StringIO()
        fieldnames = ["rule_id", "name", "device_id", "severity", "enabled"]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rules)
        return output.getvalue()

    async def export_all(
        self,
        format: ExportFormat = ExportFormat.JSON,
    ) -> str:
        """Export all configuration data"""
        devices, _ = await self._device_repo.list_all(size=10000)
        rules, _ = await self._rule_repo.list_all(size=10000)

        export_data = {
            "version": "1.0",
            "type": "full_backup",
            "exported_at": datetime.now(UTC).isoformat(),
            "devices": devices,
            "rules": rules,
            "device_count": len(devices),
            "rule_count": len(rules),
        }

        return json.dumps(export_data, indent=2, ensure_ascii=False)


class DataImportService:
    """Service for importing configuration data"""

    def __init__(
        self,
        device_repo: DeviceRepo,
        rule_repo: RuleRepo,
    ):
        self._device_repo = device_repo
        self._rule_repo = rule_repo

    async def import_devices(
        self,
        data: str,
        format: ExportFormat = ExportFormat.JSON,
        mode: ImportMode = ImportMode.SKIP,
    ) -> ImportResult:
        """Import devices from data"""
        result = ImportResult()

        try:
            if format == ExportFormat.JSON:
                items = self._parse_json(data)
            else:
                items = self._parse_devices_csv(data)

            result.total_count = len(items)

            for item in items:
                try:
                    imported = await self._import_device(item, mode, result)
                    if imported:
                        result.imported_count += 1
                        result.imported_items.append(imported)
                    else:
                        result.skipped_count += 1
                except Exception as e:
                    result.error_count += 1
                    result.errors.append(f"Device {item.get('device_id', 'unknown')}: {str(e)}")
                    result.success = False

        except Exception as e:
            result.success = False
            result.errors.append(f"Parse error: {str(e)}")

        return result

    def _parse_json(self, data: str) -> list[dict]:
        """Parse JSON export data"""
        parsed = json.loads(data)
        if "devices" in parsed:
            return parsed["devices"]
        elif "rules" in parsed:
            return parsed["rules"]
        elif isinstance(parsed, list):
            return parsed
        else:
            raise ValueError("Invalid export format")

    def _parse_devices_csv(self, data: str) -> list[dict]:
        """Parse CSV export data"""
        items = []
        reader = csv.DictReader(io.StringIO(data))
        for row in reader:
            items.append(row)
        return items

    async def _import_device(
        self,
        device: dict,
        mode: ImportMode,
        result: ImportResult,
    ) -> dict | None:
        """Import a single device"""
        device_id = device.get("device_id")
        if not device_id:
            device_id = f"device_{uuid.uuid4().hex[:8]}"
            device["device_id"] = device_id
            result.warnings.append(f"Generated device_id: {device_id}")

        # Check if device exists
        existing = await self._device_repo.get(device_id)

        if existing:
            if mode == ImportMode.SKIP:
                result.skipped_items.append(device)
                return None
            elif mode == ImportMode.ERROR:
                raise ValueError(f"Device already exists: {device_id}")
            elif mode == ImportMode.RENAME:
                new_id = f"{device_id}_imported"
                device["device_id"] = new_id
                result.warnings.append(f"Renamed device: {device_id} -> {new_id}")
            # OVERWRITE mode continues

        # Import device
        if existing and mode == ImportMode.OVERWRITE:
            updated = await self._device_repo.update(device_id, device)
            return updated
        else:
            created = await self._device_repo.create(device)
            return created

    async def import_rules(
        self,
        data: str,
        format: ExportFormat = ExportFormat.JSON,
        mode: ImportMode = ImportMode.SKIP,
    ) -> ImportResult:
        """Import rules from data"""
        result = ImportResult()

        try:
            if format == ExportFormat.JSON:
                items = self._parse_json(data)
            else:
                items = self._parse_rules_csv(data)

            result.total_count = len(items)

            for item in items:
                try:
                    imported = await self._import_rule(item, mode, result)
                    if imported:
                        result.imported_count += 1
                        result.imported_items.append(imported)
                    else:
                        result.skipped_count += 1
                except Exception as e:
                    result.error_count += 1
                    result.errors.append(f"Rule {item.get('rule_id', 'unknown')}: {str(e)}")
                    result.success = False

        except Exception as e:
            result.success = False
            result.errors.append(f"Parse error: {str(e)}")

        return result

    def _parse_rules_csv(self, data: str) -> list[dict]:
        """Parse CSV export data for rules"""
        items = []
        reader = csv.DictReader(io.StringIO(data))
        for row in reader:
            items.append(row)
        return items

    async def _import_rule(
        self,
        rule: dict,
        mode: ImportMode,
        result: ImportResult,
    ) -> dict | None:
        """Import a single rule"""
        rule_id = rule.get("rule_id")
        if not rule_id:
            rule_id = f"rule_{uuid.uuid4().hex[:8]}"
            rule["rule_id"] = rule_id
            result.warnings.append(f"Generated rule_id: {rule_id}")

        # Check if rule exists
        existing = await self._rule_repo.get(rule_id)

        if existing:
            if mode == ImportMode.SKIP:
                result.skipped_items.append(rule)
                return None
            elif mode == ImportMode.ERROR:
                raise ValueError(f"Rule already exists: {rule_id}")
            elif mode == ImportMode.RENAME:
                new_id = f"{rule_id}_imported"
                rule["rule_id"] = new_id
                result.warnings.append(f"Renamed rule: {rule_id} -> {new_id}")

        # Import rule
        if existing and mode == ImportMode.OVERWRITE:
            updated = await self._rule_repo.update(rule_id, rule)
            return updated
        else:
            created = await self._rule_repo.create(rule)
            return created

    async def import_all(
        self,
        data: str,
        mode: ImportMode = ImportMode.SKIP,
    ) -> dict[str, ImportResult]:
        """Import all configuration data"""
        results = {}

        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse import data: %s", e)
            return {"error": str(e)}

        if "devices" in parsed:
            devices_json = json.dumps({"devices": parsed["devices"]})
            results["devices"] = await self.import_devices(
                devices_json,
                ExportFormat.JSON,
                mode,
            )

        if "rules" in parsed:
            rules_json = json.dumps({"rules": parsed["rules"]})
            results["rules"] = await self.import_rules(
                rules_json,
                ExportFormat.JSON,
                mode,
            )

        return results


class TemplateService:
    """Service for managing device and rule templates"""

    def __init__(self):
        self._device_templates: dict[str, DeviceTemplate] = {}
        self._rule_templates: dict[str, RuleTemplate] = {}
        self._device_groups: dict[str, DeviceGroup] = {}
        self._group_members: dict[str, list[str]] = defaultdict(list)  # device_id -> group_ids

    # Device Templates
    def create_device_template(self, template: DeviceTemplate) -> DeviceTemplate:
        """Create a new device template"""
        if not template.template_id:
            template.template_id = f"tmpl_{uuid.uuid4().hex[:8]}"
        template.created_at = datetime.now(UTC).isoformat()
        template.updated_at = template.created_at
        self._device_templates[template.template_id] = template
        logger.info("Device template created: %s", template.template_id)
        return template

    def get_device_template(self, template_id: str) -> DeviceTemplate | None:
        """Get a device template by ID"""
        return self._device_templates.get(template_id)

    def list_device_templates(self) -> list[DeviceTemplate]:
        """List all device templates"""
        return list(self._device_templates.values())

    def update_device_template(
        self,
        template_id: str,
        data: dict,
    ) -> DeviceTemplate | None:
        """Update a device template"""
        template = self._device_templates.get(template_id)
        if not template:
            return None

        for key, value in data.items():
            if hasattr(template, key):
                setattr(template, key, value)
        template.updated_at = datetime.now(UTC).isoformat()

        logger.info("Device template updated: %s", template_id)
        return template

    def delete_device_template(self, template_id: str) -> bool:
        """Delete a device template"""
        if template_id in self._device_templates:
            del self._device_templates[template_id]
            logger.info("Device template deleted: %s", template_id)
            return True
        return False

    def apply_device_template(
        self,
        template_id: str,
        device_name: str,
        custom_config: dict | None = None,
    ) -> dict:
        """Create a device from a template"""
        template = self._device_templates.get(template_id)
        if not template:
            raise ValueError(f"Template not found: {template_id}")

        device_id = f"dev_{uuid.uuid4().hex[:8]}"

        # Merge configurations
        config = dict(template.default_config)
        if custom_config:
            config.update(custom_config)

        device = {
            "device_id": device_id,
            "name": device_name,
            "protocol": template.protocol,
            "config": config,
            "points": list(template.default_points),
            "collect_interval": template.collect_interval,
            "tags": dict(template.tags),
        }

        return device

    # Rule Templates
    def create_rule_template(self, template: RuleTemplate) -> RuleTemplate:
        """Create a new rule template"""
        if not template.template_id:
            template.template_id = f"rtmpl_{uuid.uuid4().hex[:8]}"
        template.created_at = datetime.now(UTC).isoformat()
        template.updated_at = template.created_at
        self._rule_templates[template.template_id] = template
        logger.info("Rule template created: %s", template.template_id)
        return template

    def get_rule_template(self, template_id: str) -> RuleTemplate | None:
        """Get a rule template by ID"""
        return self._rule_templates.get(template_id)

    def list_rule_templates(self) -> list[RuleTemplate]:
        """List all rule templates"""
        return list(self._rule_templates.values())

    def update_rule_template(
        self,
        template_id: str,
        data: dict,
    ) -> RuleTemplate | None:
        """Update a rule template"""
        template = self._rule_templates.get(template_id)
        if not template:
            return None

        for key, value in data.items():
            if hasattr(template, key):
                setattr(template, key, value)
        template.updated_at = datetime.now(UTC).isoformat()

        logger.info("Rule template updated: %s", template_id)
        return template

    def delete_rule_template(self, template_id: str) -> bool:
        """Delete a rule template"""
        if template_id in self._rule_templates:
            del self._rule_templates[template_id]
            logger.info("Rule template deleted: %s", template_id)
            return True
        return False

    def apply_rule_template(
        self,
        template_id: str,
        rule_name: str,
        device_id: str,
        custom_conditions: list[dict] | None = None,
    ) -> dict:
        """Create a rule from a template"""
        template = self._rule_templates.get(template_id)
        if not template:
            raise ValueError(f"Template not found: {template_id}")

        rule_id = f"rule_{uuid.uuid4().hex[:8]}"

        conditions = custom_conditions or list(template.default_conditions)

        # Replace placeholder device_id in conditions
        for cond in conditions:
            if isinstance(cond, dict) and "device_id" in cond:
                cond["device_id"] = device_id

        rule = {
            "rule_id": rule_id,
            "name": rule_name,
            "device_id": device_id,
            "conditions": conditions,
            "rule_type": template.rule_type,
            "severity": template.default_severity,
            "duration": template.default_duration,
            "notify_channels": list(template.notify_channels),
        }

        return rule

    # Device Groups
    def create_device_group(self, group: DeviceGroup) -> DeviceGroup:
        """Create a new device group"""
        if not group.group_id:
            group.group_id = f"grp_{uuid.uuid4().hex[:8]}"
        group.created_at = datetime.now(UTC).isoformat()
        group.updated_at = group.created_at
        self._device_groups[group.group_id] = group
        logger.info("Device group created: %s", group.group_id)
        return group

    def get_device_group(self, group_id: str) -> DeviceGroup | None:
        """Get a device group by ID"""
        return self._device_groups.get(group_id)

    def list_device_groups(self) -> list[DeviceGroup]:
        """List all device groups"""
        return list(self._device_groups.values())

    def update_device_group(
        self,
        group_id: str,
        data: dict,
    ) -> DeviceGroup | None:
        """Update a device group"""
        group = self._device_groups.get(group_id)
        if not group:
            return None

        for key, value in data.items():
            if hasattr(group, key):
                setattr(group, key, value)
        group.updated_at = datetime.now(UTC).isoformat()

        logger.info("Device group updated: %s", group_id)
        return group

    def delete_device_group(self, group_id: str) -> bool:
        """Delete a device group"""
        if group_id in self._device_groups:
            # Remove group membership for all devices
            for device_id, groups in list(self._group_members.items()):
                if group_id in groups:
                    groups.remove(group_id)
            del self._device_groups[group_id]
            logger.info("Device group deleted: %s", group_id)
            return True
        return False

    def add_device_to_group(self, device_id: str, group_id: str) -> bool:
        """Add a device to a group"""
        if group_id not in self._device_groups:
            return False
        if device_id not in self._group_members:
            self._group_members[device_id] = []
        if group_id not in self._group_members[device_id]:
            self._group_members[device_id].append(group_id)
            self._device_groups[group_id].device_ids.append(device_id)
            logger.info("Device %s added to group %s", device_id, group_id)
        return True

    def remove_device_from_group(self, device_id: str, group_id: str) -> bool:
        """Remove a device from a group"""
        if group_id in self._group_members.get(device_id, []):
            self._group_members[device_id].remove(group_id)
            if device_id in self._device_groups[group_id].device_ids:
                self._device_groups[group_id].device_ids.remove(device_id)
            logger.info("Device %s removed from group %s", device_id, group_id)
            return True
        return False

    def get_device_groups(self, device_id: str) -> list[DeviceGroup]:
        """Get all groups a device belongs to"""
        group_ids = self._group_members.get(device_id, [])
        return [self._device_groups[gid] for gid in group_ids if gid in self._device_groups]

    def get_group_devices(self, group_id: str) -> list[str]:
        """Get all devices in a group"""
        group = self._device_groups.get(group_id)
        return list(group.device_ids) if group else []

    def export_templates(self) -> str:
        """Export all templates to JSON"""
        data = {
            "version": "1.0",
            "type": "templates",
            "exported_at": datetime.now(UTC).isoformat(),
            "device_templates": [
                {
                    **vars(t),
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                }
                for t in self._device_templates.values()
            ],
            "rule_templates": [
                {
                    **vars(t),
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                }
                for t in self._rule_templates.values()
            ],
            "device_groups": [
                {
                    **vars(g),
                    "created_at": g.created_at,
                    "updated_at": g.updated_at,
                }
                for g in self._device_groups.values()
            ],
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    def import_templates(self, data: str) -> dict[str, int]:
        """Import templates from JSON"""
        result = {"device_templates": 0, "rule_templates": 0, "device_groups": 0}

        parsed = json.loads(data)

        for t_data in parsed.get("device_templates", []):
            template = DeviceTemplate(**t_data)
            self._device_templates[template.template_id] = template
            result["device_templates"] += 1

        for t_data in parsed.get("rule_templates", []):
            template = RuleTemplate(**t_data)
            self._rule_templates[template.template_id] = template
            result["rule_templates"] += 1

        for g_data in parsed.get("device_groups", []):
            group = DeviceGroup(**g_data)
            self._device_groups[group.group_id] = group
            # Rebuild membership
            for device_id in group.device_ids:
                if device_id not in self._group_members:
                    self._group_members[device_id] = []
                if group.group_id not in self._group_members[device_id]:
                    self._group_members[device_id].append(group.group_id)
            result["device_groups"] += 1

        logger.info("Templates imported: %s", result)
        return result


# Global instances
_export_service: DataExportService | None = None
_import_service: DataImportService | None = None
_template_service: TemplateService | None = None


def get_export_service(
    device_repo: DeviceRepo | None = None,
    rule_repo: RuleRepo | None = None,
    alarm_repo: AlarmRepo | None = None,
) -> DataExportService:
    """Get or create the export service"""
    global _export_service
    if _export_service is None and all([device_repo, rule_repo, alarm_repo]):
        _export_service = DataExportService(device_repo, rule_repo, alarm_repo)
    return _export_service


def get_import_service(
    device_repo: DeviceRepo | None = None,
    rule_repo: RuleRepo | None = None,
) -> DataImportService:
    """Get or create the import service"""
    global _import_service
    if _import_service is None and all([device_repo, rule_repo]):
        _import_service = DataImportService(device_repo, rule_repo)
    return _import_service


def get_template_service() -> TemplateService:
    """Get or create the template service"""
    global _template_service
    if _template_service is None:
        _template_service = TemplateService()
    return _template_service
