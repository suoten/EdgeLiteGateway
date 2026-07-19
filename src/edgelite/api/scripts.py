"""脚本管理 API 路由

支持脚本的创建、更新、启停、测试、审批流（提交评审 / 通过 / 拒绝）。
- SQLite 表 ``scripts`` 持久化脚本元数据
- SQLite 表 ``script_logs`` 持久化测试执行日志
- test 端点用受限内建函数的 exec 沙箱执行（仅 Python 子集）
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from edgelite.api.deps import require_permission
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scripts", tags=["Scripts"])

_SCRIPTS_TABLE = "scripts"
_LOGS_TABLE = "script_logs"

_SCRIPTS_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_SCRIPTS_TABLE} ("
    "id TEXT PRIMARY KEY, "
    "name TEXT NOT NULL, "
    "language TEXT NOT NULL, "
    "content TEXT NOT NULL, "
    "description TEXT, "
    "enabled INTEGER NOT NULL DEFAULT 0, "
    "status TEXT NOT NULL DEFAULT 'draft', "
    "created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)"
)

_LOGS_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_LOGS_TABLE} ("
    "id TEXT PRIMARY KEY, "
    "script_id TEXT NOT NULL, "
    "level TEXT, "
    "message TEXT, "
    "output TEXT, "
    "error TEXT, "
    "duration_ms INTEGER, "
    "created_at TEXT NOT NULL)"
)

# exec 沙箱白名单内建函数
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "print": print,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


async def _ensure_tables() -> None:
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(text(_SCRIPTS_DDL))
            await session.execute(text(_LOGS_DDL))
            await session.commit()
    except Exception as e:
        logger.error("scripts ensure tables failed: %s", e)


class ScriptCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    language: str = Field(default="python", max_length=32)
    content: str = Field(..., min_length=1)
    description: str | None = Field(default=None, max_length=512)


class ScriptUpdateRequest(BaseModel):
    content: str | None = None
    description: str | None = None
    enabled: bool | None = None


class ScriptTestRequest(BaseModel):
    input: dict[str, Any] | None = None


def _row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r[0],
        "name": r[1],
        "language": r[2],
        "content": r[3],
        "description": r[4],
        "enabled": bool(r[5]),
        "status": r[6],
        "created_at": r[7],
        "updated_at": r[8],
    }


def _log_row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r[0],
        "script_id": r[1],
        "level": r[2],
        "message": r[3],
        "output": r[4],
        "error": r[5],
        "duration_ms": r[6],
        "created_at": r[7],
    }


@router.get("/list", response_model=PagedResponse)
async def list_scripts(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """分页返回脚本列表"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return PagedResponse(data=[], total=0, page=page, size=size)
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            total_result = await session.execute(text(f"SELECT COUNT(*) FROM {_SCRIPTS_TABLE}"))
            total = int(total_result.scalar() or 0)
            offset = (page - 1) * size
            result = await session.execute(
                text(
                    f"SELECT id, name, language, content, description, enabled, status, created_at, updated_at "
                    f"FROM {_SCRIPTS_TABLE} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": size, "offset": offset},
            )
            rows = result.fetchall()
        items = [_row_to_dict(r) for r in rows]
        return PagedResponse(data=items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/create", response_model=ApiResponse)
async def create_script(
    req: ScriptCreateRequest,
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """创建脚本"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
        await _ensure_tables()
        script_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(
                text(
                    f"INSERT INTO {_SCRIPTS_TABLE} "
                    "(id, name, language, content, description, enabled, status, created_at, updated_at) "
                    "VALUES (:id, :name, :lang, :content, :desc, 0, 'draft', :ts, :ts)"
                ),
                {
                    "id": script_id,
                    "name": req.name,
                    "lang": req.language,
                    "content": req.content,
                    "desc": req.description,
                    "ts": now,
                },
            )
            await session.commit()
        return ApiResponse(
            data={
                "id": script_id,
                "name": req.name,
                "language": req.language,
                "description": req.description,
                "enabled": False,
                "status": "draft",
                "created_at": now,
                "updated_at": now,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts create failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/{script_id}", response_model=ApiResponse)
async def get_script(
    script_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """返回脚本详情"""
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
                    f"SELECT id, name, language, content, description, enabled, status, created_at, updated_at "
                    f"FROM {_SCRIPTS_TABLE} WHERE id=:id"
                ),
                {"id": script_id},
            )
            r = result.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        return ApiResponse(data=_row_to_dict(r))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts get failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.put("/{script_id}", response_model=ApiResponse)
async def update_script(
    req: ScriptUpdateRequest,
    script_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """更新脚本（content / description / enabled）"""
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
                    f"SELECT id, name, language, content, description, enabled, status, created_at, updated_at "
                    f"FROM {_SCRIPTS_TABLE} WHERE id=:id"
                ),
                {"id": script_id},
            )
            r = result.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
            current = _row_to_dict(r)
            now = datetime.now(UTC).isoformat()
            new_content = req.content if req.content is not None else current["content"]
            new_desc = req.description if req.description is not None else current["description"]
            new_enabled = int(req.enabled) if req.enabled is not None else int(current["enabled"])
            await session.execute(
                text(
                    f"UPDATE {_SCRIPTS_TABLE} SET content=:c, description=:d, enabled=:e, updated_at=:ts "
                    "WHERE id=:id"
                ),
                {"c": new_content, "d": new_desc, "e": new_enabled, "ts": now, "id": script_id},
            )
            await session.commit()
        return ApiResponse(
            data={
                "id": script_id,
                "name": current["name"],
                "language": current["language"],
                "content": new_content,
                "description": new_desc,
                "enabled": bool(new_enabled),
                "status": current["status"],
                "created_at": current["created_at"],
                "updated_at": now,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts update failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/{script_id}/enable", response_model=ApiResponse)
async def enable_script(
    script_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        return await _toggle_script(script_id, True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts enable failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/{script_id}/disable", response_model=ApiResponse)
async def disable_script(
    script_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    try:
        return await _toggle_script(script_id, False)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts disable failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


async def _toggle_script(script_id: str, enabled: bool) -> ApiResponse:
    from edgelite.app import _app_state

    db = getattr(_app_state, "database", None)
    if db is None:
        raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
    await _ensure_tables()
    now = datetime.now(UTC).isoformat()
    async with db.get_session() as session:
        from sqlalchemy import text

        result = await session.execute(
            text(f"UPDATE {_SCRIPTS_TABLE} SET enabled=:e, updated_at=:ts WHERE id=:id"),
            {"e": 1 if enabled else 0, "ts": now, "id": script_id},
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        await session.commit()
    return ApiResponse(data={"id": script_id, "enabled": enabled, "updated_at": now})


@router.post("/{script_id}/test", response_model=ApiResponse)
async def test_script(
    req: ScriptTestRequest,
    script_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """在受限 exec 沙箱中执行脚本测试"""
    import time

    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(f"SELECT content FROM {_SCRIPTS_TABLE} WHERE id=:id"),
                {"id": script_id},
            )
            row = result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        content = row[0]
        inputs = req.input or {}

        start = time.perf_counter()
        output_lines: list[str] = []
        error_text = ""
        try:
            sandbox_globals: dict[str, Any] = {
                "__builtins__": _SAFE_BUILTINS,
                "input": inputs,
                "output": output_lines.append,
            }
            code_obj = compile(content, f"<script:{script_id}>", "exec")
            exec(code_obj, sandbox_globals)  # noqa: S102 - 受限内建沙箱
            output_value = sandbox_globals.get("result")
        except Exception as exc:
            error_text = "".join(traceback.format_exception_only(type(exc), exc))
            output_value = None
        duration_ms = int((time.perf_counter() - start) * 1000)

        log_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        try:
            async with db.get_session() as session:
                await session.execute(
                    text(
                        f"INSERT INTO {_LOGS_TABLE} "
                        "(id, script_id, level, message, output, error, duration_ms, created_at) "
                        "VALUES (:id, :sid, :lvl, :msg, :out, :err, :dur, :ts)"
                    ),
                    {
                        "id": log_id,
                        "sid": script_id,
                        "lvl": "error" if error_text else "info",
                        "msg": "test run",
                        "out": "\n".join(output_lines),
                        "err": error_text,
                        "dur": duration_ms,
                        "ts": now,
                    },
                )
                await session.commit()
        except Exception as log_err:
            logger.warning("scripts log persist failed: %s", log_err)

        return ApiResponse(
            data={
                "log_id": log_id,
                "success": not error_text,
                "output": "\n".join(output_lines),
                "result": output_value,
                "error": error_text,
                "duration_ms": duration_ms,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts test failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/{script_id}/logs", response_model=PagedResponse)
async def list_script_logs(
    script_id: str = Path(..., max_length=128),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """分页返回脚本执行日志"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return PagedResponse(data=[], total=0, page=page, size=size)
        await _ensure_tables()
        async with db.get_session() as session:
            from sqlalchemy import text

            total_result = await session.execute(
                text(f"SELECT COUNT(*) FROM {_LOGS_TABLE} WHERE script_id=:sid"),
                {"sid": script_id},
            )
            total = int(total_result.scalar() or 0)
            offset = (page - 1) * size
            result = await session.execute(
                text(
                    f"SELECT id, script_id, level, message, output, error, duration_ms, created_at "
                    f"FROM {_LOGS_TABLE} WHERE script_id=:sid "
                    "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                {"sid": script_id, "limit": size, "offset": offset},
            )
            rows = result.fetchall()
        items = [_log_row_to_dict(r) for r in rows]
        return PagedResponse(data=items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts logs failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/{script_id}/submit-review", response_model=ApiResponse)
async def submit_review(
    script_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """提交脚本评审"""
    try:
        return await _update_status(script_id, "pending_review")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts submit-review failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/{script_id}/approve", response_model=ApiResponse)
async def approve_script(
    script_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """通过脚本评审"""
    try:
        return await _update_status(script_id, "approved")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts approve failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/{script_id}/reject", response_model=ApiResponse)
async def reject_script(
    script_id: str = Path(..., max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """拒绝脚本评审"""
    try:
        return await _update_status(script_id, "rejected")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("scripts reject failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


async def _update_status(script_id: str, status: str) -> ApiResponse:
    from edgelite.app import _app_state

    db = getattr(_app_state, "database", None)
    if db is None:
        raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
    await _ensure_tables()
    now = datetime.now(UTC).isoformat()
    async with db.get_session() as session:
        from sqlalchemy import text

        result = await session.execute(
            text(f"UPDATE {_SCRIPTS_TABLE} SET status=:s, updated_at=:ts WHERE id=:id"),
            {"s": status, "ts": now, "id": script_id},
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        await session.commit()
    return ApiResponse(data={"id": script_id, "status": status, "updated_at": now})
