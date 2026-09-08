"""v02 核心零件：洋葱圈日志 + 规划中间件。

参考本体 packages/harness/deerflow/agents/middlewares/（总论），
todo 部分对应 packages/harness/deerflow/agents/middlewares/todo_middleware.py。

AgentMiddleware 的钩子分两类：
- 洋葱型 wrap_*：包在模型/工具调用外面，进可改请求、出可观答复；
- 节点型 before_*/after_*：在图的固定节点上改 state。after_model 返回
  {"jump_to": "model"} 可把执行流弹回模型节点——防提前交卷就靠它。
"""

from __future__ import annotations

from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.todo import PlanningState, Todo, TodoListMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, HumanMessage

MAX_COMPLETION_REMINDERS = 2      # 与本体 _MAX_COMPLETION_REMINDERS 同值


class Trace:
    """本次 run 的洋葱圈流水账（进程内存打印，不落盘）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, text: str) -> None:
        self.lines.append(text)


class OnionLogger(AgentMiddleware):
    """最简洋葱圈：每次模型调用/工具调用前后各记一行。"""

    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self.trace = trace

    def wrap_model_call(self, request, handler):
        self.trace.add(f"model 进（历史 {len(request.messages)} 条）")
        response = handler(request)
        calls = [tc["name"] for tc in (response.result[0].tool_calls or [])]
        self.trace.add(f"model 出 -> {'调 ' + ','.join(calls) if calls else '交卷'}")
        return response

    def wrap_tool_call(self, request, handler):
        name = request.tool_call["name"]
        self.trace.add(f"  tool 进 {name}")
        result = handler(request)
        self.trace.add(f"  tool 出 {name}")
        return result


class PlanState(PlanningState):
    """加 todos 键的图状态（父类已声明 write_todos 所需结构）。"""

    todos: NotRequired[list[Todo]]


def _fmt(todos: list[Todo]) -> str:
    return "\n".join(f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in todos)


class TodoMiddleware(TodoListMiddleware):
    """规划制度三件套：给工具（write_todos 由父类注入）、盯销账、拦交卷。"""

    state_schema = PlanState

    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self.trace = trace
        self._count = 0

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: PlanState, runtime: Any) -> dict[str, Any] | None:
        base = super().after_model(state, runtime)   # 父类职责：并行 write_todos 检测
        if base is not None:
            return base
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if not last_ai or last_ai.tool_calls:
            return None                              # 还想调工具，不算交卷
        todos = state.get("todos") or []
        if not todos or all(t.get("status") == "completed" for t in todos):
            return None                              # 干完了（或压根没列计划）—— 放行
        if self._count >= MAX_COMPLETION_REMINDERS:
            self.trace.add(f"[Todo] 催办满 {MAX_COMPLETION_REMINDERS} 次仍交卷 -> 放行")
            return None
        self._count += 1
        self.trace.add(f"[Todo] 还有未销账 todo，拦截提前交卷 #{self._count} -> jump_to=model")
        reminder = ("<system_reminder>\n还有未完成的 todo，先干完再交卷：\n"
                    f"{_fmt([t for t in todos if t.get('status') != 'completed'])}\n"
                    "</system_reminder>")
        # 催办作为一条普通消息补进历史（严格网关要求请求里必有 user 消息，
        # SystemMessage 折叠进去会报 No user query found——在线实测踩过）
        return {"messages": [HumanMessage(content=reminder, name="todo_reminder")],
                "jump_to": "model"}
