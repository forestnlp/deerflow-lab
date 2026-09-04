# v12 · 存盘：断电重启，账本和现场都在

> 上一版的流再漂亮，进程一死，全是一次性烟花。

## 前情提要

v11 把 agent 变成了服务：`RunManager` 管 run 状态机，`MemoryStreamBridge` 管
SSE 直播。但那两个东西全住在内存里——进程一重启，run 账本蒸发，对话现场也
蒸发，前一版演示的 `GET /api/runs/{id}` 立刻变成查无此人。本版补上服务化的
第二块地基：**durable 状态**。检验标准也很硬：把进程真杀掉，重启后还能接着
同一条 thread 把话说完。

## 前置技术

- **checkpoint（检查点）**：LangGraph 的机制——图每执行完一个节点，就把整个
  图状态（messages 为主）按 `thread_id` 存一份档。下次同 `thread_id` 进来，
  先捞档再干活，历史不需要调用方自己拼接。
- **SqliteSaver**：checkpoint 的 SQLite 落盘实现，`from_conn_string("x.db")`
  拿到对象、塞进 `create_agent(checkpointer=...)` 就接上了。本体的
  checkpointer 支持 memory / sqlite / postgres 三种后端
  （`runtime/checkpointer/provider.py`），接口同一套。
- **账本表（runs / threads）**：服务侧的"事实记录"——谁起过 run、现在什么
  状态、错在哪。给运维和 API 查询用，agent 一行都不读它。
- **孤儿 run（orphan）**：进程死的时候账本里还停在 running 的 run。它不会
  自己复活，必须在新进程启动时统一"收尾"，否则账本永远在说谎。
- **os._exit**：不给任何清理机会的退出——不跑 finally、不走
  上下文管理器。想演示"猝死"只能用它，`sys.exit` 是"体面辞职"，不是猝死。

## 原理：两份状态，两种命运

初学者容易把"记忆"和"账本"混成一堆表。其实它们回答的问题不同，恢复方式也
不同，混起来的下场是两头都不可信：

```
                 进程猝死 os._exit(9)
                        │
      ┌─────────────────┴──────────────────┐
      ▼                                    ▼
 checkpoint（现场）                     账本（事实）
 checkpoints.db                        ledger.db
 thread_id → 图状态快照                runs 表：run → status
 "对话进行到哪了"                       "服务记账记到哪了"
      │                                    │
      ▼ 重启：同 thread_id 自动捞回          ▼ 重启：running 全是遗产
 agent 接着聊，历史一条不少                统一 UPDATE → error(orphan)
```

checkpoint 是**给 agent 的**：续聊、断点恢复、time-travel 全靠它，key 是
`thread_id`——这就是为什么从 v03 起 thread_id 一直是头等公民。账本是**给服务
的**：v11 的 `GET /api/runs/{id}` 想跨重启可查，背后就必须是表而不是 dict。

有意思的是崩溃后的两种"缺"：现场是**完整**的（`with` 块正常退出，最后一份
checkpoint 已落盘），账本却是**残缺**的（`finish_run` 永远没执行）。这不是
演示的巧合，而是常态——存档发生在节点边界，收尾发生在任务终点，两者之间
隔着整段可能被打断的执行。所以恢复流程必须一分为二：捞现场靠 checkpoint，
抹平账本靠孤儿恢复，谁也替不了谁。

checkpoint 的 key 只有 `thread_id`，这一点值得停一下。存档里不记"是谁起的
run"，也不记"这次跑的什么脚本"——它只回答"这条会话现在是什么状态"。正因为
key 里没有 run_id，第二个 run 才能顺利接手第一个 run 的现场：**run 会死，
thread 不死**。账本反过来，主键是 run_id，它只关心每一次执行各自的成败。一
把钥匙开两把锁，是很多持久层设计翻车的起点。

孤儿恢复的语义必须诚实。那一次 run **没有**成功，也**没有**被谁取消，它就是
死了。所以收尾成 `error` 而不是 `success`，并把原因写进 error 字段。本体多
worker 部署时不敢这么粗暴——凭什么是"上一世的遗产"而不是"别人家正在跑的
run"？靠 lease（租约）：每个 run 带一个到期时间，属主 worker 定期续租，
只有租约过期加宽限期已过（`runs/manager.py::reconcile_orphaned_inflight_runs`，
约 L1522，配 `grace_seconds`）才敢替别人收尸。单进程学习版没这个歧义：重启
之后还在 running 的，只可能是上一世的自己。

## 代码精读

`v12_persistence/main.py` 里 checkpoint 的用法只有三行，全部魔法在 config：

```python
with SqliteSaver.from_conn_string(str(CKPT_DB)) as saver:
    agent = create_agent(model=..., tools=[], checkpointer=saver)
    result = agent.invoke({"messages": [...]},
                          config={"configurable": {"thread_id": THREAD_ID}})
```

这里最要紧的是：**给 thread_id 存档不需要你写一行存档代码**。每个节点跑完
LangGraph 自动 `put`。重启后同一个 `thread_id` 再 invoke，新 saver 打开同一
个 db 文件，历史就回来了——演示里消息数从 2 变 4，靠的不是任何"读取历史"的
手写代码。

`v12_persistence/main.py` 演示"猝死"的编排：

```python
child = subprocess.run([sys.executable, str(Path(__file__)), "--phase", "1"],
                       capture_output=True, text=True, timeout=60)
print(f"[杀死] 子进程退出码 = {child.returncode}（预期 9 = 猝死）")
assert child.returncode == 9
restart_demo()
```

要紧的是"用子进程"这件事：同一个 Python 进程没法真的杀死自己再醒来，所以第
一世跑在子进程里（`--phase 1` 是 argparse 里的隐藏参数，`SUPPRESS` 掉了帮助
输出），父进程只负责确认退出码是 9，然后以"新进程"身份跑恢复逻辑。这比模拟
更接近真相——第一世的内存、连接、句柄全没了，第二世手里只剩两个 db 文件。

`v12_persistence/ledger.py` 的起账纪律：

```python
def start_run(self, run_id, thread_id):
    """起 run 即落账 status='running'——先写账再执行。"""
    self.touch_thread(thread_id)
    self._conn.execute("INSERT INTO runs (...) VALUES (?, ?, 'running', ...)", ...)
```

最要紧的是顺序：**先落账、后执行**。反过来（先执行后落账），进程若死在执行
途中，账本上连这一笔都不存在——比留下一个 running 孤儿更糟，那笔账彻底失踪，
运维连"曾经有过这件事"都不知道。本体同一立场，写在 `runs/manager.py::
_persist_new_run_to_store` 的注释里：run 创建属于"可见性边界"，调用方不该在
内存里看得见一个 run、而背后的表里却没有对应行。

`v12_persistence/ledger.py::recover_orphans` 的收尾：

```python
cur = self._conn.execute(
    "UPDATE runs SET status='error', error='orphan_recovered: 进程重启前未收尾', "
    "updated_at=? WHERE status='running'", (_now_iso(),))
return cur.rowcount
```

一句 UPDATE 就是"重启恢复"的全部。朴素，但语义完备：数量可断言（演示里
`recovered == 1`），原因可追溯（error 字段留了字样）。返回 `rowcount` 而不是
None，是为了让"恢复了几笔"这种事能被调用方看见——不可计数的恢复等于没做。

`ledger.py` 的读接口还藏着一个 sqlite3 的小门槛：

```python
self._conn.row_factory = sqlite3.Row
row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
return dict(row) if row else None
```

要紧的是 `row_factory = sqlite3.Row`：默认游标返回**只有下标**的元组，
`row[2]` 是 status 还是 error，全看 SELECT 的列序，改一句 SQL 就悄悄错位。
挂上 `Row` 才能按列名取值，演示里 `before["status"]` 这种写法才站得住。列序
依赖是持久层最难查的一类错，因为它不报错，只给错值。

## 跑起来

```bash
conda run -n deerflow_lab python v12_persistence/main.py --fake
```

预期输出（本机实跑实录；两个 run_id 每次随机，其余确定）：

```
[清空] data/ 已重置，开始演示
[phase1] 第 1 轮答完：'收到：你说了你喜欢蓝色，我记下了。'
[phase1] run=c36e10e8 账本状态=running —— 现在进程猝死（os._exit），不收尾
[杀死] 子进程退出码 = 9（预期 9 = 猝死）
[重启] 账本遗产：run=c36e10e8 status=running（上一世没来得及收尾）
[重启] 孤儿恢复：1 个 run 收尾为 error（orphan_recovered: 进程重启前未收尾）
[重启] checkpoint 从盘上捞回历史：['HumanMessage', 'AIMessage']
[续聊] 同 thread_id 第 2 轮：'翻了下历史：你之前说过你喜欢蓝色。'
[续聊] 消息列表 ['HumanMessage', 'AIMessage', 'HumanMessage', 'AIMessage']（2 条遗产 + 2 条新增）
[账本] run=c36e10e8 status=error
[账本] run=bc65c64e status=success
[断言] 孤儿已收尾、历史已复活、账本两行前后相接 —— 断电重启，现场还在
[完成] 演示结束（退出码 0）
```

"杀死→重启→续聊"是真的：第 1 轮跑在**子进程**里并以 `os._exit(9)` 猝死，续聊
发生在新进程。fake 剧本第二环故意回答"你之前说过你喜欢蓝色"——这话说得出口，
只因为历史真从盘上回来了；模型没有别的途径知道你喜欢什么颜色。账本最后两行
一 error 一 success，正是那两世的墓志铭。三行 `[重启]` 的次序也别小看：先读
遗产、再收尾孤儿、最后才捞 checkpoint——恢复流程如果反着来，agent 会先接上
一个账面还挂着的 run，那笔账此后谁也不会再来改它。

## Python 小课堂：os._exit——故意不给清理机会

演示"猝死"不能用 `sys.exit()`，它会走完 finally 和上下文管理器，孤儿当场变成
"正常收尾"，演示就白做了。`os._exit` 直接结束进程，效果等同 kill -9：

```python
import os, sys
try:
    print("A"); sys.stdout.flush(); os._exit(9)
finally:
    print("B —— 永远看不到这行")
# 退出码 9；B 永远不打印
```

`finally` 被跳过是好事，但那句 `flush()` 不是可选的：`os._exit` 连**标准输出的
缓冲区**一起放弃。stdout 接终端时是行缓冲，`A` 侥幸还在；一旦被重定向成文件或
管道就变成块缓冲，`A` 连同缓冲区一起蒸发——上面这个 demo 在 `python x.py
> log.txt` 下会留下一个空文件。本版 `phase1_child` 里 `os._exit(9)` 前那句
`sys.stdout.flush()` 就是为这个写的：父进程用 `capture_output=True` 收子进程输出，
不 flush 就什么都收不到，`[phase1]` 两行会凭空消失。

这里有个精妙的错位：`SqliteSaver` 的 `with` 块在 `os._exit` **之前**已经正常
退出，所以 checkpoint 稳稳落盘；而 ledger 的 `finish_run` 排在 `os._exit`
**之后**，永远没机会执行。一前一后，恰好构造出"现场完整、账本残缺"的孤儿
场景——想故意造都难，这是最诚实的演示写法。

顺带把 `Ledger` 每个写方法结尾那句 `commit()` 交代清楚：sqlite3 的默认行为是
在 DML 前悄悄开事务、**不自动提交**。少了这句，`INSERT` 只活在当前连接的缓冲
区里，子进程一死就跟着蒸发——那"账本遗产"那行会直接查不到记录，抛的是
`TypeError: 'NoneType' object is not subscriptable`，完全看不出根因是没提交。
跨进程可见的账本，每一步都得显式 commit。

## 与市面对比

| 方案 | 代表 | 与本版的差别 |
|---|---|---|
| 客户端自带历史 | 早期 ChatML 手工拼接 | 服务器无状态，换设备、换标签页记忆就断 |
| 平台托管记忆 | LangGraph Platform | 同样 checkpointer 抽象，存储与恢复平台代劳 |
| Postgres + lease 心跳 | DeerFlow 本体 | 机制同构；lease 让多 worker 之间敢互判死刑 |
| 只存 checkpoint 不存账本 | 多数玩具 demo | 对话能续，但"谁起过什么 run"跨重启无从可查 |

这四行的分水岭只有一句话：checkpoint 解决"接着聊"，账本解决"查得清"。缺前者
用户会问"你怎么忘了我说过什么"，缺后者运维会问"刚才那一单到底成没成"。

## 与本体差异（诚实声明）

- 账本存储：本体经 SQLAlchemy ORM 落 Postgres 或 SQLite
  （`persistence/engine.py` 两后端皆支持），并有 alembic 迁移
  （`persistence/migrations/`）；本学习版裸 sqlite3 + `CREATE TABLE IF NOT
  EXISTS`，runs 表只留 6 列；本体 `persistence/run/model.py::RunRow` 除了这
  几件，还带 user_id、assistant_id、model_name、multitask_strategy、消息与
  token 统计、owner_worker_id 与 lease_expires_at 等一大批列；
- 孤儿判定：本体用 `lease_expires_at` 加 grace 宽限期，多 worker 下不误杀
  活 run（`runs/manager.py::reconcile_orphaned_inflight_runs`）；本学习版单
  进程，"重启后 running 即孤儿"一条 SQL 收尾；
- thread 元数据：本体在 `threads_meta` 表
  （`persistence/thread_meta/model.py::ThreadMetaRow`，含 assistant_id、
  user_id、display_name、status 等 8 列）另有完整生命周期；本学习版 threads
  表只留 thread_id/title/created_at 三列，重点是"两张表职责不同"这件事；
- checkpoint 后端：本体可按配置切 memory/sqlite/postgres；本学习版写死
  `SqliteSaver`（同步版），演示完连文件一起删；
- 演示的"进程猝死"用子进程 + `os._exit(9)` 实现，等价 kill -9 但免权限问题；
  本学习版没有 v11 的 SSE/bridge，恢复后不补发历史事件帧。

## 练习

在 `--fake` 演示里把 `finish_run(new_run, "success")` 那行**注释掉**再重跑。
（实测答案：`[账本]` 第二行停在 `status=running`，末尾那句
`[r["status"] for r in runs] == ["error", "success"]` 当场抛 AssertionError
——第二个 run 成了新孤儿。再把那行断言也注释掉让它跑完，然后**另起一个进程**
对同一个 `data/ledger.db` 调一次 `recover_orphans()`：收尾数是 1，账本两行全
变 `error(orphan_recovered: …)`。注意 `--fake` 开头会 `rmtree` 清空 data/，所以
这第二步必须在演示结束之后手工做，不能靠重跑 `--fake` 达成。两步都推演对，说明
你信得过这本账：它宁可记成失败，也不会替你谎报成功。）

## 下一步

账本能扛断电了，可大门还夜不闭户：任何人任何脚本摸到端口就能起 run 烧你的
token。v13 装门卫：API key、CSRF、限流、RBAC。
