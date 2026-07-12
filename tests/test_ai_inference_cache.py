"""AI 推理结果缓存测试 - TTL + LRU 淘汰

覆盖 engine/ai_inference_cache.py：
- _make_key: model_id + input_data → MD5 哈希键
- get/put: 缓存命中/未命中/TTL 过期/LRU 移动
- LRU 淘汰: 超过 max_size 淘汰最久未用
- get_stats: 命中率统计
- invalidate_model: 清空全部缓存
- clear: 清空
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from edgelite.engine.ai_inference_cache import InferenceCache


class TestMakeKey:
    def test_same_input_same_key(self):
        k1 = InferenceCache._make_key("model_a", [1.0, 2.0, 3.0])
        k2 = InferenceCache._make_key("model_a", [1.0, 2.0, 3.0])
        assert k1 == k2

    def test_different_model_different_key(self):
        k1 = InferenceCache._make_key("model_a", [1.0])
        k2 = InferenceCache._make_key("model_b", [1.0])
        assert k1 != k2

    def test_different_input_different_key(self):
        k1 = InferenceCache._make_key("model_a", [1.0, 2.0])
        k2 = InferenceCache._make_key("model_a", [1.0, 3.0])
        assert k1 != k2

    def test_key_is_md5_hex(self):
        key = InferenceCache._make_key("model", [1.0])
        assert len(key) == 32  # MD5 hex digest length
        assert all(c in "0123456789abcdef" for c in key)

    def test_float_precision_6_decimals(self):
        """input_data 格式化为 6 位小数，1.0000001 和 1.0 产生相同键"""
        k1 = InferenceCache._make_key("m", [1.0000001])
        k2 = InferenceCache._make_key("m", [1.0])
        # 1.0000001 格式化为 {:.6f} = "1.000000" == 1.0 的 "1.000000"
        assert k1 == k2


class TestGetPut:
    def test_miss_returns_none(self):
        cache = InferenceCache()
        assert cache.get("model", [1.0]) is None

    def test_hit_returns_data(self):
        cache = InferenceCache()
        cache.put("model", [1.0, 2.0], {"result": "ok"})
        assert cache.get("model", [1.0, 2.0]) == {"result": "ok"}

    def test_put_then_get_roundtrip(self):
        cache = InferenceCache()
        output = [0.1, 0.2, 0.7]
        cache.put("m", [1.0], output)
        assert cache.get("m", [1.0]) is output

    def test_ttl_expiry(self):
        """TTL 过期后 get 返回 None"""
        cache = InferenceCache(ttl=0.05)
        cache.put("m", [1.0], "data")
        time.sleep(0.2)  # Windows 时钟分辨率低，需较长等待
        assert cache.get("m", [1.0]) is None

    def test_ttl_not_expired(self):
        cache = InferenceCache(ttl=10.0)
        cache.put("m", [1.0], "data")
        assert cache.get("m", [1.0]) == "data"

    def test_expired_entry_removed_on_get(self):
        cache = InferenceCache(ttl=0.05)
        cache.put("m", [1.0], "data")
        time.sleep(0.2)  # Windows 时钟分辨率低，需较长等待确保 monotonic 越过 TTL
        cache.get("m", [1.0])  # 触发过期清理
        assert len(cache._cache) == 0


class TestLRUEviction:
    def test_evict_oldest_when_over_capacity(self):
        cache = InferenceCache(ttl=100, max_size=2)
        cache.put("m", [1.0], "a")
        cache.put("m", [2.0], "b")
        cache.put("m", [3.0], "c")  # 超容量，淘汰 [1.0]
        assert cache.get("m", [1.0]) is None  # 被淘汰
        assert cache.get("m", [2.0]) == "b"
        assert cache.get("m", [3.0]) == "c"

    def test_get_updates_lru_order(self):
        """get 后该条目变为最近使用，不被淘汰"""
        cache = InferenceCache(ttl=100, max_size=2)
        cache.put("m", [1.0], "a")
        cache.put("m", [2.0], "b")
        cache.get("m", [1.0])  # [1.0] 变为最近使用
        cache.put("m", [3.0], "c")  # 淘汰最久未用 → [2.0]
        assert cache.get("m", [1.0]) == "a"  # 仍然存在
        assert cache.get("m", [2.0]) is None  # 被淘汰

    def test_put_same_key_updates_not_evict(self):
        cache = InferenceCache(ttl=100, max_size=2)
        cache.put("m", [1.0], "a")
        cache.put("m", [1.0], "a2")  # 更新同键
        assert len(cache._cache) == 1
        assert cache.get("m", [1.0]) == "a2"


class TestStats:
    def test_initial_stats(self):
        cache = InferenceCache(ttl=5.0, max_size=100)
        stats = cache.get_stats()
        assert stats["size"] == 0
        assert stats["max_size"] == 100
        assert stats["ttl"] == 5.0
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["hit_rate"] == 0.0

    def test_hits_and_misses(self):
        cache = InferenceCache()
        cache.get("m", [1.0])  # miss
        cache.put("m", [1.0], "data")
        cache.get("m", [1.0])  # hit
        cache.get("m", [2.0])  # miss
        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 2
        assert stats["hit_rate"] == round(1 / 3, 4)

    def test_ttl_expired_counts_as_miss(self):
        cache = InferenceCache(ttl=0.05)
        cache.put("m", [1.0], "data")
        time.sleep(0.2)  # Windows 时钟分辨率低，需较长等待
        cache.get("m", [1.0])  # 过期 → miss
        stats = cache.get_stats()
        assert stats["misses"] == 1
        assert stats["hits"] == 0

    def test_size_reflects_cache(self):
        cache = InferenceCache(max_size=10)
        cache.put("m", [1.0], "a")
        cache.put("m", [2.0], "b")
        assert cache.get_stats()["size"] == 2


class TestClear:
    def test_clear_empties_cache(self):
        cache = InferenceCache()
        cache.put("m", [1.0], "a")
        cache.clear()
        assert len(cache._cache) == 0
        assert cache.get("m", [1.0]) is None

    def test_clear_does_not_reset_stats(self):
        cache = InferenceCache()
        cache.put("m", [1.0], "a")
        cache.get("m", [1.0])  # hit
        cache.clear()
        stats = cache.get_stats()
        assert stats["hits"] == 1  # 统计不清零


class TestInvalidateModel:
    def test_invalidate_returns_count(self):
        cache = InferenceCache()
        cache.put("m1", [1.0], "a")
        cache.put("m2", [2.0], "b")
        count = cache.invalidate_model("m1")
        assert count == 2  # 清空全部

    def test_invalidate_clears_cache(self):
        cache = InferenceCache()
        cache.put("m", [1.0], "a")
        cache.invalidate_model("m")
        assert len(cache._cache) == 0
        assert cache.get("m", [1.0]) is None

    def test_invalidate_empty_cache_returns_zero(self):
        cache = InferenceCache()
        assert cache.invalidate_model("m") == 0
