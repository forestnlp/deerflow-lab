"""ledger —— runs / threads 两张 SQLite 账本表 + 重启孤儿恢复。

参考本体：backend/packages/harness/deerflow/persistence/run/sql.py（runs 账本）
        backend/packages/harness/deerflow/persistence/thread_meta/sql.py（threads 账本）
        backend/packages/harness/deerflow/runtime/runs/manager.py::
        reconcile_orphaned_inflight_runs（重启后把卡在活跃态的 run 收尾）
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Ledger:
    """run/thread 的 durable 账本。本体用 SQLAlchemy+Postgres/SQLite，本版裸 sqlite3。

    账本与 checkpoint 是两码事：checkpoint 存"对话现场"（图的 state），
    账本存"服务事实"（谁起过 run、现在什么状态）。前者给 agent 用，后者给运维用。
    """

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_id  TEXT PRIMARY KEY,
                title      TEXT,
                created_at TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id     TEXT PRIMARY KEY,
                thread_id  TEXT NOT NULL,
                status     TEXT NOT NULL,
                error      TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        self._conn.commit()

    def touch_thread(self, thread_id: str, title: str = "") -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO threads (thread_id, title, created_at) VALUES (?, ?, ?)",
            (thread_id, title, _now_iso()),
        )
        self._conn.commit()

    def start_run(self, run_id: str, thread_id: str) -> None:
        """起 run 即落账 status='running'——先写账再执行，本体的可见性边界。"""
        self.touch_thread(thread_id)
        self._conn.execute(
            "INSERT INTO runs (run_id, thread_id, status, created_at, updated_at) VALUES (?, ?, 'running', ?, ?)",
            (run_id, thread_id, _now_iso(), _now_iso()),
        )
        self._conn.commit()

    def finish_run(self, run_id: str, status: str, *, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE run_id = ?",
            (status, error, _now_iso(), run_id),
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, thread_id: str) -> list[dict]:
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE thread_id = ? ORDER BY created_at", (thread_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def recover_orphans(self) -> int:
        """进程重启：账本里所有还停在 running 的 run 都归本进程"遗产"，统一收尾 error。

        本体判断依据是 lease 是否过期（多 worker 不能误杀别人的活 run）；
        本版单进程，重启后还在 running 的只可能是上一世的遗产。
        """
        cur = self._conn.execute(
            "UPDATE runs SET status = 'error', error = 'orphan_recovered: 进程重启前未收尾', updated_at = ?"
            " WHERE status = 'running'",
            (_now_iso(),),
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
