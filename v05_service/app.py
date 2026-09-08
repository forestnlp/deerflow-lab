"""v05 核心零件：极简 runtime —— run 账本 + SSE 网关。

参考本体 packages/.../runtime/（RunManager/StreamBridge 语义）与 gateway/：
- 一次对话 = 一个 run，先落账再执行（status: queued -> success/error）；
- 事件流每帧带自增 id；客户端断线后带 Last-Event-ID 重连，零丢失零重复；
- agent 由外面装配好传进来，网关只管"跑与播"，不懂业务。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def create_app(agent, frame_interval: float = 0.15) -> FastAPI:
    app = FastAPI(title="deerflow-lab v05")
    runs: dict[str, dict] = {}                  # run_id -> {status, events:[...]}
    lock = threading.Lock()

    def worker(run_id: str, thread_id: str, prompt: str) -> None:
        try:
            for chunk in agent.stream(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"configurable": {"thread_id": thread_id}},
                    stream_mode="values"):
                last = chunk["messages"][-1]
                event = {"kind": type(last).__name__, "content": str(last.content)[:100]}
                with lock:
                    runs[run_id]["events"].append({"event": "values", "data": event})
                time.sleep(frame_interval)      # 模拟逐 token 的节奏，让断线可演示
            with lock:
                runs[run_id]["status"] = "success"
                runs[run_id]["events"].append({"event": "end", "data": {}})
        except Exception as exc:                # 兜底所有异常：run 的终态只有 success/error
            with lock:
                runs[run_id]["status"] = "error"
                runs[run_id]["events"].append({"event": "error", "data": {"why": str(exc)}})

    @app.post("/api/threads/{thread_id}/runs")
    async def start_run(thread_id: str, body: dict):
        run_id = uuid.uuid4().hex[:8]
        with lock:
            runs[run_id] = {"status": "queued", "events": []}
        threading.Thread(target=worker, args=(run_id, thread_id, body["prompt"]),
                         daemon=True).start()
        return {"run_id": run_id, "thread_id": thread_id, "status": "queued"}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        with lock:
            return {"run_id": run_id, "status": runs[run_id]["status"]}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request):
        start = int(request.headers.get("last-event-id", "-1")) + 1

        async def gen():
            i = start
            while True:
                with lock:
                    evs = runs[run_id]["events"]
                    done = runs[run_id]["status"] in ("success", "error")
                if i < len(evs):
                    ev = evs[i]
                    yield f"id: {i}\nevent: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
                    if ev["event"] in ("end", "error"):
                        return
                    i += 1
                elif done:
                    return
                else:
                    await asyncio.sleep(0.05)

        return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

    return app
