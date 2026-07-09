"""告警管理API路由"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    AlarmServiceDep,
    AuditServiceDep,
    PaginationDep,
    require_permission,
)
from edgelite.api.error_codes import AlarmErrors, AuthzErrors, CommonErrors, RepoErrors
from edgelite.models.alarm import AlarmResponse
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.models.db import StaleDataError
from edgelite.security.rbac import Permission
from edgelite.services.alarm_service import AlarmSuppressionRule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alarms", tags=["Alarms"])


async def _check_alarm_device_access(device_id: str | None, user) -> None:
    """Check if user can access a device (owner or shared). Raises 403 if not."""
    if user["role"] == "admin" or not device_id:
        return
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    device_svc = _app_state.device_service
    device = await device_svc.get_device(device_id)
    if device and device.get("created_by") == user["user_id"]:
        return
    container = _app_state
    share_repo = ResourceShareRepo(container.database, container.database.write_lock)
    has_access = await share_repo.check_user_has_access("device", device_id, user["user_id"])
    if has_access:
        return
    raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)


async def _get_accessible_device_ids_for_alarms(user) -> set[str] | None:
    """Get device IDs accessible by user (owned + shared), None for admin."""
    if user["role"] == "admin":
        return None
    from edgelite.app import _app_state
    from edgelite.storage.sqlite_repo import ResourceShareRepo

    device_svc = _app_state.device_service
    owned_ids = set(await device_svc.list_device_ids_by_owner(user["user_id"]))
    container = _app_state
    share_repo = ResourceShareRepo(container.database, container.database.write_lock)
    shared_ids = await share_repo.get_shared_resource_ids(user["user_id"], "device")
    return owned_ids | shared_ids


def _parse_silence_end_time(end_time_str) -> datetime | None:
    """SEC-FIX(R7-S-08): 解析静默记录的 end_time ISO 字符串，用于过期状态过滤。"""
    if not end_time_str:
        return None
    try:
        dt = datetime.fromisoformat(str(end_time_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


@router.get("/statistics", response_model=ApiResponse)
async def get_alarm_statistics(
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_READ)),
    days: int = Query(1, ge=1, le=365),  # FIXED-P1: 告警统计添加days参数支持
):
    try:
        owned_device_ids = None
        if user["role"] != "admin":
            owned_device_ids = await _get_accessible_device_ids_for_alarms(user)
            if not owned_device_ids:
                return ApiResponse(data={"summary": {}, "trend": []})
        stats = await svc.get_statistics_summary(device_ids=owned_device_ids)
        trend = await svc.get_trend(hours=days * 24, device_ids=owned_device_ids)
        # 修复7: 添加 Top10 报警设备/规则排名
        top_data = await svc.get_top_alarms(hours=days * 24, device_ids=owned_device_ids, limit=10)
        return ApiResponse(data={
            "summary": stats,
            "trend": trend,
            "top_devices": top_data.get("top_devices", []),
            "top_rules": top_data.get("top_rules", []),
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_alarm_statistics failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.LIST_FAILED) from e


@router.get("/trend", response_model=ApiResponse)
async def get_alarm_trend(
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_READ)),
    hours: int = Query(24, ge=1, le=720),
):
    try:
        owned_device_ids = None
        if user["role"] != "admin":
            owned_device_ids = await _get_accessible_device_ids_for_alarms(user)
            if not owned_device_ids:
                return ApiResponse(data=[])
        data = await svc.get_trend(hours, device_ids=owned_device_ids) or []
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_alarm_trend failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.LIST_FAILED) from e


@router.get("", response_model=PagedResponse[AlarmResponse])
async def list_alarms(
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_READ)),
    pagination: PaginationDep = None,  # FIXED: 原问题-默认值None导致类型检查误判，但Python语法要求有默认值（前参有默认值）
    # FIXED(一般): 枚举值未校验，恶意用户可传任意字符串绕过过滤；改为 Literal 校验
    status: Literal["firing", "acknowledged", "recovered"] | None = None,
    severity: Literal["critical", "major", "warning", "minor", "info"] | None = None,
    device_id: str | None = None,
    search: str | None = None,
):
    try:
        owned_device_ids = None
        if user["role"] != "admin":
            owned_device_ids = await _get_accessible_device_ids_for_alarms(user)
            if device_id and device_id not in owned_device_ids:
                raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
        alarms, total = await svc.list_alarms(pagination.page, pagination.size, status, severity, device_id, search, device_ids=owned_device_ids)
        return PagedResponse(data=alarms, total=total, page=pagination.page, size=pagination.size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_alarms failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.LIST_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/silence", response_model=PagedResponse)
async def list_alarm_silences(
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_READ)),
    pagination: PaginationDep = None,
    device_id: str = Query("", description="Filter by device ID"),
    rule_id: str = Query("", description="Filter by rule ID"),
    status: Literal["active", "expired", "cancelled"] | None = Query(
        None, description="Filter by status: active, expired, cancelled"
    ),
):
    """Query alarm silence list"""
    try:
        from edgelite.services.alarm_silence import get_alarm_silence_manager

        manager = get_alarm_silence_manager()
        # SEC-FIX(R7-S-08): 原问题-status=expired/cancelled 时 active_only=False 返回全部，过滤失效;
        # 修复-按 status 三态过滤：active→仅活跃，expired→仅过期，cancelled→仅已取消
        active_only = status == "active"
        silences = manager.list_silences(
            device_id=device_id,
            rule_id=rule_id,
            active_only=active_only,
        )
        # 对 expired/cancelled 状态进行二次过滤
        if status == "expired":
            now = datetime.now(UTC)
            silences = [
                s for s in silences
                if _parse_silence_end_time(s.get("end_time")) is not None
                and _parse_silence_end_time(s.get("end_time")) < now
            ]
        elif status == "cancelled":
            silences = [s for s in silences if s.get("cancelled_at") is not None]
        if user["role"] != "admin":
            accessible_device_ids = await _get_accessible_device_ids_for_alarms(user)
            silences = [s for s in silences if not s.get("device_id") or s.get("device_id") in accessible_device_ids]
        total = len(silences)
        start = (pagination.page - 1) * pagination.size
        end = start + pagination.size
        page_items = silences[start:end]
        return PagedResponse(data=page_items, total=total, page=pagination.page, size=pagination.size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("List alarm silences failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.LIST_FAILED) from e


@router.get("/correlation", response_model=ApiResponse)
async def get_alarm_correlations(
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_READ)),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Query alarm correlation groups"""
    try:
        from edgelite.services.alarm_correlation import get_alarm_correlation_manager

        manager = get_alarm_correlation_manager()
        groups = manager.get_groups(limit=limit, offset=offset)
        if user["role"] != "admin":
            accessible_device_ids = await _get_accessible_device_ids_for_alarms(user)
            groups = [g for g in groups if not g.get("root_device_id") or g.get("root_device_id") in accessible_device_ids]
        return ApiResponse(data={"groups": groups, "limit": limit, "offset": offset})
    except Exception as e:
        logger.error("Get alarm correlations failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.LIST_FAILED) from e


@router.get("/{alarm_id}", response_model=ApiResponse[AlarmResponse])
async def get_alarm(
    alarm_id: Annotated[str, Path(max_length=128)],
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_READ)),
):
    try:
        alarm = await svc.get_alarm(alarm_id)
        if alarm is None:
            raise HTTPException(status_code=404, detail=AlarmErrors.NOT_FOUND)
        if user["role"] != "admin" and alarm.get("device_id"):
            await _check_alarm_device_access(alarm["device_id"], user)
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_alarm failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.GET_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/history/{rule_id}", response_model=ApiResponse)
async def get_alarm_history(
    rule_id: Annotated[str, Path(max_length=128)],
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_READ)),
    days: int = Query(7, ge=1, le=90),
):
    """修复9: 查询指定规则最近 N 天的历史触发记录"""
    try:
        history = await svc.get_alarm_history(rule_id, days=days)
        # 非管理员仅返回有权限设备的记录
        if user["role"] != "admin":
            accessible_ids = await _get_accessible_device_ids_for_alarms(user)
            if accessible_ids is not None:
                history = [a for a in history if a.get("device_id") in accessible_ids]
        return ApiResponse(data=history)
    except Exception as e:
        logger.error("get_alarm_history failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.GET_FAILED) from e


@router.put("/{alarm_id}/ack", response_model=ApiResponse[AlarmResponse])
async def ack_alarm(
    alarm_id: Annotated[str, Path(max_length=128)],
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_ACK)),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
    request: Request = None,
):
    try:
        if user["role"] != "admin":
            alarm = await svc.get_alarm(alarm_id)
            if alarm and alarm.get("device_id"):
                await _check_alarm_device_access(alarm["device_id"], user)
        alarm = await svc.ack_alarm(alarm_id, user["username"])
        if alarm is None:
            raise HTTPException(status_code=404, detail=AlarmErrors.NOT_FOUND)
        # FIXED-BugR13: 告警存在但已被他人确认时返回 409，而非 404
        if alarm.get("status") == "acknowledged" and alarm.get("acknowledged_by") != user["username"]:
            raise HTTPException(status_code=409, detail=AlarmErrors.ALREADY_ACKNOWLEDGED)
        try:
            from edgelite.services.audit_service import AuditAction
            # 补充ip_address和user_agent用于审计追溯
            ip_address = request.client.host if request and request.client else None
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(AuditAction.ALARM_ACK, user_id=user["user_id"], username=user["username"], resource_type="alarm", resource_id=alarm_id, ip_address=ip_address, user_agent=user_agent)
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ack_alarm failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.ACK_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.put("/{alarm_id}/recover", response_model=ApiResponse[AlarmResponse])
async def recover_alarm(
    alarm_id: Annotated[str, Path(max_length=128)],
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_ACK)),
    audit_svc: AuditServiceDep = None,  # FIXED-M03: FastAPI dependency injection provides the value
    request: Request = None,
):
    try:
        if user["role"] != "admin":
            alarm = await svc.get_alarm(alarm_id)
            if alarm and alarm.get("device_id"):
                await _check_alarm_device_access(alarm["device_id"], user)
        alarm = await svc.clear_alarm(alarm_id)
        if alarm is None:
            raise HTTPException(status_code=404, detail=AlarmErrors.NOT_FOUND)
        # FIXED-BugR13: 告警存在但已恢复时返回 409，而非 404
        if alarm.get("status") == "recovered":
            raise HTTPException(status_code=409, detail=AlarmErrors.ALREADY_RECOVERED)
        try:
            from edgelite.services.audit_service import AuditAction
            # 补充ip_address和user_agent用于审计追溯
            ip_address = request.client.host if request and request.client else None
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(AuditAction.ALARM_ACK, user_id=user["user_id"], username=user["username"], resource_type="alarm", resource_id=alarm_id, ip_address=ip_address, user_agent=user_agent, details="recover")
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=alarm)
    except HTTPException:
        raise
    except StaleDataError as e:
        logger.warning("StaleDataError in recover_alarm: %s", e)
        raise HTTPException(status_code=409, detail=RepoErrors.STALE_DATA_ERROR) from e
    except Exception as e:
        logger.error("recover_alarm failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.ACK_FAILED) from e


@router.delete("/{alarm_id}", response_model=ApiResponse)
async def delete_alarm(
    alarm_id: Annotated[str, Path(max_length=128)],
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_DELETE)),
    audit_svc: AuditServiceDep = None,
    request: Request = None,
):
    """FIXED(严重): 物理删除告警记录（仅 admin）。

    原问题-缺少 DELETE 接口，历史告警无法清理导致无限累积。
    """
    # 补充ip_address和user_agent用于审计追溯
    ip_address = request.client.host if request and request.client else None
    user_agent = request.headers.get("User-Agent") if request else None
    # 先审计后业务：先写审计日志（status=pending），审计失败则不执行删除（fail-safe）
    try:
        from edgelite.services.audit_service import AuditAction
        await audit_svc.log(
            AuditAction.ALARM_DELETE,
            user_id=user["user_id"],
            username=user["username"],
            resource_type="alarm",
            resource_id=alarm_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details="delete",
            status="pending",
        )
    except Exception as e:
        logger.error("审计日志写入失败(pending)，删除操作已中止: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.DELETE_FAILED) from e  # FIXED-B904

    try:
        deleted = await svc.delete_alarm(alarm_id)
        if not deleted:
            # 删除失败，记录审计状态为 failed
            try:
                await audit_svc.log(
                    AuditAction.ALARM_DELETE,
                    user_id=user["user_id"],
                    username=user["username"],
                    resource_type="alarm",
                    resource_id=alarm_id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details="delete",
                    status="failed",
                    error_message="alarm not found or delete failed",
                )
            except Exception as e:
                logger.warning("审计日志写入失败(failed): %s", e)
            raise HTTPException(status_code=404, detail=AlarmErrors.NOT_FOUND)
        # 删除成功，记录审计状态为 success
        try:
            await audit_svc.log(
                AuditAction.ALARM_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="alarm",
                resource_id=alarm_id,
                ip_address=ip_address,
                user_agent=user_agent,
                details="delete",
                status="success",
            )
        except Exception as e:
            logger.warning("审计日志写入失败(success): %s", e)
        return ApiResponse(data={"alarm_id": alarm_id, "deleted": True})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_alarm failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.DELETE_FAILED) from e


class SuppressRequest(BaseModel):
    duration_seconds: int = Field(default=3600, ge=60, le=86400, description="Suppress duration in seconds")
    reason: str = Field(default="", description="Reason for suppression")
    tag_match: dict = Field(default_factory=dict, description="Tag key-value pairs to match for suppression")


@router.post("/{alarm_id}/suppress", response_model=ApiResponse)
async def suppress_alarm(
    alarm_id: Annotated[str, Path(max_length=128)],
    req: SuppressRequest,
    svc: AlarmServiceDep,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_ACK)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    try:
        if user["role"] != "admin":
            alarm = await svc.get_alarm(alarm_id)
            if alarm and alarm.get("device_id"):
                await _check_alarm_device_access(alarm["device_id"], user)
        # FIXED-P2: 原问题-先append抑制规则再检查告警是否存在，导致不存在的告警也被添加抑制规则
        # 现改为先检查告警存在性，再创建抑制规则
        alarm = await svc.get_alarm(alarm_id)
        if alarm is None:
            raise HTTPException(status_code=404, detail=AlarmErrors.NOT_FOUND)
        # FIXED(致命): 原问题-device_ids/rule_ids/severities 全部为空列表，
        # 根据 _is_suppressed 逻辑（空列表表示"不限制"），该规则会在 duration_seconds
        # 内抑制所有设备的所有告警，而非仅抑制指定 alarm_id 的告警。
        # 修复：使用告警自身的 device_id/rule_id/severity 进行精准抑制。
        _alarm_device_id = alarm.get("device_id") or ""
        _alarm_rule_id = alarm.get("rule_id") or ""
        _alarm_severity = alarm.get("severity") or ""
        rule = AlarmSuppressionRule(
            rule_id=f"suppress_{alarm_id}",
            name=f"Suppress alarm {alarm_id}",
            device_ids=[_alarm_device_id] if _alarm_device_id else [],
            rule_ids=[_alarm_rule_id] if _alarm_rule_id else [],
            severities=[_alarm_severity] if _alarm_severity else [],
            time_range_start="",
            time_range_end="",
            enabled=True,
            expires_at=datetime.now(UTC) + timedelta(seconds=req.duration_seconds or 3600),
        )
        rules = getattr(svc, "_suppression_rules", None)
        if rules is None:
            raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY)
        rules.append(rule)
        # FIXED-P0: 清理已过期的抑制规则，防止无限累积
        now = datetime.now(UTC)
        rules[:] = [r for r in rules if r.expires_at is None or r.expires_at > now]
        # 第四轮修复: 审计日志记录告警抑制操作
        try:
            from edgelite.api.auth import _get_client_ip
            from edgelite.services.audit_service import AuditAction
            client_ip = _get_client_ip(request) if request else ""
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(
                AuditAction.ALARM_SUPPRESS,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="alarm",
                resource_id=alarm_id,
                ip_address=client_ip,
                user_agent=user_agent,
                details={
                    "alarm_id": alarm_id,
                    "device_id": _alarm_device_id,
                    "duration_seconds": req.duration_seconds,
                    "reason": req.reason,
                },
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data={"alarm_id": alarm_id, "suppressed": True, "duration_seconds": req.duration_seconds})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("suppress_alarm failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.ACK_FAILED) from e


# --- Alarm Silence Period Routes ---


class SilenceCreateRequest(BaseModel):
    device_id: str = Field(default="", description="Device ID to silence (empty for all)")
    rule_id: str = Field(default="", description="Rule ID to silence (empty for all)")
    start_time: str = Field(default="", description="Silence start time (ISO format, empty for now)")
    end_time: str = Field(default="", description="Silence end time (ISO format, empty for 1h from now)")
    reason: str = Field(default="", description="Reason for silencing")


@router.post("/silence", response_model=ApiResponse)
async def create_alarm_silence(
    req: SilenceCreateRequest,
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_ACK)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """Set an alarm silence period"""
    try:
        # FIXED-P1: 非admin用户不允许创建全局静默规则（空device_id），且需校验指定设备的访问权限
        if user["role"] != "admin":
            if not req.device_id:
                raise HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED)
            await _check_alarm_device_access(req.device_id, user)
        from edgelite.services.alarm_silence import get_alarm_silence_manager

        manager = get_alarm_silence_manager()
        result = manager.create_silence(
            device_id=req.device_id,
            rule_id=req.rule_id,
            start_time=req.start_time,
            end_time=req.end_time,
            reason=req.reason,
            operator=user.get("username", "system"),
        )
        # 第四轮修复: 审计日志记录告警静默创建
        try:
            from edgelite.api.auth import _get_client_ip
            from edgelite.services.audit_service import AuditAction
            client_ip = _get_client_ip(request) if request else ""
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(
                AuditAction.ALARM_SILENCE_CREATE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="alarm_silence",
                resource_id=result.get("id") if isinstance(result, dict) else None,
                ip_address=client_ip,
                user_agent=user_agent,
                details={
                    "device_id": req.device_id,
                    "rule_id": req.rule_id,
                    "reason": req.reason,
                    "start_time": req.start_time,
                    "end_time": req.end_time,
                },
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("Create alarm silence validation error: %s", e)
        raise HTTPException(status_code=422, detail=CommonErrors.VALIDATION_FAILED) from e
    except Exception as e:
        logger.error("Create alarm silence failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.SILENCE_CREATE_FAILED) from e


@router.delete("/silence/{silence_id}", response_model=ApiResponse)
async def delete_alarm_silence(
    silence_id: Annotated[str, Path(max_length=128)],
    user: dict[str, str] = Depends(require_permission(Permission.ALARM_ACK)),
    request: Request = None,
    audit_svc: AuditServiceDep = None,
):
    """Cancel an alarm silence period"""
    try:
        from edgelite.services.alarm_silence import get_alarm_silence_manager

        manager = get_alarm_silence_manager()
        # 第四轮修复: 记录被删除的静默规则信息用于审计
        _silence_info = None
        if user["role"] != "admin":
            # R8-G-01: 改用 get_silence_by_id 直接查询，避免 list_silences 分页导致漏查
            target = manager.get_silence_by_id(silence_id)
            # FIXED(致命): 原问题-target.get("device_id")为空字符串时为 falsy，
            # 导致全局静默跳过权限检查，非 admin 可删除 admin 创建的全局静默（权限提升漏洞）
            # 修复：全局静默（device_id 为空）必须 admin 权限，与 create 逻辑对称
            if target is None:
                raise HTTPException(status_code=404, detail=AlarmErrors.SILENCE_NOT_FOUND)
            target_device_id = target.get("device_id") or ""
            if not target_device_id:
                # 全局静默只能由 admin 删除
                raise HTTPException(
                    status_code=403,
                    detail=AlarmErrors.SILENCE_GLOBAL_ADMIN_REQUIRED,
                )
            await _check_alarm_device_access(target_device_id, user)
            _silence_info = target
        else:
            # admin 用户也获取静默信息用于审计
            _silence_info = manager.get_silence_by_id(silence_id)
        deleted = manager.delete_silence(silence_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=AlarmErrors.SILENCE_NOT_FOUND)
        # 第四轮修复: 审计日志记录告警静默删除
        try:
            from edgelite.api.auth import _get_client_ip
            from edgelite.services.audit_service import AuditAction
            client_ip = _get_client_ip(request) if request else ""
            user_agent = request.headers.get("User-Agent") if request else None
            await audit_svc.log(
                AuditAction.ALARM_SILENCE_DELETE,
                user_id=user["user_id"],
                username=user["username"],
                resource_type="alarm_silence",
                resource_id=silence_id,
                ip_address=client_ip,
                user_agent=user_agent,
                details={
                    "device_id": _silence_info.get("device_id") if _silence_info else "",
                    "rule_id": _silence_info.get("rule_id") if _silence_info else "",
                    "reason": _silence_info.get("reason") if _silence_info else "",
                },
            )
        except Exception as e:
            logger.warning("Audit log failed: %s", e)
        return ApiResponse(data={"id": silence_id, "deleted": True})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Delete alarm silence failed: %s", e)
        raise HTTPException(status_code=500, detail=AlarmErrors.SILENCE_UPDATE_FAILED) from e
