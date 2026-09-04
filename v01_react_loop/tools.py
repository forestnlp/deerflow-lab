"""v01 的三个工具 —— 参考本体 packages/harness/deerflow/tools/tools.py（工具即反射声明）。

工具用 @tool 装饰普通函数：docstring 会变成给模型看的说明书，
函数签名会变成参数 schema。模型不是在执行你的代码，而是在"填表"，
框架负责把表（JSON）递交给你的函数。
"""

from __future__ import annotations

import math
from datetime import datetime

from langchain.tools import tool


@tool
def get_current_time(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """查询当前时间。fmt 为 strftime 格式串，默认精确到秒。"""
    return datetime.now().strftime(fmt)


@tool
def calculator(expression: str) -> str:
    """计算一个算术表达式，例如 "(2+3)*7" 或 "2**10"。只支持数字与 +-*/**()。"""
    allowed = set("0123456789+-*/.()% ")
    if not set(expression) <= allowed:
        return f"拒绝执行：表达式含非法字符（只允许数字与 +-*/.()% 空格）"
    return str(eval(expression, {"__builtins__": {}}, {}))


@tool
def sqrt(x: float) -> str:
    """计算平方根。"""
    return str(math.sqrt(x))


TOOLS = [get_current_time, calculator, sqrt]
