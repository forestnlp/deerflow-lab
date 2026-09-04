"""本版自带的 MCP server —— 一个"独立可执行文件"，工具在宿主代码里根本不存在。

手动体验它：
    python v16_mcp/server_market.py
    然后敲一行（回车即发送）：
    {"jsonrpc":"2.0","id":1,"method":"tools/list"}

注意：main.py 从头到尾没有 import 过本文件的任何函数——
agent 手里的工具是运行时通过 tools/list 从子进程里"长"出来的（跨进程反射）。
"""

from __future__ import annotations

from mcp_lite import MiniMcpServer

server = MiniMcpServer("market-tools")


@server.tool(
    name="query_fx_rate",
    description="查询指定货币对美元的汇率（学习用静态表，货币如 EUR/JPY/CNY）",
    input_schema={
        "type": "object",
        "properties": {
            "currency": {"type": "string", "description": "货币代码，如 EUR"},
        },
        "required": ["currency"],
    },
)
def query_fx_rate(currency: str) -> str:
    table = {"EUR": 1.09, "JPY": 147.2, "CNY": 7.13, "GBP": 1.26}
    if currency not in table:
        raise ValueError(f"unsupported currency: {currency}")
    return f"1 {currency} = {table[currency]} USD（数据来自 MCP 远端进程）"


@server.tool(
    name="text_stats",
    description="统计一段文本的字符数与中文字符数，返回 JSON 样式字符串",
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "待统计文本"},
        },
        "required": ["text"],
    },
)
def text_stats(text: str) -> str:
    total = len(text)
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return f"chars={total} cjk={cjk}"


if __name__ == "__main__":
    server.serve_stdio()
