"""视频候选的预览与合成倍速合同。"""
from __future__ import annotations

import math

MIN_PLAYBACK_RATE = 0.5
MAX_PLAYBACK_RATE = 2.0
DEFAULT_PLAYBACK_RATE = 1.0


def normalize_playback_rate(value: object) -> float:
    """返回可持久化倍速；拒绝 NaN、无穷与过激值。"""
    if value is None or value == "":
        return DEFAULT_PLAYBACK_RATE
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("视频倍速必须是 0.5 到 2.0 之间的数字") from exc
    if not math.isfinite(rate) or not MIN_PLAYBACK_RATE <= rate <= MAX_PLAYBACK_RATE:
        raise ValueError("视频倍速必须在 0.5 到 2.0 之间")
    return round(rate, 2)
