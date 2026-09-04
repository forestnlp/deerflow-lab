"""子代理注册表 —— 参考本体 packages/harness/deerflow/subagents/config.py、
subagents/builtins/general_purpose.py（内置类型 + 工具/模型受限）。

一个子代理类型 = 一份"入职合同"：能用哪个模型、能用哪几组工具、
最多转几轮、每一步模拟耗时多少毫秒。task 工具按类型名查这张表。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tools import resolve_groups


@dataclass(frozen=True)
class SubagentConfig:
    name: str
    system_prompt: str
    model: str | None          # None = 继承主模型；否则用受限的"便宜模型"
    groups: list[str] | None   # 允许的工具组；永远不含 task（禁止套娃）
    max_turns: int = 8
    latency_ms: int = 250      # --fake 下每步模型调用的模拟耗时（真实 LLM 本来就有）


# 内置子代理类型（本体 subagents/builtins/ 同构）
REGISTRY: dict[str, SubagentConfig] = {
    "reader": SubagentConfig(
        name="reader",
        system_prompt="你是资料精读员：读原文、给要点，不写文件、不再委派。",
        model="cheap",
        groups=["read", "count"],
    ),
    "writer": SubagentConfig(
        name="writer",
        system_prompt="你是报告撰写员：只根据给定素材落盘报告。",
        model="cheap",
        groups=["write"],
    ),
}


def available_names() -> list[str]:
    return sorted(REGISTRY)


def get_subagent_config(subagent_type: str) -> SubagentConfig | None:
    return REGISTRY.get(subagent_type)


def resolve_tools(cfg: SubagentConfig) -> list:
    """合同 → 工具对象。装配口径里根本没有 task 工具，套娃无从谈起。"""
    return resolve_groups(cfg.groups)
