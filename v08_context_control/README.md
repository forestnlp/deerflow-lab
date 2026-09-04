# v08 · 上下文控制：摘要管装得下，预算管花得起

> 窗口是行李箱，账单是体重秤——过检前各有一道闸。

## 前情提要

v07 让 agent 跨会话记住了事实，但"这一世"的对话本身还在无节制地膨胀：读两章手册，工具返回就是几百字，再来回几趟，模型每一轮都在为重读全部历史付钱。这一版装两道闸：SummarizationMiddleware 在窗口超限时把旧消息折叠成一条摘要（管"装得下"），TokenBudgetMiddleware 给单个 run 的用量装上预警、硬停两级保险丝（管"花得起"）。

## 前置技术

- **before_model**：每次模型调用前的钩子，返回 state 更新。摘要选它，因为"要在请求发出之前把窗口压小"。
- **RemoveMessage(REMOVE_ALL_MESSAGES)**：消息通道的"清空重写"指令。返回 `[RemoveMessage(ALL), *新消息]`，add_messages 归约器就把整个历史替换成新消息表——摘要中间件的全部替换手法就是这一句。
- **两级阈值**：warn（先礼：投一条提醒劝模型收尾）与 hard（后兵：剥掉 tool_calls 强制交卷）。本体两级的比例默认 0.8 / 1.0（TokenBudgetConfig 的 warn_threshold / hard_stop_threshold），学习版调低好让十步内看全。
- **硬停不抛异常**：剥 tool_calls 之后，那条 AIMessage 变成纯文本终稿，循环自然终止——调用方看到的是"正常完成"，靠 `stop_reason=token_capped` 才知其是被封顶的。
- **token 从哪来**：真实 API 的响应带 `usage_metadata`（输入/输出 token 数）；--fake 没有账单，学习版用"字符数≈token"近似，机制一字不差。
- **上下文窗口与账单的错位**：窗口是"一次装多少"，账单是"一共付多少"。同一窗口聊十轮，窗口只有一个、账单有十份输入费——本版所有机关都源自这对错位。

## 原理：两道闸为什么必须分开

直觉上"窗口没超就不用管钱"，错。一次 agent run 的花费和窗口占用是**两个变量**：窗口可以靠摘要压到很小，但每次模型调用都要为"当前窗口里的每个字符"付一次输入费——摘要压得越狠、轮次越多，累计账单越可观。反过来，账单充裕的长对话照样能撑爆窗口。所以本体是两枚独立中间件：summarization 盯 `len/trim` 触发压缩，token_budget 盯 `usage_metadata` 累计对账。

摘要机关最巧的地方在**替换的原子性**。`before_model` 返回 `{"messages": [RemoveMessage(REMOVE_ALL_MESSAGES), 摘要消息, *保留段]}`——一次状态更新里"先清空、再铺新"，模型永远看不到"旧历史已被删、摘要还没进来"的中间态。摘要以 `HumanMessage(name="summary")` 的形态躺在窗口头部：不是 SystemMessage（多数供应商不许中途插），不是 AIMessage（那是"模型说过的话"，摘要不是模型亲历），借用人话通道、拿 name 做记号，下次压缩时先把它捞出来当 previous_summary 合并——摘要于是是**滚雪球式**的，第二次压缩折进的是"旧摘要+新垃圾"，前几世的信息不会断根。

折叠点必须切在合法边界：保留段若以 ToolMessage 开头，它的父 AIMessage(tool_calls) 已被折进摘要，剩下一个没有召唤者的工具回执——严格的供应商会直接 400。学习版把切点遇 tool 行就前移；本体的做法更兜底——折叠点允许不完美，由 v09 要讲的 DanglingToolCallMiddleware 在请求边界补孤儿补丁。两版互为防线，这是"护栏各自只管一层、层间互补"的典型样本。

预算闸的预警投递时机同样有讲究。after_model 里发现用量过线时，AIMessage 刚发出 tool_calls，工具还没执行、ToolMessage 还不存在——此刻往历史里插任何非 tool 消息都会拆散 tool_calls↔ToolMessage 的配对（OpenAI 系校验立刻炸）。本体的方案：预警先**排队**，等下一次 `wrap_model_call` 时拼在请求尾部随发不落盘。学习版照抄这个"排队-随发"两段式。

```
 run 时间线 ──────────────────────────────────────────────►
 model call #k          tools            model call #k+1
     ▲                    ▲                    ▲
 after_model          ToolMessage 落账     wrap_model_call:
 用量 58% ≥ warn                          排队中的预警拼上尾部
 → 只排队，不动历史 ─────────────────────► 随发（不落盘）
 用量 85% ≥ hard → 剥 tool_calls、追加 EXCEEDED 文案 → 循环自然终结
```

## 代码精读

**① 原子替换**（`summarization_middleware.py::LabSummarizationMiddleware.before_model`，节选）：

```python
return {"compactions": n, "messages": [
    RemoveMessage(id=REMOVE_ALL_MESSAGES),
    HumanMessage(content=summary, name=SUMMARY_NAME),
    *kept,
]}
```

最要紧的是这三行把"删多少、留多少、摘要放哪"三件事收进一个返回值：删是整通道清空，留是原样搬回，摘要是带 name 的 HumanMessage。本体在同位置多做的只有两件事——摘要消息的 token 上限截断和压缩前保留动态上下文提醒，骨架分毫不差。

**② 排队-随发两段式**（`token_budget_middleware.py`，两段骨架）：

```python
# after_model：只排队，绝不动历史
self._pending.append(_WARN_MSG.format(...))
# wrap_model_call：下次请求时投递，拼尾部、不落盘
note = HumanMessage(content=..., name="budget_warning")
request = request.override(messages=[*request.messages, note])
```

最要紧的是 `override` 与落盘注入的区别：v07 的记忆提醒经 before_agent **写进历史**（后续每轮都要看见它）；预算预警**只活在那一次请求里**（劝完即散，历史保持干净）。同一个"注入"意图，两种持久度，按语义寿命选通道。

**③ 硬停的体面**（`token_budget_middleware.py::after_model` 硬停分支）：

```python
stripped = last.model_copy(update={"content": content, "tool_calls": [],
                                   "response_metadata": {..., "finish_reason": "stop"}})
return {"messages": [stripped]}
```

最要紧的是 `model_copy` 同 id 替换：add_messages 见到相同 id 的消息是**覆盖**而非追加，所以"改写最后一句"不需要先删再写。剥 tool_calls 必须连 `finish_reason` 一起改成 stop，还要清掉 `additional_kwargs` 里供应商原始载荷的 tool_calls 残留——否则有的序列化器照旧把那段 JSON 发回给 API，"剥了个寂寞"。本体 `_build_hard_stop_update` 把这三处一次擦净，学习版擦前两处，additional_kwargs 清理在 fake 路径上无从触发、留作在线适配。

## 跑起来

```bash
conda run -n deerflow_lab python v08_context_control/main.py --fake
```

预期输出（确定性，重复运行逐字节一致）：

```
==============================================
场景一 · 摘要折叠：before_model 触发，旧消息换摘要
==============================================

[A1 · 读第1章]
  [用户] 读手册第1章，给我一句要点。
  [终稿] 第1章要点：库存周转是躺在仓里的钱，转起来才算生意。
  [窗口] 4 条 ≈277 字符: ['HumanMessage', 'AIMessage', 'ToolMessage', 'AIMessage']

[A2 · 读第2章（窗口超阈值 -> 开工前折叠）]
  [用户] 再读第2章，同样给一句。
  >> [Summarization] 估算 550 字符 ≥ 阈值 420：折叠 4 条旧消息 → 摘要 1 条，保留 3 条（第 1 次压缩）
  [终稿] 第2章要点：网络密度决定单件成本，前提是别空驶。
  [窗口] 5 条 ≈330 字符: ['摘要', 'HumanMessage', 'AIMessage', 'ToolMessage', 'AIMessage']

[A3 · 折叠后的窗口上合并结论]
  [用户] 把两章合并成两句结论。
  [终稿] 两章合并：先把库存转起来，再把网络织密且不空驶，利润自己会来。
  [窗口] 7 条 ≈372 字符: ['摘要', 'HumanMessage', 'AIMessage', 'ToolMessage', 'AIMessage', 'HumanMessage', 'AIMessage']

[A4 · 折叠后再追问]
  [用户] 两章各自的主题词是什么？
  [终稿] 第1章：库存周转。第2章：网络密度。摘要里都在。
  [窗口] 9 条 ≈408 字符: ['摘要', 'HumanMessage', 'AIMessage', 'ToolMessage', 'AIMessage', 'HumanMessage', 'AIMessage', 'HumanMessage', 'AIMessage']

==============================================
场景二 · token 预算：预警 → （窗口再压缩也）→ 硬停
==============================================

[B1 · 连读带比对（预警→再压缩→硬停）]
  [用户] 依次读第1、2章，再比对一次第1章，然后给我结论。
  >> [Summarization] 估算 523 字符 ≥ 阈值 420：折叠 3 条旧消息 → 摘要 1 条，保留 2 条（第 1 次压缩）
  >> [TokenBudget] 预警：581/1000 (58%)，提醒随下次模型请求投递
  >> [Summarization] 估算 531 字符 ≥ 阈值 420：折叠 3 条旧消息 → 摘要 1 条，保留 2 条（第 2 次压缩）
  >> [TokenBudget] 硬停：851/1000 (85%)，剥掉 tool_calls 强制交卷
  [终稿] [TOKEN BUDGET EXCEEDED] 用量 851 超过安全线 1000。只能用手头结果交卷。
  [窗口] 4 条 ≈322 字符: ['摘要', 'AIMessage', 'ToolMessage', 'AIMessage']
  [stop_reason] token_capped

最终窗口落盘 data/context_log.txt：
  摘要 | AIMessage | ToolMessage | AIMessage
```

三处精读：A2 的 `>> [Summarization] 估算 550 字符 ≥ 阈值 420` 是 before_model 触发的现场——注意折叠发生在**回答之前**，模型从没见过超限的窗口；B1 里预警（58%）与硬停（85%）之间还夹着**第二次摘要压缩**——窗口这边刚从 523 字符被压回去一点，那边账单照样从 581 烧到 851 击穿硬停线。摘要救得了"装不下"，救不了"花得多"：每一次模型调用仍要为窗口里的全部字符付一遍输入费，轮次一多，账照烧。这是两道闸互不越位、谁也替代不了谁的活证；`[stop_reason] token_capped` 是硬停留在现场的签名，调用方靠它把"体面交卷"与"被封顶"区分开。

## Python 小课堂：一个返回值改写整个历史——删除的归约语义

```python
from langgraph.graph.message import add_messages, REMOVE_ALL_MESSAGES
from langchain_core.messages import AnyMessage, HumanMessage, RemoveMessage

history = [HumanMessage("第1世"), HumanMessage("第2世"), HumanMessage("第3世")]

# 普通追加（默认归约）
print(len(add_messages(history, [HumanMessage("新")])))        # 4

# 清空重写（摘要中间件的手法）
new = add_messages(history, [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                             HumanMessage("摘要")])
print([str(m.content) for m in new])                           # ['摘要']
```

`add_messages` 不是简单 append：它按消息 id 归约——同 id 覆盖、`RemoveMessage` 删除、`REMOVE_ALL_MESSAGES` 清空。"中间件返回 dict"这条统一接口之下，**追加、覆盖、清空**三种破坏力全藏在返回值的消息类型里。读懂 add_messages，就读懂了本版和 v03、v09 所有"改历史"的机关。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 截断旧消息 | trim_messages / 多数 demo | 一刀切掉最老的，信息无声蒸发 |
| LLM 摘要折叠 | Claude Code 的 auto-compact、DeerFlow | 旧消息先变知识再退场，摘要滚动合并不断根 |
| 账单熔断 | OpenAI Agents SDK 的 guardrail | 多在轮次/时间维度，token 级双阈值不常见 |

DeerFlow 把"窗口"与"账单"拆成两枚中间件、各带独立配置（summarization_config / token_budget_config），本表第三行的双阈值+排队预警在同类产品里是更精细的那一档——代价是两套状态簿记，本体各用了 BoundedDict 防泄漏。

## 与本体差异（诚实声明）

- 本体摘要继承 langchain 的 SummarizationMiddleware，按真实 token 触发，带候选模型链与重试、previous_summary 显式参数、压缩事件回调、动态上下文提醒保留；学习版直接继承 AgentMiddleware 手写触发与替换，摘要模型单候选，重试与回调砍掉。`RemoveMessage(REMOVE_ALL_MESSAGES)` 替换、`name="summary"` 摘要消息、切点保配对三条机制一致（本体切点不完美时靠 DanglingToolCallMiddleware 兜底，学习版直接在切点前移——路径不同，达到的不变量相同）。
- 本体预算读 `usage_metadata` 并按 run_id 簿记（支持子代理回溯归集、warn 0.8 / hard 1.0、BoundedDict 防泄漏、stop_reason 经 consume_stop_reason 弹出）；学习版用字符近似、单 run 现场变量、`stop_reason` 直接暴露成属性。"排队-随发"、"硬停剥 tool_calls 不抛异常"、"同 id 覆盖替换"三条与本体一致。
- 学习版把"1 字符 ≈ 1 token"当近似口径写进代码注释与输出行（`≈N 字符`），不冒充真实 token 数；在线模式代码路径已留（换 get_model 即可），但账单字段依赖真实 usage_metadata，未在线实测。

## 练习

把 `main.py` 的 `TRIGGER_CHARS` 从 420 调到 800，跑 `--fake`：`>> [Summarization]` 行全部消失（含场景二那两处），A4 的窗口变成 12 条 ≈652 字符——历史再无折叠、一路裸涨。（自证：全文 `>> [Summarization]` 行数从 3 变 0。已实测。）

## 下一步

v08 管的是"资源"失控，v09 管"行为"失控：模型原地转圈、工具调用悬空断头、供应商一句说半截、信息不够硬往上冲——四枚护栏中间件，一种一个接法。
