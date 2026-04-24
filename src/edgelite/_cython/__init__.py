# EdgeLite Cython加速模块
"""
Cython编译的加速模块，提供CPU密集型操作的C层实现。
当Cython编译不可用时，自动回退到纯Python实现。

使用方式：
    from edgelite._cython import map_device_data_fast, check_condition_fast
"""

# 尝试导入Cython编译版本，失败则回退到纯Python实现
try:
    from edgelite._cython.modbus_mapper import map_device_data_fast
except ImportError:
    from edgelite._cython.modbus_mapper_py import map_device_data_fast

try:
    from edgelite._cython.rule_compare import check_condition_fast, check_conditions_fast
except ImportError:
    from edgelite._cython.rule_compare_py import check_condition_fast, check_conditions_fast

__all__ = ["map_device_data_fast", "check_condition_fast", "check_conditions_fast"]
