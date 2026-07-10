"""AI模型管理服务层"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from edgelite.engine.edge_ai_inference import AiInferenceEngine
from edgelite.models.ai_model import (
    AiInferenceLogORM,
    AiModelDetailResponse,
    AiModelResponse,
    AiStatsResponse,
)

logger = logging.getLogger(__name__)


class AiModelService:
    """AI模型管理服务"""

    def __init__(self, ai_engine: AiInferenceEngine, database=None):
        self._engine = ai_engine
        self._database = database
        self._inference_logs: list[dict] = []
        self._max_logs: int = 1000
        self._log_write_threshold: int = 3
        self._pending_db_logs: list[dict] = []
        self._db_counts: dict[str, int] = {}  # 从数据库恢复的推理计数缓存
        self._flush_task: asyncio.Task | None = None  # FIXED-P1: 持有刷写任务引用，防止被GC回收导致日志丢失
        # FIXED(严重): 原问题-_inference_logs/_pending_db_logs的append/截断/刷写任务创建无锁保护，并发推理导致日志丢失和刷写任务竞争;  # noqa: E501
        # 修复-添加asyncio.Lock保护所有日志操作
        self._log_lock = asyncio.Lock()

    async def restore_stats_from_db(self) -> None:
        """启动时从数据库恢复推理统计，避免重启后计数归零"""
        if not self._database:
            return
        try:
            from sqlalchemy import text

            # R8-S-01 修复(严重): 原代码无锁修改 _db_counts/_db_total_count/_db_error_counts/_db_avg_latency，
            # 与 list_models/inference 等并发读取这些计数器时可能读到部分写入的不一致状态。
            # 使用 _log_lock 保护读-改-写临界区。
            async with self._log_lock:
                async with self._database.session() as session:
                    result = await session.execute(
                        text("SELECT model_id, COUNT(*) as cnt FROM ai_inference_logs GROUP BY model_id")
                    )
                    for row in result:
                        self._db_counts[row[0]] = row[1]
                    # 恢复总调用数
                    total_result = await session.execute(text("SELECT COUNT(*) FROM ai_inference_logs"))
                    self._db_total_count: int = total_result.scalar() or 0
                    # 恢复错误数
                    error_result = await session.execute(
                        text(
                            "SELECT model_id, COUNT(*) as cnt FROM ai_inference_logs WHERE status='error' GROUP BY model_id"  # noqa: E501
                        )
                    )
                    self._db_error_counts: dict[str, int] = {}
                    for row in error_result:
                        self._db_error_counts[row[0]] = row[1]
                    # 恢复平均延迟
                    latency_result = await session.execute(
                        text(
                            "SELECT model_id, AVG(latency_ms) as avg_lat FROM ai_inference_logs WHERE status='success' GROUP BY model_id"  # noqa: E501
                        )
                    )
                    self._db_avg_latency: dict[str, float] = {}
                    for row in latency_result:
                        self._db_avg_latency[row[0]] = float(row[1]) if row[1] else 0.0
                    if self._db_counts:
                        logger.info("AI inference stats restored from DB: %s", self._db_counts)
        except Exception as e:
            logger.warning("Failed to restore AI inference stats from DB: %s", e)
            self._db_counts = {}
            self._db_error_counts = getattr(self, "_db_error_counts", {})
            self._db_avg_latency = getattr(self, "_db_avg_latency", {})
            self._db_total_count = getattr(self, "_db_total_count", 0)  # FIXED-P1: 异常路径也初始化_db_total_count

    async def list_models(self, page: int = 1, page_size: int = 20) -> dict:
        # R8-S-02 修复(严重): page=0 时 start=(0-1)*page_size 产生负数切片起点，
        # 返回末尾数据而非首页。强制 page 最小为 1。
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        models = self._engine.get_loaded_models()
        items = []
        for mid, wrapper in models.items():
            # FIXED: 原问题-list_models不返回inference_count，前端列表页所有模型推理次数显示0
            stats = self._engine.get_model_stats(mid)
            item = AiModelResponse(
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
            item["inference_count"] = (stats.get("call_count", 0) if stats else 0) + self._db_counts.get(mid, 0)
            item["error_count"] = (stats.get("error_count", 0) if stats else 0) + getattr(
                self, "_db_error_counts", {}
            ).get(mid, 0)
            item["avg_latency_ms"] = (
                stats.get("avg_latency_ms", 0) if stats else int(getattr(self, "_db_avg_latency", {}).get(mid, 0))
            )
            items.append(item)
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
            inference_count=(stats.get("call_count", 0) if stats else 0) + self._db_counts.get(model_id, 0),
            error_count=(stats.get("error_count", 0) if stats else 0)
            + getattr(self, "_db_error_counts", {}).get(model_id, 0),
            avg_latency_ms=stats.get("avg_latency_ms", 0)
            if stats
            else int(getattr(self, "_db_avg_latency", {}).get(model_id, 0)),
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
        # FIXED(严重): 原问题-update_model仅更新model_name/input_schema/output_schema，遗漏preprocess_config/postprocess_config/batch_size/max_concurrent/timeout_ms/device_preference;  # noqa: E501
        # 修复-补充缺失字段更新，preprocess/postprocess变更后重建pipeline
        if update_data.get("preprocess_config"):
            wrapper.preprocess_config = update_data["preprocess_config"]
            if wrapper.preprocess_config:
                from edgelite.engine.ai_preprocess import PreprocessPipeline

                wrapper._preprocess_pipeline = PreprocessPipeline(wrapper.preprocess_config)
            else:
                wrapper._preprocess_pipeline = None
        if update_data.get("postprocess_config"):
            wrapper.postprocess_config = update_data["postprocess_config"]
            if wrapper.postprocess_config:
                from edgelite.engine.ai_postprocess import PostprocessPipeline

                wrapper._postprocess_pipeline = PostprocessPipeline(wrapper.postprocess_config)
            else:
                wrapper._postprocess_pipeline = None
        if update_data.get("batch_size"):
            wrapper.batch_size = update_data["batch_size"]
        if update_data.get("max_concurrent"):
            wrapper.max_concurrent = update_data["max_concurrent"]
        if update_data.get("timeout_ms"):
            wrapper.timeout_ms = update_data["timeout_ms"]
        if update_data.get("device_preference"):
            wrapper.device_preference = update_data["device_preference"]
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
        try:
            from edgelite.engine.edge_ai_inference import _check_onnxruntime

            if not _check_onnxruntime():
                logger.warning("ONNX Runtime not available, marking AI service as disabled for model %s", model_id)
                wrapper.status = "inactive"
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=503,
                    detail="ERR_AI_ONNXRUNTIME_NOT_INSTALLED: onnxruntime is not installed. Run: pip install onnxruntime",  # noqa: E501
                )
        except ImportError:
            pass
        ok, reason = await self._engine.enable_model(model_id)
        if not ok and reason:
            from fastapi import HTTPException

            # 根据错误码映射到合适的HTTP状态码
            if reason == "ERR_AI_ONNXRUNTIME_NOT_INSTALLED":
                raise HTTPException(
                    status_code=503,
                    detail=f"{reason}: onnxruntime is not installed. Run: pip install onnxruntime",
                )
            raise HTTPException(status_code=400, detail=reason)
        return ok

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
            logger.error("AI model hot-reload failed: %s - %s", model_id, e)
            return False

    async def inference(
        self, model_id: str, input_data: list[float], device_id: str | None = None, point_name: str | None = None
    ) -> dict:
        result = await self._engine.infer(model_id, input_data)
        log_id = str(uuid.uuid4())
        wrapper = self._engine.get_model(model_id)
        model_name = wrapper.model_name if wrapper else model_id
        input_summary = str(input_data[:5]) + ("..." if len(input_data) > 5 else "")
        output_summary = str(result.output_data)[:200]
        return_dict = {
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
        # Persist inference log
        log_entry = return_dict.get("log", {})
        # FIXED(严重): 原问题-_inference_logs/_pending_db_logs的append/截断/刷写任务创建无锁保护，并发推理导致日志丢失和刷写任务竞争;  # noqa: E501
        # 修复-使用_log_lock保护所有日志操作
        async with self._log_lock:
            self._inference_logs.append(log_entry)
            if len(self._inference_logs) > self._max_logs:
                self._inference_logs = self._inference_logs[-self._max_logs :]
            self._pending_db_logs.append(log_entry)
            if len(self._pending_db_logs) >= self._log_write_threshold:
                # FIXED-P1: 原问题-create_task返回值未持有引用，可能被Python GC回收导致日志刷写永不执行
                self._flush_task = asyncio.get_running_loop().create_task(self._flush_logs_to_db())
        return return_dict

    async def _flush_logs_to_db(self) -> None:
        # FIXED(严重): 原问题-_pending_db_logs的读取/清除/重入无锁保护，并发刷写任务竞争同一批日志;
        # 修复-使用_log_lock保护快照与清除(不在DB I/O期间持锁)，失败时重新入队也加锁
        async with self._log_lock:
            if not self._database or not self._pending_db_logs:
                return
            logs_to_write = self._pending_db_logs[:]
            self._pending_db_logs.clear()
        try:
            async with self._database.session() as session:
                for log in logs_to_write:
                    orm = AiInferenceLogORM(
                        log_id=log.get("log_id", str(uuid.uuid4())),
                        model_id=log.get("model_id", ""),
                        model_name=log.get("model_name", ""),
                        device_id=log.get("device_id"),
                        point_name=log.get("point_name"),
                        input_summary=log.get("input_summary", "")[:256],
                        output_summary=log.get("output_summary", "")[:256],
                        latency_ms=log.get("latency_ms", 0),
                        status=log.get("status", "success"),
                        error_message=log.get("error_message"),
                    )
                    session.add(orm)
                await session.commit()
        except Exception as e:
            logger.warning("Failed to flush inference logs to DB: %s", e)
            # FIXED-P1: 原问题-DB刷写失败时logs_to_write已从_pending_db_logs清除，日志永久丢失。重新入队以供下次重试
            async with self._log_lock:
                # FIX-P1: 失败日志(logs_to_write，较旧) prepend 到 pending 列表前部，
                # 保证下次重试优先写入。原截断 [-_max_logs:] 保留尾部新日志、丢弃
                # 头部重试的旧日志，与重试初衷相反；改为 [:_max_logs] 保留头部
                # 重试日志，丢弃超出上限的尾部新日志。
                self._pending_db_logs = logs_to_write + self._pending_db_logs
                if len(self._pending_db_logs) > self._max_logs:
                    self._pending_db_logs = self._pending_db_logs[: self._max_logs]

    async def get_stats(self) -> AiStatsResponse:
        snapshot = self._engine.get_stats()
        models = self._engine.get_loaded_models()
        db_total = sum(self._db_counts.values())
        db_errors = sum(getattr(self, "_db_error_counts", {}).values())
        return AiStatsResponse(
            model_count=len(models),
            total_calls=snapshot.get("total_calls", 0) + db_total,
            total_errors=snapshot.get("total_errors", 0) + db_errors,
            avg_latency_ms=snapshot.get("avg_latency_ms", 0),
            model_distribution=snapshot.get("model_distribution", {}),
        )

    async def get_inference_summary(self) -> dict:
        snapshot = self._engine.get_stats()
        models = self._engine.get_loaded_models()
        active_count = sum(1 for mw in models.values() if mw.status == "active")
        scheduled = self._engine.get_scheduled_inferences()
        # FIXED(严重): 原问题-_inference_logs读取无锁保护，并发推理时列表可能被截断导致读取不一致;
        # 修复-在锁内快照日志副本，锁外处理
        async with self._log_lock:
            logs_snapshot = list(self._inference_logs)
        recent_logs = logs_snapshot[-10:] if logs_snapshot else []
        recent_summary = []
        for log in recent_logs:
            recent_summary.append(
                {
                    "model_id": log.get("model_id", ""),
                    "model_name": log.get("model_name", ""),
                    "device_id": log.get("device_id", ""),
                    "latency_ms": log.get("latency_ms", 0),
                    "status": log.get("status", ""),
                    "timestamp": log.get("timestamp", ""),
                }
            )
        latency_trend = []
        for log in logs_snapshot[-50:]:
            if log.get("status") == "success":
                latency_trend.append(
                    {
                        "t": log.get("timestamp", ""),
                        "v": log.get("latency_ms", 0),
                    }
                )
        anomaly_count = sum(1 for log in logs_snapshot[-50:] if log.get("status") == "success")
        db_total = sum(self._db_counts.values())
        db_errors = sum(getattr(self, "_db_error_counts", {}).values())
        return {
            "model_count": len(models),
            "active_model_count": active_count,
            "total_calls": snapshot.get("total_calls", 0) + db_total,
            "total_errors": snapshot.get("total_errors", 0) + db_errors,
            "avg_latency_ms": snapshot.get("avg_latency_ms", 0),
            "model_distribution": snapshot.get("model_distribution", {}),
            "active_schedule_count": len(scheduled),
            "recent_inferences": recent_summary,
            "latency_trend": latency_trend,
            "anomaly_count": anomaly_count,
        }

    async def get_model_stats(self, model_id: str) -> dict | None:
        return self._engine.get_model_stats(model_id)

    async def get_inference_logs(self, model_id: str | None = None, page: int = 1, page_size: int = 20) -> dict:
        # FIXED(严重): 原问题-_inference_logs读取无锁保护，并发推理时列表可能被截断导致分页不一致;
        # 修复-在锁内快照日志副本
        async with self._log_lock:
            logs = list(self._inference_logs)
        if model_id:
            logs = [l for l in logs if l.get("model_id") == model_id]
        total = len(logs)
        start = (page - 1) * page_size
        end = start + page_size
        return {"items": logs[start:end], "total": total, "page": page, "page_size": page_size}

    async def register_uploaded_model(self, name: str, file_path: str) -> str:
        """Register an uploaded model file with the AI engine"""
        # FIXED-P2: 原问题-uuid已在文件顶部导入，此处重复import是冗余的
        model_id = f"custom_{name}_{uuid.uuid4().hex[:8]}"
        # FIXED(严重): 原问题-硬编码version="1.0"不匹配^v\d+\.\d+\.\d+$模式，导致_auto_increment_version()无法递增; 硬编码type="onnx"，上传.tflite/.pmml时模型类型标记错误;  # noqa: E501
        # 修复-版本改为v1.0.0匹配版本模式; 根据扩展名推断类型
        ext = Path(file_path).suffix.lower()
        model_type = {".onnx": "onnx", ".tflite": "tflite", ".pmml": "pmml"}.get(ext, "onnx")
        wrapper = await self._engine.load_custom_model(
            model_id=model_id,
            model_name=name,
            model_version="v1.0.0",
            model_type=model_type,
            model_path=file_path,
            input_schema={"shape": [1, -1], "dtype": "float32"},
            output_schema={"shape": [1], "dtype": "float32"},
        )
        if wrapper is None:
            raise RuntimeError(f"Failed to load model from {file_path}")
        return model_id

    async def shutdown(self) -> None:
        """关闭时强制刷写未持久化的推理日志"""
        # FIXED(严重): 原问题-_pending_db_logs读取无锁保护，并发推理可能仍在追加日志;
        # 修复-在锁内检查待刷写日志
        async with self._log_lock:
            pending_count = len(self._pending_db_logs)
        if pending_count:
            logger.info("Flushing %d pending inference logs before shutdown", pending_count)
            await self._flush_logs_to_db()
