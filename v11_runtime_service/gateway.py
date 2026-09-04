"""gateway —— FastAPI 服务层：起 run 的 POST 路由 + 消费 bridge 的 SSE 路由。

参考本体：backend/app/gateway/routers/runs.py（POST /runs/stream，
        media_type="text/event-stream"）
        backend/app/gateway/services.py（约 L1234：读 Last-Event-ID 请求头
        -> bridge.subscribe(last_event_id=...)）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from runs import ConflictError, RunManager
from stream_bridge import END_SENTINEL, HEARTBEAT_SENTINEL, MemoryStreamBridge
from worker import format_sse, run_agent


def create_app(agent, *, frame_interval: float = 0.0,
               frame_log: Path | None = None) -> FastAPI:
    """装配网关。agent 在 --fake 与在线模式下是同一个对象，服务层不感知差别。

    frame_log：给定路径时把每个 SSE 帧追加落盘（本版运行时产物，只写 data/）。
    """
    app = FastAPI(title="v11 mini gateway")
    manager = RunManager()          # 服务只有一个事件循环，asyncio 原语安全
    bridge = MemoryStreamBridge()

    @app.post("/api/threads/{thread_id}/runs")
    async def create_run(thread_id: str, prompt: str = "你好") -> dict:
        try:
            run = manager.create_or_reject(thread_id)
        except ConflictError as exc:
            return {"error": str(exc)}
        asyncio.create_task(
            run_agent(agent, manager, bridge, run, prompt, frame_interval=frame_interval)
        )
        return {"run_id": run.run_id, "status": run.status.value}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        record = manager.get(run_id)
        if record is None:
            return {"error": "not found"}
        return {"run_id": record.run_id, "status": record.status.value,
                "error": record.error}

    @app.get("/api/runs/{run_id}/stream")
    async def stream_run(run_id: str, request: Request):
        """SSE 端点：bridge 的事件流 -> SSE 帧流。断线重连带 Last-Event-ID 即可续传。"""
        last_event_id = request.headers.get("Last-Event-ID")

        async def gen():
            async for entry in bridge.subscribe(run_id, last_event_id=last_event_id,
                                                heartbeat_interval=2.0):
                if entry is END_SENTINEL:
                    frame = format_sse("end", {"done": True}, None)
                elif entry is HEARTBEAT_SENTINEL:
                    frame = ": heartbeat\n\n"  # SSE 注释帧，仅保活
                else:
                    frame = format_sse(entry.event, entry.data, entry.id)
                if frame_log is not None:
                    with frame_log.open("a", encoding="utf-8") as fh:
                        fh.write(frame)
                yield frame
                if entry is END_SENTINEL:
                    return

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
