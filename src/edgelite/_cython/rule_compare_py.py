"""规则评估比较逻辑 - 纯Python回退实现

当Cython编译的_cython.rule_compare不可用时使用此模块。
功能完全相同，只是没有C层加速。
"""

_OP_FUNCS = {
    ">": lambda a, t: a > t,
    ">=": lambda a, t: a >= t,
    "<": lambda a, t: a < t,
    "<=": lambda a, t: a <= t,
    "==": lambda a, t: a == t,
    "!=": lambda a, t: a != t,
    "gt": lambda a, t: a > t,
    "gte": lambda a, t: a >= t,
    "lt": lambda a, t: a < t,
    "lte": lambda a, t: a <= t,
    "eq": lambda a, t: a == t,
    "neq": lambda a, t: a != t,
}


def check_condition_fast(actual: float, op: str, threshold: float) -> bool:
    """快速条件比较（纯Python版）"""
    func = _OP_FUNCS.get(op)
    if func is None:
        return False
    return func(actual, threshold)


def check_conditions_fast(actual: float, conditions: list) -> bool:
    """快速多条件AND评估（纯Python版）"""
    for cond in conditions:
        op = cond.get("op", "")
        threshold = cond.get("value", 0.0)
        func = _OP_FUNCS.get(op)
        if func is None or not func(actual, threshold):
            return False
    return True
