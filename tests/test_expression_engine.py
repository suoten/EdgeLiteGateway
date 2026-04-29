"""表达式引擎AST安全测试"""
import pytest
import sys
sys.path.insert(0, 'src')

from edgelite.engine.expression_engine import ExpressionEngine


class TestExpressionEngineAST:
    def test_arithmetic(self):
        """算术运算"""
        engine = ExpressionEngine()
        assert engine.evaluate("2 + 3") == 5
        assert engine.evaluate("10 - 4") == 6
        assert engine.evaluate("3 * 4") == 12
        assert engine.evaluate("10 / 3") == pytest.approx(10/3)
        assert engine.evaluate("2 ** 3") == 8

    def test_comparison(self):
        """比较运算"""
        engine = ExpressionEngine()
        assert engine.evaluate("5 > 3") is True
        assert engine.evaluate("3 > 5") is False
        assert engine.evaluate("3 == 3") is True

    def test_conditional(self):
        """条件表达式"""
        engine = ExpressionEngine()
        assert engine.evaluate("1 if True else 0") == 1
        assert engine.evaluate("10 if 5 > 3 else 20") == 10

    def test_safe_functions(self):
        """安全函数调用"""
        engine = ExpressionEngine()
        assert engine.evaluate("abs(-5)") == 5
        assert engine.evaluate("round(3.7)") == 4
        assert engine.evaluate("min(1, 2, 3)") == 1
        assert engine.evaluate("max(1, 2, 3)") == 3
        assert engine.evaluate("sqrt(16)") == 4.0

    def test_dangerous_names_blocked(self):
        """危险标识符被阻止"""
        engine = ExpressionEngine()
        with pytest.raises(ValueError):
            engine.evaluate("exec('print(1)')")
        with pytest.raises(ValueError):
            engine.evaluate("open('file.txt')")
        with pytest.raises(ValueError):
            engine.evaluate("__import__('os')")

    def test_variable_resolution(self):
        """变量解析"""
        engine = ExpressionEngine()
        result = engine.evaluate("${dev1.temp} + 10", {"dev1.temp": 25.0})
        assert result == 35.0

    def test_empty_expression(self):
        """空表达式"""
        engine = ExpressionEngine()
        assert engine.evaluate("") is None
        assert engine.evaluate("   ") is None

    def test_validate_expression_method(self):
        """_validate_expression方法存在且可用"""
        engine = ExpressionEngine()
        engine._validate_expression("1 + 2")
        with pytest.raises(ValueError):
            engine._validate_expression("exec('x')")
