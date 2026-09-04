"""v04 沙箱工具 —— 参考本体 packages/harness/deerflow/sandbox/tools.py。

本体签名一律 (runtime, description, path, ...)：runtime 由框架注入携带
thread_data，description 是模型自述"为什么调我"（进执行日志）。学习版把
sandbox 收进闭包，description 保留（这是给模型看的接口设计，不是摆设）。

工具的边界就是"模型的输入进入真实世界"的边界：异常必须变成返回值
（模型看得见、能改正重试），绝不能让整轮 agent 崩掉。本体由
ToolErrorHandlingMiddleware 框架层兜底；v04 还没请中间件，工具自己兜。
"""

from __future__ import annotations

import functools

from langchain.tools import tool

from sandbox import LocalSandbox


def build_tools(sbx: LocalSandbox) -> list:
    """把一个沙箱实例装进五个工具的闭包。"""

    def guard(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 工具边界必须兜住一切异常
                return f"Error: {type(exc).__name__}: {exc}"
        return wrapper

    @tool
    @guard
    def bash(description: str, command: str) -> str:
        """Run a bash command inside the sandbox working directory (/mnt/user-data)."""
        code, output = sbx.execute_command(command)
        return f"[exit {code}]\n{output}" if output else f"[exit {code}] (no output)"

    @tool
    @guard
    def ls(description: str, path: str) -> str:
        """List files under a virtual sandbox path, e.g. /mnt/user-data/outputs."""
        entries = sbx.list_dir(path)
        return "\n".join(entries) if entries else "(empty)"

    @tool
    @guard
    def read_file(description: str, path: str) -> str:
        """Read a text file at a virtual sandbox path (/mnt/user-data/...)."""
        return sbx.read_file(path)

    @tool
    @guard
    def write_file(description: str, path: str, content: str, append: bool = False) -> str:
        """Write text to a virtual sandbox path. Overwriting an existing file
        requires reading it first (READ-BEFORE-WRITE). Max 80KB per call;
        use append=True to extend."""
        return sbx.write_file(path, content, append=append)

    @tool
    @guard
    def str_replace(description: str, path: str, old: str, new: str) -> str:
        """Replace a unique string in a file. Requires reading the file first."""
        return sbx.str_replace(path, old, new)

    return [bash, ls, read_file, write_file, str_replace]
