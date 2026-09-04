"""SummarizationMiddleware —— 参考本体
packages/harness/deerflow/agents/middlewares/summarization_middleware.py
（DeerFlowSummarizationMiddleware(SummarizationMiddleware)：before_model 判定
 上下文超限 → _maybe_summarize 返回
 [RemoveMessage(REMOVE_ALL_MESSAGES), 摘要消息, *保留消息] 整体替换消息通道；
 摘要以 HumanMessage(name="summary") 形态存在，见 _summary_count_message；
 带 previous_summary 滚动合并，旧摘要不会丢）。

学习版差异：token 计数换成字符估算；模型多候选/重试、RunJournal 事件、
动态上下文提醒的保留逻辑砍掉；折叠点的 tool_call 配对对齐保留（本体靠
DanglingToolCall 中间件在请求边界兜底，学习版直接在切点处理）。
"""

from __future__ import annotations

from typing import Any, Callable, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

SUMMARY_NAME = "summary"   # 本体同名机制


class ContextState(AgentState):
    compactions: NotRequired[int]


def estimate_chars(messages: list[AnyMessage]) -> int:
    """--fake 下的 token 近似：1 字符 ≈ 1 token（讲义里说清楚这是学习版口径）。"""
    total = 0
    for m in messages:
        total += len(str(m.content))
        total += len(str(getattr(m, "tool_calls", None) or ""))
    return total


class LabSummarizationMiddleware(AgentMiddleware):
    """before_model 触发：超限就把旧消息折叠成一条摘要消息，替换整个消息通道。"""

    state_schema = ContextState

    def __init__(self, summary_model_factory: Callable[[str], Any],
                 trigger_chars: int = 600, keep_messages: int = 4) -> None:
        super().__init__()
        self._factory = summary_model_factory
        self._trigger = trigger_chars
        self._keep = keep_messages

    @staticmethod
    def _find_previous_summary(messages: list[AnyMessage]) -> str | None:
        for m in reversed(messages):
            if isinstance(m, HumanMessage) and getattr(m, "name", None) == SUMMARY_NAME:
                return str(m.content)
        return None

    def _split_point(self, messages: list[AnyMessage]) -> int:
        """切点 = len - keep；但保留段开头不能是 ToolMessage（父 AIMessage 会被
        折走，留下无配对的孤儿），遇到就往折叠段让。"""
        cut = max(0, len(messages) - self._keep)
        # 保留段以 ToolMessage 开头 = 父 AIMessage 被折走、孩子成了孤儿；
        # 遇 tool 行一律前移，切点必然落在合法的 AIMessage 边界上。
        while cut < len(messages) and getattr(messages[cut], "type", None) == "tool":
            cut += 1
        return cut

    def before_model(self, state: ContextState, runtime: Any) -> dict[str, Any] | None:
        messages = list(state.get("messages") or [])
        used = estimate_chars(messages)
        if used < self._trigger or len(messages) <= self._keep + 1:
            return None
        if getattr(messages[-1], "name", None) == SUMMARY_NAME:
            return None                 # 刚折完就又被调起？不该再折
        cut = self._split_point(messages)
        if cut <= 0:
            return None
        old, kept = messages[:cut], messages[cut:]
        previous = self._find_previous_summary(old)
        gist = "\n".join(f"{type(m).__name__}: {str(m.content)[:60]}" for m in old
                         if getattr(m, "name", None) != SUMMARY_NAME)
        prompt = f"总结以下对话要点（≤60字）：\n{gist}"
        if previous:
            prompt += f"\n（已有旧摘要，合并进来：{previous}）"
        summary = str(self._factory(prompt).invoke(prompt).content).strip()
        n = int(state.get("compactions") or 0) + 1
        print(f"  >> [Summarization] 估算 {used} 字符 ≥ 阈值 {self._trigger}："
              f"折叠 {len(old)} 条旧消息 → 摘要 1 条，保留 {len(kept)} 条（第 {n} 次压缩）")
        return {"compactions": n, "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            HumanMessage(content=summary, name=SUMMARY_NAME),
            *kept,
        ]}
