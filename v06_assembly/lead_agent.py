"""v06 核心零件：Lead Agent 总装台。

参考本体 packages/harness/deerflow/agents/lead_agent/agent.py 的 build_middlewares。
那个函数就是本体的"总装图纸"，函数头注释即设计文档（agent.py L261-270）：

    ThreadDataMiddleware must be before SandboxMiddleware ...
    SummarizationMiddleware should be early to reduce context before other processing
    TodoListMiddleware should be before ClarificationMiddleware ...
    ClarificationMiddleware should be last

顺序不是随手排的，是设计。本版把 v02-v05 造的零件按同款顺序装到一台机器上，
每装一件打印一行【装配清单】——这张清单就是本版的确定性信号（不经模型）。

本体链上本版没造的角色（Skills / MCP 路由 / Title / ViewImage / SubagentLimit /
Clarification ...）在 README 附录交代"本体怎么做、本书为何不展开"。
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain.agents import create_agent
from langchain.tools import tool

from shared.model_factory import get_model
from v02_middleware_plan.middlewares import Trace, TodoMiddleware
from v03_sandbox_guard.guards import LoopDetectionMiddleware, ReadBeforeWriteMiddleware
from v03_sandbox_guard.sandbox import LocalSandbox, SandboxError
from v04_context_memory.context_tools import (
    BudgetMiddleware,
    MemoryMiddleware,
    SummarizeMiddleware,
)

# 装配图纸：(来自哪一版, 类, 一句话职责, 对应本体链上的位置)
BLUEPRINT = [
    ("v03", ReadBeforeWriteMiddleware, "沙箱+未读先写否决", "Sandbox/read_before_write：靠前"),
    ("v04", SummarizeMiddleware, "窗口超阈值折叠旧消息", "Summarization：must be early"),
    ("v02", TodoMiddleware, "write_todos+拦提前交卷", "TodoList：before Clarification"),
    ("v04", MemoryMiddleware, "开局注入事实/收场抽事实落盘", "Memory：after Title"),
    ("v03", LoopDetectionMiddleware, "同调用连打->警戒/剥爪", "LoopDetection：靠后"),
    ("v04", BudgetMiddleware, "预算半程预警/超支硬停", "TokenBudget：LoopDetection 之后"),
]


def build_tools(sbx: LocalSandbox):
    """模型的手脚：一个计算器 + 沙箱三件套（都只认虚拟路径）。"""

    @tool
    def calculator(expression: str) -> str:
        """计算算术表达式，例如 (2+3)*7。"""
        return str(eval(expression, {"__builtins__": {}}, {}))

    @tool
    def read_file(path: str) -> str:
        """读取虚拟路径文件全文，path 形如 /mnt/user-data/outputs/note.md。"""
        try:
            return sbx.read(path)
        except SandboxError as e:
            return f"Error: {e}"

    @tool
    def write_file(path: str, content: str) -> str:
        """把 content 全文写入虚拟路径 path（覆盖写）。"""
        try:
            return sbx.write(path, content)
        except SandboxError as e:
            return f"Error: {e}"

    @tool
    def bash(command: str) -> str:
        """在沙箱根目录执行 shell 命令，输出中的敏感信息会被遮码。"""
        return sbx.bash(command)

    return [calculator, read_file, write_file, bash]


def make_lead_agent(data_dir: Path, checkpointer=None, memory_seed: list[str] | None = None):
    """总装一台全链 Lead Agent，返回 (agent, 中间件实例清单)。

    本体同款顺序（见 BLUEPRINT 第 4 列）；每装一件打一行清单。
    checkpointer 由调用方传入（v05 的 SqliteSaver）——机器只有一台，
    各入口靠 thread_id 分会话，全靠它。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    sbx = LocalSandbox(root=data_dir / "sandbox", injected_secrets={})
    trace = Trace()

    memory_path = data_dir / "memory.json"
    if memory_seed and not memory_path.is_file():
        # 演示用：预置几条"上一次会话攒下的事实"，模拟跨对话记忆
        memory_path.write_text(json.dumps(memory_seed, ensure_ascii=False), encoding="utf-8")

    parts = [
        ReadBeforeWriteMiddleware(sbx, trace.lines),
        SummarizeMiddleware(trigger_chars=100_000, keep_recent=4),   # 本版任务短，只挂不发
        TodoMiddleware(trace),
        MemoryMiddleware(memory_path),
        LoopDetectionMiddleware(trace.lines),
        BudgetMiddleware(budget_chars=500_000),
    ]
    assert [type(p) for p in parts] == [row[1] for row in BLUEPRINT], "零件与图纸顺序不一致"

    print("[装配] Lead Agent 中间件链（自上而下 = 本体 build_middlewares 同款顺序）：")
    for i, (origin, _cls, duty, where) in enumerate(BLUEPRINT, 1):
        print(f"  #{i} [{origin}] {duty:<22} <- 本体位置：{where}")
    print(f"[装配] 共 {len(parts)} 件 + 工具 {len(build_tools(sbx))} 个（计算器/读/写/bash）\n")

    agent = create_agent(
        model=get_model(),
        tools=build_tools(sbx),
        middleware=parts,
        checkpointer=checkpointer,
    )
    return agent, parts
