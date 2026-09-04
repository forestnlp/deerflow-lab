"""v17 · 可观测 —— 看得见每一步，算得清每一分钱。

运行：
    conda run -n deerflow_lab python v17_observability/main.py --fake

观察重点：
1. 一个 trace_id 在 run 开始时生成，之后每条事件都带着它（contextvar 贯穿）；
2. 模型调用/工具调用两类事件入 SQLite：类型、耗时、token 估算、参数摘要；
3. run 结束按单价表折算费用；
4. 最后打印"最近 N 个 run 的耗时与费用"统计表（同一逻辑在 report.py CLI 里）。
"""

from __future__ import annotations

import argparse
import shutil
import time
from datetime import datetime
from pathlib import Path

import yaml
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage

from cost import CostCalculator
from model_factory import fake_model, get_model, tool_call
from observer_middleware import ObservabilityMiddleware
from report import render
from run_event_store import RunEventStore
from trace_context import current, trace_scope

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DB = DATA / "lab.sqlite"


def reset_data() -> None:
    """运行时产物只写本版 data/，启动时清空（保留 .gitkeep）。"""
    DATA.mkdir(parents=True, exist_ok=True)
    for stale in DATA.iterdir():
        if stale.name != ".gitkeep":
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()


@tool
def get_indicator(indicator: str) -> str:
    """查询经营指标。indicator 取 收入 或 时长。"""
    table = {"收入": "H1 寄递收入 42.1 亿元", "时长": "平均投递时长 26.4 小时"}
    return table.get(indicator, f"no data for {indicator!r}")


@tool
def flaky_report() -> str:
    """拉取第三方报表（已知不稳定）。"""
    return "Error: upstream timeout"


TOOLS = [get_indicator, flaky_report]


def scripts() -> list[tuple[str, list]]:
    """三个 run 的剧本：查指标、查时长、坏工具重试一次再交卷。"""
    return [
        ("H1 收入多少？", [
            tool_call("a1", "get_indicator", indicator="收入"),
            "H1 寄递收入 42.1 亿元。",
        ]),
        ("平均投递时长？", [
            tool_call("b1", "get_indicator", indicator="时长"),
            "平均投递时长 26.4 小时。",
        ]),
        ("拉第三方报表对比收入", [
            tool_call("c1", "flaky_report"),
            tool_call("c2", "get_indicator", indicator="收入"),
            "第三方报表超时；改用内部指标：H1 收入 42.1 亿元。",
        ]),
    ]


def run_once(prompt: str, script: list, store: RunEventStore, seq: int,
             model_factory) -> None:
    """一次带观测的 run：trace_scope 包住全程，中间件在任意深度都能取到 ids。"""
    run_id = f"run-{seq:04d}"          # 学习版用确定性 id，讲义输出才可复现
    trace_id = f"tr-{seq:04d}"
    thread_id = f"thread-{seq}"
    obs = ObservabilityMiddleware(store)
    agent = create_agent(model=model_factory(script), tools=TOOLS, middleware=[obs])

    with trace_scope(trace_id, run_id, thread_id):
        store.start_run(run_id, trace_id, thread_id, prompt)
        t0 = time.perf_counter()
        agent.invoke({"messages": [HumanMessage(content=prompt)]})
        dur = round(time.perf_counter() - t0, 3)
        store.record(run_id, trace_id, "run_end", "lifecycle", f"wall={dur}s")
        store.finish_run(run_id, obs.prompt_tokens, obs.completion_tokens)
        print(f"  run {run_id} 完成（trace_id={current()[0]} 已贯穿全部事件）")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()
    reset_data()

    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    pricing = cfg["pricing"]["demo"]
    store = RunEventStore(DB)
    calc = CostCalculator(pricing["per_1k_prompt"], pricing["per_1k_completion"])

    print(f"【A】连跑 {len(scripts())} 个 run（时间 {datetime.now():%Y}），事件写 {DB.name}")
    model_factory = (lambda s: fake_model(s)) if args.fake else get_model
    for i, (prompt, script) in enumerate(scripts(), start=1):
        run_once(prompt, script, store, i, model_factory)

    print("\n【B】最后一个 run 的事件流（seq 单调递增，就是审计的骨架）")
    last = store.recent_runs(limit=1)[0]
    for ev in last["events"]:
        extra = ev["meta"].get("duration_ms")
        tail = f" {extra}ms" if extra is not None else ""
        print(f"  seq={ev['seq']} [{ev['category']}/{ev['event_type']}] {ev['content']}{tail}")

    print("\n【C】最近 5 个 run 的耗时与费用（费用列 = token×单价，单价见 config.yaml）")
    print(render(store, calc, limit=5))
    store.close()
    print("\n提示：同一张表也可以随时单独查：python v17_observability/report.py")


if __name__ == "__main__":
    main()
