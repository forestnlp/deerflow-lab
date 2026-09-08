# v02 · 中间件与规划 —— 循环一行没改，制度插上了

> 好员工不是催出来的，是制度管出来的。

## 前情提要

v01 的 while 循环能干活，但裸奔：模型不列计划、干一半就交卷，都没人管。
往循环里塞检查代码？DeerFlow 有 20 多个横切能力，全塞进循环就成了意大利面。
本版给出框架的答案：中间件——循环不动，制度外挂。

## 前置技术

- **create_agent**：LangGraph 官方的 agent 构造器，把 v01 那个循环装进状态图，
  顺便开出"可以在固定位置插代码"的口子。
- **AgentMiddleware**：中间件基类。钩子分两类——
  `wrap_model_call`/`wrap_tool_call` 是**洋葱型**（包在调用外，可改请求、可否决）；
  `before_model`/`after_model` 等是**节点型**（在图的固定位置改 state）。
- **jump_to**：`after_model` 返回 `{"jump_to": "model"}` 把执行流弹回模型节点，
  相当于"这卷不收，拿回去重做"。
- **state**：agent 的共享账本。`messages` 只是其中一个键，
  中间件可以往里加自己的键（本版的 `todos` 就是）。

## 原理

中间件的执行次序是套娃，不是排队。列表里靠前的包着靠后的：

```
middleware=[OnionLogger, TodoMiddleware]

  ┌─ OnionLogger.wrap_model_call ─────────┐
  │  ┌─ TodoMiddleware.wrap_model_call ─┐ │
  │  │        （真正的模型调用）          │ │
  │  └──────────────────────────────────┘ │
  └───────────────────────────────────────┘
```

所以本体的 `build_middlewares` 里 append 的顺序是**设计**，不是随手写的：
谁先看得到消息、谁能否决谁，全由套娃次序决定。

规划制度（`todo_middleware`）用一个工具两个钩子管住"偷懒"：

```
 模型调 write_todos 列计划 ──→ todos 进 state（账本）
 模型想交卷（无 tool_calls 的回复）
        │
   after_model 查账：todos 有未销账的？
        │是                         │否
   jump_to=model 打回重做        放行交卷
   （上限 2 次，防死循环）
```

关键洞察：**`write_todos` 这个工具压根不在我们的 TOOLS 里**——是
`TodoListMiddleware` 注入的。工具都能注入，可见中间件的权限有多大。

## 代码精读

洋葱圈最简形态，一行代码不加、只旁观记录（`v02_middleware_plan/middlewares.py`）：

```python
class OnionLogger(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        self.trace.add(f"model 进（历史 {len(request.messages)} 条）")
        response = handler(request)      # 不调 handler 就直接短路，模型根本不会被调
        calls = [tc["name"] for tc in (response.result[0].tool_calls or [])]
        self.trace.add(f"model 出 -> {'调 ' + ','.join(calls) if calls else '交卷'}")
        return response
```

防提前交卷是全部机关，一个 `after_model` 讲完：

```python
@hook_config(can_jump_to=["model"])
def after_model(self, state, runtime):
    last_ai = 最后一条 AIMessage
    if not last_ai or last_ai.tool_calls:  return None   # 还想调工具，不算交卷
    todos = state.get("todos") or []
    if not todos or 全部 completed:         return None   # 干完了，放行
    if self._count >= MAX_COMPLETION_REMINDERS: return None  # 防死循环
    self._count += 1
    return {"messages": [催办消息], "jump_to": "model"}     # 打回重做
```

催办走 `HumanMessage(name="todo_reminder")` 而不是 SystemMessage——
严格网关要求每个请求里必有 user 消息，折叠成 SystemMessage 会 400
（No user query found）。这是在线实跑踩出来的坑，v04 摘要同一招。

## 跑起来

```bash
conda run -n deerflow_lab python -m v02_middleware_plan.main    # 仓库根目录执行
```

**机制信号**（每次运行都会出现）：

- trace 第 2 行固定是 `model 出 -> 调 write_todos`——该工具不在 TOOLS 里，
  是中间件注入的铁证；
- trace 成对出现 `tool 进/出`，且每次工具调用前必有一行 `model 进（历史 N 条）`，
  N 递增——洋葱圈 + 历史滚雪球；
- 收尾 `[todos]` 里所有条目 `status: completed`。

**实录参考**（某次真跑；模型若中途偷懒，还会多出一行
`[Todo] 还有未销账 todo，拦截提前交卷 #1 -> jump_to=model`）：

```
  [trace] 01 model 进（历史 1 条）
  [trace] 02 model 出 -> 调 write_todos
  [trace] 03   tool 进 write_todos
  ...
  [trace] 21 model 进（历史 11 条）
  [trace] 22 model 出 -> 交卷
[todos] [{'content': '查询上半年寄递业务量数据', 'status': 'completed'},
         {'content': '撰写分析报告并给出结论', 'status': 'completed'}]
```

## Python 小课堂：装饰器就是"中间件"

```python
def onion(f):
    def wrapped(x):
        print("进")        # wrap 的前半：改请求
        r = f(x)           # handler(request)
        print("出")        # 后半：观答复
        return r
    return wrapped
```

`wrap_model_call` 本质就是把这个 print 换成正经逻辑：改 prompt、记日志、
直接返回假结果否决本次调用。v01 的 Python 课是反射，v02 是装饰器——
框架把这两件朴素武器组装成了插件系统。

## 与市面对比

| 做法 | 代表 | 差别 |
|---|---|---|
| 循环里写死检查 | 早期 AgentExecutor 回调 | 能力和循环耦死，加一个改一次 |
| 中间件洋葱圈 | LangChain 1.x middleware、DeerFlow、各类 Web 框架 | 循环稳定，能力可插拔、顺序即语义 |

DeerFlow 本体的 20+ 中间件与 FastAPI 的中间件栈是同一个思想在两个领域的投影。

## 与本体差异（诚实声明）

- 学习版催办消息直接进历史落盘；本体把催办"只随下次请求投递、不落盘"
  （wrap_model_call 里临时拼），用户翻聊天记录看不到催办痕迹。
- 本体 TodoMiddleware 还有多 run 并发的线程安全簿记；学习版单 run 单实例，去掉了。
- 断点续做（todos 滑出上下文后靠 state 恢复）依赖摘要压缩，v04 讲摘要时再演示。

## 练习

把 `main.py` 里 middleware 列表顺序换成 `[TodoMiddleware(trace), OnionLogger(trace)]`，
预测 trace 第 1 行变不变，再实跑验证。
**自证**：洋葱外圈先"进"——换序后若 trace 首行仍是 `model 进`，说明 OnionLogger
还在外层？动手发现真相（提示：看谁的 wrap 先被调用）。

## 下一步

会规划了，但模型现在拿到了 write_file 和 bash——把 `/etc/passwd` 读出来、
把密钥 echo 进日志，都只是一次"正常的工具调用"。v03 给它建监狱：沙箱与护栏。
