"""v01 · ReAct 循环 —— model ↔ tool 的乒乓，一切 agent 的地基。

运行：
    conda run -n deerflow_lab python v01_react_loop/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点：
1. 消息列表如何一步步变长：Human → AI(tool_calls) → Tool → AI(终稿)；
2. --fake 下第 2 条 AI 消息带 2 个 tool_calls，看框架如何并行执行并回填；
3. 循环何时停：模型不再发起 tool_call 的那一刻。
"""

from __future__ import annotations

import argparse

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from model_factory import fake_model, get_model, tool_call
from tools import TOOLS


def build_agent(model):
    """一行装配：模型 + 工具 = agent。本体 lead_agent 的骨架同样只有这一句。"""
    return create_agent(model=model, tools=TOOLS)


def fake_script() -> list:
    """--fake 剧本：先调计算器，再查时间，最后交卷。"""
    return [
        tool_call("c1", "calculator", expression="(2+3)*7"),
        tool_call("c2", "get_current_time", fmt="%Y年%m月%d日"),
        "报告：(2+3)*7 = 35。时间查询已完成，任务结束。",
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    model = fake_model(fake_script()) if args.fake else get_model()
    agent = build_agent(model)

    prompt = "先算 (2+3)*7，再告诉我现在几点了，最后汇总报告。"
    print(f"[用户] {prompt}\n")

    state = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    for msg in state["messages"]:
        cls = type(msg).__name__
        detail = msg.content or f"tool_calls={getattr(msg, 'tool_calls', [])}"
        print(f"[{cls}] {detail}")


if __name__ == "__main__":
    main()
