"""视频接入API路由 - 包含AI分析功能"""

from __future__ import annotations

import base64
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from edgelite.api.deps import ConfigDep, CurrentUser, VideoServiceDep, require_permission
from edgelite.api.error_codes import VideoErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/video", tags=["Video"])

_PTZ_ACTIONS = Literal[
    "up",
    "down",
    "left",
    "right",
    "up_left",
    "up_right",
    "down_left",
    "down_right",
    "zoom_in",
    "zoom_out",
    "focus_in",
    "focus_out",
    "stop",
]


class VideoWebhookEvent(BaseModel):
    event_type: str = Field(default="", description="事件类型")
    device_id: str = Field(default="", description="设备ID")
    timestamp: str | None = Field(default=None, description="事件时间戳")

    model_config = {"extra": "allow"}


class AIAnalyzeRequest(BaseModel):
    """AI分析请求"""
    image_data: str = Field(..., description="Base64编码的图像数据")
    model_name: str = Field(default="default", description="模型名称")
    device_id: str = Field(default="", description="关联设备ID")


class AIAnalyzeResponse(BaseModel):
    """AI分析响应"""
    detections: list[dict] = Field(default_factory=list, description="检测结果列表")
    stats: dict = Field(default_factory=dict, description="分析统计")
    error: str | None = Field(default=None, description="错误信息")


class AIModelConfig(BaseModel):
    """AI模型配置"""
    name: str = Field(..., description="模型名称")
    model_path: str = Field(..., description="ONNX模型文件路径")
    model_type: str = Field(default="object_detection", description="模型类型")
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    labels: list[str] = Field(default_factory=list, description="类别标签")


def _verify_webhook_key(config=Depends(ConfigDep), x_api_key: str = Header(default="")) -> None:
    import hmac
    if (
        config
        and getattr(config, "server", None)
        and getattr(config.server, "webhook_api_key", None)
    ):
        if not x_api_key or not hmac.compare_digest(x_api_key, config.server.webhook_api_key):
            raise HTTPException(status_code=401, detail=VideoErrors.API_KEY_INVALID)
    else:
        raise HTTPException(status_code=401, detail=VideoErrors.API_KEY_NOT_CONFIGURED)


@router.get("/{device_id}/stream", response_model=ApiResponse)
async def get_stream_url(
    device_id: str,
    svc: VideoServiceDep,
    channel_id: str = "1",
    user: CurrentUser = require_permission(Permission.VIDEO_READ),
):
    try:
        url = await svc.get_stream_url(device_id, channel_id)
        if not url:
            # FIXED: 原问题-中文硬编码detail，改为error_code
            raise HTTPException(status_code=503, detail=VideoErrors.PTZ_FAILED)
        return ApiResponse(data={"url": url, "device_id": device_id, "channel_id": channel_id})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_stream_url failed: %s", e)
        raise HTTPException(status_code=500, detail=VideoErrors.PTZ_FAILED) from e


@router.post("/{device_id}/ptz", response_model=ApiResponse)
async def ptz_control(
    device_id: str,
    action: _PTZ_ACTIONS,
    svc: VideoServiceDep,
    channel_id: str = "1",
    user: CurrentUser = require_permission(Permission.VIDEO_CONTROL),
):
    try:
        success = await svc.ptz_control(device_id, channel_id, action)
        if not success:
            # FIXED: 原问题-中文硬编码detail"云台控制失败"，改为error_code
            raise HTTPException(status_code=400, detail=VideoErrors.PTZ_FAILED)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ptz_control failed: %s", e)
        raise HTTPException(status_code=500, detail=VideoErrors.PTZ_FAILED) from e


@router.post("/webhook", response_model=ApiResponse)
async def video_webhook(
    event: VideoWebhookEvent,
    svc: VideoServiceDep,
    auth: None = Depends(_verify_webhook_key),
):
    try:
        await svc.handle_webhook(event.model_dump())
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("video_webhook failed: %s", e)
        raise HTTPException(status_code=500, detail=VideoErrors.WEBHOOK_FAILED) from e


# ============= AI视频分析API =============

@router.post("/ai/analyze", response_model=ApiResponse)
async def ai_analyze(
    request: AIAnalyzeRequest,
    user: CurrentUser = require_permission(Permission.VIDEO_READ),
):
    """对图像进行AI分析

    支持基于ONNX Runtime的物体检测、缺陷检测等AI推理任务。
    """
    try:
        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        result = await analyzer.analyze_base64_image(
            request.image_data,
            request.model_name,
        )
        return ApiResponse(data=result)
    except Exception as e:
        logger.error("AI analyze failed: %s", e)
        return ApiResponse(data={"error": str(e), "detections": []})


@router.post("/ai/analyze/upload", response_model=ApiResponse)
async def ai_analyze_upload(
    file: UploadFile = File(...),
    model_name: str = "default",
    user: CurrentUser = require_permission(Permission.VIDEO_READ),
):
    """上传图像进行AI分析"""
    try:
        contents = await file.read()
        image_b64 = base64.b64encode(contents).decode("utf-8")

        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        result = await analyzer.analyze_base64_image(image_b64, model_name)
        return ApiResponse(data=result)
    except Exception as e:
        logger.error("AI analyze upload failed: %s", e)
        return ApiResponse(data={"error": str(e), "detections": []})


@router.post("/ai/model", response_model=ApiResponse)
async def ai_load_model(
    config: AIModelConfig,
    user: CurrentUser = require_permission(Permission.VIDEO_CONTROL),
):
    """加载AI模型"""
    try:
        from edgelite.drivers.video_ai import InferenceConfig, InferenceTask, get_video_analyzer

        task_map = {
            "object_detection": InferenceTask.OBJECT_DETECTION,
            "classification": InferenceTask.CLASSIFICATION,
            "anomaly_detection": InferenceTask.ANOMALY_DETECTION,
        }

        inf_config = InferenceConfig(
            model_path=config.model_path,
            model_type=task_map.get(config.model_type, InferenceTask.OBJECT_DETECTION),
            confidence_threshold=config.confidence_threshold,
            labels=config.labels,
        )

        analyzer = get_video_analyzer()
        success = analyzer.add_model(config.name, inf_config)

        if success:
            return ApiResponse(data={"model_name": config.name, "loaded": True})
        else:
            raise HTTPException(status_code=400, detail="Failed to load model")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI load model failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/ai/models", response_model=ApiResponse)
async def ai_list_models(
    user: CurrentUser = require_permission(Permission.VIDEO_READ),
):
    """列出已加载的AI模型"""
    try:
        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        return ApiResponse(data={"models": analyzer.model_names})
    except Exception as e:
        logger.error("AI list models failed: %s", e)
        return ApiResponse(data={"models": [], "error": str(e)})


@router.get("/ai/stats", response_model=ApiResponse)
async def ai_stats(
    user: CurrentUser = require_permission(Permission.VIDEO_READ),
):
    """获取AI分析统计"""
    try:
        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        return ApiResponse(data=analyzer.get_stats())
    except Exception as e:
        logger.error("AI stats failed: %s", e)
        return ApiResponse(data={"error": str(e)})


@router.delete("/ai/model/{model_name}", response_model=ApiResponse)
async def ai_unload_model(
    model_name: str,
    user: CurrentUser = require_permission(Permission.VIDEO_CONTROL),
):
    """卸载AI模型"""
    try:
        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        if model_name in analyzer._models:
            del analyzer._models[model_name]
            return ApiResponse(data={"model_name": model_name, "unloaded": True})
        else:
            raise HTTPException(status_code=404, detail="Model not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI unload model failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
