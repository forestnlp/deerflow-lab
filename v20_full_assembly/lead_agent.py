"""lead_agent —— 按本体 build_middlewares 的顺序装配学习版最小链。
参考本体 packages/harness/deerflow/agents/lead_agent/agent.py::build_middlewares。

本体链（约 L272 起，节选）与本版对应：
  build_lead_runtime_middlewares   -> 省略（ThreadData/Sandbox/ToolError，见对账表）
  DynamicContext  L325             -> DynamicContextMiddleware
  Skill*/DurableContext L330-365   -> 省略
  Summarization   L368             -> SummarizationMiddleware
  Todo            L375             -> TodoMiddleware（plan mode 常开）
  TokenUsage      L380             -> TokenUsageMiddleware
  Title           L384             -> TitleMiddleware
  Memory          L392             -> MemoryMiddleware
  ViewImage       L397             -> 省略（非视觉演示）
  MCP routing/Deferred L402-415    -> 省略（v16 已单独演示）
  Coalescing      L420             -> SystemMessageCoalescingMiddleware
  SubagentLimit   L429             -> 省略（本版无 task 工具）
  LoopDetection   L433             -> LoopDetectionMiddleware
  TokenBudget     L439             -> 省略
  Terminal/Safety L454-469         -> 省略
  Clarification   L472             -> ClarificationMiddleware（永远最后）

行号是写讲义当时的近似值，会随本体演进漂移——以 路径::符号 为准。
"""

from __future__ import annotations

from pathlib import Path

from langchain.agents import create_agent

from middlewares import (
    ClarificationMiddleware,
    DynamicContextMiddleware,
    LoopDetectionMiddleware,
    MemoryMiddleware,
    SummarizationMiddleware,
    SystemMessageCoalescingMiddleware,
    TitleMiddleware,
    TodoMiddleware,
    TokenUsageMiddleware,
)
from tools_v20 import TOOLS


def build_middlewares(data_dir: Path) -> list:
    """顺序就是语义：动一行的代价可能是另一环失去它需要的上下文。"""
    return [
        DynamicContextMiddleware(memory_text="- 用户：张三（负责寄递业务）"),
        SummarizationMiddleware(keep_recent=4),
        TodoMiddleware(),
        TokenUsageMiddleware(),
        TitleMiddleware(),
        MemoryMiddleware(store_path=data_dir / "memory.log"),
        SystemMessageCoalescingMiddleware(),
        LoopDetectionMiddleware(window=3),
        ClarificationMiddleware(),
    ]


def make_lead_agent(model, data_dir: Path):
    """迷你 make_lead_agent：模型 + 工具 + 顺序链 = 一张图。"""
    middlewares = build_middlewares(data_dir)
    agent = create_agent(
        model=model,
        tools=list(TOOLS),
        middleware=middlewares,
        system_prompt="你是邮政经营分析助手。复杂任务先写 todos；最终回答一句中文。",
    )
    names = [type(m).__name__.replace("Middleware", "") for m in middlewares]
    print("Create Agent(default) -> middleware chain: " + " -> ".join(names))
    return agent
