"""规则管理API路由"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.rule import RuleCreate, RuleResponse, RuleTestRequest, RuleUpdate
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rules", tags=["规则管理"])


def _get_rule_service():
    from edgelite.app import _app_state

    return _app_state.rule_service


@router.get("", response_model=PagedResponse[RuleResponse])
async def list_rules(
    user: CurrentUser = require_permission(Permission.RULE_READ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=1000),
    device_id: str | None = None,
    search: str | None = None,
    severity: str | None = None,
):
    try:
        svc = _get_rule_service()
        rules, total = await svc.list_rules(page, size, device_id, search, severity)
        return PagedResponse(data=rules, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取规则列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取规则列表失败") from e


@router.post("", response_model=ApiResponse[RuleResponse], status_code=201)
async def create_rule(
    body: RuleCreate, user: CurrentUser = require_permission(Permission.RULE_CREATE)
):
    svc = _get_rule_service()
    try:
        rule = await svc.create_rule(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("创建规则失败: %s", e)
        raise HTTPException(status_code=500, detail="创建规则失败") from e
    return ApiResponse(data=rule)


@router.get("/{rule_id}", response_model=ApiResponse[RuleResponse])
async def get_rule(rule_id: str, user: CurrentUser = require_permission(Permission.RULE_READ)):
    try:
        svc = _get_rule_service()
        rule = await svc.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取规则详情失败: %s", e)
        raise HTTPException(status_code=500, detail="获取规则详情失败") from e


@router.put("/{rule_id}", response_model=ApiResponse[RuleResponse])
async def update_rule(
    rule_id: str, body: RuleUpdate, user: CurrentUser = require_permission(Permission.RULE_UPDATE)
):
    try:
        svc = _get_rule_service()
        data = body.model_dump(exclude_none=True)
        rule = await svc.update_rule(rule_id, data)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新规则失败: %s", e)
        raise HTTPException(status_code=500, detail="更新规则失败") from e


@router.delete("/{rule_id}", response_model=ApiResponse)
async def delete_rule(rule_id: str, user: CurrentUser = require_permission(Permission.RULE_DELETE)):
    try:
        svc = _get_rule_service()
        success = await svc.delete_rule(rule_id)
        if not success:
            raise HTTPException(status_code=404, detail="规则不存在")
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("删除规则失败: %s", e)
        raise HTTPException(status_code=500, detail="删除规则失败") from e


@router.post("/{rule_id}/enable", response_model=ApiResponse[RuleResponse])
async def enable_rule(rule_id: str, user: CurrentUser = require_permission(Permission.RULE_TOGGLE)):
    try:
        svc = _get_rule_service()
        rule = await svc.enable_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("启用规则失败: %s", e)
        raise HTTPException(status_code=500, detail="启用规则失败") from e


@router.post("/{rule_id}/disable", response_model=ApiResponse[RuleResponse])
async def disable_rule(
    rule_id: str, user: CurrentUser = require_permission(Permission.RULE_TOGGLE)
):
    try:
        svc = _get_rule_service()
        rule = await svc.disable_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        return ApiResponse(data=rule)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("禁用规则失败: %s", e)
        raise HTTPException(status_code=500, detail="禁用规则失败") from e


@router.post("/{rule_id}/test", response_model=ApiResponse)
async def test_rule(
    rule_id: str,
    body: RuleTestRequest,
    user: CurrentUser = require_permission(Permission.RULE_READ),
):
    svc = _get_rule_service()
    try:
        result = await svc.test_rule(rule_id, body.point_values)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error("测试规则失败: %s", e)
        raise HTTPException(status_code=500, detail="测试规则失败") from e
    return ApiResponse(data=result)
