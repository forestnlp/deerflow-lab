"""模型工厂 —— 复制自 ../参考件/model_factory.py（v02 自包含副本）。"""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path

import yaml
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

_HERE = Path(__file__).resolve().parent
# 本地 config.yaml（含密钥，不入库）优先；没有则回落 config.example.yaml（--fake 够用）
CONFIG_PATH = _HERE / ("config.yaml" if (_HERE / "config.yaml").exists()
                      else "config.example.yaml")


def _expand_env(value):
    """把配置里的 $VAR 占位换成环境变量值。"""
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        name = value[1:]
        resolved = os.environ.get(name)
        if resolved is None:
            raise RuntimeError(
                f"config.yaml 引用了 {value}，但环境变量 {name} 未设置。"
                f"export {name}=... 后重试（--fake 模式不需要密钥）。"
            )
        return resolved
    return value


def get_model(model_name: str | None = None):
    """从本版 config.yaml 反射加载真实 ChatModel（本体同款语义）。"""
    cfg = yaml.safe_load(Path(os.environ.get("DFL_CONFIG", CONFIG_PATH)).read_text(encoding="utf-8"))
    entry = None
    for m in cfg.get("models", []):
        if model_name is None or m.get("name") == model_name:
            entry = m
            break
    if entry is None:
        raise KeyError(f"models 段里找不到 {model_name!r}")
    module_path, attr = entry["use"].split(":")          # 反射：字符串 → 类
    cls = getattr(import_module(module_path), attr)
    drop = {"name", "display_name", "use"}
    return cls(**{k: _expand_env(v) for k, v in entry.items() if k not in drop})


class FakeToolModel(GenericFakeChatModel):
    """离线脚本模型：bind_tools 忽略绑定，按脚本顺序吐消息。"""

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        nxt = next(self.messages, None)
        if nxt is None:
            nxt = AIMessage(content="（fake 脚本耗尽：正常收尾）")
        return ChatResult(generations=[ChatGeneration(message=nxt)])


def fake_model(script: list) -> FakeToolModel:
    """str → 纯文本 AIMessage（循环在此终止）；AIMessage 原样使用。"""
    msgs = [m if isinstance(m, AIMessage) else AIMessage(content=m) for m in script]
    return FakeToolModel(messages=iter(msgs))


def tool_call(call_id: str, name: str, **args) -> AIMessage:
    """构造带 tool_call 的 AIMessage，方便脚本化演示。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])
