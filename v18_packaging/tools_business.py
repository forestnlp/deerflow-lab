"""本版工具零件 —— config.yaml 的 tools 段用 "tools_business:xxx" 引用它们。
参考本体 config.yaml tools 段的 {name, group, use} 声明式注册。
"""

from __future__ import annotations

from datetime import datetime

from langchain.tools import tool


@tool
def get_indicator(indicator: str) -> str:
    """查询经营指标。indicator 取 收入 或 时长。"""
    table = {"收入": "H1 寄递收入 42.1 亿元", "时长": "平均投递时长 26.4 小时"}
    return table.get(indicator, f"no data for {indicator!r}")


@tool
def now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """查询当前时间，fmt 为 strftime 格式。"""
    return datetime.now().strftime(fmt)


@tool
def shout(text: str) -> str:
    """把文本变成大写强调体（演示用），中文加【】。"""
    return text.upper() if text.isascii() else f"【{text}】"
