"""成本核算 —— 单价表来自 config.yaml，token 没报就自己估。
参考本体：本体由 TokenUsageMiddleware 记录真实 usage_metadata（packages/harness/
deerflow/agents/middlewares/token_usage_middleware.py），再交计费侧核算；
学习版没有真实 usage 可依赖（fake 模型不报数），自拟一个确定性的估算器。

估算规则（写死，保证 --fake 输出可复现）：
  一个中文字符 ≈ 1 token；其余字符 4 个 ≈ 1 token。
这是粗估，但它满足观测的第一原则：**先有数，再谈准**。
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    rest = len(text) - cjk
    return cjk + max(1, rest // 4)


class CostCalculator:
    """按单价表算钱：cost = pt/1000*单价 + ct/1000*单价。"""

    def __init__(self, per_1k_prompt: float, per_1k_completion: float):
        self.pp = per_1k_prompt
        self.pc = per_1k_completion

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        return prompt_tokens / 1000 * self.pp + completion_tokens / 1000 * self.pc
