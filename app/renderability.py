"""Renderability First：剧本/分镜适配视频模型能力边界。

对应 PRD《剧本分镜主线压缩与视频能力适配方案》。
校验拦超纲细节与主线断裂，不拦「空泛」。
"""
from __future__ import annotations

import math

# 镜头数量由剧情交付和场景上下文决定，不设产品软/硬上限。
# ``None`` is retained as an explicit compatibility marker for older callers.
SHOT_HARD_MAX: int | None = None

SPINE_BEATS_MIN = 1
SPINE_BEATS_MAX: int | None = None
DROP_LIST_MIN = 0

KEY_LINES_MIN = 3
# 对白数量不设固定上限；整集预算由口播时长和单链技术熔断共同约束。
DIALOGUE_CHAIN_TURNS_HARD_MAX = 8


def dialogue_turn_speech_acts(turns: list) -> list[list]:
    """把话轮列表按「一次发言」分组。

    一句台词会被 `_split_spoken_line` 按单镜口播容量
    （`MAX_SPOKEN_CHARS_PER_SHOT`）**有意**切成多段，每段都登记成独立
    `KeyDialogueTurn`——这是 `DIALOGUE_TURN_CAPACITY_EXCEEDED` 这条硬门禁
    要求的（单轮超过 36 字必拒）。但「话轮」在
    `DIALOGUE_CHAIN_LENGTH_INVALID` 那里是语义概念：谁开口说了一次。
    同一说话人、同一 `source_text` 的连续片段属于同一次发言。

    两条门禁数的是同一个字段的两种不同单位，所以任何按数量切分的地方
    都必须用这个函数，不能直接数 `len(turns)`。
    """
    acts: list[list] = []
    previous_key: tuple[str, str] | None = None
    for turn in turns:
        key = (
            str(getattr(turn, "speaker", "") or "").strip(),
            str(getattr(turn, "source_text", "") or "").strip(),
        )
        # source_text 为空时无法证明同源，一律各自成一次发言（保守）。
        if previous_key is not None and key == previous_key and key[1]:
            acts[-1].append(turn)
        else:
            acts.append([turn])
        previous_key = key
    return acts


def chunk_dialogue_turns(
    turns: list,
    *,
    limit: int = DIALOGUE_CHAIN_TURNS_HARD_MAX,
) -> list[list]:
    """按 `limit` 切分话轮，且**不在一次发言中间切开**。

    先分组成发言，再贪心装箱。只有当**单独一次发言**本身就超过 `limit`
    时才退化为硬切——那种输入下任何切法都会切进一次发言内部，
    此时保证长度上限比保证发言完整更重要（长度是硬门禁，会拒稿）。
    """
    if limit < 1:
        raise ValueError("limit 必须为正整数")
    chunks: list[list] = []
    current: list = []
    for act in dialogue_turn_speech_acts(turns):
        if len(act) > limit:
            if current:
                chunks.append(current)
                current = []
            for offset in range(0, len(act), limit):
                chunks.append(act[offset:offset + limit])
            continue
        if len(current) + len(act) > limit:
            chunks.append(current)
            current = []
        current.extend(act)
    if current:
        chunks.append(current)
    return chunks
KEY_PLOT_POINTS_MIN = 1
KEY_PLOT_POINTS_MAX: int | None = None

# action_desc：单主动作、可读大形体，禁止写细堆砌
ACTION_DESC_HARD_MIN = 18
ACTION_DESC_TARGET_MIN = 25
ACTION_DESC_TARGET_MAX = 55

SCENE_OUTLINE_MIN = 1
SCENE_OUTLINE_MAX: int | None = None
SCENE_STORY_FUNCTION_MIN_CHARS = 6

_RENDERABILITY_PROMPT_BLOCK = """【Renderability First·视频模型能力边界】
逐镜可拍性由 ShotTask 的动作阶段、capacity_budget、可见身份、连续性状态差与
required_text 合同决定。自然语言中的物件、动作、情绪或题材词不参与通过判定。
若任务超出时长或可读窗口，应在 AtomicAction.splittable_boundaries 上重分配阶段，
不得扫描文案词汇后删除内容。"""


def renderability_prompt_block() -> str:
    return _RENDERABILITY_PROMPT_BLOCK


def shot_count_budget_errors(n_shots: int, *, context: str = "分镜") -> list[str]:
    """Shot count is never a content gate; duplicate/function gates stop runaway work."""
    _ = (n_shots, context)
    return []


# 单镜默认时长：主线压缩后优先 5s；>5 仅当口播/动作确实放不下，且进入 AI 审核标记。
PREFERRED_SHOT_DURATION_S = 5
DURATION_REVIEW_RISK_TAG = "duration_gt5_needs_review"
HUMAN_DURATION_REVIEW_TAG = "duration_human_reviewed"


def episode_target_from_spine(spine_beat_count: int) -> int:
    """Estimate duration from the delivered spine without a product maximum."""
    from app import config

    n = max(0, int(spine_beat_count or 0))
    if n <= 0:
        return config.EPISODE_TARGET_DEFAULT_S
    raw = n * 2 * PREFERRED_SHOT_DURATION_S
    raw = max(config.EPISODE_TARGET_MIN_S, raw)
    step = config.EPISODE_TARGET_STEP_S
    return max(config.EPISODE_TARGET_MIN_S, math.ceil(raw / step) * step)


def screenplay_required_duration_s(screenplay, *, minimum_s: int = 0) -> int:
    """Use the executable outline as duration authority when one can be compiled."""
    from app import config
    from app.spoken_contract import content_char_count

    if getattr(screenplay, "narrative_plan", None) is not None:
        from app.narrative_priority import (
            authoritative_outline_duration_s,
            compile_authoritative_delivery_outline,
        )

        _projected, outline, _audit = (
            compile_authoritative_delivery_outline(screenplay)
        )
        return max(
            int(minimum_s or 0),
            authoritative_outline_duration_s(outline),
        )

    # Legacy scripts without a narrative graph retain the old conservative
    # floor until they can be migrated to structured ShotTasks.
    spoken_chars = sum(
        content_char_count(turn.line)
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
    )
    spoken_rate = (
        config.SPOKEN_CHARS_PER_5_SECONDS
        / config.VIDEO_DURATION_MIN_S
    )
    spoken_seconds = math.ceil(spoken_chars / spoken_rate) if spoken_chars else 0
    spine = getattr(screenplay, "plot_spine", None)
    beats = list(getattr(spine, "spine_beats", None) or [])
    must_keep = [beat for beat in beats if beat.must_keep] or beats
    beat_seconds = len(must_keep) * 2 * PREFERRED_SHOT_DURATION_S
    scene_seconds = (
        len(getattr(screenplay, "scene_outline", None) or [])
        * PREFERRED_SHOT_DURATION_S
    )
    raw = max(
        int(minimum_s or 0),
        config.EPISODE_TARGET_MIN_S,
        spoken_seconds,
        beat_seconds,
        scene_seconds,
    )
    step = config.EPISODE_TARGET_STEP_S
    return math.ceil(raw / step) * step


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
