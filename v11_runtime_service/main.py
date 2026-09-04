"""v11 · 服务化 —— 脚本变服务：run 状态机 + StreamBridge + SSE 断线续传。

运行：
    conda run -n deerflow_lab python v11_runtime_service/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake（长驻服务，Ctrl+C 退出）

--fake 全自动演示（无需浏览器/第二终端）：
    起 uvicorn（线程）→ POST 起 run → 首连消费 SSE → 收 2 帧 values 后主动断线
    → 带 Last-Event-ID 重连续传 → 断言 run=success 且事件 id 连续 → 关服务退出。
"""

from __future__ import annotations

import argparse
import shutil
import socket
import threading
import time
from pathlib import Path

import httpx
from langchain.agents import create_agent

from gateway import create_app
from model_factory import fake_model, get_model, tool_call
from tools import TOOLS

DATA_DIR = Path(__file__).resolve().parent / "data"


def fake_script() -> list:
    """--fake 剧本：调一次计算器再交卷 —— 恰好产生 4 个 values 帧。"""
    return [
        tool_call("c1", "calculator", expression="(2+3)*7"),
        "报告：(2+3)*7 = 35，任务结束。",
    ]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(app) -> tuple[object, threading.Thread, int]:
    """在当前进程里用后台线程起 uvicorn，返回 (server, 线程, 端口)。"""
    import uvicorn

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return server, thread, port


def read_frames(stream, stop_after_values: int | None = None) -> list[dict]:
    """把 SSE 字节流解析成帧列表；stop_after_values 用于模拟"中途断线"。"""
    frames: list[dict] = []
    cur: dict[str, str] = {}
    values_seen = 0
    for line in stream:
        if not line:
            if cur:
                frames.append(cur)
                if cur.get("event") == "values":
                    values_seen += 1
                cur = {}
                if stop_after_values is not None and values_seen >= stop_after_values:
                    return frames
            continue
        field, _, value = line.partition(": ")
        if field in ("event", "data", "id"):
            cur[field] = value
    if cur:
        frames.append(cur)
    return frames


def show(frames: list[dict]) -> None:
    """人肉摘要 SSE 帧：values 帧只报消息数与末条消息，避免刷屏。"""
    import json

    for f in frames:
        data = f.get("data", "")
        if f.get("event") == "values":
            msgs = json.loads(data)["messages"]
            last = msgs[-1]
            data = f"msgs={len(msgs)} last={last['type']}/{','.join(last['tool_calls']) or '-'}"
        elif len(data) > 46:
            data = data[:46] + "…"
        id_part = f" id={f['id']}" if "id" in f else ""
        print(f"    event={f.get('event')}{id_part}  {data}")


def demo_fake(agent) -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)   # 启动时清空，保证可重复运行
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = create_app(agent, frame_interval=0.15, frame_log=DATA_DIR / "frames.log")
    server, thread, port = start_server(app)
    base = f"http://127.0.0.1:{port}"
    print(f"[server] uvicorn 已启动 {base}")
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(f"{base}/api/threads/t-demo/runs", params={"prompt": "先算 (2+3)*7 再交卷"})
            body = r.json()
            run_id = body["run_id"]
            print(f"[client] POST /api/threads/t-demo/runs -> run_id={run_id} status={body['status']}")

            with client.stream("GET", f"{base}/api/runs/{run_id}/stream") as resp:
                first = read_frames(resp.iter_lines(), stop_after_values=2)
            print("[client] 首连收到：")
            show(first)
            last_id = first[-1]["id"]
            print(f"[client] —— 模拟断线：收满 2 个 values 帧主动掐断，Last-Event-ID={last_id} ——")

            with client.stream("GET", f"{base}/api/runs/{run_id}/stream",
                               headers={"Last-Event-ID": last_id}) as resp:
                second = read_frames(resp.iter_lines())
            print("[client] 重连（带上 Last-Event-ID）收到：")
            show(second)

            status = client.get(f"{base}/api/runs/{run_id}").json()["status"]

        ids = [int(f["id"]) for f in first + second if "id" in f]
        assert status == "success", f"run 状态应为 success，实际 {status}"
        assert ids == list(range(len(ids))), f"事件 id 应连续无重复，实际 {ids}"
        assert second[-1]["event"] == "end", "重连流应以 end 帧收尾"
        print(f"[assert] run 最终状态 = {status}")
        print(f"[assert] 两次连接拼出连续事件流 id={ids}，断线处零丢失、零重复")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        print("[server] 已关闭，演示结束（退出码 0）")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线演示：不起浏览器、不联网")
    args = ap.parse_args()

    if args.fake:
        agent = create_agent(model=fake_model(fake_script()), tools=TOOLS)
        demo_fake(agent)
        return

    import uvicorn

    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    agent = create_agent(model=get_model(), tools=TOOLS)
    uvicorn.run(create_app(agent, frame_log=DATA_DIR / "frames.log"),
                host="127.0.0.1", port=8000)  # 在线长驻服务


if __name__ == "__main__":
    main()
