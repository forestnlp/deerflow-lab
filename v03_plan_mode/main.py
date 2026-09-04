"""v03 · 规划模式 —— write_todos + 防提前交卷 + 断点续做。

运行：
    conda run -n deerflow_lab python v03_plan_mode/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点：
1. write_todos 工具由中间件注入（TOOLS 里根本没有它）；
2. 剧本第 3 条消息故意提前交卷 -> after_model 拦截 jump_to=model，
   催办只进本次请求、会话历史查无此条；
3. 第二局模拟"摘要压缩后重开"：历史里没有 write_todos 痕迹但 state 里有
   todos -> before_model 注入 todo_reminder，模型靠 state 恢复进度。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage

from model_factory import fake_model, get_model, tool_call
from todo_middleware import MAX_COMPLETION_REMINDERS, PlanState, TodoMiddleware

DATA_DIR = Path(__file__).resolve().parent / "data"


def reset_data_dir() -> None:
    """运行时产物只写本版 data/，启动时清空（保留 .gitkeep 占位）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for p in DATA_DIR.iterdir():
        if p.name != ".gitkeep":
            p.unlink()


@tool
def search_notes(keyword: str) -> str:
    """检索邮政经营知识库，返回关键词命中的摘要。"""
    return f"'{keyword}': 3 条笔记 —— 寄递业务量上半年同比增长 12%。"


@tool
def write_report(topic: str) -> str:
    """撰写主题分析报告，返回报告路径。"""
    return f"/mnt/user-data/outputs/{topic}-report.md"


TOOLS = [search_notes, write_report]

PLAN = [{"content": "检索业务量数据", "status": "in_progress"},
        {"content": "撰写分析报告", "status": "pending"}]
DONE = [{"content": "检索业务量数据", "status": "completed"},
        {"content": "撰写分析报告", "status": "completed"}]


def fake_script() -> list:
    """列计划 -> 查资料 -> 【提前交卷被拦】-> 补做 -> 销账 -> 收尾。"""
    return [
        tool_call("t1", "write_todos", todos=PLAN),
        tool_call("t2", "search_notes", keyword="寄递业务量"),
        AIMessage("分析完成，报告已就绪。"),          # 故意偷懒：todo 2 没干
        tool_call("t3", "write_report", topic="寄递业务量"),
        tool_call("t4", "write_todos", todos=DONE),
        "已完成：检索数据并输出报告 /mnt/user-data/outputs/寄递业务量-report.md",
    ]


def resume_script() -> list:
    """第二局：todos 由 state 带入，剧本按提醒续做并销账。"""
    return [
        tool_call("t5", "write_report", topic="寄递业务量"),
        tool_call("t6", "write_todos", todos=DONE),
        "断点续做完成：报告已产出。",
    ]


def dump(result: dict, tag: str) -> None:
    out = DATA_DIR / f"{tag}.json"
    out.write_text(json.dumps(result.get("todos"), ensure_ascii=False, indent=2), encoding="utf-8")


def show(result: dict) -> None:
    for m in result["messages"]:
        calls = getattr(m, "tool_calls", None)
        if calls:
            print(f"  [{type(m).__name__}] -> {[c['name'] for c in calls]}")
        elif getattr(m, "name", None):
            print(f"  [{type(m).__name__} name={m.name} hidden={m.additional_kwargs.get('hide_from_ui')}]")
        else:
            text = m.content if isinstance(m.content, str) else str(m.content)
            if m.type in ("human", "tool") and len(text) > 60:
                text = text[:60] + "…"
            print(f"  [{type(m).__name__}] {text}")
    print(f"  state.todos: {result.get('todos')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    reset_data_dir()
    make_model = (lambda: fake_model(fake_script())) if args.fake else get_model
    make_model2 = (lambda: fake_model(resume_script())) if args.fake else get_model

    print("=" * 72)
    print(f"局 1 · 防提前交卷（after_model + jump_to=model，上限 {MAX_COMPLETION_REMINDERS} 次）")
    print("=" * 72)
    print("[用户] 请分析上半年寄递业务量并写报告。先列计划再执行。")
    agent = create_agent(model=make_model(), tools=TOOLS,
                         middleware=[TodoMiddleware()], state_schema=PlanState)
    r1 = agent.invoke({"messages": [HumanMessage("请分析上半年寄递业务量并写报告。先列计划再执行。")]})
    show(r1)
    dump(r1, "run1")

    print()
    print("=" * 72)
    print("局 2 · 上下文丢失恢复（todos 留在 state，write_todos 已滑出上下文）")
    print("=" * 72)
    print("[用户] 继续上次没做完的。（历史已被摘要压缩，只剩两句）")
    # 模拟压缩后重开：历史里再无 write_todos 调用，todos 靠输入通道带回 state
    agent2 = create_agent(model=make_model2(), tools=TOOLS,
                          middleware=[TodoMiddleware()], state_schema=PlanState)
    r2 = agent2.invoke({
        "messages": [HumanMessage("继续上次没做完的。"), AIMessage("（摘要）上次已完成数据检索。")],
        "todos": PLAN,   # 状态里计划未销账
    })
    show(r2)
    dump(r2, "run2")

    leaked = [m for r in (r1, r2) for m in r["messages"]
              if getattr(m, "name", None) == "todo_completion_reminder"]
    print(f"\n催办落盘检查: {len(leaked)} 条（应为 0 —— 只进请求，不进历史）")
    print("产物: data/run1.json data/run2.json")


if __name__ == "__main__":
    main()
