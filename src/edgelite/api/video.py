"""视频接入API路由 - 包含AI分析功能"""

from __future__ import annotations

import base64
import logging
import os
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from edgelite.api.deps import (
    ConfigDep,
    DeviceServiceDep,
    VideoServiceDep,
    require_permission,
)
from edgelite.api.error_codes import AiErrors, DeviceErrors, VideoErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/video", tags=["Video"])

_MAX_IMAGE_SIZE = 50 * 1024 * 1024  # FIXED-P0: 原问题-上传图像无大小限制，可导致OOM
_MAX_BASE64_IMAGE_SIZE = 80 * 1024 * 1024  # FIXED-P0: base64编码后约增大33%

# G-08: 上传图像扩展名白名单，防止上传恶意文件
_ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

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
    device_svc: DeviceServiceDep,
    channel_id: str = Query(default="1", max_length=64),
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_READ)),
):
    try:
        # FIXED(严重): 原问题-未检查设备访问权限，任何有 VIDEO_READ 权限的用户可获取
        # 任意设备的视频流 URL（IDOR 越权）。视频流 URL 通常包含认证凭证，泄露后可被未授权观看
        # 修复：复用 devices.py 的设备归属校验逻辑
        from edgelite.api.devices import _check_device_owner
        await _check_device_owner(device_svc, device_id, user)
        url = await svc.get_stream_url(device_id, channel_id)
        if not url:
            raise HTTPException(status_code=503, detail=VideoErrors.STREAM_NOT_AVAILABLE)
        return ApiResponse(data={"url": url, "device_id": device_id, "channel_id": channel_id})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_stream_url failed: %s", e, exc_info=True)
        raise HTTPException(status_code=503, detail=VideoErrors.STREAM_NOT_AVAILABLE) from e


@router.post("/{device_id}/ptz", response_model=ApiResponse)
async def ptz_control(
    device_id: str,
    action: _PTZ_ACTIONS,
    svc: VideoServiceDep,
    device_svc: DeviceServiceDep,
    channel_id: str = Query(default="1", max_length=64),
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_CONTROL)),
):
    try:
        # FIXED(严重): 原问题-未检查设备访问权限，任何有 VIDEO_CONTROL 权限的用户可控制
        # 任意设备的 PTZ（IDOR 越权）。PTZ 控制是物理操作，越权控制可能影响监控覆盖范围
        # 修复：复用 devices.py 的设备归属校验逻辑
        from edgelite.api.devices import _check_device_owner
        await _check_device_owner(device_svc, device_id, user)
        success = await svc.ptz_control(device_id, channel_id, action)
        if not success:
            raise HTTPException(status_code=400, detail=VideoErrors.PTZ_FAILED)
        return ApiResponse()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ptz_control failed: %s", e, exc_info=True)
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
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_READ)),
):
    """对图像进行AI分析

    支持基于ONNX Runtime的物体检测、缺陷检测等AI推理任务。
    """
    try:
        from edgelite.drivers.video_ai import get_video_analyzer

        # FIXED-P0: 原问题-image_data无大小限制，超大base64可导致OOM
        if len(request.image_data) > _MAX_BASE64_IMAGE_SIZE:
            raise HTTPException(status_code=413, detail=AiErrors.INFERENCE_FAILED)

        analyzer = get_video_analyzer()
        result = await analyzer.analyze_base64_image(
            request.image_data,
            request.model_name,
        )
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI analyze failed: %s", e)
        # FIXED(P2): 原问题-异常时返回HTTP 200+error_code，违反REST约定;
        # 修复-改为raise HTTPException返回正确的500状态码
        raise HTTPException(status_code=500, detail=AiErrors.INFERENCE_FAILED) from e


@router.post("/ai/analyze/upload", response_model=ApiResponse)
async def ai_analyze_upload(
    file: UploadFile = File(...),
    model_name: str = Query(default="default", max_length=128),
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_READ)),
):
    """上传图像进行AI分析"""
    try:
        # G-08: 校验文件扩展名，仅允许常见图像格式，防止上传恶意文件
        _, ext = os.path.splitext(file.filename or "")
        if ext.lower() not in _ALLOWED_IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail=AiErrors.INFERENCE_FAILED)
        contents = await file.read()
        # FIXED-P0: 原问题-上传文件无大小限制，可导致OOM
        if len(contents) > _MAX_IMAGE_SIZE:
            raise HTTPException(status_code=413, detail=AiErrors.INFERENCE_FAILED)
        image_b64 = base64.b64encode(contents).decode("utf-8")

        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        result = await analyzer.analyze_base64_image(image_b64, model_name)
        return ApiResponse(data=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI analyze upload failed: %s", e)
        # FIXED(P2): 原问题-异常时返回HTTP 200+error_code，违反REST约定;
        # 修复-改为raise HTTPException返回正确的500状态码
        raise HTTPException(status_code=500, detail=AiErrors.INFERENCE_FAILED) from e


@router.post("/ai/model", response_model=ApiResponse)
async def ai_load_model(
    config: AIModelConfig,
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_CONTROL)),
):
    """加载AI模型"""
    try:
        # FIXED(安全): 路径遍历防护 - 使用 pathlib 规范化路径后校验，防止 Windows 绝对路径/UNC 路径绕过
        from pathlib import Path

        from edgelite.drivers.video_ai import InferenceConfig, InferenceTask, get_video_analyzer
        if not config.model_path:
            raise HTTPException(status_code=400, detail=AiErrors.MODEL_LOAD_FAILED)
        _model_path = Path(config.model_path)
        if _model_path.is_absolute() or ".." in config.model_path or ":" in config.model_path[:3] or "\\\\" in config.model_path or len(config.model_path) > 512:
            raise HTTPException(status_code=400, detail=AiErrors.MODEL_LOAD_FAILED)
        # 校验扩展名
        if _model_path.suffix.lower() not in (".onnx", ".tflite", ".pmml"):
            raise HTTPException(status_code=400, detail=AiErrors.MODEL_LOAD_FAILED)
        # FIXED(P1): 原问题-未解析符号链接，攻击者可在允许目录内创建符号链接指向系统文件绕过校验;
        # 修复-resolve后校验解析路径在允许的ai_models目录内（与bootstrap_ai默认models_dir一致）
        resolved = _model_path.resolve()
        models_dir = (Path(__file__).resolve().parent.parent / "ai_models").resolve()
        # R6-S-21: 使用 os.path.normcase() 统一大小写后再比较，
        # 防止 Windows 上大小写敏感导致路径校验绕过（如 C:\ vs c:\）
        _norm_resolved = os.path.normcase(str(resolved))
        _norm_models_dir = os.path.normcase(str(models_dir))
        if not _norm_resolved.startswith(_norm_models_dir + os.sep) and _norm_resolved != _norm_models_dir:
            raise HTTPException(status_code=403, detail=AiErrors.MODEL_LOAD_FAILED)

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
            raise HTTPException(status_code=500, detail=AiErrors.MODEL_LOAD_FAILED)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI load model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.MODEL_LOAD_FAILED) from e


@router.get("/ai/models", response_model=ApiResponse)
async def ai_list_models(
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_READ)),
):
    """列出已加载的AI模型"""
    try:
        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        return ApiResponse(data={"models": analyzer.model_names})
    except Exception as e:
        logger.error("AI list models failed: %s", e)
        # FIXED(P2): 原问题-异常时返回HTTP 200+error_code，违反REST约定;
        # 修复-改为raise HTTPException返回正确的500状态码
        raise HTTPException(status_code=500, detail=AiErrors.LIST_FAILED) from e


@router.get("/ai/stats", response_model=ApiResponse)
async def ai_stats(
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_READ)),
):
    """获取AI分析统计"""
    try:
        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        return ApiResponse(data=analyzer.get_stats())
    except Exception as e:
        logger.error("AI stats failed: %s", e)
        # FIXED(P2): 原问题-异常时返回HTTP 200+error_code，违反REST约定;
        # 修复-改为raise HTTPException返回正确的500状态码
        raise HTTPException(status_code=500, detail=AiErrors.STATS_FAILED) from e


@router.delete("/ai/model/{model_name}", response_model=ApiResponse)
async def ai_unload_model(
    model_name: str,
    user: dict[str, str] = Depends(require_permission(Permission.VIDEO_CONTROL)),
):
    """卸载AI模型"""
    try:
        from edgelite.drivers.video_ai import get_video_analyzer

        analyzer = get_video_analyzer()
        # FIXED(P1): 原问题-直接del内部_models字典无锁保护，并发推理迭代_models时触发RuntimeError;
        # 修复-调用analyzer.unload_model方法，在_model_lock内安全移除并释放ONNX session
        unloaded = await analyzer.unload_model(model_name)
        if unloaded:
            return ApiResponse(data={"model_name": model_name, "unloaded": True})
        else:
            raise HTTPException(status_code=404, detail=DeviceErrors.NOT_FOUND)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("AI unload model failed: %s", e)
        raise HTTPException(status_code=500, detail=AiErrors.MODEL_LOAD_FAILED) from e
