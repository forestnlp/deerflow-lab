"""v07 · 标题与长期记忆 —— 聊完自动起标题，重启会话还记得你。

运行：
    conda run -n deerflow_lab python v07_title_memory/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点：
1. 首轮完整问答后 TitleMiddleware 生成标题进 state；第二轮不再重复生成；
2. 同一 thread 连聊两轮，防抖器只抽取一次（两轮的素材合并结算）；
3. 模拟"重启进程"：新 agent、新 thread，只有一份 data/memory.json——
   <system-reminder> 里躺着上一世的事实，本轮回答直接引用了它。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from memory import DebouncedExtractor, load_facts
from memory_middleware import MemoryInjectMiddleware, MemoryMiddleware
from model_factory import fake_model, get_model, tool_call
from title_middleware import TitleMiddleware
from tools import TOOLS

DATA = Path(__file__).resolve().parent / "data"
MEMORY_PATH = DATA / "memory.json"
DEBOUNCE_S = 0.35


def title_model_factory(prompt_text: str, fake: bool):
    """标题模型：--fake 用固定剧本；在线走 config 的 title 模型。"""
    if fake:
        topic = "月度经营报告提纲" if "提纲" in prompt_text else "经营口径与表格偏好"
        return fake_model([topic])
    return get_model("title")


def extractor_factory(old_facts: list[str], users: list[str], fake: bool):
    """抽取模型：--fake 按素材关键词给剧本；在线走 config 的 memory 模型。"""
    if fake:
        joined = "".join(users)
        if "经营分析岗" in joined:
            return fake_model(["用户已调至经营分析岗", "报告偏好：表格优先"])
        if "提纲" in joined:
            return fake_model(["用户正在准备月度经营报告提纲"])
        return fake_model([""])          # 无新事实：记忆原样
    prompt = ("合并以下旧事实与对话，输出最新事实清单（每行一条）：\n旧：" +
              "；".join(old_facts) + "\n对话：" + " | ".join(users))
    return get_model("memory")


def build_agent(saver: MemorySaver, extractor: DebouncedExtractor, fake: bool,
                script: list):
    model = fake_model(script) if fake else get_model()
    return create_agent(
        model=model,
        tools=TOOLS,
        middleware=[
            MemoryInjectMiddleware(MEMORY_PATH),
            TitleMiddleware(lambda p: title_model_factory(p, fake)),
            MemoryMiddleware(extractor),
        ],
        checkpointer=saver,
    )


def run_turn(label: str, agent, thread: str, ask: str) -> dict:
    print(f"\n[{label}]")
    print(f"  [用户] {ask}")
    state = agent.invoke({"messages": [HumanMessage(ask)]},
                         config={"configurable": {"thread_id": thread}})
    for msg in state["messages"]:
        cls = type(msg).__name__
        if getattr(msg, "name", None) == "memory_reminder":
            print(f"  [{cls}·reminder] {str(msg.content)[:88]}…")
        else:
            detail = msg.content or f"tool_calls={[tc['name'] for tc in msg.tool_calls]}"
            print(f"  [{cls}] {detail}")
    print(f"  [state.title] {state.get('title')}")
    return state


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    MEMORY_PATH.unlink(missing_ok=True)     # 运行时产物启动时清空，保证可重复

    extractor = DebouncedExtractor(
        MEMORY_PATH, lambda old, users: extractor_factory(old, users, args.fake),
        debounce_s=DEBOUNCE_S)
    saver = MemorySaver()                   # 记忆中间件与 checkpointer 无关：它只管磁盘 facts

    # —— 上一世：同一 thread 连聊两轮，防抖器应在安静期只结算一次 ——
    a1 = build_agent(saver, extractor, args.fake, [
        tool_call("g1", "get_note", tag="role"),
        "收到！你已调至经营分析岗，以后报告表格优先，我记下了。",
    ])
    run_turn("第一轮 · 交代新情况", a1, "t-a",
             "我调去经营分析岗了，以后报告都要表格优先，记一下。")
    a2 = build_agent(saver, extractor, args.fake, [
        "好，提纲里会给同比对比留出位置。",
    ])
    run_turn("第二轮 · 紧接着追问（防抖窗口内）", a2, "t-a",
             "顺便想个月度经营报告的提纲方向。")

    time.sleep(DEBOUNCE_S + 0.25)           # 等防抖安静期过去，抽取自动结算
    print(f"\n[data/memory.json] {load_facts(MEMORY_PATH)}")

    # —— 重启一世：全新 agent / 全新 thread，唯一纽带是磁盘上的 memory.json ——
    print("\n———— 模拟重启：新进程、新会话（thread=t-b），checkpointer 全丢 ————")
    b1 = build_agent(MemorySaver(), extractor, args.fake, [
        tool_call("g2", "get_note", tag="style"),
        "当然记得：你在经营分析岗、报告要表格优先。提纲按『总览-分项-同比-结论』四表展开。",
    ])
    run_turn("重启后第一轮 · 试探记忆", b1, "t-b",
             "还记得我的偏好吗？给我月度经营报告提纲。")

    extractor.flush()                       # 本体 shutdown_flush 同语义：退场前清缓冲
    facts = load_facts(MEMORY_PATH)
    print(f"\n最终记忆（两轮会话共享同一份磁盘文件）:")
    for f in facts:
        print(f"  · {f}")


if __name__ == "__main__":
    main()
