"""v09 的工具箱 —— 参考本体 packages/harness/deerflow/tools/builtins/clarification_tool.py

ask_clarification 的返回值写成了"绝不该出现"的样子：一旦它出现在输出里，
说明中间件没拦住、工具真执行了——这本身就是本版的自检探针。
"""

from __future__ import annotations

from langchain.tools import tool


@tool
def query_sales(metric: str, region: str) -> str:
    """查指定区域的销售指标。metric 如 revenue/volume，region 如 华东/华南。"""
    return f"{region} {metric} = 42.1 亿（演示数据）"


@tool
def ask_clarification(question: str) -> str:
    """信息不足时向用户提问。调用后本轮执行会被中断，问题直接呈现给用户。"""
    return "SHOULD-NOT-EXECUTE：中间件失守，澄清工具竟然跑了"


@tool
def calculator(expression: str) -> str:
    """计算算术表达式，例如 \"(2+3)*7\"。"""
    allowed = set("0123456789+-*/.()% ")
    if not set(expression) <= allowed:
        return "拒绝执行：含非法字符"
    return str(eval(expression, {"__builtins__": {}}, {}))


TOOLS = [query_sales, ask_clarification, calculator]
