# v05 · 服务化 —— 进程会死，服务要活

> 断电不可怕，可怕的是重启后它不认识你了。

## 前情提要

v04 的 agent 管好了一个会话内的窗口和账单，但它仍是个"脚本"：
进程一死历史归零，别人也没法调用它。本版补上服务化的两根柱子：
checkpoint 落盘（进程可死）与 HTTP/SSE 接口（他人可用、断线可续）。

## 前置技术

- **checkpointer**：LangGraph 的存档插件。给 agent 挂一个，每跑一步就把
  state 写进指定存储；同 `thread_id` 再调用 = 自动捞回历史。
- **SqliteSaver**：checkpointer 的 SQLite 实现（内存版 MemorySaver 进程死就没）。
- **run**：一次 agent 执行。先落账再执行（queued → success/error），
  死了的 run 在账上仍可见——本体的可见性边界。
- **SSE**：服务器按 `data:` 帧持续推送的 HTTP 长连接。每帧带 `id:`，
  客户端断线重连时塞 `Last-Event-ID` 请求头，服务端从那之后接着发。

## 原理

```
 client                       server（本进程）
   │ POST /threads/t-sse/runs    │
   ├───────────────────────────► │ 落账 run=queued ──► 后台线程跑 agent
   │ ◄── {run_id, queued}        │                     （每产一帧事件，append 进内存账）
   │ GET /runs/<id>/stream       │
   ├───────────────────────────► │ SSE: id:0, id:1 ...
   ✂ 断线（收到 2 帧主动掐）       │   （run 不停，继续生产事件）
   │ GET .../stream              │
   │  Header: Last-Event-ID: 1   │
   ├───────────────────────────► │ 从 id:2 续发 → id:4 end
```

两本账分工：**checkpoint** 存"对话进行到哪"（业务状态，可跨进程复活），
**run 事件账**存"这次执行发生过什么"（传输状态，支持断点续传）。
本体的 StreamBridge 是事件账的加强版（多订阅者、背压），语义同款。

要紧的一点：**SSE 的续传能力来自"事件先落账、连接只管播"**。
worker 线程从不关心有没有人在听；断开的是连接，不是执行。
很多教程把 agent 直接绑在响应生成器上——那种架构一断线整个 run 陪葬。

## 代码精读

续传的服务端只有八行（`v05_service/app.py`）：

```python
start = int(request.headers.get("last-event-id", "-1")) + 1
async def gen():
    i = start
    while True:
        if i < len(evs):                       # 有存货：发一帧进一步
            yield f"id: {i}\nevent: {ev['event']}\ndata: {...}\n\n"
            i += 1
        elif done: return                      # 账本到头：收流
        else: await asyncio.sleep(0.05)        # 没存货：等 next 帧
```

断电演示的"断电"是 `os._exit(9)`（`v05_service/main.py`）：
不走 atexit、不刷缓冲、不给 checkpointer 任何额外机会——
checkpoint 之所以还能救场，正因为它是**每步即时落盘**，不是"退出时保存"。

## 跑起来

```bash
conda run -n deerflow_lab python -m v05_service.main         # 自包含演示
conda run -n deerflow_lab python -m v05_service.main --serve # 常驻 8000 端口，curl 玩
```

**机制信号**（每次必现，全部 assert 硬约束）：

- 幕①：子进程退出码 9；`[重启] 从盘上捞回历史：['HumanMessage','AIMessage']`；
  续聊后 `消息 4 条（2 遗产 + 2 新增）`；
- 幕②：`首连收到 id=['0','1'] -> 主动掐断`；重连 `id=['2','3',...]`；
  末行 `两次连接拼出连续帧 id=[0..n]，断线处零丢失零重复`。

**实录参考**（某次真跑）：

```
[phase1] 第一世答完：'确认：您负责的条线是寄递业务量。'
[杀死] 子进程退出码 = 9（预期 9 = 断电）
[续聊] 第二世同 thread 回答：'您负责的条线是：**寄递业务量**。'
[assert] run 终态=success；两次连接拼出连续帧 id=[0, 1, 2, 3, 4]，断线处零丢失零重复
```

## Python 小课堂：生成器就是 SSE 的骨架

```python
def frames(events):
    for i, ev in enumerate(events):       # yield 使函数变生成器
        yield f"id: {i}\ndata: {ev}\n\n"  # 暂停点：消费方不要，就不再生产
g = frames(["a", "b"])
print(next(g))                            # 只取第一帧——"断线"即停止 next()
```

`StreamingResponse` 每 `next()` 一次推一帧；客户端断开，FastAPI 停止迭代，
生成器就地挂起。`read_frames(..., stop_after_values=2)` 能"演断线"，
靠的也是生成器消费到一半就不消费了。

## 与市面对比

| 做法 | 代表 | 差别 |
|---|---|---|
| 请求-响应 | 普通 REST 调 LLM | 长任务超时，流式没法断线恢复 |
| SSE + 事件落账 | DeerFlow runtime、OpenAI API | 执行与连接解耦，Last-Event-ID 续传 |
| WebSocket | ChatGPT 网页版 | 双向但更重，续传要自造协议 |

## 与本体差异（诚实声明）

- 本体 run 账与事件流持久化到数据库，重启服务器后历史 run 仍可查；
  学习版事件账在进程内存（演示的断线是"连接断"，不是"服务器崩"）。
- 本体 StreamBridge 支持多订阅者扇出与背压控制；学习版单订阅者、无背压。
- 本体网关还有鉴权/RBAC/CSRF/限流（v13 时代的门槛话题，本书按 6 版篇幅砍掉，
  见门面 README 附录）；学习版裸 HTTP，仅限本机演示。

## 练习

把 `--serve` 模式跑起来，用两条 curl 手工验证续传：
起 run 后先 `curl -N .../stream` 看两帧就 Ctrl-C，再
`curl -N -H "Last-Event-ID: 1" .../stream`。
**自证**：第二条 curl 的首帧是 `id: 2`——服务器认得你读到哪了。

## 下一步

持久化、接口、流式都齐了。v06 做最后一步：把 v01–v05 的全部零件
按本体的装配顺序装成一台整机，看"总装清单"到底长什么样。
