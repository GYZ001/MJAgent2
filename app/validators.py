"""分镜脚本业务校验器 V1~V8（docs/PROMPT_SPEC.md §C）。
错误消息必须具体到字段与数值——修复回路把它们逐条回喂模型（1.0 教训：从不告诉模型哪里错了）。
"""
from __future__ import annotations

import math
import re

from app import config
from app.schemas import (BEAT_TYPES, Beat, BeatChain, Bible, Shot, Storyboard,
                         SHOT_SIZES, CAMERA_MOVES, TIME_OF_DAY_ORDER, TRANSITIONS)


# 字数约束设计原则（实测教训，2026-06-12）：
# ① 只有"上限"有物理意义：旁白+台词进 Seedance story_cues，未来 TTS 要在 10s 内念完，
#    超过 ~7字/s 物理上念不完。"下限"是信息密度的代理指标，节拍表 turn 上线后已删除——
#    代理指标会让模型在「凑字数」和「禁止注水」之间振荡（实测 8 轮修复拉锯）。
# ② 提示词写目标值，校验器只卡 目标±30% 容差：LLM 数不准汉字（±30% 是常态），
#    阈值=目标值 必然返工。TARGET 进 prompt，HARD 进校验。
ORAL_TARGET_RANGE = (30, 50)        # 口播目标（prompt 用）：10s 念得完的量
ORAL_HARD_MAX_PER_10S = 70          # 口播硬上限（校验用）：物理上念不完
NARRATION_TARGET_CHARS = 45         # 旁白目标（prompt 用）
NARRATION_HARD_CHARS = 60           # 旁白硬上限（校验用，目标+33%容差）
ACTION_DESC_HARD_MIN = 50           # action_desc 硬下限（校验用；prompt 目标 70）
ACTION_DESC_MIN_CHARS = 70
VIDEO_SEGMENT_MIN_BEATS = 3
SCENE_SETTING_MAX_CHARS = 18
TRANSITION_HINTS = (
    "次日", "第二天", "当天", "清晨", "上午", "中午", "下午", "傍晚", "深夜", "夜里",
    "与此同时", "转场", "随后", "片刻后", "几小时后", "数小时后", "一夜后", "回到", "另一边",
    "带着", "顺着", "接着", "继续", "仍", "还", "已经",
)


def _text_budget(shot: Shot) -> int:
    total = len(shot.narration or "")
    for d in shot.dialogues:
        total += len(d.line)
    return total


def _action_beat_count(text: str) -> int:
    parts = [p.strip() for p in re.split(r"[，。；;、\n]+", text) if len(p.strip()) >= 4]
    return len(parts)


def _has_transition_hint(*parts: str | None) -> bool:
    text = "".join(part or "" for part in parts)
    return any(hint in text for hint in TRANSITION_HINTS)


def validate_storyboard(board: Storyboard, bible: Bible, target_duration_s: int) -> list[str]:
    errors: list[str] = []
    shots = board.shots
    if not shots:
        fixed_duration = config.FIXED_VIDEO_DURATION_S
        expected_shots = max(1, math.ceil(target_duration_s / fixed_duration))
        return [f"shots 为空；目标 {target_duration_s}s 固定 {fixed_duration}s 视频段必须输出 {expected_shots} 个镜头"]

    bible_names = {c.name for c in bible.characters}

    # V1 总时长
    total = sum(s.duration_s for s in shots)
    lo, hi = int(target_duration_s * 0.9), int(target_duration_s * 1.1)
    if not lo <= total <= hi:
        errors.append(f"总时长 {total}s 超出 {lo}~{hi}s，请调整镜头时长或增删镜头")
    fixed_duration = config.FIXED_VIDEO_DURATION_S
    if target_duration_s % fixed_duration != 0:
        errors.append(
            f"目标时长 {target_duration_s}s 不是 {fixed_duration}s 的整数倍；固定 10s 视频段要求目标取 40/50/60s")
    expected_shots = max(1, math.ceil(target_duration_s / fixed_duration))
    if len(shots) != expected_shots:
        errors.append(
            f"镜头数 {len(shots)} 不匹配；固定 {fixed_duration}s 视频段下，目标 {target_duration_s}s 必须正好 {expected_shots} 个镜头")

    prev_sizes: list[str] = []
    scene_last_seen: dict[str, int] = {}
    for i, shot in enumerate(shots):
        tag = f"shots[{i}](shot_no={shot.shot_no})"
        # V2 时长合法取值
        if shot.duration_s != fixed_duration:
            errors.append(f"{tag}.duration_s={shot.duration_s}，固定视频生成时长必须为 {fixed_duration}s")
        # V3 口播带宽：只卡有物理意义的上限（10s 念不完）；下限已删除——信息密度由节拍表 turn 保证。
        budget = _text_budget(shot)
        oral_max = shot.duration_s * ORAL_HARD_MAX_PER_10S // 10
        if budget > oral_max:
            errors.append(
                f"{tag} 旁白+台词共 {budget} 字，超过 {shot.duration_s}s 硬上限 {oral_max} 字——"
                f"配音物理上念不完，请精简到 {ORAL_TARGET_RANGE[0]}~{ORAL_TARGET_RANGE[1]} 字，多余信息移入 action_desc")
        # V8 画面丰富度：校验阈值低于 prompt 目标值（容差带，LLM 数不准汉字）。
        if len(shot.action_desc) < ACTION_DESC_HARD_MIN:
            errors.append(
                f"{tag}.action_desc 仅 {len(shot.action_desc)} 字，低于硬下限 {ACTION_DESC_HARD_MIN} 字；"
                f"请按目标 {ACTION_DESC_MIN_CHARS} 字写出连续小镜头、画面推进、角色反应和新信息")
        beat_count = _action_beat_count(shot.action_desc)
        if beat_count < VIDEO_SEGMENT_MIN_BEATS:
            errors.append(
                f"{tag}.action_desc 只有 {beat_count} 个动作/信息节拍；10s 视频段至少需要 "
                f"{VIDEO_SEGMENT_MIN_BEATS} 个连续节拍，请用逗号或分号写清先后推进")
        if budget == 0:
            errors.append(f"{tag} 是纯画面镜头；固定 10s 视频段必须加入旁白或台词来承载剧情信息")
        # V4 角色合法性
        if not shot.characters:
            errors.append(f"{tag}.characters 为空；每个 10s 视频段必须以人物和剧情为主体，至少包含 1 个角色圣经中的角色")
        for name in shot.characters:
            if name not in bible_names:
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
        # V6 场景连续性
        scene = shot.scene_setting.strip()
        if len(scene) > SCENE_SETTING_MAX_CHARS:
            errors.append(
                f"{tag}.scene_setting 过长（{len(scene)} 字）；场景只作连续性标签，"
                f"最多 {SCENE_SETTING_MAX_CHARS} 字，只保留时间+地点，删除氛围和环境描写")
        if scene in scene_last_seen and scene_last_seen[scene] != i - 1:
            errors.append(f"场景「{scene}」在 shots[{scene_last_seen[scene]}] 与 shots[{i}] 间被其他场景打断，同场景镜头必须连续排列")
        scene_last_seen[scene] = i
        # V6+ 连贯性：固定 10s 段要像连续短片，不能每镜头重开一个摘要。
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
                            "跨时间/地点请用「叠化」或「黑场」并写清承接")
                    dialogue_text = "".join(d.line for d in shot.dialogues)
                    if not _has_transition_hint(scene, shot.action_desc, shot.narration, dialogue_text):
                        errors.append(
                            f"{tag} 从上一镜「{prev_scene}」切到「{scene}」但缺少承接说明；"
                            "请在 narration 或 action_desc 写清时间跳跃、线索带入或人物为何来到新场景")
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


# ---------- C1 节拍链 ----------

_DAY_NAMES = ("首日", "次日", "第三日", "第四日", "第五日", "第六日", "第七日")


def beat_scene_label(beat: Beat) -> str:
    """节拍 → 场景标签（代码生成，分镜阶段逐字使用，保证时间线与场景标签稳定）。"""
    day = _DAY_NAMES[beat.day_offset] if beat.day_offset < len(_DAY_NAMES) else f"第{beat.day_offset + 1}日"
    return f"{day}{beat.time_of_day}，{beat.location.strip()}"


def validate_beat_chain(chain: BeatChain, bible: Bible, expected_beats: int) -> list[str]:
    errors: list[str] = []
    beats = chain.beats
    if len(beats) != expected_beats:
        return [f"beats 数量 {len(beats)} 不等于本集所需 {expected_beats} 拍（每拍对应一个 10s 视频段）"]
    bible_names = {c.name for c in bible.characters}
    tod_index = {t: i for i, t in enumerate(TIME_OF_DAY_ORDER)}
    prev_time: tuple[int, int] | None = None
    for i, b in enumerate(beats):
        tag = f"beats[{i}](beat_no={b.beat_no})"
        if b.beat_no != i + 1:
            errors.append(f"{tag}.beat_no 必须为 {i + 1}")
        if b.day_offset < 0:
            errors.append(f"{tag}.day_offset={b.day_offset}，必须 ≥0（0=本集第一天）")
        if b.time_of_day not in tod_index:
            errors.append(f"{tag}.time_of_day=「{b.time_of_day}」不在 {list(TIME_OF_DAY_ORDER)}")
        else:
            cur = (b.day_offset, tod_index[b.time_of_day])
            if prev_time is not None and cur < prev_time:
                errors.append(
                    f"{tag} 时间({beat_scene_label(b)})早于上一拍——时间只能向前，禁止闪回；"
                    "前史改用第 1 拍 event/turn 一句话带过")
            prev_time = cur
        if not 2 <= len(b.location.strip()) <= 10:
            errors.append(f"{tag}.location=「{b.location}」要求 2~10 字主地点标签")
        if not b.characters:
            errors.append(f"{tag}.characters 为空；每拍必须有实际在场角色")
        for name in b.characters:
            if name not in bible_names:
                errors.append(f"{tag}.characters 含「{name}」不在角色圣经：{'/'.join(sorted(bible_names))}")
        if not 8 <= len(b.event) <= 50:
            errors.append(f"{tag}.event 长度 {len(b.event)} 字，要求 8~50 字（谁做了什么，一句话）")
        if not 4 <= len(b.turn) <= 40:
            errors.append(f"{tag}.turn 长度 {len(b.turn)} 字，要求 4~40 字（局势变化/新信息）")
        if not 4 <= len(b.carry) <= 30:
            errors.append(f"{tag}.carry 长度 {len(b.carry)} 字，要求 4~30 字（留给下一拍的钩子）")
        if b.beat_type not in BEAT_TYPES:
            errors.append(f"{tag}.beat_type=「{b.beat_type}」不在 {sorted(BEAT_TYPES)}")
        if i > 0 and b.beat_type == "铺垫" and beats[i - 1].beat_type == "铺垫":
            errors.append(f"{tag} 与上一拍连续两拍「铺垫」——每拍必须推进局势，请改为升级/反转/高潮")
    if beats and beats[0].beat_type != "钩子":
        errors.append(f"beats[0].beat_type=「{beats[0].beat_type}」，第 1 拍必须是「钩子」")
    if beats and beats[-1].beat_type != "尾钩":
        errors.append(f"最后一拍 beat_type=「{beats[-1].beat_type}」，必须是「尾钩」")
    if len(beats) >= 4 and not any(b.beat_type in ("反转", "高潮") for b in beats[1:-1]):
        errors.append("中段（除首尾拍外）至少要有 1 拍「反转」或「高潮」，否则全集无情绪起伏")
    return errors


# ---------- C2 对拍展开 ----------

def normalize_continuity(board: Storyboard) -> None:
    """continuity/transition 由场景标签代码推导覆盖（不依赖模型自觉，消除一整类返工）。"""
    for i, shot in enumerate(board.shots):
        if i == 0:
            shot.continuity_from_prev = False
            continue
        same_scene = shot.scene_setting.strip() == board.shots[i - 1].scene_setting.strip()
        shot.continuity_from_prev = same_scene
        if same_scene:
            shot.transition = "硬切"
        elif shot.transition == "硬切":
            shot.transition = "叠化"


def validate_storyboard_against_beats(board: Storyboard, bible: Bible, target_duration_s: int,
                                      chain: BeatChain) -> list[str]:
    """两段式校验：先代码推导连贯字段，再跑通用校验，最后逐镜对拍。"""
    normalize_continuity(board)
    errors = validate_storyboard(board, bible, target_duration_s)
    labels = [beat_scene_label(b) for b in chain.beats]
    for i, shot in enumerate(board.shots):
        if i >= len(chain.beats):
            break
        beat = chain.beats[i]
        tag = f"shots[{i}](shot_no={shot.shot_no})"
        if shot.scene_setting.strip() != labels[i]:
            errors.append(f"{tag}.scene_setting 必须逐字等于节拍表给定标签「{labels[i]}」，当前为「{shot.scene_setting}」")
        missing = [n for n in beat.characters if n not in shot.characters]
        if missing:
            errors.append(f"{tag}.characters 缺少第 {i + 1} 拍在场角色：{missing}")
        if shot.narration and len(shot.narration) > NARRATION_HARD_CHARS:
            errors.append(
                f"{tag}.narration 共 {len(shot.narration)} 字，超过硬上限 {NARRATION_HARD_CHARS} 字——"
                f"请按目标 ≤{NARRATION_TARGET_CHARS} 字精简：旁白只写画面拍不出的信息（时间跳跃/内心），剧情信息移入台词或 action_desc")
    return errors


def validate_bible(bible: Bible) -> list[str]:
    errors = []
    if not 1 <= len(bible.characters) <= 8:
        errors.append(f"characters 数量 {len(bible.characters)}，要求 1~8 个")
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


def validate_plan(plan_episodes: list, chapter_count: int,
                  *, start_episode_no: int = 1, start_chapter: int = 1) -> list[str]:
    """校验一批剧集。批内 episode_no 从 start_episode_no 连续递增，
    第一集须从 start_chapter 起，章节连续不重叠不跳，不越界。"""
    errors = []
    if not plan_episodes:
        return ["本批未规划出任何剧集"]
    prev_end = start_chapter - 1
    for i, ep in enumerate(plan_episodes):
        if ep.episode_no != start_episode_no + i:
            errors.append(f"episodes[{i}].episode_no={ep.episode_no}，本批要求从 {start_episode_no} 起连续递增")
        chs = ep.source_chapters
        if not chs:
            errors.append(f"episodes[{i}].source_chapters 为空")
            continue
        if chs != list(range(chs[0], chs[-1] + 1)):
            errors.append(f"episodes[{i}].source_chapters={chs} 必须是连续区间")
        if i == 0 and chs[0] != start_chapter:
            errors.append(f"本批第一集 source_chapters 必须从第 {start_chapter} 章开始，当前为第 {chs[0]} 章")
        if chs[0] <= prev_end:
            errors.append(f"episodes[{i}].source_chapters 与上一集重叠（上一集止于第{prev_end}章）")
        if chs[0] > prev_end + 1:
            errors.append(f"episodes[{i}].source_chapters 跳过了第{prev_end + 1}~{chs[0] - 1}章，集间不允许跳章")
        if chs[-1] > chapter_count:
            errors.append(f"episodes[{i}].source_chapters 引用第{chs[-1]}章，但全书只有 {chapter_count} 章")
        prev_end = chs[-1]
        if not config.EPISODE_TARGET_MIN_S <= ep.target_duration_s <= config.EPISODE_TARGET_MAX_S:
            errors.append(
                f"episodes[{i}].target_duration_s={ep.target_duration_s}，"
                f"要求 {config.EPISODE_TARGET_MIN_S}~{config.EPISODE_TARGET_MAX_S}")
        elif ep.target_duration_s % config.EPISODE_TARGET_STEP_S != 0:
            errors.append(
                f"episodes[{i}].target_duration_s={ep.target_duration_s}，"
                f"固定 10s 视频段要求目标时长为 {config.EPISODE_TARGET_STEP_S}s 的整数倍")
    return errors
