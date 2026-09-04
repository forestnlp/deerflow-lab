"""run 事件表 —— SQLite 记录每个 run 的每一步：事件、耗时、token 估算。
参考本体 packages/harness/deerflow/persistence/models/run_event.py（RunEventRow：
thread_id/run_id/event_type/category/content/event_metadata/seq，同款字段骨架）。

本体用 SQLAlchemy + 迁移 + 分页索引；学习版一张裸 SQLite 表讲清同一件事：
**run 的真相不在内存里，在事件表里**。进程死了、模型忘了，只要表在，
"这个 run 干了什么、花了多久、烧了多少钱"就随时查得出来。

两个刻意对齐本体的细节：
1. seq 单调递增且 (thread_id, seq) 唯一 —— 断线重连后按 seq 续传不丢不重；
2. event_metadata 存 JSON —— 版本演进不用改表结构。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    trace_id   TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    trace_id  TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    event_type TEXT NOT NULL,      -- run_start / model_call / tool_call / run_end ...
    category  TEXT NOT NULL,       -- llm / tool / lifecycle
    content   TEXT DEFAULT '',
    meta      TEXT DEFAULT '{}',   -- JSON：耗时、参数、错误等
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_run ON run_events(run_id, seq);
"""


class RunEventStore:
    """run 事件账本：写入 O(1)，查询靠 (run_id, seq) 索引。"""

    def __init__(self, db_path: Path):
        # 工具节点在并行线程里执行，中间件可能从工作线程写库：
        # check_same_thread=False + 自己上锁，是本机单写者场景的标准解法。
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._seq: dict[str, int] = {}

    # ---- 写 ----
    def start_run(self, run_id: str, trace_id: str, thread_id: str, prompt: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, trace_id, thread_id, prompt, started_at) VALUES (?,?,?,?,?)",
                (run_id, trace_id, thread_id, prompt, time.time()),
            )
            self._conn.commit()
        self.record(run_id, trace_id, "run_start", "lifecycle", prompt[:120])

    def record(self, run_id: str, trace_id: str, event_type: str, category: str,
               content: str = "", meta: dict | None = None) -> None:
        with self._lock:
            seq = self._seq.get(run_id, 0) + 1
            self._seq[run_id] = seq
            self._conn.execute(
                "INSERT INTO run_events (run_id, trace_id, seq, event_type, category, content, meta, ts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, trace_id, seq, event_type, category, content,
                 json.dumps(meta or {}, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def finish_run(self, run_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at=?, prompt_tokens=?, completion_tokens=? WHERE run_id=?",
                (time.time(), prompt_tokens, completion_tokens, run_id),
            )
            self._conn.commit()

    # ---- 读 ----
    def recent_runs(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, trace_id, thread_id, prompt, started_at, finished_at,"
                " prompt_tokens, completion_tokens FROM runs"
                " ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out = []
            for run_id, trace_id, thread_id, prompt, t0, t1, pt, ct in rows:
                events = self._conn.execute(
                    "SELECT seq, event_type, category, content, meta FROM run_events"
                    " WHERE run_id=? ORDER BY seq",
                    (run_id,),
                ).fetchall()
                out.append({
                    "run_id": run_id, "trace_id": trace_id, "thread_id": thread_id,
                    "prompt": prompt, "duration_s": round((t1 or time.time()) - t0, 3),
                    "prompt_tokens": pt, "completion_tokens": ct,
                    "events": [
                        {"seq": s, "event_type": et, "category": cat, "content": c, "meta": json.loads(m)}
                        for s, et, cat, c, m in events
                    ],
                })
        return out

    def close(self) -> None:
        self._conn.close()
