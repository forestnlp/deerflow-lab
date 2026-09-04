"""FakeIM —— 内存假聊天软件：演示完整回路用的"渠道"。

参考本体：backend/app/channels/wechat.py（渠道适配器：收消息进 bus、
        出站做 2000 字分片后逐条发送）——本版把两个方向都收进一个类。
"""

from __future__ import annotations

from message_bus import InboundMessage, MessageBus, OutboundMessage
from split import split_message


class FakeIM:
    """一条假微信：inbound 用固定 message_id 模拟"重推"，outbound 分片落账。"""

    def __init__(self, bus: MessageBus, chunk_limit: int = 800) -> None:
        self.bus = bus
        self.chunk_limit = chunk_limit
        self.received: list[tuple[str, str]] = []    # (chat_id, 单条消息)

    def subscribe(self) -> None:
        self.bus.subscribe_outbound(self._on_outbound)

    async def _on_outbound(self, msg: OutboundMessage) -> None:
        for chunk in split_message(msg.text, self.chunk_limit):   # 长文切分在渠道侧
            self.received.append((msg.chat_id, chunk))

    async def send(self, chat_id: str, text: str, *, message_id: str | None = None) -> None:
        """用户发消息。message_id 可指定——同 id 再发即模拟 IM 重复投递。"""
        msg = InboundMessage(channel_name="fakeim", chat_id=chat_id,
                             user_id="bob", text=text)
        if message_id:
            msg.message_id = message_id
        await self.bus.inbound.put(msg)
