"""v09 核心零件：四枚行为护栏 —— 转圈、悬空、说半截、硬往上冲，一种一个接法。

参考本体
  agents/middlewares/loop_detection_middleware.py
      LoopDetectionMiddleware：after_model 哈希最近 N 次 tool_calls（name+稳定
      参数键），滑窗计数 ≥ warn 排队警告、≥ hard 剥 tool_calls 强制交卷；
      警告延迟到 wrap_model_call 注入（保 tool_call 配对），硬停记 stop_reason。
  agents/middlewares/dangling_tool_call_middleware.py
      DanglingToolCallMiddleware：wrap_model_call 扫描请求消息，为无配对的
      AIMessage.tool_calls 就地插入合成错误 ToolMessage、丢弃孤儿 ToolMessage。
      用 wrap 不用 before_model：补丁要插在 offending AIMessage 的正后方，
      不能像 before_model+reducer 那样只追加到队尾。
  agents/middlewares/safety_finish_reason_middleware.py + safety_termination_detectors.py
      SafetyFinishReasonMiddleware：after_model 检测 finish_reason=content_filter
      / refusal / SAFETY 等供应商安全终止，抑制可疑 tool_calls、回填空内容，
      记 stop_reason=safety_capped，不抛异常。
  agents/middlewares/clarification_middleware.py + tools/builtins/clarification_tool.py
      ClarificationMiddleware：wrap_tool_call 拦 ask_clarification，工具本体
      不执行，伪造一条提问回执并中断执行把问题呈现给用户（本体用 Command 中断，
      学习版用 jump_to=end，语义同为"这一世到此为止，答案下一世再说"）。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, ToolMessage

# ------------------------------------------------------------- ① 循环检测
_HARD_STOP_MSG = "[FORCED STOP] 重复工具调用触顶，请用手头结果直接交卷。"
_WARNING_MSG = "[LOOP DETECTED] 你在原地打转，停止新调用，立刻产出最终回答。"


def _hash_tool_calls(tool_calls: list[dict]) -> str:
    """name + 稳定参数键 → 顺序无关的 12 位哈希（本体 _hash_tool_calls 同款思路）。"""
    normalized = sorted(f"{tc.get('name','')}:{json.dumps(tc.get('args') or {}, sort_keys=True, default=str)}"
                        for tc in tool_calls)
    return hashlib.md5(json.dumps(normalized).encode()).hexdigest()[:12]


class LoopDetectionMiddleware(AgentMiddleware):
    """滑窗计数相同的 tool_call 集合：warn 劝返，hard 剥爪。"""

    def __init__(self, warn_threshold: int = 3, hard_limit: int = 5, window: int = 20) -> None:
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window = window
        self._history: list[str] = []
        self._warned: set[str] = set()
        self._pending: list[str] = []
        self.stop_reason: str | None = None

    def reset(self) -> None:
        self._history, self._warned, self._pending, self.stop_reason = [], set(), [], None

    def after_model(self, state, runtime) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return None
        call_hash = _hash_tool_calls(last.tool_calls)
        self._history.append(call_hash)
        del self._history[:-self.window]
        count = self._history.count(call_hash)
        if count >= self.hard_limit:
            print(f"  >> [LoopDetection] 同一调用出现 {count} 次 ≥ {self.hard_limit} "
                  f"→ 剥掉 tool_calls 强制交卷")
            self.stop_reason = "loop_capped"
            content = f"{last.content}\n\n{_HARD_STOP_MSG}".strip()
            stripped = last.model_copy(update={
                "content": content, "tool_calls": [],
                "response_metadata": {**(last.response_metadata or {}),
                                      "finish_reason": "stop"}})
            return {"messages": [stripped]}
        if count >= self.warn_threshold and call_hash not in self._warned:
            self._warned.add(call_hash)
            print(f"  >> [LoopDetection] 同一调用出现 {count} 次 ≥ {self.warn_threshold} "
                  f"→ 警告排队，随下次请求投递")
            self._pending.append(_WARNING_MSG)
        return None

    def wrap_model_call(self, request, handler):
        if self._pending:
            from langchain_core.messages import HumanMessage
            note = HumanMessage(content="\n\n".join(self._pending), name="loop_warning",
                                additional_kwargs={"hide_from_ui": True})
            self._pending = []
            request = request.override(messages=[*request.messages, note])
        return handler(request)


# ------------------------------------------------------------- ② 悬空修复
class DanglingToolCallMiddleware(AgentMiddleware):
    """wrap_model_call：无配对的 tool_calls 就地补合成回执，孤儿回执直接丢弃。

    只改**本次请求**（override），图状态里的原始历史一字不动——本体同名
    中间件同语义："Persisted state is untouched; this only affects the
    single model call."
    """

    @staticmethod
    def _patch(messages: list) -> tuple[list | None, int, int]:
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        asked = {tc["id"] for m in messages if isinstance(m, AIMessage)
                 for tc in (m.tool_calls or [])}
        patched, fixed, dropped = [], 0, 0
        for msg in messages:
            if isinstance(msg, ToolMessage) and msg.tool_call_id not in asked:
                dropped += 1                        # 孤儿回执：父调用已不在 → 丢
                continue
            patched.append(msg)
            for tc in (getattr(msg, "tool_calls", None) or []):
                if tc["id"] not in answered:
                    patched.append(ToolMessage(
                        content="[Tool call was interrupted and did not return a result.]",
                        tool_call_id=tc["id"], name=tc.get("name", "unknown"),
                        status="error"))
                    fixed += 1
        if not fixed and not dropped:
            return None, 0, 0
        return patched, fixed, dropped

    def wrap_model_call(self, request, handler):
        patched, fixed, dropped = self._patch(list(request.messages))
        if patched is None:
            return handler(request)
        print(f"  >> [DanglingToolCall] 补合成回执 {fixed} 条、丢孤儿回执 {dropped} 条"
              f"（补丁插在 offending AIMessage 正后方，只改本次请求）")
        return handler(request.override(messages=patched))


# ------------------------------------------------------------- ③ 安全终止
_UNSAFE_FINISH = {"content_filter", "refusal", "safety", "SAFETY"}
_SAFETY_NOTE = "（响应被供应商安全策略截断，以上为已产出的部分，后续内容不可得。）"


class SafetyFinishReasonMiddleware(AgentMiddleware):
    """finish_reason 异常时：剥可疑 tool_calls、空内容回填说明，绝不抛异常。"""

    def __init__(self) -> None:
        super().__init__()
        self.stop_reason: str | None = None

    def after_model(self, state, runtime) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage):
            return None
        reason = str((last.response_metadata or {}).get("finish_reason", ""))
        if reason.lower() not in {r.lower() for r in _UNSAFE_FINISH}:
            return None
        print(f"  >> [SafetyFinish] finish_reason={reason!r} 是安全终止 "
              f"→ 剥 tool_calls、追加截断说明（不抛异常）")
        self.stop_reason = "safety_capped"
        text = str(last.content).strip() or "（模型没有产出任何可见内容。）"
        patched = last.model_copy(update={
            "content": f"{text}\n\n{_SAFETY_NOTE}", "tool_calls": [],
            "response_metadata": {**(last.response_metadata or {}),
                                  "finish_reason": "stop"}})
        return {"messages": [patched]}


# ------------------------------------------------------------- ④ 澄清拦截
class ClarificationMiddleware(AgentMiddleware):
    """拦下 ask_clarification：工具不执行，提问回执落盘，本轮立即收束。"""

    def __init__(self) -> None:
        super().__init__()
        self._interrupt = False

    def wrap_tool_call(self, request, handler):
        if request.tool_call.get("name") != "ask_clarification":
            return handler(request)
        question = (request.tool_call.get("args") or {}).get("question", "")
        print(f"  >> [Clarification] 拦截 ask_clarification（工具未执行）→ 本轮收束")
        self._interrupt = True
        return ToolMessage(content=f"[向用户提问] {question}",
                           tool_call_id=str(request.tool_call.get("id", "")),
                           name="ask_clarification")

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        # 澄清轮本来就不该有下一次模型调用——回执落盘后直接收束，
        # 模型剧本里残留的后续消息永远轮不到执行。
        if self._interrupt:
            self._interrupt = False
            return {"jump_to": "end"}
        return None
