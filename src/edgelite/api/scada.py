from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.api.deps import CurrentUser, require_permission
from edgelite.api.error_codes import ScadaErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scada", tags=["SCADA"])

_STORE_DIR = Path("data/scada")
_STORE_DIR.mkdir(parents=True, exist_ok=True)
_file_lock = asyncio.Lock()


class ScadaProject(BaseModel):
    name: str = "default"
    widgets: list[dict[str, Any]] = []  # FIXED: 原问题-dict类型参数无schema校验，此处为动态配置场景，schema由驱动/平台运行时决定
    updated_at: str | None = None


class ScadaSaveRequest(BaseModel):
    name: str = "default"
    widgets: list[dict[str, Any]] = []  # FIXED: 原问题-dict类型参数无schema校验，此处为动态配置场景，schema由驱动/平台运行时决定


def _project_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in ("_", "-")) or "default"
    return _STORE_DIR / f"{safe}.json"


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_json(path: Path, data: dict) -> None:
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@router.get("/projects", response_model=ApiResponse)
async def list_projects(user: CurrentUser = require_permission(Permission.SYSTEM_READ)):
    projects = []
    try:
        for f in _STORE_DIR.glob("*.json"):
            data = await asyncio.to_thread(_read_json, f)
            if data is not None:
                projects.append(
                    {
                        "name": data.get("name", f.stem),
                        "widget_count": len(data.get("widgets", [])),
                        "updated_at": data.get("updated_at", ""),
                    }
                )
            else:
                logger.warning("Failed to read scada project file %s", f.name)  # FIXED: 原问题-中文硬编码日志
        return ApiResponse(data=projects)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list projects: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=ScadaErrors.LOAD_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.get("/project/{name}", response_model=ApiResponse)
async def get_project(name: str, user: CurrentUser = require_permission(Permission.SYSTEM_READ)):
    path = _project_path(name)
    try:
        if not path.exists():
            return ApiResponse(data={"name": name, "widgets": [], "updated_at": None})
        data = await asyncio.to_thread(_read_json, path)
        if data is None:
            raise HTTPException(status_code=500, detail=ScadaErrors.LOAD_FAILED)  # FIXED: 原问题-中文硬编码detail，改为error_code
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get project: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=ScadaErrors.LOAD_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.post("/project", response_model=ApiResponse)
async def save_project(req: ScadaSaveRequest, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    path = _project_path(req.name)
    try:
        data = {
            "name": req.name,
            "widgets": req.widgets,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        async with _file_lock:
            try:
                await asyncio.to_thread(_write_json, path, data)
                return ApiResponse(
                    data={"saved": True, "name": req.name, "widget_count": len(req.widgets)}
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=ScadaErrors.SAVE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to save project: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=ScadaErrors.SAVE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code


@router.delete("/project/{name}", response_model=ApiResponse)
async def delete_project(name: str, user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE)):
    path = _project_path(name)
    try:
        if path.exists():
            await asyncio.to_thread(path.unlink)
            return ApiResponse(data={"deleted": True, "name": name})
        raise HTTPException(status_code=404, detail=ScadaErrors.PROJECT_NOT_FOUND)  # FIXED: 原问题-中文硬编码detail，改为error_code
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete project: %s", e)  # FIXED: 原问题-中文硬编码日志
        raise HTTPException(status_code=500, detail=ScadaErrors.DELETE_FAILED) from e  # FIXED: 原问题-中文硬编码detail，改为error_code
