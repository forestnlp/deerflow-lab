"""v03 核心零件二：护栏中间件（否决与刹车都长在框架管道里，不长在工具里）。

参考本体 packages/harness/deerflow/agents/middlewares/ 的
read_before_write / loop_detection 两个中间件。

- ReadBeforeWrite：wrap_tool_call 里查读戳。没读过就想覆盖 -> 直接伪造一条
  ToolMessage 返回（handler 压根不调用 = 工具没执行 = 一票否决）。
- LoopDetection：after_model 数"同一工具+同参数"的连续出现次数。
  到警戒线提醒，到硬上限剥爪（抹掉 tool_calls 强制本轮变成终稿）。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage


class ReadBeforeWriteMiddleware(AgentMiddleware):
    def __init__(self, sandbox, trace: list[str]) -> None:
        super().__init__()
        self.sbx = sandbox
        self.trace = trace

    def wrap_tool_call(self, request, handler):
        tc = request.tool_call
        if tc["name"] == "write_file":
            path = tc["args"].get("path", "")
            cur = self.sbx.current_hash(path)          # 文件现身的哈希
            if cur is not None and self.sbx.stamp_of(path) != cur:
                # 文件存在且自上次读后变过（或从没读过）-> 否决，工具不执行
                self.trace.append(f"[护栏] 否决未读先写: {path}")
                return ToolMessage(
                    content=(f"write_file 被拒：{path} 已存在且你未读过最新版本。"
                             "请先 read_file 再改。"),
                    tool_call_id=tc["id"])
        return handler(request)


class LoopDetectionMiddleware(AgentMiddleware):
    def __init__(self, trace: list[str], warn: int = 3, hard: int = 5) -> None:
        super().__init__()
        self.trace = trace
        self.warn, self.hard = warn, hard
        self.stop_reason: str | None = None

    def after_model(self, state, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai is None or not last_ai.tool_calls:
            return None
        sig = str([(tc["name"], tc["args"]) for tc in last_ai.tool_calls])
        # 从历史倒扫：连续多少个模型轮在发同一份"点菜单"
        streak = 0
        for m in reversed(messages):
            if not isinstance(m, AIMessage):
                continue
            if str([(tc["name"], tc["args"]) for tc in (m.tool_calls or [])]) != sig:
                break
            streak += 1
        if streak >= self.hard:
            self.stop_reason = f"同一调用连续 {streak} 次，触发硬停"
            self.trace.append(f"[护栏] 循环硬停：剥掉 tool_calls 强制交卷（连续第 {streak} 次）")
            clean = AIMessage(
                content=(last_ai.content or "") + "\n（系统注：检测到死循环，已强制收尾）",
                id=last_ai.id)                       # 同 id = 顶掉原消息（add_messages 语义）
            return {"messages": [clean]}
        if streak == self.warn:
            self.trace.append(f"[护栏] 循环警戒：同一调用已连续 {streak} 次（{self.hard} 次剥爪）")
        return None
