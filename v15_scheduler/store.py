"""durable 任务表 —— SQLite 两张表：tasks（承诺）与 task_runs（兑现记录）。

参考本体：packages/harness/deerflow/persistence/scheduled_tasks/（Postgres/SQLAlchemy）
         + scheduled_task_runs/（每次执行一条，活跃 run 有唯一索引防重叠双开）
学习版换成裸 SQLite，字段取 MVP 最小子集。

为什么任务必须落盘？调度器的本质是"跨时间兑现承诺"——进程一重启内存全没，
承诺就全违约。持久化是调度系统的第一性要求（v12 的账本思想在此复用）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ScheduledTask:
    id: str
    title: str
    prompt: str                       # 到点让 agent 干什么
    schedule_type: str                # "cron" | "once"（本体 MVP 同款，不做 interval）
    spec: dict                        # {"cron": "0 * * * *"} 或 {"run_at": iso}
    context_mode: str                 # fresh_thread_per_run | reuse_thread
    chat_id: str                      # 结果推给哪个 IM 会话
    status: str = "enabled"           # enabled / paused / completed / failed
    next_run_at: str | None = None    # ISO 字符串；None = 不再运行

    def note(self) -> str:            # 一行摘要，供打印
        nxt = (self.next_run_at or "-")[11:16]
        return f"{self.title}(status={self.status} next={nxt})"


@dataclass
class TaskRunRow:
    task_id: str
    scheduled_for: str                # 为哪个到期槽位服务
    fired_at: str
    status: str                       # ok / failed / skipped-overlap / coalesced
    detail: str = ""


class TaskStore:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("""CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
            schedule_type TEXT NOT NULL, spec TEXT NOT NULL,
            context_mode TEXT NOT NULL, chat_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'enabled', next_run_at TEXT)""")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS task_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            scheduled_for TEXT NOT NULL, fired_at TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""")
        self._conn.commit()

    def save(self, task: ScheduledTask) -> None:
        d = asdict(task)
        d["spec"] = json.dumps(d["spec"], ensure_ascii=False)
        self._conn.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?)", (
            d["id"], d["title"], d["prompt"], d["schedule_type"], d["spec"],
            d["context_mode"], d["chat_id"], d["status"], d["next_run_at"]))
        self._conn.commit()

    def get(self, task_id: str) -> ScheduledTask | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        cols = [c[0] for c in self._conn.execute("SELECT * FROM tasks LIMIT 0").description]
        d = dict(zip(cols, row))
        d["spec"] = json.loads(d["spec"])
        return ScheduledTask(**d)

    def all(self) -> list[ScheduledTask]:
        return [t for t in (self.get(r[0]) for r in
                            self._conn.execute("SELECT id FROM tasks ORDER BY rowid")) if t]

    def append_run(self, r: TaskRunRow) -> None:
        self._conn.execute(
            "INSERT INTO task_runs(task_id,scheduled_for,fired_at,status,detail) VALUES (?,?,?,?,?)",
            (r.task_id, r.scheduled_for, r.fired_at, r.status, r.detail))
        self._conn.commit()

    def runs(self, task_id: str) -> list[tuple[str, str, str]]:
        """(scheduled_for, status, detail) 按时间序，供断言与打印。"""
        return [(r[0], r[1], r[2]) for r in self._conn.execute(
            "SELECT scheduled_for,status,detail FROM task_runs WHERE task_id=? ORDER BY id",
            (task_id,))]

    def close(self) -> None:
        self._conn.close()
