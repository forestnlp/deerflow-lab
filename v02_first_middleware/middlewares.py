"""v02 核心零件：手写的第一个 AgentMiddleware。

参考本体 packages/harness/deerflow/agents/middlewares/（总论）
      与 packages/harness/deerflow/agents/lead_agent/agent.py::build_middlewares
      （列表 append 顺序 = 洋葱圈从外到内的顺序）。

中间件系统的入门只用到两个 hook：
- wrap_model_call：每次模型调用的洋葱圈。进可改请求（request.override），
  出可观答复；不调 handler 还能直接短路。
- wrap_tool_call：每个工具调用的洋葱圈。可改写参数、可直接伪造 ToolMessage
  一票否决（v05 的护栏就用这一招）。
另有 before/after_agent、before/after_model 四个节点型钩子（改 state 用），
本版先不碰，v03/v05 各用一个。
"""

from __future__ import annotations

from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

DATA_DIR = Path(__file__).resolve().parent / "data"
LOG_PATH = DATA_DIR / "run.log"


class Trace:
    """一轮 run 的洋葱圈流水账。

    并行工具调用在多线程里落笔，先后取决于调度；渲染时把连续的 tool 行
    按调用 id 归一化排序（见 render），model 行保持原序。这样 --fake
    的打印逐字节确定，而"谁包着谁"的事实一个字节不改。
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, text: str) -> None:
        self.lines.append(text)

    def render(self) -> list[str]:
        out: list[str] = []
        tool_buf: list[str] = []

        def flush() -> None:
            # 行形如 "tool IN  echo#c1" / "tool OUT echo#c1"：先按调用 id、
            # 再按 IN/OUT 归一化，同批并行调用的行序与线程调度无关
            tool_buf.sort(key=lambda s: (s.rsplit("#", 1)[1], s.split()[1]))
            out.extend(tool_buf)
            tool_buf.clear()

        for line in self.lines:
            if line.startswith("tool "):
                tool_buf.append(line)
            else:
                flush()
                out.append(line)
        flush()
        return out


class OnionLogger(AgentMiddleware):
    """第一个中间件：给模型调用与工具调用记洋葱圈日志（stdout + data/run.log）。

    横切关注点（cross-cutting concern）的标准示范：agent 循环一行没改、
    工具函数一行没改，所有 LLM 请求和工具执行却都可观测、可落盘。
    本体 30 多个中间件（token 计费、错误兜底、审计……）全挂在这套钩子上。
    """

    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self._trace = trace

    def wrap_model_call(self, request, handler):
        self._trace.add(f"model IN  {len(request.messages)} 条消息")
        result = handler(request)
        self._trace.add("model OUT")
        return result

    def wrap_tool_call(self, request, handler):
        name, cid = request.tool_call["name"], request.tool_call["id"]
        self._trace.add(f"tool  IN  {name}#{cid}")
        result = handler(request)
        self._trace.add(f"tool  OUT {name}#{cid}")
        return result


class NoteSticker(AgentMiddleware):
    """模拟"提醒注入器"（本体 TodoMiddleware 的 completion reminder 同款手法）：

    在请求发给模型之前，往本次请求尾部追加一条隐藏便签。
    用 request.override(...) 只改**这次请求**，不落盘进会话历史。
    """

    def wrap_model_call(self, request, handler):
        note = HumanMessage(
            content="【便签】别忘了收尾。",
            name="note",
            additional_kwargs={"hide_from_ui": True},
        )
        return handler(request.override(messages=[*request.messages, note]))


class NotePeek(AgentMiddleware):
    """模拟"检查器"：汇报请求流经本层时能看到几条消息、便签是否可见。

    它看不看得到便签，完全取决于中间件列表里谁在外谁在内——
    这就是"中间件顺序敏感"的可观测证据。
    """

    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self._trace = trace

    def wrap_model_call(self, request, handler):
        seen = any(getattr(m, "name", None) == "note" for m in request.messages)
        self._trace.add(f"peek      {len(request.messages)} 条消息，便签可见: {seen}")
        return handler(request)
