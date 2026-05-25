"""Protocol debug API - packet sniffing and signal simulation"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from edgelite.api.deps import CurrentUser, get_driver_registry, require_permission
from edgelite.api.error_codes import CommonErrors
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])

# In-memory packet buffer for sniffing (bounded deque per protocol)
_MAX_PACKET_BUFFER = 1000
_packet_buffers: dict[str, deque[dict[str, Any]]] = {}


def _get_buffer(protocol: str | None = None) -> deque[dict[str, Any]]:
    """Get or create packet buffer for a protocol"""
    key = protocol or "__all__"
    if key not in _packet_buffers:
        _packet_buffers[key] = deque(maxlen=_MAX_PACKET_BUFFER)
    return _packet_buffers[key]


def record_packet(
    direction: str,
    protocol: str,
    device_id: str,
    content: str | bytes,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a protocol packet for sniffing. Called by driver layer."""
    packet = {
        "timestamp": time.time(),
        "direction": direction,  # "tx" or "rx"
        "protocol": protocol,
        "device_id": device_id,
        "content": content if isinstance(content, str) else content.hex(),
        "content_type": "hex" if isinstance(content, bytes) else "ascii",
        "metadata": metadata or {},
    }
    _get_buffer(protocol).append(packet)
    _get_buffer("__all__").append(packet)


# Modbus function code definitions for signal simulator
_MODBUS_FUNCTIONS = {
    "01": "Read Coils",
    "02": "Read Discrete Inputs",
    "03": "Read Holding Registers",
    "04": "Read Input Registers",
    "05": "Write Single Coil",
    "06": "Write Single Register",
    "15": "Write Multiple Coils",
    "16": "Write Multiple Registers",
}

# Protocol form schemas for the signal simulator
_PROTOCOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "modbus_tcp": {
        "name": "Modbus TCP",
        "fields": [
            {"key": "function_code", "label": "Function Code", "type": "select",
             "options": [{"value": k, "label": f"{k} - {v}"} for k, v in _MODBUS_FUNCTIONS.items()]},
            {"key": "start_address", "label": "Start Address", "type": "number", "default": 0, "min": 0, "max": 65535},
            {"key": "quantity", "label": "Quantity", "type": "number", "default": 1, "min": 1, "max": 125},
            {"key": "slave_id", "label": "Slave ID", "type": "number", "default": 1, "min": 1, "max": 247},
            {"key": "write_value", "label": "Write Value (for write functions)", "type": "number", "optional": True},
        ],
    },
    "modbus_rtu": {
        "name": "Modbus RTU",
        "fields": [
            {"key": "function_code", "label": "Function Code", "type": "select",
             "options": [{"value": k, "label": f"{k} - {v}"} for k, v in _MODBUS_FUNCTIONS.items()]},
            {"key": "start_address", "label": "Start Address", "type": "number", "default": 0, "min": 0, "max": 65535},
            {"key": "quantity", "label": "Quantity", "type": "number", "default": 1, "min": 1, "max": 125},
            {"key": "slave_id", "label": "Slave ID", "type": "number", "default": 1, "min": 1, "max": 247},
            {"key": "write_value", "label": "Write Value (for write functions)", "type": "number", "optional": True},
        ],
    },
    "opcua": {
        "name": "OPC UA",
        "fields": [
            {"key": "node_id", "label": "Node ID", "type": "text", "placeholder": "ns=2;i=1"},
            {"key": "operation", "label": "Operation", "type": "select",
             "options": [
                 {"value": "read", "label": "Read"},
                 {"value": "write", "label": "Write"},
                 {"value": "browse", "label": "Browse"},
             ]},
            {"key": "write_value", "label": "Write Value", "type": "text", "optional": True},
        ],
    },
    "mqtt_client": {
        "name": "MQTT",
        "fields": [
            {"key": "topic", "label": "Topic", "type": "text", "placeholder": "sensor/data"},
            {"key": "payload", "label": "Payload", "type": "textarea", "placeholder": '{"key": "value"}'},
            {"key": "qos", "label": "QoS", "type": "select",
             "options": [
                 {"value": "0", "label": "0 - At most once"},
                 {"value": "1", "label": "1 - At least once"},
                 {"value": "2", "label": "2 - Exactly once"},
             ]},
        ],
    },
    "s7": {
        "name": "Siemens S7",
        "fields": [
            {"key": "area", "label": "Area", "type": "select",
             "options": [
                 {"value": "DB", "label": "Data Block (DB)"},
                 {"value": "I", "label": "Input (I)"},
                 {"value": "Q", "label": "Output (Q)"},
                 {"value": "M", "label": "Marker (M)"},
             ]},
            {"key": "db_number", "label": "DB Number", "type": "number", "default": 1},
            {"key": "start", "label": "Start Address", "type": "number", "default": 0},
            {"key": "size", "label": "Size (bytes)", "type": "number", "default": 1, "min": 1},
        ],
    },
}


@router.get("/protocols", response_model=ApiResponse)
async def list_debug_protocols(
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
) -> ApiResponse:
    """List protocols available for debugging with their form schemas"""
    try:
        from edgelite.drivers.registry import DRIVER_DISPLAY_NAMES
    except ImportError:
        DRIVER_DISPLAY_NAMES = {}

    protocols = []
    for proto_key, schema in _PROTOCOL_SCHEMAS.items():
        display_name = DRIVER_DISPLAY_NAMES.get(proto_key, {}).get("en", schema["name"])
        protocols.append({
            "key": proto_key,
            "name": display_name,
            "schema": schema,
        })

    # Add remaining protocols without detailed schemas
    for proto_key, name_info in DRIVER_DISPLAY_NAMES.items():
        if proto_key not in _PROTOCOL_SCHEMAS:
            protocols.append({
                "key": proto_key,
                "name": name_info.get("en", proto_key),
                "schema": {
                    "name": name_info.get("en", proto_key),
                    "fields": [
                        {"key": "operation", "label": "Operation", "type": "select",
                         "options": [
                             {"value": "read", "label": "Read"},
                             {"value": "write", "label": "Write"},
                         ]},
                        {"key": "params", "label": "Parameters (JSON)", "type": "textarea",
                         "placeholder": "{}"},
                    ],
                },
            })

    return ApiResponse(data={"protocols": protocols})


@router.post("/simulate", response_model=ApiResponse)
async def simulate_signal(
    protocol: str = Query(..., description="Protocol type"),
    device_id: str = Query(..., description="Device ID"),
    operation: str = Query("read", description="Operation: read/write/discover"),
    params: dict | None = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
) -> ApiResponse:
    """Send a test signal to a device and return the raw request/response"""
    import asyncio

    from edgelite.api.deps import get_device_service
    from fastapi import Request

    # Get device info to validate it exists
    try:
        from edgelite.bootstrap import _app_state
        container = _app_state
    except ImportError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)

    if not hasattr(container, "device_service") or container.device_service is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)

    try:
        device = await container.device_service.get_device(device_id)
    except Exception:
        raise HTTPException(404, f"Device not found: {device_id}")

    if not device:
        raise HTTPException(404, f"Device not found: {device_id}")

    # Execute the operation based on protocol type
    params = params or {}
    start_time = time.time()
    result: dict[str, Any] = {
        "protocol": protocol,
        "device_id": device_id,
        "operation": operation,
        "params": params,
        "request_raw": None,
        "response_raw": None,
        "values": None,
        "error": None,
        "elapsed_ms": 0,
    }

    try:
        if protocol in ("modbus_tcp", "modbus_rtu"):
            result_update = await _simulate_modbus(container, device, operation, params)
            result.update(result_update)
        elif protocol == "opcua":
            result_update = await _simulate_opcua(container, device, operation, params)
            result.update(result_update)
        elif protocol == "mqtt_client":
            result_update = await _simulate_mqtt(container, device, operation, params)
            result.update(result_update)
        elif protocol == "s7":
            result_update = await _simulate_s7(container, device, operation, params)
            result.update(result_update)
        else:
            # Generic: try read/write via device service
            result_update = await _simulate_generic(container, device, operation, params)
            result.update(result_update)
    except Exception as e:
        result["error"] = str(e)
        logger.warning("Simulate signal failed for %s/%s: %s", protocol, device_id, e)

    result["elapsed_ms"] = round((time.time() - start_time) * 1000, 1)

    # Record the packet for sniffing
    if result.get("request_raw"):
        record_packet("tx", protocol, device_id, result["request_raw"])
    if result.get("response_raw"):
        record_packet("rx", protocol, device_id, result["response_raw"])

    return ApiResponse(data=result)


async def _simulate_modbus(
    container: Any, device: dict, operation: str, params: dict
) -> dict[str, Any]:
    """Simulate Modbus read/write operation"""
    function_code = params.get("function_code", "03")
    start_address = int(params.get("start_address", 0))
    quantity = int(params.get("quantity", 1))
    slave_id = int(params.get("slave_id", 1))
    write_value = params.get("write_value")

    driver = None
    if hasattr(container, "driver_registry") and container.driver_registry:
        proto = "modbus_tcp" if device.get("protocol") == "modbus_tcp" else "modbus_rtu"
        driver_cls = container.driver_registry.get(proto)
        if driver_cls:
            driver = driver_cls(device.get("config", {}))

    if driver is None:
        return {"error": "Modbus driver not available", "values": None}

    request_raw = f"FC={function_code} Addr={start_address} Qty={quantity} Slave={slave_id}"
    if operation == "write" or function_code in ("05", "06", "15", "16"):
        if write_value is not None:
            try:
                await driver.write(start_address, write_value)
                response_raw = f"Write OK: Addr={start_address} Value={write_value}"
                return {"request_raw": request_raw, "response_raw": response_raw, "values": {"written": write_value}}
            except Exception as e:
                return {"request_raw": request_raw, "response_raw": None, "error": str(e)}
        return {"request_raw": request_raw, "error": "write_value required for write operation"}

    # Read operation
    try:
        values = await driver.read(start_address, quantity)
        response_raw = f"Read OK: Addr={start_address} Values={values}"
        return {"request_raw": request_raw, "response_raw": response_raw, "values": values}
    except Exception as e:
        return {"request_raw": request_raw, "response_raw": None, "error": str(e)}


async def _simulate_opcua(
    container: Any, device: dict, operation: str, params: dict
) -> dict[str, Any]:
    """Simulate OPC UA read/write operation"""
    node_id = params.get("node_id", "ns=2;i=1")
    write_value = params.get("write_value")

    driver = None
    if hasattr(container, "driver_registry") and container.driver_registry:
        driver_cls = container.driver_registry.get("opcua")
        if driver_cls:
            driver = driver_cls(device.get("config", {}))

    if driver is None:
        return {"error": "OPC UA driver not available", "values": None}

    request_raw = f"NodeId={node_id} Op={operation}"
    try:
        if operation == "write" and write_value is not None:
            await driver.write(node_id, write_value)
            response_raw = f"Write OK: NodeId={node_id} Value={write_value}"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": {"written": write_value}}
        elif operation == "browse":
            result = await driver.browse(node_id)
            response_raw = f"Browse OK: {len(result) if isinstance(result, list) else 1} items"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": result}
        else:
            values = await driver.read(node_id)
            response_raw = f"Read OK: NodeId={node_id} Value={values}"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": values}
    except Exception as e:
        return {"request_raw": request_raw, "response_raw": None, "error": str(e)}


async def _simulate_mqtt(
    container: Any, device: dict, operation: str, params: dict
) -> dict[str, Any]:
    """Simulate MQTT publish/subscribe operation"""
    topic = params.get("topic", "test/debug")
    payload = params.get("payload", "")
    qos = int(params.get("qos", 0))

    request_raw = f"Topic={topic} QoS={qos} Payload={payload[:200]}"
    try:
        # MQTT publish via the driver if available
        driver = None
        if hasattr(container, "driver_registry") and container.driver_registry:
            driver_cls = container.driver_registry.get("mqtt_client")
            if driver_cls:
                driver = driver_cls(device.get("config", {}))

        if driver and hasattr(driver, "publish"):
            await driver.publish(topic, payload, qos)
            response_raw = f"Publish OK: Topic={topic}"
            return {"request_raw": request_raw, "response_raw": response_raw, "values": {"published": True}}
        return {"request_raw": request_raw, "error": "MQTT driver not available or publish not supported"}
    except Exception as e:
        return {"request_raw": request_raw, "response_raw": None, "error": str(e)}


async def _simulate_s7(
    container: Any, device: dict, operation: str, params: dict
) -> dict[str, Any]:
    """Simulate Siemens S7 read/write operation"""
    area = params.get("area", "DB")
    db_number = int(params.get("db_number", 1))
    start = int(params.get("start", 0))
    size = int(params.get("size", 1))

    request_raw = f"Area={area} DB={db_number} Start={start} Size={size}"
    driver = None
    if hasattr(container, "driver_registry") and container.driver_registry:
        driver_cls = container.driver_registry.get("s7")
        if driver_cls:
            driver = driver_cls(device.get("config", {}))

    if driver is None:
        return {"error": "S7 driver not available", "values": None}

    try:
        values = await driver.read(area, db_number, start, size)
        response_raw = f"Read OK: Area={area} DB={db_number} Values={values}"
        return {"request_raw": request_raw, "response_raw": response_raw, "values": values}
    except Exception as e:
        return {"request_raw": request_raw, "response_raw": None, "error": str(e)}


async def _simulate_generic(
    container: Any, device: dict, operation: str, params: dict
) -> dict[str, Any]:
    """Generic simulation for protocols without specific handlers"""
    request_raw = f"Op={operation} Params={params}"
    try:
        if operation == "read":
            points = device.get("points", [])
            if points:
                point_names = [p.get("name", "") for p in points]
                values = await container.device_service.read_points(device["device_id"], point_names)
                response_raw = f"Read OK: {values}"
                return {"request_raw": request_raw, "response_raw": response_raw, "values": values}
            return {"request_raw": request_raw, "error": "No points defined on device"}
        elif operation == "write":
            write_params = params.get("params", {})
            if write_params:
                for point_name, value in write_params.items():
                    await container.device_service.write_point(device["device_id"], point_name, value)
                response_raw = f"Write OK: {write_params}"
                return {"request_raw": request_raw, "response_raw": response_raw, "values": {"written": write_params}}
            return {"request_raw": request_raw, "error": "No write parameters provided"}
        else:
            return {"request_raw": request_raw, "error": f"Unsupported operation: {operation}"}
    except Exception as e:
        return {"request_raw": request_raw, "response_raw": None, "error": str(e)}


@router.get("/packets", response_model=ApiResponse)
async def get_recent_packets(
    protocol: str | None = None,
    device_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
) -> ApiResponse:
    """Get recent protocol packets for sniffing"""
    if protocol:
        buffer = _get_buffer(protocol)
    else:
        buffer = _get_buffer("__all__")

    packets = list(buffer)

    # Filter by device_id if specified
    if device_id:
        packets = [p for p in packets if p.get("device_id") == device_id]

    # Apply limit (return most recent)
    packets = packets[-limit:]

    return ApiResponse(data={"packets": packets, "total": len(packets)})


@router.delete("/packets", response_model=ApiResponse)
async def clear_packets(
    protocol: str | None = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
) -> ApiResponse:
    """Clear packet buffer"""
    if protocol:
        key = protocol
    else:
        key = "__all__"

    if key in _packet_buffers:
        count = len(_packet_buffers[key])
        _packet_buffers[key].clear()
        return ApiResponse(data={"cleared": count})

    return ApiResponse(data={"cleared": 0})


@router.get("/devices", response_model=ApiResponse)
async def list_debug_devices(
    protocol: str | None = None,
    user: CurrentUser = require_permission(Permission.SYSTEM_MANAGE),
) -> ApiResponse:
    """List devices available for debugging"""
    try:
        from edgelite.bootstrap import _app_state
        container = _app_state
    except ImportError:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)

    if not hasattr(container, "device_service") or container.device_service is None:
        raise HTTPException(503, CommonErrors.SERVICE_NOT_READY)

    try:
        devices = await container.device_service.list_devices()
        if protocol:
            devices = [d for d in devices if d.get("protocol") == protocol]
        result = [
            {
                "device_id": d.get("device_id", ""),
                "name": d.get("name", ""),
                "protocol": d.get("protocol", ""),
                "status": d.get("status", "unknown"),
            }
            for d in devices
        ]
        return ApiResponse(data={"devices": result})
    except Exception as e:
        logger.error("Failed to list debug devices: %s", e)
        raise HTTPException(500, CommonErrors.INTERNAL_ERROR) from None
