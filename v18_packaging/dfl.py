"""dfl.py —— 一条命令拉起整套（对应本体 client.py + config/ 的合体语义）。

用法（在 deerflow-lab 仓库根目录）：
    python v18_packaging/dfl.py run "问题" --fake     # 全反射装配并跑一轮
    python v18_packaging/dfl.py list-tools            # 只看装配出了什么
    python v18_packaging/dfl.py validate-config       # 校验 config.yaml
    python v18_packaging/dfl.py validate-config --config v18_packaging/config_broken.yaml
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import yaml
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # 反射目标（tools_business 等）在本版目录

from assembly import (  # noqa: E402  （必须在 sys.path 修正之后）
    ConfigError, build_from_section, build_model, validate_config,
)

DATA = HERE / "data"


def load_cfg(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def reset_data() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for stale in DATA.iterdir():
        if stale.name != ".gitkeep":
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()


def cmd_validate_config(args) -> int:
    cfg = load_cfg(Path(args.config))
    problems = validate_config(cfg)
    if problems:
        print(f"✗ 配置被拒绝: {args.config}")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ 配置通过: {args.config}（models/tools/middlewares 三段齐全，use 写法合法）")
    return 0


def cmd_list_tools(args) -> int:
    cfg = load_cfg(Path(args.config))
    problems = validate_config(cfg)
    if problems:
        print("配置不合法，先跑 validate-config 看原因"); return 1
    for t in build_from_section(cfg, "tools"):
        print(f"tool: {t.name:<14} {t.description[:36]}")
    return 0


def cmd_run(args) -> int:
    cfg = load_cfg(Path(args.config))
    problems = validate_config(cfg)
    if problems:
        print("配置不合法，拒绝启动:"); [print(f"  - {p}") for p in problems]; return 1
    reset_data()

    model_name = "fake" if args.fake else "demo"
    model = build_model(cfg, model_name)
    tools = build_from_section(cfg, "tools")
    mws = build_from_section(cfg, "middlewares")
    print(f"装配完成: model={model_name} "
          f"tools={[t.name for t in tools]} middleware={[type(m).__name__ for m in mws]}")

    agent = create_agent(model=model, tools=tools, middleware=mws)
    state = agent.invoke({"messages": [HumanMessage(content=args.question)]})
    (DATA / "last_answer.txt").write_text(str(state["messages"][-1].content), encoding="utf-8")
    print(f"[用户] {args.question}")
    for msg in state["messages"]:
        cls = type(msg).__name__
        detail = msg.content or f"tool_calls={[t['name'] for t in msg.tool_calls]}"
        print(f"[{cls}] {detail}")
    print("（最终回答同时写入 data/last_answer.txt）")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="dfl", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="装配并运行一轮")
    p_run.add_argument("question")
    p_run.add_argument("--fake", action="store_true")
    p_run.add_argument("--config", default=str(HERE / "config.yaml"))
    p_run.set_defaults(fn=cmd_run)

    p_lt = sub.add_parser("list-tools", help="列出反射装配出的工具")
    p_lt.add_argument("--config", default=str(HERE / "config.yaml"))
    p_lt.set_defaults(fn=cmd_list_tools)

    p_vc = sub.add_parser("validate-config", help="静态校验配置")
    p_vc.add_argument("--config", default=str(HERE / "config.yaml"))
    p_vc.set_defaults(fn=cmd_validate_config)

    args = ap.parse_args()
    raise SystemExit(args.fn(args))


if __name__ == "__main__":
    main()
