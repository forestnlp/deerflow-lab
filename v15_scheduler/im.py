"""FakeIM —— 最小假聊天软件：调度器把结果 publish 进群，received 落账供断言。

参考本体：backend/app/channels/（推送走渠道适配器；长文切分见 v14，此处回复短不再做）
"""

from __future__ import annotations


class FakeIM:
    def __init__(self) -> None:
        self.received: list[tuple[str, str]] = []    # (chat_id, text)

    def publish(self, chat_id: str, text: str) -> None:
        self.received.append((chat_id, text))
        print(f"  [IM     ] 群 {chat_id} 收到「{text[:40]}」")
