"""消息去重存储 —— 记录处理过的 message_id（跨重启有效）。

参考本体：backend/app/channels/dedupe_store.py（防渠道重推导致重复回复）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class DedupeStore:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("CREATE TABLE IF NOT EXISTS seen (message_id TEXT PRIMARY KEY)")
        self._conn.commit()

    def seen_before(self, message_id: str) -> bool:
        """查询并原子登记：第二次见到同一 id 返回 True。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO seen (message_id) VALUES (?)", (message_id,))
        self._conn.commit()
        return cur.rowcount == 0      # 插不进去 = 早就见过

    def close(self) -> None:
        self._conn.close()
