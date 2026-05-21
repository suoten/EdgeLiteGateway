"""MQTT北向转发离线缓存API"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/mqtt", tags=["MQTT Forwarder"])


@router.get("/offline-queue/status")
async def get_offline_queue_status(request: Request):
    """获取MQTT离线缓存队列状态"""
    forwarder = getattr(request.app.state, "mqtt_forwarder", None)
    if not forwarder:
        return {
            "enabled": False,
            "pending_count": 0,
            "sent_count": 0,
            "oldest_timestamp": None,
            "db_size_bytes": 0,
        }
    return forwarder.get_offline_queue_status()
