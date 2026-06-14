"""Prompt 编译器：分镜脚本 → Seedance prompt。确定性代码，非 LLM（PRD §4.4）。
一致性核心：画风串/场景串/角色锚点串逐字拼接，LLM 永不改写。
M0 实测网关无同步参数校验，因此本编译器是参数合法性的唯一防线。
"""
from __future__ import annotations

import hashlib
import re

from app import config
from app.schemas import Bible, Shot

NEGATIVE_SUFFIX = (
    "避免出现：真人实拍，照片写实质感，画面内任何文字/字幕/水印/logo/乱码伪字，多余人物，"
    "畸形手/多指缺指/手指粘连，肢体错位/穿模/关节扭曲，面部扭曲，五官崩坏/中途换脸，"
    "角色换发型换服装/年龄体型漂移，名人长相，道具凭空出现或消失/与手脱节，"
    "动作违反重力与人体运动规律/瞬移，画面变形 morphing/渐变扭曲，镜头中途无故切场景或跳切，"
    "画面闪烁，画风突变，满屏光效/特效遮挡面部")
# 正向质量/稳定锚点（Seedance 最佳实践：显式给出稳定与质量约束，比单纯负面词更有效）
QUALITY_SUFFIX = (
    "人物五官清晰稳定、表情自然，手部与所持道具关系正常稳定，动作符合现实物理与人体运动规律、自然连贯，"
    "单一动作一镜到底，首帧到尾帧同机位同场景、背景构图保持一致只有动作自然推进不跳变，"
    "镜头运动平稳不抖动，光影与色调统一，竖屏电影质感")
# 成片不要任何配乐：只保留人物台词/旁白人声与必要环境音
NO_BGM_SUFFIX = "全程不要任何背景音乐、不要配乐、不要 BGM；声音只保留人物台词、旁白人声与必要的环境音"
SOURCE_EXCERPT_PROMPT_MAX = 260
SOURCE_EXCERPT_MARKER = "小说原文兜底参考："

TRANSITION_VIDEO_HINTS = {
    "叠化": "画面柔和交叠，前一画面逐渐被下一场景气氛替代",
    "淡出淡入": "画面先缓慢变暗或变亮，再进入新场景，明确时间或空间跳转",
    "黑场": "画面短暂压入黑场，再进入下一镜",
    "闪黑": "用一瞬黑闪制造断裂感和悬疑冲击",
    "闪白": "用强光白闪制造冲击或记忆断片",
    "甩镜": "镜头快速横甩并产生运动模糊，在模糊中衔接新场景",
    "遮挡转场": "让人物、门、衣袖、阴影或物体掠过镜头遮住画面后转场",
    "匹配剪辑": "用相近形状、动作、颜色或构图建立视觉呼应后切换",
    "声音延续+叠化": "上一镜的台词或环境声像回忆一样延续，同时画面柔和叠化",
    "声音先行+淡入": "下一场景的声音先出现，画面再淡入新场景",
}


def _clean_transition(transition: str | None) -> str:
    transition = (transition or "").strip()
    if not transition or transition == "硬切":
        return ""
    return transition


def _transition_hint(transition: str) -> str:
    return TRANSITION_VIDEO_HINTS.get(transition, "用明确的视觉转场完成场景切换")


def _incoming_transition_line(transition: str | None) -> str:
    transition = _clean_transition(transition)
    if not transition:
        return ""
    return (
        f"本镜开头转场：从上一镜以「{transition}」进入，{_transition_hint(transition)}；"
        "开头约0.5到1秒完成过渡，随后落稳到本镜首帧和新场景，不要误以为仍在上一地点。"
    )


def _outgoing_transition_line(transition: str | None, next_scene: str | None = None,
                              next_first_frame_desc: str | None = None) -> str:
    transition = _clean_transition(transition)
    if not transition:
        return ""
    target = f"；下一镜场景：{next_scene.strip()}" if next_scene and next_scene.strip() else ""
    first_frame = (
        f"；下一镜首帧意向：{next_first_frame_desc.strip()[:80]}"
        if next_first_frame_desc and next_first_frame_desc.strip() else ""
    )
    return (
        f"本镜结尾转场：以「{transition}」连接下一镜{target}{first_frame}。"
        f"{_transition_hint(transition)}；最后约0.5到1秒执行转场，保留本镜动作结果，"
        "不要把下一场景完整拍成本镜内容。"
    )


def _scene_tail_transition_line(transition: str | None, next_scene: str | None = None,
                                next_first_frame_desc: str | None = None) -> str:
    transition = _clean_transition(transition)
    if not transition:
        return ""
    target = f"下一镜场景是「{next_scene.strip()}」" if next_scene and next_scene.strip() else "下一镜是新场景"
    first_frame = (
        f"，首帧意向是「{next_first_frame_desc.strip()[:70]}」"
        if next_first_frame_desc and next_first_frame_desc.strip() else ""
    )
    return (
        f"转场尾帧要求：本尾图需要为「{transition}」做收尾，{target}{first_frame}；"
        "这仍是一张静止尾帧，只表现渐暗、闪白、遮挡、甩镜运动模糊、叠化余韵或匹配剪辑呼应等可见视觉，不生成字幕文字。"
    )


class CompileError(Exception):
    pass


def normalize_video_args(prompt_text: str) -> str:
    """确保手写/覆盖 prompt 也使用固定 10s 视频生成参数。"""
    text = re.sub(r"\s--dur\s+\d+(?:\.\d+)?", "", prompt_text).strip()
    text = re.sub(r"\s--ratio\s+\S+", "", text).strip()
    if text:
        text += " --ratio 9:16"
    else:
        text = "--ratio 9:16"
    return f"{text} --dur {config.FIXED_VIDEO_DURATION_S}"


def _source_excerpt_line(shot: Shot, max_chars: int = SOURCE_EXCERPT_PROMPT_MAX) -> str:
    source_excerpt = (shot.source_excerpt or "").strip()
    if not source_excerpt:
        return ""
    if len(source_excerpt) > max_chars:
        source_excerpt = source_excerpt[:max_chars].rstrip() + "……"
    return f"{SOURCE_EXCERPT_MARKER}{source_excerpt}"


def _split_video_args(prompt_text: str) -> tuple[str, str]:
    normalized = normalize_video_args(prompt_text)
    args = f" --ratio 9:16 --dur {config.FIXED_VIDEO_DURATION_S}"
    if normalized.endswith(args):
        return normalized[:-len(args)].strip(), args
    return normalized.strip(), args


def ensure_source_excerpt_in_prompt(prompt_text: str, shot: Shot) -> str:
    """给旧版本/手写 prompt 补上原文兜底，保证真正发往 Seedance 的文本不漏。"""
    text = normalize_video_args(prompt_text)
    if SOURCE_EXCERPT_MARKER in text:
        return text

    body, args = _split_video_args(text)
    for max_chars in (SOURCE_EXCERPT_PROMPT_MAX, 180, 120, 80, 40):
        source_line = _source_excerpt_line(shot, max_chars)
        if not source_line:
            return text
        candidate_body = f"{body.rstrip('。')}。{source_line}" if body else source_line
        candidate = candidate_body + args
        if len(candidate) <= config.PROMPT_CHAR_LIMIT:
            return candidate

    source_line = _source_excerpt_line(shot, 24)
    if not source_line:
        return text
    max_body_len = config.PROMPT_CHAR_LIMIT - len(args) - len(source_line) - 1
    trimmed_body = body[:max(0, max_body_len)].rstrip("。；，,; ")
    candidate_body = f"{trimmed_body}。{source_line}" if trimmed_body else source_line
    return candidate_body + args


def compile_prompt(shot: Shot, bible: Bible, extra_negative: list[str] | None = None,
                   *, with_refs: bool = False, chained: bool = False,
                   prev_action: str | None = None, from_scene: bool = False,
                   critique: list[str] | None = None,
                   prev_tail_action: str | None = None,
                   with_last_frame: bool = False,
                   incoming_transition: str | None = None,
                   outgoing_transition: str | None = None,
                   next_scene: str | None = None,
                   next_first_frame_desc: str | None = None) -> str:
    bible_map = {c.name: c for c in bible.characters}
    missing = [n for n in shot.characters if n not in bible_map]
    if missing:
        raise CompileError(f"镜头 {shot.shot_no} 引用了圣经中不存在的角色：{missing}")
    if shot.duration_s != config.FIXED_VIDEO_DURATION_S:
        raise CompileError(f"镜头 {shot.shot_no} 时长 {shot.duration_s}s 不合法，视频生成统一要求 {config.FIXED_VIDEO_DURATION_S}s")

    anchors = [bible_map[n].appearance_canonical for n in shot.characters]
    negative = NEGATIVE_SUFFIX
    if extra_negative:
        negative += "，" + "，".join(x.strip() for x in extra_negative if x.strip())
    source_excerpt = _source_excerpt_line(shot)

    # 锚点串（画风/场景/角色）永不裁剪；超长时先裁剪负向词表，再裁剪动作修饰
    story_cues: list[str] = []
    if shot.narration:
        story_cues.append(f"旁白信息：{shot.narration}")
    if shot.dialogues:
        lines = "；".join(f"{d.speaker}说「{d.line}」" for d in shot.dialogues)
        story_cues.append(f"台词信息：{lines}")
    scene_hint = shot.scene_setting.strip()
    dur = config.FIXED_VIDEO_DURATION_S
    # 提示词结构遵循 Seedance 实践公式：主体 + 动作 + 景别运镜 + 环境 + 画风 + 质量约束。
    # 关键纠偏：单镜只表现“一个连贯流畅的动作”，不再要求 10s 内塞入多个小镜头/快速切景
    # （多动作快切是当前成片崩坏与画风漂移的主因）。剧情密度交给旁白承载，画面只演一件事。
    subject = "；".join(anchors)
    core_parts = [
        f"9:16 竖屏动态漫画短剧分镜，单镜约 {dur} 秒，只表现一个连贯流畅的动作过程，全程一镜到底，不要在一个镜头里快速切换多个不相关画面",
        f"画面主体：{subject}" if subject else "",
        f"镜头动作（只演这一件事，动作要有清晰的起势、过程与收势）：{shot.action_desc}",
        "动作遵循现实物理与人体运动规律、自然连续，禁止瞬移/穿模/肢体扭曲/物体凭空出现或消失；"
        "首帧与尾帧是同一机位、同一场景的同一个动作的开始与结束，两帧之间只做这一个动作的自然过渡，绝不切换场景或机位、不要让画面跳变或形变",
        "特效与光效服从剧情：日常对话与一般场景写实克制、不要满屏光效或能量粒子，仅在情绪高潮或力量爆发的镜头才用强烈特效，且不得遮挡人物面部表情",
        f"景别：{shot.shot_size}；运镜：{shot.camera_move}，镜头运动缓慢平稳",
        f"环境（弱化，仅作背景空间参考，不要抢人物主体）：{scene_hint}" if scene_hint else "",
        *story_cues,
        "把台词与（如有）旁白转化为人物可见的表情、口型、肢体动作与道具反应，不在画面上生成任何字幕文字",
        source_excerpt,
        # 画风锚点：全集逐字一致、显式禁止跨镜漂移（与具体画风无关，只强调统一）
        f"全片统一画风（每个镜头严格一致，禁止风格漂移）：{bible.world.visual_style_canonical}",
        QUALITY_SUFFIX,
        NO_BGM_SUFFIX,
    ]
    incoming_line = _incoming_transition_line(incoming_transition)
    if incoming_line:
        core_parts.insert(1, incoming_line)
    outgoing_line = _outgoing_transition_line(outgoing_transition, next_scene, next_first_frame_desc)
    if outgoing_line:
        core_parts.append(outgoing_line)
    # 跨镜连贯：本镜承接上一镜的结束状态，让拼接后上下句自然衔接（不是各拍各的）
    if prev_tail_action:
        core_parts.insert(3, f"承接上一镜：上一镜结束于「{prev_tail_action[:50]}」，本镜从这个状态自然延续，情绪与场面连贯，不要另起炉灶")
    # AI 评语返工：把上一版被指出的问题作为本次必须改正项，避免重复犯错
    if critique:
        core_parts.append("上一版视频存在以下问题，本次必须逐条改正、其余保持不变：" + "；".join(c.strip() for c in critique[:6] if c.strip()))
    if from_scene:
        # 首帧来自本镜“首图关键帧”；若同时给尾帧，则以本镜“尾图关键帧”为结束画面。
        if with_last_frame:
            core_parts.append(
                "以给定首帧（本镜首图关键帧）为起始画面，以给定尾帧（本镜尾图关键帧）为结束画面，"
                "在单镜头内自然完成上述动作，严格保持人物形象、服装、发型、场景布置、光影与画风一致，"
                "不要重新构图、不要改变风格、不要跳切")
        else:
            core_parts.append(
                "以给定首帧（本镜场景关键帧）为起始画面，让画面中的人物自然做出上述镜头动作，"
                "严格保持人物形象、服装、发型、场景布置、光影与画风和首帧完全一致，"
                "只做连贯的动作与镜头延展，不要重新构图、不要改变风格、不要跳切")
    elif chained:
        # 首帧来自上一镜预生成尾图；若同时给尾帧，则以本镜预生成尾图为结束画面。
        if with_last_frame:
            lead = ("以给定首帧（上一镜尾图关键帧）为起始画面，以给定尾帧（本镜尾图关键帧）为结束画面，"
                    "自然延续动作并完成上述镜头内容，严格保持人物形象、服装、发型、光影与画风一致，"
                    "不要重新构图、不要改变风格、不要跳切")
        else:
            lead = ("以给定首帧为起始画面自然延续动作，严格保持人物形象、服装、发型、光影与画风和首帧完全一致，"
                    "只做连贯的动作与镜头延展，不要重新构图、不要改变风格、不要跳切")
        if prev_action:
            lead += f"（上一镜结束于「{prev_action[:40]}」，本镜紧接其后）"
        core_parts.append(lead)
    if with_refs:
        core_parts.append("严格保持人物发型、服装、五官与画风和参考图完全一致")
    args = f" --ratio 9:16 --dur {config.FIXED_VIDEO_DURATION_S}"

    def assemble(neg: str) -> str:
        body = "。".join(p.strip().rstrip("。") for p in core_parts if p.strip())
        if neg:
            body += "。" + neg
        return body + args

    text = assemble(negative)
    if len(text) > config.PROMPT_CHAR_LIMIT:
        text = assemble(NEGATIVE_SUFFIX)
    if len(text) > config.PROMPT_CHAR_LIMIT:
        text = assemble("")
    if len(text) > config.PROMPT_CHAR_LIMIT:
        raise CompileError(
            f"镜头 {shot.shot_no} prompt 长度 {len(text)} 超过上限 {config.PROMPT_CHAR_LIMIT}，"
            f"且锚点串不可裁剪。请拆分为更细镜头，或在不丢失关键动作与场景锚点的前提下调整 action_desc/scene_setting")
    return text


# 场景关键帧（Seedream 静帧）负面词：静帧不需要“快速跳切”这类视频负面，但要禁文字/畸形/多人/换装漂移
SCENE_NEGATIVE = (
    "避免出现：真人实拍，照片写实质感，画面内任何文字/字幕/水印/logo/乱码伪字，多余人物，"
    "畸形手/多指缺指/手指粘连，肢体错位/关节扭曲，面部扭曲，五官崩坏，"
    "角色换脸换发型换服装/年龄体型漂移，名人长相，道具与手脱节，满屏光效遮挡面部，画风突变")
SCENE_QUALITY = (
    "竖屏 9:16 单帧定格画面，构图完整，人物五官清晰稳定、表情自然，手部与所持道具关系正常稳定，"
    "光影与色调统一，电影质感，高清")
# 角色不漂移 + 同镜两帧同机位 + 特效克制（与视频侧一致，三者是 10s 成片稳定的关键）
SCENE_CONSISTENCY = "人物形象严格遵循上方角色锚点串与参考图：同一张脸、同一发型、同一服装、同一年龄与体型，跨镜不漂移"
SCENE_SAME_FRAMING = "本帧与本镜另一张关键帧（首图/尾图）保持同一机位、同一构图、同一场景布置与光线方向，只有人物动作所处的瞬间不同，不要换机位或重新构图"
SCENE_EFFECT_RESTRAINT = "光效/特效服从剧情：日常场景克制写实、不要满屏光效或能量粒子，仅在情绪高潮或力量爆发瞬间才用强特效且不遮挡面部表情"


def compile_scene_prompt(shot: Shot, bible: Bible, *, kind: str = "tail",
                         outgoing_transition: str | None = None,
                         next_scene: str | None = None,
                         next_first_frame_desc: str | None = None) -> str:
    """编译“场景关键帧”图像生成 prompt（Seedream 用）：画风 + 场景 + 在场人物锚点 +
    本镜动作的【首图/尾图定格】。生成的图随后作为 Seedance 视频首尾帧。"""
    if kind not in ("head", "tail"):
        raise CompileError(f"未知关键帧类型：{kind}")
    bible_map = {c.name: c for c in bible.characters}
    missing = [n for n in shot.characters if n not in bible_map]
    if missing:
        raise CompileError(f"镜头 {shot.shot_no} 关键帧引用了圣经中不存在的角色：{missing}")
    anchors = "；".join(bible_map[n].appearance_canonical for n in shot.characters)
    scene_hint = shot.scene_setting.strip()
    # 优先用分镜给出的“首帧/尾帧画面描述”（两者明显不同）；缺失时退回 action_desc + 起势/收势框定
    ff = (shot.first_frame_desc or "").strip()
    lf = (shot.last_frame_desc or "").strip()
    if kind == "head":
        frame_desc = (f"画面定格在本镜【开始】的静止瞬间（动作尚未发生）：{ff}" if ff
                      else f"画面定格在本镜开始的瞬间（动作起势，尚未展开）：{shot.action_desc}")
    else:
        frame_desc = (f"画面定格在本镜【结束】的静止瞬间（动作已完成、结果清晰可见，与开始画面明显不同）：{lf}" if lf
                      else f"画面定格在本镜结束的瞬间（动作收势，动作结果清晰可见）：{shot.action_desc}")
    transition_frame_hint = (
        _scene_tail_transition_line(outgoing_transition, next_scene, next_first_frame_desc)
        if kind == "tail" else ""
    )
    parts = [
        f"统一画风：{bible.world.visual_style_canonical}",
        f"画面人物：{anchors}" if anchors else "",
        SCENE_CONSISTENCY if anchors else "",
        f"场景：{scene_hint}" if scene_hint else "",
        frame_desc,
        SCENE_SAME_FRAMING,
        SCENE_EFFECT_RESTRAINT,
        transition_frame_hint,
        f"景别：{shot.shot_size}",
        SCENE_QUALITY,
        SCENE_NEGATIVE,
    ]
    return "。".join(p.strip().rstrip("。") for p in parts if p.strip())


def scene_candidate_count(shot: Shot) -> int:
    """按分镜复杂度自适应决定场景关键帧候选数量（1~3）：人物多/动作描述长/特殊景别 → 多候选。"""
    k = 1
    if len(shot.characters) >= 2:
        k += 1
    if len((shot.action_desc or "")) >= 110 or shot.shot_size in ("远景", "全景"):
        k += 1
    return max(1, min(k, 3))


def idem_key(prompt_text: str, image_urls: list[tuple[str, str]] | None = None) -> str:
    payload = prompt_text + "|" + "|".join(f"{u}#{r}" for u, r in (image_urls or []))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def shot_cost_cny(duration_s: int) -> float:
    return round(duration_s * config.VIDEO_PRICE_PER_SECOND, 2)
