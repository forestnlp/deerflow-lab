"""v06 的工具箱 —— 参考本体 packages/harness/deerflow/tools/tools.py::get_available_tools

工具按"组"（group）登记：子代理装配时只能拿到自己被允许的那几组，
task 工具永远不在子代理的清单里（这是"禁止套娃"的第一道闸）。
"""

from __future__ import annotations

from pathlib import Path

from langchain.tools import tool

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


@tool
def read_notes(note: str) -> str:
    """读取 data/notes/ 下的一份资料原文。note 是不带扩展名的文件名。"""
    path = DATA / "notes" / f"{note}.txt"
    if not path.exists():
        return f"找不到资料：{note}"
    return path.read_text(encoding="utf-8").strip()


@tool
def word_count(text: str) -> str:
    """统计一段文本的字符数，返回纯数字字符串。用于核对摘要是否够短。"""
    return str(len(text))


@tool
def write_report(text: str) -> str:
    """把最终报告写入 data/report.txt，返回写入的字符数。"""
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "report.txt").write_text(text, encoding="utf-8")
    return f"已写入 {len(text)} 字符"


# 工具按组登记：子代理配置里写组名，装配时展开成工具对象
TOOL_GROUPS: dict[str, list] = {
    "read": [read_notes],
    "write": [write_report],
    "count": [word_count],
}

LEAD_TOOLS = [read_notes, word_count, write_report]


def resolve_groups(groups: list[str] | None) -> list:
    """组名列表 → 工具对象列表（去重、保序）。None 表示全部。"""
    if groups is None:
        return list(LEAD_TOOLS)
    out, seen = [], set()
    for g in groups:
        for t in TOOL_GROUPS.get(g, []):
            if t.name not in seen:
                seen.add(t.name)
                out.append(t)
    return out
