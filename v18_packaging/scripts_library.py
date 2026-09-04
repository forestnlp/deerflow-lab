"""--fake 剧本库：与 config.yaml 的 model name 对应，保证输出确定。"""

from __future__ import annotations

from fake_model import tool_call


def _demo_script() -> list:
    return [
        tool_call("c1", "get_indicator", indicator="收入"),
        tool_call("c2", "now", fmt="%Y年%m月"),
        "汇总：H1 寄递收入 42.1 亿元，时间戳已确认。任务完成。",
    ]


SCRIPTS = {"demo": _demo_script()}
