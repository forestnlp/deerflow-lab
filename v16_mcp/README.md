# v16 · MCP：工具从别的进程里"长"出来

> 装个可执行文件，agent 就多会一门手艺。

## 前情提要

v15 让 agent 学会了按排班表自己干活，但翻遍工具箱，还是自家 `tools.py`
里写死的那几个函数。DeerFlow 本体的真实世界里，一大半能力（浏览器自动化、
地图、数据库、截图）来自第三方 MCP server——宿主代码里根本没有它们的函数名，
甚至连它们存不存在都要到运行时才知道。本版把这门"跨进程借工具"的手艺从头
手写一遍：不装任何 MCP 库，从一条条 JSON-RPC 报文开始捏，捏完你就知道
"即插即用"四个字到底贵在哪。

## 前置技术

- **JSON-RPC 2.0**：一套"请求-响应"报文约定：发 `{jsonrpc, id, method, params}`，
  回 `{jsonrpc, id, result}` 或 `{jsonrpc, id, error}`。整个规范的核心就这几行。
- **通知（notification）**：没有 `id` 的请求，发后即忘，对方**不得**回响应——
  这条律法后面会救你一命。
- **stdio 传输**：宿主 spawn 子进程，往它的 stdin 写一行 JSON，从它的 stdout
  读一行 JSON。不需要端口，不需要鉴权，不需要 CORS。
- **MCP（Model Context Protocol）**：Anthropic 2024 年开源的"AI 工具的 USB-C"——
  工具作者写一次 server，任何支持 MCP 的宿主（Claude、DeerFlow、Cursor）都即插即用。
- **inputSchema**：工具清单里那份 JSON Schema，描述了参数名、类型、必填项；
  它最终会原样变成模型看到的"工具说明书"。
- **pydantic `create_model`**：运行时凭空造类的方法，反射三部曲的终点站。

在动手之前值得说清楚"为什么值得手捏一遍"。生产上当然该用官方 SDK——它处理
了流式、并发、取消、跨事件循环这些本版刻意不碰的硬骨头。但 SDK 把这些全
藏进了 async 上下文管理器里，出了 bug 你连断点都下不进去。手捏一遍的收益
不是省一个依赖，而是拿到一张协议地图：以后看到 "-32601"、"isError"、
"protocolVersion 不兼容"，你知道每个症状背后是哪条管道哪个分支在流血。

## 原理：三板斧、两层错误、一条车道

先回答一个自然的问题：为什么是 stdio 而不是 HTTP？因为"装一个 MCP 工具"
这个动作的用户心智就是"本机多了一个可执行文件"。宿主 spawn 它、读它的
stdout、写它的 stdin，全程不需要网络配置——对一个要分发给千万台机器的
插件生态，"零配置"比"高性能"值钱得多。代价是纪律：stdout 是协议专用车道，
日志只能走 stderr，混进一个汉字协议就断；写完必须 flush，否则报文躺在块
缓冲区里，客户端等到天荒地老也不会收到。

一次最小会话只有四种消息，行话叫"三板斧"：

```
 主进程（宿主 / MCP 客户端）              子进程（MCP server）
        │  spawn(python server_market.py)      │
        │══════════════ stdin ══════════════► │
        │  ① initialize               (id=1)  │  互相报协议版本与能力
        │◄═════════ {protocolVersion: 2024-11-05, serverInfo}
        │  ② notifications/initialized        │  ← 通知：无 id，不许回包
        │  ③ tools/list               (id=2)  │  发现：有哪些工具可用
        │◄═════════ [{name, description, inputSchema}, …]
        │  ④ tools/call               (id=3)  │  执行：真函数在对面跑
        │◄═════════ {content:[{type:text,…}], isError:false}
        ▼                                      ▼
   日志只能走 stderr ◄────────────────── 日志走 stderr
```

握手（①②）谈的是"我们能不能说上话"：双方交换协议版本与能力声明，然后
客户端发一条 initialized 通知宣布开工。发现（③）谈的是"你会什么"：返回的
`inputSchema` 接下来会被适配器直接加工成模型可见的工具说明书。执行（④）
谈的是"替我办事"：参数过去，content 数组回来。

错误分层是 MCP 最见功力的设计。协议错误（-32601 没有这个 method、-32700
收到的不是合法 JSON）走 JSON-RPC 的 `error` 字段，语义是"你话都没说对，
我不接"；工具错误（工具名不存在、缺必填参数、函数内部抛异常）走正常
`result` 里的 `isError:true`，那段错误文本是写给模型看的——模型看得懂，
它会自己换工具、补参数或向用户求助。把工具报错做成协议异常，等于剥夺了
模型自救的机会：一次业务失败会炸掉整个 agent 循环。

顺带交代两个工程细节，都是踩过坑的人写进规范的。其一，客户端每发一个请求
`id` 自增，收响应时按 `id` 对号入座——stdio 是有序管道，但协议本身不许你
赌顺序，将来换 HTTP 传输时并发响应就是乱序的，提前按 id 匹配不吃亏。其二，
server 的 `capabilities` 只声明了 `{"tools": {}}`：如实报告能力，客户端才
不会去调你没实现的方法。握手不是走过场，是一次双向的能力谈判。

还有一条隐藏红线藏在 client 的 `close()` 里：先关 stdin 再 wait 进程。
直接 kill 会让 server 在写响应时撞上 broken pipe，日志里糊一片 traceback，
看着像 bug 其实是葬礼没办对。

## 代码精读

`v16_mcp/mcp_lite.py` 的路由器只有四个分支，最见功力的是对通知保持沉默：

```python
if rid is None and str(method).startswith("notifications/"):
    if method == "notifications/initialized":
        self.log("握手完成，进入服务状态")
    return None                          # 通知：不回包

if method == "initialize":
    return result(rid, {"protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": self.name, "version": "0.1.0"}})
if method == "tools/list":
    return result(rid, {"tools": [spec for spec, _ in self._tools.values()]})
if method == "tools/call":
    return self._call_tool(rid, params)
return error(rid, METHOD_NOT_FOUND, f"no such method: {method}")
```

这里最要紧的是头三行的位置——兜底的 `-32601` 必须放在通知拦截之后。
早期实现漏了这一步，客户端会把这条"凭空多出来的响应"错配给下一个在途
请求，症状是"隔一次调用才报错"，查起来能查一晚上。JSON-RPC 规范用
"通知不得有响应"一条律法掐死了这类 bug，读协议规范的价值就在这里。

`v16_mcp/mcp_adapter.py` 是全版的心脏，三十行完成"跨进程反射"：

```python
def build_args_model(tool_name: str, schema: dict) -> type[BaseModel]:
    fields, props = {}, schema.get("properties", {})
    required = set(schema.get("required", []))
    for fname, fmeta in props.items():
        py_type = _JSON_TYPE.get(fmeta.get("type", "string"), str)
        if fname in required:
            fields[fname] = (py_type, Field(description=fmeta.get("description", "")))
        else:  # 可选参数必须给默认值，否则 pydantic 会当成必填
            fields[fname] = (py_type | None,
                             Field(default=None, description=fmeta.get("description", "")))
    return create_model(f"{tool_name}_Args", **fields)
```

这里最要紧的是 `create_model` 这一行：**写代码时没人知道这个类存在**。
它的每个字段、每个类型、每句描述，都是运行时从子进程发来的 JSON 里读出来的。
这是 v01"字符串→类"反射的完全体形态：v01 的反射目标至少在同一个仓库里躺着，
v16 的目标进程可能在你写下这行代码的三年之后才被安装到这台机器上。反射三部曲
至此收线：v01 进程内字符串换对象，v10 装配期构造器注入，v16 运行时跨进程造类。

`v16_mcp/server_market.py` 注册工具的体验和 FastAPI 一模一样：

```python
@server.tool(name="query_fx_rate",
             description="查询指定货币对美元的汇率（学习用静态表）",
             input_schema={"type": "object",
                           "properties": {"currency": {"type": "string",
                                                       "description": "货币代码"}},
                           "required": ["currency"]})
def query_fx_rate(currency: str) -> str:
    ...
```

这里最要紧的是：`main.py` 从头到尾没有 import 过 `server_market.py` 的任何东西。
agent 手里的 `query_fx_rate` 是运行时**发现**的，不是编译期**链接**的。你可以
把 server 换成任何语言写的可执行文件，宿主一行不改——这就是 USB-C 的含金量。

## 跑起来

```bash
conda run -n deerflow_lab python v16_mcp/main.py --fake
```

预期输出（确定性；开头三行是 server 的 stderr 日志，终端里可能与 stdout 交错）：

```
[mcp-server:market-tools] 已启动，等待 stdin 消息（每行一个 JSON-RPC）
[mcp-server:market-tools] 握手：客户端 deerflow-lab 请求 protocol=2024-11-05
[mcp-server:market-tools] 握手完成，进入服务状态
【A】握手与发现：spawn server 子进程，走完 MCP 三板斧的前两板
  1) initialize -> server=market-tools protocol=2024-11-05
  2) tools/list -> 2 个远端工具: ['query_fx_rate', 'text_stats']

【B】跨进程反射：远端 inputSchema -> pydantic 模型 -> LangChain 工具
  动态工具 query_fx_rate: 字段 = ['currency']
  动态工具 text_stats: 字段 = ['text']

【C】agent 用「运行时才发现」的工具干活（工具执行真的发生在子进程里）
  [用户] 查日元汇率，并统计'寄递业务量稳步增长'的字数，最后汇总。
  [HumanMessage] 查日元汇率，并统计'寄递业务量稳步增长'的字数，最后汇总。
  [AIMessage] tool_calls=['query_fx_rate']
  [ToolMessage] 1 JPY = 147.2 USD（数据来自 MCP 远端进程）
  [AIMessage] tool_calls=['text_stats']
  [ToolMessage] chars=9 cjk=9
  [AIMessage] 报告：日元汇率 1 JPY = 147.2 USD；文本统计 chars=9 cjk=9。远端工具全部调用成功。

【D】错误分层：未知工具 -> isError=True（走 result，不走协议错误码）
  tools/call no_such_tool -> isError=True: unknown tool: no_such_tool

【E】审计：本版 data/calls.jsonl 记录了每次跨进程调用
  {"tool": "query_fx_rate", "args": {"currency": "JPY"}}
  {"tool": "text_stats", "args": {"text": "寄递业务量稳步增长"}}

收尾：MCP = initialize / tools/list / tools/call 三板斧 + 两层错误。
```

对输出做三处检查：`ToolMessage` 里的数字必须来自远端表（JPY=147.2 在
server 文件里，不在 main.py 里）；`text_stats` 的 `chars=9` 说明统计发生在
收到参数之后（不是本地瞎猜）；【E】的 jsonl 是每次 tools/call 落下的审计痕迹。

## Python 小课堂：闭包陷阱与默认参数定格

`mcp_adapter.py` 里 `def _run(_client=client, _name=spec["name"], …)` 不是风格
怪癖，是在躲一个经典坑：

```python
funcs = []
for name in ["a", "b"]:
    def bad(): return name          # 闭包按引用捕获变量
    def ok(_n=name): return _n      # 默认参数在定义时刻定格
    funcs.append((bad, ok))
print([f[0]() for f in funcs])      # ['b', 'b']  ← 全指向最后一次迭代
print([f[1]() for f in funcs])      # ['a', 'b']  ← 正确
```

循环变量像一盏公共灯，所有闭包抬头看的是同一盏；默认参数等于给每个闭包
发了张合影，把定义那一刻的值定格下来。写"给每个工具生成一个包装函数"这类
代码时，这是必踩的坑——langchain-mcp-adapters 源码里也有同款处理。

## 与市面对比

| 方案 | 代表 | 与 MCP 的差别 |
|---|---|---|
| 框架私有工具接口 | LangChain Tool、AutoGPT plugin | 每个框架一套接口，工具作者写 N 遍适配 |
| OpenAI function calling | 各家 provider API | 只标准化了"模型怎么表达调用愿望"，不管工具住在哪、谁来执行 |
| MCP | Anthropic、DeerFlow、Cursor 等宿主 | 工具部署与宿主解耦：一次编写，处处即插即用 |

MCP 赌的是生态位：模型接口年年变，工具生态想要一个不随模型更换而报废的
分发格式。这个赌注目前看是押对了。再补一句大实话：MCP 的三板斧没有加密、没有鉴权、没有防重放——它的信任
模型是"本机进程，宿主说了算"。把 MCP server 暴露到网络上（Streamable HTTP
传输）时，这些功课要宿主自己补，本体的 OAuth 模块干的就是这件脏活。协议
把简单留给了 90% 的本机场景，把复杂留给了 10% 的远程场景，这个取舍本身就是
值得抄的设计判断。

## 与本体差异（诚实声明）

- 本体 `mcp/tools.py` 用官方 `mcp` SDK + `langchain-mcp-adapters` 加载工具，学习版
  手写 JSON-RPC 2.0 over stdio——协议消息逐条一致，实现规模差百倍；
- 本体 `mcp/session_pool.py` 维护**常驻会话池**：LRU 逐出、owner-task 生命周期
  （规避 anyio cancel-scope 跨任务崩溃，GitHub issue #3379）、按
  `(server, thread_id)` 作用域共享 server 端状态；学习版只有一条进程内
  同步管道，无池化、无并发会话——"同一 thread 连续调用共享浏览器状态"
  这类语义学习版没有；
- 学习版只实现 tools 能力：resources、prompts、sampling、progress 通知、
  OAuth（本体 `mcp/oauth.py`）、远端工具名净化（防提示词注入）、本地产物
  路径改写（截图文件回收进沙箱）全部省略；
- 本体 stdio server 的 cwd/tmp 被钉死在线程工作区内以便产物可审计，学习版未做；
- `config.yaml` 的 `mcp_servers` 段在交 `build_servers_config` 消费，学习版仅作文档示意。

## 练习

给 `server_market.py` 加第三个工具 `date_diff(start, end)`（返回两日期间隔
天数，格式 `YYYY-MM-DD`），并在 `main.py` 的 `fake_script()` 里插一条对应
tool_call。自证三处：`tools/list` 打印出 3 个工具；`data/calls.jsonl` 出现
第 3 行；消息列表多出一对 AI(tool_calls)+Tool 且终稿引用了天数。

## 下一步

工具能跨进程借了，账单谁来看着？v17 给每个 run 立一本 SQLite 流水账：
每一步花了多久、烧了多少 token、折合成钱是多少——顺带回答"上周三晚上
到底是谁把 API 额度跑光的"。
