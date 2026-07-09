"""规则评估比较逻辑 - 纯Python回退实现

当Cython编译的_cython.rule_compare不可用时使用此模块。
功能完全相同，只是没有C层加速。
"""

_OP_FUNCS = {
    ">": lambda a, t: a > t,
    ">=": lambda a, t: a >= t,
    "<": lambda a, t: a < t,
    "<=": lambda a, t: a <= t,
    "=": lambda a, t: a == t,
    "==": lambda a, t: a == t,
    "!=": lambda a, t: a != t,
    "gt": lambda a, t: a > t,
    "gte": lambda a, t: a >= t,
    "lt": lambda a, t: a < t,
    "lte": lambda a, t: a <= t,
    "eq": lambda a, t: a == t,
    "neq": lambda a, t: a != t,
}


def check_condition_fast(actual: float, operator: str, threshold: float) -> bool:
    """快速条件比较（纯Python版）"""
    # FIXED-P1: 原问题-actual 或 threshold 为 None 时直接比较会抛 TypeError，导致规则评估中断
    if actual is None or threshold is None:
        return False
    func = _OP_FUNCS.get(operator)
    if func is None:
        return False
    try:
        return func(actual, threshold)
    except TypeError:
        # FIXED-P1: 非数值类型比较时返回 False 而非抛异常
        return False


def check_conditions_fast(actual: float, conditions: list, logic: str = "AND") -> bool:
    """快速多条件评估（纯Python版）

    Args:
        actual: 实际值
        conditions: 条件列表 [{"operator": ">=", "threshold": 50.0}, ...]
        logic: 逻辑组合 "AND" 或 "OR"

    Returns:
        条件组合结果
    """
    # FIXED-P2: 空条件列表与 rule_service 保持一致，返回 False
    if not conditions:
        return False
    # FIXED-P1: 原问题-actual 为 None 时所有比较都会抛 TypeError
    if actual is None:
        return False
    for cond in conditions:
        op = cond.get("operator", cond.get("op", ""))
        threshold = cond.get("threshold", cond.get("value", 0.0))
        if threshold is None:
            threshold = 0.0
        func = _OP_FUNCS.get(op)
        if func is None:
            if logic == "OR":
                continue
            return False
        try:
            result = func(actual, threshold)
        except TypeError:
            # FIXED-P1: 非数值类型比较时视为条件不满足
            if logic == "AND":
                return False
            continue
        if logic == "AND" and not result:
            return False
        if logic == "OR" and result:
            return True

    return logic == "AND"
