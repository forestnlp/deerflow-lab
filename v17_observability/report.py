"""观测查询 CLI —— "最近 5 个 run 各自耗时与费用"。
参考本体：本体的观测走 Langfuse/LangSmith（tracing/factory.py）+ run 事件 API；
学习版把查询做成一条本地命令，字段与讲义【跑起来】一节逐字对应。

用法：
    python v17_observability/report.py            # 默认读本版 data/lab.sqlite
    python v17_observability/report.py --limit 3
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from cost import CostCalculator
from run_event_store import RunEventStore

HERE = Path(__file__).resolve().parent


def render(store: RunEventStore, calc: CostCalculator, limit: int = 5) -> str:
    runs = store.recent_runs(limit=limit)
    lines = []
    lines.append(f"{'run_id':<14}{'trace_id':<14}{'耗时(s)':>9}{'事件':>5}"
                 f"{'tokens(估)':>12}{'费用(USD)':>12}  提示词")
    lines.append("-" * 96)
    total = 0.0
    for r in runs:
        cost = calc.cost_usd(r["prompt_tokens"], r["completion_tokens"])
        total += cost
        tokens = f"{r['prompt_tokens']}+{r['completion_tokens']}"
        lines.append(f"{r['run_id']:<14}{r['trace_id']:<14}{r['duration_s']:>9.3f}"
                     f"{len(r['events']):>5}{tokens:>12}{cost:>12.5f}  {r['prompt'][:24]}")
    lines.append("-" * 96)
    lines.append(f"合计费用（本次展示 {len(runs)} 个 run）: {total:.5f} USD")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--db", type=Path, default=HERE / "data" / "lab.sqlite")
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"找不到 {args.db}——先跑一次 python v17_observability/main.py --fake")

    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    pricing = cfg["pricing"]["demo"]
    store = RunEventStore(args.db)
    try:
        print(render(store, CostCalculator(pricing["per_1k_prompt"], pricing["per_1k_completion"]),
                     limit=args.limit))
    finally:
        store.close()


if __name__ == "__main__":
    main()
