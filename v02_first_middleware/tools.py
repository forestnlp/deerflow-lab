"""v02 的演示工具 —— 参考本体 packages/harness/deerflow/tools/tools.py（工具即反射声明）。

本版只需要一个最笨的工具：观察"工具调用是否真的被执行、被谁包了一层"。
"""

from __future__ import annotations

from langchain.tools import tool


@tool
def echo(text: str) -> str:
    """回显一段文本，用于观察工具调用是否真的被执行。"""
    return f"echo: {text}"


TOOLS = [echo]
