# v15 · 装个闹钟：到点自己起 run

> 路修到微信群了，可消息得有人先开口。老板说：日报要自己来。

## 前情提要

v14 之后 agent 随叫随到，但它从不主动——没人发消息，它就永远安静地等着。本版
装闹钟：cron 决定"何时"，到点走的还是 v11 那条普通 run 流水线，结果由 v14 那条
渠道推进群。顺手把调度系统两张最狰狞的脸也见了：停机的 miss（misfire）和慢任务
的撞车（overlap）。玩具和生产级系统的分界线，几乎就画在这两张脸上。

## 前置技术

- **cron 表达式**：5 段"分 时 日 月 周"，`30 18 * * 1-5` 读作"工作日 18:30"。支
  持 `*`、`n`、`a-b`、`a,b`、`*/n`。本体的计算交给 croniter 库。
- **tick 轮询**：调度器其实不需要"定时器"，每隔 N 秒问一遍"谁到期了"就够了。本
  体是后台协程按 `poll_interval_seconds` 循环调 `run_once(now=...)`
  （`app/scheduler/service.py`）。
- **durable 任务表**：任务与执行历史必须落盘。调度器的本质是"跨时间兑现承诺"，
  承诺存在内存里等于没有承诺。
- **misfire**：宕机期间错过的班次怎么办？补跑几班？一班不补？这叫 misfire 策略。
- **overlap**：上一班还没跑完，下一班的时刻到了。排队？跳过？杀掉上一班？这叫
  overlap 策略。本体的 `overlap_policy` 列默认值就是 `skip`。
- **context_mode**：到点起的 run 用哪条 thread。`fresh_thread_per_run` 每班新开
  一条；`reuse_thread` 永远复用同一条。

## 原理：一条承诺的兑现流水线

```
 create("小时报","0 * * * *") ─► tasks 表: next_run_at=10:00   ← durable 落盘
 tick() 每 N 秒 ─┬─ now ≥ next_run_at ?
                 │     ├─ 否：这一班还不该动，跳过
                 │     └─ 是：
                 │   ① misfire 合并：due 与 now 之间还压着几班？
                 │       10:00 11:00 错过 → 各记一行 coalesced 留痕，
                 │       只补最近一班(12:00)，绝不连环补跑炸群
                 │   ② 重叠 skip：has_active_runs(task)？
                 │       是 → 记 skipped-overlap 墓碑，不排队
                 │   ③ 起普通 run（context_mode 决定新线程 / 复用线程）
                 │   ④ publish(chat_id, 回复) → 渠道推送（v14 那条路）
                 │   ⑤ next_run_at = next_after(now) 落盘，等下一班
```

两条铁律直接继承自本体。**其一，调度器只决定 when，怎么跑归 run 服务**：本版
`start_run` 是注入进来的回调，走的是和聊天消息完全同一条 agent 执行路径，绝不因
为"是定时触发的"就另开旁路。听着像废话，但多少调度系统就坏在这里——定时任务有
自己一套执行逻辑，于是聊天下能跑的东西到点跑不了。**其二，context_mode 二选一**：
日报类必须 `fresh_thread_per_run`，否则一年下来那条 thread 里躺着几百轮"汇总上
一小时"，上下文无限膨胀；提醒类要 `reuse_thread`，它得记得上周已经催过谁。

misfire 为什么"只补最近一次"？停机 3 小时醒来连补 3 份日报，等于把一次事故升级
成一场骚扰。本体的处理更干脆：每轮 `next_run_at` 都基于 now 重算，错过的班次就
是错过了，无声消失（`next_run_at()` 里 once 过期直接返回 None，不补跑）。本版把
"追班"显式做成一行 `coalesced` 记录，好处是**账上看得见曾经错过**——调度系统最
贵的东西不是执行，是可解释。

overlap 为什么宁跳不排？排队的代价是堆积：一个每小时的慢任务一旦开始排队，积压
会永远追不上，最后要么爆内存要么集中爆发。skip 的代价只是"少跑一班"，而且留了
一行墓碑，谁都能查出它为什么没跑。本体的 skip 判定还有一层兜底：`has_active_runs`
是非原子的快路径，真正的仲裁者是数据库上那个部分唯一索引
`uq_scheduled_task_run_active`——两条 dispatch 同时判到"没有活跃 run"时，第二条
的 INSERT 会撞索引。本学习版单进程，省下了这一层。

## 代码精读

`v15_scheduler/cron.py` 的字段解析是全部分魔法的所在地：

```python
if part == "*":               start, end = lo, hi
elif "-" in part:             a, b = part.split("-"); start, end = int(a), int(b)
else:                         start = end = int(part)
if not (lo <= start <= end <= hi) or step < 1:  raise ValueError(...)
values.update(range(start, end + 1, step))     # */n 的 step 在此生效
```

字段 → 集合，命中判断就成了 `dt.minute in self.minutes` 这种毫无想象力的写法。
要紧的是两个方言坑。**周字段 0 = 周日**（cron 传统），而 Python `weekday()` 是
0 = 周一，所以每次比较前都要 `(dt.weekday() + 1) % 7` 换算一次。**日与周同时受限
时是 OR**——`0 9 1 * 5` 是"每月 1 号**或**每周五"，不是"每月 1 号那个周五"。这是
Vixie cron 的经典行为，croniter 与之对齐，所以解析时要记下 `_dom_restricted` /
`_dow_restricted` 两个开关，`_date_ok` 里据此在 OR 与 AND 之间切换。这两个坑不
踩一次，人会一直以为自己写的调度器是对的。

`v15_scheduler/scheduler.py` 的 misfire 合并核心是一个 while：

```python
slot, missed = due, 0
cur = CronSchedule(task.spec["cron"]).next_after(due)
while cur is not None and cur <= now:          # due 与 now 之间还有几班
    slot, missed = cur, missed + 1             # 一路向后合并到最近一班
    cur = CronSchedule(task.spec["cron"]).next_after(cur)
if missed:
    self.store.append_run(TaskRunRow(..., "coalesced", f"错过 {missed} 个槽位…"))
```

要紧的是"只补最近一班"这个决定是**在算法里显式写出来**的，而不是靠"反正也不会
补"糊过去。停机期间小时报错过 10:00 与 11:00 两班，醒来时账上是一行
`coalesced(错过 2 个槽位)` 加一次 12:00 的真实执行——审计时你能说清那两班去哪了。

skip 分支里 once 任务的收尾最见功力：

```python
if self.has_active_runs(task.id):          # 重叠 skip
    self.store.append_run(TaskRunRow(..., "skipped-overlap", SKIP_DETAIL))
    self._reschedule(task, now, executed=False)
    return
```

而 `_reschedule` 在 `executed=False` 且是 once 时，把状态写成 **failed 而不是
completed**。本体 `app/scheduler/service.py::_task_status_for_skip` 的理由值得
抄进枕头：那唯一一次执行没发生，"completed 会谎称发生过一次执行"。状态机的诚实
程度决定你敢不敢信它——一个会说"完成了"其实没跑的系统，三个月后没人敢照着它做
决策。本体那段注释的原话是 `"completed" would claim an execution that never
happened`（这里译成中文）。同理 `_next_time` 里 once 过期返回 None 而不是"顺延到
明天"：闹钟没响就是没响，擅自替用户改时间是危险的体贴。

`v15_scheduler/main.py` 的虚拟时钟是这一版能测起来的唯一原因：

```python
clock = {"vnow": datetime(2026, 9, 4, 9, 5)}     # 周五 09:05
ticks = ["2026-09-04T12:35", "2026-09-04T13:05", "2026-09-04T13:35", "2026-09-04T14:05"]
for label, iso in zip(labels, ticks):
    clock["vnow"] = datetime.fromisoformat(iso)
    time.sleep(1.0)                               # 真睡 1 秒，但世界过了 30 分钟
    sched.tick(clock["vnow"])
```

要紧的是 `tick(now)` 的时间**从参数进来**：`SchedulerService` 整个类没有一处
`datetime.now()`。想测"周五 19:00 之后工作日 18:30 的下一班是下周一"，就传那个
时刻进去，不必真等到下周一。`cron_preflight()` 那 4 条断言全是这么写的。

## 跑起来

```bash
conda run -n deerflow_lab python v15_scheduler/main.py --fake
```

预期输出（本机实跑实录；thread id 里的 8 位十六进制每次随机，其余确定；全程真
睡约 4 秒 = 虚拟过 5 小时）：

```
[cron  ] 断言 ×4 通过：周末跳班 / 日周 OR / 时段步长 / 5 段校验
[建任务] 小时报(status=enabled next=10:00)
[建任务] standup提醒(status=enabled next=09:30)
[建任务] 一次性清理(status=enabled next=10:10)
── ② 停机 2.5 小时后醒来：tick @ 12:35 ──
  [misfire] 小时报: 停机期间错过 2 班，合并补跑最近一次 12:00
  [触发   ] 小时报 slot=12:00 thread=thread-task-95935b30-1200 (fresh_thread_per_run)
  [run    ] 小时报: 本轮很慢，活跃到 13:10
  [IM     ] 群 ops-group 收到「[小时报] 小时报完成：汇总上一小时指标」
  [触发   ] standup提醒 slot=09:30 thread=thread-task-b9572dec (reuse_thread)
  [IM     ] 群 ops-group 收到「[standup提醒] standup提醒完成：催团队站会」
  [触发   ] 一次性清理 slot=10:10 thread=thread-task-e7c0ac90-1010 (fresh_thread_per_run)
  [IM     ] 群 ops-group 收到「[一次性清理] 一次性清理完成：清临时文件」
── ③ 上一轮仍活跃 -> skip：tick @ 13:05 ──
  [skip   ] 小时报: 上一轮仍在执行 -> 本轮记 skipped，不排队
── ④ 空 tick（没有到期）：tick @ 13:35 ──
── ⑤ 释放后下一班照常：tick @ 14:05 ──
  [触发   ] 小时报 slot=14:00 thread=thread-task-95935b30-1400 (fresh_thread_per_run)
  [run    ] 小时报: 本轮很慢，活跃到 14:40
  [IM     ] 群 ops-group 收到「[小时报] 小时报完成：汇总上一小时指标」
── ⑥ 重开任务表（模拟重启）+ 全链路断言 ──
[持久  ] standup提醒(status=enabled next=09:30) runs=[('09:30', 'ok')]
[持久  ] 一次性清理(status=completed next=) runs=[('10:10', 'ok')]
[持久  ] 小时报(status=enabled next=15:00) runs=[('10:00', 'coalesced'), ('12:00', 'ok'), ('13:00', 'skipped-overlap'), ('14:00', 'ok')]
[收尾  ] 群消息 4 条；misfire 合并与重叠 skip 各就各位（退出码 0）
```

时间线这么读：09:05 建三个任务，调度器"停机"到 12:35。醒来发现小时报压着
10:00 / 11:00 两班 → 合并补 12:00 一班；这班故意做得很慢（活跃到 13:10），于是
13:00 那班撞车被 skip；14:00 班正常。standup 是 `reuse_thread`，thread 名里**没有
时刻后缀**；两个 fresh 任务的 thread 名都带 `-1200` / `-1400` 后缀，一眼看出每班
新线程。⑥ 那三行是重开 `tasks.db` 读回来的，其中 `一次性清理(status=completed)`
正是 once 跑完的终态。群消息恰好 4 条，等于 4 次 `ok`——skip 与 coalesced 都不推
送，闹钟不该拿"我没干"去骚扰群。

## Python 小课堂：把时钟变成参数

```python
def tick(now):            # 不读 datetime.now()，now 是入参
    return [t for t in tasks if t.due <= now]
tick(datetime(2026, 9, 4, 12, 35))     # 想几点测就几点测
```

整个 `SchedulerService` 里没有一处 `datetime.now()`，连 `create()` 也只接受
`now=` 关键字参数（而且是必填，漏了直接 TypeError，编译器替你把关）。时间从参数
来，测试里"每天 18:30"不用等到 18:30——这是可测试性最便宜的买路钱。反面写法是
在函数深处偷偷 `datetime.now()`：那一刻起，这段代码只有在 18:30 才能被验证。

同一招在 `store.py` 上又兑现了一次。`TaskRunRow` 里区分着 `scheduled_for` 与
`fired_at` 两个时间：前者是"为哪一班服务"，后者是"实际什么时候动的"。合班补跑时
两者必然不同——`scheduled_for=12:00` 而 `fired_at=12:35`。只记一个时间的账本，无
法回答"这班晚了多久"，而调度系统事故复盘时问的往往正是这句。

## 与市面对比

| 方案 | 代表 | 与本版的差别 |
|---|---|---|
| 系统级 | crontab / systemd timer | 单机、无执行账本；DeerFlow 任务多租户且可 API 管理 |
| 分布式 | Celery beat / APScheduler | 靠锁与租约防多实例抢单；本体 Postgres lease 同理 |
| 平台托管 | LangGraph Platform cron | 平台管队列与重试；DeerFlow 自己管账本 |
| croniter 直用 | 本体 `scheduler/schedules.py` | 库算下一班；本版手写 100 行换取彻底理解 |

## 与本体差异（诚实声明）

- cron 计算：本体 `packages/harness/deerflow/scheduler/schedules.py` 用 croniter，
  只做"恰好 5 段"的校验（`normalize_cron_expression`）；本学习版自己实现
  `next_after`，采用 800 天窗口内"逐日粗筛 + 逐分钟精筛"的扫描（`SCAN_DAYS = 800`），
  窗口外返回 None——边界写在注释里，不假装万能；
- 任务表：本体是 Postgres / SQLAlchemy（`persistence/scheduled_tasks/` 与
  `persistence/scheduled_task_runs/`），带 `lease_owner` 认领、活跃 run 部分唯一
  索引 `uq_scheduled_task_run_active` 兜底并发；本学习版裸 SQLite、单进程、无
  lease，`has_active_runs` 由 `main.py` 用回调模拟；
- misfire：本体靠"每轮基于 now 重算 `next_run_at`"让错过的班次无声消失，**没有
  `coalesced` 这个状态**；本学习版把它显式记成一行留痕，这是自拟；
- once 过期：本体在创建时由路由直接 422（`once schedule must be in the future`），
  且额外有 `min_once_delay_seconds` 约束；本学习版在 `create()` 里标 `failed` 并
  记一行 run 记录，更宽松；
- once 执行期状态：本体触发后置 `running`，等 run 完成的回调才落 `completed`
  （进程重启由 `cancel_stuck_once_tasks` 兜底）；本学习版 `invoke` 同步跑完直接
  落 `completed`，没有中间态；
- 管理面：本学习版只有 `create` / `pause`；本体的 `routers/scheduled_tasks.py` 有
  列表、创建、详情、编辑、pause、resume、trigger、删除、运行历史共 10 个端点；
- 调度类型：本学习版只做 `cron` 与 `once`；时区上本体全程 zoneinfo、UTC 入库，本
  学习版单时区裸 datetime——这两条是本学习版最实在的简化。

## 练习

把 `main.py` 剧本里 ③ 的 tick 时刻从 `13:05` 改成 `13:45`，重跑。
（实测答案：13:00 那班确实不再被 skip——13:45 时上一轮早在 13:10 释放了。但账本
变成 `[('10:00','coalesced'), ('12:00','ok'), ('13:00','ok'),
('14:00','skipped-overlap')]`，撞车的对象换成了 14:00 那班：13:45 补跑的这轮又是
"很慢"，活跃到 14:20，于是把下一班顶掉了。`reborn.runs(t1.id)` 那条断言当场抛
AssertionError。这个结果比"少 skip 一次"有意思得多——**慢任务会把撞车往后推**，
只要每轮都比周期长，skip 就会永远发生。想验证这一点，把 ③ 的 tick 一路往后挪，
直到 `make_start_run` 里那句"活跃 35 分钟"小于 60 分钟，撞车才真的消失。）

## 下一步

到点能自己干活了，可工具全焊死在自己代码里。v16 让工具从别的进程里"长"出来
——MCP。
