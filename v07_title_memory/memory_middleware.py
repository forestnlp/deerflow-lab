"""MemoryMiddleware —— 参考本体 packages/harness/deerflow/agents/middlewares/memory_middleware.py
（after_agent：run 收尾时把对话交给 MemoryManager.add —— 只排队不改 state、
 只取用户输入与最终回复、异步防抖抽取）
与 agents/lead_agent/prompt.py（记忆以 <system-reminder><memory>…</memory> 注入；
 注入消息带来源标记，防伪造闭合标签）。

两枚中间件按方向分工：
- MemoryInject（before_agent）：读方向 —— 进程启动/本轮开工前，把磁盘上的
  facts 拼成 <system-reminder> 注入本轮请求（不落盘进历史，wrap 还是 before
  二选一；学习版取 before_agent 落盘路径，本体 lead_agent 走 system prompt 拼装）。
- MemoryMiddleware（after_agent）：写方向 —— 对话结束把素材交给防抖抽取器。
"""

from __future__ import annotations

from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, HumanMessage

from memory import DebouncedExtractor, load_facts

REMINDER_NAME = "memory_reminder"   # title_middleware 统计用户消息时按此跳过


class MemoryState(AgentState):
    injected_facts: NotRequired[list[str]]


def format_memory_reminder(facts: list[str]) -> str:
    """本体 lead_agent/prompt.py 的同款外壳；值里的闭合字符序列被剥掉，
    防止事实内容伪造 </memory> 越狱（本体 prompt.py 的转义纪律）。"""
    safe = [f.replace("</", "<\\/") for f in facts]
    lines = "\n".join(f"  <fact>{f}</fact>" for f in safe)
    return ("<system-reminder>\n<memory>\n" + lines + "\n</memory>\n</system-reminder>")


class MemoryInjectMiddleware(AgentMiddleware):
    """读方向：facts 还在磁盘上，本轮模型却看不见——before_agent 补一发提醒。"""

    state_schema = MemoryState

    def __init__(self, path) -> None:
        super().__init__()
        self._path = path

    def before_agent(self, state: MemoryState, runtime: Any) -> dict[str, Any] | None:
        facts = load_facts(self._path)
        if not facts:
            return None
        already = any(getattr(m, "name", None) == REMINDER_NAME
                      for m in (state.get("messages") or []))
        if already:
            return None                     # 本轮已注入过，不重复
        print(f"  >> [MemoryInject] 注入长期记忆 {len(facts)} 条 -> <system-reminder>")
        return {"messages": [HumanMessage(content=format_memory_reminder(facts),
                                          name=REMINDER_NAME,
                                          additional_kwargs={"hide_from_ui": True})]}


class MemoryMiddleware(AgentMiddleware):
    """写方向：after_agent 把"用户说了什么 + 最终答了什么"交给防抖抽取器。

    只取无 tool_calls 的 AI 消息——工具往返是过程，长期记忆只关心结论
    （本体 MemoryMiddleware docstring 第 2 条同语义）。
    """

    state_schema = MemoryState

    def __init__(self, extractor: DebouncedExtractor) -> None:
        super().__init__()
        self._extractor = extractor

    def after_agent(self, state: MemoryState, runtime: Any) -> dict[str, Any] | None:
        users, ais = [], []
        for m in state.get("messages") or []:
            if getattr(m, "name", None) == REMINDER_NAME:
                continue                    # 注入的提醒不是用户说的话
            if isinstance(m, HumanMessage):
                users.append(str(m.content))
            elif isinstance(m, AIMessage) and not m.tool_calls and m.content:
                ais.append(str(m.content))
        if users:
            self._extractor.enqueue(users, ais)
        return None                         # 排队不改 state（本体同语义）
