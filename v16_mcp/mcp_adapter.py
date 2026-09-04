"""跨进程反射适配器：远端 inputSchema -> pydantic 模型 -> LangChain 工具。
参考本体 packages/harness/deerflow/mcp/tools.py（用 langchain-mcp-adapters 的
load_mcp_tools 做同一件事；学习版手写 40 行，机制一致，规模差百倍）。

这是 v01 "字符串→类"反射的完全体形态：
  v01:  config.yaml 的 use: "module:attr" —— 进程内，写代码时类已存在
  v16:  tools/list 返回的 JSON            —— 跨进程，写代码时这个类根本不存在
pydantic.create_model 在运行时凭空造出一个带校验的类，它的每个字段都来自
服务器发来的 JSON。也就是说：远端 server 的 inputSchema，变成了模型看到的
工具说明书。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, create_model

from langchain_core.tools import StructuredTool

from mcp_client import MiniMcpClient

_JSON_TYPE = {"string": str, "number": float, "integer": int, "boolean": bool}


def build_args_model(tool_name: str, schema: dict) -> type[BaseModel]:
    """inputSchema(JSON) -> pydantic 模型（运行时凭空造出一个类）。"""
    fields: dict[str, tuple] = {}
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    for fname, fmeta in props.items():
        py_type = _JSON_TYPE.get(fmeta.get("type", "string"), str)
        if fname in required:
            fields[fname] = (py_type, Field(description=fmeta.get("description", "")))
        else:  # 可选参数必须给默认值，否则 pydantic 当成必填
            fields[fname] = (py_type | None, Field(default=None, description=fmeta.get("description", "")))
    return create_model(f"{tool_name}_Args", **fields)


def mcp_tools_as_langchain(client: MiniMcpClient, data_dir=None) -> list[StructuredTool]:
    """拉取远端工具清单，现场组装成 LangChain 工具（.call() 通过管道打 RPC）。"""
    tools = []
    for spec in client.list_tools():
        args_model = build_args_model(spec["name"], spec.get("inputSchema") or {"properties": {}})

        # 闭包绑定当前循环变量：直接引用 spec 是经典坑（全部工具会共享最后一次
        # 迭代的值），用默认参数在定义时刻"定格"。
        def _run(_client=client, _name=spec["name"], _dir=data_dir, **kwargs) -> str:
            args = {k: v for k, v in kwargs.items() if v is not None}
            if _dir is not None:  # 演示要求：调用痕迹只写本版 data/
                _write_audit(_dir, _name, args)
            text, is_err = _client.call_tool(_name, args)
            return f"[MCP 工具报错] {text}" if is_err else text

        tools.append(StructuredTool(
            name=spec["name"],
            description=spec.get("description", ""),
            args_schema=args_model,
            func=_run,
        ))
    return tools


def _write_audit(data_dir, name: str, args: dict) -> None:
    """每次 tools/call 往本版 data/calls.jsonl 追记一行，证明它真发生过。"""
    import json
    from pathlib import Path

    path = Path(data_dir) / "calls.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": name, "args": args}, ensure_ascii=False) + "\n")
