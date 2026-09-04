"""渠道抽象层 —— BaseChannel 三件套：connect / push / on_message。
参考本体 backend/app/channels/base.py（Channel ABC：start/stop/send +
bus 收发）与 manager.py（ChannelManager 多渠道路由，约 L898 起）。

本体 Channel 的三件事与本版三件套的对应：
    本体 start()        -> 学习版 connect()      建立与外部平台的连接
    本体 send(msg)      -> 学习版 push(msg)      出站：把回复送回平台
    本体 _make_inbound + bus.publish -> on_message
        学习版把"收到消息后往总线投"的动作做成基类方法，子类只需
        _read_next() 提供下一条原始消息（或测试直接 inject）。

为什么抽象层值得单独一版？v14 的教训：渠道机制（去重/映射/命令）只写了一遍，
但它长在 wechat 的壳里。加第二个渠道就得复制粘贴。抽象出 BaseChannel 后，
新渠道 = 三件套 + 注册，一行业务路由代码都不用碰。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
import uuid


@dataclass
class Inbound:
    channel: str
    chat_id: str
    sender: str
    text: str
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Outbound:
    channel: str
    chat_id: str
    text: str


class Bus:
    """inbound 队列 + outbound 按渠道名分发（本体 message_bus.py 的骨架）。"""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[Inbound | None] = asyncio.Queue()
        self._channels: dict[str, "BaseChannel"] = {}

    def register(self, ch: "BaseChannel") -> None:
        self._channels[ch.name] = ch

    async def publish_outbound(self, msg: Outbound) -> None:
        ch = self._channels.get(msg.channel)
        if ch is None:
            raise KeyError(f"出站消息找不到渠道: {msg.channel}")
        await ch.push(msg)


class BaseChannel(ABC):
    """三件套抽象：connect 建联、push 出站、on_message 入站（投总线）。"""

    name: str = "base"

    def __init__(self, bus: Bus, config: dict[str, Any] | None = None):
        self.bus = bus
        self.config = config or {}
        self.connected = False
        bus.register(self)

    # -- 三件套：子类必须会答的三道题 ------------------------------------
    @abstractmethod
    async def connect(self) -> None:
        """与外部平台建联（登录、起长轮询、验证 webhook 地址……）。"""

    @abstractmethod
    async def push(self, msg: Outbound) -> None:
        """出站：把 agent 的回复送回平台。"""

    async def on_message(self, inbound: Inbound) -> None:
        """入站：默认动作是投总线。子类一般不用改——这正是抽象的意义。"""
        await self.bus.inbound.put(inbound)

    # -- 连通性自检（对应本体渠道连接检查语义；学习版自拟三段式，见 README） ----
    async def self_test(self) -> tuple[bool, str]:
        """connect -> push 探针 -> 报告。渠道实现者可覆写做更深的探测。"""
        try:
            if not self.connected:
                await self.connect()
            await self.push(Outbound(self.name, "__probe__", "__ping__"))
            sent = self.probe_sent()
            return (True, f"connect 成功，push 探针{'已送达' if sent else '未送达'}")
        except Exception as exc:  # noqa: BLE001 —— 自检的目的就是把异常变成结论
            return (False, f"失败: {exc}")

    def probe_sent(self) -> bool:
        """自检探针是否被 push 处理过；渠道子类用各自的证据回答。"""
        return self.connected


class FakeIM(BaseChannel):
    """假 IM：脚本化的"群消息"逐条到达；push 记入 delivery_log 供断言。"""

    name = "fakeim"

    def __init__(self, bus: Bus, config: dict[str, Any] | None = None):
        super().__init__(bus, config)
        self.script: list[Inbound] = list(config.get("script", []))
        self.delivery_log: list[Outbound] = []

    async def connect(self) -> None:
        self.connected = True

    async def push(self, msg: Outbound) -> None:
        self.delivery_log.append(msg)

    def probe_sent(self) -> bool:
        return any(m.chat_id == "__probe__" for m in self.delivery_log)

    async def run(self) -> None:
        """模拟长轮询：把剧本消息一条条 on_message 进总线。"""
        for msg in self.script:
            await self.on_message(msg)
            await asyncio.sleep(0)


class ConsoleChannel(BaseChannel):
    """控制台渠道：push 打印到终端；入站由测试 inject（真 CLI 用 input()）。"""

    name = "console"

    def __init__(self, bus: Bus, config: dict[str, Any] | None = None):
        super().__init__(bus, config)
        self.pushed: list[Outbound] = []

    async def connect(self) -> None:
        self.connected = True

    async def push(self, msg: Outbound) -> None:
        self.pushed.append(msg)
        if msg.chat_id != "__probe__":
            print(f"  << [console->屏幕] {msg.text}")

    def probe_sent(self) -> bool:
        return any(m.chat_id == "__probe__" for m in self.pushed)


class WebhookChannel(BaseChannel):
    """Webhook 渠道（内存假 HTTP）：入站 = handle_http_post()；
    出站 = 向 config['endpoint'] 发 POST —— 这里的"HTTP 客户端"是内存队列，
    接口形状与真实 httpx.post 一致，换成真客户端只改 push 一行。"""

    name = "webhook"

    def __init__(self, bus: Bus, config: dict[str, Any] | None = None):
        super().__init__(bus, config)
        self.token = config.get("token", "")
        self.endpoint: str = config.get("endpoint", "")
        self.outbox: list[dict] = []       # 假 HTTP：所有 POST 落在这

    async def connect(self) -> None:
        if not self.token:
            raise RuntimeError("webhook 渠道缺 token，无法验证回调签名")
        self.connected = True

    async def handle_http_post(self, payload: dict) -> None:
        """假 HTTP 入口（真实场景是 FastAPI 路由收到 POST 后调这里）。"""
        if not self.connected:
            raise RuntimeError("webhook 未 connect 就收到了 POST")
        await self.on_message(Inbound(self.name, payload["chat_id"],
                                      payload.get("user", "anon"), payload["text"]))

    async def push(self, msg: Outbound) -> None:
        # 真实版：await httpx.post(self.endpoint, json=..., headers=auth)
        self.outbox.append({"endpoint": self.endpoint, "chat_id": msg.chat_id,
                            "text": msg.text})
