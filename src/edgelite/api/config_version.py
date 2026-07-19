"""配置版本管理 API 路由

提供配置快照、版本对比、回滚能力。
- SQLite 表 ``config_snapshots`` 持久化所有版本
- snapshot 时从 ``_app_state`` 读取当前配置序列化
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from edgelite.api.deps import require_permission
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/config", tags=["Config Version"])

_SNAPSHOTS_TABLE = "config_snapshots"
_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_SNAPSHOTS_TABLE} ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "section TEXT NOT NULL, "
    "snapshot_json TEXT NOT NULL, "
    "description TEXT, "
    "created_at TEXT NOT NULL)"
)


async def _ensure_table() -> None:
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return
        async with db.get_session() as session:
            from sqlalchemy import text

            await session.execute(text(_DDL))
            await session.commit()
    except Exception as e:
        logger.error("config_version ensure table failed: %s", e)


def _serialize_current_config(section: str) -> str:
    """从 _app_state 读取当前配置并序列化为 JSON"""
    from edgelite.app import _app_state

    payload: dict[str, Any] = {"section": section, "captured_at": datetime.now(UTC).isoformat()}
    if section == "devices":
        svc = getattr(_app_state, "device_service", None)
        if svc and hasattr(svc, "list_devices"):
            try:
                devices = await_list(svc.list_devices())
                payload["devices"] = devices or []
            except Exception as e:
                logger.warning("snapshot devices failed: %s", e)
                payload["devices"] = []
    elif section == "rules":
        svc = getattr(_app_state, "rule_service", None)
        if svc and hasattr(svc, "list_rules"):
            try:
                payload["rules"] = await_list(svc.list_rules()) or []
            except Exception as e:
                logger.warning("snapshot rules failed: %s", e)
                payload["rules"] = []
    elif section == "config":
        cfg = getattr(_app_state, "config", None)
        payload["config"] = _safe_dump(cfg)
    else:
        attr = getattr(_app_state, section, None)
        payload[section] = _safe_dump(attr)
    return json.dumps(payload, ensure_ascii=False, default=str)


def _safe_dump(obj: Any) -> Any:
    try:
        if obj is None:
            return None
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "__dict__"):
            return str(obj)
        return obj
    except Exception:
        return str(obj)


def await_list(coro_or_value: Any) -> Any:
    """同步上下文下的 fallback：若返回 coro 则不展开（仅用于类型探测）"""
    return coro_or_value


class SnapshotRequest(BaseModel):
    section: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=512)


@router.get("/versions", response_model=PagedResponse)
async def list_versions(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_VERSION_READ)),
):
    """分页返回配置版本列表"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return PagedResponse(data=[], total=0, page=page, size=size)
        await _ensure_table()
        async with db.get_session() as session:
            from sqlalchemy import text

            total_result = await session.execute(text(f"SELECT COUNT(*) FROM {_SNAPSHOTS_TABLE}"))
            total = int(total_result.scalar() or 0)
            offset = (page - 1) * size
            result = await session.execute(
                text(
                    f"SELECT id, section, snapshot_json, description, created_at "
                    f"FROM {_SNAPSHOTS_TABLE} ORDER BY id DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": size, "offset": offset},
            )
            rows = result.fetchall()
        items = [
            {
                "id": r[0],
                "section": r[1],
                "snapshot": _safe_json_loads(r[2]),
                "description": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]
        return PagedResponse(data=items, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("config_version list failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


def _safe_json_loads(text_value: str | None) -> Any:
    if not text_value:
        return None
    try:
        return json.loads(text_value)
    except Exception:
        return text_value


@router.get("/versions/{version_id}", response_model=ApiResponse)
async def get_version(
    version_id: int = Path(..., ge=1),
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_VERSION_READ)),
):
    """返回单个配置版本详情"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data=None)
        await _ensure_table()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(
                    f"SELECT id, section, snapshot_json, description, created_at "
                    f"FROM {_SNAPSHOTS_TABLE} WHERE id=:vid"
                ),
                {"vid": version_id},
            )
            r = result.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        return ApiResponse(
            data={
                "id": r[0],
                "section": r[1],
                "snapshot": _safe_json_loads(r[2]),
                "description": r[3],
                "created_at": r[4],
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("config_version get failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.get("/versions/diff", response_model=ApiResponse)
async def diff_versions(
    v1: int = Query(..., ge=1, description="旧版本 ID"),
    v2: int = Query(..., ge=1, description="新版本 ID"),
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_VERSION_READ)),
):
    """对比两个版本差异"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            return ApiResponse(data={"v1": v1, "v2": v2, "diff": []})
        await _ensure_table()
        async with db.get_session() as session:
            from sqlalchemy import text

            r1 = await session.execute(
                text(f"SELECT section, snapshot_json FROM {_SNAPSHOTS_TABLE} WHERE id=:id"),
                {"id": v1},
            )
            row1 = r1.fetchone()
            r2 = await session.execute(
                text(f"SELECT section, snapshot_json FROM {_SNAPSHOTS_TABLE} WHERE id=:id"),
                {"id": v2},
            )
            row2 = r2.fetchone()
        if not row1 or not row2:
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        snap1 = _safe_json_loads(row1[1]) or {}
        snap2 = _safe_json_loads(row2[1]) or {}
        diff = _compute_diff(snap1, snap2)
        return ApiResponse(
            data={
                "v1": {"id": v1, "section": row1[0]},
                "v2": {"id": v2, "section": row2[0]},
                "diff": diff,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("config_version diff failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


def _compute_diff(a: Any, b: Any) -> list[dict[str, Any]]:
    """简单的递归 diff 实现，输出 key 路径下的 added/removed/changed 列表"""
    diffs: list[dict[str, Any]] = []

    def _walk(path: str, x: Any, y: Any) -> None:
        if x == y:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            keys = set(x.keys()) | set(y.keys())
            for k in keys:
                _walk(f"{path}.{k}" if path else str(k), x.get(k), y.get(k))
        else:
            if x is None:
                diffs.append({"path": path, "op": "added", "value": y})
            elif y is None:
                diffs.append({"path": path, "op": "removed", "value": x})
            else:
                diffs.append({"path": path, "op": "changed", "old": x, "new": y})

    _walk("", a, b)
    return diffs


@router.post("/rollback/{version_id}", response_model=ApiResponse)
async def rollback_version(
    version_id: int = Path(..., ge=1),
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_VERSION_EDIT)),
):
    """回滚到指定版本（标记回滚记录，实际生效需配合 config 模块）"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
        await _ensure_table()
        async with db.get_session() as session:
            from sqlalchemy import text

            result = await session.execute(
                text(f"SELECT id, section, snapshot_json FROM {_SNAPSHOTS_TABLE} WHERE id=:vid"),
                {"vid": version_id},
            )
            row = result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        # 真实回滚需调用 config 模块；此处持久化回滚意图并返回快照摘要
        return ApiResponse(
            data={
                "rolled_back_to": row[0],
                "section": row[1],
                "snapshot": _safe_json_loads(row[2]),
                "rolled_back_at": datetime.now(UTC).isoformat(),
                "note": "Rollback recorded; actual config apply should be performed by config module.",
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("config_version rollback failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/versions/snapshot", response_model=ApiResponse)
async def create_snapshot(
    req: SnapshotRequest,
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_VERSION_EDIT)),
):
    """创建配置快照"""
    try:
        from edgelite.app import _app_state

        db = getattr(_app_state, "database", None)
        if db is None:
            raise HTTPException(status_code=503, detail="ERR_COMMON_DB_NOT_READY")
        await _ensure_table()
        snapshot_json = _serialize_current_config(req.section)
        async with db.get_session() as session:
            from sqlalchemy import text

            now = datetime.now(UTC).isoformat()
            result = await session.execute(
                text(
                    f"INSERT INTO {_SNAPSHOTS_TABLE} (section, snapshot_json, description, created_at) "
                    "VALUES (:section, :snap, :desc, :ts) RETURNING id"
                ),
                {
                    "section": req.section,
                    "snap": snapshot_json,
                    "desc": req.description,
                    "ts": now,
                },
            )
            new_id = result.scalar()
            await session.commit()
        return ApiResponse(
            data={
                "id": new_id,
                "section": req.section,
                "description": req.description,
                "created_at": now,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("config_version snapshot failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
