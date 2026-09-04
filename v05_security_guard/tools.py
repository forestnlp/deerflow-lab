"""v05 的"笨工具" —— 参考本体 packages/harness/deerflow/sandbox/tools.py。

对比 v04：write_file 不再自查 READ-BEFORE-WRITE，read_file 不再记账。
工具只认领身份（从 runtime.state 的 thread_data 拿 user/thread），
只干活，不管纪律。护栏在 guard_middlewares.py 的管道层统一安装。
"""

from __future__ import annotations

import functools

from langchain.tools import ToolRuntime, tool

from sandbox import SandboxProvider


def build_tools(provider: SandboxProvider) -> list:
    def guard(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 工具边界必须兜住一切异常
                return f"Error: {type(exc).__name__}: {exc}"
        return wrapper

    def sandbox_of(runtime) -> SandboxProvider:
        st = runtime.state if runtime is not None else {}
        td = (st or {}).get("thread_data") or {}
        return provider.get(td.get("user_id", "anon"), td.get("thread_id", "default"))

    @tool
    @guard
    def read_file(description: str, path: str, runtime: ToolRuntime = None) -> str:
        """Read a text file at a virtual sandbox path (/mnt/user-data/...)."""
        return sandbox_of(runtime).read_file(path)

    @tool
    @guard
    def write_file(description: str, path: str, content: str, runtime: ToolRuntime = None) -> str:
        """Write text to a virtual sandbox path. (safety enforced by middleware, not here)"""
        return sandbox_of(runtime).write_file(path, content)

    @tool
    @guard
    def bash(description: str, command: str, runtime: ToolRuntime = None) -> str:
        """Run a bash command in the sandbox working directory (/mnt/user-data)."""
        code, output = sandbox_of(runtime).execute_command(command)
        return f"[exit {code}]\n{output}" if output else f"[exit {code}] (no output)"

    return [read_file, write_file, bash]
