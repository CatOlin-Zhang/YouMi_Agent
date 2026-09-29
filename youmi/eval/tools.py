"""
评测确定性工具集 (eval.tools)

为 eval 基准提供**确定性、无副作用**的工具，配合 MockLLMServer
实现完全可复现的 LLM 依赖路径测试。

工具清单（均为纯函数）::

    get_weather(city)      — 固定天气库查询（北京/上海/深圳/广州）
    calculate(expression)  — 安全四则运算（AST 解析，不用 eval）
    reverse_text(text)     — 文本反转
    word_count(text)       — 文本长度统计
"""

from __future__ import annotations

import ast
import operator
from typing import Any

from youmi.core.tool import ToolRegistry

# 固定天气库（保证确定性）
_WEATHER_DB: dict[str, str] = {
    "北京": "晴，25℃",
    "上海": "多云，28℃",
    "深圳": "小雨，32℃",
    "广州": "阴，30℃",
}


def get_weather(city: str) -> str:
    """查询指定城市的天气（评测用固定数据源）"""
    return _WEATHER_DB.get(city, f"暂无 {city} 的天气数据")


# 允许的运算符
_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _eval_ast(node: ast.AST) -> float:
    """递归求值 AST（仅允许数字字面量、一元正负、四则运算）"""
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_ast(node.left), _eval_ast(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_ast(node.operand)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
        return _eval_ast(node.operand)
    raise ValueError(f"不支持的表达式元素: {type(node).__name__}")


def calculate(expression: str) -> str:
    """安全计算数学表达式结果（仅支持数字与 + - * / // % ** 括号）"""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_ast(tree)
    except Exception as exc:
        return f"计算错误: {exc}"
    # 整数值不带小数点
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return str(result)


def reverse_text(text: str) -> str:
    """反转文本"""
    return text[::-1]


def word_count(text: str) -> str:
    """统计文本字符数与空白分词数"""
    words = len(text.split())
    return f"字符数 {len(text)}，词数 {words}"


def build_default_tools(registry: ToolRegistry | None = None) -> ToolRegistry:
    """构建内置评测工具注册表（可传入已有 registry 追加注册）"""
    reg = registry if registry is not None else ToolRegistry()
    reg.register_function(get_weather)
    reg.register_function(calculate)
    reg.register_function(reverse_text)
    reg.register_function(word_count)
    return reg


__all__ = [
    "get_weather",
    "calculate",
    "reverse_text",
    "word_count",
    "build_default_tools",
]
