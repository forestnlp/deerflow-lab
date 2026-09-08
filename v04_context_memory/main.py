"""v04 · 上下文与记忆 —— 窗口会撑爆，账单会起飞，人会换会话。

运行（仓库根目录）：python -m v04_context_memory.main

三幕：
① 摘要折叠：长历史超阈值 -> 旧消息换一条摘要（信号：[摘要] 行 + 折叠后窗口变小）；
② 预算硬停：字符预算过半预警、超支 jump_to=end（信号确定，不经模型意志）；
③ 长期记忆：run 结束抽取事实落盘，重启新会话开局注入（跨进程信号确定）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from shared.model_factory import get_model
from v04_context_memory.context_tools import (MEMORY_NAME, SUMMARY_NAME, BudgetMiddleware,
                                              MemoryMiddleware, SummarizeMiddleware, estimate_chars)

DATA = Path(__file__).resolve().parent / "data"
MEMORY_PATH = DATA / "memory.json"
CHAPTER = ("库存周转是躺在仓库里的现金；网络密度决定单件成本；时效是生命线；"
           "农村投递补贴核算复杂；国际件毛利波动大。") * 4          # 每章约 260 字


@tool
def read_manual(chapter: int) -> str:
    """读经营手册第 chapter 章（1-3），每章约 260 字。"""
    return f"第{chapter}章 " + CHAPTER


def window(messages) -> str:
    return f"{len(messages)} 条 ≈{estimate_chars(messages)} 字符: " + \
        str(["摘要" if getattr(m, "name", None) == SUMMARY_NAME else type(m).__name__
             for m in messages])


def main() -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)

    # —— 幕①②：同一 checkpointer，历史随 thread 滚雪球 ——
    summarizer = SummarizeMiddleware(trigger_chars=600, keep_recent=2)
    budget = BudgetMiddleware(budget_chars=100_000)          # 幕①预算形同关闭
    agent = create_agent(model=get_model(), tools=[read_manual],
                         middleware=[summarizer, budget],
                         checkpointer=MemorySaver())

    print("=== 幕① 摘要折叠：连读三章，窗口超 600 字符就折叠 ===")
    for i, ask in enumerate(["读手册第1章，说一句要点。", "再读第2章，同样一句。",
                             "再读第3章，然后把三章各浓缩成5个字。"], 1):
        print(f"\n[A{i}] 用户: {ask}")
        r = agent.invoke({"messages": [HumanMessage(ask)]},
                         config={"configurable": {"thread_id": "t-a"}})
        print(f"  [窗口] {window(r['messages'])}")

    print("\n=== 幕② 预算硬停 ===")
    print("--- 自检（不经模型，直接敲 before_model，信号 100% 确定） ---")
    from langchain_core.messages import AIMessage
    fat = [HumanMessage("长任务")] + [AIMessage("x" * 200) for _ in range(8)]   # ≈1600 字符
    budget.configure(300)
    patch = budget.before_model({"messages": fat}, None)
    print(f"  >> 自检：patch={patch} stop_reason={budget.stop_reason}")
    budget.configure(2400)
    budget.before_model({"messages": fat}, None)          # 触发预警路径（打印预警行）

    print("--- 真模型演示：单独 agent 只挂预算（防摘要干扰），预算 600 必然超支 ---")
    budget.configure(600)
    agent_b = create_agent(model=get_model(), tools=[read_manual], middleware=[budget])
    r = agent_b.invoke({"messages": [HumanMessage("务必用 read_manual 把第1、2、3章全部读一遍，逐章给要点。")]})
    print(f"  [stop_reason] {budget.stop_reason}")
    print(f"  [窗口] {window(r['messages'])}")

    # —— 幕③：记忆中间件单独上岗（它每次 run 收尾都花一次旁路模型调用）——
    print("\n=== 幕③ 长期记忆：第一世说偏好 -> 结算落盘 ===")
    agent2 = create_agent(model=get_model(), tools=[read_manual],
                          middleware=[MemoryMiddleware(MEMORY_PATH)],
                          checkpointer=MemorySaver())
    agent2.invoke({"messages": [HumanMessage("记一下：我负责寄递业务，看报告只看收入。")]},
                  config={"configurable": {"thread_id": "t-c"}})

    print("\n=== 幕③ 第二世：全新 thread（模拟重启），看记忆是否在场 ===")
    r = agent2.invoke({"messages": [HumanMessage("根据我的分工，推荐一个我每周该盯的指标。")]},
                      config={"configurable": {"thread_id": "t-d"}})
    mem_in = [m for m in r["messages"] if getattr(m, "name", None) == MEMORY_NAME]
    print(f"  [记忆注入] {'在场' if mem_in else '缺席'}: "
          f"{mem_in[0].content if mem_in else ''}")
    print(f"  [回答] {r['messages'][-1].content}")
    disk = MEMORY_PATH.read_text(encoding="utf-8") if MEMORY_PATH.exists() else "无"
    print(f"\n[磁盘] {MEMORY_PATH.name}: {disk}")


if __name__ == "__main__":
    main()
