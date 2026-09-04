"""内置命令解析 —— 以 / 开头的消息不进 agent，由渠道层直接回答。

参考本体：backend/app/channels/commands.py（/new /status /help 等内置命令）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedCommand:
    name: str            # 不含斜杠，小写
    arg: str = ""


def parse_command(text: str) -> ParsedCommand | None:
    """"/new" -> ParsedCommand("new")；普通消息 -> None（要走 agent）。"""
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(maxsplit=1)
    if not parts:
        return None
    return ParsedCommand(name=parts[0].lower(), arg=parts[1] if len(parts) > 1 else "")


HELP_TEXT = "可用命令：\n/new 开新会话\n/status 当前映射\n/help 本帮助"
