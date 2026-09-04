"""v14 · 渠道 —— MessageBus + chat→thread 映射 + 去重 + 命令 + 长文切分（FakeIM 全回路）。

运行：
    conda run -n deerflow_lab python v14_channels/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake（命令行当聊天窗）

--fake 全自动回路：FakeIM 发消息 → bus.inbound → ChannelManager
    （去重/命令/映射）→ 带 SqliteSaver 的 run → bus.outbound → FakeIM 分片收信。
    演示：正常问答、重复投递只回一次、/status /new 命令、映射跨"重启"持久、
    长回复 4 片送达 —— 全程断言，退出码 0。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path

from langchain.agents import create_agent
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from dedupe_store import DedupeStore
from fakeim import FakeIM
from manager import ChannelManager
from message_bus import MessageBus
from model_factory import fake_model, get_model
from store import ThreadMappingStore

DATA_DIR = Path(__file__).resolve().parent / "data"
SCRIPTS = {
    "我喜欢蓝色": "收到：你说了你喜欢蓝色，记下了。",
    "我喜欢的颜色是什么？": "翻历史查到：你喜欢蓝色。",
}
LONG_PARAS = [f"第{i}段：" + "故事" * 120 for i in range(1, 5)]   # 每段 244 字
CHUNK_LIMIT = 400


def make_run_handler(saver, fake: bool):
    async def handler(thread_id: str, msg) -> str:
        text = msg.text.strip()
        if text.startswith("讲三个故事"):
            reply = "\n\n".join(LONG_PARAS)
            script = [reply]
        else:
            script = [SCRIPTS.get(text, f"（通用回复）收到：{text}")]
        model = fake_model(script) if fake else get_model()
        agent = create_agent(model=model, tools=[], checkpointer=saver)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": text}]},
            config={"configurable": {"thread_id": thread_id}},
        )
        return result["messages"][-1].content

    return handler


async def history_len(saver, thread_id: str) -> int:
    tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return 0
    return len(tup.checkpoint["channel_values"]["messages"])


async def demo_fake() -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)   # 启动时清空，保证可重复运行
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    bus = MessageBus()
    im = FakeIM(bus, chunk_limit=CHUNK_LIMIT)
    im.subscribe()
    mapping = ThreadMappingStore(DATA_DIR / "mapping.json")
    dedupe = DedupeStore(DATA_DIR / "dedupe.db")

    async with AsyncSqliteSaver.from_conn_string(str(DATA_DIR / "checkpoints.db")) as saver:
        mgr = ChannelManager(bus, mapping, dedupe, make_run_handler(saver, fake=True))
        worker = asyncio.create_task(mgr.run_forever())

        async def chat(text: str, message_id: str | None = None) -> None:
            print(f"[用户] fakeim:g1 发「{text[:18]}」（id={message_id or '随机'}）")
            bus.processed.clear()                       # 先清灯再发货
            await im.send("g1", text, message_id=message_id)
            await bus.processed.wait()                  # 等 manager 回执

        print("── ① 正常问答：消息 → run → 回复 ──")
        await chat("我喜欢蓝色", message_id="m-1")

        print("── ② 重复投递：IM 重推同 message_id，不再回第二次 ──")
        n_before = len(im.received)
        await chat("我喜欢蓝色", message_id="m-1")
        await chat("我喜欢蓝色", message_id="m-1")
        assert len(im.received) == n_before, "重复投递不应产生新回复"

        print("── ③ 同群第二条消息：同 thread，历史续得上 ──")
        await chat("我喜欢的颜色是什么？")
        thread1 = mapping.get_or_create("fakeim", "g1")
        print(f"[映射] fakeim:g1 -> {thread1}，checkpoint 历史 {await history_len(saver, thread1)} 条")
        assert await history_len(saver, thread1) == 4

        print("── ④ 命令：/status 不改线程，/new 换线程 ──")
        await chat("/status")
        await chat("/new")
        thread2 = mapping.get_or_create("fakeim", "g1")
        assert thread2 != thread1
        await chat("我喜欢蓝色")
        assert await history_len(saver, thread2) == 2, "新线程从零开始"
        assert await history_len(saver, thread1) == 4, "旧线程现场不动"

        print("── ⑤ 长回复切分：一条 976 字回复按 ≤400 字分片送达 ──")
        n_before = len(im.received)
        await chat("讲三个故事")
        chunks = [c for _, c in im.received[n_before:]]
        print(f"[IM ] 长回复拆成 {len(chunks)} 片，各片长度 {[len(c) for c in chunks]}")
        assert len(chunks) == 4 and all(len(c) <= CHUNK_LIMIT for c in chunks)

        print("── ⑥ 未知命令提示 + 映射持久化（模拟重启）──")
        await chat("/rm -rf")
        reborn = ThreadMappingStore(DATA_DIR / "mapping.json")
        assert reborn.all() == mapping.all()
        print(f"[持久] 重开映射表：{reborn.all()}")

        await bus.inbound.put(None)               # 哨兵：让 manager 收工
        await worker
    dedupe.close()
    print(f"[收尾] FakeIM 共收到 {len(im.received)} 条消息；全链路断言通过（退出码 0）")


async def online_repl() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    im = FakeIM(bus)
    im.subscribe()
    mapping = ThreadMappingStore(DATA_DIR / "mapping.json")
    dedupe = DedupeStore(DATA_DIR / "dedupe.db")
    async with AsyncSqliteSaver.from_conn_string(str(DATA_DIR / "checkpoints.db")) as saver:
        mgr = ChannelManager(bus, mapping, dedupe, make_run_handler(saver, fake=False))
        worker = asyncio.create_task(mgr.run_forever())
        print("命令行聊天窗（/new /status /help，空行退出）")
        while True:
            text = input("\n你> ").strip()
            if not text:
                break
            n = len(im.received)
            bus.processed.clear()
            await im.send("cli", text)
            await bus.processed.wait()
            for _, chunk in im.received[n:]:
                print(f"AI> {chunk}")
        await bus.inbound.put(None)
        await worker
    dedupe.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="FakeIM 离线全回路演示，零 API key")
    args = ap.parse_args()
    if args.fake:
        asyncio.run(demo_fake())
    else:
        asyncio.run(online_repl())


if __name__ == "__main__":
    main()
