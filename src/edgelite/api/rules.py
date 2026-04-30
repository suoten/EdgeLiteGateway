"""规则管理API路由"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from edgelite.models.rule import RuleCreate, RuleUpdate, RuleResponse, RuleTestRequest
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.api.deps import CurrentUser, require_permission
from edgelite.security.rbac import Permission

router = APIRouter(prefix="/api/v1/rules", tags=["规则管理"])


def _get_rule_service():
    from edgelite.app import _app_state
    return _app_state.rule_service


@router.get("", response_model=PagedResponse[RuleResponse])
async def list_rules(
    user: CurrentUser = require_permission(Permission.RULE_READ),
    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=1000),
    device_id: str | None = None,
    search: str | None = None,
    severity: str | None = None,
):
    svc = _get_rule_service()
    rules, total = await svc.list_rules(page, size, device_id)
    if search:
        search_lower = search.lower()
        rules = [r for r in rules if search_lower in r.get("name", "").lower() or search_lower in r.get("rule_id", "").lower()]
    if severity:
        rules = [r for r in rules if r.get("severity", "").lower() == severity.lower()]
    return PagedResponse(data=rules, total=total, page=page, size=size)


@router.post("", response_model=ApiResponse[RuleResponse], status_code=201)
async def create_rule(body: RuleCreate, user: CurrentUser = require_permission(Permission.RULE_CREATE)):
    svc = _get_rule_service()
    try:
        rule = await svc.create_rule(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ApiResponse(data=rule)


@router.get("/{rule_id}", response_model=ApiResponse[RuleResponse])
async def get_rule(rule_id: str, user: CurrentUser = require_permission(Permission.RULE_READ)):
    svc = _get_rule_service()
    rule = await svc.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return ApiResponse(data=rule)


@router.put("/{rule_id}", response_model=ApiResponse[RuleResponse])
async def update_rule(rule_id: str, body: RuleUpdate, user: CurrentUser = require_permission(Permission.RULE_UPDATE)):
    svc = _get_rule_service()
    data = body.model_dump(exclude_none=True)
    rule = await svc.update_rule(rule_id, data)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return ApiResponse(data=rule)


@router.delete("/{rule_id}", response_model=ApiResponse)
async def delete_rule(rule_id: str, user: CurrentUser = require_permission(Permission.RULE_DELETE)):
    svc = _get_rule_service()
    success = await svc.delete_rule(rule_id)
    if not success:
        raise HTTPException(status_code=404, detail="规则不存在")
    return ApiResponse()


@router.post("/{rule_id}/enable", response_model=ApiResponse[RuleResponse])
async def enable_rule(rule_id: str, user: CurrentUser = require_permission(Permission.RULE_TOGGLE)):
    svc = _get_rule_service()
    rule = await svc.enable_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return ApiResponse(data=rule)


@router.post("/{rule_id}/disable", response_model=ApiResponse[RuleResponse])
async def disable_rule(rule_id: str, user: CurrentUser = require_permission(Permission.RULE_TOGGLE)):
    svc = _get_rule_service()
    rule = await svc.disable_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return ApiResponse(data=rule)


@router.post("/{rule_id}/test", response_model=ApiResponse)
async def test_rule(rule_id: str, body: RuleTestRequest, user: CurrentUser = require_permission(Permission.RULE_READ)):
    svc = _get_rule_service()
    try:
        result = await svc.test_rule(rule_id, body.point_values)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return ApiResponse(data=result)
