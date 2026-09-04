"""task 工具 —— 参考本体 packages/harness/deerflow/tools/builtins/task_tool.py::task_tool
与 subagents/executor.py（后台执行 + 轮询到终态 + 终态清理）。

主 agent 只要这一把工具，就能把子任务外包给"受限的子 agent"：
查注册表 → 后台起跑 → 轮询到终态 → 把事件流水和结果打包成工具返回值。
本体在轮询期间还会把 task_started/running/completed 推成 SSE 事件，
学习版攒进 SubagentResult.events，由 ToolMessage 一次性带回。
"""

from __future__ import annotations

from typing import Annotated

from langchain.tools import InjectedToolCallId, tool

from executor import SubagentExecutor, cleanup, wait_terminal
from subagents import available_names, get_subagent_config

# 模型工厂由 main.py 启动时注入：(模型名, 任务, 每步延迟ms) → ChatModel。
# 工具签名里不带模型，模型策略（--fake 脚本 / 真实便宜模型）留在应用层。
_model_factory = None


def configure(model_factory) -> None:
    global _model_factory
    _model_factory = model_factory


@tool
def task(description: str, prompt: str, subagent_type: str,
         tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
    """把一件边界清楚的子任务外包给专职子代理，等它干完把结果带回。

    Args:
        description: 一句话说明这次委派要达成什么。
        prompt: 交给子代理的完整任务书——子代理看不到主对话，要写就写全。
        subagent_type: 子代理类型名，只能是注册表里登记过的。
    """
    cfg = get_subagent_config(subagent_type)
    if cfg is None:
        # 本体同款：未知类型不抛异常，把"可用类型"回给模型，让它自己纠正。
        return f"未知子代理类型 {subagent_type!r}，可用：{', '.join(available_names())}。任务未执行。"
    if _model_factory is None:
        return "task 工具尚未配置模型工厂（main.py 未调用 configure）。任务未执行。"

    executor = SubagentExecutor(cfg, _model_factory)
    task_id = f"task-{tool_call_id}"          # 用 tool_call_id 做任务号，输出可复现
    executor.execute_async(prompt, task_id=task_id)
    r = wait_terminal(task_id)
    if r is None:
        return f"任务 {task_id} 登记丢失（不应发生）。"

    lines = [f"[{task_id}] {r.subagent} {r.status.value}（{r.steps} 轮）"]
    lines += [f"  · {e}" for e in r.events]
    if r.result is not None:
        lines.append(f"结果: {r.result}")
    if r.error is not None:
        lines.append(f"错误: {r.error}")
    cleanup(task_id)                           # 终态才清理，本体同款
    return "\n".join(lines)
