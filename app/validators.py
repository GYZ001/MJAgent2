"""分镜脚本业务校验器 V1~V8（docs/PROMPT_SPEC.md §C）。
错误消息必须具体到字段与数值——修复回路把它们逐条回喂模型（1.0 教训：从不告诉模型哪里错了）。
"""
from __future__ import annotations

import difflib
import math
import re

from app import config
from app.schemas import (Bible, EpisodeScreenplay, Shot, Storyboard,
                         StoryboardOutline, SHOT_SIZES, CAMERA_MOVES, TRANSITIONS)


# 字数约束设计原则（2026-06-15 v12：旁白改为「选填且少用」）：
# ① 叙事主力改为【台词 + 可见画面动作】；旁白(narration)默认留空，只在画面与台词都
#    无法传达关键信息时（较大时间跳跃/必要内心独白/隐藏因果）才写一句短旁白。
#    因此取消旁白下限校验、取消「纯画面空镜必须加旁白」的硬性要求。
# ② 旁白仍保留上限校验：若写，必须短到 10s 念得完，避免又退回到旁白堆砌。
# ③ 分镜模块不校验其它字数上限——台词/原文摘录/场景标签/节拍字段只设必要下限。
ORAL_TARGET_RANGE = (35, 55)        # 口播目标（prompt 用，仅引导）：旁白+台词总量，10s 念得完
NARRATION_TARGET_CHARS = 40         # 旁白目标上限（prompt 用）：若写则一句短旁白，10s 念得完
NARRATION_TARGET_MIN_CHARS = 30     # 旧目标下限（保留供引用，旁白已改为选填）
NARRATION_HARD_MAX = 52             # 旁白硬上限（校验用）：若写则 10s 配音念得完，目标 40 +30% 容差
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

# 分镜镜头数不再与 target/10 死锁。target/10 是基础节拍数；当关键台词/内心OS导致单镜口播超限时，
# 允许额外拆出少量镜头承接同一剧情，避免模型在“不能加镜头”和“不能超口播”之间反复失败。
EXTRA_SPLIT_SHOTS = 2
TARGET_DURATION_OVERAGE_RATIO = 1.2

SCENE_CUT_TRANSITIONS = TRANSITIONS - {"硬切"}
EMOTIONAL_TRANSITIONS = {"叠化", "淡出淡入", "声音延续+叠化", "声音先行+淡入"}
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
    """根据换场关系给一个稳定默认值；具体创作仍允许模型/人工选择更贴合的转场。"""
    if not prev:
        return "硬切"
    shared_chars = set(prev.characters) & set(shot.characters)
    text = f"{prev.narration or ''}{shot.narration or ''}{prev.action_desc}{shot.action_desc}"
    if shared_chars and any(k in text for k in ("回忆", "想起", "余音", "话音", "怔住", "眼眶", "沉默", "失神")):
        return "声音延续+叠化"
    if any(k in text for k in ("冲", "追", "逃", "奔", "扑", "甩")):
        return "甩镜"
    if any(k in text for k in ("惊", "爆", "强光", "刺眼", "斗气", "火光")):
        return "闪白"
    return "淡出淡入"


def _text_budget(shot: Shot) -> int:
    total = len(shot.narration or "")
    for d in shot.dialogues:
        total += len(d.line)
    return total


def storyboard_shot_count_range(target_duration_s: int) -> tuple[int, int]:
    """返回自动分镜允许的镜头数范围。

    下限仍是目标时长折算的基础节拍数；上限只放开少量额外拆分镜，用来承接口播过长、
    必保留台词/剧情点过密的情况，不让模型把一集拆成过碎的流水账。
    """
    base = max(1, math.ceil(target_duration_s / config.FIXED_VIDEO_DURATION_S))
    return base, base + EXTRA_SPLIT_SHOTS


def storyboard_duration_limit(target_duration_s: int, board: Storyboard | None = None) -> int:
    """自动分镜允许的整集总时长上限。

    target_duration_s 是节奏目标，不是硬封顶；产品侧允许单集到 EPISODE_TARGET_MAX_S（当前 90s）。
    口播刚需可能进一步抬高下限，避免"为卡目标时长而截短台词"。
    """
    enforced_floor_total = 0
    if board is not None:
        enforced_floor_total = sum(enforced_min_duration(board, s) for s in board.shots)
    return max(
        int(target_duration_s * TARGET_DURATION_OVERAGE_RATIO),
        config.EPISODE_TARGET_MAX_S,
        enforced_floor_total,
    )


def _voiced_shot_count(shots: list[Shot]) -> int:
    return sum(1 for shot in shots if (shot.narration or "").strip() or shot.dialogues)


def _soundtrack_text(shot: Shot) -> str:
    return "".join([shot.narration or "", *(d.line for d in shot.dialogues)])


def _action_beat_count(text: str) -> int:
    parts = [p.strip() for p in re.split(r"[，。；;、\n]+", text) if len(p.strip()) >= 4]
    return len(parts)


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
    *,
    enforce_total_duration: bool = True,
) -> list[str]:
    errors: list[str] = []
    shots = board.shots
    if not shots:
        min_shots, max_shots = storyboard_shot_count_range(target_duration_s)
        return [
            f"shots 为空；目标 {target_duration_s}s 至少需要 {min_shots} 个基础镜头；"
            f"遇到口播/关键内容过密可拆到最多 {max_shots} 个镜头"
        ]

    bible_names = {c.name for c in bible.characters}

    fixed_duration = config.FIXED_VIDEO_DURATION_S
    min_dur, max_dur = config.MIN_VIDEO_DURATION_S, config.MAX_VIDEO_DURATION_S
    if target_duration_s % fixed_duration != 0:
        errors.append(
            f"目标时长 {target_duration_s}s 不是 {fixed_duration}s 的整数倍；"
            f"节拍单元按 10s 换算要求目标取 {'/'.join(str(x) for x in config.EPISODE_TARGET_CHOICES)}s")
    # 镜头数以 target/10 为基础节拍，允许额外拆少量镜头分担口播与必保留关键内容。
    min_shots, max_shots = storyboard_shot_count_range(target_duration_s)
    if not min_shots <= len(shots) <= max_shots:
        errors.append(
            f"镜头数 {len(shots)} 不匹配；目标 {target_duration_s}s 下基础镜头数为 {min_shots} 个，"
            f"口播/关键内容过密时可拆分到最多 {max_shots} 个镜头；请增删镜头并保持 shot_no 连续")
    # 自动生成阶段需要用目标时长约束模型，避免它靠注水拉长；人工编辑确认阶段则不把
    # 规划目标当硬上限，用户明确设置的实际总时长只要逐镜合法即可进入生成。
    if enforce_total_duration:
        total = sum(s.duration_s for s in shots)
        hi = storyboard_duration_limit(target_duration_s, board)
        if total > hi:
            errors.append(f"总时长 {total}s 超出上限 {hi}s，请缩短部分镜头时长")

    prev_sizes: list[str] = []
    scene_last_seen: dict[str, int] = {}
    for i, shot in enumerate(shots):
        shot.action_desc = normalize_action_desc(shot.action_desc)
        tag = f"shots[{i}](shot_no={shot.shot_no})"
        # V2 时长合法取值
        if not min_dur <= shot.duration_s <= max_dur:
            errors.append(f"{tag}.duration_s={shot.duration_s}，视频生成时长必须在 {min_dur}~{max_dur}s 之间")
        # V8 画面清晰度：单镜只演一个连贯动作，把它写清即可（不再逼塞多个快切小镜头）。
        if len(shot.action_desc) < ACTION_DESC_HARD_MIN:
            errors.append(
                f"{tag}.action_desc 仅 {len(shot.action_desc)} 字，低于硬下限 {ACTION_DESC_HARD_MIN} 字；"
                f"请按目标 {ACTION_DESC_MIN_CHARS} 字把这一个动作的起势、过程、收势和人物表情/反应写清")
        source_len = len((shot.source_excerpt or "").strip())
        if source_len < SOURCE_EXCERPT_MIN_CHARS:
            errors.append(
                f"{tag}.source_excerpt 仅 {source_len} 字；每个分镜必须带对应小说原文摘录，"
                f"请从本集原文中逐字摘录至少 {SOURCE_EXCERPT_MIN_CHARS} 字作为 Seedance 兜底参考")
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
        # 旁白选填、少用：不再要求每镜必填，也不再禁止纯画面/纯台词镜头；
        # 若写了旁白则保留上限校验，避免重新退回旁白堆砌。
        narration_len = len((shot.narration or "").strip())
        if narration_len > NARRATION_HARD_MAX:
            errors.append(
                f"{tag}.narration 共 {narration_len} 字，超过硬上限 {NARRATION_HARD_MAX} 字——10s 配音念不完、读太快观感差；"
                f"旁白请精简到 {NARRATION_TARGET_CHARS} 字以内（一句最关键的推进），或直接留空、改用台词与画面动作承载")
        # 口播总量必须能在视频最长时长内念完（含台词+旁白），否则配音会被截断、音画不同步。
        spoken_chars = sum(len(re.sub(r"\s+", "", d.line or "")) for d in shot.dialogues) \
            + len(re.sub(r"\s+", "", (shot.narration or "")))
        if spoken_chars > config.MAX_SPOKEN_CHARS_PER_SHOT:
            errors.append(
                f"{tag} 台词+旁白共 {spoken_chars} 字，超过单镜口播上限 {config.MAX_SPOKEN_CHARS_PER_SHOT} 字"
                f"（{config.MAX_VIDEO_DURATION_S}s 也念不完）；请新增或利用相邻镜头分担这段台词，必要时再精简非关键口水话，"
                "不要让一镜塞下念不完的台词")
        # V4 角色合法性
        if not shot.characters:
            errors.append(f"{tag}.characters 为空；每个 10s 视频段必须以人物和剧情为主体，至少包含 1 个角色圣经中的角色")
        for name in shot.characters:
            if bible_names and name not in bible_names:
                errors.append(f"{tag}.characters 含「{name}」，角色圣经中不存在。圣经角色为：{'/'.join(sorted(bible_names))}")
        named_mentions = [name for name in shot.characters if name in shot.action_desc]
        if shot.characters and not named_mentions:
            errors.append(
                f"{tag}.action_desc 未出现本镜头角色名；必须用角色圣经的准确姓名写人物动作，"
                "不要只写他/她/纸张/镜头/场景")
        speakers_ok = set(shot.characters)
        for j, d in enumerate(shot.dialogues):
            if d.speaker not in speakers_ok:
                errors.append(
                    f"{tag}.dialogues[{j}].speaker=「{d.speaker}」不在该镜头 characters 中；"
                    "dialogues 只写人物实际开口台词，旁白请放 narration")
        # V5：10s 视频段允许多个连续动作/小镜头，禁止回到单一低信息动作。
        if len(shot.action_desc) < 10:
            errors.append(f"{tag}.action_desc 长度 {len(shot.action_desc)} 字，要求至少 10 字")
        # 枚举值
        if shot.shot_size not in SHOT_SIZES:
            errors.append(f"{tag}.shot_size=「{shot.shot_size}」不在 {sorted(SHOT_SIZES)}")
        if shot.camera_move not in CAMERA_MOVES:
            errors.append(f"{tag}.camera_move=「{shot.camera_move}」不在 {sorted(CAMERA_MOVES)}")
        if shot.transition not in TRANSITIONS:
            errors.append(f"{tag}.transition=「{shot.transition}」不在 {sorted(TRANSITIONS)}")
        # V6 场景连续性（场景标签长度上限校验已取消）
        scene = shot.scene_setting.strip()
        if scene in scene_last_seen and scene_last_seen[scene] != i - 1:
            errors.append(f"场景「{scene}」在 shots[{scene_last_seen[scene]}] 与 shots[{i}] 间被其他场景打断，同场景镜头必须连续排列")
        scene_last_seen[scene] = i
        # V6+ 连贯性：每个可变时长视频段都要像连续短片，不能每镜头重开一个摘要。
        if i == 0 and shot.continuity_from_prev:
            errors.append(f"{tag}.continuity_from_prev=true，但第一个镜头没有上一镜可承接")
        if i > 0:
            prev = shots[i - 1]
            prev_scene = prev.scene_setting.strip()
            shared_chars = set(prev.characters) & set(shot.characters)
            if shot.continuity_from_prev:
                if scene != prev_scene:
                    errors.append(
                        f"{tag}.continuity_from_prev=true 但 scene_setting 从「{prev_scene}」变为「{scene}」；"
                        "接上镜必须沿用同一时间地点标签，换场请设为 false 并写清转场")
                if not shared_chars:
                    errors.append(
                        f"{tag}.continuity_from_prev=true 但与上一镜没有共同角色；"
                        "同场景接镜必须保留上一镜核心人物或在 action_desc 写明入场/离场承接")
                if shot.transition != "硬切":
                    errors.append(f"{tag}.transition=「{shot.transition}」，同场景接上镜应使用「硬切」")
            else:
                if scene == prev_scene:
                    errors.append(
                        f"{tag}.continuity_from_prev=false 但 scene_setting 与上一镜同为「{scene}」；"
                        "同一场景内除首镜外必须接上镜，避免上下文断裂")
                else:
                    if shot.transition == "硬切":
                        errors.append(
                            f"{tag}.transition=硬切 但 scene_setting 从「{prev_scene}」切到「{scene}」；"
                            f"跨时间/地点请用 {sorted(SCENE_CUT_TRANSITIONS)} 之一，并写清承接")
                    elif shot.transition not in SCENE_CUT_TRANSITIONS:
                        errors.append(
                            f"{tag}.transition=「{shot.transition}」不适合换场；"
                            f"换场请用 {sorted(SCENE_CUT_TRANSITIONS)} 之一")
                    dialogue_text = "".join(d.line for d in shot.dialogues)
                    # 承接说明判定（放宽，杜绝高频误伤）：满足以下任一即视为已写清承接——
                    # ① 含时间/线索类承接词；② 动作/旁白写了人物移动（走过去/转身离开/来到=移动即承接）；
                    # ③ 与上一镜是同一片连续空间的子区域移动（主地点相同）。三者都不满足才是真·无解释硬跳。
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
_SPEAKER_PREFIX_RE = re.compile(r"^[^\n：:]{1,16}(?:（[^）]{0,12}）)?[：:]")
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


def validate_screenplay(script: EpisodeScreenplay, bible: Bible, expected_beats: int,
                        episode_no: int | None = None) -> list[str]:
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
        tag = f"scene_outline[{i - 1}]"
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
        unknown = [name for name in scene.characters if name not in bible_names]
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
    if len((script.emotional_curve or "").strip()) < 6:
        errors.append("emotional_curve 过短或缺失；请说明本集情绪推进")
    if len((script.ending_hook or "").strip()) < 6:
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
    missing_in_script = [
        ln for ln in key_lines
        if _longest_run_ratio(_strip_speaker(ln), full_text) < KEY_LINE_PRESENT_RATIO
        and _bigram_coverage(_strip_speaker(ln), full_text) < KEY_LINE_BIGRAM_COVERAGE]
    if missing_in_script:
        shown = "；".join(missing_in_script[:KEY_CONTENT_MAX_REPORT])
        errors.append(
            f"key_lines 有 {len(missing_in_script)} 条未真正写进 full_script_text：{shown}"
            "；关键台词必须在剧本正文里实际出现，不能只列在清单里")
    key_points = [pt.strip() for pt in (script.key_plot_points or []) if pt and pt.strip()]
    if len(key_points) < MIN_KEY_PLOT_POINTS:
        errors.append(
            f"key_plot_points 仅 {len(key_points)} 条；请列出本集至少 {MIN_KEY_PLOT_POINTS} 条绝不能丢的关键剧情点"
            "（核心事件/反转/信息揭示）")
    if script.beats:
        errors.append("剧本台不再接受 beats 拍卡结构；请重新生成完整剧本")
    return errors


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
    分镜台不能把它们压成纯画面卡”。错误会进入修复回路，让模型补齐声轨。
    """
    errors: list[str] = []
    shots = board.shots
    if not shots:
        return errors

    stats = _screenplay_sound_stats(screenplay)
    script_sound_cues = sum(stats.values())
    if script_sound_cues == 0:
        return errors

    expected_shots = max(1, math.ceil(target_duration_s / config.FIXED_VIDEO_DURATION_S))
    voiced_count = _voiced_shot_count(shots)
    min_voiced = min(len(shots), max(2, math.ceil(expected_shots * 0.75)))
    if voiced_count < min_voiced:
        errors.append(
            f"分镜声轨过少：完整剧本含 {script_sound_cues} 处台词/内心OS/旁白/人群声音，"
            f"但只有 {voiced_count}/{len(shots)} 个镜头写了 dialogues 或 narration；"
            f"请至少让 {min_voiced} 个镜头保留可听见的剧情信息，避免生成纯画面哑剧")

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


def validate_storyboard_outline(outline: StoryboardOutline, screenplay: EpisodeScreenplay,
                                target_duration_s: int) -> list[str]:
    """校验分镜大纲：镜头数在范围内、shot_no 连续、每镜有推进、相邻镜不停留在同一节拍，
    且全集必保留关键台词/剧情点都被分配到某一镜（防止规划阶段就把剧情铺一半、后段漏戏）。"""
    errors: list[str] = []
    shots = outline.shots
    min_shots, max_shots = storyboard_shot_count_range(target_duration_s)
    if not shots:
        return [f"分镜大纲为空；目标 {target_duration_s}s 需规划 {min_shots}~{max_shots} 条镜头节拍，"
                "请把整集剧情从头到尾铺成有序镜头列表"]
    if not min_shots <= len(shots) <= max_shots:
        errors.append(
            f"大纲镜头数 {len(shots)} 不在 {min_shots}~{max_shots} 之间；"
            "请按剧情密度在该区间内取值，并把整集剧情均匀铺满，不要前松后紧或半途收尾")
    actual = [s.shot_no for s in shots]
    if actual != list(range(1, len(shots) + 1)):
        errors.append(f"大纲 shot_no 必须为连续递增 1..{len(shots)}，当前为 {actual}")
    for i, s in enumerate(shots):
        if len((s.beat or "").strip()) < 6:
            errors.append(f"大纲第 {i + 1} 镜 beat 过短或缺失；请用一句话写清本镜推进的剧情（谁做了什么/局势如何变化）")
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
    return errors


# ---------- C2 基于完整剧本的分镜校验 ----------

def estimate_speech_seconds(shot) -> float:
    """估算本镜配音（台词 + 旁白）从开场留白到念完所需秒数。口径与 audio.spoken_text 一致：标点也占停顿时间，
    故按非空白字符计；并计入开场留白 SPEECH_LEAD_IN_S（人声前的动作建立）。返回 0 表示本镜无声轨。"""
    parts = [(d.line or "").strip() for d in shot.dialogues]
    narration = (shot.narration or "").strip()
    if narration:
        parts.append(narration)
    chars = sum(len(re.sub(r"\s+", "", p)) for p in parts if p)
    if chars <= 0:
        return 0.0
    return config.SPEECH_LEAD_IN_S + chars / config.SPEECH_CHARS_PER_SECOND + config.SPEECH_TAIL_BUFFER_S


def normalize_durations_for_speech(board: Storyboard) -> None:
    """确定性时长归一：把每镜 duration_s 抬到至少能念完本镜台词/旁白的长度，再 clamp 到 [MIN,MAX]。

    动作密度给出的时长只是兜底下限；台词较长的镜头若沿用"动作简单=短时长"会让视频动作先于台词结束，
    造成音画不同步（台词没说完人就做完动作）。与 normalize_continuity 同理由代码强制覆盖，不依赖模型自觉。
    台词超过 MAX 秒数才念得完的镜头无法再加长（Seedance 上限），保持 MAX 并由 prompt 侧约束单镜台词不要过长。"""
    min_dur = config.MIN_VIDEO_DURATION_S
    for shot in board.shots:
        floor = enforced_min_duration(board, shot)
        shot.duration_s = max(int(shot.duration_s or min_dur), floor)


def compact_durations_to_budget(board: Storyboard, target_duration_s: int, *,
                                desired_total_s: int | None = None) -> dict:
    """只压缩 duration_s，不改内容。

    每镜最多压到 enforced_min_duration（口播能念完 + 开场镜保底），优先压缩冗余最多的长镜。
    desired_total_s 用于人工/按钮希望尽量回到目标；不传则只保证不超过硬上限。
    """
    limit = storyboard_duration_limit(target_duration_s, board)
    total_before = sum(int(s.duration_s or 0) for s in board.shots)
    target_total = min(desired_total_s or limit, limit)
    target_total = max(target_total, sum(enforced_min_duration(board, s) for s in board.shots))
    changes: list[dict] = []

    while sum(int(s.duration_s or 0) for s in board.shots) > target_total:
        candidates = []
        for i, shot in enumerate(board.shots):
            floor = enforced_min_duration(board, shot)
            slack = int(shot.duration_s or 0) - floor
            if slack > 0:
                candidates.append((slack, int(shot.duration_s or 0), i, floor))
        if not candidates:
            break
        _, _, idx, floor = max(candidates)
        shot = board.shots[idx]
        before = int(shot.duration_s or 0)
        need = sum(int(s.duration_s or 0) for s in board.shots) - target_total
        after = max(floor, before - need)
        shot.duration_s = after
        changes.append({"shot_no": shot.shot_no, "before": before, "after": after})

    return {
        "total_before": total_before,
        "total_after": sum(int(s.duration_s or 0) for s in board.shots),
        "target_total": target_total,
        "limit": limit,
        "changes": changes,
    }


def _is_episode_opening_shot(board: Storyboard, shot) -> bool:
    """是否为全片开场镜：第一集（episode_no==1）的第一镜（shot_no==1）。"""
    return int(getattr(board, "episode_no", 0) or 0) == 1 and int(getattr(shot, "shot_no", 0) or 0) == 1


def enforced_min_duration(board: Storyboard, shot) -> int:
    """本镜由代码强制保证的最小时长：取「配音念完所需」与「开场建场镜固定长时长」的较大者，clamp 到 [MIN,MAX]。
    校验总时长上限与归一时长共用此口径，避免确定性覆盖把合法分镜误判超时退回。"""
    need = math.ceil(estimate_speech_seconds(shot))
    if _is_episode_opening_shot(board, shot):
        need = max(need, config.ESTABLISHING_SHOT_DURATION_S)
    return max(config.MIN_VIDEO_DURATION_S, min(config.MAX_VIDEO_DURATION_S, need))


def normalize_episode_opening_shot(board: Storyboard) -> None:
    """第一集第一镜=全片开场建场镜的出片侧确定性覆盖：拉长时长 + 强制远景建场 + 缓慢推近运镜。
    与 normalize_continuity 同理由代码强制（不依赖模型自觉），保证开场镜稳定地"先立背景"。"""
    for shot in board.shots:
        if _is_episode_opening_shot(board, shot):
            shot.duration_s = enforced_min_duration(board, shot)
            shot.shot_size = config.ESTABLISHING_SHOT_SIZE
            shot.camera_move = config.ESTABLISHING_CAMERA_MOVE


def normalize_continuity(board: Storyboard) -> None:
    """continuity/transition 由场景标签代码推导覆盖（不依赖模型自觉，消除一整类返工）。"""
    for i, shot in enumerate(board.shots):
        if i == 0:
            shot.continuity_from_prev = False
            shot.transition = "硬切"
            continue
        same_scene = shot.scene_setting.strip() == board.shots[i - 1].scene_setting.strip()
        shot.continuity_from_prev = same_scene
        if same_scene:
            shot.transition = "硬切"
        elif shot.transition == "硬切":
            shot.transition = default_scene_transition(board.shots[i - 1], shot)


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


def _snap_duration(value: int) -> int:
    """把目标时长吸附到 [MIN, MAX] 内、STEP 的整数倍（40/50/60/70/80/90）。"""
    step = config.EPISODE_TARGET_STEP_S
    lo, hi = config.EPISODE_TARGET_MIN_S, config.EPISODE_TARGET_MAX_S
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = config.EPISODE_TARGET_DEFAULT_S
    v = min(max(v, lo), hi)
    v = round(v / step) * step
    return min(max(v, lo), hi)


def normalize_plan_chapters(plan_episodes: list, *, start_episode_no: int = 1,
                            start_chapter: int = 1, chapter_count: int = 1) -> None:
    """生产级兜底：用确定性代码强制 episode_no / source_chapters / target_duration_s 满足不变量，
    而不是把“章节区间记账”这种 LLM 不擅长的活儿丢给模型反复重试。
    创意内容（标题/钩子/梗概）仍由模型负责；本函数只就地修正结构字段，原地修改 plan_episodes。
    规则：episode_no 连续；首集从 start_chapter 起；其后每集 [lo,hi] 连续、不倒退、不跳章、不越界，
    允许同一章被连续多集共同覆盖。"""
    prev_start = start_chapter
    prev_end = start_chapter - 1
    for i, ep in enumerate(plan_episodes):
        ep.episode_no = start_episode_no + i
        ep.target_duration_s = _snap_duration(getattr(ep, "target_duration_s", config.EPISODE_TARGET_DEFAULT_S))
        chs = [c for c in (ep.source_chapters or []) if isinstance(c, int)]
        lo = min(chs) if chs else (start_chapter if i == 0 else prev_end)
        hi = max(chs) if chs else lo
        if i == 0:
            lo = start_chapter
        else:
            lo = min(max(lo, prev_start), prev_end + 1)   # 不早于上集起点、不跳章
        hi = min(max(hi, lo, prev_end), chapter_count)    # 不早于上集终点、不越界、≥lo
        lo = min(lo, chapter_count)
        ep.source_chapters = list(range(lo, hi + 1))
        prev_start, prev_end = lo, hi


def validate_plan(plan_episodes: list, chapter_count: int,
                  *, start_episode_no: int = 1, start_chapter: int = 1) -> list[str]:
    """校验一批剧集。批内 episode_no 从 start_episode_no 连续递增，第一集须从 start_chapter 起，
    章节只能向前推进（不倒退、不跳章），不越界。
    允许同一章被连续多集共同覆盖——章节内容多时一章可拆成 2~3 集，是合理结构，
    不能因“章节数 < 想要的集数”就逼模型必须每章独占一集（那会导致无法满足、无限重试）。"""
    errors = []
    if not plan_episodes:
        return ["本批未规划出任何剧集"]
    prev_start = start_chapter      # 上一集的起始章（用于判断是否倒退）
    prev_end = start_chapter - 1    # 已推进到的最后一章
    for i, ep in enumerate(plan_episodes):
        if ep.episode_no != start_episode_no + i:
            errors.append(f"episodes[{i}].episode_no={ep.episode_no}，本批要求从 {start_episode_no} 起连续递增")
        chs = ep.source_chapters
        if not chs:
            errors.append(f"episodes[{i}].source_chapters 为空")
            continue
        if chs != list(range(chs[0], chs[-1] + 1)):
            errors.append(f"episodes[{i}].source_chapters={chs} 必须是连续区间")
        if i == 0:
            if chs[0] != start_chapter:
                errors.append(f"本批第一集 source_chapters 必须从第 {start_chapter} 章开始，当前为第 {chs[0]} 章")
        else:
            # 允许：续讲同一章（chs[0]==prev_end，把一章拆成多集）或顺接下一章（chs[0]==prev_end+1）。
            if chs[0] < prev_start:
                errors.append(f"episodes[{i}].source_chapters 起点第{chs[0]}章早于上一集起点第{prev_start}章，集间剧情不允许倒退")
            elif chs[0] > prev_end + 1:
                errors.append(f"episodes[{i}].source_chapters 跳过了第{prev_end + 1}~{chs[0] - 1}章，集间不允许跳章")
            elif chs[-1] < prev_end:
                errors.append(f"episodes[{i}].source_chapters 止于第{chs[-1]}章，早于上一集的第{prev_end}章，集间剧情不允许倒退")
        if chs[-1] > chapter_count:
            errors.append(f"episodes[{i}].source_chapters 引用第{chs[-1]}章，但全书只有 {chapter_count} 章")
        prev_start = chs[0]
        prev_end = max(prev_end, chs[-1])
        if not config.EPISODE_TARGET_MIN_S <= ep.target_duration_s <= config.EPISODE_TARGET_MAX_S:
            errors.append(
                f"episodes[{i}].target_duration_s={ep.target_duration_s}，"
                f"要求 {config.EPISODE_TARGET_MIN_S}~{config.EPISODE_TARGET_MAX_S}")
        elif ep.target_duration_s % config.EPISODE_TARGET_STEP_S != 0:
            errors.append(
                f"episodes[{i}].target_duration_s={ep.target_duration_s}，"
                f"集目标时长需按 {config.EPISODE_TARGET_STEP_S}s 步进取值")
    return errors
