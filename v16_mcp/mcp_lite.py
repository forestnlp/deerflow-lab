"""MCP 协议库 —— 手写 JSON-RPC 2.0 over stdio，零第三方依赖。
参考本体 packages/harness/deerflow/mcp/tools.py（本体走官方 mcp SDK + langchain-mcp-adapters，
学习版把协议本身捏一遍，差异见 README【与本体差异】）。

一次最小 MCP 会话（stdio 传输）只有四种消息：
  1. initialize                  握手：互相报协议版本与能力
  2. notifications/initialized   确认开工（通知：没有 id，不许回包）
  3. tools/list                  发现工具：name/description/inputSchema
  4. tools/call                  远程执行：返回 content 数组 + isError

为什么是 stdio 而不是 HTTP？装一个 MCP 工具 = "本机有一个可执行文件"。
宿主 spawn 它、读它的 stdout、写它的 stdin——不需要端口、鉴权、跨域。
代价是纪律：stdout 是协议专用通道，日志只准走 stderr；写完必须 flush。
"""

from __future__ import annotations

import json
import sys

JSONRPC = "2.0"
PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC 2.0 规范固定的标准错误码（背下来能排障）
PARSE_ERROR = -32700        # 收到的不是合法 JSON
METHOD_NOT_FOUND = -32601   # 没有这个 method


def request(rid: int, method: str, params: dict | None = None) -> dict:
    msg: dict = {"jsonrpc": JSONRPC, "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def notification(method: str, params: dict | None = None) -> dict:
    """通知 = 没有 id 的请求：发后即忘，对方不得回响应。"""
    msg: dict = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def result(rid, payload: dict) -> dict:
    return {"jsonrpc": JSONRPC, "id": rid, "result": payload}


def error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": JSONRPC, "id": rid, "error": {"code": code, "message": message}}


class MiniMcpServer:
    """stdio MCP 服务端：stdin 逐行读 JSON-RPC，stdout 逐行写回复。"""

    def __init__(self, name: str):
        self.name = name
        self._tools: dict[str, tuple[dict, object]] = {}

    def tool(self, name: str, description: str, input_schema: dict):
        """装饰器注册工具——和 FastAPI 的 @app.get 同一个体验。"""
        def deco(fn):
            self._tools[name] = (
                {"name": name, "description": description, "inputSchema": input_schema},
                fn,
            )
            return fn
        return deco

    def log(self, text: str) -> None:
        # 铁律：日志只走 stderr。stdout 混进一个汉字，协议就断了。
        print(f"[mcp-server:{self.name}] {text}", file=sys.stderr, flush=True)

    def handle(self, msg: dict) -> dict | None:
        method, rid = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}

        # JSON-RPC 铁律：通知（无 id）一律不回响应。
        # 若对 notifications/* 回了个 -32601，客户端会把这条"凭空多出的响应"
        # 错配给下一个在途请求——新手最难查的一类 bug。
        if rid is None and str(method).startswith("notifications/"):
            if method == "notifications/initialized":
                self.log("握手完成，进入服务状态")
            return None

        if method == "initialize":
            client = (params.get("clientInfo") or {}).get("name", "?")
            self.log(f"握手：客户端 {client} 请求 protocol={params.get('protocolVersion')}")
            return result(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},   # 只声明 tools（真实 MCP 还有 resources/prompts）
                "serverInfo": {"name": self.name, "version": "0.1.0"},
            })

        if method == "tools/list":
            return result(rid, {"tools": [spec for spec, _ in self._tools.values()]})

        if method == "tools/call":
            return self._call_tool(rid, params)

        return error(rid, METHOD_NOT_FOUND, f"no such method: {method}")

    def _call_tool(self, rid, params: dict) -> dict:
        """MCP 分层错误约定：未知工具/缺参/工具内部爆炸都不是协议错误（-326xx），
        而是 isError=True 的正常 result——那段错误文本是给模型看的，它能自救。"""
        name = params.get("name", "")
        entry = self._tools.get(name)
        if entry is None:
            return result(rid, {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
                                "isError": True})
        spec, fn = entry
        args = params.get("arguments") or {}
        missing = [k for k in spec["inputSchema"].get("required", []) if k not in args]
        if missing:
            return result(rid, {"content": [{"type": "text",
                                             "text": f"missing required args: {missing}"}],
                                "isError": True})
        try:
            out = fn(**args)
            return result(rid, {"content": [{"type": "text", "text": str(out)}], "isError": False})
        except Exception as exc:  # noqa: BLE001  工具崩了也要有体面地报告给模型
            return result(rid, {"content": [{"type": "text", "text": f"tool error: {exc}"}],
                                "isError": True})

    def serve_stdio(self) -> None:
        self.log("已启动，等待 stdin 消息（每行一个 JSON-RPC）")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = error(None, PARSE_ERROR, "invalid JSON")
            else:
                reply = self.handle(msg)
            if reply is not None:                # 通知没有回复
                sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                sys.stdout.flush()               # 不 flush = 客户端永远等不到（块缓冲第一坑）
