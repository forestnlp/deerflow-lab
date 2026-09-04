# v01 · ReAct 循环：agent 的全部秘密就是一场乒乓

> 模型不会干活，模型只会"说话"。会干活的，是听到它说话后动手的那个循环。

## 前情提要

这是第一版，没有前情。但要预告一个后文：DeerFlow 有 30 多个中间件、8 个渠道、
一整套调度器——它们全部挂在这一版的循环上。地基歪一寸，楼歪一丈。

## 前置技术

- **ChatModel**：一个对象，吃进消息列表，吐回一条 AI 回复。仅此接口，别无所求。
- **tool（工具）**：一个带 docstring 的 Python 函数。docstring 是给模型看的说明书。
- **tool_call**：模型回复里的一种特殊字段——"我想调用 calculator，参数是 …"。
  注意：模型只是**表达愿望**，执行是框架的事。
- **create_agent**：LangGraph 官方的装配函数，一行把"模型+工具"拼成循环。

## 原理：为什么叫"循环"

模型一次调用只能输出一条消息。它想"先查资料再作答"怎么办？框架把
它的话变成行动，把行动结果塞回对话，**再问它一遍**。如此往复，直到它不再点菜：

```
   用户: 算 (2+3)*7 并告诉我现在几点
      │
      ▼
 ┌─ model ──► "调 calculator('(2+3)*7')"   ← 第 1 问
 │    ▲
 │  tool 执行=35，结果塞回对话
 │    │
 │  model ──► "调 get_current_time()"      ← 第 2 问（它现在知道答案=35 了）
 │    ▲
 │  tool 执行=2026年09月04日
 │    │
 └──model ──► "报告：35，今天是…"           ← 不再调工具 = 循环终止
```

历史教训在于终止条件：不是"模型说完了"，而是"**这一条回复里没有 tool_calls**"。
所有 agent 的"自主性"都来自这个 while 循环，没有任何魔法。

## 代码精读

`v01_react_loop/main.py` 核心只有五行：

```python
def build_agent(model):
    return create_agent(model=model, tools=TOOLS)

state = agent.invoke({"messages": [HumanMessage(content=prompt)]})
for msg in state["messages"]:
    print(type(msg).__name__, msg.content or msg.tool_calls)
```

最要紧的是最后一行：**消息列表就是全部真相**。模型的"记忆"、工具的"结果"、
循环的"进度"，全都只是这个列表里的消息。后面每一版往列表里加花样
（todos、摘要、注入的记忆），本质都是在**编排这个列表**。

`tools.py` 里注意 docstring：

```python
@tool
def calculator(expression: str) -> str:
    """计算一个算术表达式，例如 "(2+3)*7"。只支持数字与 +-*/**()。"""
```

说明书写得含糊，模型就用得离谱。本体给模型的每个工具文档都是几十字的
"什么时候用我、参数怎么写"，不是风格洁癖，是**接口设计**。

## 跑起来

```bash
conda run -n deerflow_lab python v01_react_loop/main.py --fake
```

预期输出（确定性，一个字不差）：

```
[HumanMessage] 先算 (2+3)*7，再告诉我现在几点了，最后汇总报告。
[AIMessage] tool_calls=[calculator '(2+3)*7']
[ToolMessage] 35
[AIMessage] tool_calls=[get_current_time ...]
[ToolMessage] 2026年09月04日
[AIMessage] 报告：(2+3)*7 = 35。时间查询已完成，任务结束。
```

## Python 小课堂：反射——把字符串变成类

本项目的 `config.yaml` 写着 `use: "langchain_openai:ChatOpenAI"`，工厂把
字符串换成真类，靠的就是反射：

```python
from importlib import import_module
module_path, attr = "langchain_openai:ChatOpenAI".split(":")
cls = getattr(import_module(module_path), attr)   # 字符串 → 类
model = cls(model="gpt-4o-mini", temperature=0.2)  # 类 → 实例
```

本体全仓库的模型、工具、渠道都是这个套路：**配置声明，反射装配**。
好处是加一个模型/工具不用改一行代码，只改配置。

## 与市面对比

| 做法 | 代表 | 与本版的差别 |
|---|---|---|
| 手写 while 循环 | 最早期 AutoGPT 教程 | 逻辑相同，但消息管理/并行工具/流式全自己扛 |
| create_agent | LangGraph 官方 | 本版的装配层，官方帮你写好循环图 |
| 中间件环绕循环 | DeerFlow / OpenClaw hooks | v02 登场：循环不动，行为可插拔 |

## 与本体差异（诚实声明）

- 本体 `agents/factory.py` 的反射工厂还处理多 provider 修补、thinking 模式，本版只反射一条最直的路；
- 本体工具来自 `config.yaml` 的 `tools:` 声明段，本版工具列表写死在 `tools.py`（v18 会补配置驱动）；
- 本版无流式（stream），一次 `invoke` 拿全量——流式在 v11 作为一等公民登场。

## 练习

给 `tools.py` 加一个 `days_until(date_str)` 工具（返回距目标日期还有几天），
在 `fake_script` 里插一条对应 tool_call，确认 `[ToolMessage]` 出现在预期位置。
（答案特征：消息列表多出一对 AI(tool_calls)+Tool 消息，终稿引用天数。）

## 下一步

循环能转了，但模型开始偷懒：任务做到一半直接"报告完成"。v02 造第一个
中间件，把"不许提前交卷"变成制度。
