# v06 · 总装 —— 一台机器，多个入口

> 零件各自能转不算完工。本体真正的机密藏在两件事里：中间件的**装配顺序**，
> 和"机器只有一台、入口却有好几个"的架构。

## 前情提要

五版下来，零件箱已经装满：v02 的洋葱圈与规划制度、v03 的沙箱与两道护栏、
v04 的摘要/预算/长期记忆、v05 的 checkpointer 与服务化。
但它们各自为战——每版只挂自己那两个中间件跑自己的演示。
本版做两件事：把它们按**本体 build_middlewares 的同款顺序**装成一台整机；
再让 CLI、调度器、IM 渠道三个入口汇入这同一台机器。

## 前置技术

- **build_middlewares**：本体 `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
  里的总装函数。`create_agent(middleware=[...])` 收的就是它返回的那个列表。
- **顺序是设计**：本体在函数头上写了七行注释当设计文档（agent.py L261-270），
  全文照抄前两行感受一下：

  ```
  ThreadDataMiddleware must be before SandboxMiddleware to ensure thread_id is available
  ...
  SummarizationMiddleware should be early to reduce context before other processing
  TodoListMiddleware should be before ClarificationMiddleware to allow todo management
  ...
  ClarificationMiddleware should be last
  ```

- **多入口单机器**：CLI、cron、IM 网关都只是"外壳"——把外部输入翻译成
  `agent.invoke(...)`，把结果翻译回去。agent 实例与 checkpointer 全进程只有一个，
  会话之间靠 `thread_id` 隔离。

## 原理

```
  CLI 终端          cron 调度器          IM 渠道
  entry_cli()      entry_cron()        entry_im()
      │                │                   │
      │ thread=im-main │ thread=cron-daily │ thread=im-main   ← 只有 thread_id 不同
      └────────┬───────┴─────────┬─────────┘
               ▼                 ▼
        make_lead_agent()  ←—— 一台机器（六件中间件按图纸顺序）
               │
        SqliteSaver（v05）── 每个 thread 一本历史账
               │
        LocalSandbox（v03）── 文件都落在同一个沙箱
```

装配顺序为什么要紧？看图纸里两个位置就懂了：

- **摘要 must be early**——先折叠再让别人干活。若 LoopDetection 排前面，
  它倒扫的是折叠前的原始历史，白算；先摘要，后面对所有中间件都是轻装。
- **护栏（未读先写否决）wrap_tool_call 在最外层的工具环节**——否决发生在
  "工具执行之前"。v04 实测过 `jump_to="end"` 拦不住**已发出**的工具调用，
  执行前拦截只能靠 wrap_tool_call，所以这类否决件必须排在会被它否决的件之前。

## 代码精读

`v06_assembly/lead_agent.py` 的图纸与总装：

```python
BLUEPRINT = [   # (来自哪一版, 类, 职责, 对应本体链上的位置)
    ("v03", ReadBeforeWriteMiddleware, "沙箱+未读先写否决", "Sandbox/read_before_write：靠前"),
    ("v04", SummarizeMiddleware,       "窗口超阈值折叠旧消息", "Summarization：must be early"),
    ("v02", TodoMiddleware,            "write_todos+拦提前交卷", "TodoList：before Clarification"),
    ("v04", MemoryMiddleware,          "开局注入事实/收场抽事实落盘", "Memory：after Title"),
    ("v03", LoopDetectionMiddleware,   "同调用连打->警戒/剥爪", "LoopDetection：靠后"),
    ("v04", BudgetMiddleware,          "预算半程预警/超支硬停", "TokenBudget：LoopDetection 之后"),
]
```

`make_lead_agent()` 逐件实例化、`assert` 实例顺序与图纸一致、打印装配清单，
最后一次性交给 `create_agent(middleware=parts, checkpointer=checkpointer)`。

`v06_assembly/main.py` 的三个入口各只有几行，全是"翻译"工作：

```python
def entry_cron(agent, saver) -> None:
    r = agent.invoke(
        {"messages": [HumanMessage("定时任务：read_file 读出 ... 然后一句话汇报。")]},
        config={"configurable": {"thread_id": CRON_THREAD}})   # 没有终端，代码就是"用户"
```

注意幕③ IM 用的 `thread_id` 与幕① CLI 相同——所以它能接上幕①的历史继续聊，
这正是"跨入口续聊"的全部秘密：**外壳换了，账本没换**。

## 跑起来

```bash
conda run -n deerflow_lab python -m v06_assembly.main
```

**机制信号**（每次必现，全部 assert 硬约束）：

- 真模型开口**之前**先打出六行【装配清单】（装配清单不经模型，顺序 100% 确定）；
- 三幕各现 `>> [记忆] 注入 1 条磁盘事实`（开局）与 `>> [记忆] 结算落盘`（收场）；
- `note.md` 落沙箱、`memory.json` 落盘、IM 幕消息数 > CLI 幕（6 → 8），
  末行 `[assert] 同一 agent 三个入口 ... 跨入口续聊由 checkpointer 接管`。

**实录参考**（某次真跑，你的措辞会不同）：

```
=== 幕① CLI 入口 ===
  >> [记忆] 注入 1 条磁盘事实（name=memory_reminder，对用户不可见）
[CLI] 终稿：已记住：您负责的条线是**寄递业务量**。…12 × 1.5 = **18**
  >> [记忆] 结算落盘 memory.json: ['用户负责的条线是寄递业务量']
=== 幕② 调度器入口（cron 08:00 触发） ===
[Cron] 汇报：文件是一份「业务量备忘」，记录了你负责的寄递业务量条线上半年业务量同比增长 12%。
=== 幕③ IM 渠道入口（同一 thread 续聊） ===
[IM] 终稿：1. 您负责的条线：**寄递业务量**  2. 上半年业务量同比增长：**12%**
[assert] 同一 agent 三个入口：CLI 6 条 -> IM 8 条，跨入口续聊由 checkpointer 接管；
         调度器 thread 独立互不串台
```

幕③里模型没看任何文件就答出"12%"——那在幕①的历史里，靠 checkpointer 捞回；
而"寄递业务量"它同时也能从 memory.json 的注入里拿到。**两条记忆通路，同一幕会师**，
你能从回答里看出它们互为冗余——这正是本体的双保险设计。

## Python 小课堂：列表即图纸

```python
BLUEPRINT = [("v03", ReadBeforeWriteMiddleware, ...), ...]  # 数据
assert [type(p) for p in parts] == [row[1] for row in BLUEPRINT]  # 图纸与实物对账
```

装配清单写成**数据**而不是散落的 `append` 调用，好处有三：可以打印（本版信号）、
可以对账（上面的 assert）、可以注释位置依据（第 4 列）。本体没这么写（它是一串
带注释的 `append`），但那种写法顺序错了没人知道——数据驱动 + 对账断言是学习版
给"顺序是设计"上的保险丝。

## 与市面对比

| 做法 | 代表 | 差别 |
|---|---|---|
| 单入口脚本 | 多数 agent 教程 | 一个 main 函数走到底，cron/IM 各复制一份代码 |
| 单机器多入口 | DeerFlow、OpenAI Assistants API | 入口只是翻译层；会话状态在 checkpointer |
| 图编排平台 | Dify / n8n | 编排可视化，但中间件级细粒度否决不如代码直接 |

## 与本体差异（诚实声明）

- 本体链上还有十来件本版没造的角色：ThreadData/Uploads（多租户文件）、
  DynamicContext（日期/记忆以 `<system-reminder>` 注入首条 HumanMessage，
  保持 system prompt 全静态吃前缀缓存）、Skills 两件、DurableContext、
  TokenUsage/Title、ViewImage（仅 vision 模型）、MCP 路由 + DeferredToolFilter、
  SystemMessageCoalescing、SubagentLimit、TerminalResponse、SafetyFinishReason、
  Clarification（永远最后）。各自一句话见门面 README 附录。
- SystemMessageCoalescing 与本书 v04 踩过的是同一个坑：严格网关要求请求里
  必有 user 消息，所以"折叠系统消息"这类活儿本体单独立一件中间件收尾。
- 学习版 Summarize/Budget 阈值设得极宽（只挂不发），因为本版主角是装配本身；
  想调戏它们，改 `lead_agent.py` 里那两个数字即可（v04 README 有实测手感数据）。

## 练习

把 `BLUEPRINT` 里 TodoMiddleware 挪到 SummarizeMiddleware 之前，
`assert` 会当场翻脸——然后你想办法让代码重新通过（提示：改图纸不是改实物，
图纸才是设计）。再想想：真要让摘要在 Todo 之后跑，本体会出什么事？
**自证**：说出至少一条"位置错了虽不报错但语义变了"的后果
（参考原理一节的两个例子）。

## 下一步

六版到此收官。回门面 README 看全景地图，以及被本书砍掉的机制
（subagents / MCP / 渠道 / 调度器 / RBAC / 可观测性）在本体里各是什么位置——
那是你合上书之后真正去读 `build_middlewares` 全文时的地图。
