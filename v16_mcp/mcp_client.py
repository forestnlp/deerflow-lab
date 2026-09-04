"""MCP 客户端：spawn server 子进程 -> stdio 握手 -> tools/list -> tools/call。
参考本体 packages/harness/deerflow/mcp/session_pool.py（本体用官方 SDK 维护
跨事件循环的常驻会话池；学习版是同步阻塞的一问一答，差异见 README）。

传输层与协议层刻意分开：
  传输层 = _send/_recv（管道上的一问一答，换个 transport 不用改上面）
  协议层 = initialize/list_tools/call_tool（MCP 三板斧）
"""

from __future__ import annotations

import json
import subprocess

from mcp_lite import notification, request


class MiniMcpClient:
    """与 MiniMcpServer 通过子进程 stdin/stdout 对话的同步客户端。"""

    def __init__(self, argv: list[str], env: dict[str, str] | None = None):
        # bufsize=1 行缓冲 + 手动 flush：双向管道不 flush 等于不说话
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self._rid = 0

    # ---- 传输层：一问一答 ----
    def _send(self, msg: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _recv(self) -> dict:
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("server 关闭了 stdout（进程可能崩了）")
        return json.loads(line)

    def call(self, method: str, params: dict | None = None) -> dict:
        self._rid += 1
        self._send(request(self._rid, method, params))
        reply = self._recv()
        if "error" in reply:  # JSON-RPC 层错误（协议级，不是工具级）
            raise RuntimeError(f"JSON-RPC error {reply['error']['code']}: {reply['error']['message']}")
        return reply["result"]

    # ---- 协议层：MCP 三板斧 ----
    def initialize(self, client_name: str = "deerflow-lab") -> dict:
        info = self.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "0.1.0"},
        })
        self._send(notification("notifications/initialized"))  # 通知：不等回复
        return info

    def list_tools(self) -> list[dict]:
        return self.call("tools/list")["tools"]

    def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        out = self.call("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(c["text"] for c in out.get("content", []) if c.get("type") == "text")
        return text, out.get("isError", False)

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait(timeout=5)
