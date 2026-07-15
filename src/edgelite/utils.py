"""通用工具函数模块。"""

import time


def timestamp_ms() -> int:
    """返回当前时间的毫秒级 Unix 时间戳。"""
    return int(time.time() * 1000)


def timestamp_s() -> int:
    """返回当前时间的秒级 Unix 时间戳。"""
    return int(time.time())
