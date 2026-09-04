# v17 · 可观测：看得见每一步，算得清每一分钱

> agent 不是黑盒，只是你还没给它装表。

## 前情提要

v16 让工具从别的进程里长了出来，随之带来一个不安的问题：一次 run 里 agent
到底干了什么？调了谁、慢在哪、烧了多少 token——内存里的答案随进程一起蒸发。
DeerFlow 本体用 run 事件表加外部 tracing 平台回答这些问题，本版用一张 SQLite
表加一个 contextvar，把同样的机制立起来。

## 前置技术

- **run / trace / thread**：一次用户请求的完整执行叫 run；给它发一个全局身份证
  `trace_id`，这次执行里所有日志与事件都盖这个章；thread 是跨 run 的会话线。
- **事件溯源**：不存"当前状态"，存"发生过什么"。状态可以从事件序列重放出来，
  审计、调试、计费全从同一份数据出。
- **contextvar**：Python 的"执行上下文里的全局变量"——深层调用点不用层层传参
  也能拿到 trace_id，并发任务之间还互不污染。
- **usage_metadata**：真实模型响应里带的 token 账单；fake 模型没有，就得自己估。
- **SQLite**：单文件数据库，零部署。单机单写者场景下它比你想的能扛。
- **聚合预计算**：把"每个 run 的总 token"在写入时就累计好，报表查询从
  扫全表变成读一行。

为什么"事件表 + 单价表"的组合值得单独占一个版本？因为市面上太多 agent
框架把可观测做成了"日志打印器"——log 是给眼睛看的，表是给 SQL 用的。
当老板问"财务月报里 agent 部门本月成本多少"，你需要的是后者。


## 原理：账本要回答的三个问题

一个 agent run 结束，运维想知道的永远是三件事：**干了什么**（每步动作）、
**多快**（每步耗时）、**多少钱**（token 折算费用）。前两件靠埋点，最后一件
靠单价表。把它们焊在一个数据结构上，就得到这张表：

```
 run 事件表（run_events）                    runs（run 头表）
 ┌────┬────────┬──────────┬───────────────┐   ┌──────────┬──────────┐
 │seq │run_id  │category/ │content/meta   │   │ run_id   │ trace_id │
 │    │trace_id│event_type│               │   │ prompt   │ started  │
 ├────┼────────┼──────────┼───────────────┤   │ finished │ pt / ct  │
 │ 1  │ run-03 │ lifecycle│ run_start     │   └────┬─────┘──────────┘
 │ 2  │ run-03 │ llm      │ ~11+~0 tok    │        │ 1:N
 │ 3  │ run-03 │ tool     │ flaky_report  │   run_events ──► 按 (run_id, seq)
 │ 4  │ run-03 │ llm      │ ~16+~0 tok    │                   索引，顺序读
 │ 5  │ run-03 │ tool     │ get_indicator │
 │ 6  │ run-03 │ llm      │ ~25+~20 tok   │   费用 = Σtokens × 单价表
 │ 7  │ run-03 │ lifecycle│ run_end       │          （config.yaml pricing 段）
 └────┴────────┴──────────┴───────────────┘
```

设计上要抠的是两个点。**其一，埋点放哪**。答案在 v02 就写好了：中间件。
观测逻辑一旦渗进业务代码，就像审计员亲自下场做账。`wrap_model_call` 和
`wrap_tool_call` 两个钩子把每次模型调用与工具调用夹在中间，进出各取一次时间，
顺手从响应里摸 token——链路上任何一环都跑不掉，摘掉中间件业务一行不改。

**其二，seq 为什么金贵**。事件按 run 内单调递增编号，(run_id, seq) 建索引。
断线重连的 SSE 客户端只需报告"我收到 seq=5"，服务端从 6 续推，不丢不重；
事后审计按 seq 重放，每一步的先后关系铁证如山。没有 seq 的事件表只是一堆
带时间戳的碎片——时钟会漂移，序号不会。

token 从哪来的问题要诚实回答：真实模型在 `usage_metadata` 里报账，直接读；
fake 模型不报账，本版用一个确定性估算法——一个中文字算 1 token，其余字符
四个折 1 个。它不准，但它**可复现**且量级不差。观测系统的第一原则是先有数
都读写同一个文件，为什么还要 `runs` 头表和 `run_events` 明细表两张？
因为查询模式是两种：报表问"最近 5 个 run 各自多少钱"——这是按 run 聚合，
头表一行一个 run，扫 5 行就够；排障问"这个 run 的每一步"——这是按 seq 扫明细，
走 (run_id, seq) 索引。一张表硬扛两种查询，等于让图书馆用同一套书架既按
出版社排又按出版年排。费力的聚合在写入时预计算好（token 累计存进头表），
这是所有报表系统的祖传配方。

再谈准：有数，可以校准；没数，连错都没法纠。

## 代码精读

`v17_observability/trace_context.py` 全文最重要的就是这几行：

```python
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")

@contextmanager
def trace_scope(trace_id: str, run_id: str, thread_id: str):
    t1 = trace_id_var.set(trace_id)
    t2 = run_id_var.set(run_id)          # run_id/thread_id 同款处理
    t3 = thread_id_var.set(thread_id)
    try:
        yield
    finally:
        trace_id_var.reset(t1)
        run_id_var.reset(t2)
        thread_id_var.reset(t3)
```

这里最要紧的是"贯穿"二字的实现方式：不是每个函数多带一个 trace_id 参数，
而是 run 开头 set 一次，之后任何深度的代码（包括中间件里、线程里）get 即得。
函数签名保持干净，contextvar 跟着执行上下文走，并发的两个 run 各拿各的 id。

`v17_observability/observer_middleware.py` 的记账钩子：

```python
def wrap_tool_call(self, request, handler):
    trace_id, run_id = self._rid()            # 从 contextvar 拿身份
    t0 = time.perf_counter()
    tc = request.tool_call
    result = handler(request)                 # 真正执行工具
    dur_ms = round((time.perf_counter() - t0) * 1000, 1)
    self.store.record(run_id, trace_id, "tool_call", "tool", content=tc["name"],
                      meta={"duration_ms": dur_ms,
                            "args": _clip(json.dumps(tc["args"], ensure_ascii=False)),
                            "result": _clip(str(getattr(result, "content", "")))})
    return result
```

这里最要紧的是"只包裹、不干预"：观测中间件对 handler 的入参出参一个字节都
不改，取完证据原样交还。观测代码一旦开始"顺手修一下数据"，它就不再是观测了，
是事故现场的第二名参与者。另外注意 args 与 result 都过了 `_clip` 截断——
事件表是拿来查线索的，不是拿来存全量 payload 的，后者该去对象存储。

`v17_observability/run_event_store.py` 里有个不属于"概念"但必须交代的细节：

```python
self._lock = threading.Lock()
self._conn = sqlite3.connect(db_path, check_same_thread=False)
```

`v17_observability/report.py` 的 `render()` 值得看一眼，CLI 和 main.py
共用这一个函数，两处输出才能逐字一致：

```python
def render(store: RunEventStore, calc: CostCalculator, limit: int = 5) -> str:
    runs = store.recent_runs(limit=limit)
    lines = []
    lines.append(f"{'run_id':<14}{'trace_id':<14}{'耗时(s)':>9}{'事件':>5}"
                 f"{'tokens(估)':>12}{'费用(USD)':>12}  提示词")
    total = 0.0
    for r in runs:
        cost = calc.cost_usd(r["prompt_tokens"], r["completion_tokens"])
        total += cost
        tokens = f"{r['prompt_tokens']}+{r['completion_tokens']}"
        lines.append(f"{r['run_id']:<14}{r['trace_id']:<14}{r['duration_s']:>9.3f}"
                     f"{len(r['events']):>5}{tokens:>12}{cost:>12.5f}  {r['prompt'][:24]}")
    lines.append(f"合计费用（本次展示 {len(runs)} 个 run）: {total:.5f} USD")
    return "\n".join(lines)
```

这里最要紧的是"单价从配置来"：`CostCalculator` 的两个单价都读自
`config.yaml` 的 `pricing` 段。调价是运维动作，不该是发版动作——这是 v18
"配置驱动"思想在记账场景的第一次落地。

工具节点会在并行工作线程里执行，中间件可能从那条线程写库，SQLite 默认
"连接不许跨线程"会当场炸。`check_same_thread=False` 解除禁令，再自己上锁
串行化写入——单写者场景的标准解法，本体用 SQLAlchemy 连接池做的是同一件事。

`run_event_store.py` 的写入接口克制到只有四个动词：

```python
store.start_run(run_id, trace_id, thread_id, prompt)   # 开户
store.record(run_id, trace_id, "tool_call", "tool",    # 记账
            content="flaky_report", meta={"duration_ms": 0.8, ...})
store.finish_run(run_id, prompt_tokens, completion_tokens)  # 结账
store.recent_runs(limit=5)                              # 查账
```

这里最要紧的是 `seq` 由 store 自己发号（`self._seq[run_id] += 1`），调用方
无权指定。序号一旦允许外部传入，"按 seq 续传"的契约就不成立了——发号权
必须垄断，这和发号车牌、电梯编号是同一个道理。

## 跑起来

```bash
conda run -n deerflow_lab python v17_observability/main.py --fake
python v17_observability/report.py          # 事后单独查账，读同一张表
```

预期输出（结构、行数、事件名、token 数、费用全部确定；耗时/毫秒两列随机器
快慢浮动，其余一字不差）：

```
【A】连跑 3 个 run（时间 2026），事件写 lab.sqlite
  run run-0001 完成（trace_id=tr-0001 已贯穿全部事件）
  run run-0002 完成（trace_id=tr-0002 已贯穿全部事件）
  run run-0003 完成（trace_id=tr-0003 已贯穿全部事件）

【B】最后一个 run 的事件流（seq 单调递增，就是审计的骨架）
  seq=1 [lifecycle/run_start] 拉第三方报表对比收入
  seq=2 [llm/model_call] ~11+~0 tokens 0.8ms
  seq=3 [tool/tool_call] flaky_report 0.8ms
  seq=4 [llm/model_call] ~16+~0 tokens 0.7ms
  seq=5 [tool/tool_call] get_indicator 1.2ms
  seq=6 [llm/model_call] ~25+~20 tokens 0.4ms
  seq=7 [lifecycle/run_end] wall=0.349s

【C】最近 5 个 run 的耗时与费用（费用列 = token×单价，单价见 config.yaml）
run_id        trace_id          耗时(s)   事件   tokens(估)     费用(USD)  提示词
------------------------------------------------------------------------------------------------
run-0003      tr-0003           0.422    7       52+20     0.01980  拉第三方报表对比收入
run-0002      tr-0002           0.337    5        23+9     0.00885  平均投递时长？
run-0001      tr-0001           0.995    5        18+8     0.00750  H1 收入多少？
------------------------------------------------------------------------------------------------
合计费用（本次展示 3 个 run）: 0.03615 USD

提示：同一张表也可以随时单独查：python v17_observability/report.py
```

拿到输出后验三件事：【B】里 7 个事件 seq 从 1 排到 7 且 run-0003 的 trace 从头
到尾没变；【C】里坏工具 run（run-0003）事件最多、费用最高——这正是要观测它
的原因；`report.py` 单独跑出的表格与【C】完全一致，说明账真的落了盘。

顺带解释【C】里一个可能让人皱眉的现象：三个 run 的耗时列出现了 0.995 > 0.422
的"倒挂"——最先跑的 run-0001 反而最慢。这是首次加载的冷启动税：第一次
`create_agent` 装配、第一次 import langgraph 编译器，都记在 run-0001 头上。
真实生产上这类噪声更夸张，所以成熟的可观测系统会把"启动耗时"和"请求耗时"
分开记账；本版把三者都记进 run 生命周期，恰好留了一个活教材：看数据要懂
数据的出生顺序。

## Python 小课堂：contextvars 三分钟上手

```python
import contextvars, threading

req_id = contextvars.ContextVar("req_id", default="-")

def worker():
    print("线程里读到:", req_id.get())   # 新线程默认拿不到主上下文，打印 "-"

def scoped(v):
    req_id.set(v)
    t = threading.Thread(target=worker)
    t.start(); t.join()
    print("作用域内读到:", req_id.get())  # 打印 v

scoped("trace-42")
```

要点：contextvar 的值跟着**执行上下文**走。`asyncio` 每个 Task 自动拷贝一份
上下文，所以并发的两个 run 各改各的 trace_id 互不串线；普通线程则是干净的
新上下文。这就是"隐式传参"的安全边界——它不是全局变量的马甲，是任务作用域的
抽屉。（本版的工具在临时线程里执行时读不到 contextvar？读不到就在中间件
进入工具前把 ids 取好——见 `observer_middleware._rid()` 的写法。）

## 与市面对比

| 方案 | 代表 | 差别 |
|---|---|---|
| 全托管 tracing | Langfuse、LangSmith（本体 `tracing/` 对接的正是这类） | 采集、存储、UI 全有，但数据出内网，私有化要看预算 |
| 结构化日志凑合 | print + grep | 零成本，但一按"每 run 费用"聚合就露馅 |
| 本地事件表 + 导出 | 本版、本体 run_event 表 | 账本在自己库里，想 JOIN 什么 JOIN 什么 |

DeerFlow 本体两边都要：run 事件表是**业务真相**（断线续传、审计都靠它），
外部 tracing 是**观测增强**，职责不同不是二选一。再补一个判断框架：选型时
先问"数据要活多久、谁来看"。看的人是开发、活的是七天的排障现场——外部
tracing 平台合适；看的人是财务和合规、活的是三年的账——事件表必须在自己
手里。两个都要付出代价：前者是订阅费与数据出域，后者是自己写报表的自己
debug。免费的是日志，贵的是可信的数字。

## 与本体差异（诚实声明）

- 本体事件模型在 `persistence/models/run_event.py`（SQLAlchemy ORM + 迁移 + 分页
  索引 + user_id 权限列），学习版是一张裸 SQLite 表，无迁移、无鉴权；
- 本体 token 数来自 `TokenUsageMiddleware` 记录的真实 `usage_metadata`；学习版
  fake 路径用自拟估算法（中文字 1 token、其余 4 字符折 1），估算法为学习版自拟，
  本体无对应物；
- 本体 tracing 走 `tracing/factory.py` 接 Langfuse/LangSmith（span 级、含图结构），
  本学习版目录无独立 `metrics/` 模块（已 Glob 核实），本版 report.py 的统计
  口径为学习版自拟；
- 学习版 run_id/trace_id 用确定性编号（run-0001/tr-0001）以保讲义输出可复现，
  本体用随机 uuid；
- 无事件保留策略/TTL、无并发写入压测，SQLite 单机假设见上文。

先把预期输出当测试用例读一遍：`main.py --fake` 的 stdout 里，`seq`、
`tokens(估)`、`费用(USD)` 三列就是本版的验收单。你甚至可以把上面那段预期
输出存成 golden 文件，改代码前后 diff 一次（耗时列除外）——回归测试的
种子常常就躺在讲义里。

## 练习

把 `report.py` 的默认 `--limit 5` 之外加一个 `--by-tool` 选项：按工具名聚合
`tool_call` 事件的次数与总耗时并打印。自证：跑完 main.py 后 `--by-tool` 里
`get_indicator` 出现 2 次、`flaky_report` 出现 1 次（对不上就是聚合键写错了）。

## 下一步

五个版本攒了一堆零件：模型工厂、中间件、事件表、渠道。可每版都得自己
`python main.py`，改配置要翻代码。v18 立规矩：一份 config.yaml 声明全部，
一条 `dfl.py run` 拉起整套——配置驱动贯彻到底。
