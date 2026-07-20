"""规则管理API路由"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    AuditServiceDep,
    PaginationDep,
    RuleServiceDep,
    require_permission,
)
from edgelite.api.error_codes import AuthzErrors, RepoErrors, RuleErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.db import StaleDataError
from edgelite.models.rule import RuleCreate, RuleResponse, RuleTestRequest, RuleUpdate
from edgelite.security.rbac import Permission
from edgelite.services.audit_service import AuditAction

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rules", tags=["Rules"])


class BatchRuleIds(BaseModel):
    """批量规则ID请求模型"""

    rule_ids: list[str] = Field(..., min_length=1, max_length=100)


async def _check_rule_access(rule: dict, user) -> None:
    """Check if user can access a rule (owner or shared). Raises 403 if not."""
    if user["role"] == "admin":
        return
    if rule.get("created_by") == user["user_id"]:
        return
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    container = _app_state
    share_repo = ResourceShareRepo(container.database, container.database.write_lock)
    has_access = await share_repo.check_user_has_access("rule", rule["rule_id"], user["user_id"])
    if has_access:
        return
    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)


@router.get("", response_model=PagedResponse[RuleResponse])
async def list_rules(
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_READ)),
    pagination: PaginationDep = None,  # type: ignore[assignment]  # FIXED: FastAPI 依赖注入需要 = None 语法，但实际类型非 Optional  # noqa: E501
    device_id: str | None = None,
    search: str | None = None,
    # FIXED(一般): 枚举值未校验，恶意用户可传任意字符串绕过过滤；改为 Literal 校验
    severity: Literal["critical", "major", "warning", "minor", "info"] | None = None,
):
    try:
        created_by = None if user["role"] == "admin" else user["user_id"]
        rules, total = await svc.list_rules(
            pagination.page, pagination.size, device_id, search, severity, created_by=created_by
        )
        if user["role"] != "admin":
            from edgelite.app import _app_state
            from edgelite.storage.sqlite_repo import ResourceShareRepo

            container = _app_state
            share_repo = ResourceShareRepo(container.database, container.database.write_lock)
            shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "rule")
            if shared_ids:
                owned_ids = {r["rule_id"] for r in rules}
                missing_ids = shared_ids - owned_ids
                if missing_ids:
                    # FIXED(严重): 原问题-循环逐个调用 svc.get_rule(rid) 存在 N+1 查询；
                    # 改为批量查询 list_rules_by_ids 一次获取所有缺失规则
                    missing_rules = await svc.list_rules_by_ids(list(missing_ids))
                    rules.extend(missing_rules)
                    total += len(missing_rules)
        return PagedResponse(data=rules, total=total, page=pagination.page, size=pagination.size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_rules failed: %s", e)
        raise HTTPException(
            status_code=500, detail=RuleErrors.LIST_FAILED
        ) from e  # FIXED: 原问题：中文硬编码detail，改为error_code


@router.post("", response_model=ApiResponse[RuleResponse], status_code=201)
async def create_rule(
    body: RuleCreate,
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_CREATE)),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
    request: Request | None = None,
):
    try:
        logger.info(
            "Creating rule: name=%s, device_id=%s, user=%s", body.name, body.device_id, user.get("user_id", "?")
        )
        rule = await svc.create_rule(body.model_dump(), created_by=user["user_id"])
        logger.info("Rule created successfully: rule_id=%s", rule.get("rule_id", "?"))
        try:
            from edgelite.services.audit_service import AuditAction

            # 补充ip_address和user_agent用于审计追溯
            ip_address = request.client.host if request and request.client else None
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(
                AuditAction.RULE_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="rule",
                resource_id=rule.get("rule_id", ""),
                ip_address=ip_address,
                user_agent=user_agent,
                after_value=body.model_dump(),
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=rule)
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail={"error_code": RuleErrors.CONDITION_INVALID, "errors": [str(e)], "warnings": []}
        ) from e
    except Exception as e:
        logger.error("create_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.CREATE_FAILED) from e


@router.post("/test", response_model=ApiResponse)
async def test_rule_definition(
    body: RuleCreate,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_READ)),
):
    """测试规则定义（不持久化，不影响运行时）"""
    try:
        conditions = [c.model_dump() for c in body.conditions]
        triggered: list[dict] = []
        for cond in body.conditions:
            triggered.append(
                {"point": cond.point, "operator": cond.operator, "threshold": cond.threshold, "type": cond.type}
            )

        result = {
            "rule_name": body.name,
            "device_id": body.device_id,
            "severity": body.severity,
            "logic": body.logic,
            "conditions": conditions,
            "duration": body.duration,
            "notify_channels": body.notify_channels,
            "evaluable": True,
        }
        return ApiResponse(data=result)
    except Exception as e:
        logger.error("test_rule_definition failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.TEST_FAILED) from e


# FIXED: 批量操作端点必须在/{rule_id}动态路由之前注册，否则FastAPI将"batch"匹配为rule_id导致404
@router.post("/batch/delete", response_model=ApiResponse)
async def batch_delete_rules(
    body: BatchRuleIds,
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_DELETE)),
    audit_svc: AuditServiceDep = None,
):
    """批量删除规则"""
    try:
        # 批量查询规则用于权限校验，避免 N+1 查询
        rules_map = {r["rule_id"]: r for r in await svc.list_rules_by_ids(body.rule_ids) if r}
        success_count = 0
        failed: dict[str, str] = {}
        for rid in body.rule_ids:
            rule = rules_map.get(rid)
            if rule is None:
                failed[rid] = RuleErrors.NOT_FOUND
                continue
            try:
                await _check_rule_access(rule, user)
            except HTTPException as e:
                failed[rid] = str(e.detail)
                continue
            ok = await svc.delete_rule(rid)
            if ok:
                success_count += 1
                try:
                    await audit_svc.log(
                        AuditAction.RULE_DELETE,
                        user_id=user["user_id"],
                        username=user["username"],
                        resource_type="rule",
                        resource_id=rid,
                        status="success",
                    )
                except Exception as e:
                    logger.warning("Rule batch delete audit failed for %s: %s", rid, e)
            else:
                failed[rid] = RuleErrors.NOT_FOUND
        return ApiResponse(
            data={
                "success_count": success_count,
                "failed": failed,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("batch_delete_rules failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.DELETE_FAILED) from e


@router.post("/batch/enable", response_model=ApiResponse)
async def batch_enable_rules(
    body: BatchRuleIds,
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_TOGGLE)),
    audit_svc: AuditServiceDep = None,
):
    """批量启用规则"""
    try:
        # 批量查询规则用于权限校验，避免 N+1 查询
        rules_map = {r["rule_id"]: r for r in await svc.list_rules_by_ids(body.rule_ids) if r}
        success_count = 0
        failed: dict[str, str] = {}
        for rid in body.rule_ids:
            rule = rules_map.get(rid)
            if rule is None:
                failed[rid] = RuleErrors.NOT_FOUND
                continue
            try:
                await _check_rule_access(rule, user)
            except HTTPException as e:
                failed[rid] = str(e.detail)
                continue
            updated = await svc.enable_rule(rid)
            if updated is not None:
                success_count += 1
                try:
                    await audit_svc.log(
                        AuditAction.RULE_ENABLE,
                        user_id=user["user_id"],
                        username=user["username"],
                        resource_type="rule",
                        resource_id=rid,
                    )
                except Exception as e:
                    logger.warning("Rule batch enable audit failed for %s: %s", rid, e)
            else:
                failed[rid] = RuleErrors.NOT_FOUND
        return ApiResponse(
            data={
                "success_count": success_count,
                "failed": failed,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("batch_enable_rules failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.ENABLE_FAILED) from e


@router.post("/batch/disable", response_model=ApiResponse)
async def batch_disable_rules(
    body: BatchRuleIds,
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_TOGGLE)),
    audit_svc: AuditServiceDep = None,
):
    """批量禁用规则"""
    try:
        # 批量查询规则用于权限校验，避免 N+1 查询
        rules_map = {r["rule_id"]: r for r in await svc.list_rules_by_ids(body.rule_ids) if r}
        success_count = 0
        failed: dict[str, str] = {}
        for rid in body.rule_ids:
            rule = rules_map.get(rid)
            if rule is None:
                failed[rid] = RuleErrors.NOT_FOUND
                continue
            try:
                await _check_rule_access(rule, user)
            except HTTPException as e:
                failed[rid] = str(e.detail)
                continue
            updated = await svc.disable_rule(rid)
            if updated is not None:
                success_count += 1
                try:
                    await audit_svc.log(
                        AuditAction.RULE_DISABLE,
                        user_id=user["user_id"],
                        username=user["username"],
                        resource_type="rule",
                        resource_id=rid,
                    )
                except Exception as e:
                    logger.warning("Rule batch disable audit failed for %s: %s", rid, e)
            else:
                failed[rid] = RuleErrors.NOT_FOUND
        return ApiResponse(
            data={
                "success_count": success_count,
                "failed": failed,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("batch_disable_rules failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.DISABLE_FAILED) from e


@router.get("/{rule_id}", response_model=ApiResponse[RuleResponse])
async def get_rule(
    rule_id: Annotated[str, Path(max_length=128)],
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_READ)),
):
    try:
        rule = await svc.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)
        await _check_rule_access(rule, user)
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_rule failed: %s", e)
        raise HTTPException(
            status_code=500, detail=RuleErrors.GET_FAILED
        ) from e  # FIXED: 原问题：中文硬编码detail，改为error_code


@router.put("/{rule_id}", response_model=ApiResponse[RuleResponse])
async def update_rule(
    rule_id: Annotated[str, Path(max_length=128)],
    body: RuleUpdate,
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_UPDATE)),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
    request: Request | None = None,
):
    try:
        data = body.model_dump(exclude_none=True)
        before_rule = await svc.get_rule(rule_id)
        if before_rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)
        await _check_rule_access(before_rule, user)
        # FIXED-P2: 乐观锁-将当前version注入更新数据，防止并发更新丢失
        if isinstance(before_rule, dict) and "version" in before_rule:
            data["_version"] = before_rule["version"]

        rule = await svc.update_rule(rule_id, data)
        if rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)
        try:
            from edgelite.services.audit_service import AuditAction

            # 补充ip_address和user_agent用于审计追溯
            ip_address = request.client.host if request and request.client else None
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(
                AuditAction.RULE_UPDATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="rule",
                resource_id=rule_id,
                ip_address=ip_address,
                user_agent=user_agent,
                before_value=before_rule,
                after_value=data,
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except StaleDataError as e:
        logger.warning("StaleDataError in update_rule: %s", e)
        raise HTTPException(status_code=409, detail=RepoErrors.STALE_DATA_ERROR) from e
    except Exception as e:
        logger.error("update_rule failed: %s", e)
        raise HTTPException(
            status_code=500, detail=RuleErrors.UPDATE_FAILED
        ) from e  # FIXED: 原问题：中文硬编码detail，改为error_code


@router.delete("/{rule_id}", response_model=ApiResponse)
async def delete_rule(
    rule_id: Annotated[str, Path(max_length=128)],
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_DELETE)),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
    request: Request | None = None,
):
    try:
        before_rule = await svc.get_rule(rule_id)
        if before_rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)
        await _check_rule_access(before_rule, user)

        # 先审计后业务：先写审计日志（status=pending），审计失败则不执行删除（fail-safe）
        try:
            from edgelite.services.audit_service import AuditAction

            # 补充ip_address和user_agent用于审计追溯
            ip_address = request.client.host if request and request.client else None
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(
                AuditAction.RULE_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="rule",
                resource_id=rule_id,
                ip_address=ip_address,
                user_agent=user_agent,
                before_value=before_rule,
                status="pending",
            )
        except Exception as e:
            logger.error("审计日志写入失败(pending)，删除操作已中止: %s", e)
            raise HTTPException(status_code=500, detail=RuleErrors.DELETE_FAILED) from e  # FIXED-B904

        success = await svc.delete_rule(rule_id)
        if not success:
            # 删除失败，记录审计状态为 failed
            try:
                await audit_svc.log(
                    AuditAction.RULE_DELETE,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="rule",
                    resource_id=rule_id,
                    status="failed",
                    error_message="rule not found or delete failed",
                )
            except Exception as e:
                logger.warning("审计日志写入失败(failed): %s", e)
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)
        # 删除成功，记录审计状态为 success
        try:
            await audit_svc.log(
                AuditAction.RULE_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="rule",
                resource_id=rule_id,
                status="success",
            )
        except Exception as e:
            logger.warning("审计日志写入失败(success): %s", e)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_rule failed: %s", e)
        raise HTTPException(
            status_code=500, detail=RuleErrors.DELETE_FAILED
        ) from e  # FIXED: 原问题：中文硬编码detail，改为error_code


@router.post("/{rule_id}/enable", response_model=ApiResponse[RuleResponse])
async def enable_rule(
    rule_id: Annotated[str, Path(max_length=128)],
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_TOGGLE)),
    audit_svc: AuditServiceDep = None,
):
    try:
        existing = await svc.get_rule(rule_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)
        await _check_rule_access(existing, user)
        rule = await svc.enable_rule(rule_id)
        if rule is None:
            raise HTTPException(
                status_code=404, detail=RuleErrors.NOT_FOUND
            )  # FIXED: 原问题：中文硬编码detail，改为error_code
        # SEC-FIX-V10: 规则启用审计日志（原缺失，攻击者可静默启用规则）
        try:
            await audit_svc.log(
                AuditAction.RULE_ENABLE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="rule",
                resource_id=rule_id,
            )
        except Exception as e:
            logger.warning("Rule enable audit failed: %s", e)
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("enable_rule failed: %s", e)
        raise HTTPException(
            status_code=500, detail=RuleErrors.ENABLE_FAILED
        ) from e  # FIXED: 原问题：中文硬编码detail，改为error_code


@router.post("/{rule_id}/disable", response_model=ApiResponse[RuleResponse])
async def disable_rule(
    rule_id: Annotated[str, Path(max_length=128)],
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_TOGGLE)),
    audit_svc: AuditServiceDep = None,
):
    try:
        rule = await svc.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)
        await _check_rule_access(rule, user)
        rule = await svc.disable_rule(rule_id)
        if rule is None:
            raise HTTPException(
                status_code=404, detail=RuleErrors.NOT_FOUND
            )  # FIXED: 原问题：中文硬编码detail，改为error_code
        # SEC-FIX-V10: 规则禁用审计日志（原缺失，攻击者可静默禁用告警）
        try:
            await audit_svc.log(
                AuditAction.RULE_DISABLE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="rule",
                resource_id=rule_id,
            )
        except Exception as e:
            logger.warning("Rule disable audit failed: %s", e)
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("disable_rule failed: %s", e)
        raise HTTPException(
            status_code=500, detail=RuleErrors.DISABLE_FAILED
        ) from e  # FIXED: 原问题：中文硬编码detail，改为error_code


@router.post("/{rule_id}/test", response_model=ApiResponse)
async def test_rule(
    rule_id: Annotated[str, Path(max_length=128)],
    body: RuleTestRequest,
    svc: RuleServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_READ)),
):
    try:
        rule = await svc.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)
        await _check_rule_access(rule, user)
        result = await svc.test_rule(rule_id, body.point_values)
    except HTTPException:
        raise
    except ValueError as e:
        # FIXED-P2: 原问题-ValueError返回404 NOT_FOUND语义错误，ValueError表示测试输入无效而非规则不存在
        raise HTTPException(
            status_code=422, detail={"error_code": RuleErrors.CONDITION_INVALID, "errors": [str(e)], "warnings": []}
        ) from e
    except Exception as e:
        logger.error("test_rule failed: %s", e)
        raise HTTPException(
            status_code=500, detail=RuleErrors.TEST_FAILED
        ) from e  # FIXED: 原问题：中文硬编码detail，改为error_code
    return ApiResponse(data=result)
