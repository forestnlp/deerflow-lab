"""TitleMiddleware —— 参考本体 packages/harness/deerflow/agents/middlewares/title_middleware.py
（TitleMiddleware.after_model：首轮完整问答后生成线程标题写进 state["title"]；
 _should_generate_title：已有标题不重生成、恰好一条用户消息+至少一条 AI 回复才生成；
 动态注入的提醒消息不计入用户消息数）。

学习版差异：标题模型由构造参数注入（--fake 用脚本模型、在线用 config 的 title 模型）；
去掉异步分支、RunJournal 打标、think 标签清洗与 fallback 标题链路。
"""

from __future__ import annotations

from typing import Any, Callable, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, HumanMessage

# 学习版约定：由框架注入的提醒消息带 name 标记，标题统计"用户消息"时跳过
# （本体对应 is_dynamic_context_reminder 的过滤，见 title_middleware.py）。
INJECTED_MESSAGE_NAMES = {"memory_reminder"}


class TitleState(AgentState):
    """图状态加一个 title 键（本体 TitleMiddlewareState 同款）。"""

    title: NotRequired[str | None]


class TitleMiddleware(AgentMiddleware):
    """首轮结束后，用一次廉价模型调用给这轮对话起个标题，写进 state。"""

    state_schema = TitleState

    def __init__(self, title_model_factory: Callable[[str], Any]) -> None:
        """title_model_factory(prompt_text) -> ChatModel：每次生成现场造一个模型。"""
        super().__init__()
        self._factory = title_model_factory

    @staticmethod
    def _is_real_user_message(msg: Any) -> bool:
        return isinstance(msg, HumanMessage) and getattr(msg, "name", None) not in INJECTED_MESSAGE_NAMES

    def _should_generate(self, state: TitleState) -> bool:
        if state.get("title"):
            return False                      # 已有标题：整个生命周期只起一次
        messages = state.get("messages") or []
        users = [m for m in messages if self._is_real_user_message(m)]
        ais = [m for m in messages if isinstance(m, AIMessage) and m.content]
        return len(users) == 1 and len(ais) >= 1

    def _build_prompt(self, state: TitleState) -> tuple[str, str]:
        user = next(m for m in state["messages"] if self._is_real_user_message(m))
        ai = next(m for m in state["messages"] if isinstance(m, AIMessage) and m.content)
        user_text = str(user.content)[:200]
        ai_text = str(ai.content)[:200]
        prompt = (
            "用不超过 10 个字给这段对话起个标题，只输出标题本身：\n"
            f"用户：{user_text}\n助手：{ai_text}"
        )
        return prompt, user_text

    def after_model(self, state: TitleState, runtime: Any) -> dict[str, Any] | None:
        if not self._should_generate(state):
            return None
        prompt, user_text = self._build_prompt(state)
        try:
            reply = self._factory(prompt).invoke(prompt)
            title = str(reply.content).strip().strip('"“”')[:24]
        except Exception:                     # 起标题失败不能拖垮对话（本体同策）
            title = user_text[:12] + "…"
        if not title:
            return None
        print(f"  >> [Title] 首轮结束，生成标题：{title}")
        return {"title": title}
