"""Prompt 编译器：分镜脚本 → Seedance prompt。确定性代码，非 LLM（PRD §4.4）。
一致性核心：画风串/场景串/角色锚点串逐字拼接，LLM 永不改写。
M0 实测网关无同步参数校验，因此本编译器是参数合法性的唯一防线。
"""
from __future__ import annotations

import hashlib
import re

from app import config
from app.schemas import Bible, Shot

NEGATIVE_SUFFIX = "画面中避免出现：真人，照片质感，文字，水印，字幕，logo，多余的人，畸形手指，面部扭曲，名人长相，画面割裂"


class CompileError(Exception):
    pass


def normalize_video_args(prompt_text: str) -> str:
    """确保手写/覆盖 prompt 也使用固定 10s 视频生成参数。"""
    text = re.sub(r"\s--dur\s+\d+(?:\.\d+)?", "", prompt_text).strip()
    if "--ratio" not in text:
        text += " --ratio 9:16"
    return f"{text} --dur {config.FIXED_VIDEO_DURATION_S}"


def compile_prompt(shot: Shot, bible: Bible, extra_negative: list[str] | None = None,
                   *, with_refs: bool = False, chained: bool = False,
                   prev_action: str | None = None) -> str:
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

    # 锚点串（画风/场景/角色）永不裁剪；超长时先裁剪负向词表，再裁剪动作修饰
    story_cues: list[str] = []
    if shot.narration:
        story_cues.append(f"旁白信息：{shot.narration}")
    if shot.dialogues:
        lines = "；".join(f"{d.speaker}说「{d.line}」" for d in shot.dialogues)
        story_cues.append(f"台词信息：{lines}")
    scene_hint = shot.scene_setting.strip()
    core_parts = [
        f"固定{config.FIXED_VIDEO_DURATION_S}秒竖屏漫剧视频段，人物和剧情优先，节奏紧凑，在10秒内尽可能塞入更多连续小镜头、动作节点和剧情信息，允许快速切景、推近、特写、道具插入与角色反应连续发生",
        bible.world.visual_style_canonical,
        f"{shot.shot_size}，{shot.camera_move}镜头",
        *anchors,
        shot.action_desc,
        *story_cues,
        f"弱背景提示：{scene_hint}，场景只作空间参考，不要抢人物和剧情主体" if scene_hint else "",
        "将旁白和台词信息转化为可见动作、表情、道具和构图变化，不生成字幕文字",
    ]
    if chained:
        # 首帧来自上一镜尾帧：让文本与视觉信号同向，避免模型把首帧当成要推翻的画面
        lead = f"画面从给定首帧自然延续{('：上一镜头结束于「' + prev_action[:40] + '」') if prev_action else ''}，动作连贯不跳切"
        core_parts.append(lead)
    if with_refs:
        core_parts.append("严格保持角色发型、服装、五官与参考图一致")
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


def idem_key(prompt_text: str, image_urls: list[tuple[str, str]] | None = None) -> str:
    payload = prompt_text + "|" + "|".join(f"{u}#{r}" for u, r in (image_urls or []))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def shot_cost_cny(duration_s: int) -> float:
    return round(duration_s * config.VIDEO_PRICE_PER_SECOND, 2)
