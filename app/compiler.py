"""Prompt 编译器：分镜脚本 → Seedance prompt。确定性代码，非 LLM（PRD §4.4）。
一致性核心：画风串/场景串/角色锚点串逐字拼接，LLM 永不改写。
M0 实测网关无同步参数校验，因此本编译器是参数合法性的唯一防线。
"""
from __future__ import annotations

import hashlib
import re

from app import config
from app.character_policy import functional_extra_anchor, is_functional_extra
from app.renderability import strip_overdetail_terms
from app.schemas import Bible, Shot

# 全知视角的结尾悬念钩旁白（"可他不知道…/殊不知…/然而…"）念在台词【之后】；
# 其余旁白（情境画外音、人物内心OS、人群声）都是先给情境、人物再开口反应，必须念在台词【之前】。
_NARRATION_AFTER_MARKERS = (
    "可他", "可她", "可这", "可此时", "殊不知", "却不知", "然而", "但他不知", "但她不知",
    "但谁也", "而此刻", "只是此时", "谁也没想到", "没有人注意到", "没人知道", "此时的他", "此时的她",
)


def narration_after_dialogue(narration: str) -> bool:
    """该镜旁白是否应排在台词【之后】：仅全知结尾悬念钩旁白（"可他不知道…/殊不知…"）放最后，其余放台词前。"""
    n = (narration or "").lstrip(" 　")
    return any(n.startswith(m) for m in _NARRATION_AFTER_MARKERS)

NEGATIVE_SUFFIX = (
    "避免出现：真人实拍，照片写实质感，画面内任何文字/字幕/水印/logo/乱码伪字，多余人物，"
    "同一角色重复出现/分身/双重人物/画面里多出一个一模一样的人，前景出现贴满画面的巨大人物剪影遮挡主体，"
    "畸形手/多指缺指/手指粘连，肢体错位/穿模/关节扭曲，面部扭曲，五官崩坏/中途换脸，"
    "角色换发型换服装/年龄体型漂移，名人长相，道具凭空出现或消失/与手脱节，"
    "角色凭空出现或消失，动作违反重力与人体运动规律/瞬移，画面变形 morphing/渐变扭曲，镜头中途无故切场景或跳切，"
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


class CompileError(ValueError):
    """可在生成前纠正的 prompt 编译错误。

    继承 ValueError 让单镜生成路由按 409 业务冲突返回，而不是被全局异常处理器
    误报为 500 系统内部错误。
    """
    pass


def clip_duration_value(value: int | float | str | None) -> int:
    """把人工/历史输入收敛到供应商支持的 5~10 秒整数区间。"""
    try:
        duration = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return config.DEFAULT_VIDEO_DURATION_S
    return min(max(duration, config.VIDEO_DURATION_MIN_S), config.VIDEO_DURATION_MAX_S)


def clip_duration(shot: Shot) -> int:
    """返回供应商合同支持的镜头时长。"""
    return clip_duration_value(getattr(shot, "duration_s", None))


def normalize_video_args(prompt_text: str, duration: int | None = None) -> str:
    """移除历史参数并写入当前固定比例与模型选择的合法时长。"""
    if duration is None:
        matches = re.findall(r"(?:^|\s)--dur\s+(\d+(?:\.\d+)?)", prompt_text)
        duration = matches[-1] if matches else config.DEFAULT_VIDEO_DURATION_S
    dur = clip_duration_value(duration)
    text = re.sub(r"(?:^|\s)--dur\s+\d+(?:\.\d+)?", "", prompt_text).strip()
    text = re.sub(r"(?:^|\s)--ratio\s+\S+", "", text).strip()
    if text:
        text += " --ratio 9:16"
    else:
        text = "--ratio 9:16"
    return f"{text} --dur {dur}"


def _source_excerpt_line(shot: Shot, max_chars: int = SOURCE_EXCERPT_PROMPT_MAX) -> str:
    source_excerpt = (shot.source_excerpt or "").strip()
    if not source_excerpt:
        return ""
    if len(source_excerpt) > max_chars:
        source_excerpt = source_excerpt[:max_chars].rstrip() + "……"
    return f"{SOURCE_EXCERPT_MARKER}{source_excerpt}"


def _split_video_args(prompt_text: str, duration: int | None = None) -> tuple[str, str]:
    normalized = normalize_video_args(prompt_text, duration)
    dur_match = re.search(r"--dur\s+(\d+)$", normalized)
    dur = int(dur_match.group(1)) if dur_match else config.DEFAULT_VIDEO_DURATION_S
    args = f" --ratio 9:16 --dur {dur}"
    if normalized.endswith(args):
        return normalized[:-len(args)].strip(), args
    return normalized.strip(), args


def _replace_age(match: re.Match) -> str:
    suffix = match.group(2) or ""
    if suffix in {"少女", "女孩"}:
        return "年轻女性角色"
    if suffix in {"男孩", "少年", "少男"}:
        return "少年感年轻角色"
    return "年轻角色"


# 通用安全降级（题材无关，首发即生效）：脏话软化 + 未成年/年龄安全归一。
# 这些不改变场景与情绪语义，只规避平台审核与未成年风险。
_ALWAYS_REPLACEMENTS = (
    ("我草", "可恶"),
    ("卧槽", "可恶"),
    ("我操", "可恶"),
    ("他妈的", "可恶"),
    ("他妈", "可恶"),
    ("妈的", "可恶"),
    ("该死", "可恶"),
    ("骂了一句", "低声抱怨一句"),
)
# 题材专用的措辞降级（修真/玄幻语境的场景与情绪改写）。这些会篡改画面场景（卧室→修炼静室）
# 与人物情绪（愤怒→不甘），对都市/言情/悬疑等题材是降质的——因此【默认不启用】，
# 只在平台已返回 InputTextSensitiveContentDetected 后的 aggressive 重提里启用。
_AGGRESSIVE_REPLACEMENTS = (
    ("床榻上", "修炼蒲团上"),
    ("床榻", "修炼蒲团"),
    ("床上", "室内蒲团上"),
    ("卧室", "修炼静室"),
    ("口鼻钻入体内", "从周围缓缓汇聚并融入经脉"),
    ("钻入体内", "融入经脉"),
    ("涌入体内", "汇入经脉"),
    ("进入体内", "融入经脉"),
    ("吸收殆尽", "悄然吸收"),
    ("死死捏紧拳头", "用力握拳"),
    ("死死攥紧拳头", "用力握拳"),
    ("死死", "用力"),
    ("愤怒地", "神情不甘地"),
    ("愤怒", "不甘"),
    ("暴怒", "强烈不甘"),
    ("诡异", "神秘"),
    ("邪异", "神秘"),
)


def _rewrite_sensitive_terms(text: str, *, aggressive: bool = False) -> str:
    out = text
    for old, new in _ALWAYS_REPLACEMENTS:
        out = out.replace(old, new)
    if aggressive:
        for old, new in _AGGRESSIVE_REPLACEMENTS:
            out = out.replace(old, new)
    # 年龄/未成年安全归一始终生效（与题材无关的合规护栏）
    out = re.sub(r"(?:\d{1,3}|[一二两三四五六七八九十]{1,4})岁(清秀|稚嫩|年少)?(少年|少女|男孩|女孩|少男)?", _replace_age, out)
    out = re.sub(r"未成年(?:人)?", "年轻角色", out)
    out = re.sub(r"草([！!。,.，、？?])", r"可恶\1", out)
    return out


def sanitize_seedance_prompt(prompt_text: str, *, aggressive: bool = False,
                             extra_terms: tuple[tuple[str, str], ...] | None = None) -> str:
    """降低 Seedance 文本安全误拦截概率。

    普通模式只做确定性措辞降级；aggressive=True 用于平台已经返回
    InputTextSensitiveContentDetected 后的自动重提，会移除原文兜底和露骨台词原句。
    extra_terms：额外的字面替换（如版权角色专名→中性代称），用于版权限制后的重提。
    """
    body, args = _split_video_args(prompt_text)
    if extra_terms:
        for old, new in extra_terms:
            if old:
                body = body.replace(old, new)
    body = _rewrite_sensitive_terms(body, aggressive=aggressive)
    body = strip_overdetail_terms(body)
    if aggressive:
        body = re.sub(rf"{re.escape(SOURCE_EXCERPT_MARKER)}[^。；\n]*[。；\n]?", "", body)
        body = re.sub(
            r"台词信息：[^。\n]{0,220}",
            "台词信息：角色以短促口型和压抑情绪表达懊恼，不生成字幕文字",
            body,
        )
        body = re.sub(r"[^。；\n]{0,18}低声[^。；\n]{0,80}可恶[^。；\n]{0,30}", "角色低声表达懊恼", body)
    # 保留段落换行（新 Seedance 分段协议）；仅压缩行内空白与多余空行
    if "[" in body and "]" in body and "\n" in body:
        lines = []
        for line in body.splitlines():
            lines.append(re.sub(r"[ \t]+", " ", line).strip())
        body = "\n".join(lines)
        body = re.sub(r"\n{3,}", "\n\n", body).strip(" \n。；")
    else:
        body = re.sub(r"\s+", " ", body).strip(" 。；")
    return f"{body}{args}" if body else args.strip()


def ensure_source_excerpt_in_prompt(prompt_text: str, shot: Shot) -> str:
    """兼容旧调用点：PRD 禁止把原文章节送入 Seedance，因此只做规范化与消毒，不再注入原文。"""
    text = normalize_video_args(prompt_text, shot.duration_s)
    # 若历史 prompt 仍含原文标记，主动剥离
    if SOURCE_EXCERPT_MARKER in text:
        body, args = _split_video_args(text, shot.duration_s)
        body = re.sub(rf"{re.escape(SOURCE_EXCERPT_MARKER)}[^。；\n]*[。；\n]?", "", body)
        body = re.sub(r"\s+", " ", body).strip(" 。；")
        text = f"{body}{args}" if body else args.strip()
    return sanitize_seedance_prompt(text)


def _framing_scale_hint(shot_size: str) -> str:
    """景别 → 人物在画面中的尺度/数量锚定，消解“景别说远景、参考图却是满屏全身像”导致的尺度打架
    （模型两种尺度都画 → 前景巨人 + 远景小人 = 同一角色两份/穿模）。"""
    s = (shot_size or "").strip()
    if "远景" in s:  # 含大远景/远景
        return ("人物在画面中占比小、完整置于环境空间内（全身可见但绝不顶满画面），"
                "画面里只有这一个主体，严禁出现贴满画面的巨大人物")
    if "全景" in s:
        return "人物全身完整入画、约占画面高度三分之二，处于场景空间中，单一主体不重复"
    if "中景" in s:
        return "取人物腰部以上半身，单一主体，人物比例自然、不顶满画面"
    return ""


# 接触类动作：人物与人物/道具发生真实肢体接触或近距交递，侧面机位最易读清接触点。
_CONTACT_ACTION_MARKERS = (
    "触碰", "触摸", "抚摸", "按压", "按住", "按下", "按上", "贴上", "贴住", "贴紧",
    "拿取", "拿起", "握住", "抓住", "抓牢", "递出", "递给", "递过", "接过", "接住",
    "挥击", "挥砍", "击打", "击中", "格挡", "挡住", "搀扶", "扶住", "扶着", "搀着",
    "拍打", "拍肩", "拍了", "推开", "推住", "拉住", "拉着", "拖住", "抱住", "抱紧",
    "搂住", "握手", "搭手", "搭肩", "抵住", "顶住", "压住", "捂住", "托住", "托着",
    "拔出", "插入", "刺向", "砍向", "捅向", "碰到", "摸到", "摸着", "摸上",
)
_SIDE_VIEW_MARKERS = ("侧面", "侧视", "侧拍", "侧机位", "侧身机位", "侧面机位", "侧面视角")
_HEIGHT_DIFF_MARKERS = (
    "身高差", "一高一低", "高他一头", "高她一头", "高出一头", "矮半头", "矮一头",
    "明显更高", "明显更矮", "比.*高", "比.*矮", "仰头看", "仰视对方", "俯视对方",
    "俯身看", "巨汉", "娇小", "矮小", "孩童", "幼童", "小孩", "儿童", "孩子气身材",
)
_HEIGHT_DIFF_RE = re.compile("|".join(_HEIGHT_DIFF_MARKERS))


def _shot_visual_text(shot: Shot) -> str:
    """汇总本镜可用于检测接触/身高差的视觉描述文本。"""
    parts = [
        shot.primary_action or "",
        shot.action_desc or "",
        shot.state_in or "",
        shot.state_out or "",
        shot.first_frame_desc or "",
        shot.last_frame_desc or "",
        shot.spatial_anchor or "",
    ]
    return "。".join(p.strip() for p in parts if p and p.strip())


def has_contact_action(shot: Shot) -> bool:
    """本镜主动作是否含人物与人物/道具的真实接触互动。"""
    text = _shot_visual_text(shot)
    return any(m in text for m in _CONTACT_ACTION_MARKERS)


def has_explicit_height_difference(shot: Shot, bible: Bible | None = None) -> bool:
    """提示词/外观锚点是否已明确写出身高差（有则不强行同身高）。"""
    chunks = [_shot_visual_text(shot)]
    if bible is not None:
        bible_map = {c.name: c for c in bible.characters}
        for name in shot.characters or []:
            ch = bible_map.get(name)
            if ch and (ch.appearance_canonical or "").strip():
                chunks.append(ch.appearance_canonical)
    text = "。".join(chunks)
    return bool(_HEIGHT_DIFF_RE.search(text))


def _resolve_camera_angle(shot: Shot) -> str:
    """接触类动作默认侧面视角；已显式侧面则保留，非接触沿用原机位角。"""
    current = (shot.camera_angle or "").strip()
    if has_contact_action(shot):
        if current and any(m in current for m in _SIDE_VIEW_MARKERS):
            return current
        # 接触动作：强制侧面，便于看清肢体与接触点；覆盖空值/平视等正面默认
        return "侧面"
    return current or "平视"


def _equal_height_hint(shot: Shot, bible: Bible | None = None) -> str:
    """多人物同框：无明示身高差时锁定站立同高/齐眼线，避免随机一高一低。"""
    if len(shot.characters or []) < 2:
        return ""
    if has_explicit_height_difference(shot, bible):
        return ""
    return (
        "同框人物站立身高与眼线尽量齐平、体型尺度协调一致；"
        "除非剧情已写明身高差，禁止把角色画成随意一高一低"
    )



def _compile_text_policy(shot: Shot) -> str:
    required = getattr(shot, "required_text", None)
    if required is not None and (getattr(required, "exact_text", None) or "").strip():
        exact = required.exact_text.strip()
        surface = (required.surface or "画面指定表面").strip()
        style = (required.style or "清晰可读").strip()
        start = getattr(required, "appear_start_s", 0.0) or 0.0
        until = getattr(required, "stable_until_s", None)
        until_s = f"{until}s" if until is not None else "镜头结束"
        return (
            f"仅在{surface}上于 {start}s 起稳定显示指定文字「{exact}」，保持到 {until_s}；"
            f"文字样式：{style}。禁止出现任何其他文字、字幕、标志、水印或乱码。"
        )
    return "画面中不出现任何文字、字幕、标志或水印。"


def _compile_audio_timeline(shot: Shot, voice_bible: list | None = None) -> str:
    from app.continuity import ensure_audio_timeline
    ensure_audio_timeline(shot, voice_bible)
    lines: list[str] = []
    for item in shot.audio_timeline:
        span = f"{item.start_s:g}–{item.end_s:g} 秒"
        if item.type == "ambient_sound":
            lines.append(f"{span}：{item.text or '自然环境声'}")
            continue
        speaker = item.speaker_id or "未知"
        voice = (item.voice_canonical or "").strip()
        voice_bit = f"，声音特征：{voice}" if voice else ""
        lip = "需要对口型" if item.lip_sync else "不在画面中或无需口型"
        emotion = item.emotion or "平静"
        if item.type == "narration":
            lines.append(
                f"{span}：旁白用独立叙述者嗓音念「{item.text}」{voice_bit}；不对口型，不要让画面人物说这段。"
            )
        elif item.type == "offscreen_voice":
            lines.append(
                f"{span}：{speaker}在画外以{emotion}语气说「{item.text}」{voice_bit}；{lip}；"
                "保持该角色声音身份，不得改成通用旁白。"
            )
        else:
            lines.append(
                f"{span}：画面中的{speaker}以{emotion}语气开口说「{item.text}」{voice_bit}；{lip}。"
            )
    lines.append("所有对白和必要声音必须由本条视频直接生成并在片段结束前完整结束；只生成指定说话人和指定台词。")
    return "\n".join(lines)


def _compile_reference_roles(shot: Shot, *, continuity_mode: str, with_refs: bool,
                             chained: bool) -> str:
    from app.continuity import reference_role_plan, uses_previous_tail_frame
    roles = reference_role_plan(shot, continuity_mode=continuity_mode)
    lines: list[str] = []
    if uses_previous_tail_frame(continuity_mode) and (chained or "start_state_reference" in roles):
        lines.append(
            "参考图 A 是上一镜真实结束帧，也是本视频 0.0 秒的强制起始状态。"
            "保持其中人物数量、身份、服装、位置、朝向、手势、道具状态和光线；"
            "从该状态继续当前动作，不重复上一镜已经完成的动作。"
        )
    if continuity_mode in {"same_scene_cut", "reaction_cut", "reverse_angle", "insert_detail"}:
        lines.append(
            "场景参考仅用于建筑、材质、光线和色调一致；当前镜头需要重新构图。"
            "人物参考仅用于身份和服装一致，不复制参考图中的姿势与画面布局。"
        )
    elif continuity_mode == "scene_change":
        lines.append("使用新场景标准参考建立稳定开场；不继承上一镜环境构图。")
    if with_refs and not lines:
        lines.append("只按参考素材角色使用图片；不得把参考图中的构图、额外人物或上一镜主体复制到当前镜头。")
    if not lines:
        lines.append("无参考图时仍保持人物身份与场景不变量一致。")
    # 显式角色映射摘要
    if roles:
        lines.append("参考角色映射：" + "、".join(roles) + "。")
    lines.append("只按上述角色使用参考素材；不得把参考图中的构图、额外人物或上一镜主体复制到当前镜头。")
    return "\n".join(lines)


def _compile_negative_constraints(shot: Shot, extra_negative: list[str] | None,
                                  continuity_mode: str, *,
                                  bible: Bible | None = None) -> str:
    from app.continuity import effective_characters_visible, uses_previous_tail_frame
    parts = [
        "不要重演前序剧情",
        "不要提前表演下一镜内容",
        "不要生成字幕、乱码或水印" if not (shot.required_text and (shot.required_text.exact_text or "").strip())
        else f"除「{(shot.required_text.exact_text or '').strip()}」外不要出现任何其他文字",
        "不要出现镜头内未指定的人物",
        "不要复制人物或生成分身",
    ]
    for item in shot.do_not_repeat or []:
        text = (item or "").strip()
        # do_not_repeat may still contain historical internal IDs.  They are
        # useful for ledger deduplication but meaningless to Seedance, so only
        # forward resolved natural-language constraints.
        if text and re.search(r"[\u3400-\u9fff]", text):
            parts.append(f"不要重复：{text}")
    for tag in shot.risk_tags or []:
        if tag == "crowd_consistency":
            parts.append("人群数量与朝向保持稳定，不要随机增减人脸")
        elif tag == "offscreen_voice":
            parts.append("画外说话人不要入画，也不要用旁白腔替代")
    visible = effective_characters_visible(shot)
    if visible:
        parts.append(f"画面可见角色仅限：{'、'.join(visible)}")
    if not uses_previous_tail_frame(continuity_mode):
        parts.append("不要沿用上一镜完整构图或主体尾帧姿势")
    if has_contact_action(shot):
        parts.append("接触动作禁止正面摆拍、禁止手悬空未触及目标")
    if len(shot.characters or []) >= 2 and not has_explicit_height_difference(shot, bible):
        parts.append("禁止同框人物随意一高一低或眼线错位")
    if extra_negative:
        parts.extend(x.strip() for x in extra_negative if x and x.strip())
    # 保留关键安全负面词（精简，避免与条件文字策略冲突）
    parts.append(
        "避免真人实拍、畸形手、肢体错位、面部崩坏、角色换装漂移、动作瞬移、画风突变、满屏光效遮挡面部"
    )
    return "；".join(dict.fromkeys(parts))


def compile_prompt(shot: Shot, bible: Bible, extra_negative: list[str] | None = None,
                   *, with_refs: bool = False, chained: bool = False,
                   prev_action: str | None = None, from_scene: bool = False,
                   critique: list[str] | None = None,
                   prev_tail_action: str | None = None,
                   with_last_frame: bool = False,
                   incoming_transition: str | None = None,
                   outgoing_transition: str | None = None,
                   next_scene: str | None = None,
                   next_first_frame_desc: str | None = None,
                   continuity_mode: str | None = None,
                   prev_state_out: str | None = None,
                   voice_bible: list | None = None,
                   visual_style: str | None = None,
                   aspect_ratio: str = "9:16") -> str:
    """编译 Seedance 最终提示词（PRD §11）：最小完备、固定段落、禁止原文/前镜完整动作/未来剧情。"""
    from app.continuity import (
        derive_continuity_mode,
        effective_primary_action,
        effective_state_in,
        ensure_audio_timeline,
        planned_state_out,
        reference_role_plan,
        sync_shot_continuity_fields,
        uses_previous_tail_frame,
    )
    from app.schemas import PROMPT_CONTRACT_VERSION

    bible_map = {c.name: c for c in bible.characters}
    missing = [
        name for name in shot.characters
        if name not in bible_map and not is_functional_extra(name)
    ]
    if missing:
        raise CompileError(
            f"镜头 {shot.shot_no} 引用了既不在角色圣经、也不是功能性路人的角色：{missing}"
        )
    if shot.duration_s not in config.ALLOWED_DURATIONS:
        raise CompileError(
            f"镜头 {shot.shot_no} 时长 {shot.duration_s}s 不合法，视频生成时长必须为 "
            f"{config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S}s 的整数")

    sync_shot_continuity_fields(shot)
    mode = (continuity_mode or derive_continuity_mode(shot)).strip()
    if mode not in {"action_continuation", "same_scene_cut", "reaction_cut",
                    "reverse_angle", "insert_detail", "scene_change"}:
        mode = derive_continuity_mode(shot)
    shot.continuity_mode = mode
    shot.prompt_contract_version = PROMPT_CONTRACT_VERSION
    ensure_audio_timeline(shot, voice_bible)
    shot.reference_roles = reference_role_plan(shot, continuity_mode=mode)

    shot_dur = clip_duration(shot)
    state_in = effective_state_in(shot)
    if uses_previous_tail_frame(mode) and (prev_state_out or "").strip():
        # 连续动作：以实际/计划尾状态为强制起点（不使用上一镜完整 action_desc）
        state_in = prev_state_out.strip()
        shot.state_in = state_in
    state_out = planned_state_out(shot)
    primary = effective_primary_action(shot)
    if not state_in or not primary or not state_out:
        raise CompileError(
            f"镜头 {shot.shot_no} 缺少 state_in/primary_action/state_out，无法编译最小完备提示词"
        )

    style = (visual_style or bible.world.visual_style_canonical or "写实国风玄幻动画").strip()
    scale_hint = _framing_scale_hint(shot.shot_size)
    camera_angle = _resolve_camera_angle(shot)
    # 回写解析后的机位角，便于下游审计/重试与关键帧对齐
    shot.camera_angle = camera_angle
    camera_line = (
        f"{shot.shot_size}；{camera_angle}；{shot.camera_move}"
        + (f"。{scale_hint}" if scale_hint else "")
    )
    if has_contact_action(shot):
        camera_line += (
            "。接触类动作必须侧面机位拍摄，清楚展现肢体与接触点、人物与对象的空间关系，"
            "禁止正面端站摆拍"
        )
    spatial = (shot.spatial_anchor or shot.scene_setting or "").strip()
    if spatial:
        camera_line += f"。保持空间轴线和人物方位：{spatial}"

    # 角色/场景锚点（短）
    anchors = []
    for name in shot.characters:
        if name in bible_map:
            anchors.append(f"{name}：{bible_map[name].appearance_canonical}")
        else:
            anchors.append(f"{name}：{functional_extra_anchor(name)}")
    character_anchor = "；".join(anchors[:4]) if anchors else "保持人物身份服装一致"
    scene_anchor = f"场景：{shot.scene_setting}" if shot.scene_setting else ""
    prop_anchor = ""
    if shot.required_text and (shot.required_text.surface or "").strip():
        prop_anchor = f"文字承载面：{shot.required_text.surface.strip()}"
    height_hint = _equal_height_hint(shot, bible)

    # 转场：仅描述本镜可完成的视觉出口/入口，不注入下一镜详细剧情
    transition_bits: list[str] = []
    if incoming_transition and _clean_transition(incoming_transition):
        transition_bits.append(
            f"本镜开头以「{_clean_transition(incoming_transition)}」进入并尽快落稳到起始状态；"
            "不要误以为仍在上一地点。"
        )
    if outgoing_transition and _clean_transition(outgoing_transition):
        # 不写入 next_first_frame_desc / 下一镜详细内容（PRD 禁止未来剧情）
        target = f"，为切换到「{next_scene.strip()}」留出视觉出口" if next_scene and next_scene.strip() else ""
        transition_bits.append(
            f"本镜结尾以「{_clean_transition(outgoing_transition)}」收束{target}；"
            f"{_transition_hint(_clean_transition(outgoing_transition))}；"
            "不要把下一场景完整拍成本镜内容。"
        )

    format_block = (
        f"生成 {shot_dur} 秒、{aspect_ratio}、{style} 的完整可直接采用视频。"
        "不得依赖后期裁切、配音、字幕叠加或音效补充。"
        "全程不要任何背景音乐或 BGM；声音只保留指定人声与必要环境音。"
    )
    reference_block = _compile_reference_roles(
        shot, continuity_mode=mode, with_refs=with_refs,
        chained=chained or uses_previous_tail_frame(mode),
    )
    # 兼容旧 chained/from_scene 调用：仅 action_continuation 才强调尾帧起点
    if uses_previous_tail_frame(mode) and (chained or from_scene or with_last_frame):
        if "强制起始状态" not in reference_block:
            reference_block = (
                "参考图 A 是上一镜真实结束帧，也是本视频 0.0 秒的强制起始状态。\n"
                + reference_block
            )

    action_block = primary
    if transition_bits:
        action_block = primary + "。" + "".join(transition_bits)
    action_block += "。只完成这一项主要动作，不重演前序剧情，不提前表演下一镜内容。"

    # 明确忽略 prev_action / prev_tail_action 中的完整动作描述（API 兼容保留参数）
    _ = prev_action
    _ = prev_tail_action

    audio_block = _compile_audio_timeline(shot, voice_bible)
    text_block = _compile_text_policy(shot)
    consistency_parts = [
        character_anchor, scene_anchor, prop_anchor, height_hint, f"全片统一画风：{style}",
    ]
    consistency_block = "\n".join(p for p in consistency_parts if p)
    negative_block = _compile_negative_constraints(shot, extra_negative, mode, bible=bible)
    if critique:
        negative_block += "；上一版必须改正：" + "；".join(
            c.strip() for c in critique[:6] if c and c.strip()
        )

    # 固定段落顺序（PRD §11.1）；超长时按优先级压缩，不得截断台词/文字/首尾状态
    sections: list[tuple[str, str, int]] = [
        ("FORMAT", format_block, 5),
        ("REFERENCE ROLES", reference_block, 3),
        ("START STATE | 0.0s", state_in, 1),
        ("ONE CURRENT ACTION", action_block, 1),
        (f"END STATE | {shot_dur}.0s", state_out + "。最后状态稳定、清楚、可作为下一镜的叙事依据。", 1),
        ("CAMERA", camera_line, 4),
        ("AUDIO TIMELINE", audio_block, 2),
        ("ON-SCREEN TEXT", text_block, 2),
        ("CONSISTENCY", consistency_block, 4),
        ("DO NOT", negative_block, 5),
    ]

    args = f" --ratio {aspect_ratio} --dur {shot_dur}"

    def render(active: list[tuple[str, str, int]]) -> str:
        body = "\n\n".join(f"[{title}]\n{content.strip()}" for title, content, _ in active if content.strip())
        return body + args

    active = list(sections)
    text = render(active)

    def fits(t: str) -> bool:
        return len(t) <= config.PROMPT_CHAR_LIMIT

    # 压缩策略：先压低优先级段落（通用风格/重复锚点），永不删除优先级 1/2
    compact_map = {
        "CONSISTENCY": "保持人物身份服装与场景材质光线一致。",
        "REFERENCE ROLES": (
            "仅按角色映射使用参考图；连续动作才把尾帧当 0 秒起点，其余模式重新构图。"
            if uses_previous_tail_frame(mode) else
            "场景/人物参考只锁材质与身份，重新构图，不复制姿势布局。"
        ),
        "FORMAT": f"生成 {shot_dur} 秒、{aspect_ratio}、{style} 完整可直接采用视频；禁止后期。",
        "DO NOT": "；".join(negative_block.split("；")[:6]),
        "CAMERA": (
            f"{shot.shot_size}；{camera_angle}；{shot.camera_move}"
            + ("；接触动作侧面机位" if has_contact_action(shot) else "")
        ),
    }
    for priority in (5, 4, 3):
        if fits(text):
            break
        for idx, (title, content, pri) in enumerate(active):
            if pri != priority:
                continue
            key = title.split("|")[0].strip() if "|" in title else title
            # FORMAT / DO NOT / REFERENCE / CONSISTENCY / CAMERA
            for cand_key, compact in compact_map.items():
                if title.startswith(cand_key) or key == cand_key:
                    active[idx] = (title, compact, pri)
                    break
            text = render(active)

    # 仍超长：压缩音频时间线描述（保留台词原文）
    if not fits(text):
        short_audio_lines = []
        for item in shot.audio_timeline:
            if item.type == "ambient_sound":
                short_audio_lines.append(f"{item.start_s:g}–{item.end_s:g}s 环境声")
            else:
                short_audio_lines.append(
                    f"{item.start_s:g}–{item.end_s:g}s {item.speaker_id or item.type}「{item.text}」"
                )
        for idx, (title, content, pri) in enumerate(active):
            if title == "AUDIO TIMELINE":
                active[idx] = (title, "；".join(short_audio_lines), pri)
        text = render(active)

    if not fits(text):
        # 镜头任务过载：不得截断必填字段
        raise CompileError(
            f"镜头 {shot.shot_no} 必填提示词段落总长 {len(text)} 超过上限 {config.PROMPT_CHAR_LIMIT}；"
            "说明镜头任务过载，请回到分镜阶段拆分，不得截断台词/文字/首尾状态或否定条件"
        )

    # 最终不得含原文标记
    if SOURCE_EXCERPT_MARKER in text:
        text = re.sub(rf"{re.escape(SOURCE_EXCERPT_MARKER)}[^。；\n]*[。；\n]?", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text.endswith(args):
            text = text.rstrip() + args

    return sanitize_seedance_prompt(text)



# 场景关键帧（Seedream 静帧）负面词：静帧不需要“快速跳切”这类视频负面，但要禁文字/畸形/多人/换装漂移
SCENE_NEGATIVE = (
    "避免出现：真人实拍，照片写实质感，画面内任何文字/字幕/水印/logo/乱码伪字，多余人物，"
    "畸形手/多指缺指/手指粘连，肢体错位/关节扭曲，面部扭曲，五官崩坏，"
    "角色换脸换发型换服装/年龄体型漂移，名人长相，道具与手脱节，满屏光效遮挡面部，画风突变")
SCENE_QUALITY = (
    "竖屏 9:16 单帧定格画面，构图完整，人物五官清晰稳定、表情自然，手部与所持道具关系正常稳定，"
    "光影与色调统一，电影质感，高清")
# 角色不漂移 + 同镜两帧同机位 + 特效克制（与视频侧一致，三者是 5~10s 成片稳定的关键）
SCENE_CONSISTENCY = (
    "人物形象严格遵循上方角色锚点串与参考图：同一张脸、同一发型、同一服装、同一年龄与体型，跨镜不漂移；"
    "同框多人物默认站立身高与眼线齐平、体型尺度协调，除非画面描述已写明身高差，禁止随意一高一低"
)
SCENE_SAME_FRAMING = "本帧与本镜另一张关键帧（首图/尾图）保持同一机位、同一构图、同一场景布置与光线方向，只有人物动作所处的瞬间不同，不要换机位或重新构图"
# 动作/互动保真：把"摸石碑"画成"正面端站、手悬空、与石碑互不相干"是当前关键帧最常见的失真。
SCENE_ACTION_FIDELITY = (
    "严格按上方画面描述还原人物的动作与朝向：若描述中人物在触碰/按压/拿取/递出/挥击/指向/注视/搀扶某个对象或另一个人，"
    "必须画出明确的接触或明确朝向该对象——人物的身体、肩线、面部与视线随动作转向目标，手部真实搭在/握住/伸向目标，"
    "人物与对象形成清晰可读的互动关系；切勿把有互动的动作画成正面端站、双手垂放、目视镜头、与对象彼此无关的摆拍站姿；"
    "接触类动作优先侧面构图，清楚展现接触点与双方相对方位"
)
SCENE_CONTACT_SIDE_VIEW = (
    "本帧含接触类动作：采用侧面视角构图，清楚展现肢体接触点与人物/对象的空间关系，禁止正面摆拍"
)
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
    missing = [
        name for name in shot.characters
        if name not in bible_map and not is_functional_extra(name)
    ]
    if missing:
        raise CompileError(
            f"镜头 {shot.shot_no} 关键帧引用了既不在角色圣经、也不是功能性路人的角色：{missing}"
        )
    anchors = "；".join(
        bible_map[name].appearance_canonical
        if name in bible_map else functional_extra_anchor(name)
        for name in shot.characters
    )
    visible_names = "、".join(shot.characters)
    visible_roster = (
        f"本帧只允许出现这些画面人物：{visible_names}；不得添加名单外人物、无关路人或多余人影，"
        "人物位置必须符合本镜首尾帧描述和动作调度"
        if visible_names else "")
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
        visible_roster,
        SCENE_CONSISTENCY if anchors else "",
        f"场景：{scene_hint}" if scene_hint else "",
        frame_desc,
        SCENE_ACTION_FIDELITY if anchors else "",
        SCENE_CONTACT_SIDE_VIEW if has_contact_action(shot) else "",
        SCENE_SAME_FRAMING,
        SCENE_EFFECT_RESTRAINT,
        transition_frame_hint,
        f"景别：{shot.shot_size}" + ("；机位：侧面" if has_contact_action(shot) else ""),
        SCENE_QUALITY,
        SCENE_NEGATIVE,
    ]
    return "。".join(p.strip().rstrip("。") for p in parts if p.strip())


def idem_key(prompt_text: str, image_urls: list[tuple[str, str]] | None = None) -> str:
    payload = prompt_text + "|" + "|".join(f"{u}#{r}" for u, r in (image_urls or []))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def shot_cost_cny(duration_s: int) -> float:
    return round(duration_s * config.VIDEO_PRICE_PER_SECOND, 2)
