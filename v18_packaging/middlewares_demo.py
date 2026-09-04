"""本版中间件零件 —— config.yaml 的 middlewares 段反射装配它们。
参考本体 agents/middlewares/ 一目录一中间件、build_middlewares 按序装配。
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage


class EchoUsageMiddleware(AgentMiddleware):
    """每次模型调用后打一行用量（演示 v02 钩子洋葱圈的可装配性）。"""

    def __init__(self, tag: str = "usage", **kw):
        super().__init__(**kw)
        self.tag = tag
        self.calls = 0

    def after_model(self, state, runtime):
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai is None:
            return None
        self.calls += 1
        n = len(last_ai.tool_calls or [])
        print(f"  >> [{self.tag}] 第 {self.calls} 次模型调用，tool_calls={n}")
        return None


class StampMiddleware(AgentMiddleware):
    """给首条用户消息盖一个"配置装配成功"的时间戳（纯演示横切注入）。"""

    def __init__(self, stamp: str = "dfl-v18", **kw):
        super().__init__(**kw)
        self.stamp = stamp

    def before_model(self, state, runtime):
        # 只在第一轮打一次标记，之后静默；真实场景这里做的是租户/审计注入
        if not getattr(self, "_done", False):
            self._done = True
            print(f"  >> [{self.stamp}] 中间件已在请求链上")
        return None
