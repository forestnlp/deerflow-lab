# v09 · 行为护栏：agent 发疯的四种姿势，各有一张接人的网

> 模型会犯的错，框架必须全部接得住。

## 前情提要

v08 治的是资源失控——窗口撑爆、账单烧穿。这一版治**行为**失控：模型原地转圈刷同一个调用、会话中断留下没回执的"断头"工具调用、供应商安全策略把回复拦腰截断、信息明明不够却硬着头皮往下查。本体给每种失控配了一枚专职中间件，四枚的共性是：**都不抛异常**。护栏的职责不是惩罚模型，而是把失态的对话拉回可继续、可交付的轨道。

## 前置技术

- **tool_calls 与 ToolMessage 的配对契约**：AIMessage 每发起一个 tool_call，历史上就必须有一条同 id 的 ToolMessage 作回执。OpenAI 系严格校验，缺一条就 400——悬空修复与循环剥爪都在伺候这条契约。
- **剥 tool_calls**：把 AIMessage 的 tool_calls 清空、finish_reason 改成 stop，模型就"被迫"以纯文本交卷。循环检测与预算硬停共用的那招，v08 已见过一次。
- **finish_reason**：供应商在 response_metadata 里说明回复为何结束。`tool_calls`/`stop` 正常；`content_filter`（OpenAI 系）、`refusal`（Anthropic）、`SAFETY`（Gemini）意味着"被安全策略拦腰砍"。
- **wrap_tool_call 拦截**：v02 的否决权——不调 handler，直接伪造 ToolMessage。澄清中间件用它让 ask_clarification"永远不执行"。
- **jump_to=end**：v03 见过的跳转，after/before_model 返回 `{"jump_to": "end"}` 让本轮立即收束。澄清的"执行中断"在学习版借它实现。

## 原理：四种失控，一张对照表

先看全景。四枚中间件挂在不同钩子上，动刀的深度也不同——这是设计的全部要点：**能不动历史就不动，必须动时只动最后一次**。

```
 失控症状              中间件                钩子              动刀深度
 ────────────────────────────────────────────────────────────────────
 原地转圈          LoopDetection        after_model 计哈希   触顶才改写最后一条
   （同一调用反复）                     wrap_model_call 投递  + 排队警告随发
 断头调用          DanglingToolCall     wrap_model_call      只改本次请求
   （回执缺失）                         补配对/丢孤儿         历史一字不动
 说半截话          SafetyFinishReason   after_model 查        改写最后一条
   （content_filter）                   finish_reason         剥爪+回填说明
 硬往上冲          Clarification        wrap_tool_call 拦工具  落一条提问回执
   （信息不足）                         before_model jump end  本轮收束
```

**循环检测**的判据是"同一组工具调用的哈希在滑窗里出现了几次"。哈希把 name+参数压成 12 位指纹，排序后再哈希——并行调用的顺序不同也算同一组。三级反应：不足为惧、到 warn 线排队提醒（等下一次请求随发，理由和 v08 的预算预警完全相同：after_model 时刻 ToolMessage 还没落账，此刻插话会拆散配对）、到 hard 线剥爪强制交卷。为什么不能只靠"轮数上限"一刀切？因为正常任务也可能二十轮，**重复**才是病，轮多不是。

**悬空修复**是四枚里唯一"只改请求、不改历史"的。它的触发场景很具体：用户中途关掉页面，最后一个 tool_call 发出去了、回执永远不会有；下次开机，这条 AIMessage 就成了没答案的问题悬在历史里。修复必须**就地**插在 offending AIMessage 的正后方——若用 before_model 返回消息，归约器只会把它追加到队尾，答案离问题十万八千里，严格的供应商照样拒收。这是 wrap_model_call 存在的最好理由：**消息的插入位置也是语义的一部分**。顺带它反向也修：孤儿 ToolMessage（父调用已被摘要折走）直接从请求里丢掉，防 400。

**安全终止**处理的场面最委屈：模型其实说了一半人话（content 有值），只是 finish_reason 暴露了它被 content_filter 拦腰砍。粗暴做法是整条丢弃，但已产出的半段分析仍有价值；本体的做法是保留文本、剥掉可能残缺可疑的 tool_calls、追加一句"后续内容不可得"，并把 `safety_capped` 写进停止原因。空 content 也要回填占位文本——否则空 assistant 消息会把整条 thread 毒死（下一请求必被严格供应商拒绝），本体注释里专门记了这个坑（#4393）。

**澄清**反着来：前三枚都在"收拾模型做坏的事"，这枚是"拦下模型该问不问的事"。模型调 ask_clarification 说明它自己承认信息不够——此时继续跑下去只会拿错误假设烧完整条流水线。拦截点选 wrap_tool_call：工具本体不执行、伪造一条 `[向用户提问]` 的回执落盘（会话界面据此渲染问题），再 jump end 收束本轮。用户的答案作为下一条 HumanMessage 开新一世——**用对话轮次本身实现"中断等待"**，学习版不依赖 LangGraph 的 interrupt 原语，语义等价且更适合演示。

## 代码精读

**① 循环的指纹**（`guard_middlewares.py::_hash_tool_calls` + `after_model` 计数）：

```python
call_hash = _hash_tool_calls(last.tool_calls)
self._history.append(call_hash)
del self._history[:-self.window]
count = self._history.count(call_hash)
```

最要紧的是**滑窗**而非全量计数：窗口外的重复会自然衰减出局，"上午查过三次华东、下午正当再查三次"不会被误判成转圈。本体的窗是 20，另有一层"同工具类型频次"检测抓换参数的跨文件轮读，学习版只留指纹层。

**② 就地补配对**（`guard_middlewares.py::DanglingToolCallMiddleware._patch`，骨架）：

```python
patched.append(msg)                     # AIMessage 原样
for tc in (msg.tool_calls or []):
    if tc["id"] not in answered:        # 但它的每个调用必须有回执
        patched.append(ToolMessage(content="[Tool call was interrupted...]",
                                   tool_call_id=tc["id"], status="error"))
```

最要紧的是补丁的**位置**：紧跟在 offending AIMessage 后面，消息的因果序在请求里被复原，而 `request.override` 保证图状态一个字不改。场景② 的自检行（`lost-1 依旧没有回执：True`）就是这条不变量的收据。

**③ 拦下而非执行**（`guard_middlewares.py::ClarificationMiddleware`）：

```python
def wrap_tool_call(self, request, handler):
    if request.tool_call.get("name") != "ask_clarification":
        return handler(request)
    self._interrupt = True
    return ToolMessage(content=f"[向用户提问] {question}", ...)
```

最要紧的是那个没被调用的 `handler`：工具函数里的 `SHOULD-NOT-EXECUTE` 探针值永不出现，是"中间件失守与否"的自证装置——写完护栏不妨先写一个"护栏失效长什么样"的哨兵值，测试即演示。

## 跑起来

```bash
conda run -n deerflow_lab python v09_guardrails/main.py --fake
```

预期输出（确定性，重复运行逐字节一致）：

```
====================================================
场景① LoopDetection：同一调用刷满 5 次，剥爪交卷
====================================================
  >> [LoopDetection] 同一调用出现 3 次 ≥ 3 → 警告排队，随下次请求投递
  >> [LoopDetection] 同一调用出现 5 次 ≥ 5 → 剥掉 tool_calls 强制交卷
  [AIMessage] tool_calls=['calculator']
  [ToolMessage] 35
  [AIMessage] [FORCED STOP] 重复工具调用触顶，请用手头结果直接交卷。
  [stop_reason] loop_capped

====================================================
场景② DanglingToolCall：断头调用在请求边界被补齐
====================================================
  >> [DanglingToolCall] 补合成回执 1 条、丢孤儿回执 1 条（补丁插在 offending AIMessage 正后方，只改本次请求）
  [HumanMessage] 接着上次说，结果呢？
  [AIMessage] 上次的查询因中断没有返回。我已按你的新问题继续。
  [自检] 落盘历史里 lost-1 依旧没有回执（补丁只活在请求里）： True

====================================================
场景③ SafetyFinish：finish_reason 异常时的安全收尾
====================================================
  >> [SafetyFinish] finish_reason='content_filter' 是安全终止 → 剥 tool_calls、追加截断说明（不抛异常）
  [AIMessage] 季度收入的前半段分析：华东同比 +8%，华南

（响应被供应商安全策略截断，以上为已产出的部分，后续内容不可得。）
  [stop_reason] safety_capped

====================================================
场景④ Clarification：信息不足，拦下工具、收束本轮、把问题还给你
====================================================
  >> [Clarification] 拦截 ask_clarification（工具未执行）→ 本轮收束
  [HumanMessage] 查下销售情况。
  [AIMessage] tool_calls=['ask_clarification']
  [ToolMessage] [向用户提问] 要查哪个区域、哪个指标、哪个季度？
  （剧本的第二条消息没有机会执行 —— jump end 生效）

四道护栏各自的现场签名已落盘 data/guardrail_events.txt
```

三处精读：场景① 的两行 `>>` 各自对应 warn/hard 两条阈值线，剧本第 6 条"我不该被看到"永远躺在脚本里没轮到吐出来；场景② 注意补丁只出现在模型请求里——落盘历史仍保留断头原样，修复是**请求级**的临时整形；场景③ 里"剥爪+回填"后 run 以正常终稿收场，调用方全凭 `safety_capped` 知道这不是自然完成。

## Python 小课堂：哈希指纹——把"重复行为"压缩成可数的字符串

```python
import hashlib, json

def fingerprint(calls):        # calls: [{"name":..., "args":{...}}, ...]
    norm = sorted(f"{c['name']}:{json.dumps(c['args'], sort_keys=True)}" for c in calls)
    return hashlib.md5(json.dumps(norm).encode()).hexdigest()[:12]

a = [{"name": "query", "args": {"region": "华东"}}]
b = [{"name": "query", "args": {"region": "华南"}}]
c = [{"name": "query", "args": {"region": "华东"}}]
print(fingerprint(a) == fingerprint(c),   # True  —— 同参数同指纹
      fingerprint(a) == fingerprint(b))   # False —— 换参数就换指纹
```

`sort_keys=True` 消掉 JSON 键序噪音、列表排序消掉并行调用顺序噪音，剩下"行为本身"参与哈希。计数、滑窗、比较全在字符串上做，成本 O(1)。本体在指纹原料上更讲究：read_file 按行号分桶（读 1-100 行与 2-50 行算同一桶）、write_file 却全参数敏感（同路径不同内容是真·不同操作）——**指纹的粗细则决定了检测的误杀率**，这是循环检测真正的工程难点。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 递归上限兜底 | LangGraph 默认 recursion_limit | 不分青红皂白掐整个 run，正常长任务陪葬 |
| 专职循环检测 | DeerFlow / OpenAI Agents SDK guardrail | 按"重复度"定性，warn→hard 两级，先劝后拦 |
| 悬空自动修复 | 多数框架手工处理 | DeerFlow 在请求边界全自动补配对，历史零污染 |

四合一都挂中间件而非塞进 agent 循环，是 DeerFlow"横切进管道"哲学的延续（v05 讲了为什么）；行为护栏与资源护栏、安全护栏共用同一套洋葱圈，装配顺序即优先级（v20 总装见全表）。值得学的一课是四枚中间件共同的**失败观**：护栏触发后被掐断的方向永远朝"多干活"（新工具调用、下一轮模型），从不朝"已完成的成果"——用户已经拿到的信息一个字不清零，交付只是提前发生。粗暴终止和精细护栏的差距，全在这条不对称上。

## 与本体差异（诚实声明）

- 本体 LoopDetection 双层检测（指纹层 + 同工具类型频次层）、per-thread LRU 簿记、BoundedDict、stop_reason 经 consume_stop_reason 弹出；学习版只留指纹层、单 run 现场变量、stop_reason 直接读属性。warn 排队→wrap 投递、hard 剥 tool_calls、滑窗衰减与本体一致。
- 本体 DanglingToolCall 还处理 invalid_tool_calls、缺 id/缺 name 的畸形载荷重编号、additional_kwargs 里的供应商原始 tool_calls；学习版只修两类典型（缺回执补合成、孤儿丢）。"wrap 不 before、位置语义、只改请求不落盘"三条一致。
- 本体 SafetyFinish 由三个可配 detector（OpenAI content_filter / Anthropic refusal / Gemini SAFETY）+ 审计事件 + stop_reason 上下文广播组成，且"有可见文本且无 tool_calls"的终止**不干预**（让半截话自然送达）；学习版检测器合一、审计砍掉，且对空内容同样回填（本体在另一分支回填，语义相同）。
- 本体 Clarification 用 `Command` 中断 + 结构化表单字段（options/input 类型），工具签名带 clarification_type 枚举与多字段 form；学习版只留 question 一参数，中断以 before_model jump end 实现。"工具不执行、提问以 ToolMessage 形态呈现、本轮不再调模型"三条一致。

## 练习

把 `scenario_loop` 剧本里 `range(1, 6)` 改成 `range(1, 4)`（只刷 3 遍），跑 `--fake`：`>> [LoopDetection]` 只剩 warn 一行，"剥掉 tool_calls"行消失，终稿从剧本第 4 条自然交出（场景里能看到 `我不该被看到`）。再改回 5。（自证：`FORCED STOP` 字样从有到无再到有。）

## 下一步

护栏收齐，agent 已经"打不死"。v10 转向能力上限：让模型学会思考（thinking 开关）、睁开眼睛（多模态图片）、按需取用工具（分组延迟加载），再把技能说明书缝进系统提示词。
