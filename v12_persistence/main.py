"""v12 · 存盘 —— checkpoint 断点 + SQLite 账本：杀进程重启，同 thread_id 续聊。

运行：
    conda run -n deerflow_lab python v12_persistence/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake（交互式续聊）

--fake 全自动演示（模拟"进程被杀"）：
    清空 data/ → 子进程跑第 1 轮（run 落账 running，回答完直接 os._exit 猝死）
    → 父进程确认账本里留下 running 孤儿 → 以"新进程"身份重启：
    孤儿恢复为 error → SqliteSaver 重开同一 thread_id，断言历史从盘上回来了
    → 续聊第 2 轮（消息数 2→4）→ 全部断言通过，退出码 0。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver

from ledger import Ledger
from model_factory import fake_model, get_model

DATA_DIR = Path(__file__).resolve().parent / "data"
CKPT_DB = DATA_DIR / "checkpoints.db"
LEDGER_DB = DATA_DIR / "ledger.db"
RUN_FILE = DATA_DIR / "crashed_run.json"
THREAD_ID = "t-crash-demo"


def fake_script_round1() -> list:
    return ["收到：你说了你喜欢蓝色，我记下了。"]


def fake_script_round2() -> list:
    return ["翻了下历史：你之前说过你喜欢蓝色。"]


def phase1_child() -> None:
    """第一世：正常起 run 答完第 1 轮，然后"猝死"——run 来不及收尾。"""
    ledger = Ledger(LEDGER_DB)
    run_id = uuid.uuid4().hex[:8]
    ledger.start_run(run_id, THREAD_ID)          # 先落账再执行（本体的可见性边界）
    RUN_FILE.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")

    with SqliteSaver.from_conn_string(str(CKPT_DB)) as saver:
        agent = create_agent(model=fake_model(fake_script_round1()), tools=[], checkpointer=saver)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": "我喜欢蓝色"}]},
            config={"configurable": {"thread_id": THREAD_ID}},
        )
    print(f"[phase1] 第 1 轮答完：{result['messages'][-1].content!r}")
    print(f"[phase1] run={run_id} 账本状态=running —— 现在进程猝死（os._exit），不收尾")
    sys.stdout.flush()
    import os
    os._exit(9)                                   # 模拟 kill -9：ledger 里留下 running 孤儿


def restart_demo() -> None:
    """第二世（新进程）：孤儿恢复 + 同 thread_id 续聊 + 断言。"""
    run_id = json.loads(RUN_FILE.read_text(encoding="utf-8"))["run_id"]

    with Ledger(LEDGER_DB) as ledger:
        before = ledger.get_run(run_id)
        print(f"[重启] 账本遗产：run={run_id} status={before['status']}（上一世没来得及收尾）")
        assert before["status"] == "running"

        recovered = ledger.recover_orphans()
        after = ledger.get_run(run_id)
        print(f"[重启] 孤儿恢复：{recovered} 个 run 收尾为 {after['status']}（{after['error']}）")
        assert recovered == 1 and after["status"] == "error"

        with SqliteSaver.from_conn_string(str(CKPT_DB)) as saver:
            tup = saver.get_tuple({"configurable": {"thread_id": THREAD_ID}})
            hist = tup.checkpoint["channel_values"]["messages"]
            print(f"[重启] checkpoint 从盘上捞回历史：{[type(m).__name__ for m in hist]}")
            assert len(hist) == 2, "上一轮的 2 条消息应当都在"

            new_run = uuid.uuid4().hex[:8]
            ledger.start_run(new_run, THREAD_ID)
            agent = create_agent(model=fake_model(fake_script_round2()), tools=[], checkpointer=saver)
            result = agent.invoke(
                {"messages": [{"role": "user", "content": "我喜欢什么颜色？"}]},
                config={"configurable": {"thread_id": THREAD_ID}},
            )
            ledger.finish_run(new_run, "success")

        msgs = result["messages"]
        print(f"[续聊] 同 thread_id 第 2 轮：{msgs[-1].content!r}")
        print(f"[续聊] 消息列表 {[type(m).__name__ for m in msgs]}（2 条遗产 + 2 条新增）")
        assert len(msgs) == 4

        runs = ledger.list_runs(THREAD_ID)
        for r in runs:
            print(f"[账本] run={r['run_id']} status={r['status']}")
        assert [r["status"] for r in runs] == ["error", "success"]
        print("[断言] 孤儿已收尾、历史已复活、账本两行前后相接 —— 断电重启，现场还在")


def online_repl() -> None:
    """在线模式：一条 thread 反复聊，Ctrl+C 后再启动接着同一条聊。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(CKPT_DB)) as saver:
        agent = create_agent(model=get_model(), tools=[], checkpointer=saver)
        config = {"configurable": {"thread_id": THREAD_ID}}
        print(f"在线续聊模式，thread_id={THREAD_ID}（Ctrl+C 退出，重启后记忆仍在）")
        while True:
            user = input("\n你> ").strip()
            if not user:
                break
            result = agent.invoke({"messages": [{"role": "user", "content": user}]}, config=config)
            print(f"AI> {result['messages'][-1].content}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线演示杀进程重启续聊，零 API key")
    ap.add_argument("--phase", choices=["1"], help=argparse.SUPPRESS)  # 内部：子进程第一世
    args = ap.parse_args()

    if args.phase == "1":
        phase1_child()
        return

    if args.fake:
        import shutil
        shutil.rmtree(DATA_DIR, ignore_errors=True)   # 启动时清空，保证可重复运行
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        print("[清空] data/ 已重置，开始演示")
        child = subprocess.run([sys.executable, str(Path(__file__)), "--phase", "1"],
                               capture_output=True, text=True, timeout=60)
        for line in child.stdout.strip().splitlines():
            print(line)
        print(f"[杀死] 子进程退出码 = {child.returncode}（预期 9 = 猝死）")
        assert child.returncode == 9
        restart_demo()
        print("[完成] 演示结束（退出码 0）")
        return

    online_repl()


if __name__ == "__main__":
    main()
