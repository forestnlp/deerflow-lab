# v20 · 总装：按本体接线图，全链合一

> 二十版攒了一抽屉零件，今天照原厂图纸把它们插成一台车。

## 前情提要

v19 让一个 agent 同时服务三个渠道，但每一版的中间件都是单件试装：v03 装
Todo、v08 装摘要、v09 装刹车，各管一段。DeerFlow 本体的生产链是三十来个
中间件按**严格顺序**串成的流水线，顺序本身就是语义。本版做总装：按本体
`agents/lead_agent/agent.py::build_middlewares` 的原始顺序，把学习版的
DynamicContext→Summarization→Todo→TokenUsage→Title→Memory→Coalescing→
LoopDetection→Clarification 九环一次插齐，CLI、调度器、渠道三个入口汇进
同一张图，一次 --fake 演示"带 todo、会摘要、有记忆、循环刹车生效"。

## 前置技术

- **中间件洋葱圈**（v02）：before_model 从外到内、after_model 从内到外，
  wrap_model_call 包住整个模型调用；链的顺序决定谁先看到数据。
- **build_middlewares**：本体 lead_agent 的装配函数，返回一个顺序敏感的
  列表喂给 `create_agent(middleware=...)`。
- **jump_to**：中间件返回值里的导航指令，`"model"` 把回合踢回模型重想
  （防提前交卷、循环止损），`"end"` 直接收工（澄清挂起）。
- **旁路（after_agent）**：run 收尾时的异步侧干环节——写标题、进记忆队列，
  失败不阻塞用户已经拿到手的答案。
- **一张图多入口**：图（agent）只有一个，入口（CLI/HTTP/IM/定时器）只是
  不同的 `invoke` 姿势；会话隔离全靠 `configurable.thread_id`。

## 原理：顺序为什么就是语义

总装不是把零件堆到一起，是照图纸落座。九环里至少有三处"换个座位就出
事故"的硬依赖，画出来是这张链：

```
 request ──► DynamicContext  把日期/记忆注进首条 Human（system 保持静态）
        ┌──►  Summarization   先折叠历史：给后面所有人省上下文
        │   ┌► Todo           依赖摘要之后：折叠可能卷走 write_todos，
        │   │                 由 Todo 的提醒机制兜底（顺序=事故与救援）
        │   │  TokenUsage     每次模型调用后记一笔账
        │   │  Title          首轮完整往返后起标题（截的正是原始首条）
        │   │  Memory         after_agent 排队写记忆（本体：防抖+旁路）
        │   │  Coalescing     紧贴模型：把散落的 SystemMessage 捏成一条
        │   │                 前置——放在任何注入 System 的中间件之前=白放
        │   │  LoopDetection  同参三连 -> jump model 踢回重想
        │   │  Clarification  永远最后：ask_clarification -> jump end
        │   └────────────── model / tools ──────────────┘
        └── 三入口共图：CLI.invoke / scheduler.tick / channel.pump
            都以 configurable.thread_id 分会话，中间件状态互不污染
```

第一处：**摘要必须在 Todo 之前**。摘要折叠的是"旧消息"，而 write_todos 的
调用与回执正是最容易变旧的消息。折叠发生在 Todo 检查之前，Todo 才有机会
发现"我的清单滑出上下文了"并补救；顺序反过来，Todo 看到的世界永远是旧的。

第二处：**Coalescing 必须紧贴模型**。链上不止一处会追加 SystemMessage
（摘要的折叠摘要、循环刹车的提醒）。谁先执行不重要，重要的是所有 System
注入者都在 Coalescing **上游**——它在最后一道工序把所有 System 捏成一条
置前，严格网关（vLLM、SGLang、Qwen 一类）才挑不出"非首位 SystemMessage"
的毛病。放链首，等于捏了个寂寞。

第三处：**Clarification 永远最后**。它是全链的"人质谈判专家"：别的中间件
可以放行、可以改道，只有它有权在模型刚开口问人时把整个 run 就地叫停。
若它前面还排着别的 after_model，被它救下的消息会先被下游误处理。本体的
注释原文就是 "ClarificationMiddleware should always be last"。

三入口共图则回答另一个问题：能力该长在哪一层？会话隔离若做在渠道层，
每个入口要重写一遍；做在图层（thread_id 进 configurable），入口就退化成
三行胶水。这就是"薄入口、厚图"。

## 代码精读

`v20_full_assembly/lead_agent.py` 的 build_middlewares，九环各就各位，
注释右侧标着本体 `build_middlewares` 里的出处（约 L 号，行号会漂移）：

```python
def build_middlewares(data_dir: Path) -> list:
    """顺序就是语义：动一行的代价可能是另一环失去它需要的上下文。"""
    return [
        DynamicContextMiddleware(memory_text="- 用户：张三（负责寄递业务）"),
        SummarizationMiddleware(keep_recent=4),    # 本体 L368
        TodoMiddleware(),                          # L375（plan mode 常开）
        TokenUsageMiddleware(),                    # L380
        TitleMiddleware(),                         # L384
        MemoryMiddleware(store_path=data_dir / "memory.log"),   # L392
        SystemMessageCoalescingMiddleware(),       # L420
        LoopDetectionMiddleware(window=3),         # L433
        ClarificationMiddleware(),                 # L472 永远最后
    ]
```

这里最要紧的是这份名单**短得诚实**。九环之外本体还有二十来个中间件，
本版按"能力对账表"逐个交代省略理由，而不是假装链本来就这么短——总装版
最容易骗人的方式，就是只演装配成功的那部分。

`middlewares.py` 的 TodoMiddleware 是全链最复杂的住户，三件事挤在一个类里：

```python
class TodoMiddleware(AgentMiddleware):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.todos: list[dict] = []
        self._reminded = False
        middleware_self = self

        @tool
        def write_todos(todos: list) -> str:
            """创建/更新任务清单。todos: [{content, status}]…"""
            middleware_self.todos = list(todos)     # 工具写实例状态
            return f"已记录 {len(todos)} 项，{done} 项完成"

        self.tools = [write_todos]                  # 工具随中间件一起注册进图

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        msgs = state.get("messages") or []
        last = msgs[-1] if msgs else None
        if (isinstance(last, AIMessage) and not last.tool_calls
                and self._pending() and not self._reminded):
            self._reminded = True                   # 只拦一次，防死循环
            return {"messages": [HumanMessage(
                content="<system_reminder>todos 未完成，先更新状态再交卷。</system_reminder>")],
                "jump_to": "model"}
        return None

    def after_agent(self, state, runtime):
        self._reminded = False                      # run 收尾复位
        return None
```

这里最要紧的是 `_reminded` 的双段生命周期：after_model 里置 True 保证
"一次 run 最多拦一次"（否则模型再交一次卷又跳回去，无限乒乓）；
after_agent 里复位保证**同一个 agent 实例**接下一个 run 时守卫重新武装。
三入口共图意味着实例跨 run 存活，任何"run 级"状态不清零，第二个 run 就
捡到上一个的残骸。这是总装阶段最典型的一类 bug，学习版把它写进代码当教具。

`main.py` 的调度器入口证明"厚图薄入口"不是口号：

```python
class SchedulerEntry:
    def tick(self, now_desc: str) -> list[str]:
        fired = []
        for cron_desc, prompt in self.table:
            if now_desc in cron_desc:               # 学习版匹配：描述含当前时刻即到点
                state = self.agent.invoke(
                    {"messages": [HumanMessage(content=f"[定时任务] {prompt}")]},
                    config={"configurable": {"thread_id": "thr-scheduler"}})
                last = state["messages"][-1]
                asks = [tc for tc in (getattr(last, "tool_calls", []) or [])
                        if tc["name"] == "ask_clarification"]
                fired.append(f"❓ {asks[-1]['args']['question']}" if asks
                             else str(last.content))
        return fired
```

这里最要紧的是最后那五行"澄清转译"：Clarification 的 jump end 让 run 停
在 AIMessage 的 tool_call 上——**没有** ToolMessage，问题藏在参数里。每个
入口都要把它翻译成本渠道的人话（这里加个 ❓ 前缀）。本体在渠道层做的是
同一件事，只是多了持久化和 @提醒。

## 跑起来

```bash
conda run -n deerflow_lab python v20_full_assembly/main.py --fake
```

预期输出（token 累计数、记忆条目确定；无耗时类随机量）：

```
Create Agent(default) -> middleware chain: DynamicContext -> Summarization -> Todo -> TokenUsage -> Title -> Memory -> SystemMessageCoalescing -> LoopDetection -> Clarification

==================== 幕1：CLI 入口（todo + 摘要 + 防提前退出） ====================
    >> [TokenUsage] 本次 ~8，累计 ~8
    >> [Todo] 0/2 完成: 查收入指标[in_progress], 汇总周报[pending]
    >> [TokenUsage] 本次 ~8，累计 ~16
    >> [Title] 会话标题：《生成本周周报，先列计划再干活》
    >> [TokenUsage] 本次 ~14，累计 ~30
    >> [Todo] 想提前交卷？清单还没跑完 -> 拦回模型
    >> [Summarization] 折叠 3 条旧消息 -> 摘要
    >> [TokenUsage] 本次 ~8，累计 ~38
    >> [Todo] 2/2 完成: 查收入指标[completed], 汇总周报[completed]
    >> [Summarization] 折叠 3 条旧消息 -> 摘要
    >> [TokenUsage] 本次 ~33，累计 ~71
    >> [Memory] 写入长期记忆: u1: 完成：H1 寄递收入 42.1 亿元，周报已汇总。
[CLI 回答] 完成：H1 寄递收入 42.1 亿元，周报已汇总。

==================== 幕2：调度器入口（循环刹车 + 澄清挂起） ==================
  << [scheduler] 每天 09:00 拉第三方报表并对比 触发: 拉第三方报表对比收入
    >> [TokenUsage] 本次 ~8，累计 ~79
    >> [TokenUsage] 本次 ~8，累计 ~87
    >> [LoopDetection] 同一调用连续 3 次 -> 刹车
    >> [Summarization] 折叠 3 条旧消息 -> 摘要
    >> [Clarification] 模型要反问 -> 结束本轮
[调度产出] ❓ 第三方报表持续超时，稍后重试还是先用内部指标？

==================== 幕3：渠道入口（旁路机制照常） ====================
    >> [TokenUsage] 本次 ~24，累计 ~111
    >> [Memory] 写入长期记忆: u1: 今日无新增数据，昨日结论仍有效。
[渠道送出] [-> room-经营群] 今日无新增数据，昨日结论仍有效。

==================== 收尾对账 ====================
长期记忆（2 条，每 run 一条）:
  - u1: 完成：H1 寄递收入 42.1 亿元，周报已汇总。
  - u1: 今日无新增数据，昨日结论仍有效。
运行时产物: ['.gitkeep', 'memory.log']
```

验收四条，一条一个主角：幕1 里"拦回模型"后模型**确实回来补写**了
todo 状态（防提前退出生效）；幕1 出现两次"折叠 3 条旧消息"（摘要生效）；
幕2 的 `flaky_report` 三连触发"刹车"，模型改调 ask_clarification 被闸停
（刹车+澄清生效）；收尾的 memory.log 两行对应两个 run 的旁路写入（记忆
生效）。四个入口级主角同框，这就是"总装"的验收单。

## 能力对账表（本体 30+ 中间件 → 学习版对应物）

按 `build_middlewares` 源文件顺序逐条对账；`agents/middlewares/` 目录共
32 个文件（含检测器/元数据等辅助件），本体链的开关条件多为"配置开启才挂"。

| # | 本体中间件（middlewares/） | 学习版对应物 | 说明 |
|---|---|---|---|
| 1 | thread_data / uploads / sandbox_audit / tool_error_handling / dangling_tool_call / llm_error_handling（build_lead_runtime_middlewares 打包的六件） | 有意省略 | 依赖沙箱与线程目录（v04/v05 已单独演示机制），总装链不再重复 |
| 2 | dynamic_context_middleware | DynamicContextMiddleware | 同款：日期/记忆注入首条 Human |
| 3 | skill_activation / skill_tool_policy | 有意省略 | 技能市场是独立子系统，认知模型在 v10 讲过 |
| 4 | durable_context_middleware | 有意省略 | 委派账本+技能文件快照，依赖 3 与 task 工具 |
| 5 | summarization_middleware | SummarizationMiddleware | 同款折叠时机；学习版用抽取式摘要不调 LLM |
| 6 | todo_middleware | TodoMiddleware | write_todos+防提前退出+复位三件套 |
| 7 | token_usage_middleware | TokenUsageMiddleware | 同款 after_model 记账，fake 用估算 |
| 8 | title_middleware | TitleMiddleware | 同款时机；学习版规则截断代替独立 LLM 调用 |
| 9 | memory_middleware | MemoryMiddleware | 同款 after_agent 旁路；学习版无防抖队列 |
| 10 | view_image_middleware | 有意省略 | 仅视觉模型挂载，学习版无视觉演示 |
| 11 | mcp_routing / deferred_tool_filter | 有意省略 | v16 已单独演示 MCP；tool_search 延迟加载未装 |
| 12 | system_message_coalescing_middleware | SystemMessageCoalescingMiddleware | 同款"紧贴模型捏 System" |
| 13 | subagent_limit_middleware | 有意省略 | 无 task 工具即无并发限额对象（机制见 v06） |
| 14 | loop_detection_middleware | LoopDetectionMiddleware | 同款同参窗口刹车，jump model |
| 15 | token_budget_middleware | 有意省略 | v08 讲过预算语义；总装与 fake 剧本无互动 |
| 16 | configured_extensions（配置注入自定义中间件） | 有意省略 | 本体经反射加载，v18 演示了同款反射语义 |
| 17 | terminal_response / model_length_finish_reason / safety_finish_reason | 有意省略 | 三件都是 provider 边角修补（空回复/截断/安全终止），无故障注入不演示 |
| 18 | clarification_middleware | ClarificationMiddleware | 同款永远最后，jump end |
| 19 | read_before_write / input_sanitization / tool_output_budget / tool_output_synopsis / tool_progress / tool_result_meta / tool_result_sanitization / tool_call_metadata | 有意省略 | 工具管道护栏群，v05/v09 各讲其机制 |

对账结论：学习版九环全部在本体链上有同名座位且顺序一致；省略的二十余件
每件给了理由，没有一件是"忘了"。

## Python 小课堂：列表就是策略表

```python
# 装配链写成"返回一个列表"而不是"写一串 add 调用"，好处此刻显形：
mws = build_middlewares(data_dir)
names = [type(m).__name__ for m in mws]
assert names[-1] == "ClarificationMiddleware", "Clarification 必须最后"
assert names.index("SummarizationMiddleware") < names.index("TodoMiddleware")
```

要点：链是数据，就能**断言**。把"永远最后""摘要在前"这类口头规矩写成对
列表的 assert，顺序被改坏时测试当场报警。二十行断言买断九环顺序，是总装
版性价比最高的一笔保险——本体用注释写规矩，学习版建议你再进一步用断言。

## 与市面对比

| 方案 | 代表 | 差别 |
|---|---|---|
| 单体 prompt 工程 | 早期 GPT wrapper | 全塞一个 system prompt，互相打架无法归因 |
| 图手写节点/边 | 原生 LangGraph 手搓 | 自由度高，横切逻辑散落各节点，复用靠抄 |
| 中间件流水线 | DeerFlow、OpenClaw hooks | 横切逻辑各归一家，顺序即架构，可单测可断言 |

## 与本体差异（诚实声明）

- 九环为**最小可演示子集**：本体链 20+ 件，对账表列了省略理由；行号引用
  以写作当日的 `agent.py` 为准，本体演进后以 路径::符号 为准；
- Summarization 本体用独立 LLM 做语义摘要并有 DurableContext 兜底，学习版
  是抽取式拼接；Title 同理（规则截断 vs 独立模型调用）；
- Memory 本体是"防抖队列 + LLM 抽取 + 旁路注入"三件套（agents/memory/），
  学习版只保留 after_agent 落盘 + DynamicContext 注入两个端点，无队列无抽取；
- Todo 的 write_todos 参数校验、todos 滑出上下文后的状态恢复、goal 机制
  （goal_state.py）均未实现；防提前退出为"每 run 最多拦一次"的最简策略；
- LoopDetection 本体有窗口配置、多形态循环识别（loop_detection_config.py），
  学习版只认"同工具同参数连续 window 次"一种形态；
- 三入口中：CLI 无流式；调度器用"描述串匹配"代替 cron 解析（cron 语义在
  v15 已讲）；渠道入口只有 inbox 队列骨架（完整机制在 v14/v19）；
- 无 checkpointer：三个 thread_id 的会话不跨进程存活（v12 讲过 checkpoint
  机制，总装为保持焦点未接）。

## 练习

把 `lead_agent.py` 里 `ClarificationMiddleware()` 与 `LoopDetectionMiddleware`
两行**对调**，重跑 --fake。第一处可见变化在装配日志：
`middleware chain: … -> Clarification -> LoopDetection`——链尾换位了。
第二处要你推演：本版 fake 剧本里两者不会在同一次模型调用上开火，所以幕2
行为不变；但若把剧本改成"ask_clarification 恰好是第 3 次重复调用"，
对调后的链上 LoopDetection 会先于 Clarification 检查这条消息——刹车可能
抢在谈判专家前面把反问吞掉。再对调回来，按 Python 小课堂给链加两条
assert（Clarification 最后、Summarization 在 Todo 前），跑一遍确认绿。
自证：装配行链尾永远是 Clarification，且断言存在。

## 下一步

没有下一步了——这行字本身就是这一版的目的：二十版到此合拢。往后的路是
拿这张地图回本体仓库逐件对照，把省掉的二十来件里挑你需要的装回去。
