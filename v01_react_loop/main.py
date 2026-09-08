"""v01 · ReAct 循环 —— 智能体的心跳。

运行（仓库根目录）：python -m v01_react_loop.main

观察重点：
1. 工具是普通 Python 函数，@tool 从类型注解生成 schema；
2. 模型自己"点菜"（发起 tool_calls），循环替它"做菜"（执行）并把结果喂回；
3. 只要还有 tool_calls 循环就继续；模型某轮不再点菜，循环自然终止 ——
   终止是模型决定的，不是代码里写死的 if。

参考本体：packages/harness/deerflow/models/factory.py（反射工厂）
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from shared.model_factory import get_model


@tool
def calculator(expression: str) -> str:
    """计算一个算术表达式，例如 (2+3)*7。"""
    return str(eval(expression, {"__builtins__": {}}, {}))   # 教学用：只允许算术


@tool
def query_sales(metric: str, region: str) -> str:
    """查询邮政寄递业务的经营数字。metric 取 revenue(收入) 或 volume(业务量)；region 是区域名，如 华东。"""
    book = {
        ("revenue", "华东"): "42.1亿", ("revenue", "华南"): "28.7亿",
        ("volume", "华东"): "3.12亿件", ("volume", "华南"): "1.98亿件",
    }
    return book.get((metric, region), f"查无 ({metric}, {region}) 的数据")


TOOLS = {t.name: t for t in (calculator, query_sales)}


def main() -> None:
    model = get_model().bind_tools(list(TOOLS.values()))
    question = ("先算华东和华南的收入合计（亿元），再查华东的业务量，"
                "最后告诉我这两个数哪个数字更大。")
    messages = [HumanMessage(question)]
    print(f"[用户] {question}\n")

    for step in range(1, 9):                      # 上限 9 轮：防止模型无限点菜
        ai = model.invoke(messages)
        messages.append(ai)
        if not ai.tool_calls:
            print(f"[轮 {step}] 模型没有再调工具 -> 循环自然终止")
            print(f"\n[终稿] {ai.content}")
            return
        for tc in ai.tool_calls:
            print(f"[轮 {step}] 模型要求 -> {tc['name']}({tc['args']})")
            result = TOOLS[tc["name"]].invoke(tc["args"])
            print(f"          工具返回 -> {result}")
            messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
    print("触发 9 轮上限，强制收场（真生产系统里这是护栏，见 v03）")


if __name__ == "__main__":
    main()
