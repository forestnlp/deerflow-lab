"""v18 · 配置驱动贯彻到底 —— 一份 config.yaml 装出整个 agent。

运行：
    conda run -n deerflow_lab python v18_packaging/main.py --fake

等价于 CLI 的完整旅程（main.py 是它的无人值守浓缩版）：
    validate-config -> 反射装配 models/tools/middlewares -> create_agent -> 回答
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
sys.path.insert(0, str(HERE))

from assembly import build_from_section, build_model, validate_config  # noqa: E402

DATA = HERE / "data"


def reset_data() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for stale in DATA.iterdir():
        if stale.name != ".gitkeep":
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线剧本模型，零 API key")
    args = ap.parse_args()
    reset_data()

    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))

    print("【A】validate-config：错误配置活不过启动期")
    problems = validate_config(cfg)
    print(f"  config.yaml -> {'通过' if not problems else problems}")
    broken = yaml.safe_load((HERE / "config_broken.yaml").read_text(encoding="utf-8"))
    for p in validate_config(broken):
        print(f"  config_broken.yaml 被拒绝: {p}")

    print("\n【B】反射装配：三段 use 字符串变成三个活对象集合")
    model_name = "fake" if args.fake else "demo"
    model = build_model(cfg, model_name)
    tools = build_from_section(cfg, "tools")
    mws = build_from_section(cfg, "middlewares")
    print(f"  model      = {type(model).__name__} (name={model_name})")
    print(f"  tools      = {[t.name for t in tools]}")
    print(f"  middleware = {[type(m).__name__ for m in mws]}")

    print("\n【C】装配产物直接开跑（和手写 create_agent 没有任何区别）")
    agent = create_agent(model=model, tools=tools, middleware=mws)
    prompt = "查一下收入指标，然后收尾。"
    print(f"  [用户] {prompt}")
    state = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    for msg in state["messages"]:
        cls = type(msg).__name__
        detail = msg.content or f"tool_calls={[t['name'] for t in msg.tool_calls]}"
        print(f"  [{cls}] {detail}")
    (DATA / "last_answer.txt").write_text(str(state["messages"][-1].content), encoding="utf-8")
    print("\n收尾：加一个工具 = 往 tools 段加两行；换一个模型 = 改一个 name。代码零改动。")


if __name__ == "__main__":
    main()
