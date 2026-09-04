"""多渠道路由 —— 一条总线喂一个 agent，回复按渠道名回门。
参考本体 backend/app/channels/manager.py（ChannelManager：去重、
(channel, chat_id) -> thread 映射、组装出站回复）。

不串线的两条军规（和 v14 相同，只是现在渠道本身可插拔了）：
1. 入站消息自带 channel 字段 —— 回复永远发回"来时的渠道"，靠数据不靠运气；
2. (channel, chat_id) 联合键映射 thread —— fakeim 的 c1 和 webhook 的 c1
   是两个会话，绝不因为 chat_id 撞名而串会话。
"""

from __future__ import annotations

from typing import Awaitable, Callable

from channels import Bus, Outbound


class ChannelManager:
    def __init__(self, bus: Bus,
                 run_handler: Callable[[str, object], Awaitable[str]]):
        self.bus = bus
        self.run_handler = run_handler          # async (thread_id, inbound) -> 回复文本
        self._seen: set[str] = set()            # 去重（重推保护）
        self.threads: dict[str, str] = {}       # "channel:chat_id" -> thread_id
        self.routed: list[tuple[str, str]] = [] # 审计：每条回复回给了哪个渠道

    def thread_of(self, channel: str, chat_id: str) -> str:
        key = f"{channel}:{chat_id}"
        if key not in self.threads:
            self.threads[key] = f"thr-{key.replace(':', '-').lower()}"
        return self.threads[key]

    async def dispatch(self) -> None:
        """消费一条入站；收到 None 返回 False 结束循环。"""
        msg = await self.bus.inbound.get()
        if msg is None:
            return False
        if msg.message_id in self._seen:
            return True
        self._seen.add(msg.message_id)

        thread_id = self.thread_of(msg.channel, msg.chat_id)
        answer = await self.run_handler(thread_id, msg)
        await self.bus.publish_outbound(Outbound(msg.channel, msg.chat_id, answer))
        self.routed.append((msg.channel, thread_id))
        return True

    async def run_forever(self) -> None:
        while await self.dispatch():
            pass
