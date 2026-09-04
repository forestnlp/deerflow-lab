"""RunManager —— run 生命周期状态机（queued -> running -> success/error）。

参考本体：backend/packages/harness/deerflow/runtime/runs/manager.py
        （RunRecord / RunStatus / create_or_reject 的 reject 语义）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class RunStatus(StrEnum):
    """本体是 pending/running/success/error/interrupted/timeout，学习版留四态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


ACTIVE_STATUSES = {RunStatus.QUEUED, RunStatus.RUNNING}


class ConflictError(Exception):
    """同线程已有活跃 run —— 本体 create_or_reject 的 reject 语义。"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class RunRecord:
    run_id: str
    thread_id: str
    status: RunStatus = RunStatus.QUEUED
    error: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


class RunManager:
    """内存版 run 登记表：起 run、查状态、状态流转。

    本体用 asyncio.Lock 保护并发写，并可选挂一个持久化 RunStore；
    本版单线程演示，两者都省（见 README【与本体差异】）。
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}

    def create_or_reject(self, thread_id: str) -> RunRecord:
        """为 thread_id 起一个新 run；同线程已有 queued/running 的 run 则拒绝。"""
        inflight = [
            r
            for r in self._runs.values()
            if r.thread_id == thread_id and r.status in ACTIVE_STATUSES
        ]
        if inflight:
            raise ConflictError(f"线程 {thread_id} 已有活跃 run（{inflight[0].run_id}）")
        record = RunRecord(run_id=uuid.uuid4().hex[:8], thread_id=thread_id)
        self._runs[record.run_id] = record
        return record

    def set_status(self, run_id: str, status: RunStatus, *, error: str | None = None) -> None:
        record = self._runs[run_id]
        record.status = status
        record.updated_at = _now_iso()
        if error is not None:
            record.error = error

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)
