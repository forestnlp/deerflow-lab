"""v02 · 第一个中间件 —— 循环一行没改，行为插上了管。

运行：
    conda run -n deerflow_lab python v02_first_middleware/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点：
1. OnionLogger 在模型/工具调用的进与出各记一行——洋葱圈成形；
2. 一条 AI 消息带 2 个 tool_calls，同一轮里全部执行（工具调用合并）；
3. 便签中间件与检查中间件换一下列表顺序，检查器"看不看得到便签"就翻转——
   中间件顺序敏感，本体 build_middlewares 的 append 顺序因此是设计而非随意。
"""

from __future__ import annotations

import argparse

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

from middlewares import DATA_DIR, LOG_PATH, NotePeek, NoteSticker, OnionLogger, Trace
from model_factory import fake_model, get_model
from tools import TOOLS

PROMPT = "用 echo 工具分别回显 A 和 B，然后收尾。"


def reset_data_dir() -> None:
    """运行时产物只写本版 data/，启动时清空（保留 .gitkeep 占位）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for p in DATA_DIR.iterdir():
        if p.name != ".gitkeep":
            p.unlink()


def fake_script() -> list:
    """一条 AI 消息带 2 个 tool_calls（演示工具调用合并），然后交卷。"""
    parallel = AIMessage(
        content="",
        tool_calls=[
            {"name": "echo", "args": {"text": "A"}, "id": "c1"},
            {"name": "echo", "args": {"text": "B"}, "id": "c2"},
        ],
    )
    return [parallel, "两项回显完成，任务结束。"]


def run_round(make_model, make_middlewares, label: str) -> None:
    print(f"\n--- 轮次：{label} ---")
    # 每轮全新模型与全新中间件实例：fake 脚本按轮重发，流水账从零开始
    trace = Trace()
    agent = create_agent(model=make_model(), tools=TOOLS, middleware=make_middlewares(trace))
    state = agent.invoke({"messages": [HumanMessage(content=PROMPT)]})
    lines = trace.render()
    for i, line in enumerate(lines, 1):
        print(f"    [trace] {i:02d} {line}")
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.writelines(f"{i:02d} {line}\n" for i, line in enumerate(lines, 1))
    tools_out = [m.content for m in state["messages"] if m.type == "tool"]
    print(f"  工具结果: {tools_out}")
    print(f"  终稿: {state['messages'][-1].content}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    reset_data_dir()

    make_model = (lambda: fake_model(fake_script())) if args.fake else get_model

    print(f"[用户] {PROMPT}")
    # 轮次 1：便签贴在最外、检查器在最内 —— 内层看得到外层贴上的便签
    run_round(make_model,
              lambda t: [OnionLogger(t), NoteSticker(), NotePeek(t)],
              "中间件顺序 = [洋葱日志, 贴便签, 检查器]（便签在外层）")
    # 轮次 2：两者互换 —— 检查器先于便签看到请求，便签对它不可见
    run_round(make_model,
              lambda t: [OnionLogger(t), NotePeek(t), NoteSticker()],
              "中间件顺序 = [洋葱日志, 检查器, 贴便签]（便签在内层）")

    print("\n--- data/run.log（洋葱日志落盘，两轮连续追加） ---")
    print(LOG_PATH.read_text(encoding="utf-8"), end="")


if __name__ == "__main__":
    main()
