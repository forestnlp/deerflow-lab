"""v08 · 上下文控制 —— 摘要管"装得下"，预算管"花得起"。

运行：
    conda run -n deerflow_lab python v08_context_control/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点：
1. 场景一的第三轮：before_model 估算超阈值，7 条旧消息折叠成 1 条摘要，
   消息通道整体替换（RemoveMessage + 摘要 + 保留段）；
2. 场景二单跑到底：先触发预算预警（提醒随下次请求投递），中途摘要又
   压了一次窗口——但账单不会因窗口变小而回滚，最终硬停剥 tool_calls；
3. fake 下 1 字符≈1 token 的近似口径；在线路径换 usage_metadata，机制不变。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from model_factory import fake_model, get_model, tool_call
from summarization_middleware import SUMMARY_NAME, ContextState, LabSummarizationMiddleware, estimate_chars
from token_budget_middleware import LabTokenBudgetMiddleware
from tools import TOOLS

DATA = Path(__file__).resolve().parent / "data"
TRIGGER_CHARS = 420      # 摘要触发阈值（学习版调小让机关十步内现形）
KEEP_MESSAGES = 3


def summary_model(fake: bool):
    if fake:
        return fake_model(["第1章：库存周转是躺在仓里的钱；第2章：网络密度定成本，切莫空驶。"])
    return get_model("summary")


def make_agent(saver, summarizer, budget, script, fake: bool):
    model = fake_model(script) if fake else get_model()
    return create_agent(model=model, tools=TOOLS,
                        middleware=[summarizer, budget],
                        state_schema=ContextState, checkpointer=saver)


def run_turn(label, agent, thread, ask):
    print(f"\n[{label}]")
    print(f"  [用户] {ask}")
    state = agent.invoke({"messages": [HumanMessage(ask)]},
                         config={"configurable": {"thread_id": thread}})
    last = state["messages"][-1]
    print(f"  [终稿] {str(last.content)[:88]}")
    names = ["摘要" if getattr(m, "name", None) == SUMMARY_NAME else type(m).__name__
             for m in state["messages"]]
    print(f"  [窗口] {len(state['messages'])} 条 ≈{estimate_chars(state['messages'])} 字符: {names}")
    return state


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "context_log.txt").unlink(missing_ok=True)

    summarizer = LabSummarizationMiddleware(
        lambda p: summary_model(args.fake), trigger_chars=TRIGGER_CHARS,
        keep_messages=KEEP_MESSAGES)
    budget = LabTokenBudgetMiddleware()          # 场景一档：天文预算，等于关闭
    saver = MemorySaver()

    print("=" * 46)
    print("场景一 · 摘要折叠：before_model 触发，旧消息换摘要")
    print("=" * 46)
    run_turn("A1 · 读第1章", make_agent(saver, summarizer, budget,
             [tool_call("f1", "fetch_chapter", no="1"),
              "第1章要点：库存周转是躺在仓里的钱，转起来才算生意。"], args.fake),
             "t1", "读手册第1章，给我一句要点。")
    run_turn("A2 · 读第2章（窗口超阈值 -> 开工前折叠）", make_agent(saver, summarizer, budget,
             [tool_call("f2", "fetch_chapter", no="2"),
              "第2章要点：网络密度决定单件成本，前提是别空驶。"], args.fake),
             "t1", "再读第2章，同样给一句。")
    run_turn("A3 · 折叠后的窗口上合并结论", make_agent(saver, summarizer, budget,
             ["两章合并：先把库存转起来，再把网络织密且不空驶，利润自己会来。"], args.fake),
             "t1", "把两章合并成两句结论。")
    run_turn("A4 · 折叠后再追问", make_agent(saver, summarizer, budget,
             ["第1章：库存周转。第2章：网络密度。摘要里都在。"], args.fake),
             "t1", "两章各自的主题词是什么？")

    print()
    print("=" * 46)
    print("场景二 · token 预算：预警 → （窗口再压缩也）→ 硬停")
    print("=" * 46)
    budget.configure(budget=1000, warn_ratio=0.5, hard_ratio=0.8)
    budget.reset()
    s2 = run_turn("B1 · 连读带比对（预警→再压缩→硬停）", make_agent(saver, summarizer, budget,
             [tool_call("f3", "fetch_chapter", no="1"),
              tool_call("f4", "fetch_chapter", no="2"),
              tool_call("f5", "fetch_chapter", no="1"),   # 明知故犯：第三次读第1章
              tool_call("f6", "fetch_chapter", no="3"),
              "这本手册值得一页纸的结论。"], args.fake),
             "t2", "依次读第1、2章，再比对一次第1章，然后给我结论。")
    print(f"  [stop_reason] {budget.stop_reason}")

    with open(DATA / "context_log.txt", "w", encoding="utf-8") as f:
        for m in s2["messages"]:
            tag = "SUMMARY" if getattr(m, "name", None) == SUMMARY_NAME else type(m).__name__
            f.write(f"{tag}: {str(m.content)[:40]}\n")
    print(f"\n最终窗口落盘 data/context_log.txt：")
    print(f"  " + " | ".join("摘要" if getattr(m, "name", None) == SUMMARY_NAME
                             else type(m).__name__ for m in s2["messages"]))


if __name__ == "__main__":
    main()
