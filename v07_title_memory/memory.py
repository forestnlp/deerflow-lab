"""记忆存储与防抖抽取 —— 参考本体 packages/harness/deerflow/agents/memory/manager.py
（MemoryManager.add：对话入队、防抖合并、异步经 LLM 抽取事实；
 shutdown_flush：进程退出前把防抖缓冲里的尾巴冲干净）
与 agents/memory/backends/deermem/（事实落盘、注入上下文时的转义防护）。

memory.json 的形态（学习版自拟的精简结构，本体在 deermem 后端里更丰富）：
    {"facts": ["用户在经营分析岗", "偏好表格输出"], "updated_by": "fake"}

防抖（debounce）：多次对话到达时重置同一个定时器，安静 debounce_s 秒后
才真正抽取一次——用户连发五条消息，不该触发五次 LLM 抽取。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage


def load_facts(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("facts", []))
    except (json.JSONDecodeError, AttributeError):
        return []


def save_facts(path: Path, facts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"facts": facts}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)                        # 原子替换：断电也不留半截 JSON


class DebouncedExtractor:
    """防抖抽取器：对话入队 → 定时器归零后，用抽取模型合并出新的 facts。"""

    def __init__(self, path: Path, extractor_factory: Callable[[list[str], list[str]], Any],
                 debounce_s: float = 0.2) -> None:
        self._path = path
        self._extractor_factory = extractor_factory
        self._debounce_s = debounce_s
        self._lock = threading.Lock()
        self._pending_users: list[str] = []
        self._pending_ais: list[str] = []
        self._timer: threading.Timer | None = None
        self.last_extraction: dict[str, Any] | None = None   # main 打印用

    def enqueue(self, users: list[str], ais: list[str]) -> None:
        """after_agent 只入队 + 重置定时器——对话主流程一毫秒都不许多等。

        快照语义：after_agent 每次传的是该 thread 的**全量**对话，后到的
        快照必然包含先前的，覆盖即可——追加会把老消息数两遍。
        """
        with self._lock:
            self._pending_users = users
            self._pending_ais = ais
            if self._timer is not None:
                self._timer.cancel()          # 新对话到了，重新等
            self._timer = threading.Timer(self._debounce_s, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def flush(self, timeout_s: float = 5.0) -> None:
        """进程退出前清缓冲（本体 shutdown_flush 同语义）。"""
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        self._flush()

    def _flush(self) -> None:
        with self._lock:
            users, ais = self._pending_users, self._pending_ais
            self._pending_users, self._pending_ais = [], []
        if not users:
            return
        old = load_facts(self._path)
        model = self._extractor_factory(old, users)
        reply = model.invoke("extract")       # fake 忽略入参；在线模型收到抽取提示词
        new = [f.strip() for f in str(reply.content).split("\n") if f.strip()]
        merged = old + [f for f in new if f not in old]
        save_facts(self._path, merged)
        with self._lock:
            self.last_extraction = {"users": len(users), "facts": merged}
        print(f"  >> [Memory] 防抖到期，抽取 {len(users)} 轮对话 -> {len(merged)} 条事实")
