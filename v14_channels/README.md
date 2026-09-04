# v14 · 修一条从微信群到 run 的路

> 大门装好了，可老板只打开微信，不打开 curl。

## 前情提要

v13 的门卫很称职，但进出的全是 HTTP 脚本：`X-API-Key`、`X-CSRF-Token`，一个
比一个像机器。真实世界里指令躺在微信群里，回复也得回到群里。本版不新造大脑，
只修路：把 IM 消息一路送进 run，再把回复原路抬回去。路上有四道关卡要设——认
路（映射）、防重（去重）、听懂暗号（命令）、掰开喂（切分）。

## 前置技术

- **pub/sub**：生产者不认消费者。消息扔进总线，订阅者各取各的。渠道与 agent
  就此解耦：加一个 IM 不用改 agent 一行，`MessageBus` 就是那条总线。
- **inbound / outbound**：方向约定。inbound 是"从 IM 进来、奔向 agent"，
  outbound 是"从 agent 出去、回到 IM"。两条道各走各的，绝不共用一个队列。
- **chat_id → thread_id**：微信群只有一个会话，DeerFlow 的会话单位是 thread。
  群成员共享同一条 thread，靠的是一张持久化映射表；表在，记忆就在。
- **消息去重**：IM 的重推机制（长轮询超时重试、webhook 重放、用户手抖连点）会
  把同一条消息送来好几次，不去重就成了复读机。
- **出站切分**：微信单条有长度上限，长回复必须切着发；切在段落接缝上，读者才
  看不出是被切的。
- **asyncio.Event**：一盏单向灯，`set()` 亮、`clear()` 熄，`wait()` 等亮。本版
  用它做"这条消息处理完了"的握手信号。

## 原理：一条消息的往返

```
 用户 ⇢ [FakeIM] ⇢ bus.inbound ⇢ [ChannelManager] ⇢ run(带 checkpoint)
                     队列         ①去重 seen_before
                                  ②命令 /new /status   ⇢ 渠道层直接回
                                  ③映射 chat→thread + 同 chat 串行保护
                                  ④run_handler(thread, msg)
                                       │回复文本        ⇢ bus.outbound 广播
 用户 ⇠ 分片≤400字 ⇠ [FakeIM._on_outbound] ⇠────────────┘
```

四道关卡的**顺序**有讲究，不是随手排的。去重最前：重推的消息根本不配往下走，
越早拦越省。命令其次：`/new` 若先进了 run，等于花钱问 agent 一个渠道层自己就
能答的问题——而且 agent 还真会"努力执行"，把新会话开成一段闲聊。映射第三：
命令不配占用线程，否则 `/status` 一次就多一条 thread 记录。busy 保护紧贴 run
之前：同群上一条没回完，回一句"稍候"，绝不并发写同一条 thread——这正是 v11 里
`create_or_reject` 那条 reject 语义在渠道层的回声，只是这次拦得更早、更便宜。

去重为什么放 SQLite 而不是一个内存 `set`？为了**跨重启**。进程死了 IM 还在重推，
内存 set 一清零就漏，用户看到的就是"我明明只发过一次"。而查登记必须一步原子完
成：

```
先查后插（有竞缝）              INSERT OR IGNORE（一步到位）
  SELECT ... → 没见过去重表        INSERT OR IGNORE INTO seen VALUES (?)
  INSERT INTO seen                 → rowcount == 0 ? 见过 : 新的
  （两步之间若并发，双双放行）      ← 主键冲突即"见过"，天生无竞缝
```

`INSERT OR IGNORE` 后看 `cur.rowcount == 0`：插不进去说明这 id 早就在表里。查询
与登记合成一条语句，没有"查完再插"的时间窗——这是 SQLite 里做去重最省事的写法。

FakeIM 在这张图里同时站了两个岗：`send()` 扮演用户往 inbound 发货，
`_on_outbound()` 扮演聊天软件收 outbound 并落账到 `received`。一个类占两端，是
演示能全自动跑完并自己断言的前提——没有真网络、真端口，也就不用担心时序抖动，
但代价是"网络会不会丢包、重推多久来"这类真实渠道的麻烦被整体省略了。这条边界
记在【与本体差异】里。

## 代码精读

`v14_channels/message_bus.py` 小得羞耻：一个 `asyncio.Queue` 加一个监听列表，
和总线这名字听起来的气势完全不搭。要紧的反而是 `manager.py` 主循环的退出协议：

```python
async def run_forever(self) -> None:
    while not self._stopped:
        msg = await self.bus.inbound.get()
        if msg is None:            # None 哨兵：队列里的"下班卡"
            return
        await self._dispatch(msg)
```

协程没有"挂电话"的硬办法——你不能从外面强行掐掉一个正 `await` 的协程。想让它
体面收工，就往队列里塞一个 `None`：正常的消息不会是 None，所以它是安全的外带
暗号。`main.py` 末尾那句 `await bus.inbound.put(None)` 之后，`await worker`
才回得来。

`_dispatch` 再包一层 try/finally，无论消息死在去重、命令还是 run 异常，最后都
会 `self.bus.processed.set()`。这是演示端与 manager 之间的握手：

```python
bus.processed.clear()              # 先熄灯再发货
await im.send("g1", text, message_id="m-1")
await bus.processed.wait()         # 等回执，再断言
```

要紧的是"先 clear 再 send"这个次序：反过来的话，manager 可能在你 clear 之前
就把灯点亮了，你随后 clear 掉它，然后 `wait()` 到天荒地老。这里最初用的是
`queue.join()`/`task_done()`——那是个**必须还债**的协议，handler 一抛异常、
`task_done()` 少调一次，`join()` 就永久死锁。Event 是"处理过了"的单向灯，丢了
不亏；join 是账本，漏一次死全家。

顺带一提 `main.py` 里的 `chat()` 助手为什么每条消息前都要 `bus.processed.clear()`：
一盏灯服务整场演示，就必须每次自己熄灯。如果改成每条消息新建一个 Event，握手
反而更啰嗦——共享一盏灯省了对象，代价是把"谁负责熄灯"变成一条口头约定。演示
代码可以这么省，生产代码最好别。

`v14_channels/manager.py::_route` 的串行保护：

```python
thread_id = self.mapping.get_or_create(msg.channel_name, msg.chat_id)
chat_key = f"{msg.channel_name}:{msg.chat_id}"
if chat_key in self._busy_chats:
    await self._reply(msg, "上一条还在处理中，请稍候…")
    return
self._busy_chats.add(chat_key)
try:
    reply = await self.run_handler(thread_id, msg)
finally:
    self._busy_chats.discard(chat_key)
```

要紧的是 `finally` 里用 `discard` 而不是 `remove`：`remove` 在键不在时抛
KeyError，会把一个已经处理完的消息变成异常；`discard` 是"有就删、没有拉倒"。
busy 键用 `channel:chat_id` 而不是 thread_id——`/new` 换了 thread，但同一个群
的串行性必须跟着群走，不能因为换线程就允许两条消息同时进 run。

`v14_channels/dedupe_store.py` 的原子判重：

```python
cur = self._conn.execute("INSERT OR IGNORE INTO seen (message_id) VALUES (?)", (message_id,))
self._conn.commit()
return cur.rowcount == 0      # 插不进去 = 早就见过
```

`seen_before` 这名字看着像查询，其实**边查边登记**。名字与副作用不符，是这里
唯一需要警惕的地方——所以注释里写明了"第二次见到同一 id 返回 True"。

`v14_channels/store.py::reset` 是 `/new` 的真身，只有一行关键动作：

```python
key = self._key(channel, chat_id)
self._data[key] = f"thread-{uuid.uuid4().hex[:8]}"
self._save()
return self._data[key]
```

要紧的是它**只换指针，不删数据**。旧 thread_id 从映射表里消失了，但它在
checkpoint 里的消息一条没动——所以 ④ 的断言才能同时成立：新线程历史长度 2，旧
线程仍是 4。"开新会话"从来不是"清空记忆"，只是"换一本笔记继续写"。

`v14_channels/split.py` 是贪心装箱：按 `\n\n` 分段攒片，攒不下就封箱，单段超
限才硬切。优先在段落接缝下刀，读者看不出是被切的。演示剧本里那封"长回复"是四段
各 244 字，正好每段独占一片，于是打印出 `[244, 244, 244, 244]`——`

` 分隔符在
装箱时被丢掉，所以四片合计 976 字，比原文少 6 个字符。真实回复不会这么规整，
`limit` 一到，攒不下封箱和硬切两条分支就会一起上工。

## 跑起来

```bash
conda run -n deerflow_lab python v14_channels/main.py --fake
```

预期输出（本机实跑实录；两个 thread id 每次随机，其余确定）：

```
── ① 正常问答：消息 → run → 回复 ──
[用户] fakeim:g1 发「我喜欢蓝色」（id=m-1）
── ② 重复投递：IM 重推同 message_id，不再回第二次 ──
[用户] fakeim:g1 发「我喜欢蓝色」（id=m-1）
  [manager] 去重丢弃 fakeim:m-1
[用户] fakeim:g1 发「我喜欢蓝色」（id=m-1）
  [manager] 去重丢弃 fakeim:m-1
── ③ 同群第二条消息：同 thread，历史续得上 ──
[用户] fakeim:g1 发「我喜欢的颜色是什么？」（id=随机）
[映射] fakeim:g1 -> thread-99e18624，checkpoint 历史 4 条
── ④ 命令：/status 不改线程，/new 换线程 ──
[用户] fakeim:g1 发「/status」（id=随机）
[用户] fakeim:g1 发「/new」（id=随机）
[用户] fakeim:g1 发「我喜欢蓝色」（id=随机）
── ⑤ 长回复切分：一条 976 字回复按 ≤400 字分片送达 ──
[用户] fakeim:g1 发「讲三个故事」（id=随机）
[IM ] 长回复拆成 4 片，各片长度 [244, 244, 244, 244]
── ⑥ 未知命令提示 + 映射持久化（模拟重启）──
[用户] fakeim:g1 发「/rm -rf」（id=随机）
[持久] 重开映射表：{'fakeim:g1': 'thread-ff708ed7'}
[收尾] FakeIM 共收到 10 条消息；全链路断言通过（退出码 0）
```

断言里有一笔暗账值得手算。FakeIM 收到的 10 条 = ①问答 1 + ③追问 1 + ④三条命令
回复（/status、/new、加上新会话里那句"我喜欢蓝色"）+ ⑤长文 4 片 + ⑥未知命令
提示 1；②那两次重推贡献了 **0** 条，去重在这里立了功。另有两笔账在断言里没打
印：`/new` 之后新 thread 的历史从 2 条起（新会话从零开始），而旧 thread 停在
4 条——**换会话不删档**，老现场整整齐齐躺在 checkpoint 里，只是没人再指过去。

## Python 小课堂：asyncio.Event——单向灯

```python
import asyncio
async def waiter(ev):  await ev.wait(); print("醒了")
async def main():
    ev = asyncio.Event()
    t = asyncio.create_task(waiter(ev))
    await asyncio.sleep(0.1); ev.set()      # 亮灯，唤醒所有等待者
    await t
    ev.clear()                              # 熄灯，可复用
asyncio.run(main())                          # -> 醒了
```

Event 只有两个动作，不记"谁消费的""消费几次"的账。所以它可以复用：本版每发一
条消息前 `clear()`、处理完 `set()`，一盏灯服务整场演示。反过来，如果你的场景
是"N 件事各有一件要等"，Event 就不够了——那要 N 盏灯，或者换成 `asyncio.Queue`
的 `join()`，但那就是把债务协议请回来了，异常路径得自己兜住。

## 与市面对比

| 方案 | 代表 | 与本版的差别 |
|---|---|---|
| 渠道框架 | LangGraph Platform + Assistants metadata | 映射存在平台侧；DeerFlow 自己管这张表 |
| 事件总线 | Redis pub/sub / NATS | 跨进程、可持久化订阅；本体的 bus 也是进程内 |
| SDK 派 | Slack Bolt / 各家官方 SDK | 每个平台各写一遍业务；DeerFlow 一套 manager 通吃 |

一句话概括这张表：DeerFlow 把"业务判定"收在 `ChannelManager` 一处，把"平台差异"
推到渠道适配器一角。加一个 IM 的成本，是写一个适配器，而不是再抄一遍去重和命令。

## 与本体差异（诚实声明）

- 去重：本体 `app/channels/dedupe_store.py` 是 Postgres + 10 分钟 TTL
  （`INBOUND_DEDUPE_TTL_SECONDS = 10 * 60`）+ **fail-open**——存储挂了记日志
  放行（"绝不允许因为存储不可用而丢消息"）。本学习版 SQLite 永久记、无 TTL，
  查不到当新，教学场景宁可复读不可漏答；
- 切分：本体 `app/channels/wechat.py::_split_wechat_text`，上限 2000 字、在窗口
  内用 `rfind("\n")` 找行边界；本学习版按 `\n\n` 段落贪心装箱，并把 limit 调到
  400 好在演示里凑出 4 片；
- 映射表：本体 `app/channels/store.py::ChannelStore` 也是 JSON 文件，且 key 多一
  个 `topic_id` 维度（同一群的不同话题分线程）；本学习版 key 只有
  `channel:chat_id`；
- 命令表：本体 `app/channels/commands.py::KNOWN_CHANNEL_COMMANDS` 共 7 个
  （/bootstrap /goal /new /status /models /memory /help），解析时 `.lower()`；
  本学习版只有 /new /status /help 加未知命令提示，同样 `.lower()`；
- 本学习版 `MessageBus` 的 inbound 也是 `asyncio.Queue`（与本体
  `publish_inbound`/`get_inbound` 同构），但额外加了 `processed` 这个
  `asyncio.Event`——纯演示握手用，生产不需要；
- 本体 manager 另有 run_policy（每渠道的 run 配置，见 `run_policy.py`、
  `feishu_run_policy.py`、`wechat_run_policy.py`）、连接身份
  （`connection_identity.py`）与 8 个真实渠道适配器（dingtalk、discord、feishu、
  github、slack、telegram、wechat、wecom）；本学习版只留 FakeIM 一条假渠道，
  run_handler 直接内联在 `main.py::make_run_handler`。

## 练习

给 manager 加一个 `/id` 命令，回复当前 `message_id` 的前 8 位——`_command_text`
里加一个分支即可（`name` 与 `msg` 两个参数都已经在手上了）。重跑，在 ⑥ 后面照
抄一行 `await chat("/id")`，验证收信数变成 11、且不再出现"未知命令"那行。
（能一次跑过 = 你真看懂了 ②命令关卡为什么排在 ③映射前面：`/id` 压根不需要
thread_id，它要是先走了映射，等于为了看门牌先把房子盖一遍。）

## 下一步

路修通了，可消息得有人先开口。v15 装个闹钟：到点自己起 run，把结果推进群里。
