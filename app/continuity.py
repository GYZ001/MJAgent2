"""剧本/分镜/Seedance 连续性生产协议（PRD：剧本分镜与 Seedance 视频连续性整改方案）。

确定性辅助：状态链、连续性模式、信息台账、音频容量、参考图路由、生成前门禁。
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from app import config
from app.schemas import (
    AUDIO_TIMELINE_TYPES,
    CONTINUITY_MODES,
    DELIVERY_OWNERS,
    PROMPT_CONTRACT_VERSION,
    AudioTimelineItem,
    EpisodeScreenplay,
    RequiredOnScreenText,
    Shot,
    Storyboard,
    StoryboardOutline,
    VoiceCanonical,
)

# 多动作过载：5 秒镜头超过 2 个独立顺序节拍即拒绝（PRD §7.1 / §14.1）
_SEQUENTIAL_ACTION_SPLITTERS = re.compile(
    r"[，,；;、]|然后|接着|随后|之后|再|又|紧接着|同时"
)
_DISTINCT_ACTION_VERBS = (
    "走出", "走进", "跑出", "跑向", "走向", "走到", "上前", "离开", "伸手", "触碰", "按住",
    "闭上", "睁开", "亮起", "显现", "宣布", "喊出", "点名", "自我介绍", "微笑", "惊叹",
    "倒吸", "窃笑", "转身", "跪下", "站起", "举起", "放下", "拔出", "插入",
)


def effective_state_in(shot: Shot) -> str:
    return (shot.state_in or shot.first_frame_desc or "").strip()


def effective_state_out(shot: Shot) -> str:
    return (shot.observed_state_out or shot.state_out or shot.last_frame_desc or "").strip()


def planned_state_out(shot: Shot) -> str:
    return (shot.state_out or shot.last_frame_desc or "").strip()


def effective_primary_action(shot: Shot) -> str:
    return (shot.primary_action or shot.action_desc or "").strip()


def effective_characters_visible(shot: Shot) -> list[str]:
    return list(shot.characters_visible or shot.characters or [])


def effective_audio_cast(shot: Shot) -> list[str]:
    if shot.audio_cast:
        return list(shot.audio_cast)
    cast: list[str] = []
    for item in shot.audio_timeline or []:
        if item.speaker_id and item.speaker_id not in cast:
            cast.append(item.speaker_id)
    for d in shot.dialogues or []:
        if d.speaker and d.speaker not in cast:
            cast.append(d.speaker)
    if (shot.narration or "").strip() and "旁白" not in cast:
        # 真正旁白不强制进 audio_cast；保留角色画外音 speaker
        pass
    return cast


def uses_previous_tail_frame(mode: str) -> bool:
    return (mode or "").strip() == "action_continuation"


def derive_continuity_mode(shot: Shot, prev: Shot | None = None) -> str:
    """解析连续性模式。旧 continuity_from_prev 不得直接映射为 action_continuation。"""
    mode = (shot.continuity_mode or "").strip()
    if mode in CONTINUITY_MODES:
        return mode
    if prev is None:
        return "scene_change" if shot.shot_no == 1 else "same_scene_cut"
    same_scene = (shot.scene_setting or "").strip() == (prev.scene_setting or "").strip()
    if not same_scene:
        return "scene_change"
    # 旧数据 continuity_from_prev=true：仅表示同场景接镜，不是动作连续；默认 same_scene_cut
    if shot.continuity_from_prev:
        return "same_scene_cut"
    return "same_scene_cut"


def sync_shot_continuity_fields(shot: Shot, prev: Shot | None = None) -> str:
    """回填 state/continuity/visible 字段，并同步 legacy continuity_from_prev。"""
    mode = derive_continuity_mode(shot, prev)
    shot.continuity_mode = mode
    shot.continuity_from_prev = uses_previous_tail_frame(mode)
    if not (shot.state_in or "").strip():
        shot.state_in = (shot.first_frame_desc or "").strip()
    if not (shot.state_out or "").strip():
        shot.state_out = (shot.last_frame_desc or "").strip()
    if not (shot.primary_action or "").strip():
        shot.primary_action = (shot.action_desc or "").strip()
    # 旧镜头可能缺首尾帧，或首尾帧过短：用主动作合成最小可用状态
    action_fallback = (shot.primary_action or shot.action_desc or "").strip()
    if len((shot.state_in or "").strip()) < 8 and action_fallback:
        shot.state_in = f"动作开始前：{action_fallback[:80]}"
    if len((shot.state_out or "").strip()) < 8 and action_fallback:
        shot.state_out = f"动作完成后：{action_fallback[:80]}"
    if not shot.characters_visible:
        shot.characters_visible = list(shot.characters or [])
    if not shot.audio_cast:
        shot.audio_cast = effective_audio_cast(shot)
    if not shot.prompt_contract_version:
        shot.prompt_contract_version = PROMPT_CONTRACT_VERSION
    # 首尾帧与 state 双向同步，保持关键帧链路可用
    if (shot.state_in or "").strip() and not (shot.first_frame_desc or "").strip():
        shot.first_frame_desc = shot.state_in
    if (shot.state_out or "").strip() and not (shot.last_frame_desc or "").strip():
        shot.last_frame_desc = shot.state_out
    if (shot.primary_action or "").strip() and not (shot.action_desc or "").strip():
        shot.action_desc = shot.primary_action
    return mode


def normalize_board_continuity(board: Storyboard) -> None:
    """按连续性模式规范化整板；不再把同场景强制写成 action_continuation。"""
    for i, shot in enumerate(board.shots):
        prev = board.shots[i - 1] if i > 0 else None
        mode = sync_shot_continuity_fields(shot, prev)
        if i == 0:
            shot.continuity_mode = "same_scene_cut" if mode != "scene_change" else mode
            if shot.continuity_mode == "action_continuation":
                shot.continuity_mode = "same_scene_cut"
            shot.continuity_from_prev = False
            shot.transition = "硬切"
            continue
        same_scene = (shot.scene_setting or "").strip() == (prev.scene_setting or "").strip()
        if not same_scene:
            shot.continuity_mode = "scene_change"
            shot.continuity_from_prev = False
            if shot.transition == "硬切":
                from app.validators import default_scene_transition
                shot.transition = default_scene_transition(prev, shot)
        else:
            if shot.continuity_mode == "scene_change":
                shot.continuity_mode = "same_scene_cut"
            shot.continuity_from_prev = uses_previous_tail_frame(shot.continuity_mode)
            if shot.continuity_mode != "scene_change":
                shot.transition = "硬切"


def count_sequential_action_beats(text: str) -> int:
    """估算顺序动作节拍数：独立动词短语数量（不是逗号分句数）。"""
    raw = (text or "").strip()
    if not raw:
        return 0
    verbs = [v for v in _DISTINCT_ACTION_VERBS if v in raw]
    # 去重相邻重复
    unique: list[str] = []
    for v in verbs:
        if not unique or unique[-1] != v:
            unique.append(v)
    if len(unique) >= 2:
        return len(unique)
    parts = [p.strip() for p in _SEQUENTIAL_ACTION_SPLITTERS.split(raw) if p.strip()]
    # 只有一个动作但描写充分：返回 1；空动作返回 0
    return max(1 if raw else 0, min(len(parts), len(unique) or 1))


def action_capacity_errors(shot: Shot) -> list[str]:
    errors: list[str] = []
    action = effective_primary_action(shot)
    beats = count_sequential_action_beats(action)
    limit = 2 if int(getattr(shot, "duration_s", 5) or 5) <= 6 else 3
    if beats > limit:
        errors.append(
            f"shot_no={shot.shot_no} 含约 {beats} 个顺序动作节拍，超过 {shot.duration_s}s 镜头容量上限 {limit}；"
            "请删减超纲动作，优先保留单一主线动作；确需拆镜时最多 +1 相邻镜，禁止无限拆碎"
        )
    return errors


def speech_capacity_budget(duration_s: int, *, lead_in: float = 0.3, lead_out: float = 0.3,
                           action_reserve: float = 0.5) -> float:
    """可用说话时长（秒）：镜头时长减去起音/收音/必要动作占用。"""
    duration = float(min(max(int(duration_s), config.VIDEO_DURATION_MIN_S), config.VIDEO_DURATION_MAX_S))
    return max(0.5, duration - lead_in - lead_out - action_reserve)


def content_char_count(text: str | None) -> int:
    """口播纯文字字数：去空白与 Unicode 标点，只计汉字/字母/数字等。"""
    total = 0
    for ch in text or "":
        if ch.isspace():
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        total += 1
    return total


def max_speech_chars(duration_s: int) -> int:
    """单镜台词纯文字上限；与 config.max_spoken_chars_for_duration 同口径。"""
    return config.max_spoken_chars_for_duration(duration_s)


def spoken_chars_from_shot(shot: Shot) -> int:
    """本镜真实台词纯文字字数（不计标点、不计旁白）。"""
    if shot.audio_timeline:
        total = 0
        for item in shot.audio_timeline:
            if item.type in {"spoken_dialogue", "offscreen_voice"}:
                total += content_char_count(item.text)
        return total
    return sum(content_char_count(d.line) for d in shot.dialogues or [])


def speech_capacity_errors(shot: Shot) -> list[str]:
    errors: list[str] = []
    chars = spoken_chars_from_shot(shot)
    limit = max_speech_chars(shot.duration_s)
    if chars > limit:
        errors.append(
            f"shot_no={shot.shot_no} 台词纯文字 {chars} 字（不计标点），超过 {shot.duration_s}s 口播上限 {limit} 字；"
            f"请缩短台词、拆镜或增加合理时长"
        )
    # 多主说话人抢占
    speakers: list[str] = []
    if shot.audio_timeline:
        for item in shot.audio_timeline:
            if item.type in {"spoken_dialogue", "offscreen_voice"} and item.speaker_id:
                if item.speaker_id not in speakers:
                    speakers.append(item.speaker_id)
    else:
        for d in shot.dialogues or []:
            delivery = getattr(d, "delivery", "spoken_dialogue") or "spoken_dialogue"
            if delivery in {"spoken_dialogue", "offscreen_voice"} and d.speaker not in speakers:
                speakers.append(d.speaker)
    if len(speakers) > 1:
        # 允许一主一短应，但同一镜优先单说话人；超过 2 硬失败
        if len(speakers) > 2:
            errors.append(
                f"shot_no={shot.shot_no} 有多个主要说话人 {speakers}；一条视频优先只有一个主要说话人"
            )
    return errors


def build_audio_timeline_from_legacy(shot: Shot, voice_bible: list[VoiceCanonical] | None = None
                                     ) -> list[AudioTimelineItem]:
    """从 dialogues 推导音频时间线（产品禁止旁白，不再写入 narration 轨）。"""
    if shot.audio_timeline:
        # 历史脏数据：丢掉 narration 轨，只保留真实台词与环境声
        return [item for item in shot.audio_timeline if item.type != "narration"]
    voice_map = {v.speaker_id: v.voice_canonical for v in (voice_bible or [])}
    dur = float(shot.duration_s or config.DEFAULT_VIDEO_DURATION_S)
    items: list[AudioTimelineItem] = []
    cursor = 0.3
    dialogues = list(shot.dialogues or [])

    def _consume(text: str, typ: str, speaker: str | None, lip: bool, emotion: str) -> None:
        nonlocal cursor
        chars = content_char_count(text)
        need = max(0.8, chars / 3.5) if chars else 0.8
        end = min(dur - 0.2, cursor + need)
        if end <= cursor:
            end = min(dur, cursor + 0.6)
        items.append(AudioTimelineItem(
            start_s=round(cursor, 2),
            end_s=round(end, 2),
            type=typ,
            speaker_id=speaker,
            text=text,
            lip_sync=lip,
            emotion=emotion,
            voice_canonical=voice_map.get(speaker or "", ""),
        ))
        cursor = end

    for d in dialogues:
        delivery = getattr(d, "delivery", None) or "spoken_dialogue"
        if delivery == "narration":
            delivery = "offscreen_voice"
        if delivery not in AUDIO_TIMELINE_TYPES or delivery == "narration":
            delivery = "spoken_dialogue"
        visible = set(effective_characters_visible(shot))
        if delivery == "spoken_dialogue" and d.speaker not in visible:
            delivery = "offscreen_voice"
        _consume(d.line, delivery, d.speaker, delivery == "spoken_dialogue", d.emotion or "平静")
    if not items:
        items.append(AudioTimelineItem(
            start_s=0.0, end_s=dur, type="ambient_sound",
            text="仅保留与画面匹配的自然环境声，不要额外台词或旁白",
        ))
    elif cursor < dur - 0.2:
        items.append(AudioTimelineItem(
            start_s=round(cursor, 2), end_s=dur, type="ambient_sound",
            text="收束为自然环境声，不新增台词",
        ))
    return items


def ensure_audio_timeline(shot: Shot, voice_bible: list[VoiceCanonical] | None = None) -> None:
    if not shot.audio_timeline:
        shot.audio_timeline = build_audio_timeline_from_legacy(shot, voice_bible)
    else:
        shot.audio_timeline = [item for item in shot.audio_timeline if item.type != "narration"]
        if not shot.audio_timeline:
            shot.audio_timeline = build_audio_timeline_from_legacy(shot, voice_bible)
    if not shot.audio_cast:
        shot.audio_cast = effective_audio_cast(shot)


def _shot_value(shot: Shot | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(shot, dict):
        return shot.get(key, default)
    return getattr(shot, key, default)


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def information_items_for_shot(
    shot: Shot | dict[str, Any],
    screenplay: EpisodeScreenplay | None = None,
) -> list[dict[str, str]]:
    """Resolve internal info IDs to user/model-facing Chinese content.

    New productions use ``screenplay.information_ledger`` as the authority.  Legacy
    boards may contain model-invented snake_case IDs and no ledger; for those, derive
    one Chinese delivery description from the shot's own action/state instead of
    exposing or forwarding the opaque ID.
    """
    ids = [str(x).strip() for x in (_shot_value(shot, "new_information_ids", []) or []) if str(x).strip()]
    if not ids:
        return []
    ledger = {
        item.info_id: (item.content or "").strip()
        for item in (screenplay.information_ledger if screenplay else [])
        if (item.info_id or "").strip()
    }
    derived = next((
        str(_shot_value(shot, key, "") or "").strip()
        for key in ("purpose", "primary_action", "state_out", "action_desc")
        if _has_chinese(str(_shot_value(shot, key, "") or "").strip())
    ), "本镜首次交付的剧情信息")
    return [
        {
            "info_id": info_id,
            "content": ledger.get(info_id) or derived,
            "source": "ledger" if ledger.get(info_id) else "derived",
        }
        for info_id in ids
    ]


def resolve_do_not_repeat_texts(
    shot: Shot | dict[str, Any],
    screenplay: EpisodeScreenplay | None = None,
    prior_shots: list[Shot] | None = None,
) -> list[str]:
    """Turn do-not-repeat IDs into Chinese semantic constraints for Seedance."""
    lookup = {
        item.info_id: (item.content or "").strip()
        for item in (screenplay.information_ledger if screenplay else [])
        if (item.info_id or "").strip() and (item.content or "").strip()
    }
    for prior in prior_shots or []:
        for item in information_items_for_shot(prior, screenplay):
            lookup.setdefault(item["info_id"], item["content"])

    resolved: list[str] = []
    for raw in _shot_value(shot, "do_not_repeat", []) or []:
        value = str(raw or "").strip()
        if not value:
            continue
        content = lookup.get(value, "")
        if not content:
            parts = re.split(r"[:：]", value, maxsplit=1)
            if len(parts) == 2 and _has_chinese(parts[1]):
                content = parts[1].strip()
            elif _has_chinese(value):
                content = value
        # 裸 snake_case / I001 只对内部去重有意义，不能作为视频模型指令。
        if content and content not in resolved:
            resolved.append(content)
    return resolved


def information_ledger_errors(
    board: Storyboard,
    screenplay: EpisodeScreenplay | None,
) -> list[str]:
    """未标记强化的 info_id 不得重复交付。"""
    if not screenplay or not screenplay.information_ledger:
        return []
    errors: list[str] = []
    ledger = {item.info_id: item for item in screenplay.information_ledger}
    delivered: dict[str, int] = {}
    for shot in board.shots:
        for info_id in shot.new_information_ids or []:
            if info_id not in ledger:
                errors.append(
                    f"shot_no={shot.shot_no} 使用了信息台账中不存在的 ID {info_id}；"
                    "new_information_ids 只能引用 screenplay.information_ledger"
                )
                continue
            if info_id in delivered and info_id not in (shot.reinforcement_info_ids or []):
                item = ledger.get(info_id)
                reinforce = bool(item and item.reinforcement_allowed)
                if not reinforce:
                    errors.append(
                        f"shot_no={shot.shot_no} 重复交付已在镜{delivered[info_id]}交付的信息 {info_id}"
                        f"（{item.content if item else ''}）；如需强调请标记 reinforcement_allowed / reinforcement_info_ids"
                    )
            delivered[info_id] = shot.shot_no
            if info_id in ledger and ledger[info_id].status == "unassigned":
                ledger[info_id].status = "assigned"
                ledger[info_id].assigned_shot_no = shot.shot_no
    return errors


def state_chain_errors(board: Storyboard) -> list[str]:
    errors: list[str] = []
    for i, shot in enumerate(board.shots):
        prev = board.shots[i - 1] if i > 0 else None
        mode = sync_shot_continuity_fields(shot, prev)
        state_in = effective_state_in(shot)
        state_out = planned_state_out(shot)
        action = effective_primary_action(shot)
        tag = f"shot_no={shot.shot_no}"
        if len(state_in) < 8:
            errors.append(f"{tag}.state_in 缺失或过短；必须写清精确起始状态")
        if len(state_out) < 8:
            errors.append(f"{tag}.state_out 缺失或过短；必须写清精确结束状态")
        if len(action) < 8:
            errors.append(f"{tag}.primary_action 缺失或过短")
        if mode not in CONTINUITY_MODES:
            errors.append(f"{tag}.continuity_mode=「{mode}」不在 {sorted(CONTINUITY_MODES)}")
        if i == 0 and mode == "action_continuation":
            errors.append(f"{tag}.continuity_mode=action_continuation，但第一个镜头没有上一镜可承接")
        if prev is not None and mode == "action_continuation":
            prev_out = effective_state_out(prev)
            # 粗粒度一致性：当前 state_in 应引用上一镜结束状态的关键语义，不能完全无关
            if prev_out and state_in and _too_divergent(prev_out, state_in):
                errors.append(
                    f"{tag}.state_in 与上一镜 state_out/observed_state_out 矛盾："
                    f"上一镜结束于「{prev_out[:40]}」，本镜却从「{state_in[:40]}」开始；"
                    "action_continuation 要求当前 state_in 等于上一镜实际尾状态"
                )
        if prev is not None:
            same_scene = (shot.scene_setting or "").strip() == (prev.scene_setting or "").strip()
            if mode == "scene_change" and same_scene:
                errors.append(f"{tag}.continuity_mode=scene_change 但 scene_setting 与上一镜相同")
            if mode != "scene_change" and not same_scene:
                errors.append(
                    f"{tag}.continuity_mode={mode} 但 scene_setting 从「{prev.scene_setting}」变为「{shot.scene_setting}」；"
                    "跨场应使用 scene_change"
                )
    return errors


def _too_divergent(prev_out: str, state_in: str) -> bool:
    a = re.sub(r"\s+", "", prev_out)
    b = re.sub(r"\s+", "", state_in)
    if not a or not b:
        return False
    # 共享至少 2 个连续汉字，或一方包含另一方的短核心片段
    shared = 0
    for i in range(len(a) - 1):
        if a[i:i + 2] in b:
            shared += 1
            if shared >= 2:
                return False
    return True


def required_text_conflict_errors(shot: Shot, prompt_text: str | None = None) -> list[str]:
    errors: list[str] = []
    required = shot.required_text
    has_text = bool(required and (required.exact_text or "").strip())
    prompt = prompt_text or ""
    if has_text:
        if "画面中不出现任何文字" in prompt and (required.exact_text or "") in prompt:
            # 同时要求与禁止
            errors.append(
                f"shot_no={shot.shot_no} 需要画面文字「{required.exact_text}」但提示词仍含全面禁止文字；"
                "请改用条件化文字策略"
            )
        if "全面禁止文字" in prompt or ("禁止任何文字" in prompt and "只允许" not in prompt):
            errors.append(
                f"shot_no={shot.shot_no} required_text 与负面文字规则冲突"
            )
    return errors


def forbidden_prompt_content_errors(prompt_text: str, shot: Shot) -> list[str]:
    """最终提示词不得含原文/完整前镜动作/未来剧情。"""
    errors: list[str] = []
    text = prompt_text or ""
    if "小说原文兜底参考：" in text or "SOURCE_EXCERPT" in text:
        errors.append(f"shot_no={shot.shot_no} 最终提示词包含原文章节摘录")
    if (shot.source_excerpt or "").strip() and (shot.source_excerpt or "").strip()[:24] in text:
        # 允许极短偶然重合；超过 24 字连续命中视为注入原文
        errors.append(f"shot_no={shot.shot_no} 最终提示词包含 source_excerpt 原文内容")
    return errors


def reference_role_plan(shot: Shot, *, continuity_mode: str | None = None) -> list[str]:
    mode = continuity_mode or derive_continuity_mode(shot)
    roles: list[str] = []
    if uses_previous_tail_frame(mode):
        roles.append("start_state_reference")
    if mode != "scene_change":
        roles.append("scene_reference")
    for name in effective_characters_visible(shot):
        roles.append(f"character_identity:{name}")
    if shot.required_text and (shot.required_text.exact_text or "").strip():
        roles.append("text_surface_reference")
    if shot.reference_roles:
        # 保留显式声明，同时保证强制规则
        merged = list(dict.fromkeys([*roles, *shot.reference_roles]))
        return merged
    return roles


def preflight_seedance_gates(
    shot: Shot,
    *,
    prev: Shot | None = None,
    prompt_text: str | None = None,
    screenplay: EpisodeScreenplay | None = None,
    delivered_info_ids: set[str] | None = None,
) -> list[str]:
    """生成前静态门禁（PRD §14.1）：任一项失败不得提交 Seedance。"""
    sync_shot_continuity_fields(shot, prev)
    ensure_audio_timeline(shot, screenplay.voice_bible if screenplay else None)
    errors: list[str] = []
    errors.extend(action_capacity_errors(shot))
    errors.extend(speech_capacity_errors(shot))
    errors.extend(state_chain_errors(Storyboard(episode_no=0, shots=([prev, shot] if prev else [shot]))))
    # 过滤只属于 prev 的报错
    errors = [e for e in errors if f"shot_no={shot.shot_no}" in e or "shots[" not in e]

    delivered = delivered_info_ids or set()
    for info_id in shot.new_information_ids or []:
        if info_id in delivered and info_id not in (shot.reinforcement_info_ids or []):
            item = None
            if screenplay:
                item = next((x for x in screenplay.information_ledger if x.info_id == info_id), None)
            if not (item and item.reinforcement_allowed):
                errors.append(
                    f"shot_no={shot.shot_no} new_information_ids 含已交付信息 {info_id} 且未标记有意强化"
                )

    mode = shot.continuity_mode
    roles = reference_role_plan(shot, continuity_mode=mode)
    if uses_previous_tail_frame(mode) and "start_state_reference" not in roles:
        errors.append(f"shot_no={shot.shot_no} continuity_mode=action_continuation 但缺少 start_state_reference")
    if not uses_previous_tail_frame(mode) and "start_state_reference" in (shot.reference_roles or []):
        errors.append(
            f"shot_no={shot.shot_no} continuity_mode={mode} 不得使用上一镜尾帧参考"
        )

    if prompt_text:
        errors.extend(required_text_conflict_errors(shot, prompt_text))
        errors.extend(forbidden_prompt_content_errors(prompt_text, shot))
        # 仅对新协议分段提示词检查必填段落；兼容测试/人工 override 的短 prompt
        if "[FORMAT]" in prompt_text or "[ONE CURRENT ACTION]" in prompt_text:
            for marker in ("[START STATE", "[ONE CURRENT ACTION]", "[END STATE"):
                if marker not in prompt_text:
                    errors.append(f"shot_no={shot.shot_no} 提示词缺少必填段落 {marker}")

    # 需要后期能力才能成立的转场
    if shot.transition in {"声音延续+叠化", "声音先行+淡入"}:
        errors.append(
            f"shot_no={shot.shot_no} transition={shot.transition} 依赖后期声画桥接；"
            "本项目无后期能力，请改用单侧可完成的硬切/淡出淡入/遮挡转场"
        )
    return errors


def mark_legacy_unvalidated(shot: Shot) -> None:
    missing = not (
        (shot.state_in or shot.first_frame_desc)
        and (shot.state_out or shot.last_frame_desc)
        and (shot.continuity_mode in CONTINUITY_MODES)
        and (shot.audio_timeline or shot.dialogues is not None)
        and (shot.story_event_id or shot.new_information_ids)
    )
    shot.legacy_unvalidated = bool(missing)


def shot_contract_dict(shot: Shot) -> dict[str, Any]:
    """持久化到 shots.shot_contract_json 的生产契约字段。"""
    required = None
    if shot.required_text is not None:
        required = shot.required_text.model_dump()
    return {
        "story_event_id": shot.story_event_id,
        "purpose": shot.purpose,
        "new_information_ids": list(shot.new_information_ids or []),
        "reinforcement_info_ids": list(shot.reinforcement_info_ids or []),
        "state_in": shot.state_in,
        "primary_action": shot.primary_action,
        "emotion_beat": shot.emotion_beat,
        "state_out": shot.state_out,
        "observed_state_out": shot.observed_state_out,
        "continuity_mode": shot.continuity_mode,
        "characters_visible": list(shot.characters_visible or []),
        "audio_cast": list(shot.audio_cast or []),
        "audio_timeline": [x.model_dump() for x in (shot.audio_timeline or [])],
        "required_text": required,
        "reference_roles": list(shot.reference_roles or []),
        "do_not_repeat": list(shot.do_not_repeat or []),
        "risk_tags": list(shot.risk_tags or []),
        "prompt_contract_version": shot.prompt_contract_version or PROMPT_CONTRACT_VERSION,
        "legacy_unvalidated": bool(shot.legacy_unvalidated),
        "camera_angle": shot.camera_angle,
        "spatial_anchor": shot.spatial_anchor,
        "is_final": bool(shot.is_final),
    }


def apply_shot_contract(shot: Shot, payload: dict[str, Any] | str | None) -> Shot:
    if not payload:
        return shot
    data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    for key in (
        "story_event_id", "purpose", "state_in", "primary_action", "emotion_beat",
        "state_out", "observed_state_out", "continuity_mode", "prompt_contract_version",
        "camera_angle", "spatial_anchor",
    ):
        if data.get(key) not in (None, ""):
            setattr(shot, key, data[key])
    for key in (
        "new_information_ids", "reinforcement_info_ids", "characters_visible",
        "audio_cast", "reference_roles", "do_not_repeat", "risk_tags",
    ):
        if key in data and data[key] is not None:
            setattr(shot, key, list(data[key] or []))
    if "audio_timeline" in data and data["audio_timeline"] is not None:
        shot.audio_timeline = [AudioTimelineItem.model_validate(x) for x in data["audio_timeline"]]
    if "required_text" in data:
        rt = data["required_text"]
        shot.required_text = RequiredOnScreenText.model_validate(rt) if rt else None
    if "legacy_unvalidated" in data:
        shot.legacy_unvalidated = bool(data["legacy_unvalidated"])
    if "is_final" in data:
        shot.is_final = bool(data["is_final"])
    return shot


def ledger_context_for_shot(
    screenplay: EpisodeScreenplay,
    completed_shots: list[Shot],
    current_info_ids: list[str] | None = None,
) -> dict[str, list[Any]]:
    """已交付 / 当前交付 / 待交付三栏，同时提供稳定 ID 与中文语义。"""
    ledger = list(screenplay.information_ledger or [])
    delivered: list[str] = []
    for shot in completed_shots:
        for info_id in shot.new_information_ids or []:
            if info_id not in delivered:
                delivered.append(info_id)
    current = list(current_info_ids or [])
    pending = [
        item.info_id for item in ledger
        if item.info_id not in delivered and item.info_id not in current
    ]
    ledger_by_id = {item.info_id: item for item in ledger}
    delivered_items = []
    for info_id in delivered:
        item = ledger_by_id.get(info_id)
        if item and (item.content or "").strip():
            delivered_items.append({"info_id": info_id, "content": item.content.strip()})
            continue
        source_shot = next(
            (shot for shot in completed_shots if info_id in (shot.new_information_ids or [])),
            None,
        )
        if source_shot:
            match = next((x for x in information_items_for_shot(source_shot, screenplay)
                          if x["info_id"] == info_id), None)
            if match:
                delivered_items.append({"info_id": info_id, "content": match["content"]})
    current_items = [
        {"info_id": info_id, "content": ledger_by_id[info_id].content}
        for info_id in current if info_id in ledger_by_id
    ]
    pending_items = [
        {"info_id": item.info_id, "content": item.content}
        for item in ledger if item.info_id in pending
    ]
    do_not_repeat = list(dict.fromkeys(
        item["content"] for item in delivered_items
        if item["content"] and not (
            ledger_by_id.get(item["info_id"])
            and ledger_by_id[item["info_id"]].reinforcement_allowed
        )
    ))
    return {
        "delivered_ids": delivered,
        "current_ids": current,
        "pending_ids": pending,
        "delivered_items": delivered_items,
        "current_items": current_items,
        "pending_items": pending_items,
        "do_not_repeat": do_not_repeat,
    }


def adaptation_hook_errors(screenplay: EpisodeScreenplay, episode: dict | None = None) -> list[str]:
    """空钩子不得触发剧情发明；adaptation_addition 必须授权。"""
    errors: list[str] = []
    episode = episode or {}
    cliff = (episode.get("cliffhanger") or "").strip()
    hook = (episode.get("hook") or "").strip()
    ending = (screenplay.ending_hook or "").strip()
    # 集级钩子为空时，不允许为了模板发明新钩子
    if not cliff and not hook:
        if ending and "（待定）" not in ending and "无钩子" not in ending and "无集级钩子" not in ending:
            # 允许显式声明无钩子
            if len(ending) >= 6 and not ending.startswith("无"):
                # 检查是否有未授权改编事件
                unauthorized = [
                    e for e in (screenplay.events or [])
                    if e.adaptation_addition and not e.approved
                ]
                if unauthorized:
                    errors.append(
                        "集级 hook/cliffhanger 为空，但 events 含未授权 adaptation_addition；"
                        "禁止为满足钩子形式擅自发明下一集剧情"
                    )
    for event in screenplay.events or []:
        if event.adaptation_addition and not event.approved:
            errors.append(
                f"事件 {event.event_id} 标记为改编新增但未批准：{event.adaptation_reason or event.source_fact}"
            )
    for item in screenplay.information_ledger or []:
        if item.delivery_owner and item.delivery_owner not in DELIVERY_OWNERS:
            errors.append(
                f"信息 {item.info_id} 的 delivery_owner={item.delivery_owner} 不合法"
            )
    return errors


def outline_atomic_errors(outline: StoryboardOutline) -> list[str]:
    """大纲阶段状态链与原子性检查。"""
    errors: list[str] = []
    for i, shot in enumerate(outline.shots or []):
        tag = f"outline.shots[{i}](shot_no={shot.shot_no})"
        if (shot.state_in or "").strip() and (shot.state_out or "").strip():
            if (shot.state_in or "").strip() == (shot.state_out or "").strip():
                errors.append(f"{tag} state_in 与 state_out 相同；主线镜头必须有可见/可听状态变化")
        mode = (shot.continuity_mode or "").strip()
        if mode and mode not in CONTINUITY_MODES:
            errors.append(f"{tag}.continuity_mode=「{mode}」不合法")
        action = (shot.primary_action or shot.beat or "").strip()
        if action and count_sequential_action_beats(action) > 2:
            errors.append(
                f"{tag} 主动作过载（{action[:40]}…）；请压缩为单一主线动作，"
                "确需拆镜时计入 8~16 软预算，禁止为细节无限拆碎"
            )
    return errors


HARD_QA_FAILURE_TYPES = {
    "story_repeat",
    "future_leak",
    "wrong_dialogue",
    "text_error",
    "character_duplicate",
    "state_mismatch",
    "needs_crop",
}


def classify_video_hard_failures(qa: dict[str, Any] | None, *,
                                 technical: dict[str, Any] | None = None) -> list[str]:
    """从 QA/技术门禁提取硬失败类型。"""
    failures: list[str] = []
    qa = qa or {}
    technical = technical or {}
    if technical and not technical.get("passed", True):
        failures.append("needs_crop")
    issues = [str(x).lower() for x in (qa.get("issues") or [])]
    failure_types = [str(x) for x in (qa.get("failure_types") or [])]
    for ft in failure_types:
        if ft in HARD_QA_FAILURE_TYPES and ft not in failures:
            failures.append(ft)
    joined = "；".join(issues)
    checks = (
        ("重复", "story_repeat"),
        ("重演", "story_repeat"),
        ("抢演", "future_leak"),
        ("下一镜", "future_leak"),
        ("台词", "wrong_dialogue"),
        ("口型", "wrong_dialogue"),
        ("文字", "text_error"),
        ("乱码", "text_error"),
        ("字幕", "text_error"),
        ("复制", "character_duplicate"),
        ("分身", "character_duplicate"),
        ("双人", "character_duplicate"),
        ("首帧", "state_mismatch"),
        ("尾帧", "state_mismatch"),
        ("状态", "state_mismatch"),
        ("裁切", "needs_crop"),
        ("裁剪", "needs_crop"),
    )
    for keyword, code in checks:
        if keyword in joined and code not in failures:
            failures.append(code)
    # 分项硬门槛
    for key in ("start_state_match", "end_state_match", "action_match", "character_match"):
        try:
            score = float(qa.get(key)) if qa.get(key) is not None else None
        except (TypeError, ValueError):
            score = None
        if score is not None and score < 0.45 and "state_mismatch" not in failures:
            if key in {"start_state_match", "end_state_match"}:
                failures.append("state_mismatch")
    return failures


def retry_patch_for_failure(failure_type: str) -> dict[str, Any]:
    """按失败类型定向修正建议（供 enqueue critique / 提示词附加）。"""
    mapping = {
        "story_repeat": {
            "extra_negative": ["不要重演上一镜已完成的动作", "不要重复已交付剧情"],
            "hint": "删除上一镜动作上下文；核对是否误用尾帧",
        },
        "future_leak": {
            "extra_negative": ["不要提前表演下一镜内容", "不要描述下一场开场"],
            "hint": "删除未来情节/转场描述；收窄 state_out",
        },
        "character_duplicate": {
            "extra_negative": ["画面中不要出现重复人物/分身/双重人物"],
            "hint": "移除不该出现的参考图；写明精确人数",
        },
        "wrong_dialogue": {
            "extra_negative": ["不要用通用旁白替代指定角色声音", "不要改写指定台词"],
            "hint": "强化 speaker_id + voice_canonical + lip_sync",
        },
        "text_error": {
            "extra_negative": ["文字必须准确，禁止乱码缺字"],
            "hint": "独立文字镜头、稳定构图、缩短文字",
        },
        "state_mismatch": {
            "extra_negative": ["必须从指定起始状态开始并在指定结束状态收束"],
            "hint": "核对状态链；仅连续动作使用真实尾帧",
        },
        "needs_crop": {
            "extra_negative": ["整条视频必须可直接采用，不要片头片尾无效段"],
            "hint": "收紧起势收势，禁止依赖后期裁切",
        },
    }
    return mapping.get(failure_type, {
        "extra_negative": [],
        "hint": "按失败类型定向调整，避免完全重写提示词",
    })
