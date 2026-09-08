"""v04 核心零件：上下文三件套——摘要折叠、token 预算、长期记忆。

参考本体 packages/harness/deerflow/agents/middlewares/ 的
summarization / token_budget / memory 三组中间件。

共同套路：都是"旁路模型调用 + 往消息流里注入"，agent 主循环一行不改。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from shared.model_factory import get_model

SUMMARY_NAME = "summary"          # 折叠产生的摘要消息用它认领身份
MEMORY_NAME = "memory_reminder"


def estimate_chars(messages) -> int:
    return sum(len(str(m.content)) for m in messages)


class SummarizeMiddleware(AgentMiddleware):
    """窗口超阈值 -> 旧消息折叠成一条摘要（真模型执笔）。

    摘要走 HumanMessage(name="summary") 而不是 SystemMessage：
    严格网关要求请求里必有 user 消息，折叠点之后若无 user 消息直接 400
    （No user query found）——在线实测踩过，本体同法可查证。
    """

    def __init__(self, trigger_chars: int, keep_recent: int) -> None:
        super().__init__()
        self.trigger, self.keep = trigger_chars, keep_recent

    def before_model(self, state, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if estimate_chars(messages) < self.trigger:
            return None
        old, recent = messages[:-self.keep], messages[-self.keep:]
        lines = [f"{m.type}: {str(m.content)[:60]}" for m in old]
        digest = get_model().invoke(
            "把下面对话压缩成不超过 60 字的一条摘要，只保留事实：\n" + "\n".join(lines))
        summary = HumanMessage(name=SUMMARY_NAME,
                               content=f"<conversation_summary>{digest.content.strip()}</conversation_summary>")
        print(f"  >> [摘要] 窗口 {estimate_chars(messages)} 字符超阈值 "
              f"{self.trigger} -> {len(old)} 条旧消息折叠成 1 条摘要")
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary, *recent]}


class BudgetMiddleware(AgentMiddleware):
    """token 预算：过半预警，超支硬停（jump_to=end）。"""

    def __init__(self, budget_chars: int) -> None:
        super().__init__()
        self.budget = budget_chars
        self.spent = 0
        self.stop_reason: str | None = None

    def reset(self) -> None:
        self.spent, self.stop_reason = 0, None

    def configure(self, budget_chars: int) -> None:
        self.budget = budget_chars
        self.reset()

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime: Any) -> dict[str, Any] | None:
        cost = estimate_chars(state.get("messages") or [])
        if self.spent + cost > self.budget:
            self.stop_reason = f"预算 {self.budget} 字符已耗尽（本轮需 {cost}）"
            print(f"  >> [预算] {self.stop_reason} -> jump_to=end 硬停")
            return {"jump_to": "end"}
        if self.spent + cost > self.budget * 0.5 and self.spent <= self.budget * 0.5:
            print(f"  >> [预算] 预警：已近半程（{self.spent} + {cost} > {self.budget // 2}）")
        self.spent += cost
        return None


class MemoryMiddleware(AgentMiddleware):
    """长期记忆：开局注入磁盘 facts，收场让模型抽取新事实落盘。

    记忆与 checkpointer 无关：checkpointer 管"这场对话的完整历史"，
    记忆管"跨对话值得记住的几句话"，两个存储、两种生命周期。
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def _load(self) -> list[str]:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return []

    def before_model(self, state, runtime: Any) -> dict[str, Any] | None:
        facts = self._load()
        if not facts:
            return None
        messages = state.get("messages") or []
        if any(getattr(m, "name", None) == MEMORY_NAME for m in messages):
            return None                        # 本次 run 已注入过
        print(f"  >> [记忆] 注入 {len(facts)} 条磁盘事实（name={MEMORY_NAME}，对用户不可见）")
        return {"messages": [HumanMessage(
            name=MEMORY_NAME, additional_kwargs={"hide_from_ui": True},
            content="<user_memory>" + "；".join(facts) + "</user_memory>")]}

    def after_agent(self, state, runtime: Any) -> None:
        users = [str(m.content) for m in (state.get("messages") or []) if m.type == "human"]
        old = self._load()
        out = get_model().invoke(
            "从对话里抽取值得长期记住的用户事实（每条不超过 15 字，每行一条）。"
            f"已知旧事实：{'；'.join(old) or '无'}。\n对话：{' | '.join(users)[:600]}\n"
            "只输出完整清单（旧事实中已过时的要更新），没有新事实就原样输出旧清单。")
        facts = [ln.strip().lstrip("-•0123456789. ") for ln in out.content.splitlines() if ln.strip()]
        self.path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  >> [记忆] 结算落盘 {self.path.name}: {facts}")
