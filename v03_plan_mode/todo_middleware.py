"""v03 核心零件：TodoMiddleware —— 会拆任务、不许偷懒、断了能续。

参考本体 packages/harness/deerflow/agents/middlewares/todo_middleware.py
      （class TodoMiddleware(TodoListMiddleware)，约 355 行；
        学习版保留全部可观察行为，去掉多 run 并发的线程安全簿记）

三个 hook 各管一段制度：
- before_model（上下文丢失恢复）：todos 还在 state 里、但原始 write_todos
  调用已被摘要截出上下文 -> 注入 name="todo_reminder" 的隐藏 HumanMessage，
  把清单重新"提醒"给模型。这条提醒**会**落盘进历史。
- after_model（防提前交卷）：模型给出无 tool_calls 的终稿、但还有未完成
  todo -> 返回 {"jump_to": "model"} 把执行流弹回 model 节点。上限 2 次防死循环。
- wrap_model_call（提醒投递）：被拦截时排队的提醒文本，只在下一次请求里
  临时拼进去，不作为普通消息落盘——控制指令不该出现在用户可见的存档里。
"""

from __future__ import annotations

from typing import Any, NotRequired

from langchain.agents.middleware.todo import PlanningState, Todo, TodoListMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, HumanMessage

MAX_COMPLETION_REMINDERS = 2  # 与本体 _MAX_COMPLETION_REMINDERS 同值


class PlanState(PlanningState):
    """给图状态加 todos 键，并允许它作为输入（学习版断点续做用）。

    父类的 todos 带 OmitFromInput（只进不出），学习版重新声明一次去掉
    该标记：main.py 模拟"压缩后重开一局"时要能把旧 todos 塞回 state。
    本体靠 checkpointer 跨 run 延续 state，不需要这个口子。
    """

    todos: NotRequired[list[Todo]]


def _fmt(todos: list[Todo]) -> str:
    return "\n".join(f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in todos)


def format_completion_reminder(todos: list[Todo]) -> str:
    """未完成清单的催办文案（本体 _format_completion_reminder 同款结构）。"""
    incomplete = [t for t in todos if t.get("status") != "completed"]
    return (
        "<system_reminder>\n"
        "You have incomplete todo items that must be finished before giving your final response:\n\n"
        f"{_fmt(incomplete)}\n\n"
        "Please continue working on these tasks. Call `write_todos` to mark items as completed\n"
        "</system_reminder>"
    )


class TodoMiddleware(TodoListMiddleware):
    """本体 todo_middleware.py 的学习版：三个 hook、一套规划制度。"""

    state_schema = PlanState

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[str] = []   # 排队等投递的催办文案
        self._count = 0                 # 本次 run 已拦截次数

    def before_model(self, state: PlanState, runtime: Any) -> dict[str, Any] | None:
        todos = state.get("todos") or []
        if not todos:
            return None
        messages = state.get("messages") or []
        has_write_call = any(
            tc.get("name") == "write_todos"
            for m in messages
            if isinstance(m, AIMessage)
            for tc in (m.tool_calls or [])
        )
        already = any(
            isinstance(m, HumanMessage) and getattr(m, "name", None) == "todo_reminder"
            for m in messages
        )
        if has_write_call or already:
            return None
        print("  >> [TodoMiddleware] write_todos 已滑出上下文 -> 注入 todo_reminder")
        return {"messages": [HumanMessage(
            name="todo_reminder",
            additional_kwargs={"hide_from_ui": True},
            content=(
                "<system_reminder>\n"
                "Your todo list is no longer visible in the current context window, "
                f"but it is still active:\n\n{_fmt(todos)}\n"
                "</system_reminder>"
            ),
        )]}

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: PlanState, runtime: Any) -> dict[str, Any] | None:
        base = super().after_model(state, runtime)  # 父类职责：并行 write_todos 检测
        if base is not None:
            return base
        messages = state.get("messages") or []
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if not last_ai or last_ai.tool_calls:
            return None  # 还想调工具/刚调完，不算交卷
        todos = state.get("todos") or []
        if not todos or all(t.get("status") == "completed" for t in todos):
            return None  # 全部干完（或压根没计划）—— 放行
        if self._count >= MAX_COMPLETION_REMINDERS:
            print(f"  >> [TodoMiddleware] 催办已满 {MAX_COMPLETION_REMINDERS} 次仍交卷 -> 放行（防死循环）")
            return None
        self._count += 1
        self._pending.append(format_completion_reminder(todos))
        print(f"  >> [TodoMiddleware] 拦截提前交卷 #{self._count} -> jump_to=model")
        return {"jump_to": "model"}

    def wrap_model_call(self, request, handler):
        if self._pending:
            merged = "\n\n".join(dict.fromkeys(self._pending))  # 去重保序（本体同款）
            self._pending = []
            print("  >> [TodoMiddleware] 催办随本次请求投递（不落盘）")
            request = request.override(messages=[
                *request.messages,
                HumanMessage(content=merged, name="todo_completion_reminder",
                             additional_kwargs={"hide_from_ui": True}),
            ])
        return handler(request)
