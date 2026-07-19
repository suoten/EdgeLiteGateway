"""协议桥接 API 路由

使用 edgelite.engine.protocol_bridge.get_bridge_manager() 单例与
ProtocolBridge / MappingRule dataclass 提供协议桥接配置管理。

注意：
- ProtocolBridgeManager 通过 _sync_loop 后台运行桥接，本 API 仅管理配置。
- 前端 BridgeConfig 形状: {name, mappings: BridgeMapping[], enabled}
- 内部 ProtocolBridge 数据: {bridge_id, source_protocol, target_protocol, source_config, target_config, mapping_rules, enabled}
- 前端 BridgeMapping 字段: {source_protocol, source_device_id, source_point, target_protocol, target_device_id, target_point, transform?, enabled}
- 内部 MappingRule 字段: {rule_id, source_protocol, target_protocol, source_device, source_point, target_device, target_point, data_type, scale, offset, enabled}
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from edgelite.api.deps import PaginationDep, require_permission
from edgelite.api.error_codes import CommonErrors
from edgelite.models.common import ApiResponse, PagedResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/bridge", tags=["Protocol Bridge"])


class BridgeMapping(BaseModel):
    """前端映射规则模型。"""

    source_protocol: str = ""
    source_device_id: str = ""
    source_point: str = ""
    target_protocol: str = ""
    target_device_id: str = ""
    target_point: str = ""
    transform: str | None = "passthrough"
    scale: float = 1.0
    offset: float = 0.0
    enabled: bool = True


class BridgeConfig(BaseModel):
    """前端桥接配置模型。

    create 时需提供完整 config；update 时部分字段可选。
    """

    name: str
    source_protocol: str | None = None
    target_protocol: str | None = None
    mappings: list[BridgeMapping] = Field(default_factory=list)
    enabled: bool = True
    config: dict[str, Any] | None = None


class BridgeUpdateBody(BaseModel):
    """桥接更新请求体（task 规范 {config} 与前端 Partial<BridgeConfig> 兼容）。"""

    config: dict[str, Any] | None = None
    source_protocol: str | None = None
    target_protocol: str | None = None
    mappings: list[BridgeMapping] | None = None
    enabled: bool | None = None


def _get_manager():
    from edgelite.engine.protocol_bridge import get_bridge_manager

    return get_bridge_manager()


def _mapping_to_rule(mapping: BridgeMapping, rule_id: str | None = None):
    """将前端 BridgeMapping 转为内部 MappingRule。"""
    from edgelite.engine.protocol_bridge import MappingRule

    return MappingRule(
        rule_id=rule_id or f"rule_{uuid.uuid4().hex[:8]}",
        source_protocol=mapping.source_protocol,
        target_protocol=mapping.target_protocol,
        source_device=mapping.source_device_id,
        source_point=mapping.source_point,
        target_device=mapping.target_device_id,
        target_point=mapping.target_point,
        data_type=mapping.transform or "passthrough",
        scale=mapping.scale,
        offset=mapping.offset,
        enabled=mapping.enabled,
    )


def _rule_to_mapping(rule) -> dict[str, Any]:
    """将内部 MappingRule 转为前端 BridgeMapping dict。"""
    return {
        "source_protocol": getattr(rule, "source_protocol", ""),
        "source_device_id": getattr(rule, "source_device", ""),
        "source_point": getattr(rule, "source_point", ""),
        "target_protocol": getattr(rule, "target_protocol", ""),
        "target_device_id": getattr(rule, "target_device", ""),
        "target_point": getattr(rule, "target_point", ""),
        "transform": getattr(rule, "data_type", "passthrough"),
        "scale": getattr(rule, "scale", 1.0),
        "offset": getattr(rule, "offset", 0.0),
        "enabled": getattr(rule, "enabled", True),
    }


def _bridge_to_config(bridge) -> dict[str, Any]:
    """将内部 ProtocolBridge 转为前端 BridgeConfig dict。"""
    return {
        "name": getattr(bridge, "bridge_id", ""),
        "source_protocol": getattr(bridge, "source_protocol", ""),
        "target_protocol": getattr(bridge, "target_protocol", ""),
        "mappings": [_rule_to_mapping(r) for r in getattr(bridge, "mapping_rules", [])],
        "enabled": getattr(bridge, "enabled", True),
    }


def _find_bridge(manager, name: str):
    """从 manager 内部 _bridges 字典获取 ProtocolBridge 实例。"""
    bridges: dict[str, Any] = getattr(manager, "_bridges", {}) or {}
    return bridges.get(name)


def _create_bridge_from_config(body: BridgeConfig):
    """根据 BridgeConfig 构造 ProtocolBridge 实例。"""
    from edgelite.engine.protocol_bridge import ProtocolBridge

    # source_protocol/target_protocol 取 body 顶层或 mappings[0]
    src_proto = body.source_protocol or (body.mappings[0].source_protocol if body.mappings else "")
    tgt_proto = body.target_protocol or (body.mappings[0].target_protocol if body.mappings else "")
    rules = [_mapping_to_rule(m) for m in body.mappings]
    return ProtocolBridge(
        bridge_id=body.name,
        source_protocol=src_proto,
        target_protocol=tgt_proto,
        source_config=body.config or {},
        target_config={},
        mapping_rules=rules,
        enabled=body.enabled,
    )


@router.get("/list", response_model=PagedResponse)
async def list_bridges(
    pagination: PaginationDep,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """列出所有协议桥接配置。"""
    try:
        manager = _get_manager()
        bridges: dict[str, Any] = getattr(manager, "_bridges", {}) or {}
        items = [_bridge_to_config(b) for b in bridges.values()]
        total = len(items)
        page = pagination.page
        size = pagination.size
        start = (page - 1) * size
        end = start + size
        paged = items[start:end]
        return PagedResponse(data=paged, total=total, page=page, size=size)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_bridges failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.get("/{name}", response_model=ApiResponse)
async def get_bridge(
    name: str,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_READ)),
):
    """获取指定名称的桥接配置。"""
    try:
        manager = _get_manager()
        bridge = _find_bridge(manager, name)
        if bridge is None:
            raise HTTPException(status_code=404, detail=CommonErrors.NOT_FOUND)
        return ApiResponse(data=_bridge_to_config(bridge))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_bridge failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/create", response_model=ApiResponse)
async def create_bridge(
    body: BridgeConfig,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """创建新的协议桥接。"""
    try:
        manager = _get_manager()
        if _find_bridge(manager, body.name) is not None:
            raise HTTPException(status_code=400, detail=CommonErrors.VALIDATION_FAILED)
        bridge = _create_bridge_from_config(body)
        manager.add_bridge(bridge)
        return ApiResponse(data=_bridge_to_config(bridge))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_bridge failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.put("/{name}", response_model=ApiResponse)
async def update_bridge(
    name: str,
    body: BridgeUpdateBody,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """更新指定桥接配置。

    支持部分更新：mappings 替换映射规则；enabled 启停；source/target_protocol 修改协议。
    """
    try:
        manager = _get_manager()
        bridge = _find_bridge(manager, name)
        if bridge is None:
            raise HTTPException(status_code=404, detail=CommonErrors.NOT_FOUND)
        if body.source_protocol is not None:
            bridge.source_protocol = body.source_protocol
        if body.target_protocol is not None:
            bridge.target_protocol = body.target_protocol
        if body.enabled is not None:
            bridge.enabled = body.enabled
        if body.mappings is not None:
            bridge.mapping_rules = [_mapping_to_rule(m) for m in body.mappings]
        if body.config is not None:
            bridge.source_config = body.config
        return ApiResponse(data=_bridge_to_config(bridge))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_bridge failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/{name}/enable", response_model=ApiResponse)
async def enable_bridge(
    name: str,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """启用指定桥接。"""
    try:
        manager = _get_manager()
        bridge = _find_bridge(manager, name)
        if bridge is None:
            raise HTTPException(status_code=404, detail=CommonErrors.NOT_FOUND)
        bridge.enabled = True
        return ApiResponse(data={"name": name, "enabled": True})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("enable_bridge failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None


@router.post("/{name}/disable", response_model=ApiResponse)
async def disable_bridge(
    name: str,
    _user: dict[str, str] = Depends(require_permission(Permission.SYSTEM_MANAGE)),
):
    """禁用指定桥接。"""
    try:
        manager = _get_manager()
        bridge = _find_bridge(manager, name)
        if bridge is None:
            raise HTTPException(status_code=404, detail=CommonErrors.NOT_FOUND)
        bridge.enabled = False
        return ApiResponse(data={"name": name, "enabled": False})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("disable_bridge failed: %s", e)
        raise HTTPException(status_code=503, detail=CommonErrors.SERVICE_NOT_READY) from None
