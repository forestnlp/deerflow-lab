"""v05 · 服务化 —— 进程会死，服务要活。

运行（仓库根目录）：
    python -m v05_service.main            # 自包含演示（幕①断电续聊 + 幕②SSE断线续传）
    python -m v05_service.main --serve    # 常驻 127.0.0.1:8000，供 curl / 浏览器体验

观察重点（信号确定，不赌模型措辞）：
幕① SqliteSaver：子进程答完一轮后 os._exit(9) 模拟断电；重启进程从盘上捞回历史、
     同 thread_id 续聊 —— assert 历史 2 条遗产、续聊后 4 条。
幕② SSE：进程内起 uvicorn，首连收满 2 帧主动断线，带 Last-Event-ID 重连 ——
     assert 两次连接拼出的帧 id 连续无重复、以 end 收尾。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from shared.model_factory import get_model
from v05_service.app import create_app

DATA = Path(__file__).resolve().parent / "data"
CKPT = DATA / "checkpoints.db"
ROOT = Path(__file__).resolve().parent.parent      # 仓库根（import shared 需要）
THREAD = "t-demo"


@tool
def calculator(expression: str) -> str:
    """计算算术表达式，例如 (2+3)*7。"""
    return str(eval(expression, {"__builtins__": {}}, {}))


TOOLS = [calculator]


# ---------- 幕① SqliteSaver：断电重启，现场还在 ----------
def phase1_child() -> None:
    """第一世（子进程）：真模型答完一轮，然后 os._exit(9) 模拟断电。"""
    with SqliteSaver.from_conn_string(str(CKPT)) as saver:
        agent = create_agent(model=get_model(), tools=TOOLS, checkpointer=saver)
        r = agent.invoke(
            {"messages": [HumanMessage("请记住：我负责的条线是寄递业务量。一句话确认。")]},
            config={"configurable": {"thread_id": THREAD}})
    print(f"[phase1] 第一世答完：{r['messages'][-1].content!r}")
    print("[phase1] 现在断电（os._exit(9)）——不给它优雅收尾的机会")
    sys.stdout.flush()
    os._exit(9)


def restart_demo() -> None:
    """第二世（当前进程）：从盘上捞历史 + 同 thread 续聊。"""
    with SqliteSaver.from_conn_string(str(CKPT)) as saver:
        tup = saver.get_tuple({"configurable": {"thread_id": THREAD}})
        hist = tup.checkpoint["channel_values"]["messages"]
        print(f"[重启] 从盘上捞回历史：{[type(m).__name__ for m in hist]}")
        assert len(hist) == 2, "第一世的 2 条消息应当都在盘上"

        agent = create_agent(model=get_model(), tools=TOOLS, checkpointer=saver)
        r = agent.invoke({"messages": [HumanMessage("我负责的条线是什么？")]},
                         config={"configurable": {"thread_id": THREAD}})
    msgs = r["messages"]
    print(f"[续聊] 第二世同 thread 回答：{msgs[-1].content!r}")
    print(f"[续聊] 消息 {len(msgs)} 条（2 遗产 + 2 新增）——断电重启，记忆无缝")
    assert len(msgs) == 4


# ---------- 幕② SSE：断线重连，零丢失零重复 ----------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_frames(lines, stop_after_values: int | None = None) -> list[dict]:
    frames, cur = [], {}
    for line in lines:
        if line.startswith("id: "):
            cur["id"] = line[4:]
        elif line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = line[6:]
        elif line == "" and cur:
            frames.append(cur)
            cur = {}
            if stop_after_values and sum(f["event"] == "values" for f in frames) >= stop_after_values:
                return frames
    return frames


def sse_demo() -> None:
    import uvicorn

    agent = create_agent(model=get_model(), tools=TOOLS)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(agent), host="127.0.0.1",
                                           port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    print(f"\n[server] uvicorn 已启动 {base}")
    try:
        with httpx.Client(timeout=60) as c:
            body = c.post(f"{base}/api/threads/t-sse/runs",
                          json={"prompt": "先用 calculator 算 (2+3)*7，再告诉我结果。"}).json()
            run_id = body["run_id"]
            print(f"[client] POST 起 run -> run_id={run_id} status={body['status']}")

            with c.stream("GET", f"{base}/api/runs/{run_id}/stream") as resp:
                first = read_frames(resp.iter_lines(), stop_after_values=2)   # 收 2 帧就走 = 断线
            print(f"[client] 首连收到 id={[f['id'] for f in first]} -> 主动掐断")
            last_id = first[-1]["id"]

            with c.stream("GET", f"{base}/api/runs/{run_id}/stream",
                          headers={"Last-Event-ID": last_id}) as resp:
                second = read_frames(resp.iter_lines())
            print(f"[client] 重连带 Last-Event-ID={last_id}，收到 id={[f['id'] for f in second]}")

            status = c.get(f"{base}/api/runs/{run_id}").json()["status"]

        ids = [int(f["id"]) for f in first + second]
        assert status == "success", f"run 终态应 success，实际 {status}"
        assert ids == list(range(len(ids))), f"帧 id 应连续无重复，实际 {ids}"
        assert second[-1]["event"] == "end", "重连流应以 end 收尾"
        print(f"[assert] run 终态={status}；两次连接拼出连续帧 id={ids}，断线处零丢失零重复")
    finally:
        server.should_exit = True


def serve() -> None:
    import uvicorn

    with SqliteSaver.from_conn_string(str(CKPT)) as saver:
        agent = create_agent(model=get_model(), tools=TOOLS, checkpointer=saver)
        print("常驻服务：POST /api/threads/<id>/runs  body={\"prompt\":...}；"
              "GET /api/runs/<run_id>/stream 看 SSE（Ctrl-C 退出）")
        uvicorn.run(create_app(agent), host="127.0.0.1", port=8000)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--serve", action="store_true", help="常驻在线服务，供 curl/浏览器")
    ap.add_argument("--phase1", action="store_true", help=argparse.SUPPRESS)   # 子进程内部用
    args = ap.parse_args()

    if args.phase1:
        phase1_child()
        return
    if args.serve:
        DATA.mkdir(parents=True, exist_ok=True)
        serve()
        return

    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)

    print("=== 幕① SqliteSaver 断电续聊 ===")
    child = subprocess.run([sys.executable, "-m", "v05_service.main", "--phase1"],
                           capture_output=True, text=True, timeout=180, cwd=ROOT)
    for ln in child.stdout.strip().splitlines():
        print(ln)
    print(f"[杀死] 子进程退出码 = {child.returncode}（预期 9 = 断电）")
    assert child.returncode == 9
    restart_demo()

    print("\n=== 幕② SSE 断线续传 ===")
    sse_demo()
    print("\n[完成] 演示结束（退出码 0）")


if __name__ == "__main__":
    main()
