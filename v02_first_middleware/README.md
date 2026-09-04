# v02 · 第一个中间件：循环一动不动，行为插上了管

> 加功能不必给病人开刀，装个接口就行。

## 前情提要

v01 把 model ↔ tool 的乒乓循环转起来了，但那次装配一行写死：`create_agent(model, tools)`。想给每次模型调用记日志、给每个工具把关，总不能钻进框架肚子里改代码。DeerFlow 全仓库有 30 多枚中间件，这一版先手写最小的一枚，把管道的形状、接法、脾气摸熟。

## 前置技术

- **create_agent**：把模型+工具拼成 LangGraph 循环的官方装配函数，v01 已见过它的庐山真面目。
- **hook（钩子）**：框架在执行链路的关键位置预留的固定缺口，供外部代码插入。缺口开在哪、能改什么，决定了中间件的能力边界。
- **wrap_model_call / wrap_tool_call**：分别包住"每一次模型调用"和"每一次工具调用"的洋葱圈钩子。方法体内先改请求、再调 `handler(request)` 放行、后看结果；不调 handler 就是短路拦截。
- **before/after_agent、before/after_model**：四个节点型钩子，运行在图节点边界上，返回字典就是给共享 state 打补丁。本版先不碰，v03、v05 各用一个。
- **洋葱模型**：多个包裹者嵌套执行——外层先进后出，内层后进先出。谁包谁由 `middleware=[...]` 列表顺序决定，越靠前越靠外。
- **横切关注点**：日志、计费、护栏这类散落在每个业务路径里的需求，集中到一处实现、对业务代码零侵入，就叫"横切"。

## 原理：为什么要有中间件

把 agent 想成一条流水线：请求从 model 节点出发去问模型，工具结果从 tool 节点回来续写历史。业务代码（prompt、工具函数）只管生产，但审计、限流、兜底、注入提醒这些"管纪律的"需求，遍布在每一个生产环节上。如果让纪律代码直接写进业务，等于让保安替厨师颠勺——每个岗位都要会所有手艺，改一处动全身。

中间件制度的做法是：**在流水线上设检查站**。每个检查站都能干三件事——放行前改包（`request.override(...)`，只对通过的这份请求生效）；拦截（不叫 handler，直接伪造一个返回值塞回去，业务方甚至不知道发生过什么）；放行后记账（读结果、写日志、计费）。三件事，就够了。剩下的想象力全部用于组合。

有检查站就有先后，先后就是语义。外层检查站贴上的封条，内层看得见；反过来则看不见。所以同两枚中间件，`[A, B]` 与 `[B, A]` 是两个不同的系统。框架把列表合成为一个嵌套函数——`factory.py` 里那个 `composed` 函数逐层把 handler 往里递——于是列表顺序直接等于洋葱圈从外到内的圈层顺序。本体 `agent.py::build_middlewares` 里二十来步 append 的先后——先注入日期与记忆、再做上下文摘要、后挂护栏与限流——每一条都是踩坑定下来的制度，绝非随手排列。

```
middleware = [Logger, Sticker, Peek]          请求方向 ──►

  用户消息 ─► ┌─ Logger ──────────────────────┐
              │  ┌─ Sticker：贴上隐藏便签 ──┐ │   洋葱圈：
              │  │  ┌─ Peek：便签可见✓ ──┐  │ │   先进后出，
              │  │  │ （模型真正收到请求）│  │ │   后进先出
              │  │  └────────────────────┘  │ │
              │  └──────────────────────────┘ │
              └───────────────────────────────┘
  换成 [Logger, Peek, Sticker]：Peek 先于便签看到请求 → 便签可见✗
```

还有一处容易被跳过的装配细节：`create_agent(..., middleware=[A, B, C])` 拿到列表后，从最内层往外逐层合成——C 的 handler 是真正调模型的那个函数，B 包 C，A 包 B。所以**列表第一个元素拥有最大的改写权重**：它最先看到原始请求、也最后经手返回结果。写中间件团队规约时这是必答题：谁的钩子必须先跑？本体的回答散落在 `build_middlewares` 的注释里——比如"系统消息合并"必须排在所有注入系统消息的中间件之后、贴着模型之前，否则各家 provider 对多条 SystemMessage 的耐受度不同，炸在网关上就查不清是谁引入的。

## 代码精读

**① 最小洋葱圈**（`v02_first_middleware/middlewares.py::OnionLogger`）：

```python
class OnionLogger(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        self._trace.add(f"model IN  {len(request.messages)} 条消息")
        result = handler(request)   # 不调这行 = 短路，模型根本不会被问
        self._trace.add("model OUT")
        return result

    def wrap_tool_call(self, request, handler):
        name, cid = request.tool_call["name"], request.tool_call["id"]
        self._trace.add(f"tool  IN  {name}#{cid}")
        result = handler(request)
        self._trace.add(f"tool  OUT {name}#{cid}")
        return result
```

这里最要紧的是：agent 循环一行没改、工具函数一行没改，可观测性却全部到手——模型被问了几次、每次带几条消息、哪些工具在何时执行，全部落进流水账。这就是横切关注点的正确寄放处，本体的 token 计费、错误兜底、审计中间件全是这个形状，只是往账本里记的东西不同。

**② 改请求，不落盘**（`middlewares.py::NoteSticker`）：

```python
class NoteSticker(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        note = HumanMessage(content="【便签】别忘了收尾。", name="note",
                            additional_kwargs={"hide_from_ui": True})
        return handler(request.override(messages=[*request.messages, note]))
```

最要紧的是 `request.override(...)` 这七个字符：便签只进**这一次请求**，图state里的会话历史查无此条。本体 TodoMiddleware 的"不许提前交卷"提醒（本体 `todo_middleware.py::TodoMiddleware.wrap_model_call`）正是同款手法——纪律要让模型听见，但不必让用户看见，更不该污染存档。

**③ 顺序敏感的证据**（`v02_first_middleware/main.py::run_round`）：同一枚 `NotePeek` 检查器，只汇报两个事实——流经本层的消息条数、便签是否可见。第一轮把它放在贴便签的**内侧**，看到 `便签可见: True`；第二轮挪到**外侧**，看到 `False`。两轮的工具结果与终稿一字不差，变的只有中间件列表顺序。这就是"顺序即语义"的可观测证据。

另一个细节在 `middlewares.py::Trace.render()`：一条 AI 消息里的两个 tool_calls 由线程池并行执行，日志行的落笔先后看调度脸色。渲染时把连续的 tool 行按调用 id 与 IN/OUT 归一化排序，model 行保持原序——--fake 输出逐字节可复现，而"谁包着谁"的事实一个字节不改。

## 跑起来

```bash
conda run -n deerflow_lab python v02_first_middleware/main.py --fake
```

预期输出（确定性，一个字不差；末尾还会打印 `data/run.log`，内容与两段 trace 相同）：

```
[用户] 用 echo 工具分别回显 A 和 B，然后收尾。

--- 轮次：中间件顺序 = [洋葱日志, 贴便签, 检查器]（便签在外层） ---
    [trace] 01 model IN  1 条消息
    [trace] 02 peek      2 条消息，便签可见: True
    [trace] 03 model OUT
    [trace] 04 tool  IN  echo#c1
    [trace] 05 tool  OUT echo#c1
    [trace] 06 tool  IN  echo#c2
    [trace] 07 tool  OUT echo#c2
    [trace] 08 model IN  4 条消息
    [trace] 09 peek      5 条消息，便签可见: True
    [trace] 10 model OUT
  工具结果: ['echo: A', 'echo: B']
  终稿: 两项回显完成，任务结束。

--- 轮次：中间件顺序 = [洋葱日志, 检查器, 贴便签]（便签在内层） ---
    [trace] 01 model IN  1 条消息
    [trace] 02 peek      1 条消息，便签可见: False
    [trace] 03 model OUT
    [trace] 04 tool  IN  echo#c1
    [trace] 05 tool  OUT echo#c1
    [trace] 06 tool  IN  echo#c2
    [trace] 07 tool  OUT echo#c2
    [trace] 08 model IN  4 条消息
    [trace] 09 peek      4 条消息，便签可见: False
    [trace] 10 model OUT
  工具结果: ['echo: A', 'echo: B']
  终稿: 两项回显完成，任务结束。
```

读输出时注意两处：`04–07` 一行 IN 配一行 OUT，说明一条 AI 消息里的两个 tool_calls 在同一轮全部执行、各自被洋葱圈包过——这就是工具调用合并；`08 model IN 4 条消息`比 `01` 多出的三条正是 AI(tool_calls) 加两条 Tool 结果——历史变长，模型才有"下一步"的依据。

**④ 钩子的能力边界**：把三套钩子排个队。`wrap_*` 系能改单次请求与结果，但看不到图 state 全貌；`before/after_model` 能读写 state，却插不进"模型调用"这件事的中间；`before/after_agent` 只在 run 首尾各跑一次。为什么需要三套而不是无敌的一套？因为权限越大的钩子越难优化——框架知道 wrap 系是纯包裹，可以放心做成函数嵌套、零节点开销；state 钩子涉及通道合并，必须落成图节点。选钩子，本质是在"我要的权限"与"框架能省的开销"之间对号入座。本版只用 wrap 系，v03 起三种轮番上场。

## Python 小课堂：闭包手搓洋葱圈

框架把中间件列表合成为一个嵌套函数，靠的是闭包——内层函数"记得"外层递进来的 handler：

```python
def onion(tag, inner):
    def run(x):
        print(f">> {tag}")
        r = inner(x)          # handler：把请求继续往里递
        print(f"<< {tag}")
        return r
    return run

core = lambda x: f"答复({x})"
run = onion("外", onion("内", core))
print(run("hi"))    # >> 外  >> 内  << 内  << 外  答复(hi)
```

`wrap_model_call` 里你手写的 `handler(request)`，就是这个 `inner(x)`。所谓洋葱架构，拆开看不过是一串"记得下一跳"的闭包。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 散装回调 | 早期 agent 教程的 pre/post hook | 钩子各管一段、彼此不知深浅，没有统一的嵌套顺序可言 |
| Web 中间件 | FastAPI / Express | 思想同源，那些包的是 HTTP 请求，这里包的是模型与工具调用 |
| AgentMiddleware | LangChain 1.x 官方 | 本版的钩子形状即其正主；DeerFlow 在这套管道上排了 30+ 枚 |

## 与本体差异（诚实声明）

- 本体 `build_middlewares` 按配置与运行模式装配二十余枚中间件，顺序牵动摘要、记忆、护栏语义；本版只有 3 枚手写件，演示"顺序敏感"这条原理，不是本体的完整链条。
- 本体中间件类来自 `config.yaml` 声明并反射加载；本版在 `main.py` 里直接实例化，配置驱动留到 v18 补。
- `Trace.render()` 对并行 tool 行按 id 归一化排序是学习版自拟，为的是讲义输出逐字节可复现；本体日志不做这种归一化。
- 便签文案为学习版中文自拟；本体提醒是固定英文模板（`todo_middleware.py::_format_completion_reminder`）。

## 练习

给 `middlewares.py` 加一枚 `Veto`：在 `wrap_tool_call` 里，若 `request.tool_call["args"]["text"] == "B"`，**不调 handler**，直接 `return ToolMessage(content="拒绝", tool_call_id=request.tool_call["id"])`（记得从 `langchain_core.messages` 导入 `ToolMessage`）。把它挂在轮次 1 列表的**最前面**（洋葱日志之前）再跑——对错自证：工具结果变为 `['echo: A', '拒绝']`，且轮次 1 的 trace 里 `echo#c2` 的 IN/OUT 两行整体消失（否决发生在洋葱日志之外，日志层根本不知道发生过这次调用）。

## 下一步

洋葱圈有了，可模型开始耍滑头：todo 列到一半就宣布"任务完成"。v03 用 `after_model` + `jump_to` 把提前交卷变成制度性不可能。
