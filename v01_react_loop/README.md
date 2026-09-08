# v01 · ReAct 循环 —— 智能体的心跳

> 会调工具的 LLM，才算上了岗。

## 前情提要

裸 LLM 只会说话，不会做事。让它"查华东收入再算合计"，它要么编一个数，
要么把算术题留给你。本版给它两只手（工具）和一条心跳（循环），看它自己把活干完。

## 前置技术

- **tool calling**：模型回复里可以带一段结构化请求（`tool_calls`），
  内容是"我要调哪个工具、参数是什么"——它只负责点菜，做菜的是你的代码。
- **@tool 装饰器**：把普通 Python 函数变成工具；函数 docstring 和类型注解
  就是模型看到的说明书。
- **消息历史**：对话是一个消息列表，四种角色各司其职——
  `HumanMessage`（人说的）、`AIMessage`（模型说的，可能带 tool_calls）、
  `ToolMessage`（工具结果，用 `tool_call_id` 挂回那次点菜）。
- **反射工厂**：配置里写 `"module:attr"` 字符串，代码用
  `import_module` + `getattr` 把字符串换成类。DeerFlow 本体全系统都靠它装配。

## 原理

ReAct（Reason + Act）不神秘，就是一个 while 循环。模型每轮二选一：
要么点菜（发 tool_calls），要么交卷（纯文本回复）。代码只做一个判断：

```
        ┌───────────────┐
        │  消息历史 → 模型  │
        └───────┬───────┘
        带 tool_calls? 
          │是              │否
   逐个执行工具          终稿到手
   结果以 ToolMessage     循环结束
   追加回历史 ──┐      （终止是模型
                └──↺    自己决定的）
```

要紧的一点：**循环的终止条件是"模型不再点菜"，不是代码写死的步数**。
本轮模型先并行查两个区域的收入，拿到数后又自己发起加法，再补一次业务量查询，
最后才交卷——三步规划全是它自己排的，代码里一个字都没提"先查后算"。

## 代码精读

工具就是带注解的函数（`v01_react_loop/main.py`）：

```python
@tool
def query_sales(metric: str, region: str) -> str:
    """查询邮政寄递业务的经营数字。metric 取 revenue(收入) 或 volume(业务量)；
    region 是区域名，如 华东。"""
    ...
```

说明书（docstring）写得越像给新同事交代工作，模型用得越准。
`metric 取 revenue 或 volume` 这种取值枚举写进去，模型就很少瞎编参数。

循环本体，全部机关在 `if not ai.tool_calls` 一行：

```python
for step in range(1, 9):
    ai = model.invoke(messages)
    messages.append(ai)
    if not ai.tool_calls:            # 模型没点菜 = 交卷
        return
    for tc in ai.tool_calls:
        result = TOOLS[tc["name"]].invoke(tc["args"])
        messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
```

`tool_call_id` 是回执单号：一轮可能有几个点菜，结果必须按号归位，
模型才知道哪个数对应哪道菜。9 轮上限是防呆保险，生产级的护栏在 v03 展开。

## 跑起来

```bash
conda run -n deerflow_lab python -m v01_react_loop.main    # 仓库根目录执行
```

**机制信号**（每次运行都会出现，与模型措辞无关）：

- 至少两轮 `[轮 N] 模型要求 -> query_sales(...)`，且合计一步出现
  `calculator({'expression': '42.1 + 28.7'})`——模型自己决定要算这道加法；
- 最后一行固定是 `[轮 N] 模型没有再调工具 -> 循环自然终止`。

**实录参考**（某次真跑，你的措辞会不同）：

```
[用户] 先算华东和华南的收入合计（亿元），再查华东的业务量，最后告诉我这两个数哪个数字更大。

[轮 1] 模型要求 -> query_sales({'metric': 'revenue', 'region': '华东'})
          工具返回 -> 42.1亿
[轮 1] 模型要求 -> query_sales({'metric': 'revenue', 'region': '华南'})
          工具返回 -> 28.7亿
[轮 2] 模型要求 -> calculator({'expression': '42.1 + 28.7'})
          工具返回 -> 70.8
[轮 2] 模型要求 -> query_sales({'metric': 'volume', 'region': '华东'})
          工具返回 -> 3.12亿件
[轮 3] 模型没有再调工具 -> 循环自然终止
[终稿] …70.8 > 3.12…（还提醒了单位不同不能直接比——这不在剧本里）
```

## Python 小课堂：反射——字符串变类

```python
from importlib import import_module
cls = getattr(import_module("langchain_openai"), "ChatOpenAI")  # 字符串 → 类
model = cls(model="qwen-plus", api_key="...")
```

配置驱动系统的通病是"加功能要改代码"；反射把"用哪个类"下沉到配置文件，
本体新增一个模型接入，Python 代码零改动。

## 与市面对比

| 做法 | 代表 | 差别 |
|---|---|---|
| 手写 ReAct 循环 |  LangChain 0.x `AgentExecutor`、本书本版 | 循环可见，便于理解 |
| 图执行器 | LangGraph `create_agent` | 同样的循环搬进状态图，多出中断/持久化能力（v02 起用） |

## 与本体差异（诚实声明）

- 本体用 `PatchedChatOpenAI`（修 Gemini thought_signature 回传）；本版直连
  `langchain_openai:ChatOpenAI`，机制同款、少了这层补丁。
- 本体的工厂还要处理能力档案（thinking 开关合并 when_* 段）；本版只做
  最小反射 + 元字段剥离（`shared/model_factory.py`）。
- 数据是写死的字典；本体查真库。

## 练习

给 `main.py` 加第三个工具 `convert_unit(value: float, src: str, dst: str)`
（亿元↔万元即可），把提问改成"把华南收入换算成万元"。
**自证**：运行后能看到 `模型要求 -> convert_unit(...)`，且终稿数字 = 工具返回。

## 下一步

循环能跑但裸奔：没有计划约束、没有护栏、模型偷工减料没人管。
v02 给循环装上"钩子"——中间件，看 DeerFlow 一系横切能力长在哪。
