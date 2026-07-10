"""MCP (Model Context Protocol) 服务

提供AI助手与EdgeLite网关交互的标准协议接口。
包含工具调用、资源访问、提示模板、SSE推送和密钥管理等核心能力。
业务逻辑从API层解耦到此处。
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from edgelite.api.error_codes import McpErrors
from edgelite.constants import _MCP_QUERY_SIZE

logger = logging.getLogger(__name__)


class MCPToolService:
    """MCP工具调用服务"""

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._resources: dict[str, dict] = {}
        self._prompts: dict[str, dict] = {}
        # FIXED: AI 依赖注入入口，bootstrap 启动时注入，解决 4 个 AI 工具永远不可用问题 [2026-06-29]
        self._ai_scheduler: Any = None
        self._anomaly_learner: Any = None
        self._threshold_learner: Any = None
        self._register_tools()
        self._register_resources()
        self._register_prompts()

    def set_ai_dependencies(
        self,
        ai_scheduler: Any = None,
        anomaly_learner: Any = None,
        threshold_learner: Any = None,
    ) -> None:
        """FIXED: 注入 AI 推理调度器和自学习器实例，使 4 个 AI MCP 工具可用 [2026-06-29]

        在 bootstrap_ai 中调用。参数为 None 时表示对应依赖不可用（如 onnxruntime 未安装），
        MCP 工具会返回 503 而非永远静默失败。
        """
        if ai_scheduler is not None:
            self._ai_scheduler = ai_scheduler
        if anomaly_learner is not None:
            self._anomaly_learner = anomaly_learner
        if threshold_learner is not None:
            self._threshold_learner = threshold_learner

    def _register_tools(self):
        self._tools = {
            "list_devices": {
                "name": "list_devices",
                "description": "Get all devices list",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "get_device_status": {
                "name": "get_device_status",
                "description": "Get the running status of a specific device",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "string", "description": "Device ID"}
                    },  # FIXED: 原问题-中文硬编码description
                    "required": ["device_id"],
                },
            },
            "read_device_points": {
                "name": "read_device_points",
                "description": "Read current values of device points",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "string", "description": "Device ID"}
                    },  # FIXED: 原问题-中文硬编码description
                    "required": ["device_id"],
                },
            },
            "write_device_point": {
                "name": "write_device_point",
                "description": "Write a value to a device point",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "string"},
                        "point_name": {"type": "string"},
                        "value": {"type": "number"},
                    },
                    "required": ["device_id", "point_name", "value"],
                },
            },
            "list_alarms": {
                "name": "list_alarms",
                "description": "Get current active alarms list",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "description": "Filter by alarm severity"}
                    },  # FIXED: 原问题-中文硬编码description
                    "required": [],
                },
            },
            "get_system_status": {
                "name": "get_system_status",
                "description": "Get system running status",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "list_rules": {
                "name": "list_rules",
                "description": "Get all rules list",  # FIXED: 原问题-中文硬编码description
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "ai_inference": {
                "name": "ai_inference",
                "description": "Run AI inference on specified model with input data",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "model_id": {
                            "type": "string",
                            "description": "Model ID (elg-anomaly-v1, elg-trend-v1, elg-threshold-v1)",
                        },
                        "input_data": {"type": "array", "items": {"type": "number"}, "description": "Input data array"},
                    },
                    "required": ["model_id", "input_data"],
                },
            },
            "ai_model_status": {
                "name": "ai_model_status",
                "description": "Get AI model status and metrics",
                "inputSchema": {
                    "type": "object",
                    "properties": {"model_id": {"type": "string", "description": "Model ID (optional, omit for all)"}},
                    "required": [],
                },
            },
            "ai_anomaly_history": {
                "name": "ai_anomaly_history",
                "description": "Get recent anomaly detection results",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "string", "description": "Filter by device ID"},
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": [],
                },
            },
            "ai_submit_feedback": {
                "name": "ai_submit_feedback",
                "description": "Submit feedback for AI inference result",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "model_id": {"type": "string", "description": "Model ID"},
                        "feedback_type": {
                            "type": "string",
                            "description": "Feedback type (confirmed/ignored/too_sensitive/too_insensitive)",
                        },
                    },
                    "required": ["model_id", "feedback_type"],
                },
            },
        }

    def _register_resources(self):
        self._resources = {
            "devices": {
                "uri": "edgelite://devices",
                "name": "Device List",  # FIXED: 原问题-中文硬编码description
                "description": "Summary of all registered devices",  # FIXED: 原问题-中文硬编码description
                "mimeType": "application/json",
            },
            "alarms/active": {
                "uri": "edgelite://alarms/active",
                "name": "Active Alarms",  # FIXED: 原问题-中文硬编码description
                "description": "List of currently unacknowledged alarms",  # FIXED: 原问题-中文硬编码description
                "mimeType": "application/json",
            },
            "system/status": {
                "uri": "edgelite://system/status",
                "name": "System Status",  # FIXED: 原问题-中文硬编码description
                "description": "System running status summary",  # FIXED: 原问题-中文硬编码description
                "mimeType": "application/json",
            },
            "ai/models": {
                "uri": "edgelite://ai/models",
                "name": "AI Models",
                "description": "AI model status and performance metrics",
                "mimeType": "application/json",
            },
            "ai/inference/recent": {
                "uri": "edgelite://ai/inference/recent",
                "name": "Recent AI Inferences",
                "description": "Recent AI inference results across all models",
                "mimeType": "application/json",
            },
            "ai/anomalies": {
                "uri": "edgelite://ai/anomalies",
                "name": "Anomaly History",
                "description": "Recent anomaly detection results",
                "mimeType": "application/json",
            },
        }

    def _register_prompts(self):
        self._prompts = {
            "analyze_device": {
                "name": "analyze_device",
                "description": "Analyze device running status and anomalies",  # FIXED: 原问题-中文硬编码description
                "arguments": [
                    {
                        "name": "device_id",
                        "description": "Device ID to analyze",
                        "required": True,
                    }  # FIXED: 原问题-中文硬编码description
                ],
            },
            "alarm_summary": {
                "name": "alarm_summary",
                "description": "Generate alarm summary report",  # FIXED: 原问题-中文硬编码description
                "arguments": [
                    {
                        "name": "severity",
                        "description": "Filter by alarm severity",
                        "required": False,
                    }  # FIXED: 原问题-中文硬编码description
                ],
            },
        }

    @property
    def tools(self) -> dict[str, dict]:
        return self._tools

    @property
    def resources(self) -> dict[str, dict]:
        return self._resources

    @property
    def prompts(self) -> dict[str, dict]:
        return self._prompts

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        device_service=None,
        alarm_service=None,
        system_service=None,
        rule_service=None,
        user: dict | None = None,
        audit_svc=None,
    ) -> Any:
        from fastapi import HTTPException

        args = arguments or {}
        try:
            if name == "list_devices":
                if not device_service:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.DEVICE_SERVICE_UNAVAILABLE
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                devices, total = await device_service.list_devices(
                    page=1, size=_MCP_QUERY_SIZE
                )  # FIXED: 原问题-size=200魔法数字
                return {"devices": devices, "total": total}

            elif name == "get_device_status":
                device_id = args.get("device_id", "")
                if not device_id:
                    raise HTTPException(
                        status_code=400, detail=McpErrors.MISSING_DEVICE_ID
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                if not device_service:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.DEVICE_SERVICE_UNAVAILABLE
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                device = await device_service.get_device(device_id)
                if device is None:
                    raise HTTPException(
                        status_code=404, detail=McpErrors.DEVICE_NOT_FOUND
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                return device

            elif name == "read_device_points":
                device_id = args.get("device_id", "")
                if not device_id:
                    raise HTTPException(
                        status_code=400, detail=McpErrors.MISSING_DEVICE_ID
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                if not device_service:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.DEVICE_SERVICE_UNAVAILABLE
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                points = await device_service.read_points(device_id)
                return {"device_id": device_id, "points": points}

            elif name == "write_device_point":
                device_id = args.get("device_id", "")
                point_name = args.get("point_name", "")
                value = args.get("value", 0)
                # FIXED: 原问题-MCP写入值未做类型校验，AI传入字符串时驱动行为不可预测
                if isinstance(value, str):
                    try:
                        value = float(value)
                        if value == int(value):
                            value = int(value)
                    except (ValueError, TypeError) as exc:  # FIXED(P3): 原问题-B904 异常链丢失; 修复-添加 from exc
                        low_val = value.lower()
                        if low_val in (
                            "true",
                            "on",
                            "yes",
                            "1",
                        ):  # FIXED-P2: 字符串值转换失败时pass保留原始字符串传给驱动，现添加bool值识别
                            value = True
                        elif low_val in ("false", "off", "no", "0"):
                            value = False
                        else:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Invalid value type: cannot convert '{value}' to number or boolean",
                            ) from exc
                if not device_id or not point_name:
                    raise HTTPException(
                        status_code=400, detail=McpErrors.MISSING_PARAMS
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                if not device_service:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.DEVICE_SERVICE_UNAVAILABLE
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                # SEC-FIX: MCP 写入必须遵守驱动写保护策略，不得绕过 check_write_allowed
                driver = getattr(device_service, "_driver_instances", {}).get(device_id)
                if driver is not None and hasattr(driver, "check_write_allowed"):
                    try:
                        allowed = driver.check_write_allowed(device_id, point_name)
                    except Exception as ce:
                        allowed = False
                        logger.warning("[mcp] check_write_allowed raised: %s", ce)
                    if not allowed:
                        raise HTTPException(
                            status_code=403,
                            detail="ERR_WRITE_NOT_ALLOWED: point blocked by write policy",
                        )
                # SEC-FIX: 传入 user 上下文，使驱动层 RBAC 与审计 user 字段生效
                mcp_user = {
                    "username": f"mcp:{user.get('username', 'unknown')}" if user else "mcp:system",
                    "role": user.get("role", "admin") if user else "admin",
                    "source": "mcp",
                }
                await device_service.write_point(device_id, point_name, value, user=mcp_user)
                # SEC-FIX: MCP 写入审计日志记录
                try:
                    if audit_svc is None:
                        from edgelite.app import _app_state

                        audit_svc = getattr(_app_state, "audit_service", None)
                    if audit_svc is not None:
                        from edgelite.services.audit_service import AuditAction

                        await audit_svc.log(
                            action=AuditAction.DEVICE_WRITE_POINT,
                            user_id=user.get("user_id", "mcp") if user else "mcp",
                            username=mcp_user["username"],
                            resource_type="device_point",
                            resource_id=f"{device_id}/{point_name}",
                            after_value={"point": point_name, "value": value, "source": "mcp"},
                        )
                except Exception as e:
                    logger.warning("[mcp] Write audit log failed: %s", e)
                return {"success": True, "device_id": device_id, "point_name": point_name, "value": value}

            elif name == "list_alarms":
                severity = args.get("severity")
                if not alarm_service:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.ALARM_SERVICE_UNAVAILABLE
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                alarms, total = await alarm_service.list_alarms(
                    page=1, size=_MCP_QUERY_SIZE, severity=severity
                )  # FIXED: 原问题-size=200魔法数字
                return {"alarms": alarms, "total": total}

            elif name == "get_system_status":
                if not system_service:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.SYSTEM_SERVICE_UNAVAILABLE
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                return await system_service.get_status()

            elif name == "list_rules":
                if not rule_service:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.RULE_SERVICE_UNAVAILABLE
                    )  # FIXED: 原问题-中文硬编码detail，改为error_code
                rules, total = await rule_service.list_rules(
                    page=1, size=_MCP_QUERY_SIZE
                )  # FIXED: 原问题-size=200魔法数字
                return {"rules": rules, "total": total}

            elif name == "ai_inference":
                model_id = args.get("model_id", "")
                input_data = args.get("input_data", [])
                if not model_id:
                    raise HTTPException(
                        status_code=400, detail=McpErrors.MISSING_MODEL_ID
                    )  # FIXED-P2: 原问题-硬编码字符串，改为错误码
                ai_scheduler = getattr(self, "_ai_scheduler", None)
                if not ai_scheduler:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.AI_SCHEDULER_UNAVAILABLE
                    )  # FIXED-P2: 原问题-硬编码字符串，改为错误码
                from edgelite.engine.inference_scheduler import InferencePriority

                result = await ai_scheduler.submit_and_wait(model_id, input_data, InferencePriority.USER_QUERY)
                output = result
                if hasattr(result, "__dict__"):
                    output = {k: v for k, v in result.__dict__.items() if not k.startswith("_")}
                return {"model_id": model_id, "result": output}

            elif name == "ai_model_status":
                model_id = args.get("model_id", "")
                ai_scheduler = getattr(self, "_ai_scheduler", None)
                if not ai_scheduler:
                    raise HTTPException(
                        status_code=503, detail=McpErrors.AI_SCHEDULER_UNAVAILABLE
                    )  # FIXED-P2: 原问题-硬编码字符串，改为错误码
                if model_id:
                    metrics = await ai_scheduler.get_model_metrics(model_id)
                    if not metrics:
                        raise HTTPException(
                            status_code=404, detail=McpErrors.MODEL_NOT_FOUND
                        )  # FIXED-P2: 原问题-硬编码字符串，改为错误码
                    return metrics
                return await ai_scheduler.get_stats()

            elif name == "ai_anomaly_history":
                device_id = args.get("device_id", "")
                limit = args.get("limit", 20)
                anomaly_learner = getattr(self, "_anomaly_learner", None)
                if not anomaly_learner:
                    return {"anomalies": [], "total": 0}
                dashboard = anomaly_learner.get_dashboard() if hasattr(anomaly_learner, "get_dashboard") else {}
                recent = dashboard.get("recent_anomalies", [])
                if device_id:
                    recent = [a for a in recent if a.get("device_id") == device_id]
                return {"anomalies": recent[:limit], "total": len(recent)}

            elif name == "ai_submit_feedback":
                model_id = args.get("model_id", "")
                feedback_type = args.get("feedback_type", "")
                if not model_id or not feedback_type:
                    raise HTTPException(
                        status_code=400, detail=McpErrors.MISSING_FEEDBACK_PARAMS
                    )  # FIXED-P2: 原问题-硬编码字符串，改为错误码
                learner = None
                anomaly_learner = getattr(self, "_anomaly_learner", None)
                threshold_learner = getattr(self, "_threshold_learner", None)
                if model_id == "elg-anomaly-v1" and anomaly_learner:
                    learner = anomaly_learner
                elif model_id == "elg-threshold-v1" and threshold_learner:
                    learner = threshold_learner
                if not learner:
                    raise HTTPException(
                        status_code=404, detail=McpErrors.MODEL_NOT_FOUND
                    )  # FIXED-P2: 原问题-硬编码字符串，改为错误码
                if model_id == "elg-anomaly-v1":
                    result = await learner.submit_feedback(value=0, score=0, is_anomaly=False, feedback=feedback_type)
                else:
                    result = await learner.submit_feedback(feedback_type=feedback_type)
                return {"status": "ok", "result": result}

            else:
                raise HTTPException(
                    status_code=400, detail=McpErrors.UNKNOWN_TOOL
                )  # FIXED: 原问题-中文硬编码detail，改为error_code

        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCP tool call failed %s: %s", name, e)
            raise HTTPException(status_code=500, detail=McpErrors.CALL_FAILED) from e

    def validate_tool_call(self, name: str, arguments: dict[str, Any] | None) -> list[str]:
        from fastapi import HTTPException

        if name not in self._tools:
            raise HTTPException(
                status_code=400, detail=McpErrors.UNKNOWN_TOOL
            )  # FIXED: 原问题-中文硬编码detail，改为error_code

        tool_def = self._tools[name]
        input_schema = tool_def.get("inputSchema") if isinstance(tool_def, dict) else None
        errors = []
        if input_schema and isinstance(input_schema, dict):
            required = input_schema.get("required", [])
            properties = input_schema.get("properties", {})
            args = arguments or {}
            for field_name in required:
                if field_name not in args:
                    errors.append(f"Missing required parameter: {field_name}")  # FIXED: 原问题-中文硬编码description
            for key in args:
                if key not in properties:
                    errors.append(f"Unknown parameter: {key}")  # FIXED: 原问题-中文硬编码description
        return errors


class MCPAuthManager:
    """MCP API密钥管理"""

    _STORE_FILE = Path("data/mcp_keys.json")

    def __init__(self):
        self._enabled = False
        self._keys: dict[str, dict[str, Any]] = {}
        # R8-S-07 修复(严重): 保护 _keys 的并发读写。
        # verify_key 迭代 _keys 时与 create_key/delete_key 并发会引发
        # "dictionary changed size during iteration"。
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            if self._STORE_FILE.exists():
                with open(self._STORE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                with self._lock:
                    self._keys = data.get("keys", {})
                    self._enabled = data.get("enabled", False)
        except Exception as e:
            logger.warning("加载MCP密钥文件失败: %s", e)
            with self._lock:
                self._keys = {}
                self._enabled = False

    def _save(self):
        try:
            self._STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
            # FIXED-P2: MCP API密钥明文存储，保存时对key字段做SHA256哈希，验证时比对哈希
            # R8-S-07: 锁内快照 _keys 后释放锁，再执行文件 I/O（耗时操作不持锁）
            with self._lock:
                keys_snapshot = {k: dict(v) for k, v in self._keys.items()}
                enabled = self._enabled
            safe_keys = {}
            for k, v in keys_snapshot.items():
                safe_v = dict(v)
                if "key" in safe_v:
                    import hashlib

                    safe_v["key_hash"] = hashlib.sha256(safe_v.pop("key").encode()).hexdigest()
                safe_keys[k] = safe_v
            with open(self._STORE_FILE, "w", encoding="utf-8") as f:
                json.dump({"keys": safe_keys, "enabled": enabled}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存MCP密钥文件失败: %s", e)

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def list_keys(self) -> list[dict[str, Any]]:
        # FIXED-P1: 列表接口遮蔽API Key，仅显示前4位+***
        # R8-S-07: 锁内快照后释放锁，避免迭代时并发修改
        with self._lock:
            items = list(self._keys.items())
        return [
            {
                "id": k,
                "name": v["name"],
                "key": self._mask_key(v.get("key", "")),
                "scopes": v["scopes"],
                "created_at": v.get("created_at", ""),
            }
            for k, v in items
        ]

    @staticmethod
    def _mask_key(key: str) -> str:
        """遮蔽API Key，仅保留前4位"""
        if not key or len(key) <= 8:
            return "****"
        return key[:4] + "****"

    def create_key(self, name: str, scopes: list[str]) -> dict[str, Any]:
        key_id = str(uuid.uuid4())[:8]
        new_api_key = f"mcp_{uuid.uuid4().hex[:32]}"
        with self._lock:
            self._keys[key_id] = {
                "name": name,
                "scopes": scopes,
                "key": new_api_key,
                "created_at": datetime.now(UTC).isoformat(),
            }
            self._enabled = True
        self._save()
        return {"id": key_id, "name": name, "key": new_api_key, "scopes": scopes}

    def delete_key(self, key_id: str) -> bool:
        with self._lock:
            if key_id not in self._keys:
                return False
            del self._keys[key_id]
            self._enabled = bool(self._keys)
        self._save()
        return True

    def verify_key(self, api_key: str) -> dict[str, Any] | None:
        import hashlib

        # R8-S-07: 锁内快照后释放锁，避免迭代时与 create/delete 竞态
        with self._lock:
            items = list(self._keys.items())
        for key_id, key_data in items:
            name = key_data.get("name")
            scopes = key_data.get("scopes")
            if name is None or scopes is None:
                continue
            # FIXED-P0: 原问题-_save将key哈希为key_hash存盘，但verify_key仅比对明文key。重启后从文件加载的keys只有key_hash无key，导致所有API密钥验证失败  # noqa: E501
            # 情况1: 内存中的明文key（创建后未重启）
            stored_key = key_data.get("key", "")
            if stored_key and hmac.compare_digest(stored_key, api_key):
                return {"id": key_id, "name": name, "scopes": scopes}
            # 情况2: 从文件恢复的key_hash（重启后）
            stored_hash = key_data.get("key_hash", "")
            if stored_hash:
                input_hash = hashlib.sha256(api_key.encode()).hexdigest()
                if hmac.compare_digest(stored_hash, input_hash):
                    return {"id": key_id, "name": name, "scopes": scopes}
        return None
