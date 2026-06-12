"""分镜脚本业务校验器 V1~V7（docs/PROMPT_SPEC.md §C）。
错误消息必须具体到字段与数值——修复回路把它们逐条回喂模型（1.0 教训：从不告诉模型哪里错了）。
"""
from __future__ import annotations

import re

from app import config
from app.schemas import Bible, Shot, Storyboard, SHOT_SIZES, CAMERA_MOVES, TRANSITIONS

COMPOUND_ACTION_MARKERS = ("然后", "接着", "随后", "之后又", "边", "一边")
CHARS_PER_SECOND_MAX = 5
CHARS_PER_SECOND_MIN = 3


def _text_budget(shot: Shot) -> int:
    total = len(shot.narration or "")
    for d in shot.dialogues:
        total += len(d.line)
    return total


def validate_storyboard(board: Storyboard, bible: Bible, target_duration_s: int) -> list[str]:
    errors: list[str] = []
    shots = board.shots
    if not shots:
        return ["shots 为空，至少需要 6 个镜头"]

    bible_names = {c.name for c in bible.characters}

    # V1 总时长
    total = sum(s.duration_s for s in shots)
    lo, hi = int(target_duration_s * 0.9), int(target_duration_s * 1.1)
    if not lo <= total <= hi:
        errors.append(f"总时长 {total}s 超出 {lo}~{hi}s，请调整镜头时长或增删镜头")

    prev_sizes: list[str] = []
    scene_last_seen: dict[str, int] = {}
    for i, shot in enumerate(shots):
        tag = f"shots[{i}](shot_no={shot.shot_no})"
        # V2 时长合法取值
        if shot.duration_s not in config.ALLOWED_DURATIONS:
            errors.append(f"{tag}.duration_s={shot.duration_s} 不在合法取值 {sorted(config.ALLOWED_DURATIONS)}")
        # V3 语速预算
        budget = _text_budget(shot)
        max_chars = shot.duration_s * CHARS_PER_SECOND_MAX
        min_chars = shot.duration_s * CHARS_PER_SECOND_MIN
        if budget > max_chars:
            errors.append(f"{tag} 旁白+台词共 {budget} 字，超出 {shot.duration_s}s×{CHARS_PER_SECOND_MAX}={max_chars} 字上限，请精简文案或拆分镜头")
        elif 0 < budget < min_chars:
            errors.append(f"{tag} 旁白+台词仅 {budget} 字，低于 {shot.duration_s}s×{CHARS_PER_SECOND_MIN}={min_chars} 字下限，请补充文案或缩短镜头")
        # V4 角色合法性
        for name in shot.characters:
            if name not in bible_names:
                errors.append(f"{tag}.characters 含「{name}」，角色圣经中不存在。圣经角色为：{'/'.join(sorted(bible_names))}")
        speakers_ok = set(shot.characters) | {"旁白"}
        for j, d in enumerate(shot.dialogues):
            if d.speaker not in speakers_ok:
                errors.append(f"{tag}.dialogues[{j}].speaker=「{d.speaker}」不在该镜头 characters 中（也不是旁白）")
            if len(d.line) > 20:
                errors.append(f"{tag}.dialogues[{j}] 单句台词 {len(d.line)} 字超过 20 字，请拆短")
        # V5 单动作启发式（宁可漏判不可误判）
        marker_hits = sum(shot.action_desc.count(m) for m in COMPOUND_ACTION_MARKERS)
        if marker_hits >= 2 or "然后" in shot.action_desc:
            errors.append(f"{tag}.action_desc 含复合动作连接词（然后/接着/边…边），一个镜头只描述一个主要动作，请拆分镜头")
        if not 10 <= len(shot.action_desc) <= 60:
            errors.append(f"{tag}.action_desc 长度 {len(shot.action_desc)} 字，要求 10~60 字")
        # 枚举值
        if shot.shot_size not in SHOT_SIZES:
            errors.append(f"{tag}.shot_size=「{shot.shot_size}」不在 {sorted(SHOT_SIZES)}")
        if shot.camera_move not in CAMERA_MOVES:
            errors.append(f"{tag}.camera_move=「{shot.camera_move}」不在 {sorted(CAMERA_MOVES)}")
        if shot.transition not in TRANSITIONS:
            errors.append(f"{tag}.transition=「{shot.transition}」不在 {sorted(TRANSITIONS)}")
        # V6 场景连续性
        scene = shot.scene_setting.strip()
        if scene in scene_last_seen and scene_last_seen[scene] != i - 1:
            errors.append(f"场景「{scene}」在 shots[{scene_last_seen[scene]}] 与 shots[{i}] 间被其他场景打断，同场景镜头必须连续排列")
        scene_last_seen[scene] = i
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
        if not 60 <= ep.target_duration_s <= 90:
            errors.append(f"episodes[{i}].target_duration_s={ep.target_duration_s}，要求 60~90")
    return errors
