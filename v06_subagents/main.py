"""v06 · 子代理 —— 大任务外包：task 工具、后台线程、并发闸、禁止套娃。

运行：
    conda run -n deerflow_lab python v06_subagents/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点：
1. 主 agent 只发一条含 3 个 task 调用的 AIMessage，三个子代理在后台线程
   并行起跑；并发闸 MAX_CONCURRENT_SUBAGENTS=2，所以必有一个排队；
2. 每个 task 的 ToolMessage 带回子代理的事件流水（起跑→调工具→交终稿）；
3. 主 agent 随后委派给不存在的类型 intern —— 工具不抛异常，把可用类型
   回给模型让它自纠；注册表里每个子代理的工具清单都没有 task —— 套娃无门。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

import executor
from model_factory import fake_model, get_model, tool_call
from subagents import REGISTRY, available_names, resolve_tools
from task_tool import configure, task
from tools import LEAD_TOOLS

DATA = Path(__file__).resolve().parent / "data"
NOTES = DATA / "notes"

# 资料是静态输入，但按 STYLE_GUIDE"启动时清空"的纪律每次重写，保证幂等
NOTES_TEXT = {
    "alpha": "ReAct 循环就是 model 和 tool 的乒乓：模型出招，工具接招，回合制推进。",
    "beta": "上下文是稀缺资源：窗口有限，塞进去的每一 token 都要值回票价。",
    "gamma": "护栏不是可有可无的装饰，它决定 agent 失控时砸的是墙还是客户。",
}


def reset_data_dir() -> None:
    """运行时产物只写本版 data/，启动时清空（保留 .gitkeep，重写资料）。"""
    if DATA.exists():
        for p in DATA.iterdir():
            if p.name not in (".gitkeep", "notes"):
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    NOTES.mkdir(parents=True, exist_ok=True)
    for name, text in NOTES_TEXT.items():
        (NOTES / f"{name}.txt").write_text(text, encoding="utf-8")


def fake_sub_script(task_text: str) -> list:
    """--fake 子代理剧本：按任务书关键词选脚本，输出确定。"""
    if "alpha" in task_text:
        return [tool_call("r1", "read_notes", note="alpha"),
                "alpha 要点：ReAct 是 model 与 tool 的回合制乒乓。"]
    if "beta" in task_text:
        return [tool_call("r2", "read_notes", note="beta"),
                "beta 要点：上下文是稀缺资源，每 token 都要值回票价。"]
    if "gamma" in task_text:
        return [tool_call("w1", "write_report",
                          text="护栏决定 agent 失控时砸的是墙还是客户，必须先立规矩再谈自由。"),
                "报告已落盘 data/report.txt。"]
    return ["（无匹配剧本，直接收尾）"]


def lead_script() -> list:
    """主 agent 剧本：一次委派三件事（并发闸只有 2 个名额），再试错一次。"""
    three = AIMessage(content="", tool_calls=[
        {"name": "task", "args": {"description": "精读 alpha",
                                  "prompt": "精读 alpha 并给出一句话要点",
                                  "subagent_type": "reader"}, "id": "c1"},
        {"name": "task", "args": {"description": "精读 beta",
                                  "prompt": "精读 beta 并给出一句话要点",
                                  "subagent_type": "reader"}, "id": "c2"},
        {"name": "task", "args": {"description": "撰写 gamma 报告",
                                  "prompt": "依据 gamma 撰写一句话报告并落盘",
                                  "subagent_type": "writer"}, "id": "c3"},
    ])
    return [
        three,
        tool_call("c4", "task", description="让实习生收尾",
                  prompt="把结论整理一下", subagent_type="intern"),
        "汇总：两份要点已带回，报告已落盘；intern 类型不存在，下次改用注册表里的类型。",
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    reset_data_dir()
    executor.reset_stats()
    executor.reset_background()

    if args.fake:
        # 子代理剧本按任务书选；latency 取合同里的 latency_ms（真实 LLM 本来就有耗时）
        def sub_factory(model_name, task_text, latency_ms):
            return fake_model(fake_sub_script(task_text), latency_s=latency_ms / 1000.0)
        lead_model = fake_model(lead_script())
    else:
        def sub_factory(model_name, task_text, latency_ms):
            return get_model(model_name)
        lead_model = get_model()
    configure(sub_factory)

    agent = create_agent(model=lead_model, tools=[*LEAD_TOOLS, task])
    prompt = ("派 reader 分别精读 alpha 和 beta 各给一句要点；派 writer 把 gamma 的要点"
              "写成一句话报告落盘。做完后再派 intern 收个尾。")
    print(f"[用户] {prompt}\n")

    state = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    for msg in state["messages"]:
        cls = type(msg).__name__
        detail = msg.content or f"tool_calls={[(tc['name'], tc['args']) for tc in msg.tool_calls]}"
        print(f"[{cls}] {detail}")

    print("\n并发闸观察（MAX_CONCURRENT_SUBAGENTS=2）:")
    print(f"  同时运行峰值 = {executor.peak_concurrency()}，"
          f"因名额已满排队次数 = {executor.queue_waits()}")

    print("\n子代理注册表（合同里就没有 task 工具 —— 禁止套娃）:")
    for name in available_names():
        cfg = REGISTRY[name]
        tool_names = [t.name for t in resolve_tools(cfg)]
        print(f"  {name}: model={cfg.model} groups={cfg.groups} tools={tool_names}")

    report = DATA / "report.txt"
    print(f"\n报告文件 data/report.txt: "
          f"{report.read_text(encoding='utf-8') if report.exists() else '（未生成）'}")


if __name__ == "__main__":
    main()
