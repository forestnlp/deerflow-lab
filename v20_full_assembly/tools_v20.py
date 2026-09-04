"""v20 的三个演示工具 —— 够总装链每一环都有活干即可。"""

from __future__ import annotations

from langchain.tools import tool


@tool
def get_indicator(indicator: str) -> str:
    """查询经营指标。indicator 取 收入 或 时长。"""
    table = {"收入": "H1 寄递收入 42.1 亿元", "时长": "平均投递时长 26.4 小时"}
    return table.get(indicator, f"no data for {indicator!r}")


@tool
def flaky_report() -> str:
    """拉取第三方报表（已知不稳定，用于演示循环刹车）。"""
    return "Error: upstream timeout"


@tool
def ask_clarification(question: str) -> str:
    """向用户反问澄清；本轮会在工具执行前结束，问题由渠道/CLI 转达。"""
    return "（已把问题抛回用户）"


TOOLS = [get_indicator, flaky_report, ask_clarification]
