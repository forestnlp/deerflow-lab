"""DeferredToolFilterMiddleware —— 参考本体
packages/harness/deerflow/agents/middlewares/deferred_tool_filter_middleware.py

本体原文注释：
  "ToolNode still holds all tools (including deferred) for execution routing,
   hide deferred tool schemas from model binding."
两个 hook 各管一头：
  wrap_model_call —— 从绑定给模型的 tools 清单里滤掉未提升工具的 schema
                     （模型"看不见"它，才不会去调）
  wrap_tool_call  —— 模型真凭猜测调了它：伪造一条 error ToolMessage，告诉它
                     先 tool_search / load_tools 提升再试（学习版按组提升）
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

import tools as tool_registry


class DeferredToolFilterMiddleware(AgentMiddleware):
    """工具按需上岗：schema 先藏后放，硬调必被拦。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen_tool_names: list[list[str]] = []   # 每次模型调用时的可见清单

    def _filter_tools(self, request):
        if not tool_registry.GROUPS:
            return request
        allowed = {n for n in tool_registry.visible_tool_names()}
        kept = [t for t in request.tools if getattr(t, "name", None) in allowed]
        if len(kept) == len(request.tools):
            return request
        return request.override(tools=kept)

    def wrap_model_call(self, request, handler):
        request = self._filter_tools(request)
        names = sorted(getattr(t, "name", "?") for t in (request.tools or []))
        self.seen_tool_names.append(names)
        print(f"  >> [DeferredTools] 本轮模型可见工具: {names}")
        return handler(request)

    def wrap_tool_call(self, request, handler):
        blocked = tool_registry.block_unpromoted(request)
        if blocked is not None:
            print(f"  >> [DeferredTools] 否决未放行工具: {request.tool_call.get('name')}")
            return blocked
        return handler(request)
