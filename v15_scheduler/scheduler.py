"""SchedulerService —— tick 轮询、到期认领、重叠 skip、misfire 合并。

参考本体：app/scheduler/service.py（ScheduledTaskService）
         overlap_policy 在本体 MVP 固定为 skip（"fixed to `skip` in MVP"）：
         上一轮还有活跃 run 就把本轮记成 skipped 墓碑，绝不排队堆积；
         packages/harness/deerflow/scheduler/schedules.py（next_run_at）：
         once 过期返回 None —— 学习版把"错过 N 个槽位"合并成"只补最近一次"。

两条本体铁律，本版原样继承：
  ① scheduler decides *when*, run services decide *how* ——
     到点触发就是起一个普通 run（start_run 注入），绝不另开执行旁路；
  ② context_mode 二选一：fresh_thread_per_run（日报类，不无限累积上下文）
     或 reuse_thread（提醒类，要延续记忆）。
时间一律显式传 now：整个类不读系统时钟，"每天 18:30"不用真等 18:30 就能测。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from cron import CronSchedule
from store import ScheduledTask, TaskRunRow, TaskStore

SKIP_DETAIL = "skipped: 上一轮仍在执行（overlap_policy=skip）"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


class SchedulerService:
    """start_run(task, thread_id, slot) -> 回复文本；publish(chat_id, text) 推渠道。"""

    def __init__(self, store: TaskStore, start_run: Callable, publish: Callable,
                 has_active_runs: Callable[[str], bool]) -> None:
        self.store = store
        self.start_run = start_run
        self.publish = publish
        self.has_active_runs = has_active_runs

    # ---- 管理面（本体 CRUD 的最小子集）----
    def create(self, title: str, prompt: str, schedule_type: str, spec: dict, *,
               context_mode: str = "fresh_thread_per_run", chat_id: str = "ops-group",
               now: datetime) -> ScheduledTask:
        task = ScheduledTask(id=uuid.uuid4().hex[:8], title=title, prompt=prompt,
                             schedule_type=schedule_type, spec=spec,
                             context_mode=context_mode, chat_id=chat_id)
        nxt = self._next_time(task, now)
        if nxt is None:      # once 且 run_at 已过期：这一辈子都不会执行了
            task.status, task.next_run_at = "failed", None
            self.store.append_run(TaskRunRow(task.id, "-", _iso(now), "failed",
                                             "run_at 已过期，从未执行"))
        else:
            task.next_run_at = _iso(nxt)
        self.store.save(task)
        return task

    def pause(self, task_id: str) -> None:
        t = self._need(task_id)
        t.status = "paused"
        self.store.save(t)

    # ---- 运行面 ----
    def tick(self, now: datetime) -> int:
        """轮询一遍任务，兑现到期承诺。本体是后台协程每 poll_interval 秒一次。"""
        fired = 0
        for task in self.store.all():
            if task.status != "enabled" or not task.next_run_at:
                continue
            due = datetime.fromisoformat(task.next_run_at)
            if due > now:
                continue
            self._settle(task, due, now)
            fired += 1
        return fired

    def _settle(self, task: ScheduledTask, due: datetime, now: datetime) -> None:
        slot, missed = due, 0
        if task.schedule_type == "cron":           # misfire 合并：只补最近一次
            cur = CronSchedule(task.spec["cron"]).next_after(due)
            while cur is not None and cur <= now:
                slot, missed = cur, missed + 1
                cur = CronSchedule(task.spec["cron"]).next_after(cur)
            if missed:
                self.store.append_run(TaskRunRow(
                    task.id, _iso(due), _iso(now), "coalesced",
                    f"错过 {missed} 个槽位，只补最近一次({slot:%H:%M})"))
                print(f"  [misfire] {task.title}: 停机期间错过 {missed} 班，"
                      f"合并补跑最近一次 {slot:%H:%M}")
        if self.has_active_runs(task.id):          # 重叠 skip（本体 MVP 固定策略）
            self.store.append_run(TaskRunRow(task.id, _iso(slot), _iso(now),
                                             "skipped-overlap", SKIP_DETAIL))
            print(f"  [skip   ] {task.title}: 上一轮仍在执行 -> 本轮记 skipped，不排队")
            self._reschedule(task, now, executed=False)
            return
        thread_id = (f"thread-task-{task.id}" if task.context_mode == "reuse_thread"
                     else f"thread-task-{task.id}-{slot:%H%M}")
        print(f"  [触发   ] {task.title} slot={slot:%H:%M} thread={thread_id} "
              f"({task.context_mode})")
        try:
            reply = self.start_run(task, thread_id, slot)    # 铁律①：普通 run 路径
            status, detail = "ok", reply[:60]
        except Exception as exc:                  # noqa: BLE001  调度器不能被一个任务炸死
            status, detail = "failed", str(exc)[:80]
        if status == "ok":
            self.publish(task.chat_id, f"[{task.title}] {reply}")
        self.store.append_run(TaskRunRow(task.id, _iso(slot), _iso(now), status, detail))
        self._reschedule(task, now)

    def _reschedule(self, task: ScheduledTask, now: datetime, executed: bool = True) -> None:
        if task.schedule_type == "cron":
            nxt = self._next_time(task, now)
            task.next_run_at = _iso(nxt) if nxt else None
        elif executed:
            task.next_run_at, task.status = None, "completed"   # once 真跑过：完结
        else:
            # once 被 skip：唯一那次执行丢了。本体 _task_status_for_skip 同款语义：
            # 标 failed 而非 completed——"completed 会谎称发生过一次执行"
            task.next_run_at, task.status = None, "failed"
        self.store.save(task)

    @staticmethod
    def _next_time(task: ScheduledTask, now: datetime) -> datetime | None:
        if task.schedule_type == "cron":
            return CronSchedule(task.spec["cron"]).next_after(now)
        if task.schedule_type == "once":
            run_at = datetime.fromisoformat(task.spec["run_at"])
            return run_at if run_at > now else None   # 本体语义：过期即 None，不补跑
        raise ValueError(f"不支持的 schedule_type: {task.schedule_type}")

    def _need(self, task_id: str) -> ScheduledTask:
        t = self.store.get(task_id)
        if t is None:
            raise KeyError(f"任务不存在: {task_id}")
        return t
