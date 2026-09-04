"""v10 的工具箱：按 config 声明分组，default 组先上岗，其余组等 load_tools 放行。

参考本体
  config/tool_config.py（工具即配置声明）
  agents/middlewares/deferred_tool_filter_middleware.py
      DeferredToolFilterMiddleware：wrap_model_call 从绑定给模型的 tools 清单里
      滤掉未提升（deferred）的工具 schema；wrap_tool_call 拦住对未提升工具的
      调用（回一句"先 tool_search 提升再试"）。ToolNode 仍持有全部工具用于路由，
      "看不见"的是 schema，不是执行能力。
  tools/builtins/tool_search.py（本体按关键词检索并提升；学习版简化为按组提升）
"""

from __future__ import annotations

from pathlib import Path

import yaml
from langchain.tools import tool
from langchain_core.messages import ToolMessage

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# config 的 tools 段：name → group。装配后 _BY_NAME 提供 name → 工具对象。
GROUPS: dict[str, list[str]] = {}
_BY_NAME: dict[str, object] = {}
_promoted: set[str] = set()          # 除 default 外已放行的组


def _register(tool_obj) -> None:
    _BY_NAME[tool_obj.name] = tool_obj


@tool
def query_sales(metric: str, region: str) -> str:
    """查指定区域的销售指标。metric 如 revenue/volume，region 如 华东/华南。"""
    return f"{region} {metric} = 42.1 亿（演示数据）"


@tool
def calculator(expression: str) -> str:
    """计算算术表达式，例如 "(2+3)*7"。只支持数字与 +-*/**()。"""
    allowed = set("0123456789+-*/.()% ")
    if not set(expression) <= allowed:
        return "拒绝执行：表达式含非法字符（只允许数字与 +-*/.()% 空格）"
    return str(eval(expression, {"__builtins__": {}}, {}))


@tool
def word_count(text: str) -> str:
    """统计文本字符数（word 组成员，需先 load_tools("word")）。"""
    return str(len(text))


_register(query_sales)
_register(calculator)
_register(word_count)


def load_registry() -> dict[str, list[str]]:
    """读 config 的 tools 段并校验每个名字都有实现。返回 group → [工具名]。"""
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = {}
    for entry in cfg.get("tools", []):
        name, group = entry["name"], entry.get("group", "default")
        if name not in _BY_NAME:
            raise KeyError(f"config 声明了工具 {name!r}，但没有实现")
        groups.setdefault(group, []).append(name)
    GROUPS.clear()
    GROUPS.update(groups)
    return GROUPS


def tools_for_groups(groups: list[str]) -> list:
    return [_BY_NAME[n] for g in groups for n in GROUPS.get(g, [])]


def visible_groups() -> list[str]:
    return ["default"] + sorted(_promoted)


def visible_tool_names() -> list[str]:
    return [t.name for g in visible_groups() for t in tools_for_groups([g])]


def reset_promotion() -> None:
    _promoted.clear()


@tool
def load_tools(group: str) -> str:
    """按组放行延迟加载的工具。group 只能是 default 之外的组名（见 config tools 段）。"""
    if group == "default":
        return "default 组本来就可见，无需放行。"
    if group not in GROUPS:
        return f"没有 {group!r} 组。可用组：{', '.join(g for g in GROUPS if g != 'default')}。"
    if group in _promoted:
        return f"{group} 组已放行过。"
    _promoted.add(group)
    names = ", ".join(GROUPS[group])
    return f"{group} 组已放行，新增可用：{names}。下次模型调用起生效。"


def block_unpromoted(request) -> ToolMessage | None:
    """未放行组的工具被硬调 → 否决（本体 DeferredToolFilter.wrap_tool_call 同款）。"""
    name = request.tool_call.get("name")
    impl = _BY_NAME.get(name)
    if impl is None or not GROUPS:
        return None
    group = next((g for g, names in GROUPS.items() if name in names), "default")
    if group == "default" or group in _promoted:
        return None
    return ToolMessage(
        content=f"Error: Tool '{name}' belongs to group '{group}' which is not loaded yet. "
                f"Call load_tools('{group}') first, then retry.",
        tool_call_id=str(request.tool_call.get("id", "")),
        name=name, status="error")


_register(load_tools)

TOOL_OBJECTS = [query_sales, calculator, word_count, load_tools]
