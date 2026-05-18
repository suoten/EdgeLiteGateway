"""规则管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from edgelite.api.deps import CurrentUser, PaginationDep, RuleServiceDep, require_permission
from edgelite.api.error_codes import RuleErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.rule import RuleCreate, RuleResponse, RuleTestRequest, RuleUpdate
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rules", tags=["Rules"])


@router.get("", response_model=PagedResponse[RuleResponse])
async def list_rules(
    svc: RuleServiceDep,
    user: CurrentUser = require_permission(Permission.RULE_READ),
    pagination: PaginationDep = None,  # FIXED: 原问题-硬编码分页参数，未使用公共PaginationParams模型
    device_id: str | None = None,
    search: str | None = None,
    severity: str | None = None,
):
    try:
        rules, total = await svc.list_rules(pagination.page, pagination.size, device_id, search, severity)
        return PagedResponse(data=rules, total=total, page=pagination.page, size=pagination.size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_rules failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.LIST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("", response_model=ApiResponse[RuleResponse], status_code=201)
async def create_rule(
    body: RuleCreate,
    svc: RuleServiceDep,
    user: CurrentUser = require_permission(Permission.RULE_CREATE),
):
    try:
        rule = await svc.create_rule(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("create_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.CREATE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code
    return ApiResponse(data=rule)


@router.get("/{rule_id}", response_model=ApiResponse[RuleResponse])
async def get_rule(
    rule_id: str,
    svc: RuleServiceDep,
    user: CurrentUser = require_permission(Permission.RULE_READ),
):
    try:
        rule = await svc.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.GET_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.put("/{rule_id}", response_model=ApiResponse[RuleResponse])
async def update_rule(
    rule_id: str,
    body: RuleUpdate,
    svc: RuleServiceDep,
    user: CurrentUser = require_permission(Permission.RULE_UPDATE),
):
    try:
        data = body.model_dump(exclude_none=True)
        rule = await svc.update_rule(rule_id, data)
        if rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.UPDATE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.delete("/{rule_id}", response_model=ApiResponse)
async def delete_rule(
    rule_id: str,
    svc: RuleServiceDep,
    user: CurrentUser = require_permission(Permission.RULE_DELETE),
):
    try:
        success = await svc.delete_rule(rule_id)
        if not success:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.DELETE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/{rule_id}/enable", response_model=ApiResponse[RuleResponse])
async def enable_rule(
    rule_id: str,
    svc: RuleServiceDep,
    user: CurrentUser = require_permission(Permission.RULE_TOGGLE),
):
    try:
        rule = await svc.enable_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("enable_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.ENABLE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/{rule_id}/disable", response_model=ApiResponse[RuleResponse])
async def disable_rule(
    rule_id: str,
    svc: RuleServiceDep,
    user: CurrentUser = require_permission(Permission.RULE_TOGGLE),
):
    try:
        rule = await svc.disable_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=RuleErrors.NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("disable_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.DISABLE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/{rule_id}/test", response_model=ApiResponse)
async def test_rule(
    rule_id: str,
    body: RuleTestRequest,
    svc: RuleServiceDep,
    user: CurrentUser = require_permission(Permission.RULE_READ),
):
    try:
        result = await svc.test_rule(rule_id, body.point_values)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error("test_rule failed: %s", e)
        raise HTTPException(status_code=500, detail=RuleErrors.TEST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code
    return ApiResponse(data=result)
