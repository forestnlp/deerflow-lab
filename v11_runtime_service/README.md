# v11 · 服务化：脚本变服务，answer 变 stream

> 答案再好，进程结束才亮牌就是烟花——本版让第一手就被看见。

## 前情提要

前十版把 agent 本身越修越结实：ReAct 循环、中间件管道、沙箱、安全护栏、
子代理，一样不缺。但形态从头到尾没变过——`invoke` 进去，结果出来，进程
结束。真实的 DeerFlow 是 7×24 的服务：用户发条消息，服务端起一个 **run**，
前端要**实时**看到进度，网断了还得**接着看**。前十版修的是大脑，本版补的
是"服务"这两个字：run 状态机、SSE 直播、断线续传。

## 前置技术

- **run**：一次 agent 执行的服务端记账单位。有身份（run_id）、归属
  （thread_id）、生命周期状态机。它和协程的区别是：协程只有"在跑"这一种
  真相，run 还有"别人查得到它跑到哪了"这一层。
- **SSE**：Server-Sent Events。服务器往一条长连接的 HTTP 响应里连续写文本
  帧，每帧 `event:`/`data:`/`id:` 各一行、空行收尾；浏览器原生支持它断线
  自动重连。方向只有下行，正好对上一半的 agent 场景。
- **Last-Event-ID**：SSE 客户端重连时自动带上的请求头，意思是"我最后看到
  的帧 id 是几"。服务端据此决定从哪一条续发——断线续传在协议层的全部
  秘密，就是这一个头加服务端一本带序号的账。
- **生产者/消费者解耦**：agent 只管往中间层写事件，几个消费者、在不在看、
  各看到哪了，它一概不知。这个中间层在本版叫 `MemoryStreamBridge`。
- **asyncio 后台任务**：`asyncio.create_task` 把协程挂上事件循环后立即返回
  ——HTTP 先回"已受理"，活继续在后台干。这是"POST 起 run 能秒回 run_id"
  的全部机关；否则客户端要等整个 agent 跑完才拿到回执。
- **stream_mode="values"**：LangGraph 的流式模式之一，每完成一个节点就把
  当前**全量** state 吐一份出来。所以帧与帧之间消息数是递增的，不是增量。

## 原理：为什么要中间那一层

天真做法是 agent 生成一段就往 HTTP 响应写一段。能跑，但埋三个雷：消费者
一断开，下一次写入当场就炸；开两个标签页，第二个看不到直播；断线重连时，
没有"播到哪了"的任何记录可查。三个雷同根同源——**生产者与消费者硬绑定**，
一条流的生死系于唯一那个读者。

DeerFlow 的答案是在中间垫一层**进程内事件日志**：生产者只管往日志尾部追加，
每个消费者自带游标只管往前读，两边永远不见面。

```
   POST /api/threads/{tid}/runs    GET /api/runs/{rid}/stream  ← 断线带 Last-Event-ID
      │                          ▲      ▲
      ▼                          │      │ 各自游标，各看各的
 ┌─ RunManager ─┐        ┌────── StreamBridge ──────┐
 │ queued       │  起    │ run_id → 事件日志(带 id)  │
 │  → running   ├─worker─┤  0,1,2,3,4 …  裁剪旧帧    │
 │  → success/  │ publish│  ↑publish  ↓subscribe    │
 │    error     │        └──────────────────────────┘
 └──────────────┘           agent 不知道有几个人在看
```

拆开之后三个雷各自熄灭：读者跑了，日志照写不误；多观众，多几个游标就是；
重连按 Last-Event-ID 定位"下一条"。还白赚一样东西：**状态机独立于流**。
没人看，run 照样从 queued 走到 success，`GET /api/runs/{id}` 随时查得到。
左边那本"状态账"和右边那本"事件账"分开记，是这一版真正的分水岭——很多
自研 agent 服务做到后面发现要补的，恰恰就是当初省掉的这两本账。

顺带交代一个选型：为什么用 SSE 不用 WebSocket？因为这里的数据流是单向的
——服务端播、客户端看，控制面（起 run、查状态）走普通 REST 就够了。SSE
就是普通 HTTP，浏览器断线自动重连并自带 Last-Event-ID，等于免费拿到续传
协议的客户端半套；WebSocket 是双向的，帧要自己管、心跳要自己发、重连与
续传逻辑全得手写。用不上双向却付全套的钱，DeerFlow 主通道至今是 SSE。

日志还配了一个保活细节：订阅者空等超过 `heartbeat_interval` 秒，bridge 会
吐一个 `HEARTBEAT_SENTINEL`，网关把它翻译成 `: heartbeat` 这样的 SSE 注释帧
写进连接。注释帧不触发任何事件回调，纯粹是让中间代理知道"这条线还有气"——
长连接最怕的不是慢，是安静。

## 代码精读

`v11_runtime_service/runs.py` 的 reject 语义：

```python
def create_or_reject(self, thread_id: str) -> RunRecord:
    inflight = [r for r in self._runs.values()
                if r.thread_id == thread_id and r.status in ACTIVE_STATUSES]
    if inflight:
        raise ConflictError(f"线程 {thread_id} 已有活跃 run（{inflight[0].run_id}）")
    record = RunRecord(run_id=uuid.uuid4().hex[:8], thread_id=thread_id)
    self._runs[record.run_id] = record
    return record
```

这里最要紧的是：**同一线程同时只许一个活跃 run**。`ACTIVE_STATUSES` 就是
queued 加 running 两态。两个 run 并行写同一份消息历史必然互相踩，与其事后
和解，不如在门口就把第二个拒掉。方法名 `create_or_reject` 本身就是语义：
要么给身份，要么明确拒绝，不存在"先收下再说"。

`v11_runtime_service/stream_bridge.py` 的续传定位是纯算术：

```python
def _resolve_offset(self, stream, last_event_id):
    if last_event_id is None:
        return stream.start_offset          # 新订阅：从最早保留的帧起
    seq = int(last_event_id)
    if seq < stream.start_offset:           # 要的帧已被裁剪，从最早重放
        return stream.start_offset
    return seq + 1                          # 事件 id == 绝对偏移，O(1) 定位
```

要紧的是事件 id 的设计：run 内单调序号恰好等于数组偏移，重连定位不用查表，
`seq + 1` 就是下一条。日志超过 `maxsize=256` 时旧帧被裁掉，`start_offset`
同步前移——序号继续涨、下标会归零，两套坐标靠这一个偏移量对齐。

`v11_runtime_service/worker.py` 的收尾纪律：

```python
try:
    async for chunk in agent.astream(..., stream_mode="values"):
        await bridge.publish(run.run_id, "values", {...})
    manager.set_status(run.run_id, RunStatus.SUCCESS)
except Exception as exc:
    manager.set_status(run.run_id, RunStatus.ERROR, error=str(exc))
finally:
    await bridge.publish_end(run.run_id)    # 无论成败必发终止信号
```

最要紧的是 `finally`：不发 end，订阅者会在 `condition.wait()` 里一直等到
超时、然后每 `heartbeat_interval` 秒吐一个心跳帧，永远等下去。worker 死得
体面，观众却成了冤魂。同理 `except` 里那句 publish 也不能省——错误要让
正在看的人当场看见，而不是靠轮询状态去猜。

同一文件里 `_msg_brief` 负责瘦身。因为 values 帧带的是**全量**消息，长对话
里原样广播等于每帧重发一遍历史：

```python
def _msg_brief(m) -> dict:
    return {"type": m.type,
            "content": (m.content or "")[:120],
            "tool_calls": [c["name"] for c in (getattr(m, "tool_calls", None) or [])]}
```

要紧的是"直播只报进度，不搬仓库"：观众要看的是"到哪一步了"，全量历史留在
checkpoint 里按需去取。帧越瘦，能同时喂的观众越多。

`v11_runtime_service/gateway.py` 把三条线拧在一起：

```python
@app.post("/api/threads/{thread_id}/runs")
async def create_run(thread_id: str, prompt: str = "你好") -> dict:
    run = manager.create_or_reject(thread_id)        # ConflictError 另有分支
    asyncio.create_task(run_agent(agent, manager, bridge, run, prompt, ...))
    return {"run_id": run.run_id, "status": run.status.value}
```

要紧的是"先记账、再挂任务、立刻返回"这个顺序：run 已登记为 queued，回执里
那个 run_id 拿回去一定查得到，而任务才刚排上事件循环。反过来先起任务后登记，
就会出现"帧都写了、登记表里却查无此 run"的空窗。

`v11_runtime_service/main.py` 里断线重连的编排与验收：

```python
with client.stream("GET", f"{base}/api/runs/{run_id}/stream") as resp:
    first = read_frames(resp.iter_lines(), stop_after_values=2)
last_id = first[-1]["id"]
with client.stream("GET", f"{base}/api/runs/{run_id}/stream",
                   headers={"Last-Event-ID": last_id}) as resp:
    second = read_frames(resp.iter_lines())
ids = [int(f["id"]) for f in first + second if "id" in f]
assert ids == list(range(len(ids)))         # id 连续 = 零丢失、零重复
```

要紧的是断言写法：不比内容，比**序号连续性**。序号连续且唯一，断线处就
一个不少、一个不重——比逐帧比对省事得多。`end` 帧故意不带 id，因为它不对应
任何状态变化，只是"可以挂电话了"的礼貌信号。

## 跑起来

```bash
conda run -n deerflow_lab python v11_runtime_service/main.py --fake
```

预期输出（本机实跑实录；端口与 run_id 每次随机，其余确定）：

```
[server] uvicorn 已启动 http://127.0.0.1:36561
[client] POST /api/threads/t-demo/runs -> run_id=76418951 status=queued
[client] 首连收到：
    event=metadata id=0  {"run_id": "76418951", "thread_id": "t-demo"}
    event=values id=1  msgs=1 last=human/-
    event=values id=2  msgs=2 last=ai/calculator
[client] —— 模拟断线：收满 2 个 values 帧主动掐断，Last-Event-ID=2 ——
[client] 重连（带上 Last-Event-ID）收到：
    event=values id=3  msgs=3 last=tool/-
    event=values id=4  msgs=4 last=ai/-
    event=end  {"done": true}
[assert] run 最终状态 = success
[assert] 两次连接拼出连续事件流 id=[0, 1, 2, 3, 4]，断线处零丢失、零重复
[server] 已关闭，演示结束（退出码 0）
```

怎么读这段输出：id=2 与 id=3 之间就是断线点，一个不多一个不少。`msgs` 从 1
涨到 4，恰好对应 fake 剧本的四步——人类进来 → AI 点 `calculator` 算
`(2+3)*7` → 工具回执 → AI 交卷。`last=ai/calculator` 里斜杠后半段是
`_msg_brief` 带出的工具名，说明这一帧 AI 还在"点菜"、任务没完。演示用
`frame_interval=0.15` 把帧间隔人为拉长，客户端才来得及"中途掐线"；
`data/frames.log` 落了全部原帧，想核对 SSE 帧格式可以直接打开看。

开头那帧 `metadata` 值得留意：它不带消息，只把 run_id 与 thread_id 亮给
观众自报家门——观众拿到重连凭证的第一步，就藏在这一帧里。

## Python 小课堂：asyncio.Condition——广播而非抢消息

Bridge 用 Condition 不用 Queue：每个消费者要自己的游标，而 Queue 一条消息
只能被一个人取走：

```python
import asyncio
async def main():
    cond, log = asyncio.Condition(), []
    async def prod():
        for i in range(3):
            async with cond: log.append(i); cond.notify_all()
    async def cons(n):
        seen = 0
        while seen < 3:
            async with cond:
                while len(log) <= seen: await cond.wait()
                print(n, log[seen]); seen += 1
    await asyncio.gather(prod(), cons("A"), cons("B"))
asyncio.run(main())   # A、B 各自看到 0 1 2
```

区别一句话：Queue 是"抢"——消息离队即蒸发；Condition+日志是"播"——记录
留在原地，来晚了从任意游标补看。另外注意 `while len(log) <= seen` 这个写法：
`wait()` 可能被虚假唤醒，也可能被别人的事件唤醒，所以醒来必须重新核对条件，
绝不能醒来就直接读。`bridge.subscribe` 里那个 `asyncio.wait_for(..., timeout)`
配 `TimeoutError` 的写法也是同一套路：唤醒有第二种原因（超时）时，必须区分
"有新东西"和"只是该发心跳了"。直播和点播，本来就是两种数据结构。

## 与市面对比

| 方案 | 代表 | 与本版的差别 |
|---|---|---|
| 裸 SSE 直写 | 早期 serve 教程 | 无 bridge 层，一断全炸，第二个观众没直播 |
| WebSocket | ChatGPT 网页版 | 双向，但续传、重放、多消费者全要自己造 |
| Queue+StreamManager | LangGraph Platform | 与 DeerFlow 同构，平台把队列托管了 |
| 消息队列直通 | Kafka / RabbitMQ | 跨进程天然多消费者；按 run 重放照样得自己搭 |

一句话总结这张表：事件流这层早晚要有人记账，区别只是**你自己记**（DeerFlow
的 bridge）、**平台替你记**（LangGraph Platform），还是**假装不用记**（裸
SSE 直写，前两个雷响得最响）。

## 与本体差异（诚实声明）

- 路由形态：本体 `routers/thread_runs.py` 把起 run（POST）、查状态
  （GET `/{thread_id}/runs/{run_id}`）、重连接听（join）分成独立端点，
  本学习版合成三条简化路由（查状态是 GET `/api/runs/{run_id}`）；
- 事件 id：本体形如 `{ts}-{seq}`（`runtime/stream_bridge/memory.py::_next_id`），
  本学习版只用纯序号；日志裁剪超限时本体发 `StreamGap` 提示客户端去重载
  持久化状态（`runtime/stream_bridge/base.py::StreamGap`），本学习版静默
  从最早保留的帧重放；
- bridge 实现：本体有 memory 与 Redis Streams 两种（`supports_cross_process`
  区分），后者可跨进程；本学习版只有进程内 memory。空等发心跳的机制与本体
  一致（`stream_bridge/base.py::StreamBridge.subscribe` 同款默认 15 秒，
  演示里为早点看到调成了 2 秒）；
- 状态机：本体六态 pending/running/success/error/timeout/interrupted
  （`runtime/runs/schemas.py::RunStatus`），另有 lease 心跳与孤儿回收；
  本学习版留四态加 reject，并把 pending 改名 queued；
- 断线语义：本体 `sse_consumer` 的 `finally` 里还要执行 on_disconnect
  （cancel 时真去中断后台任务，continue 时放任跑完），本学习版只保留
  "发终止信号"这一件事；
- `frame_interval` 为本学习版自拟参数，专为演示"中途断线"拉长帧间隔；SSE
  读 `Last-Event-ID` 再传给 `subscribe(last_event_id=...)` 的写法，与本体
  `gateway/services.py::sse_consumer`（约 L1234）一致。

## 练习

把 `main.py` 首连的 `stop_after_values=2` 改成 1 跑一次，再改成 4 跑一次。
（答案特征：改 1 时首连只见 id=0、1，掐线行末尾显示 `Last-Event-ID=1`，重连
补 id=2 到 4，拼接断言照旧通过；改 4 时首连读完全部 values 帧，重连只剩一个
end 帧，断言行一字不变。注意掐线那行的前半句"收满 2 个 values 帧"是 `main.py`
里写死的文案，不随参数变——只有 `Last-Event-ID` 那个数是真的。两次结果都能提前
推演出来，说明 `_resolve_offset` 真读懂了。）

## 下一步

内存里的日志和账本，进程一死全归零。v12 把状态写进 SQLite：杀进程重启，
同 thread_id 接着聊。
