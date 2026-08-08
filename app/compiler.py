"""Prompt 编译器：分镜脚本 → Seedance prompt。确定性代码，非 LLM（PRD §4.4）。
一致性核心：画风串/场景串/角色锚点串逐字拼接，LLM 永不改写。
M0 实测网关无同步参数校验，因此本编译器是参数合法性的唯一防线。
"""
from __future__ import annotations

import hashlib
import re

from app import config
from app.character_policy import (
    collective_role_anchor,
    functional_extra_anchor,
    is_allowed_storyboard_character,
    is_collective_role,
    typed_functional_identity_names,
)
from app.schemas import Bible, EpisodeScreenplay, Shot
from app.spoken_contract import SPOKEN_DELIVERIES

# 正向质量/稳定锚点（Seedance 最佳实践：显式给出稳定与质量约束，比单纯负面词更有效）
QUALITY_SUFFIX = (
    "人物五官清晰稳定、表情自然，手部与所持道具关系正常稳定，动作符合现实物理与人体运动规律、自然连贯，"
    "单一动作一镜到底，首帧到尾帧同机位同场景、背景构图保持一致只有动作自然推进不跳变，"
    "镜头运动平稳不抖动，光影与色调统一，竖屏电影质感")
# 成片不要任何配乐：只保留人物台词/旁白人声与必要环境音
NO_BGM_SUFFIX = "全程不要任何背景音乐、不要配乐、不要 BGM；声音只保留人物台词、旁白人声与必要的环境音"
SOURCE_EXCERPT_PROMPT_MAX = 260
SOURCE_EXCERPT_MARKER = "小说原文兜底参考："

def _clean_transition(transition: str | None) -> str:
    transition = (transition or "").strip()
    if not transition or transition == "硬切":
        return ""
    return transition

def _incoming_transition_line(transition: str | None) -> str:
    transition = _clean_transition(transition)
    if not transition:
        return ""
    return (
        f"最终编辑会用「{transition}」将上一镜接入本镜；"
        "原始片段从稳定、干净的本镜首帧开始，不自行叠化、闪黑、闪白或重复转场。"
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
        f"最终编辑会以「{transition}」连接下一镜{target}{first_frame}。"
        "本镜末尾预留约0.3秒稳定的动作结果；不自行生成转场特效，"
        "不要把下一场景拍成本镜内容。"
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
        f"编辑衔接尾帧：最终编辑将用「{transition}」进入下一镜，{target}{first_frame}；"
        "本尾图只保留稳定、干净、可重叠的动作结果，不预烧渐暗、闪白、叠化或字幕。"
    )


class CompileError(ValueError):
    """可在生成前纠正的 prompt 编译错误。

    继承 ValueError 让单镜生成路由按 409 业务冲突返回，而不是被全局异常处理器
    误报为 500 系统内部错误。
    """
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        failure_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.failure_kind = failure_kind


def _shot_character_contract_names(shot: Shot) -> list[str]:
    """Prompt 可能消费到的全部角色身份，不只是 legacy ``characters``。"""
    names: list[str] = [
        *((name or "").strip() for name in (shot.characters or [])),
        *((name or "").strip() for name in (shot.characters_visible or [])),
        *_shot_voice_contract_names(shot),
    ]
    for role in shot.reference_roles or []:
        prefix, separator, name = str(role or "").partition(":")
        if separator and prefix in {"character_identity", "collective_group"}:
            names.append(name.strip())
    return list(dict.fromkeys(name for name in names if name))


def _shot_visual_contract_names(shot: Shot) -> list[str]:
    names = [
        *((name or "").strip() for name in (shot.characters or [])),
        *((name or "").strip() for name in (shot.characters_visible or [])),
    ]
    for role in shot.reference_roles or []:
        prefix, separator, name = str(role or "").partition(":")
        if separator and prefix in {"character_identity", "collective_group"}:
            names.append(name.strip())
    return list(dict.fromkeys(name for name in names if name))


def _shot_voice_contract_names(shot: Shot) -> list[str]:
    """Return actual spoken identities, excluding non-person ambient sources."""
    names = [
        *((dialogue.speaker or "").strip() for dialogue in (shot.dialogues or [])),
        *(
            (item.speaker_id or "").strip()
            for item in (shot.audio_timeline or [])
            if item.type in SPOKEN_DELIVERIES
        ),
    ]
    return list(dict.fromkeys(name for name in names if name))


def _assert_shot_character_contract(
    shot: Shot,
    bible: Bible,
    *,
    context: str = "Prompt",
    screenplay: EpisodeScreenplay | None = None,
):
    if screenplay is not None and screenplay.narrative_plan is not None:
        from app.identity_contracts import (
            IdentityContractError,
            narrative_identity_resolver,
        )

        try:
            resolver = narrative_identity_resolver(bible, screenplay)
            for name in _shot_visual_contract_names(shot):
                resolver.resolve(name, usage="visual")
            for name in _shot_voice_contract_names(shot):
                resolver.resolve(name, usage="voice")
        except IdentityContractError as exc:
            raise CompileError(
                f"镜头 {shot.shot_no} {context} typed identity contract 无法解析：{exc}"
            ) from exc
        return resolver
    bible_names = {character.name for character in bible.characters}
    declared_functional_names = typed_functional_identity_names(screenplay)
    invalid = [
        name
        for name in _shot_character_contract_names(shot)
        if not is_allowed_storyboard_character(
            name,
            bible_names,
            allow_without_bible=False,
            declared_functional_names=declared_functional_names,
        )
    ]
    if invalid:
        raise CompileError(
            f"镜头 {shot.shot_no} {context} 角色合同残留了既不在角色圣经、"
            f"也不是功能性路人或群体标签的角色：{invalid}；"
            "请先同步 characters、characters_visible、声轨与参考角色"
        )
    return None


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


def sanitize_seedance_prompt(prompt_text: str, *, aggressive: bool = False,
                             extra_terms: tuple[tuple[str, str], ...] | None = None) -> str:
    """Normalize layout and video arguments without rewriting story content.

    ``aggressive`` and ``extra_terms`` remain as compatibility parameters for
    historical callers.  They intentionally do not trigger word-list based
    mutation or retry behaviour.
    """
    _ = aggressive, extra_terms
    body, args = _split_video_args(prompt_text)
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
    """在最终供应商边界移除非法原文，同时保留合同内合法对白/必现文字。

    这是入队与 worker 提交共用的最后一道防线：新编译 prompt、人工 override 和
    历史排队版本都会经过这里。原文重合被替换为确定性的合同提示，不会因为一段
    脏字段直接让整个镜头失败，也不会把章节原文发送给视频供应商。
    """
    from app.continuity import (
        _allowed_prompt_verbatim_texts,
        source_excerpt_overlap_spans,
    )

    body, args = _split_video_args(prompt_text, shot.duration_s)

    # 历史版本把兜底原文作为末尾独立行；分段 prompt 整行移除，旧单行格式则
    # 从 marker 截到结尾（video args 已由 _split_video_args 单独保存）。
    if SOURCE_EXCERPT_MARKER in body:
        if "\n" in body:
            kept_lines: list[str] = []
            for line in body.splitlines():
                if SOURCE_EXCERPT_MARKER not in line:
                    kept_lines.append(line)
                    continue
                prefix = line.split(SOURCE_EXCERPT_MARKER, 1)[0].rstrip(" ：:；;")
                if prefix:
                    kept_lines.append(prefix)
            body = "\n".join(kept_lines)
        else:
            body = body.split(SOURCE_EXCERPT_MARKER, 1)[0].rstrip(" ：:；;")

    excerpt = (shot.source_excerpt or "").strip()
    protected: list[tuple[str, str]] = []
    if excerpt:
        for index, allowed in enumerate(_allowed_prompt_verbatim_texts(shot)):
            if not allowed or allowed not in body:
                continue
            token = f"__MANJU_ALLOWED_VERBATIM_{index}__"
            while token in body:
                token += "_"
            body = body.replace(allowed, token)
            protected.append((token, allowed))

        spans = source_excerpt_overlap_spans(body, excerpt)
        replacement = "按本镜主动作与首尾状态概括呈现，不复述小说原文"
        for start, end in reversed(spans):
            body = body[:start] + replacement + body[end:]

        for token, allowed in protected:
            body = body.replace(token, allowed)

    text = f"{body}{args}" if body.strip() else args.strip()
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


def has_contact_action(shot: Shot) -> bool:
    """本镜主动作是否含人物与人物/道具的真实接触互动。"""
    return shot_contact_phase(shot) in {"approach", "established", "separated"}


def shot_contact_phase(shot: Shot) -> str:
    """Read the phase declared by the storyboard contract."""
    for tag in shot.risk_tags or []:
        prefix, separator, value = str(tag).partition(":")
        if prefix == "contact_phase" and separator and value in {
            "approach", "established", "separated",
        }:
            return value
    return "none"


def contains_contact_action(text: str | None) -> bool:
    """Unstructured text does not carry an authoritative interaction phase."""
    _ = text
    return False


def contains_established_contact_action(text: str | None) -> bool:
    """一段画面描述是否明确表示接触已经成立。"""
    _ = text
    return False


def contact_action_phase(text: str | None) -> str:
    """Legacy text cannot authoritatively declare an interaction phase."""
    _ = text
    return "none"


def has_explicit_height_difference(shot: Shot, bible: Bible | None = None) -> bool:
    """提示词/外观锚点是否已明确写出身高差（有则不强行同身高）。"""
    return bool(explicit_height_difference_evidence(shot, bible))


def explicit_height_difference_evidence(shot: Shot, bible: Bible | None = None) -> list[str]:
    """Return source text only when the shot explicitly declares this relation."""
    _ = bible
    if "explicit_height_difference" not in (shot.risk_tags or []):
        return []
    return list(dict.fromkeys(
        value
        for value in (
            (shot.spatial_anchor or "").strip(),
            (shot.primary_action or "").strip(),
            (shot.action_desc or "").strip(),
        )
        if value
    ))[:4]


def _resolve_camera_angle(shot: Shot) -> str:
    """接触类动作默认侧面视角；已显式侧面则保留，非接触沿用原机位角。"""
    current = (shot.camera_angle or "").strip()
    if has_contact_action(shot):
        return "侧面"
    return current or "平视"


def _equal_height_hint(
    shot: Shot,
    bible: Bible | None = None,
    *,
    collective_names: set[str] | None = None,
) -> str:
    """多人物同框：无明示身高差时锁定站立同高/齐眼线，避免随机一高一低。"""
    from app.continuity import effective_characters_visible

    bible_names = {c.name for c in bible.characters} if bible is not None else set()
    individuals = [
        name for name in effective_characters_visible(shot)
        if (
            name not in collective_names
            if collective_names is not None
            else name in bible_names or not is_collective_role(name)
        )
    ]
    if len(individuals) < 2:
        return ""
    if has_explicit_height_difference(shot, bible):
        return ""
    return (
        "【同身高硬合同】同框人物站立身高与眼线尽量齐平；"
        "同框青少年/成人的站直基准身高、头身比与体型尺度必须一致；"
        "两人同时站立时，双脚落在同一地面与景深平面，头顶、肩线、髋线和眼线齐平；"
        "“抬头/仰望/低头”只能用头颈和视线方向表现，不得借此改变身高；"
        "除非剧情已写明身高差，禁止儿童化、随意一高一低或用强透视制造尺度差"
    )


def _matching_scene(shot: Shot, bible: Bible | None) -> object | None:
    """按规范场景名匹配 Bible 场景；最长名称优先，避免子串误配。"""
    if bible is None:
        return None
    scenes = list(getattr(bible, "scenes", None) or [])
    explicit = (shot.scene_name or "").strip()
    if explicit:
        for scene in scenes:
            if (getattr(scene, "name", "") or "").strip() == explicit:
                return scene
    setting = (shot.scene_setting or "").strip()
    matches = [
        scene for scene in scenes
        if (getattr(scene, "name", "") or "").strip()
        and (getattr(scene, "name", "") or "").strip() in setting
    ]
    return max(matches, key=lambda item: len((getattr(item, "name", "") or "").strip()), default=None)


def _scene_geometry_contract(shot: Shot, bible: Bible | None) -> tuple[str, str, list[str]]:
    """返回场景文本锚点、固定几何合同与显式地标。

    场景参考图只能帮助“像同一个地方”，不能保证视频时序中的固定物体不 morph。
    因此把场景圣经的 canonical/landmarks 直接写进视频与关键帧合同。
    """
    scene = _matching_scene(shot, bible)
    canonical = (getattr(scene, "scene_canonical", "") or "").strip() if scene else ""
    landmarks = [
        str(item).strip()
        for item in (getattr(scene, "landmarks", None) or [])
        if str(item).strip()
    ]
    scene_anchor = (
        f"场景固定锚点：{canonical}"
        if canonical else (f"场景：{shot.scene_setting}" if shot.scene_setting else "")
    )
    explicit_landmarks = "、".join(landmarks)
    geometry = (
        "同一视频内固定地标、大型陈设和剧情道具从首帧到尾帧保持同一外形、数量与位置；"
        "构图内物体不得消失、复制、变形、换位后再出现。人物远离只通过连续走位；"
        "固定镜头下留在原位，移动镜头下只能正常出画"
    )
    if canonical:
        geometry += f"。当前场景固定布局：{canonical}"
    if explicit_landmarks:
        geometry += f"。显式固定地标：{explicit_landmarks}"
    return scene_anchor, geometry, landmarks


def _named_character_is_explicitly_offscreen(name: str, text: str) -> bool:
    """角色名所在短句是否已经明确说明角色不入画。"""
    escaped = re.escape(name)
    return bool(
        re.search(
            rf"(?:画外|镜外|不入画|留在画外|退到画外)(?:的)?[^，。；！？]{{0,12}}{escaped}",
            text,
        )
        or re.search(
            rf"{escaped}[^，。；！？]{{0,12}}(?:在画外|于画外|不入画|留在画外|退到画外)",
            text,
        )
    )


def _has_unqualified_character_mention(name: str, text: str) -> bool:
    """可视描述是否把非画内角色当成了未限定的画面对象。"""
    for clause in re.split(r"[，。；！？]", text or ""):
        if name in clause and not _named_character_is_explicitly_offscreen(name, clause):
            return True
    return False


def _mark_character_offscreen(name: str, text: str) -> str:
    """逐短句标记画外角色，保留已经正确标注的短句。"""
    if not text or name not in text:
        return text
    parts = re.split(r"([，。；！？])", text)
    for index in range(0, len(parts), 2):
        clause = parts[index]
        if name in clause and not _named_character_is_explicitly_offscreen(name, clause):
            parts[index] = clause.replace(name, f"画外{name}")
    return "".join(parts)


def project_visual_contract_to_visible_cast(
    shot: Shot,
    *,
    state_in: str,
    state_out: str,
    primary_action: str,
    full_action: str,
    visible_names: list[str],
    bible_names: list[str],
    continuity_mode: str,
) -> tuple[str, str, str, str, str]:
    """把叙事连续性投影为本镜可拍的画面合同。

    ``state_in/state_out`` 仍保留在分镜数据中供叙事连续性使用。只有发送给视频模型的
    可视字段会在角色名单冲突时改用首尾帧描述，并把非画内人物明确标为画外。
    连续动作镜依赖上一镜真实尾帧，不在这里重写其起点。
    """
    if continuity_mode == "action_continuation":
        return state_in, state_out, primary_action, full_action, ""

    visible = {name for name in visible_names if name}
    candidates = [name for name in bible_names if name and name not in visible]
    visual_fields = (
        state_in,
        state_out,
        primary_action,
        full_action,
        shot.first_frame_desc or "",
        shot.last_frame_desc or "",
        shot.spatial_anchor or "",
    )
    offscreen_names = [
        name for name in sorted(candidates, key=len, reverse=True)
        if any(_has_unqualified_character_mention(name, value) for value in visual_fields)
    ]
    if not offscreen_names:
        return state_in, state_out, primary_action, full_action, ""

    state_conflict = any(
        _has_unqualified_character_mention(name, value)
        for name in offscreen_names
        for value in (state_in, state_out)
    )
    if state_conflict:
        state_in = (shot.first_frame_desc or state_in).strip()
        state_out = (shot.last_frame_desc or state_out).strip()

    for name in offscreen_names:
        state_in = _mark_character_offscreen(name, state_in)
        state_out = _mark_character_offscreen(name, state_out)
        primary_action = _mark_character_offscreen(name, primary_action)
        full_action = _mark_character_offscreen(name, full_action)

    if visible_names:
        visible_contract = f"可辨识画面人物仅限：{'、'.join(visible_names)}。"
    else:
        visible_contract = "本镜不得出现可辨识人物。"
    visible_contract += (
        f"{'、'.join(offscreen_names)}只作为画外叙事关系；"
        "不得出现其身体、脸、背影、倒影或剪影，也不得把其动作、表情或状态变化可视化。"
    )
    return (
        state_in,
        state_out,
        primary_action,
        full_action,
        visible_contract,
    )


def _narrative_keyframe_target_with_source(shot: Shot) -> tuple[str, str]:
    terminal_moments = (
        ("last_frame_desc", shot.last_frame_desc),
        ("state_out", shot.state_out),
    )
    action_moments = (
        ("primary_action", shot.primary_action),
        ("action_desc", shot.action_desc),
    )
    moments = (*terminal_moments, *action_moments)
    if has_contact_action(shot):
        for source, moment in terminal_moments:
            if (moment or "").strip():
                return (moment or "").strip(), source
        for source, moment in action_moments:
            if (moment or "").strip():
                return (moment or "").strip(), source
    for source, moment in (
        *moments,
        ("first_frame_desc", shot.first_frame_desc),
        ("state_in", shot.state_in),
    ):
        if (moment or "").strip():
            return (moment or "").strip(), source
    return (shot.scene_setting or "").strip(), "scene_setting"


def narrative_keyframe_target(shot: Shot) -> str:
    """返回叙事关键帧必须表现的唯一动作瞬间。

    关键帧不是首帧/尾帧协议。接触镜头优先选“接触已成立”的尾状态；
    其他镜头选主动作。这样图片模型不会把首尾两个时刻拼成一张图。
    """
    return _narrative_keyframe_target_with_source(shot)[0]


def _keyframe_required_text_expected(shot: Shot, target: str, target_source: str) -> bool:
    from app.continuity import required_text_strategy

    required = getattr(shot, "required_text", None)
    exact = str(getattr(required, "exact_text", "") or "").strip() if required is not None else ""
    if not exact:
        return False
    if required_text_strategy(shot) != "embedded_prop":
        return False
    if exact in (target or ""):
        return True
    try:
        appear_at = float(getattr(required, "appear_start_s", 0.0) or 0.0)
    except (TypeError, ValueError):
        appear_at = 0.0
    stable_raw = getattr(required, "stable_until_s", None)
    try:
        stable_until = float(stable_raw) if stable_raw is not None else None
    except (TypeError, ValueError):
        stable_until = None
    target_time: float | None
    if target_source in {"last_frame_desc", "state_out"}:
        target_time = float(getattr(shot, "duration_s", 0) or 0)
    elif target_source in {"first_frame_desc", "state_in"}:
        target_time = 0.0
    else:
        target_time = None
    if target_time is None:
        # 中间动作无精确时码；只有从 0s 就稳定存在的文字才可强制入画。
        return appear_at <= 0 and (stable_until is None or stable_until >= 0)
    return appear_at <= target_time and (stable_until is None or target_time <= stable_until)


def keyframe_visual_contract(
    shot: Shot,
    bible: Bible | None = None,
    *,
    screenplay: EpisodeScreenplay | None = None,
) -> dict[str, object]:
    """视频编译器与叙事关键帧共用的确定性构图合同。"""
    from app.continuity import dialogue_focus_subject, effective_characters_visible

    visible_characters = effective_characters_visible(shot)
    dialogue_focus = dialogue_focus_subject(shot)
    bible_names = {c.name for c in bible.characters} if bible is not None else set()
    identity_resolver = None
    if screenplay is not None and screenplay.narrative_plan is not None:
        if bible is None:
            raise CompileError("narrative 关键帧合同缺少 Bible，无法解析身份")
        identity_resolver = _assert_shot_character_contract(
            shot, bible, context="关键帧构图", screenplay=screenplay,
        )
        collective_roles = [
            name
            for name in visible_characters
            if identity_resolver.resolve(name, usage="visual").is_collective
        ]
    else:
        collective_roles = [
            name for name in visible_characters
            if name not in bible_names and is_collective_role(name)
        ]
    individual_characters = [name for name in visible_characters if name not in collective_roles]
    identity_verification: dict[str, dict[str, object]] = {}
    for name in individual_characters:
        if identity_resolver is not None:
            identity = identity_resolver.resolve(name, usage="visual")
            identity_verification[name] = {
                "mode": (
                    "visual_anchor"
                    if identity.requires_asset
                    else "text_contract"
                ),
                "visual_policy": identity.visual_policy,
                "visual_canonical": identity.visual_anchor(),
            }
        else:
            character = next(
                (
                    item for item in (bible.characters if bible is not None else [])
                    if item.name == name
                ),
                None,
            )
            identity_verification[name] = {
                "mode": "visual_anchor" if character is not None else "text_contract",
                "visual_policy": "canonical" if character is not None else "contextual",
                "visual_canonical": (
                    character.appearance_canonical if character is not None else ""
                ),
            }
    target_keyframe_desc, target_source = _narrative_keyframe_target_with_source(shot)
    target_contact_phase = shot_contact_phase(shot)
    # 多时序关键帧中，接触镜的开场/反应帧本身可能已不含“接触”词面，
    # 但仍必须与决定性帧共用侧面互动轴。该标记只强制机位，不伪造已建立接触。
    inherited_contact_axis = "timeline_contact_side_axis" in (shot.risk_tags or [])
    contact_camera_required = (
        target_contact_phase in {"approach", "established", "separated"}
        or inherited_contact_axis
    )
    established_contact_required = target_contact_phase == "established"
    height_difference_evidence = explicit_height_difference_evidence(shot, bible)
    explicit_height_difference = bool(height_difference_evidence)
    if len(individual_characters) < 2:
        relative_height_policy = "single_subject"
    elif explicit_height_difference:
        relative_height_policy = "preserve_explicit_difference"
    else:
        relative_height_policy = "equal_scale"
    target_forbids_crowd = "collective_presence_forbidden" in (shot.risk_tags or [])
    target_requires_anonymous_crowd = (
        bool(collective_roles) and not dialogue_focus and not target_forbids_crowd
    )
    anonymous_background_allowed = (
        not dialogue_focus and not target_forbids_crowd and bool(collective_roles)
    )
    _, scene_geometry, scene_landmarks = _scene_geometry_contract(shot, bible)
    matched_scene = _matching_scene(shot, bible)
    scene_canonical = (
        (getattr(matched_scene, "scene_canonical", "") or "").strip()
        if matched_scene is not None else ""
    )
    return {
        "target_keyframe_desc": target_keyframe_desc,
        "target_source": target_source,
        "target_contact_phase": target_contact_phase,
        "contact_evidence": [f"{target_source}: {target_keyframe_desc}"] if contact_camera_required else [],
        # contact_required 保留为旧调用方的侧拍别名。
        "contact_required": contact_camera_required,
        "contact_camera_required": contact_camera_required,
        "contact_axis_inherited": inherited_contact_axis,
        "established_contact_required": established_contact_required,
        "camera_angle": (
            (shot.camera_angle or "").strip()
            if not contact_camera_required
            else (
                "侧面"
            )
        ) or "平视",
        "relative_height_policy": relative_height_policy,
        "explicit_height_difference": explicit_height_difference,
        "height_difference_evidence": height_difference_evidence,
        "visible_characters": visible_characters,
        "individual_visible_characters": individual_characters,
        "identity_verification": identity_verification,
        "collective_visible_roles": collective_roles,
        "dialogue_focus_subject": dialogue_focus,
        "dialogue_closeup_required": bool(dialogue_focus),
        "collective_presence_required": (
            bool(collective_roles) or target_requires_anonymous_crowd
        ) and not target_forbids_crowd,
        "collective_presence_forbidden": target_forbids_crowd or bool(dialogue_focus),
        "required_text_expected": _keyframe_required_text_expected(
            shot, target_keyframe_desc, target_source,
        ),
        "spatial_anchor": (shot.spatial_anchor or "").strip(),
        "scene_canonical": scene_canonical,
        "scene_landmarks": scene_landmarks,
        "scene_geometry_contract": scene_geometry,
        "anonymous_background_allowed": anonymous_background_allowed,
    }



def _compile_text_policy(shot: Shot) -> str:
    from app.continuity import required_text_strategy

    required = getattr(shot, "required_text", None)
    if required is not None and (getattr(required, "exact_text", None) or "").strip():
        exact = required.exact_text.strip()
        surface = (required.surface or "画面指定表面").strip()
        strategy = required_text_strategy(shot)
        if strategy == "audio_only":
            return (
                f"只通过对白/画外音交付「{exact}」的信息；{surface}上不出现可读文字。"
                "画面中禁止字幕、标志、水印或乱码。"
            )
        if strategy == "deterministic_insert":
            return (
                f"只生成无字、干净的「{surface}」与人物表演；不得尝试拼写「{exact}」。"
                "精确中文由服务端确定性插入镜头交付，原始视频禁止任何可读字。"
            )
        if strategy == "none":
            return "画面不出现任何文字、字幕、标志或水印。"
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
    has_spoken_content = False
    for item in shot.audio_timeline:
        span = f"{item.start_s:g}–{item.end_s:g} 秒"
        if item.type == "ambient_sound":
            lines.append(f"{span}：{item.text or '自然环境声'}")
            continue
        has_spoken_content = True
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
    if has_spoken_content:
        lines.append(
            "所有对白和必要声音必须由本条视频直接生成并在片段结束前完整结束；"
            "只生成指定说话人和指定台词。"
        )
    else:
        lines.append(
            "本镜没有任何指定台词、画外音或旁白；所有人物全程闭口，不做说话口型，"
            "不得自行补充问候、应答、语气词或任何可辨识人声。"
        )
    return "\n".join(lines)


def _compile_reference_roles(shot: Shot, *, continuity_mode: str, with_refs: bool,
                             chained: bool, individual_names: set[str] | None = None,
                             collective_names: set[str] | None = None,
                             video_generation_mode: str | None = None,
                             first_frame_source: str | None = None) -> str:
    from app.continuity import reference_role_plan, uses_previous_tail_frame
    roles = reference_role_plan(
        shot,
        continuity_mode=continuity_mode,
        individual_names=individual_names,
        collective_names=collective_names,
    )
    lines: list[str] = []
    if video_generation_mode == "FIRST_LAST_FRAME_MODE":
        source_label = {
            "PREVIOUS_ADOPTED_TAIL": "上一镜采用视频的真实尾帧",
            "PREVIOUS_STATIC_TAIL": "上一镜已冻结的静态尾帧",
            "STATIC_BOUNDARY_ASSET": "本镜独立生成的静态首帧",
        }.get(first_frame_source or "", "已冻结的首帧")
        lines.extend([
            f"输入中的 first_frame 是{source_label}，也是本视频 0.0 秒必须逐像素承接的真实起点；"
            "不得先重画、换人、换景或跳到另一构图。",
            "输入中的 last_frame 是本视频结束时必须到达的真实终点。first_frame/last_frame 是时间边界，"
            "不是普通风格参考；只允许用连续可见动作与平稳摄影运动在两者之间过渡。",
        ])
        if roles:
            lines.append("当前镜角色/场景锚点映射：" + "、".join(roles) + "。")
        lines.append(
            "禁止把首尾图叠在一起，禁止溶图、换脸、人物融合、凭空消失、瞬移、硬切或中途切场景。"
        )
        return "\n".join(lines)
    if uses_previous_tail_frame(continuity_mode) and (chained or "start_state_reference" in roles):
        lines.append(
            "参考图 A 是上一镜真实结束帧，也是本视频 0.0 秒的强制起始状态。"
            "保持其中人物数量、身份、服装、位置、朝向、手势、道具状态和光线；"
            "从该状态继续当前动作，不重复上一镜已经完成的动作。"
        )
    if continuity_mode in {"same_scene_cut", "reaction_cut", "reverse_angle", "insert_detail"}:
        lines.append(
            "场景参考用于锁定建筑、固定地标、大型陈设、材质、光线和色调；当前镜头只重新选择摄影构图，"
            "不得改动石碑、门、桌台、屏幕等固定物体在世界空间中的位置、数量或外形。"
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
                                  bible: Bible | None = None,
                                  collective_names: set[str] | None = None,
                                  first_last_boundary: bool = False) -> str:
    from app.continuity import (
        dialogue_focus_subject,
        effective_characters_visible,
        required_text_strategy,
        uses_previous_tail_frame,
    )
    parts = [
        "不要重演前序剧情",
        "不要提前表演下一镜内容",
        (
            f"除「{(shot.required_text.exact_text or '').strip()}」外不要出现任何其他文字"
            if required_text_strategy(shot) == "embedded_prop"
            else "不要生成字幕、乱码、可读道具字样或水印"
        ),
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
        if first_last_boundary:
            parts.append(
                f"完成首帧边界运镜后的当前动作可见角色仅限：{'、'.join(visible)}；"
                "输入首帧中已有的边界人物只可随连续运镜自然出画，不得变形、融合或被复制"
            )
        else:
            parts.append(f"画面可见角色仅限：{'、'.join(visible)}")
    focus_subject = dialogue_focus_subject(shot)
    if focus_subject:
        if first_last_boundary:
            parts.extend([
                f"完成首帧边界运镜后，对白镜头只允许「{focus_subject}」一人入画",
                "首帧已有听者只能随连续运镜自然出画；运镜完成后，听者、其他说话人和人群留在画外",
                "当前对白构图不要双人站桩或群像构图",
            ])
        else:
            parts.extend([
                f"对白镜头只允许「{focus_subject}」一人入画",
                "听者、其他说话人和人群全部留在画外，不得出现肩膀、背影、倒影或模糊人脸",
                "不要双人同框、多人站桩或群像构图",
            ])
    if not uses_previous_tail_frame(continuity_mode) and not first_last_boundary:
        parts.append("不要沿用上一镜完整构图或主体尾帧姿势")
    if first_last_boundary:
        parts.append(
            "不得忽略或重画首帧；不得用溶图、变形、换脸、人物融合、瞬移或硬切伪造首尾帧过渡"
        )
    contact_phase = shot_contact_phase(shot)
    if contact_phase == "established":
        parts.append("已接触动作禁止正面摆拍、禁止手悬空或接触点留缝")
    elif contact_phase == "approach":
        parts.append("接近/未命中动作禁止正面摆拍；保留清晰间距，禁止提前改成已碰触")
    elif contact_phase == "separated":
        parts.append("松开/分离动作禁止正面摆拍；保留已分开的清晰间距，禁止重新画成已接触")
    bible_names = {c.name for c in bible.characters} if bible is not None else set()
    individual_visible = [
        name for name in visible
        if (
            name not in collective_names
            if collective_names is not None
            else name in bible_names or not is_collective_role(name)
        )
    ]
    if len(individual_visible) >= 2 and not has_explicit_height_difference(shot, bible):
        parts.append("禁止同框人物随意一高一低或眼线错位")
    if extra_negative:
        parts.extend(x.strip() for x in extra_negative if x and x.strip())
    parts.append(
        "严格满足上述结构化身份、场景、动作、连续性、声音、文字与技术合同；"
        "任何未声明的主体、状态或媒介变化都视为合同外输出"
    )
    return "；".join(dict.fromkeys(parts))


def _first_last_boundary_path(
    *,
    duration_s: int,
    first_frame_source: str | None,
    relation_edit: str | None,
    relation_action: str | None,
) -> tuple[str, str]:
    edit = (relation_edit or "unknown").strip()
    action = (relation_action or "unknown").strip()
    duration = max(1.0, float(duration_s))
    departure_end = duration * 0.22
    settle_start = duration * 0.78

    def _time(value: float) -> str:
        return f"{value:.2f}".rstrip("0").rstrip(".")

    camera_move = "连续跟随主体的轨道移动、横摇、推拉与弧形重构运镜"
    source_label = {
        "PREVIOUS_ADOPTED_TAIL": "上一镜真实尾帧",
        "PREVIOUS_STATIC_TAIL": "上一镜静态尾帧",
        "STATIC_BOUNDARY_ASSET": "本镜静态首帧",
    }.get(first_frame_source or "", "输入首帧")
    path = (
        f"{source_label}就是 0.0 秒画面，输入尾帧就是 {_time(duration)} 秒画面；"
        "两个端点都不可重画。\n"
        f"0.0–{_time(departure_end)} 秒：从首帧原构图平滑起步，先建立同一场景的三维方位，"
        "摄影机缓慢加速，人物只开始当前动作，不得跳位。\n"
        f"{_time(departure_end)}–{_time(settle_start)} 秒：完整执行主动作，同时使用"
        f"{camera_move}消化景别、角度、主体位置和背景透视差异。端点构图差距越大，"
        "摄影机轨迹和人物合理位移越充分；差距越小，自动减小运镜幅度，禁止无意义晃动。\n"
        f"{_time(settle_start)}–{_time(duration)} 秒：摄影机平滑减速，动作完成并精确重构到"
        "输入尾帧的机位、景别、人物位置、朝向和道具状态，结尾稳定停留。\n"
        f"剪辑关系={edit}，动作关系={action}。只允许用同一物理空间内连续可解释的摄影机运动、"
        "遮挡关系、视差和人物正常出入画连接端点；运镜不能掩盖换人、换装、换景或身份漂移。"
        "若端点人物身份或场景并非同一连续世界，不得用溶图、变形、复制、瞬移或硬切伪造过渡。"
    )
    return path, camera_move


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
                   screenplay: EpisodeScreenplay | None = None,
                   visual_style: str | None = None,
                   aspect_ratio: str = "9:16",
                   video_generation_mode: str | None = None,
                   first_frame_source: str | None = None,
                   boundary_relation_edit: str | None = None,
                   boundary_relation_action: str | None = None,
                   boundary_start_state: str | None = None) -> str:
    """编译 Seedance 最终提示词（PRD §11）：最小完备、固定段落、禁止原文/前镜完整动作/未来剧情。"""
    from app.continuity import (
        dialogue_action_staging_kind,
        dialogue_focus_subject,
        derive_continuity_mode,
        effective_characters_visible,
        effective_primary_action,
        effective_state_in,
        ensure_audio_timeline,
        implicit_speech_without_dialogue_errors,
        planned_state_out,
        reference_role_plan,
        required_text_strategy,
        structured_state_prompt,
        sync_shot_continuity_fields,
        uses_previous_tail_frame,
    )
    from app.schemas import PROMPT_CONTRACT_VERSION

    bible_map = {c.name: c for c in bible.characters}
    identity_resolver = _assert_shot_character_contract(
        shot, bible, screenplay=screenplay,
    )
    if shot.duration_s not in config.ALLOWED_DURATIONS:
        raise CompileError(
            f"镜头 {shot.shot_no} 时长 {shot.duration_s}s 不合法，视频生成时长必须为 "
            f"{config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S}s 的整数")

    sync_shot_continuity_fields(shot)
    visible_names = effective_characters_visible(shot)
    dialogue_focus = dialogue_focus_subject(shot)
    dialogue_staging = dialogue_action_staging_kind(shot)
    mode = (continuity_mode or derive_continuity_mode(shot)).strip()
    if dialogue_focus and mode == "action_continuation":
        mode = "same_scene_cut"
    if mode not in {"action_continuation", "same_scene_cut", "reaction_cut",
                    "reverse_angle", "insert_detail", "scene_change"}:
        mode = derive_continuity_mode(shot)
    first_last_boundary = video_generation_mode == "FIRST_LAST_FRAME_MODE"
    shot.continuity_mode = mode
    shot.prompt_contract_version = PROMPT_CONTRACT_VERSION
    ensure_audio_timeline(shot, voice_bible)
    implicit_speech_errors = implicit_speech_without_dialogue_errors(shot)
    if implicit_speech_errors:
        raise CompileError("；".join(implicit_speech_errors))
    if identity_resolver is not None:
        # ``ensure_audio_timeline`` may deterministically materialise entries
        # that were absent on the input shot; validate the final provider cast.
        for name in _shot_voice_contract_names(shot):
            identity_resolver.resolve(name, usage="voice")
        collective_names = {
            name
            for name in visible_names
            if identity_resolver.resolve(name, usage="visual").is_collective
        }
        individual_names = set(visible_names) - collective_names
        known_identity_tokens = list(dict.fromkeys(
            token
            for identity in identity_resolver.identities
            for token in (identity.identity_id, identity.display_name)
            if token
        ))
    else:
        collective_names = None
        individual_names = set(bible_map)
        known_identity_tokens = list(bible_map)
    shot.reference_roles = reference_role_plan(
        shot,
        continuity_mode=mode,
        individual_names=individual_names,
        collective_names=collective_names,
    )

    shot_dur = clip_duration(shot)
    state_in = effective_state_in(shot)
    if first_last_boundary and (boundary_start_state or "").strip():
        state_in = boundary_start_state.strip()
    elif uses_previous_tail_frame(mode) and (prev_state_out or "").strip():
        # 连续动作：以实际/计划尾状态为强制起点（不使用上一镜完整 action_desc）
        state_in = prev_state_out.strip()
        shot.state_in = state_in
    state_out = planned_state_out(shot)
    if first_last_boundary and (shot.last_frame_desc or "").strip():
        state_out = shot.last_frame_desc.strip()
    primary = effective_primary_action(shot)
    full_action = (shot.action_desc or "").strip()
    (
        state_in,
        state_out,
        primary,
        full_action,
        visible_cast_block,
    ) = project_visual_contract_to_visible_cast(
        shot,
        state_in=state_in,
        state_out=state_out,
        primary_action=primary,
        full_action=full_action,
        visible_names=visible_names,
        bible_names=known_identity_tokens,
        continuity_mode=mode,
    )
    if first_last_boundary and (boundary_start_state or "").strip():
        # 0 秒画面由真实首帧控制。可见角色投影只约束当前动作，不得把上游
        # 边界人物从 START STATE 文本中抹掉后再要求模型重画首帧。
        state_in = boundary_start_state.strip()
        if visible_cast_block:
            visible_cast_block += (
                "以上当前动作可见名单从首帧边界运镜完成后生效；"
                "输入首帧已有边界人物必须先原样保留，再通过连续运镜自然出画。"
            )
    if not state_in or not primary or not state_out:
        raise CompileError(
            f"镜头 {shot.shot_no} 缺少 state_in/primary_action/state_out，无法编译最小完备提示词"
        )

    style = (visual_style or bible.world.visual_style_canonical or "写实国风玄幻动画").strip()
    render_shot_size = (
        ("特写" if shot.shot_size == "特写" else "近景")
        if dialogue_focus else (
            ("全景" if dialogue_staging == "spatial" else "中景")
            if dialogue_staging and shot.shot_size in {"近景", "特写"}
            else shot.shot_size
        )
    )
    render_camera_move = (
        shot.camera_move
        if not dialogue_focus or shot.camera_move in {"固定", "推近"}
        else "固定"
    )
    boundary_path = ""
    if first_last_boundary:
        boundary_path, render_camera_move = _first_last_boundary_path(
            duration_s=shot_dur,
            first_frame_source=first_frame_source,
            relation_edit=boundary_relation_edit,
            relation_action=boundary_relation_action,
        )
    scale_hint = _framing_scale_hint(render_shot_size)
    camera_angle = _resolve_camera_angle(shot)
    # 回写解析后的机位角，便于下游审计/重试与关键帧对齐
    shot.camera_angle = camera_angle
    camera_line = (
        f"{render_shot_size}；{camera_angle}；{render_camera_move}"
        + (f"。{scale_hint}" if scale_hint else "")
    )
    if (shot.camera_motivation or "").strip():
        camera_line += f"。摄影意图：{shot.camera_motivation.strip()}"
    if dialogue_focus:
        boundary_camera_prefix = "完成首帧边界的连续运镜后，" if first_last_boundary else ""
        camera_line += (
            f"。{boundary_camera_prefix}竖屏单人对白构图只拍「{dialogue_focus}」"
            "一人的面部、上半身与自然口型，"
            "视线朝向画外听者；听者和同场其他人物完全不入画"
        )
    elif dialogue_staging:
        staging_label = "完整走位和人物与场景的距离变化" if dialogue_staging == "spatial" else "双手、剧情道具与接触关系"
        camera_line += (
            f"。动作对白构图：必须完整拍出{staging_label}，说话人口型在动作过程中自然完成；"
            "动作是主交付，对白不能把画面降级为静态大头或原地站桩"
        )
    contact_phase = shot_contact_phase(shot)
    if contact_phase == "established":
        camera_line += (
            "。已接触动作必须从互动轴侧面拍摄，清楚展现肢体与接触点、人物与对象的空间关系，"
            "禁止正面端站摆拍"
        )
    elif contact_phase == "approach":
        camera_line += (
            "。接近/未命中互动必须从互动轴侧面拍摄，清楚展现两者间距，"
            "禁止正面端站或提前改成已接触"
        )
    elif contact_phase == "separated":
        camera_line += (
            "。松开/分离互动必须从互动轴侧面拍摄，清楚展现已分开的间距，"
            "禁止正面端站或重新改成已接触"
        )
    spatial = (shot.spatial_anchor or shot.scene_setting or "").strip()
    if spatial:
        camera_line += f"。保持空间轴线和人物方位：{spatial}"

    # 角色/场景锚点（短）
    anchors = []
    declared_functional_names = typed_functional_identity_names(screenplay)
    for name in visible_names:
        if identity_resolver is not None:
            anchors.append(f"{name}：{identity_resolver.visual_anchor(name)}")
        elif name in bible_map:
            anchors.append(f"{name}：{bible_map[name].appearance_canonical}")
        elif is_collective_role(name):
            anchors.append(f"{name}：{collective_role_anchor(name)}")
        else:
            functional_anchor = functional_extra_anchor(
                name,
                declared_functional_names=declared_functional_names,
            )
            anchors.append(f"{name}：{functional_anchor}")
    character_anchor = "；".join(anchors[:4]) if anchors else "保持人物身份服装一致"
    scene_anchor, scene_geometry, _ = _scene_geometry_contract(shot, bible)
    prop_anchor = ""
    if shot.required_text and (shot.required_text.surface or "").strip():
        prop_anchor = f"文字承载面：{shot.required_text.surface.strip()}"
    height_hint = _equal_height_hint(
        shot, bible, collective_names=collective_names,
    )

    # 转场由最终编辑执行；生成模型只提供干净可重叠的句柄。
    transition_bits: list[str] = []
    if incoming_transition and _clean_transition(incoming_transition):
        transition_bits.append(
            f"最终编辑会以「{_clean_transition(incoming_transition)}」接入本镜；"
            "本镜直接从稳定起始状态开始，不自行重复转场。"
        )
    if outgoing_transition and _clean_transition(outgoing_transition):
        # 不写入 next_first_frame_desc / 下一镜详细内容（PRD 禁止未来剧情）
        target = f"，为切换到「{next_scene.strip()}」留出视觉出口" if next_scene and next_scene.strip() else ""
        transition_bits.append(
            f"最终编辑会以「{_clean_transition(outgoing_transition)}」将本镜连到下一镜{target}；"
            "末尾保留约0.3秒稳定动作结果，不自行生成渐变、闪光或叠化，"
            "不把下一场景拍进本镜。"
        )

    post_text_note = "最终编辑只负责镜间转场、音量归一化"
    if required_text_strategy(shot) == "deterministic_insert":
        post_text_note += "与精确文字插入"
    post_text_note += "；不得依赖后期补齐剧情动作、配音或关键音效。"
    format_block = (
        f"生成 {shot_dur} 秒、{aspect_ratio}、{style} 的完整可直接采用视频。"
        f"{post_text_note}"
        "全程不要任何背景音乐或 BGM；声音只保留指定人声与必要环境音。"
    )
    reference_block = _compile_reference_roles(
        shot, continuity_mode=mode, with_refs=with_refs,
        chained=chained or uses_previous_tail_frame(mode),
        individual_names=individual_names,
        collective_names=collective_names,
        video_generation_mode=video_generation_mode,
        first_frame_source=first_frame_source,
    )
    # 兼容旧 chained/from_scene 调用：仅 action_continuation 才强调尾帧起点
    if uses_previous_tail_frame(mode) and (chained or from_scene or with_last_frame):
        if "强制起始状态" not in reference_block:
            reference_block = (
                "参考图 A 是上一镜真实结束帧，也是本视频 0.0 秒的强制起始状态。\n"
                + reference_block
            )

    action_block = f"主动作：{primary}"
    if full_action and full_action != primary:
        action_block += f"。完整可见执行：{full_action}"
    action_block += (
        "。必须从起始状态连续拍到结束状态，按描述顺序完整呈现每个明示的大形体动作和道具状态变化；"
        "不得只拍站立说话、口型或表情变化来替代动作"
    )
    if transition_bits:
        action_block += "。" + "".join(transition_bits)
    if dialogue_focus:
        if first_last_boundary:
            action_block += (
                f"。首帧边界运镜完成后，对白构图以「{dialogue_focus}」为唯一可见主体；"
                "输入首帧已有听者只能通过连续运镜自然出画，之后保持为画外关系"
            )
        else:
            action_block += (
                f"。对白构图以「{dialogue_focus}」为唯一可见主体；"
                "原动作中提到的听者、围观者和其他角色均视为画外关系，不得画入镜头"
            )
    elif dialogue_staging:
        action_block += "。对白与动作同步发生；必须先保证整段动作路径完整可见，再保证自然口型"
    action_block += "。只完成这一项主要动作，不重演前序剧情，不提前表演下一镜内容。"

    # 明确忽略 prev_action / prev_tail_action 中的完整动作描述（API 兼容保留参数）
    _ = prev_action
    _ = prev_tail_action

    audio_block = _compile_audio_timeline(shot, voice_bible)
    text_block = _compile_text_policy(shot)
    structured_state_block = structured_state_prompt(shot)
    consistency_parts = [
        character_anchor, scene_anchor, prop_anchor, height_hint,
        "人物头身比沿用角色参考的自然比例；参考图裁切大小不代表实体头部大小，禁止跨镜突然大头或幼态化",
        f"全片统一画风：{style}",
    ]
    consistency_block = "\n".join(p for p in consistency_parts if p)
    negative_block = _compile_negative_constraints(
        shot,
        extra_negative,
        mode,
        bible=bible,
        collective_names=collective_names,
        first_last_boundary=first_last_boundary,
    )
    if critique:
        negative_block += "；上一版必须改正：" + "；".join(
            c.strip() for c in critique[:6] if c and c.strip()
        )

    # 固定段落顺序（PRD §11.1）；超长时按优先级压缩，不得截断台词/文字/首尾状态
    sections: list[tuple[str, str, int]] = [
        ("FORMAT", format_block, 5),
        ("REFERENCE ROLES", reference_block, 3),
        ("VISIBLE CAST", visible_cast_block, 1),
        ("START STATE | 0.0s", state_in, 1),
        ("FIRST-LAST CONTINUOUS PATH", boundary_path, 1),
        ("ONE CURRENT ACTION", action_block, 1),
        (f"END STATE | {shot_dur}.0s", state_out + "。结尾稳定，可供下镜承接。", 1),
        ("STRUCTURED CONTINUITY", structured_state_block, 1),
        ("PERSISTENT SCENE GEOMETRY", scene_geometry, 1),
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
    compact_negative_parts = negative_block.split("；")[:6]
    compact_map = {
        "CONSISTENCY": "保持人物身份服装与场景材质光线一致。",
        "REFERENCE ROLES": (
            "仅按角色映射使用参考图；连续动作才把尾帧当 0 秒起点，其余模式重新构图。"
            if uses_previous_tail_frame(mode) else
            "场景图锁固定地标，人物图锁身份服装；只换构图，不改固定物体。"
        ),
        "FORMAT": (
            f"生成 {shot_dur} 秒、{aspect_ratio}、{style} 可用片段；"
            "终剪限转场/音量/指定文字。"
        ),
        "DO NOT": "；".join(compact_negative_parts),
        "CAMERA": (
            f"{render_shot_size}；{camera_angle}；{render_camera_move}"
            + (f"；单人对白近景，仅「{dialogue_focus}」入画" if dialogue_focus else "")
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



SCENE_RUNTIME_CONTRACT = (
    "严格满足上方结构化身份、场景、动作、构图、文字和技术合同；"
    "不添加合同外主体、状态、标记或媒介变化"
)
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
                         next_first_frame_desc: str | None = None,
                         screenplay: EpisodeScreenplay | None = None) -> str:
    """编译“场景关键帧”图像生成 prompt（Seedream 用）：画风 + 场景 + 在场人物锚点 +
    本镜动作的【首图/尾图定格】。生成的图随后作为 Seedance 视频首尾帧。"""
    if kind not in ("head", "tail"):
        raise CompileError(f"未知关键帧类型：{kind}")
    bible_map = {c.name: c for c in bible.characters}
    identity_resolver = _assert_shot_character_contract(
        shot, bible, context="关键帧", screenplay=screenplay,
    )
    declared_functional_names = typed_functional_identity_names(screenplay)
    anchors = "；".join(
        identity_resolver.visual_anchor(name)
        if identity_resolver is not None
        else (
            bible_map[name].appearance_canonical
            if name in bible_map else (
                collective_role_anchor(name)
                if is_collective_role(name)
                else functional_extra_anchor(
                    name,
                    declared_functional_names=declared_functional_names,
                )
            )
        )
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
        SCENE_RUNTIME_CONTRACT,
    ]
    return "。".join(p.strip().rstrip("。") for p in parts if p.strip())


def idem_key(prompt_text: str, image_urls: list[tuple[str, str]] | None = None) -> str:
    payload = prompt_text + "|" + "|".join(f"{u}#{r}" for u, r in (image_urls or []))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def shot_cost_cny(duration_s: int) -> float:
    return round(duration_s * config.VIDEO_PRICE_PER_SECOND, 2)
