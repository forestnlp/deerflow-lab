"""TokenBudgetMiddleware —— 参考本体
packages/harness/deerflow/agents/middlewares/token_budget_middleware.py
（after_model 汇总本轮 run 的 usage_metadata → 比例 ≥ warn_threshold 排队警告、
 ≥ hard_stop_threshold 剥掉 tool_calls 强制收尾；警告经 wrap_model_call 在下次
 模型请求时以 HumanMessage 注入，保住 tool_calls↔ToolMessage 配对；
 硬停不抛异常，只记 stop_reason=token_capped 供 executor 消费）。

学习版差异：真实 token 换成"消息字符数"近似（--fake 下 usage_metadata 恒空，
真实路径下换成 msg.usage_metadata 求和即可，两级闸的语义一个字节不用改）；
run_id 簿记、BoundedDict、子代理回溯归集砍掉。
"""

from __future__ import annotations

import threading
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from summarization_middleware import estimate_chars

_WARN_MSG = ("[TOKEN BUDGET WARNING] 本次 run 已用 {used} / 预算 {budget}"
             "（{percent:.0f}%）。请收尾，别再开新工具调用。")
_EXCEEDED_MSG = ("[TOKEN BUDGET EXCEEDED] 用量 {used} 超过安全线 {budget}。"
                 "只能用手头结果交卷。")


class LabTokenBudgetMiddleware(AgentMiddleware):
    """两级保险丝：先礼（预警随下次请求送达）后兵（剥 tool_calls 强制交卷）。"""

    def configure(self, budget: int, warn_ratio: float, hard_ratio: float) -> None:
        """场景切换时换一档预算（演示用；生产一档跑到底）。"""
        with self._lock:
            self._budget, self._warn, self._hard = budget, warn_ratio, hard_ratio

    def __init__(self, budget: int = 10**9, warn_ratio: float = 0.5, hard_ratio: float = 0.8) -> None:
        super().__init__()
        self._budget = budget
        self._warn = warn_ratio
        self._hard = hard_ratio
        self._lock = threading.Lock()
        self._seen_ids: set[str] = set()      # 上一世（run 前）就有的消息不算本单
        self._used = 0
        self._warned = False
        self._pending: list[str] = []
        self.stop_reason: str | None = None

    def reset(self) -> None:
        with self._lock:
            self._seen_ids, self._used = set(), 0
            self._warned, self._pending, self.stop_reason = False, [], None

    def before_agent(self, state, runtime) -> None:
        """run 开始前把存量消息全标记为'已见过'：预算只管本 run 花的钱。"""
        with self._lock:
            for m in state.get("messages") or []:
                if isinstance(m, AIMessage):
                    self._seen_ids.add(m.id)

    def _bill(self, state) -> int:
        """给本 run 新增的每次模型调用记一笔"输入侧"字符账。

        近似口径：真实 API 每次调用按**整个上下文**收 input token 费，所以
        新回复出现时，把它之前全部消息的字符数记入账单（本体读
        usage_metadata.input_tokens，口径相同；输出侧不并进来，两级闸的
        语义不受影响）。
        """
        with self._lock:
            messages = state.get("messages") or []
            if messages:
                last = messages[-1]
                if isinstance(last, AIMessage) and last.id not in self._seen_ids:
                    self._seen_ids.add(last.id)
                    self._used += estimate_chars(messages[:-1])
            return self._used

    def after_model(self, state, runtime) -> dict[str, Any] | None:
        used = self._bill(state)
        ratio = used / self._budget
        last = (state.get("messages") or [])[-1]
        if ratio >= self._hard:
            print(f"  >> [TokenBudget] 硬停：{used}/{self._budget} "
                  f"({ratio:.0%})，剥掉 tool_calls 强制交卷")
            self.stop_reason = "token_capped"
            stop_msg = _EXCEEDED_MSG.format(used=used, budget=self._budget)
            content = f"{last.content}\n\n{stop_msg}" if last.content else stop_msg
            stripped = last.model_copy(update={
                "content": content, "tool_calls": [],
                "response_metadata": {**(last.response_metadata or {}),
                                      "finish_reason": "stop"},
            })
            return {"messages": [stripped]}   # 同 id 替换：add_messages 语义
        if ratio >= self._warn and not self._warned:
            self._warned = True
            self._pending.append(_WARN_MSG.format(
                used=used, budget=self._budget, percent=ratio * 100))
            print(f"  >> [TokenBudget] 预警：{used}/{self._budget} ({ratio:.0%})，"
                  f"提醒随下次模型请求投递")
        return None

    def wrap_model_call(self, request, handler):
        with self._lock:
            pending, self._pending = self._pending, []
        if pending:
            note = HumanMessage(content="\n\n".join(pending), name="budget_warning",
                                additional_kwargs={"hide_from_ui": True})
            request = request.override(messages=[*request.messages, note])
        return handler(request)
