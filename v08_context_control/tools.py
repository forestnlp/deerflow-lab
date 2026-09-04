"""v08 的工具箱 —— 参考本体 packages/harness/deerflow/tools/tools.py::get_available_tools

fetch_chapter 特意返回一大段文本：让"读两章"就足以顶爆上下文阈值与预算，
摘要与预算两级机关才能在十步之内全部现形。
"""

from __future__ import annotations

from langchain.tools import tool

CHAPTERS = {
    "1": ("第一章讲库存周转：仓里躺着的是钱，转起来的才是生意。" * 6),
    "2": ("第二章讲网络密度：网点织得越密，单件成本越薄，前提是别空驶。" * 6),
}


@tool
def fetch_chapter(no: str) -> str:
    """按章节号取经营手册的一章原文（现有 1、2 两章）。"""
    return CHAPTERS.get(no, f"没有第 {no} 章")


TOOLS = [fetch_chapter]
