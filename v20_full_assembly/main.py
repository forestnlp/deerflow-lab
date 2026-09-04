"""v20 · 总装 —— 按本体接线图，全链合一，三个入口汇一张图。

运行：
    conda run -n deerflow_lab python v20_full_assembly/main.py --fake

三幕（同一个 lead_agent 图，三个入口）：
    幕1 CLI   : write_todos -> 查指标 -> 提前交卷被拦 -> 补完 todo -> 终稿
    幕2 调度器: flaky 三连 -> LoopDetection 刹车 -> ask_clarification -> 本轮挂起
    幕3 渠道  : 群消息进来 -> 直接答 -> 记忆/标题旁路照常工作
"""

from __future__ import annotations

import argparse
import shutil
from collections import deque
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from lead_agent import make_lead_agent
from model_factory import fake_model, get_model, tool_call

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def reset_data() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for stale in DATA.iterdir():
        if stale.name != ".gitkeep":
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()


# ---------------------------------------------------------------- 剧本
def script_act1() -> list:
    plan = [{"content": "查收入指标", "status": "in_progress"},
            {"content": "汇总周报", "status": "pending"}]
    done = [{"content": "查收入指标", "status": "completed"},
            {"content": "汇总周报", "status": "completed"}]
    return [
        tool_call("t1", "write_todos", todos=plan),
        tool_call("t2", "get_indicator", indicator="收入"),
        AIMessage(content="周报已生成。"),          # todo2 还挂着 -> 拦截
        tool_call("t3", "write_todos", todos=done),
        "完成：H1 寄递收入 42.1 亿元，周报已汇总。",
    ]


def script_act2() -> list:
    return [
        tool_call("t4", "flaky_report"),
        tool_call("t5", "flaky_report"),
        tool_call("t6", "flaky_report"),           # 三连同参 -> 刹车
        tool_call("t7", "ask_clarification",
                  question="第三方报表持续超时，稍后重试还是先用内部指标？"),
    ]


def script_act3() -> list:
    return [AIMessage(content="今日无新增数据，昨日结论仍有效。")]


def fake_script() -> list:
    return [*script_act1(), *script_act2(), *script_act3()]


# ---------------------------------------------------------------- 三个入口
def entry_cli(agent, prompt: str, thread_id: str) -> str:
    """入口一：命令行。最朴素：读一行字，喂给图，打完收工。"""
    state = agent.invoke({"messages": [HumanMessage(content=prompt)]},
                         config={"configurable": {"thread_id": thread_id}})
    return str(state["messages"][-1].content)


class SchedulerEntry:
    """入口二：调度器最小版 —— 任务表 + "到点就跑"，跑的还是那张图。"""

    def __init__(self, agent):
        self.agent = agent
        self.table: list[tuple[str, str]] = []      # (人类可读排程, 提示词)

    def add(self, cron_desc: str, prompt: str) -> None:
        self.table.append((cron_desc, prompt))

    def tick(self, now_desc: str) -> list[str]:
        fired = []
        for cron_desc, prompt in self.table:
            if now_desc in cron_desc:               # 学习版匹配：描述里含当前时刻即到点
                print(f"  << [scheduler] {cron_desc} 触发: {prompt}")
                state = self.agent.invoke(
                    {"messages": [HumanMessage(content=f"[定时任务] {prompt}")]},
                    config={"configurable": {"thread_id": "thr-scheduler"}},
                )
                last = state["messages"][-1]
                asks = [tc for tc in (getattr(last, "tool_calls", []) or [])
                        if tc["name"] == "ask_clarification"]
                if asks:                            # 澄清语义：问题在 tool_call 里，转成人话
                    fired.append(f"❓ {asks[-1]['args'].get('question', '请补充')}")
                else:
                    fired.append(str(last.content))
        return fired


class ChannelEntry:
    """入口三：渠道最小版 —— 一条入站队列 + 回投，机制同 v19 但只留骨架。"""

    def __init__(self, agent):
        self.agent = agent
        self.inbox: deque = deque()
        self.sent: list[str] = []

    def receive(self, chat_id: str, sender: str, text: str) -> None:
        self.inbox.append((chat_id, sender, text))

    def pump(self) -> None:
        while self.inbox:
            chat_id, sender, text = self.inbox.popleft()
            state = self.agent.invoke(
                {"messages": [HumanMessage(content=f"[{sender}] {text}")]},
                config={"configurable": {"thread_id": f"thr-{chat_id}"}},
            )
            reply = str(state["messages"][-1].content)
            self.sent.append(f"[-> {chat_id}] {reply}")


# ---------------------------------------------------------------- 主程序
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线剧本模型，零 API key")
    args = ap.parse_args()
    reset_data()

    model = fake_model(fake_script()) if args.fake else get_model()
    agent = make_lead_agent(model, data_dir=DATA)

    print("\n" + "=" * 20 + " 幕1：CLI 入口（todo + 摘要 + 防提前退出） " + "=" * 20)
    ans = entry_cli(agent, "生成本周周报，先列计划再干活", "thr-cli")
    print(f"[CLI 回答] {ans}")

    print("\n" + "=" * 20 + " 幕2：调度器入口（循环刹车 + 澄清挂起） " + "=" * 18)
    sched = SchedulerEntry(agent)
    sched.add("每天 09:00 拉第三方报表并对比", "拉第三方报表对比收入")
    for out in sched.tick("每天 09:00"):
        print(f"[调度产出] {out}")

    print("\n" + "=" * 20 + " 幕3：渠道入口（旁路机制照常） " + "=" * 20)
    ch = ChannelEntry(agent)
    ch.receive("room-经营群", "张三", "今天有什么新数据吗？")
    ch.pump()
    for line in ch.sent:
        print(f"[渠道送出] {line}")

    print("\n" + "=" * 20 + " 收尾对账 " + "=" * 20)
    mem_lines = (DATA / "memory.log").read_text(encoding="utf-8").splitlines()
    print(f"长期记忆（{len(mem_lines)} 条，每 run 一条）:")
    for line in mem_lines:
        print(f"  - {line}")
    print(f"运行时产物: {sorted(p.name for p in DATA.iterdir())}")


if __name__ == "__main__":
    main()
