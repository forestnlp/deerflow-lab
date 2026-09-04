"""MessageBus —— 渠道与调度器之间的异步 pub/sub 集线器。

参考本体：backend/app/channels/message_bus.py
        （InboundMessage/OutboundMessage + MessageBus：inbound asyncio 队列
         + outbound subscribe/publish 回调广播）

本体 inbound 同为 asyncio.Queue（publish_inbound / get_inbound），outbound 走
回调订阅（subscribe_outbound）；学习版照此实现，另加一个 processed Event 作为
演示端的握手信号（见 README 差异）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """从 IM 渠道进来、奔向 agent 调度器的消息。"""

    channel_name: str        # "fakeim" / "wechat" / ...
    chat_id: str             # IM 侧会话 ID（群 / 私聊）
    user_id: str             # 发送人（群聊里区分成员）
    text: str
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)


@dataclass
class OutboundMessage:
    """从 agent 调度器回到渠道的回复。"""

    channel_name: str
    chat_id: str
    thread_id: str
    text: str
    is_final: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


OutboundListener = Callable[[OutboundMessage], Awaitable[None]]


class MessageBus:
    """inbound 队列 + outbound 订阅广播：渠道不认识 agent，agent 不认识渠道。"""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        # "已处理完"信号：manager 每消化完一条就 set，演示/测试端 await 它等回执
        self.processed = asyncio.Event()
        self._listeners: list[OutboundListener] = []

    def subscribe_outbound(self, listener: OutboundListener) -> None:
        self._listeners.append(listener)

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        for listener in self._listeners:
            await listener(msg)
