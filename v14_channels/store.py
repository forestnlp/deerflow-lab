"""chat→thread 映射持久化 —— (channel, chat_id) 稳定映射到一个 DeerFlow thread。

参考本体：backend/app/channels/store.py 与 manager.py 中的会话映射
        （微信群固定一个 chat_id ⇒ 群成员共享同一条 DeerFlow 线程）
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path


class ThreadMappingStore:
    """JSON 文件版映射表（本体同名机制，Postgres 换 SQLite/JSON 同理）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, str] = {}
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _key(channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    def get_or_create(self, channel: str, chat_id: str) -> str:
        key = self._key(channel, chat_id)
        if key not in self._data:
            self._data[key] = f"thread-{uuid.uuid4().hex[:8]}"
            self._save()
        return self._data[key]

    def reset(self, channel: str, chat_id: str) -> str:
        """/new 的实现：换一个新 thread_id 落盘，旧现场留在 checkpoint 里不删。"""
        key = self._key(channel, chat_id)
        self._data[key] = f"thread-{uuid.uuid4().hex[:8]}"
        self._save()
        return self._data[key]

    def all(self) -> dict[str, str]:
        return dict(self._data)

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
