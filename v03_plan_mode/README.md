# v03 · 规划模式：给模型立一本不许赖账的作业簿

> 会做一半就交卷的，不是聪明，是缺检查。

## 前情提要

v02 装好了中间件管道，横切关注点有了家。这一版往里挂第一枚"有立场"的中间件：模型面对复杂任务会列计划吗？列了会照做吗？做一半宣布完成怎么办？会话被摘要压缩、计划滑出上下文怎么办？TodoMiddleware 用三个钩子把这些问题逐个钉死。

## 前置技术

- **中间件洋葱圈**：v02 的核心——`middleware=[...]` 列表顺序 = 从外到内的圈层；`wrap_*` 系钩子包住每一次调用。
- **节点型钩子 before_model / after_model**：运行在 model 节点前后，返回字典即更新共享 state；与 wrap 系"每请求一次"不同，它们改的是**图状态**。
- **write_todos 工具**：中间件自带的工具（`middleware.tools` 注册），参数是 `[{content, status}]`，status 三态 pending / in_progress / completed。调用它不是"说给模型听"，而是把清单**写进 state 的 todos 键**。
- **jump_to**：after_model 返回 `{"jump_to": "model"}` 可把执行流弹回 model 节点重跑，需 `@hook_config(can_jump_to=["model"])` 声明。
- **state 与 messages 的分工**：messages 是给模型看的（会被摘要截断），state 是给框架用的（todos 不会被截断）。规划模式的命根子是后者的独立性。

## 原理：为什么要单独造一本"作业簿"

模型的记忆就是消息列表，而消息列表有两个靠不住的毛病。其一，它是**模型的草稿纸**：模型写到一半，草稿纸还在，可它的注意力早飘了——任务做到一半直接"报告完成"，是训练目标（讨好式收尾）使然，不是 bug。其二，它是**有长度的草稿纸**：v08 的摘要中间件迟早会把老消息折掉，连同当初 write_todos 的原文一起压成一句"曾制定过计划"。

对策是把"计划"从草稿纸里**捞出来放进账本**：write_todos 工具返回 `Command(update={"todos": ..., "messages": [...]})`，一次调用同时干两件事——往 state 写权威清单、往历史回一句确认。账本（state.todos）不受摘要影响，这就是断点续做的依据。

然后是制度设计。防提前交卷的判定必须极窄：只有"最后一条 AI 消息**没有** tool_calls 且 todos 有未完成项"才触发。有 tool_calls 说明还在干活，不该管；todos 全 completed 说明真干完了，更不该管。触发之后也不写死历史，而是把催办文案排队，下一次 `wrap_model_call` 时临时拼进请求——模型被催办，用户看不到，存档里查无此条。最后，催办上限 2 次：模型要是两次催办还交卷，说明它真做不下去，继续拦就是死循环。这个上限是整套制度里最容易被忽略、也最能体现工程成熟度的一笔——**制度的最后一道护栏，是知道自己何时该松手**。拦截失败不报错、不抛异常，而是安静放行：agent 系统的错误预算必须留给真正的故障，而不是烧在一次无法推进的任务上。本体把这个上限写成类常量 `_MAX_COMPLETION_REMINDERS = 2`，学习版以模块常量 `MAX_COMPLETION_REMINDERS` 对齐取值，方便逐字对照。

```
        model 节点产出                after_model 判定            结局
   AI(tool_calls=[write_todos]) ──> 还在干活        ──────────> 放行
   AI(tool_calls=[search_notes]) ─> 还在干活        ──────────> 放行
   AI("分析完成，报告已就绪。")     无tool_calls ∧ todos 未完成 ──> 排队催办
                                                        jump_to=model
   （第二次机会）AI 继续干活… 销账 ─> 全 completed   ──────────> 放行交卷
```

## 代码精读

**① 计划入账**：`write_todos` 由 `TodoListMiddleware.tools` 注册（`v03_plan_mode/main.py` 的 TOOLS 里根本没有它），返回 `Command(update={"todos": todos, ...})` 直写 state。最要紧的是：**清单的权威副本从此不在 messages 里**。后面两个钩子全都只信 `state["todos"]`。

**② 防提前交卷**（`v03_plan_mode/todo_middleware.py::TodoMiddleware.after_model`，核心六行）：

```python
last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
if not last_ai or last_ai.tool_calls:
    return None                        # 还想调工具 —— 不算交卷
todos = state.get("todos") or []
if not todos or all(t.get("status") == "completed" for t in todos):
    return None                        # 干完了（或没计划）—— 放行
if self._count >= MAX_COMPLETION_REMINDERS:
    return None                        # 催满 2 次 —— 防死循环，放行
self._pending.append(format_completion_reminder(todos))
return {"jump_to": "model"}            # 拦截：弹回 model 节点
```

判定顺序即优先级：先确认"真交卷"，再确认"没干完"，再确认"还有的催"，三步都过才拍回。本体同款逻辑只多一层并发簿记（按 (thread_id, run_id) 记账），学习版单会话跑，账本简化成两个实例字段。

**③ 断点续做**（`todo_middleware.py::before_model`）：触发条件是 `state 有 todos ∧ 历史无 write_todos 调用 ∧ 没提醒过`。三条与逻辑，一条都不多——write_todos 还在上下文里就不啰嗦，已经提醒过就不重复。注入的 `name="todo_reminder"` 隐藏消息**会**落盘（与催办相反）：它是恢复上下文的正式记录。学习版为演示"压缩后重开"，把父类 `todos` 的 `OmitFromInput` 标记去掉、允许它从输入进 state（见 `PlanState` 注释）；本体靠 checkpointer 跨 run 延续，不需要这个口子。

**④ 三个钩子的分工**：`write_todos` 入账靠工具返回的 `Command`（框架执行状态更新），防交卷靠 `after_model`（判定后弹回），断点恢复靠 `before_model`（检查后注入），催办投递靠 `wrap_model_call`（拼进请求即焚）。一个特性拆四种机构，各管一段互不越权——最要紧的是**没有任何一个钩子试图改模型已经说出的话**：已经落盘的终稿一个字都不动，制度只影响下一次机会。改历史是中间件的头号禁忌，本体的提醒全部走"追加"或"临时拼"，从不回改。

## 跑起来

```bash
conda run -n deerflow_lab python v03_plan_mode/main.py --fake
```

预期输出（确定性，一个字不差）：

```
========================================================================
局 1 · 防提前交卷（after_model + jump_to=model，上限 2 次）
========================================================================
[用户] 请分析上半年寄递业务量并写报告。先列计划再执行。
  >> [TodoMiddleware] 拦截提前交卷 #1 -> jump_to=model
  >> [TodoMiddleware] 催办随本次请求投递（不落盘）
  [HumanMessage] 请分析上半年寄递业务量并写报告。先列计划再执行。
  [AIMessage] -> ['write_todos']
  [ToolMessage name=write_todos hidden=None]
  [AIMessage] -> ['search_notes']
  [ToolMessage name=search_notes hidden=None]
  [AIMessage] 分析完成，报告已就绪。
  [AIMessage] -> ['write_report']
  [ToolMessage name=write_report hidden=None]
  [AIMessage] -> ['write_todos']
  [ToolMessage name=write_todos hidden=None]
  [AIMessage] 已完成：检索数据并输出报告 /mnt/user-data/outputs/寄递业务量-report.md
  state.todos: [{'content': '检索业务量数据', 'status': 'completed'}, {'content': '撰写分析报告', 'status': 'completed'}]

========================================================================
局 2 · 上下文丢失恢复（todos 留在 state，write_todos 已滑出上下文）
========================================================================
[用户] 继续上次没做完的。（历史已被摘要压缩，只剩两句）
  >> [TodoMiddleware] write_todos 已滑出上下文 -> 注入 todo_reminder
  [HumanMessage] 继续上次没做完的。
  [AIMessage] （摘要）上次已完成数据检索。
  [HumanMessage name=todo_reminder hidden=True]
  [AIMessage] -> ['write_report']
  [ToolMessage name=write_report hidden=None]
  [AIMessage] -> ['write_todos']
  [ToolMessage name=write_todos hidden=None]
  [AIMessage] 断点续做完成：报告已产出。
  state.todos: [{'content': '检索业务量数据', 'status': 'completed'}, {'content': '撰写分析报告', 'status': 'completed'}]

催办落盘检查: 0 条（应为 0 —— 只进请求，不进历史）
产物: data/run1.json data/run2.json
```

局 1 精读：`分析完成，报告已就绪。`之后没有 ToolMessage，紧跟着又是一次 `write_report` 调用——这就是被弹回 model 重跑的痕迹。局 2 精读：`todo_reminder` 由 before_model 注入且 `hidden=True`，模型看到它之后接着干活并销账。

## Python 小课堂：生成器表达式 + 三元组与逻辑

三个钩子共用一种句式——在消息堆里找证据：

```python
msgs = [AIMessage_无tool_calls, AIMessage_带tc(write_todos)]
has_write = any(tc.get("name") == "write_todos"
                for m in msgs
                if isinstance(m, AIMessage)          # 只翻 AI 消息
                for tc in (m.tool_calls or []))      # 再翻每条的调用
```

`any(... for ... if ... for ...)` 是双层循环的惰性短路版：找到一条立即返回，消息列表再长也不全表扫描。写中间件的条件判定，这个句式一日要写八遍。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 无规划 | v01 裸循环 | 长任务要么跑偏要么烂尾，全靠模型自觉 |
| 规划即提示 | "先列 TODO 再执行"的 system prompt | 清单只存在于草稿纸（messages），摘要一压就没 |
| 规划入 state | LangChain TodoListMiddleware / DeerFlow | 账本独立于上下文，截不断；再叠防偷懒制度 |

## 与本体差异（诚实声明）

- 本体按 (thread_id, run_id) 做催办簿记并带 4096 键上限的 LRU 清理（多 run 并发安全）；学习版单 run 单会话，简化为两个实例字段，上限 2 次的语义不变。
- 本体只信 checkpointer 延续的 state；学习版为演示断点续做，`PlanState` 重声明 `todos` 去掉了 `OmitFromInput`，允许输入通道带入。
- 本体的 `after_model` 还识别 `invalid_tool_calls`、`finish_reason=tool_calls` 等provider边角（`_has_tool_call_intent_or_error`），学习版只查 `tool_calls` 非空这一主干。
- 工具名、todos 结构、钩子分工与本体一致；reminder 文案与 `_format_completion_reminder` 同构但为学习版自拟。

## 练习

把 `todo_middleware.py` 的 `MAX_COMPLETION_REMINDERS` 改成 0，跑 `--fake` 看局 1：模型第一次提前交卷就没人拦，剧本第 4 条 `write_report` 永远不会被消费，`state.todos` 里"撰写分析报告"停在 pending。改回 2 再跑一遍确认恢复。（自证：两次 `data/run1.json` 内容不同。）

## 下一步

会规划的模型马上要碰真文件——而模型的手（工具）目前既没笼子也没记性。v04 造沙箱：虚拟路径、bash 超时杀进程组、输出掩码，外加"没读过就不许写"的第一版自觉。
