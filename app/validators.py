"""分镜脚本业务校验器 V1~V8（docs/PROMPT_SPEC.md §C）。
错误消息必须具体到字段与数值——修复回路把它们逐条回喂模型（1.0 教训：从不告诉模型哪里错了）。
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from app import config, textmatch
from app.character_policy import is_functional_extra
from app.continuity import (
    normalize_board_continuity,
    state_chain_errors,
    action_capacity_errors,
    speech_capacity_errors,
    spoken_contract_coherence_errors,
    shot_id_space_errors,
    information_ledger_errors,
    outline_atomic_errors,
    adaptation_hook_errors,
    sync_shot_continuity_fields,
    action_capacity_limit,
    count_sequential_action_beats,
    split_sequential_action_text,
    dialogue_focus_subject,
    dialogue_framing_errors,
    dialogue_two_shot_required,
    spoken_chars_from_shot,
)
from app.spoken_contract import (
    RULE_SPOKEN_COHERENCE,
    build_timeline_from_segments,
    content_char_count,
    max_speech_chars,
    segments_from_timeline,
    spoken_text_of,
    validate_spoken_contract,
)
from app.schemas import (Bible, EpisodeScreenplay, InformationItem, Shot, Storyboard,
                         StoryboardOutline, StoryEvent, SHOT_SIZES, CAMERA_MOVES, TRANSITIONS,
                         CONTINUITY_MODES, DELIVERY_OWNERS)
from app.renderability import (
    ACTION_DESC_HARD_MIN,
    ACTION_DESC_TARGET_MAX,
    ACTION_DESC_TARGET_MIN,
    DROP_LIST_MIN,
    DIALOGUE_CHAIN_TURNS_HARD_MAX,
    DURATION_REVIEW_RISK_TAG,
    KEY_LINES_MIN,
    KEY_PLOT_POINTS_MAX,
    KEY_PLOT_POINTS_MIN,
    PREFERRED_SHOT_DURATION_S,
    SCENE_OUTLINE_MAX,
    SCENE_OUTLINE_MIN,
    SHOT_HARD_MAX,
    SHOT_SOFT_MIN,
    SPINE_BEATS_MAX,
    SPINE_BEATS_MIN,
    duration_gt5_errors,
    overdetail_errors,
    shot_count_budget_errors,
    shot_duration_should_prefer_five,
)


# 字数约束设计原则（2026-06-15 v12：旁白改为「选填且少用」；2026-07-25 Renderability First）：
# ① 叙事主力改为【台词 + 可见画面动作】；旁白(narration)默认留空，只在画面与台词都
#    无法传达关键信息时（较大时间跳跃/必要内心独白/隐藏因果）才写一句短旁白。
#    因此取消旁白下限校验、取消「纯画面空镜必须加旁白」的硬性要求。
# ② 旁白仍保留上限校验：若写，必须短到最短 5s 镜头也能念完，避免又退回到旁白堆砌。
# ③ action_desc 只要求单主动作可读（硬下限约 18 字，目标 25~55），禁止堆微细节。
NARRATION_TARGET_CHARS = 12
NARRATION_HARD_MAX = 14
# 兼容旧 import：目标字数取区间中位偏下，prompt 引导用
ACTION_DESC_MIN_CHARS = ACTION_DESC_TARGET_MIN
SOURCE_EXCERPT_MIN_CHARS = 8
# 已废除「至少 2 个动作片段」硬门槛；保留符号供旧测试 import 时不崩，值为 0 表示不校验。
VIDEO_SEGMENT_MIN_BEATS = 0
# 显式多镜头/快切/蒙太奇标记：出现即判定为“一个镜头里塞多段”，高精度、低误伤。
CUT_MARKERS = (
    "切到", "切至", "切换到", "切换至", "镜头切", "画面切", "镜头转向", "镜头转到",
    "闪回", "回忆画面", "回忆起", "蒙太奇", "分屏", "下一个镜头", "下一镜", "转场到", "→",
)
SCENE_SETTING_MAX_CHARS = 18        # 仅作 prompt 建议值，不再参与校验
TRANSITION_HINTS = (
    "次日", "第二天", "当天", "清晨", "上午", "中午", "下午", "傍晚", "深夜", "夜里",
    "与此同时", "转场", "随后", "片刻后", "几小时后", "数小时后", "一夜后", "回到", "另一边",
    "带着", "顺着", "接着", "继续", "仍", "还", "已经",
)


def _named_character_is_explicitly_offscreen(name: str, text: str) -> bool:
    """允许动作描述交代听者在画外，但不能把其可见动作混进单人对白镜。"""
    escaped = re.escape(name)
    return bool(
        re.search(rf"(?:画外|镜外|不入画|留在画外)[^，。；]{{0,12}}{escaped}", text)
        or re.search(rf"{escaped}[^，。；]{{0,12}}(?:在画外|于画外|不入画|留在画外)", text)
    )
# 换场承接的「移动/抵达」动词：动作里出现这些即说明人物是“走过去/来到”新场景，移动本身就是承接，
# 不该因为没用到 TRANSITION_HINTS 里那批固定承接词就误判“缺少承接”（实测高频误伤，白耗修复轮次）。
MOVEMENT_HINTS = (
    "走到", "走向", "走出", "走进", "走来", "走去", "走上", "走下", "走过", "来到", "回到", "返回",
    "转身", "离开", "起身", "出门", "进门", "推门", "步入", "踏入", "迈进", "迈步", "穿过", "穿出",
    "跑向", "跑到", "跑出", "冲向", "冲进", "赶到", "赶往", "退到", "退出", "上前", "退后", "跟上",
    "登上", "爬上", "钻进", "前往", "折返", "驻足", "停在", "停步", "停下",
)

# 目标时长只提供初始节拍参考；剧情未完整覆盖时可继续补 5~10 秒镜头。
SCENE_CUT_TRANSITIONS = TRANSITIONS - {"硬切"}
SAME_SCENE_CONTINUITY_MODES = {
    "same_scene_cut",
    "reaction_cut",
    "reverse_angle",
    "insert_detail",
}
TRANSITION_VISUAL_HINTS = {
    "叠化": ("叠化", "渐", "柔", "余韵", "模糊", "压低"),
    "淡出淡入": ("淡出", "淡入", "渐暗", "渐黑", "渐亮", "压暗", "暗下"),
    "黑场": ("黑场", "黑", "暗"),
    "闪黑": ("闪黑", "黑", "暗"),
    "闪白": ("闪白", "白", "强光", "亮", "刺眼"),
    "甩镜": ("甩", "模糊", "横摇", "拖影", "运动"),
    "遮挡转场": ("遮挡", "掠过", "遮住", "挡住", "黑影", "衣袖", "门"),
    "匹配剪辑": ("匹配", "呼应", "相同", "同样", "圆", "构图"),
    "声音延续+叠化": ("叠化", "余音", "话音", "声音", "回响", "渐"),
    "声音先行+淡入": ("声音", "先行", "淡入", "渐", "传来"),
}


def default_scene_transition(prev: Shot | None, shot: Shot) -> str:
    """根据换场关系给一个稳定默认值。禁止选用依赖后期声画桥的转场。"""
    if not prev:
        return "硬切"
    text = f"{prev.narration or ''}{shot.narration or ''}{prev.action_desc}{shot.action_desc}"
    if any(k in text for k in ("冲", "追", "逃", "奔", "扑", "甩")):
        return "甩镜"
    if any(k in text for k in ("惊", "爆", "强光", "刺眼", "斗气", "火光")):
        return "闪白"
    if any(k in text for k in ("回忆", "想起", "余音", "话音", "怔住", "眼眶", "沉默", "失神")):
        return "叠化"
    return "淡出淡入"


def storyboard_shot_count_range(target_duration_s: int) -> tuple[int, int]:
    """Renderability First：软预算约 8~16，硬上限 20（防碎镜）；不再使用百万级熔断当产品上限。"""
    _ = target_duration_s
    return SHOT_SOFT_MIN, SHOT_HARD_MAX


def _voiced_shot_count(shots: list[Shot]) -> int:
    return sum(1 for shot in shots if (shot.narration or "").strip() or shot.dialogues)


def _soundtrack_text(shot: Shot) -> str:
    return "".join([shot.narration or "", *(d.line for d in shot.dialogues)])


def _normalized_spoken_text(text: str | None) -> str:
    """Normalize punctuation/spacing so adjacent repeated delivery cannot hide behind typography."""
    return re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE).casefold()


def adjacent_spoken_repeat_errors(board: Storyboard) -> list[str]:
    """Reject a line that is delivered again by the same speaker in the next shot.

    A longer line may legitimately span shots, so this only rejects a current line whose
    complete normalized text already appears in the immediately previous shot. Short
    interjections are ignored to avoid false positives such as repeated names or greetings.
    """
    errors: list[str] = []
    for index in range(1, len(board.shots)):
        previous = board.shots[index - 1]
        current = board.shots[index]
        previous_by_speaker: dict[str, str] = {}
        for dialogue in previous.dialogues:
            speaker = (dialogue.speaker or "").strip().casefold()
            previous_by_speaker[speaker] = (
                previous_by_speaker.get(speaker, "") + _normalized_spoken_text(dialogue.line)
            )
        for dialogue in current.dialogues:
            speaker = (dialogue.speaker or "").strip().casefold()
            normalized = _normalized_spoken_text(dialogue.line)
            if len(normalized) < 8 or not speaker:
                continue
            if normalized in previous_by_speaker.get(speaker, ""):
                errors.append(
                    f"shots[{index}](shot_no={current.shot_no}) 与上一镜相邻重复台词："
                    f"{dialogue.speaker} 的「{dialogue.line}」已在镜{previous.shot_no:02d}完整说过；"
                    "请删除重复台词并改为无台词反应镜，或改写为新的有效信息"
                )
    return errors


def _action_beat_count(text: str) -> int:
    parts = [p.strip() for p in re.split(r"[，。；;、\n]+", text) if len(p.strip()) >= 4]
    return max(len(parts), count_sequential_action_beats(text or ""))


def _explicit_cut_markers(text: str | None) -> list[str]:
    """识别 action_desc 里真正的多镜头/快切/闪回标记（而非把逗号分句当快切）。"""
    t = text or ""
    return [m for m in CUT_MARKERS if m in t]


def _too_similar(a: str, b: str) -> bool:
    """首尾帧描述是否过于相似（几乎是同一句、看不出动作推进）。

    旧实现用【字符集合】Jaccard≥0.8：但首尾帧本就要求"同机位同构图、只让动作推进"，
    天然高词汇重叠，集合 Jaccard 会把"描写到位但动作确有变化"的合规首尾帧误判为雷同，
    反逼模型把首尾写成两个不同镜头/景别——正好制造它想避免的跳变。
    改用序列相似度（difflib，计入顺序与长度），只拦近乎逐字重复的真雷同。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _has_transition_hint(*parts: str | None) -> bool:
    text = "".join(part or "" for part in parts)
    return any(hint in text for hint in TRANSITION_HINTS)


def _has_movement_cue(*parts: str | None) -> bool:
    """动作/旁白里是否写了人物“走过去/转身离开/来到”这类移动，移动本身即换场承接说明。"""
    text = "".join(part or "" for part in parts)
    return any(hint in text for hint in MOVEMENT_HINTS)


def _scene_location(scene: str) -> str:
    """scene_setting 形如「时间，地点」；取地点部分用于判断是否同一片连续空间。"""
    return scene.split("，")[-1].strip()


def _contiguous_scene_move(prev_scene: str, scene: str) -> bool:
    """相邻两镜是否为同一片连续空间内的子区域移动（如 广场→广场边缘→广场外小路）。
    主地点相同、只是换到相邻子区域时，人物走过去本身即承接，无需额外的时间跳跃说明——
    模型常把一片连续场地切成多个子标签（preflight 已劝阻但仍会发生），不应再因此误判缺少承接。"""
    a, b = _scene_location(prev_scene), _scene_location(scene)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    common = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        common += 1
    return common >= 3


def _has_transition_visual(transition: str, *parts: str | None) -> bool:
    text = "".join(part or "" for part in parts)
    hints = TRANSITION_VISUAL_HINTS.get(transition, (transition,))
    return any(hint in text for hint in hints)


def _transition_visual_suffix(transition: str) -> str:
    return {
        "叠化": "，画面边缘轻微模糊并留下叠化余韵",
        "淡出淡入": "，画面光线逐渐压暗，准备淡出淡入下一场景",
        "黑场": "，画面逐渐压黑进入黑场",
        "闪黑": "，画面瞬间闪黑作为转场收尾",
        "闪白": "，画面被刺眼白光短暂吞没形成闪白转场",
        "甩镜": "，画面带出快速甩动的运动模糊",
        "遮挡转场": "，前景人影掠过遮住画面形成遮挡转场",
        "匹配剪辑": "，画面构图保持呼应以衔接下一镜",
        "声音延续+叠化": "，话音余韵未散，画面边缘渐渐叠化",
        "声音先行+淡入": "，下一场景的声音先行传来，画面准备淡入",
    }.get(transition, f"，画面带出{transition}的转场收尾")


def normalize_transition_visuals(board: Storyboard) -> None:
    """非硬切换场时，自动在上一镜尾帧补转场视觉。

    这是跨镜字段：生成当前镜时已经落库的上一镜不方便让模型修改，确认门应做确定性补齐。
    """
    for i in range(1, len(board.shots)):
        shot = board.shots[i]
        prev = board.shots[i - 1]
        if shot.continuity_from_prev or shot.transition == "硬切":
            continue
        if _has_transition_visual(shot.transition, prev.last_frame_desc, prev.action_desc):
            continue
        prev.last_frame_desc = (prev.last_frame_desc or "").rstrip("。") + _transition_visual_suffix(shot.transition) + "。"


_LEADING_ACTION_SEQUENCE_RE = re.compile(r"^\s*(?:先|首先)\s*(?:[，,、。；;：:]|…+|\.{2,})\s*")


def normalize_action_desc(text: str | None) -> str:
    """去掉模型把顺序提示词误写进 action_desc 句首的孤立标记。"""
    normalized = (text or "").strip()
    while True:
        cleaned = _LEADING_ACTION_SEQUENCE_RE.sub("", normalized, count=1).lstrip()
        if cleaned == normalized:
            return normalized
        normalized = cleaned


def validate_storyboard(
    board: Storyboard,
    bible: Bible,
    target_duration_s: int,
) -> list[str]:
    errors: list[str] = []
    shots = board.shots
    if not shots:
        return ["shots 为空；请按完整剧本至少生成一个 5~10 秒镜头"]

    bible_names = {c.name for c in bible.characters}

    beat_unit = config.VIDEO_DURATION_MIN_S
    if target_duration_s % beat_unit != 0:
        errors.append(
            f"目标时长 {target_duration_s}s 不是 {beat_unit}s 的整数倍；"
            f"节拍单元按 5s 换算要求目标取 {'/'.join(str(x) for x in config.EPISODE_TARGET_CHOICES)}s")
    # 镜头数量和整集总时长不设产品上限；完整覆盖剧本是唯一收束条件。

    prev_sizes: list[str] = []
    scene_last_seen: dict[str, int] = {}
    for i, shot in enumerate(shots):
        shot.action_desc = normalize_action_desc(shot.action_desc)
        tag = f"shots[{i}](shot_no={shot.shot_no})"
        # V2 时长合法取值
        if shot.duration_s not in config.ALLOWED_DURATIONS:
            errors.append(
                f"{tag}.duration_s={shot.duration_s}，必须由模型按本镜动作与口播判断为 "
                f"{config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S}s 的整数")
        spoken_for_dur = spoken_chars_from_shot(shot)
        action_beats_for_dur = count_sequential_action_beats(
            (shot.primary_action or shot.action_desc or "").strip()
        )
        errors.extend(duration_gt5_errors(
            shot_no=shot.shot_no,
            duration_s=shot.duration_s,
            spoken_chars=spoken_for_dur,
            action_beats=action_beats_for_dur,
        ))
        if (
            int(shot.duration_s or 0) > PREFERRED_SHOT_DURATION_S
            and not shot_duration_should_prefer_five(
                spoken_chars=spoken_for_dur, action_beats=action_beats_for_dur
            )
            and DURATION_REVIEW_RISK_TAG not in (shot.risk_tags or [])
        ):
            tags = list(shot.risk_tags or [])
            tags.append(DURATION_REVIEW_RISK_TAG)
            shot.risk_tags = tags
        # V8 画面清晰度：单镜只演一个连贯主动作（Renderability：硬下限约 18，目标 25~55）。
        if len(shot.action_desc) < ACTION_DESC_HARD_MIN:
            errors.append(
                f"{tag}.action_desc 仅 {len(shot.action_desc)} 字，低于硬下限 {ACTION_DESC_HARD_MIN} 字；"
                f"请用 {ACTION_DESC_TARGET_MIN}~{ACTION_DESC_TARGET_MAX} 字写清这一个大形体主动作（谁做了什么），"
                "禁止堆微表情/手指/衣褶细节")
        elif len(shot.action_desc) > ACTION_DESC_TARGET_MAX + 40:
            errors.append(
                f"{tag}.action_desc 共 {len(shot.action_desc)} 字，过长易塞入超纲细节；"
                f"请压缩到约 {ACTION_DESC_TARGET_MAX} 字以内，只保留单主动作")
        source_len = len((shot.source_excerpt or "").strip())
        if source_len < SOURCE_EXCERPT_MIN_CHARS:
            errors.append(
                f"{tag}.source_excerpt 仅 {source_len} 字；每个分镜必须带对应小说原文摘录，"
                f"请从本集原文中逐字摘录至少 {SOURCE_EXCERPT_MIN_CHARS} 字作为上游改编证据与审核追溯，不得送入 Seedance")
        errors.extend(overdetail_errors(shot.action_desc, f"{tag}.action_desc"))
        errors.extend(overdetail_errors(shot.first_frame_desc, f"{tag}.first_frame_desc"))
        errors.extend(overdetail_errors(shot.last_frame_desc, f"{tag}.last_frame_desc"))
        cut_markers = _explicit_cut_markers(shot.action_desc)
        if cut_markers:
            errors.append(
                f"{tag}.action_desc 出现多镜头/快切标记 {cut_markers}；单镜只拍一个连贯动作，"
                "请删掉切镜/闪回/分屏等跳切，把多余剧情拆到相邻镜或写入画面动作")
        # 首尾帧：必须填写且明显不同（否则生成的首图/尾图一模一样、视频没有动作）
        ff = (shot.first_frame_desc or "").strip()
        lf = (shot.last_frame_desc or "").strip()
        if len(ff) < 10:
            errors.append(f"{tag}.first_frame_desc 太短或缺失；请写本镜【开始】的静止画面（动作发生前，25~50字）")
        if len(lf) < 10:
            errors.append(f"{tag}.last_frame_desc 太短或缺失；请写本镜【结束】的静止画面（动作完成后，25~50字）")
        if ff and lf and _too_similar(ff, lf):
            errors.append(
                f"{tag} 首帧与尾帧画面描述几乎相同；二者必须明显不同（动作前 vs 动作后，体现姿态/表情/手部/道具的可见变化），"
                "否则首图尾图会一模一样、视频没有动作")
        errors.extend(action_capacity_errors(shot))
        # 口播容量只在 speech_capacity_errors 里实现一次；此处曾重复计算同一规则，
        # 导致同一根因在确认门输出两条不同文案（VAL-422 根因 R5）。
        errors.extend(speech_capacity_errors(shot))
        errors.extend(dialogue_framing_errors(shot))
        # 同一镜头只能有一套有效口播：dialogues 与 audio_timeline 分叉即 blocker。
        errors.extend(spoken_contract_coherence_errors(shot))
        # 产品合同：禁止一切旁白/内心OS；信息由真实台词或画面动作承载。
        narration_len = len((shot.narration or "").strip())
        if narration_len > 0:
            errors.append(
                f"{tag}.narration 非空（{narration_len} 字）；禁止旁白/内心OS，请删空 narration，"
                "改用 dialogues 真实台词或 action_desc 画面动作")
        errors.extend(shot_id_space_errors(shot))
        # V4 角色合法性
        if not shot.characters:
            errors.append(
                f"{tag}.characters 为空；每个视频段至少包含 1 个画面角色，"
                "可以是角色圣经成员或功能性路人"
            )
        elif len(shot.characters) > 3:
            errors.append(
                f"{tag}.characters 共 {len(shot.characters)} 人，超过单镜可渲染上限 3；"
                "请减少画面角色或拆到相邻镜，禁止群戏调度"
            )
        for name in shot.characters:
            if bible_names and name not in bible_names and not is_functional_extra(name):
                errors.append(
                    f"{tag}.characters 含「{name}」，既不在角色圣经中，也不是允许的功能性路人标签；"
                    f"圣经角色为：{'/'.join(sorted(bible_names))}。无姓名群演请使用测验员、守卫、"
                    "路人甲/乙/丙、族人甲、弟子乙等通用身份标签"
                )
        named_mentions = [name for name in shot.characters if name in shot.action_desc]
        if shot.characters and not named_mentions:
            errors.append(
                f"{tag}.action_desc 未出现本镜头角色名；必须用 characters 中的准确姓名"
                "（角色圣经成员或功能性路人标签）写人物动作，不要只写他/她/纸张/镜头/场景")
        visual_text = "".join(
            (shot.action_desc or "", shot.first_frame_desc or "", shot.last_frame_desc or "")
        )
        focus_subject = dialogue_focus_subject(shot)
        if focus_subject and not dialogue_two_shot_required(shot):
            for other_name in sorted(bible_names - {focus_subject}):
                if other_name not in visual_text:
                    continue
                if _named_character_is_explicitly_offscreen(other_name, visual_text):
                    continue
                errors.append(
                    f"{tag} 是「{focus_subject}」的单人对白近景，但 action_desc/首尾帧仍把"
                    f"「{other_name}」写进可见画面；请把听者明确留在画外，下一话轮再切反打"
                )
        for name in (item for item in shot.characters if is_functional_extra(item)):
            if name not in visual_text:
                errors.append(
                    f"{tag}.characters 中的功能性路人「{name}」未在 action_desc/首尾帧中明确入画；"
                    "路人可以不进角色圣经，但必须看得见其位置、动作或开口过程"
                )
        visible_speakers = set(shot.characters)
        audio_cast = set(getattr(shot, "audio_cast", []) or [])
        for j, d in enumerate(shot.dialogues):
            delivery = getattr(d, "delivery", "spoken_dialogue") or "spoken_dialogue"
            if delivery == "offscreen_voice" or d.speaker in audio_cast:
                continue
            if d.speaker not in visible_speakers:
                errors.append(
                    f"{tag}.dialogues[{j}].speaker=「{d.speaker}」不在该镜头 characters 中；"
                    "画面开口台词必须由 characters 中的可见角色说出，画外音请设 delivery=offscreen_voice 或加入 audio_cast")
        # V5：可变时长视频段只允许一个连续动作，禁止回到低信息空动作。
        if len(shot.action_desc) < 10:
            errors.append(f"{tag}.action_desc 长度 {len(shot.action_desc)} 字，要求至少 10 字")
        # 枚举值
        if shot.shot_size not in SHOT_SIZES:
            errors.append(f"{tag}.shot_size=「{shot.shot_size}」不在 {sorted(SHOT_SIZES)}")
        if shot.camera_move not in CAMERA_MOVES:
            errors.append(f"{tag}.camera_move=「{shot.camera_move}」不在 {sorted(CAMERA_MOVES)}")
        if shot.transition not in TRANSITIONS:
            errors.append(f"{tag}.transition=「{shot.transition}」不在 {sorted(TRANSITIONS)}")
        # V6 场景连续性（场景标签长度上限校验已取消）。
        # 按主场景判定：同一地点的子机位标签（"广场" vs "广场·中央石台"）视为同一场景，
        # 不算被打断——否则模型给同一地点加子机位后会误报"场景被打断"，逼出无谓重试。
        scene = shot.scene_setting.strip()
        scene_key = _scene_contiguity_key(scene)
        if scene_key in scene_last_seen and scene_last_seen[scene_key] != i - 1:
            errors.append(f"场景「{scene}」在 shots[{scene_last_seen[scene_key]}] 与 shots[{i}] 间被其他场景打断，同场景镜头必须连续排列")
        scene_last_seen[scene_key] = i
        # V6+ 连贯性：使用 continuity_mode 表达“是否使用上一镜尾帧”，不再把同场景布尔等同动作连续。
        # 始终 sync：无 prev 时会降级 action_continuation，与 derive_continuity_mode / 入队门禁一致。
        prev_for_mode = shots[i - 1] if i > 0 else None
        mode = sync_shot_continuity_fields(shot, prev_for_mode)
        if mode not in CONTINUITY_MODES:
            errors.append(f"{tag}.continuity_mode=「{mode}」不在 {sorted(CONTINUITY_MODES)}")
        if i == 0:
            if mode == "action_continuation":
                errors.append(f"{tag}.continuity_mode=action_continuation，但第一个镜头没有上一镜可承接")
            if shot.continuity_from_prev:
                errors.append(f"{tag}.continuity_from_prev=true，但第一个镜头没有上一镜可承接")
        elif mode in CONTINUITY_MODES:
            prev = shots[i - 1]
            prev_scene = prev.scene_setting.strip()
            same_scene = scene == prev_scene
            shared_chars = set(prev.characters) & set(shot.characters)
            if same_scene and mode == "scene_change":
                errors.append(f"{tag}.continuity_mode=scene_change 但 scene_setting 与上一镜同为「{scene}」")
            if not same_scene and mode != "scene_change":
                errors.append(
                    f"{tag}.continuity_mode={mode} 但 scene_setting 从「{prev_scene}」变为「{scene}」；"
                    "跨时间/地点必须使用 scene_change")
            if mode == "action_continuation":
                if shot.transition != "硬切":
                    errors.append(f"{tag}.transition=「{shot.transition}」，action_continuation 必须使用「硬切」")
                if not shared_chars and not _has_movement_cue(
                    prev.action_desc, prev.narration, shot.action_desc, shot.narration
                ):
                    errors.append(
                        f"{tag}.continuity_mode=action_continuation 但与上一镜没有共同角色或可见移动承接；"
                        "动作连续必须保留上一镜核心人物，或在 action_desc/narration 写明入场、离场、跟随等移动线索")
            elif mode in SAME_SCENE_CONTINUITY_MODES:
                if not same_scene:
                    errors.append(
                        f"{tag}.continuity_mode={mode} 但 scene_setting 从「{prev_scene}」变为「{scene}」；"
                        "同场景切换模式必须沿用同一时间地点")
                if shot.transition != "硬切":
                    errors.append(f"{tag}.transition=「{shot.transition}」，{mode} 必须使用「硬切」")
                if shot.continuity_from_prev:
                    errors.append(
                        f"{tag}.continuity_from_prev=true 但 continuity_mode={mode}；"
                        "只有 action_continuation 可以使用上一镜尾帧作为起始连续参考")
            elif mode == "scene_change":
                if shot.continuity_from_prev:
                    errors.append(
                        f"{tag}.continuity_from_prev=true 但 continuity_mode=scene_change；"
                        "换场不得使用上一镜尾帧连续参考")
                if shot.transition == "硬切":
                    errors.append(
                        f"{tag}.transition=硬切 但 continuity_mode=scene_change 且 scene_setting 从「{prev_scene}」切到「{scene}」；"
                        f"跨时间/地点请用 {sorted(SCENE_CUT_TRANSITIONS)} 之一，并写清承接")
                elif shot.transition not in SCENE_CUT_TRANSITIONS:
                    errors.append(
                        f"{tag}.transition=「{shot.transition}」不适合换场；"
                        f"换场请用 {sorted(SCENE_CUT_TRANSITIONS)} 之一")
                dialogue_text = "".join(d.line for d in shot.dialogues)
                # 承接说明判定（放宽，杜绝高频误伤）：满足以下任一即视为已写清承接——
                # ① 含时间/线索类承接词；② 动作/旁白写了人物移动；③ 同一片连续空间子区域移动。
                move_explained = (
                    _has_transition_hint(scene, shot.action_desc, shot.narration, dialogue_text)
                    or _has_movement_cue(shot.action_desc, shot.narration)
                    or _contiguous_scene_move(prev_scene, scene)
                )
                if not move_explained:
                    errors.append(
                        f"{tag} 从上一镜「{prev_scene}」切到「{scene}」但缺少承接说明；"
                        "请在 narration 或 action_desc 写清时间跳跃、线索带入或人物为何来到新场景")
                if not _has_transition_visual(shot.transition, prev.last_frame_desc, prev.action_desc):
                    errors.append(
                        f"shots[{i - 1}](shot_no={prev.shot_no}).last_frame_desc 未体现进入镜{shot.shot_no:02d}的「{shot.transition}」转场收尾；"
                        "请在上一镜尾帧写出可见转场视觉，例如渐暗/闪白/遮挡/甩镜模糊/叠化余韵/匹配构图呼应")
        # V7 景别不三连
        prev_sizes.append(shot.shot_size)
        if len(prev_sizes) >= 3 and prev_sizes[-1] == prev_sizes[-2] == prev_sizes[-3]:
            errors.append(f"{tag} 起连续 3 个镜头景别均为「{shot.shot_size}」，请交替景别")

    # V7 shot_no 连续
    expected = list(range(1, len(shots) + 1))
    actual = [s.shot_no for s in shots]
    if actual != expected:
        errors.append(f"shot_no 必须为连续递增 1..{len(shots)}，当前为 {actual}")

    # V12 场景必须落在场景图素材库内（库非空时；同时回填 shot.scene_name 供渲染期复用同一张场景图）
    errors.extend(adjacent_spoken_repeat_errors(board))
    errors.extend(validate_storyboard_scenes(board, bible))
    errors.extend(state_chain_errors(board))
    errors.extend(shot_count_budget_errors(len(shots), context="分镜"))

    return errors

# ---------- 场景图素材库：场景标签 → 库内规范场景的归一化匹配 ----------

def _normalize_scene_label(s: str) -> str:
    """去掉时间前缀/标点/空白，得到纯地点 token，用于场景标签的容错匹配。"""
    return re.sub(r"[\s，,。.：:；;/、|]+", "", (s or "").strip())


def _scene_contiguity_key(scene: str) -> str:
    """场景连续性的归一化主键：剥掉子机位/子区域后缀（"·中央石台""-树荫下"等），
    让同一地点的不同机位标签算同一场景，避免"加了子机位 = 场景被打断"的误报。"""
    base = re.split(r"[·・·\-—/]", (scene or "").strip(), maxsplit=1)[0]
    return _normalize_scene_label(base)


def match_scene_name(scene_setting: str, scenes) -> str | None:
    """把分镜 scene_setting 归一化匹配到 bible.scenes 中的规范场景名。
    容错：去标点后规范名是 setting 的子串（或反之）即命中（最强）；否则取相似度最高且 ≥0.6 的场景。
    返回命中的规范场景名，或 None（无库/无匹配）。"""
    setting = (scene_setting or "").strip()
    if not setting or not scenes:
        return None
    norm_setting = _normalize_scene_label(setting)
    if not norm_setting:
        return None
    best: str | None = None
    best_score = 0.0
    for sc in scenes:
        name = (getattr(sc, "name", "") or "").strip()
        norm_name = _normalize_scene_label(name)
        if not norm_name:
            continue
        if norm_name in norm_setting or norm_setting in norm_name:
            return name  # 子串命中，最强
        ratio = difflib.SequenceMatcher(None, norm_name, norm_setting).ratio()
        if ratio > best_score:
            best_score, best = ratio, name
    return best if best_score >= 0.6 else None


def validate_storyboard_scenes(board: Storyboard, bible: Bible) -> list[str]:
    """V12：每个 shot.scene_setting 必须映射到场景图素材库（bible.scenes）里的规范场景，
    命中则回填 shot.scene_name（渲染期据此为同一场景复用同一张场景库图，跨镜/跨集一致）。
    务实优先：库为空（旧项目或尚未生成场景圣经）时直接放行，绝不误伤。"""
    scenes = getattr(bible, "scenes", None) or []
    if not scenes:
        return []
    errors: list[str] = []
    names = "/".join(sc.name for sc in scenes if getattr(sc, "name", ""))
    for i, shot in enumerate(board.shots):
        matched = match_scene_name(shot.scene_setting, scenes)
        if matched:
            shot.scene_name = matched
        else:
            shot.scene_name = ""
            errors.append(
                f"shots[{i}](shot_no={shot.shot_no}).scene_setting=「{shot.scene_setting}」不在场景图素材库内；"
                f"scene_setting 必须收敛到库内规范场景之一：{names}（若确为剧情需要的新场景，请沿用语义最接近的库内场景名）")
    return errors


# ---------- C1.5 可拍剧本 ----------

FULL_SCRIPT_FORBIDDEN_TERMS = (
    "拍01", "拍1", "拍 01", "拍 1", "镜头", "景别", "运镜", "首帧", "尾帧", "参考图", "提示词", "prompt",
)
SCRIPT_SCENE_HEADING_RE = re.compile(r"【场\s*\d+】")
SCRIPT_DIALOGUE_LINE_RE = re.compile(r"^[^\n：]{1,16}(?:（[^）]{1,12}）)?：", re.M)
SCRIPT_SOUND_LINE_RE = re.compile(r"^([^\n：（]{1,16})(?:（([^）]{1,12})）)?：(.+)$", re.M)
# 模型偶发把「【场1】角色：台词」粘在同一行；剥场次标题后再识别说话人。
_SCRIPT_GLUED_HEADING_DIALOGUE_RE = re.compile(
    r"^【场\s*\d+】\s*([^\n：/（]{1,16})(?:（([^）]{1,12})）)?：(.+)$"
)
INNER_VOICE_MARKERS = ("内心", "心声", "OS", "os", "独白")


def _iter_script_sound_matches(full_text: str):
    """逐行提取剧本对白，避免把场次标题/地点梗概误判成说话人。"""
    for raw_line in (full_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if SCRIPT_SCENE_HEADING_RE.match(line):
            glued = _SCRIPT_GLUED_HEADING_DIALOGUE_RE.match(line)
            if glued and "/" not in glued.group(1):
                yield glued
            continue
        match = SCRIPT_SOUND_LINE_RE.match(line)
        if not match:
            continue
        speaker = match.group(1).strip()
        # 地点梗概（含 /）或残留场次标记不是说话人
        if "/" in speaker or "【" in speaker:
            continue
        yield match


def _script_dialogue_turns(full_text: str) -> list[tuple[int, str, str]]:
    """Return screenplay dialogue turns as ``(scene_no, speaker, line)`` in story order."""
    turns: list[tuple[int, str, str]] = []
    scene_no = 0
    for raw_line in (full_text or "").splitlines():
        line = raw_line.strip()
        heading = SCRIPT_SCENE_HEADING_RE.search(line)
        if heading:
            number = re.search(r"\d+", heading.group(0))
            if number:
                scene_no = int(number.group(0))
        for match in _iter_script_sound_matches(line):
            turns.append((scene_no, match.group(1).strip(), match.group(3).strip()))
    return turns


def _screenplay_scene_space(heading: str) -> tuple[str, str]:
    """把场次标题拆成时间与地点，用于识别同一连续空间的子区域。"""
    value = re.sub(r"^【场\s*\d+】\s*", "", (heading or "").strip())
    parts = re.split(r"\s*/\s*", value, maxsplit=1)
    if len(parts) == 1:
        return "", _condense(parts[0])
    return _condense(parts[0]), _condense(parts[1])


def _dialogue_chain_crosses_hard_scene_boundary(
    script: EpisodeScreenplay,
    scene_numbers: set[int],
) -> bool:
    """区分真正换场与同一时空内的相邻子区域切块。

    剧本常把「迎客大厅」和「迎客大厅角落」拆成两个节拍场块；这不是对白
    因果被打断，不应阻断发布。非相邻场次、时间变化或地点无连续关系时仍按
    真正跨场处理。
    """
    ordered = sorted(number for number in scene_numbers if number > 0)
    if len(ordered) != len(scene_numbers) or any(
        right != left + 1 for left, right in zip(ordered, ordered[1:])
    ):
        return True
    headings = {
        int(scene.scene_no): str(scene.scene_heading or "")
        for scene in (script.scene_outline or [])
    }
    for left, right in zip(ordered, ordered[1:]):
        left_heading = headings.get(left, "")
        right_heading = headings.get(right, "")
        if not left_heading or not right_heading:
            return True
        left_time, left_location = _screenplay_scene_space(left_heading)
        right_time, right_location = _screenplay_scene_space(right_heading)
        if left_time and right_time and left_time != right_time:
            return True
        if not left_location or not right_location:
            return True
        if left_location in right_location or right_location in left_location:
            continue
        common = 0
        for left_char, right_char in zip(left_location, right_location):
            if left_char != right_char:
                break
            common += 1
        if common < 3:
            return True
    return False


# ---------- 关键内容（必保留清单）模糊匹配工具 ----------
# 防丢失校验的共用底座：剧本台/分镜台都要判断"某条关键台词/剧情点是否仍真实存在于文本里"。
# 务实优先（本次定调）：只拦【明显丢失】，用模糊匹配容忍口语化改写/标点差异，绝不逐字比对，
# 避免像历史 false-positive 那样空耗修复轮次。
_SPEAKER_PREFIX_RE = textmatch._SPEAKER_PREFIX_RE
_NON_CONTENT_RE = textmatch._NON_CONTENT_RE
KEY_LINE_PRESENT_RATIO = textmatch.KEY_LINE_PRESENT_RATIO
KEY_LINE_BIGRAM_COVERAGE = textmatch.KEY_LINE_BIGRAM_COVERAGE
KEY_POINT_COVERAGE = textmatch.KEY_POINT_COVERAGE
KEY_CONTENT_MAX_REPORT = 4       # 单条错误最多点名几条，避免错误列表过长把 prompt 撑爆
MIN_KEY_LINES = KEY_LINES_MIN
MIN_KEY_PLOT_POINTS = KEY_PLOT_POINTS_MIN
MAX_KEY_PLOT_POINTS = KEY_PLOT_POINTS_MAX


_strip_speaker = textmatch.strip_speaker
_speaker_name = textmatch.speaker_name


_CONTEXT_DEPENDENT_DIALOGUE_MARKERS = (
    "以前你", "你以前", "你曾", "你说过", "你问过", "你叫我",
    "我相信", "我知道", "我明白", "我也", "没错", "正是", "当然",
    "因为", "可是", "但是", "不过", "所以", "本来就", "并不是",
    "不是这样", "不会的", "你会重新", "你还能", "你仍然",
)


def _is_context_dependent_dialogue(line: str) -> bool:
    compact = re.sub(r"\s+", "", _strip_speaker(line or ""))
    return any(marker in compact for marker in _CONTEXT_DEPENDENT_DIALOGUE_MARKERS)


def _matching_text_indices(needle: str, ordered_texts: list[str]) -> list[int]:
    core = _strip_speaker(needle)
    return [
        i for i, text in enumerate(ordered_texts)
        if _longest_run_ratio(core, text) >= KEY_LINE_PRESENT_RATIO
        or _bigram_coverage(core, text) >= KEY_LINE_BIGRAM_COVERAGE
    ]


def key_line_order_errors(
    key_lines: list[str], ordered_texts: list[str], *, subject: str,
) -> list[str]:
    """Ensure key dialogue remains in narrative order, not merely present as a bag of lines."""
    last_index = -1
    out_of_order: list[str] = []
    for line in key_lines:
        candidates = _matching_text_indices(line, ordered_texts)
        if not candidates:  # Missing-content validators report this separately.
            continue
        following = [index for index in candidates if index >= last_index]
        if following:
            last_index = following[0]
        else:
            out_of_order.append(line)
    if not out_of_order:
        return []
    shown = "；".join(out_of_order[:KEY_CONTENT_MAX_REPORT])
    return [
        f"{subject}打乱了主线对白顺序：{shown}；key_lines 是按剧情发生顺序排列的对白链，"
        "提问/刺激必须先于回答/安慰/反驳，禁止只保留一组无序金句"
    ]


def _source_bible_dialogues(source_text: str | None, bible: Bible) -> list[str]:
    """Extract source dialogue lines spoken by characters already present in the bible."""
    if not source_text:
        return []
    bible_names = [c.name.strip() for c in bible.characters if c.name and c.name.strip()]
    if not bible_names:
        return []
    names = "|".join(re.escape(name) for name in sorted(bible_names, key=len, reverse=True))
    prefix_re = re.compile(
        rf"^\s*({names})(?:[（(][^）)]{{1,12}}[）)])?\s*[:：]\s*(\S.+?)\s*$",
        flags=re.MULTILINE,
    )
    found: list[str] = []
    seen: set[str] = set()
    for match in prefix_re.finditer(source_text):
        speaker = match.group(1).strip()
        line = match.group(2).strip().strip("“”\"'")
        if len(_condense(line)) < 2:
            continue
        item = f"{speaker}：{line}"
        key = _condense(item)
        if key not in seen:
            seen.add(key)
            found.append(item)
    return found


_condense = textmatch.condense
_longest_run_ratio = textmatch.longest_run_ratio
_bigram_set = textmatch.bigram_set
_bigram_coverage = textmatch.bigram_coverage
_CLAIM_SPLIT_RE = textmatch._CLAIM_SPLIT_RE
_atomize_claim = textmatch.atomize_claim


_SOURCE_QUOTED_UTTERANCE_RE = re.compile(
    r"[“「『](?P<line>[^”」』\n]{2,240})[”」』]"
)
_SOURCE_PREFIXED_UTTERANCE_RE = re.compile(
    r"(?m)^\s*[^\n：:“「『]{1,20}(?:[（(][^）)]{1,12}[）)])?\s*[：:]\s*(?P<line>\S.{1,239})\s*$"
)


def source_dialogue_fragments(source_text: str | None) -> list[str]:
    """Extract source utterances in deterministic source order.

    This inventory exists before the model chooses ``key_lines``.  It closes
    the former circular contract where a line omitted by the model could no
    longer be detected because the model-authored key-line list was the only
    source of truth.
    """
    if not source_text:
        return []
    matches: list[tuple[int, str]] = []
    for pattern in (_SOURCE_QUOTED_UTTERANCE_RE, _SOURCE_PREFIXED_UTTERANCE_RE):
        for match in pattern.finditer(source_text):
            line = match.group("line").strip().strip("“”「」『』\"'")
            if len(_condense(line)) >= 2:
                matches.append((match.start(), line))
    matches.sort(key=lambda item: item[0])
    result: list[str] = []
    seen: set[str] = set()
    for _offset, line in matches:
        identity = _condense(line)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(line)
    return result


_DIALOGUE_TURN_FUNCTIONS = {
    "trigger", "announcement", "question", "response", "decision", "statement",
}
_DIALOGUE_RESPONSE_FUNCTIONS = {"response"}


def normalize_screenplay_dialogue_chains(script: EpisodeScreenplay) -> EpisodeScreenplay:
    """Make structured dialogue chains authoritative for downstream key-line delivery."""
    if not script.dialogue_chains:
        return script
    flattened: list[str] = []
    for chain in script.dialogue_chains:
        for turn in chain.turns or []:
            speaker = (turn.speaker or "").strip()
            line = (turn.line or "").strip()
            if speaker and line:
                flattened.append(f"{speaker}：{line}")
    script.key_lines = flattened
    return script


def _is_grounded_short_utterance(
    line: str,
    source_line: str,
    source_text: str | None,
) -> bool:
    """放行原文中真实存在的单字口语回应（如“哦。”）。

    这类句子本身可演、可配音；字数下限只应拦空值，不应把原文短回应
    当成视频质量问题。
    """
    spoken = _condense(line)
    evidence = _condense(source_line)
    if not spoken or spoken != evidence:
        return False
    return not source_text or evidence in _condense(source_text)


def validate_dialogue_chains(
    script: EpisodeScreenplay,
    *,
    source_text: str | None,
    required: bool,
    required_dialogue_lines: list[str] | None = None,
) -> list[str]:
    """Validate source-grounded trigger→reply chains before accepting a screenplay."""
    errors: list[str] = []
    chains = script.dialogue_chains or []
    if required and not chains:
        return [
            "dialogue_chains 缺失；必须先从原文建立“触发台词→回答/安慰/反驳”的主线对白链，"
            "再由后端生成 key_lines，禁止直接挑选孤立金句"
        ]
    if not chains:
        return errors

    chain_ids: set[str] = set()
    total_turns = 0
    full_turns = _script_dialogue_turns(script.full_script_text or "")
    full_texts = [turn[2] for turn in full_turns]
    source_fragments = source_dialogue_fragments(source_text)
    required_lines = [line for line in (required_dialogue_lines or []) if (line or "").strip()]
    for chain_index, chain in enumerate(chains):
        tag = f"dialogue_chains[{chain_index}]"
        chain_id = (chain.chain_id or "").strip().upper()
        if not re.fullmatch(r"DC\d{1,3}", chain_id):
            errors.append(f"{tag}.chain_id 必须使用 DC1、DC2 这类稳定编号")
        elif chain_id in chain_ids:
            errors.append(f"{tag}.chain_id=「{chain_id}」重复")
        else:
            chain_ids.add(chain_id)
        if len((chain.topic or "").strip()) < 4:
            errors.append(f"{tag}.topic 过短；请写清这组对白围绕的同一话题")
        turns = chain.turns or []
        total_turns += len(turns)
        if not 1 <= len(turns) <= DIALOGUE_CHAIN_TURNS_HARD_MAX:
            errors.append(
                f"{tag}.turns 需包含 1~{DIALOGUE_CHAIN_TURNS_HARD_MAX} 个连续话轮"
            )
            continue
        if turns and (turns[0].function or "").strip() == "response":
            errors.append(f"{tag} 不能从 response 开始；必须先保留触发句/宣布/提问")
        previous_speaker = ""
        matched_indices: list[int] = []
        for turn_index, turn in enumerate(turns):
            turn_tag = f"{tag}.turns[{turn_index}]"
            speaker = (turn.speaker or "").strip()
            line = (turn.line or "").strip()
            function = (turn.function or "").strip()
            source_line = (turn.source_text or "").strip()
            if not speaker:
                errors.append(f"{turn_tag}.speaker 不能为空")
            grounded_short = _is_grounded_short_utterance(line, source_line, source_text)
            if len(_condense(line)) < 2 and not grounded_short:
                errors.append(f"{turn_tag}.line 过短或为空")
            if function not in _DIALOGUE_TURN_FUNCTIONS:
                errors.append(
                    f"{turn_tag}.function=「{function}」非法；只能是 "
                    "trigger|announcement|question|response|decision|statement"
                )
            if function in _DIALOGUE_RESPONSE_FUNCTIONS and (
                turn_index == 0 or not previous_speaker or previous_speaker == speaker
            ):
                errors.append(
                    f"{turn_tag} 是 response，但前一话轮没有另一角色的触发台词"
                )
            if len(_condense(source_line)) < 2 and not grounded_short:
                errors.append(f"{turn_tag}.source_text 不能为空；必须引用原文对白证据")
            else:
                if source_text and (
                    _longest_run_ratio(source_line, source_text) < KEY_LINE_PRESENT_RATIO
                    and _bigram_coverage(source_line, source_text) < KEY_LINE_BIGRAM_COVERAGE
                ):
                    errors.append(f"{turn_tag}.source_text 未在本集原文中找到：{source_line}")
            candidates = _matching_text_indices(line, full_texts)
            # A short character-address line can fuzzily match an earlier,
            # unrelated utterance that merely contains the same name.  Prefer
            # the declared speaker, then the exact spoken text, before using
            # the ordered fuzzy fallback.  Otherwise a chain fully contained
            # in one scene can be falsely reported as spanning several scenes.
            same_speaker = [
                idx for idx in candidates
                if _condense(full_turns[idx][1]) == _condense(speaker)
            ]
            if same_speaker:
                candidates = same_speaker
            exact_text = [
                idx for idx in candidates
                if _condense(full_turns[idx][2]) == _condense(line)
            ]
            if exact_text:
                candidates = exact_text
            after = [idx for idx in candidates if not matched_indices or idx >= matched_indices[-1]]
            if not after:
                errors.append(f"{turn_tag}.line 未按对白链顺序写进 full_script_text：{line}")
            else:
                matched_indices.append(after[0])
            previous_speaker = speaker
        if matched_indices:
            scenes = {full_turns[idx][0] for idx in matched_indices}
            speakers = {
                _condense(turn.speaker or "")
                for turn in turns
                if _condense(turn.speaker or "")
            }
            # “同一触发→回应链不得跨场”只适用于人物之间的互动链。单人自语/独白
            # 可能因动作节拍被拆成相邻场块，跨块并不会破坏对白因果，不应阻断交付。
            if (
                len(scenes) > 1
                and len(speakers) > 1
                and _dialogue_chain_crosses_hard_scene_boundary(script, scenes)
            ):
                errors.append(f"{tag} 被拆到多个场次；同一触发→回应链必须在同一场完成")

    # 对白密度由本集时长预算和后续逐镜口播容量控制。这里仅保证至少有一组
    # 可追溯的主线对白，不再把“精选台词软建议”误当成整集对白硬上限。
    if total_turns < MIN_KEY_LINES:
        errors.append(
            f"dialogue_chains 共 {total_turns} 个话轮；请至少保留 {MIN_KEY_LINES} 个"
            "推动主线且可追溯的完整话轮"
        )
    if source_fragments:
        opening = source_fragments[0]
        first_chain_source = (
            (chains[0].turns[0].source_text or "").strip()
            if chains and chains[0].turns else ""
        )
        first_chain_line = (
            (chains[0].turns[0].line or "").strip()
            if chains and chains[0].turns else ""
        )
        if _condense(opening) != _condense(first_chain_source):
            errors.append(
                f"原文开场第一句对白未作为 dialogue_chains[0].turns[0]：{opening}；"
                "开场对白是第一条对白链锚点，不能在模型挑选 key_lines 前静默丢失"
            )
        elif (
            _longest_run_ratio(opening, first_chain_line) < KEY_LINE_PRESENT_RATIO
            and _bigram_coverage(opening, first_chain_line) < KEY_LINE_BIGRAM_COVERAGE
        ):
            errors.append(
                f"开场对白锚点被改写到失去原意：原文「{opening}」→台词「{first_chain_line}」；"
                "D001 只允许口语压缩，不得替换成另一句台词"
            )
    if required_lines:
        chain_sources = [
            (turn.source_text or "").strip()
            for chain in chains for turn in (chain.turns or [])
        ]
        chain_lines = [
            (turn.line or "").strip()
            for chain in chains for turn in (chain.turns or [])
        ]
        for required_line in required_lines:
            needle = textmatch.strip_speaker(required_line)
            identity = _condense(needle)
            source_locked = any(identity and identity in _condense(line) for line in chain_sources)
            adapted_locked = any(identity and identity in _condense(line) for line in chain_lines)
            spoken_locked = any(identity and identity in _condense(turn[2]) for turn in full_turns)
            if not source_locked:
                errors.append(
                    f"用户锁定台词未进入 dialogue_chains.source_text：「{required_line}」"
                )
            if not adapted_locked:
                errors.append(
                    f"用户锁定台词未逐字进入 dialogue_chains.line：「{required_line}」"
                )
            if not spoken_locked:
                errors.append(
                    f"用户锁定台词未作为角色对白写进 full_script_text：「{required_line}」"
                )
    return errors


# 逐镜 covers 原子用更宽的"明显缺失"判定：covers 是模型自写的事实改写，连接词（"被…当众宣告为"）
# 会拉低覆盖率，故只在"整件事几乎零命中"时才算漏，容忍同义改写，专拦真正被整段略过的事实。
COVERS_ATOM_ABSENT_RUN = 0.3
COVERS_ATOM_ABSENT_COVERAGE = 0.25
# 方案 B：抽象概括词→具体同义改写兜底。covers 原子常写概括词（"引发全场哄笑与贬损议论"），
# 模型在 action_desc/narration 里写成具体动作（"人群哄笑轰然炸开""摇头嗤声""耳语"），2-gram 覆盖率会被
# 连接词拉低而误判缺失。这里给高频抽象词配同义词组：covers 原子含触发词、shot_text 含任一同义具体词
# 即视为落实。只救同义改写，不救核心动作整段缺失。
# 覆盖范围：人群声（哄笑/议论/嘲讽）、追捧赞叹、成绩段位、震惊错愕——这些是 covers 最常写抽象、
# 模型最常具象化的高频词。新题材出现新抽象词时，按同样格式追加组即可。
COVERS_CROWD_SEMANTIC_GROUPS = (
    (("哄笑", "哄堂", "大笑"), ("哄笑", "大笑", "拍膝", "哗然", "轰然", "爆笑", "哄堂", "笑声", "哄笑")),
    (("议论", "贬损", "非议"), ("议论", "耳语", "指点", "低语", "私语", "纷纷", "交头接耳", "窃窃私语", "指点", "议论")),
    (("嘲讽", "嗤笑", "嘲笑", "讥笑", "耻笑"), ("嘲讽", "嗤笑", "嘲笑", "讥笑", "耻笑", "讥讽", "冷笑", "嗤声", "讥笑声")),
    # 追捧/赞叹类：covers 写"引发追捧"，模型写成"赞叹""欢呼""喝彩""真了不起"
    (("追捧", "赞颂", "称赞", "夸赞"), ("追捧", "赞叹", "欢呼", "喝彩", "叫好", "称赞", "夸赞", "赞颂", "了不起", "种子级")),
    # 成绩/段位类：covers 写"七段成绩"，模型写成"测出七段""斗之气七段""七段！"
    (("成绩", "结果", "测定"), ("成绩", "结果", "测出", "测得", "测定", "段位", "段", "级", "评")),
    # 震惊/错愕类：covers 写"引发震惊"，模型写成"愕然""倒吸凉气""哗然""瞳孔骤缩"
    (("震惊", "惊愕", "错愕", "惊诧"), ("震惊", "惊愕", "错愕", "惊诧", "愕然", "倒吸", "哗然", "瞳孔", "骤缩", "失色")),
)

# 方案 A/C 共用：covers 里"角色开口宣告"的动词与"人群声"的名词。
# 用于判定某镜 covers 是否"不可单镜完成"——同时要求角色开口+人群声时，两类声轨叠加易超单镜口播上限；
# 依赖非路人的圣经外角色开口时，会与 characters 合同相互锁死；功能性路人由独立合同承载。
COVERS_SPOKEN_VERBS = ("宣告", "宣布", "宣读", "宣判", "公布")
COVERS_CROWD_WORDS = ("哄笑", "哄堂", "嘲讽", "议论", "嗤笑", "嘲笑", "讥笑", "耻笑", "哗然", "群嘲",
                      "私语", "耳语", "窃窃", "起哄", "喝彩", "欢呼", "惊呼", "惊叹", "赞叹", "唏嘘")

# P0/P1：导演抽象词不得作为 covers 原子。covers 只承载可拍/可念/可核对事实；
# 「反差/对比/衬托」等意图应写进 beat/primary_action/state_out，否则逐镜词匹配会死循环
# （实测镜05：「与萧炎形成反差」反复修仍无质量提升）。
COVERS_ABSTRACT_DIRECTING_TERMS = (
    "形成反差", "形成对比", "形成对照", "相互映衬", "彼此映衬",
    "反差", "对比", "对照", "衬托", "呼应", "烘托",
    "强调", "暗示", "渲染氛围", "营造氛围", "氛围感",
)
COVERS_ABSTRACT_FILLERS = (
    "形成", "产生", "造成", "之间", "彼此", "相互", "互相",
    "的", "与", "和", "并", "而", "来", "去", "出",
)
COVERS_ABSTRACT_REWRITE_DIRECTIVE = (
    "（导演意图不得写在 covers：请把反差/对比写成双方可见状态——"
    "如「甲测出七段、人群赞叹；乙低头不语握拳」，写入 action_desc/narration/dialogues）"
)
# 逐镜软放行：正文同时出现「落势」与「高势」状态线索，视为抽象反差已具象落实。
COVERS_CONTRAST_LOW_CUES = (
    "低头", "不语", "沉默", "落寞", "握拳", "屈辱", "咬牙", "黯然",
    "冷脸", "垂眸", "攥紧", "咬唇", "后退", "避开",
)
COVERS_CONTRAST_HIGH_CUES = (
    "赞叹", "欢呼", "喝彩", "亮起", "得意", "微笑", "骄傲", "惊呼",
    "高光", "七段", "叫好", "光芒", "起立", "簇拥",
)


def _covers_has_spoken(covers: str) -> bool:
    return any(v in covers for v in COVERS_SPOKEN_VERBS)


def _covers_has_crowd(covers: str) -> bool:
    return any(w in covers for w in COVERS_CROWD_WORDS)


def _strip_abstract_directing_phrases(text: str) -> str:
    """从 covers 文本剥离导演抽象短语，保留可拍事实残段。"""
    out = text or ""
    for term in sorted(COVERS_ABSTRACT_DIRECTING_TERMS, key=len, reverse=True):
        out = out.replace(term, "")
    out = re.sub(r"[；;]{2,}", "；", out)
    out = re.sub(r"[，,]{2,}", "，", out)
    out = out.strip(" \t\r\n；;，,、")
    out = re.sub(r"^[与和并对而的]+", "", out)
    out = re.sub(r"[与和并对而的]+$", "", out)
    return out.strip(" \t\r\n；;，,、")


def _is_abstract_directing_atom(atom: str) -> bool:
    """原子是否几乎全是导演抽象意图（剥离抽象词后几乎无具体事实）。"""
    text = (atom or "").strip()
    if not text or not any(t in text for t in COVERS_ABSTRACT_DIRECTING_TERMS):
        return False
    remainder = _strip_abstract_directing_phrases(text)
    for filler in COVERS_ABSTRACT_FILLERS:
        remainder = remainder.replace(filler, "")
    return len(_condense(remainder)) < 4


def _covers_abstract_atoms(covers: str) -> list[str]:
    return [a for a in _atomize_claim(covers) if _is_abstract_directing_atom(a)]


def _shot_shows_contrast_states(shot_text: str) -> bool:
    """正文是否同时含落势与高势可见状态——用作抽象反差 covers 的具象落实证据。"""
    text = shot_text or ""
    return (
        any(c in text for c in COVERS_CONTRAST_LOW_CUES)
        and any(c in text for c in COVERS_CONTRAST_HIGH_CUES)
    )


def _abstract_contrast_realized_in_shot(atom: str, shot_text: str) -> bool:
    """P1 兜底：抽象反差/对比原子若正文已写出双方可见状态对比，视为已落实。"""
    if not any(t in (atom or "") for t in COVERS_ABSTRACT_DIRECTING_TERMS):
        return False
    return _shot_shows_contrast_states(shot_text)


def rewrite_outline_abstract_covers(outline: StoryboardOutline) -> list[dict]:
    """P1：确定性剥离 covers 中的导演抽象原子/短语，并写入 beat 改写指引。

    - 纯抽象原子整段删除（避免逐镜词匹配「形成反差」死循环）；
    - 混合原子剥离抽象短语、保留具体事实残段；
    - beat 追加可拍改写模板（幂等，不重复追加）。
    就地修改 outline，返回改写记录供监控日志。
    """
    changed: list[dict] = []
    for s in outline.shots:
        covers = (s.covers or "").strip()
        if not covers or not any(t in covers for t in COVERS_ABSTRACT_DIRECTING_TERMS):
            continue
        atoms = _atomize_claim(covers)
        kept: list[str] = []
        removed: list[str] = []
        for atom in atoms:
            if _is_abstract_directing_atom(atom):
                removed.append(atom)
                continue
            if any(t in atom for t in COVERS_ABSTRACT_DIRECTING_TERMS):
                cleaned = _strip_abstract_directing_phrases(atom)
                removed.append(atom)
                if cleaned and len(_condense(cleaned)) >= 2:
                    kept.append(cleaned)
            else:
                kept.append(atom)
        new_covers = "；".join(kept)
        if not removed and new_covers == covers:
            # 句读未切开、但仍含抽象短语：整段剥离一次。
            cleaned_all = _strip_abstract_directing_phrases(covers)
            if cleaned_all == covers:
                continue
            new_covers = cleaned_all
            removed = [covers]
        if new_covers == covers:
            continue
        before = covers
        s.covers = new_covers
        if COVERS_ABSTRACT_REWRITE_DIRECTIVE not in (s.beat or ""):
            s.beat = (s.beat or "").rstrip() + COVERS_ABSTRACT_REWRITE_DIRECTIVE
        changed.append({
            "shot_no": s.shot_no,
            "before": before[:80],
            "after": new_covers[:80],
            "removed": [r[:40] for r in removed[:4]],
        })
    return changed


# 被动宣告句式「被X（当众/高声）宣告」：group(1)=宣告者，group(2)=宣告动词。
# 判定（_covers_outside_spoken）与改写（downgrade_outline_offbible_spoken）共用此正则，口径必然一致。
# 角色名用非贪婪 {2,6}?，避免把后面的「当众/高声」等修饰词吞进角色名（否则圣经内角色「萧战当众」
# 会被误判为圣经外、进而被误降级）。
_OUTSIDE_SPOKEN_RE = re.compile(
    r"被([一-龥]{2,6}?)(?:当众|高声|大声|公然)?(宣告|宣布|宣读|宣判|公布)")


def downgrade_outline_offbible_spoken(outline: StoryboardOutline,
                                      bible: Bible | None) -> list[dict]:
    """把不属于角色圣经、也不是功能性路人的宣告者降级为旁白转述。

    根因：原文常有"测验员"等次要角色开口的关键台词，但其不在角色圣经里。covers 若写成
    "被测验员宣布为低级"，逐镜阶段会卡在"保留测验员→characters 校验失败 / 删测验员→covers
    落实不了"之间死循环（修复停滞根因）。与其反复要求模型自己 reroute（实测会连刷多轮同一错误
    直至修复停滞兜底），不如在校验前就地改写：
    - covers 里"被{圣经外角色}{宣告动词}"去掉角色名（及当众/高声等修饰）→ "被{宣告动词}"，
      事实保留、不再要求该角色开口；改写后判定正则不再命中，方案 A 的硬性报错自然不再触发；
    - 同时在 beat 末尾追加一句旁白转述指令，让逐镜阶段把该宣告交给旁白、不安排该角色出镜。
    角色圣经成员与功能性路人（如测验员）都可合法出镜开口，原样保留。
    就地修改 outline，返回已改写镜头记录（供监控日志）。
    """
    bible_names = {c.name for c in bible.characters} if bible else set()
    if not bible_names:
        return []
    changed: list[dict] = []
    for s in outline.shots:
        covers = s.covers or ""
        if not covers:
            continue
        outside: list[str] = []

        def _sub(m: "re.Match") -> str:
            name, verb = m.group(1), m.group(2)
            if name in bible_names or is_functional_extra(name):
                return m.group(0)
            outside.append(name)
            return "被" + verb     # 去掉圣经外角色名与修饰，仅留被动宣告

        new_covers = _OUTSIDE_SPOKEN_RE.sub(_sub, covers)
        if not outside:
            continue
        names = "/".join(dict.fromkeys(outside))  # 去重保序
        s.covers = new_covers
        directive = f"（{names}不在角色圣经：相关宣告改由旁白转述交代，勿安排其出镜或开口）"
        if directive not in (s.beat or ""):
            s.beat = (s.beat or "").rstrip() + directive
        changed.append({"shot_no": s.shot_no, "names": list(dict.fromkeys(outside)),
                        "before": covers[:80], "after": new_covers[:80]})
    return changed


def defer_establishing_covers(outline: StoryboardOutline, episode_no: int) -> list[dict]:
    """减重试 #2：第一集第 1 镜被 _first_shot_rule 强制为「开场建场镜」——只交代世界观/主角处境、
    动作克制、不抛核心冲突。但大纲常把判决/反转类 covers（如「全场最低」）也派给第 1 镜，于是逐镜
    阶段陷入两条硬指令对冲：照建场写→漏 covers（报「未落实本镜大纲」）；硬塞判决→只能借测验员/
    围观者开口→characters 圣经校验失败。实测会先漏 covers、再引入圣经外角色，连打两轮修复。

    这里把第 1 镜的 covers 顺延合并到第 2 镜：建场镜不再被要求落实关键内容（brief.covers 清空，
    模型可专心建场），关键内容仍留在大纲（第 2 镜）里、整集 covers 覆盖校验不会判漏；第 2 镜不受
    建场约束，可正常把判决拍出来/念出来。只对第一集生效；常规集第 1 镜是 hook 镜、不受建场约束，
    原样保留。就地修改 outline，返回调整记录供监控日志。"""
    if int(episode_no or 0) != 1:
        return []
    shots = outline.shots
    if len(shots) < 2:
        return []
    first, second = shots[0], shots[1]
    moved = (first.covers or "").strip()
    if not moved:
        return []
    first.covers = ""
    existing = (second.covers or "").strip()
    second.covers = f"{moved}；{existing}" if existing else moved
    moved_key_lines = list(first.key_line_ids or [])
    if moved_key_lines:
        second.key_line_ids = list(dict.fromkeys([
            *moved_key_lines,
            *(second.key_line_ids or []),
        ]))
        first.key_line_ids = []
    moved_audio_cast = list(first.audio_cast or [])
    if moved_audio_cast:
        second.audio_cast = list(dict.fromkeys([
            *moved_audio_cast,
            *(second.audio_cast or []),
        ]))
        first.audio_cast = []
    return [{
        "shot_no": 1,
        "deferred_to": 2,
        "covers": moved[:80],
        "key_line_ids": moved_key_lines,
        "audio_cast": moved_audio_cast,
    }]


def _covers_outside_spoken(covers: str, bible_names: set[str]) -> list[str]:
    """返回既不在角色圣经、也不是功能性路人的宣告者。

    只看被动句「被X（当众）宣告」——「被」之后的 X 几乎总是人名，精度高、误伤低；
    主动句「X宣告」里的 X 可能是「石碑/天空/系统」等非人名，不校验。
    用于在大纲阶段拦截'依赖圣经外角色开口'的不可拍 covers，避免逐镜阶段 characters 校验与
    covers 落实相互锁死。
    """
    if not bible_names or not covers:
        return []
    found = {m.group(1) for m in _OUTSIDE_SPOKEN_RE.finditer(covers)}
    return [n for n in found if n not in bible_names and not is_functional_extra(n)]


def _crowd_semantic_hit(atom: str, haystack: str) -> bool:
    """方案 B：covers 原子含人群声概括词（哄笑/议论/嘲讽），shot_text 含任一同义具体词即算落实。

    专治"引发全场哄笑与贬损议论"→"人群哄笑轰然炸开...摇头嗤声...耳语"这类同义改写误判——
    2-gram 覆盖率会被连接词拉低，但"哄笑/嗤声/耳语"确实是"哄笑与议论"的具体化，不该判缺失。
    """
    for triggers, synonyms in COVERS_CROWD_SEMANTIC_GROUPS:
        if any(t in atom for t in triggers):
            if any(s in haystack for s in synonyms):
                return True
    return False


def _claim_clearly_absent(atom: str, haystack: str) -> bool:
    """这条原子在文本里是否"几乎完全没出现"——主干连续命中和 2-gram 覆盖都低于宽松下限才算缺失。"""
    core = _strip_speaker(atom)
    if (_longest_run_ratio(core, haystack) >= COVERS_ATOM_ABSENT_RUN
            or _bigram_coverage(core, haystack) >= COVERS_ATOM_ABSENT_COVERAGE):
        return False
    # 方案 B：人群声概括→具体同义改写兜底（哄笑/议论/嘲讽）
    if _crowd_semantic_hit(core, haystack):
        return False
    return True


def normalize_screenplay_ledgers(script: EpisodeScreenplay) -> EpisodeScreenplay:
    """Renderability：清洗空壳 events/ledger，必要时从 plot_spine 确定性回填。

    模型常输出「有壳无肉」的 information_ledger（content/event_id 为空），旧 QA 会硬拦并卡在
    WARNING 候选。主线权威是 plot_spine；台账只是下游拆镜索引，允许从 spine 合成最小完备集。
    """
    spine = script.plot_spine
    # 1) 清洗 events：丢掉缺 id / 缺状态链的空壳
    cleaned_events: list[StoryEvent] = []
    seen_eids: set[str] = set()
    for event in script.events or []:
        eid = (event.event_id or "").strip()
        if not eid or eid in seen_eids:
            continue
        if len((event.visible_change or "").strip()) < 2 and len((event.state_out or "").strip()) < 2:
            continue
        seen_eids.add(eid)
        cleaned_events.append(event)

    # 2) 若 events 过稀且有 spine → 按 must_keep 节拍合成
    must_beats = [b for b in (spine.spine_beats if spine else []) if b.must_keep] or list(
        (spine.spine_beats if spine else []) or []
    )
    if spine and must_beats and len(cleaned_events) < min(3, len(must_beats)):
        cleaned_events = []
        for i, beat in enumerate(must_beats, start=1):
            cleaned_events.append(StoryEvent(
                event_id=f"E{i}",
                source_span=(beat.beat_id or f"S{i:02d}"),
                source_fact=f"{(beat.who or '').strip()}{(beat.does or '').strip()}".strip() or f"主线节拍{i}",
                state_in="节拍开始",
                trigger=(beat.does or "").strip() or "主线推进",
                visible_change=f"{(beat.who or '').strip()}{(beat.does or '').strip()}".strip() or f"主线动作{i}",
                state_out=(beat.turn or "").strip() or "局势变化",
                must_keep=bool(beat.must_keep),
            ))
    script.events = cleaned_events
    event_ids = {(e.event_id or "").strip() for e in cleaned_events if (e.event_id or "").strip()}

    # 3) 清洗 ledger：丢掉无中文 content 的空壳；event_id 空/非法时按序号挂到事件
    cleaned_ledger: list[InformationItem] = []
    seen_iids: set[str] = set()
    event_list = list(cleaned_events)
    for idx, item in enumerate(script.information_ledger or []):
        content = (item.content or "").strip()
        if len(content) < 4 or not re.search(r"[\u3400-\u9fff]", content):
            continue
        iid = (item.info_id or "").strip() or f"I{idx + 1}"
        if not re.fullmatch(r"I\d{1,4}", iid, flags=re.IGNORECASE):
            iid = f"I{idx + 1}"
        if iid in seen_iids:
            continue
        eid = (item.event_id or "").strip()
        if eid not in event_ids:
            eid = event_list[min(idx, len(event_list) - 1)].event_id if event_list else ""
        if not eid:
            continue
        seen_iids.add(iid)
        cleaned_ledger.append(InformationItem(
            info_id=iid,
            event_id=eid,
            content=content,
            delivery_owner=item.delivery_owner if item.delivery_owner in DELIVERY_OWNERS else "visual_action",
            speaker_id=item.speaker_id,
            exact_text=item.exact_text,
            reinforcement_allowed=bool(item.reinforcement_allowed),
            status=(item.status or "unassigned"),
            assigned_shot_no=item.assigned_shot_no,
        ))

    # 4) ledger 仍空且有 events → 每事件一条主线信息
    if not cleaned_ledger and cleaned_events:
        for i, event in enumerate(cleaned_events, start=1):
            content = (
                (event.visible_change or "").strip()
                or (event.source_fact or "").strip()
                or (event.state_out or "").strip()
                or f"主线信息{i}"
            )
            if not re.search(r"[\u3400-\u9fff]", content):
                content = f"主线节拍{i}的局势变化"
            cleaned_ledger.append(InformationItem(
                info_id=f"I{i}",
                event_id=event.event_id,
                content=content[:80],
                delivery_owner="visual_action",
                status="unassigned",
            ))

    # 5) 加帽：≤ spine×2
    spine_n = len(must_beats) if must_beats else 0
    if spine_n:
        cap = max(SPINE_BEATS_MIN, spine_n * 2)
        cleaned_ledger = cleaned_ledger[:cap]

    script.information_ledger = cleaned_ledger
    if not (script.episode_premise or "").strip() and spine and (spine.episode_premise or "").strip():
        script.episode_premise = spine.episode_premise.strip()
    return script


def normalize_screenplay_candidate(script: EpisodeScreenplay) -> EpisodeScreenplay:
    """在 QA 之前生成规范化副本；QA 本身不得修改候选内容。"""
    normalized = script.model_copy(deep=True)
    normalize_screenplay_ledgers(normalized)
    normalize_screenplay_dialogue_chains(normalized)
    return normalized


def validate_plot_spine(script: EpisodeScreenplay) -> list[str]:
    """先校验主线骨架，再允许正文通过（Renderability First）。"""
    errors: list[str] = []
    spine = script.plot_spine
    if spine is None:
        errors.append(
            "plot_spine 缺失；请先输出主线骨架（episode_premise / spine_beats / must_keep_ending / drop_list），"
            "再写正文——只保改变局势的主线，禁止抠细节"
        )
        return errors
    if len((spine.episode_premise or "").strip()) < 8:
        errors.append("plot_spine.episode_premise 过短；请用一句话写本集主角要什么、碰到什么阻力")
    beats = spine.spine_beats or []
    if not SPINE_BEATS_MIN <= len(beats) <= SPINE_BEATS_MAX:
        errors.append(
            f"plot_spine.spine_beats 共 {len(beats)} 条；必须在 {SPINE_BEATS_MIN}~{SPINE_BEATS_MAX} 条，"
            "每条写清谁做了什么→局势变化"
        )
    beat_ids: set[str] = set()
    must_keep_count = 0
    for i, beat in enumerate(beats):
        tag = f"plot_spine.spine_beats[{i}]"
        bid = (beat.beat_id or "").strip()
        if not bid:
            errors.append(f"{tag}.beat_id 不能为空")
        elif bid in beat_ids:
            errors.append(f"{tag}.beat_id=「{bid}」重复")
        else:
            beat_ids.add(bid)
        if len((beat.who or "").strip()) < 1:
            errors.append(f"{tag}.who 不能为空")
        if len((beat.does or "").strip()) < 4:
            errors.append(f"{tag}.does 过短；请写可见/可听的主动作")
        if len((beat.turn or "").strip()) < 4:
            errors.append(f"{tag}.turn 过短；请写局势变化")
        if beat.must_keep:
            must_keep_count += 1
        errors.extend(overdetail_errors(
            f"{beat.who}{beat.does}{beat.turn}", tag))
    if beats and must_keep_count < 3:
        errors.append(
            f"plot_spine 中 must_keep=true 仅 {must_keep_count} 条；主线因果至少保留 3 条必拍节拍"
        )
    if len((spine.must_keep_ending or "").strip()) < 8:
        errors.append(
            "plot_spine.must_keep_ending 过短；请锁定本章收束（与原文本章结局同向，禁止发明下一章钩子）"
        )
    drops = [d.strip() for d in (spine.drop_list or []) if d and d.strip()]
    if len(drops) < DROP_LIST_MIN:
        errors.append(
            f"plot_spine.drop_list 仅 {len(drops)} 条；至少列出 {DROP_LIST_MIN} 条「本章有但不拍」的支线/气氛戏"
        )
    return errors


def validate_screenplay(script: EpisodeScreenplay, bible: Bible, expected_beats: int,
                        episode_no: int | None = None, source_text: str | None = None,
                        require_dialogue_chains: bool = False,
                        required_dialogue_lines: list[str] | None = None) -> list[str]:
    """纯 QA：只读取候选并返回问题，不补字段、不覆盖投影、不修改输入。"""
    errors: list[str] = []
    errors.extend(validate_dialogue_chains(
        script, source_text=source_text, required=require_dialogue_chains,
        required_dialogue_lines=required_dialogue_lines,
    ))
    if episode_no is not None and script.episode_no != episode_no:
        errors.append(f"episode_no={script.episode_no}，必须等于 {episode_no}")
    if (script.mode or "full_script") != "full_script":
        errors.append(f"mode=「{script.mode}」非法；剧本台仅支持 full_script")
    errors.extend(validate_plot_spine(script))
    if len((script.title or "").strip()) < 2:
        errors.append("title 过短或缺失；请填写本集标题")
    if len((script.logline or "").strip()) < 8:
        errors.append("logline 过短或缺失；请用一句话概括本集核心事件")
    if len((script.script_format_note or "").strip()) < 6:
        errors.append("script_format_note 过短或缺失；请说明正文采用的台本格式")
    scenes = script.scene_outline or []
    if not SCENE_OUTLINE_MIN <= len(scenes) <= SCENE_OUTLINE_MAX:
        errors.append(
            f"scene_outline 场次数量为 {len(scenes)}；只演主线时需提供 "
            f"{SCENE_OUTLINE_MIN}~{SCENE_OUTLINE_MAX} 场连续场次结构"
        )
    bible_names = {c.name for c in bible.characters}
    for i, scene in enumerate(scenes, start=1):
        heading = (scene.scene_heading or "").strip()
        tag = f"scene_outline 第{i}场" + (f"「{heading}」" if heading else "")
        if scene.scene_no != i:
            errors.append(f"{tag}.scene_no 必须从 1 连续递增；当前为 {scene.scene_no}")
        if len((scene.scene_heading or "").strip()) < 4:
            errors.append(f"{tag}.scene_heading 过短；请写成可读的场次标题")
        if len((scene.story_function or "").strip()) < 6:
            errors.append(f"{tag}.story_function 过短；请说明本场戏剧功能")
        if len((scene.summary or "").strip()) < 16:
            errors.append(f"{tag}.summary 过短；请概括本场具体戏剧内容")
        if len((scene.turn or "").strip()) < 4:
            errors.append(f"{tag}.turn 过短；请说明本场交给下一场的状态变化")
        if len((scene.source_basis or "").strip()) < 8:
            errors.append(f"{tag}.source_basis 过短；请保留本场原文依据")
        if not scene.characters:
            errors.append(f"{tag}.characters 不能为空；请写本场实际参与角色")
        unknown = [
            name for name in scene.characters
            if name not in bible_names and not is_functional_extra(name)
        ]
        if bible_names and unknown:
            errors.append(f"{tag}.characters 含角色圣经外角色：{unknown}")
        errors.extend(overdetail_errors(
            f"{scene.summary}{scene.conflict}{scene.turn}", tag))
    full_text = (script.full_script_text or "").strip()
    spine_n = len((script.plot_spine.spine_beats if script.plot_spine else None) or [])
    min_script_chars = max(160, spine_n * 36 if spine_n else max(160, expected_beats * 30))
    if len(full_text) < min_script_chars:
        errors.append(
            f"full_script_text 过短；当前仅 {len(full_text)} 字，至少需要 {min_script_chars} 字"
            "（只演主线骨架，勿注水细节）"
        )
    # 可拍性词表只约束画面动作，不约束角色说出的原文台词。过去直接扫描全文会把
    # 必保留台词里的“微微”等词也误判成不可拍细节，造成无意义的修复死循环。
    action_text = "\n".join(
        line for line in full_text.splitlines()
        if not SCRIPT_DIALOGUE_LINE_RE.match(line.strip())
        and not SCRIPT_SCENE_HEADING_RE.match(line.strip())
    )
    errors.extend(overdetail_errors(action_text, "full_script_text"))
    for term in FULL_SCRIPT_FORBIDDEN_TERMS:
        if term in full_text:
            errors.append(f"full_script_text 含禁用词「{term}」；剧本台正文不能写拍卡/分镜/执行语言")
    heading_matches = SCRIPT_SCENE_HEADING_RE.findall(full_text)
    if len(heading_matches) < 3:
        errors.append("full_script_text 缺少足够的场次标题；请使用“【场1】...”这类场次化台本格式")
    elif scenes and len(heading_matches) != len(scenes):
        errors.append(f"full_script_text 场次标题数 {len(heading_matches)} 与 scene_outline 场次数 {len(scenes)} 不一致")
    content_lines = [ln for ln in full_text.splitlines() if ln.strip()]
    min_lines = max(6, len(scenes) * 2)
    if len(content_lines) < min_lines:
        errors.append("full_script_text 段落过少；请按场次标题、动作段、对白段分行书写，不要挤成一段梗概")
    dialogue_lines = [
        match.group(0) for match in _iter_script_sound_matches(full_text)
    ]
    if len(dialogue_lines) < 2:
        errors.append("full_script_text 对白行过少；请按“角色名：台词”写出真正可演的对白")
    if bible_names:
        offbible_speakers = sorted({
            match.group(1).strip()
            for match in _iter_script_sound_matches(full_text)
            if match.group(1).strip() != "旁白"
            and match.group(1).strip() not in bible_names
            and not is_functional_extra(match.group(1).strip())
        })
        if offbible_speakers:
            errors.append(
                "full_script_text 含未进入人物谱的具名说话人："
                f"{offbible_speakers}；重要具名角色必须先由人物发现步骤补进人物谱，"
                "无需定妆的临时角色请改用测验员/守卫/围观者等功能性身份标签"
            )
    if len((script.emotional_curve or "").strip()) < 6:
        errors.append("emotional_curve 过短或缺失；请说明本集情绪推进")
    ending_hook = (script.ending_hook or "").strip()
    no_episode_hook_markers = {"无", "无钩子", "无集级钩子", "（无）"}
    explicit_no_episode_hook = ending_hook in no_episode_hook_markers or ending_hook.startswith("无集级")
    if len(ending_hook) < 6 and not explicit_no_episode_hook:
        errors.append("ending_hook 过短或缺失；请明确本集结尾钩子")
    if len((script.source_basis or "").strip()) < 12:
        errors.append("source_basis 过短或缺失；请概括本集原文依据与关键事件")
    if len((script.dramatic_question or "").strip()) < 6:
        errors.append("dramatic_question 过短或缺失；请用一句话写出本集观众心里追问的戏剧问题")
    if len((script.protagonist_goal or "").strip()) < 4:
        errors.append("protagonist_goal 过短或缺失；请写本集主角看得见、可完成的外在目标")
    if len((script.obstacle or "").strip()) < 4:
        errors.append("obstacle 过短或缺失；请写本集阻力（外部对手/规则 + 内部恐惧/执念）")
    if len((script.stakes or "").strip()) < 4:
        errors.append("stakes 过短或缺失；请写失败代价（输了会失去什么关系/尊严/目标）")
    key_lines = [ln.strip() for ln in (script.key_lines or []) if ln and ln.strip()]
    if len(key_lines) < MIN_KEY_LINES:
        errors.append(
            f"key_lines 仅 {len(key_lines)} 条；请保留至少 {MIN_KEY_LINES} 条推动主线的台词")
    bible_names = {c.name for c in bible.characters}
    if bible_names:
        non_bible_key_lines = []
        for ln in key_lines:
            speaker = _speaker_name(ln)
            if not speaker:
                continue
            if speaker not in bible_names and not is_functional_extra(speaker):
                non_bible_key_lines.append(ln)
        if non_bible_key_lines:
            shown = "；".join(non_bible_key_lines[:KEY_CONTENT_MAX_REPORT])
            extra = (f"（另有 {len(non_bible_key_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                     if len(non_bible_key_lines) > KEY_CONTENT_MAX_REPORT else "")
            errors.append(
                f"key_lines 有 {len(non_bible_key_lines)} 条含非人物谱角色台词：{shown}{extra}"
                f"；key_lines 只能保留角色圣经角色（{'、'.join(sorted(bible_names))}）的台词，"
                "测验员/守卫等功能性角色可作为对白链触发者进入 key_lines；旁白不得进入；"
                "其他具名角色必须先补进人物谱")
    # 主线台词只能由真正的“角色名：台词”行交付。旧校验拿整个 full_script_text
    # 当 haystack，导致模型把台词抄进动作描述/梗概也能通过，页面看得到 key_lines
    # 清单，角色却从未开口。
    dialogue_turns = _script_dialogue_turns(full_text)
    script_dialogues: list[tuple[str, str]] = [
        (speaker, spoken) for _scene_no, speaker, spoken in dialogue_turns
    ]
    missing_in_dialogue: list[str] = []
    mismatched: list[str] = []
    for ln in key_lines:
        core = _strip_speaker(ln)
        matching_speakers = {
            speaker for speaker, spoken in script_dialogues
            if _longest_run_ratio(core, spoken) >= KEY_LINE_PRESENT_RATIO
            or _bigram_coverage(core, spoken) >= KEY_LINE_BIGRAM_COVERAGE
        }
        if not matching_speakers:
            missing_in_dialogue.append(ln)
            continue
        expected_speaker = _speaker_name(ln)
        if expected_speaker and expected_speaker not in matching_speakers:
            mismatched.append(
                f"{ln}（正文归属为：{'、'.join(sorted(matching_speakers))}）"
            )
    if missing_in_dialogue:
        shown = "；".join(missing_in_dialogue[:KEY_CONTENT_MAX_REPORT])
        errors.append(
            f"key_lines 有 {len(missing_in_dialogue)} 条未真正写进 full_script_text 的角色对白：{shown}"
            "；主线台词必须落在“角色名：台词”对白行，动作描述或梗概中的文字不算交付")
    if mismatched:
        shown = "；".join(mismatched[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(mismatched) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(mismatched) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"key_lines 有 {len(mismatched)} 条台词的说话人与 full_script_text 不一致：{shown}{extra}"
            "；同一句台词在 key_lines 和 full_script_text 中必须由同一角色说出")
    errors.extend(key_line_order_errors(
        key_lines,
        [spoken for _scene_no, _speaker, spoken in dialogue_turns],
        subject="full_script_text",
    ))
    orphan_responses: list[str] = []
    spoken_turn_texts = [spoken for _scene_no, _speaker, spoken in dialogue_turns]
    key_turn_indices = {
        index
        for key_line in key_lines
        for index in _matching_text_indices(key_line, spoken_turn_texts)
    }
    for line in key_lines:
        if not _is_context_dependent_dialogue(line):
            continue
        candidates = _matching_text_indices(
            line, [spoken for _scene_no, _speaker, spoken in dialogue_turns]
        )
        if not candidates:
            continue
        turn_index = candidates[0]
        scene_no, speaker, _spoken = dialogue_turns[turn_index]
        prior_context = [
            dialogue_turns[prior_index]
            for prior_index in range(max(0, turn_index - 2), turn_index)
            if dialogue_turns[prior_index][0] == scene_no
            and dialogue_turns[prior_index][1] != speaker
            and prior_index in key_turn_indices
        ]
        if not prior_context:
            orphan_responses.append(line)
    if orphan_responses:
        shown = "；".join(orphan_responses[:KEY_CONTENT_MAX_REPORT])
        errors.append(
            f"主线对白上下文断裂：{shown}；这类回答/安慰/反驳依赖前文，"
            "必须把同一场前两轮内另一角色的触发台词也列入 key_lines，"
            "让下游整组保留，不能让主要角色突然冒出一句回应"
        )
    _ = source_text  # 保留参数兼容；全量原文台词入库已废止
    key_points = [pt.strip() for pt in (script.key_plot_points or []) if pt and pt.strip()]
    if len(key_points) < MIN_KEY_PLOT_POINTS:
        errors.append(
            f"key_plot_points 仅 {len(key_points)} 条；请列出至少 {MIN_KEY_PLOT_POINTS} 条与 spine 对齐的局势变化"
            f"（上限 {MAX_KEY_PLOT_POINTS}）")
    if len(key_points) > MAX_KEY_PLOT_POINTS:
        errors.append(
            f"key_plot_points 共 {len(key_points)} 条，超过上限 {MAX_KEY_PLOT_POINTS}；"
            "只保留主线局势变化，细节支线放入 drop_list"
        )
    event_ids: set[str] = set()
    if not script.events:
        errors.append("events 不能为空；必须把完整剧本拆成可追溯的状态变化事件")
    for i, event in enumerate(script.events or []):
        tag = f"events[{i}]"
        event_id = (event.event_id or "").strip()
        if not event_id:
            errors.append(f"{tag}.event_id 不能为空")
        elif event_id in event_ids:
            errors.append(f"{tag}.event_id=「{event_id}」重复；events.event_id 必须唯一")
        else:
            event_ids.add(event_id)
        for field in ("state_in", "visible_change", "state_out"):
            if len((getattr(event, field, "") or "").strip()) < 4:
                errors.append(f"{tag}.{field} 缺失或过短；事件必须写清状态输入、可见变化和状态输出")
        errors.extend(overdetail_errors(
            f"{event.visible_change}{event.state_in}{event.state_out}", tag))
    info_ids: set[str] = set()
    if not script.information_ledger:
        errors.append("information_ledger 不能为空；必须为观众需要获得的剧情信息建立中文交付台账")
    ledger = script.information_ledger or []
    ledger_cap = max(SPINE_BEATS_MIN * 2, (spine_n or len(script.events or [])) * 2)
    if spine_n and len(ledger) > ledger_cap:
        errors.append(
            f"information_ledger 共 {len(ledger)} 条，超过主线容量上限 {ledger_cap}"
            f"（≤ spine_beats×2）；请只登记主线信息，禁止为气氛声拆 info"
        )
    for i, item in enumerate(ledger):
        tag = f"information_ledger[{i}]"
        info_id = (item.info_id or "").strip()
        if not info_id:
            errors.append(f"{tag}.info_id 不能为空")
        elif info_id in info_ids:
            errors.append(f"{tag}.info_id=「{info_id}」重复；information_ledger.info_id 必须唯一")
        else:
            info_ids.add(info_id)
        if info_id and not re.fullmatch(r"I\d{1,4}", info_id, flags=re.IGNORECASE):
            errors.append(
                f"{tag}.info_id=「{info_id}」不是稳定内部编号；请使用 I1、I2 这类编号，"
                "不要使用英文 snake_case 剧情描述"
            )
        content = (item.content or "").strip()
        if len(content) < 4 or not re.search(r"[\u3400-\u9fff]", content):
            errors.append(f"{tag}.content 必须用简体中文写清观众获得的具体信息")
        event_id = (item.event_id or "").strip()
        if not event_id or event_id not in event_ids:
            errors.append(f"{tag}.event_id=「{event_id}」未对应 events 中的有效事件")
        if item.delivery_owner and item.delivery_owner not in DELIVERY_OWNERS:
            errors.append(f"信息 {item.info_id} 的 delivery_owner={item.delivery_owner} 不合法")
    if script.plot_spine and script.plot_spine.drop_list:
        drop_hits = []
        for drop in script.plot_spine.drop_list:
            d = (drop or "").strip()
            if len(_condense(d)) < 6:
                continue
            if _bigram_coverage(d, full_text) >= 0.55:
                drop_hits.append(d)
        if drop_hits:
            shown = "；".join(drop_hits[:KEY_CONTENT_MAX_REPORT])
            errors.append(
                f"full_script_text 又写回了 drop_list 中的内容：{shown}；"
                "已声明不拍的支线/气氛戏不得出现在正文"
            )
    errors.extend(adaptation_hook_errors(script))
    return list(dict.fromkeys(errors))

def _screenplay_sound_stats(script: EpisodeScreenplay) -> dict[str, int]:
    full_text = (script.full_script_text or "").strip()
    stats = {"dialogues": 0, "inner": 0, "narration": 0, "quoted_voice": 0}
    for match in _iter_script_sound_matches(full_text):
        speaker = match.group(1).strip()
        parenthetical = (match.group(2) or "").strip()
        if speaker == "旁白":
            stats["narration"] += 1
        elif any(marker in parenthetical for marker in INNER_VOICE_MARKERS):
            stats["inner"] += 1
        else:
            stats["dialogues"] += 1
    stats["quoted_voice"] = len(re.findall(r"(?:声音|嘲讽声|恭维|呼唤|自语|旁白)[^。！？\n]{0,24}[:：]“[^”]{2,}”", full_text))
    stats["narration"] += full_text.count("旁白：")
    return stats


def validate_storyboard_soundtrack(board: Storyboard, screenplay: EpisodeScreenplay,
                                   target_duration_s: int) -> list[str]:
    """校验从完整剧本拆分出的分镜是否保留了可听见的剧情信息。

    通用 validate_storyboard 只管结构与画面可生成性；这里专门约束“剧本台已有台词/内心/旁白，
    分镜台不能把它们压成纯画面卡”。反应镜头可仅保留环境声/氛围，不强制 75% 镜头都有口播。
    错误会进入修复回路，让模型补齐关键声轨。
    """
    errors: list[str] = []
    shots = board.shots
    if not shots:
        return errors

    stats = _screenplay_sound_stats(screenplay)
    script_sound_cues = sum(stats.values())
    if script_sound_cues == 0:
        return errors

    script_dialogue_targets = stats["dialogues"] + stats["quoted_voice"]
    if script_dialogue_targets >= 2:
        dialogue_count = sum(len(shot.dialogues) for shot in shots)
        # Renderability：只要求覆盖主线口播密度，不再按「剧本对白处数 × 50% 镜头」硬逼。
        key_line_n = len([ln for ln in (screenplay.key_lines or []) if ln and str(ln).strip()])
        min_dialogues = max(1, min(script_dialogue_targets, key_line_n or 2, 4))
        if dialogue_count < min_dialogues:
            errors.append(
                f"分镜对白不足：主线至少需要约 {min_dialogues} 句角色开口"
                f"（当前 dialogues={dialogue_count}）；请把 key_lines/主线对白写入 dialogues，"
                "群嘲等环境声可写进 action_desc，不要为密度硬塞碎镜")

    # 产品合同禁止旁白/内心OS：剧本中的内心描写改由画面姿态表达，不再强制落到 narration。
    return errors


def validate_storyboard_preserves_key_content(board: Storyboard,
                                              screenplay: EpisodeScreenplay) -> list[str]:
    """防丢失核心校验：分镜必须保留剧本台显式标记的【必保留关键台词 / 关键剧情点】。

    与 validate_storyboard_soundtrack 互补——后者只看"有没有声轨、声轨够不够多"，
    这里看"剧本里那几句金句/那几个关键反转有没有真的落到镜头里"，专治"重要台词/剧情被静默丢弃"。
    务实优先：用模糊匹配只拦【明显丢失】，命中即放行；剧本未声明清单时（旧数据/兜底）直接放行。
    """
    errors: list[str] = []
    shots = board.shots
    if not shots:
        return errors
    key_lines = [ln.strip() for ln in (screenplay.key_lines or []) if ln and ln.strip()]
    key_points = [pt.strip() for pt in (screenplay.key_plot_points or []) if pt and pt.strip()]

    # 关键台词只认有效口播（spoken_text_of）；source_excerpt 是审计证据，不能证明「已说出」。
    spoken_text = "".join(spoken_text_of(s) for s in shots)
    # 剧情点/画面覆盖可用动作描述；仍不含 source_excerpt，避免把摘录当已拍证据。
    visual_text = spoken_text + "".join((s.action_desc or "") for s in shots)

    missing_lines = []
    for ln in key_lines:
        core = _strip_speaker(ln)
        if (
            _longest_run_ratio(core, spoken_text) < KEY_LINE_PRESENT_RATIO
            and _bigram_coverage(core, spoken_text) < KEY_LINE_BIGRAM_COVERAGE
        ):
            missing_lines.append(ln)
    if missing_lines:
        shown = "；".join(missing_lines[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_lines) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"分镜丢失了剧本标记的 {len(missing_lines)} 条主线台词：{shown}{extra}；"
            "请把它们写进对应镜头的 dialogues（人物开口），不要在压缩中丢弃")
    ordered_spoken_turns = [
        dialogue.line
        for shot in shots
        for dialogue in (shot.dialogues or [])
        if (dialogue.line or "").strip()
    ]
    errors.extend(key_line_order_errors(
        key_lines, ordered_spoken_turns, subject="分镜 dialogues",
    ))

    spine = screenplay.plot_spine
    # textmatch 模糊匹配只用于没有稳定 ID 的旧数据降级判定。新版剧本已有 plot_spine，
    # 其 must_keep 由下方 validate_spine_delivery_ledger 逐 ID 校验；若仍用自由文本摘要的
    # 2-gram 重合率单独报 blocker，会把“萧炎三段低级已由 S01+KL01/KL02 交付”误判成
    # 没逐字写“天才跌落谷底”而缺剧情，与 textmatch 模块的主从口径相冲突。
    if not (spine and spine.spine_beats):
        missing_points = [
            pt for pt in key_points
            if _bigram_coverage(pt, visual_text) < KEY_POINT_COVERAGE
        ]
        if missing_points:
            shown = "；".join(missing_points[:KEY_CONTENT_MAX_REPORT])
            extra = (f"（另有 {len(missing_points) - KEY_CONTENT_MAX_REPORT} 条从略）"
                     if len(missing_points) > KEY_CONTENT_MAX_REPORT else "")
            errors.append(
                f"分镜丢失了剧本标记的 {len(missing_points)} 条主线剧情点：{shown}{extra}；"
                "请在对应镜头的 action_desc 或声轨中体现这些局势变化，不能整段略过")

    if spine and spine.spine_beats:
        errors.extend(validate_spine_delivery_ledger(board, screenplay))
        for drop in spine.drop_list or []:
            d = (drop or "").strip()
            if len(_condense(d)) < 6:
                continue
            if _bigram_coverage(d, visual_text) >= 0.55:
                errors.append(
                    f"分镜又拍回了 drop_list 内容「{d[:40]}」；已声明不拍的支线不得进入分镜"
                )
                break
    return errors


def key_line_delivery_errors(shot: Shot, screenplay: EpisodeScreenplay | None = None) -> list[str]:
    """单镜声明的 key_line_ids 必须真实出现在有效口播段中（source_excerpt 不算交付）。"""
    kids = [str(k).strip().upper() for k in (shot.key_line_ids or []) if str(k).strip()]
    if not kids:
        return []
    catalog = key_line_catalog(screenplay) if screenplay is not None else {}
    spoken = spoken_text_of(shot)
    errors: list[str] = []
    for kid in kids:
        text = catalog.get(kid)
        if text:
            core = _strip_speaker(text)
            if (
                _longest_run_ratio(core, spoken) < KEY_LINE_PRESENT_RATIO
                and _bigram_coverage(core, spoken) < KEY_LINE_BIGRAM_COVERAGE
            ):
                errors.append(
                    f"shot_no={shot.shot_no} 声明了 {kid} 但有效口播未说出「{core[:36]}」；"
                    "关键台词必须出现在 dialogues/audio_timeline，source_excerpt 不算交付"
                )
        # 无剧本 catalog 时只校验 ID 格式（格式已由 shot_id_space_errors 负责）
    return errors


def validate_spine_delivery_ledger(
    board: Storyboard, screenplay: EpisodeScreenplay
) -> list[str]:
    """结构化主线覆盖（PRD VAL-422 §4.4.3）：以 spine_beat_ids + I*/KL* 台账为主判据。

    - 至少一个镜头声明对应 spine_beat_id，且 beat.who 必须在该镜或相邻镜的可见动作中出现；
      只写 ID、由测验员宣布结果或由路人谈论当事人，不能替代真正拍出动作主体；
    - 若 beat 绑定了 information_ids/key_line_ids，则这些原子必须由声明该 spine 的镜头
      （或其相邻镜头）交付；
    - 全集没有任何结构化 ID 时，二元字组失败只产生 LEGACY_COVERAGE_UNCERTAIN，
      不得单独冒充 must_keep missing blocker。
    """
    spine = screenplay.plot_spine
    if not spine or not spine.spine_beats:
        return []
    shots = board.shots or []
    delivered_ids: set[str] = set()
    shots_for_beat: dict[str, list[Shot]] = {}
    for s in shots:
        for beat_id in s.spine_beat_ids or []:
            bid = str(beat_id).strip().upper()
            if not bid:
                continue
            delivered_ids.add(bid)
            shots_for_beat.setdefault(bid, []).append(s)

    structured_mode = bool(delivered_ids) or any(
        (s.key_line_ids or s.information_ids) for s in shots
    )
    catalog = key_line_catalog(screenplay)
    ledger_by_id = {
        (item.info_id or "").strip().upper(): item
        for item in (screenplay.information_ledger or [])
        if (item.info_id or "").strip()
    }
    visual_text = "".join(spoken_text_of(s) + (s.action_desc or "") for s in shots)
    errors: list[str] = []
    missing_beats: list[str] = []
    legacy_uncertain: list[str] = []
    missing_atoms: list[str] = []
    missing_visual_subjects: list[str] = []
    missing_visible_actions: list[str] = []

    for beat in spine.spine_beats:
        if not beat.must_keep:
            continue
        beat_id = (beat.beat_id or "").strip().upper()
        claim = f"{beat.who}{beat.does}{beat.turn}"
        if beat_id and beat_id in delivered_ids:
            owners = shots_for_beat.get(beat_id) or []
            # 允许相邻镜共同交付：把声明该 spine 的镜号 ±1 一并纳入窗口。
            owner_nos = {s.shot_no for s in owners}
            window = [
                s for s in shots
                if s.shot_no in owner_nos
                or any(abs(s.shot_no - n) == 1 for n in owner_nos)
            ]
            window_spoken = "".join(spoken_text_of(s) for s in window)
            window_visual = window_spoken + "".join((s.action_desc or "") for s in window)
            who_parts = [
                part.strip()
                for part in re.split(r"[、，,和与及/／\s]+", (beat.who or "").strip())
                if part.strip()
            ]
            # “他/她/众人”等功能性概括没有稳定人物名，继续由 does/信息原子校验；
            # 具名动作主体则必须真正进入可见动作文本，不能只靠 S* 编号冒充覆盖。
            generic_who = {"他", "她", "他们", "她们", "众人", "人群", "围观者", "双方", "所有人"}
            for who in who_parts:
                if who in generic_who:
                    continue
                subject_shots = [
                    s for s in window
                    if any(
                        who in visible_name or visible_name in who
                        for visible_name in {
                            str(name).strip()
                            for name in [*(s.characters or []), *(s.characters_visible or [])]
                            if str(name).strip()
                        }
                    )
                ]
                if not subject_shots:
                    missing_visual_subjects.append(f"{beat_id}/{who}")
                    continue
                subject_action_text = "".join(
                    (s.primary_action or "")
                    + (s.action_desc or "")
                    + (s.first_frame_desc or "")
                    + (s.last_frame_desc or "")
                    for s in subject_shots
                )
                if (beat.does or "").strip() and _claim_clearly_absent(beat.does, subject_action_text):
                    missing_visible_actions.append(f"{beat_id}/{who}:{beat.does}")
            for kid in (beat.key_line_ids or []):
                kid_u = str(kid).strip().upper()
                text = catalog.get(kid_u)
                if not text:
                    continue
                core = _strip_speaker(text)
                if (
                    _longest_run_ratio(core, window_spoken) < KEY_LINE_PRESENT_RATIO
                    and _bigram_coverage(core, window_spoken) < KEY_LINE_BIGRAM_COVERAGE
                ):
                    missing_atoms.append(f"{beat_id}/{kid_u}")
            for iid in (beat.information_ids or []):
                iid_u = str(iid).strip().upper()
                item = ledger_by_id.get(iid_u)
                content = (item.content if item else "") or iid_u
                owner = (item.delivery_owner if item else "visual_action") or "visual_action"
                haystack = window_spoken if owner == "spoken_dialogue" else window_visual
                if item and item.exact_text:
                    if (
                        _longest_run_ratio(item.exact_text, haystack) < KEY_LINE_PRESENT_RATIO
                        and _bigram_coverage(item.exact_text, haystack) < KEY_LINE_BIGRAM_COVERAGE
                    ):
                        missing_atoms.append(f"{beat_id}/{iid_u}")
                elif _bigram_coverage(content, haystack) < KEY_POINT_COVERAGE:
                    missing_atoms.append(f"{beat_id}/{iid_u}")
            continue

        # 无 spine_beat_id：结构化模式下硬失败；legacy 模式字面命中放行，否则 uncertain。
        if structured_mode:
            missing_beats.append(f"{beat_id or '?'}:{beat.does}")
            continue
        if _bigram_coverage(claim, visual_text) >= KEY_POINT_COVERAGE:
            continue
        legacy_uncertain.append(f"{beat_id or '?'}:{beat.does}")

    if missing_beats:
        shown = "；".join(missing_beats[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_beats) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_beats) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"分镜未覆盖 {len(missing_beats)} 条 must_keep 主线节拍：{shown}{extra}；"
            "请在对应镜头写入 spine_beat_ids，允许相邻多镜共同交付同一 S*"
        )
    if missing_atoms:
        shown = "；".join(missing_atoms[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_atoms) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_atoms) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"主线节拍缺少必需信息原子/关键台词：{shown}{extra}；"
            "请在声明该 spine_beat_id 的镜头或其相邻镜交付对应 information_id/key_line_id"
        )
    if missing_visual_subjects:
        unique_missing = list(dict.fromkeys(missing_visual_subjects))
        shown = "；".join(unique_missing[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(unique_missing) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(unique_missing) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"主线节拍缺少可见动作主体：{shown}{extra}；"
            "请让 beat.who 在对应镜头或相邻镜的 action_desc/首尾帧中亲自完成主线动作；"
            "宣布结果、路人议论或仅填写 spine_beat_ids 都不能替代动作入画"
        )
    if missing_visible_actions:
        unique_missing = list(dict.fromkeys(missing_visible_actions))
        shown = "；".join(unique_missing[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(unique_missing) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(unique_missing) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"主线节拍动作主体已入画但未完成对应动作：{shown}{extra}；"
            "请在该主体可见的 action_desc/首尾帧中明确拍出 beat.does，"
            "不要用后续走位、静态反应或他人宣布替代"
        )
    if legacy_uncertain:
        shown = "；".join(legacy_uncertain[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(legacy_uncertain) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(legacy_uncertain) > KEY_CONTENT_MAX_REPORT else "")
        msg = (
            f"LEGACY_COVERAGE_UNCERTAIN：{len(legacy_uncertain)} 条 must_keep 主线节拍缺少 "
            f"spine_beat_ids 且字面证据不足：{shown}{extra}；请补 ID 或人工复核，"
            "二元字组匹配不得单独判定 must_keep missing"
        )
        from app.observability.metrics import inc, spine_structured_hard_gate
        inc("spine_coverage_legacy_fallback_total", count=len(legacy_uncertain))
        if spine_structured_hard_gate():
            errors.append(msg)
        # flag 关闭时仅记指标，不阻断确认
    return errors


def validate_storyboard_shot_covers_outline(
    shot: Shot, covers: str, shot_no: int,
    *, prior_text: str = "", later_planned_covers: str = "",
) -> list[str]:
    """逐镜填充阶段校验：大纲声明本镜要落实的 covers，必须真的进入本镜文本。

    这比收尾时才跑整集必保留校验更早发现漏戏，避免模型第 6 镜才被告知第 2 镜漏了"低级"。

    covers 是模型自写的复合事实改写（"测出三段，被宣告低级，引发哄笑"），按句读拆成原子逐条核对。
    判定务实优先、只拦"整件事彻底没拍"：用更宽的"明显缺失"阈值（_claim_clearly_absent），
    某条原子在本镜+前序里几乎零命中才算漏——避免"宣告→宣读"这类同义改写把本已落实的一拍卡死。
    同义词组兜底（_crowd_semantic_hit）覆盖哄笑/议论/嘲讽/追捧赞叹/成绩段位/震惊错愕等高频抽象词，
    模型把"成绩"写成"测出七段"、"追捧"写成"赞叹欢呼"都算落实。报错只点名真正缺失的那条。
    P1 兜底：纯导演抽象（反差/对比）若正文已写出双方可见状态对比，视为已落实，避免修复死循环。
    两类"承接"不算本镜漏戏：
    - 向前承接：该原子已在前序已通过镜头（prior_text）里体现；
    - 向后承接：大纲把同一事实也排给了后续镜头（later_planned_covers），留给后面拍。
    """
    atoms = _atomize_claim(covers)
    if not atoms:
        return []

    # 口播只认有效口播段；source_excerpt / 旁白不得充当「已说出」证据（VAL-422）。
    spoken = spoken_text_of(shot)
    shot_text = (shot.action_desc or "") + spoken
    realized_text = shot_text + (prior_text or "")
    later = later_planned_covers or ""
    missing = [
        atom for atom in atoms
        if _claim_clearly_absent(atom, realized_text)
        and not (later and not _claim_clearly_absent(atom, later))
        # P1 兜底：抽象反差/对比若正文已写出双方可见状态，不再当漏戏硬拦。
        and not _abstract_contrast_realized_in_shot(atom, shot_text)
    ]
    if not missing:
        return []

    shown = "；".join(missing[:KEY_CONTENT_MAX_REPORT])
    extra = (f"（另有 {len(missing) - KEY_CONTENT_MAX_REPORT} 条从略）"
             if len(missing) > KEY_CONTENT_MAX_REPORT else "")
    return [
        f"第 {shot_no} 镜未落实本镜大纲 covers：{shown}{extra}；"
        "请把这些事实或台词明确写进本镜 action_desc 或有效口播（dialogues/audio_timeline），不能只停留在大纲里"
    ]


def key_line_catalog(screenplay: EpisodeScreenplay) -> dict[str, str]:
    """剧本 key_lines 的稳定 ID 映射：按出现顺序 KL01..KLnn。"""
    catalog: dict[str, str] = {}
    for idx, line in enumerate(screenplay.key_lines or [], start=1):
        text = (line or "").strip()
        if not text:
            continue
        catalog[f"KL{idx:02d}"] = text
    return catalog


def outline_key_line_capacity_errors(
    outline: StoryboardOutline, screenplay: EpisodeScreenplay
) -> list[str]:
    """大纲阶段：分配到同一镜的必保留台词字数不得超过该镜最大口播容量。"""
    catalog = key_line_catalog(screenplay)
    if not catalog:
        return []
    errors: list[str] = []
    assigned: set[str] = set()
    for shot in outline.shots or []:
        duration = int(shot.duration_s or config.DEFAULT_VIDEO_DURATION_S)
        capacity = max_speech_chars(duration)
        kids = [str(k).strip().upper() for k in (shot.key_line_ids or []) if str(k).strip()]
        required_chars = 0
        lines_for_msg: list[str] = []
        for kid in kids:
            text = catalog.get(kid)
            if not text:
                errors.append(
                    f"大纲第 {shot.shot_no} 镜 key_line_ids 含未知「{kid}」；"
                    f"合法范围：{', '.join(catalog)}"
                )
                continue
            assigned.add(kid)
            chars = content_char_count(_strip_speaker(text))
            required_chars += chars
            lines_for_msg.append(f"{kid}({chars}字)")
        if required_chars > capacity:
            errors.append(
                f"大纲第 {shot.shot_no} 镜必保留台词约 {required_chars} 字，"
                f"超过 {duration}s 口播上限 {capacity} 字（{', '.join(lines_for_msg)}）；"
                "请拆镜或把部分 key_line_ids 挪到相邻镜头，禁止把不可满足合同交给逐镜修复"
            )
    # 未分配的关键台词：若大纲声明了任何 key_line_ids，则要求全集覆盖
    if any((s.key_line_ids or []) for s in (outline.shots or [])):
        missing = [kid for kid in catalog if kid not in assigned]
        if missing:
            errors.append(
                f"大纲未分配关键台词 ID：{', '.join(missing)}；"
                "请把每条 KL* 写入某一镜的 key_line_ids"
            )
    return errors


def assign_outline_delivery_ids(
    outline: StoryboardOutline, screenplay: EpisodeScreenplay
) -> list[dict]:
    """确定性回填大纲 spine_beat_ids / key_line_ids（LLM 漏填时的安全网）。

    按 covers/beat 与剧本台账的模糊匹配把 KL*/S* 分配到镜头；已有 ID 不覆盖。
    返回变更日志供可观测性。
    """
    changes: list[dict] = []
    catalog = key_line_catalog(screenplay)
    spine = screenplay.plot_spine
    beats = list(spine.spine_beats or []) if spine else []
    assigned_kl: set[str] = set()
    for shot in outline.shots or []:
        for kid in shot.key_line_ids or []:
            assigned_kl.add(str(kid).strip().upper())
    for shot in outline.shots or []:
        plan = ((shot.covers or "") + (shot.beat or "")).strip()
        if not plan:
            continue
        if catalog and not (shot.key_line_ids or []):
            matched: list[str] = []
            for kid, text in catalog.items():
                if kid in assigned_kl:
                    continue
                core = _strip_speaker(text)
                if (
                    _longest_run_ratio(core, plan) >= KEY_LINE_PRESENT_RATIO
                    or _bigram_coverage(core, plan) >= KEY_LINE_BIGRAM_COVERAGE
                ):
                    matched.append(kid)
            if matched:
                shot.key_line_ids = matched
                assigned_kl.update(matched)
                changes.append({"shot_no": shot.shot_no, "key_line_ids": matched})
        if beats and not (shot.spine_beat_ids or []):
            matched_beats: list[str] = []
            for beat in beats:
                bid = (beat.beat_id or "").strip().upper()
                if not bid:
                    continue
                claim = f"{beat.who}{beat.does}{beat.turn}"
                if _bigram_coverage(claim, plan) >= KEY_POINT_COVERAGE:
                    matched_beats.append(bid)
            if matched_beats:
                shot.spine_beat_ids = matched_beats
                changes.append({"shot_no": shot.shot_no, "spine_beat_ids": matched_beats})
    return changes


def outline_key_line_speaker_errors(
    outline: StoryboardOutline, screenplay: EpisodeScreenplay
) -> list[str]:
    """大纲阶段禁止把不同说话人的关键台词塞进同一视频镜头。"""
    catalog = key_line_catalog(screenplay)
    errors: list[str] = []
    for shot in outline.shots or []:
        speaker_order: list[str] = []
        for kid in shot.key_line_ids or []:
            text = catalog.get(str(kid).strip().upper(), "")
            speaker = _speaker_name(text)
            if speaker and speaker not in speaker_order:
                speaker_order.append(speaker)
        if len(speaker_order) > 1:
            errors.append(
                f"大纲第 {shot.shot_no} 镜分配了多个说话人 {speaker_order}；"
                "请按话轮拆成相邻单人近景/特写，使用 reverse_angle 或 reaction_cut 正反打"
            )
    return errors


def _outline_dialogue_two_shot_required(
    shot: Any, speaker: str,
) -> bool:
    """大纲已有明确双人接触动作时，不把直接互动对象错误裁掉。"""
    visible = [str(name).strip() for name in (shot.characters_visible or []) if str(name).strip()]
    if len(visible) != 2 or speaker not in visible:
        return False
    other = next((name for name in visible if name != speaker), "")
    text = "；".join(
        str(value or "")
        for value in (shot.primary_action, shot.beat, shot.covers)
    )
    interaction = re.compile(
        r"搀扶|扶住|抱住|拥抱|握住|抓住|拉住|推开|挡住|托住|"
        r"递给|递出|接过|抢夺|碰杯|亲吻|背起|抱起|交手|对打|扭打"
    )
    return any(
        speaker in clause and other in clause and interaction.search(clause)
        for clause in re.split(r"[，,。；;！？\n]", text)
    )


def split_outline_on_speaker_changes(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    *,
    max_shots: int,
) -> list[dict]:
    """按关键台词说话人变化确定性拆镜，同一人的连续短句可保留在一镜。"""
    from app.schemas import StoryboardOutlineShot

    catalog = key_line_catalog(screenplay)
    if not catalog or not outline.shots:
        return []
    events: list[dict] = []
    index = 0
    while index < len(outline.shots):
        shot = outline.shots[index]
        kids = [
            str(kid).strip().upper()
            for kid in (shot.key_line_ids or [])
            if str(kid).strip().upper() in catalog
        ]
        groups: list[tuple[str, list[str]]] = []
        for kid in kids:
            speaker = _speaker_name(catalog[kid])
            if groups and groups[-1][0] == speaker:
                groups[-1][1].append(kid)
            else:
                groups.append((speaker, [kid]))
        distinct = [speaker for speaker, _ in groups if speaker]
        if len(set(distinct)) <= 1:
            if distinct:
                if not _outline_dialogue_two_shot_required(shot, distinct[0]):
                    shot.characters_visible = [distinct[0]]
                shot.audio_cast = [distinct[0]]
            index += 1
            continue
        needed = len(groups) - 1
        if len(outline.shots) + needed > max_shots:
            index += 1
            continue

        before_count = len(outline.shots)
        original_state_out = shot.state_out
        original_beat = shot.beat
        original_spine = list(shot.spine_beat_ids or [])
        original_information = list(shot.information_ids or [])
        original_event = shot.story_event_id
        original_duration = shot.duration_s or config.DEFAULT_VIDEO_DURATION_S
        previous_state = shot.state_in
        new_shots: list[StoryboardOutlineShot] = []
        for group_index, (speaker, group_kids) in enumerate(groups):
            lines = [catalog[kid] for kid in group_kids]
            covers = "；".join(lines)
            state_out = (
                original_state_out
                if group_index == len(groups) - 1
                else f"{speaker or '当前说话人'}说完本话轮，听者仍留在画外等待反应"
            )
            if group_index == 0:
                target = shot
                keep_two_shot = _outline_dialogue_two_shot_required(target, speaker)
                target.key_line_ids = list(group_kids)
                target.covers = covers
                if not keep_two_shot:
                    target.primary_action = f"{speaker}单人近景说出本话轮"
                target.state_out = state_out
                target.information_ids = (
                    original_information if group_index == len(groups) - 1 else []
                )
                target.new_information_ids = list(target.information_ids)
                if not keep_two_shot:
                    target.characters_visible = [speaker] if speaker else []
                target.audio_cast = [speaker] if speaker else []
                target.continuity_mode = (
                    "same_scene_cut"
                    if target.continuity_mode == "action_continuation"
                    else (target.continuity_mode or "same_scene_cut")
                )
            else:
                target = StoryboardOutlineShot(
                    shot_no=shot.shot_no + group_index,
                    scene_setting=shot.scene_setting,
                    beat=f"（对白正反打）{speaker}承接上一话轮作出回应；{original_beat}",
                    covers=covers,
                    story_event_id=original_event,
                    spine_beat_ids=original_spine,
                    key_line_ids=list(group_kids),
                    information_ids=(original_information if group_index == len(groups) - 1 else []),
                    new_information_ids=(original_information if group_index == len(groups) - 1 else []),
                    state_in=previous_state,
                    primary_action=f"{speaker}单人近景说出本话轮",
                    state_out=state_out,
                    continuity_mode="reverse_angle",
                    duration_s=original_duration,
                    characters_visible=[speaker] if speaker else [],
                    audio_cast=[speaker] if speaker else [],
                )
                new_shots.append(target)
            previous_state = state_out
        for offset, new_shot in enumerate(new_shots, start=1):
            outline.shots.insert(index + offset, new_shot)
        for shot_index, item in enumerate(outline.shots, start=1):
            item.shot_no = shot_index
        events.append({
            "shot_no": index + 1,
            "speakers": distinct,
            "groups": [group_kids for _speaker, group_kids in groups],
            "shots_before": before_count,
            "shots_after": len(outline.shots),
            "reason": "dialogue_speaker_change_requires_reverse_shot",
        })
        index += len(groups)
    return events


_ACTION_CAPACITY_SPLIT_MARKER = "动作容量拆分"


def _outline_action_candidate(shot: Any) -> tuple[str, int, int]:
    """Choose the outline field that exposes the richest action sequence."""
    candidates = [
        str(value or "").strip()
        for value in (shot.primary_action, shot.beat, shot.covers)
        if str(value or "").strip()
    ]
    if not candidates:
        return "", 0, 0
    scored = [
        (text, count_sequential_action_beats(text), len(_atomize_claim(text)))
        for text in candidates
    ]
    return max(scored, key=lambda item: (item[1], item[2], len(item[0])))


def _split_outline_action_text(
    text: str,
    *,
    limit: int,
    force: bool,
) -> tuple[str, str] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    beats = count_sequential_action_beats(raw)
    if beats > limit or force:
        by_verbs = split_sequential_action_text(raw)
        if by_verbs is not None:
            return by_verbs
    atoms = _atomize_claim(raw)
    if len(atoms) < 2:
        return None
    split_at = max(1, len(atoms) // 2)
    return "；".join(atoms[:split_at]), "；".join(atoms[split_at:])


def split_outline_over_action_capacity(
    outline: StoryboardOutline,
    *,
    max_shots: int,
    shot_nos: set[int] | None = None,
    force: bool = False,
) -> list[dict]:
    """Split action-heavy outline nodes before per-shot generation.

    The threshold comes from the same helper used by the paid-video preflight:
    up to two sequential beats for 5-6 seconds and three for 7-10 seconds.  A
    source node is split at most once (``动作容量拆分`` marker), preserving the
    existing event/spine allocation while moving dialogue and newly delivered
    information to the latter half.  ``force`` is used only by the local repair
    router after a detailed shot expands beyond its compact outline wording.
    """
    from app.schemas import StoryboardOutlineShot

    if not outline.shots or len(outline.shots) >= max_shots:
        return []
    restrict_to_targets = shot_nos is not None
    targets = {int(no) for no in (shot_nos or set()) if int(no) > 0}
    events: list[dict] = []
    index = 0
    while index < len(outline.shots) and len(outline.shots) < max_shots:
        if restrict_to_targets and not targets:
            break
        shot = outline.shots[index]
        original_no = int(shot.shot_no)
        if restrict_to_targets and original_no not in targets:
            index += 1
            continue
        if _ACTION_CAPACITY_SPLIT_MARKER in (shot.beat or ""):
            targets.discard(original_no)
            index += 1
            continue
        plan_text, beats, atom_count = _outline_action_candidate(shot)
        limit = action_capacity_limit(shot.duration_s)
        if not force and beats <= limit:
            targets.discard(original_no)
            index += 1
            continue
        plan_parts = _split_outline_action_text(plan_text, limit=limit, force=force)
        if plan_parts is None:
            targets.discard(original_no)
            index += 1
            continue
        front_action, back_action = plan_parts
        if not front_action.strip() or not back_action.strip():
            targets.discard(original_no)
            index += 1
            continue

        cover_parts = _split_outline_action_text(
            shot.covers or "", limit=limit, force=True,
        )
        front_covers, back_covers = (
            cover_parts if cover_parts is not None else ("", shot.covers or "")
        )
        before_count = len(outline.shots)
        original_state_out = shot.state_out
        original_information = list(shot.information_ids or [])
        original_new_information = list(shot.new_information_ids or [])
        original_key_lines = list(shot.key_line_ids or [])
        original_audio_cast = list(shot.audio_cast or [])
        intermediate_state = f"{front_action.rstrip('。')}完成，准备承接后续动作"

        shot.beat = f"（{_ACTION_CAPACITY_SPLIT_MARKER}：前段）{front_action}"
        shot.primary_action = front_action
        shot.covers = front_covers
        shot.state_out = intermediate_state
        shot.key_line_ids = []
        shot.information_ids = []
        shot.new_information_ids = []
        shot.audio_cast = []

        outline.shots.insert(
            index + 1,
            StoryboardOutlineShot(
                shot_no=original_no + 1,
                scene_setting=shot.scene_setting,
                beat=f"（{_ACTION_CAPACITY_SPLIT_MARKER}：后段）{back_action}",
                covers=back_covers,
                story_event_id=shot.story_event_id,
                spine_beat_ids=list(shot.spine_beat_ids or []),
                key_line_ids=original_key_lines,
                information_ids=original_information,
                new_information_ids=original_new_information,
                state_in=intermediate_state,
                primary_action=back_action,
                emotion_beat=shot.emotion_beat,
                state_out=original_state_out,
                continuity_mode="action_continuation",
                duration_s=shot.duration_s or config.DEFAULT_VIDEO_DURATION_S,
                characters_visible=list(shot.characters_visible or []),
                audio_cast=original_audio_cast,
            ),
        )
        for shot_index, item in enumerate(outline.shots, start=1):
            item.shot_no = shot_index
        events.append({
            "shot_no": original_no,
            "estimated_action_beats": beats,
            "action_atoms": atom_count,
            "capacity": limit,
            "front_action": front_action,
            "back_action": back_action,
            "shots_before": before_count,
            "shots_after": len(outline.shots),
            "reason": "sequential_action_beats_exceed_video_capacity",
            "forced": force,
        })
        targets.discard(original_no)
        index += 2
    return events


def split_outline_over_key_line_capacity(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    *,
    max_shots: int,
) -> list[dict]:
    """把超出口播容量的 key_line_ids 拆到相邻镜头（PRD §4.2）。

    在进入逐镜生成前执行；拆分后重排 shot_no。返回每次拆分的遥测记录。
    """
    from app.schemas import StoryboardOutlineShot

    catalog = key_line_catalog(screenplay)
    if not catalog or not outline.shots:
        return []
    events: list[dict] = []
    # 反复拆直到不再超容或触顶；单轮最多拆 len(shots) 次避免死循环。
    for _ in range(max(1, len(outline.shots))):
        if len(outline.shots) >= max_shots:
            break
        overflow_index: int | None = None
        overflow_kids: list[str] = []
        capacity = 0
        required = 0
        for idx, shot in enumerate(outline.shots):
            duration = int(shot.duration_s or config.DEFAULT_VIDEO_DURATION_S)
            capacity = max_speech_chars(duration)
            kids = [str(k).strip().upper() for k in (shot.key_line_ids or []) if str(k).strip()]
            required = sum(
                content_char_count(_strip_speaker(catalog[k]))
                for k in kids if k in catalog
            )
            if required > capacity and len(kids) >= 2:
                overflow_index = idx
                overflow_kids = kids
                break
        if overflow_index is None:
            break
        # 尽量让前半不超过容量：从后往前挪出可移动的 KL*。
        keep: list[str] = []
        move: list[str] = []
        running = 0
        for kid in overflow_kids:
            chars = content_char_count(_strip_speaker(catalog.get(kid, "")))
            if not keep or running + chars <= capacity:
                keep.append(kid)
                running += chars
            else:
                move.append(kid)
        if not move:
            # 单条已超容：仍拆出最后一条，逼迫下游用 adapted_line / 人工处理。
            keep, move = overflow_kids[:-1] or overflow_kids[:1], overflow_kids[-1:]
        current = outline.shots[overflow_index]
        before_count = len(outline.shots)
        current.key_line_ids = keep
        # covers 也按句读拆半，避免新镜 covers 空。
        atoms = _atomize_claim(current.covers or "")
        if len(atoms) >= 2:
            split_at = max(1, len(atoms) // 2)
            front, back = "；".join(atoms[:split_at]), "；".join(atoms[split_at:])
            current.covers = front
        else:
            back = current.covers or "；".join(move)
        insert_at = overflow_index + 1
        outline.shots.insert(
            insert_at,
            StoryboardOutlineShot(
                shot_no=current.shot_no + 1,
                scene_setting=current.scene_setting,
                beat=f"（容量拆分：承接第{current.shot_no}镜关键台词）{back}",
                covers=back,
                key_line_ids=move,
                spine_beat_ids=list(current.spine_beat_ids or []),
                information_ids=list(current.information_ids or []),
                story_event_id=current.story_event_id,
                state_in=current.state_out or current.state_in,
                primary_action=back[:40] or current.primary_action,
                state_out=current.state_out,
                continuity_mode=current.continuity_mode or "same_scene_cut",
                duration_s=current.duration_s or config.DEFAULT_VIDEO_DURATION_S,
                characters_visible=list(current.characters_visible or []),
                audio_cast=list(current.audio_cast or []),
            ),
        )
        for i, s in enumerate(outline.shots):
            s.shot_no = i + 1
        events.append({
            "shot_no": current.shot_no,
            "required_chars": required,
            "capacity": capacity,
            "kept_key_line_ids": keep,
            "moved_key_line_ids": move,
            "shots_before": before_count,
            "shots_after": len(outline.shots),
            "reason": "required_spoken_chars_exceed_max_capacity",
        })
    return events


def validate_storyboard_outline(outline: StoryboardOutline, screenplay: EpisodeScreenplay,
                                target_duration_s: int, *,
                                bible: Bible | None = None) -> list[str]:
    """校验分镜大纲：镜头数在范围内、shot_no 连续、每镜有推进、相邻镜不停留在同一节拍，
    且全集必保留关键台词/剧情点都被分配到某一镜（防止规划阶段就把剧情铺一半、后段漏戏）。

    方案 A：新增 covers 可拍性预检——某镜 covers 若依赖角色圣经外角色开口（被X宣告），或同时要求
    角色开口+人群声（两类声轨叠加必超单镜口播上限），直接在大纲阶段拦下并要求拆成相邻镜头。
    避免逐镜阶段陷入'删角色→covers 落实不了 / 保留角色→characters 校验失败'的死循环（镜03 根因）。
    P0：covers 含纯导演抽象（反差/对比/衬托等）时硬拦，要求改成双方可见状态等可拍事实。
    bible 为空时跳过角色一致性校验（务实优先，旧数据放行）。
    """
    errors: list[str] = []
    shots = outline.shots
    if not shots:
        return ["分镜大纲为空；请按主线骨架规划连续镜头并覆盖 must_keep spine"]
    errors.extend(shot_count_budget_errors(len(shots), context="分镜大纲"))
    actual = [s.shot_no for s in shots]
    if actual != list(range(1, len(shots) + 1)):
        errors.append(f"大纲 shot_no 必须为连续递增 1..{len(shots)}，当前为 {actual}")
    bible_names = {c.name for c in bible.characters} if bible else set()
    for i, s in enumerate(shots):
        if len((s.beat or "").strip()) < 6:
            errors.append(f"大纲第 {i + 1} 镜 beat 过短或缺失；请用一句话写清本镜推进的剧情（谁做了什么/局势如何变化）")
        # 方案 A：covers 可拍性预检
        covers = (s.covers or "").strip()
        if covers:
            outside = _covers_outside_spoken(covers, bible_names)
            if outside:
                errors.append(
                    f"大纲第 {i + 1} 镜 covers 依赖角色圣经外角色「{'/'.join(outside)}」开口宣告；"
                    "请把该角色补入角色圣经，或改由圣经角色完成该宣告，或拆给相邻镜头用 narration 转述，"
                    "不要让逐镜阶段在'删角色→covers 落实不了 / 保留角色→characters 校验失败'之间卡死")
            if _covers_has_spoken(covers) and _covers_has_crowd(covers):
                errors.append(
                    f"大纲第 {i + 1} 镜 covers 同时要求角色开口宣告和人群哄笑议论，两类声轨叠加易超单镜口播上限"
                    f"（最长 {config.VIDEO_DURATION_MAX_S}s 最多 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字）；"
                    "请拆成相邻 2 镜分担：一镜落实宣告，下一镜落实哄笑议论")
            # P0：纯导演抽象不得进 covers（rewrite_outline_abstract_covers 会先剥离；此处拦残余）
            abstract = _covers_abstract_atoms(covers)
            if abstract:
                shown = "；".join(abstract[:3])
                errors.append(
                    f"大纲第 {i + 1} 镜 covers 含导演抽象意图「{shown}」；"
                    "covers 只写可拍/可念/可核对的具体事实（动作、台词、可见反应、信息点），"
                    "反差/对比/衬托/呼应/强调等意图请写入 beat/primary_action/state_out，"
                    "并改成双方可见状态（如「薰儿测出七段、人群赞叹；萧炎低头不语」）")
    # 反停留：相邻两镜的 beat 几乎逐字相同 = 停在同一节拍上空转，必须推进到新剧情。
    for i in range(1, len(shots)):
        if _too_similar(shots[i - 1].beat, shots[i].beat):
            errors.append(
                f"大纲第 {i} 与第 {i + 1} 镜剧情几乎相同（停留在同一节拍）；"
                "每镜必须推进到新的剧情进展，禁止把同一情绪/同一句原文拆成多镜空耗时长")
        repeated_key_lines = sorted(
            set(shots[i - 1].key_line_ids or []).intersection(shots[i].key_line_ids or [])
        )
        if repeated_key_lines:
            errors.append(
                f"大纲第 {i} 与第 {i + 1} 镜重复分配关键台词 "
                f"{repeated_key_lines}；同一句台词只能在一镜完整说出，下一镜应推进到人物反应或新信息"
            )
    # 关键台词/剧情点必须在大纲里被分配到某一镜（beat 或 covers 中体现），否则后段必丢戏。
    plan_text = "".join((s.beat or "") + (s.covers or "") for s in shots)
    key_lines = [ln.strip() for ln in (screenplay.key_lines or []) if ln and ln.strip()]
    key_points = [pt.strip() for pt in (screenplay.key_plot_points or []) if pt and pt.strip()]
    missing_lines = [
        ln for ln in key_lines
        if _longest_run_ratio(_strip_speaker(ln), plan_text) < KEY_LINE_PRESENT_RATIO
        and _bigram_coverage(_strip_speaker(ln), plan_text) < KEY_LINE_BIGRAM_COVERAGE
    ]
    if missing_lines:
        shown = "；".join(missing_lines[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_lines) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"大纲未安排 {len(missing_lines)} 条必保留关键台词：{shown}{extra}；"
            "请把每条关键台词分配到对应镜头的 covers，确保整集都规划进去")
    errors.extend(key_line_order_errors(
        key_lines,
        [(s.beat or "") + (s.covers or "") for s in shots],
        subject="分镜大纲",
    ))
    missing_points = [pt for pt in key_points if _bigram_coverage(pt, plan_text) < KEY_POINT_COVERAGE]
    if missing_points:
        shown = "；".join(missing_points[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_points) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_points) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"大纲未安排 {len(missing_points)} 条主线剧情点：{shown}{extra}；"
            "请把每个剧情点分配到对应镜头的 beat/covers；drop_list 内容禁止安排")
    spine = screenplay.plot_spine
    if spine and spine.drop_list:
        for drop in spine.drop_list:
            d = (drop or "").strip()
            if len(_condense(d)) < 6:
                continue
            if _bigram_coverage(d, plan_text) >= 0.55:
                errors.append(
                    f"大纲安排了 drop_list 内容「{d[:40]}」；已声明不拍的支线不得进入大纲"
                )
                break
    errors.extend(outline_key_line_speaker_errors(outline, screenplay))
    errors.extend(outline_key_line_capacity_errors(outline, screenplay))
    errors.extend(outline_atomic_errors(outline))
    return errors


# ---------- C2 基于完整剧本的分镜校验 ----------

def normalize_continuity(board: Storyboard) -> None:
    """保持旧调用入口；实际连续性归一化由 continuity 模块按 continuity_mode 执行。"""
    for i, shot in enumerate(board.shots):
        prev = board.shots[i - 1] if i > 0 else None
        sync_shot_continuity_fields(shot, prev)
    normalize_board_continuity(board)


def validate_storyboard_continuity_contract(
    board: Storyboard,
    screenplay: EpisodeScreenplay | None = None,
) -> list[str]:
    """PRD 连续性合同校验：状态链、信息台账、单镜动作/口播容量。"""
    errors: list[str] = []
    for shot in board.shots:
        errors.extend(action_capacity_errors(shot))
        errors.extend(speech_capacity_errors(shot))
    errors.extend(state_chain_errors(board))
    errors.extend(information_ledger_errors(board, screenplay))
    return errors


def spoken_char_count(shot) -> int:
    """本镜真实台词纯文字字数（去空白与标点），与单镜口播上限校验同口径。"""
    return spoken_chars_from_shot(shot)


def strip_all_narration(board: Storyboard) -> list[dict]:
    """确定性清空旁白：产品禁止 narration / 内心OS / timeline narration 轨。

    不清空内容翻译成台词（避免发明对白）；信息改由已有 dialogues 或 action_desc 画面承载。
    """
    changes: list[dict] = []
    for shot in board.shots:
        changed = False
        narration = (shot.narration or "").strip()
        if narration:
            shot.narration = ""
            changed = True
            changes.append({"shot_no": shot.shot_no, "cleared_narration": narration[:40]})
        if shot.audio_timeline:
            kept = [item for item in shot.audio_timeline if item.type != "narration"]
            if len(kept) != len(shot.audio_timeline):
                shot.audio_timeline = kept
                changed = True
                if not any(c.get("shot_no") == shot.shot_no and "cleared_narration" in c for c in changes):
                    changes.append({"shot_no": shot.shot_no, "stripped_timeline_narration": True})
            elif changed and not kept:
                shot.audio_timeline = []
        if changed and not (shot.narration or "").strip():
            # 确保字段为规范化空值
            shot.narration = ""
    return changes


def _narration_is_crowd_ambient(narration: str) -> bool:
    """旁白是否是'人群声/环境声'类（哄笑/议论/嘲讽/惊呼…）而非角色内心OS或全知收尾钩。
    这类声音本是环境氛围，不必占用人物口播——可降级成 action_desc 的画面描写，信息仍在画面里。"""
    n = (narration or "").strip()
    if not n:
        return False
    if any(m in n for m in INNER_VOICE_MARKERS):
        return False
    return _covers_has_crowd(n)


def relieve_spoken_overflow(board: Storyboard) -> list[dict]:
    """兼容旧调用：先清空全部旁白，再按口播上限检查（旁白不再参与口播）。"""
    return strip_all_narration(board)


def _retime_coherent_spoken_timeline(shot: Shot) -> bool:
    """Duration normalization must not manufacture an out-of-range timeline.

    A generated candidate may legitimately choose 6~10 seconds and place its
    spoken segments across that interval.  ``prefer_default_shot_durations``
    can subsequently compress the shot to five seconds.  When dialogues and
    timeline still describe the same speech, retiming is an unambiguous
    derived-field repair.  A genuine dialogues/timeline fork remains untouched
    so the spoken-contract gate can report it instead of silently picking a
    side.
    """
    if not shot.audio_timeline:
        return False
    issues = validate_spoken_contract(shot)
    if any(issue.rule_id == RULE_SPOKEN_COHERENCE for issue in issues):
        return False
    spoken = segments_from_timeline(shot)
    if not spoken:
        return False
    shot.audio_timeline = build_timeline_from_segments(shot, spoken)
    return True


def prefer_default_shot_durations(board: Storyboard) -> list[dict]:
    """主线压缩：能 5s 讲完的镜压回 5s；仍需 6~10s 的镜打上 AI 审核标记。"""
    changes: list[dict] = []
    for shot in board.shots:
        spoken = spoken_char_count(shot)
        beats = count_sequential_action_beats(
            (shot.primary_action or shot.action_desc or "").strip()
        )
        tags = list(shot.risk_tags or [])
        if shot_duration_should_prefer_five(spoken_chars=spoken, action_beats=beats):
            duration_changed = int(shot.duration_s or 0) != PREFERRED_SHOT_DURATION_S
            if duration_changed:
                changes.append({
                    "shot_no": shot.shot_no,
                    "from": shot.duration_s,
                    "to": PREFERRED_SHOT_DURATION_S,
                    "reason": "content_fits_5s",
                })
                shot.duration_s = PREFERRED_SHOT_DURATION_S
                if _retime_coherent_spoken_timeline(shot):
                    changes.append({
                        "shot_no": shot.shot_no,
                        "duration_s": shot.duration_s,
                        "reason": "retimed_audio_after_duration_normalization",
                    })
            if DURATION_REVIEW_RISK_TAG in tags:
                tags = [t for t in tags if t != DURATION_REVIEW_RISK_TAG]
                shot.risk_tags = tags
            continue
        if int(shot.duration_s or 0) > PREFERRED_SHOT_DURATION_S:
            if DURATION_REVIEW_RISK_TAG not in tags:
                tags.append(DURATION_REVIEW_RISK_TAG)
                shot.risk_tags = tags
                changes.append({
                    "shot_no": shot.shot_no,
                    "duration_s": shot.duration_s,
                    "reason": "needs_ai_duration_review",
                })
        elif DURATION_REVIEW_RISK_TAG in tags:
            shot.risk_tags = [t for t in tags if t != DURATION_REVIEW_RISK_TAG]
    return changes


def _canonical_bible_name(name: str, bible_names: set[str]) -> str | None:
    """把疑似别名/简称/错字的角色名【唯一】对应到圣经正名；无唯一命中返回 None（按路人剥离）。

    只认包含关系：圣经名是该名子串（"萧炎少爷"→"萧炎"）或该名（≥2字）是圣经名子串（"萧薰"→"萧薰儿"）。
    命中多于一个圣经名（如"萧"同时命中萧炎/萧媚）视为不可判定，返回 None——宁可剥离也不错配。"""
    name = (name or "").strip()
    if not name:
        return None
    hits = {b for b in bible_names if b in name or (len(name) >= 2 and name in b)}
    return next(iter(hits)) if len(hits) == 1 else None


def _rename_shot_character(shot: Shot, old: str, new: str) -> None:
    """把镜头里某个角色名整体改写为圣经正名：characters 之外，dialogues.speaker 与画面文本一并替换，
    避免改了 characters 却漏改 speaker/action_desc 触发其它校验。

    仅当别名不是正名的子串时才替换画面文本——否则 replace('萧薰','萧薰儿') 会把文本里已有的
    '萧薰儿'撑成'萧薰儿儿'。speaker 是精确等值匹配、不受此问题影响，照常改。"""
    if old not in new:
        for field in ("action_desc", "first_frame_desc", "last_frame_desc", "narration"):
            val = getattr(shot, field, None)
            if val:
                setattr(shot, field, val.replace(old, new))
    for d in shot.dialogues:
        if d.speaker == old:
            d.speaker = new


def _offload_extra_character_voice(shot: Shot, name: str) -> None:
    """把被剥离路人（测验员/围观者甲等）的台词移出 dialogues，内容不丢、降级为 action_desc。

    产品禁止旁白：不再转写进 narration。人群声与宣告一律并入 action_desc 画面描写。
    """
    moved = [(d.line or "").strip() for d in shot.dialogues if d.speaker == name and (d.line or "").strip()]
    shot.dialogues = [d for d in shot.dialogues if d.speaker != name]
    if not moved:
        return
    text = "；".join(moved)
    merged = (shot.action_desc or "").rstrip("。； ")
    shot.action_desc = f"{merged}；{name}{text}。" if merged else f"{name}{text}。"


def normalize_offbible_characters(board: Storyboard, bible: Bible | None) -> list[dict]:
    """按角色圣经与功能性路人合同确定性规范镜头角色。

    根因：原文里的测验员/围观者甲等次要在场人物会被模型写进 characters / dialogues.speaker，但它们不在
    角色圣经里 → validate_storyboard 报「角色圣经中不存在」→ 触发整轮修复（实测会与 covers 落实相互
    拉扯成多轮重试）。真正重要的新角色仍必须进入角色圣经；无姓名、无需跨集定妆的功能性路人可以按
    通用身份标签留在镜头中：
    - 能唯一对应到某圣经角色（别名/简称/错字）→ 规范成圣经正名（characters、speaker、画面文本一并替换）；
    - 功能性路人（测验员、路人甲等）→ 保留在 characters；若只作为 dialogue speaker 出现则补入 characters；
    - 其它圣经外名字 → 从 characters 剥离，其台词降级为画面/旁白。
    就地修改 board，返回带分类依据的调整记录供监控与 Harness 留痕。"""
    bible_names = {c.name for c in bible.characters} if bible else set()
    changes: list[dict] = []
    for shot in board.shots:
        kept: list[str] = []
        for name in shot.characters:
            if not bible_names or name in bible_names:
                kept.append(name)
                continue
            canon = _canonical_bible_name(name, bible_names)
            if canon:
                _rename_shot_character(shot, name, canon)
                kept.append(canon)
                changes.append({
                    "shot_no": shot.shot_no,
                    "renamed": f"{name}→{canon}",
                    "mutated": True,
                })
            elif is_functional_extra(name):
                kept.append(name)
                changes.append({
                    "shot_no": shot.shot_no,
                    "allowed_functional_extra": name,
                    "source": "characters",
                    "mutated": False,
                })
            else:
                _offload_extra_character_voice(shot, name)
                changes.append({
                    "shot_no": shot.shot_no,
                    "stripped": name,
                    "mutated": True,
                })
        # 去重保序（规范后可能与既有正名重复）
        seen: set[str] = set()
        shot.characters = [n for n in kept if not (n in seen or seen.add(n))]
        for dialogue in shot.dialogues:
            speaker = (dialogue.speaker or "").strip()
            if speaker and speaker not in shot.characters and is_functional_extra(speaker):
                shot.characters.append(speaker)
                changes.append({
                    "shot_no": shot.shot_no,
                    "allowed_functional_extra": speaker,
                    "source": "dialogue_speaker",
                    "mutated": True,
                })
    return changes


def validate_bible(bible: Bible) -> list[str]:
    errors = []
    # 初始人物谱由 prompt 约束为 ≤8 个；上限放宽到 60，给「按 20 集补录新登场角色」留出增长空间。
    if not 1 <= len(bible.characters) <= 60:
        errors.append(f"characters 数量 {len(bible.characters)}，要求 1~60 个")
    names = [c.name for c in bible.characters]
    if len(names) != len(set(names)):
        errors.append("characters.name 存在重复")
    for i, c in enumerate(bible.characters):
        if not 30 <= len(c.appearance_canonical) <= 80:
            errors.append(f"characters[{i}]({c.name}).appearance_canonical 长度 {len(c.appearance_canonical)} 字，要求 30~80 字")
        for r in c.relationships:
            if r.to not in names:
                errors.append(f"characters[{i}]({c.name}).relationships 指向「{r.to}」不在角色列表中")
    if not 15 <= len(bible.world.visual_style_canonical) <= 60:
        errors.append(f"world.visual_style_canonical 长度 {len(bible.world.visual_style_canonical)} 字，要求 15~60 字")
    return errors


def validate_scene_bible(scenes: list) -> list[str]:
    """场景圣经业务校验（与 validate_bible 同构）：数量 1~40、name 唯一非空、
    scene_canonical 长度 30~80 字（足以稳定定场又不冗长）。"""
    errors: list[str] = []
    if not 1 <= len(scenes) <= 40:
        errors.append(f"scenes 数量 {len(scenes)}，要求 1~40 个")
    names = [(getattr(s, "name", "") or "").strip() for s in scenes]
    if any(not n for n in names):
        errors.append("scenes.name 不能为空")
    if len(names) != len(set(names)):
        errors.append("scenes.name 存在重复")
    for i, s in enumerate(scenes):
        canonical = getattr(s, "scene_canonical", "") or ""
        if not 30 <= len(canonical) <= 80:
            errors.append(f"scenes[{i}]({names[i] or '?'}).scene_canonical 长度 {len(canonical)} 字，要求 30~80 字")
    return errors
