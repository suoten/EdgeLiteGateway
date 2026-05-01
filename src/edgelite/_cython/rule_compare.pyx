# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
"""规则评估比较逻辑Cython加速模块

将规则条件评估从Python对象操作优化为C层直接数值比较。
Cython编译后比纯Python实现快5-10倍（消除Python方法调用和对象创建开销）。
"""


cdef dict _OP_MAP = {
    ">": 0, ">=": 1, "<": 2, "<=": 3, "==": 4, "!=": 5,
    "gt": 0, "gte": 1, "lt": 2, "lte": 3, "eq": 4, "neq": 5,
}


cdef inline bint _compare_c(double actual, int op_code, double threshold) noexcept:
    """C层内联比较（零Python对象开销）"""
    if op_code == 0:    # >
        return actual > threshold
    elif op_code == 1:  # >=
        return actual >= threshold
    elif op_code == 2:  # <
        return actual < threshold
    elif op_code == 3:  # <=
        return actual <= threshold
    elif op_code == 4:  # ==
        return actual == threshold
    elif op_code == 5:  # !=
        return actual != threshold
    return False


def check_condition_fast(double actual, str operator, double threshold) -> bool:
    """快速条件比较（Cython加速版）

    Args:
        actual: 实际值
        operator: 比较操作符 (">", ">=", "<", "<=", "==", "!=")
        threshold: 阈值

    Returns:
        比较结果
    """
    cdef int op_code = _OP_MAP.get(operator, -1)
    if op_code < 0:
        return False
    return _compare_c(actual, op_code, threshold)


def check_conditions_fast(double actual, list conditions, str logic="AND") -> bool:
    """快速多条件评估（Cython加速版）

    Args:
        actual: 实际值
        conditions: 条件列表 [{"operator": ">=", "threshold": 50.0}, ...]
        logic: 逻辑组合 "AND" 或 "OR"

    Returns:
        条件组合结果
    """
    cdef int op_code
    cdef double cond_threshold
    cdef bint result

    for cond in conditions:
        op = cond.get("operator", cond.get("op", ""))
        cond_threshold = <double>cond.get("threshold", cond.get("value", 0.0))
        op_code = _OP_MAP.get(op, -1)
        if op_code < 0:
            if logic == "OR":
                continue
            return False
        result = _compare_c(actual, op_code, cond_threshold)
        if logic == "AND" and not result:
            return False
        if logic == "OR" and result:
            return True

    return logic == "AND"
