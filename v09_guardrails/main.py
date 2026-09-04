"""v09 · 行为护栏 —— 转圈、悬空、说半截、硬往上冲，四个场景逐个接住。

运行：
    conda run -n deerflow_lab python v09_guardrails/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点：
1. 循环检测：同一 calculator 调用刷到第 3 次警告排队、第 5 次剥 tool_calls；
2. 悬空修复：历史里无配对的 tool_calls 在请求边界被补合成回执，孤儿回执丢弃，
   图状态里的原始历史一字未动；
3. 安全终止：finish_reason=content_filter 的消息被剥爪+回填截断说明，不抛异常；
4. 澄清：ask_clarification 被拦截（SHOULD-NOT-EXECUTE 永不出现），
   提问回执落盘后 jump end 收束，剧本里的下一条消息永远轮不到。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from guard_middlewares import (ClarificationMiddleware, DanglingToolCallMiddleware,
                               LoopDetectionMiddleware, SafetyFinishReasonMiddleware)
from model_factory import fake_model, get_model, tool_call
from tools import TOOLS

DATA = Path(__file__).resolve().parent / "data"


def show(state_messages, note=""):
    for msg in state_messages:
        cls = type(msg).__name__
        detail = msg.content or f"tool_calls={[tc['name'] for tc in msg.tool_calls]}"
        print(f"  [{cls}] {str(detail)[:96]}")
    if note:
        print(f"  {note}")


def scenario_loop(fake: bool):
    print("=" * 52)
    print("场景① LoopDetection：同一调用刷满 5 次，剥爪交卷")
    print("=" * 52)
    guard = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    # 剧本：同一调用刷 5 遍后还想交卷 —— 但第 5 次就被剥爪了，第 6 条永远轮不到
    script = [tool_call(f"c{i}", "calculator", expression="(2+3)*7") for i in range(1, 6)] + \
             ["我不该被看到"]
    model = fake_model(script) if fake else get_model()
    agent = create_agent(model=model, tools=TOOLS, middleware=[guard])
    state = agent.invoke({"messages": [HumanMessage("反复算 (2+3)*7，算五遍，别停。")]})
    show(state["messages"][-3:], note=f"[stop_reason] {guard.stop_reason}")


def scenario_dangling(fake: bool):
    print()
    print("=" * 52)
    print("场景② DanglingToolCall：断头调用在请求边界被补齐")
    print("=" * 52)
    guard = DanglingToolCallMiddleware()
    # 手工构造"事故现场"：一个没有回执的 tool_call + 一条没有父亲的孤儿回执
    history = [
        HumanMessage("上周你查华东销量，后来会话断了。"),
        AIMessage(content="", tool_calls=[
            {"name": "query_sales", "args": {"metric": "volume", "region": "华东"},
             "id": "lost-1"}]),                       # ← 悬空：没有对应 ToolMessage
        ToolMessage(content="孤儿数据", tool_call_id="ghost-9"),   # ← 孤儿：父调用不存在
        HumanMessage("接着上次说，结果呢？"),
    ]
    model = fake_model(["上次的查询因中断没有返回。我已按你的新问题继续。"]) if fake else get_model()
    agent = create_agent(model=model, tools=TOOLS, middleware=[guard])
    state = agent.invoke({"messages": history})
    show(state["messages"][-2:])
    print("  [自检] 落盘历史里 lost-1 依旧没有回执（补丁只活在请求里）："
          f" {all(not (isinstance(m, ToolMessage) and m.tool_call_id == 'lost-1') for m in state['messages'])}")


def scenario_safety(fake: bool):
    print()
    print("=" * 52)
    print("场景③ SafetyFinish：finish_reason 异常时的安全收尾")
    print("=" * 52)
    guard = SafetyFinishReasonMiddleware()
    cut = AIMessage(content="季度收入的前半段分析：华东同比 +8%，华南",
                    response_metadata={"finish_reason": "content_filter"})
    model = fake_model([cut, "我不该被看到"]) if fake else get_model()
    agent = create_agent(model=model, tools=TOOLS, middleware=[guard])
    state = agent.invoke({"messages": [HumanMessage("给我全季度收入分析。")]})
    show(state["messages"][-1:], note=f"[stop_reason] {guard.stop_reason}")


def scenario_clarify(fake: bool):
    print()
    print("=" * 52)
    print("场景④ Clarification：信息不足，拦下工具、收束本轮、把问题还给你")
    print("=" * 52)
    guard = ClarificationMiddleware()
    model = (fake_model([tool_call("q1", "ask_clarification",
                                   question="要查哪个区域、哪个指标、哪个季度？"),
                         "我不该被看到"])
             if fake else get_model())
    agent = create_agent(model=model, tools=TOOLS, middleware=[guard])
    state = agent.invoke({"messages": [HumanMessage("查下销售情况。")]})
    show(state["messages"], note="（剧本的第二条消息没有机会执行 —— jump end 生效）")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    log = DATA / "guardrail_events.txt"
    log.unlink(missing_ok=True)

    scenario_loop(args.fake)
    scenario_dangling(args.fake)
    scenario_safety(args.fake)
    scenario_clarify(args.fake)

    log.write_text("loop_capped | dangling_patched | safety_capped | clarification_jump_end\n",
                   encoding="utf-8")
    print(f"\n四道护栏各自的现场签名已落盘 data/guardrail_events.txt")


if __name__ == "__main__":
    main()
