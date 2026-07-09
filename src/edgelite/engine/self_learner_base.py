"""自学习器基类：数据收集 → 训练 → ONNX 导出 → 原子替换 → 热重载

FIXED: 2026-06-30 项2 - 自学习器 ONNX 回写闭环
之前预置 AI 模型（elg-anomaly-v1, elg-trend-v1, elg-threshold-v1）使用随机权重，
无法从实际运行数据中学习。本模块提供标准化的训练-导出-替换-重载闭环：
1. 收集设备运行数据到内存缓冲区
2. 达到训练阈值后触发训练（纯 numpy 实现，无 sklearn/torch 依赖）
3. 将训练后的权重构造为 ONNX 图（手动构造，无 skl2onnx 依赖）
4. 原子写入模型文件（tempfile + os.replace，防止崩溃留下半截文件）
5. 触发 AiInferenceEngine 热重载（reload_model），不中断在线推理

子类需实现：
- _build_onnx_graph(weights) -> bytes: 构造 ONNX 模型字节
- _train(data) -> dict: 从数据训练权重，返回权重字典
- _default_weights() -> dict: 返回默认（未训练）权重
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 训练触发阈值
_MIN_TRAIN_SAMPLES = 100  # 最少样本数才允许训练
_MAX_BUFFER_SIZE = 10000  # 缓冲区最大容量（FIFO 淘汰）
_AUTO_TRAIN_THRESHOLD = 500  # 达到此样本数自动触发训练


class SelfLearnerBase:
    """自学习器基类：管理数据收集、训练、ONNX 导出与热重载

    线程安全：
    - _data_lock (threading.Lock): 保护数据缓冲区的同步读写
    - _train_lock (asyncio.Lock): 确保同一时刻只有一个训练任务运行
    """

    def __init__(
        self,
        model_id: str,
        model_file: str,
        models_dir: str | Path,
        ai_engine: Any = None,
        *,
        min_samples: int = _MIN_TRAIN_SAMPLES,
        max_buffer: int = _MAX_BUFFER_SIZE,
        auto_train_threshold: int = _AUTO_TRAIN_THRESHOLD,
    ):
        self.model_id = model_id
        self.model_file = model_file
        self.models_dir = Path(models_dir)
        self.ai_engine = ai_engine

        self.min_samples = min_samples
        self.max_buffer = max_buffer
        self.auto_train_threshold = auto_train_threshold

        # 数据缓冲区（线程安全的 deque， maxlen 自动 FIFO 淘汰）
        self._data_buffer: deque[Any] = deque(maxlen=max_buffer)
        self._data_lock = threading.Lock()

        # 训练锁（异步，防止并发训练）
        self._train_lock = asyncio.Lock()

        # 状态跟踪
        self._last_train_time: datetime | None = None
        self._train_count = 0
        self._last_train_error: str | None = None
        self._last_train_sample_count = 0
        self._feedback_history: list[dict[str, Any]] = []
        self._feedback_lock = threading.Lock()
        self._recent_results: list[dict[str, Any]] = []  # 最近的推理/检测结果

    # ------------------------------------------------------------------
    # 数据收集
    # ------------------------------------------------------------------

    def add_sample(self, sample: Any) -> None:
        """添加训练样本到缓冲区（线程安全）

        sample 应为可被 _train 解析的格式（通常为 numpy 数组或 dict）。
        缓冲区满时自动 FIFO 淘汰最旧样本。
        """
        with self._data_lock:
            self._data_buffer.append(sample)

    def add_samples(self, samples: list[Any]) -> None:
        """批量添加训练样本"""
        with self._data_lock:
            for s in samples:
                self._data_buffer.append(s)

    def get_sample_count(self) -> int:
        """返回当前缓冲区样本数"""
        with self._data_lock:
            return len(self._data_buffer)

    def get_buffer_snapshot(self) -> list[Any]:
        """返回缓冲区快照（拷贝，不影响原缓冲区）"""
        with self._data_lock:
            return list(self._data_buffer)

    def clear_buffer(self) -> None:
        """清空训练缓冲区"""
        with self._data_lock:
            self._data_buffer.clear()

    # ------------------------------------------------------------------
    # 训练与导出
    # ------------------------------------------------------------------

    async def train_and_export(self, force: bool = False) -> dict[str, Any]:
        """训练模型并导出 ONNX，原子替换后触发热重载

        Args:
            force: True 时即使样本不足也强制训练（用于测试）

        Returns:
            训练结果摘要 dict

        Raises:
            RuntimeError: 训练或导出失败
        """
        async with self._train_lock:
            sample_count = self.get_sample_count()
            if not force and sample_count < self.min_samples:
                logger.info(
                    "%s: skip training, only %d samples (need %d)",
                    self.model_id, sample_count, self.min_samples,
                )
                return {
                    "model_id": self.model_id,
                    "status": "skipped",
                    "reason": f"insufficient samples ({sample_count}/{self.min_samples})",
                    "sample_count": sample_count,
                }

            # 快照数据（不持锁训练，避免阻塞 add_sample）
            data = self.get_buffer_snapshot()
            if not data:
                return {
                    "model_id": self.model_id,
                    "status": "skipped",
                    "reason": "empty buffer",
                    "sample_count": 0,
                }

            train_start = time.monotonic()
            try:
                # 子类实现训练逻辑（纯 numpy 计算）
                weights = self._train(data)
                if weights is None:
                    raise RuntimeError("training returned None")

                # 构造 ONNX 模型字节
                model_bytes = self._build_onnx_graph(weights)
                if model_bytes is None:
                    raise RuntimeError("ONNX graph construction returned None")

                # 原子写入模型文件
                model_path = self._atomic_write_model(model_bytes)

                # 触发热重载
                reload_status = await self._trigger_reload(model_path)

                train_duration_ms = int((time.monotonic() - train_start) * 1000)
                self._last_train_time = datetime.now(UTC)
                self._train_count += 1
                self._last_train_sample_count = len(data)
                self._last_train_error = None

                logger.info(
                    "%s: training completed (samples=%d, duration=%dms, path=%s, reload=%s)",
                    self.model_id, len(data), train_duration_ms,
                    model_path, reload_status,
                )
                return {
                    "model_id": self.model_id,
                    "status": "success",
                    "sample_count": len(data),
                    "duration_ms": train_duration_ms,
                    "model_path": str(model_path),
                    "model_size_bytes": len(model_bytes),
                    "reload_status": reload_status,
                    "train_count": self._train_count,
                    "trained_at": self._last_train_time.isoformat(),
                }
            except Exception as e:
                self._last_train_error = str(e)
                train_duration_ms = int((time.monotonic() - train_start) * 1000)
                logger.error(
                    "%s: training failed after %dms: %s",
                    self.model_id, train_duration_ms, e, exc_info=True,
                )
                return {
                    "model_id": self.model_id,
                    "status": "failed",
                    "error": str(e),
                    "duration_ms": train_duration_ms,
                    "sample_count": len(data),
                }

    def _atomic_write_model(self, model_bytes: bytes) -> Path:
        """原子写入 ONNX 模型文件（tempfile + os.replace）

        防止进程崩溃留下半截 .onnx 文件，导致下次启动 ort.InferenceSession 失败。
        与 edge_ai_inference.py:_try_generate_preset 的写入策略一致。
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.models_dir / self.model_file
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.models_dir), suffix=".onnx.tmp"
        )
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(model_bytes)
            os.replace(tmp_path, target_path)
        except BaseException:
            # 清理临时文件
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        return target_path

    async def _trigger_reload(self, model_path: Path) -> str:
        """触发 AiInferenceEngine 热重载已更新的模型

        Returns:
            "reloaded" | "skipped" | "failed:<reason>"
        """
        if self.ai_engine is None:
            logger.info(
                "%s: ai_engine not set, skip hot-reload (file written to %s)",
                self.model_id, model_path,
            )
            return "skipped:no_ai_engine"

        reload_fn = getattr(self.ai_engine, "reload_model", None)
        if reload_fn is None:
            logger.warning("%s: ai_engine has no reload_model method", self.model_id)
            return "skipped:no_reload_method"

        try:
            # reload_model 是 async 方法
            await reload_fn(self.model_id, str(model_path))
            return "reloaded"
        except Exception as e:
            logger.error("%s: hot-reload failed: %s", self.model_id, e, exc_info=True)
            return f"failed:{e}"

    # ------------------------------------------------------------------
    # 子类需实现的接口
    # ------------------------------------------------------------------

    def _train(self, data: list[Any]) -> dict[str, Any] | None:
        """从训练数据计算权重（子类实现）

        Args:
            data: 训练样本列表

        Returns:
            权重字典，或 None 表示训练失败
        """
        raise NotImplementedError

    def _build_onnx_graph(self, weights: dict[str, Any]) -> bytes | None:
        """将训练权重构造为 ONNX 模型字节（子类实现）

        使用 onnx.helper 手动构造图（无 skl2onnx 依赖）。
        opset 13，ir_version 7（与 edge_ai_inference._generate_onnx_model 一致）。

        Returns:
            序列化的 ONNX 模型字节，或 None 表示构造失败
        """
        raise NotImplementedError

    def _default_weights(self) -> dict[str, Any]:
        """返回默认（未训练）权重（子类实现）"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 反馈与仪表盘
    # ------------------------------------------------------------------

    def add_result(self, result: dict[str, Any], max_results: int = 100) -> None:
        """记录最近的推理/检测结果（用于仪表盘展示）"""
        with self._feedback_lock:
            self._recent_results.append(result)
            if len(self._recent_results) > max_results:
                self._recent_results = self._recent_results[-max_results:]

    async def submit_feedback(self, **kwargs: Any) -> dict[str, Any]:
        """提交反馈（子类可覆盖以实现重训练触发逻辑）

        通用实现：记录反馈到历史，达到阈值后触发重训练。
        """
        with self._feedback_lock:
            self._feedback_history.append({
                "feedback": kwargs,
                "timestamp": datetime.now(UTC).isoformat(),
            })
            feedback_count = len(self._feedback_history)

        result = {
            "model_id": self.model_id,
            "feedback_count": feedback_count,
            "status": "recorded",
        }

        # 反馈累积到一定数量后触发重训练
        if feedback_count >= 10 and feedback_count % 10 == 0:
            train_result = await self.train_and_export()
            result["retrain"] = train_result

        return result

    def get_dashboard(self) -> dict[str, Any]:
        """返回仪表盘数据（状态、统计、最近结果）"""
        with self._data_lock:
            sample_count = len(self._data_buffer)
        with self._feedback_lock:
            feedback_count = len(self._feedback_history)
            recent_results = list(self._recent_results[-20:])

        return {
            "model_id": self.model_id,
            "model_file": self.model_file,
            "sample_count": sample_count,
            "min_samples": self.min_samples,
            "max_buffer": self.max_buffer,
            "train_count": self._train_count,
            "last_train_time": self._last_train_time.isoformat() if self._last_train_time else None,
            "last_train_sample_count": self._last_train_sample_count,
            "last_train_error": self._last_train_error,
            "feedback_count": feedback_count,
            "recent_results": recent_results,
            "ready_to_train": sample_count >= self.min_samples,
        }

    def set_ai_engine(self, ai_engine: Any) -> None:
        """注入 AiInferenceEngine 实例（bootstrap 时调用）"""
        self.ai_engine = ai_engine
