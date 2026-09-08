# v04 · 上下文与记忆 —— 窗口、账单、与换个会话还是你

> 记忆不是记住一切，是知道该忘什么。

## 前情提要

v03 管住了模型的行为，还没管住对话本身：连读三章手册，窗口从 277 字符滚到
1200+，再聊十轮网关直接拒收；账单没人看管；用户明天换个会话，一切归零。
本版装三个"忘性管理"中间件：折叠、掐表、留底。

## 前置技术

- **上下文窗口**：模型一次能看的字符总量有上限，超了要么拒收要么截断。
  本版用字符数估算 token（学习版口径，讲义不装精确）。
- **before_model / after_agent**：v02 的节点型钩子在本版全员上岗——
  开局前改窗口（折叠、注入），收场后干私活（抽取记忆）。
- **RemoveMessage(REMOVE_ALL_MESSAGES)**：LangGraph 消息通道的"清空重放"指令，
  中间件返回 `[RemoveMessage(ALL), 摘要, *保留消息]` 即整体替换。
- **checkpointer vs 记忆**：前者存"这场对话的完整录像"（thread 级），
  后者存"跨对话值得记的几句话"（用户级、纯文本）。两种生命周期，两个存储。

## 原理

三条线各掐一个尺度：

```
                 ┌ 幕① Summarize（管窗口）
   消息通道 ──────┤    超 600 字符 → 旧账折成一条摘要
                 │      [H,A,T,A,H,A,T,A] ──→ [摘要,A,H,A,T,A]
                 ├ 幕② Budget（管账单）
                 │    半程预警 → 超支 jump_to=end 掐整个 run
                 │
                 └ 幕③ Memory（管跨世）
                      run 收尾：旁路模型抽事实 → memory.json
                      新 run 开局：facts 注入 <user_memory>
```

三幕共用一个套路：**旁路模型调用 + 往消息流注入**，agent 主循环一行不改。
摘要执笔的是真模型，记忆抽取的也是——中间件有权自己调模型，这是它比
"回调钩子"强的地方。

两个注入消息都有讲究：
- 摘要走 `HumanMessage(name="summary")` 而非 SystemMessage——严格网关要求
  每个请求必有 user 消息，折叠点之后没了 user 直接 400（在线实测踩过）；
- 记忆走 `HumanMessage(name="memory_reminder", hide_from_ui=True)`——
  前端按 name 过滤，用户看不到，模型看得到。

## 代码精读

折叠的全部动作（`v04_context_memory/context_tools.py`）：

```python
if estimate_chars(messages) < self.trigger:
    return None
old, recent = messages[:-self.keep], messages[-self.keep:]
digest = get_model().invoke("把下面对话压缩成不超过 60 字的一条摘要…" + ...)
summary = HumanMessage(name=SUMMARY_NAME,
                       content=f"<conversation_summary>{digest.content}</conversation_summary>")
return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary, *recent]}
```

要紧的是**旁路模型只喂截断后的线索**（每条 60 字）：压缩历史这件事本身
不能把全量历史再花一遍 token，否则折叠就成了亏本买卖。

预算中间件里最值得琢磨的是实测行为：`jump_to="end"` 硬停时，**已经发出去的
工具照样执行完**，掐掉的是下一次模型轮——

```
[窗口] 5 条: ['HumanMessage', 'AIMessage', 'ToolMessage', 'ToolMessage', 'ToolMessage']
```

三次读取全部完成（钱已花），但读完后模型没有机会再说话。想"执行前就拦"，
得用 v03 的 wrap_tool_call 否决——两道机关掐的位置不同，账单责任也不同。

## 跑起来

```bash
conda run -n deerflow_lab python -m v04_context_memory.main    # 仓库根目录执行
```

**机制信号**（每次必现）：

- 幕①：某轮出现 `>> [摘要] 窗口 N 字符超阈值 600 -> x 条旧消息折叠成 1 条摘要`，
  且窗口清单第一个元素变成 `摘要`；
- 幕②自检（不经模型）：`patch={'jump_to': 'end'}` 与 `预警：已近半程` 两行；
  真模型演示：`[stop_reason] 预算 600 字符已耗尽…` 且终窗无最终 AIMessage；
- 幕③：第一世 `结算落盘 memory.json: [...]`，第二世 `注入 2 条磁盘事实` +
  `[记忆注入] 在场`，且 `data/memory.json` 存在。

**实录参考**（某次真跑）：

```
>> [摘要] 窗口 620 字符超阈值 600 -> 7 条旧消息折叠成 1 条摘要
[窗口] 6 条 ≈618 字符: ['摘要', 'AIMessage', 'HumanMessage', 'AIMessage', 'ToolMessage', 'AIMessage']
...
>> [预算] 预算 600 字符已耗尽（本轮需 672） -> jump_to=end 硬停
...
[记忆注入] 在场: <user_memory>负责寄递业务；看报告只看收入</user_memory>
[回答] 建议每周盯：周寄递业务收入（按 国际件/农村/国内标准件 三段拆开看）
       …你只看收入，那就把收入用到极致…
```

模型的回答确实在"你只看收入"的口径内展开——记忆注入起效的直接证据。

## Python 小课堂：`getattr` 的默认值做身份过滤

```python
["摘要" if getattr(m, "name", None) == "summary" else type(m).__name__ for m in messages]
```

消息基类没有 `name` 属性，有名字的注入消息才有。`getattr(m, "name", None)`
一行完成"有则取值、无则 None"，比 `hasattr` + 取值的两步写法干净。
本体大量用 `name` 给注入消息发身份证（summary/memory_reminder/
todo_reminder），前端和中间件都按证识人。

## 与市面对比

| 做法 | 代表 | 差别 |
|---|---|---|
| 截断最旧消息 | 多数聊天套壳 | 丢信息，丢得悄无声息 |
| LLM 摘要折叠 | Claude Code、DeerFlow | 花一次旁路调用保住事实，且可审计（摘要在窗口里看得见） |
| 向量化长期记忆 | mem0、Letta | 按语义检索注入；本书用"全量小文件"，量级小、可直读 |

## 与本体差异（诚实声明）

- 本体摘要带 previous_summary 滚动合并（折叠多次不丢最早事实）、token 用真
  分词器数；学习版每次现摘、字符估算。
- 本体记忆抽取带防抖（debounce）+ 多轮合并，避免每轮 run 都花旁路调用；
  学习版每次 after_agent 直接结算。
- 折叠点的 tool_call/ToolMessage 配对对齐：本体靠 DanglingToolCall 中间件
  兜底，学习版靠 keep_recent 取偶数条粗略规避——极端场景可能拆开配对，已知粗化。

## 练习

把 `SummarizeMiddleware(trigger_chars=600, keep_recent=2)` 的
`keep_recent` 改成 `4`，幕①的摘要信号晚出现在哪一轮？预测后实跑。
**自证**：A2 或 A3 的 `[窗口]` 行里 `摘要` 首次出现的轮次变了，
且折叠后保留的尾部消息多了一条。

## 下一步

单人单机版能干了，但它还不是一门"服务"：没有 API、进程一挂历史全无。
v05 把它接到网上——SqliteSaver 持久化 + FastAPI + SSE 断线续传。
