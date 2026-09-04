"""v07 的工具箱 —— 参考本体 packages/harness/deerflow/tools/tools.py::get_available_tools

本版只需要一把小工具，用来演示"工具往返是过程，不进长期记忆素材"。
"""

from __future__ import annotations

from langchain.tools import tool

NOTES = {
    "role": "岗位说明：经营分析岗负责月度经营报告的口径与呈现。",
    "style": "偏好备忘：报告优先用表格，一段话讲不清就画表。",
}


@tool
def get_note(tag: str) -> str:
    """按标签查一条工作备忘。tag 只能是 role 或 style。"""
    return NOTES.get(tag, f"没有标签为 {tag!r} 的备忘")


TOOLS = [get_note]
