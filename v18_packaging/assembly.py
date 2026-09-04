"""反射装配内核 —— 把 config.yaml 的 "module:attr" 字符串换成活对象。
参考本体 packages/harness/deerflow/reflection/resolvers.py（resolve_variable：
同款 "module:attr" 语义、同款缺失依赖提示）与 config/app_config.py（声明式装配）。

本体一条铁律在这里复刻：**配置声明，反射装配，装配前校验**。
- 声明：models/tools/middlewares 三段都是 {name, use, ...参数}；
- 反射：rsplit(":", 1) + import_module + getattr；
- 校验：resolve 之后 isinstance 检查（工具必须是 BaseTool，中间件必须是
  AgentMiddleware，模型必须是 BaseChatModel）——错了在启动期炸，不在运行期炸。
"""

from __future__ import annotations

import os
from importlib import import_module

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

# 模块根 -> pip 包名 的映射（本体同款：报错时顺手告诉你装什么）
MODULE_TO_PACKAGE_HINTS = {
    "langchain_openai": "langchain-openai",
    "langchain_anthropic": "langchain-anthropic",
}


def resolve_variable(path: str, expected_type: type | tuple[type, ...] | None = None):
    """把 "package.module:attr" 换成对象；可选地做类型校验。"""
    try:
        module_path, attr = path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(f"{path!r} 不像变量路径，正确样式: pkg.sub.mod:attr") from err
    try:
        module = import_module(module_path)
    except ImportError as err:
        root = module_path.split(".", 1)[0]
        hint = MODULE_TO_PACKAGE_HINTS.get(root, root.replace("_", "-"))
        raise ImportError(f"无法导入 {module_path}；试 pip install {hint}") from err
    try:
        obj = getattr(module, attr)
    except AttributeError as err:
        raise ImportError(f"模块 {module_path} 里没有 {attr!r}") from err
    if expected_type is not None and not isinstance(obj, expected_type):
        raise TypeError(
            f"{path} 解析出 {type(obj).__name__}，但期望 "
            f"{getattr(expected_type, '__name__', expected_type)}"
        )
    return obj


class ConfigError(Exception):
    """配置校验失败：带条目定位，一条消息说清哪一段哪一条缺什么。"""

    def __init__(self, section: str, name: str, problems: list[str]):
        self.section, self.name, self.problems = section, name, problems
        super().__init__(
            f"config.yaml [{section}] 条目 {name!r} 校验失败:\n  - "
            + "\n  - ".join(problems)
        )


REQUIRED_FIELDS = {
    "models": ["name", "use", "model"],
    "tools": ["name", "use"],
    "middlewares": ["name", "use"],
}

# 各段 resolve 之后的类型契约（本体同款思路：类型错 = 配置错）
TYPE_CONTRACT = {
    "models": BaseChatModel,
    "tools": BaseTool,
    "middlewares": AgentMiddleware,
}


def validate_config(cfg: dict) -> list[str]:
    """静态校验（不 import 任何 use 目标）：返回问题列表，空列表 = 通过。

    只做三类检查：段在不在、条目有没有必填字段、use 长得像不像 module:attr。
    真正 import 是 build_* 的事；但"写法错"没必要等到 import 才发现。
    """
    problems: list[str] = []
    if not isinstance(cfg, dict):
        return ["config.yaml 顶层必须是 mapping"]
    for section, fields in REQUIRED_FIELDS.items():
        entries = cfg.get(section)
        if entries is None:
            problems.append(f"缺少 [{section}] 段")
            continue
        if not isinstance(entries, list) or not entries:
            problems.append(f"[{section}] 必须是非空列表")
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append(f"[{section}] 有条目不是 mapping: {entry!r}")
                continue
            name = entry.get("name", "<无 name>")
            for f in fields:
                if f not in entry:
                    problems.append(f"[{section}] 条目 {name!r} 缺字段 {f!r}")
            use = entry.get("use")
            if isinstance(use, str):
                try:
                    mod, attr = use.rsplit(":", 1)
                except ValueError:
                    problems.append(f"[{section}] 条目 {name!r} 的 use={use!r} 不是 module:attr")
                    continue
                if not mod or not attr:
                    problems.append(f"[{section}] 条目 {name!r} 的 use={use!r} 模块名或属性名为空")
    return problems


def expand_env(value):
    """config.yaml 里的 $VAR 占位 -> 环境变量值（密钥永不进文件）。"""
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        name = value[1:]
        resolved = os.environ.get(name)
        if resolved is None:
            raise RuntimeError(
                f"config.yaml 引用了 {value}，但环境变量 {name} 未设置"
                f"（--fake 模式用不到它：选 --fake 或不装配该条目即可）。"
            )
        return resolved
    return value


def build_model(cfg: dict, name: str | None = None):
    """models 段按 name 选装一条（--fake 选 fake，在线选 demo）。"""
    entries = [m for m in cfg.get("models", []) if name is None or m.get("name") == name]
    if not entries:
        raise KeyError(f"models 段里找不到 {name!r}")
    return build_from_section({"models": entries}, "models")[0]


def build_from_section(cfg: dict, section: str, drop: set[str] | None = None):
    """按段装配：逐条 resolve use，实例化（若解析出的是类），再按契约验货。"""
    drop = drop or {"name", "display_name", "use"}
    expected = TYPE_CONTRACT.get(section)
    built = []
    for entry in cfg.get(section, []):
        name = entry.get("name", "<无 name>")
        obj = resolve_variable(entry["use"])
        kwargs = {k: expand_env(v) for k, v in entry.items() if k not in drop}
        if isinstance(obj, type):           # use 指向类：用剩余字段实例化
            obj = obj(**kwargs)
        elif kwargs and callable(obj) and not isinstance(obj, expected or ()):
            obj = obj(**kwargs)             # use 指向工厂函数：同上
        if expected is not None and not isinstance(obj, expected):
            raise ConfigError(section, name, [
                f"use={entry['use']} 装配出 {type(obj).__name__}，期望 {expected.__name__}"
            ])
        built.append(obj)
    return built
