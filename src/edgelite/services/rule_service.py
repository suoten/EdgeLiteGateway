"""规则管理业务逻辑"""

from __future__ import annotations

import asyncio
import logging

from edgelite.storage.sqlite_repo import DeviceRepo, RuleRepo

logger = logging.getLogger(__name__)

# FIXED(严重): 单设备规则数量上限，防止攻击者创建大量规则导致评估超时和告警风暴
_MAX_RULES_PER_DEVICE = 200


class RuleService:
    """规则管理业务逻辑"""

    def __init__(self, rule_repo: RuleRepo, device_repo: DeviceRepo):
        self._repo = rule_repo
        self._device_repo = device_repo
        # R11-SVC-04: 将全局 _create_lock 细化为 per-device 锁字典，
        # 避免不同设备的 create_rule 互相串行化
        self._create_locks: dict[str, asyncio.Lock] = {}
        self._create_locks_meta = asyncio.Lock()  # 保护 _create_locks 字典本身

    async def _get_create_lock(self, device_id: str) -> asyncio.Lock:
        """R11-SVC-04: 获取指定设备的创建锁，按需创建并缓存"""
        async with self._create_locks_meta:
            lock = self._create_locks.get(device_id)
            if lock is None:
                lock = asyncio.Lock()
                self._create_locks[device_id] = lock
            return lock

    async def create_rule(self, data: dict, created_by: str | None = None) -> dict:
        device_id = data.get("device_id")
        if device_id is None:
            raise ValueError("Missing required field: device_id")
        device = await self._device_repo.get(device_id)
        if device is None:
            raise ValueError(f"Device not found: {device_id}")
        # 并发安全: 加锁串行化"查询数量+创建规则"的 check-then-act 流程，
        # 防止并发调用绕过数量上限校验
        # R11-SVC-04: 使用 per-device 锁，不同设备的创建互不阻塞
        async with await self._get_create_lock(device_id):
            # FIXED(严重): 校验单设备规则数量上限，超限时拒绝创建，防止评估超时和告警风暴
            _, existing_count = await self._repo.list_all(
                page=1, size=1, device_id=device_id,
            )
            if existing_count >= _MAX_RULES_PER_DEVICE:
                raise ValueError(
                    f"Rule limit reached for device {device_id}: "
                    f"{existing_count}/{_MAX_RULES_PER_DEVICE}"
                )
            result = await self._repo.create(data, created_by=created_by)
        # FIXED-P0: invalidate_cache是async方法，必须await；原代码缺少await导致协程从未执行，规则热更新失效
        try:
            from edgelite.app import _app_state

            if _app_state.evaluator:
                await _app_state.evaluator.invalidate_cache()
        except (ImportError, AttributeError):
            logger.debug("Evaluator cache invalidation skipped (module not available)")
        return result

    async def get_rule(self, rule_id: str) -> dict | None:
        return await self._repo.get(rule_id)

    async def list_rules_by_ids(self, rule_ids: list[str]) -> list[dict]:
        """FIXED(严重): 批量查询规则，避免 N+1 查询。"""
        return await self._repo.list_rules_by_ids(rule_ids)

    async def list_rules(
        self,
        page: int = 1,
        size: int = 20,
        device_id: str | None = None,
        search: str | None = None,
        severity: str | None = None,
        created_by: str | None = None,
    ) -> tuple[list[dict], int]:
        return await self._repo.list_all(page, size, device_id, search, severity, created_by)

    async def update_rule(self, rule_id: str, data: dict) -> dict | None:
        result = await self._repo.update(rule_id, data)
        if result:
            # FIXED-P0: 原代码缺少 try-except 保护，_app_state 不可用时会抛出异常
            try:
                from edgelite.app import _app_state

                if _app_state.evaluator:
                    # FIXED-P1: 原问题-遗漏 cleanup_duration_tracker(rule_id)
                    # 导致修改阈值后基于陈旧时间立即误触发告警
                    # 对比 delete_rule 正确调用了两个方法
                    await _app_state.evaluator.cleanup_duration_tracker(rule_id)
                    await _app_state.evaluator.invalidate_cache()
            except (ImportError, AttributeError):
                logger.debug("Evaluator cache invalidation skipped (module not available)")
        return result

    async def delete_rule(self, rule_id: str) -> bool:
        # FIX-P1: 原代码顺序为「恢复告警→删除静默→删除规则」，若规则删除失败，
        # 已恢复告警和已删静默无法回滚，导致状态不一致（规则仍在但告警已恢复、
        # 静默已删）。改为先删除规则（不可逆操作），成功后再清理关联数据，
        # 失败时直接返回不触碰关联数据，保证一致性。
        # 1. 删除前获取规则信息（用于恢复事件的 rule_name 解析）
        rule = await self._repo.get(rule_id)
        rule_name = rule.get("name", "") if rule else ""
        # R11-SVC-04: 删除前记录 device_id，删除成功后清理对应的 per-device 锁
        device_id_for_lock = rule.get("device_id") if rule else None
        # 2. 先删除规则（不可逆操作）
        result = await self._repo.delete(rule_id)
        if not result:
            return False
        # R11-SVC-04: 规则删除成功后，清理该设备的 per-device 锁，避免 _create_locks 无限增长
        if device_id_for_lock is not None:
            async with self._create_locks_meta:
                self._create_locks.pop(device_id_for_lock, None)
        # 3. 规则删除成功后，清理关联数据（恢复告警、删除静默、失效缓存）
        # 批量恢复活跃告警并发布恢复事件
        try:
            from edgelite.app import _app_state
            from edgelite.engine.event_bus import AlarmEvent

            alarm_repo = _app_state._repos.get("alarm") if hasattr(_app_state, "_repos") else None
            event_bus = getattr(_app_state, "event_bus", None)
            if alarm_repo is not None:
                recovered = await alarm_repo.recover_active_by_rule(rule_id)
                if recovered:
                    # 解析设备名称（同一规则的告警共享同一 device_id）
                    device_name = ""
                    first_device_id = recovered[0].get("device_id") or ""
                    if first_device_id:
                        try:
                            device = await self._device_repo.get(first_device_id)
                            device_name = device.get("name", "") if device else ""
                        except Exception as e:
                            logger.debug("Device name lookup failed: %s", e)
                    for alarm in recovered:
                        event = AlarmEvent(
                            alarm_id=alarm.get("alarm_id", ""),
                            rule_id=rule_id,
                            rule_name=rule_name,
                            device_id=alarm.get("device_id", ""),
                            device_name=device_name,
                            severity=alarm.get("severity", "info"),
                            action="recovered",
                        )
                        if event_bus:
                            try:
                                await event_bus.publish(event)
                            except Exception as e:
                                logger.warning(
                                    "Publish recovery event failed for alarm %s: %s",
                                    alarm.get("alarm_id"), e,
                                )
        except (ImportError, AttributeError):
            logger.debug("Orphan alarm cleanup skipped (module not available)")
        except Exception as e:
            logger.warning("Orphan alarm cleanup failed for rule %s: %s", rule_id, e)
        # R7-S-06 修复(严重): 删除规则后清理关联的 alarm_silences，避免孤儿静默记录残留
        # 导致后续告警被错误静默（规则已删除但静默仍生效）
        try:
            from edgelite.services.alarm_silence import get_alarm_silence_manager

            silence_mgr = get_alarm_silence_manager()
            deleted_count = silence_mgr.delete_silences_by_rule(rule_id)
            if deleted_count:
                logger.info("Cleaned up %d alarm silences for deleted rule %s", deleted_count, rule_id)
        except Exception as e:
            logger.warning("Alarm silence cleanup failed for rule %s: %s", rule_id, e)
        # 4. 失效 evaluator 缓存
        # FIXED-P0: 原代码缺少 try-except 保护
        try:
            from edgelite.app import _app_state

            if _app_state.evaluator:
                await _app_state.evaluator.cleanup_duration_tracker(rule_id)
                await _app_state.evaluator.invalidate_cache()
        except (ImportError, AttributeError):
            logger.debug("Evaluator cache invalidation skipped (module not available)")
        return result

    async def enable_rule(self, rule_id: str) -> dict | None:
        result = await self._repo.toggle(rule_id, True)
        if result:
            # FIXED-P0: 原代码缺少 try-except 保护
            try:
                from edgelite.app import _app_state

                if _app_state.evaluator:
                    # FIXED-Bug9: 防御性清理，避免禁用期间残留的 duration_tracker 导致重新启用后误触发
                    await _app_state.evaluator.cleanup_duration_tracker(rule_id)
                    await _app_state.evaluator.invalidate_cache()
            except (ImportError, AttributeError):
                logger.debug("Evaluator cache invalidation skipped (module not available)")
        return result

    async def disable_rule(self, rule_id: str) -> dict | None:
        result = await self._repo.toggle(rule_id, False)
        if result:
            # FIXED-P0: 原代码缺少 try-except 保护
            try:
                from edgelite.app import _app_state

                if _app_state.evaluator:
                    # FIXED-Bug9: 与 update_rule/delete_rule 保持一致，清理 duration_tracker
                    # 否则重新启用时会基于陈旧 first_match_time 立即误触发告警
                    await _app_state.evaluator.cleanup_duration_tracker(rule_id)
                    await _app_state.evaluator.invalidate_cache()
            except (ImportError, AttributeError):
                logger.debug("Evaluator cache invalidation skipped (module not available)")
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

        # FIXED-Bug8: NOT 语义为 not all(matched)，之前误用 any(matched)
        if logic == "NOT":
            all_matched = not all(r["matched"] for r in results)
        elif logic == "AND":
            all_matched = all(r["matched"] for r in results)
        else:
            all_matched = any(r["matched"] for r in results)

        return {
            "rule_id": rule_id,
            "logic": logic,
            "condition_results": results,
            "all_matched": all_matched,
        }

    async def evaluate_rule(self, rule_id: str, point_values: dict[str, float] | None = None) -> dict:
        """评估规则（支持AI推理、死区、持续时间等高级特性）"""
        rule = await self._repo.get(rule_id)
        if rule is None:
            raise ValueError(f"Rule not found: {rule_id}")

        conditions = rule.get("conditions") or []
        logic = rule.get("logic", "AND")
        priority = rule.get("priority", 0)
        duration_seconds = rule.get("duration_seconds", 0)

        if not conditions:
            return {
                "rule_id": rule_id,
                "logic": logic,
                "priority": priority,
                "condition_results": [],
                "all_matched": False,
            }

        # 获取评估器实例以使用完整条件检查
        evaluator = None
        try:
            from edgelite.app import _app_state

            evaluator = getattr(_app_state, "evaluator", None)
        except (ImportError, AttributeError):
            pass

        if evaluator and point_values:
            matched = await evaluator._check_conditions(
                conditions, point_values, logic,
                rule.get("device_id", ""), rule_id,
            )
            return {
                "rule_id": rule_id,
                "logic": logic,
                "priority": priority,
                "duration_seconds": duration_seconds,
                "all_matched": matched,
            }

        # 回退到简单评估
        results = []
        for cond in conditions:
            point = cond.get("point")
            operator = cond.get("operator")
            threshold = cond.get("threshold")
            if point is None or operator is None or threshold is None:
                results.append({"condition": cond, "matched": False, "actual_value": None})
                continue
            value = (point_values or {}).get(point)

            if value is None:
                results.append({"condition": cond, "matched": False, "actual_value": None})
                continue

            matched = self._compare(value, operator, threshold)
            results.append({"condition": cond, "matched": matched, "actual_value": value})

        # FIXED-Bug8: NOT 语义为 not all(matched)，之前误用 any(matched)
        if logic == "NOT":
            all_matched = not all(r["matched"] for r in results)
        elif logic == "AND":
            all_matched = all(r["matched"] for r in results)
        else:
            all_matched = any(r["matched"] for r in results)

        return {
            "rule_id": rule_id,
            "logic": logic,
            "priority": priority,
            "duration_seconds": duration_seconds,
            "condition_results": results,
            "all_matched": all_matched,
        }

    @staticmethod
    def _compare(value: float, operator: str, threshold: float) -> bool:
        # FIXED-P1: 原代码未处理 threshold/value 为字符串的情况，从数据库加载的值可能是字符串
        # 导致 TypeError: '>' not supported between instances of 'str' and 'str'
        try:
            value = float(value)
            threshold = float(threshold)
        except (TypeError, ValueError):
            logger.warning("Invalid comparison types: value=%r, threshold=%r", value, threshold)
            return False

        ops = {
            ">": value > threshold,
            ">=": value >= threshold,
            "<": value < threshold,
            "<=": value <= threshold,
            "=": abs(value - threshold) < 1e-9,
            "==": abs(value - threshold) < 1e-9,
            "!=": abs(value - threshold) >= 1e-9,
        }
        return ops.get(operator, False)
