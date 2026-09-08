"""v02 · 中间件与规划 —— 循环一行没改，管人的制度插上了。

运行（仓库根目录）：python -m v02_middleware_plan.main

观察重点：
1. write_todos 工具是中间件注入的（本文件的 TOOLS 里根本没有它）；
2. OnionLogger 在每次模型/工具调用进出各记一行——洋葱圈成形；
3. 模型若列了计划却没销账就想交卷，after_model 会 jump_to=model 拦回去
   （模型乖乖干活时不触发，信号看前两条 + 终稿前的全绿清单）。
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage

from shared.model_factory import get_model
from v02_middleware_plan.middlewares import OnionLogger, PlanState, TodoMiddleware, Trace


@tool
def search_notes(keyword: str) -> str:
    """检索邮政经营知识库，返回关键词命中的摘要。"""
    return f"'{keyword}': 3 条笔记 —— 寄递业务量上半年同比增长 12.3%。"


@tool
def write_report(topic: str) -> str:
    """撰写主题分析报告，返回报告路径。"""
    return f"/mnt/user-data/outputs/{topic}-report.md"


TOOLS = [search_notes, write_report]


def main() -> None:
    trace = Trace()
    agent = create_agent(
        model=get_model(),
        tools=TOOLS,
        middleware=[OnionLogger(trace), TodoMiddleware(trace)],   # 顺序=洋葱从外到内
        state_schema=PlanState,
    )
    ask = "分析上半年寄递业务量：先列两步计划（查数据、写报告），逐步执行并随时销账，最后给我一句话结论。"
    print(f"[用户] {ask}\n")
    state = agent.invoke({"messages": [HumanMessage(ask)]})

    for i, line in enumerate(trace.lines, 1):
        print(f"  [trace] {i:02d} {line}")

    print(f"\n[todos] {state.get('todos')}")
    print(f"[终稿] {state['messages'][-1].content}")


if __name__ == "__main__":
    main()
