"""v10 · LLM 能力 —— 会思考、能看图、按需取工具、读过说明书再上岗。

运行：
    conda run -n deerflow_lab python v10_llm_capabilities/main.py --fake
    # 在线：export DFL_BASE_URL=... DFL_API_KEY=... 后去掉 --fake

观察重点（--fake 全程零 API 调用，只验结构与配置）：
1. thinking 开关由 config 的三支字段驱动，打印两次装配算出的 kwargs 差异；
2. 图片路径 → image content block，结构自检（media_type + base64 长度）；
3. 第一轮模型只看得见 default 组；硬调 math 组工具被否决；load_tools 放行后
   第二轮的可见清单里才出现 calculator；
4. data/skills/*.md 解析后全文拼进 system prompt（长度与技能名可核对）。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, SystemMessage

import tools as tool_registry
from capabilities import (build_multimodal_message, compose_system_prompt,
                          describe_message_blocks, load_skills, model_kwargs)
from deferred_tool_filter_middleware import DeferredToolFilterMiddleware
from model_factory import fake_model, get_model, tool_call

DATA = Path(__file__).resolve().parent / "data"
SKILLS = DATA / "skills"
UPLOADS = DATA / "uploads"

# 图片是 1x1 透明 PNG 的最小合法字节序列（演示结构用，不含业务信息）
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffffff7f030007060502fee3f2c00000000049454e44ae426082")

SKILL_FILES = {
    "report-tone.md": (
        "---\nname: 报告语气\ndescription: 经营报告的措辞与口径纪律。\n---\n"
        "结论先行，数字必带单位与同比；不确定的口径写进脚注而不是含糊其辞；"
        "禁用'大幅''明显'等无量化副词。"),
    "table-first.md": (
        "---\nname: 表格优先\ndescription: 呈现形式的选择规则。\n---\n"
        "三个以上可比对象一律表格；表格必须有表头与单位；跨期对比表按时间升序。"),
}


def reset_data_dir() -> None:
    """运行时产物只写本版 data/，启动时清空（保留 .gitkeep，重建输入）。"""
    if DATA.exists():
        for p in DATA.iterdir():
            if p.name != ".gitkeep":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    SKILLS.mkdir(parents=True, exist_ok=True)
    for name, text in SKILL_FILES.items():
        (SKILLS / name).write_text(text, encoding="utf-8")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / "chart.png").write_bytes(_PNG_1PX)


def step(n: int, title: str) -> None:
    print("\n" + "=" * 52)
    print(f"步骤 {n} · {title}")
    print("=" * 52)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fake", action="store_true", help="离线脚本模型，零 API key")
    args = ap.parse_args()

    reset_data_dir()
    groups = tool_registry.load_registry()
    tool_registry.reset_promotion()
    print("config 声明的工具分组:", {g: names for g, names in sorted(groups.items())})

    # ---- ① thinking 开关：config 驱动，两种状态两套 kwargs ----
    step(1, "thinking 开关（config 的三支字段 → 模型 kwargs）")
    off = model_kwargs("demo", thinking_enabled=False)
    on = model_kwargs("demo", thinking_enabled=True)
    print(f"  thinking=False -> kwargs = {off}")
    print(f"  thinking=True  -> kwargs = {on}")
    print("  （在线路径这两套分别喂给 ChatOpenAI(...)；--fake 下只验配置读取）")

    # ---- ② 多模态：图片路径 → image content block ----
    step(2, "多模态消息结构（不真调 API，只验消息形状）")
    msg = build_multimodal_message("这张趋势图里哪个月跌得最狠？", UPLOADS / "chart.png")
    print(f"  content blocks = {describe_message_blocks(msg)}")
    print(f"  第2块 source.type = {msg.content[1]['source']['type']}")

    # ---- ③ 技能注入：data/skills/*.md → system prompt ----
    step(3, "技能注入（frontmatter 解析 + 全文拼进 system prompt）")
    skills = load_skills(SKILLS)
    for s in skills:
        print(f"  技能 {s.name}: {s.description}（正文 {len(s.body)} 字）")
    system_prompt = compose_system_prompt("你是邮政经营分析助手。", skills)
    print(f"  system prompt 总长 {len(system_prompt)} 字符，含技能名: "
          f"{[s.name for s in skills]}")
    (DATA / "system_prompt.txt").write_text(system_prompt, encoding="utf-8")

    # ---- ④ 工具分组延迟加载：藏 schema → 否决硬调 → 放行 → 再可见 ----
    step(4, "工具分组延迟加载（default 先上岗，load_tools 再放行）")
    guard = DeferredToolFilterMiddleware()
    script = [
        tool_call("m1", "calculator", expression="(2+3)*7"),        # math 组：还没放行
        tool_call("m2", "load_tools", group="math"),                 # 放行 math 组
        tool_call("m3", "calculator", expression="(2+3)*7"),         # 现在能算了
        "结论：(2+3)*7 = 35；先放行再调用，两步完成。",
    ]
    model = fake_model(script) if args.fake else get_model()
    agent = create_agent(
        model=model,
        tools=tool_registry.TOOL_OBJECTS,   # ToolNode 持全量：藏的是 schema 不是执行能力
        middleware=[guard],
    )
    state = agent.invoke({"messages": [
        SystemMessage(content=system_prompt),
        msg,
        HumanMessage("算 (2+3)*7，然后给结论。"),
    ]})
    for m in state["messages"]:
        cls = type(m).__name__
        if isinstance(m, SystemMessage):
            print(f"  [{cls}] {str(m.content)[:38]}…（{len(str(m.content))} 字符）")
        elif isinstance(m, HumanMessage) and isinstance(m.content, list):
            print(f"  [{cls}] {describe_message_blocks(m)}")
        else:
            detail = m.content or f"tool_calls={[tc['name'] for tc in m.tool_calls]}"
            print(f"  [{cls}] {str(detail)[:96]}")

    print(f"\n  各次模型调用看到的工具清单（schema 过滤的直接证据）:")
    for i, names in enumerate(guard.seen_tool_names, 1):
        print(f"    第{i}次: {names}")
    print(f"  最终放行组: {tool_registry.visible_groups()}")
    print(f"  system prompt 落盘 data/system_prompt.txt（{len(system_prompt)} 字符）")


if __name__ == "__main__":
    main()
