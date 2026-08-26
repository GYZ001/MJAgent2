"""分镜台 2.0.0：把映射台 (episode_prep_pack) 的产出直接生成可投喂视频模型的分段提示词。

背景（docs/STORYBOARD_PROMPT_IR_DESIGN.md，2026-08-26 用户拍板）：
- 剧本台已转型映射台（提交 48e01ff），episode_prep_pack 契约升到 2.0.0，
  不再产出 event_chain / hook / cliffhanger。旧的「事件链 -> 大纲 -> 逐镜」
  管线（app/stages.py 的 generate_storyboard_outline 及其调用的一整套
  narrative_plan 相关校验）是为 event_chain 驱动的叙事权威合同设计的，
  对 episode_prep_pack 这种「只出人物/场景/道具映射，不出事件」的输入
  结构性不适用。
- 这一模块是给 episode_prep_pack 输入专设的新生成路径，不复用旧的
  outline/逐镜/repair 状态机：分两阶段调用模型——
    阶段一：把本章原文（按 source_excerpt.index_source_segments 分段，
      与映射台使用的是同一套分段函数，segment_index 对齐）交给模型，
      产出节拍表（beat_sheet）与节拍到段的归组（一段 = 一个叙事单元，
      固定 15 秒 / 3-4 镜）——这一步决定「这一集有几段」，是整个改造的
      支点：取消事件链之后，段数不再由上游给，必须由本阶段从原文推导。
    阶段二：对每一段，把该段对应的原文切片 + 该段涉及的人物/场景/道具
      资源 + 目标模型（Seedance 2.0 / MiniMax H3）的方言约束一起交给
      模型，模型直接产出一整块可复制的 prompt_text——代码不再拼装、
      不挂尾缀（对照 app/video_prompt_ai.py 的 _render_seedance_prompt /
      _render_minimax_h3_prompt，那是本模块要替代的、按草稿字段拼接
      最终字符串的旧路径；这里模型的 prompt_text 只做 strip()，不做
      任何字段级重组）。

持久化形状（用户已拍板，不是本模块自行决定）：一个 15 秒段 = shots 表一行，
段内的 3-4 个镜头切换写在 prompt_text 文本里，不拆成独立数据行。因此
shot_size / camera_move / camera_angle 这类描述单个连续镜头的字段在这里
粒度失效，本模块写入的新架构行一律留空，改用 Shot.storyboard_pack_segment
承载完整的冻结契约段记录（prompt_text / resources / dialogue /
degraded_capabilities / source_segment_indexes / shot_count）；
app/continuity.py、app/validators.py、app/domain/video_ops.py 对应位置的
校验器已按这个 marker 字段显式退役/改判旧的单镜构图假设，而不是让它们
对新架构的行悄悄判错或悄悄放行。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from app import config
from app.db import new_id, now
from app.domain.common import _episode_source_text
from app.harness import model_gateway
from app.schemas import Bible, Dialogue
from app.source_excerpt import SourceSegment, index_source_segments
from app.video_prompt_profiles import VideoPromptProfile

STORYBOARD_PACK_VERSION = "2.0.0"

#: Written to Shot.prompt_contract_version for every row this module writes.
#: This is the single, principled marker every downstream consumer keys off
#: of to know "this row's shot_size/camera_move/first_frame_desc/... are not
#: authoritative -- read storyboard_pack_segment.prompt_text instead". It is
#: a data-derived version tag, not a per-episode/per-shot allowlist.
STORYBOARD_PACK_CONTRACT_MARKER = "storyboard_pack/2.0.0"

SEGMENT_DURATION_S = 15
MIN_SHOTS_PER_SEGMENT = 3
MAX_SHOTS_PER_SEGMENT = 4


# ---------------------------------------------------------------------------
# 方言约束（供第二阶段模型使用；对照 docs/STORYBOARD_PROMPT_IR_DESIGN.md 的
# 对照表与 docs/prompt-skills/{novel-to-storyboard,minimax-h3-prompts}/。
# H3 的字段名与固定语法是接口约定，逐字符照抄；Seedance 是自由散文，按同一
# 精神收窄成可执行规则，不是逐字抄 skill 原文。
# ---------------------------------------------------------------------------

SEEDANCE_DIALECT_INSTRUCTIONS = """\
目标模型：Seedance 2.0（中文自由散文，一整块可直接复制的提示词，不要拆成
JSON 字段或分点罗列）。

- 第一句必须是「电影级预告片质感，多镜头叙事，镜头之间硬切。」——这是触发
  15 秒档多镜头模式的固定锚句，照写，不得省略或改写。
- 用「镜头1（约0-X秒）」「镜头2（约X-Y秒）」……序号排列，本段固定 3-4 镜；
  括号里的秒数只是软提示，不是精确切点，不要为了卡秒数牺牲镜头数。
- 每个镜头描述顺序：一个运镜（推近/拉远/横摇/固定/跟随/环绕，只选一个，不
  要复合运镜）→ 主体（用 @角色名 引用）→ 一个具体动作 → 场景 → 光影。
- 角色第一次出现时用至少三个可视觉验证特征定义（年龄区间、体型脸型、发型
  头饰、服装颜色材质、随身物）；此后每一个「镜头N」都要重新 @点名，并把
  头巾/伤疤/随身物等连续性元素重复写一遍——不能只在开头写一次就假设模型
  记得，这是段内漂移最常见的根因。
- 情绪一律写成面部肌肉动作和肢体动作（例如「眉毛拧起、嘴大张、眼睛瞪圆」），
  不写抽象情绪词（「惊恐」「释然」这类词模型没有稳定映射）。
- 承担叙事功能的关键道具要写成构图约束——「XX始终清晰可见，位于画面XX」，
  不要只写道具被作用的动作（「玉佩砸入水面」会让道具直接消失在水花里）。
- 群像要正向锁人数并加负向排除，例如「画面中只有两名绿袍修士，不出现其他
  人物」，两句缺一都会导致模型自己加人。
- 神通/异能等超自然效果用物理描述代替文化词（「化作长虹」→「一道细长银白
  光带以极高速度横穿画面并留下拖影」）。
- 若这是全片收尾段，最后一镜必须是大远景或缓慢升起拉远的格局镜，不能停在
  人物中近景上。
- 结尾必须有一段「全片贯穿：音频……；风格……；约束……」，音频（环境音/对白/
  配乐）不能留空，约束里必须包含「面部一致、手指正确、人数锁定、无字幕
  水印」。
- 画面中任何需要出现的文字（牌匾、书信、标题）一律写「无字」/「空白」，交给
  后期合成——Seedance 对汉字字形的还原极不稳定，这是能力缺失，不是可选项。
  凡是写了「无字」的地方，必须在 degraded_capabilities 里对应记一条后期文字
  合成清单条目（写清载体是什么、原文应该是什么字）。
"""

MINIMAX_H3_DIALECT_INSTRUCTIONS = """\
Target model: MiniMax H3. The prompt is exactly three fields, field names
copied character-for-character, each separated by a blank line (T2VA mode --
no image-alignment instruction line, since this project uses reference-image
mode instead of first/last-frame chaining):

integrated_multimodal_description: [Shot 1] <style>, <description>... [Shot 2]
At 00:0X.000, the camera cuts to ...

overall_soundscape: <1-4 English sentences>

non_diegetic_music: <1-3 English sentences, or N/A>

Rules:
- integrated_multimodal_description opens with "[Shot 1]" (no timestamp),
  first declaring the overall style (e.g. "Live-action, cinematic" or
  "2D-animated"), then subsequent shots use "[Shot N] At 00:SS.sss, the
  camera cuts to ..." with strictly increasing timestamps. This segment is a
  fixed 15 seconds; write 3-4 Shots total.
- Write all descriptive prose in English. Keep dialogue and any on-screen
  text verbatim in their original language -- do not translate them.
- Camera moves are one natural English sentence combining type + amplitude +
  speed (e.g. "The camera pushes in with small amplitude at slow speed
  toward her hands"), not a stack of tags at the end of the sentence. One
  dominant camera move per shot.
- Speaking characters get a stable ID: (S1), (S2)... reused across shots for
  the same character. Put age/voice/accent context outside the <d> block;
  dialogue text goes verbatim inside: (S1) says: <d>[Chinese] 原话</d>.
  Off-screen voice uses "says in an off-screen voiceover" and must state the
  on-screen character's lips remain closed.
- overall_soundscape must never be left empty -- H3 is audio-visual joint
  generation and an empty field means the model invents uncontrolled sound.
  Only write "N/A" if the user explicitly wants total silence.
- non_diegetic_music: instrument / tempo / rhythm / dynamics language, not
  abstract mood words ("sad music" is invalid; "a slow solo piano note with
  a swelling low string" is valid). No music -> write "N/A".
- On-screen text (signs, letters, titles) is H3's strong suit: quote it
  verbatim in double quotes inside integrated_multimodal_description, e.g.
  reading "靠山宗". Do not translate it.
- A cut must carry new information (subject/space/state/viewpoint/time).
  Reframing alone is a camera move, not a cut.
- Reference character/scene images are attached separately by the platform,
  not embedded as an alignment instruction line; refer to them inline by
  role, e.g. "the character shown in the reference image, wearing ..." --
  give each reference material exactly one stated role, never let two
  references' roles overlap.
"""


def _dialect_for_target_video_model(target_video_model: str) -> tuple[VideoPromptProfile, str, str]:
    """Return (profile, target_model_literal, dialect_instructions).

    ``target_model`` uses the frozen contract's own vocabulary
    ("seedance_2" | "minimax_h3"), derived from the resolved prompt profile
    rather than hard-coded off the raw provider key, so this stays correct
    if a provider's profile binding ever changes.
    """
    from app.video_prompt_profiles import resolve_video_prompt_profile

    profile = resolve_video_prompt_profile(provider=target_video_model)
    if profile.render_format == "minimax_h3_native_fields":
        return profile, "minimax_h3", MINIMAX_H3_DIALECT_INSTRUCTIONS
    return profile, "seedance_2", SEEDANCE_DIALECT_INSTRUCTIONS


# ---------------------------------------------------------------------------
# 阶段一：节拍表 + 分段
# ---------------------------------------------------------------------------

class _AiBeat(BaseModel):
    beat_id: str
    summary: str
    segment_indexes: list[int] = Field(min_length=1)


class _AiSegmentPlan(BaseModel):
    segment_no: int
    synopsis: str
    source_segment_indexes: list[int] = Field(min_length=1)
    beat_ids: list[str] = Field(default_factory=list)


class _AiBeatSheetDraft(BaseModel):
    beat_sheet: list[_AiBeat] = Field(min_length=1)
    segments: list[_AiSegmentPlan] = Field(min_length=1)


def _validate_beat_sheet_draft(
    draft: _AiBeatSheetDraft, *, total_segments: int
) -> list[str]:
    errors: list[str] = []
    beat_ids = {beat.beat_id for beat in draft.beat_sheet}
    if len(beat_ids) != len(draft.beat_sheet):
        errors.append("beat_sheet 中 beat_id 必须唯一")
    for beat in draft.beat_sheet:
        bad = [i for i in beat.segment_indexes if i < 1 or i > total_segments]
        if bad:
            errors.append(f"beat {beat.beat_id} 引用了不存在的原文段号 {bad}")
    expected_nos = list(range(1, len(draft.segments) + 1))
    actual_nos = [s.segment_no for s in draft.segments]
    if actual_nos != expected_nos:
        errors.append(f"segments[].segment_no 必须为连续递增 1..{len(draft.segments)}，当前为 {actual_nos}")
    for seg in draft.segments:
        bad = [i for i in seg.source_segment_indexes if i < 1 or i > total_segments]
        if bad:
            errors.append(f"段 {seg.segment_no} 引用了不存在的原文段号 {bad}")
        unknown_beats = [b for b in seg.beat_ids if b not in beat_ids]
        if unknown_beats:
            errors.append(f"段 {seg.segment_no} 引用了不存在的 beat_id {unknown_beats}")
    return errors


def _manifest_brief_for_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact asset_manifest summary handed to the model as light context.

    Only names/ids/segment_indexes -- not portrait binaries or provenance --
    so phase 1 (which only needs to recognize named entities while drafting
    the beat sheet) doesn't pay for the full manifest payload twice.
    """
    manifest = payload.get("asset_manifest") or {}
    return {
        "characters": [
            {
                "identity_id": c.get("identity_id"),
                "display_name": c.get("display_name"),
                "aliases": c.get("aliases") or [],
                "segment_indexes": c.get("segment_indexes") or [],
            }
            for c in (manifest.get("characters") or [])
        ],
        "scenes": [
            {
                "scene_id": s.get("scene_id"),
                "display_name": s.get("display_name"),
                "segment_indexes": s.get("segment_indexes") or [],
            }
            for s in (manifest.get("scenes") or [])
        ],
        "props": [
            {
                "label": p.get("label"),
                "segment_indexes": p.get("segment_indexes") or [],
            }
            for p in (manifest.get("props") or [])
        ],
    }


async def _generate_beat_sheet(
    *,
    episode_id: str,
    episode_no: int,
    segments: list[SourceSegment],
    payload: dict[str, Any],
) -> _AiBeatSheetDraft:
    source_block = "\n".join(
        f"[段{index}] {segment.text}" for index, segment in enumerate(segments, start=1)
    )
    task_payload = {
        "task": (
            "通读本章原文，列出节拍表（beat_sheet）：每个节拍是一次情绪或信息的变化，"
            "不是一个句子；合并同质描写，删掉内心独白里无法视觉化的部分。然后把节拍按"
            "叙事单元归入段（一个段要能用一句话概括，例如「他扔掉了理想」「反派现身」），"
            "不是按时长平均切；段与段之间硬切。每段固定 15 秒、内含 3-4 个镜头（这不是"
            "你要填的字段，是下一步的产出约束，这里只需要正确分段）。"
        ),
        "rules": [
            "beat_sheet[].segment_indexes 与 segments[].source_segment_indexes 必须引用"
            "下方原文自带的 [段N] 编号，不得虚构或越界",
            "segments[].segment_no 必须从 1 开始连续递增",
            "不是原文每一句话都有剧情意义；无法视觉化的内心独白、纯环境铺垫可以不进入"
            "任何节拍，但已进入节拍的原文不得凭空编造情节",
            "segments[].synopsis 用一句话概括这个段落在讲什么",
            "段落数量由节拍的叙事单元数量决定，不是按原文段数或时长机械平分",
        ],
        "episode_no": episode_no,
        "known_assets": _manifest_brief_for_prompt(payload),
        "source_text_by_segment": source_block,
        "output_schema": _AiBeatSheetDraft.model_json_schema(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return await model_gateway.chat_structured(
        [
            {
                "role": "system",
                "content": (
                    "你是短剧分镜师。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown"
                    "或解释。"
                ),
            },
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
        ],
        model_type=_AiBeatSheetDraft,
        validate=lambda value: _validate_beat_sheet_draft(
            value, total_segments=len(segments)
        ),
        operation_id=f"storyboard_pack_beat_sheet_{episode_id}_{fingerprint}",
        max_tokens=6000,
        format_retry_limit=1,
        semantic_retry_limit=2,
        temperature=0.4,
        call_meta={
            "stage_key": "storyboard_pack_beat_sheet",
            "call_role": "storyboard_beat_sheet",
            "initiator_label": "分镜台节拍表",
            "episode_id": episode_id,
            "contract_version": STORYBOARD_PACK_VERSION,
        },
        repair_context=f"原文共 {len(segments)} 段，段号范围 1..{len(segments)}",
    )


# ---------------------------------------------------------------------------
# 阶段二：逐段提示词
# ---------------------------------------------------------------------------

class _AiDialogueLine(BaseModel):
    speaker_identity_id: str
    line: str
    source_segment_index: int


class _AiResourceCharacter(BaseModel):
    identity_id: str
    portrait_id: str | None = None
    description: str = ""


class _AiResourceScene(BaseModel):
    scene_id: str
    scene_reference_id: str | None = None
    description: str = ""


class _AiResourceProp(BaseModel):
    label: str
    description: str = ""


class _AiSegmentResources(BaseModel):
    characters: list[_AiResourceCharacter] = Field(default_factory=list)
    scenes: list[_AiResourceScene] = Field(default_factory=list)
    props: list[_AiResourceProp] = Field(default_factory=list)


class _AiStoryboardSegmentDraft(BaseModel):
    prompt_text: str = Field(min_length=1)
    shot_count: int = Field(ge=MIN_SHOTS_PER_SEGMENT, le=MAX_SHOTS_PER_SEGMENT)
    dialogue: list[_AiDialogueLine] = Field(default_factory=list)
    resources: _AiSegmentResources = Field(default_factory=_AiSegmentResources)
    degraded_capabilities: list[str] = Field(default_factory=list)


def _segment_relevant_assets(
    payload: dict[str, Any], source_segment_indexes: list[int]
) -> dict[str, Any]:
    wanted = set(source_segment_indexes)
    manifest = payload.get("asset_manifest") or {}

    def _hits(entry: dict[str, Any]) -> bool:
        return bool(wanted & set(entry.get("segment_indexes") or []))

    characters = [c for c in (manifest.get("characters") or []) if _hits(c)]
    scenes = [s for s in (manifest.get("scenes") or []) if _hits(s)]
    props = [p for p in (manifest.get("props") or []) if _hits(p)]
    functional_extras = [f for f in (manifest.get("functional_extras") or []) if _hits(f)]
    appellations = [
        a for a in (payload.get("appellation_map") or [])
        if int(a.get("segment_index") or -1) in wanted
    ]
    return {
        "characters": characters,
        "scenes": scenes,
        "props": props,
        "functional_extras": functional_extras,
        "appellation_map": appellations,
    }


def _validate_segment_draft(
    draft: _AiStoryboardSegmentDraft,
    *,
    known_character_ids: set[str],
    known_scene_ids: set[str],
    source_segment_indexes: list[int],
    dialect_render_format: str,
) -> list[str]:
    errors: list[str] = []
    if len(draft.prompt_text) > config.PROMPT_CHAR_LIMIT:
        errors.append(
            f"prompt_text 长度 {len(draft.prompt_text)} 超过上限 {config.PROMPT_CHAR_LIMIT}"
        )
    allowed_segments = set(source_segment_indexes)
    for index, line in enumerate(draft.dialogue):
        if line.source_segment_index not in allowed_segments:
            errors.append(
                f"dialogue[{index}].source_segment_index={line.source_segment_index} "
                f"不在本段引用的原文段号 {sorted(allowed_segments)} 内"
            )
    for index, character in enumerate(draft.resources.characters):
        if known_character_ids and character.identity_id not in known_character_ids:
            errors.append(
                f"resources.characters[{index}].identity_id="
                f"「{character.identity_id}」不是映射台已知的人物身份"
            )
    for index, scene in enumerate(draft.resources.scenes):
        if known_scene_ids and scene.scene_id not in known_scene_ids:
            errors.append(
                f"resources.scenes[{index}].scene_id=「{scene.scene_id}」不是映射台已知场景"
            )
    if dialect_render_format == "minimax_h3_native_fields":
        for field in ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:"):
            if field not in draft.prompt_text:
                errors.append(f"prompt_text 缺少 H3 固定字段「{field}」")
    return errors


async def _generate_segment_prompt(
    *,
    episode_id: str,
    episode_no: int,
    segment_plan: _AiSegmentPlan,
    beats_by_id: dict[str, _AiBeat],
    segments: list[SourceSegment],
    payload: dict[str, Any],
    target_video_model: str,
    bible: Bible | None,
) -> _AiStoryboardSegmentDraft:
    profile, target_model_literal, dialect_instructions = _dialect_for_target_video_model(
        target_video_model
    )
    source_text = "\n".join(
        f"[段{index}] {segments[index - 1].text}"
        for index in segment_plan.source_segment_indexes
        if 1 <= index <= len(segments)
    )
    relevant = _segment_relevant_assets(payload, segment_plan.source_segment_indexes)
    beat_summaries = [
        {"beat_id": beat_id, "summary": beats_by_id[beat_id].summary}
        for beat_id in segment_plan.beat_ids
        if beat_id in beats_by_id
    ]
    known_character_ids = {
        str(c.get("identity_id") or "") for c in (payload.get("asset_manifest") or {}).get("characters") or []
    }
    known_scene_ids = {
        str(s.get("scene_id") or "") for s in (payload.get("asset_manifest") or {}).get("scenes") or []
    }
    task_payload = {
        "task": (
            "为下方这一段原文和节拍写一整段可直接投喂视频生成模型的提示词（prompt_text）。"
            "prompt_text 必须是完整、可直接复制使用的一整块文本，不要拆成多个片段或只写"
            "关键词——你产出的字符串会被原样保存并原样提交给视频生成接口，不会再被代码"
            "拼接、改写或补充任何后缀。"
        ),
        "segment_no": segment_plan.segment_no,
        "synopsis": segment_plan.synopsis,
        "beats": beat_summaries,
        "duration_s": SEGMENT_DURATION_S,
        "shot_count_range": [MIN_SHOTS_PER_SEGMENT, MAX_SHOTS_PER_SEGMENT],
        "source_segment_indexes": segment_plan.source_segment_indexes,
        "source_text_by_segment": source_text,
        "relevant_assets": relevant,
        "visual_style": (
            bible.world.visual_style_canonical
            if bible is not None and bible.world is not None
            else ""
        ),
        "target_video_model": target_model_literal,
        "dialect_instructions": dialect_instructions,
        # app.video_prompt_profiles 的 SEEDANCE_2_PROFILE/MINIMAX_H3_PROFILE 是
        # 既有的正确接缝（docs/STORYBOARD_PROMPT_IR_DESIGN.md「与既有代码的衔接」），
        # 职责收窄为"交给模型的方言约束"；dialect_instructions 是本模块新写的
        # 详细版本（含 H3 字段名等接口语法），这里把 profile 自带的精简规则也一并
        # 带上作为强化重申，避免两处描述同一模型方言、其中一处不再被任何调用方
        # 读取而悄悄漂移。
        "profile_generation_rules": list(profile.generation_rules),
        "output_contract": {
            "prompt_text": "完整可复制的提示词整块文本，按上面的方言约束写",
            "shot_count": f"{MIN_SHOTS_PER_SEGMENT}-{MAX_SHOTS_PER_SEGMENT} 之间的整数，须与 prompt_text 里实际写的镜头数一致",
            "dialogue": (
                "本段实际出现的台词（可以是原文对话的压缩/改写，不要求逐字，但不得偏离"
                "本段剧情）；每条必须给 speaker_identity_id（引用 relevant_assets.characters "
                "的 identity_id）与 source_segment_index（这句话对应原文的哪一段，必须在 "
                f"{segment_plan.source_segment_indexes} 范围内）"
            ),
            "resources": "本段实际用到的人物/场景/道具，identity_id/scene_id 必须来自 relevant_assets；素材库没有对应图的（scene_reference_id 或 portrait_id 为空）如实留空，不得编造",
            "degraded_capabilities": "本段因模型能力缺失而做的降级处理清单（例如 Seedance 侧的屏上文字改「无字」+ 后期合成说明）；没有降级则留空数组，不得留空字符串占位",
        },
        "output_schema": _AiStoryboardSegmentDraft.model_json_schema(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return await model_gateway.chat_structured(
        [
            {
                "role": "system",
                "content": (
                    "你是短剧分镜师和视频生成提示词撰写者。"
                    f"当前目标模型是 {profile.model_family}。"
                    "只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释；"
                    "prompt_text 字段内部可以换行，但整体是一个字符串。"
                ),
            },
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
        ],
        model_type=_AiStoryboardSegmentDraft,
        validate=lambda value: _validate_segment_draft(
            value,
            known_character_ids=known_character_ids,
            known_scene_ids=known_scene_ids,
            source_segment_indexes=segment_plan.source_segment_indexes,
            dialect_render_format=profile.render_format,
        ),
        operation_id=f"storyboard_pack_segment_{episode_id}_{segment_plan.segment_no}_{fingerprint}",
        max_tokens=6000,
        format_retry_limit=1,
        semantic_retry_limit=2,
        temperature=0.6,
        call_meta={
            "stage_key": "storyboard_pack_segment",
            "call_role": "storyboard_pack_segment",
            "initiator_label": "分镜台段提示词",
            "episode_id": episode_id,
            "segment_no": segment_plan.segment_no,
            "target_video_model": target_video_model,
            "contract_version": STORYBOARD_PACK_VERSION,
        },
        repair_context=f"本段原文段号={segment_plan.source_segment_indexes}",
    )


# ---------------------------------------------------------------------------
# 契约装配与持久化
# ---------------------------------------------------------------------------

class StoryboardPackBeat(BaseModel):
    beat_id: str
    summary: str
    segment_indexes: list[int]


class StoryboardPackSegment(BaseModel):
    segment_no: int
    duration_s: int = SEGMENT_DURATION_S
    synopsis: str
    source_segment_indexes: list[int]
    prompt_text: str
    shot_count: int
    dialogue: list[dict[str, Any]]
    resources: dict[str, Any]
    degraded_capabilities: list[str]


class StoryboardPack(BaseModel):
    storyboard_version: str = STORYBOARD_PACK_VERSION
    episode_no: int
    target_model: str
    beat_sheet: list[StoryboardPackBeat]
    segments: list[StoryboardPackSegment]


def _load_indexed_source_segments(conn, ep) -> list[SourceSegment]:
    """Segment the chapter text exactly the way app.production.prep_pack does.

    Both call sites in prep_pack.py call ``index_source_segments(source_text)``
    with no override (default max_chars=900); this reuses the identical
    function so ``segment_index`` here means the same 1-based position that
    asset_manifest/appellation_map already anchor on.
    """
    source_text = _episode_source_text(conn, ep)
    return index_source_segments(source_text)


async def generate_storyboard_pack(
    episode_id: str,
    *,
    ep: Any,
    conn: Any,
    payload: dict[str, Any],
) -> StoryboardPack:
    """Generate the frozen 2.0.0 storyboard contract for a prep_pack episode.

    Answers "which function decides how many segments this episode has, and
    on what basis" (docs/STORYBOARD_PROMPT_IR_DESIGN.md 交付前必须回答 #1):
    ``_generate_beat_sheet`` -- it reads the full chapter text (not the
    now-empty event_chain) and asks the model to list narrative beats and
    group them into segments; segment count is exactly ``len(draft.segments)``
    from that one call, never computed arithmetically from duration.
    """
    episode_no = int(ep["episode_no"])
    segments = _load_indexed_source_segments(conn, ep)
    if not segments:
        raise ValueError(f"episode {episode_id} 没有可用原文，无法生成分镜")
    target_video_model = str(ep["target_video_model"] or "hiagent").strip() or "hiagent"

    bible: Bible | None = None
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    if project and project["bible_json"]:
        from app.portraits import bible_for_episode

        bible = bible_for_episode(
            ep["project_id"], Bible.model_validate(json.loads(project["bible_json"])), episode_no,
        )

    beat_draft = await _generate_beat_sheet(
        episode_id=episode_id, episode_no=episode_no, segments=segments, payload=payload,
    )
    beats_by_id = {beat.beat_id: beat for beat in beat_draft.beat_sheet}

    async def _one(segment_plan: _AiSegmentPlan) -> StoryboardPackSegment:
        draft = await _generate_segment_prompt(
            episode_id=episode_id,
            episode_no=episode_no,
            segment_plan=segment_plan,
            beats_by_id=beats_by_id,
            segments=segments,
            payload=payload,
            target_video_model=target_video_model,
            bible=bible,
        )
        return StoryboardPackSegment(
            segment_no=segment_plan.segment_no,
            duration_s=SEGMENT_DURATION_S,
            synopsis=segment_plan.synopsis,
            source_segment_indexes=list(segment_plan.source_segment_indexes),
            prompt_text=draft.prompt_text.strip(),
            shot_count=draft.shot_count,
            dialogue=[line.model_dump(mode="json") for line in draft.dialogue],
            resources=draft.resources.model_dump(mode="json"),
            degraded_capabilities=list(draft.degraded_capabilities),
        )

    pack_segments = await asyncio.gather(*(_one(plan) for plan in beat_draft.segments))
    _, target_model_literal, _ = _dialect_for_target_video_model(target_video_model)
    return StoryboardPack(
        episode_no=episode_no,
        target_model=target_model_literal,
        beat_sheet=[
            StoryboardPackBeat(
                beat_id=beat.beat_id, summary=beat.summary, segment_indexes=list(beat.segment_indexes),
            )
            for beat in beat_draft.beat_sheet
        ],
        segments=list(pack_segments),
    )


def _resource_identity_display_names(payload: dict[str, Any], identity_ids: list[str]) -> list[str]:
    by_id = {
        str(c.get("identity_id") or ""): str(c.get("display_name") or c.get("identity_id") or "")
        for c in (payload.get("asset_manifest") or {}).get("characters") or []
    }
    return [by_id.get(identity_id, identity_id) for identity_id in identity_ids]


def persist_storyboard_pack(
    conn,
    episode_id: str,
    ep: Any,
    payload: dict[str, Any],
    pack: StoryboardPack,
    *,
    segments: list[SourceSegment] | None = None,
) -> list[str]:
    """Write one ``shots`` row (+ its adopted ``shot_versions`` row) per segment.

    Answers "does anything post-process the model's prompt_text"
    (docs/STORYBOARD_PROMPT_IR_DESIGN.md 交付前必须回答 #2): no. ``draft.prompt_text``
    is ``.strip()``-ed in ``generate_storyboard_pack`` and then written verbatim
    into ``shot_versions.prompt_text`` below -- there is no render/compile step
    between the model call and persistence, unlike the legacy
    ``_render_seedance_prompt``/``_render_minimax_h3_prompt`` code path in
    app/video_prompt_ai.py that this module replaces for prep_pack episodes.

    One 15s segment = one shots row (user-frozen decision): the 3-4 internal
    shot cuts live inside prompt_text as free text, never split into separate
    rows. shot_size/camera_move/camera_angle/first_frame_desc/last_frame_desc
    are left empty -- they describe a single continuous camera setup, a
    granularity this row no longer has; the marker
    ``prompt_contract_version=storyboard_pack/2.0.0`` is how every consumer
    (app/continuity.py, app/validators.py, app/domain/video_ops.py) knows to
    stop treating those columns as authoritative for this row instead of
    silently failing or silently passing on empty values.
    """
    from app.domain.storyboard_ops import _assert_storyboard_write_authorized

    _assert_storyboard_write_authorized(conn, episode_id, None)
    if segments is None:
        segments = _load_indexed_source_segments(conn, ep)
    conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
    shot_ids: list[str] = []
    for segment in pack.segments:
        character_ids = [
            str(c.get("identity_id") or "") for c in (segment.resources.get("characters") or [])
        ]
        scene_entries = segment.resources.get("scenes") or []
        scene_display_name = str(scene_entries[0].get("display_name") or "") if scene_entries else ""
        source_excerpt = "\n".join(
            segments[i - 1].text for i in segment.source_segment_indexes if 1 <= i <= len(segments)
        )
        shot_id = new_id("shot")
        shot_uid = new_id("shotuid")
        segment_record = segment.model_dump(mode="json")
        segment_record["beat_ids"] = [
            beat.beat_id for beat in pack.beat_sheet
            if set(beat.segment_indexes) & set(segment.source_segment_indexes)
        ]
        segment_record["target_model"] = pack.target_model
        segment_record["storyboard_version"] = pack.storyboard_version
        is_final = segment.segment_no == len(pack.segments)
        dialogues = [
            Dialogue(
                speaker=str(line.get("speaker_identity_id") or ""),
                line=str(line.get("line") or ""),
                emotion="平静",
                delivery="spoken_dialogue",
            ).model_dump()
            for line in segment.dialogue
        ]
        # continuity_mode/transition/first_frame_desc/last_frame_desc describe a
        # single continuous camera setup and are not meaningful once one row
        # covers 3-4 internal cuts; left at their non-committal defaults rather
        # than a fabricated enum value (this row's prompt_contract_version marker
        # is what tells every downstream consumer to stop reading these as
        # authoritative -- see the module docstring and app/continuity.py).
        conn.execute(
            "INSERT INTO shots(id, shot_uid, episode_id, script_id, shot_no, duration_s, "
            "shot_size, camera_move, scene_time, scene_setting, scene_name, characters, "
            "action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, "
            "dialogues, transition, continuity_from_prev, shot_contract_json, "
            "continuity_mode, observed_state_out, storyboard_artifact_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                shot_id, shot_uid, episode_id, None, segment.segment_no, segment.duration_s,
                "", "", "", "",
                scene_display_name or None,
                json.dumps(
                    _resource_identity_display_names(payload, character_ids),
                    ensure_ascii=False,
                ),
                segment.synopsis, "", "",
                source_excerpt,
                segment.synopsis,
                json.dumps(dialogues, ensure_ascii=False),
                "硬切", 0,
                json.dumps(
                    {
                        "storyboard_pack_segment": segment_record,
                        "prompt_contract_version": STORYBOARD_PACK_CONTRACT_MARKER,
                        "is_final": is_final,
                    },
                    ensure_ascii=False,
                ),
                "", "", None,
            ),
        )
        version_id = new_id("shotver")
        conn.execute(
            "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, "
            "status, video_slot_active, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                version_id, shot_id, 1, segment.prompt_text,
                new_id("idem"), "queued", 0, now(),
            ),
        )
        conn.execute(
            "UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id),
        )
        shot_ids.append(shot_id)
    conn.commit()
    return shot_ids


async def run_storyboard_pack_generation(
    episode_id: str,
    *,
    ep: Any,
    conn: Any,
    payload: dict[str, Any],
    resume: bool = True,
):
    """Entry point called from app.storyboard_supervisor.run_storyboard_supervisor
    for every episode whose screenplay_json is an episode_prep_pack payload.

    This intentionally does not touch the legacy checkpoint-driven repair
    state machine (PLANNING_OUTLINE / GENERATING_SHOTS / REPAIRING / ...) that
    the rest of run_storyboard_supervisor implements: that machinery exists to
    incrementally repair a 50-field per-shot narrative contract one shot at a
    time, keyed off screenplay.narrative_plan / screenplay.events, which
    episode_prep_pack (2.0.0) structurally does not have. Each segment here is
    a single self-contained model call (retried internally by
    model_gateway.chat_structured's own format/semantic retry budget), so
    there is no equivalent multi-shot repair loop to run. On success this
    reuses the exact same completion contract the legacy path uses for its
    non-narrative-authority branch (app.storyboard_supervisor.py's own
    ``else: _finalize_storyboard_evidence(episode_id, evaluation.board)`` /
    ``cp.phase = "SUCCEEDED"`` tail) so publish/certificate/evidence and the
    confirmation gate see the same shape of "done" they already know how to
    handle.
    """
    from app.storyboard_supervisor import SupervisorCheckpoint, save_checkpoint
    from app.domain.storyboard_ops import (
        _board_from_shot_rows,
        _finalize_storyboard_evidence,
    )

    if resume:
        existing = conn.execute(
            "SELECT id, shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        if existing and all(
            STORYBOARD_PACK_CONTRACT_MARKER
            in (row["shot_contract_json"] or "") for row in existing
        ) and str(ep["status"] or "") in ("scripted", "confirmed", "generating", "done"):
            # 已经用同一套分镜台 2.0.0 契约生成过，且集状态显示流程已经往前走了
            # （不是半途失败的残留）。resume 语义下不重新调模型、不 DELETE 重灌——
            # 那会连同已经采纳/生成的视频版本一起级联删掉（shots -> shot_versions
            # -> jobs 都是 ON DELETE CASCADE）。直接把已持久化的结果重建成
            # checkpoint 返回。
            rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
            ).fetchall()
            cp = SupervisorCheckpoint(
                episode_id=episode_id,
                phase="SUCCEEDED",
                outcome="SUCCEEDED_READY_FOR_CONFIRM",
                expected_total=len(rows),
                validated_prefix_end=len(rows),
                next_shot_no=len(rows) + 1,
                input_versions={"screenplay_artifact_id": ep["screenplay_artifact_id"]},
            )
            save_checkpoint(cp)
            return cp

    segments = _load_indexed_source_segments(conn, ep)
    pack = await generate_storyboard_pack(episode_id, ep=ep, conn=conn, payload=payload)
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    board = _board_from_shot_rows(rows, int(ep["episode_no"]))
    _finalize_storyboard_evidence(episode_id, board)
    conn.execute(
        "UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?",
        (episode_id,),
    )
    conn.commit()

    cp = SupervisorCheckpoint(
        episode_id=episode_id,
        phase="SUCCEEDED",
        outcome="SUCCEEDED_READY_FOR_CONFIRM",
        expected_total=len(pack.segments),
        validated_prefix_end=len(pack.segments),
        next_shot_no=len(pack.segments) + 1,
        input_versions={
            "screenplay_artifact_id": ep["screenplay_artifact_id"],
        },
    )
    save_checkpoint(cp)
    return cp
