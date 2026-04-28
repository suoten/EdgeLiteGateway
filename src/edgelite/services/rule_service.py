"""规则管理业务逻辑"""

from __future__ import annotations

from edgelite.storage.sqlite_repo import RuleRepo, DeviceRepo


class RuleService:
    """规则管理业务逻辑"""

    def __init__(self, rule_repo: RuleRepo, device_repo: DeviceRepo):
        self._repo = rule_repo
        self._device_repo = device_repo

    async def create_rule(self, data: dict) -> dict:
        # 验证设备存在
        device = await self._device_repo.get(data["device_id"])
        if device is None:
            raise ValueError(f"设备不存在: {data['device_id']}")
        result = await self._repo.create(data)
        from edgelite.app import _app_state
        if _app_state.evaluator:
            _app_state.evaluator.invalidate_cache()
        return result

    async def get_rule(self, rule_id: str) -> dict | None:
        return await self._repo.get(rule_id)

    async def list_rules(self, page: int = 1, size: int = 20, device_id: str | None = None) -> tuple[list[dict], int]:
        return await self._repo.list_all(page, size, device_id)

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
            raise ValueError(f"规则不存在: {rule_id}")

        conditions = rule["conditions"]
        logic = rule["logic"]

        if not conditions:
            return {"rule_id": rule_id, "logic": logic, "condition_results": [], "all_matched": False}

        results = []
        for cond in conditions:
            point = cond["point"]
            operator = cond["operator"]
            threshold = cond["threshold"]
            value = point_values.get(point)

            if value is None:
                results.append({"condition": cond, "matched": False, "actual_value": None})
                continue

            matched = self._compare(value, operator, threshold)
            results.append({"condition": cond, "matched": matched, "actual_value": value})

        all_matched = all(r["matched"] for r in results) if logic == "AND" else any(r["matched"] for r in results)

        return {"rule_id": rule_id, "logic": logic, "condition_results": results, "all_matched": all_matched}

    @staticmethod
    def _compare(value: float, operator: str, threshold: float) -> bool:
        ops = {">": value > threshold, ">=": value >= threshold, "<": value < threshold, "<=": value <= threshold, "==": abs(value - threshold) < 1e-9}
        return ops.get(operator, False)
