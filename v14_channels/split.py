"""出站长文切分 —— IM 单条消息有长度上限，长回复必须切着发。

参考本体：backend/app/channels/wechat.py（出站 2000 字分片后逐条发送）
"""

from __future__ import annotations


def split_message(text: str, limit: int = 800) -> list[str]:
    """按段（\\n\\n）贪心装箱；单段超限再按 limit 硬切。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        while len(para) > limit:            # 超长单段：硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:limit])
            para = para[limit:]
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) > limit:
            chunks.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks
