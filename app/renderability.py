"""Renderability First：剧本/分镜适配视频模型能力边界。

对应 PRD《剧本分镜主线压缩与视频能力适配方案》。
校验拦超纲细节与主线断裂，不拦「空泛」。
"""
from __future__ import annotations

import re

# 单集镜头软预算（情密章可上浮至 HARD）
SHOT_SOFT_MIN = 8
SHOT_SOFT_MAX = 16
SHOT_HARD_MAX = 20

SPINE_BEATS_MIN = 5
SPINE_BEATS_MAX = 12
DROP_LIST_MIN = 2

KEY_LINES_MIN = 3
KEY_LINES_MAX = 6
# KEY_LINES_MAX 是整集“精选台词”的软预算，不应被误用为单条连续对白链的
# 硬上限。问答/安慰这类完整语义链允许略长，但仍保留一个防止模型灌水的硬闸。
DIALOGUE_CHAIN_TURNS_HARD_MAX = 8
KEY_PLOT_POINTS_MIN = 4
KEY_PLOT_POINTS_MAX = 8

# action_desc：单主动作、可读大形体，禁止写细堆砌
ACTION_DESC_HARD_MIN = 18
ACTION_DESC_TARGET_MIN = 25
ACTION_DESC_TARGET_MAX = 55

SCENE_OUTLINE_MIN = 3
SCENE_OUTLINE_MAX = 5

# 超纲词表（命中 → 校验失败；修复方向是删细节，不是补细）
OVERDETAIL_TERMS: tuple[str, ...] = (
    "微微",
    "轻轻颤抖",
    "泪珠",
    "眼泪",
    "泪水",
    "指节",
    "衣角",
    "发丝",
    "瞳孔",
    "嘴角",
    "绣纹",
    "纹理",
    "逐个",
    "同时说道",
    "分屏",
    "闪回",
)

_RENDERABILITY_PROMPT_BLOCK = """【Renderability First·视频模型能力边界】
你在为 AI 视频短剧写作，不是写话剧精排场刊。当代视频模型画不稳微表情、复杂手指与群戏。
稳定可做：1~2 个主体的大形体（走/停/转身/伸手/开口）、单句短对白、大方向情绪（怒/惊/冷/喜）、一次简单道具接触、固定或轻推运镜。
禁止写入：微表情/微动作、手部精细、材质服饰堆砌、同镜多节拍、群戏轮流说话、小字长文、抽象文学比喻。
禁止用词示例：微微、轻轻颤抖、泪珠/眼泪、指节、衣角、发丝、瞳孔、嘴角、绣纹、纹理、逐个、同时说道、分屏、闪回。
修复方向永远是删除超纲细节、合并碎镜、回到主线骨架——禁止「写得更细」。"""


def renderability_prompt_block() -> str:
    return _RENDERABILITY_PROMPT_BLOCK


def find_overdetail_hits(text: str | None) -> list[str]:
    """返回文本中命中的超纲词（去重、保序）。"""
    raw = text or ""
    if not raw:
        return []
    hits: list[str] = []
    for term in OVERDETAIL_TERMS:
        if term in raw and term not in hits:
            hits.append(term)
    return hits


def overdetail_errors(text: str | None, field_path: str) -> list[str]:
    hits = find_overdetail_hits(text)
    if not hits:
        return []
    shown = "、".join(hits[:8])
    extra = f"（另有 {len(hits) - 8} 个从略）" if len(hits) > 8 else ""
    return [
        f"{field_path} 含超纲细节词：{shown}{extra}；"
        "请删除微表情/手指/衣褶/材质级描写，只保留大形体可读动作，不要改写得更细"
    ]


def strip_overdetail_terms(text: str) -> str:
    """编译层第二道闸：剥离超纲词（根治仍在上游合同）。"""
    out = text or ""
    for term in OVERDETAIL_TERMS:
        out = out.replace(term, "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"。{2,}", "。", out)
    return out.strip()


def shot_count_budget_errors(n_shots: int, *, context: str = "分镜") -> list[str]:
    """单集镜头软预算：>16 进入 repair；>20 硬失败。"""
    errors: list[str] = []
    if n_shots > SHOT_HARD_MAX:
        errors.append(
            f"{context}共 {n_shots} 镜，超过硬上限 {SHOT_HARD_MAX}；"
            f"请合并反应镜、删除 drop_list/非 must_keep 支线，压回 {SHOT_SOFT_MAX} 镜以内，禁止继续拆碎"
        )
    elif n_shots > SHOT_SOFT_MAX:
        errors.append(
            f"{context}共 {n_shots} 镜，超过软预算 {SHOT_SOFT_MAX}（目标约 {SHOT_SOFT_MIN}~{SHOT_SOFT_MAX}）；"
            "请合并相邻反应镜或压缩对白，不要再拆新镜"
        )
    return errors


# 单镜默认时长：主线压缩后优先 5s；>5 仅当口播/动作确实放不下，且进入 AI 审核标记。
PREFERRED_SHOT_DURATION_S = 5
DURATION_REVIEW_RISK_TAG = "duration_gt5_needs_review"


def episode_target_from_spine(spine_beat_count: int) -> int:
    """按主线节拍估算集目标时长（秒）：约 1 beat ≈ 1~2 镜 × 5s，落入产品档位。"""
    from app import config

    n = max(0, int(spine_beat_count or 0))
    if n <= 0:
        return config.EPISODE_TARGET_DEFAULT_S
    raw = n * 2 * PREFERRED_SHOT_DURATION_S
    raw = max(config.EPISODE_TARGET_MIN_S, min(80, raw))
    step = config.EPISODE_TARGET_STEP_S
    rounded = ((raw + step // 2) // step) * step
    return min(config.EPISODE_TARGET_MAX_S, max(config.EPISODE_TARGET_MIN_S, rounded))


def shot_duration_should_prefer_five(*, spoken_chars: int, action_beats: int) -> bool:
    """口播与动作都装得进 5s 时，应压回默认 5s，不必拉长。"""
    from app import config

    return (
        spoken_chars <= config.max_spoken_chars_for_duration(PREFERRED_SHOT_DURATION_S)
        and max(1, int(action_beats or 1)) <= 1
    )


def duration_gt5_errors(
    *,
    shot_no: int,
    duration_s: int,
    spoken_chars: int,
    action_beats: int,
) -> list[str]:
    """超过 5s 且内容装得进 5s → 硬拦；真正需要更长则留给 AI 审核标记。"""
    if int(duration_s or 0) <= PREFERRED_SHOT_DURATION_S:
        return []
    if shot_duration_should_prefer_five(spoken_chars=spoken_chars, action_beats=action_beats):
        return [
            f"shots shot_no={shot_no} duration_s={duration_s} 过长：口播与单主动作均可在 "
            f"{PREFERRED_SHOT_DURATION_S}s 内完成；请改回 {PREFERRED_SHOT_DURATION_S}s。"
            f"仅当口播超 {PREFERRED_SHOT_DURATION_S}s 预算或确需连续铺陈时才取 6~10s，并接受 AI 审核"
        ]
    return []
