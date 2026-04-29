"""数据预处理模块单元测试"""
import pytest
import sys
import time
sys.path.insert(0, 'src')

from edgelite.engine.preprocessor import DataPreprocessor


class TestPreprocessor:
    def test_no_config_passthrough(self):
        """无配置透传"""
        pp = DataPreprocessor()
        val, report = pp.process("dev.temp", 25.0)
        assert val == 25.0
        assert report is True

    def test_deadband_absolute_within(self):
        """绝对死区-变化在死区内"""
        pp = DataPreprocessor()
        pp.configure("dev.temp", {"deadband": 0.5})
        pp.process("dev.temp", 25.0)
        val, report = pp.process("dev.temp", 25.3)
        assert report is False

    def test_deadband_absolute_outside(self):
        """绝对死区-变化超出死区"""
        pp = DataPreprocessor()
        pp.configure("dev.temp", {"deadband": 0.5})
        pp.process("dev.temp", 25.0)
        val, report = pp.process("dev.temp", 25.6)
        assert report is True
        assert val == 25.6

    def test_deadband_percent(self):
        """百分比死区"""
        pp = DataPreprocessor()
        pp.configure("dev.power", {"deadband_percent": 5.0})
        pp.process("dev.power", 100.0)
        val, report = pp.process("dev.power", 103.0)
        assert report is False
        val, report = pp.process("dev.power", 106.0)
        assert report is True

    def test_median_filter(self):
        """中值滤波"""
        pp = DataPreprocessor()
        pp.configure("dev.noise", {"filter": "median_3", "filter_window": 3})
        pp.process("dev.noise", 10.0)
        pp.process("dev.noise", 100.0)
        val, report = pp.process("dev.noise", 12.0)
        assert val == 12.0

    def test_aggregation_avg(self):
        """时间窗口聚合-平均值"""
        pp = DataPreprocessor()
        pp.configure("dev.temp", {"aggregate": "avg", "aggregate_window_sec": 10})
        now = time.time()
        val1, _ = pp.process("dev.temp", 20.0, now - 5)
        val2, _ = pp.process("dev.temp", 30.0, now)
        assert val2 is not None
        assert abs(val2 - 25.0) < 0.01

    def test_pipeline_order(self):
        """处理链顺序：滤波→死区→聚合"""
        pp = DataPreprocessor()
        pp.configure("dev.temp", {"filter": "median_3", "filter_window": 3, "deadband": 1.0})
        pp.process("dev.temp", 25.0)
        pp.process("dev.temp", 25.1)
        val, report = pp.process("dev.temp", 25.2)
        assert report is False
