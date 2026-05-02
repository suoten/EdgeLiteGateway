import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scada", tags=["组态管理"])

_STORE_DIR = Path("data/scada")
_STORE_DIR.mkdir(parents=True, exist_ok=True)


class ScadaProject(BaseModel):
    name: str = "default"
    widgets: list[dict[str, Any]] = []
    updated_at: Optional[str] = None


class ScadaSaveRequest(BaseModel):
    name: str = "default"
    widgets: list[dict[str, Any]] = []


class ApiResponse:
    @staticmethod
    def data(data: Any = None) -> dict:
        return {"code": 0, "message": "success", "data": data}

    @staticmethod
    def error(msg: str, code: int = -1) -> dict:
        return {"code": code, "message": msg, "data": None}


def _project_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in ("_", "-")) or "default"
    return _STORE_DIR / f"{safe}.json"


@router.get("/projects", response_model=dict)
async def list_projects():
    projects = []
    for f in _STORE_DIR.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            projects.append({
                "name": data.get("name", f.stem),
                "widget_count": len(data.get("widgets", [])),
                "updated_at": data.get("updated_at", ""),
            })
        except Exception:
            pass
    return ApiResponse.data(projects)


@router.get("/project/{name}", response_model=dict)
async def get_project(name: str):
    path = _project_path(name)
    if not path.exists():
        return ApiResponse.data({"name": name, "widgets": [], "updated_at": None})
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ApiResponse.data(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取组态项目失败: {e}")


@router.post("/project", response_model=dict)
async def save_project(req: ScadaSaveRequest):
    path = _project_path(req.name)
    data = {
        "name": req.name,
        "widgets": req.widgets,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return ApiResponse.data({"saved": True, "name": req.name, "widget_count": len(req.widgets)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存组态项目失败: {e}")


@router.delete("/project/{name}", response_model=dict)
async def delete_project(name: str):
    path = _project_path(name)
    if path.exists():
        path.unlink()
        return ApiResponse.data({"deleted": True, "name": name})
    raise HTTPException(status_code=404, detail=f"项目 {name} 不存在")
