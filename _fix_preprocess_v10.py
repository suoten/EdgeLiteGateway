
content = open('src/edgelite/api/preprocess.py', 'r', encoding='utf-8').read()

old = """    if req.global_config:
        config = _get_config()
        if hasattr(config, "preprocess") and config.preprocess:
            enabled = req.global_config.get("enabled")
            if enabled is not None:
                config.preprocess.enabled = enabled
            deadband = req.global_config.get("default_deadband")
            if deadband is not None:
                config.preprocess.default_deadband = deadband
            filter_window = req.global_config.get("default_filter_window")
            if filter_window is not None:
                config.preprocess.default_filter_window = filter_window
            aggregate_window = req.global_config.get("default_aggregate_window_sec")
            if aggregate_window is not None:
                config.preprocess.default_aggregate_window_sec = aggregate_window"""

new = """    if req.global_config:
        config = _get_config()
        if hasattr(config, "preprocess") and config.preprocess:
            config.preprocess.enabled = req.global_config.enabled
            config.preprocess.default_deadband = req.global_config.default_deadband
            config.preprocess.default_filter_window = req.global_config.default_filter_window
            config.preprocess.default_aggregate_window_sec = req.global_config.default_aggregate_window_sec"""

content = content.replace(old, new)

# Add logging import
content = content.replace(
    "from fastapi import APIRouter, HTTPException",
    "import logging\n\nfrom fastapi import APIRouter, HTTPException"
)

# Add logger
content = content.replace(
    'router = APIRouter(prefix="/api/v1/preprocess"',
    'logger = logging.getLogger(__name__)\n\nrouter = APIRouter(prefix="/api/v1/preprocess"'
)

# Add try/except to get_preprocess_config
old_get = """@router.get("/config", response_model=ApiResponse)
async def get_preprocess_config(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    config = _get_config()
    preprocessor = _get_preprocessor()

    point_configs = {}
    if preprocessor:
        point_configs = getattr(preprocessor, "_configs", {})

    preprocess_config = getattr(config, "preprocess", None)

    return ApiResponse(data={
        "enabled": getattr(preprocess_config, "enabled", False) if preprocess_config else False,
        "default_deadband": getattr(preprocess_config, "default_deadband", 0.0) if preprocess_config else 0.0,
        "default_filter_window": getattr(preprocess_config, "default_filter_window", 3) if preprocess_config else 3,
        "default_aggregate_window_sec": getattr(preprocess_config, "default_aggregate_window_sec", 0) if preprocess_config else 0,
        "point_configs": point_configs,
    })"""

new_get = """@router.get("/config", response_model=ApiResponse)
async def get_preprocess_config(
    user: CurrentUser = require_permission(Permission.SYSTEM_READ),
):
    try:
        config = _get_config()
        preprocessor = _get_preprocessor()

        point_configs = {}
        if preprocessor:
            point_configs = getattr(preprocessor, "_configs", {})

        preprocess_config = getattr(config, "preprocess", None)

        return ApiResponse(data={
            "enabled": getattr(preprocess_config, "enabled", False) if preprocess_config else False,
            "default_deadband": getattr(preprocess_config, "default_deadband", 0.0) if preprocess_config else 0.0,
            "default_filter_window": getattr(preprocess_config, "default_filter_window", 3) if preprocess_config else 3,
            "default_aggregate_window_sec": getattr(preprocess_config, "default_aggregate_window_sec", 0) if preprocess_config else 0,
            "point_configs": point_configs,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取预处理配置失败: %s", e)
        raise HTTPException(status_code=500, detail="获取预处理配置失败")"""

content = content.replace(old_get, new_get)

# Add try/except to update_preprocess_config
old_put = """@router.put("/config", response_model=ApiResponse)
async def update_preprocess_config(
    req: PreprocessUpdateRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    preprocessor = _get_preprocessor()
    if not preprocessor:
        raise HTTPException(status_code=503, detail="预处理器未初始化")

    if req.global_config:
        config = _get_config()
        if hasattr(config, "preprocess") and config.preprocess:
            config.preprocess.enabled = req.global_config.enabled
            config.preprocess.default_deadband = req.global_config.default_deadband
            config.preprocess.default_filter_window = req.global_config.default_filter_window
            config.preprocess.default_aggregate_window_sec = req.global_config.default_aggregate_window_sec

    for point_key, config in req.points.items():
        preprocessor.configure(point_key, config)

    return ApiResponse(data={"status": "updated", "points_configured": len(req.points)})"""

new_put = """@router.put("/config", response_model=ApiResponse)
async def update_preprocess_config(
    req: PreprocessUpdateRequest,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
):
    try:
        preprocessor = _get_preprocessor()
        if not preprocessor:
            raise HTTPException(status_code=503, detail="预处理器未初始化")

        if req.global_config:
            config = _get_config()
            if hasattr(config, "preprocess") and config.preprocess:
                config.preprocess.enabled = req.global_config.enabled
                config.preprocess.default_deadband = req.global_config.default_deadband
                config.preprocess.default_filter_window = req.global_config.default_filter_window
                config.preprocess.default_aggregate_window_sec = req.global_config.default_aggregate_window_sec

        for point_key, point_cfg in req.points.items():
            preprocessor.configure(point_key, point_cfg)

        return ApiResponse(data={"status": "updated", "points_configured": len(req.points)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新预处理配置失败: %s", e)
        raise HTTPException(status_code=500, detail="更新预处理配置失败")"""

content = content.replace(old_put, new_put)

with open('src/edgelite/api/preprocess.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('V1.0 preprocess.py fixed successfully')
