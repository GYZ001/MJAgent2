"""分镜脚本业务校验器 V1~V8（docs/PROMPT_SPEC.md §C）。
错误消息必须具体到字段与数值——修复回路把它们逐条回喂模型（1.0 教训：从不告诉模型哪里错了）。
"""
from __future__ import annotations

import difflib
import math
import re

from app import config
from app.character_policy import is_functional_extra
from app.continuity import (
    normalize_board_continuity,
    state_chain_errors,
    action_capacity_errors,
    speech_capacity_errors,
    information_ledger_errors,
    outline_atomic_errors,
    adaptation_hook_errors,
    sync_shot_continuity_fields,
    count_sequential_action_beats,
    max_speech_chars,
    spoken_chars_from_shot,
)
from app.schemas import (Bible, EpisodeScreenplay, Shot, Storyboard,
                         StoryboardOutline, SHOT_SIZES, CAMERA_MOVES, TRANSITIONS,
                         CONTINUITY_MODES, DELIVERY_OWNERS)


# 字数约束设计原则（2026-06-15 v12：旁白改为「选填且少用」）：
# ① 叙事主力改为【台词 + 可见画面动作】；旁白(narration)默认留空，只在画面与台词都
#    无法传达关键信息时（较大时间跳跃/必要内心独白/隐藏因果）才写一句短旁白。
#    因此取消旁白下限校验、取消「纯画面空镜必须加旁白」的硬性要求。
# ② 旁白仍保留上限校验：若写，必须短到最短 5s 镜头也能念完，避免又退回到旁白堆砌。
# ③ 分镜模块不校验其它字数上限——台词/原文摘录/场景标签/节拍字段只设必要下限。
NARRATION_TARGET_CHARS = 12
NARRATION_HARD_MAX = 14
ACTION_DESC_HARD_MIN = 40           # action_desc 硬下限（校验用）：够把一个动作写清即可
ACTION_DESC_MIN_CHARS = 70          # prompt 目标：把单一动作的起势/过程/收势与人物反应写清
SOURCE_EXCERPT_MIN_CHARS = 8
# 单镜=一个连贯动作（Seedance 实践）。下限 2 仅用于挡“半句话空动作”。
# 注意：不再用“逗号分句数”当快切上限——一个动作写细了天然就有 5~7 个逗号分句，
# 那样会把“描写充分的单一动作”误判成快切，导致模型永远改不对、无限重试。
# 真正的“多镜头/快切”信号是显式的切镜/闪回/分屏词，用 _explicit_cut_markers 精确识别。
VIDEO_SEGMENT_MIN_BEATS = 2
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
    """至少一镜；第二项只是防异常无限循环的技术熔断值，不是产品上限。"""
    return 1, config.STORYBOARD_MAX_SHOTS


def _voiced_shot_count(shots: list[Shot]) -> int:
    return sum(1 for shot in shots if (shot.narration or "").strip() or shot.dialogues)


def _soundtrack_text(shot: Shot) -> str:
    return "".join([shot.narration or "", *(d.line for d in shot.dialogues)])


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
        # V8 画面清晰度：单镜只演一个连贯动作，把它写清即可（不再逼塞多个快切小镜头）。
        if len(shot.action_desc) < ACTION_DESC_HARD_MIN:
            errors.append(
                f"{tag}.action_desc 仅 {len(shot.action_desc)} 字，低于硬下限 {ACTION_DESC_HARD_MIN} 字；"
                f"请按目标 {ACTION_DESC_MIN_CHARS} 字把这一个动作的起势、过程、收势和人物表情/反应写清")
        source_len = len((shot.source_excerpt or "").strip())
        if source_len < SOURCE_EXCERPT_MIN_CHARS:
            errors.append(
                f"{tag}.source_excerpt 仅 {source_len} 字；每个分镜必须带对应小说原文摘录，"
                f"请从本集原文中逐字摘录至少 {SOURCE_EXCERPT_MIN_CHARS} 字作为上游改编证据与审核追溯，不得送入 Seedance")
        beat_count = _action_beat_count(shot.action_desc)
        if beat_count < VIDEO_SEGMENT_MIN_BEATS:
            errors.append(
                f"{tag}.action_desc 只有 {beat_count} 个动作片段，几乎是空动作；"
                "请把这一个连贯动作写出起势与收势（如「她攥紧衣角，眼泪无声砸落」）")
        cut_markers = _explicit_cut_markers(shot.action_desc)
        if cut_markers:
            errors.append(
                f"{tag}.action_desc 出现多镜头/快切标记 {cut_markers}；单镜只拍一个连贯动作，"
                "请删掉切镜/闪回/分屏等跳切，把多余剧情或时间跳跃移入 narration")
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
        errors.extend(speech_capacity_errors(shot))
        # 旁白选填、少用：不再要求每镜必填，也不再禁止纯画面/纯台词镜头；
        # 若写了旁白则保留上限校验，避免重新退回旁白堆砌。
        narration_len = len((shot.narration or "").strip())
        if narration_len > NARRATION_HARD_MAX:
            errors.append(
                f"{tag}.narration 共 {narration_len} 字，超过硬上限 {NARRATION_HARD_MAX} 字——最短镜头配音念不完、读太快观感差；"
                f"旁白请精简到 {NARRATION_TARGET_CHARS} 字以内（一句最关键的推进），或直接留空、改用台词与画面动作承载")
        # 口播总量必须能在视频最长时长内念完（含台词+旁白），否则配音会被截断、音画不同步。
        spoken_chars = spoken_chars_from_shot(shot)
        spoken_limit = max_speech_chars(shot.duration_s)
        if spoken_chars > spoken_limit:
            errors.append(
                f"{tag} 台词+旁白共 {spoken_chars} 字，超过本镜 {shot.duration_s}s 的口播上限 {spoken_limit} 字；"
                f"可在动作仍是同一连续节拍时把 duration_s 调到最多 {config.VIDEO_DURATION_MAX_S}s，"
                "否则请新增或利用相邻镜头分担这段台词，必要时再精简非关键口水话，"
                "不要让一镜塞下念不完的台词")
        # V4 角色合法性
        if not shot.characters:
            errors.append(
                f"{tag}.characters 为空；每个视频段至少包含 1 个画面角色，"
                "可以是角色圣经成员或功能性路人"
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
        mode = (shot.continuity_mode or "").strip()
        if not mode:
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
    errors.extend(validate_storyboard_scenes(board, bible))
    errors.extend(state_chain_errors(board))

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
INNER_VOICE_MARKERS = ("内心", "心声", "OS", "os", "独白")


# ---------- 关键内容（必保留清单）模糊匹配工具 ----------
# 防丢失校验的共用底座：剧本台/分镜台都要判断"某条关键台词/剧情点是否仍真实存在于文本里"。
# 务实优先（本次定调）：只拦【明显丢失】，用模糊匹配容忍口语化改写/标点差异，绝不逐字比对，
# 避免像历史 false-positive 那样空耗修复轮次。
_SPEAKER_PREFIX_RE = re.compile(r"^([^\n：（(:]{1,16})(?:（[^）]{0,12}）)?[：:]")
_NON_CONTENT_RE = re.compile(r"""[\s，。、；;：:！!？?“”"'‘’（）()【】\[\]《》〈〉—…·.,~\-]+""")
# 关键台词主干连续保留过半即视为"仍在"（容忍前后改写，只要核心句仍出现）。
KEY_LINE_PRESENT_RATIO = 0.4
KEY_LINE_BIGRAM_COVERAGE = 0.42
# 关键剧情点是描述而非逐字，故用 2-gram 覆盖率判定："过三分之一被涵盖"即视为"已落实"。
KEY_POINT_COVERAGE = 0.34
KEY_CONTENT_MAX_REPORT = 4       # 单条错误最多点名几条，避免错误列表过长把 prompt 撑爆
MIN_KEY_LINES = 3                # 必保留关键台词下限（漫剧基本都有对白，floor=3 不易误伤）
MIN_KEY_PLOT_POINTS = 3          # 必保留关键剧情点下限


def _strip_speaker(line: str) -> str:
    """去掉"角色名（情绪）："前缀，取台词正文本身用于匹配。"""
    return _SPEAKER_PREFIX_RE.sub("", (line or "").strip(), count=1).strip()


def _speaker_name(line: str) -> str | None:
    """提取 key_lines / 对白中的说话人姓名（不含情绪括号）。"""
    m = _SPEAKER_PREFIX_RE.match((line or "").strip())
    if not m:
        return None
    return m.group(1).strip() or None


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


def _condense(text: str) -> str:
    """压成纯内容字符串（去空白与标点），让匹配对标点/排版差异稳健。"""
    return _NON_CONTENT_RE.sub("", text or "")


def _longest_run_ratio(needle: str, haystack: str) -> float:
    """needle 核心字符在 haystack 中的最长连续公共块长度 ÷ needle 长度。
    用于判断"一句关键台词是否大体保留"：只要主干连续出现就算保留。"""
    n, h = _condense(needle), _condense(haystack)
    if not n:
        return 1.0
    if n in h:
        return 1.0
    block = difflib.SequenceMatcher(None, n, h).find_longest_match(0, len(n), 0, len(h))
    return block.size / len(n)


def _bigram_set(text: str) -> set[str]:
    c = _condense(text)
    if len(c) < 2:
        return {c} if c else set()
    return {c[i:i + 2] for i in range(len(c) - 1)}


def _bigram_coverage(needle: str, haystack: str) -> float:
    """needle 的 2-gram 有多大比例出现在 haystack 里。
    用于判断"一条关键剧情点是否被涵盖"（剧情点是描述、非逐字，用覆盖率而非连续块）。"""
    nb = _bigram_set(needle)
    if not nb:
        return 1.0
    return len(nb & _bigram_set(haystack)) / len(nb)


# 句读分隔：把一条复合的关键内容切成原子。纯标点驱动、与具体剧情无关，适用任意题材。
_CLAIM_SPLIT_RE = re.compile(r"[；;。.！!？?，,、\n]+")


def _atomize_claim(text: str) -> list[str]:
    """把一条可能复合的关键内容（一句里含多个事实/动作/关系变化）按句读切成原子 claim。

    复合 covers/剧情点如"测出三段，被宣告低级，引发哄笑"应逐条核对，避免"漏掉其中一件事"
    时整句一起判失败、报错也指不到具体缺哪条。过短碎片（连接词等）丢弃，避免噪声。
    """
    atoms: list[str] = []
    seen: set[str] = set()
    for piece in _CLAIM_SPLIT_RE.split(text or ""):
        atom = _strip_speaker(piece).strip()
        key = _condense(atom)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        atoms.append(atom)
    return atoms


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

def _covers_has_spoken(covers: str) -> bool:
    return any(v in covers for v in COVERS_SPOKEN_VERBS)


def _covers_has_crowd(covers: str) -> bool:
    return any(w in covers for w in COVERS_CROWD_WORDS)


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
    return [{"shot_no": 1, "deferred_to": 2, "covers": moved[:80]}]


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


def validate_screenplay(script: EpisodeScreenplay, bible: Bible, expected_beats: int,
                        episode_no: int | None = None, source_text: str | None = None) -> list[str]:
    """剧本层校验：剧本台只接受完整剧本格式，不再兼容旧拍卡结构。"""
    errors: list[str] = []
    if episode_no is not None and script.episode_no != episode_no:
        errors.append(f"episode_no={script.episode_no}，必须等于 {episode_no}")
    if (script.mode or "full_script") != "full_script":
        errors.append(f"mode=「{script.mode}」非法；剧本台仅支持 full_script")
    if len((script.title or "").strip()) < 2:
        errors.append("title 过短或缺失；请填写本集标题")
    if len((script.logline or "").strip()) < 8:
        errors.append("logline 过短或缺失；请用一句话概括本集核心事件")
    if len((script.script_format_note or "").strip()) < 6:
        errors.append("script_format_note 过短或缺失；请说明正文采用的台本格式")
    scenes = script.scene_outline or []
    if not 3 <= len(scenes) <= 6:
        errors.append(f"scene_outline 场次数量为 {len(scenes)}；生产级剧本稿需提供 3~6 场连续场次结构")
    bible_names = {c.name for c in bible.characters}
    for i, scene in enumerate(scenes, start=1):
        # 报错锚点用 1-based 场序（模型按 scene_no 思考），并附场标，避免之前 0-based 索引让模型改错场而陷入打回循环。
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
    full_text = (script.full_script_text or "").strip()
    min_script_chars = max(220, expected_beats * 55)
    if len(full_text) < min_script_chars:
        errors.append(f"full_script_text 过短；当前仅 {len(full_text)} 字，至少需要 {min_script_chars} 字的生产级剧本正文")
    for term in FULL_SCRIPT_FORBIDDEN_TERMS:
        if term in full_text:
            errors.append(f"full_script_text 含禁用词「{term}」；剧本台正文不能写拍卡/分镜/执行语言")
    heading_matches = SCRIPT_SCENE_HEADING_RE.findall(full_text)
    if len(heading_matches) < 3:
        errors.append("full_script_text 缺少足够的场次标题；请使用“【场1】...”这类场次化台本格式")
    elif scenes and len(heading_matches) != len(scenes):
        errors.append(f"full_script_text 场次标题数 {len(heading_matches)} 与 scene_outline 场次数 {len(scenes)} 不一致")
    # 段落充分性：旧实现按「空行分隔的段落块」(re.split \n\s*\n) 计数、要求 ≥6 块。但模型通常只在
    # 【场次之间】空行、场次内各动作/对白行用单换行分隔——这是合规台本写法（prompt 要求“分行”不是“空行”），
    # 于是 3~5 场的合格台本只被算成 3~5 块、误判“段落过少”，白白耗尽修复轮次（实测此项是剧本台首位失败原因）。
    # 改为按【非空文本行】计数（任意换行都算分行）：既贴合 prompt 的“分行书写”，又能稳定区分真正挤成一段的梗概块
    # （梗概只有 1~3 行，台本有几十行）。门槛随场次数缩放，避免少场次时门槛偏高。
    content_lines = [ln for ln in full_text.splitlines() if ln.strip()]
    min_lines = max(6, len(scenes) * 2)
    if len(content_lines) < min_lines:
        errors.append("full_script_text 段落过少；请按场次标题、动作段、对白段分行书写，不要挤成一段梗概")
    dialogue_lines = SCRIPT_DIALOGUE_LINE_RE.findall(full_text)
    if len(dialogue_lines) < 2:
        errors.append("full_script_text 对白行过少；请按“角色名：台词”写出真正可演的对白")
    if bible_names:
        offbible_speakers = sorted({
            match.group(1).strip()
            for match in SCRIPT_SOUND_LINE_RE.finditer(full_text)
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
    # 单集戏剧契约（调研文档 §3.4/§3.5）：方向性信息必须显式存在，避免压缩到 50s 时被一起丢掉。
    if len((script.dramatic_question or "").strip()) < 6:
        errors.append("dramatic_question 过短或缺失；请用一句话写出本集观众心里追问的戏剧问题")
    if len((script.protagonist_goal or "").strip()) < 4:
        errors.append("protagonist_goal 过短或缺失；请写本集主角看得见、可完成的外在目标")
    if len((script.obstacle or "").strip()) < 4:
        errors.append("obstacle 过短或缺失；请写本集阻力（外部对手/规则 + 内部恐惧/执念）")
    if len((script.stakes or "").strip()) < 4:
        errors.append("stakes 过短或缺失；请写失败代价（输了会失去什么关系/尊严/目标）")
    # 必保留清单（防丢失核心）：先卡数量，再确认每条关键台词【真的写进了正文】，否则清单形同虚设。
    key_lines = [ln.strip() for ln in (script.key_lines or []) if ln and ln.strip()]
    if len(key_lines) < MIN_KEY_LINES:
        errors.append(
            f"key_lines 仅 {len(key_lines)} 条；请从原文挑出本集至少 {MIN_KEY_LINES} 条绝不能丢的关键台词"
            "（金句/决定性对白/情绪爆点），尽量保留人物说话风格")
    # key_lines 只能含人物谱角色台词：测验员/围观者等非圣经角色台词会让分镜陷入死循环
    # （分镜 characters 不允许填这些角色，但 key_lines 又要求逐条覆盖）。
    bible_names = {c.name for c in bible.characters}
    if bible_names:
        non_bible_key_lines = []
        for ln in key_lines:
            speaker = _speaker_name(ln)
            if not speaker:
                continue
            if speaker not in bible_names:
                non_bible_key_lines.append(ln)
        if non_bible_key_lines:
            shown = "；".join(non_bible_key_lines[:KEY_CONTENT_MAX_REPORT])
            extra = (f"（另有 {len(non_bible_key_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                     if len(non_bible_key_lines) > KEY_CONTENT_MAX_REPORT else "")
            errors.append(
                f"key_lines 有 {len(non_bible_key_lines)} 条含非人物谱角色台词：{shown}{extra}"
                f"；key_lines 只能保留角色圣经角色（{'、'.join(sorted(bible_names))}）的台词，"
                "功能性路人/旁白台词可写进 full_script_text 但不得进入 key_lines；"
                "其他具名角色必须先补进人物谱")
    missing_in_script = [
        ln for ln in key_lines
        if _longest_run_ratio(_strip_speaker(ln), full_text) < KEY_LINE_PRESENT_RATIO
        and _bigram_coverage(_strip_speaker(ln), full_text) < KEY_LINE_BIGRAM_COVERAGE]
    if missing_in_script:
        shown = "；".join(missing_in_script[:KEY_CONTENT_MAX_REPORT])
        errors.append(
            f"key_lines 有 {len(missing_in_script)} 条未真正写进 full_script_text：{shown}"
            "；关键台词必须在剧本正文里实际出现，不能只列在清单里")
    # key_lines 的 speaker 必须与 full_script_text 中对白行的 speaker 一致：
    # 防止"清单写张三说、正文却写李四说"的归属错位（去 speaker 后正文匹配通过，但归属已错）。
    if bible_names:
        script_speakers: dict[str, list[str]] = {}
        for sm in SCRIPT_SOUND_LINE_RE.finditer(full_text):
            sp = sm.group(1).strip()
            line_text = sm.group(3).strip()
            script_speakers.setdefault(sp, []).append(line_text)
        mismatched = []
        for ln in key_lines:
            kl_speaker = _speaker_name(ln)
            if not kl_speaker or kl_speaker not in bible_names:
                continue  # 非圣经角色已由上一条校验拦截
            kl_text = _strip_speaker(ln)
            # 在 full_script_text 中找台词主干能匹配的对白行，检查其 speaker 是否与 key_lines 一致
            matched_speakers = {
                sp for sp, texts in script_speakers.items()
                if any(_longest_run_ratio(kl_text, t) >= KEY_LINE_PRESENT_RATIO
                       or _bigram_coverage(kl_text, t) >= KEY_LINE_BIGRAM_COVERAGE
                       for t in texts)
            }
            if matched_speakers and kl_speaker not in matched_speakers:
                mismatched.append(f"{ln}（正文归属为：{'、'.join(sorted(matched_speakers))}）")
        if mismatched:
            shown = "；".join(mismatched[:KEY_CONTENT_MAX_REPORT])
            extra = (f"（另有 {len(mismatched) - KEY_CONTENT_MAX_REPORT} 条从略）"
                     if len(mismatched) > KEY_CONTENT_MAX_REPORT else "")
            errors.append(
                f"key_lines 有 {len(mismatched)} 条台词的说话人与 full_script_text 不一致：{shown}{extra}"
                "；同一句台词在 key_lines 和 full_script_text 中必须由同一角色说出")
    source_dialogues = _source_bible_dialogues(source_text, bible)
    if source_dialogues:
        key_text = "\n".join(key_lines)
        missing_from_key_lines = [
            ln for ln in source_dialogues
            if _longest_run_ratio(_strip_speaker(ln), key_text) < KEY_LINE_PRESENT_RATIO
            and _bigram_coverage(_strip_speaker(ln), key_text) < KEY_LINE_BIGRAM_COVERAGE
        ]
        if missing_from_key_lines:
            shown = "；".join(missing_from_key_lines[:KEY_CONTENT_MAX_REPORT])
            extra = (f"（另有 {len(missing_from_key_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                     if len(missing_from_key_lines) > KEY_CONTENT_MAX_REPORT else "")
            errors.append(
                f"key_lines 漏掉了 {len(missing_from_key_lines)} 条人物谱角色在原文中的台词：{shown}{extra}"
                "；剧本台必须保留本集所有人物谱角色台词，不能只筛选重点台词")
        missing_source_dialogues = [
            ln for ln in source_dialogues
            if _longest_run_ratio(_strip_speaker(ln), full_text) < KEY_LINE_PRESENT_RATIO
            and _bigram_coverage(_strip_speaker(ln), full_text) < KEY_LINE_BIGRAM_COVERAGE
        ]
        if missing_source_dialogues:
            shown = "；".join(missing_source_dialogues[:KEY_CONTENT_MAX_REPORT])
            extra = (f"（另有 {len(missing_source_dialogues) - KEY_CONTENT_MAX_REPORT} 条从略）"
                     if len(missing_source_dialogues) > KEY_CONTENT_MAX_REPORT else "")
            errors.append(
                f"full_script_text 漏掉了 {len(missing_source_dialogues)} 条人物谱角色在原文中的台词：{shown}{extra}"
                "；请将这些台词按可拍台本格式写回正文，可轻微口语化但主干不能丢")
    key_points = [pt.strip() for pt in (script.key_plot_points or []) if pt and pt.strip()]
    if len(key_points) < MIN_KEY_PLOT_POINTS:
        errors.append(
            f"key_plot_points 仅 {len(key_points)} 条；请列出本集至少 {MIN_KEY_PLOT_POINTS} 条绝不能丢的关键剧情点"
            "（核心事件/反转/信息揭示）")
    if script.beats:
        errors.append("剧本台不再接受 beats 拍卡结构；请重新生成完整剧本")
    event_ids: set[str] = set()
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
    info_ids: set[str] = set()
    for i, item in enumerate(script.information_ledger or []):
        tag = f"information_ledger[{i}]"
        info_id = (item.info_id or "").strip()
        if not info_id:
            errors.append(f"{tag}.info_id 不能为空")
        elif info_id in info_ids:
            errors.append(f"{tag}.info_id=「{info_id}」重复；information_ledger.info_id 必须唯一")
        else:
            info_ids.add(info_id)
        if item.delivery_owner and item.delivery_owner not in DELIVERY_OWNERS:
            errors.append(f"信息 {item.info_id} 的 delivery_owner={item.delivery_owner} 不合法")
    errors.extend(adaptation_hook_errors(script))
    return list(dict.fromkeys(errors))


def _screenplay_sound_stats(script: EpisodeScreenplay) -> dict[str, int]:
    full_text = (script.full_script_text or "").strip()
    stats = {"dialogues": 0, "inner": 0, "narration": 0, "quoted_voice": 0}
    for match in SCRIPT_SOUND_LINE_RE.finditer(full_text):
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

    expected_shots = max(1, len(shots))
    script_dialogue_targets = stats["dialogues"] + stats["quoted_voice"]
    if script_dialogue_targets >= 2:
        dialogue_count = sum(len(shot.dialogues) for shot in shots)
        min_dialogues = min(script_dialogue_targets, max(2, math.ceil(expected_shots * 0.5)))
        if dialogue_count < min_dialogues:
            errors.append(
                f"分镜对白不足：完整剧本至少有 {script_dialogue_targets} 处角色开口/人声信息，"
                f"但分镜 dialogues 只有 {dialogue_count} 句；请把关键对白写入 dialogues，"
                "非角色圣经里的群嘲/恭维声可改写到 narration 或 action_desc")

    if stats["inner"] > 0:
        soundtrack = "".join(_soundtrack_text(shot) for shot in shots)
        if not any(marker in soundtrack for marker in INNER_VOICE_MARKERS):
            errors.append(
                f"完整剧本含 {stats['inner']} 处内心OS，但分镜未保留任何内心声轨；"
                "请在对应镜头 narration 中写“内心OS：……”或“内心：……”，"
                "把主角无法说出口的屈辱、怀疑或决心保留下来")
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
    if not key_lines and not key_points:
        return errors

    # 关键台词优先在声轨（台词+旁白）里找，找不到再退到画面动作/原文摘录里兜底（动作里复述也算保留）。
    spoken_text = "".join(_soundtrack_text(s) for s in shots)
    all_text = spoken_text + "".join((s.action_desc or "") + (s.source_excerpt or "") for s in shots)

    missing_lines = []
    for ln in key_lines:
        core = _strip_speaker(ln)
        if (
            _longest_run_ratio(core, spoken_text) < KEY_LINE_PRESENT_RATIO
            and _longest_run_ratio(core, all_text) < KEY_LINE_PRESENT_RATIO
            and _bigram_coverage(core, all_text) < KEY_LINE_BIGRAM_COVERAGE
        ):
            missing_lines.append(ln)
    if missing_lines:
        shown = "；".join(missing_lines[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_lines) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"分镜丢失了剧本标记的 {len(missing_lines)} 条关键台词：{shown}{extra}；"
            "请把它们写进对应镜头的 dialogues（人物开口）或 narration（内心OS/结尾旁白），不要在压缩中丢弃")

    missing_points = [pt for pt in key_points if _bigram_coverage(pt, all_text) < KEY_POINT_COVERAGE]
    if missing_points:
        shown = "；".join(missing_points[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_points) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_points) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"分镜丢失了剧本标记的 {len(missing_points)} 条关键剧情点：{shown}{extra}；"
            "请在对应镜头的 action_desc 或声轨中体现这些剧情，不能整段略过")
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
    两类"承接"不算本镜漏戏：
    - 向前承接：该原子已在前序已通过镜头（prior_text）里体现；
    - 向后承接：大纲把同一事实也排给了后续镜头（later_planned_covers），留给后面拍。
    """
    atoms = _atomize_claim(covers)
    if not atoms:
        return []

    shot_text = (
        (shot.action_desc or "")
        + (shot.narration or "")
        + "".join(d.line for d in shot.dialogues)
    )
    realized_text = shot_text + (prior_text or "")
    later = later_planned_covers or ""
    missing = [
        atom for atom in atoms
        if _claim_clearly_absent(atom, realized_text)
        and not (later and not _claim_clearly_absent(atom, later))
    ]
    if not missing:
        return []

    shown = "；".join(missing[:KEY_CONTENT_MAX_REPORT])
    extra = (f"（另有 {len(missing) - KEY_CONTENT_MAX_REPORT} 条从略）"
             if len(missing) > KEY_CONTENT_MAX_REPORT else "")
    return [
        f"第 {shot_no} 镜未落实本镜大纲 covers：{shown}{extra}；"
        "请把这些事实或台词明确写进本镜 action_desc、narration 或 dialogues，不能只停留在大纲里"
    ]


def validate_storyboard_outline(outline: StoryboardOutline, screenplay: EpisodeScreenplay,
                                target_duration_s: int, *,
                                bible: Bible | None = None) -> list[str]:
    """校验分镜大纲：镜头数在范围内、shot_no 连续、每镜有推进、相邻镜不停留在同一节拍，
    且全集必保留关键台词/剧情点都被分配到某一镜（防止规划阶段就把剧情铺一半、后段漏戏）。

    方案 A：新增 covers 可拍性预检——某镜 covers 若依赖角色圣经外角色开口（被X宣告），或同时要求
    角色开口+人群声（两类声轨叠加必超单镜口播上限），直接在大纲阶段拦下并要求拆成相邻镜头。
    避免逐镜阶段陷入'删角色→covers 落实不了 / 保留角色→characters 校验失败'的死循环（镜03 根因）。
    bible 为空时跳过角色一致性校验（务实优先，旧数据放行）。
    """
    errors: list[str] = []
    shots = outline.shots
    if not shots:
        return ["分镜大纲为空；请按完整剧本规划连续镜头并从头到尾覆盖剧情"]
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
    # 反停留：相邻两镜的 beat 几乎逐字相同 = 停在同一节拍上空转，必须推进到新剧情。
    for i in range(1, len(shots)):
        if _too_similar(shots[i - 1].beat, shots[i].beat):
            errors.append(
                f"大纲第 {i} 与第 {i + 1} 镜剧情几乎相同（停留在同一节拍）；"
                "每镜必须推进到新的剧情进展，禁止把同一情绪/同一句原文拆成多镜空耗时长")
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
    missing_points = [pt for pt in key_points if _bigram_coverage(pt, plan_text) < KEY_POINT_COVERAGE]
    if missing_points:
        shown = "；".join(missing_points[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(missing_points) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(missing_points) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"大纲未安排 {len(missing_points)} 条必保留关键剧情点：{shown}{extra}；"
            "请把每个剧情点分配到对应镜头的 beat/covers，确保后段不漏戏")
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
    """本镜台词+旁白的纯发声字数（去空白），与单镜口播上限校验同口径。"""
    return spoken_chars_from_shot(shot)


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
    """确定性卸载单镜口播超限：把'人群议论/哄笑/惊呼'类旁白降级为 action_desc 画面描写，
    使本镜台词+旁白回落到口播上限内（治本届 shots[*] 台词+旁白共 N 字超限报错的最常见来源）。

    人群声本是环境氛围、不必让旁白嗓音念出来；降级后它仍留在 action_desc——画面里看得见，
    且防丢失校验的 all_text 仍涵盖它，关键剧情点不会因此判丢。
    只动'人群声旁白'：绝不删角色台词、不删内心OS、不删全知收尾钩旁白；
    这些情况它改不动就原样返回，交给 ① 大纲拆分或逐镜重试处理。返回实际调整供日志。"""
    changes: list[dict] = []
    for shot in board.shots:
        limit = config.max_spoken_chars_for_duration(shot.duration_s)
        if spoken_char_count(shot) <= limit:
            continue
        narration = (shot.narration or "").strip()
        if narration and _narration_is_crowd_ambient(narration):
            merged = (shot.action_desc or "").rstrip("。； ")
            addition = narration.rstrip("。；")
            shot.action_desc = f"{merged}；{addition}。" if merged else f"{addition}。"
            shot.narration = ""
            changes.append({"shot_no": shot.shot_no, "demoted_crowd_narration": addition[:40]})
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
    """把被剥离路人（测验员/围观者甲等）的台词移出 dialogues，内容不丢、降级承载：
    人群声类（哄笑/议论/嘲讽…）并入 action_desc 当画面群像（与 relieve_spoken_overflow 同口径）；
    其余（如宣告）转写进 narration 保持可听见，但仅在不超单镜口播上限时；否则同样并入 action_desc，
    避免反而触发口播超限的新一轮重试。其在 action_desc 等画面文本中的提及保留原样（在场群像，合法）。"""
    moved = [(d.line or "").strip() for d in shot.dialogues if d.speaker == name and (d.line or "").strip()]
    shot.dialogues = [d for d in shot.dialogues if d.speaker != name]
    if not moved:
        return
    text = "；".join(moved)

    def _into_action() -> None:
        merged = (shot.action_desc or "").rstrip("。； ")
        shot.action_desc = f"{merged}；{name}{text}。" if merged else f"{name}{text}。"

    if _covers_has_crowd(text):
        _into_action()
        return
    existing = (shot.narration or "").strip()
    candidate = f"{existing}；{name}：{text}" if existing else f"{name}：{text}"
    spoken_after = len(re.sub(r"\s+", "", candidate + "".join(d.line for d in shot.dialogues)))
    if spoken_after <= config.max_spoken_chars_for_duration(shot.duration_s):
        shot.narration = candidate
    else:
        _into_action()


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
