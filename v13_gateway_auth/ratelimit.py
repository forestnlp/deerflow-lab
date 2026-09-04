"""ratelimit —— per-thread 令牌桶限流。

参考本体：本体网关在 routers/auth.py、routers/channel_connections.py 等处
        以 429 + 明确原因做局部限流；DeerFlow 的 run 准入另有
        "同线程单活跃 run" 约束（v11 已见）。令牌桶为学习版自拟的标准算法。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class TokenBucket:
    """容量 capacity，每秒回填 refill_rate 个令牌。take() 拿不到就吃 429。"""

    capacity: float
    refill_rate: float
    tokens: float
    updated_at: float

    def take(self, now: float) -> bool:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.refill_rate)
        self.updated_at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class PerThreadBucketRegistry:
    """按 key（thread_id）各自一个桶：一个会话刷屏不能饿死别的会话。"""

    def __init__(self, capacity: float, refill_rate: float) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._buckets: dict[str, TokenBucket] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self._capacity, self._refill_rate, self._capacity, now)
            self._buckets[key] = bucket
        return bucket.take(now)
