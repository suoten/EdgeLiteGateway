"""AI 推理结果缓存 - TTL + LRU 淘汰

避免重复预处理和推理，提升并发推理性能。

使用方式:
    cache = InferenceCache(ttl=5.0, max_size=1024)
    cached = cache.get("model_id", input_data)
    if cached is not None:
        return cached
    # ... 执行推理 ...
    cache.put("model_id", input_data, output_data)
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


class InferenceCache:
    """线程安全的 TTL + LRU 推理结果缓存

    线程安全:
    - _lock (threading.Lock): 保护 _cache 的并发读写
    """

    def __init__(self, ttl: float = 5.0, max_size: int = 1024):
        self._ttl = ttl
        self._max_size = max_size
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _make_key(model_id: str, input_data: list[float]) -> str:
        """生成缓存键: model_id + input_data 的哈希"""
        # 将 input_data 转为 bytes 再哈希
        data_str = f"{model_id}:{','.join(f'{v:.6f}' for v in input_data)}"
        return hashlib.md5(data_str.encode()).hexdigest()

    def get(self, model_id: str, input_data: list[float]) -> Any | None:
        """获取缓存结果

        Returns:
            缓存的输出数据，未命中返回 None
        """
        key = self._make_key(model_id, input_data)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            ts, data = entry
            # 检查 TTL
            if time.monotonic() - ts > self._ttl:
                self._cache.pop(key, None)
                self._misses += 1
                return None
            # LRU: 移到末尾（最近使用）
            self._cache.move_to_end(key)
            self._hits += 1
            return data

    def put(self, model_id: str, input_data: list[float], output_data: Any) -> None:
        """写入缓存结果"""
        key = self._make_key(model_id, input_data)
        with self._lock:
            self._cache[key] = (time.monotonic(), output_data)
            self._cache.move_to_end(key)
            # LRU 淘汰
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        """清空缓存"""
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> dict[str, Any]:
        """获取缓存统计"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "ttl": self._ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
            }

    def invalidate_model(self, model_id: str) -> int:
        """使指定模型的所有缓存失效（模型热重载后调用）

        Returns:
            清除的缓存条目数
        """
        # 由于 key 是哈希值，无法直接按 model_id 过滤
        # 简化实现：清空全部缓存（模型重载频率低，可接受）
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count
