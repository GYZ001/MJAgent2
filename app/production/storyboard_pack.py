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
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.schemas import Bible, Dialogue
from app.source_excerpt import SourceSegment, index_source_segments
from app.video_prompt_profiles import VideoPromptProfile

#: 2.0.1（真实 EP1 回归，ep_3d523ff4d0a4/run_46660b74d025，三个逐段核对发现
#: 的产出缺陷）：送模型的 task_payload 形状变了——relevant_assets.
#: characters[]/functional_extras[]/scenes[] 新增世界书标准外观/场景锚点
#: （appearance/scene_canonical），phase 2 新增 rules[] 三条自洽要求；两个
#: 方言指令块也各补了一条硬要求。持久化契约（StoryboardPack/
#: StoryboardPackSegment 的字段名与形状）没有变，只是补丁级修正，不是 minor
#: ——但必须换版本号：不换的话 run_storyboard_pack_generation 的 resume 分支
#: 会看见 EP1 已持久化 shots 的 shot_contract_json 里仍带着旧版
#: STORYBOARD_PACK_CONTRACT_MARKER，判定"已经用同一套契约生成过"直接复用
#: 旧结果，不会真的用新 prompt 重新调模型（resume 短路机制本身不动，只是
#: marker 值变了才会让它对旧行判"不算数"）。
STORYBOARD_PACK_VERSION = "2.0.1"

#: Written to Shot.prompt_contract_version for every row this module writes.
#: This is the single, principled marker every downstream consumer keys off
#: of to know "this row's shot_size/camera_move/first_frame_desc/... are not
#: authoritative -- read storyboard_pack_segment.prompt_text instead". It is
#: a data-derived version tag, not a per-episode/per-shot allowlist.
STORYBOARD_PACK_CONTRACT_MARKER = "storyboard_pack/2.0.1"

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
  记得，这是段内漂移最常见的根因。relevant_assets 里每个角色/场景都带一个
  外观/场景字段（角色是 appearance，场景是 scene_canonical）：内容是一段
  具体描述时，那就是这个角色/场景在本集的标准锚点，第一次出现时的特征定义
  必须逐字沿用这段描述本身，不得改写、精简、替换或按本段情境调整；内容是
  「没有标准外观/场景……」这类说明文字时，才由你自行确定特征，并让同一
  角色/场景在本集所有出现它的镜头里沿用同一套自定特征。
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
  水印」。dialogue[] 里的每一句台词都必须在这段音频描述里用引号带出原话、
  逐句出现，不能只写「XX说话声」这类概括；反过来，音频描述里用引号写出的
  台词原话也必须逐句同时登记进 dialogue[]——两处台词是同一份清单的两种
  呈现，不是各自独立的两份内容。
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


# ---------------------------------------------------------------------------
# 世界书标准外观/场景锚点接入（问题一修复，2026-08-26 真实 EP1 回归：孟浩在
# 10 段里换了三套衣服）。根因不是模型能力，是管道没接上——prep_pack 装配
# asset_manifest.characters[]/scenes[] 时只写 identity_id/display_name/
# portrait_id/scene_reference_id 等身份字段，从不带外观/场景描述本身；模型
# 只能从自己这一段的原文现推，原文没写衣着的段落只能各段各编。世界书里的
# 标准外观/场景锚点（character_portraits.appearance / scene_references.
# scene_canonical）一直都在，只是没被送给模型。
# ---------------------------------------------------------------------------

_NO_CANONICAL_APPEARANCE_NOTE = (
    "素材库没有为这个角色建立标准外观定妆照（群演/一次性人物，没有定妆照）："
    "由你在本集第一次出现这个角色时自行确定其外观特征（年龄体型、发型头饰、"
    "服装颜色材质、随身物等可视信息），并在本集所有涉及这个角色的段落里原样"
    "沿用同一套自定特征，不得每段重新编写。"
)

_NO_CANONICAL_SCENE_NOTE = (
    "素材库没有为这个场景建立标准场景描述：由你在本集第一次出现这个场景时"
    "自行确定其可视特征（空间格局、主要陈设、光线氛围等），并在本集所有涉及"
    "这个场景的段落里原样沿用同一套自定特征，不得每段重新编写。"
)


def _character_canonical_appearance(conn, portrait_id: str | None) -> str | None:
    """这个已解析 portrait_id 对应的世界书标准外观锚点串。

    ``portrait_id`` 在传入这里之前，已经由映射台
    ``app.production.prep_pack._resolve_portrait_id`` 按本集集号在
    ``character_portraits.ep_start``/``ep_end`` 区间里选定过一次（见该函数
    与 asset_manifest.characters[] 的装配处）——本函数只按这个已选定的 id
    取值，不重新做一遍区间选择，选取逻辑只有一套。
    """
    if not portrait_id:
        return None
    row = conn.execute(
        "SELECT appearance FROM character_portraits WHERE id=?", (portrait_id,),
    ).fetchone()
    if row is None:
        return None
    appearance = str(row["appearance"] or "").strip()
    return appearance or None


def _scene_canonical_description(conn, scene_reference_id: str | None) -> str | None:
    """场景侧同构：``scene_reference_id`` 同样已由
    ``app.production.prep_pack._resolve_scene_reference_id`` 按本集集号选定
    过一次，这里只按这个已选定的 id 取 ``scene_references.scene_canonical``。
    """
    if not scene_reference_id:
        return None
    row = conn.execute(
        "SELECT scene_canonical FROM scene_references WHERE id=?", (scene_reference_id,),
    ).fetchone()
    if row is None:
        return None
    canonical = str(row["scene_canonical"] or "").strip()
    return canonical or None


def _enrich_asset_manifest_canonical_visuals(conn, payload: dict[str, Any]) -> None:
    """原地把世界书标准外观/场景锚点补进 ``payload["asset_manifest"]``。

    在 ``_generate_beat_sheet``/``_generate_segment_prompt`` 之前调用一次
    （逐 identity 只查一次，不在逐段循环里重复查询）；``_segment_relevant_
    assets`` 之后按段筛选时拿到的就是同一批已带 appearance/scene_canonical
    的条目对象，不需要再改那个函数。只多一次查询，不建新表也不建新缓存。

    ``functional_extras``（群演/一次性人物）没有 portrait_id、天生没有标准
    外观：这里显式写一条说明而不是留空——留空会被模型读成"没有任何关于外观
    的信息"，导致同一群演在不同段落里各编一套，是问题一的同一种漂移换了个
    没有 portrait_id 的马甲，不是不同的问题。
    """
    manifest = payload.get("asset_manifest") or {}
    for character in manifest.get("characters") or []:
        appearance = _character_canonical_appearance(conn, character.get("portrait_id"))
        character["appearance"] = appearance or _NO_CANONICAL_APPEARANCE_NOTE
    for extra in manifest.get("functional_extras") or []:
        extra["appearance"] = _NO_CANONICAL_APPEARANCE_NOTE
    for scene in manifest.get("scenes") or []:
        canonical = _scene_canonical_description(conn, scene.get("scene_reference_id"))
        scene["scene_canonical"] = canonical or _NO_CANONICAL_SCENE_NOTE


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
    dialect_render_format: str,
) -> list[str]:
    """Blocking (format-only) checks -- the only things that can make
    ``model_gateway.chat_structured`` retry or fail this segment.

    2026-08-26（用户拍板，第一版分镜提示词不设任何内容门禁）：内容类判断
    （台词说话人是否在场、对白/资源是否能溯源到映射台已知身份）一律不许
    再出现在这个函数里——不是因为它们不该算，是因为算完之后的结论不能是
    "拦截生成"。它们移到 ``_segment_content_advisories``，在模型已经产出
    通过格式校验的 draft 之后再算一遍，结果记进 degraded_capabilities，
    不参与重试/失败判定。这里只留"下一环节会真的用不了"的形状问题：
    prompt_text 是否为空/超限、H3 的三个固定字段名是否存在——写错字段名
    H3 不会报错，只会静默降级成自由文本理解，这条不是内容质量判断，是
    接口语法对不对。
    """
    errors: list[str] = []
    if not draft.prompt_text.strip():
        errors.append("prompt_text 为空")
    elif len(draft.prompt_text) > config.PROMPT_CHAR_LIMIT:
        errors.append(
            f"prompt_text 长度 {len(draft.prompt_text)} 超过上限 {config.PROMPT_CHAR_LIMIT}"
        )
    if dialect_render_format == "minimax_h3_native_fields":
        for field in ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:"):
            if field not in draft.prompt_text:
                errors.append(f"prompt_text 缺少 H3 固定字段「{field}」")
    return errors


def _segment_content_advisories(
    draft: _AiStoryboardSegmentDraft,
    *,
    known_character_ids: set[str],
    known_scene_ids: set[str],
    source_segment_indexes: list[int],
) -> list[str]:
    """Non-blocking content checks: computed every time, never gate generation.

    2026-08-26（用户拍板）：「我认为第一版的分镜提示词先不需要任何门禁……
    只要格式没问题就直接作用到下一环节」。这些判断此前是
    ``_validate_segment_draft`` 的一部分，会触发 chat_structured 的语义重试
    直到耗尽预算后整段失败；现在原样保留计算，只是结论从「拦截」改成
    「附在产物 degraded_capabilities[] 上的信息」——校验照算，不是删掉，
    删掉以后就永远看不到这条不一致了。台词说话人是否在场的第三条（rule 1）
    对照的是这一段自己的 resources.characters（映射台对"这个人物在这些
    段落里在场"的结论），不是全集已知人物表，与
    app.validators.storyboard_pack_dialogue_errors 用的是同一套判据，只是
    这里在生成时就先算一遍、写进产物，那边在确认时再算一遍、当作可见但
    不拦截的 warning——两处判据不重复发明，只是消费方式不同。
    """
    # Tag names deliberately match app.validators.storyboard_pack_dialogue_errors'
    # [STORYBOARD_PACK_DIALOGUE_*] codes -- same underlying judgment computed
    # at two points in the pipeline (here at generation time, there again at
    # confirmation time against the persisted row), so a search for one code
    # finds both occurrences instead of two unrelated-looking strings.
    advisories: list[str] = []
    allowed_segments = set(source_segment_indexes)
    segment_character_ids = {c.identity_id for c in draft.resources.characters}
    for index, line in enumerate(draft.dialogue):
        if line.speaker_identity_id not in segment_character_ids:
            advisories.append(
                f"[STORYBOARD_PACK_DIALOGUE_SPEAKER_ABSENT][未拦截] dialogue[{index}] "
                f"的说话人「{line.speaker_identity_id}」不在本段 resources.characters 内，"
                "没有在场证据"
            )
        if line.source_segment_index not in allowed_segments:
            advisories.append(
                f"[STORYBOARD_PACK_DIALOGUE_NO_SOURCE][未拦截] dialogue[{index}]"
                f".source_segment_index={line.source_segment_index} "
                f"不在本段引用的原文段号 {sorted(allowed_segments)} 内"
            )
    for index, character in enumerate(draft.resources.characters):
        if known_character_ids and character.identity_id not in known_character_ids:
            advisories.append(
                f"[STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN][未拦截] "
                f"resources.characters[{index}].identity_id=「{character.identity_id}」"
                "不是映射台已知的人物身份，已按纯文字描述处理"
            )
    for index, scene in enumerate(draft.resources.scenes):
        if known_scene_ids and scene.scene_id not in known_scene_ids:
            advisories.append(
                f"[STORYBOARD_PACK_RESOURCE_SCENE_UNKNOWN][未拦截] "
                f"resources.scenes[{index}].scene_id=「{scene.scene_id}」"
                "不是映射台已知场景，已按纯文字描述处理"
            )
    return advisories


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
    # functional_extras（群演/一次性人物）没有 identity_id，用自己的
    # visual_entity_id 当作 resources.characters[].identity_id 的合法来源
    # （问题三：模型确实会正确引用群演的 visual_entity_id，例如真实 EP1
    # 第10段绿袍男子=entity:fdd28fea634a6cdc；漏掉这一路会让本来合法的引用
    # 被 _segment_content_advisories 误判成"不是映射台已知的人物身份"）。
    known_character_ids = {
        str(c.get("identity_id") or "") for c in (payload.get("asset_manifest") or {}).get("characters") or []
    } | {
        str(e.get("visual_entity_id") or "")
        for e in (payload.get("asset_manifest") or {}).get("functional_extras") or []
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
        "rules": [
            "relevant_assets 里每个角色/场景都带一个外观/场景字段（角色和"
            "群演统一叫 appearance；场景叫 scene_canonical）：内容是一段"
            "具体描述时，那就是这个角色/场景在本集的标准锚点，写它时必须"
            "逐字沿用这段描述本身，不得改写、精简、替换或按本段情境调整；"
            "内容是「没有标准外观/场景……」这类说明文字时，才由你自行确定"
            "特征，并让同一角色/场景在本集所有涉及它的段落里保持同一套"
            "自定特征。",
            "dialogue[] 与 prompt_text 两处的台词必须互相覆盖、逐句一致："
            "dialogue[] 列出的每一句台词都必须能在 prompt_text 里找到对应"
            "原话，prompt_text 里写出的台词原话也必须同时登记进 "
            "dialogue[]，不得只在一处出现。",
            "prompt_text 里出场或说话的每一个角色都必须同时列进 "
            "resources.characters；resources.characters[].identity_id 只能"
            "使用 relevant_assets.characters 的 identity_id 或 "
            "relevant_assets.functional_extras 的 visual_entity_id，不得"
            "自造新 id。",
        ],
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
    draft = await model_gateway.chat_structured(
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
    advisories = _segment_content_advisories(
        draft,
        known_character_ids=known_character_ids,
        known_scene_ids=known_scene_ids,
        source_segment_indexes=segment_plan.source_segment_indexes,
    )
    if advisories:
        draft.degraded_capabilities = [*draft.degraded_capabilities, *advisories]
    return draft


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
    # Carries forward the model's own self-declared association from the
    # beat-sheet stage (_AiSegmentPlan.beat_ids) -- the same list already used
    # to build ``beat_summaries`` for this segment's own prompt (see
    # _generate_segment_prompt). This is the authoritative source for "which
    # beats does this segment cover"; persist_storyboard_pack must key off it
    # directly instead of re-deriving via segment_indexes/source_segment_indexes
    # set intersection, which is only a proxy and can disagree with what the
    # prompt was actually built from at edges (e.g. a beat whose
    # segment_indexes happens to overlap this segment's source range without
    # the model having assigned it here, or vice versa).
    beat_ids: list[str] = Field(default_factory=list)
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
    _enrich_asset_manifest_canonical_visuals(conn, payload)

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
            beat_ids=list(segment_plan.beat_ids),
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


def _largest_contiguous_source_run(indexes: list[int]) -> list[int]:
    """Pick the one run this shot's source binding can honestly prove.

    ``storyboard_source_bindings`` is schema-fixed to one (chapter, start,
    end) span per shot (app/db.py: ``shot_id TEXT PRIMARY KEY``), and
    ``assert_storyboard_source_bindings_complete`` requires the sliced
    ``content[start:end]`` to equal ``shots.source_excerpt`` byte-for-byte.
    Real ``source_segment_indexes`` are routinely non-contiguous (EP1 data:
    shot 2 = [12,14,15,16,17], skipping 13; shot 6 = [44,46,47,48], skipping
    45) because the model omits a connective paragraph it judged irrelevant
    to this narrative beat. Taking the naive ``min(indexes)..max(indexes)``
    envelope would silently claim the skipped paragraph as part of this
    shot's verified excerpt -- pretending a non-contiguous reference is
    contiguous. Instead this keeps only the longest actually-contiguous run
    (ties broken toward the earliest/smallest-starting run, for
    determinism); indexes outside that run are not represented in
    ``shots.source_excerpt`` or the binding.
    """
    unique_sorted = sorted(set(indexes))
    if not unique_sorted:
        return []
    runs: list[list[int]] = []
    current = [unique_sorted[0]]
    for value in unique_sorted[1:]:
        if value == current[-1] + 1:
            current.append(value)
        else:
            runs.append(current)
            current = [value]
    runs.append(current)
    return max(runs, key=lambda run: (len(run), -run[0]))


def _resolve_segment_source_binding(
    *,
    segment_no: int,
    source_segment_indexes: list[int],
    segments: list[SourceSegment],
    full_source_text: str,
    authorized_sources: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Derive (source_excerpt, normalized binding) for one persisted shot row.

    ``normalized`` matches exactly the shape ``app.storyboard_workspace.
    persist_source_binding`` expects and the legacy pipeline already produces
    (see ``verify_or_bind_existing_excerpt``/``align_generated_source_evidence``
    in app/storyboard_workspace.py): binding_kind/chapter_id/chapter_idx/
    source_version_hash/start_offset/end_offset/excerpt_hash, offsets always
    chapter-local (matched against ``chapters.content``, not the multi-chapter
    joined text ``index_source_segments`` was run against).

    The excerpt text itself is the literal joined-text slice for the chosen
    contiguous run (``full_source_text[start:end]``), not a
    ``"\\n".join(segment.text ...)`` reconstruction -- ``index_source_segments``
    splits on blank-line boundaries (``\\n\\s*\\n``), so a single ``\\n`` join
    does not reproduce the real inter-paragraph whitespace and would already
    fail the binding's byte-for-byte check even for a fully contiguous run.
    Locating which authorized chapter contains that literal slice (and its
    chapter-local offset) is done the same way the legacy pipeline does it --
    ``content.find(excerpt)`` against each authorized chapter -- rather than
    re-deriving offsets from the join format, which would be fragile across
    multi-chapter episodes.

    One join artifact needs an explicit unwrap: ``_episode_source_text``
    (app/domain/common.py) prefixes each chapter's block with
    ``【{title}】\\n`` before joining, so ``index_source_segments`` frequently
    folds that bracketed heading into segment 1 of a chapter (real EP1 data:
    segment 1's text is literally ``"【第一章书生孟浩】\\n第一章书生孟浩"`` --
    chapters.content itself repeats its own title as its first line, see
    ``app.source_excerpt.chapter_title_segment_ids``'s docstring for the same
    join artifact). ``chapters.content`` never contains the bracket wrapper,
    only chapters.content's own text, so a run starting at a chapter's first
    segment must have that wrapper stripped before ``content.find`` can ever
    match -- this is not a fuzzy/best-effort fallback, the wrapper is a fixed,
    known literal (``_episode_source_text``'s own format string), so this
    reproduces exactly the bytes ``chapters.content`` actually holds rather
    than approximating them.
    """
    valid_indexes = sorted({i for i in source_segment_indexes if 1 <= i <= len(segments)})
    if not valid_indexes:
        raise ValueError(
            f"第 {segment_no} 段没有落在原文分段范围内的段号（{source_segment_indexes}），无法生成原文绑定"
        )
    run = _largest_contiguous_source_run(valid_indexes)
    start = segments[run[0] - 1].start_offset
    end = segments[run[-1] - 1].end_offset
    excerpt = full_source_text[start:end]
    for source in authorized_sources:
        content = str(source.get("content") or "")
        title = str(source.get("title") or "")
        candidate = excerpt
        wrapper = f"【{title}】\n"
        if title and excerpt.startswith(wrapper):
            candidate = excerpt[len(wrapper):]
        local_start = content.find(candidate)
        if local_start >= 0:
            return candidate, {
                "binding_kind": "source_excerpt",
                "chapter_id": int(source["id"]),
                "chapter_idx": int(source["idx"]),
                "source_version_hash": source["source_version_hash"],
                "start_offset": local_start,
                "end_offset": local_start + len(candidate),
                "excerpt_hash": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            }
    raise ValueError(
        f"第 {segment_no} 段原文摘录（原文段号 {valid_indexes}）无法在本集授权章节中定位"
    )


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
    from app.storyboard_workspace import chapter_sources, persist_source_binding

    _assert_storyboard_write_authorized(conn, episode_id, None)
    if segments is None:
        segments = _load_indexed_source_segments(conn, ep)
    full_source_text = _episode_source_text(conn, ep)
    authorized_sources = chapter_sources(episode_id, conn=conn)
    conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
    beats_by_id = {beat.beat_id: beat for beat in pack.beat_sheet}
    shot_ids: list[str] = []
    for segment in pack.segments:
        character_ids = [
            str(c.get("identity_id") or "") for c in (segment.resources.get("characters") or [])
        ]
        scene_entries = segment.resources.get("scenes") or []
        scene_display_name = str(scene_entries[0].get("display_name") or "") if scene_entries else ""
        source_excerpt, source_binding = _resolve_segment_source_binding(
            segment_no=segment.segment_no,
            source_segment_indexes=segment.source_segment_indexes,
            segments=segments,
            full_source_text=full_source_text,
            authorized_sources=authorized_sources,
        )
        shot_id = new_id("shot")
        shot_uid = new_id("shotuid")
        segment_record = segment.model_dump(mode="json")
        # Single source of truth: the model's own self-declared segment.beat_ids
        # (_AiSegmentPlan.beat_ids, carried through StoryboardPackSegment) --
        # the same list that built this segment's own prompt (see
        # _generate_segment_prompt's beat_summaries). _validate_beat_sheet_draft
        # already rejects any beat_id that doesn't exist in pack.beat_sheet, so
        # the lookup below cannot silently drop a real beat; the ``in
        # beats_by_id`` guard is defense in depth, not a coverage gap.
        # Previously this was re-derived from
        # ``set(beat.segment_indexes) & set(segment.source_segment_indexes)`` --
        # a different field (segment_indexes) standing in for beat_ids, which
        # could disagree with what the model actually declared/was prompted
        # with at the edges. See the "拿一个维度的代理担保另一个维度" note.
        matched_beats = [
            beats_by_id[beat_id] for beat_id in segment.beat_ids if beat_id in beats_by_id
        ]
        # ``beat_ids`` (bare id list) is the pre-existing key frontend/api.ts and
        # BoardPage.tsx already read (StoryboardPackSegment.beat_ids) -- kept
        # unchanged for that consumer plus any historical row shape. ``beats``
        # is the new self-contained field: each shot dict must be renderable on
        # its own (docs/STORYBOARD_PROMPT_IR_DESIGN.md's beat_sheet exists for
        # 留痕 -- a bare id conveys nothing without the summary next to it), so
        # this carries the frozen contract's own per-beat shape
        # (beat_id/summary/segment_indexes, no invented field names) rather
        # than making the frontend join against the episode-level beat_sheet.
        segment_record["beat_ids"] = [beat.beat_id for beat in matched_beats]
        segment_record["beats"] = [
            {
                "beat_id": beat.beat_id,
                "summary": beat.summary,
                "segment_indexes": list(beat.segment_indexes),
            }
            for beat in matched_beats
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
        # storyboard_source_bindings is the provable "this excerpt really is
        # this chapter, this offset range, source unchanged since" pointer
        # app.storyboard_workspace.assert_storyboard_source_bindings_complete
        # gates on -- without this the row's source_excerpt above is an
        # unverifiable free-text field and every shot fails that gate.
        persist_source_binding(shot_id, source_binding, conn=conn, commit=False)
        shot_ids.append(shot_id)

    # Full beat_sheet (with summaries), stored once per generation independent
    # of any single segment row. Per-segment ``beats`` above only carries the
    # beats each segment overlaps -- it cannot answer "how was the segment
    # count decided" on its own if a beat somehow ends up unclaimed by every
    # segment, and it duplicates the same beat's summary across every segment
    # it touches instead of having one canonical copy. This artifact is that
    # canonical copy: an auditable record of exactly what
    # ``_generate_beat_sheet`` produced (segment_count here is
    # ``len(pack.segments)``, i.e. the number this whole module exists to
    # decide -- see ``generate_storyboard_pack``'s docstring).
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_pack_beat_sheet",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content={
                "storyboard_version": pack.storyboard_version,
                "episode_no": pack.episode_no,
                "target_model": pack.target_model,
                "segment_count": len(pack.segments),
                "beat_sheet": [beat.model_dump(mode="json") for beat in pack.beat_sheet],
            },
            parent_artifact_ids=(
                [str(ep["screenplay_artifact_id"])] if ep["screenplay_artifact_id"] else []
            ),
            contract_version=pack.storyboard_version,
        ),
        conn=conn,
        commit=False,
    )
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
        tail_contract: dict[str, Any] = {}
        if existing:
            try:
                tail_contract = json.loads(existing[-1]["shot_contract_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                tail_contract = {}
        if (
            existing
            and all(
                STORYBOARD_PACK_CONTRACT_MARKER
                in (row["shot_contract_json"] or "") for row in existing
            )
            and bool(tail_contract.get("is_final"))
        ):
            # 判据完全落在产物本身，不看 episodes.status：
            # 1) 每一行都带当前 STORYBOARD_PACK_CONTRACT_MARKER —— 同一套契约生成的。
            # 2) 尾镜自带 is_final=True —— persist_storyboard_pack 只在
            #    generate_storyboard_pack 一次整跑成功、写完 pack.segments 的最后一段
            #    时才会落这个标记（本模块的持久化是单事务、一次性全写，不存在
            #    "写了一半"的中间态），所以 is_final=True 就等价于"这是一整套完整
            #    产物，不是半途残留"。
            # 之前这里判的是 ``ep["status"] in ("scripted","confirmed","generating",
            # "done")``——事故根因就在这儿：resume_storyboard()（app/domain/
            # storyboard_ops.py）这个 HTTP 路由，在派发生成任务之前，自己会先把
            # episodes.status 改成 'scripting' 并提交（给 _storyboard_generation_is_live
            # 之类的去重用），然后才 spawn 任务；run_storyboard_supervisor 随后重新
            # SELECT 出来的 ep 快照因此必然是 'scripting'——不在允许列表里，短路
            # 必然判不过，100% 落到下面的全量重灌分支，不是偶发。真实事故
            # （ep_3d523ff4d0a4，run_84f1d96f9963 把已通过的 10 段吃成 7 段）正是
            # 这个必然失败的短路触发的。episodes.status 是会被同一次请求自己的写
            # 操作耦合改动的外部可变字段，不该是"这批产物完不完整"的判据——判据必须
            # 只看产物自己（marker + is_final）。resume 语义下不重新调模型、不 DELETE
            # 重灌——那会连同已经采纳/生成的视频版本一起级联删掉（shots ->
            # shot_versions -> jobs 都是 ON DELETE CASCADE）。直接把已持久化的结果
            # 重建成 checkpoint 返回。
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
