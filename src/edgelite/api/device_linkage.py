"""设备联动 API 路由

支持联动规则的 CRUD、执行历史查询和统计。
- SQLite 表 ``linkage_rules`` 持久化规则
- SQLite 表 ``linkage_executions`` 持久化执行历史
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from edgelite.api.deps import require_permission
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/linkage", tags=["Device Linkage"])

_RULES_TABLE = "linkage_rules"
_EXECS_TABLE = "linkage_executions"

_RULES_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_RULES_TABLE} ("
    "id TEXT PRIMARY KEY, "
    "name TEXT NOT NULL, "
    "source_device TEXT NOT NULL, "
    "source_point TEXT NOT NULL, "
    "condition TEXT NOT NULL, "
    "target_device TEXT NOT NULL, "
    "target_point TEXT NOT NULL, "
    "action TEXT NOT NULL, "
    "enabled INTEGER NOT NULL DEFAULT 1, "
    "created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)"
)

_EXECS_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_EXECS_TABLE} ("
    "id TEXT PRIMARY KEY, "
    "rule_id TEXT NOT NULL, "
    "rule_name TEXT, "
    "status TEXT NOT NULL, "
    "input_value TEXT, "
    "output_value TEXT, "
    "error TEXT, "
    "duration_ms INTEGER, "
    "executed_at TEXT NOT NULL)"
)


async def _ensure_tables() -> None:
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(text(_RULES_DDL))
            await session.execute(text(_EXECS_DDL))
            await session.commit()
    except Exception as e:
        logger.error("linkage ensure tables failed: %s", e)


class RuleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    source_device: str = Field(..., max_length=128)
    source_point: str = Field(..., max_length=128)
    condition: str = Field(..., max_length=512)
    target_device: str = Field(..., max_length=128)
    target_point: str = Field(..., max_length=128)
    action: str = Field(..., max_length=64)


class RuleUpdateRequest(BaseModel):
    name: str | None = None
    source_device: str | None = None
    source_point: str | None = None
    condition: str | None = None
    target_device: str | None = None
    target_point: str | None = None
    target_action: str | None = None
    action: str | None = None
    enabled: bool | None = None


def _rule_row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r[0],
        "name": r[1],
        "source_device": r[2],
        "source_point": r[3],
        "condition": r[4],
        "target_device": r[5],
        "target_point": r[6],
        "action": r[7],
        "enabled": bool(r[8]),
        "created_at": r[9],
        "updated_at": r[10],
    }


def _exec_row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r[0],
        "rule_id": r[1],
        "rule_name": r[2],
        "status": r[3],
        "input_value": _safe_json_loads(r[4]),
        "output_value": _safe_json_loads(r[5]),
        "error": r[6],
        "duration_ms": r[7],
        "executed_at": r[8],
    }


def _safe_json_loads(text_value: str | None) -> Any:
    if not text_value:
        return None
    try:
        return json.loads(text_value)
    except Exception:
        return text_value


@router.get("/rules", response_model=PagedResponse)
async def list_rules(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.RULE_READ)),
):
    """分页返回联动规则"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return PagedResponse(data=[], total=0, page=page, size=size)
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            total_result = await session.execute(text(f"SELECT COUNT(*) FROM {_RULES_TABLE}"))
            total = int(total_result.scalar() or 0)
            offset = (page - 1) * size
            result = await session.execute(
                text(
                    f"SELECT id, name, source_device, source_point, condition, "
                    "target_device, target_point, action, enabled, created_at, updated_at "
                    f"FROM {_RULES_TABLE} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": size, "offset": offset},
            )
            rows = result.fetchall()
        items = [_rule_row_to_dict(r) for r in rows]
        return PagedResponse(data=items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("linkage list rules failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/rules", response_model=ApiResponse)
async def create_rule(
    req: RuleCreateRequest,
    user: dict[str, str] = Depends(require_permission(Permission.RULE_CREATE)),
):
    """创建联动规则"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
        await _ensure_tables()
        rule_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(
                text(
                    f"INSERT INTO {_RULES_TABLE} "
                    "(id, name, source_device, source_point, condition, "
                    "target_device, target_point, action, enabled, created_at, updated_at) "
                    "VALUES (:id, :name, :src_d, :src_p, :cond, "
                    ":tgt_d, :tgt_p, :action, 1, :ts, :ts)"
                ),
                {
                    "id": rule_id,
                    "name": req.name,
                    "src_d": req.source_device,
                    "src_p": req.source_point,
                    "cond": req.condition,
                    "tgt_d": req.target_device,
                    "tgt_p": req.target_point,
                    "action": req.action,
                    "ts": now,
                },
            )
            await session.commit()
        return ApiResponse(
            data={
                "id": rule_id,
                "name": req.name,
                "source_device": req.source_device,
                "source_point": req.source_point,
                "condition": req.condition,
                "target_device": req.target_device,
                "target_point": req.target_point,
                "action": req.action,
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("linkage create rule failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/rules/{rule_id}", response_model=ApiResponse)
async def get_rule(
    rule_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.RULE_READ)),
):
    """返回联动规则详情"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data=None)
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"SELECT id, name, source_device, source_point, condition, "
                    "target_device, target_point, action, enabled, created_at, updated_at "
                    f"FROM {_RULES_TABLE} WHERE id=:id"
                ),
                {"id": rule_id},
            )
            r = result.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        return ApiResponse(data=_rule_row_to_dict(r))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("linkage get rule failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.put("/rules/{rule_id}", response_model=ApiResponse)
async def update_rule(
    req: RuleUpdateRequest,
    rule_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.RULE_UPDATE)),
):
    """更新联动规则"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"SELECT id, name, source_device, source_point, condition, "
                    "target_device, target_point, action, enabled, created_at, updated_at "
                    f"FROM {_RULES_TABLE} WHERE id=:id"
                ),
                {"id": rule_id},
            )
            r = result.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
            current = _rule_row_to_dict(r)
            now = datetime.now(UTC).isoformat()
            new_values = {
                "name": req.name if req.name is not None else current["name"],
                "source_device": req.source_device
                if req.source_device is not None
                else current["source_device"],
                "source_point": req.source_point
                if req.source_point is not None
                else current["source_point"],
                "condition": req.condition if req.condition is not None else current["condition"],
                "target_device": req.target_device
                if req.target_device is not None
                else current["target_device"],
                "target_point": req.target_point
                if req.target_point is not None
                else current["target_point"],
                "action": req.action if req.action is not None else current["action"],
                "enabled": 1 if req.enabled else 0
                if req.enabled is not None
                else int(current["enabled"]),
            }
            await session.execute(
                text(
                    f"UPDATE {_RULES_TABLE} SET name=:name, source_device=:src_d, "
                    "source_point=:src_p, condition=:cond, target_device=:tgt_d, "
                    "target_point=:tgt_p, action=:action, enabled=:enabled, updated_at=:ts "
                    "WHERE id=:id"
                ),
                {
                    "id": rule_id,
                    "name": new_values["name"],
                    "src_d": new_values["source_device"],
                    "src_p": new_values["source_point"],
                    "cond": new_values["condition"],
                    "tgt_d": new_values["target_device"],
                    "tgt_p": new_values["target_point"],
                    "action": new_values["action"],
                    "enabled": new_values["enabled"],
                    "ts": now,
                },
            )
            await session.commit()
        return ApiResponse(
            data={
                "id": rule_id,
                "name": new_values["name"],
                "source_device": new_values["source_device"],
                "source_point": new_values["source_point"],
                "condition": new_values["condition"],
                "target_device": new_values["target_device"],
                "target_point": new_values["target_point"],
                "action": new_values["action"],
                "enabled": bool(new_values["enabled"]),
                "created_at": current["created_at"],
                "updated_at": now,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("linkage update rule failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.delete("/rules/{rule_id}", response_model=ApiResponse)
async def delete_rule(
    rule_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.RULE_DELETE)),
):
    """删除联动规则"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(f"DELETE FROM {_RULES_TABLE} WHERE id=:id"),
                {"id": rule_id},
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
            await session.commit()
        return ApiResponse(data={"id": rule_id, "deleted": True})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("linkage delete rule failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/executions", response_model=PagedResponse)
async def list_executions(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    rule_id: str | None = Query(default=None, max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.RULE_READ)),
):
    """分页返回执行历史"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return PagedResponse(data=[], total=0, page=page, size=size)
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            if rule_id:
                total_result = await session.execute(
                    text(f"SELECT COUNT(*) FROM {_EXECS_TABLE} WHERE rule_id=:rid"),
                    {"rid": rule_id},
                )
                total = int(total_result.scalar() or 0)
                offset = (page - 1) * size
                result = await session.execute(
                    text(
                        f"SELECT id, rule_id, rule_name, status, input_value, output_value, "
                        "error, duration_ms, executed_at "
                        f"FROM {_EXECS_TABLE} WHERE rule_id=:rid "
                        "ORDER BY executed_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    {"rid": rule_id, "limit": size, "offset": offset},
                )
            else:
                total_result = await session.execute(text(f"SELECT COUNT(*) FROM {_EXECS_TABLE}"))
                total = int(total_result.scalar() or 0)
                offset = (page - 1) * size
                result = await session.execute(
                    text(
                        f"SELECT id, rule_id, rule_name, status, input_value, output_value, "
                        f"error, duration_ms, executed_at FROM {_EXECS_TABLE} "
                        "ORDER BY executed_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    {"limit": size, "offset": offset},
                )
            rows = result.fetchall()
        items = [_exec_row_to_dict(r) for r in rows]
        return PagedResponse(data=items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("linkage executions list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/executions/stats", response_model=ApiResponse)
async def executions_stats(
    start: str = Query(..., description="ISO8601 起始时间"),
    end: str = Query(..., description="ISO8601 结束时间"),
    user: dict[str, str] = Depends(require_permission(Permission.RULE_READ)),
):
    """返回指定时间窗口内的执行统计"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data={"total": 0, "success": 0, "failed": 0, "success_rate": 0.0})
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"SELECT status, COUNT(*) FROM {_EXECS_TABLE} "
                    "WHERE executed_at >= :start AND executed_at <= :end "
                    "GROUP BY status"
                ),
                {"start": start, "end": end},
            )
            rows = result.fetchall()
        stats: dict[str, int] = {row[0]: int(row[1]) for row in rows}
        total = sum(stats.values())
        success = stats.get("success", 0)
        failed = stats.get("failed", 0) + stats.get("error", 0)
        success_rate = round((success / total) * 100, 2) if total else 0.0
        return ApiResponse(
            data={
                "start": start,
                "end": end,
                "total": total,
                "success": success,
                "failed": failed,
                "success_rate": success_rate,
                "by_status": stats,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("linkage executions stats failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
