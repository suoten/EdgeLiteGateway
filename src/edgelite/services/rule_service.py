"""规则管理业务逻辑"""

from __future__ import annotations

import logging

from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo

logger = logging.getLogger(__name__)


class RuleService:
    """规则管理业务逻辑"""

    def __init__(self, rule_repo: RuleRepo, device_repo: DeviceRepo):
        self._repo = rule_repo
        self._device_repo = device_repo

    async def create_rule(self, data: dict) -> dict:
        # 验证设备存在
        device_id = data.get("device_id")  # FIXED: 原问题-data["device_id"]硬索引
        if device_id is None:
            raise ValueError("Missing required field: device_id")
        device = await self._device_repo.get(device_id)
        if device is None:
            raise ValueError(f"Device not found: {device_id}")  # FIXED: 原问题-中文硬编码错误消息
        result = await self._repo.create(data)
        # FIXED: _app_state.evaluator可能为None
        try:
            from edgelite.app import _app_state

            if _app_state.evaluator:
                _app_state.evaluator.invalidate_cache()
        except (ImportError, AttributeError):
            logger.debug("Evaluator cache invalidation skipped (module not available)")  # FIXED: 原问题-静默pass可能导致规则缓存不更新
        return result

    async def get_rule(self, rule_id: str) -> dict | None:
        return await self._repo.get(rule_id)

    async def list_rules(
        self,
        page: int = 1,
        size: int = 20,
        device_id: str | None = None,
        search: str | None = None,
        severity: str | None = None,
    ) -> tuple[list[dict], int]:
        return await self._repo.list_all(page, size, device_id, search, severity)

    async def update_rule(self, rule_id: str, data: dict) -> dict | None:
        result = await self._repo.update(rule_id, data)
        if result:
            from edgelite.app import _app_state

            if _app_state.evaluator:
                _app_state.evaluator.invalidate_cache()
        return result

    async def delete_rule(self, rule_id: str) -> bool:
        result = await self._repo.delete(rule_id)
        if result:
            from edgelite.app import _app_state

            if _app_state.evaluator:
                _app_state.evaluator.cleanup_duration_tracker(rule_id)
                _app_state.evaluator.invalidate_cache()
        return result

    async def enable_rule(self, rule_id: str) -> dict | None:
        result = await self._repo.toggle(rule_id, True)
        if result:
            from edgelite.app import _app_state

            if _app_state.evaluator:
                _app_state.evaluator.invalidate_cache()
        return result

    async def disable_rule(self, rule_id: str) -> dict | None:
        result = await self._repo.toggle(rule_id, False)
        if result:
            from edgelite.app import _app_state

            if _app_state.evaluator:
                _app_state.evaluator.invalidate_cache()
        return result

    async def test_rule(self, rule_id: str, point_values: dict[str, float]) -> dict:
        """测试规则执行"""
        rule = await self._repo.get(rule_id)
        if rule is None:
            raise ValueError(f"Rule not found: {rule_id}")  # FIXED: 原问题-中文硬编码错误消息

        # FIXED: conditions可能为None
        conditions = rule.get("conditions") or []
        logic = rule.get("logic", "AND")

        if not conditions:
            return {
                "rule_id": rule_id,
                "logic": logic,
                "condition_results": [],
                "all_matched": False,
            }

        results = []
        for cond in conditions:
            point = cond.get("point")  # FIXED: 原问题-cond["point"]硬索引
            operator = cond.get("operator")  # FIXED: 原问题-cond["operator"]硬索引
            threshold = cond.get("threshold")  # FIXED: 原问题-cond["threshold"]硬索引
            if point is None or operator is None or threshold is None:
                results.append({"condition": cond, "matched": False, "actual_value": None})
                continue
            value = point_values.get(point)

            if value is None:
                results.append({"condition": cond, "matched": False, "actual_value": None})
                continue

            matched = self._compare(value, operator, threshold)
            results.append({"condition": cond, "matched": matched, "actual_value": value})

        all_matched = (
            all(r["matched"] for r in results)
            if logic == "AND"
            else any(r["matched"] for r in results)
        )

        return {
            "rule_id": rule_id,
            "logic": logic,
            "condition_results": results,
            "all_matched": all_matched,
        }

    @staticmethod
    def _compare(value: float, operator: str, threshold: float) -> bool:
        ops = {
            ">": value > threshold,
            ">=": value >= threshold,
            "<": value < threshold,
            "<=": value <= threshold,
            "==": abs(value - threshold) < 1e-9,
            "!=": abs(value - threshold) >= 1e-9,
        }
        return ops.get(operator, False)
