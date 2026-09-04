"""本版演示工具 —— 参考本体 tools/builtins/（只留一个计算器，够触发 tool 帧即可）。"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def calculator(expression: str) -> str:
    """计算一个算术表达式，例如 "(2+3)*7"。只支持数字与 +-*/**()。"""
    allowed = set("0123456789+-*/(). %")
    if not set(expression) <= allowed:
        raise ValueError(f"非法表达式: {expression!r}")
    return str(eval(expression))  # noqa: S307 —— 字符白名单已过滤，教学版够用


TOOLS = [calculator]
