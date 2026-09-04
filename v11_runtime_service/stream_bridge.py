"""MemoryStreamBridge —— 生产者/消费者解耦的进程内事件日志。

参考本体：backend/packages/harness/deerflow/runtime/stream_bridge/base.py
        backend/packages/harness/deerflow/runtime/stream_bridge/memory.py
        （StreamEvent / END_SENTINEL / HEARTBEAT_SENTINEL / Last-Event-ID 重放）
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    id: str      # SSE 帧的 id: 字段，供 Last-Event-ID 断线续传
    event: str   # SSE 事件名：metadata / values / error / end
    data: Any    # JSON 可序列化负载


END_SENTINEL = StreamEvent(id="", event="__end__", data=None)
HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)


@dataclass
class _RunStream:
    events: list[StreamEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    start_offset: int = 0  # 事件被裁剪后，events[0] 对应的绝对序号


class MemoryStreamBridge:
    """每个 run 一条内存事件日志；publish 永不阻塞生产者，subscribe 各自带游标。

    与本体一致的关键机制：
    - 事件 id 是 run 内单调递增的序号，且序号 == 绝对偏移，重连定位 O(1)
      （本体 id 形如 {ts}-{seq}，本版去掉时间戳只用 seq，见 README 差异声明）；
    - 超过 maxsize 的旧事件被裁剪，start_offset 前移（本体会发 StreamGap，
      本版退化为"从最早保留的事件重放"）；
    - 消费者空等超过 heartbeat_interval 秒时吐出 HEARTBEAT_SENTINEL。
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._streams: dict[str, _RunStream] = {}
        self._counters: dict[str, int] = {}

    def _get_or_create(self, run_id: str) -> _RunStream:
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
            self._counters[run_id] = 0
        return self._streams[run_id]

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        stream = self._get_or_create(run_id)
        self._counters[run_id] += 1
        entry = StreamEvent(id=str(self._counters[run_id] - 1), event=event, data=data)
        async with stream.condition:
            stream.events.append(entry)
            if len(stream.events) > self._maxsize:
                overflow = len(stream.events) - self._maxsize
                del stream.events[:overflow]
                stream.start_offset += overflow
            stream.condition.notify_all()

    async def publish_end(self, run_id: str) -> None:
        stream = self._get_or_create(run_id)
        async with stream.condition:
            stream.ended = True
            stream.condition.notify_all()

    def _resolve_offset(self, stream: _RunStream, last_event_id: str | None) -> int:
        """把 Last-Event-ID 换成"从哪个绝对偏移继续读"。"""
        if last_event_id is None:
            return stream.start_offset
        try:
            seq = int(last_event_id)
        except ValueError:
            return stream.start_offset
        if seq < stream.start_offset:  # 客户端要的事件已被裁剪，保守从最早重放
            return stream.start_offset
        return seq + 1

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        stream = self._get_or_create(run_id)
        async with stream.condition:
            next_offset = self._resolve_offset(stream, last_event_id)
        while True:
            async with stream.condition:
                if next_offset < stream.start_offset:  # 消费太慢，事件被裁剪：跳到最早保留的
                    next_offset = stream.start_offset
                if next_offset - stream.start_offset < len(stream.events):
                    entry = stream.events[next_offset - stream.start_offset]
                    next_offset += 1
                elif stream.ended:
                    entry = END_SENTINEL
                else:
                    try:
                        await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                    except TimeoutError:
                        entry = HEARTBEAT_SENTINEL
                    else:
                        continue
            if entry is END_SENTINEL:
                yield END_SENTINEL
                return
            yield entry

    async def cleanup(self, run_id: str) -> None:
        self._streams.pop(run_id, None)
        self._counters.pop(run_id, None)
