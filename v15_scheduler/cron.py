"""手写 5 段 cron —— "分 时 日 月 周"，支持 * n a-b a,b */n a-b/n。

参考本体：packages/harness/deerflow/scheduler/schedules.py
         （normalize_cron_expression 只校验"恰好 5 段"，取下一次交给 croniter 库）
学习版自己实现 next_after，两个经典坑一个都不放过：
  1. 周字段 0=周日（cron 传统），而 Python weekday() 是 0=周一 —— 必须换算；
  2. "日"与"周"同时受限时是 OR 语义：`0 9 1 * 5` = 每月 1 号 **或** 每周五。
"""

from __future__ import annotations

from datetime import datetime, timedelta

SCAN_DAYS = 800          # 逐日扫描窗口：再晚的下一班就当作"排不出来"


class CronSchedule:
    def __init__(self, expression: str) -> None:
        parts = [p for p in expression.split() if p]
        if len(parts) != 5:                       # 与本体同款校验
            raise ValueError(f"cron 必须恰好 5 段，收到 {len(parts)} 段: {expression!r}")
        self.expression = " ".join(parts)
        self.minutes = self._field(parts[0], 0, 59)
        self.hours = self._field(parts[1], 0, 23)
        self.days = self._field(parts[2], 1, 31)
        self.months = self._field(parts[3], 1, 12)
        self.weekdays = self._field(parts[4], 0, 6)
        # 日/周 OR 语义要知道"是否受过限制"，* 与 */n 都算不设限
        self._dom_restricted = parts[2] not in ("*",) and not parts[2].startswith("*/")
        self._dow_restricted = parts[4] not in ("*",) and not parts[4].startswith("*/")

    @staticmethod
    def _field(spec: str, lo: int, hi: int) -> set[int]:
        """一个字段 → 命中值集合。全部魔法在此。"""
        values: set[int] = set()
        for part in spec.split(","):
            step = 1
            if "/" in part:
                part, step_s = part.split("/")
                step = int(step_s)
            if part == "*":
                start, end = lo, hi
            elif "-" in part:
                a, b = part.split("-")
                start, end = int(a), int(b)
            else:
                start = end = int(part)
            if not (lo <= start <= end <= hi) or step < 1:
                raise ValueError(f"字段越界/步长非法: {spec!r}（允许 {lo}..{hi}）")
            values.update(range(start, end + 1, step))
        return values

    def _date_ok(self, dt: datetime) -> bool:
        dow = (dt.weekday() + 1) % 7          # Python 周一=0 → cron 周日=0
        if self._dom_restricted and self._dow_restricted:
            return dt.month in self.months and (dt.day in self.days or dow in self.weekdays)
        return (dt.month in self.months and dt.day in self.days and dow in self.weekdays)

    def matches(self, dt: datetime) -> bool:
        return self._date_ok(dt) and dt.minute in self.minutes and dt.hour in self.hours

    def next_after(self, after: datetime) -> datetime | None:
        """after 之后第一个命中时刻：先按天粗筛，命中日再逐分钟精筛。

        croniter 会聪明地"跳到下一个候选值"，逐分钟扫描只在 800 天窗口内够用——
        说清楚边界，比假装完美重要。
        """
        start = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        day = start.replace(hour=0, minute=0)
        for _ in range(SCAN_DAYS):
            if self._date_ok(day):
                for m in range(24 * 60):
                    cand = day + timedelta(minutes=m)
                    if cand > after and self.matches(cand):
                        return cand
            day += timedelta(days=1)
        return None
