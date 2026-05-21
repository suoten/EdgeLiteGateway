"""AI模型管理服务层"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from edgelite.engine.edge_ai_inference import AiInferenceEngine
from edgelite.models.ai_model import (
    AiInferenceLogResponse,
    AiModelDetailResponse,
    AiModelResponse,
    AiStatsResponse,
    ModelStatus,
)

logger = logging.getLogger(__name__)


class AiModelService:
    """AI模型管理服务"""

    def __init__(self, ai_engine: AiInferenceEngine, database=None):
        self._engine = ai_engine
        self._database = database

    async def list_models(self, page: int = 1, page_size: int = 20) -> dict:
        models = self._engine.get_loaded_models()
        items = []
        for mid, wrapper in models.items():
            items.append(
                AiModelResponse(
                    model_id=wrapper.model_id,
                    model_name=wrapper.model_name,
                    model_version=wrapper.model_version,
                    model_type=wrapper.model_type,
                    model_file_path=wrapper.model_path,
                    status=wrapper.status,
                    is_preset=wrapper.is_preset,
                    input_schema=wrapper.input_schema,
                    output_schema=wrapper.output_schema,
                    created_at=wrapper.loaded_at.isoformat() if wrapper.loaded_at else "",
                    updated_at=wrapper.loaded_at.isoformat() if wrapper.loaded_at else "",
                ).model_dump()
            )
        total = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        return {"items": items[start:end], "total": total, "page": page, "page_size": page_size}

    async def get_model(self, model_id: str) -> AiModelDetailResponse | None:
        wrapper = self._engine.get_model(model_id)
        if not wrapper:
            return None
        stats = self._engine.get_model_stats(model_id)
        return AiModelDetailResponse(
            model_id=wrapper.model_id,
            model_name=wrapper.model_name,
            model_version=wrapper.model_version,
            model_type=wrapper.model_type,
            model_file_path=wrapper.model_path,
            status=wrapper.status,
            is_preset=wrapper.is_preset,
            input_schema=wrapper.input_schema,
            output_schema=wrapper.output_schema,
            created_at=wrapper.loaded_at.isoformat() if wrapper.loaded_at else "",
            updated_at=wrapper.loaded_at.isoformat() if wrapper.loaded_at else "",
            inference_count=stats.get("call_count", 0) if stats else 0,
            avg_latency_ms=stats.get("avg_latency_ms", 0) if stats else 0,
            last_inference_at=None,
        )

    async def update_model(self, model_id: str, update_data: dict) -> AiModelResponse | None:
        wrapper = self._engine.get_model(model_id)
        if not wrapper:
            return None
        if update_data.get("model_name"):
            wrapper.model_name = update_data["model_name"]
        if update_data.get("input_schema"):
            wrapper.input_schema = update_data["input_schema"]
        if update_data.get("output_schema"):
            wrapper.output_schema = update_data["output_schema"]
        return AiModelResponse(
            model_id=wrapper.model_id,
            model_name=wrapper.model_name,
            model_version=wrapper.model_version,
            model_type=wrapper.model_type,
            model_file_path=wrapper.model_path,
            status=wrapper.status,
            is_preset=wrapper.is_preset,
            input_schema=wrapper.input_schema,
            output_schema=wrapper.output_schema,
            created_at=wrapper.loaded_at.isoformat() if wrapper.loaded_at else "",
            updated_at=datetime.now(UTC).isoformat(),
        )

    async def delete_model(self, model_id: str) -> bool:
        wrapper = self._engine.get_model(model_id)
        if not wrapper:
            return False
        if wrapper.is_preset:
            return False
        await self._engine.remove_model(model_id)
        return True

    async def enable_model(self, model_id: str) -> bool:
        wrapper = self._engine.get_model(model_id)
        if not wrapper:
            return False
        await self._engine.enable_model(model_id)
        return True

    async def disable_model(self, model_id: str) -> bool:
        wrapper = self._engine.get_model(model_id)
        if not wrapper:
            return False
        await self._engine.disable_model(model_id)
        return True

    async def reload_model(self, model_id: str, model_file_path: str) -> bool:
        try:
            await self._engine.reload_model(model_id, model_file_path)
            return True
        except Exception as e:
            logger.error("模型热加载失败: %s - %s", model_id, e)
            return False

    async def inference(self, model_id: str, input_data: list[float], device_id: str | None = None, point_name: str | None = None) -> dict:
        result = await self._engine.infer(model_id, input_data)
        log_id = str(uuid.uuid4())
        wrapper = self._engine.get_model(model_id)
        model_name = wrapper.model_name if wrapper else model_id
        input_summary = str(input_data[:5]) + ("..." if len(input_data) > 5 else "")
        output_summary = str(result.output_data)[:200]
        return {
            "model_id": result.model_id,
            "output_data": result.output_data,
            "latency_ms": result.latency_ms,
            "timestamp": result.timestamp.isoformat(),
            "status": result.status,
            "error_message": result.error_message,
            "log": {
                "log_id": log_id,
                "model_id": model_id,
                "model_name": model_name,
                "device_id": device_id,
                "point_name": point_name,
                "input_summary": input_summary,
                "output_summary": output_summary,
                "latency_ms": result.latency_ms,
                "status": result.status,
                "error_message": result.error_message,
                "timestamp": result.timestamp.isoformat(),
            },
        }

    async def get_stats(self) -> AiStatsResponse:
        snapshot = self._engine.get_stats()
        return AiStatsResponse(
            total_calls=snapshot.get("total_calls", 0),
            total_errors=snapshot.get("total_errors", 0),
            avg_latency_ms=snapshot.get("avg_latency_ms", 0),
            model_distribution=snapshot.get("model_distribution", {}),
        )

    async def get_model_stats(self, model_id: str) -> dict | None:
        return self._engine.get_model_stats(model_id)

    async def get_inference_logs(self, model_id: str | None = None, page: int = 1, page_size: int = 20) -> dict:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
