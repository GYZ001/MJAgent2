"""统一有效口播合同（PRD：分镜 VAL-422 一致性与主线覆盖修复方案 §4.1）。

事故根因 R1：`dialogues` / `audio_timeline` / `source_excerpt` 三处都能被当成口播真相源，
字数从 timeline 统计、关键台词从 dialogues 统计，同一镜头因此能同时被判定为
「有 37 字口播」和「关键台词不存在」。

本模块是口播的唯一入口：字数、关键台词覆盖、声轨统计、prompt 编译、UI 预览、确认门
一律读 `effective_spoken_segments`，不得各自拼装台词。`source_excerpt` 退化为来源审计证据，
不能证明「关键台词已经真的说出口」。

依赖边界：只允许依赖 `app.config` 与 `app.schemas`，避免与 `app.continuity` 形成循环。
"""
from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from app import config
from app.schemas import AudioTimelineItem, Dialogue, Shot, VoiceCanonical

# 真实口播 delivery：只有这两类算「观众能听到有人在说话」。
# ambient_sound 是环境声、narration 已被产品合同禁止，都不计入口播。
SPOKEN_DELIVERIES: frozenset[str] = frozenset({"spoken_dialogue", "offscreen_voice"})

# 稳定 rule_id（PRD §4.7）：同一规则、同一主体只允许产生一条诊断。
RULE_SPOKEN_CAPACITY = "SHOT.SPOKEN.CAPACITY"
RULE_SPOKEN_COHERENCE = "SHOT.SPOKEN.COHERENCE"
RULE_SPOKEN_TIMELINE = "SHOT.SPOKEN.TIMELINE"
RULE_SPOKEN_SPEAKERS = "SHOT.SPOKEN.SPEAKERS"

SpokenContractStatus = Literal["coherent", "conflict", "legacy"]


class SpokenSegment(BaseModel):
    """一段有效口播。所有下游模块共用这一份结构，不再各自从原始字段拼装。"""

    speaker_id: str
    text: str
    delivery: Literal["spoken_dialogue", "offscreen_voice"] = "spoken_dialogue"
    emotion: str = "平静"
    start_s: float | None = None
    end_s: float | None = None
    lip_sync: bool = True
    voice_canonical: str = ""


class SpokenIssue(BaseModel):
    """确定性口播诊断。message 兼容现有 `list[str]` 校验管线，rule_id 供去重使用。"""

    code: str
    rule_id: str
    shot_no: int
    message: str
    severity: Literal["blocker", "warning"] = "blocker"
    evidence: dict[str, Any] = Field(default_factory=dict)
    repair_options: list[str] = Field(default_factory=list)


class SyncResult(BaseModel):
    """`synchronize_spoken_contract` 的结果：做了什么、结论是什么、还剩哪些 blocker。"""

    status: SpokenContractStatus
    changed: bool = False
    actions: list[str] = Field(default_factory=list)
    issues: list[SpokenIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "blocker" for issue in self.issues)


# ---------- 基础文本口径 ----------

def content_char_count(text: str | None) -> int:
    """口播纯文字字数：去空白与 Unicode 标点，只计汉字/字母/数字等。"""
    total = 0
    for ch in text or "":
        if ch.isspace():
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        total += 1
    return total


def onscreen_text_for_capacity(required_text: object) -> str:
    """Return only text that the viewer must visually read in this shot."""
    if required_text is None:
        return ""
    if isinstance(required_text, dict):
        strategy = str(
            required_text.get("strategy") or "deterministic_insert"
        ).strip().casefold()
        exact_text = required_text.get("exact_text")
    else:
        strategy = str(
            getattr(required_text, "strategy", None) or "deterministic_insert"
        ).strip().casefold()
        exact_text = getattr(required_text, "exact_text", "")
    if strategy in {"audio_only", "none"}:
        return ""
    return str(exact_text or "")


def _condense(text: str | None) -> str:
    """比较用的归一化文本：标点/空白不参与发声，不应制造伪冲突。"""
    return "".join(
        ch for ch in (text or "")
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def max_speech_chars(duration_s: int) -> int:
    """单镜台词纯文字上限；与 config.max_spoken_chars_for_duration 同口径。"""
    return config.max_spoken_chars_for_duration(duration_s)


MAX_SHOT_SPOKEN_CHARS = config.MAX_SPOKEN_CHARS_PER_SHOT


def _visible_names(shot: Shot) -> set[str]:
    return {str(x) for x in (shot.characters_visible or shot.characters or [])}


def _resolve_delivery(speaker: str, declared: str | None, visible: set[str]) -> str:
    """本镜某说话人的规范发声方式：`dialogues` 与 `audio_timeline` 共用这一条规则。

    事故根因 R1 的姊妹缺陷：可见性降级过去只写在 dialogues 派生里，timeline 派生原样
    照搬 `item.type`，于是同一句「非可见说话人 + spoken_dialogue」在两侧被归一成不同
    delivery，`_diff_segments` 判成伪冲突（shot_no=83：宗门绿袍修士2 不在 characters_visible，
    两侧都存 spoken_dialogue，却被判 SPOKEN_CONTRACT_CONFLICT）。发声方式只有一条业务
    定义，必须在唯一入口收敛，不能分叉到两条派生路径。

    - 非法/空 delivery → spoken_dialogue（默认可听、可见）；
    - 其余情况保留声明值，两侧真实分歧仍会照常触发冲突。

    可见性不得改写语义。原始合同明确声明 ``spoken_dialogue``
    却漏了 speaker 时，必须由确定性门禁拒绝，不能为了让数据
    自洽而静默篡改成 ``offscreen_voice``。
    """
    delivery = (declared or "spoken_dialogue").strip()
    if delivery not in SPOKEN_DELIVERIES:
        delivery = "spoken_dialogue"
    return delivery


def _canonicalize_delivery_fields(shot: Shot) -> bool:
    """把规范发声方式写回原始 `audio_timeline` / `dialogues`，返回是否有改动。

    只矫正真实口播轨（spoken_dialogue/offscreen_voice）的 delivery 与配套的 lip_sync；
    ambient_sound 等非口播轨、文本、时间轴一律不动。派生段读的是同一条 `_resolve_delivery`，
    因此这一步只是把已经生效的口径固化到落库字段，保证下游原始读取者口径一致。
    """
    visible = _visible_names(shot)
    changed = False
    for item in shot.audio_timeline or []:
        if item.type not in SPOKEN_DELIVERIES:
            continue
        speaker = (item.speaker_id or "").strip()
        delivery = _resolve_delivery(speaker, item.type, visible)
        lip_sync = delivery == "spoken_dialogue"
        if item.type != delivery or bool(item.lip_sync) != lip_sync:
            item.type = delivery
            item.lip_sync = lip_sync
            changed = True
    for dialogue in shot.dialogues or []:
        if not (dialogue.line or "").strip():
            continue
        speaker = (dialogue.speaker or "").strip()
        delivery = _resolve_delivery(speaker, getattr(dialogue, "delivery", None), visible)
        if getattr(dialogue, "delivery", None) != delivery:
            dialogue.delivery = delivery
            changed = True
    return changed


def _voice_map(voice_bible: Iterable[VoiceCanonical] | None) -> dict[str, str]:
    voices: dict[str, str] = {}
    for voice in voice_bible or []:
        for token in (
            voice.speaker_id,
            getattr(voice, "display_name", ""),
        ):
            value = str(token or "").strip()
            if value:
                voices[value] = voice.voice_canonical
    return voices


# ---------- 有效口播段 ----------

def segments_from_timeline(shot: Shot) -> list[SpokenSegment]:
    """从 audio_timeline 的真实口播轨派生有效口播段。

    delivery 走 `_resolve_delivery`：与 dialogues 派生同一条可见性规则，
    避免「非可见说话人在 timeline 存 spoken_dialogue、在 dialogues 被降级」造成伪冲突。
    """
    visible = _visible_names(shot)
    segments: list[SpokenSegment] = []
    for item in shot.audio_timeline or []:
        if item.type not in SPOKEN_DELIVERIES:
            continue
        if not (item.text or "").strip():
            continue
        speaker = (item.speaker_id or "").strip()
        delivery = _resolve_delivery(speaker, item.type, visible)
        segments.append(SpokenSegment(
            speaker_id=speaker,
            text=(item.text or "").strip(),
            delivery=delivery,  # type: ignore[arg-type]
            emotion=item.emotion or "平静",
            start_s=item.start_s,
            end_s=item.end_s,
            lip_sync=delivery == "spoken_dialogue",
            voice_canonical=item.voice_canonical or "",
        ))
    return segments


def segments_from_dialogues(
    shot: Shot, voice_bible: Iterable[VoiceCanonical] | None = None
) -> list[SpokenSegment]:
    """从 dialogues 派生有效口播段（narration 已被禁止，统一降级为画外音）。"""
    voices = _voice_map(voice_bible)
    visible = _visible_names(shot)
    segments: list[SpokenSegment] = []
    for dialogue in shot.dialogues or []:
        text = (dialogue.line or "").strip()
        if not text:
            continue
        speaker = (dialogue.speaker or "").strip()
        delivery = _resolve_delivery(speaker, getattr(dialogue, "delivery", None), visible)
        segments.append(SpokenSegment(
            speaker_id=speaker,
            text=text,
            delivery=delivery,  # type: ignore[arg-type]
            emotion=dialogue.emotion or "平静",
            lip_sync=delivery == "spoken_dialogue",
            voice_canonical=voices.get(speaker, ""),
        ))
    return segments


def effective_spoken_segments(
    shot: Shot, voice_bible: Iterable[VoiceCanonical] | None = None
) -> list[SpokenSegment]:
    """本镜的唯一有效口播内容。

    timeline 存在真实口播轨时以它为准（它带时间与口型信息），否则回退 dialogues。
    两侧分叉不在这里静默择一——那正是事故根因；分叉由 `validate_spoken_contract` 报 blocker。
    """
    timeline_segments = segments_from_timeline(shot)
    if timeline_segments:
        return timeline_segments
    return segments_from_dialogues(shot, voice_bible)


def spoken_text_of(shot: Shot) -> str:
    """本镜真实说出口的全部文本（关键台词覆盖的唯一依据）。"""
    return "".join(segment.text for segment in effective_spoken_segments(shot))


def spoken_char_total(shot: Shot) -> int:
    """本镜真实台词纯文字字数（不计标点、不计旁白）。"""
    return sum(content_char_count(segment.text) for segment in effective_spoken_segments(shot))


def spoken_speakers(shot: Shot) -> list[str]:
    """按出场顺序去重的主说话人列表。"""
    speakers: list[str] = []
    for segment in effective_spoken_segments(shot):
        if segment.speaker_id and segment.speaker_id not in speakers:
            speakers.append(segment.speaker_id)
    return speakers


# ---------- 双向重建 ----------

def build_timeline_from_segments(
    shot: Shot,
    segments: list[SpokenSegment],
    voice_bible: Iterable[VoiceCanonical] | None = None,
) -> list[AudioTimelineItem]:
    """按 ~3.5 字/秒把有效口播段确定性排成时间轴；无口播时保留环境声轨。"""
    voices = _voice_map(voice_bible)
    duration = float(shot.duration_s or config.DEFAULT_VIDEO_DURATION_S)
    items: list[AudioTimelineItem] = []
    cursor = 0.3
    for segment in segments:
        chars = content_char_count(segment.text)
        need = max(0.8, chars / 3.5) if chars else 0.8
        end = min(duration - 0.2, cursor + need)
        if end <= cursor:
            end = min(duration, cursor + 0.6)
        items.append(AudioTimelineItem(
            start_s=round(cursor, 2),
            end_s=round(end, 2),
            type=segment.delivery,
            speaker_id=segment.speaker_id or None,
            text=segment.text,
            lip_sync=segment.delivery == "spoken_dialogue",
            emotion=segment.emotion or "平静",
            voice_canonical=segment.voice_canonical or voices.get(segment.speaker_id, ""),
        ))
        cursor = end
    if not items:
        items.append(AudioTimelineItem(
            start_s=0.0, end_s=duration, type="ambient_sound",
            text="仅保留与画面匹配的自然环境声，不要额外台词或旁白",
        ))
    elif cursor < duration - 0.2:
        items.append(AudioTimelineItem(
            start_s=round(cursor, 2), end_s=duration, type="ambient_sound",
            text="收束为自然环境声，不新增台词",
        ))
    return items


def dialogues_from_segments(segments: list[SpokenSegment]) -> list[Dialogue]:
    """从有效口播段派生 dialogues，保留 delivery 以区分画内/画外。"""
    return [
        Dialogue(
            speaker=segment.speaker_id,
            line=segment.text,
            emotion=segment.emotion or "平静",
            delivery=segment.delivery,
        )
        for segment in segments
    ]


# ---------- 一致性判定 ----------

def _identity(segment: SpokenSegment) -> tuple[str, str, str]:
    return (segment.speaker_id, _condense(segment.text), segment.delivery)


def _diff_segments(
    left: list[SpokenSegment], right: list[SpokenSegment]
) -> list[dict[str, Any]]:
    """逐段比较说话人、文本、delivery、顺序；返回具体差异而不是一句「不一致」。"""
    diffs: list[dict[str, Any]] = []
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a is not None and b is not None and _identity(a) == _identity(b):
            continue
        diffs.append({
            "index": index,
            "dialogues": None if a is None else {
                "speaker_id": a.speaker_id, "text": a.text, "delivery": a.delivery,
            },
            "timeline": None if b is None else {
                "speaker_id": b.speaker_id, "text": b.text, "delivery": b.delivery,
            },
        })
    return diffs


def _conflict_issue(shot: Shot, diffs: list[dict[str, Any]]) -> SpokenIssue:
    first = diffs[0]
    dial_item = first["dialogues"] or {}
    timeline_item = first["timeline"] or {}

    def _describe(item: dict[str, Any]) -> str:
        if not item:
            return "（缺失）"
        return (
            f"{item.get('speaker_id') or '未知说话人'}｜"
            f"{item.get('delivery') or '未知发声方式'}｜"
            f"{item.get('text') or '（空文本）'}"
        )

    dial = _describe(dial_item)
    tl = _describe(timeline_item)
    return SpokenIssue(
        code="SPOKEN_CONTRACT_CONFLICT",
        rule_id=RULE_SPOKEN_COHERENCE,
        shot_no=shot.shot_no,
        message=(
            f"shot_no={shot.shot_no} dialogues 与 audio_timeline 的口播内容分叉（第 {first['index'] + 1} 段："
            f"dialogues=「{dial}」/ timeline=「{tl}」）；说话人、文本、发声方式和顺序必须完全一致，"
            "spoken_dialogue 的角色必须可见且时间轴 lip_sync=true；画外发声则两侧都用 offscreen_voice。"
            "同一镜头只能有一套有效口播，"
            "请选择以台词为准重建时间轴，或以时间轴为准重建台词，系统不会静默择一"
        ),
        evidence={
            "diff_count": len(diffs),
            "diffs": diffs[:5],
            "dialogues_text": "".join(
                (d["dialogues"] or {}).get("text", "") for d in diffs[:5]
            ),
            "timeline_text": "".join(
                (d["timeline"] or {}).get("text", "") for d in diffs[:5]
            ),
        },
        repair_options=["rebuild_timeline_from_dialogues", "rebuild_dialogues_from_timeline"],
    )


def capacity_issue(shot: Shot) -> SpokenIssue | None:
    """口播容量的唯一实现（PRD §4.7：删除 validate_storyboard 内的重复计算）。"""
    chars = spoken_char_total(shot)
    limit = max_speech_chars(shot.duration_s)
    if chars <= limit:
        return None
    return SpokenIssue(
        code="SPOKEN_CAPACITY_EXCEEDED",
        rule_id=RULE_SPOKEN_CAPACITY,
        shot_no=shot.shot_no,
        message=(
            f"shot_no={shot.shot_no} 台词纯文字 {chars} 字（不计标点），超过 {shot.duration_s}s 口播上限 {limit} 字；"
            f"请缩短台词、拆镜或增加合理时长"
        ),
        evidence={
            "chars": chars,
            "limit": limit,
            "duration_s": shot.duration_s,
            "max_chars": MAX_SHOT_SPOKEN_CHARS,
        },
    )


def _timeline_issues(shot: Shot) -> list[SpokenIssue]:
    issues: list[SpokenIssue] = []
    duration = float(shot.duration_s or config.DEFAULT_VIDEO_DURATION_S)
    spoken = [item for item in (shot.audio_timeline or []) if item.type in SPOKEN_DELIVERIES]
    visible = _visible_names(shot)
    previous_end: float | None = None
    for item in spoken:
        speaker = str(item.speaker_id or "").strip()
        if item.type == "spoken_dialogue" and (
            not speaker or speaker not in visible or not bool(item.lip_sync)
        ):
            issues.append(SpokenIssue(
                code="SPOKEN_VISIBLE_SPEAKER_INVALID",
                rule_id=RULE_SPOKEN_SPEAKERS,
                shot_no=shot.shot_no,
                message=(
                    f"shot_no={shot.shot_no} spoken_dialogue 说话人"
                    f"「{speaker or '未知'}」必须在画面可见人物中且 "
                    "lip_sync=true；若业务语义确为画外发声，必须由上游"
                    "显式声明 offscreen_voice，系统不会静默改写"
                ),
                evidence={
                    "speaker_id": speaker,
                    "delivery": item.type,
                    "lip_sync": bool(item.lip_sync),
                    "visible_characters": sorted(visible),
                },
                repair_options=[
                    "add_speaker_to_visible_characters",
                    "explicitly_author_offscreen_voice",
                ],
            ))
            continue
        if item.type == "offscreen_voice" and bool(item.lip_sync):
            issues.append(SpokenIssue(
                code="SPOKEN_OFFSCREEN_LIPSYNC_INVALID",
                rule_id=RULE_SPOKEN_SPEAKERS,
                shot_no=shot.shot_no,
                message=(
                    f"shot_no={shot.shot_no} offscreen_voice 不得启用 lip_sync"
                ),
                evidence={"speaker_id": speaker, "lip_sync": True},
                repair_options=["disable_lip_sync"],
            ))
            continue
        start, end = float(item.start_s or 0.0), float(item.end_s or 0.0)
        if end < start or start < 0 or end > duration + 1e-6:
            issues.append(SpokenIssue(
                code="SPOKEN_TIMELINE_OUT_OF_RANGE",
                rule_id=RULE_SPOKEN_TIMELINE,
                shot_no=shot.shot_no,
                message=(
                    f"shot_no={shot.shot_no} 口播时间段 [{start}, {end}] 超出本镜 {duration}s 时长或起止颠倒；"
                    "请把口播时间轴收进镜头时长内"
                ),
                evidence={"start_s": start, "end_s": end, "duration_s": duration},
            ))
            break
        if previous_end is not None and start + 1e-6 < previous_end:
            issues.append(SpokenIssue(
                code="SPOKEN_TIMELINE_OVERLAP",
                rule_id=RULE_SPOKEN_TIMELINE,
                shot_no=shot.shot_no,
                message=(
                    f"shot_no={shot.shot_no} 口播时间段在 {start}s 处与上一段（结束于 {previous_end}s）非法重叠；"
                    "两个人不能同时占用同一段口播时间"
                ),
                evidence={"start_s": start, "previous_end_s": previous_end},
            ))
            break
        previous_end = end
    return issues


def validate_spoken_contract(shot: Shot) -> list[SpokenIssue]:
    """单镜口播合同确定性校验：分叉、时间轴合法性、容量。"""
    issues: list[SpokenIssue] = []
    timeline_segments = segments_from_timeline(shot)
    dialogue_segments = segments_from_dialogues(shot)
    if timeline_segments and dialogue_segments:
        diffs = _diff_segments(dialogue_segments, timeline_segments)
        if diffs:
            issues.append(_conflict_issue(shot, diffs))
    issues.extend(_timeline_issues(shot))
    capacity = capacity_issue(shot)
    if capacity is not None:
        issues.append(capacity)
    return issues


# ---------- 同步 ----------

def synchronize_spoken_contract(
    shot: Shot,
    changed_fields: Iterable[str] | None = None,
    voice_bible: Iterable[VoiceCanonical] | None = None,
) -> SyncResult:
    """把 `dialogues` 与 `audio_timeline` 收敛成同一套有效口播（PRD §4.1.1）。

    `changed_fields` 表达「这次是谁被改了」，据此决定派生方向：
    只改 dialogues → 重建 timeline；只改 timeline → 派生 dialogues；
    两边都改或来源不明 → 必须完全一致，否则标记 conflict 且不静默覆盖。
    """
    changed = set(changed_fields or ())
    voices = list(voice_bible or [])
    actions: list[str] = []
    mutated = False

    # 产品合同：禁止旁白轨；它既不是台词也不是环境声，必须先剥离再判定一致性。
    if shot.audio_timeline:
        kept = [item for item in shot.audio_timeline if item.type != "narration"]
        if len(kept) != len(shot.audio_timeline):
            shot.audio_timeline = kept
            mutated = True
            actions.append("stripped_narration_track")

    # 发声方式收敛：可见性降级过去只作用在派生段上，原始字段却仍留着 spoken_dialogue，
    # 下游直接读 audio_timeline.type / dialogues.delivery 的编译与冷评（compiler、narrative）
    # 会把画外说话人当成画内开口。这里把规范 delivery 写回原始字段，保证上下游读到同一口径。
    if _canonicalize_delivery_fields(shot):
        mutated = True
        actions.append("canonicalized_delivery")

    timeline_segments = segments_from_timeline(shot)
    dialogue_segments = segments_from_dialogues(shot, voices)

    def _rebuild_timeline() -> None:
        nonlocal mutated
        shot.audio_timeline = build_timeline_from_segments(shot, dialogue_segments, voices)
        mutated = True
        actions.append("rebuilt_timeline_from_dialogues")

    def _rebuild_dialogues() -> None:
        nonlocal mutated
        shot.dialogues = dialogues_from_segments(timeline_segments)
        mutated = True
        actions.append("rebuilt_dialogues_from_timeline")

    dialogues_only = changed == {"dialogues"}
    timeline_only = changed == {"audio_timeline"}

    if dialogues_only:
        _rebuild_timeline()
    elif timeline_only:
        _rebuild_dialogues()
    elif not timeline_segments and dialogue_segments:
        _rebuild_timeline()
    elif timeline_segments and not dialogue_segments:
        _rebuild_dialogues()
    elif timeline_segments and dialogue_segments:
        diffs = _diff_segments(dialogue_segments, timeline_segments)
        if diffs:
            shot.spoken_contract_status = "conflict"
            try:
                from app.observability.metrics import inc
                inc("spoken_contract_conflict_total", shot_no=shot.shot_no, source="synchronize")
            except Exception:  # noqa: BLE001
                pass
            return SyncResult(
                status="conflict",
                changed=mutated,
                actions=actions,
                issues=[_conflict_issue(shot, diffs), *_post_sync_issues(shot)],
            )
    elif not shot.audio_timeline:
        # 无台词镜：补一条环境声轨，保证下游编译拿到完整时间轴。
        shot.audio_timeline = build_timeline_from_segments(shot, [], voices)
        mutated = True
        actions.append("filled_ambient_timeline")

    shot.spoken_contract_status = "coherent"
    return SyncResult(
        status="coherent", changed=mutated, actions=actions, issues=_post_sync_issues(shot)
    )


def _post_sync_issues(shot: Shot) -> list[SpokenIssue]:
    """同步后仍然成立的问题：容量与时间轴合法性（分叉已单独处理）。"""
    issues = _timeline_issues(shot)
    capacity = capacity_issue(shot)
    if capacity is not None:
        issues.append(capacity)
    return issues


def audit_legacy_spoken_contract(shot: Shot) -> SpokenContractStatus:
    """旧数据只读审计（PRD §6.1）：不修改任何字段，只给出结论。"""
    timeline_segments = segments_from_timeline(shot)
    dialogue_segments = segments_from_dialogues(shot)
    if timeline_segments and dialogue_segments:
        return "conflict" if _diff_segments(dialogue_segments, timeline_segments) else "coherent"
    if timeline_segments or dialogue_segments:
        return "legacy"
    return "coherent"
