"""分镜段草稿的确定性校验：画面里不得出现台词字幕 / 说话人名条（把台词文本叠在画面上）。

产品规则（2026-09-14 用户拍板，2026-09-15 收窄）：视频只负责画面与声音，字幕由后期功能另做；
画面本身需要的文字（牌匾、书信、屏幕内容、倒计时、贴图艺术字）由视频模型直接生成，**不拦**。
拦的只有一类：把台词/字幕/说话人名条叠在画面上。「不出字幕」「无字幕」这类约束句的否定
形式不算。命中就报错让模型重写，不做机械改写。
"""
from __future__ import annotations

import re
from typing import Any

_SUBTITLE_RE = re.compile(
    r"(?<!不出)(?<!不叠加)(?<!无)(?<!不加)(?<!不带)(?<!不显示)字幕"
    r"|名条|说话人名|台词文本|台词字样|对白文字"
)


def overlay_text_errors(draft: Any) -> list[str]:
    """prompt_text 里出现「把台词叠在画面上」的措辞即报错；返回空列表表示通过。"""
    prompt = str(getattr(draft, "prompt_text", "") or "")
    hits = sorted({m.group(0) for m in _SUBTITLE_RE.finditer(prompt)})
    if not hits:
        return []
    return [
        "画面里不得出现台词字幕或说话人名条（" + "、".join(f"「{h}」" for h in hits[:4]) + "）："
        "台词只以声音呈现，字幕由后期功能另做。画面本身需要的文字（牌匾、屏幕内容、倒计时、"
        "艺术字）可以写，写清载体与逐字内容即可。"
    ]


__all__ = ["overlay_text_errors"]
