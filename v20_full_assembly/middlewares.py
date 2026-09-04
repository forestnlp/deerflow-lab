"""学习版九大中间件 —— 顺序照抄本体 build_middlewares（agent.py 约 L272 起）。
参考本体 packages/harness/deerflow/agents/lead_agent/agent.py::build_middlewares
与 agents/middlewares/ 目录（一文件一中间件）。

本体顺序（学习版对齐的九环）：
  DynamicContext -> Summarization -> Todo -> TokenUsage -> Title
  -> Memory -> SystemMessageCoalescing -> SubagentLimit -> LoopDetection
  -> Clarification（永远最后）
本版把 SubagentLimit 简化掉（无 task 工具则无意义），保留其余八环 + Clarification。
"""

from __future__ import annotations

import json
from collections import deque

from langchain.agents.middleware import AgentMiddleware, hook_config, wrap_model_call
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES


class DynamicContextMiddleware(AgentMiddleware):
    """日期/记忆拼进首条 HumanMessage，system prompt 保持全静态（前缀缓存友好）。"""

    def __init__(self, memory_text: str = "", **kw):
        super().__init__(**kw)
        self._memory_text = memory_text

    @wrap_model_call
    def inject(self, request, handler):
        reminder = f"<system-reminder>{self._memory_text}</system-reminder>"
        messages = list(request.messages)
        for i, m in enumerate(messages):
            if isinstance(m, HumanMessage) and "<system-reminder>" not in str(m.content):
                messages[i] = m.__class__(content=f"{reminder}\n\n{m.content}", id=m.id)
                break
        return handler(request.override(messages=messages))


class SummarizationMiddleware(AgentMiddleware):
    """历史超过 keep_recent 条就折叠成一条摘要 SystemMessage。

    时机必须早：晚于 Todo 就会把 write_todos 折叠掉（本体为此专门做了
    上下文恢复；学习版把"折叠后 todos 滑出视野"交给 Todo 的提醒机制兜底）。
    """

    def __init__(self, keep_recent: int = 4, **kw):
        super().__init__(**kw)
        self.keep_recent = keep_recent

    def before_model(self, state, runtime):
        messages = state.get("messages") or []
        if len(messages) <= self.keep_recent + 1:
            return None
        old, recent = messages[:-self.keep_recent], messages[-self.keep_recent:]
        parts = []
        for m in old:
            calls = getattr(m, "tool_calls", None)
            if calls:
                parts.append(f"{m.type} 调用 {[c['name'] for c in calls]}")
            elif m.content:
                parts.append(f"{m.type}: {str(m.content)[:40]}")
        summary = SystemMessage(content="<conversation_summary>"
                                + " | ".join(parts[-4:]) + "</conversation_summary>")
        print(f"    >> [Summarization] 折叠 {len(old)} 条旧消息 -> 摘要")
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary, *recent]}


class TodoMiddleware(AgentMiddleware):
    """write_todos 工具 + 防提前退出 + 折叠后提醒（本体 todo_middleware 三件套）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.todos: list[dict] = []
        self._reminded = False
        middleware_self = self

        @tool
        def write_todos(todos: list) -> str:
            """创建/更新任务清单。todos: [{content, status}]，status 取 pending/in_progress/completed。"""
            middleware_self.todos = list(todos)
            done = sum(1 for t in todos if t.get("status") == "completed")
            print(f"    >> [Todo] {done}/{len(todos)} 完成: "
                  + ", ".join(f"{t['content']}[{t['status']}]" for t in todos))
            return f"已记录 {len(todos)} 项，{done} 项完成"

        self.tools = [write_todos]

    def _pending(self) -> list:
        return [t for t in self.todos if t.get("status") != "completed"]

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        msgs = state.get("messages") or []
        last = msgs[-1] if msgs else None
        if (isinstance(last, AIMessage) and not last.tool_calls
                and self._pending() and not self._reminded):
            self._reminded = True
            print("    >> [Todo] 想提前交卷？清单还没跑完 -> 拦回模型")
            return {"messages": [HumanMessage(
                content="<system_reminder>todos 未完成，先更新状态再交卷。</system_reminder>")],
                "jump_to": "model"}
        return None

    def after_agent(self, state, runtime):
        # run 收尾复位，同一个 agent 实例接下一轮时状态干净
        self._reminded = False
        return None


class TokenUsageMiddleware(AgentMiddleware):
    """每轮模型调用后累计 token（真实 usage 优先，fake 用长度估算）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.total = 0

    def after_model(self, state, runtime):
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai is None:
            return None
        usage = getattr(last_ai, "usage_metadata", None) or {}
        tokens = usage.get("total_tokens") or max(1, len(str(last_ai.content)) + 8)
        self.total += tokens
        print(f"    >> [TokenUsage] 本次 ~{tokens}，累计 ~{self.total}")
        return None


class TitleMiddleware(AgentMiddleware):
    """首轮完整往返后生成会话标题（本体用独立 LLM 调用；学习版规则截断演示时机）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.title: str | None = None

    @staticmethod
    def _plain(content: str) -> str:
        """剥掉 DynamicContext 注入的 <system-reminder> 包装，还原用户原话。"""
        text = str(content)
        if "</system-reminder>" in text:
            text = text.split("</system-reminder>", 1)[1].strip()
        return text

    def after_model(self, state, runtime):
        if self.title:
            return None
        messages = state.get("messages") or []
        humans = [self._plain(m.content) for m in messages if m.type == "human"]
        humans = [t for t in humans if t and not t.startswith("<system_reminder")]
        ais = [m for m in messages if isinstance(m, AIMessage) and m.content and not m.tool_calls]
        if humans and ais:
            self.title = humans[-1][:16].strip()
            print(f"    >> [Title] 会话标题：《{self.title}》")
        return None


class MemoryMiddleware(AgentMiddleware):
    """after_agent 时把本轮要点写进记忆文件（本体：防抖抽取 + 旁路注入）。"""

    def __init__(self, store_path, user_id: str = "u1", **kw):
        super().__init__(**kw)
        self.path = store_path
        self.user_id = user_id

    def after_agent(self, state, runtime):
        messages = state.get("messages") or []
        finals = [str(m.content) for m in messages
                  if isinstance(m, AIMessage) and m.content and not m.tool_calls]
        if not finals:
            return None
        fact = f"{self.user_id}: {finals[-1][:60]}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(fact + "\n")
        print(f"    >> [Memory] 写入长期记忆: {fact}")
        return None


class SystemMessageCoalescingMiddleware(AgentMiddleware):
    """发给模型前把多条 SystemMessage 合并为一条前置（严格网关的硬要求）。"""

    @wrap_model_call
    def coalesce(self, request, handler):
        systems = [m for m in request.messages if isinstance(m, SystemMessage)]
        if len(systems) <= 1:
            return handler(request)
        rest = [m for m in request.messages if not isinstance(m, SystemMessage)]
        merged = SystemMessage(content="\n\n".join(str(s.content) for s in systems))
        print(f"    >> [Coalescing] 合并 {len(systems)} 条 SystemMessage -> 1 条前置")
        return handler(request.override(messages=[merged, *rest]))


class LoopDetectionMiddleware(AgentMiddleware):
    """同一 (工具+参数) 连续 window 次 -> 注入止损并踢回模型重想。"""

    def __init__(self, window: int = 3, **kw):
        super().__init__(**kw)
        self.window = window
        self.recent: deque[str] = deque(maxlen=window)
        self.tripped = False

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if not last_ai or not last_ai.tool_calls:
            return None
        for tc in last_ai.tool_calls:
            self.recent.append(f"{tc['name']}:{json.dumps(tc['args'], sort_keys=True, ensure_ascii=False)}")
        if (len(self.recent) == self.window and len(set(self.recent)) == 1
                and not self.tripped):
            self.tripped = True
            print(f"    >> [LoopDetection] 同一调用连续 {self.window} 次 -> 刹车")
            return {"messages": [HumanMessage(
                content="<system_reminder>你在重复同一个调用，停止重试，"
                        "在最终回答里说明障碍。</system_reminder>")],
                "jump_to": "model"}
        return None


class ClarificationMiddleware(AgentMiddleware):
    """永远最后：ask_clarification 一跳，立即结束本轮等用户补充。"""

    @hook_config(can_jump_to=["end"])
    def after_model(self, state, runtime):
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai and any(tc["name"] == "ask_clarification" for tc in (last_ai.tool_calls or [])):
            print("    >> [Clarification] 模型要反问 -> 结束本轮")
            return {"jump_to": "end"}
        return None
