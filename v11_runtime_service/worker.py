"""run worker —— 后台跑 agent，把 astream 的每个切片 publish 进 StreamBridge。

参考本体：backend/packages/harness/deerflow/runtime/runs/worker.py
        （run_agent：astream -> 逐帧 publish -> 状态终结 -> publish_end）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from runs import RunManager, RunRecord, RunStatus
from stream_bridge import MemoryStreamBridge


def format_sse(event: str, data: Any, event_id: str | None = None) -> str:
    """SSE 帧：event:/data:/id: 各一行 + 空行收尾（本体 gateway 同款格式）。"""
    lines = [f"event: {event}", f"data: {json.dumps(data, ensure_ascii=False, default=str)}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    return "\n".join(lines) + "\n\n"


def _msg_brief(m) -> dict:
    """values 帧瘦身：只带类型、正文摘要、工具名，别把全量消息怼给前端。"""
    return {
        "type": m.type,
        "content": (m.content or "")[:120],
        "tool_calls": [c["name"] for c in (getattr(m, "tool_calls", None) or [])],
    }


async def run_agent(agent, manager: RunManager, bridge: MemoryStreamBridge,
                    run: RunRecord, user_text: str, *, frame_interval: float = 0.0) -> None:
    """worker 的骨架：queued -> running -> (success|error)，全程往 bridge 写帧。

    frame_interval 只为 --fake 演示拉长窗口（让客户端来得及"中途断线"），
    本体没有这个参数——生产环境帧与帧之间天然有 LLM 延迟。
    """
    manager.set_status(run.run_id, RunStatus.RUNNING)
    await bridge.publish(run.run_id, "metadata",
                         {"run_id": run.run_id, "thread_id": run.thread_id})
    try:
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": user_text}]},
            stream_mode="values",
        ):
            await bridge.publish(run.run_id, "values",
                                 {"messages": [_msg_brief(m) for m in chunk.get("messages", [])]})
            if frame_interval:
                await asyncio.sleep(frame_interval)
        manager.set_status(run.run_id, RunStatus.SUCCESS)
    except Exception as exc:  # noqa: BLE001 —— worker 绝不能把异常抛回事件循环
        manager.set_status(run.run_id, RunStatus.ERROR, error=str(exc))
        await bridge.publish(run.run_id, "error", {"message": str(exc)})
    finally:
        await bridge.publish_end(run.run_id)
