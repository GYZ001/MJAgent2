"""剧本/分镜/Seedance 连续性生产协议（PRD：剧本分镜与 Seedance 视频连续性整改方案）。

确定性辅助：状态链、连续性模式、信息台账、音频容量、参考图路由、生成前门禁。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app import config
from app.schemas import (
    AudienceStatePathRef,
    CONTINUITY_MODES,
    DELIVERY_OWNERS,
    KEY_LINE_ID_RE,
    PROMPT_CONTRACT_VERSION,
    SPINE_BEAT_ID_RE,
    STORY_EVENT_ID_RE,
    AudioTimelineItem,
    ContinuityState,
    EpisodeScreenplay,
    NarrativeBoundaryContract,
    NarrativeContinuityPlan,
    RequiredOnScreenText,
    Shot,
    ShotCapacityBudget,
    ShotContribution,
    Storyboard,
    StoryboardOutline,
    VoiceCanonical,
)
from app.scene_contract import same_scene, scene_time_of
from app.spoken_contract import (
    RULE_SPOKEN_CAPACITY,
    build_timeline_from_segments,
    capacity_issue,
    effective_spoken_segments,
    segments_from_dialogues,
    spoken_char_total,
    spoken_speakers,
    synchronize_spoken_contract,
    validate_spoken_contract,
)

# 多动作过载：5 秒镜头超过 2 个独立顺序节拍即拒绝（PRD §7.1 / §14.1）
_SEQUENTIAL_ACTION_SPLITTERS = re.compile(
    r"[，,；;、]|然后|接着|随后|之后|再|又|紧接着|同时"
)
_DISTINCT_ACTION_VERBS = (
    "走出", "走进", "跑出", "跑向", "走向", "走到", "上前", "离开", "伸手", "触碰", "按住",
    "穿过", "行至", "退到", "停下", "闭上", "睁开", "亮起", "显现", "宣布", "喊出", "点名",
    "翻开", "合拢", "抬头", "收回", "触摸", "点头", "自我介绍", "微笑", "惊叹", "倒吸",
    "窃笑", "转身", "跪下", "站起", "举起", "放下", "拔出", "插入",
)

DIALOGUE_FOCUS_RISK_TAG = "dialogue_speaker_closeup"
DIALOGUE_TWO_SHOT_RISK_TAG = "dialogue_two_shot_required"
DIALOGUE_CLOSEUP_SHOT_SIZES = frozenset({"近景", "特写"})
DIALOGUE_CLOSEUP_CAMERA_MOVES = frozenset({"固定", "推近"})
_DIALOGUE_TWO_SHOT_INTERACTION_RE = re.compile(
    r"搀扶|扶住|抱住|拥抱|握住|抓住|拉住|按住|推开|挡住|托住|"
    r"递给|递出|接过|抢夺|碰杯|亲吻|背起|抱起|交手|对打|扭打|"
    r"共同(?:握住|托住|抬起|推动|按住)|同时(?:握住|托住|抬起|推动|按住)"
)
_IMPLICIT_SPEECH_RE = re.compile(
    r"开口|说出|说完|说话|问话|打招呼|询问|"
    r"宣读|宣布|告知|解释|质问|反问|承诺|喊出|呼喊|"
    r"念出|念道|嘀咕|喃喃|自语|台词|口型|"
    r"(?<!准备)(?<!正要)(?<!打算)(?<!即将)(?:提出|发出|说出)"
    r"[^，。；！？]{0,8}请求|"
    r"(?:开口|出声)[^，。；！？]{0,6}(?:回答|回应)|"
    r"(?:回答|回应)(?:道|说|问题|问话)|"
    r"嘴(?:巴|唇)[^，。；！？]{0,8}(?:张开|微张|开合|翕动)"
)

# 有对白不等于只能拍脸。角色在说话的同时完成走位、离场或操作剧情道具时，
# 这些可见动作本身就是主线交付，若仍强制单人大近景，视频模型通常只保留口型，
# 把走位/道具动作整段省略。此处只识别大形体、可核验动作，不把抬眼、微笑等
# 表情表演误判成动作调度镜。
_DIALOGUE_SPATIAL_STAGING_RE = re.compile(
    r"走(?:向|到|过|进|出|开)|穿过|转身|离开|退(?:到|向|开)|上前|跑(?:向|到|出|进)|"
    r"快步|缓步|迈步|起身|站起|坐下|跪下|绕过|移步|追上|进入|退出"
)
_DIALOGUE_PROP_STAGING_RE = re.compile(
    r"翻开|合拢|拿起|放下|举起|抬手|伸手|收回手|触碰|触摸|按住|按上|贴上|"
    r"握住|递出|递给|接过|拔出|推开|拉开|打开|关闭|指向|挥击|敲击|端起|拾起"
)


def _scene_time_context(value: Any) -> str:
    """Use the same broad time buckets as storyboard validation."""
    raw = scene_time_of(value)
    if any(
        token in raw
        for token in ("凌晨", "清晨", "早晨", "上午", "白天", "日间", "日")
    ):
        return "day"
    if any(
        token in raw
        for token in ("中午", "午后", "下午", "傍晚", "黄昏")
    ):
        return "late_day"
    if any(
        token in raw
        for token in ("夜晚", "夜里", "深夜", "午夜", "夜")
    ):
        return "night"
    return re.sub(r"\s+", "", raw).lower()


def raw_characters_visible(shot: Shot) -> list[str]:
    """分镜声明的原始可见人物，不应用对白构图派生规则。"""
    return [
        str(name).strip()
        for name in (shot.characters_visible or shot.characters or [])
        if str(name).strip()
    ]


def _character_mentioned_as_visible(name: str, text: str) -> bool:
    """Whether a visual clause requires ``name`` instead of an offscreen relation."""
    for clause in re.split(r"[，。；！？]", text or ""):
        if name not in clause:
            continue
        if any(
            marker in clause
            for marker in (
                f"画外{name}", f"画面外{name}", f"镜外{name}", f"{name}画外",
                f"{name}不入画", f"{name}留在画外",
            )
        ):
            continue
        return True
    return False


def required_visual_action_characters(shot: Shot) -> list[str]:
    """Recover visible action participants from typed start/end states.

    ``characters_visible`` may be narrowed to the dialogue speaker upstream.
    That is valid for a pure close-up, but not when the same shot explicitly
    shows another person entering, receiving an object, collapsing, or staying
    in frame. Typed continuity state is the authoritative evidence for those
    action participants.
    """
    visual_text = "。".join(
        str(value or "").strip()
        for value in (
            shot.primary_action,
            shot.action_desc,
            shot.first_frame_desc,
            shot.last_frame_desc,
        )
        if str(value or "").strip()
    )
    candidates = list(shot.characters or [])
    for state in (shot.continuity_state_in, shot.continuity_state_out):
        for name, character in (state.characters or {}).items():
            visibility = (
                character.get("visibility")
                if isinstance(character, dict)
                else getattr(character, "visibility", "")
            )
            if str(visibility or "").strip().lower() in {
                "hidden", "offscreen", "not_visible", "画外", "不可见",
            }:
                continue
            candidates.append(name)
    return list(dict.fromkeys(
        str(name).strip()
        for name in candidates
        if str(name).strip()
        and _character_mentioned_as_visible(str(name).strip(), visual_text)
    ))


def onscreen_dialogue_speakers(shot: Shot) -> list[str]:
    """按口播顺序返回需要画内对口型的说话人。"""
    speakers: list[str] = []
    try:
        segments = effective_spoken_segments(shot)
    except (AttributeError, TypeError):
        # model_copy(update=...) 与历史调用可能绕过字段重校验并留下 dict。
        # 这里只做只读兼容，不替代 spoken_contract 的正式同步与校验。
        segments = []
        timeline_has_spoken = False
        for item in shot.audio_timeline or []:
            item_type = item.get("type") if isinstance(item, dict) else item.type
            if item_type not in {"spoken_dialogue", "offscreen_voice"}:
                continue
            timeline_has_spoken = True
            speaker = (
                item.get("speaker_id") if isinstance(item, dict) else item.speaker_id
            )
            lip_sync = item.get("lip_sync") if isinstance(item, dict) else item.lip_sync
            if item_type == "spoken_dialogue" and lip_sync and str(speaker or "").strip():
                normalized = str(speaker).strip()
                if normalized not in speakers:
                    speakers.append(normalized)
        if timeline_has_spoken:
            return speakers
        for dialogue in shot.dialogues or []:
            delivery = (
                dialogue.get("delivery", "spoken_dialogue")
                if isinstance(dialogue, dict)
                else dialogue.delivery
            )
            speaker = (
                dialogue.get("speaker") if isinstance(dialogue, dict) else dialogue.speaker
            )
            normalized = str(speaker or "").strip()
            if delivery == "spoken_dialogue" and normalized and normalized not in speakers:
                speakers.append(normalized)
        return speakers
    for segment in segments:
        if segment.delivery == "spoken_dialogue" and segment.lip_sync:
            speaker = (segment.speaker_id or "").strip()
            if speaker and speaker not in speakers:
                speakers.append(speaker)
    return speakers


def dialogue_two_shot_required(
    shot: Shot,
    *,
    narrative_authority: bool = False,
) -> bool:
    """仅真实双人肢体互动允许对白镜头保留第二个可见人物。"""
    if narrative_authority:
        # The model classifies the actor/target visibility intent from the
        # authority graph.  Do not re-infer it from a language-specific list of
        # contact verbs.
        return DIALOGUE_TWO_SHOT_RISK_TAG in (shot.risk_tags or [])
    speakers = onscreen_dialogue_speakers(shot)
    visible = raw_characters_visible(shot)
    if len(speakers) != 1 or len(visible) < 2:
        return False
    visual_text = "；".join(
        str(value or "")
        for value in (
            shot.primary_action,
            shot.action_desc,
            shot.first_frame_desc,
            shot.last_frame_desc,
        )
    )
    speaker = speakers[0]
    others = [name for name in visible if name != speaker]
    for clause in re.split(r"[，,。；;！？\n]", visual_text):
        if not _DIALOGUE_TWO_SHOT_INTERACTION_RE.search(clause):
            continue
        if speaker not in clause:
            continue
        if any(name in clause for name in others):
            return True
        if len(visible) == 2 and re.search(r"对方|他|她|其手|其肩|彼此", clause):
            return True
    return False


def dialogue_action_staging_kind(
    shot: Shot,
    *,
    narrative_authority: bool = False,
) -> str:
    """返回对白镜必须保留的动作调度类型：spatial / prop / 空。

    ``dialogue_focus_subject`` 会据此放弃“只拍脸”的派生构图，但仍保持一镜只有
    一位画内说话人。显式 risk tag 供人工编辑/历史数据在词面未命中时强制保留动作。
    """
    if len(onscreen_dialogue_speakers(shot)) != 1:
        return ""
    if narrative_authority:
        return "semantic" if "dialogue_action_staging" in (shot.risk_tags or []) else ""
    visual_text = "；".join(
        str(value or "")
        for value in (
            shot.primary_action,
            shot.action_desc,
            shot.first_frame_desc,
            shot.last_frame_desc,
        )
    )
    if _DIALOGUE_SPATIAL_STAGING_RE.search(visual_text):
        return "spatial"
    if _DIALOGUE_PROP_STAGING_RE.search(visual_text):
        return "prop"
    if "dialogue_action_staging" in (shot.risk_tags or []):
        return "prop"
    return ""


def dialogue_focus_subject(
    shot: Shot,
    *,
    narrative_authority: bool = False,
) -> str | None:
    """返回专业对白镜头的唯一画面主体；双人肢体互动属于显式例外。"""
    speakers = onscreen_dialogue_speakers(shot)
    if (
        len(speakers) != 1
        or dialogue_two_shot_required(shot, narrative_authority=narrative_authority)
        or dialogue_action_staging_kind(shot, narrative_authority=narrative_authority)
    ):
        return None
    visible = raw_characters_visible(shot)
    return speakers[0] if speakers[0] in visible else None


def dialogue_framing_errors(
    shot: Shot,
    *,
    strict_composition: bool = True,
    narrative_authority: bool = False,
) -> list[str]:
    """对白镜头构图门禁：一镜一位画内说话人，默认单人近景/特写。"""
    speakers = onscreen_dialogue_speakers(shot)
    if not speakers:
        return []
    if len(speakers) > 1:
        return [
            f"shot_no={shot.shot_no} 同一镜包含多个画内说话人 {speakers}；"
            "优秀漫剧对白应按话轮拆成相邻正反打，每镜只保留一位画内说话人"
        ]

    speaker = speakers[0]
    visible = raw_characters_visible(shot)
    errors: list[str] = []
    if speaker not in visible:
        errors.append(
            f"shot_no={shot.shot_no} 画内说话人「{speaker}」不在 characters_visible；"
            "需要口型的说话人必须入画，画外说话请改为 offscreen_voice"
        )
        return errors
    if dialogue_two_shot_required(shot, narrative_authority=narrative_authority):
        if len(visible) > 2:
            errors.append(
                f"shot_no={shot.shot_no} 虽有双人肢体互动，但画面声明了 {len(visible)} 人；"
                "对白双人镜最多保留说话人与直接互动对象，其余人物留在画外"
            )
        return errors
    staging_kind = dialogue_action_staging_kind(
        shot,
        narrative_authority=narrative_authority,
    )
    if staging_kind:
        # 动作对白仍只允许一个画内说话人，但不可为了口型裁掉走位、肢体或剧情道具。
        # 其他可见角色可作为无台词的直接互动对象/背景关系存在。
        if staging_kind == "spatial" and shot.shot_size not in {"远景", "全景", "中景"}:
            errors.append(
                f"shot_no={shot.shot_no} 的对白同时包含走位/离场等大形体动作，"
                f"shot_size 应为中景、全景或远景，当前为「{shot.shot_size}」；"
                "必须完整拍出动作，不能用单人大近景替代"
            )
        elif staging_kind == "prop" and shot.shot_size == "特写":
            errors.append(
                f"shot_no={shot.shot_no} 的对白同时包含剧情道具操作，shot_size 不得为特写；"
                "请至少使用近景并完整保留双手、道具和接触关系"
            )
        return errors
    if not strict_composition:
        return errors
    if visible != [speaker]:
        errors.append(
            f"shot_no={shot.shot_no} 是「{speaker}」的对白镜头，但画面可见角色为 {visible}；"
            "请只保留说话人，听者和人群留在画外，下一话轮再切反打/反应镜"
        )
    if shot.shot_size not in DIALOGUE_CLOSEUP_SHOT_SIZES:
        errors.append(
            f"shot_no={shot.shot_no} 是单人对白镜头，shot_size 应为近景或特写，"
            f"当前为「{shot.shot_size}」"
        )
    if shot.camera_move not in DIALOGUE_CLOSEUP_CAMERA_MOVES:
        errors.append(
            f"shot_no={shot.shot_no} 是单人对白镜头，camera_move 应为固定或推近，"
            f"当前为「{shot.camera_move}」"
        )
    return errors


def effective_state_in(shot: Shot) -> str:
    return (shot.state_in or shot.first_frame_desc or "").strip()


def effective_state_out(shot: Shot) -> str:
    return (shot.observed_state_out or shot.state_out or shot.last_frame_desc or "").strip()


def planned_state_out(shot: Shot) -> str:
    return (shot.state_out or shot.last_frame_desc or "").strip()


def effective_primary_action(shot: Shot) -> str:
    return (shot.primary_action or shot.action_desc or "").strip()


def effective_characters_visible(shot: Shot) -> list[str]:
    focus = dialogue_focus_subject(shot)
    if focus:
        return [focus]
    return list(dict.fromkeys([
        *raw_characters_visible(shot),
        *required_visual_action_characters(shot),
    ]))


def effective_audio_cast(shot: Shot) -> list[str]:
    """声轨说话人唯一口径：显式 audio_cast 优先，否则读有效口播段。"""
    if shot.audio_cast:
        return list(shot.audio_cast)
    return spoken_speakers(shot)


def uses_previous_tail_frame(mode: str) -> bool:
    return (mode or "").strip() == "action_continuation"


def derive_continuity_mode(shot: Shot, prev: Shot | None = None) -> str:
    """解析连续性模式。旧 continuity_from_prev 不得直接映射为 action_continuation。

    无上一镜时不得保留 action_continuation：单镜 preflight / 缺 prev 入队会把该镜
    当成链首，否则会误报「第一个镜头没有上一镜可承接」。
    """
    mode = (shot.continuity_mode or "").strip()
    if mode == "action_continuation" and dialogue_focus_subject(shot):
        # 对白近景是一次明确切镜，不能把上一镜尾帧当作 0 秒构图继续复制。
        mode = "same_scene_cut"
    if prev is None:
        if mode == "action_continuation" or mode not in CONTINUITY_MODES:
            return "scene_change" if int(shot.shot_no or 0) == 1 else "same_scene_cut"
        return mode
    same_context = (
        same_scene(shot, prev)
        and _scene_time_context(shot) == _scene_time_context(prev)
    )
    if mode == "scene_change" and same_context:
        return "same_scene_cut"
    if mode in CONTINUITY_MODES and not same_context:
        return "scene_change"
    if mode in CONTINUITY_MODES:
        return mode
    if not same_context:
        return "scene_change"
    # 旧数据 continuity_from_prev=true：仅表示同场景接镜，不是动作连续；默认 same_scene_cut
    if shot.continuity_from_prev:
        return "same_scene_cut"
    return "same_scene_cut"


def resolve_first_last_boundary_relation(
    shot: Shot,
    prev: Shot | None,
    *,
    planned_edit: str | None,
    planned_action: str | None,
) -> tuple[str, str, str]:
    """Correct boundary motion semantics from the accepted shot contracts."""
    edit = str(planned_edit or "unknown").strip()
    action = str(planned_action or "unknown").strip()
    mode = (shot.continuity_mode or "").strip()
    explicit_edit = {
        "reverse_angle": "reverse_angle",
        "reaction_cut": "reaction_cut",
        "insert_detail": "insert_cut",
        "action_continuation": "continuous_take",
    }.get(mode)
    if explicit_edit:
        return (
            explicit_edit,
            (
                "continues_same_action"
                if mode == "action_continuation"
                else "starts_new_action"
            ),
            f"storyboard_continuity_mode:{mode}",
        )
    if prev is None:
        return edit, action, "planned_relation"

    previous_speakers = onscreen_dialogue_speakers(prev)
    current_speakers = onscreen_dialogue_speakers(shot)
    if (
        len(previous_speakers) == 1
        and len(current_speakers) == 1
        and previous_speakers[0] != current_speakers[0]
    ):
        return "reverse_angle", "starts_new_action", "onscreen_speaker_changed"

    previous_visible = set(effective_characters_visible(prev))
    current_visible = set(effective_characters_visible(shot))
    if previous_visible != current_visible and edit == "continuous_take":
        return "angle_cut", "starts_new_action", "visible_cast_changed"
    return edit, action, "planned_relation"


def _merge_structured_entity(base: Any, overlay: Any) -> Any:
    """Merge a partial structured state without erasing inherited values with defaults."""
    merged = base.model_copy(deep=True)
    explicit = set(getattr(overlay, "model_fields_set", set()))
    for field in type(overlay).model_fields:
        value = getattr(overlay, field)
        if isinstance(value, str):
            if value.strip() and (field in explicit or not str(getattr(merged, field, "")).strip()):
                setattr(merged, field, value.strip())
        elif isinstance(value, dict):
            if value:
                current = dict(getattr(merged, field, {}) or {})
                current.update({str(key): str(item) for key, item in value.items() if str(item).strip()})
                setattr(merged, field, current)
        elif field in explicit:
            setattr(merged, field, value)
    return merged


def _merge_structured_state(base: ContinuityState, overlay: ContinuityState) -> ContinuityState:
    merged = base.model_copy(deep=True)
    merged.scene = _merge_structured_entity(merged.scene, overlay.scene)
    for name, state in overlay.characters.items():
        existing = merged.characters.get(name)
        merged.characters[name] = (
            _merge_structured_entity(existing, state) if existing is not None else state.model_copy(deep=True)
        )
    for prop_id, state in overlay.props.items():
        existing = merged.props.get(prop_id)
        merged.props[prop_id] = (
            _merge_structured_entity(existing, state) if existing is not None else state.model_copy(deep=True)
        )
    return merged


def _carried_state_across_scene(state: ContinuityState) -> ContinuityState:
    """Scene changes keep entity identity/state, never the old environment geometry."""
    characters = {}
    for name, item in state.characters.items():
        carried = item.model_copy(deep=True)
        # 画面方位和表演姿态属于旧场景构图，跨场继承会制造错误的站位约束。
        carried.screen_side = ""
        carried.pose = ""
        carried.facing = ""
        carried.gaze_target = ""
        characters[name] = carried
    carried = ContinuityState(
        characters=characters,
        props={prop_id: item.model_copy(deep=True) for prop_id, item in state.props.items()},
    )
    return carried


def inherit_structured_continuity_state(shot: Shot, prev: Shot | None = None) -> None:
    """Deterministically fill unchanged structured fields across a shot boundary.

    The model remains responsible only for explicit changes. This helper never judges or
    blocks a shot; it produces a complete comparison baseline for prompting and reports.
    """
    state_in = shot.continuity_state_in or ContinuityState()
    if prev is not None:
        previous_out = prev.continuity_state_out or ContinuityState()
        base = (
            _carried_state_across_scene(previous_out)
            if derive_continuity_mode(shot, prev) == "scene_change"
            else previous_out
        )
        state_in = _merge_structured_state(base, state_in)
    shot.continuity_state_in = state_in
    shot.continuity_state_out = _merge_structured_state(
        state_in,
        shot.continuity_state_out or ContinuityState(),
    )


def sync_shot_continuity_fields(shot: Shot, prev: Shot | None = None) -> str:
    """回填 state/continuity/visible 字段，并同步 legacy continuity_from_prev。"""
    mode = derive_continuity_mode(shot, prev)
    shot.continuity_mode = mode
    shot.continuity_from_prev = uses_previous_tail_frame(mode)
    inherit_structured_continuity_state(shot, prev)
    if not (shot.state_in or "").strip():
        shot.state_in = (shot.first_frame_desc or "").strip()
    if not (shot.state_out or "").strip():
        shot.state_out = (shot.last_frame_desc or "").strip()
    primary_action = (shot.primary_action or "").strip()
    action_desc = (shot.action_desc or "").strip()
    if not primary_action or (len(primary_action) < 8 and len(action_desc) >= 8):
        shot.primary_action = action_desc
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
    focus = dialogue_focus_subject(shot)
    tags = list(shot.risk_tags or [])
    if focus and DIALOGUE_FOCUS_RISK_TAG not in tags:
        tags.append(DIALOGUE_FOCUS_RISK_TAG)
    elif not focus and DIALOGUE_FOCUS_RISK_TAG in tags:
        tags.remove(DIALOGUE_FOCUS_RISK_TAG)
    if dialogue_two_shot_required(shot) and DIALOGUE_TWO_SHOT_RISK_TAG not in tags:
        tags.append(DIALOGUE_TWO_SHOT_RISK_TAG)
    if dialogue_action_staging_kind(shot) and "dialogue_action_staging" not in tags:
        tags.append("dialogue_action_staging")
    shot.risk_tags = tags
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
        same_location = same_scene(shot, prev)
        same_time = scene_time_of(shot) == scene_time_of(prev)
        if not same_location or not same_time:
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


def action_capacity_limit(duration_s: int | None) -> int:
    """Return the shared storyboard/video limit for sequential action beats."""
    return 2 if int(duration_s or 5) <= 6 else 3


def split_sequential_action_text(text: str) -> tuple[str, str] | None:
    """Split an overloaded action near its middle distinct verb.

    This is deliberately the structural counterpart of
    :func:`count_sequential_action_beats`: the storyboard planner and the paid
    video preflight use the same verb vocabulary, so a plan split cannot drift
    from the later provider gate.  Subject carry-over is handled by the outline
    layer, which knows the visible cast.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    matches: list[tuple[int, int]] = []
    for verb in _DISTINCT_ACTION_VERBS:
        start = raw.find(verb)
        if start >= 0:
            matches.append((start, start + len(verb)))
    matches.sort()
    non_overlapping: list[tuple[int, int]] = []
    for start, end in matches:
        if non_overlapping and start < non_overlapping[-1][1]:
            continue
        non_overlapping.append((start, end))
    if len(non_overlapping) < 2:
        return None
    split_at = non_overlapping[len(non_overlapping) // 2][0]
    front = raw[:split_at].rstrip(" 　，,；;、。然后接着随后之后再又紧接着同时")
    back = raw[split_at:].lstrip(" 　，,；;、。")
    if not front or not back:
        return None
    return front, back


def narrative_action_capacity_profile(
    shot: Any,
    narrative_plan: NarrativeContinuityPlan | None,
) -> tuple[int, float, list[str]]:
    """Return the action demand declared by the narrative authority graph.

    The new contract deliberately does not inspect ``action_desc`` or a verb
    vocabulary.  A shot task names its primary/supporting ``AtomicAction``
    objects and explicitly assigns ``action_phase_ids`` to this shot.  Those
    assigned phases are the sequential execution units; an action may span
    multiple adjacent shots without every shot being charged for every phase.
    Preconditions are required where the first phase starts and effects where
    the last phase completes.  An action without explicit phases is one
    indivisible primary-action unit.

    The returned tuple is ``(phase_count, estimated_min_s, contract_errors)``.
    Keeping the profile independent of prose makes fictional actions and
    synonymous rewrites capacity-equivalent by construction.
    """
    if narrative_plan is None:
        return 0, 0.0, [
            f"[NARRATIVE_ACTION_AUTHORITY_MISSING] shot_no={shot.shot_no} "
            "启用了叙事权威校验但缺少 narrative_plan"
        ]

    action_by_id = {
        str(action.action_id).strip(): action
        for action in narrative_plan.atomic_actions
        if str(action.action_id).strip()
    }
    phase_by_id = {
        str(phase.phase_id).strip(): (action, phase)
        for action in narrative_plan.atomic_actions
        for phase in (action.temporal_phases or [])
        if str(phase.phase_id).strip()
    }
    task_action_ids = list(dict.fromkeys([
        *(
            [str(shot.primary_action_id).strip()]
            if getattr(shot, "primary_action_id", None)
            else []
        ),
        *[
            str(action_id).strip()
            for action_id in (getattr(shot, "supporting_action_ids", []) or [])
            if str(action_id).strip()
        ],
    ]))
    raw_phase_ids = [
        str(phase_id).strip()
        for phase_id in (getattr(shot, "action_phase_ids", []) or [])
    ]
    assigned_phase_ids = list(dict.fromkeys(
        phase_id for phase_id in raw_phase_ids if phase_id
    ))
    errors: list[str] = []
    if any(not phase_id for phase_id in raw_phase_ids) or len(assigned_phase_ids) != len(raw_phase_ids):
        errors.append(
            f"[NARRATIVE_ACTION_PHASE_ASSIGNMENT_INVALID] shot_no={shot.shot_no} "
            "action_phase_ids 含空值或重复阶段"
        )

    if not task_action_ids:
        # Establishing/reaction/assimilation shots may intentionally be
        # action-free; their ShotContribution is validated by narrative.py.
        if assigned_phase_ids:
            errors.append(
                f"[NARRATIVE_ACTION_PHASE_WITHOUT_ACTION] shot_no={shot.shot_no} "
                "未绑定 AtomicAction 却分配了 action_phase_ids"
            )
        return 0, 0.0, errors

    phase_count = 0
    estimated_min_s = 0.0
    task_state_in = set(getattr(shot, "planned_state_in_fact_ids", []) or [])
    task_adds = set(getattr(shot, "planned_delta_add_fact_ids", []) or [])
    task_removes = set(getattr(shot, "planned_delta_remove_fact_ids", []) or [])
    valid_assigned: dict[str, tuple[Any, Any]] = {}
    for phase_id in assigned_phase_ids:
        owned = phase_by_id.get(phase_id)
        if owned is None:
            errors.append(
                f"[NARRATIVE_ACTION_PHASE_REF_MISSING] shot_no={shot.shot_no} "
                f"action_phase_ids 引用了不存在的阶段 {phase_id}"
            )
            continue
        owner, phase = owned
        if str(owner.action_id).strip() not in task_action_ids:
            errors.append(
                f"[NARRATIVE_ACTION_PHASE_OWNER_MISMATCH] shot_no={shot.shot_no} "
                f"阶段 {phase_id} 不属于本镜绑定的 AtomicAction"
            )
            continue
        valid_assigned[phase_id] = (owner, phase)

    for action_id in task_action_ids:
        action = action_by_id.get(action_id)
        if action is None:
            errors.append(
                f"[NARRATIVE_ACTION_REF_MISSING] shot_no={shot.shot_no} "
                f"镜头任务引用了不存在的 AtomicAction {action_id}"
            )
            continue
        phases = list(action.temporal_phases or [])
        phase_ids = [str(phase.phase_id).strip() for phase in phases]
        delivered_phase_ids = [
            phase_id for phase_id in assigned_phase_ids if phase_id in set(phase_ids)
        ]
        if phases and not delivered_phase_ids:
            errors.append(
                f"[NARRATIVE_ACTION_PHASE_ASSIGNMENT_MISSING] shot_no={shot.shot_no} "
                f"AtomicAction {action_id} 未声明本镜实际负责的 action_phase_ids"
            )
        elif not phases:
            if action_id in set(getattr(shot, "supporting_action_ids", []) or []):
                errors.append(
                    f"[NARRATIVE_PHASELESS_SUPPORTING_ACTION_INVALID] shot_no={shot.shot_no} "
                    f"AtomicAction {action_id} 没有可分阶段，不得作为 supporting action"
                )
            else:
                phase_count += 1
        else:
            phase_count += len(delivered_phase_ids)
            estimated_min_s += sum(
                max(0.0, float(valid_assigned[phase_id][1].estimated_min_s or 0.0))
                for phase_id in delivered_phase_ids
                if phase_id in valid_assigned
            )

        starts_action = not phases or bool(phase_ids and phase_ids[0] in delivered_phase_ids)
        completes_action = not phases or bool(phase_ids and phase_ids[-1] in delivered_phase_ids)
        missing_preconditions = (
            set(action.precondition_fact_ids) - task_state_in if starts_action else set()
        )
        missing_adds = set(action.effects_add) - task_adds if completes_action else set()
        missing_removes = set(action.effects_remove) - task_removes if completes_action else set()
        if missing_preconditions or missing_adds or missing_removes:
            details: list[str] = []
            if missing_preconditions:
                details.append(f"precondition={sorted(missing_preconditions)}")
            if missing_adds:
                details.append(f"effects_add={sorted(missing_adds)}")
            if missing_removes:
                details.append(f"effects_remove={sorted(missing_removes)}")
            errors.append(
                f"[NARRATIVE_SHOT_TASK_ACTION_DRIFT] shot_no={shot.shot_no} "
                f"未完整承接 {action_id} 的前置/效果合同：{', '.join(details)}"
            )
        if not str(action.completion_condition or "").strip():
            errors.append(
                f"[NARRATIVE_ACTION_COMPLETION_MISSING] shot_no={shot.shot_no} "
                f"AtomicAction {action_id} 缺少可观察完成条件"
            )

    budget = getattr(shot, "capacity_budget", None)
    if budget is not None and float(budget.action_phase_s or 0.0) + 1e-9 < estimated_min_s:
        errors.append(
            f"[NARRATIVE_ACTION_BUDGET_UNDERSTATED] shot_no={shot.shot_no} "
            f"capacity_budget.action_phase_s={float(budget.action_phase_s or 0.0):g}s，"
            f"低于已分配阶段的最短执行时间 {estimated_min_s:g}s"
        )
    return phase_count, estimated_min_s, errors


def action_capacity_errors(
    shot: Shot,
    *,
    narrative_authority: bool = False,
    narrative_plan: NarrativeContinuityPlan | None = None,
) -> list[str]:
    errors: list[str] = []
    if narrative_authority:
        phases, estimated_min_s, contract_errors = narrative_action_capacity_profile(
            shot, narrative_plan,
        )
        errors.extend(contract_errors)
        # The authority path is governed by an explicit time equation, not a
        # genre-agnostic phase-count table.  Three very short observable phases
        # can be cheaper than one long transformation; only their declared
        # minimum time and the joint audience-task budget determine feasibility.
        if estimated_min_s > float(getattr(shot, "duration_s", 0) or 0):
            errors.append(
                f"[NARRATIVE_ACTION_TIME_CAPACITY_EXCEEDED] shot_no={shot.shot_no} "
                f"AtomicAction 阶段最短执行时间 {estimated_min_s:g}s "
                f"超过镜头时长 {shot.duration_s}s"
            )
        return list(dict.fromkeys(errors))

    # primary_action 常被模型压成一句摘要，真正会交给视频模型执行的细节仍在
    # action_desc。容量必须取两者中节拍更多的一份，否则“穿过人群→停下→开口”
    # 会以单动作摘要绕过门禁，最终只剩静态口型。
    action_candidates = [
        text.strip() for text in (effective_primary_action(shot), shot.action_desc or "") if text.strip()
    ]
    beats = max((count_sequential_action_beats(text) for text in action_candidates), default=0)
    limit = action_capacity_limit(getattr(shot, "duration_s", 5))
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


def spoken_chars_from_shot(shot: Shot) -> int:
    """本镜真实台词纯文字字数（不计标点、不计旁白）。

    统一读取有效口播段：字数、关键台词覆盖、声轨统计、prompt 编译共用同一口径，
    杜绝「容量从 timeline 统计、丢词从 dialogues 统计」的矛盾诊断（VAL-422 根因 R1）。
    """
    return spoken_char_total(shot)


def speech_capacity_errors(shot: Shot) -> list[str]:
    """口播容量的唯一实现（PRD §4.7）；说话人构图由 dialogue_framing_errors 负责。"""
    errors: list[str] = []
    capacity = capacity_issue(shot)
    if capacity is not None:
        errors.append(capacity.message)
    return errors


def implicit_speech_without_dialogue_errors(shot: Shot) -> list[str]:
    """Reject visual instructions that force a silent shot to invent speech."""
    if effective_spoken_segments(shot):
        return []
    conflicts: list[str] = []
    for field in (
        "primary_action",
        "action_desc",
        "first_frame_desc",
        "last_frame_desc",
    ):
        value = str(getattr(shot, field, "") or "").strip()
        if _IMPLICIT_SPEECH_RE.search(value):
            conflicts.append(f"{field}=「{value[:48]}」")
    if not conflicts:
        return []
    return [
        f"shot_no={shot.shot_no} 没有有效 dialogues/audio_timeline 口播，"
        f"但画面合同要求人物说话（{'；'.join(conflicts)}）；"
        "禁止让视频模型自行发明台词。请补入有原文/剧本依据的准确台词，"
        "或把本镜改为明确闭口的非语言动作/反应镜"
    ]


def spoken_contract_coherence_errors(shot: Shot) -> list[str]:
    """口播字段一致性与时间轴合法性；容量由 speech_capacity_errors 单独负责，避免重复报告。"""
    return [
        issue.message for issue in validate_spoken_contract(shot)
        if issue.rule_id != RULE_SPOKEN_CAPACITY
    ]


def shot_id_space_errors(shot: Shot) -> list[str]:
    """E/S/I/KL 四类 ID 不得混用（PRD VAL-422 §4.4.1）。

    事故里 `story_event_id` 存的是 S07 这种主线节拍 ID，导致「按事件归属聚合」根本无从建立，
    主线覆盖只能退回全局字面匹配。这里在写入侧就把混用拦下来。
    """
    errors: list[str] = []
    event_id = (shot.story_event_id or "").strip()
    if event_id and SPINE_BEAT_ID_RE.match(event_id):
        errors.append(
            f"shot_no={shot.shot_no}.story_event_id=「{event_id}」是主线节拍 ID；"
            "story_event_id 只能写剧本事件 E*，主线节拍请写入 spine_beat_ids"
        )
    elif event_id and not STORY_EVENT_ID_RE.match(event_id):
        errors.append(
            f"shot_no={shot.shot_no}.story_event_id=「{event_id}」不是合法剧本事件 ID；"
            "请写 screenplay.events[].event_id（形如 E1/E5），或留空"
        )
    for beat_id in shot.spine_beat_ids or []:
        if not SPINE_BEAT_ID_RE.match(str(beat_id).strip()):
            errors.append(
                f"shot_no={shot.shot_no}.spine_beat_ids 含「{beat_id}」；"
                "只能引用 plot_spine.spine_beats[].beat_id（形如 S04）"
            )
    for key_line_id in shot.key_line_ids or []:
        if not KEY_LINE_ID_RE.match(str(key_line_id).strip()):
            errors.append(
                f"shot_no={shot.shot_no}.key_line_ids 含「{key_line_id}」；"
                "关键台词只能引用形如 KL01 的稳定 ID"
            )
    return errors


def migrate_shot_id_spaces(shot: Shot) -> list[str]:
    """旧数据只读迁移（PRD §6.2）：把误存进 story_event_id 的 S* 移到 spine_beat_ids。

    无法确定真正 E* 时置空并标记 legacy_unvalidated，不靠猜测合并多个事件。
    """
    actions: list[str] = []
    event_id = (shot.story_event_id or "").strip()
    if event_id and SPINE_BEAT_ID_RE.match(event_id):
        beat_id = event_id.upper()
        if beat_id not in (shot.spine_beat_ids or []):
            shot.spine_beat_ids = [*(shot.spine_beat_ids or []), beat_id]
        shot.story_event_id = ""
        shot.legacy_unvalidated = True
        actions.append(f"moved_story_event_id_{beat_id}_to_spine_beat_ids")
    return actions


def build_audio_timeline_from_legacy(shot: Shot, voice_bible: list[VoiceCanonical] | None = None
                                     ) -> list[AudioTimelineItem]:
    """从 dialogues 推导音频时间线（产品禁止旁白，不再写入 narration 轨）。"""
    if shot.audio_timeline:
        # 历史脏数据：丢掉 narration 轨，只保留真实台词与环境声
        return [item for item in shot.audio_timeline if item.type != "narration"]
    return build_timeline_from_segments(
        shot, segments_from_dialogues(shot, voice_bible), voice_bible
    )


def ensure_audio_timeline(shot: Shot, voice_bible: list[VoiceCanonical] | None = None) -> None:
    """收敛口播合同并保证 timeline/audio_cast 可用。

    旧实现只补空字段，两侧分叉时静默放行——这正是第 9 镜「timeline 有关键台词、dialogues 没有」
    却一路走到确认门的原因。现在改为调用 `synchronize_spoken_contract`：能确定性派生就派生，
    真冲突则标记 `spoken_contract_status=conflict`，交由校验器拦截，不静默覆盖任何一侧。
    """
    synchronize_spoken_contract(shot, voice_bible=voice_bible)
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
    """Validate the legacy ledger without mutating screenplay truth.

    New narrative contracts assign delivery to a per-prior target delta in
    ``ShotContribution``.  Repeating an info alias may be a valid suspicion →
    confirmation or recall beat, so only the narrative ownership validator may
    decide it.  The old ID rule remains solely for legacy artifacts.
    """
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
            if (
                screenplay.narrative_plan is None
                and info_id in delivered
                and info_id not in (shot.reinforcement_info_ids or [])
            ):
                item = ledger.get(info_id)
                reinforce = bool(item and item.reinforcement_allowed)
                if not reinforce:
                    errors.append(
                        f"shot_no={shot.shot_no} 重复交付已在镜{delivered[info_id]}交付的信息 {info_id}"
                        f"（{item.content if item else ''}）；如需强调请标记 reinforcement_allowed / reinforcement_info_ids"
                    )
            delivered[info_id] = shot.shot_no
    return errors


def state_chain_errors(
    board: Storyboard,
    *,
    narrative_authority: bool = False,
) -> list[str]:
    errors: list[str] = []
    for i, shot in enumerate(board.shots):
        prev = board.shots[i - 1] if i > 0 else None
        mode = (
            (shot.continuity_mode or "").strip()
            if narrative_authority
            else sync_shot_continuity_fields(shot, prev)
        )
        state_in = effective_state_in(shot)
        state_out = planned_state_out(shot)
        action = effective_primary_action(shot)
        tag = f"shot_no={shot.shot_no}"
        if len(state_in) < 8:
            errors.append(f"{tag}.state_in 缺失或过短；必须写清精确起始状态")
        if len(state_out) < 8:
            errors.append(f"{tag}.state_out 缺失或过短；必须写清精确结束状态")
        # A reaction, establishing, processing or spatial-orientation shot may
        # intentionally have no new atomic action.  Its structured narrative
        # contribution is the hard contract; legacy shots still require the
        # textual primary action for backward compatibility.
        if len(action) < 8 and shot.shot_contribution is None:
            errors.append(f"{tag}.primary_action 缺失或过短")
        if mode not in CONTINUITY_MODES:
            errors.append(f"{tag}.continuity_mode=「{mode}」不在 {sorted(CONTINUITY_MODES)}")
        if i == 0 and mode == "action_continuation":
            errors.append(f"{tag}.continuity_mode=action_continuation，但第一个镜头没有上一镜可承接")
        if prev is not None and mode == "action_continuation":
            prev_out = effective_state_out(prev)
            # Contract-less boards have no stable state IDs, so their legacy
            # fallback can only compare prose.  Narrative boards instead carry
            # explicit fact sets and boundary contracts, which are checked by
            # ``validate_storyboard_narrative``.  Never reject a modern board
            # merely because two natural-language renderings share too few
            # Chinese bigrams: that is a language-specific guess, not state
            # evidence.
            if (
                not narrative_authority
                and prev_out
                and state_in
                and _too_divergent(prev_out, state_in)
            ):
                errors.append(
                    f"{tag}.state_in 与上一镜 state_out/observed_state_out 矛盾："
                    f"上一镜结束于「{prev_out[:40]}」，本镜却从「{state_in[:40]}」开始；"
                    "action_continuation 要求当前 state_in 等于上一镜实际尾状态"
                )
        if prev is not None:
            same_context = (
                same_scene(shot, prev)
                and _scene_time_context(shot) == _scene_time_context(prev)
            )
            if mode == "scene_change" and same_context:
                errors.append(f"{tag}.continuity_mode=scene_change 但 scene_name/scene_time 与上一镜相同")
            if mode != "scene_change" and not same_context:
                errors.append(
                    f"{tag}.continuity_mode={mode} 但 scene_name/scene_time 已变化；"
                    "跨场应使用 scene_change"
                )
    return errors


def _state_value(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def structured_boundary_issues(prev: Shot, current: Shot) -> list[dict[str, Any]]:
    """比较上一镜结束与当前镜开始的结构化状态。

    返回值只用于定向修复与风险报告，不得单独阻断生成或合成。
    """
    before = prev.continuity_state_out or ContinuityState()
    after = current.continuity_state_in or ContinuityState()
    mode = derive_continuity_mode(current, prev)
    issues: list[dict[str, Any]] = []

    def add(code: str, subject: str, expected: str, actual: str, severity: str = "warning") -> None:
        issues.append({
            "code": code,
            "severity": severity,
            "from_shot_no": prev.shot_no,
            "to_shot_no": current.shot_no,
            "subject": subject,
            "expected": expected,
            "actual": actual,
            "repairable": True,
            "runtime_blocking": False,
        })

    if mode != "scene_change":
        scene_pairs = (
            ("scene_revision_id", "BOUNDARY_SCENE_REVISION"),
            ("time_of_day", "BOUNDARY_TIME_OF_DAY"),
            ("lighting_state", "BOUNDARY_LIGHTING"),
            ("axis_id", "BOUNDARY_CAMERA_AXIS"),
        )
        for field, code in scene_pairs:
            expected = _state_value(getattr(before.scene, field, ""))
            actual = _state_value(getattr(after.scene, field, ""))
            if expected and actual and expected != actual:
                add(code, f"scene.{field}", expected, actual)
        for landmark, expected_raw in before.scene.landmarks.items():
            expected = _state_value(expected_raw)
            actual = _state_value(after.scene.landmarks.get(landmark))
            if expected and actual and expected != actual:
                add("BOUNDARY_LANDMARK", f"scene.landmarks.{landmark}", expected, actual)

    for name, expected_character in before.characters.items():
        actual_character = after.characters.get(name)
        if not actual_character:
            continue
        for field, code in (
            ("look_revision_id", "BOUNDARY_CHARACTER_IDENTITY"),
            ("outfit_revision_id", "BOUNDARY_CHARACTER_OUTFIT"),
        ):
            expected = _state_value(getattr(expected_character, field, ""))
            actual = _state_value(getattr(actual_character, field, ""))
            if expected and actual and expected != actual:
                add(code, f"characters.{name}.{field}", expected, actual, "high")
        # 反打允许画面左右改变，但手持物与姿态状态仍需承接。
        if mode != "reverse_angle":
            expected_side = _state_value(expected_character.screen_side)
            actual_side = _state_value(actual_character.screen_side)
            if expected_side and actual_side and expected_side != actual_side:
                add("BOUNDARY_SCREEN_SIDE", f"characters.{name}.screen_side", expected_side, actual_side)
        for hand in ("left_hand", "right_hand"):
            expected = _state_value(getattr(expected_character, hand, ""))
            actual = _state_value(getattr(actual_character, hand, ""))
            if expected and actual and expected != actual:
                add("BOUNDARY_HAND_OCCUPANCY", f"characters.{name}.{hand}", expected, actual)

    for prop_id, expected_prop in before.props.items():
        actual_prop = after.props.get(prop_id)
        if not actual_prop:
            if expected_prop.required or expected_prop.visibility == "required":
                add(
                    "BOUNDARY_PROP_MISSING",
                    f"props.{prop_id}",
                    expected_prop.canonical_name or prop_id,
                    "missing",
                    "high",
                )
            continue
        for field, code, severity in (
            ("revision_id", "BOUNDARY_PROP_IDENTITY", "high"),
            ("owner", "BOUNDARY_PROP_OWNER", "high"),
            ("location", "BOUNDARY_PROP_LOCATION", "warning"),
            ("form", "BOUNDARY_PROP_FORM", "high"),
            ("text_state", "BOUNDARY_PROP_TEXT_STATE", "warning"),
        ):
            expected = _state_value(getattr(expected_prop, field, ""))
            actual = _state_value(getattr(actual_prop, field, ""))
            if expected and actual and expected != actual:
                add(code, f"props.{prop_id}.{field}", expected, actual, severity)
    return issues


def structured_state_prompt(shot: Shot) -> str:
    """把结构化状态渲染为紧凑的生成约束；空合同不增加 prompt 噪声。"""
    state_in = shot.continuity_state_in or ContinuityState()
    state_out = shot.continuity_state_out or ContinuityState()
    if not (
        state_in.characters or state_in.props or state_out.characters or state_out.props
        or any(state_in.scene.model_dump().values()) or any(state_out.scene.model_dump().values())
    ):
        return ""
    return (
        "结构化起始状态："
        + json.dumps(state_in.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        + "\n结构化结束状态："
        + json.dumps(state_out.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        + "\n未在当前主动作中明示改变的 revision_id、owner、location、form、手持状态与空间轴线必须保持不变。"
    )


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


def required_text_strategy(shot: Shot) -> str:
    required = shot.required_text
    if not required or not (required.exact_text or "").strip():
        return "none"
    strategy = str(getattr(required, "strategy", "") or "deterministic_insert").strip()
    if strategy not in {"audio_only", "deterministic_insert", "embedded_prop", "none"}:
        return "deterministic_insert"
    return strategy


def _allowed_prompt_verbatim_texts(shot: Shot) -> list[str]:
    """最终提示词中允许原样出现的文本（台词 / 画面必现字），不视为 source_excerpt 泄漏。"""
    allowed: list[str] = []
    for dialogue in shot.dialogues or []:
        line = (getattr(dialogue, "line", None) or "").strip()
        if line:
            allowed.append(line)
    for item in shot.audio_timeline or []:
        if isinstance(item, dict):
            text = (item.get("text") or "").strip()
        else:
            text = (getattr(item, "text", None) or "").strip()
        if text:
            allowed.append(text)
    required = shot.required_text
    if required is not None:
        exact = (getattr(required, "exact_text", None) or "").strip()
        if exact:
            allowed.append(exact)
    # 长句优先剔除，避免短句先替换导致长句残留
    return sorted(dict.fromkeys(allowed), key=len, reverse=True)


SOURCE_EXCERPT_MIN_OVERLAP = 24


def source_excerpt_overlap_spans(
    text: str,
    excerpt: str,
    *,
    min_chars: int = SOURCE_EXCERPT_MIN_OVERLAP,
) -> list[tuple[int, int]]:
    """返回 ``text`` 中与原文任意位置连续重合的区间。

    旧逻辑只检查 ``excerpt[:24]``，因此原文中段被复制进 prompt 时会漏检。
    这里使用 24 字种子并向右扩展，既能覆盖任意中段，也避免把短的常用句式
    当成原文泄漏。返回值按文本顺序排列且不会重叠。
    """
    haystack = text or ""
    source = (excerpt or "").strip()
    threshold = max(1, int(min_chars))
    if len(haystack) < threshold or len(source) < threshold:
        return []

    source_windows: dict[str, list[int]] = {}
    for index in range(0, len(source) - threshold + 1):
        source_windows.setdefault(source[index:index + threshold], []).append(index)

    spans: list[tuple[int, int]] = []
    cursor = 0
    last_seed = len(haystack) - threshold
    while cursor <= last_seed:
        starts = source_windows.get(haystack[cursor:cursor + threshold])
        if not starts:
            cursor += 1
            continue
        best_end = cursor + threshold
        for source_start in starts:
            text_end = cursor + threshold
            source_end = source_start + threshold
            while (
                text_end < len(haystack)
                and source_end < len(source)
                and haystack[text_end] == source[source_end]
            ):
                text_end += 1
                source_end += 1
            best_end = max(best_end, text_end)
        spans.append((cursor, best_end))
        cursor = best_end
    return spans


def forbidden_prompt_content_errors(prompt_text: str, shot: Shot) -> list[str]:
    """最终提示词不得含原文/完整前镜动作/未来剧情。

    台词与画面必现字可以与 source_excerpt 重合；从 prompt 中剔除这些允许文本后，
    扫描原文任意位置的长连续片段。
    """
    errors: list[str] = []
    text = prompt_text or ""
    if "小说原文兜底参考：" in text or "SOURCE_EXCERPT" in text:
        errors.append(f"shot_no={shot.shot_no} 最终提示词包含原文章节摘录")
    excerpt = (shot.source_excerpt or "").strip()
    if excerpt:
        remainder = text
        for allowed in _allowed_prompt_verbatim_texts(shot):
            if allowed:
                remainder = remainder.replace(allowed, "")
        # 允许极短偶然重合；任意位置超过 24 字连续命中视为注入原文。
        if source_excerpt_overlap_spans(remainder, excerpt):
            errors.append(f"shot_no={shot.shot_no} 最终提示词包含 source_excerpt 原文内容")
    return errors


def reference_role_plan(
    shot: Shot,
    *,
    continuity_mode: str | None = None,
    individual_names: set[str] | None = None,
    collective_names: set[str] | None = None,
) -> list[str]:
    from app.character_policy import is_collective_role

    mode = continuity_mode or derive_continuity_mode(shot)
    roles: list[str] = []
    if uses_previous_tail_frame(mode):
        roles.append("start_state_reference")
    if mode != "scene_change":
        roles.append("scene_reference")
    visible_names = effective_characters_visible(shot)
    for name in visible_names:
        is_collective = (
            name in collective_names
            if collective_names is not None
            else is_collective_role(name)
        )
        roles.append(
            f"collective_group:{name}"
            if is_collective and (individual_names is None or name not in individual_names)
            else f"character_identity:{name}"
        )
    if required_text_strategy(shot) == "embedded_prop":
        roles.append("text_surface_reference")
    for prop_id, prop in (shot.continuity_state_in.props or {}).items():
        if prop.revision_id:
            roles.append(f"prop_identity:{prop_id}")
        if prop.form:
            roles.append(f"prop_state:{prop_id}:{prop.form}")
    if shot.reference_roles:
        # 保留显式声明，同时保证强制规则
        auto_entity_roles = {
            role
            for name in visible_names
            for role in (f"character_identity:{name}", f"collective_group:{name}")
        }
        explicit_roles = [role for role in shot.reference_roles if role not in auto_entity_roles]
        merged = list(dict.fromkeys([*roles, *explicit_roles]))
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
    narrative_plan = screenplay.narrative_plan if screenplay else None
    if narrative_plan is None:
        sync_shot_continuity_fields(shot, prev)
    ensure_audio_timeline(shot, screenplay.voice_bible if screenplay else None)
    errors: list[str] = []
    errors.extend(implicit_speech_without_dialogue_errors(shot))
    if narrative_plan is None:
        errors.extend(action_capacity_errors(
            shot,
            narrative_authority=False,
            narrative_plan=None,
        ))
        errors.extend(speech_capacity_errors(shot))
        errors.extend(dialogue_framing_errors(
            shot,
            strict_composition=False,
            narrative_authority=False,
        ))
    errors.extend(state_chain_errors(
        Storyboard(episode_no=0, shots=([prev, shot] if prev else [shot])),
        narrative_authority=narrative_plan is not None,
    ))
    # 只保留本镜报错，避免上一镜 shot_no=N 的状态链错误漏进当前生成
    shot_tag = f"shot_no={shot.shot_no}"
    errors = [e for e in errors if shot_tag in e]

    if narrative_plan is None:
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

    return errors


def mark_legacy_unvalidated(shot: Shot) -> None:
    missing = not (
        (shot.state_in or shot.first_frame_desc)
        and (shot.state_out or shot.last_frame_desc)
        and (shot.continuity_mode in CONTINUITY_MODES)
        and (shot.audio_timeline or shot.dialogues is not None)
        and (shot.story_event_id or shot.new_information_ids or shot.shot_contribution)
    )
    shot.legacy_unvalidated = bool(missing)


def shot_contract_dict(shot: Shot) -> dict[str, Any]:
    """持久化到 shots.shot_contract_json 的生产契约字段。"""
    required = None
    if shot.required_text is not None:
        required = shot.required_text.model_dump()
    shot_contribution = (
        shot.shot_contribution.model_dump(mode="json")
        if shot.shot_contribution is not None
        else None
    )
    narrative_boundary = (
        shot.narrative_boundary_from_previous.model_dump(mode="json")
        if shot.narrative_boundary_from_previous is not None
        else None
    )
    capacity_budget = (
        shot.capacity_budget.model_dump(mode="json")
        if shot.capacity_budget is not None
        else None
    )
    return {
        "story_event_id": shot.story_event_id,
        "purpose": shot.purpose,
        "spine_beat_ids": list(shot.spine_beat_ids or []),
        "key_line_ids": list(shot.key_line_ids or []),
        "information_ids": list(shot.information_ids or []),
        "new_information_ids": list(shot.new_information_ids or []),
        "reinforcement_info_ids": list(shot.reinforcement_info_ids or []),
        "spoken_contract_status": shot.spoken_contract_status or "legacy",
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
        "continuity_state_in": shot.continuity_state_in.model_dump(mode="json"),
        "continuity_state_out": shot.continuity_state_out.model_dump(mode="json"),
        "reference_roles": list(shot.reference_roles or []),
        "do_not_repeat": list(shot.do_not_repeat or []),
        "risk_tags": list(shot.risk_tags or []),
        "prompt_contract_version": shot.prompt_contract_version or PROMPT_CONTRACT_VERSION,
        "legacy_unvalidated": bool(shot.legacy_unvalidated),
        "camera_angle": shot.camera_angle,
        "spatial_anchor": shot.spatial_anchor,
        "is_final": bool(shot.is_final),
        # 纯叙事任务同样属于镜头的权威合同。它们只存在内存模型会导致
        # 服务重启、人工编辑或修复投影后丢失观众状态与证据所有权。
        "shot_id": shot.shot_id,
        "scene_id": shot.scene_id,
        "event_ids": list(shot.event_ids or []),
        "primary_action_id": shot.primary_action_id,
        "supporting_action_ids": list(shot.supporting_action_ids or []),
        "action_phase_ids": list(shot.action_phase_ids or []),
        "visible_entity_ids": list(shot.visible_entity_ids or []),
        "offscreen_action_actor_ids": list(shot.offscreen_action_actor_ids or []),
        "offscreen_action_target_ids": list(shot.offscreen_action_target_ids or []),
        "capacity_budget": capacity_budget,
        "shot_contribution": shot_contribution,
        "audience_state_paths": [
            item.model_dump(mode="json") for item in (shot.audience_state_paths or [])
        ],
        "planned_state_in_fact_ids": list(shot.planned_state_in_fact_ids or []),
        "planned_delta_add_fact_ids": list(shot.planned_delta_add_fact_ids or []),
        "planned_delta_remove_fact_ids": list(shot.planned_delta_remove_fact_ids or []),
        "planned_state_out_fact_ids": list(shot.planned_state_out_fact_ids or []),
        "completed_before_action_ids": list(shot.completed_before_action_ids or []),
        "completed_before_action_phase_ids": list(
            shot.completed_before_action_phase_ids or []
        ),
        "reserved_future_event_ids": list(shot.reserved_future_event_ids or []),
        "readability_window_ids": list(shot.readability_window_ids or []),
        "narrative_boundary_from_previous": narrative_boundary,
    }


def apply_shot_contract(shot: Shot, payload: dict[str, Any] | str | None) -> Shot:
    if not payload:
        return shot
    data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    for key in (
        "story_event_id", "purpose", "state_in", "primary_action", "emotion_beat",
        "state_out", "observed_state_out", "continuity_mode", "prompt_contract_version",
        "camera_angle", "spatial_anchor", "spoken_contract_status",
    ):
        if data.get(key) not in (None, ""):
            setattr(shot, key, data[key])
    for key in (
        "spine_beat_ids", "key_line_ids", "information_ids",
        "new_information_ids", "reinforcement_info_ids", "characters_visible",
        "audio_cast", "reference_roles", "do_not_repeat", "risk_tags",
        "event_ids", "supporting_action_ids", "planned_state_in_fact_ids",
        "action_phase_ids", "visible_entity_ids", "offscreen_action_actor_ids",
        "offscreen_action_target_ids",
        "planned_delta_add_fact_ids", "planned_delta_remove_fact_ids",
        "planned_state_out_fact_ids", "completed_before_action_ids",
        "completed_before_action_phase_ids",
        "reserved_future_event_ids", "readability_window_ids",
    ):
        if key in data and data[key] is not None:
            setattr(shot, key, list(data[key] or []))
    for key in ("shot_id", "scene_id"):
        if key in data and data[key] is not None:
            setattr(shot, key, str(data[key]))
    if "primary_action_id" in data:
        value = data["primary_action_id"]
        shot.primary_action_id = str(value) if value not in (None, "") else None
    # 旧 payload 只有 new_information_ids；schema 校验器已双向归一，此处补齐直接 setattr 的分支。
    merged_info = list(dict.fromkeys([*(shot.information_ids or []), *(shot.new_information_ids or [])]))
    shot.information_ids = merged_info
    shot.new_information_ids = merged_info
    if "audio_timeline" in data and data["audio_timeline"] is not None:
        shot.audio_timeline = [AudioTimelineItem.model_validate(x) for x in data["audio_timeline"]]
    if "required_text" in data:
        rt = data["required_text"]
        shot.required_text = RequiredOnScreenText.model_validate(rt) if rt else None
    if "shot_contribution" in data:
        contribution = data["shot_contribution"]
        shot.shot_contribution = (
            ShotContribution.model_validate(contribution) if contribution else None
        )
    if "capacity_budget" in data:
        budget = data["capacity_budget"]
        shot.capacity_budget = ShotCapacityBudget.model_validate(budget) if budget else None
    if "audience_state_paths" in data:
        shot.audience_state_paths = [
            AudienceStatePathRef.model_validate(item)
            for item in (data["audience_state_paths"] or [])
        ]
    if "narrative_boundary_from_previous" in data:
        boundary = data["narrative_boundary_from_previous"]
        shot.narrative_boundary_from_previous = (
            NarrativeBoundaryContract.model_validate(boundary) if boundary else None
        )
    for key in ("continuity_state_in", "continuity_state_out"):
        if key in data and data[key] is not None:
            setattr(shot, key, ContinuityState.model_validate(data[key] or {}))
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
                "确需拆镜时按剧情自然拆分，禁止为细节无限拆碎"
            )
    return errors


HARD_QA_FAILURE_TYPES = {
    "story_repeat",
    "future_leak",
    "wrong_dialogue",
    "text_error",
    "required_text_error",
    "character_duplicate",
    "state_mismatch",
    "needs_crop",
    "wrong_identity",
    "wrong_outfit",
    "subject_occlusion",
    "action_missing",
    "wrong_action",
    "prop_identity_mismatch",
    "prop_state_mismatch",
    "object_count_mismatch",
    "wrong_camera_axis",
    "geometry_guard_unverified",
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
    aliases = {
        "wrong_action": "action_missing",
        "required_text_error": "text_error",
    }
    failure_types = [aliases.get(str(x), str(x)) for x in (qa.get("failure_types") or [])]
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
        ("错人", "wrong_identity"),
        ("身份", "wrong_identity"),
        ("换脸", "wrong_identity"),
        ("服装", "wrong_outfit"),
        ("复制", "character_duplicate"),
        ("分身", "character_duplicate"),
        ("双人", "character_duplicate"),
        ("动作缺失", "action_missing"),
        ("没有完成动作", "action_missing"),
        ("道具变形", "prop_identity_mismatch"),
        ("道具消失", "prop_state_mismatch"),
        ("数量", "object_count_mismatch"),
        ("越轴", "wrong_camera_axis"),
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
    for key in (
        "start_state_match", "end_state_match", "action_match", "character_match",
        "prop_identity_match", "prop_state_match", "object_count_match", "camera_axis_match",
    ):
        try:
            score = float(qa.get(key)) if qa.get(key) is not None else None
        except (TypeError, ValueError):
            score = None
        if score is None or score >= 0.45:
            continue
        failure = {
            "start_state_match": "state_mismatch",
            "end_state_match": "state_mismatch",
            "action_match": "action_missing",
            "character_match": "wrong_identity",
            "prop_identity_match": "prop_identity_mismatch",
            "prop_state_match": "prop_state_mismatch",
            "object_count_match": "object_count_mismatch",
            "camera_axis_match": "wrong_camera_axis",
        }[key]
        if failure not in failures:
            failures.append(failure)
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
        "wrong_identity": {
            "extra_negative": ["角色身份必须与人物真值图一致，禁止换脸、换年龄或生成其他角色"],
            "hint": "强化人物身份参考图；减少同框干扰角色",
        },
        "wrong_outfit": {
            "extra_negative": ["服装款式、主色、发型和发饰必须与人物真值图一致"],
            "hint": "锁定人物造型版本，移除冲突参考图",
        },
        "action_missing": {
            "extra_negative": ["必须完整执行本镜唯一核心动作，禁止只站立、只说话或用镜头运动代替动作"],
            "hint": "把动作收敛为一个可见事件；必要时拆镜",
        },
        "prop_identity_mismatch": {
            "extra_negative": ["关键道具外形、材质、颜色和数量必须与道具真值图一致，禁止融合或变形"],
            "hint": "注入唯一道具状态图；减少同镜道具操作数量",
        },
        "prop_state_mismatch": {
            "extra_negative": ["关键道具必须从指定持有人、位置和开合状态开始并保持到动作发生"],
            "hint": "核对道具 owner/form/location 状态链",
        },
        "object_count_mismatch": {
            "extra_negative": ["画面中的人物与关键道具数量必须严格等于镜头合同"],
            "hint": "在提示中明确人数和逐件道具数量",
        },
        "wrong_camera_axis": {
            "extra_negative": ["保持既定空间轴线、人物屏幕方位和视线方向，禁止越轴"],
            "hint": "使用上一镜尾状态约束下一镜关键帧机位",
        },
        "subject_occlusion": {
            "extra_negative": ["主体脸部、手部动作和关键道具不得被遮挡"],
            "hint": "调整构图并降低前景遮挡",
        },
        "geometry_guard_unverified": {
            "extra_negative": ["固定地标、人物比例与关键道具几何关系必须清楚可见且保持稳定"],
            "hint": "收敛构图并强化空间地标",
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
