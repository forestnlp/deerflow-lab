"""模型工厂 —— 参考本体 packages/harness/deerflow/models/factory.py。

本体语义：config.yaml 的 models 段每条是 {name, use: "module:attr", ...}，
工厂用反射把 use 字符串换成 LangChain ChatModel 类，其余字段进构造器。
本件复刻同一语义，只多两件事：$VAR 占位换环境变量；能力档案元字段不进构造器。
"""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = _ROOT / "config.yaml"   # 本地真实配置（gitignore），cp config.example.yaml 而来

# 装配控制字段 + 能力档案声明：给工厂和上层代码看，绝不能进模型构造器
META_FIELDS = {"name", "display_name", "use", "supports_thinking", "supports_vision",
               "thinking", "when_thinking_enabled", "when_thinking_disabled"}


def _expand_env(value):
    """把配置里的 $VAR 占位换成环境变量值（与本体 config 环境语义一致）。"""
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        name = value[1:]
        resolved = os.environ.get(name)
        if resolved is None:
            raise RuntimeError(f"config.yaml 引用了 {value}，但环境变量 {name} 未设置。")
        return resolved
    return value


def get_model(model_name: str | None = None):
    """从仓库根 config.yaml 反射装配真实 ChatModel（本体同款语义）。"""
    cfg = yaml.safe_load(Path(os.environ.get("DFL_CONFIG", CONFIG_PATH)).read_text(encoding="utf-8"))
    models = cfg.get("models", [])
    target = model_name or (models[0].get("name") if models else None)
    entry = next((m for m in models if m.get("name") == target), None)
    if entry is None:
        raise KeyError(f"config.yaml 的 models 段里找不到 {target!r}")
    module_path, attr = entry["use"].split(":")          # 反射：字符串 → 类
    cls = getattr(import_module(module_path), attr)
    kwargs = {k: v for k, v in entry.items() if k not in META_FIELDS}
    return cls(**{k: _expand_env(v) for k, v in kwargs.items()})
