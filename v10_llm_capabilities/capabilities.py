"""v10 核心零件：thinking 开关、多模态消息、技能装载 —— 三样"感知与装备"能力。

参考本体
  config/model_config.py
      supports_thinking / when_thinking_enabled / when_thinking_disabled / thinking
      —— thinking 参数不硬编码在调用处，而是配置声明的模型档案字段，装配时合并。
  tools/builtins/view_image_tool.py、uploads/manager.py
      上传文件→图片内容块→随消息发给支持视觉的模型。
  skills/parser.py、skills/catalog.py、agents/lead_agent/prompt.py
      技能 markdown（frontmatter: name/description + 正文）解析成目录，
      拼进 system prompt 让模型"读过说明书再上岗"。

主题聚合声明：本体没有 agents/llm_capabilities.py 这个文件（讲义差异节如实写明），
本文件是三块本体机制的学习版汇集点。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# ---------------------------------------------------------- ① thinking 开关


def model_kwargs(model_name: str, *, thinking_enabled: bool) -> dict:
    """按 config 档案算出"这次装配该给模型类传哪些 kwargs"。

    真实路径：get_model 把这些 kwargs 喂给 ChatOpenAI(...)；
    --fake 路径：main 打印本函数的返回，证明开关确实由 config 驱动——
    开关不改机制，只换配置里的一个布尔。
    """
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    entry = next(m for m in cfg["models"] if m.get("name") == model_name)
    # 档案元字段（供装配器读的声明）不透传给模型类 —— 本体 create_chat_model 同法
    archival = {"name", "display_name", "use", "supports_thinking",
                "when_thinking_enabled", "when_thinking_disabled", "thinking"}
    merged = {k: v for k, v in entry.items() if k not in archival}
    supports = bool(entry.get("supports_thinking"))
    if thinking_enabled and not supports:
        raise ValueError(f"模型 {model_name!r} 档案里 supports_thinking=false，不能开 thinking")
    if thinking_enabled:
        merged.update(entry.get("thinking") or {})        # 简写：thinking == enabled 支的快捷方式
        merged.update(entry.get("when_thinking_enabled") or {})
    else:
        merged.update(entry.get("when_thinking_disabled") or {})
    return merged


# ---------------------------------------------------------- ② 多模态消息

_MAGIC = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".gif": "image/gif", ".webp": "image/webp"}


def build_multimodal_message(text: str, image_path: Path) -> HumanMessage:
    """图片文件 → image content block，和文字块拼成一条 HumanMessage。

    block 形状就是发给供应商前的最终形状（--fake 下结构自检、不真调 API）。
    """
    media_type = _MAGIC.get(image_path.suffix.lower(), "application/octet-stream")
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return HumanMessage(content=[
        {"type": "text", "text": text},
        {"type": "image",
         "source": {"type": "base64", "media_type": media_type, "data": data,
                    "provider": {"file_provider": "file"}}},
    ])


def describe_message_blocks(msg: HumanMessage) -> list[str]:
    out = []
    for block in msg.content:
        if isinstance(block, dict) and block.get("type") == "image":
            src = block["source"]
            out.append(f"image({src['media_type']}, {len(src['data'])}B b64)")
        elif isinstance(block, dict):
            out.append(f"text({str(block.get('text', ''))[:18]}…)"
                       if len(str(block.get("text", ""))) > 18
                       else f"text({block.get('text', '')})")
        else:
            out.append(f"raw({str(block)[:18]})")
    return out


# ---------------------------------------------------------- ③ 技能装载


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str


def load_skills(skills_dir: Path) -> list[Skill]:
    """data/skills/*.md：--- frontmatter --- 正文（本体 skills/parser.py 同款极简版）。"""
    skills = []
    for path in sorted(skills_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        name, description, body = path.stem, "", text
        if text.startswith("---"):
            _, fm, rest = text.split("---", 2)
            meta = yaml.safe_load(fm) or {}
            name = str(meta.get("name", name))
            description = str(meta.get("description", ""))
            body = rest.strip()
        skills.append(Skill(name=name, description=description, body=body))
    return skills


def compose_system_prompt(base: str, skills: list[Skill]) -> str:
    """技能全文拼进 system prompt —— 模型上岗前先读说明书。"""
    parts = [base, "\n\n# 已装载技能"]
    for s in skills:
        parts.append(f"\n## {s.name}\n{s.description}\n\n{s.body}")
    return "".join(parts)
