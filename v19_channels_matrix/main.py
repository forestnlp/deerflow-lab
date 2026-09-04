"""v19 · 渠道矩阵 —— 一个 agent，三个渠道，消息不串门。

运行：
    conda run -n deerflow_lab python v19_channels_matrix/main.py --fake

观察重点：
1. 三个渠道（FakeIM / Console / Webhook）只实现三件套，路由层零改动；
2. 同一 agent 同时接三渠道，回复按 channel 字段回门，线程按联合键隔离；
3. 连通性自检：好渠道三段绿，缺 token 的 webhook 当场报红。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path

import yaml
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage

from channels import Bus, ConsoleChannel, FakeIM, Inbound, WebhookChannel
from manager import ChannelManager
from model_factory import fake_model, get_model, tool_call

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

CHANNEL_TYPES = {"fakeim": FakeIM, "console": ConsoleChannel, "webhook": WebhookChannel}


@tool
def get_indicator(indicator: str) -> str:
    """查询经营指标。indicator 取 收入 或 时长。"""
    table = {"收入": "H1 寄递收入 42.1 亿元", "时长": "平均投递时长 26.4 小时"}
    return table.get(indicator, f"no data for {indicator!r}")


def reset_data() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for stale in DATA.iterdir():
        if stale.name != ".gitkeep":
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()


def fake_script() -> list:
    """三条消息各一段剧本（dispatch 串行消费，剧本顺序 = 消息顺序）。"""
    return [
        tool_call("m1", "get_indicator", indicator="收入"),
        "FakeIM 答案：H1 寄递收入 42.1 亿元。",
        tool_call("m2", "get_indicator", indicator="时长"),
        "Webhook 答案：平均投递时长 26.4 小时。",
        tool_call("m3", "get_indicator", indicator="收入"),
        "Console 答案：H1 寄递收入 42.1 亿元。",
    ]


async def drive(fake: bool) -> None:
    reset_data()
    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))

    print("【A】连通性自检（对应本体渠道连接检查：connect + push 探针）")
    for spec in cfg["channels"]:
        ch = CHANNEL_TYPES[spec["name"]](Bus(), dict(spec))
        ok, why = await ch.self_test()
        label = f"{spec['name']}"
        print(f"  [{label:<8}] {'OK  ' if ok else 'FAIL'} {why}")
    bad = WebhookChannel(Bus(), dict(cfg["broken_channels"][0]))
    ok, why = await bad.self_test()
    label = "webhook(坏)"
    print(f"  [{label:<8}] {'OK  ' if ok else 'FAIL'} {why}")

    print("\n【B】一个 agent 同时接三渠道（消息并发进入，回复各回各家）")
    bus = Bus()
    fakeim = FakeIM(bus, {"script": []})
    console = ConsoleChannel(bus, {})
    webhook = WebhookChannel(bus, {"token": "dfl-secret",
                                   "endpoint": "https://fake.internal/hook"})
    for ch in (fakeim, console, webhook):
        await ch.connect()

    model = fake_model(fake_script()) if fake else get_model()
    agent = create_agent(model=model, tools=[get_indicator])

    async def run_handler(thread_id: str, msg: Inbound) -> str:
        state = agent.invoke(
            {"messages": [HumanMessage(content=f"[{msg.sender}] {msg.text}")]},
            config={"configurable": {"thread_id": thread_id}},
        )
        return str(state["messages"][-1].content)

    manager = ChannelManager(bus, run_handler)
    worker = asyncio.create_task(manager.run_forever())

    print("  >> [fakeim<-张三]      收入多少？")
    await fakeim.on_message(Inbound("fakeim", "room-1", "张三", "收入多少？", message_id="i1"))
    print("  >> [webhook POST<-李四] 时长多少？")
    await webhook.handle_http_post({"chat_id": "room-1", "user": "李四", "text": "时长多少？"})
    print("  >> [console<-王五]      再说一遍收入")
    await console.on_message(Inbound("console", "room-1", "王五", "再说一遍收入", message_id="i3"))

    await bus.inbound.put(None)      # 毒丸收尾
    await worker

    print(f"  << [fakeim->room-1] {fakeim.delivery_log[-1].text}")
    print(f"  << [webhook POST->{webhook.outbox[-1]['endpoint']}] {webhook.outbox[-1]['text']}")

    print("\n【C】对账：路由审计 + 会话隔离")
    for ch_name, thr in manager.routed:
        print(f"  回复 -> 渠道 {ch_name:<8} 线程 {thr}")
    (DATA / "routing.json").write_text(
        json.dumps({"threads": manager.threads, "routed": manager.routed},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  会话映射（联合键，chat_id 同名也不串）: "
          f"{json.dumps(manager.threads, ensure_ascii=False)}")
    print("  （审计已写 data/routing.json）")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()
    asyncio.run(drive(args.fake))


if __name__ == "__main__":
    main()
