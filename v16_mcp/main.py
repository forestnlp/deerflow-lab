"""v16 · MCP —— 工具从别的进程里"长"出来。

运行：
    conda run -n deerflow_lab python v16_mcp/main.py --fake

观察重点：
1. 主进程 spawn server 子进程，MCP 三板斧（initialize→tools/list→tools/call）逐条打印；
2. tools/list 的 JSON 在运行时变成 LangChain 工具，注入 create_agent；
3. agent 调用远端工具的返回值出现在 ToolMessage 里——数据确实来自另一个进程；
4. 未知工具的报错走 isError（模型可自救），不走协议错误码。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from mcp_adapter import mcp_tools_as_langchain
from mcp_client import MiniMcpClient
from model_factory import fake_model, get_model, tool_call

HERE = Path(__file__).resolve().parent
SERVER = [sys.executable, str(HERE / "server_market.py")]
DATA = HERE / "data"


def reset_data() -> None:
    """运行时产物只写本版 data/，启动时清空（保留 .gitkeep），保证可重复运行。"""
    DATA.mkdir(parents=True, exist_ok=True)
    for stale in DATA.iterdir():
        if stale.name != ".gitkeep":
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()


def fake_script() -> list:
    """--fake 剧本：先查汇率，再统计文本，最后交卷。工具名来自远端，本地没写过。"""
    return [
        tool_call("r1", "query_fx_rate", currency="JPY"),
        tool_call("r2", "text_stats", text="寄递业务量稳步增长"),
        "报告：日元汇率 1 JPY = 147.2 USD；文本统计 chars=9 cjk=9。远端工具全部调用成功。",
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()
    reset_data()

    print("【A】握手与发现：spawn server 子进程，走完 MCP 三板斧的前两板")
    client = MiniMcpClient(SERVER)
    info = client.initialize()
    print(f"  1) initialize -> server={info['serverInfo']['name']} "
          f"protocol={info['protocolVersion']}")
    remote = client.list_tools()
    print(f"  2) tools/list -> {len(remote)} 个远端工具: {[t['name'] for t in remote]}")

    print("\n【B】跨进程反射：远端 inputSchema -> pydantic 模型 -> LangChain 工具")
    tools = mcp_tools_as_langchain(client, data_dir=DATA)
    for t in tools:
        print(f"  动态工具 {t.name}: 字段 = {list(t.args_schema.model_fields)}")  # type: ignore[attr-defined]

    print("\n【C】agent 用「运行时才发现」的工具干活（工具执行真的发生在子进程里）")
    model = fake_model(fake_script()) if args.fake else get_model()
    agent = create_agent(model=model, tools=tools)
    prompt = "查日元汇率，并统计'寄递业务量稳步增长'的字数，最后汇总。"
    print(f"  [用户] {prompt}")
    state = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    for msg in state["messages"]:
        cls = type(msg).__name__
        detail = msg.content or f"tool_calls={[t['name'] for t in msg.tool_calls]}"  # type: ignore[attr-defined]
        print(f"  [{cls}] {detail}")

    print("\n【D】错误分层：未知工具 -> isError=True（走 result，不走协议错误码）")
    text, is_err = client.call_tool("no_such_tool", {})
    print(f"  tools/call no_such_tool -> isError={is_err}: {text}")

    print("\n【E】审计：本版 data/calls.jsonl 记录了每次跨进程调用")
    for line in (DATA / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        print(f"  {line}")

    client.close()
    print("\n收尾：MCP = initialize / tools/list / tools/call 三板斧 + 两层错误。")


if __name__ == "__main__":
    main()
