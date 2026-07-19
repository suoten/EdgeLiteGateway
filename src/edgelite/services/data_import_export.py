"""Data import/export service for configuration management

Supports:
- Export devices, rules, and alarm configurations to JSON/CSV
- Import configurations with validation and conflict resolution
- Template management for devices and rules
- Batch operations
"""

from __future__ import annotations

import csv

# FIXED(P3): 原问题-F401未使用导入asyncio; 修复-删除该导入行
import io
import json
import logging
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import func, select

from edgelite.models.db import DeviceORM, RuleORM
from edgelite.storage.sqlite_repo import AlarmRepo, DeviceRepo, RuleRepo

logger = logging.getLogger(__name__)


def _sanitize_csv_cell(value: Any) -> Any:
    """FIXED-P1: 防止CSV注入攻击

    如果单元格值以 =, +, -, @, \\t, \\r 开头，在前面加单引号防止公式注入。
    """
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{value}"
    return value


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


@dataclass
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

    @staticmethod
    async def _list_all_paginated(repo: Any, page_size: int = 1000) -> list[dict]:
        """分页循环加载所有数据。

        FIXED(严重): 原问题-export_*方法使用list_all(size=10000)硬编码上限，超过10000条被静默截断;
        修复-分页循环加载所有数据，避免截断
        """
        all_items: list[dict] = []
        page = 1
        while True:
            items, total = await repo.list_all(page=page, size=page_size)
            all_items.extend(items)
            if not items or len(items) < page_size or len(all_items) >= total:
                break
            page += 1
        return all_items

    async def export_devices(
        self,
        device_ids: list[str] | None = None,
        format: ExportFormat = ExportFormat.JSON,
    ) -> str:
        """Export devices to specified format"""
        if device_ids:
            # FIXED(严重): 原问题-循环 await self._device_repo.get(did) 导致 N+1 查询，
            # 导出 100 个设备 = 100 次 DB 往返 + 100 次 session 创建
            # 修复：使用 list_devices_by_ids 单次 IN 查询
            devices = await self._device_repo.list_devices_by_ids(device_ids)
        else:
            # FIXED(严重): 原问题-list_all(size=10000)硬编码上限，超出被静默截断;修复-分页循环加载
            devices = await self._list_all_paginated(self._device_repo)

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
        for device in devices:
            # FIXED-P1: 原问题-CSV导出未净化单元格值，存在CSV注入风险
            sanitized = {k: _sanitize_csv_cell(v) for k, v in device.items()}
            writer.writerow(sanitized)
        return output.getvalue()

    async def export_rules(
        self,
        rule_ids: list[str] | None = None,
        format: ExportFormat = ExportFormat.JSON,
    ) -> str:
        """Export rules to specified format"""
        if rule_ids:
            # FIXED(严重): 原问题-循环 await self._rule_repo.get(rid) 导致 N+1 查询
            # 修复：使用 list_rules_by_ids 单次 IN 查询
            rules = await self._rule_repo.list_rules_by_ids(rule_ids)
        else:
            # FIXED(严重): 原问题-list_all(size=10000)硬编码上限，超出被静默截断;修复-分页循环加载
            rules = await self._list_all_paginated(self._rule_repo)

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
        for rule in rules:
            # FIXED-P1: 原问题-CSV导出未净化单元格值，存在CSV注入风险
            sanitized = {k: _sanitize_csv_cell(v) for k, v in rule.items()}
            writer.writerow(sanitized)
        return output.getvalue()

    async def export_all(
        self,
        format: ExportFormat = ExportFormat.JSON,
    ) -> str:
        """Export all configuration data"""
        # FIXED(严重): 原问题-list_all(size=10000)硬编码上限，超出被静默截断;修复-分页循环加载
        devices = await self._list_all_paginated(self._device_repo)
        rules = await self._list_all_paginated(self._rule_repo)

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
        """Import devices from data

        FIXED-P0: 原问题-逐条独立提交，部分失败时已导入设备无法回滚
        改为：使用单事务批量导入，任一条失败则整批回滚
        """
        result = ImportResult()

        try:
            if format == ExportFormat.JSON:
                items = self._parse_json(data)
            else:
                items = self._parse_devices_csv(data)

            result.total_count = len(items)

            if not items:
                return result

            database = getattr(self._device_repo, "_database", None)
            if database is not None:
                try:
                    async with database.session() as session:
                        # FIXED(严重-R2): 原问题-预检查循环内逐条 SELECT，N条数据 N次查询
                        # 修复-批量预查询所有已存在的 device_id
                        all_device_ids = [item.get("device_id") for item in items if item.get("device_id")]
                        existing_device_ids_set: set[str] = set()
                        if all_device_ids:
                            existing_result = await session.execute(
                                select(DeviceORM.device_id).where(DeviceORM.device_id.in_(all_device_ids))
                            )
                            existing_device_ids_set = {row[0] for row in existing_result}

                        if (
                            mode == ImportMode.RENAME
                        ):  # FIXED-P2: 原问题-预检用repo.get()创建独立session，TOCTOU窗口；改为同一session内查询ORM
                            for item in items:
                                device_id = item.get("device_id")
                                if device_id and device_id in existing_device_ids_set:
                                    new_id = f"{device_id}_imported"
                                    item["device_id"] = new_id
                                    result.warnings.append(f"Renamed device: {device_id} -> {new_id}")
                        elif mode == ImportMode.ERROR:
                            for item in items:
                                device_id = item.get("device_id")
                                if device_id and device_id in existing_device_ids_set:
                                    raise ValueError(f"Device already exists: {device_id}")
                        created, skipped, errors = await self._device_repo.upsert_bulk(
                            items,
                            session,
                            skip_existing=(mode == ImportMode.SKIP),
                        )
                        if any(e for e in errors):
                            await session.rollback()
                            result.success = False
                            result.error_count = sum(1 for e in errors if e)
                            result.errors = [e for e in errors if e]
                            result.imported_count = 0
                            return result
                        await session.commit()
                        result.imported_count = created
                        result.skipped_count = skipped
                except Exception as e:
                    result.success = False
                    result.errors.append(f"Atomic import failed: {str(e)}")
                    return result
            else:
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
        """Parse JSON export data

        FIXED(S-07): 原问题-解析结果未进行类型校验，攻击者可构造特殊 JSON
        使解析结果为非预期类型（如 string 而非 dict，或 list 中混入非 dict 元素），
        导致后续遍历/访问抛出 AttributeError/KeyError 或触发未处理异常路径。
        修复-对返回值进行严格类型校验：顶层必须为 dict 或 list，最终结果必须为 list[dict]。
        """
        parsed = json.loads(data)

        # 顶层结构校验：仅允许 dict（带 devices/rules 键）或 list
        if not isinstance(parsed, (dict, list)):
            raise ValueError(f"Invalid export format: expected dict or list at top level, got {type(parsed).__name__}")

        # 根据顶层类型提取目标列表
        if isinstance(parsed, dict):
            if "devices" in parsed:
                items = parsed["devices"]
                expected_key = "devices"
            elif "rules" in parsed:
                items = parsed["rules"]
                expected_key = "rules"
            else:
                raise ValueError("Invalid export format: missing 'devices' or 'rules' key in dict")
        else:
            items = parsed
            expected_key = "<top-level list>"

        # 顶层列表校验：必须是 list
        if not isinstance(items, list):
            raise ValueError(f"Invalid export format: expected list for '{expected_key}', got {type(items).__name__}")

        # 每个元素必须是 dict，防止后续 item.get() 抛 AttributeError
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Invalid export format: item at index {idx} in '{expected_key}' "
                    f"expected dict, got {type(item).__name__}"
                )

        return items

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
        """Import rules from data

        FIXED-P0: 原问题-逐条独立提交，部分失败时已导入规则无法回滚
        改为：使用单事务批量导入，任一条失败则整批回滚
        """
        result = ImportResult()

        try:
            if format == ExportFormat.JSON:
                items = self._parse_json(data)
            else:
                items = self._parse_rules_csv(data)

            result.total_count = len(items)

            if not items:
                return result

            database = getattr(self._rule_repo, "_database", None)
            if database is not None:
                try:
                    async with database.session() as session:
                        # FIXED(严重-R2): 原问题-预检查循环内逐条 SELECT，N条数据 N次查询
                        # 修复-批量预查询所有已存在的 rule_id
                        all_rule_ids = [item.get("rule_id") for item in items if item.get("rule_id")]
                        existing_rule_ids_set: set[str] = set()
                        if all_rule_ids:
                            existing_result = await session.execute(
                                select(RuleORM.rule_id).where(RuleORM.rule_id.in_(all_rule_ids))
                            )
                            existing_rule_ids_set = {row[0] for row in existing_result}

                        if (
                            mode == ImportMode.RENAME
                        ):  # FIXED-P2: 原问题-预检用repo.get()创建独立session，TOCTOU窗口；改为同一session内查询ORM
                            for item in items:
                                rule_id = item.get("rule_id")
                                if rule_id and rule_id in existing_rule_ids_set:
                                    new_id = f"{rule_id}_imported"
                                    item["rule_id"] = new_id
                                    result.warnings.append(f"Renamed rule: {rule_id} -> {new_id}")
                        elif mode == ImportMode.ERROR:
                            for item in items:
                                rule_id = item.get("rule_id")
                                if rule_id and rule_id in existing_rule_ids_set:
                                    raise ValueError(f"Rule already exists: {rule_id}")
                        created, skipped, errors = await self._rule_repo.upsert_bulk(
                            items,
                            session,
                            skip_existing=(mode == ImportMode.SKIP),
                        )
                        if any(e for e in errors):
                            await session.rollback()
                            result.success = False
                            result.error_count = sum(1 for e in errors if e)
                            result.errors = [e for e in errors if e]
                            result.imported_count = 0
                            return result
                        await session.commit()
                        result.imported_count = created
                        result.skipped_count = skipped
                except Exception as e:
                    result.success = False
                    result.errors.append(f"Atomic import failed: {str(e)}")
                    return result
            else:
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
        """Import all configuration data

        FIXED(S-08): 原问题-import_all 回滚逻辑失效
        1. 异常时未显式 rollback，仅依赖上下文管理器隐式回滚
        2. 未记录 ERROR 日志包含异常信息和已导入数量
        3. rollback 后未确认数据库状态
        4. fallback 路径中规则导入失败后手动删除设备，非事务回滚，可能失败
        修复-确保整个导入过程在单个事务内，异常时立即 rollback 并记录日志，
        rollback 后查询关键表行数确认数据库状态。
        """
        results: dict[str, ImportResult] = {}

        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse import data: %s", e)
            # FIXED-mypy: 返回类型为 dict[str, ImportResult]，需用 ImportResult 包装错误信息
            return {"error": ImportResult(success=False, error_count=1, errors=[str(e)])}

        # FIXED(S-07): 对解析结果进行类型校验，防止非 dict 类型导致后续 .get() 抛 AttributeError
        if not isinstance(parsed, dict):
            logger.error(
                "Invalid import data: expected dict at top level, got %s",
                type(parsed).__name__,
            )
            return {"error": ImportResult(success=False, error_count=1, errors=[f"Invalid import data: expected dict, got {type(parsed).__name__}"])}

        devices_data = parsed.get("devices", [])
        rules_data = parsed.get("rules", [])

        # 校验 devices_data 和 rules_data 类型，防止非 list 导致后续遍历异常
        if not isinstance(devices_data, list):
            logger.error(
                "Invalid devices data: expected list, got %s",
                type(devices_data).__name__,
            )
            return {"error": ImportResult(success=False, error_count=1, errors=[f"Invalid devices data: expected list, got {type(devices_data).__name__}"])}
        if not isinstance(rules_data, list):
            logger.error(
                "Invalid rules data: expected list, got %s",
                type(rules_data).__name__,
            )
            return {"error": ImportResult(success=False, error_count=1, errors=[f"Invalid rules data: expected list, got {type(rules_data).__name__}"])}

        device_database = getattr(self._device_repo, "_database", None)
        rule_database = getattr(self._rule_repo, "_database", None)

        if device_database is not None and rule_database is not None and (devices_data or rules_data):
            # FIXED(S-08): 整个导入过程在单个事务内执行，异常时立即 rollback
            try:
                async with device_database.session() as session:
                    try:
                        if devices_data:
                            d_created, d_skipped, d_errors = await self._device_repo.upsert_bulk(
                                devices_data,
                                session,
                                skip_existing=(mode == ImportMode.SKIP),
                            )
                        else:
                            d_created, d_skipped, d_errors = 0, 0, []
                        if rules_data:
                            r_created, r_skipped, r_errors = await self._rule_repo.upsert_bulk(
                                rules_data,
                                session,
                                skip_existing=(mode == ImportMode.SKIP),
                            )
                        else:
                            r_created, r_skipped, r_errors = 0, 0, []

                        all_errors = [e for e in d_errors if e] + [e for e in r_errors if e]
                        if all_errors:
                            # FIXED(S-08): 显式 rollback 并记录 ERROR 日志包含已导入数量
                            await session.rollback()
                            logger.error(
                                "Import all failed with %d errors, transaction rolled back. "
                                "Devices imported before rollback: %d, Rules imported before rollback: %d",
                                len(all_errors),
                                d_created,
                                r_created,
                            )
                            # FIXED(S-08): rollback 后再次确认数据库状态（查询关键表行数）
                            try:
                                # FIXED-P1: 原问题-加载全表再计数导致大表 OOM；改为 COUNT 查询
                                dev_count_result = await session.execute(select(func.count()).select_from(DeviceORM))
                                dev_count_after = dev_count_result.scalar() or 0
                                rule_count_result = await session.execute(select(func.count()).select_from(RuleORM))
                                rule_count_after = rule_count_result.scalar() or 0
                                logger.info(
                                    "Database state after rollback: devices=%d, rules=%d",
                                    dev_count_after,
                                    rule_count_after,
                                )
                            except Exception as verify_err:
                                logger.error(
                                    "Failed to verify database state after rollback: %s",
                                    verify_err,
                                )
                            results["devices"] = ImportResult(
                                success=False,
                                total_count=len(devices_data),
                                error_count=len([e for e in d_errors if e]),
                                errors=[e for e in d_errors if e],
                            )
                            results["rules"] = ImportResult(
                                success=False,
                                total_count=len(rules_data),
                                error_count=len([e for e in r_errors if e]),
                                errors=[e for e in r_errors if e],
                            )
                            return results

                        await session.commit()
                        results["devices"] = ImportResult(
                            success=True,
                            total_count=len(devices_data),
                            imported_count=d_created,
                            skipped_count=d_skipped,
                        )
                        results["rules"] = ImportResult(
                            success=True,
                            total_count=len(rules_data),
                            imported_count=r_created,
                            skipped_count=r_skipped,
                        )
                        return results
                    except Exception as e:
                        # FIXED(S-08): 异常时立即 rollback，并记录 ERROR 日志包含异常信息和已导入数量
                        try:
                            await session.rollback()
                        except Exception as rb_err:
                            logger.error("Rollback failed: %s", rb_err)
                        # rollback 后再次确认数据库状态（查询关键表行数）
                        try:
                            # FIXED-P1: 原问题-加载全表再计数导致大表 OOM；改为 COUNT 查询
                            dev_count_result = await session.execute(select(func.count()).select_from(DeviceORM))
                            dev_count_after = dev_count_result.scalar() or 0
                            rule_count_result = await session.execute(select(func.count()).select_from(RuleORM))
                            rule_count_after = rule_count_result.scalar() or 0
                            logger.info(
                                "Database state after rollback: devices=%d, rules=%d",
                                dev_count_after,
                                rule_count_after,
                            )
                        except Exception as verify_err:
                            logger.error(
                                "Failed to verify database state after rollback: %s",
                                verify_err,
                            )
                        logger.error(
                            "Import all exception: %s. Transaction rolled back. "
                            "Devices data count: %d, Rules data count: %d",
                            str(e),
                            len(devices_data),
                            len(rules_data),
                        )
                        results["devices"] = ImportResult(
                            success=False,
                            errors=[f"Atomic import failed: {str(e)}"],
                            total_count=len(devices_data),
                        )
                        results["rules"] = ImportResult(
                            success=False,
                            errors=[f"Atomic import failed: {str(e)}"],
                            total_count=len(rules_data),
                        )
                        return results
            except Exception as e:
                # session 创建失败等极端情况
                logger.error(
                    "Import all session error: %s. Devices data count: %d, Rules data count: %d",
                    str(e),
                    len(devices_data),
                    len(rules_data),
                )
                results["devices"] = ImportResult(success=False, errors=[f"Atomic import failed: {str(e)}"])
                results["rules"] = ImportResult(success=False, errors=[f"Atomic import failed: {str(e)}"])
                return results

        # Fallback 路径：无 database 时分别导入
        if devices_data:
            devices_json = json.dumps({"devices": devices_data})
            results["devices"] = await self.import_devices(
                devices_json,
                ExportFormat.JSON,
                mode,
            )

        device_result = results.get("devices")
        if device_result and not device_result.success and device_result.imported_count == 0:
            logger.warning("Device import failed entirely, skipping rule import to maintain consistency")
            results["rules"] = ImportResult(success=False, errors=["Skipped: device import failed"])
            return results

        if rules_data:
            rules_json = json.dumps({"rules": rules_data})
            results["rules"] = await self.import_rules(
                rules_json,
                ExportFormat.JSON,
                mode,
            )

        rule_result = results.get("rules")
        if rule_result and not rule_result.success and device_result and device_result.imported_count > 0:
            # FIXED(S-08): fallback 路径中规则导入失败，记录 ERROR 日志包含已导入数量和回滚结果
            logger.warning(
                "Rule import failed, rolling back %d imported devices for consistency",
                device_result.imported_count,
            )
            rollback_count = 0
            for item in device_result.imported_items:
                try:
                    device_id = item.get("device_id")
                    if device_id:
                        await self._device_repo.delete(device_id)
                        rollback_count += 1
                except Exception as e:
                    logger.error("Rollback device %s failed: %s", item.get("device_id"), e)
            logger.error(
                "Rule import failed, rolled back %d/%d devices. Rule errors: %s",
                rollback_count,
                device_result.imported_count,
                str(rule_result.errors),
            )
            device_result.imported_count = 0
            device_result.success = False
            device_result.errors.append("Rolled back due to rule import failure")

        return results


class TemplateService:
    """Service for managing device and rule templates

    FIXED-P1: 原问题-TemplateService纯内存存储，进程重启后模板丢失
    Device templates now delegate to TemplateRepo for persistence.
    FIXED-P2: Rule templates and device groups now also persisted via database.
    """

    def __init__(self, template_repo: Any | None = None, database: Any | None = None):
        self._template_repo = template_repo
        self._database = database  # FIXED-P2: 原问题-RuleTemplate/DeviceGroup纯内存；改为注入database用于ORM持久化
        self._device_templates: dict[
            str, DeviceTemplate
        ] = {}  # FIXED-P0: 原问题-_device_templates未在__init__中定义，导致apply_device_template/export_templates/import_templates抛AttributeError  # noqa: E501
        self._rule_templates: dict[str, RuleTemplate] = {}
        self._device_groups: dict[str, DeviceGroup] = {}
        self._group_members: dict[str, list[str]] = defaultdict(list)
        # FIXED-P1: 保护共享内存集合的并发读写，避免多线程并发修改引发字典/列表损坏
        self._lock = threading.Lock()

    # Device Templates
    async def create_device_template(self, template: DeviceTemplate) -> DeviceTemplate:
        """Create a new device template (persisted via TemplateRepo)"""
        if self._template_repo is not None:
            data = {
                "name": template.name or f"tmpl_{uuid.uuid4().hex[:8]}",
                "protocol": template.protocol,
                "config_template": template.default_config,
                "point_templates": template.default_points,
            }
            result = await self._template_repo.create(data)
            template.template_id = result.get("name", "")
            template.created_at = result.get("created_at", datetime.now(UTC).isoformat())
            template.updated_at = template.created_at
            return template
        if not template.template_id:
            template.template_id = f"tmpl_{uuid.uuid4().hex[:8]}"
        template.created_at = datetime.now(UTC).isoformat()
        template.updated_at = template.created_at
        # FIXED-P1: 保护 _device_templates_fallback 并发读写
        with self._lock:
            self._device_templates_fallback: dict = getattr(self, "_device_templates_fallback", {})
            self._device_templates_fallback[template.template_id] = template
        return template

    async def get_device_template(self, template_id: str) -> DeviceTemplate | None:
        if self._template_repo is not None:
            result = await self._template_repo.get(template_id)
            if result is None:
                return None
            return DeviceTemplate(
                template_id=result.get("name", ""),
                name=result.get("name", ""),
                protocol=result.get("protocol", ""),
                default_config=result.get("config_template", {}),
                default_points=result.get("point_templates", []),
                created_at=str(result.get("created_at", "")),
                updated_at=str(result.get("created_at", "")),
            )
        # FIXED-P1: 快照读取 fallback，避免并发修改
        with self._lock:
            fallback: dict = getattr(self, "_device_templates_fallback", {})
            return fallback.get(template_id)

    async def list_device_templates(self) -> list[DeviceTemplate]:
        if self._template_repo is not None:
            results, _total = await self._template_repo.list_all()
            return [
                DeviceTemplate(
                    template_id=r.get("name", ""),
                    name=r.get("name", ""),
                    protocol=r.get("protocol", ""),
                    default_config=r.get("config_template", {}),
                    default_points=r.get("point_templates", []),
                    created_at=str(r.get("created_at", "")),
                    updated_at=str(r.get("created_at", "")),
                )
                for r in results
            ]
        # FIXED-P1: 快照读取 fallback 列表，避免并发修改
        with self._lock:
            fallback: dict = getattr(self, "_device_templates_fallback", {})
            return list(fallback.values())

    async def delete_device_template(self, template_id: str) -> bool:
        if self._template_repo is not None:
            return await self._template_repo.delete(template_id)
        # FIXED-P1: 保护 fallback 并发删除
        with self._lock:
            fallback: dict = getattr(self, "_device_templates_fallback", {})
            if template_id in fallback:
                del fallback[template_id]
                return True
            return False

    async def apply_device_template(
        self,
        template_id: str,
        device_name: str,
        custom_config: dict | None = None,
    ) -> dict:
        """Create a device from a template"""
        # FIXED(致命): 原代码从 self._device_templates 读取（始终为空），
        # 改为 async 并调用 get_device_template 正确查询 DB/fallback
        template = await self.get_device_template(template_id)
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
    async def create_rule_template(
        self, template: RuleTemplate
    ) -> RuleTemplate:  # FIXED-P2: 原问题-纯内存存储；改为async+ORM持久化
        """Create a new rule template"""
        if not template.template_id:
            template.template_id = f"rtmpl_{uuid.uuid4().hex[:8]}"
        template.created_at = datetime.now(UTC).isoformat()
        template.updated_at = template.created_at
        if self._database is not None:
            try:
                from edgelite.models.db import RuleTemplateORM

                async with self._database.session() as session:
                    orm = RuleTemplateORM(
                        template_id=template.template_id,
                        name=template.name,
                        description=template.description,
                        rule_type=template.rule_type,
                        default_conditions=json.dumps(template.default_conditions, ensure_ascii=False),
                        default_severity=template.default_severity,
                        default_duration=template.default_duration,
                        notify_channels=json.dumps(template.notify_channels, ensure_ascii=False),
                    )
                    session.add(orm)
                    await session.commit()
            except Exception as e:
                logger.error("Rule template DB persist failed: %s", e)
                # FIXED-P1: 保护 _rule_templates 并发写入
                with self._lock:
                    self._rule_templates[template.template_id] = template
                return template
        else:
            # FIXED-P1: 保护 _rule_templates 并发写入
            with self._lock:
                self._rule_templates[template.template_id] = template
        logger.info("Rule template created: %s", template.template_id)
        return template

    async def get_rule_template(self, template_id: str) -> RuleTemplate | None:  # FIXED-P2: 原问题-纯内存；改为优先查DB
        """Get a rule template by ID"""
        if self._database is not None:
            try:
                from edgelite.models.db import RuleTemplateORM

                async with self._database.session() as session:
                    result = await session.execute(
                        __import__("sqlalchemy")
                        .select(RuleTemplateORM)
                        .where(RuleTemplateORM.template_id == template_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm:
                        return RuleTemplate(
                            template_id=orm.template_id,
                            name=orm.name,
                            description=orm.description,
                            rule_type=orm.rule_type,
                            default_conditions=json.loads(orm.default_conditions),
                            default_severity=orm.default_severity,
                            default_duration=orm.default_duration,
                            notify_channels=json.loads(orm.notify_channels),
                            created_at=str(orm.created_at),
                            updated_at=str(orm.updated_at),
                        )
            except Exception as e:
                logger.error("Rule template DB read failed: %s", e)
        # FIXED-P1: 快照读取 _rule_templates，避免并发修改
        with self._lock:
            return self._rule_templates.get(template_id)

    async def list_rule_templates(self) -> list[RuleTemplate]:  # FIXED-P2: 原问题-纯内存；改为优先查DB
        """List all rule templates"""
        if self._database is not None:
            try:
                from edgelite.models.db import RuleTemplateORM

                async with self._database.session() as session:
                    result = await session.execute(__import__("sqlalchemy").select(RuleTemplateORM))
                    rows = result.scalars().all()
                    return [
                        RuleTemplate(
                            template_id=r.template_id,
                            name=r.name,
                            description=r.description,
                            rule_type=r.rule_type,
                            default_conditions=json.loads(r.default_conditions),
                            default_severity=r.default_severity,
                            default_duration=r.default_duration,
                            notify_channels=json.loads(r.notify_channels),
                            created_at=str(r.created_at),
                            updated_at=str(r.updated_at),
                        )
                        for r in rows
                    ]
            except Exception as e:
                logger.error("Rule template DB list failed: %s", e)
        # FIXED-P1: 快照读取 _rule_templates 列表，避免并发修改
        with self._lock:
            return list(self._rule_templates.values())

    async def update_rule_template(  # FIXED-P2: 原问题-纯内存更新；改为DB持久化
        self,
        template_id: str,
        data: dict,
    ) -> RuleTemplate | None:
        """Update a rule template"""
        if self._database is not None:
            try:
                from edgelite.models.db import RuleTemplateORM

                async with self._database.session() as session:
                    result = await session.execute(
                        __import__("sqlalchemy")
                        .select(RuleTemplateORM)
                        .where(RuleTemplateORM.template_id == template_id)
                    )
                    orm = result.scalar_one_or_none()
                    if not orm:
                        return None
                    for key in ("name", "description", "rule_type", "default_severity"):
                        if key in data:
                            setattr(orm, key, data[key])
                    if "default_conditions" in data:
                        orm.default_conditions = json.dumps(data["default_conditions"], ensure_ascii=False)
                    if "default_duration" in data:
                        orm.default_duration = data["default_duration"]
                    if "notify_channels" in data:
                        orm.notify_channels = json.dumps(data["notify_channels"], ensure_ascii=False)
                    await session.commit()
                    return RuleTemplate(
                        template_id=orm.template_id,
                        name=orm.name,
                        description=orm.description,
                        rule_type=orm.rule_type,
                        default_conditions=json.loads(orm.default_conditions),
                        default_severity=orm.default_severity,
                        default_duration=orm.default_duration,
                        notify_channels=json.loads(orm.notify_channels),
                        created_at=str(orm.created_at),
                        updated_at=str(orm.updated_at),
                    )
            except Exception as e:
                logger.error("Rule template DB update failed: %s", e)
        # FIXED-P1: 保护 _rule_templates 读改写
        with self._lock:
            template = self._rule_templates.get(template_id)
            if not template:
                return None
            for key, value in data.items():
                if hasattr(template, key):
                    setattr(template, key, value)
            template.updated_at = datetime.now(UTC).isoformat()
        logger.info("Rule template updated: %s", template_id)
        return template

    async def delete_rule_template(self, template_id: str) -> bool:  # FIXED-P2: 原问题-纯内存删除；改为DB持久化
        """Delete a rule template"""
        if self._database is not None:
            try:
                from edgelite.models.db import RuleTemplateORM

                async with self._database.session() as session:
                    result = await session.execute(
                        __import__("sqlalchemy")
                        .delete(RuleTemplateORM)
                        .where(RuleTemplateORM.template_id == template_id)
                    )
                    await session.commit()
                    if result.rowcount > 0:
                        logger.info("Rule template deleted: %s", template_id)
                        return True
                    return False
            except Exception as e:
                logger.error("Rule template DB delete failed: %s", e)
        # FIXED-P1: 保护 _rule_templates 并发删除
        with self._lock:
            if template_id in self._rule_templates:
                del self._rule_templates[template_id]
                logger.info("Rule template deleted: %s", template_id)
                return True
            return False

    async def apply_rule_template(
        self,
        template_id: str,
        rule_name: str,
        device_id: str,
        custom_conditions: list[dict] | None = None,
    ) -> dict:
        """Create a rule from a template"""
        # FIXED(致命): 原代码从 self._rule_templates 读取（DB 模式下为空），
        # 改为 async 并调用 get_rule_template 正确查询 DB/fallback
        template = await self.get_rule_template(template_id)
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
    async def create_device_group(
        self, group: DeviceGroup
    ) -> DeviceGroup:  # FIXED-P2: 原问题-纯内存；改为async+ORM持久化
        """Create a new device group"""
        if not group.group_id:
            group.group_id = f"grp_{uuid.uuid4().hex[:8]}"
        group.created_at = datetime.now(UTC).isoformat()
        group.updated_at = group.created_at
        if self._database is not None:
            try:
                from edgelite.models.db import DeviceGroupORM

                async with self._database.session() as session:
                    orm = DeviceGroupORM(
                        group_id=group.group_id,
                        name=group.name,
                        description=group.description,
                        parent_id=group.parent_id or None,
                        device_ids=json.dumps(group.device_ids, ensure_ascii=False),
                        tags=json.dumps(group.tags, ensure_ascii=False),
                    )
                    session.add(orm)
                    await session.commit()
            except Exception as e:
                logger.error("Device group DB persist failed: %s", e)
                # FIXED-P1: 保护 _device_groups 并发写入
                with self._lock:
                    self._device_groups[group.group_id] = group
                return group
        else:
            # FIXED-P1: 保护 _device_groups 并发写入
            with self._lock:
                self._device_groups[group.group_id] = group
        logger.info("Device group created: %s", group.group_id)
        return group

    async def get_device_group(self, group_id: str) -> DeviceGroup | None:  # FIXED-P2: 原问题-纯内存；改为优先查DB
        """Get a device group by ID"""
        if self._database is not None:
            try:
                from edgelite.models.db import DeviceGroupORM

                async with self._database.session() as session:
                    result = await session.execute(
                        __import__("sqlalchemy").select(DeviceGroupORM).where(DeviceGroupORM.group_id == group_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm:
                        return DeviceGroup(
                            group_id=orm.group_id,
                            name=orm.name,
                            description=orm.description,
                            parent_id=orm.parent_id or "",
                            device_ids=json.loads(orm.device_ids),
                            tags=json.loads(orm.tags),
                            created_at=str(orm.created_at),
                            updated_at=str(orm.updated_at),
                        )
            except Exception as e:
                logger.error("Device group DB read failed: %s", e)
        # FIXED-P1: 快照读取 _device_groups，避免并发修改
        with self._lock:
            return self._device_groups.get(group_id)

    async def list_device_groups(self) -> list[DeviceGroup]:  # FIXED-P2: 原问题-纯内存；改为优先查DB
        """List all device groups"""
        if self._database is not None:
            try:
                from edgelite.models.db import DeviceGroupORM

                async with self._database.session() as session:
                    result = await session.execute(__import__("sqlalchemy").select(DeviceGroupORM))
                    rows = result.scalars().all()
                    return [
                        DeviceGroup(
                            group_id=r.group_id,
                            name=r.name,
                            description=r.description,
                            parent_id=r.parent_id or "",
                            device_ids=json.loads(r.device_ids),
                            tags=json.loads(r.tags),
                            created_at=str(r.created_at),
                            updated_at=str(r.updated_at),
                        )
                        for r in rows
                    ]
            except Exception as e:
                logger.error("Device group DB list failed: %s", e)
        # FIXED-P1: 快照读取 _device_groups 列表，避免并发修改
        with self._lock:
            return list(self._device_groups.values())

    async def update_device_group(  # FIXED-P2: 原问题-纯内存更新；改为DB持久化
        self,
        group_id: str,
        data: dict,
    ) -> DeviceGroup | None:
        """Update a device group"""
        if self._database is not None:
            try:
                from edgelite.models.db import DeviceGroupORM

                async with self._database.session() as session:
                    result = await session.execute(
                        __import__("sqlalchemy").select(DeviceGroupORM).where(DeviceGroupORM.group_id == group_id)
                    )
                    orm = result.scalar_one_or_none()
                    if not orm:
                        return None
                    for key in ("name", "description", "parent_id"):
                        if key in data:
                            setattr(orm, key, data[key])
                    if "device_ids" in data:
                        orm.device_ids = json.dumps(data["device_ids"], ensure_ascii=False)
                    if "tags" in data:
                        orm.tags = json.dumps(data["tags"], ensure_ascii=False)
                    await session.commit()
                    return DeviceGroup(
                        group_id=orm.group_id,
                        name=orm.name,
                        description=orm.description,
                        parent_id=orm.parent_id or "",
                        device_ids=json.loads(orm.device_ids),
                        tags=json.loads(orm.tags),
                        created_at=str(orm.created_at),
                        updated_at=str(orm.updated_at),
                    )
            except Exception as e:
                logger.error("Device group DB update failed: %s", e)
        # FIXED-P1: 保护 _device_groups 读改写
        with self._lock:
            group = self._device_groups.get(group_id)
            if not group:
                return None
            for key, value in data.items():
                if hasattr(group, key):
                    setattr(group, key, value)
            group.updated_at = datetime.now(UTC).isoformat()
        logger.info("Device group updated: %s", group_id)
        return group

    async def delete_device_group(self, group_id: str) -> bool:  # FIXED-P2: 原问题-纯内存删除；改为DB持久化
        """Delete a device group"""
        if self._database is not None:
            try:
                from edgelite.models.db import DeviceGroupORM

                async with self._database.session() as session:
                    result = await session.execute(
                        __import__("sqlalchemy").delete(DeviceGroupORM).where(DeviceGroupORM.group_id == group_id)
                    )
                    await session.commit()
                    if result.rowcount > 0:
                        # FIXED-P1: 保护 _group_members 并发修改
                        with self._lock:
                            for _device_id, groups in list(
                                self._group_members.items()
                            ):  # FIXED(P3): 原问题-B007循环变量device_id未使用; 修复-改为_device_id
                                if group_id in groups:
                                    groups.remove(group_id)
                        logger.info("Device group deleted: %s", group_id)
                        return True
                    return False
            except Exception as e:
                logger.error("Device group DB delete failed: %s", e)
        # FIXED-P1: 保护 _device_groups 和 _group_members 并发删除
        with self._lock:
            if group_id in self._device_groups:
                for _device_id, groups in list(
                    self._group_members.items()
                ):  # FIXED(P3): 原问题-B007循环变量device_id未使用; 修复-改为_device_id
                    if group_id in groups:
                        groups.remove(group_id)
                del self._device_groups[group_id]
                logger.info("Device group deleted: %s", group_id)
                return True
            return False

    async def add_device_to_group(
        self, device_id: str, group_id: str
    ) -> bool:  # FIXED-P2: 原问题-内存操作不持久化；改为同步更新DB
        """Add a device to a group"""
        # FIXED-P1: 快照检查是否已是成员，避免并发重复添加
        with self._lock:
            if device_id in self._group_members and group_id in self._group_members[device_id]:
                return True
        # FIXED-P1: 原问题-先改内存后写DB，DB失败时内存已变更但不回滚，造成静默数据不一致
        # 修复-先写DB成功后再更新内存，DB失败时不修改内存并返回False
        if self._database is not None:
            try:
                from edgelite.models.db import DeviceGroupORM

                async with self._database.session() as session:
                    result = await session.execute(
                        __import__("sqlalchemy").select(DeviceGroupORM).where(DeviceGroupORM.group_id == group_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm:
                        ids = json.loads(orm.device_ids)
                        if device_id not in ids:
                            ids.append(device_id)
                            orm.device_ids = json.dumps(ids, ensure_ascii=False)
                            await session.commit()
            except Exception as e:
                logger.error("add_device_to_group DB update failed: %s", e)
                return False
        else:
            # FIXED-P1: 保护 _device_groups 并发修改
            with self._lock:
                group = self._device_groups.get(group_id)
                if group and device_id not in group.device_ids:
                    group.device_ids.append(device_id)
        # DB写入成功后才更新内存
        # FIXED-P1: 保护 _group_members 并发修改
        with self._lock:
            if device_id not in self._group_members:
                self._group_members[device_id] = []
            if group_id not in self._group_members[device_id]:
                self._group_members[device_id].append(group_id)
        logger.info("Device %s added to group %s", device_id, group_id)
        return True

    async def remove_device_from_group(
        self, device_id: str, group_id: str
    ) -> bool:  # FIXED-P2: 原问题-内存操作不持久化；改为同步更新DB
        """Remove a device from a group"""
        # FIXED-P1: 快照检查是否是成员
        with self._lock:
            if group_id not in self._group_members.get(device_id, []):
                return False
        # FIXED-P1: 原问题-先改内存后写DB，DB失败时内存已变更但不回滚，造成静默数据不一致
        # 修复-先写DB成功后再更新内存，DB失败时不修改内存并返回False
        if self._database is not None:
            try:
                from edgelite.models.db import DeviceGroupORM

                async with self._database.session() as session:
                    result = await session.execute(
                        __import__("sqlalchemy").select(DeviceGroupORM).where(DeviceGroupORM.group_id == group_id)
                    )
                    orm = result.scalar_one_or_none()
                    if orm:
                        ids = json.loads(orm.device_ids)
                        if device_id in ids:
                            ids.remove(device_id)
                            orm.device_ids = json.dumps(ids, ensure_ascii=False)
                            await session.commit()
            except Exception as e:
                logger.error("remove_device_from_group DB update failed: %s", e)
                return False
        else:
            # FIXED-P1: 保护 _device_groups 并发修改
            with self._lock:
                group = self._device_groups.get(group_id)
                if group and device_id in group.device_ids:
                    group.device_ids.remove(device_id)
        # DB写入成功后才更新内存
        # FIXED-P1: 保护 _group_members 并发修改
        with self._lock:
            if device_id in self._group_members and group_id in self._group_members[device_id]:
                self._group_members[device_id].remove(group_id)
        logger.info("Device %s removed from group %s", device_id, group_id)
        return True

    def get_device_groups(self, device_id: str) -> list[DeviceGroup]:
        """Get all groups a device belongs to"""
        # FIXED-P1: 快照读取，避免并发修改
        with self._lock:
            group_ids = list(self._group_members.get(device_id, []))
            return [self._device_groups[gid] for gid in group_ids if gid in self._device_groups]

    def get_group_devices(self, group_id: str) -> list[str]:
        """Get all devices in a group"""
        # FIXED-P1: 快照读取，避免并发修改
        with self._lock:
            group = self._device_groups.get(group_id)
            return list(group.device_ids) if group else []

    async def export_templates(self) -> str:
        """Export all templates to JSON"""
        # FIXED(致命): 原代码从 self._device_templates/self._rule_templates 读取（DB 模式下为空），
        # 改为 async 并调用 list_device_templates/list_rule_templates 正确查询 DB/fallback
        device_templates = await self.list_device_templates()
        rule_templates = await self.list_rule_templates()
        # FIXED-P1: 快照读取 _device_groups，避免并发修改
        with self._lock:
            device_groups_snapshot = list(self._device_groups.values())
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
                for t in device_templates
            ],
            "rule_templates": [
                {
                    **vars(t),
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                }
                for t in rule_templates
            ],
            "device_groups": [
                {
                    **vars(g),
                    "created_at": g.created_at,
                    "updated_at": g.updated_at,
                }
                for g in device_groups_snapshot
            ],
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    def import_templates(self, data: str) -> dict[str, int]:
        """Import templates from JSON"""
        result = {"device_templates": 0, "rule_templates": 0, "device_groups": 0}

        parsed = json.loads(data)

        # FIXED-P1: 保护共享集合的批量并发写入
        with self._lock:
            # FIX-P0: 原代码写入 self._device_templates，但所有读取方法
            # (get_device_template/list_device_templates/delete_device_template)
            # 均读取 self._device_templates_fallback，导致导入的模板在 fallback
            # 模式下不可见。统一写入 _device_templates_fallback，保持读写一致。
            # FIXED-mypy: 移除重复的类型注解 (在 line 862 已定义)，避免 no-redef 错误
            self._device_templates_fallback = getattr(self, "_device_templates_fallback", {})
            for t_data in parsed.get("device_templates", []):
                device_template = DeviceTemplate(**t_data)
                self._device_templates_fallback[device_template.template_id] = device_template
                result["device_templates"] += 1

            for t_data in parsed.get("rule_templates", []):
                rule_template = RuleTemplate(**t_data)
                self._rule_templates[rule_template.template_id] = rule_template
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
) -> DataExportService | None:
    """Get or create the export service

    FIXED-mypy: 返回类型修正为 Optional[DataExportService]，因为首次调用未提供 repos 时返回 None。
    内部使用 assert 缩窄类型，确保传给构造函数的参数非 None。
    """
    global _export_service
    if _export_service is None and all([device_repo, rule_repo, alarm_repo]):
        # FIXED-mypy: all() 检查不会缩窄类型，需显式 assert 让 mypy 知道参数非 None
        assert device_repo is not None
        assert rule_repo is not None
        assert alarm_repo is not None
        _export_service = DataExportService(device_repo, rule_repo, alarm_repo)
    return _export_service


def get_import_service(
    device_repo: DeviceRepo | None = None,
    rule_repo: RuleRepo | None = None,
) -> DataImportService | None:
    """Get or create the import service

    FIXED-mypy: 返回类型修正为 Optional[DataImportService]，因为首次调用未提供 repos 时返回 None。
    内部使用 assert 缩窄类型，确保传给构造函数的参数非 None。
    """
    global _import_service
    if _import_service is None and all([device_repo, rule_repo]):
        # FIXED-mypy: all() 检查不会缩窄类型，需显式 assert 让 mypy 知道参数非 None
        assert device_repo is not None
        assert rule_repo is not None
        _import_service = DataImportService(device_repo, rule_repo)
    return _import_service


def get_template_service() -> TemplateService:
    """Get or create the template service"""
    global _template_service
    if _template_service is None:
        _template_service = TemplateService()
    return _template_service
