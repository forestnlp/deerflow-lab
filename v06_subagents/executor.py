"""子代理执行器 —— 参考本体 packages/harness/deerflow/subagents/executor.py
（SubagentStatus / SubagentResult.try_set_terminal / execute_async / 后台线程 + 超时）
与 tools/builtins/task_tool.py（轮询到终态、事件回报、终态清理）。

三件事与本体同构：
1. 后台线程执行：结果写在 SubagentResult 里，主循环只轮询；
2. 事件回报：每个任务一条事件流水（running → completed/failed/timed_out）；
3. 并发闸：BoundedSemaphore 卡住同时运行的子代理数（本体 MAX_CONCURRENT_SUBAGENTS=3）。

学习版差异：事件不进 SSE 而是攒在结果对象里由 main.py 打印；
超时从"几百秒"缩成"几秒"；trace/鉴权字段全部砍掉。
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# ---------------------------------------------------------------- 并发闸
# 本体是常量 + 线程池 max_workers 双保险；学习版一把信号量足够看清语义。
MAX_CONCURRENT_SUBAGENTS = 2
_gate = threading.BoundedSemaphore(MAX_CONCURRENT_SUBAGENTS)

_running = 0                  # 当前同时运行的子代理数
_peak = 0                     # 本次进程观测到的峰值并发
_queued = 0                   # 因名额已满而排队的事件数
_state_lock = threading.Lock()


def _enter_gate() -> None:
    global _running, _peak
    with _state_lock:
        _running += 1
        _peak = max(_peak, _running)


def _exit_gate() -> None:
    global _running
    with _state_lock:
        _running -= 1


def peak_concurrency() -> int:
    with _state_lock:
        return _peak


def queue_waits() -> int:
    with _state_lock:
        return _queued


def reset_stats() -> None:
    global _running, _peak, _queued
    with _state_lock:
        _running = _peak = _queued = 0


# ---------------------------------------------------------------- 状态与结果
class SubagentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.TIMED_OUT}


@dataclass
class SubagentResult:
    task_id: str
    subagent: str
    status: SubagentStatus = SubagentStatus.PENDING
    result: str | None = None
    error: str | None = None
    steps: int = 0
    events: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, line: str) -> None:
        """事件写进本任务自己的流水（线程安全），main 按消息顺序打印——确定性。"""
        with self._lock:
            self.events.append(line)

    def try_set_terminal(self, status: SubagentStatus, *, result: str | None = None, error: str | None = None) -> bool:
        """终态只认第一次写入：后台超时和执行线程会抢同一个 holder。"""
        with self._lock:
            if self.status.is_terminal:
                return False
            self.status = status
            if result is not None:
                self.result = result
            if error is not None:
                self.error = error
            return True


_background: dict[str, SubagentResult] = {}
_bg_lock = threading.Lock()

_scheduler = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sub-sched-")


def get_result(task_id: str) -> SubagentResult | None:
    with _bg_lock:
        return _background.get(task_id)


def cleanup(task_id: str) -> None:
    """终态才删——本体同款：删早了执行线程还在写一个已经不存在的条目。"""
    with _bg_lock:
        r = _background.get(task_id)
        if r is not None and r.status.is_terminal:
            del _background[task_id]


def reset_background() -> None:
    with _bg_lock:
        _background.clear()


# ---------------------------------------------------------------- 执行器
class SubagentExecutor:
    """把 SubagentConfig 装配成独立 agent，在后台线程跑一个任务。"""

    def __init__(self, cfg, model_factory, timeout_seconds: float = 20.0):
        self.cfg = cfg
        self._model_factory = model_factory     # (模型名, 任务, 每步延迟ms) → ChatModel
        self.timeout_seconds = timeout_seconds

    def _run(self, task: str, holder: SubagentResult) -> None:
        from subagents import resolve_tools

        # 同一个 create_agent，换受限模型 + 受限工具集——这就是"子 agent"的全部魔法
        agent = create_agent(
            model=self._model_factory(self.cfg.model, task, self.cfg.latency_ms),
            tools=resolve_tools(self.cfg),
        )
        state = agent.invoke({"messages": [
            SystemMessage(content=self.cfg.system_prompt),
            HumanMessage(content=task),
        ]})
        ai_msgs = [m for m in state["messages"] if isinstance(m, AIMessage)]
        holder.steps = len(ai_msgs)
        for i, m in enumerate(ai_msgs, 1):
            if m.tool_calls:
                calls = ", ".join(f"{tc['name']}({tc['args']})" for tc in m.tool_calls)
                holder.emit(f"第{i}轮 调工具: {calls}")
            else:
                holder.emit(f"第{i}轮 交终稿: {m.content}")
        text = (ai_msgs[-1].content if ai_msgs else "") or ""
        if text.strip():
            holder.try_set_terminal(SubagentStatus.COMPLETED, result=text.strip())
        else:
            holder.try_set_terminal(SubagentStatus.FAILED, error="子代理没有产出终稿")

    def execute_async(self, task: str, task_id: str | None = None) -> str:
        task_id = task_id or uuid.uuid4().hex[:8]
        holder = SubagentResult(task_id=task_id, subagent=self.cfg.name)
        with _bg_lock:
            _background[task_id] = holder

        def worker() -> None:
            # 并发闸：先试拿名额，拿不到就记一次排队、再老实等位。
            # （排队事件记在全局计数里而不是任务流水里，输出才不受线程抢跑顺序影响。）
            if not _gate.acquire(blocking=False):
                global _queued
                with _state_lock:
                    _queued += 1
                _gate.acquire()
            _enter_gate()
            try:
                holder.status = SubagentStatus.RUNNING
                holder.emit(f"起跑 model={self.cfg.model}")
                self._run(task, holder)
            except Exception as exc:       # noqa: BLE001 - 后台线程兜底，异常不许吞
                holder.try_set_terminal(SubagentStatus.FAILED, error=str(exc))
            finally:
                _exit_gate()
                _gate.release()

        _scheduler.submit(worker)
        return task_id


def wait_terminal(task_id: str, poll_s: float = 0.01, deadline_s: float | None = None) -> SubagentResult | None:
    """轮询到终态（本体每 5 秒轮一次并带超时预算；学习版毫秒级，语义相同）。"""
    budget = deadline_s if deadline_s is not None else 20.0
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        r = get_result(task_id)
        if r is not None and r.status.is_terminal:
            return r
        time.sleep(poll_s)
    r = get_result(task_id)
    if r is not None:
        r.try_set_terminal(SubagentStatus.TIMED_OUT, error=f"{budget:.0f}s 内未达终态")
    return r
