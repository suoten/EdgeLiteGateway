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
from edgelite.constants import _MAX_QUERY_SIZE
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


async def _serialize_current_config(section: str) -> str:
    """从 _app_state 读取当前配置并序列化为 JSON

    FIXED(安全-P1): 原问题-devices/rules 分支调用 await_list(svc.list_devices())
    原样返回协程对象，经 json.dumps(default=str) 序列化为 "<coroutine object ...>"
    对象地址——最关键的两类配置从未真正存过快照。修复-改为 async 函数真正 await
    服务层调用，并顺带消除"coroutine never awaited"告警。

    FIXED(P1-2 补充): 服务层 list_devices()/list_rules() 返回 (items, total) 元组，
    原修复直接把元组存进快照——快照内容变成 [设备列表, 总数] 的错误形状。修复-解包
    元组只保留条目列表，并经 _json_safe 递归转换（Pydantic 模型/datetime/对象兜底），
    保证快照 JSON 可被回滚端点直接消费。
    """
    from edgelite.app import _app_state

    payload: dict[str, Any] = {"section": section, "captured_at": datetime.now(UTC).isoformat()}
    if section == "devices":
        svc = getattr(_app_state, "device_service", None)
        if svc and hasattr(svc, "list_devices"):
            try:
                result = await svc.list_devices(page=1, size=_MAX_QUERY_SIZE)
                # DeviceService.list_devices 返回 (items, total) 元组，兼容直接返回列表的实现
                items = result[0] if isinstance(result, tuple) else result
                payload["devices"] = _json_safe(items) or []
            except Exception as e:
                logger.warning("snapshot devices failed: %s", e)
                payload["devices"] = []
    elif section == "rules":
        svc = getattr(_app_state, "rule_service", None)
        if svc and hasattr(svc, "list_rules"):
            try:
                result = await svc.list_rules(page=1, size=_MAX_QUERY_SIZE)
                items = result[0] if isinstance(result, tuple) else result
                payload["rules"] = _json_safe(items) or []
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


def _json_safe(obj: Any) -> Any:
    """递归转换为可 JSON 序列化结构（P1-2）。

    - dict/list/tuple/set 递归处理；
    - datetime → ISO8601 字符串；
    - Pydantic 模型 → model_dump(mode="json")；
    - 其他带 __dict__ 的对象 → 属性字典；
    - 兜底 str(obj)，绝不返回协程等不可序列化对象。
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if hasattr(obj, "model_dump"):
        try:
            return _json_safe(obj.model_dump(mode="json"))
        except Exception:
            return str(obj)
    if hasattr(obj, "__dict__"):
        return _json_safe(vars(obj))
    return str(obj)


def _safe_dump(obj: Any) -> Any:
    """P1-2 起统一走 _json_safe（保留原函数名兼容既有引用）。"""
    return _json_safe(obj)


def await_list(coro_or_value: Any) -> Any:
    """已废弃：FIXED(安全-P1) 前的占位实现，原样返回协程导致快照存的是协程字符串。

    保留空实现仅为兼容潜在外部引用；快照序列化已改为 async 真正 await 服务层调用。
    """
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
                    f"SELECT id, section, snapshot_json, description, created_at FROM {_SNAPSHOTS_TABLE} WHERE id=:vid"
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


# 回滚时允许写回的设备字段（与 devices.py update 端点的字段白名单对齐，
# 额外放行 protocol/status 以支持快照恢复语义）
_ROLLBACK_DEVICE_FIELDS = ("name", "protocol", "config", "points", "collect_interval", "status")
# 回滚时允许写回的规则字段（对齐 _orm_to_rule 输出，剔除身份/审计字段）
_ROLLBACK_RULE_FIELDS = (
    "name",
    "device_id",
    "conditions",
    "logic",
    "duration",
    "severity",
    "enabled",
    "notify_channels",
    "script",
    "rule_type",
)


def _parse_snapshot_or_409(version_id: int, snapshot_json: str) -> dict[str, Any]:
    """解析快照 JSON，损坏（非法 JSON / 非对象 / 旧格式协程字符串）时抛 409。"""
    try:
        snapshot = json.loads(snapshot_json)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ERR_CONFIG_SNAPSHOT_CORRUPTED",
                "message": (
                    f"Snapshot #{version_id} is corrupted and cannot be rolled back: "
                    "snapshot_json is not valid JSON. "
                    "Hint: snapshots created before the P1 fix may contain '<coroutine object>' strings."
                ),
            },
        ) from e
    if not isinstance(snapshot, dict):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ERR_CONFIG_SNAPSHOT_CORRUPTED",
                "message": f"Snapshot #{version_id} is corrupted: snapshot_json is not a JSON object.",
            },
        )
    return snapshot


def _extract_section_items(snapshot: dict[str, Any], section: str, version_id: int) -> list[Any]:
    """从快照中取出 devices/rules 条目列表；旧格式（协程字符串/非列表）抛 409。"""
    items = snapshot.get(section)
    if items is None:
        # 该 section 快照缺失（旧版快照序列化失败时写入空列表，None 属于结构损坏）
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ERR_CONFIG_SNAPSHOT_CORRUPTED",
                "message": f"Snapshot #{version_id} has no '{section}' section; cannot roll back.",
            },
        )
    # 旧格式损坏特征：协程对象被 default=str 序列化为 "<coroutine object ...>" 字符串
    if isinstance(items, str) or not isinstance(items, list):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ERR_CONFIG_SNAPSHOT_CORRUPTED",
                "message": (
                    f"Snapshot #{version_id} '{section}' section is corrupted "
                    f"(expected a list, got {type(items).__name__}). "
                    "Hint: snapshots created before the P1 fix stored coroutine repr strings."
                ),
            },
        )
    return items


@router.post("/rollback/{version_id}", response_model=ApiResponse)
async def rollback_version(
    version_id: int = Path(..., ge=1),
    user: dict[str, str] = Depends(require_permission(Permission.CONFIG_VERSION_EDIT)),
):
    """回滚到指定版本（真实回滚）

    FIXED(P1-2 假回滚): 原实现只查库返回成功字段，不执行任何回滚——操作员以为已回滚，
    实际什么都没发生。现在 devices/rules 段逐条调用现有 service 更新方法
    （device_service.update_device / rule_service.update_rule），返回成功/失败计数；
    config 及其他 section 明确返回 501 未支持（不再假装成功）；快照内容损坏
    （如旧格式存了 coroutine 字符串）返回 409 + 明确错误信息。
    """
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

        section = row[1]
        snapshot = _parse_snapshot_or_409(version_id, row[2])

        applied = 0
        failed: list[dict[str, Any]] = []

        if section == "devices":
            device_svc = getattr(_app_state, "device_service", None)
            if device_svc is None:
                raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY")
            items = _extract_section_items(snapshot, "devices", version_id)
            for item in items:
                if not isinstance(item, dict) or not item.get("device_id"):
                    failed.append({"id": None, "error": "invalid device entry in snapshot"})
                    continue
                device_id = item["device_id"]
                update_data = {k: item[k] for k in _ROLLBACK_DEVICE_FIELDS if k in item}
                try:
                    updated = await device_svc.update_device(device_id, update_data)
                    if updated is None:
                        failed.append({"id": device_id, "error": "device not found"})
                    else:
                        applied += 1
                except Exception as e:
                    logger.warning("rollback device %s failed: %s", device_id, e)
                    failed.append({"id": device_id, "error": str(e)})
        elif section == "rules":
            rule_svc = getattr(_app_state, "rule_service", None)
            if rule_svc is None:
                raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY")
            items = _extract_section_items(snapshot, "rules", version_id)
            for item in items:
                if not isinstance(item, dict) or not item.get("rule_id"):
                    failed.append({"id": None, "error": "invalid rule entry in snapshot"})
                    continue
                rule_id = item["rule_id"]
                update_data = {k: item[k] for k in _ROLLBACK_RULE_FIELDS if k in item}
                try:
                    updated = await rule_svc.update_rule(rule_id, update_data)
                    if updated is None:
                        failed.append({"id": rule_id, "error": "rule not found"})
                    else:
                        applied += 1
                except Exception as e:
                    logger.warning("rollback rule %s failed: %s", rule_id, e)
                    failed.append({"id": rule_id, "error": str(e)})
        else:
            # config 及其他 section 涉及系统级配置热更新，当前不支持自动回滚——
            # 明确 501，绝不假装成功
            raise HTTPException(
                status_code=501,
                detail={
                    "error_code": "ERR_CONFIG_ROLLBACK_SECTION_UNSUPPORTED",
                    "message": (
                        f"Automatic rollback for section '{section}' is not supported; "
                        "only 'devices' and 'rules' snapshots can be rolled back."
                    ),
                },
            )

        rolled_back_at = datetime.now(UTC).isoformat()
        # 回滚操作审计日志（尽力而为，不阻塞回滚结果）
        try:
            audit_service = getattr(_app_state, "audit_service", None)
            if audit_service is not None:
                from edgelite.services.audit_service import AuditAction

                await audit_service.log(
                    AuditAction.CONFIG_UPDATE,
                    user_id=user.get("user_id"),
                    username=user.get("username"),
                    resource_type="config_snapshot",
                    resource_id=str(version_id),
                    details={
                        "operation": "config_rollback",
                        "section": section,
                        "applied": applied,
                        "failed_count": len(failed),
                    },
                    status="success" if not failed else "partial",
                )
        except Exception as e:
            logger.warning("config rollback audit log failed: %s", e)

        return ApiResponse(
            data={
                "rolled_back_to": version_id,
                "section": section,
                "applied": applied,
                "failed": failed,
                "failed_count": len(failed),
                "success": not failed,
                "rolled_back_at": rolled_back_at,
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
        snapshot_json = await _serialize_current_config(req.section)
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
