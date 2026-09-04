"""ChannelManager —— 去重、命令、会话映射、串行保护、分发到 run。

参考本体：backend/app/channels/manager.py
        （message_id 去重 / 内置命令 / (channel,chat_id)→thread_id 映射 /
         同 chat 串行保护 / 组装出站回复）
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from commands import HELP_TEXT, parse_command
from dedupe_store import DedupeStore
from message_bus import InboundMessage, MessageBus, OutboundMessage
from store import ThreadMappingStore

RunHandler = Callable[[str, InboundMessage], Awaitable[str]]   # (thread_id, msg) -> 回复文本


class ChannelManager:
    """渠道消息的总机：一条 inbound 进来，判去重/命令/正常，出去一路 reply。"""

    def __init__(self, bus: MessageBus, mapping: ThreadMappingStore,
                 dedupe: DedupeStore, run_handler: RunHandler) -> None:
        self.bus = bus
        self.mapping = mapping
        self.dedupe = dedupe
        self.run_handler = run_handler
        self._busy_chats: set[str] = set()
        self._stopped = False

    async def run_forever(self) -> None:
        """主循环：消费 inbound；收到 None 哨兵或 stop() 后退出。"""
        while not self._stopped:
            msg = await self.bus.inbound.get()
            if msg is None:
                return
            await self._dispatch(msg)

    def stop(self) -> None:
        self._stopped = True

    async def _dispatch(self, msg: InboundMessage) -> None:
        try:
            await self._route(msg)
        finally:
            self.bus.processed.set()      # 无论走到哪一步，这条算"处理过了"

    async def _route(self, msg: InboundMessage) -> None:
        # ① 去重：IM 的重推/长轮询会把同一条消息送来多次
        if self.dedupe.seen_before(msg.message_id):
            print(f"  [manager] 去重丢弃 {msg.channel_name}:{msg.message_id}")
            return

        # ② 内置命令：不进 agent，渠道层直接答
        cmd = parse_command(msg.text)
        if cmd is not None:
            await self._reply(msg, self._command_text(cmd.name, msg))
            return

        # ③ 会话映射 + 同 chat 串行：上一条没回完，先说"稍候"，绝不并发写同一线程
        thread_id = self.mapping.get_or_create(msg.channel_name, msg.chat_id)
        chat_key = f"{msg.channel_name}:{msg.chat_id}"
        if chat_key in self._busy_chats:
            await self._reply(msg, "上一条还在处理中，请稍候…")
            return
        self._busy_chats.add(chat_key)
        try:
            reply = await self.run_handler(thread_id, msg)
        finally:
            self._busy_chats.discard(chat_key)

        # ④ 出站：长文切分在渠道适配器里做（FakeIM.send），manager 只管发一条
        await self._reply(msg, reply, thread_id=thread_id)

    def _command_text(self, name: str, msg: InboundMessage) -> str:
        if name == "new":
            return f"已开启新会话：{self.mapping.reset(msg.channel_name, msg.chat_id)}"
        if name == "status":
            thread = self.mapping.get_or_create(msg.channel_name, msg.chat_id)
            return f"channel={msg.channel_name} chat={msg.chat_id} thread={thread}（同群成员共享会话）"
        if name == "help":
            return HELP_TEXT
        return f"未知命令 /{name}，发送 /help 查看帮助"

    async def _reply(self, msg: InboundMessage, text: str, thread_id: str = "-") -> None:
        await self.bus.publish_outbound(OutboundMessage(
            channel_name=msg.channel_name, chat_id=msg.chat_id,
            thread_id=thread_id, text=text))
