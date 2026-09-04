# v07 · 标题与长期记忆：聊完自动起标题，重启还认得你

> 会话会死，记忆不死——它活在磁盘上。

## 前情提要

v06 把大活外包给了子代理，但主 agent 自己仍是"金鱼记忆"：进程一重启，你调岗了、你爱看表格，它全忘光。这一版补两条旁路：TitleMiddleware 在首轮聊完后花一次廉价模型调用给会话起标题（会话列表不再是"未命名对话 17"）；MemoryMiddleware 在对话收尾时把素材交给一个**防抖**的抽取器，事实落到 `data/memory.json`，下次开机由注入中间件塞回模型眼前。

## 前置技术

- **after_model / after_agent**：v03 用过的节点钩子。after_model 每轮模型回复后触发（起标题在这）；after_agent 整个 run 收尾时触发（记忆入队在这）。
- **before_agent 注入**：run 起点把一条隐藏 HumanMessage 追加进历史。和 v02 的 wrap_model_call 临时便签不同，这条会**落盘**——同一 thread 的后续轮次都看得见它。
- **防抖（debounce）**：每次事件到达就把定时器推倒重来，只在"安静了 N 秒"之后才真正执行一次。前端搜索框、resize 事件都用这招，本体记忆抽取同款。
- **旁路（side-channel）**：起标题、抽记忆都是额外的模型调用，但绝不让主对话等它——一个当场只做判断，一个干脆扔给定时器。
- **checkpointer 与记忆的分界**：checkpointer 存"这一世"的完整对话（进程死就没）；memory.json 存"跨世"的事实摘要（文件在就活）。两套存储管两种寿命。

## 原理：为什么记忆必须异步、还只抽"结论"

先想清楚要记什么。一轮工具密集的对话可能有二十条消息，其中十八条是 tool_calls 和工具返回——那是**过程**，模型自己都能重做出来，记下来只会稀释重点。本体 MemoryMiddleware 的 docstring 写得明白：只取用户输入与最终回复。学习版照抄这条纪律：`after_agent` 里只收 HumanMessage 和无 tool_calls 的 AIMessage。

再想清楚什么时候抽。用户连发五句话，若每句都触发一次 LLM 抽取，五次的输出会互相打架（第三次还没抽完第五次又来了），成本和乱序双杀。防抖的答案：**等用户说完了再算总账**。实现朴素得让人怀疑——一个 `threading.Timer`，每次新对话到达就 `cancel()` 重建；安静 0.35 秒（本体默认以秒计），定时器到点，抽取线程开工，主流程早已走远。这带来一个必须直说的语义：**进程若在半路被杀，防抖缓冲里的对话就丢了**——本体的对策是 `shutdown_flush`，收到 SIGTERM 时限时把缓冲冲干净；学习版在 main 结尾照抄这个姿势。

顺带说清一个容易混淆的分工：MemoryInject 与 Memory 是**方向**不同而非生命周期不同——前者是读（磁盘 → 模型眼前），挂在 before_agent，赶在第一次模型调用之前把上一世的功课铺好；后者是写（对话 → 磁盘），挂在 after_agent，等这一世尘埃落定才收拾行李。两枚中间件互不认识，靠 memory.json 这个"信箱"传话，这是旁路系统最常用的解耦法：生产者与消费者不同时在场也不要紧。

起标题是另一个方向的取舍：它**当场**做一次模型调用（用户等着看标题呢），但选最便宜的模型、只喂首条问答各 200 字、失败就地降级为用户消息截断。标题进的是图 state 的自定义键 `title`——和 v05 往 state 里写 `thread_data` 是同一个手法：中间件声明 schema、写键，持久化由 checkpointer 顺手带走，学习版没有会话列表，就在 main 里把 `state["title"]` 打印出来充当"会话列表"。标题生成的判据同样值得抄：恰好一条真实用户消息 + 至少一条 AI 回复才生成——"真实"二字排除了框架注入的提醒消息（学习版按消息 `name` 过滤，本体按 `dynamic_context_reminder` 标记过滤），否则记忆提醒一进历史，就会被当成"用户又说话了"，标题永远生不成。

```
        对话进行中                    安静 0.35s 后              下次开机
  user ──► agent ──► 最终回复        Timer 到点                 新进程读 memory.json
     │      after_agent                  │                          │
     │      enqueue(全量快照)             ▼                          ▼
     └── cancel+重建 Timer ──►  抽取模型: 对话→事实清单      before_agent 注入
                               合并去重 → memory.json      <system-reminder>
                               （主流程零等待 ✓）            <memory><fact>…
```

注入方向还有一个安全细节：事实内容里若混进 `</memory></system-reminder>`，就能伪造闭合、越狱出提醒块。本体的 prompt.py 对注入值统一做转义，学习版照抄——`format_memory_reminder` 先把 `</` 替换成 `<\/`。

## 代码精读

**① 标题的判据与时机**（`title_middleware.py::TitleMiddleware.after_model`，骨架）：

```python
def _should_generate(self, state):
    if state.get("title"):
        return False                      # 一生只起一次
    users = [m for m in messages if self._is_real_user_message(m)]
    ais   = [m for m in messages if isinstance(m, AIMessage) and m.content]
    return len(users) == 1 and len(ais) >= 1
```

最要紧的是"真实用户消息"的过滤：记忆提醒、待办提醒都是框架冒充用户说的话，统计时必须剔除，否则判据永远差一票。

**② 防抖的三行核心**（`memory.py::DebouncedExtractor.enqueue`）：

```python
if self._timer is not None:
    self._timer.cancel()          # 新对话到了，安静期重新计时
self._timer = threading.Timer(self._debounce_s, self._flush)
self._timer.start()
```

最要紧的是**快照覆盖**而非追加：after_agent 每次传的是该 thread 的全量对话，后到的快照必然包住先前的，覆盖即可——追加会把老消息数两遍，抽取器就会把同一件事记两次。

**③ 写方向的克制**（`memory_middleware.py::MemoryMiddleware.after_agent`）：

```python
elif isinstance(m, AIMessage) and not m.tool_calls and m.content:
    ais.append(str(m.content))
...
self._extractor.enqueue(users, ais)
return None                         # 排队不改 state（本体同语义）
```

最要紧的是那个 `return None`：记忆中间件对图状态**零写入**。它是寄生在管道上的旁路，主对话的 state 一个字节都不该为它改变——读方向（注入）例外，因为注入本来就是要让模型看见。

## 跑起来

```bash
conda run -n deerflow_lab python v07_title_memory/main.py --fake
```

预期输出（确定性，重复运行逐字节一致）：

```

[第一轮 · 交代新情况]
  [用户] 我调去经营分析岗了，以后报告都要表格优先，记一下。
  >> [Title] 首轮结束，生成标题：经营口径与表格偏好
  [HumanMessage] 我调去经营分析岗了，以后报告都要表格优先，记一下。
  [AIMessage] tool_calls=['get_note']
  [ToolMessage] 岗位说明：经营分析岗负责月度经营报告的口径与呈现。
  [AIMessage] 收到！你已调至经营分析岗，以后报告表格优先，我记下了。
  [state.title] 经营口径与表格偏好

[第二轮 · 紧接着追问（防抖窗口内）]
  [用户] 顺便想个月度经营报告的提纲方向。
  [HumanMessage] 我调去经营分析岗了，以后报告都要表格优先，记一下。
  [AIMessage] tool_calls=['get_note']
  [ToolMessage] 岗位说明：经营分析岗负责月度经营报告的口径与呈现。
  [AIMessage] 收到！你已调至经营分析岗，以后报告表格优先，我记下了。
  [HumanMessage] 顺便想个月度经营报告的提纲方向。
  [AIMessage] 好，提纲里会给同比对比留出位置。
  [state.title] 经营口径与表格偏好
  >> [Memory] 防抖到期，抽取 2 轮对话 -> 1 条事实

[data/memory.json] ['用户已调至经营分析岗']

———— 模拟重启：新进程、新会话（thread=t-b），checkpointer 全丢 ————

[重启后第一轮 · 试探记忆]
  [用户] 还记得我的偏好吗？给我月度经营报告提纲。
  >> [MemoryInject] 注入长期记忆 1 条 -> <system-reminder>
  >> [Title] 首轮结束，生成标题：月度经营报告提纲
  [HumanMessage] 还记得我的偏好吗？给我月度经营报告提纲。
  [HumanMessage·reminder] <system-reminder>
<memory>
  <fact>用户已调至经营分析岗</fact>
</memory>
</system-reminder>…
  [AIMessage] tool_calls=['get_note']
  [ToolMessage] 偏好备忘：报告优先用表格，一段话讲不清就画表。
  [AIMessage] 当然记得：你在经营分析岗、报告要表格优先。提纲按『总览-分项-同比-结论』四表展开。
  [state.title] 月度经营报告提纲
  >> [Memory] 防抖到期，抽取 1 轮对话 -> 2 条事实

最终记忆（两轮会话共享同一份磁盘文件）:
  · 用户已调至经营分析岗
  · 用户正在准备月度经营报告提纲
```

三处精读：第二轮结束后才出现"防抖到期，抽取 2 轮对话"——两轮的素材被合并成**一次**结算，防抖的铁证；`———— 模拟重启 ————` 之后是全新的 agent、全新的 thread、全新的 checkpointer，`[MemoryInject] 注入长期记忆 1 条` 和模型那句"当然记得"全凭磁盘上那份 memory.json；第二轮没有再生成标题（`state.title` 沿用第一轮），一生一次的判据也在。

## Python 小课堂：contextlib 之外的"延迟结算"原语——Timer

```python
import threading, time

fired = []
timer = None

def arrive(tag):                      # 模拟连续到达的对话
    global timer
    if timer: timer.cancel()          # 推翻上一次的结算承诺
    timer = threading.Timer(0.2, lambda: fired.append(tag))
    timer.daemon = True
    timer.start()

arrive("a"); arrive("b"); arrive("c") # 0.1 秒内三连发
time.sleep(0.35)                      # 安静下来
print(fired)                          # ['c'] —— 三次到达，一次结算
```

`cancel()` 只对还没到点的 Timer 有效，所以"覆盖"语义天然安全。真实系统里防抖后面通常还挂一把锁与一个队列（本体的 debounce queue 就是），但那只解决并发到达，不改变"只结算最后一次快照"的本质。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 全量历史重放 | 无记忆功能的聊天应用 | 每次开机把旧对话整个塞进窗口，贵且不可扩展 |
| 向量库检索记忆 | mem0、LangGraph Memory | 按语义检索相关记忆，规模大时更准，但多一套基础设施 |
| 防抖抽取 + 全量注入 | DeerFlow MemoryMiddleware | 事实以文件为准、开机全注入，量小就是王道，检索是以后的事 |

DeerFlow 把"记忆"定位为**小而稳的事实集**而非大检索库——事实条数有上限、注入格式有外壳、内容有转义，先保证不被污染，再谈变大。标题一项各家的做法其实高度趋同（首轮后一次廉价调用），差别在失败姿势：有的直接留空等用户手改，DeerFlow 选择降级也要给一个，因为会话列表里空标题比丑标题更伤。

## 与本体差异（诚实声明）

- 本体记忆是完整的 MemoryManager + deermem 后端（事实带 id/时间戳/置信度、correction 检测、搜索接口、多租户 user_id）；学习版只留"facts 列表 + 防抖抽取 + 合并去重"，memory.json 结构为学习版自拟。
- 本体注入走 lead_agent 的 system prompt 拼装（`<system-reminder><memory>` 外壳同款、转义纪律同款）；学习版走 before_agent 落盘提醒，与 v03 的 todo_reminder 同路——这是路径差异，注入内容、外壳与转义语义一致。
- 本体标题模型可配（TitleConfig.model_name），未配则用本地降级标题；学习版把标题模型做成构造参数注入，降级路径保留但砍掉 think 标签清洗与异步分支。
- 本体 after_agent 还捕获 user_id/trace_id 随对话入队（后台线程取不到 ContextVar，必须在请求上下文还活着时抓走）；学习版单用户单进程，全部砍掉。
- 学习版标题文案按任务书关键词直接给定（fake 剧本），真实在线路径才走标题模型；本体的 fake 等价物是其测试夹具，生产代码路径一致。防抖合并、只取用户+终稿、旁路零写 state、注入带标记可过滤、退场前 flush 五条机制与本体一致。

## 练习

把 `main.py` 的 `DEBOUNCE_S` 改成 `5`，跑 `--fake`：`[data/memory.json] []`——安静期没到就打印，抽取还没发生；再把 `time.sleep(DEBOUNCE_S + 0.25)` 一并调大，输出恢复原样。（自证：memory.json 行是否在 `防抖到期` 行**之后**出现。）

## 下一步

记忆解决"记得住"，v08 解决"装得下"：上下文窗口不是无底洞，摘要中间件折叠旧消息，token 预算中间件给账单装上两级保险丝。
