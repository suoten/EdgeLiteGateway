import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from edgelite.api.deps import CurrentUser
from edgelite.models.common import ApiResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scada", tags=["组态管理"])

_STORE_DIR = Path("data/scada")
_STORE_DIR.mkdir(parents=True, exist_ok=True)
_file_lock = asyncio.Lock()


class ScadaProject(BaseModel):
    name: str = "default"
    widgets: list[dict[str, Any]] = []
    updated_at: str | None = None


class ScadaSaveRequest(BaseModel):
    name: str = "default"
    widgets: list[dict[str, Any]] = []


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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@router.get("/projects", response_model=ApiResponse)
async def list_projects(_user: CurrentUser):
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
                logger.warning("读取组态项目文件失败 %s", f.name)
        return ApiResponse(data=projects)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取列表失败: %s", e)
        raise HTTPException(status_code=500, detail="获取列表失败") from e


@router.get("/project/{name}", response_model=ApiResponse)
async def get_project(name: str, _user: CurrentUser):
    path = _project_path(name)
    try:
        if not path.exists():
            return ApiResponse(data={"name": name, "widgets": [], "updated_at": None})
        data = await asyncio.to_thread(_read_json, path)
        if data is None:
            raise HTTPException(status_code=500, detail="读取组态项目失败")
        return ApiResponse(data=data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取失败: %s", e)
        raise HTTPException(status_code=500, detail="获取失败") from e


@router.post("/project", response_model=ApiResponse)
async def save_project(req: ScadaSaveRequest, _user: CurrentUser):
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
                raise HTTPException(status_code=500, detail="保存组态项目失败") from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("保存失败: %s", e)
        raise HTTPException(status_code=500, detail="保存失败") from e


@router.delete("/project/{name}", response_model=ApiResponse)
async def delete_project(name: str, _user: CurrentUser):
    path = _project_path(name)
    try:
        if path.exists():
            await asyncio.to_thread(path.unlink)
            return ApiResponse(data={"deleted": True, "name": name})
        raise HTTPException(status_code=404, detail=f"项目 {name} 不存在")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("删除失败: %s", e)
        raise HTTPException(status_code=500, detail="删除失败") from e
