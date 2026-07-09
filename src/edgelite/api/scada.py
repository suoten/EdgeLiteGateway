from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from edgelite.api.deps import require_permission
from edgelite.api.error_codes import ScadaErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scada", tags=["SCADA"])

# FIXED(一般): 原问题-相对路径依赖CWD, os.getcwd()返回进程启动时的工作目录, 不可靠;
# 修复-基于项目根目录的绝对路径。scada.py 位于 src/edgelite/api/, 需上溯4级到项目根目录
# (与 storage/database.py 中 _get_project_root 的 4 级 .parent 约定一致)
_STORE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "scada"
_STORE_DIR.mkdir(parents=True, exist_ok=True)
_file_lock = asyncio.Lock()


class ScadaProject(BaseModel):
    name: str = "default"
    widgets: list[dict[str, Any]] = []  # FIXED: 原问题-dict类型参数无schema校验，此处为动态配置场景，schema由驱动/平台运行时决定
    scenes: list[dict[str, Any]] = []  # 修复5: 多画面管理，每项 {id, name, widgets}
    updated_at: str | None = None


class ScadaSaveRequest(BaseModel):
    name: str = "default"
    widgets: list[dict[str, Any]] = []  # FIXED: 原问题-dict类型参数无schema校验，此处为动态配置场景，schema由驱动/平台运行时决定
    scenes: list[dict[str, Any]] = []  # 修复5: 多画面管理，每项 {id, name, widgets}


def _project_path(name: str) -> Path:
    # FIXED(一般): 原问题-isalnum()接受Unicode字符（中文/阿拉伯文），可构造异常文件名;
    # 修复-限制为ASCII字母数字
    safe = "".join(c for c in name if (c.isascii() and c.isalnum()) or c in ("_", "-")) or "default"
    return _STORE_DIR / f"{safe}.json"


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        logger.warning("Failed to read SCADA project: %s", e)  # FIXED-P1: 原问题-SCADA项目读取失败静默返回None
        return None


def _write_json(path: Path, data: dict) -> None:
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


@router.get("/projects", response_model=ApiResponse)
async def list_projects(user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
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
async def get_project(name: str, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ))):
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
async def save_project(req: ScadaSaveRequest, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
    path = _project_path(req.name)
    try:
        data = {
            "name": req.name,
            "widgets": req.widgets,
            "scenes": req.scenes,
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
async def delete_project(name: str, user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE))):
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
