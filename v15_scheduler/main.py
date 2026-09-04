"""v15 · 闹钟 —— 手写 cron + durable 任务表 + 重叠 skip + misfire 合并（1 秒 tick）。

运行：
    conda run -n deerflow_lab python v15_scheduler/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake（真实时钟长驻循环）

--fake 全自动演示（虚拟时钟：真睡 1 秒 = 虚拟过 30 分钟，调度器从不读系统表）：
    建 3 个任务（小时报 / 工作日 standup / 一次性清理）
    → "停机 2.5 小时"醒来：错过的整点只补最近一次（misfire 合并）
    → 上一轮还在跑：本轮记 skipped-overlap，绝不排队（重叠 skip）
    → 到点触发 = 起普通 run（fake 模型 + SqliteSaver）+ 渠道推送
    → 重开任务表断言持久化，全链路断言通过退出码 0。
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver

from cron import CronSchedule
from im import FakeIM
from model_factory import fake_model, get_model
from scheduler import SchedulerService
from store import TaskStore

DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def cron_preflight() -> None:
    """先拿三条铁律喂断言，解析器不可信则一切免谈。"""
    # 周五 19:00 之后，"工作日 18:30"的下一班是下周一
    assert CronSchedule("30 18 * * 1-5").next_after(
        datetime(2026, 9, 4, 19, 0)) == datetime(2026, 9, 7, 18, 30)
    # 日/周 OR 语义：9 月 4 日是周五（不是 1 号）也命中 "0 9 1 * 5"
    assert CronSchedule("0 9 1 * 5").matches(datetime(2026, 9, 4, 9, 0))
    # 工作时段 9-17 每 15 分钟：周五 17:50 的下一班是周一 09:00
    assert CronSchedule("*/15 9-17 * * 1-5").next_after(
        datetime(2026, 9, 4, 17, 50)) == datetime(2026, 9, 7, 9, 0)
    try:
        CronSchedule("30 18 * *")
        raise AssertionError("4 段应当报错")
    except ValueError:
        pass
    print("[cron  ] 断言 ×4 通过：周末跳班 / 日周 OR / 时段步长 / 5 段校验")


def make_start_run(saver, clock, release, runs_seen, fake: bool):
    """到点执行 = 起一个普通 run（本体铁律①：调度只决定"何时"）。"""
    def start_run(task, thread_id: str, slot: datetime) -> str:
        runs_seen.setdefault(task.id, []).append(thread_id)
        if "小时报" in task.title:      # 伪造慢 run：占到 13:10，制造下一班重叠
            release[task.id] = clock["vnow"] + timedelta(minutes=35)
            print(f"  [run    ] {task.title}: 本轮很慢，活跃到 {release[task.id]:%H:%M}")
        model = fake_model([f"{task.title}完成：{task.prompt}"]) if fake else get_model()
        agent = create_agent(model=model, tools=[], checkpointer=saver)
        res = agent.invoke({"messages": [{"role": "user", "content": task.prompt}]},
                           config={"configurable": {"thread_id": thread_id}})
        return res["messages"][-1].content
    return start_run


def demo_fake() -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)   # 启动时清空，保证可重复运行
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cron_preflight()

    clock = {"vnow": datetime(2026, 9, 4, 9, 5)}  # 周五 09:05，虚拟时钟
    release: dict[str, datetime] = {}             # task_id -> 活跃截止时间
    runs_seen: dict[str, list[str]] = {}
    im = FakeIM()

    def has_active_runs(task_id: str) -> bool:    # 本体同款 has_active_runs 查询
        until = release.get(task_id)
        if until and clock["vnow"] >= until:
            del release[task_id]
        return task_id in release

    store = TaskStore(DATA_DIR / "tasks.db")
    with SqliteSaver.from_conn_string(str(DATA_DIR / "checkpoints.db")) as saver:
        sched = SchedulerService(store, make_start_run(saver, clock, release, runs_seen, True),
                                 im.publish, has_active_runs)
        t1 = sched.create("小时报", "汇总上一小时指标", "cron", {"cron": "0 * * * *"},
                          now=clock["vnow"])
        t2 = sched.create("standup提醒", "催团队站会", "cron", {"cron": "30 9 * * 1-5"},
                          context_mode="reuse_thread", now=clock["vnow"])
        t3 = sched.create("一次性清理", "清临时文件", "once",
                          {"run_at": "2026-09-04T10:10"}, now=clock["vnow"])
        for t in (t1, t2, t3):
            print(f"[建任务] {t.note()}")

        # 真睡 1 秒 = 虚拟过 30 分钟；10:05 那一班在"停机"中错过
        ticks = ["2026-09-04T12:35", "2026-09-04T13:05",
                 "2026-09-04T13:35", "2026-09-04T14:05"]
        labels = ["② 停机 2.5 小时后醒来", "③ 上一轮仍活跃 -> skip",
                  "④ 空 tick（没有到期）", "⑤ 释放后下一班照常"]
        for label, iso in zip(labels, ticks):
            clock["vnow"] = datetime.fromisoformat(iso)
            print(f"── {label}：tick @ {clock['vnow']:%H:%M} ──")
            time.sleep(1.0)                       # 1 秒 tick：真实时钟在走
            sched.tick(clock["vnow"])

        print("── ⑥ 重开任务表（模拟重启）+ 全链路断言 ──")
        reborn = TaskStore(DATA_DIR / "tasks.db")
        assert [t.note() for t in reborn.all()] == [t.note() for t in store.all()]
        for t in reborn.all():
            print(f"[持久  ] {t.note()} runs={[(s[11:16], st) for s, st, _ in reborn.runs(t.id)]}")
        assert [(s[11:16], st) for s, st, _ in reborn.runs(t1.id)] == [
            ("10:00", "coalesced"), ("12:00", "ok"),
            ("13:00", "skipped-overlap"), ("14:00", "ok")]
        assert reborn.get(t3.id).status == "completed"
        assert reborn.get(t1.id).next_run_at == "2026-09-04T15:00"
        assert reborn.get(t2.id).next_run_at == "2026-09-07T09:30"
        assert len(runs_seen[t1.id]) == 2 and len(set(runs_seen[t1.id])) == 2, \
            "fresh_thread_per_run：每班一条新线程"
        assert runs_seen[t2.id] == [f"thread-task-{t2.id}"], "reuse_thread 固定线程"
        tup = saver.get_tuple({"configurable": {"thread_id": runs_seen[t2.id][0]}})
        assert len(tup.checkpoint["channel_values"]["messages"]) == 2
        assert len(im.received) == 4, "skip 不推送：4 次 ok 才 4 条群消息"
        store.close()
    print(f"[收尾  ] 群消息 {len(im.received)} 条；misfire 合并与重叠 skip 各就各位（退出码 0）")


def online_loop() -> None:
    """在线长驻：config.yaml 的 tasks: 段建任务，真实时钟每 1 秒 tick（Ctrl-C 退出）。"""
    import yaml
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load(Path(os.environ.get("DFL_CONFIG", CONFIG_PATH)).read_text(encoding="utf-8"))
    im = FakeIM()
    store = TaskStore(DATA_DIR / "tasks.db")
    with SqliteSaver.from_conn_string(str(DATA_DIR / "checkpoints.db")) as saver:
        sched = SchedulerService(store, make_start_run(saver, {"vnow": None}, {}, {}, False),
                                 im.publish, lambda task_id: False)
        now = datetime.now()
        for spec in cfg.get("tasks", []):
            print("[建任务]", sched.create(spec["title"], spec["prompt"], spec["schedule_type"],
                                           spec["spec"], now=now).note())
        print("真实时钟 1 秒 tick（cron 最快到分钟级，Ctrl-C 退出）")
        try:
            while True:
                time.sleep(1.0)
                sched.tick(datetime.now())
        except KeyboardInterrupt:
            print("再见")
    store.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线虚拟时钟演示，零 API key")
    args = ap.parse_args()
    demo_fake() if args.fake else online_loop()


if __name__ == "__main__":
    main()
