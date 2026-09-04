"""观测中间件 —— 把模型调用与工具调用录进事件表，全程带 trace_id。
参考本体 packages/harness/deerflow/tracing/（本体把 span 发给 Langfuse/LangSmith
等外部观测平台；学习版落在本地 SQLite，查询自由且零依赖）。

为什么用中间件而不是在业务代码里打点？横切关注点的铁律（v02 就讲过）：
观测逻辑不许渗进 agent 的业务代码。一个中间件挂上去，链路上每一次
model/tool 调用自动有记录；摘掉它，业务一行不改。

token 从哪来？优先读 AIMessage.usage_metadata（真实模型会给）；
fake 模型不给，就用 cost.estimate_tokens 估算——两条路径同一个字段落库。
"""

from __future__ import annotations

import json
import time

from langchain.agents.middleware import AgentMiddleware

from cost import estimate_tokens
from trace_context import current


class ObservabilityMiddleware(AgentMiddleware):
    """记录 model_call / tool_call 两类事件：耗时 + token 估算。"""

    def __init__(self, store, **kw):
        super().__init__(**kw)
        self.store = store
        # 本 run 累计 token（after_agent 时写回 runs 表）
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def _rid(self) -> tuple[str, str]:
        trace_id, run_id, _thread = current()
        return trace_id, run_id

    def wrap_model_call(self, request, handler):
        trace_id, run_id = self._rid()
        t0 = time.perf_counter()
        # 请求侧 token：把本轮发给模型的全部消息拼起来估（真实值应读 usage，见 README）
        sent = "\n".join(str(m.content) for m in request.messages)
        result = handler(request)
        dur_ms = round((time.perf_counter() - t0) * 1000, 1)
        # handler 返回 ModelResponse：生成的消息在 .result（langchain 1.2 的形状）
        last = result.result[-1] if getattr(result, "result", None) else None
        usage = (getattr(last, "usage_metadata", None) or {})
        pt = usage.get("input_tokens") or estimate_tokens(sent)
        ct = usage.get("output_tokens") or estimate_tokens(str(last.content) if last else "")
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.store.record(run_id, trace_id, "model_call", "llm",
                          content=f"~{pt}+~{ct} tokens",
                          meta={"duration_ms": dur_ms, "prompt_tokens": pt,
                                "completion_tokens": ct})
        return result

    def wrap_tool_call(self, request, handler):
        trace_id, run_id = self._rid()
        t0 = time.perf_counter()
        tc = request.tool_call
        result = handler(request)
        dur_ms = round((time.perf_counter() - t0) * 1000, 1)
        self.store.record(run_id, trace_id, "tool_call", "tool",
                          content=tc["name"],
                          meta={"duration_ms": dur_ms,
                                "args": _clip(json.dumps(tc["args"], ensure_ascii=False)),
                                "result": _clip(str(getattr(result, "content", "")))})
        return result


def _clip(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n] + "…"
