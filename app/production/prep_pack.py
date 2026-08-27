"""Lightweight episode_prep_pack generation & atomic publish.

Screenplay contract 6.0.0 (docs/TRANSFORM_FREEZE_PLAN.md §3). Replaces the
heavy blueprint -> scene-shard -> compile -> repair pipeline
(app/production/screenplay_repair.py and friends -- left dormant, not deleted,
simply no longer called) for new runs. ``workflow_type`` stays ``'screenplay'``:
the state machine, episodes.screenplay_status lifecycle, monitoring and driver
all key off it unchanged. Only the business logic that runs *inside* one
screenplay Run is new; Run/Step/Artifact harness and the completion-certificate
machinery (app/production/certificate.py) are reused as-is.

Frozen artifact payload shape (single source of truth -- field names must not
change; see the task brief / docs/TRANSFORM_FREEZE_PLAN.md §3). 2.0.0
(architecture narrowing, see the 2.0.0 note further down for the full
argument): this module -- 映射台 / the mapping stage -- no longer produces any
narrative content (no event list, no dialogue extraction, no hook/
cliffhanger). Its only job is discovering this chapter's new characters/
scenes, mapping them (and every ambiguous in-text appellation) to the bible's
existing image assets, and proving every source segment was actually read.
Which/how-many narrative beats a chapter contains is entirely the storyboard
stage's own job now, derived straight from source text:
{
  "prep_pack_version": "2.0.0",
  "episode_no": int,
  "episode_scope": {"chapter_indexes": [int], "source_segment_count": int},
  "asset_manifest": {
      "characters": [{"identity_id": str, "display_name": str,
                       "display_appellation": str, "aliases": [str],
                       "portrait_id": str | None, "visual_entity_id": str,
                       "segment_indexes": [int]}],
      "scenes": [{"scene_id": str, "display_name": str,
                  "scene_reference_id": str | None, "segment_indexes": [int]}],
      "props": [{"label": str, "description": str, "segment_indexes": [int]}],
      "functional_extras": [{"label": str, "visual_entity_id": str,
                              "segment_indexes": [int]}],
  },
  "appellation_map": [{"raw_mention": str, "segment_index": int,
                        "identity_id": str, "canonical_appellation": str}],
  "coverage_ledger": {"total_segments": int, "delivered": [int], "merged": [int],
      "retained_as_context": [int],
      "proven_duplicates": [{"segment_index": int, "duplicate_of_segment_index": int}],
      "paratext": [int], "uncovered": [int]},
}
Every ``asset_manifest`` entry and ``characters[]``/``scenes[]``/
``functional_extras[]`` also still carries a ``provenance`` dict (``method``/
``anchor_segments``/``anchor_phrase``/...) -- additive, not part of the
frozen field list above (same "new fields are additive, frozen fields never
renamed" convention every prior version bump in this file has followed), see
_prep_pack_provenance.

1.2.0 (coordinator amendment, real-EP2 field bug): asset_manifest.characters
entries gained ``aliases`` -- the raw in-episode mention strings (e.g. a
nickname like "小胖子") that were disambiguated to this character's canonical
name, for P1 storyboard prompts. This accompanies a correctness fix, not a
cosmetic one: resolution no longer exempts a mention just because the
event-chain extraction model guessed ``is_background_extra=true`` on it --
that guess is untrusted prose from a model call that never looked at the
bible, and treating it as authoritative silently dropped a real, already-
carded, portrait-bearing character ("小胖子" == 李富贵) from a real EP2
artifact. See _resolve_assets below for the corrected flow.

1.3.0 (coordinator amendment, real-EP13 finding): asset_manifest gained
``functional_extras`` -- one-off/occupation-title character mentions
("养丹坊掌柜", "围观弟子") that app.portraits' identity discovery neither
resolved to a real identity nor explicitly failed on, kept under their own
raw source label per app.portraits' long-standing rule for unconfirmed-real-
name one-offs (portraits.py:1727, 7340: typed functional identity, never
silently dropped, never renamed to something generic). A real EP13 run
proved discovery's own candidate phrasing does not always exactly match
prep_pack's chunk-extraction phrasing for the same crowd concept (discovery
said "外宗弟子", the chunk extractor said "一名外宗弟子") even though
discovery ran cleanly and, in the same call, successfully carded+portraited a
genuinely new character ("曹阳"). Silence from a clean discovery run is not
grounds to gate-fail; only discovery explicitly failing on that specific name
still blocks (_discovery_errored_names). ``characters`` keeps its
existing portrait_id-bearing-only meaning; P1 storyboard prompts need
functional_extras to know who else is in frame. See _resolve_assets below.

1.4.0 (coordinator decision, later retired the same day -- see 1.4.1 below):
coverage_ledger gained a fifth account, ``paratext``. The first cut classified
it purely deterministically from the source text's own shape (keyword table +
position rules, no model involvement at all). A real round-15 EP2 regression
against a real chapter (proj_3ac0b627fa46, chapters.idx=2) proved that
approach could not stay both precise and complete against real author
phrasing -- see 1.4.1.

1.4.1 (coordinator decision, real round-15 EP2 regression fix): paratext
classification mechanism replaced -- the model itself already recognizes an
author's note when it reads one (the real regression: it spontaneously
summarized the offending segments as an event named "作者发布留言"), so v1's
mistake was trying to re-derive that recognition from the source text alone
instead of just capturing it. Under 1.4.1, the model DECLARES which of its
own chunk's segments are paratext (``paratext_segments`` in _ChunkResponse,
see _extract_chunk's prompt) and skips building narrative events for them;
app.validators.build_prep_pack_span_ledger then runs three independent
deterministic veto gates over that declaration (position / no-dependency /
exclusivity -- see its module comment above PARATEXT_TAIL_WINDOW_SEGMENTS for
the full argument) before any of it is trusted. This is still a bookkeeping
change, not a coverage-gate weakening: 洞即删戏 still applies to every
segment that is *not* accepted paratext, an over-claimed segment has no
silent path (it just falls back to needing real event coverage, gate-blocked
if none exists), and a segment that somehow ends up both accepted-paratext
*and* inside some event's validated span is a fatal ledger contradiction
(exclusivity gate) exactly as under 1.4.0, not silently tolerated either way.
The event-chain extraction prompt still receives the full chunk text
(paratext segments included, for narrative context) but now asks the model
to identify+declare them itself rather than being told in advance which
numbers are exempt.

1.4.2 (coordinator decision, real round-16 EP5 regression fix): asset
resolution (_resolve_assets) gained a text-evidence gate. Real EP5 output
bound two events describing an unnamed pair of old men on an unrelated
mountain peak near 靠山宗 to a pre-existing character ("丹鬼") and scene
("大青山山顶") from elsewhere in the story -- chapter 5's own text has zero
occurrences of either string (verified directly against the stored chapter
content), and the event-chain extraction model wrote those exact names
directly as characters[]/scenes[].display_name, not the text's own
descriptive terms ("灰袍老者"/"山顶老者" only ever appeared as
key_lines[].speaker, a field _resolve_assets never reads). Root cause:
neither _resolve_portrait_id nor _resolve_scene_reference_id required any
evidence beyond "a DB row with this exact name exists for some episode" --
a bare name-string coincidence was silently trusted. Fix (see
_prep_pack_mention_has_text_evidence and its two call sites in _pass, both
inside _resolve_assets): a direct name bind now additionally requires the
raw mention text to appear verbatim in this episode's own source_text.
Character and scene failure modes are deliberately asymmetric per the
coordinator's instruction: a character mention that resolves (directly or
via a discovery rename) but has no textual evidence for its own mention
text is a named, hard PrepPackGateError-eligible error (rerouting it to
discovery risks repeating the same confident-but-wrong guess); a scene
direct hit with no evidence is instead treated as unresolved and rerouted
into the existing discovery path (app.scenes.ensure_scenes_for_labels),
which is exactly the mechanism already designed to register a genuinely new
scene when nothing existing actually matches. asset_manifest's own shape is
unchanged.

1.5.0 (three coordinator amendments, same batch, real round-16/17
regressions -- schema changed, hence the minor bump):
  a) Prior-knowledge declare-then-verify (user correction: outright banning
     the model's own book knowledge in the extraction prompt was wrong --
     a correct guess like "丹鬼" should be a bonus, not discarded).
     _ModelCharacterMention/_ModelSceneMention gain ``suspected_true_name``
     (required, nullable); display_name still must be the verbatim
     in-episode term, never replaced. See
     _prep_pack_verify_true_name_hypothesis: a hypothesis is only trusted
     once it resolves to an existing bible identity AND passes the identity
     binding trial procedure (round 29: whole-book dossier retrieval + one
     independent model verdict + verbatim quote-pinning against the dossier;
     see the detailed comment above _prep_pack_true_name_dossier) -- never
     taken on the model's word alone, and never on a hand-written rule
     guessing what "same person" phrasing looks like.
  b) Speaker roster referencing (real EP2 finding: a key line's speaker was
     written as "韩宗", a character absent until chapter 5, with zero
     validation on that field ever). event_chain[].key_lines[] gains
     ``speaker_ref``, resolved deterministically against the ALREADY-gated
     episode roster (asset_manifest.characters/functional_extras) by
     _prep_pack_resolve_key_line_speakers -- a speaker that resolves to
     nothing in this episode's own roster is a named, hard gate failure.
     Also added: prose-field lint (_prep_pack_prose_lint_warnings,
     summary/hook/cliffhanger) -- observability only, not fatal.
  c) Span-overlap semantic separation (ERR-20260824-22cb1c, real round-17
     EP3 regression) -- see app.validators.build_prep_pack_span_ledger's
     "语义分离" docstring note for the full argument. event_chain[].
     source_span keeps publishing the EXTENDED value (unchanged), but
     adjacent events' source_span may now legitimately overlap by a
     segment or two when a later event's own verified quote reaches one
     segment into a shared transition -- that is delivery-evidence
     spillover, not a narrative-boundary claim (the ordering/crossing gate
     itself only ever looks at the model's DECLARED span, never the
     extended one, as of this version). P1 storyboard consumers of this
     payload must not treat source_span overlap between consecutive events
     as "double-booked" story time.

Coverage accounting design (three real EP1 iterations, see
docs/TRANSFORM_FREEZE_PLAN.md and app.validators.build_prep_pack_span_ledger):
the model declares each event's ``source_span`` (a closed [from_segment,
to_segment] interval) instead of enumerating a disposition for every
individual segment. The coverage_ledger is then a deterministic PROJECTION
of the validated spans -- delivered/retained_as_context/uncovered are
derived, not model-declared; merged/proven_duplicates are always empty under
this accounting; paratext (1.4.0/1.4.1) is the one account seeded by a model
declaration rather than derived purely from spans, but even that declaration
only lands in the ledger after surviving deterministic gates (see the 1.4.1
note above) -- nothing in this ledger is ever a bare, unverified model claim.
This replaced an earlier per-segment
disposition-declaration design (2026-08-24) that made the model's bookkeeping
burden scale with segment count and left it randomly dropping ~1 short
segment (2-6 chars, e.g. a single interjection) per real run despite three
rounds of gate-shape patching -- see the git history on this file for that
design if it is ever needed for reference, but do not resurrect it: span
declaration is structurally simpler for the model (closer to "summarize this
range" than "fill out a per-item form") and fixes the failure class instead
of chasing individual instances of it.
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import nullcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.identity_authority import visual_entity_id_for_resolution
from app.observability.tracing import bind_trace, current_trace
from app.orchestration.state_machine import transition_step
from app.production.certificate import (
    assert_publish_has_certificate,
    consume_completion_certificate,
    issue_completion_certificate,
    verify_completion_certificate,
)
from app.schemas import Bible
from app.source_excerpt import (
    SourceSegment,
    chapter_title_segment_indexes,
    index_source_segments,
)
from app.validators import (
    assert_prep_pack_coverage_complete,
    match_scene_name,
)

# 2.0.0: align_source_excerpt/bigram_coverage/assert_prep_pack_span_union_
# matches_ledger/build_prep_pack_span_ledger were only ever used by the
# event_chain/hook/cliffhanger machinery this version removes (quote
# alignment for source_evidence/key_lines, hook/cliffhanger grounding, and
# the event-span coverage ledger respectively) -- no longer imported here.
# build_prep_pack_span_ledger/assert_prep_pack_span_union_matches_ledger
# stay defined in app/validators.py, unused-but-not-deleted (same "dormant,
# not deleted" precedent as app/production/screenplay_repair.py), still
# exercised directly by tests/test_prep_pack_coverage.py.
PREP_PACK_VERSION = "2.0.3"  # 1.1.0: event_chain entries carry source_span (P1 storyboard needs it).
# 1.2.0: asset_manifest.characters entries carry aliases; 1.3.0: asset_manifest
# gained functional_extras; 1.4.0: coverage_ledger gained paratext (deterministic
# keyword/position classifier, since replaced); 1.4.1: paratext classification
# mechanism replaced with model-declares + deterministic-veto-gates (real
# round-15 EP2 regression -- see module docstring's 1.4.1 note and
# app.validators.build_prep_pack_span_ledger's module comment above
# PARATEXT_TAIL_WINDOW_SEGMENTS). coverage_ledger.paratext's own shape is
# unchanged (still a flat [int] list) -- only how it gets populated changed,
# but this still counts as a classification-mechanism change for provenance
# purposes, hence the version bump. 1.4.2: asset resolution (_resolve_assets)
# gained a text-evidence gate for direct character/scene name binds (real
# round-16 EP5 regression -- see module docstring's 1.4.2 note and
# _prep_pack_mention_has_text_evidence's comment). asset_manifest's own shape
# is unchanged; this is a resolution-correctness fix, not a payload-shape
# change, but bumped for the same provenance reason as 1.4.1. 1.5.0 (real
# schema change, see module docstring's 1.5.0 note): _ModelCharacterMention/
# _ModelSceneMention gain ``suspected_true_name`` (model-declared prior-
# knowledge hypothesis, verified not trusted); event_chain[].key_lines[]
# gains ``speaker_ref`` (deterministic roster resolution of the free-text
# speaker field, real EP2 finding: a key line's speaker was written as
# "韩宗" -- a character absent until chapter 5 -- with zero validation).
# 1.5.1 (real round-18 audit finding, A2 主病灶 47 条): scene resolution
# (_resolve_assets) now consults the bible's registered scene aliases (via
# app.validators.match_scene_name, reused as-is from app.scenes' own
# discovery path), not just scene_references.scene_name's exact string, and
# persists any newly-used wording back into Bible.scenes[].aliases (see
# _prep_pack_resolve_scene_reference_with_alias /
# _prep_pack_register_scene_alias_if_new). asset_manifest's own shape is
# unchanged; bumped for the same provenance reason as 1.4.2.
# 1.5.2 (task②, real project-level finding: "小胖子" wrongly rebound to
# "王有材" in a real EP3 artifact, zero textual evidence for "王有材" found
# anywhere in that chapter): a character rename (from either app.portraits'
# own character_rename or the 1.5.0 suspected_true_name fast path) is now
# checked against every OTHER already-published episode's asset_manifest
# before being accepted -- if the same alias string is already bound to a
# DIFFERENT canonical name elsewhere in the project, the rebind is rejected
# (falls back to the raw mention's own normal resolution) and logged (see
# _prep_pack_cross_episode_alias_conflict). asset_manifest's own shape is
# unchanged; bumped for the same provenance reason as 1.4.2/1.5.1.
# 1.5.2 also gained (real round-21 EP1 finding ERR-20260824-34347a, version
# NOT re-bumped -- pure gate-semantics refinement, no schema/prompt-contract
# change): _prep_pack_resolve_key_line_speakers became an asymmetric
# three-branch gate instead of a binary roster-hit-or-block check. speaker
# and an event's own characters[] are two independent phrasings from the
# SAME model call, so bare string equality is fragile ("被困者"/"王有材"/
# "被困少年" can all name one person) -- blocking every mismatch would also
# kill legitimate phrasing drift, not just real hallucinations ("韩宗", a
# character absent from this episode entirely). The fix distinguishes them
# deterministically: a speaker string that collides with ANY character name
# in the full project bible (not just this episode's own roster) but is
# absent from this episode's roster is still a fatal, named block (real
# hallucinated-attribution shape); a speaker with zero collision anywhere in
# the project bible is a purely descriptive one-off term and gets absorbed
# into functional_extras instead (typed functional identity's original
# meaning) rather than blocked. See _prep_pack_all_project_character_names.
# 1.5.2 also gained (real round-24 EP3 finding ERR-20260824-d0830a, version
# NOT re-bumped -- both fixes are internal resolution-logic/gate-semantics
# refinements, no schema/prompt-contract change):
#   task① 角色别名注册表读侧：裸精确匹配失败后，跟场景轴 1.5.1
#   （_prep_pack_resolve_scene_reference_with_alias）对称地查项目内已发布
#   分集的 asset_manifest.characters[].aliases（跟 _prep_pack_cross_episode_
#   alias_conflict 冲突检查同一数据源）——命中唯一目标才绑定（复用同一套
#   冲突拒绝逻辑守多目标）。此前只有写侧（角色改名落地时把 name 记进
#   aliases）没有读侧，别名库形同虚设：EP2 一次消歧确立"小胖子"→李富贵后，
#   EP3 仍然要重新赌一次消歧模型调用才能复现同一个结论。见
#   _prep_pack_lookup_character_alias_canonical_name。
#   task② 称谓证据闸语义精化：「穿杂役衫的魁梧大汉」经消歧正确解析到
#   赵武刚，却被"称谓未逐字出现在原文"拦截——原文只有分散的描述性叙述，
#   模型综合出的这个名词短语天然不可能逐字命中，不是幻觉归属的形状。
#   区分裸直接命中（resolved_name==name，未经任何改名路径，如真实 EP5 的
#   "丹鬼"案）与经消歧/发现/别名注册表解析绑定（resolved_name!=name，
#   合法性由那条解析路径自身的证据链承担）：前者反幻觉主防线不动，仍要求
#   逐字证据；后者不再重复要求称谓本身逐字出现。别名注册表仍只登记逐字
#   出现于原文的称谓——组合短语不进别名库，防止注册表被合成词污染。
# 1.6.0（第25轮收口指令，schema/payload 契约变更，版本确实推进）：审计对
# 1.5.x 陆续放宽字面锚定要求后剩余的 83 条定性为"管线与审计标准分叉"——
# 合成标签合法（消歧/发现/别名注册表/吸收群演都不再要求逐字命中）但不可
# 审计（判断"这次绑定为什么合法"的依据只留在 Evaluation.evidence 里，不是
# payload 的一等公民）。asset_manifest.characters[]/scenes[]/
# functional_extras[] 每项新增 provenance: {method, anchor_segments,
# anchor_phrase}；event_chain[].key_lines[] 每条新增 speaker_provenance
# （跟绑定到的角色/群演共用同一份 provenance，不是重新算一份，协调方
# 形状对齐指令明确的字段名）。method ∈ direct/alias/resolution/discovery/
# absorbed_speaker，anchor_segments 是支撑这次绑定的本集原文段号，
# anchor_phrase 是触发绑定的原文短语——必须逐字存在于 anchor_segments 所指
# 原文（发布前 _prep_pack_verify_manifest_provenance 自校验一遍，不成立
# 即门禁拦，见该函数与 _prep_pack_local_text_anchor 上方的完整说明）。
# 两个字段都是新增可选结构（旧包没有这两个字段，反序列化/前端读取时按
# "不存在"处理，不破坏任何既有消费者，payload 冻结纪律没有被打破——冻结的
# 是既有字段的语义不变，不是禁止新增字段）。
# 1.6.1（独立评审 blocker，prompt-contract 变更，版本确实推进）：
# _prep_pack_true_name_verdict 被角色分支（resolve_fn=_resolve_portrait_id）
# 与场景分支（resolve_fn=_resolve_scene_reference_id）共用，旧版裁决提示词
# 硬编码人物语义（"判断称谓 X 与人名 Y 是否指同一个人"）——场景假设走到这
# 条路时模型实际被问的是"这两个是不是同一个人"，问题本身跟场景/地点语义
# 无关，裁决结果不可靠。这不属于上面 1.5.2 两条"版本未重新推进"的内部
# 逻辑/闸门语义精化（那两条都没有改变发给模型的实际文本）——这次改的正是
# 发给模型的提示词本身（按 subject_kind 分流为"同一个人"/"同一个场景或
# 地点"两套措辞，见 _TRUE_NAME_VERDICT_SUBJECT_COPY），是真正的 prompt-
# contract 变更，会实际改变场景假设裁决的模型输出，因此比照 1.4.1 的先例
# （分类机制变更即使 payload 形状不变也要为可追溯性推进版本号）推进版本。
# 顺带修了同一函数的一处 minor：判决缓存 verdict_cache 的键此前只有
# (alias, suspected_true_name)，角色循环与场景循环共用同一个缓存字典对象，
# 不按 subject_kind 隔离会导致跨域撞名时错误复用另一个域的裁决结果——键
# 里加入 subject_kind 修复（不改变 payload 形状，仅内部正确性修复，不单独
# 占用版本号，随本次一起推进）。
# 1.7.0（角色身份三层架构 P0 收口，docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md
# §4.1/§4.3/§6 第 3、7 项，schema 变更，版本推进）：
#   a) 跨集别名读源切换：_prep_pack_cross_episode_alias_conflict /
#      _prep_pack_lookup_character_alias_canonical_name 主读源改为
#      Bible.characters[].aliases（内存一次拿到，见 app.schemas.CharacterAlias），
#      直接切断"未绑定角色落 functional_extras 永不写别名 -> 下集查不到 ->
#      仍未绑定"的死循环（真实许清 EP1/EP5/EP6 三集三种措辞、EP13 才绑上）。
#      旧的"扫描其它已发布分集 asset_manifest"路径保留（P2 §16 尚未退役，
#      见函数注释），但只在人物谱对这个别名毫无记录时才补充生效，绝不
#      推翻人物谱已经给出的结论。
#   b) asset_manifest.characters[]/functional_extras[] 每项新增
#      visual_entity_id（app.identity_authority.visual_entity_id_for_
#      resolution，具名/未具名都有，跨集稳定，决定取图）与
#      display_appellation（characters[] 独有，本集原文措辞，决定字幕/
#      台词显示，不随全局规范名改写）。display_name 语义不变（仍是消歧后
#      的规范名，向后兼容既有消费者）——两个字段都是新增可选结构，不破坏
#      payload 冻结纪律。
# 1.8.0（用户诉求收口，schema/prompt-contract 变更，版本推进）：真实第 37
# 轮 EP1 复核——"许清"人物谱已登记确认别名"许师姐"（app.stages 全书别名
# 回填核验通过）、appearance_canonical 明确写着"常年穿银色长袍"、定妆照
# ep_start=1 已就绪，本集原文两次出现"许师姐"，但事件链抽取模型给出场的
# 标签是外貌描述"银色长袍女子"——跟别名库登记的称谓类型对不上，两个字符串
# 毫不相干，_resolve_portrait_id 与别名注册表查找都必然落空，此前一路落
# functional_extras 当无图群演。根因不是别名机制坏了，是这类"既查不到
# portrait、也命中不了别名"、即将落入 functional_extras 的标签从未真正过
# 一遍"人物谱里有没有人已经在本集原文里跟它共现"的判别。
# 修复：_resolve_assets 在这类标签真正落 functional_extras 之前，补一次
# 候选判别（范式完全复用 app/stages.py 当晚落地的别名裁决庭：代码检索卷宗
# → 模型候选判别 → 段号钉证，见 _prep_pack_functional_candidate_roster /
# _prep_pack_functional_candidate_names / _prep_pack_functional_candidate_
# dossier / _prep_pack_functional_candidate_call / _prep_pack_functional_
# candidate_pin_segment / _prep_pack_resolve_functional_extra_candidate 的
# docstring）：候选集是本集 source_text 里规范名或已确认别名有字面命中的
# 人物谱角色（零语义，候选集为空直接维持原行为）；卷宗覆盖全部候选各自的
# 出场证据（不只是被测标签周围），避免"选择题名存实亡"；模型做的是候选
# 选择题（"标签 X 最可能指候选中的哪一位"，含"都不是/无法确定"选项），不是
# 诱发确认偏误的是非题；钉证只要求引用卷宗段号（schema enum 收紧），不比对
# 模型转录的引句。命中后 asset_manifest.characters[] 新增 provenance.method
# 取值 "candidate_verdict"（与既有 direct/alias/resolution/discovery/
# absorbed_speaker/resolution_forward/alias_inherited 并列，如实标注这次
# 绑定走的是本机制，供审计区分），anchor_segments/anchor_phrase 是钉证命中
# 的卷宗段落本身（代码检索出的真实原文，天然满足自校验的逐字命中）；
# display_appellation 仍是本集原文措辞（"银色长袍女子"），不提前剧透
# display_name 这个全局规范名。schema 新增取值属于 provenance 既有可选
# 结构的扩展，不破坏 payload 冻结纪律，但会实际改变一部分此前落
# functional_extras 的标签的解析结果（真正的 prompt-contract/行为变更），
# 比照 1.4.1/1.6.1 的先例推进版本号。
# 1.8.1（真实数据、已完整诊断的后续事故，prompt-contract 变更，版本推进）：
# 1.8.0 机制本身工作正常，但目标案例（"银色长袍女子"应绑定许清）仍然失败——
# 卷宗检索（_prep_pack_functional_candidate_dossier）靠 `label in seg.text`
# 逐字匹配定位，而标签本身是模型转述短语、原文 0 次逐字出现，定位从一开始
# 就打空，候选锚点段落失去参照点后退化成文档顺序，主角"孟浩"开篇独白段落
# 吃光预算，真正的证据段"许师姐"（许清已确认别名，紧邻案发现场）进不去
# 卷宗，模型如实回答"无法确定"（模型本身没有错，是喂给它的卷宗本身没有
# 证据）。修复：卷宗主锚点改用事件跨度定位——见 _prep_pack_functional_
# candidate_event_span_segments（标签所属事件的 source_span 覆盖段落，
# 事件链抽取模型必须为每个事件声明这个字段，不依赖标签措辞是否逐字命中
# 原文）与 _prep_pack_functional_candidate_dossier 改造后的完整说明。这
# 会实际改变发给候选判别模型的卷宗内容本身（真正的 prompt-contract 变更，
# 会实际改变部分此前误落 functional_extras 的标签的判别结果），asset_
# manifest/event_chain 的 payload 结构与既有 provenance.method 取值集合
# 均未改变，比照 1.4.1/1.6.1 的先例推进版本号（第三位，不动 schema 位）。
# 1.8.2（真实数据、当晚同一事故的第二层根因，prompt-contract 变更，版本
# 推进）：1.8.1 修好了"标签逐字定位打空"，EP1 目标标签"银色长袍女子"的卷宗
# 确实改成了事件跨度定位、也确实包含了银袍女子登场那段——但目标依然失败
# （provider_calls id=10469）：这次事件跨度本身连续覆盖 12 段（35-46），
# 恰好把 _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES 占满，候选
# 锚点段落（"许师姐"那两段，紧邻案发现场之后、绿袍男子称呼她的地方，但不
# 落在事件跨度本身之内）一条都没进卷宗（该次调用提示词 `含许师姐: False`）。
# 模型的回答本身是对的——它引用的支撑句正是银袍女子的外貌描写，说明它确实
# "看到"了这个人，只是卷宗里没有任何材料能把这个人和候选"许清"连起来。
#
# 根因：1.8.1 的预算分配是"A 侧（事件跨度段+标签字面段）全收，剩余预算才
# 给 B 侧（候选锚点段）"——只要 A 侧单独就能塞满 12 条上限，B 侧永远轮不到，
# 跟 A 侧具体有多长完全无关，是一个结构性的"严格优先级会让一侧饿死"缺陷，
# 不是"这次事件跨度恰好长"的偶然。修复：_prep_pack_functional_candidate_
# dossier 改为按层保底配额（_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_
# MIN_SIDE_ENTRIES），A、B 两侧各自先分到一份不可被对方挤占的保底名额（受
# 各自实际可用证据量与总上限约束），保底满足后剩余的"flex"名额才按原有的
# "A 优先、B 按邻近度补足"规则继续分配——这样即使 A 侧证据再长，B 侧只要
# 非空就必有代表段进卷宗。B 侧内部这次也改为按候选做轮转合并
# （_prep_pack_functional_candidate_anchor_pool 的 round-robin 排序，非
# 简单邻近度全局排序），避免同一个"主角淹没预算"陷阱在 B 侧内部以候选粒度
# 重演——多候选场景下，本章高频出现的候选不能吃光 B 侧保底配额，害其它候选
# 零证据（同一晚在这条判别链上反复出现的失败形状）。详见
# _prep_pack_functional_candidate_dossier 与
# _prep_pack_functional_candidate_anchor_pool 各自完整 docstring。会实际
# 改变发给候选判别模型的卷宗内容本身（真正的 prompt-contract 变更），
# asset_manifest/event_chain 的 payload 结构与既有 provenance.method 取值
# 集合均未改变，比照 1.4.1/1.6.1/1.8.1 的先例推进版本号（第三位）。
# 1.8.3（真实数据、同一晚同一事故的第三层根因，provider_calls id=10520 可
# 复核，prompt-contract 变更，版本推进）：1.8.2 的按层保底配额（A/B 两侧各
# 保底 4 条）确实生效——EP1 目标标签"银色长袍女子"的卷宗从纯事件跨度变成了
# 段 31,33-42,60，B 侧成功挤进 1 段——但目标依然失败，模型再次答"都不是/
# 无法确定"，提示词里 `含「许师姐」: False`。两层新根因：
#   一、条数配额没有对应的字数配额。A 侧那 11 段包含大段外貌与环境描写，
#      几乎吃光 _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS，B 侧
#      "保底 4 条"里只有排在最前面、恰好能塞进剩余预算的 1 条真正被
#      _prep_pack_functional_candidate_dossier 的选择循环收录——保底的是
#      "配额位置"，不是"配额一定进得去卷宗"，字数上限才是真正没被堵住的
#      约束。
#   二、B 侧唯一挤进去的那 1 段还被候选轮转顺序里排第一的主角类候选占了——
#      "主角淹没预算"这个陷阱在这一晚已经从卷宗整体（1.8.1）、到 B 侧
#      内部候选轮转（1.8.2 修复的问题）、到 B 侧稀缺槽位本身（这次）连续
#      三层复现。
# 修复分两处：
#   改动一（本文件，_prep_pack_functional_candidate_dossier +
#   _prep_pack_functional_candidate_anchor_pool + 新增
#   _prep_pack_functional_candidate_truncate_segment）：保底粒度从"A 侧/B
#   侧"下沉到"每个候选"——B 侧每个确有锚点证据的候选都独立保证至少 1 段
#   进卷宗，不再是一个笼统的"B 侧保底 4 条"位置数字任由字数预算和轮转顺序
#   争抢。保底层的字数预算同样按候选粒度兜底：保底段一律收录，不因为字数
#   超限被整条跳过（这正是本轮失败的直接原因）——超过
#   _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS 就
#   做确定性截断（保留含锚点词的核心句 + 省略标记），不整段丢弃某个候选的
#   唯一证据。A 侧保底让位给"每候选至少一段"这个硬要求：先满足每候选保底
#   的条数名额，A 侧保底只取剩余名额与自身可用条数的较小值。两个既有上限
#   常量（MAX_ENTRIES/MAX_CHARS）本身原样不变，只是同一份预算内部的分配
#   规则更细颗粒度、且保底层不再因字数被挤出——不是靠放大上限绕过问题。
#   改动二（本文件，新增 _prep_pack_functional_candidate_registered_
#   names，_prep_pack_resolve_functional_extra_candidate 候选集改为两类
#   并集）：候选集补全——当前候选集只看"规范名或已确认别名在本集原文逐字
#   出现"（甲类），漏掉了一整类角色：人物谱已登记 ep_start/ep_end 覆盖本集
#   （即人物谱自己声明"这个角色在本集是活跃的"），但本集原文只有纯外貌
#   描写、规范名与别名一次都没出现的角色（真实 EP1 案例：李富贵第 1 章只
#   被写成"白白净净身子较胖"，标签"白白净净的胖少年"永远无法绑定，即使
#   人物谱早已登记他 ep_start=1、外貌"身形圆胖皮肤白净"）。乙类候选补入
#   候选集后，若其锚点在本集原文里找不到，卷宗检索侧不受影响（乙类候选
#   天然没有 B 侧锚点段落，不参与也不稀释每候选保底），只在发给模型的
#   提示词里追加一段"人物谱登记显示在本集活跃、但本集原文未必直接点名的
#   候选"，附上人物谱外貌描述作为身份背景参考，并与"本集原文中已直接出现
#   的候选"分区展示，明确标注两者证据强度不同。乙类候选不降低任何判定
#   门槛——候选选择题、段号钉证（仍只能引用卷宗里真实存在的段落，乙类候选
#   没有专属段落可钉）、"都不是/无法确定"拒绝三条既有纪律原样不动，乙类
#   候选能被选中，前提仍是模型能在卷宗的原文段落（通常是 A 侧材料）里找到
#   支撑其身份的独立证据，只是现在多了一个可以被点名的选项。两处改动都
#   不改变每个未解析标签仍然只发起一次模型调用这一既有约束，候选集变大
#   不增加调用次数。会实际改变发给候选判别模型的卷宗内容与候选名单本身
#   （真正的 prompt-contract 变更，会实际改变部分此前误落 functional_
#   extras 的标签的判别结果），asset_manifest/event_chain 的 payload
#   结构与既有 provenance.method 取值集合均未改变，比照 1.4.1/1.6.1/
#   1.8.1/1.8.2 的先例推进版本号（第三位）。
# 1.8.4（协调层复核，真实数据落库后确认误绑，prompt-contract 变更，版本
# 推进）：1.8.3 改动二上线后真实落库结果——EP1/EP2 的"赵武刚"都是靠标签
# "绿袍男子"经 method=candidate_verdict 绑定的，但赵武刚在这两章原文里
# 一次都没被提到，是纯粹的误绑。
#
# 改动二回退——原理性失效，不是实现 bug：改动二当时给的保险是"乙类候选
# 不发放合成卷宗条目，仍须钉住真实卷宗段落才能绑定"，但这道保险对乙类
# 候选原理上不成立。钉证的意义在于"模型引用的这段话确实支持它选择的这个
# 候选"；甲类候选的卷宗段落本来就是靠这个候选自己的规范名/别名字面命中
# 检索出来的，钉证等于验证了"这段话谈的确实是这个候选"。乙类候选恰恰是
# "本集原文里找不到他"这一类角色（人物谱注册区间覆盖本集，但规范名/别名
# 一次都没在原文出现）——卷宗里根本不存在属于他的锚点段，模型能钉的只有
# 别人的锚点段（比如标签本身出现的那段）。钉证通过只证明"这段话真实存在
# 于原文"，证不出"这段话支持乙类候选就是标签所指之人"——保险形同虚设，
# 模型因此可以（也确实）把任意一个"人物谱说本集活跃"的候选安在任意一个
# 未解析标签头上。回退范围：删除 _prep_pack_functional_candidate_
# registered_names 及其唯一调用点；_prep_pack_functional_candidate_call
# 去掉 registered_only_profiles 参数与"人物谱登记显示在本集活跃……"提示词
# 分区；_prep_pack_resolve_functional_extra_candidate 候选集恢复为
# _prep_pack_functional_candidate_names 单一来源（甲类：规范名或已确认
# 别名在本集原文逐字出现）。李富贵在 EP1 的真实需求（只被写成"白白净净
# 身子较胖"、全章不具名，证据要到第 3 章"小胖子"称呼出现才能确立身份）
# 不属于本集候选判别的职责——那是跨章推理，本节的候选与证据检索范围本来
# 就限定在本集 source_text 内（见本节顶部大注释），不该靠扩大候选集在
# 本集内强行凑出一个原理上不存在的答案。
#
# 改动一保留，但发现了改动一自己此前一直没暴露的第五层根因，真实数据
# 复核（provider_calls id=10582，EP1，可复核 request_json）：目标标签
# "银色长袍女子"的卷宗仍是 A 侧事件跨度段 33-44 连续十二段，一条 B 侧都
# 没有——候选"许清"确认别名"许师姐"真实出现在段 52（"许师姐好手段，出门
# 一次竟带回了四个拥有资质的小娃"，48 字）与段 56，"每候选保底"却完全没
# 生效。根因不在保底本身的配额/字数计算（那部分改动一确实是对的），而在
# 更早一步——B 侧候选锚点扫描（_prep_pack_functional_candidate_anchor_
# pool）对"落在事件跨度内的段落"一律 `continue` 跳过，不进入任何候选的
# per_candidate_indexes。这段代码原本的意图是"事件跨度段已经算 A 侧了，
# 不用再重复给 label 逐字匹配算一次"，但同一个 continue 也连带跳过了
# candidate 匹配——如果某个候选自己的锚点文本恰好落在事件跨度内部（真实
# 数据实测：事件链抽取模型给"银色长袍女子"关联的三个事件 source_span 并集
# 是段 35-59，比看上去的"12 段"大得多，段 52/56 都落在这个并集内部），
# 这个候选就从"每候选保底"的候选池里彻底消失，只能眼睁睁被塞进 A 侧、跟
# 其余二十多段事件跨度材料抢那 4 个 A 侧保底位——这是 1.8.1/1.8.2 就已经
# 存在的缺陷（复核 provider_calls id=10469/10520 两次历史调用，用真实
# events 重放 _prep_pack_functional_candidate_event_span_segments 确认
# 事件跨度并集在那两轮就已经是段 35-59，"许师姐"段 52/56 同样从未进过
# per_candidate_indexes），1.8.1-1.8.3 三轮修复各自动的是保底"配额"和
# "字数"的分配规则，没有一轮碰过"候选锚点扫描本身要不要跳过事件跨度内的
# 段落"这个更早的输入侧问题，所以三轮都没能修好这个真实目标案例。
#
# 修复（本文件，_prep_pack_functional_candidate_anchor_pool）：扫描
# 不再对事件跨度内的段落跳过候选匹配——只有 label 逐字匹配这一支路才在
# 事件跨度内跳过（继续保留，理由不变：这段已经算 A 侧了，不需要重复计入
# label_text_indexes）；候选匹配对每一段都执行，不管这段是否已经在事件
# 跨度集合里。这样"每候选保底"的输入端 per_candidate_indexes 才是真正
# 完整的"这个候选在本集原文里出现过的全部段落"，不再有一整类"恰好落在
# 事件跨度内"的候选证据从一开始就对保底机制隐身。连带修复一处因此暴露的
# 二次 bug：保底层渲染时挑选截断锚点词的 anchor_hint 原先按"这段是否属于
# primary_index_set（事件跨度∪label 命中）"二选一用 label 或候选锚点词，
# 隐含假设两者互斥；候选保底段现在也可能同时落在事件跨度内，若不优先用
# 候选自己的锚点词，会退化成用原文里根本不存在的 label 去定位核心句，
# 截断退回"从头部截断"这个更保守的分支，有截掉候选证据本身的风险——
# 改为只要 guaranteed_b_anchor 里记录了这个段落的候选锚点词就优先使用，
# 没有才退回 label。两个既有上限常量（MAX_ENTRIES/MAX_CHARS）原样不变，
# 没有新增任何模型调用——这是同一份既有输入（本集 segments、既有事件跨度）
# 的检索完整性修复，不是放宽或收紧任何判定门槛。会实际改变发给候选判别
# 模型的卷宗内容本身（真正的 prompt-contract 变更），asset_manifest/
# event_chain 的 payload 结构与既有 provenance.method 取值集合均未改变，
# 比照 1.4.1/1.6.1/1.8.1/1.8.2/1.8.3 的先例推进版本号（第三位）。
#
# 1.8.5（根因收口：把 1.8.0-1.8.4 一直在下游"打补丁"的候选判别卷宗/检索
# 问题，往回收到真正的源头——事件链抽取阶段给角色起标签这一步，prompt-
# contract 变更，版本推进）：1.8.0-1.8.4 五轮修复全部发生在
# _prep_pack_resolve_functional_extra_candidate 这条下游候选判别通路自己
# 身上（卷宗检索完整性、保底配额、字数分配……），从未碰过一件事——这些
# 标签为什么一开始就没能直接命中别名注册表。真实落库复核（当前十集
# episode_prep_pack 最新快照）：EP1"许清"display_appellation="银色长袍
# 女子"（method=candidate_verdict，模型裁决而非确定性命中）、EP5"许清"
# 干脆没能落进 characters[]，标签"许姓女子"整段掉进 functional_extras
# （method=discovery，彻底未绑定）、EP8"赵武刚"display_appellation=
# "外宗同门"（同样 method=candidate_verdict）——而人物谱（Bible.characters
# [].aliases）里，这三个角色本来就各自登记着一个原文真实用过的称谓
# （honorific 类，跟这三个标签毫无关系），说明不是这些称谓不存在，是事件链
# 抽取模型在这几集里一次都没有把它写进 display_name，转而自己综合了一个
# 外貌/关系描述短语。候选判别机制本身工作正常（EP1/EP8 最终确实判对了），
# 但每次都要靠一次不保真的模型裁决"赌"出同一个本该靠别名表零成本查到的
# 答案，真实回归也确实观测到同一角色跨轮结果不稳定（EP5"许清"在此前定点
# 验证轮次绑上过，本轮又掉了）——这正是候选判别取代确定性别名命中要付的
# 代价。
#
# 修复（本文件，_extract_chunk 的"命名纪律"提示词分区）：新增一条
# characters 专属的取词优先级——本段原文中该角色只要存在称谓性表述（人名、
# 尊称、绰号等，不含只是概括所属群体/类别、换成同类另一个人也说得通的
# 泛称），display_name 必须逐字采用其中之一，不得改用自己综合的描述性
# 短语；仅当该角色本段原文通篇只有描述性表述时才允许描述性标签（跟既有
# "display_name 必须逐字出现在原文"这条硬约束正交，不放宽也不收紧那条）。
# 同一角色本段原文若有不止一种称谓，取本段出现次数最多的一个，次数相同
# 取最先出现的那个，本段所有事件统一取值——消除"同一角色不同事件换着用
# 不同措辞"这个额外的不稳定源。不改 _ModelCharacterMention/_ChunkResponse
# 的 schema 形状（display_name 仍是它，字段集合、必填/可选均未变），
# episode_prep_pack_chunk_v3 这个 schema_name 因此不需要跟着推进——但发给
# 模型的实际提示词文本变了，会实际改变部分角色标签的选词结果（进而改变
# 它们在 _resolve_assets 里落地的 method：真实带称谓的角色应更多从
# candidate_verdict/discovery 直接命中 alias/direct，不再需要那次模型
# 裁决调用），是真正的 prompt-contract 变更，比照 1.4.1/1.6.1/1.8.1-1.8.4
# 的先例推进版本号（第三位）。不改变候选判别机制本身、不放宽反幻觉主防线
# （"逐字必须出现在原文"），也不影响本就没有任何称谓、只能靠描述性标签的
# 真实无名群演——functional_extras 仍可正常吸收这类角色。
# 1.9.0（真实 EP5 回归 + 十集存量扫描确认为系统性问题，coverage_ledger 判定
# 语义变更，版本推进）：EP5 事件链第 1 个事件是"显示第五章标题《此子不错》"，
# 只覆盖 SRC0001 一段——章节标题（排版元素）被当成了剧情事件，一路流到分镜
# 台和视频。根因链条（已核实，不是猜测）：
#   a) app.domain.common._episode_source_text 把每章拼成
#      "【{chapters.title}】\n{chapters.content}"，而 chapters.content 自己
#      的首段又是原样重复的标题文本（真实数据：proj_3ac0b627fa46 EP5
#      chapters.idx=5，title="第五章此子不错"，content 以"第五章此子不错\n\n
#      ……"开头）——【标题】与 content 首段标题正文之间只隔一个换行，被
#      app.source_excerpt.index_source_segments 的空行分段规则合并成同一个
#      段 SRC0001，两次标题文本挤在同一段里。
#   b) 这不是这个问题第一次出现：5a67511（1.4.0）第一次给 paratext 账，用的
#      是纯确定性关键词+位置分类器，真实回归（proj_3ac0b627fa46,
#      chapters.idx=2）证明关键词表覆盖不住作者写法的多样性，遂在 6e27764
#      （1.4.1，同批次并入本文件当时的 1.5.0）整个改成"模型自报
#      paratext_segments + 三道确定性否决闸"——章节标题从"代码直接认定"
#      退化成"模型这次调用愿不愿意申报"。模型申报是非确定性的：EP5 自身
#      17 次历史重跑里，1.4.1 上线后 16 次中有 3 次漏报（约 19%）；EP1-EP10
#      当前产物 1/10 命中该缺陷。漏报时"洞即删戏"
#      （app.validators.assert_prep_pack_coverage_complete）仍强制 SRC0001
#      必须被某个事件覆盖，模型最省力的满足方式就是编一个只覆盖这一段的
#      "显示标题"伪事件——它逐字抄自标题原文的引文又恰好能通过引文锚地闸门，
#      于是合法通过洞即删戏/引文锚地/跨度有序全部三道致命闸门，无任何报错。
#      1.4.1 的三道否决闸从设计上就只否决"错误的申报"，从未被设计为补上
#      "模型压根没申报"这种缺失。
#   c) v1（5a67511）真正失败在"想用确定性规则覆盖一个本质上需要语义判断的
#      场景"——尾部作者求票/求收藏留言这类没有数据库锚点、只能靠语义理解
#      识别的通用 paratext，关键词表天然覆盖不全。本次不重蹈覆辙：只把
#      "这一段是不是本集自己某一章的标题"这个有 chapters.title 数据库列
#      作锚点、可以逐字比对的窄场景改回确定性判定（见
#      app.source_excerpt.chapter_title_segment_ids：段文本归一化空白后与
#      本集所属各章的 chapters.title 逐一比对，容许【】包裹与"标题在段内
#      重复出现两遍"这一已核实的拼接形态；判据完全从数据推导，不使用任何
#      人名/称谓硬编码名单）；尾部作者留言等没有数据库锚点的 paratext 判定
#      继续保持模型自报 + 三道否决闸不变，不扩大确定性判定的适用范围。
#      chapters.title 为 NULL/空串的章节退回 1.4.1 原有行为（既有
#      _CHAPTER_HEADING_RE 正则降级兜底，仍需模型申报），不产生新的失败面。
#   修复四处联动（app.validators.build_prep_pack_span_ledger 新增
#   chapter_titles 参数，详见该函数 docstring"确定性标题裁边"一节）：
#   ① 本文件 _extract_chunk 的提示词把确定性算出的标题段号直接告知模型
#      （既成事实，不是让它判断），本 chunk 不含标题段时提示词逐字节不变
#      （同 92c9e7a 认知卡注入的空态处理惯例）；
#   ② 确定性命中的标题段无条件计入 coverage_ledger.paratext 账户，不再
#      要求模型申报，模型申报了也不冲突（取并集）；
#   ③ 原排他闸（申报的 paratext 段落在某事件已验证 span 内即致命报错）对
#      确定性标题段改为确定性裁边（如 [1,5] 因段 1 是标题裁成 [2,5]），
#      同步更新该事件发布的 source_span；模型自报（非 DB 锚定）的 paratext
#      与事件 span 冲突仍然致命，未受影响；
#   ④ 事件裁边后 span 变空（即该事件从头到尾只覆盖标题段——正是本缺陷的
#      伪事件形态）新增为致命错误，明确报出"事件仅覆盖章节标题段"，防止
#      模型公然违背 ① 的确定性提示时问题被静默吞掉。
# 不改变 event_chain/asset_manifest/coverage_ledger 的字段集合（paratext
# 仍是既有的 flat [int] list），但会实际改变 coverage_ledger.paratext 的
# 判定结果与部分事件发布的 source_span（真正的判定语义变更，不只是提示词
# 措辞），比照 1.4.1 的先例（分类机制变更即使 payload 形状不变也要推进
# 版本号）推进版本号第二位。
# 1.10.0（两个已完成根因定位的缺陷，独立评审 blocker，prompt-contract 变更，
# 版本推进）：
#
# 缺陷 A 根因（_prep_pack_true_name_verdict 一族）：真名裁决问的是
# same/different/uncertain 是非题——app/stages.py 那条孪生路径（别名裁决庭）
# 在 7959b48 已经把同一种是非题形态改成候选判别（该提交信息原话："是非题
# 改为候选判别"），理由是是非题会诱发确认偏误：模型被直接问"X 是不是 Y"时，
# 倾向于确认递给它的假设，而不是独立枚举"X 还可能是谁"。prep_pack.py 这条
# 更早落地、结构几乎一样的孪生路径（角色/场景改名共用）当时没有跟着迁移。
# 本项目已有四次真实误绑事故印证这个失败形状：小胖子→王有材、上官修身边
# 男子→上官修（这两条被后续的反证/包含关系检查拦下，但拦截靠的是运气好
# 卷宗里恰好有反证段，不是机制本身不会诱发确认偏误）、孟浩←虎爷爷、
# 王腾飞←王师弟（后两条是 app/stages.py 那条孪生路径迁移前的真实误登记，
# 印证的正是同一种是非题问法的通病）。
# 钉证同样零保护：_prep_pack_pin_dossier_quote 只检查模型引用的
# supporting_quote 逐字存在于卷宗某一条里，不检查这条引句是否包含 alias
# 本身——卷宗的 single 桶天然会收录只含 alias 或只含 true_name 其中一个词
# 的段落，一个 same 判决完全可能钉在一句只谈 true_name、压根没提到 alias
# 的段落上。生产数据实测（data/manju.db，project_id=proj_3ac0b627fa46，
# 397 条历史 true_name_verdict 调用）坐实了这个数量级：114 条真实 same
# 判决里 56 条（49%）引用的 supporting_quote 缺 alias/true_name 至少一个；
# 只看明确询问人名（noun_label="人名"）的 75 条，18 条（24%）缺至少一个，
# 其中 16 条缺的是 true_name（下面会说明这类基本合法，不该拦）、2 条缺的
# 是 alias 本身（这才是真正的"零保护"——钉的话跟待判标签毫无关系）。
#
# 为什么不能一刀切要求引句同时字面包含 alias 与 true_name（双锚定）：
# 真名裁决的卷宗检索范围是全书 chapters 全表（_prep_pack_true_name_dossier
# docstring 开宗明义），不是本集正文——这是它跟 app/stages.py 那条孪生路径
# 的关键差异，后者的裁决卷宗缩小到已经定位到的单一桥接章（见 app/stages.py
# 模块顶部"A1a. 桥接章确定性检索"一节），本函数的检索对象天生跨章。别名
# 出现在本集，真名往往要到全书更靠后的章节才第一次被作者直接点名——这不是
# 理论推测，是本次修复前用真实语料验证过的结构性事实：EP5 目标标签
# "许姓女子"→"许清"，"许姓女子"只在第 5 章出现（4 次），"许清"二字在全书
# 第 34 章才第一次逐字出现（第 1-33 章全部是 0 次），中间桥接的是已确认
# 别名"许师姐"（第 5 章 6 次、第 12 章 18 次……），"许姓女子"与"许清"两个
# 字符串在全书任何一段里都不会同时出现——不是这次采样漏了，是结构上不存在
# 这样的段落。强制双锚定会把这条本该成立的绑定直接判死。
#
# 钉证规则（最终选定，三条约束叠加，都用真实数据验证过对目标场景零误伤）：
#   1. 是非题改候选判别（_prep_pack_true_name_verdict_candidates）：候选集
#      = suspected_true_name 本身 ∪ 人物谱/场景谱里在卷宗文本中有字面命中
#      的其它候选，加一个显式"都不是/无法确定"出口（enum 收紧协议层选项，
#      同 _prep_pack_functional_candidate_call 的写法）。候选来源完全是
#      结构化数据（Bible.characters/Bible.scenes）+ 卷宗文本的逐字包含
#      判断，不硬编码任何具体人名/场景名。
#   2. 钉证至少要求引句逐字包含被解析的那个 alias 本身（新增的结构性
#      前提，_prep_pack_verify_true_name_hypothesis 里
#      `alias not in pinned["text"]` 即拒）——这是零保护的主要来源，加上
#      这一条对上面 EP5 真实案例零伤害（"许姓女子"那两段本来就含 alias）。
#   3. 分情况处理 true_name 是否也要求逐字出现：
#      - 若全卷宗存在同时含 alias 与 true_name 的条目（both 桶非空，
#        dual_anchor_available=True），钉证必须钉在其中一条上，不接受
#        只钉了 alias 那一侧的弱证据——卷宗里明明有更强的桥接句摆在模型
#        面前，没有理由接受它舍强就弱；
#      - 若全卷宗结构上不存在双锚定条目（本项目 EP5 案例即此，
#        dual_anchor_available=False），退化为只要求钉住的条目含 alias、
#        且这个条目确实来自本集自己的原文（``pinned["text"] in
#        source_text``）——即"集内指代段落"，不是全书任意位置巧合复现的
#        同一个短语。这条限制不是理论推演：真实语料里"许姓女子"这个短语
#        在第 981 章又出现一次，但那段是完全不相关的转世预言片段（"第九山
#        轮回之魂，许姓女子，万鬼护送……"），跟 EP5 的孟浩/上官修剧情毫无
#        关系——没有"集内"这条限制，钉证可能钉在这类不相关的巧合复现上。
#      两种情形是否发生的判定结果（``dual_anchor`` 布尔值）不静默吞掉，写进
#      asset_manifest 条目的 provenance.dual_anchor 与 true_name_hints 里
#      accepted 记录的 dual_anchor 字段，可观测、可审计，符合本项目"禁止
#      静默降级"的硬性纪律。
#   钉证机制本身也从"逐字引句比对"改为"段号钉证"（_prep_pack_true_name_
#   pin_dossier_entry，参照 _prep_pack_functional_candidate_pin_segment 与
#   app/stages.py._alias_verdict_pin_segment 的既有先例）：生产数据里能
#   看到模型的 supporting_quote 有把多个卷宗段落拼接、摘要成一整句"证词"
#   的情形（provider_calls id=9700/10498），逐字比对因此系统性误杀部分
#   真正成立的判定；卷宗内容本身是代码检索出的真实原文，模型只需引用目录
#   编号，不存在编造空间，钉证退化为整数是否落在集合内的结构性判断。
#   顺带修一处可观测性缺口（未解析角色标签候选判别机制，1.8.0）：
#   provenance.method="discovery" 此前把"从未获得候选判别机会"（候选集/
#   卷宗为空，压根没发起模型调用）与"候选判别跑过但没选中"（发起了模型
#   调用，模型选了"都不是"或钉证未通过）坍缩成同一个值，只能翻
#   provider_calls 反推。新增 provenance.candidate_verdict_attempted
#   布尔字段区分两种情形（未新增 method 取值，scripts/episode_source_
#   audit.py 的 ANCHOR_VERIFIED_METHODS 无需同步登记）。
#
# 缺陷 B 根因（_pass 里的 skip_character_names 短路）：pass2 里
# `if name in skip_character_names: continue` 排在 suspected_true_name
# 核验代码之前执行——一旦某个提及的原始 name 恰好也被角色发现（本函数之外
# 一次独立的全集重新通读）判定为需要 skip（跟本提及毫无关系的另一处同名
# 巧合），这个 continue 会在核验代码执行之前就跳出循环，即使
# suspected_true_name 早在 pass1（skip_character_names 为空集，不存在
# 短路可能）里已经核验通过、钉证成功、accepted=True，pass2 重跑同一个提及
# 时这个已经成立的结论会被无声作废——4 条提及从未进入 unresolved_
# characters，候选判别循环里从未出现以这个字符串为标的的记录，
# true_name_verdict_cache 里明明已经缓存着 accepted 判决，却因为 continue
# 提前执行，代码根本没有运行到读缓存的那一行。真实 EP5 数据完整复现了这条
# 因果链（"许姓女子"→"许清"：pass1 accepted，pass2 因角色发现独立判定
# 另一段"许姓女子"为群演而被撞名短路作废，最终发布产物里"许姓女子"整段
# 掉进 functional_extras，method="discovery"）。
# 修法：把 suspected_true_name 核验代码移到 skip_character_names 短路
# 判断之前无条件执行（对所有提及一视同仁，包括最终会落 skip 的那些——
# 多数提及的 suspected_true_name 为空，核验代码内部第一行就直接返回，
# 不产生额外模型调用），短路条件追加 `and not via_suspected_true_name`：
# 已核验通过的信号优先于"角色发现独立通读凑巧撞出同名功能簇"这个更弱的
# 兜底信号。因为 pass1 与 pass2 遍历的是同一份 events 列表（同一组
# (alias, suspected_true_name) 组合），这次重排不增加任何新的模型调用——
# pass2 里被重新算到的核验请求，其 (subject_kind, alias, suspected_true_
# name) 组合在 pass1 已经跑过并写入 true_name_verdict_cache，直接命中
# 缓存复用，成本为零。
#
# 两个缺陷的交互（验收纪律）：缺陷 A 让钉证变严（alias 必须逐字出现、
# 双锚定优先），可能使某条此前"侥幸"通过的绑定在新规则下判定证据不足；
# 缺陷 B 修好只是让"已经成立的判决不再被短路作废"，不改变判决本身是否
# 成立。若 A 的新规则判定某条绑定证据不足，B 修好也不会让它重新绑上——
# 不为了让测试/某条真实数据"看起来绑上了"而放宽 A 新增的任何一条约束。
#
# 不改变 event_chain/asset_manifest 既有字段集合（新增字段均为可选，
# dual_anchor/candidate_verdict_attempted 都是纯附加），但会实际改变
# suspected_true_name 裁决的模型输出与部分绑定的钉证通过与否（真正的
# prompt-contract 变更），比照 1.6.1（同一类"是非题改措辞/范式"的先例）
# 推进版本号。
# 1.11.0（任务①，独立评审 blocker：反幻觉主防线覆盖面比它宣称的窄，见
# _prep_pack_mention_has_text_evidence 唯一执行点上方注释，schema 新增
# 可选字段+部分既有字段取值语义变更，prompt-contract 不变，版本推进）：
#
# 根因：literal_evidence 闸门只在 came_via_resolution=False（裸命中未
# 解析）时执行；标签一旦经过任何解析路径（alias/resolution/discovery/
# candidate_verdict）改名，闸门整体跳过——1.5.2 的理由"合法性由那条解析
# 路径自身的证据链承担"对 resolved_name 指向谁（身份正确性）成立，但
# candidate_verdict 只核验"标签指向谁"，从不核验 display_appellation/
# functional_extras.label 这两个"给观众看的原文称谓"字段字符串本身是否
# 真的逐字写在原文里——两件事被闸门自己的跳过条件混为一谈。真实数据坐实
# 两者是不同维度：EP1 display_appellation="银色长袍女子"（method=
# candidate_verdict，身份指向许清完全正确，候选判别钉证通过），但"银色
# 长袍"与"女子"在原文里相隔二十余字、分属不同短语，逐字形式不存在。
#
# 测量（只读 SQL，data/manju.db，project_id=proj_3ac0b627fa46，11 集
# 已发布 episode_prep_pack，来源=episodes.published_screenplay_
# artifact_id 或最新 status='approved' 的 artifacts.content_json，正文=
# episodes.source_chapters 指向的 chapters.content 按【title】\ncontent
# 两两换行拼接——同 app.domain.common._episode_source_text/scripts/
# episode_source_audit.py._build_source_text 完全一致的拼接方式）：
#   characters[].display_appellation（按 provenance.method；EP13 是
#   provenance 字段上线前的 1.3.0 旧包，3 条 method 缺失，不计入决策）：
#   direct 18 条非逐字 0、resolution 6 条非逐字 0、candidate_verdict 1 条
#   非逐字 1（上面 EP1 案例）——25 条里 1 条非逐字（4%），影响 1/10 当前
#   格式集（EP1）。
#   functional_extras[].label（同上，EP13 的 5 条旧包不计）：discovery
#   35 条非逐字 21（60%）、absorbed_speaker 3 条非逐字 2（67%）——38 条里
#   23 条非逐字（61%），影响 8/10 当前格式集（EP1/2/3/4/5/7/8/10）。
#
# 方案取舍（评估三案，选最后一案）：
#   (a) 硬闸门：非逐字一律拒绝，走既有内容族失败+attempt_hint 整包重试。
#   否决——functional_extras 61% 非逐字，8/10 集会触发失败；这批标签多数
#   是模型对纯描述性群演的合成短语（"白净微胖少年"这类，1.5.2 已经论证过
#   合法，不是幻觉），重试不会改变模型下次仍会合成短语的行为，只会反复
#   烧 220 秒/次整包重试预算，不收敛，属于制造重试而非解决问题。
#   (c) 纯标记，不做任何修正：新增 provenance.label_literal，不阻断，不
#   改 display_appellation/label 取值。否决为唯一手段——characters 侧
#   成本极低（1/25 条、1 集）就能做到真正修正而不只是标记，产品明确在意
#   的正是这类"字幕给观众看的称谓失真"，放着不修没有理由。
#   (d) 分级，最终选定：
#     characters[]：literal_evidence 为假时，若该绑定分支本来就要算出的
#     anchor_phrase 非空、且确实逐字出现在本集 source_text（防御性复核，
#     见下），用 anchor_phrase 替换 display_appellation——不新开任何证据
#     检索，anchor_phrase 是这条绑定分支既有变量，不是为这次修复新增的
#     检索。anchor_phrase 不可用（为空，或候选判别保底层命中过
#     _prep_pack_functional_candidate_truncate_segment 的截断、带省略
#     标记不再是纯净子串）时保留原始 name，只标 label_literal=False，
#     不伪造、不阻断——1/25 条量级，即使落进"只标记"分支，成本也可控。
#     不落回 resolved_name（全局规范名）：那正是 display_appellation 的
#     存在意义（1.7.0："决定字幕/台词显示，不随全局规范名改写"），提前
#     剧透跟本次要修的问题是同一枚硬币的另一面，不能拿一个错误换另一个。
#     functional_extras[]：只标记 provenance.label_literal，不替换
#     label——label 在这里不是纯展示字段，是内部真正的连接键（写侧
#     functional_extras 字典本身按 label 分组去重；读侧
#     _prep_pack_build_speaker_roster/_prep_pack_resolve_key_line_speakers
#     按 label 逐字匹配 key_lines[].speaker，两者出自同一次模型调用、
#     约定用词一致）——替换 label 的取值会让台词说话人匹配对不上同一批
#     群演，是这次不做的真实理由，不是偷懒。functional_extras 目前也没有
#     任何一处现成的、能同时满足"逐字""是称谓而非整段证据句"两个条件的
#     候选材料（discovery 分支的 anchor_phrase 候选序列只试 label 自己，
#     非逐字时天然为空；absorbed_speaker 分支的 anchor_phrase 是台词
#     原句，拿整句台词当群演"称谓"展示是另一种体验倒退）——先把独立判定
#     与可观测标记做实（数据积累），不无凭据地伪造替换材料。
#
# 落地：literal_evidence 计算本身不变（unconditional，不受 came_via_
# resolution 影响；变化的只是下游消费——此前只用于门禁判断，现在也用于
# display_appellation 取值与 label_literal 标记）；反幻觉主防线本身（裸
# 命中硬拒绝那道既有闸门）不动，不放宽也不收紧任何既有判定标准。新增
# provenance.label_literal 是纯附加可选字段（characters[]/functional_
# extras[] 每项都会带上，语义独立于 method，不影响既有消费者按 method
# 分支读取的逻辑），但 characters[] 部分条目的 display_appellation 实际
# 取值本身会变（真实产出语义变更，不是提示词变更），比照 1.9.0（判定
# 语义变更、payload 形状不变仍需推进版本号）的先例推进版本号。
#
# 任务②（K/M 并发化，见 _prep_pack_gather_concurrent /
# _prep_pack_collect_true_name_verification_requests 上方大注释）不单独
# 推进版本号：把 _pass() 内角色/场景两支 suspected_true_name 核验、以及
# 未解析角色标签候选判别，从"每条提及各自 await 一次"改成"先并发批量核验
# 预热缓存，_pass() 主循环原有的逐条读取/判定/写回代码一行不改"，只改
# 这些既有模型调用的发起方式（并发 vs 串行）与内部执行路径，不改变发给
# 模型的提示词内容、不改变任何裁决判据、也不改变 characters/scenes/
# functional_extras/errors 等最终产物的取值——本文件历次版本号推进的
# 判据（1.4.1/1.6.1/1.8.x/1.9.0/1.10.0）全部是"发给模型的实际内容变了"
# 或"某个判定的语义变了"，这次两条都不成立，产物对同一份输入逐字节不变
# （见 tests/test_prep_pack_asset_discovery.py 的并发完成顺序确定性红灯：
# 两次以相反完成顺序跑完同一份输入，characters/functional_extras 逐字节
# 相同），所以不占用版本号——版本号是产物契约的版本，不是实现细节的版本。
# 1.11.1（真实回归，1.11.0 上线后首次真实生成 EP1 当场复现，characters[]
# 侧处置手段撤回，判定语义变更，版本推进）：1.11.0 批准时没有核实
# anchor_phrase 的实际形态就拍板了替换方案。真实证据（1.11.0 生成的 EP1
# 产物，data/manju.db 只读复核确认）：许清 display_appellation 变成
# ``“许师姐好手段，出门一次竟带回了四个拥有资质的小娃。”两个男子中的一人，
# 带着恭维向着那女子说道。``——candidate_verdict 分支的 anchor_phrase 取的
# 是钉证命中的整条卷宗段落原文（见上面该分支"anchor_phrase 直接取钉证命中
# 的卷宗段落原文本身"的既有注释），从设计上就不保证是短语；对当前十集已
# 发布产物全量复核（characters[]/functional_extras[] 两侧、全部 method）
# 证实这不是孤例：anchor_phrase 非空样本里，characters[] 侧 25 条中位数
# 长度 3 字符但均值 22.8 字符、最长 130 字符（李富贵，EP6，整段场景描写），
# functional_extras[] 侧 25 条中位数 5 字符、均值 16.5 字符、最长 58 字符，
# 两侧均有 7-10 条明显是带句号/引号的完整叙述句而非称谓（"围观弟子"←
# "竟然是上官师叔亲自来发丹……"56 字整句）。anchor_phrase 的真实身份是
# "证据段落"（钉证机制拿它证明"这段话支持这个绑定"），不是"称谓候选"，
# 1.11.0 把两者当同一种东西处理是这次误判的根因。替换前的合成标签
# （如"银色长袍女子"）虽非逐字，但至少是原文措辞组成的、观众能看懂的称谓，
# 替换后的整句旁白让字段直接不可用——严格更差，是真实回归不是改进。影响面
# （只读 SQL 复核，同上范围）：当前十集已发布产物里仅 1 条 characters[]
# 记录被 1.11.0 实际替换成了句子——EP1"许清"（上面的复现案例本身，唯一
# method=candidate_verdict 且非逐字的记录）；EP6 也在 1.11.0 版本重新生成
# 过，但 EP6"许清"/"李富贵"两条的 name 本身逐字出现在原文（literal_
# evidence=True），从未进入替换分支，不受影响。
#
# 修复：characters[] 侧改为跟 functional_extras[] 侧同一处置——只标记
# provenance.label_literal，不替换 display_appellation 的取值（撤回
# 1.11.0"用 anchor_phrase 替换 display_appellation"这一段，1.11.0 大注释
# 原文保留作历史记录，不删除）。"标签用词接地"（label_literal）与"身份指向
# 正确"（method/anchor_segments 既有职责）仍是两个独立判定，只是前者的
# 处置手段从"确定性替换"降为"如实标记"——可替换的确定性来源只有
# anchor_phrase，而它是证据段落不是称谓，替换会让字段不可用；宁可保留一个
# 非逐字但可用的称谓（原始 name）并如实标记 label_literal=False，也不要
# 一个逐字但不可用的整句旁白。评估过、本次不做：有没有一条确定性的办法从
# anchor_phrase 里抽出一个真正的称谓短语？没有找到——从证据段落里抽短语
# 需要语义判断（"这段话里哪几个字是在称呼这个人"），任何基于长度阈值/
# 标点切分/首尾N字的启发式都对真实语料（钉证命中的段落里称谓可能在句首、
# 句中引号内、或整句都是叙述完全不含称谓）系统性失效，且本项目禁止名单式
# 判定（见 no-blacklist-fixes 纪律）——留给以后有专门语义抽取机制时再做，
# 不在这次回归修复范围内伪造一个脆弱的替代方案。
# 保留不动：literal_evidence 计算本身仍是 unconditional（不受 came_via_
# resolution 短路，1.11.0 这条改动本身是对的）；provenance.label_literal
# 标记机制不变；functional_extras[] 侧处置不变（本来就只标记）。不改变
# event_chain/asset_manifest 既有字段集合（label_literal 仍是纯附加可选
# 字段），但 characters[] 部分条目（EP1 许清这一类）的 display_appellation
# 实际取值会变回 1.11.0 之前的合成标签而非替换后的整句，是真正的产出语义
# 变更，比照 1.4.1/1.6.1/1.8.1-1.8.5/1.9.0/1.10.0 的先例推进版本号（第三
# 位，不动 schema 位——跟 1.8.4 同一类"协调层复核，真实数据落库后确认误
# 绑，回退处置手段"的先例）。
# 2.0.0（用户判定链路错误，架构收窄，schema 大幅变更，版本主位推进）：产品
# 判定原链路（小说 -> 剧本台产出 event_chain -> 分镜台消费）本身走错了——很多
# 小说不适合转剧本，真正要的是"把小说改写成视频生成提示词"，剧本台存在的
# 唯一理由是保证传给生成模型的图片素材合理（谁、哪张图、哪个场景）。本次
# 改造把这个模块从"剧本台"改造成"映射台"，职责收窄为三件事：①发现本章
# 新人物/新场景；②把人物/地点映射到世界书（Bible）已有的图像素材；③把
# 原文里的模糊人物称谓映射成人物谱里的精准称谓。事件链的定量职责（这一
# 集有几个片段/节拍）连同 event_chain 本身、hook/cliffhanger、key_lines/
# 台词抽取全部砍掉——不再是这个模块的职责，下游分镜台改为直接从原文自己
# 提节拍表（另一 agent 的工作范围，见交付说明）。
#
# 锚点从 event_ids 换成 segment_indexes 不是改名，是真实重新推导：旧版
# characters[]/scenes[]/functional_extras[] 用"这个资产出现在哪些事件"
# （event_ids）记账；事件没了，新版每一条 asset_manifest 记录直接携带
# ``segment_indexes``——这个资产真正在场的原文段号，来源是 _extract_chunk
# 让模型对每个人物/场景/道具的提及**直接**申报它出现在本 chunk 哪些段号
# （不再经过"先分事件、再从事件跨度反推段号"这道间接层）。
# _prep_pack_gate_segment_indexes 只做一道结构闸：申报的段号必须落在模型
# 这次调用真正看到的 chunk 范围内（防止编造压根没读过的段落归属），刻意
# 不在这里额外要求 display_name/label 逐字出现在该段落——那道逐字证据闸
# 早就存在（_prep_pack_mention_has_text_evidence，见"称谓证据闸"），但
# 只对"裸直接命中"生效，经 alias/discovery/candidate_verdict 任何一条
# 解析路径绑定的合成描述性标签（真实 EP1 案例"银色长袍女子"从未逐字出现
# 在原文）一直被刻意豁免（1.5.x task②、1.8.0-1.8.5 五轮真实回归的共同
# 结论）——段号入口若在这里重复要求逐字命中，会在候选判别机会到来之前就
# 把整条提及连同 segment_indexes 一起丢弃，直接堵死候选判别机制，见
# _prep_pack_gate_segment_indexes 上方完整说明。"名字出现 ≠ 人在场"这条
# 判据因此完全交给模型的语义职责（提示词明确要求只申报"画面中出场"的
# 段号，不是被提及/被回忆/被转述的段落）+ _resolve_assets 既有的按 method
# 分支各自核验（不针对任何具体人名/场景名做特判，见 no-blacklist-fixes
# 纪律），不是新发明一套判据，也不在段号入口重新发明一遍。
#
# label_literal（1.11.0/1.11.1）在 2.0.0 里整体撤下——不是因为它变得
# 恒真（上面刚论证过合成标签仍然合法且常见非逐字），是纯粹的范围收窄：
# 映射台现在只对"绑定到谁"（method/anchor_segments/anchor_phrase）负责，
# "给观众看的这个称谓好不好看/是不是逐字"这类纯观测性标记不再是这个更
# 收窄的模块职责，需要时可以在分镜台消费 display_appellation 时自行判断。
#
# 新增 props（世界书没有道具素材库，只出 label+description 文字描述，
# 不映射图；跟 characters/scenes 同一套逐段证据闸，但没有身份消歧/发现
# 环节——道具就是它自己，按 label 精确字符串去重）与 appellation_map
# （把 _resolve_assets 内已有的别名消歧结论——_prep_pack_bible_alias_
# owner/_prep_pack_cross_episode_alias_conflict 等既有机制不动——显式
# 摊平成一张 (raw_mention, segment_index) -> (identity_id,
# canonical_appellation) 表，不是另起一套消歧逻辑）。
#
# 保留不动（本次架构收窄明确不影响）：本章新人物/新场景发现
# （_discover_new_characters/_discover_new_scenes）；跨集别名冲突检测
# （_prep_pack_cross_episode_alias_conflict 系）；suspected_true_name
# 声明-核验通道（_prep_pack_verify_true_name_hypothesis 系）；未解析
# 角色候选判别（_prep_pack_resolve_functional_extra_candidate 系，锚点
# 输入改用 mention 自带的 segment_indexes 直接算并集，不再需要"标签所属
# 事件跨度"这层间接——见 _prep_pack_functional_candidate_label_segments，
# 是 _prep_pack_functional_candidate_event_span_segments 的直接替代，
# 逻辑更简单，行为对齐 1.8.1 引入事件跨度锚点时想解决的同一个问题（标签
# 字面命中原文会打空），只是现在不需要事件跨度这层中介就能拿到同样精确
# （更精确）的锚点段落）；provenance 校验（_prep_pack_verify_manifest_
# provenance）；coverage_ledger 五账投影（重新实现为
# _prep_pack_build_coverage_ledger，直接基于已核验的 segment_indexes
# 并集 + 已过闸的 paratext 集合，不再经过 app.validators.
# build_prep_pack_span_ledger 那套事件跨度账本——该函数与
# assert_prep_pack_span_union_matches_ledger 留在 app/validators.py 原地
# 不动、不删（跟本文件 app/production/screenplay_repair.py 同一条"停用不
# 删除"先例），本模块新流程不再调用，只是不再是唯一实现；
# assert_prep_pack_coverage_complete 复用不变，只看 uncovered 是否为空，
# 跟具体怎么投影出来的无关）。
#
# 下游消费方处置（不在本次改造范围内，详见交付说明里的消费方清单）：
# event_chain 曾经的消费方（app.production.screenplay_authority.
# project_prep_pack_to_screenplay、app.domain.storyboard_ops、
# app.storyboard_supervisor 的 spine_n 兜底估算等）全部是分镜台侧代码，
# 依约不在本次改动范围内，交由另一 agent 协调更新；hook/cliffhanger 的
# episodes.hook/episodes.cliffhanger 写入本来就会被 app/production/
# publish.py 在真正发布时用 script.ending_hook 覆盖（那是发布时的权威
# 来源，不是 prep_pack 阶段的），prep_pack 不再预写这两列不是能力回退。
#
# 2.0.1（bug fix，补测试缺口过程中发现、协调方独立复现确认）：appellation_
# map 的构造真源从"拿 characters[].aliases 反查 character_mentions"改成
# "_resolve_assets 在解析每条角色提及时原地记录自己的结论"（见
# _prep_pack_build_appellation_map 上方"2.0.1 根因"大注释）。根因是拿
# aliases 的字面证据门槛（保护跨集别名注册表不被合成标签污染的判据）冒充
# "这条提及有没有解析出身份"的判据——两者是不同维度，混用后模糊/描述性
# 称谓（"穿杂役衫的魁梧大汉"一类，恰是这张表存在的理由本身）在已经真实
# 解析成功、发布进 asset_manifest.characters 的情况下仍会从 appellation_
# map 里静默消失。aliases 字段本身的语义/门槛不变；_resolve_assets 新增
# 可选出参 appellation_resolutions（默认 None，不影响其余全部既有调用点
# 的返回元组形状）。产出语义变更（appellation_map 的实际行数、不是字段
# 形状），比照 1.4.1/1.6.1/1.8.1-1.8.5/1.9.0/1.10.0/1.11.1 的先例推进
# 版本号第三位，不动 schema 位。
#
# 2.0.2（真实回归，48e01ff 当晚上线即复现，ERR-20260826-37cf79，EP1 五个
# 场景全灭）：修 2.0.0 砍事件链时留下的一个真实证据源缺口，不是新功能。
# 根因：场景 resolution/discovery 两支的锚点候选（见下面 _pass() 里
# "场景绑定的锚点候选"一节）原本是 [canonical_scene_name, name,
# *scene_event_evidence_quotes]——第三路 scene_event_evidence_quotes 来自
# event_chain[].source_evidence[].quote（事件抽取模型逐字摘录、经
# _prep_pack_local_text_anchor 逐字校验的原文片段），是唯一一路"独立于
# 场景名本身"的证据；前两路（canonical_scene_name/name）在场景名是模型
# 综合出的合成标签时结构上必然落空（EP1 五个场景全部如此："大青山山顶"/
# "大青山半山腰裂缝"等无一逐字出现在原文，原文写的是"这青山顶端"/"山腰
# 裂缝"）。48e01ff 砍掉 event_chain 时，_pass() 里的候选表被如实收窄成
# [canonical_scene_name, name]（见该处注释"2.0.0 起不再额外拼接
# scene_event_evidence_quotes——那是该场景所涉事件的 source_evidence 地点
# 描述短语，event_chain 撤销后不再存在"），但没有补一条替代的独立证据
# 源——candidate_verdict_pins（人物侧候选判别）、true_name_pinned_quote
# （suspected_true_name 核验）这些独立证据机制场景侧本来就没有，唯一
# 依赖的正是被砍掉的那一路，结果 resolution/discovery 两支合成场景名
# 100% 落空，has_scene_anchor 门禁具名拦截——这是 48e01ff 引入的结构性
# 回归，不是本次新发现的既有缺陷（has_scene_anchor 门禁本身、
# _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR 判据均不动，见该常量与
# _prep_pack_verify_manifest_provenance 上方各自的完整说明——这道门禁
# 是 scripts/episode_source_audit.py 实测 19 条 A2_scene_no_text_evidence
# 换来的，绝不放宽）。
#
# 修复：不是在事件层面恢复证据（事件链本身不回来，产品判断不变），是把
# 同一个"逐字引文"证据形状下沉到 _ModelSceneMention 自己身上——新增
# ``quote``（required str，可以是空字符串，语义严格对齐旧
# _ModelSourceEvidence.quote：从这条提及自己申报的 segment_indexes 任一
# 编号原文里逐字摘录、能证明"这就是这个地点"的一段原文，不得改写/概括/
# 跨编号拼接；这条提及在本段确实没有可摘录证据时如实留空，不编造）。
# _extract_chunk 提示词新增对应字段说明；_pass() 里 scenes 的 resolution/
# discovery 两支候选表恢复成 [canonical_scene_name, name, scene_quote]
# （跟旧候选表同构，唯一区别是第三路的申报粒度从"事件"下沉到"提及"，
# 对审计而言是更精确的绑定，不是更弱的证据）；alias 分支
# （_prep_pack_scene_alias_provenance 的第三个参数）同样恢复传入这条
# 提及自己的 quote，不再传空列表——该函数早就为这个用途保留了这个参数
# （见其 docstring），只是 48e01ff 之后一直传空。
#
# 为什么这不是同义反复（红线判据，见 _prep_pack_local_text_anchor 上方
# "跨集别名场景绑定的锚点强化"一节对"同义反复"的完整定义）：quote 不是
# name/canonical_scene_name 的重复或变体，是模型对"这段原文是不是在写
# 这个地点"这个独立语义问题给出的另一次单独申报，且必须逐字命中它自己
# 声称的原文段落才会被 _prep_pack_verify_manifest_provenance 采信——跟
# name 本身是否逐字出现是两个不同的判据，二者可能同时为真、同时为假、
# 或一真一假（EP1 五个场景就是"name 假、quote 真"的真实样本）。
#
# 人物侧核实（用户点名"查清楚，别只修报错的那一半"）：characters[] 从
# 1.6.0 引入 _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR 起就只对 scenes[]
# 生效——_prep_pack_verify_manifest_provenance 对角色调用 _check(...) 时
# 从未传过 require_anchor=True（characters 侧从未存在过 requiring-anchor
# 常量，见该函数与 _check 内部逻辑），48e01ff 之前如此、之后也如此，不是
# 今晚改出来的差异。这不是"角色侧恰好没触发"——是角色侧的空 anchor_
# phrase 结构上永远被自校验豁免放行，has_scene_anchor 这类具名拦截压根
# 不适用于 characters[]。这本身是一个更宽松的既有判据（跟本次事故的
# 因果链无关，48e01ff 没有改动它），不在这次回归修复范围内收紧——收紧
# 门禁判据是范围外的产品决策，不是"修一个当晚引入的回归"应该顺带做的事。
#
# 2.0.3（真实回归，EP4 离线复现：54 段章节 chapters.idx=4，"我欲封天"
# proj_3ac0b627fa46——只用 index_source_segments/_chunk_segments 跑通
# 分块逻辑，未触发任何模型调用/发布）：asset_manifest.scenes 只出了 1 条
# （"外宗宝阁一层"，segment_indexes 2~20），但原文在 21 段之后明确切了
# 至少两个新地点（24 段起"外宗边缘单人居所"、45 段起"外宗放丹广场"，两个
# 名字都已经在这个项目的 bible.scenes 里登记过，不是需要新发现的生词）。
#
# 根因排查（三个怀疑方向逐一核验，结论都写在这里，不是猜测）：
#   1) 分块合并只保留第一个 chunk？排除——离线用真实章节文本跑
#      _chunk_segments 证明这四章（idx 4/8/9/10，含用户对照的 EP8/9/10）
#      全部落在 _CHUNK_MAX_CHARS=6000 的单个 chunk 里，压根不存在"跨
#      chunk 合并"这一步，_generate_prep_pack_once 的 chunk 循环也早已
#      验证过是对每个 chunk 的 mention 做累加（append/update），不是
#      "只留最后一次"。
#   2) 场景去重/合并把新场景吃掉？排除——ensure_scenes_for_labels
#      （app/scenes.py）对传入的每个未匹配 label 逐个跑 assess_new_scene，
#      不存在"每次调用最多发现 N 个新场景"的配额或提前 return；
#      visual_entity_merges 表（db.py 建表处、portraits.py 写入处）只服务
#      角色侧的 functional→具名身份折叠，不涉及场景。且 scenes[entry]
#      的合并本身是并集（entry["segment_indexes"] = 现有 | 新增，见
#      _resolve_assets scenes 分支），会漏字段只可能是"这条 mention
#      从未被模型报出来"，不可能是"报出来了又被合并丢掉"——2-20 到 21
#      这个干净的截断本身就是证据：如果是合并丢失，丢的应该是散点，不会
#      恰好在"宝阁大门关闭"（21 段）这个真实的场景边界上戛然而止。
#   3) 真正成立的：模型在单次调用里只完整报出了本段最先出现的那个场景，
#      对同一次调用后半段（24 段起，接近本段末尾）的场景转换未申报——
#      characters/props 两类在同一次调用里没有这个问题（下游故事线仍能
#      跟住主角），只有 scenes 这一类在长 chunk 里出现"报了开头、漏了
#      结尾"的退化，与既有 2.0.0 大注释里角色侧记录过的
#      character_manifest_anomaly（第31轮 EP7 回归）是同一种"单一维度
#      在长输出里提前收尾"的模式，只是这次出现在 scenes 而不是
#      characters。这是模型行为层面的证据链（章节内容、bible 已登记场景名
#      核对、代码路径排除），不是靠重新触发一次真实模型调用验证的——按
#      边界要求没有发起那次调用，如果需要更强的"prompt 改了之后模型真的
#      按新指令报全"这一层验证，要靠真实运行确认，不在这次改动范围内。
#
# 修复两处，都不改变现有字段的形状、不新增门禁、不做兜底填充：
#   a) _extract_chunk 提示词新增"场景的持续性"一段（segment_indexes 判据
#      段落之后）：明确告诉模型场景在同一地点的后续编号里默认延续，不需要
#      每个编号都重新出现地点描写才能计入，只有情节明确换地点/原文写明
#      离开才停止延续——针对性回应上面第 3 点证据，不是通用的"别漏报"
#      重复表述（那句已经存在，没能挡住这次退化）。
#   b) coverage_ledger 新增一个并列账目 scene_coverage（scene_delivered/
#      scene_uncovered，语义见 _prep_pack_scene_coverage_account 的
#      docstring）：不影响、不参与既有五账或 assert_prep_pack_coverage_
#      complete 门禁（该门禁只读 ledger["uncovered"]），单纯让"这一章
#      有多少段落完全没有任何场景归属"在映射台自己的产出里就可见，不用
#      等分镜台的三态告警才第一次被看见。scene_uncovered 非空是合法状态
#      （例如确实没有场景描写的纯心理/纯对白段），这里只记账、不拦截、
#      不用上一段落的场景往后续段落填充——那是伪造归属，比空着更危险。
QA_PROFILE_VERSION = "prep-pack-qa-gate-1"
_QA_EVALUATOR_NAME = "screenplay_production_qa"
_CHUNK_MAX_CHARS = 6000

# Mirrors app.domain.common._placeholder_bible's literal (that module is not
# importable here -- it is exec()'d into app.api's namespace, not a normal
# module, see app/domain/__init__.py). Only reached when a project's bible_json
# is still empty, which real prep_pack runs never hit by EP2 (EP1's screenplay
# already required a bible); kept only so discovery degrades instead of
# crashing on that edge case.
_FALLBACK_VISUAL_STYLE = "国漫风格，非真人CG渲染，统一电影感光影，暖灰色调"

# app.portraits identity-resolution kinds that resolve a mention without a
# character card/portrait: "functional_identity" is a typed one-off (确定性
# 群演，见 docs 任务描述), "reference_identity" is a stable authority that is
# only referenced, never on-screen this episode. Both mean "resolved, no asset
# required" for asset-mapping purposes -- not a gate failure.
_FUNCTIONAL_RESOLUTION_KINDS = {"functional_identity", "reference_identity"}


class PrepPackGateError(ValueError):
    """One generation attempt failed a deterministic hard gate; retryable.

    ``had_events``（第23轮 ERR-20260824-7ab7cb 真实回归修复）记录这次尝试在
    触发这道门禁之前，事件链抽取本身是否拿到过任何事件。默认 True——绝大
    多数门禁（跨度账本、覆盖完整性、资产映射、说话人解析、hook/cliffhanger
    接地……）都发生在"本集未抽取到任何事件"这道最早的门禁（见
    _generate_prep_pack_once 里 `if not raw_events` 的唯一 raise 点）之后，
    此时事件链必然非空。只有那一道门禁本身传 False。见
    run_episode_prep_pack 的采纳护栏：一次退化为空事件链的重试，不得静默
    覆盖此前一次真的抽到了事件、只是被别的门禁拒绝的尝试。
    """

    def __init__(self, message: str, *, had_events: bool = True) -> None:
        super().__init__(message)
        self.had_events = had_events


# ---------------------------------------------------------------------------
# Model response schemas
# ---------------------------------------------------------------------------

class _ModelCharacterMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    # 1.5.0 (kept in 2.0.0): model-declared prior-knowledge hypothesis (real
    # EP5 finding: outright banning this discarded a genuinely CORRECT guess
    # -- see _prep_pack_verify_true_name_hypothesis below). display_name
    # must still be the verbatim in-episode term of address; this field is
    # never used to replace it, only as an unverified candidate for _pass to
    # check.
    suspected_true_name: str | None
    # 2.0.0: this mention's own claim of which segments (global 1-based,
    # same numbering the model was shown in this chunk) it is actually
    # ON-SCREEN in -- not merely named/recalled/heard-of elsewhere. This
    # replaces the old event_id/source_span indirection: segment_indexes IS
    # now the segment-attribution claim (see _prep_pack_gate_segment_indexes
    # for the deterministic per-segment literal-evidence gate every
    # declared index must clear before being trusted).
    segment_indexes: list[int]


class _ModelSceneMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    suspected_true_name: str | None  # isomorphic to the character field above
    segment_indexes: list[int]
    # 2.0.2 (real regression fix, see PREP_PACK_VERSION's 2.0.2 note above):
    # a verbatim excerpt from one of this mention's own segment_indexes that
    # supports "this is that place" -- isomorphic to the old, now-removed
    # event_chain[].source_evidence[].quote, just declared at mention grain
    # instead of event grain. Required (not Optional) matching this module's
    # strict-schema convention; legal to be "" when this mention genuinely
    # has no excerptable evidence in this chunk (never fabricate one). This
    # is the sole reason the field exists: display_name/canonical scene
    # names are frequently model-synthesized labels that never appear
    # verbatim in the source text (real EP1: "大青山山顶" vs source "这青山
    # 顶端"), so they cannot themselves serve as independent local-text-
    # anchor evidence for resolution/discovery scene bindings -- see
    # _prep_pack_local_text_anchor's "同义反复" note and _pass()'s scene
    # anchor-candidate section below for how this flows into anchor_phrase.
    quote: str


class _ModelPropMention(BaseModel):
    """2.0.0, new: a physical object/item the episode actually shows on
    screen. No bible image library exists for props (unlike characters/
    scenes) -- this is a text-only asset, ``description`` is its only
    payload, never a portrait_id/scene_reference_id/visual_entity_id."""
    model_config = ConfigDict(extra="forbid")
    label: str
    description: str
    segment_indexes: list[int]


class _ChunkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    characters: list[_ModelCharacterMention]
    scenes: list[_ModelSceneMention]
    props: list[_ModelPropMention]
    # 1.4.1 (kept in 2.0.0): the model's own paratext claim for this chunk
    # (chapter title / author's note segments) -- untrusted like every other
    # model claim in this module; see _prep_pack_build_coverage_ledger for
    # how this gets reconciled against the DB-anchored deterministic chapter
    # -title segments and against segments that DO carry verified asset
    # evidence (an asset-bearing segment cannot also be paratext -- the
    # asset evidence wins, the paratext claim is rejected and logged
    # observably, see rejected_paratext_claims). Required (not defaulted),
    # matching every other field's strict-schema convention in this module
    # -- an empty list is a legal, explicit "none in this chunk", not an
    # omission.
    paratext_segments: list[int]


def _response_format(model_type: type[BaseModel], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": model_type.model_json_schema(),
        },
    }


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

def _chunk_segments(
    segments: list[SourceSegment], *, max_chars: int = _CHUNK_MAX_CHARS,
) -> list[list[tuple[int, SourceSegment]]]:
    """Group indexed segments into model-call-sized chunks (长章节切块)."""
    indexed = list(enumerate(segments, start=1))
    if not indexed:
        return []
    chunks: list[list[tuple[int, SourceSegment]]] = []
    current: list[tuple[int, SourceSegment]] = []
    current_chars = 0
    for item in indexed:
        _, segment = item
        segment_chars = len(segment.text)
        if current and current_chars + segment_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += segment_chars
    if current:
        chunks.append(current)
    return chunks


def _render_chunk(chunk: list[tuple[int, SourceSegment]]) -> str:
    return "\n\n".join(f"【{index}】\n{segment.text}" for index, segment in chunk)


# 段号结构闸（2.0.0，见 PREP_PACK_VERSION 上方 2.0.0 大注释"锚点从
# event_ids 换成 segment_indexes"一节）：一个提及（角色/场景/道具）自报的
# 每一个 segment_index，必须落在本次 chunk 自己的全局段号范围内——防止模型
# 把别的 chunk 的段号写到这里，每次 chunk 调用只看得到自己那一段原文，
# 声称之外的段号结构上不可信、必须丢弃。
#
# 刻意不在这里额外要求 display_name/label 逐字出现在该段落原文里：那道
# 逐字证据闸本来就已经存在（_prep_pack_mention_has_text_evidence，
# _resolve_assets 内"称谓证据闸"一节），但只对"裸直接命中"（没有经过
# alias/discovery/candidate_verdict 任何一条解析路径）生效，长期以来
# （1.5.x task②、1.8.0-1.8.5 五轮真实回归）刻意豁免经解析路径绑定的合成
# 描述性标签——例如真实 EP1 案例"银色长袍女子"从未逐字出现在原文（原文写
# "穿着一身银色长袍"），要靠候选判别（_prep_pack_resolve_functional_
# extra_candidate）独立的卷宗检索+钉证才能正确绑定许清；如果在这里（比
# _resolve_assets 更早的入口）就要求 display_name 逐字命中它自己声明的
# 段落，会在候选判别机会到来之前就把这整条提及连同它的 segment_indexes
# 一并丢弃，直接堵死候选判别机制——不是收紧反幻觉防线，是重新引入五轮
# 真实回归修过的同一个缺陷。评估过、放弃：per-segment 逐字闸看似能"更
# 精确"，但精确的代价是打断已经证明有效、职责单一的既有分工（模型申报语义
# 判断 -> _resolve_assets 按 method 分支各自核验）。
#
# "这段文字里出现了这个名字"从来不是也不该是"这个人真的在画面里出场"的
# 判据本身——后者是模型的语义职责（_extract_chunk 的提示词明确只要求申报
# "画面中出场"的段号，不是被提及/回忆/转述的段落），不针对任何具体人名/
# 称谓做特判，也不使用任何人名/称谓硬编码名单（no-blacklist-fixes 纪律）。
def _prep_pack_gate_segment_indexes(
    label: str, declared_indexes: list[int],
    chunk_global_indexes: set[int], chunk_by_index: dict[int, SourceSegment],
) -> list[int]:
    label = str(label or "").strip()
    if not label:
        return []
    verified: set[int] = set()
    for raw in declared_indexes:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        if index in chunk_global_indexes and index in chunk_by_index:
            verified.add(index)
    return sorted(verified)


def _known_character_names(conn, project_id: str, episode_no: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT character_name FROM character_portraits "
        "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY character_name",
        (project_id, episode_no, episode_no),
    ).fetchall()
    return [str(row["character_name"]) for row in rows]


def _known_scene_names(conn, project_id: str, episode_no: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT scene_name FROM scene_references "
        "WHERE project_id=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY scene_name",
        (project_id, episode_no, episode_no),
    ).fetchall()
    return [str(row["scene_name"]) for row in rows]


def _prep_pack_chapter_titles(
    conn, project_id: str, chapter_indexes: list[int],
) -> list[str]:
    """This episode's own DB-anchored chapter titles (1.9.0, see
    PREP_PACK_VERSION's 1.9.0 note above). Only non-NULL, non-blank titles
    are returned -- a chapter whose ``chapters.title`` is NULL/blank is
    simply absent from the result, which is exactly the signal
    app.source_excerpt.chapter_title_segment_indexes and
    app.validators.build_prep_pack_span_ledger's chapter_titles parameter
    need to fall back to the pre-1.9.0 regex+model-declare path for that
    one chapter (see build_prep_pack_span_ledger's docstring)."""
    if not chapter_indexes:
        return []
    placeholders = ",".join("?" for _ in chapter_indexes)
    rows = conn.execute(
        f"SELECT title FROM chapters WHERE project_id=? AND idx IN ({placeholders})",
        (project_id, *chapter_indexes),
    ).fetchall()
    return [
        str(row["title"]) for row in rows
        if row["title"] is not None and str(row["title"]).strip()
    ]


def _resolve_portrait_id(conn, project_id: str, character_name: str, episode_no: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY ep_start DESC LIMIT 1",
        (project_id, character_name, episode_no, episode_no),
    ).fetchone()
    return str(row["id"]) if row else None


def _resolve_scene_reference_id(conn, project_id: str, scene_name: str, episode_no: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM scene_references WHERE project_id=? AND scene_name=? "
        "AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY ep_start DESC LIMIT 1",
        (project_id, scene_name, episode_no, episode_no),
    ).fetchone()
    return str(row["id"]) if row else None


def _prep_pack_scene_reference_origin_episode(conn, scene_reference_id: str) -> int | None:
    """"来源集号"（第30轮②）：直接复用 scene_references.ep_start——这个
    场景参考在注册表里生效的起始集号，是现成数据，不另外发明新的追踪
    字段（alias_inherited 绑定的合法性来源于"这个场景本来就已经在注册表
    里"，ep_start 正是这件事本身的记录）。"""
    row = conn.execute(
        "SELECT ep_start FROM scene_references WHERE id=?", (scene_reference_id,),
    ).fetchone()
    if not row or row["ep_start"] is None:
        return None
    return int(row["ep_start"])


# 场景别名锚定（1.5.1，真实第18轮审计 A2 主病灶，47 条）：场景规范名（如
# "杂役处居所内"）往往是发现时铸造的标签，天然不在原文——本集若换了个
# 说法提这个场景（"杂役们住的地方"），_resolve_scene_reference_id 的裸精确
# 匹配（只查 scene_references.scene_name）找不到它，哪怕这个说法早就被
# app.scenes._append_scene_alias 登记成了该场景的别名
# （Bible.scenes[].aliases）也一样——写入和读取完全脱节：别名库在长，但
# 场景解析从来不读它，同一个说法每次都要重新走一遍发现（多余的模型调用，
# 也多一次误判机会）。
def _prep_pack_resolve_scene_reference_with_alias(
    conn, project_id: str, episode_no: int, resolved_name: str, bible: Bible,
) -> tuple[str | None, str]:
    """裸精确匹配优先；失败后复用 app.validators.match_scene_name（跟
    app.scenes 的发现路径同一套判定，含别名，allow_fuzzy=False 避免模糊
    误配）把 resolved_name 归一到已登记的规范场景名，再用规范名查表。
    返回 (scene_reference_id, canonical_name)：canonical_name 供调用方判断
    是否需要把这次的原文措辞记为新别名（不同才需要，见
    _prep_pack_register_scene_alias_if_new）。
    """
    scene_reference_id = _resolve_scene_reference_id(
        conn, project_id, resolved_name, episode_no,
    )
    if scene_reference_id:
        return scene_reference_id, resolved_name
    canonical = match_scene_name(resolved_name, bible.scenes, allow_fuzzy=False)
    if not canonical or canonical == resolved_name:
        return None, resolved_name
    scene_reference_id = _resolve_scene_reference_id(
        conn, project_id, canonical, episode_no,
    )
    return scene_reference_id, canonical


def _prep_pack_register_scene_alias_if_new(
    conn, project_id: str, *, canonical_name: str, wording: str,
) -> bool:
    """把本集实际用到的原文措辞记为该场景的新别名（幂等，见
    app.scenes._append_scene_alias：已登记过直接返回 False，不重复写）。
    别名库随集数增长越来越全，是通用设计，不认识任何具体场景/词形。
    """
    if not wording or wording == canonical_name:
        return False
    from app.scenes import _append_scene_alias

    return _append_scene_alias(conn, project_id, canonical_name, wording)


def _prep_pack_first_evidence_segment(segments: list[SourceSegment], text: str) -> int | None:
    """观测用：`text` 第一次逐字出现在哪个 1-based segment_index，供别名
    锚定来源段号记录（找不到就是 None，不阻断任何流程，纯观测）。"""
    if not text:
        return None
    for index, segment in enumerate(segments, start=1):
        if text in segment.text:
            return index
    return None


# manifest 绑定来源证明（provenance，1.6.0，第25轮收口指令：审计剩余83条
# 定性为"合成标签合法但不可审计"——1.5.x 各轮陆续放宽了字面锚定要求
# （task②：经消歧/发现解析的绑定不再要求 mention 本身逐字出现），但放宽后
# 判断"这次绑定为什么合法"的依据只留在 Evaluation.evidence 里（true_name_
# hints/scene_alias_anchors/absorbed_speakers_count……），不是 payload 的
# 一等公民，审计只能翻 Evaluation 观测，无法直接从 asset_manifest 本身复核
# 每一条绑定的证据链。这个函数是所有 provenance 计算共用的确定性锚点查找：
# 按优先级尝试一组候选逐字短语，返回第一个真的出现在本集原文里的
# (anchor_segments, anchor_phrase)。全部候选都不出现时返回 ([], "")——不是
# 所有合法绑定都必然有本集内的逐字锚点（比如 suspected_true_name 经前瞻
# 窗口核验通过，证据在下一集原文里，不在本集）；空 anchor_phrase 在自校验
# 里视为"这条绑定没有可本地核验的锚点"，直接跳过验证，不阻断——已经比
# 1.5.x 之前"完全没有这个字段"更诚实，不需要为了填满字段而编造一个假锚点。
def _prep_pack_local_text_anchor(
    segments: list[SourceSegment], candidates: list[str],
) -> tuple[list[int], str]:
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        segment_index = _prep_pack_first_evidence_segment(segments, candidate)
        if segment_index is not None:
            return [segment_index], candidate
    return [], ""


# 跨集别名场景绑定的锚点强化（第30轮②，真实 scripts/episode_source_
# audit.py 复核实测：19 条 A2_scene_no_text_evidence，全部 provenance.
# method="alias"、aliases=0 个、display_name 不逐字出现在本集原文——该
# 审计脚本对 alias/direct 两个 method 走的是 TEXT_VERIFIED 标准（只查
# display_name/aliases 字符串是否逐字出现，不看 anchor_phrase，见该脚本
# TEXT_VERIFIED_METHODS 常量上方注释），跟 resolution/discovery/
# absorbed_speaker 的 ANCHOR_VERIFIED 标准（查 anchor_segments/
# anchor_phrase）是两套不同规则。旧实现只试 [name]（这次绑定用到的原始
# 称谓本身）——这个候选之所以必然命中，只是因为它就是"别名注册表"这个
# 称谓本身，命中的其实只是"这个称谓确实是这么写的"这件同义反复的事实
# （对 TEXT_VERIFIED 毫无帮助：display_name 是规范名，不是这个别名字符
# 串，name 命中不了 TEXT_VERIFIED 检查的是 display_name/aliases，scene
# 目前没有 aliases 字段），没有独立证明本集里还有别的什么依据把它跟这个
# 场景绑在一起。实测（真实 EP1-8 19 条）canonical_scene_name 无一逐字
# 命中本集原文，但该场景所涉事件的 source_evidence 地点描述短语 19/19
# 命中——这才是真正独立、有信息量的证据。改法：候选序列改成
# [canonical_scene_name, *scene_event_evidence_quotes]（沿用第28轮①给
# discovery/resolution 两支的同一批候选来源，故意不包含 name 本身这个
# 同义反复候选）；命中 → 这是比"alias"更强的证据形状，跟 resolution 走
# 同一套锚点核验标准，method 直接升级为 "resolution"（ANCHOR_VERIFIED，
# 自校验/外部审计都认这份 anchor_phrase）；三候选（不含 name）全部落空
# → 绝不伪造锚点，也不再谎称"alias"（同义反复的空壳），改标
# method="alias_inherited"，用现成的 scene_references.ep_start 记这次
# 绑定最初在注册表里生效的集号（source_episode_no，跟审计脚本
# _verify_alias_inherited_scene 期望的字段名/类型完全对齐——那段递归核验
# 逻辑早于本次改动已经写好，是这次改动要对齐的既有契约，不是本次新造的
# 字段名），供审计走对应的递归核验分支（不在
# _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR 里，空锚合法豁免，跟
# resolution_forward 同待遇）。
#
# 2.0.2 更新（见 PREP_PACK_VERSION 上方 2.0.2 大注释）：2.0.0 砍
# event_chain 后，调用方一度把 scene_event_evidence_quotes 传空列表
# （event_chain 没了，暂时没有替代来源）——这不是这份函数签名/判据本身
# 的改动，函数体一行未动，仍然是"给一份候选引文列表，命中就升级
# resolution，不命中就诚实降级 alias_inherited"这同一套判据；变化只在
# 调用方现在恢复传入这条场景提及自己申报的 quote（_ModelSceneMention.
# quote，isomorphic 于旧 event_chain[].source_evidence[].quote，只是
# 粒度从"事件"下沉到"提及"，见调用点上方注释）。
def _prep_pack_scene_alias_provenance(
    conn, segments: list[SourceSegment], scene_reference_id: str,
    canonical_scene_name: str, scene_event_evidence_quotes: list[str],
) -> tuple[str, list[int], str, int | None]:
    """Returns ``(method, anchor_segments, anchor_phrase, source_episode_no)``
    for a scene mention resolved via the cross-episode alias registry."""
    anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
        segments, [canonical_scene_name, *scene_event_evidence_quotes],
    )
    if anchor_segments:
        return "resolution", anchor_segments, anchor_phrase, None
    source_episode_no = _prep_pack_scene_reference_origin_episode(conn, scene_reference_id)
    return "alias_inherited", [], "", source_episode_no


def _prep_pack_provenance(
    method: str, anchor_segments: list[int], anchor_phrase: str,
    *, forward_chapter_label: str = "", source_episode_no: int | None = None,
    dual_anchor: bool | None = None,
    candidate_verdict_attempted: bool | None = None,
) -> dict[str, Any]:
    """统一构造 provenance 结构，避免多处调用各自拼一份字面量字典漂移。
    forward_chapter_label（1.6.0 第28轮）只在 method="resolution_forward"
    时非空——见 _prep_pack_verify_manifest_provenance 上方关于
    resolution_forward 空锚豁免的完整说明。source_episode_no（第30轮②）
    只在 method="alias_inherited" 时非 None——字段名/类型（int，不是格式化
    字符串）对齐 scripts/episode_source_audit.py 的
    _verify_alias_inherited_scene 既有契约（该脚本先于本次改动就已经按
    这个字段名写好了递归核验：来源集号须严格早于当前集、来源集需有已发布
    pack 且同 scene_reference_id 的绑定同名、那条来源绑定自身还要递归核验
    通过），不是本次新造的字段名，也故意不跟 forward_chapter_label 复用
    同一个字段——两者是不同的编号域（前瞻窗口指向"章"，跨集别名继承指向
    "集"），混装单位会重蹈第28轮排查过的"同一数据两个真源"覆辙。
    dual_anchor（1.10.0，缺陷 A 修复）只在 suspected_true_name 核验通过
    时非 None——True 表示钉证命中的是同时含 alias 与 true_name 的双锚定
    条目，False 表示全卷宗结构上不存在双锚定证据、退化为仅含 alias 的
    集内指代条目（见 _prep_pack_verify_true_name_hypothesis docstring）。
    显式记录这个布尔值是可观测降级的落地点——本项目明令禁止静默降级，不能
    只在 provider_calls 里才看得出这次绑定走的是退化路径。
    candidate_verdict_attempted（1.10.0，缺陷 A 顺带修复的可观测性缺口）
    只在 method="discovery" 时非 None——区分这批 functional_extras 是
    「从未获得候选判别机会」（候选集为空/卷宗为空，True 之前从未发起过
    候选判别模型调用）还是「候选判别跑过但没选中」（发起过调用，模型选了
    "都不是/无法确定"或钉证未通过），此前两者坍缩成同一个 method 值，只能
    翻 provider_calls 反推。三者都是纯附加字段，其它 method/情形不带这些
    key，不影响既有消费者（payload 冻结纪律照旧）。
    label_literal（1.11.0/1.11.1，任务①）已在 2.0.0 撤下：不是因为它变得
    结构性恒真（合成描述性标签仍然合法、仍然常见非逐字，见
    _prep_pack_gate_segment_indexes 上方说明——那道结构闸刻意不做逐字
    核验，避免在候选判别机会到来之前就堵死它），是纯粹的范围收窄——映射台
    2.0.0 只对"绑定到谁"负责，"这个称谓好不好看/是不是逐字"这类纯观测性
    标记不再是这个模块的职责，见 PREP_PACK_VERSION 上方 2.0.0 大注释。"""
    provenance = {
        "method": method,
        "anchor_segments": list(anchor_segments),
        "anchor_phrase": anchor_phrase,
    }
    if forward_chapter_label:
        provenance["forward_chapter_label"] = forward_chapter_label
    if source_episode_no is not None:
        provenance["source_episode_no"] = source_episode_no
    if dual_anchor is not None:
        provenance["dual_anchor"] = dual_anchor
    if candidate_verdict_attempted is not None:
        provenance["candidate_verdict_attempted"] = candidate_verdict_attempted
    return provenance


# 场景侧 resolution/discovery 两个 method 不允许空锚（第28轮 ERR-20260824，
# v3 审计 A2_scene_no_text_evidence 25 条）：消歧/发现判定"这是哪个场景"
# 凭的必然是本集文本依据，不存在"合法但无锚"的场景绑定——跟角色侧
# resolution_forward（证据在前瞻窗口，本集内没有锚点是正确的）不是同一件
# 事。direct method 已经被 _prep_pack_resolve_scene_reference_with_alias
# 上游的证据闸挡住（见该函数与其调用点上方注释），结构上不可能出现空锚，
# 不需要在这里重复要求——第30轮②：原来的 alias 分支不再产出裸的
# method="alias" 挂空锚（见 _prep_pack_scene_alias_provenance 上方完整
# 说明）：真找到独立证据就升级成 resolution（走这条锚点必填规则），真没有
# 就诚实改标 alias_inherited，跟 resolution_forward 同样豁免、同样在这里
# 不需要登记。
_PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR = frozenset({"resolution", "discovery"})


def _prep_pack_verify_manifest_provenance(
    segments: list[SourceSegment], asset_manifest: dict[str, Any],
    source_text: str = "",
) -> list[str]:
    """发布前自校验（1.6.0）：每一条非空 anchor_phrase 必须真的逐字出现在
    它自己 anchor_segments 指向的原文段里（至少一段命中即可，不要求每段都
    命中——一条绑定可能引用多个证据段，只要其中一个真的载有这句 anchor_
    phrase 就算锚定成立）。anchor_phrase 为空默认视为"这条绑定没有本集
    本地锚点"，跳过逐字校验（见 _prep_pack_local_text_anchor 的完整说明）
    ——但这条豁免不是无条件的：场景侧的 resolution/discovery 两个 method
    真实回归证明"空锚"从来不是合法状态，而是 1.6.0 最初实现里
    _prep_pack_local_text_anchor 候选序列覆盖不全（只试了触发发现/消歧的
    原始 label，没试模型申报的规范名、也没试该场景所涉事件自己的证据
    原文）导致的假阴性——真锚点本来就在，只是没找全；这两个 method 现在
    强制要求非空锚，空锚直接判定自校验失败（见
    _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR）。resolution_forward（角色
    与场景共用同一语义）的空 anchor_segments（本地段号）是合法的：
    suspected_true_name 核验通过、证据在前瞻窗口而非本集，本集内找不到
    本地锚点是正确结论，不是缺陷——但 forward_chapter_label（指向哪一章）
    和 anchor_phrase（那一章里被钉住的支撑句）两个字段第30轮起强制同时
    非空："半张证书等于没证书"：真实 EP2/6/8 回归过 anchor_phrase 被误写
    成空字符串（钉住的支撑句明明存在，却没记下来）、EP2/6/8 角色侧还
    额外查出 forward_chapter_label 本身也会在特定路径下丢失（见
    _prep_pack_verify_true_name_hypothesis 调用点上方 via_suspected_
    true_name 标志位的完整说明）——两种半张证书现在都在这里被拦截，不再
    悄悄发布。

    段号越界（不在 1..len(segments) 内）本身就是判定失败，不静默忽略。
    段号取值域（第28轮排查记录，E 类重复真源变体）：本函数与
    _prep_pack_local_text_anchor/_pass 全程共用同一个 segments 闭包变量
    （index_source_segments(source_text) 的全局 1-based 编号），跟
    coverage_ledger/source_span 同域；_chunk_segments 分块时用
    list(enumerate(segments, start=1)) 保留原始全局下标分组、不做分块内
    重新编号，_render_chunk 展示给模型的编号、event 的 source_span/
    source_evidence[].segment_index 因此也都是全局域——沿链路排查未发现
    第二套局部编号并存（若未来某处引入分块内局部重新编号，必须只保留
    全局域一份，禁止两个编号域并存后再各自"验一遍"，那正是同一数据的
    两个真源互相打架、两边各自"验过"却结论相反的形状）。见
    tests/test_prep_pack_asset_discovery.py 里显式构造跨 chunk 场景的
    自校验红灯，作为这条不变量的回归防线。

    ``source_text``（2.0.0 起不再驱动任何检查，仅保留形参兼容既有调用点/
    测试签名——1.11.0/1.11.1 引入的 label_literal 自校验已随该字段一起撤下
    （纯范围收窄，不是失败类别被结构性堵死，见 PREP_PACK_VERSION 上方
    2.0.0 大注释与 _prep_pack_provenance 的 docstring）。"""
    errors: list[str] = []
    total_segments = len(segments)

    def _check(
        kind: str, label: str, provenance: Any, *, require_anchor: bool = False,
    ) -> None:
        if not isinstance(provenance, dict):
            return
        method = str(provenance.get("method") or "")
        phrase = str(provenance.get("anchor_phrase") or "").strip()
        # resolution_forward（第30轮，用户点名"半张证书等于没证书"）：证据
        # 在前瞻/别处章节而非本集，anchor_segments 本地段号合法留空——但
        # forward_chapter_label（指向哪一章）和 anchor_phrase（那一章里的
        # 哪句话）两个字段必须同时非空，才是一条完整、可被
        # scripts/episode_source_audit.py 的 _verify_provenance_forward_
        # anchor 独立复核的证明；任一为空都是半张证书，一律拦截，不再往下
        # 走本地 anchor_segments 逐字校验（那套校验假设短语就在本集里，对
        # resolution_forward 从语义上就不适用）。
        if method == "resolution_forward":
            forward_chapter_label = str(
                provenance.get("forward_chapter_label") or ""
            ).strip()
            if not forward_chapter_label or not phrase:
                errors.append(
                    f"{kind}「{label}」的 provenance.method=resolution_forward "
                    f"缺少 forward_chapter_label（{forward_chapter_label!r}）或 "
                    f"anchor_phrase（{phrase!r}）——前瞻绑定必须同时携带"
                    "章节标注与被钉住的支撑句，半张证书等于没证书，来源"
                    "证明自校验失败，门禁具名拦截"
                )
            return
        if not phrase:
            if require_anchor:
                errors.append(
                    f"{kind}「{label}」的 provenance.method="
                    f"{provenance.get('method')!r} 缺少 anchor_phrase——"
                    "resolution/discovery 绑定必须有本集文本依据，来源"
                    "证明自校验失败，门禁具名拦截"
                )
            return
        raw_segments = provenance.get("anchor_segments") or []
        segment_indexes = [
            int(value) for value in raw_segments
            if isinstance(value, (int, float)) or (
                isinstance(value, str) and value.strip().lstrip("-").isdigit()
            )
        ]
        in_range = [
            index for index in segment_indexes if 1 <= index <= total_segments
        ]
        if not in_range or not any(
            phrase in segments[index - 1].text for index in in_range
        ):
            errors.append(
                f"{kind}「{label}」的 provenance.anchor_phrase「{phrase}」未在"
                f"anchor_segments={segment_indexes} 所指原文中逐字命中，来源"
                "证明自校验失败，门禁具名拦截"
            )

    for character in asset_manifest.get("characters") or []:
        _check("角色", str(character.get("display_name") or ""), character.get("provenance"))
    for scene in asset_manifest.get("scenes") or []:
        provenance = scene.get("provenance")
        require_anchor = (
            isinstance(provenance, dict)
            and str(provenance.get("method") or "")
            in _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR
        )
        _check(
            "场景", str(scene.get("display_name") or ""), provenance,
            require_anchor=require_anchor,
        )
    for extra in asset_manifest.get("functional_extras") or []:
        _check("群演", str(extra.get("label") or ""), extra.get("provenance"))
    # 2.0.0 新增：props 也走同一条 anchor_phrase 自校验（见
    # _prep_pack_build_prop_manifest 的 provenance 构造，method 恒
    # "direct"，anchor_segments/anchor_phrase 来自它自己已经逐段字面核验
    # 过的 segment_indexes/label——道具没有解析路径豁免，见该函数说明）。
    for prop in asset_manifest.get("props") or []:
        _check("道具", str(prop.get("label") or ""), prop.get("provenance"))
    return errors


# 称谓/场景名证据闸（1.4.2，real round-16 EP5 regression fix）. Real EP5 output
# resolved a completely unrelated pair of mountain-top old men -- the raw
# text only ever calls them "两个老者"/穿灰袍的高大老者", never a proper
# name -- to a pre-existing character ("丹鬼") and scene ("大青山山顶") from
# elsewhere in the story, purely because the event-chain extraction model
# happened to write those exact already-registered names (both are 0
# occurrences in chapter 5's own text; verified directly against the real
# chapters row). Root cause: neither _resolve_portrait_id nor
# _resolve_scene_reference_id require any evidence beyond "a DB row with
# this exact name exists somewhere, for any episode" -- a bare name-string
# coincidence was silently trusted as a real identification. Traced two
# independent binds:
#   - character "丹鬼": the chunk-extraction model wrote "丹鬼" directly as
#     characters[].display_name (NOT "灰袍老者"/"山顶老者" -- those only ever
#     appeared as key_lines[].speaker, a field this module never resolves
#     through) -- a bare direct hit, not a legitimate forward-looking
#     identity resolution (which is why aliases ended up empty: no rename
#     ever happened, so the existing "aliases.append(name) when name !=
#     resolved_name" logic never had anything to record).
#   - scene "大青山山顶": same shape -- scenes[].display_name was written as
#     "大青山山顶" directly, an existing scene_reference from unrelated
#     earlier context, despite the text explicitly saying "靠山宗四周的山峰"
#     / "外宗旁的山峰".
def _prep_pack_mention_has_text_evidence(name: str, source_text: str) -> bool:
    """Does ``name`` -- the raw mention/称谓 text an event actually carries,
    exactly as the event-chain extraction model wrote it -- appear verbatim
    anywhere in this episode's own ``source_text``? A plain substring check
    is deliberately sufficient here (unlike align_source_excerpt's fuzzy
    quote-matching, which exists for full-sentence quotes): character/scene
    names are short proper nouns, not sentences, so an exact substring
    either is or isn't real textual grounding for "this term was actually
    used to refer to something in this chapter."
    """
    return bool(name) and name in (source_text or "")


# 跨集别名一致性（1.5.2/task②，真实第18轮审计 B 类缺陷）：proj_3ac0b627fa46
# 项目内"小胖子"同时被登记为李富贵和王有材的别名——溯源确认 EP3 那次绑定
# 完全没有本集文本依据（chapters 表 EP3 原文直查："王有材"逐字出现 0 次，
# "小胖子"高频出现且从上下文看自始至终是同一个人）；EP2/EP6 两集独立正确
# 绑到了李富贵。EP3 反复重新生成横跨第15-18轮，累计 80+ 次 identity.current
# 与 144+ 次涉及"小胖子"的 identity.future 调用，未能定位到单一一次把
# "小胖子"分组直接判给"王有材"的调用记录——大概率是 app.portraits.
# append_candidate 的"全批唯一字面锚点自动改绑"机制或跨 chunk 的
# functional_identity_key 合并在某次重试中产生，具体哪一次已经无法在保留的
# provider_calls 历史里精确复现（EP3 之后的多轮重新生成覆盖了当时的中间
# 状态）。核心事实清楚：这次改绑在其发生的那一集里没有任何逐字证据支撑，
# 正是"真名核验佐证不足"的形状。
#
# 1.7.0（层一，docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1/§6 第3项）：
# 主读源切换为 Bible.characters[].aliases（app.schemas.CharacterAlias，全书
# 分析阶段模型申报+代码核验后落库，见 app.stages.generate_bible）。旧的
# "扫描项目内其它已发布分集 asset_manifest" 路径不是本次要修的 bug 现场，
# 而是 bug 本身的根因——第 1 集永远是"其它已发布分集"最空的一集，未绑定
# 角色落 functional_extras 从不写别名，形成死循环（详见设计文档 §2.3，
# 真实案例：许清 EP1/EP5/EP6 三集三种措辞、无一绑定，EP13 才第一次绑上）。
# 人物谱在全书分析阶段就已经知道这些别名，不需要等任何一集先发布。
# P2 §16 决定：旧扫描路径保留一段时间做双重校验再退役，但只在人物谱对这个
# 别名字符串毫无记录时才补充生效——绝不允许旧路径的结论推翻人物谱已经给出
# 的结论（人物谱是唯一被要求携带逐字证据锚点的数据源，可信度更高）。
def _prep_pack_bible_alias_owner(bible: Bible | None, alias: str) -> str | None:
    """人物谱里第一个（``bible.characters`` 列表顺序）在 ``aliases`` 登记了
    这个别名字符串的角色名；没有任何角色登记则返回 ``None``。只回答"有没有
    人认领"，不判断唯一性——唯一性/冲突判定是
    ``_prep_pack_bible_alias_conflicting_owner`` 的职责，两者分工跟历史的
    "读侧函数"/"冲突检查函数"两分是同构的，不合并成一个函数。"""
    if not alias or bible is None:
        return None
    for character in bible.characters:
        if any(a.text == alias for a in (character.aliases or [])):
            return character.name
    return None


def _prep_pack_bible_alias_conflicting_owner(
    bible: Bible | None, alias: str, canonical_name: str,
) -> str | None:
    """人物谱里除 ``canonical_name`` 本人之外，是否还有别的角色也在
    ``aliases`` 里登记了同一个别名字符串？命中即返回那个冲突角色名。"""
    if not alias or bible is None:
        return None
    for character in bible.characters:
        if character.name == canonical_name:
            continue
        if any(a.text == alias for a in (character.aliases or [])):
            return character.name
    return None


def _prep_pack_cross_episode_alias_conflict(
    conn, project_id: str, episode_id: str, *, alias: str, canonical_name: str,
    bible: Bible | None = None,
) -> str | None:
    """这同一个 alias 字符串是否已经被记在了一个不同的 canonical_name 名下？
    命中就返回那个冲突的 canonical_name（供调用方拒绝这次改绑、留痕），没有
    冲突返回 None。主读源是人物谱（``bible.characters[].aliases``，见本函数
    上方 1.7.0 说明）；只有人物谱对这个别名毫无记录时，才补充查旧路径——
    项目内其它已发布分集的 asset_manifest（P2 §16 双重校验期，未退役）。
    """
    if not alias or not canonical_name:
        return None
    bible_conflict = _prep_pack_bible_alias_conflicting_owner(
        bible, alias, canonical_name,
    )
    if bible_conflict:
        return bible_conflict
    if bible is not None and _prep_pack_bible_alias_owner(bible, alias) == canonical_name:
        # 人物谱明确认领这个别名归属 canonical_name 本人、且没有第二个认领
        # 者——这是主源给出的明确"无冲突"结论，旧路径的信号不得推翻它。
        return None
    return _prep_pack_cross_episode_alias_conflict_legacy_scan(
        conn, project_id, episode_id, alias=alias, canonical_name=canonical_name,
    )


def _prep_pack_cross_episode_alias_conflict_legacy_scan(
    conn, project_id: str, episode_id: str, *, alias: str, canonical_name: str,
) -> str | None:
    """旧路径（P2 §16，双重校验期保留，仅在人物谱对该别名毫无记录时补充
    生效，见 ``_prep_pack_cross_episode_alias_conflict`` 调用点）：项目内其它
    已发布分集是否已经把这同一个 alias 字符串记在了一个不同的 canonical_name
    名下？纯粹按"同一别名字符串在项目内是否已经指向不同的人"这个结构性事实
    判断，不需要认识 alias 具体是什么词。"""
    if not alias or not canonical_name:
        return None
    rows = conn.execute(
        "SELECT screenplay_json FROM episodes WHERE project_id=? AND id!=? "
        "AND screenplay_json IS NOT NULL",
        (project_id, episode_id),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["screenplay_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        manifest = payload.get("asset_manifest") or {}
        for character in manifest.get("characters") or []:
            if alias not in (character.get("aliases") or []):
                continue
            other_name = str(character.get("display_name") or "").strip()
            if other_name and other_name != canonical_name:
                return other_name
    return None


# 角色别名注册表读侧（1.5.x task①，真实第24轮 EP3 回归 ERR-20260824-d0830a）：
# 跟场景轴 1.5.1（_prep_pack_resolve_scene_reference_with_alias）完全对称的
# 缺陷——场景侧写别名会被后续读，角色侧却只写不读。1.7.0 起主读源改为人物谱
# （见 _prep_pack_cross_episode_alias_conflict 上方 1.7.0 说明，同一次切换、
# 同一套双重校验纪律）：EP2 一次消歧确立"小胖子"→李富贵后，不必等它作为
# "已发布分集"被扫描到——只要人物谱里已经登记了这条别名（全书分析阶段申报，
# 不依赖任何一集先发布），EP1 起就能直接复用，不必每集重新赌一次消歧模型
# 调用。只返回第一个命中的候选 canonical_name——是否唯一由调用方另外过一遍
# _prep_pack_cross_episode_alias_conflict 确认（复用同一套冲突拒绝逻辑守多
# 目标，不在这里重复实现一份等价的唯一性判断）。
def _prep_pack_lookup_character_alias_canonical_name(
    conn, project_id: str, episode_id: str, name: str,
    bible: Bible | None = None,
) -> str | None:
    """是否已有数据源把 ``name`` 登记为某个角色的别名？命中返回该
    canonical_name，没有命中返回 None。主读源是人物谱，见本节顶部 1.7.0
    说明；人物谱毫无记录时才补充查旧路径（项目内其它已发布分集）。"""
    if not name:
        return None
    bible_owner = _prep_pack_bible_alias_owner(bible, name)
    if bible_owner:
        return bible_owner
    return _prep_pack_lookup_character_alias_canonical_name_legacy_scan(
        conn, project_id, episode_id, name,
    )


def _prep_pack_lookup_character_alias_canonical_name_legacy_scan(
    conn, project_id: str, episode_id: str, name: str,
) -> str | None:
    """旧路径（P2 §16，双重校验期保留，仅在人物谱对该别名毫无记录时补充
    生效）：项目内是否已有其它已发布分集把 ``name`` 登记为某个角色的别名？
    命中返回该 canonical_name，没有命中返回 None。"""
    if not name:
        return None
    rows = conn.execute(
        "SELECT screenplay_json FROM episodes WHERE project_id=? AND id!=? "
        "AND screenplay_json IS NOT NULL",
        (project_id, episode_id),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["screenplay_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        manifest = payload.get("asset_manifest") or {}
        for character in manifest.get("characters") or []:
            if name in (character.get("aliases") or []):
                canonical = str(character.get("display_name") or "").strip()
                if canonical:
                    return canonical
    return None


# 先验知识申报通道（1.5.0，用户修正令：outright 禁止会扔掉真正猜对的真名，
# "丹鬼"这类猜对了本该是加分项）。模型可能在训练语料里读过这部小说，与其
# 假装它不知道（禁止），不如让它把这份先验知识当一个可核验的候选申报出来
# （_ModelCharacterMention/_ModelSceneMention.suspected_true_name），申报本身
# 从不被直接采信——必须先通过下面这道确定性核验，核验不过就丢弃，回退到
# display_name 本身的常规解析路线（消歧/群演/发现），不静默相信任何猜测。
#
# 身份绑定审判程序（真实回归：用户抓到 EP2/EP3 把"小胖子"误绑成"王有材"，
# 又抓到 EP6"上官修身边的男子"被绑成上官修本人——旧版核验只检查候选真名
# 这个字符串在前瞻窗口里出现过，"名字存在"被当成了"身份链接"的充分条件。
# 中途曾尝试过两版结构规则："列举反证"、"包含关系方向性规则"，均被用户
# 否决——那些规则本质是穿着语法外衣的黑白名单，靠人工穷举的分隔符/方位词
# 判断语义，覆盖不了语言的全部表达方式。最终架构改为完全不猜测语义规则，
# 把"是否同一人"这个语义判断彻底交给模型，代码只负责三件事：把全部相关
# 原文老老实实检索出来、把模型的结论钉在真实存在的原文引句上、以及记账）：
#   1) 卷宗检索（代码，零语义，_prep_pack_true_name_dossier）：检索项目
#      全书（chapters 全表，不只是本集或某个前瞻窗口）里所有含 alias 的
#      自然段 ∪ 所有含 suspected_true_name 的自然段，段落原文 + 章节号
#      组成"卷宗"。反证证据（比如"小胖子、王有材"这种并列举出两个不同人
#      的段落）因为同时含两个词，天然会被检索到卷宗里，不需要另外写一条
#      "反证"规则去猜它长什么样。卷宗超过字符预算时用
#      _prep_pack_sample_dossier_entries_within_budget 做确定性（非随机）
#      采样：同时含两词的段落全部保留，只含一个词的段落按下标等距抽样。
#   2) 裁决（模型，唯一一次调用，_prep_pack_true_name_verdict，1.10.0 改为
#      候选判别，不再是同一人是非题——见 PREP_PACK_VERSION 上方 1.10.0 大
#      注释的完整根因与数据）：给模型卷宗原文 + 一份候选真名/候选场景名单
#      （suspected_true_name 本身 ∪ 人物谱/场景谱里在卷宗文本中有字面命中
#      的其它候选）+ 显式"都不是/无法确定"出口，问"称谓 alias 最可能指候选
#      中的哪一个"，不是"是不是 Y"——避免旧版是非题诱发确认偏误。
#   3) 钉证（代码，_prep_pack_true_name_pin_dossier_entry，1.10.0 改为段号
#      钉证）：模型只需引用卷宗目录里的候选编号（entry_index），不比对
#      模型转录的逐字引句（真实生产数据证明旧版逐字比对会被模型的跨段
#      拼接/摘要噪音误杀，见 PREP_PACK_VERSION 上方大注释）；钉中的卷宗
#      条目还必须逐字包含 alias 本身（待判标签，此前零要求，是"钉证在近半
#      数真实 same 判决里形同虚设"的主因）；若全卷宗存在同时含 alias 与
#      true_name 的双锚定条目，钉中的条目必须就是双锚定条目之一，否则必须
#      钉在本集自己的（alias 逐字命中的）段落上——两种情形都不满足则拒绝。
#      selected_candidate 必须精确等于 suspected_true_name 且钉证通过，才
#      算核验通过；其它任何结果（选了别的候选/选了"都不是"/钉证失败）一律
#      不采信——默认安全侧，不确定就不绑，回退到 alias 自身的常规解析路线
#      （未被发现进一步归类时自然落为群演，见 _pass 里 unresolved_
#      characters 的处理，不需要这里单独再写一条"走群演"分支）。
#   4) 记账（代码）：核验通过的判决连同钉住的原文引句进 provenance
#      （anchor_phrase 就是这句被钉住的支撑句），alias 才会被写进
#      entry["aliases"]（见调用点），写入注册表的东西天然带着完整证据链；
#      读侧（_prep_pack_lookup_character_alias_canonical_name，task①）
#      逻辑不变——源头干净，继承出去的自然干净。同一 (subject_kind, alias,
#      true_name) 组合在同一次生成里只会真正发一次模型调用：_resolve_assets
#      级别的 true_name_verdict_cache 字典按 (subject_kind, alias, true_name)
#      缓存判决结果（subject_kind 隔离角色/场景两个共用同一字典的域），
#      重复出现的提及直接复用（"注册表即缓存"里"注册表"指的是跨集持久化
#      那一层，这里的进程内字典是同一次生成内的短期缓存，两者不冲突：
#      前者防跨集重复裁决，后者防同一集内同一对提及反复调用）。
# 跨集矛盾绑定（同一 alias 在不同已发布分集里被判给不同的 true_name）：
# 复用既有的 _prep_pack_cross_episode_alias_conflict（task②）继续按拒绝
# 处理——发现冲突就不接受这次改名，回退到 alias 自身的解析路线，冲突记入
# rejected_alias_conflicts（观测）。协调方设想的"合并双方卷宗重审一次，
# 仍矛盾则两边都降级为群演"是更精细的处理，本轮未实现（没有红灯明确要求
# 这一步，且现状——拒绝新的改名、绝不静默接受任何一边——已经是安全默认
# 值，不会把错误绑定放出去），留待后续有真实回归再做。
def _prep_pack_true_name_dossier(
    conn, project_id: str, alias: str, true_name: str,
) -> list[dict[str, Any]]:
    """1) 卷宗检索：零语义，纯字符串包含判断。扫描项目 chapters 全表
    （不限本集/前瞻窗口——EP2 的真实事故正是因为旧版只查了一个有限窗口，
    真正的反证/佐证段落可能在全书任何一章），按自然段
    （index_source_segments）逐段检查是否包含 alias 和/或 true_name。
    双词共现段全部保留；单词段超出预算时交给
    _prep_pack_sample_dossier_entries_within_budget 做确定性采样。每条记录
    额外带 ``entry_index``（1.10.0，缺陷 A 修复新增，见 PREP_PACK_VERSION
    上方大注释）：1-based、按本函数返回顺序（both 在前、single 采样结果在
    后）分配的扁平序号，供候选判别改用段号钉证（_prep_pack_true_name_pin_
    dossier_entry）——卷宗跨多章检索，(chapter_idx, segment_index) 二元组
    不是一个单值 enum 候选，需要一个扁平序号才能像 _prep_pack_functional_
    candidate_dossier 那样把钉证收紧成"选中的序号是否落在卷宗集合内"的结构
    判断。"""
    if not alias or not true_name:
        return []
    from app.portraits import CAST_DISCOVERY_SOURCE_BUDGET

    both: list[dict[str, Any]] = []
    single: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT idx, content FROM chapters WHERE project_id=? ORDER BY idx",
        (project_id,),
    ).fetchall()
    for row in rows:
        chapter_idx = int(row["idx"])
        content = str(row["content"] or "")
        if alias not in content and true_name not in content:
            continue
        for segment_index, segment in enumerate(
            index_source_segments(content), start=1,
        ):
            has_alias = alias in segment.text
            has_true_name = true_name in segment.text
            if not has_alias and not has_true_name:
                continue
            entry = {
                "chapter_idx": chapter_idx, "segment_index": segment_index,
                "text": segment.text,
            }
            (both if has_alias and has_true_name else single).append(entry)
    dossier = list(both)
    used_chars = sum(len(item["text"]) for item in dossier)
    remaining_budget = max(0, CAST_DISCOVERY_SOURCE_BUDGET - used_chars)
    dossier.extend(
        _prep_pack_sample_dossier_entries_within_budget(single, remaining_budget)
    )
    for entry_index, item in enumerate(dossier, start=1):
        item["entry_index"] = entry_index
    return dossier


def _prep_pack_sample_dossier_entries_within_budget(
    entries: list[dict[str, Any]], char_budget: int,
) -> list[dict[str, Any]]:
    """单词段的确定性（非随机）等距采样：预算充足时全收；不足时按下标
    等距抽取，让样本铺满全书范围而不是只取前几章——同一份输入，任何时候
    重跑都得到一模一样的卷宗，可复现、可审计，这也是不能用随机采样的
    原因（审判程序的证据卷宗必须是确定性的，不能这次抽到反证下次抽不到）。
    """
    if not entries or char_budget <= 0:
        return []
    total_chars = sum(len(item["text"]) for item in entries)
    if total_chars <= char_budget:
        return list(entries)
    average_chars = max(1.0, total_chars / len(entries))
    approx_count = max(1, int(char_budget / average_chars))
    step = max(1.0, len(entries) / approx_count)
    picked_indexes = sorted({
        min(len(entries) - 1, int(i * step)) for i in range(approx_count)
    })
    selected: list[dict[str, Any]] = []
    used = 0
    for index in picked_indexes:
        entry = entries[index]
        entry_chars = len(entry["text"])
        if used + entry_chars > char_budget:
            continue
        selected.append(entry)
        used += entry_chars
    return selected


# 候选判别响应（1.10.0，缺陷 A 修复，见 PREP_PACK_VERSION 上方大注释）：
# 替换掉旧版 same/different/uncertain 是非题——selected_candidate 是一道
# 候选选择题（候选集 = suspected_true_name 本身 ∪ 人物谱/场景谱里在卷宗
# 文本中有字面命中的其它候选 ∪ 显式"都不是/无法确定"），跟
# _PrepPackFunctionalCandidateVerdict 同一范式，两者独立定义，互不复用
# （问的语义/候选来源不同，见 _prep_pack_true_name_verdict_candidates 与
# _prep_pack_true_name_verdict 的说明）。supporting_entry_index 钉的是
# 卷宗目录里的候选编号（entry_index，见 _prep_pack_true_name_dossier），
# 不是逐字引句——真实生产数据证明逐字引句钉证在跨章场景下会被模型的
# 拼接/摘要噪音系统性误杀。supporting_quote 保留为可选观测字段，不参与
# 判定。
class _PrepPackTrueNameVerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_candidate: str
    supporting_entry_index: int
    supporting_quote: str = ""


_PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL = "都不是/无法确定"

# 裁决提示词按 subject_kind 分域的措辞表（独立评审 blocker：本函数被角色
# 分支 resolve_fn=_resolve_portrait_id 与场景分支 resolve_fn=
# _resolve_scene_reference_id 共用，旧版提示词硬编码"是否指同一个人"——
# 场景假设走到这里时模型被问"这两个是不是同一个人"，语义错误，裁决不可靠。
# noun_label 是候选的名词身份；same_subject 是任务句里"是否指同一 X"的
# X，结构（卷宗引用、显式拒绝出口）跟 1.10.0 改动前完全一致，只换名词。）
_TRUE_NAME_VERDICT_SUBJECT_COPY: dict[str, dict[str, str]] = {
    "character": {"noun_label": "人名", "same_subject": "同一个人"},
    "scene": {"noun_label": "地点名", "same_subject": "同一个场景或地点"},
}


def _prep_pack_true_name_verdict_roster(
    bible: Bible, subject_kind: Literal["character", "scene"],
) -> dict[str, list[str]]:
    """候选面快照（1.10.0，缺陷 A 修复）：规范名 -> [规范名, 已确认别名...]，
    按 subject_kind 分流。character 分支直接复用 _prep_pack_functional_
    candidate_roster（人物谱同一构造，避免重复实现）；scene 分支同构，读
    bible.scenes[].aliases（纯字符串列表，跟 Character.aliases 的
    CharacterAlias 对象列表结构不同，见 app.schemas.Scene 字段说明）。"""
    if subject_kind == "character":
        return _prep_pack_functional_candidate_roster(bible)
    return {scene.name: [scene.name, *scene.aliases] for scene in bible.scenes}


def _prep_pack_true_name_verdict_candidates(
    dossier: list[dict[str, Any]], roster: dict[str, list[str]], true_name: str,
) -> list[str]:
    """候选判别候选集（1.10.0，缺陷 A 修复）：确定性、零语义——人物谱/
    场景谱（按 subject_kind 对应的 roster）里，规范名或已确认别名在卷宗
    （已经检索出的真实原文段落，覆盖全书范围）文本里逐字命中的候选。这样
    候选永远有真实卷宗材料支撑，不会出现"选项本身卷宗里毫无证据"的名存
    实亡选择题。``true_name``（即 suspected_true_name，被验证的假设）永远
    强制在候选集内——dossier 检索本身就是按"含 alias 和/或 true_name"筛选
    出来的，卷宗非空时通常已经命中，这里防御性再保证一次，候选判别不能连
    被测假设本身都问不出来。不针对任何具体人名/场景名做特判——candidates
    完全来自卷宗文本与人物谱/场景谱两份结构化数据的逐字包含判断，跟
    _prep_pack_functional_candidate_names 同一纪律。"""
    dossier_text = "".join(item["text"] for item in dossier)
    candidates = [
        name for name, forms in roster.items()
        if any(form and form in dossier_text for form in forms)
    ]
    if true_name not in candidates:
        candidates.insert(0, true_name)
    return candidates


async def _prep_pack_true_name_verdict(
    *, run_id: str | None, episode_id: str, project_id: str | None,
    subject_kind: Literal["character", "scene"],
    alias: str, true_name: str, dossier: list[dict[str, Any]],
    candidates: list[str],
) -> _PrepPackTrueNameVerdictResponse:
    """2) 裁决：唯一一次模型调用，只给卷宗原文 + 候选名单，不携带任何
    "我怀疑 X 就是 Y"的推理引导——问"称谓 alias 最可能指候选中的哪一位/
    哪一处"，候选集之外强制一个"都不是/无法确定"选项（1.10.0，缺陷 A
    修复，见 PREP_PACK_VERSION 上方大注释：旧版 same/different/uncertain
    是非题诱发确认偏误，本项目已有四次真实误绑事故）。``subject_kind`` 只
    决定问的是"同一个人"还是"同一个场景或地点"这一个名词，卷宗引用/
    显式拒绝出口等结构完全不变（见 _TRUE_NAME_VERDICT_SUBJECT_COPY
    上方注释）。"""
    copy = _TRUE_NAME_VERDICT_SUBJECT_COPY[subject_kind]
    noun_label = copy["noun_label"]
    same_subject = copy["same_subject"]
    catalog = "\n\n".join(
        f"[候选{item['entry_index']}][第{item['chapter_idx']}章·段{item['segment_index']}] "
        f"{item['text']}"
        for item in dossier
    )
    entry_indexes = [item["entry_index"] for item in dossier]
    candidate_options = [*candidates, _PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    prompt = f"""下面是从原著全书范围内检索到的原文段落，与称谓"{alias}"或候选{noun_label}
有关（出现顺序不代表任何推断结论），每段前标了候选编号：
{catalog}

候选{noun_label}名单（判别范围仅限以下几项，不要引入名单之外的{same_subject}）：
{candidate_list}

任务：仅依据以上原文段落本身，判断称谓"{alias}"是否与候选名单中的某一位属于
{same_subject}，是的话具体是哪一位。
- selected_candidate 必须从候选名单中选一个精确的{noun_label}；原文不足以确定
  "{alias}"具体对应候选中的哪一个时，选"{_PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL}"，
  不要勉强给出确定结论；不要因为某个候选在段落里出现次数多、看起来眼熟就倾向选它，
  只依据原文是否真的能确定二者是{same_subject}；
- supporting_entry_index 必须填上面某个候选编号（取值只能是 {entry_indexes} 之一），
  选你得出这个结论最主要依据的那一段；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，不要求逐字
  精确，留空也可以。
只输出符合 Schema 的 JSON。"""
    schema = _PrepPackTrueNameVerdictResponse.model_json_schema()
    # 参照 _prep_pack_functional_candidate_call 对 output_schema 注入 enum
    # 的写法：候选段号、候选名单都收紧到本次实际可用的集合，模型在协议层面
    # 就选不出卷宗外的编号或候选集之外的人/地；真正生效的核验仍在
    # _prep_pack_true_name_pin_dossier_entry 与
    # _prep_pack_verify_true_name_hypothesis 里做代码侧结构校验。
    schema["properties"]["supporting_entry_index"]["enum"] = entry_indexes
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_true_name_verdict",
        prompt=prompt,
        model_type=_PrepPackTrueNameVerdictResponse,
        schema_name="episode_prep_pack_true_name_verdict_v2",
        operation_id=(
            f"episode_prep_pack:{episode_id}:true_name_verdict:"
            + evidence_repository.content_hash({
                "subject_kind": subject_kind,
                "alias": alias, "true_name": true_name,
                "candidates": candidates,
                "dossier": [item["entry_index"] for item in dossier],
            })
        ),
        max_tokens=500,
        output_schema=schema,
        # 低温：这道闸的语义判断要稳定——同一份卷宗重跑不该一次选中一次
        # 不确定，跟 _prep_pack_functional_candidate_call 同一考量。
        temperature=0.0,
        call_meta={
            "stage_key": "episode_prep_pack_true_name_verdict",
            "episode_id": episode_id,
            "subject_kind": subject_kind,
            "project_id": project_id,
            "candidates": candidates,
        },
    )


def _prep_pack_true_name_pin_dossier_entry(
    dossier: list[dict[str, Any]], entry_index: Any,
) -> dict[str, Any] | None:
    """3) 钉证：结构性核验，模型只需引用卷宗目录里某个候选编号
    （entry_index），不要求逐字复述原文（1.10.0，缺陷 A 修复，见
    PREP_PACK_VERSION 上方大注释）——真实生产数据（provider_calls
    id=9700/10498）证明旧版逐字引句比对会被模型的跨段拼接/摘要噪音系统性
    误杀（同一失败模式 stages.py._alias_verdict_pin_segment 已经修过一次，
    见该函数 docstring），跟 _prep_pack_functional_candidate_pin_segment
    同一修法：卷宗内容本身就是代码检索出的真实原文，模型选中某一条不存在
    "编造"或"转录出错"的空间，钉证退化为一次整数是否落在集合内的结构性
    判断。非法输入（不是整数、或不在本次卷宗集合内）一律返回 None。"""
    try:
        target = int(entry_index)
    except (TypeError, ValueError):
        return None
    for item in dossier:
        if item["entry_index"] == target:
            return item
    return None


# K/M 共用的并发失败语义（任务②，见 PREP_PACK_VERSION 上方大注释"并发闸"
# 一节）：不用 asyncio.gather 的默认异常语义——默认模式下第一个抛异常的
# 任务会立刻让 gather 重新抛出，但其它还没跑完的任务不会被取消，会在后台
# "孤儿"运行到自己结束，它们的返回值/异常因为没人再等待而被静默丢弃（这是
# asyncio.gather 本身有文档记载的既有行为，不是这里才有的新坑）。改用
# return_exceptions=True 让 gather 等全部任务真正跑完（成功或失败）才
# 返回，再按传入顺序扫一遍结果，遇到第一个异常就原样重新抛出（``raise
# result`` 重新抛出的是同一个异常对象，自带原始 traceback，不是包一层
# 新异常）——不吞、不改写、不静默降级，只是把"谁先失败就立刻甩出、其它
# 任务放养"改成"全部等完再决定失败"，避免孤儿任务与未被读取的异常。没有
# 任何任务失败时原样按输入顺序返回全部结果（跟 asyncio.gather 默认返回值
# 同形状——asyncio.gather 本身就保证结果顺序等于传入顺序，不是完成顺序，
# 这里复用这个既有保证，不需要额外排序）。K（真名核验）、M（候选判别）
# 两条并发化的循环共用这一份失败语义，不分别各写一套。
async def _prep_pack_gather_concurrent(coros: list) -> list:
    results = await asyncio.gather(*coros, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


# K：真名核验并发化（任务②，见 PREP_PACK_VERSION 上方大注释）——单集耗时
# 里 provider_calls 延迟占墙钟 99.4%，_pass() 内角色/场景两支
# suspected_true_name 核验此前对每条提及各自 await 一次
# _prep_pack_verify_true_name_hypothesis，彼此互不依赖却排成一条串行链
# （10 集测量：41 次调用、串行 204.6 秒，6 并发估算 57.8 秒）。核验函数
# 自己已经按 (subject_kind, alias, suspected_true_name) 三元组去重
# （true_name_verdict_cache，见该函数 docstring）——直接把它塞进
# asyncio.gather 会破坏这条去重语义：并发下同一个三元组的多次调用会同时
# 未命中缓存、同时发起模型调用，重复裁决同一件事。修法不是改核验函数本身
# （去重逻辑本来就是对的），而是在真正发起并发调用之前先做一次去重——本
# 函数只做纯读的收集：扫一遍 events，按跟 _pass() 内两处调用点完全相同的
# 判据（角色分支不看 discovery 改名、场景分支要求未经 discovery 改名，
# 逐条对齐，不是重新发明一套判据）算出这一遍 _pass() 需要核验的全部
# (subject_kind, alias, suspected_true_name) 三元组，用 dict 保序去重（同一
# 三元组在多个事件里重复出现是常态，如 1.10.0 缺陷 B 注释里"许姓女子"在
# 4 个事件都出现的真实案例）。调用方（_pass 顶部）用这份去重后的清单过滤掉
# true_name_verdict_cache 里已经有的键（跨 pass1/pass2 复用，语义不变），
# 对剩余的键一次性 asyncio.gather——键已经去重，gather 内不会有两个任务
# 争抢同一个三元组。gather 跑完后，_pass() 原有的逐条 await 调用完全不动
# （见下面两处调用点及其上方大段既有注释）：它们会命中刚刚写热的缓存，
# 同步立即返回，不产生第二次模型调用，也不改变原有的任何一行判定/写回
# 逻辑——并发只发生在"值都还没算出来"的那一刻，一旦缓存写好，_pass() 剩下
# 的全部代码（characters/scenes/functional_extras 等共享字典的写回顺序）
# 100% 保持原来的确定性单线程顺序不变，不需要为并发单独设计写回排序规则。
def _prep_pack_collect_true_name_verification_requests(
    character_mentions: list[dict[str, Any]],
    scene_mentions: list[dict[str, Any]],
    character_rename: dict[str, str],
    scene_rename: dict[str, str],
) -> list[tuple[Literal["character", "scene"], str, str]]:
    """收集这一遍 _pass() 会触发核验的全部 (subject_kind, alias,
    suspected_true_name) 三元组，去重、保插入顺序（顺序只影响 gather 的
    任务提交顺序，不影响任何最终写回结果——见本函数上方注释）。判据必须
    跟 _pass() 内角色/场景两处调用点的既有 if 条件逐字对齐，这里不是重新
    定义一套判据，只是把同一个判据提前算一遍、抽出需要核验的键。2.0.0：
    入参从按事件分组的 ``events`` 改为扁平的 ``character_mentions``/
    ``scene_mentions``（事件分组已随 event_chain 一起撤销），判据本身
    逐字未变。"""
    requests: dict[tuple[Literal["character", "scene"], str, str], None] = {}
    for mention in character_mentions:
        name = str(mention["display_name"] or "").strip()
        if not name:
            continue
        resolved_name = character_rename.get(name, name)
        suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
        if suspected_true_name and suspected_true_name != resolved_name:
            requests[("character", name, suspected_true_name)] = None
    for mention in scene_mentions:
        name = str(mention["display_name"] or "").strip()
        if not name:
            continue
        resolved_via_discovery = name in scene_rename
        resolved_name = scene_rename.get(name, name)
        suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
        if (
            suspected_true_name
            and suspected_true_name != resolved_name
            and not resolved_via_discovery
        ):
            requests[("scene", name, suspected_true_name)] = None
    return list(requests)


async def _prep_pack_verify_true_name_hypothesis(
    conn, *, project_id: str, episode_id: str, episode_no: int, source_text: str,
    alias: str, suspected_true_name: str,
    subject_kind: Literal["character", "scene"], resolve_fn, run_id: str | None,
    bible: Bible | None = None,
    verdict_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify (not trust) a model-declared ``suspected_true_name`` guess via
    the dossier trial procedure documented above. Returns a dict:
    ``accepted``；不通过时的 ``reason``（rejected_no_dossier/
    rejected_verdict_different/rejected_verdict_uncertain/
    rejected_entry_not_pinned/rejected_pinned_entry_missing_alias/
    rejected_dual_anchor_available_not_pinned/
    rejected_degraded_pin_out_of_episode，都归入观测的 rejected_verdicts
    概念，1.10.0 缺陷 A 修复新增后三种，见 PREP_PACK_VERSION 上方大注释）；
    通过时的 ``pinned_quote``/``pinned_chapter_idx``（分别供 provenance.
    anchor_phrase 与 method="resolution"/"resolution_forward" 判定）与
    ``dual_anchor``（1.10.0 新增：钉证命中的是双锚定条目还是退化后的
    集内别名指代条目——不可观测的静默降级本项目明令禁止，调用方须把这个
    布尔值写进 provenance/true_name_hints，见 _pass 里两处调用点）。
    ``subject_kind`` 区分角色分支（resolve_fn=_resolve_portrait_id）与
    场景分支（resolve_fn=_resolve_scene_reference_id）——两者共用本函数
    与下面的裁决调用，但问的语义不同（"同一个人" vs "同一个场景或地点"，
    见 _prep_pack_true_name_verdict 的 _TRUE_NAME_VERDICT_SUBJECT_COPY），
    调用点各自传对。``bible`` 由调用方（_resolve_assets 的 _pass 闭包）
    传入已加载好的项目圣经，避免每次核验重复查库；缺省时现查一次（防御性
    兜底，理论上调用点都会传）。``verdict_cache`` 是 _resolve_assets 级别
    按 (subject_kind, alias, suspected_true_name) 缓存的判决结果，同一次
    生成内重复出现的同一对提及不重复发起模型调用；subject_kind 纳入键是
    因为角色循环与场景循环共用同一个缓存字典，不按域隔离会导致跨域撞名
    时复用错误域的裁决（独立评审发现的 minor）。"""
    empty = {
        "accepted": False, "reason": "", "pinned_quote": "",
        "pinned_chapter_idx": None, "dual_anchor": False,
    }
    if not suspected_true_name:
        return empty
    if resolve_fn(conn, project_id, suspected_true_name, episode_no) is None:
        return empty
    cache_key = (subject_kind, alias, suspected_true_name)
    if verdict_cache is not None and cache_key in verdict_cache:
        return verdict_cache[cache_key]

    def _reject(reason: str) -> dict[str, Any]:
        result = {**empty, "reason": reason}
        if verdict_cache is not None:
            verdict_cache[cache_key] = result
        return result

    dossier = _prep_pack_true_name_dossier(conn, project_id, alias, suspected_true_name)
    if not dossier:
        return _reject("rejected_no_dossier")

    project_bible = bible if bible is not None else _load_project_bible(conn, project_id)
    roster = _prep_pack_true_name_verdict_roster(project_bible, subject_kind)
    candidates = _prep_pack_true_name_verdict_candidates(dossier, roster, suspected_true_name)
    # 双锚定是否结构上可能存在（1.10.0，缺陷 A 修复第②③点）：全卷宗（不只是
    # 模型最终钉中的那一条）是否存在同时逐字含 alias 与 suspected_true_name
    # 的条目——这份判断只用 both 桶天然的性质（budget 裁剪只影响 single 桶，
    # both 桶全收，见 _prep_pack_true_name_dossier docstring），不依赖模型
    # 这次选了哪一条，是纯粹的既有材料事实。
    dual_anchor_available = any(
        alias in item["text"] and suspected_true_name in item["text"] for item in dossier
    )
    response = await _prep_pack_true_name_verdict(
        run_id=run_id, episode_id=episode_id, project_id=project_id,
        subject_kind=subject_kind, alias=alias, true_name=suspected_true_name,
        dossier=dossier, candidates=candidates,
    )
    if response.selected_candidate != suspected_true_name:
        reason = (
            "rejected_verdict_uncertain"
            if response.selected_candidate == _PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL
            else "rejected_verdict_different"
        )
        return _reject(reason)
    pinned = _prep_pack_true_name_pin_dossier_entry(dossier, response.supporting_entry_index)
    if pinned is None:
        return _reject("rejected_entry_not_pinned")
    # 钉证至少要求引句逐字包含被解析的那个别名本身（1.10.0，缺陷 A 修复
    # 第②点）：这是"零保护"的主要来源——生产数据实测 114 条真实 same 判决
    # 里，56 条（49%）引用的支撑句缺 alias/true_name 至少一个；只看明确
    # 询问人名的，18/75（24%）里 2 条连 alias 本身都不含，钉的是一句跟
    # 待判标签毫无关系的话。这一条对合法的跨章绑定（EP5"许姓女子"→"许清"
    # 那类，见下面 dual_anchor_available 分支）零伤害——集内指代段落
    # 天然含 alias 本身。
    if alias not in pinned["text"]:
        return _reject("rejected_pinned_entry_missing_alias")
    if dual_anchor_available:
        # 优先要求双锚定引句（1.10.0，缺陷 A 修复第③点）：卷宗结构上确实
        # 存在能同时证明 alias 与 true_name 的桥接句时，钉证必须钉在其中
        # 一条上——不能在更强证据摆在模型眼前时，仍然只钉一句弱证据（真实
        # 数据：18/75 里另有一部分是"卷宗里其实有更强证据，模型没用上"的
        # 形状，即使这次不专门统计，收紧钉证目标本身就同时堵住了这一类）。
        if suspected_true_name not in pinned["text"]:
            return _reject("rejected_dual_anchor_available_not_pinned")
        dual_anchor_used = True
    else:
        # 退化：全卷宗都不存在双锚定证据（结构性事实，不是这次没找到——
        # 真实 EP5 案例："许清"这个名字要到第34章才第一次在原著里出现，
        # 跟"许姓女子"永远不会同段共现，dual anchor 在这本书里对这对
        # (alias, true_name) 原理上不可能存在）。允许退化为"仅含别名的
        # 集内指代段落"——但必须真的是本集自己的段落，不是全书别处巧合
        # 复现的同一个短语（真实数据坐实的风险：proj_3ac0b627fa46 第981章
        # 也有一处"许姓女子"，却是完全不相关的转世预言片段，跟 EP5 本集
        # 语境毫无关系——不做这条限制，钉证可能钉在这类不相关的巧合复现
        # 上）。dual_anchor_used=False 是显式的可观测降级标记（本项目明令
        # 禁止静默降级），调用方须写进 provenance/true_name_hints。
        if pinned["text"] not in source_text:
            return _reject("rejected_degraded_pin_out_of_episode")
        dual_anchor_used = False

    result = {
        "accepted": True, "reason": "", "pinned_quote": pinned["text"],
        "pinned_chapter_idx": pinned["chapter_idx"], "dual_anchor": dual_anchor_used,
    }
    if verdict_cache is not None:
        verdict_cache[cache_key] = result
    return result

def _load_project_bible(conn, project_id: str) -> Bible:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    raw = (row["bible_json"] or "").strip() if row else ""
    if raw:
        return Bible.model_validate(json.loads(raw))
    return Bible.model_validate({
        "characters": [], "scenes": [],
        "world": {"era": "", "genre": "", "visual_style_canonical": _FALLBACK_VISUAL_STYLE},
    })


def _character_discovery_dispositions(
    discovery_result: dict[str, Any],
) -> tuple[set[str], dict[str, str], set[str]]:
    """Turn app.portraits.ensure_cards_for_text's result into lookup aids for
    the second resolution pass:
    - skip_names: mentions the discovery mechanism itself (not this file)
      determined need no character card/portrait -- typed functional identity,
      stable reference-only identity, or a ``skipped`` disposition. Recorded
      as a functional extra (unless also in non_person_names), not silently
      dropped -- see _resolve_assets.
    - rename_map: mentions whose confirmed real name differs from the event
      chain's raw mention text (e.g. a title resolved to the true name),
      re-keyed by that real name instead.
    - non_person_names: the subset of skip_names discovery explicitly judged
      is not a person at all (``skipped_not_person`` -- a sect/artifact/pen
      name the chunk extractor mistakenly listed as a character). These are
      still legally skip-able (no portrait required) but must NOT show up in
      functional_extras, which is a list of *people* in frame for P1
      storyboard prompts, not a dumping ground for every non-card mention.
    These only match by exact string equality against discovery's own
    source_label/name, which is a *different* model call's phrasing of the
    same source text and will not always coincide with prep_pack's chunk-
    extraction phrasing (real EP13 case: discovery resolved "外宗弟子" while
    the published chunk extraction said "一名外宗弟子" -- same real-world
    concept, different string). A name this misses is not necessarily
    unclassified; see _resolve_assets' functional-extra default and
    _discovery_errored_names for what actually still blocks.
    """
    skip_names: set[str] = set()
    non_person_names: set[str] = set()
    for item in discovery_result.get("skipped") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        skip_names.add(name)
        if str(item.get("status") or "").strip() == "skipped_not_person":
            non_person_names.add(name)
    rename_map: dict[str, str] = {}
    for item in discovery_result.get("resolutions") or []:
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        resolution = str(item.get("resolution") or "").strip()
        if not source_label:
            continue
        if resolution in _FUNCTIONAL_RESOLUTION_KINDS:
            skip_names.add(source_label)
        elif canonical_name and canonical_name != source_label:
            rename_map[source_label] = canonical_name
    return skip_names, rename_map, non_person_names


def _discovery_errored_names(
    discovery_result: dict[str, Any], candidate_names: list[str],
) -> set[str]:
    """Which of *our* raw mention strings discovery explicitly failed on.

    ensure_cards_for_text's own error strings are name-prefixed
    ("{name}：原因", app/portraits.py:7383/7407) but not schema-guaranteed, so
    this checks containment against each of our own candidate names rather
    than trying to parse discovery's message format -- a name only lands here
    if discovery said something concrete *about that name*, e.g. "身份模型已
    确认真名，但人物卡模型未返回完整稳定卡片" (a confirmed real identity
    whose card generation itself failed -- a real defect, must block) or an
    exception during its own processing. This is deliberately the one thing
    _resolve_assets still hard-blocks on after discovery runs; everything
    else defaults to a functional extra (see its docstring).
    """
    messages = [str(message) for message in discovery_result.get("errors") or []]
    if not messages:
        return set()
    return {
        name for name in candidate_names
        if name and any(name in message for message in messages)
    }


async def _discover_new_characters(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    source_text: str, run_id: str | None,
) -> dict[str, Any]:
    """谱外新角色 → 发现 → 补录人物谱 → 生成定妆照。

    Reuses app.portraits' identity-discovery machinery as-is (does not
    reimplement it): importance = source chapters + CHARACTER_IMPORTANCE_
    FORWARD_CHAPTERS, true-name resolution = its own independent
    IDENTITY_DISCOVERY_FORWARD_CHAPTERS window (portraits.py:384-385), and the
    spoiler rule that forward context may only resolve an already-appeared
    identity's stable name, never pull future plot into this episode
    (ensure_cards_for_text -> discover_character_candidates docstrings). Only
    called when pass 1 of ``_resolve_assets`` below leaves a real,
    non-background-extra character mention unresolved -- see the zero-call
    regression assertion in tests/test_prep_pack_asset_discovery.py.
    """
    from app.portraits import (
        ensure_cards_for_text,
        persist_screenplay_character_resolutions,
        screenplay_identity_scope_fingerprint,
    )
    from app.source_paratext import strip_paratext

    bible = _load_project_bible(conn, project_id)
    # Same purification prep_pack's dead-code predecessor
    # (app.domain.screenplay_ops._screenplay_character_discovery) applied
    # before discovery: stage 0 runs before any paratext judgment exists, so
    # without this an author's pen name in chapter-end commentary gets
    # mistaken for a character. Only the discovery-facing copy is stripped;
    # source_text itself (used for event-chain evidence) is untouched.
    discovery_text = await strip_paratext(
        source_text,
        operation_id=f"episode_prep_pack.character_discovery.paratext:{episode_id}",
    )
    result = await ensure_cards_for_text(
        project_id, episode_no, discovery_text, bible, generate_portraits=True,
    )
    persist_screenplay_character_resolutions(
        conn, episode_id, result.get("resolutions") or [],
        retire_legacy_future_identity=True,
        expected_active_run_id=run_id,
        replace_identity_scope=screenplay_identity_scope_fingerprint(episode_no, source_text),
    )
    return result


async def _discover_new_scenes(
    conn, *, project_id: str, episode_no: int, labels: list[str],
) -> dict[str, Any]:
    """谱外新场景 → 发现 → 补录场景库 → 生成场景参考图。

    Reuses app.scenes' reactive scene-discovery machinery as-is via
    ``ensure_scenes_for_labels`` (a thin adapter added alongside
    ``ensure_scenes_for_storyboard`` for callers, like this one, that have a
    flat label list instead of a compiled screenplay object -- same
    assess_new_scene/_generate_and_register_scene functions underneath, no
    discovery logic duplicated). Only called when pass 1 below leaves a scene
    mention unresolved.
    """
    from app.scenes import ensure_scenes_for_labels

    return await ensure_scenes_for_labels(project_id, episode_no, labels)


# ---------- 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大注释
# 的完整案情）：用户原始诉求——同一角色在不同集换脸，真名揭晓前人物建模
# 持续漂移。真实 EP1 现场：标签"银色长袍女子"本该绑定许清（appearance_
# canonical 明确写着"常年穿银色长袍"，人物谱已登记确认别名"许师姐"，本集
# 原文两次出现"许师姐"），却因为标签类型对不上（模型给出场角色起的是外貌
# 描述，别名库登记的是称谓）落 functional_extras 当无图群演。
#
# 根因不是别名机制坏了——是这类"既查不到 portrait、也命中不了别名"、即将
# 落入 functional_extras 的标签，从未真正过一遍"人物谱里有没有人已经在
# 本集原文里跟它共现"的判别。skip_character_names 的两条既有来源（discovery
# 自己判定 skip、以及 _resolve_assets 下方"Coordinator-mandated default"
# 兜底）都只回答了"这不是一个可以直接建卡的新角色"，从未回答这个问题。
#
# 修复范式完全复用 app/stages.py 当晚落地的别名裁决庭三段式（_alias_
# verdict_dossier / _alias_verdict_candidates / _alias_verdict_call /
# _alias_verdict_pin_segment：代码检索卷宗 → 候选判别 → 段号钉证），但作用
# 域收窄到本集自己的 source_text——prep_pack 不需要 stages.py 那样跨全书找
# "桥接章"：这里的候选与证据都只在本集范围内找，找不到就维持原行为落群演，
# 不做跨集检索，跟"确定性、零语义"的既有纪律一致。两个模块不允许互相导入
# 内部函数（保持边界干净），本节是同一范式的独立实现，不是重构共享：
#   1) 候选集（代码，零语义，_prep_pack_functional_candidate_names）：本集
#      source_text 里规范名或已确认别名有字面命中的人物谱角色。不针对任何
#      具体人名/姓氏做特判（真实误登记事故教训，见 stages.py 同名注释）；
#      候选集为空直接维持原行为，不发起任何模型调用。
#   2) 卷宗（代码，零语义，_prep_pack_functional_candidate_dossier）：按
#      自然段切分本集原文，覆盖全部候选各自的出场证据——不能只收集被测
#      标签周围的证据，那会让下一步的选择题名存实亡（stages.py 已验证的
#      真实教训：模型看不到正确候选的材料，只能靠反复出现的候选拍脑袋）。
#      1.8.1 起卷宗主锚点改为事件跨度定位，见该函数与 _prep_pack_
#      functional_candidate_event_span_segments 的完整说明（下面单独一段）。
#   3) 裁决（模型，唯一一次调用，_prep_pack_functional_candidate_call）：
#      候选选择题——"标签 X 最可能指候选中的哪一位"，候选集之外强制一个
#      "都不是/无法确定"选项，schema 用 enum 收紧到候选集与卷宗段号。不是
#      "标签是不是候选 A"的是非题（stages.py 已验证是非题诱发确认偏误：
#      模型看到反复出现的某个候选会不自觉地倾向他，跟他是不是正确答案
#      无关）。
#   4) 钉证（代码，结构性，_prep_pack_functional_candidate_pin_segment）：
#      模型只需引用卷宗目录里的段号，不比对模型转录的逐字引句——今晚已
#      证明那种比对方式会因转录波动（跨段拼接/省略号/标点微调）误杀正确
#      判定，钉证退化为"选中的段号是否落在卷宗集合内"这一结构性判断。
# 选中候选集里的真实一员、且段号钉证通过、且这个候选在本集确有已生成的
# 定妆照（复用既有 _resolve_portrait_id，不重复实现一遍"有没有图"的判断）、
# 且这次改名不会与跨集别名注册表冲突（复用既有 _prep_pack_cross_episode_
# alias_conflict，同一套"不确定不绑"纪律），才把这个标签重新计入
# character_rename——调用点见 _resolve_assets 内 "Coordinator-mandated
# default" 循环之后。选了"都不是/无法确定"、选了候选集之外的值（协议层
# 已经不可能，代码侧仍做防御性核验）、卷宗为空、候选没有可用定妆照、或
# 存在跨集别名冲突，一律返回 None——调用方维持原行为，标签留在
# skip_character_names 正常落 functional_extras，绝不猜。
#
# 严禁任何具体人名/称谓的硬编码特判；严禁外貌关键词模糊匹配（"绿袍男子"
# 这类外貌描述在长篇小说里能撞上一大片人，模糊匹配就是下一个误绑事故）——
# 本节全程只用"人物谱角色的规范名/已确认别名是否逐字命中原文"这一结构判据
# 构造候选与卷宗，谁是正确答案完全交给模型基于真实原文独立判别。
#
# 1.8.1（真实数据、已完整诊断的后续事故）：上面 1.8.0 机制本身工作正常
# （EP1 实测 10 次调用全部 OK），但目标案例仍然失败——标签"银色长袍女子"→
# 候选集正确含"许清"→模型却答"都不是/无法确定"，因为卷宗（2)步骤检索出
# 的段落里根本没有任何相关证据：`label in seg.text` 逐字匹配"银色长袍女子"
# 在原文里 0 次命中（原文写的是"穿着一身银色长袍"，模型转述成了这个标签，
# 不是原文字面），both/text_only 两类因此全空；候选锚点段落（anchor_only）
# 在失去参照点后退化成文档顺序，主角"孟浩"几乎每段都出现的开篇独白段落
# 吃光了卷宗预算，"许师姐"（许清的已确认别名，紧邻"银袍女子被绿袍男子
# 称许师姐"这一幕）那两段根本没进卷宗——这正是 stages.py._alias_verdict_
# dossier docstring 里写明要防的"主角淹没预算"陷阱，prep_pack 这侧因为缺
# 标签锚点而失效。修法：卷宗主锚点改用事件跨度定位而非标签字面匹配——见
# _prep_pack_functional_candidate_event_span_segments（标签所属事件的
# source_span 覆盖段落，事件链抽取模型必须为每个事件声明这个字段，不依赖
# 标签措辞是否逐字命中原文）与 _prep_pack_functional_candidate_dossier
# 改造后的两层主锚点 + 候选锚点段落按"离事件跨度的邻近度"补足预算（详见
# 两个函数各自的完整 docstring）。label 逐字命中原文这条路径继续保留、
# 不因为改用事件定位就丢弃（有些标签确实是原文用词）；事件跨度缺失/为空
# 时防御性退回 1.8.1 之前的既有行为，不崩。
#
# 1.8.2/1.8.3（同一晚同一事故的第二、三层根因）：完整案情见 PREP_PACK_
# VERSION 上方对应版本号大注释，不在这里重复——概括地说，1.8.2 把 A/B 两侧
# 保底配额下沉到卷宗预算分配层，1.8.3 进一步把 B 侧保底粒度下沉到"每个
# 候选"、字数预算也按同样粒度兜底（保底段一律收录，超限做确定性截断而非
# 整段丢弃），并把候选集从"只看本集原文逐字命中"扩展为"逐字命中 ∪ 人物谱
# 注册区间覆盖本集"两类并集。

_PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL = "都不是/无法确定"
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES = 12  # 单条候选判别卷宗最多收录的段落数
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS = 6000  # 单条候选判别卷宗最多收录的总字符数
# A 侧（事件跨度段+标签字面段）保底配额——见 PREP_PACK_VERSION 上方 1.8.2
# 大注释的完整根因。1.8.3 起这个常量只约束 A 侧一家：B 侧的保底改为"每个
# 候选至少一段"（见下面 _prep_pack_functional_candidate_dossier 的按候选
# 保底逻辑），不再是一个笼统的 B 侧总量数字——1.8.2 用同一个常量给 A、B
# 两侧各分 4 条，A 侧那 11 段大段外貌/环境描写几乎吃光 MAX_CHARS 时，B 侧
# 名义上保底 4 条实际只有 1 条真正挤进卷宗（1.8.3 大注释根因一）；同时
# 4 条位置数字本身也无法阻止候选轮转顺序里排最前的候选独占那唯一挤进去的
# 名额（1.8.3 大注释根因二）。取值 4 保留给 A 侧：标签指涉对象所在的现场
# 本身（事件跨度/标签字面）同样是判别必需材料，不能因为改成"每候选保底"
# 就彻底不留位置；A 侧段落多时这份保底优先让位给"每候选至少一段"这个更
# 具体的硬要求（见 dossier 函数 reserve_a 的计算）。
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES = 4
# 1.8.3 新增：保底层（A 侧保底 + 每候选保底）单段文本被截断时的目标长度
# 上限，见 PREP_PACK_VERSION 上方 1.8.3 大注释根因一、
# _prep_pack_functional_candidate_truncate_segment 完整说明。取值 260：
# 真实事故的决定性证据句"绿袍男子对着她躬身行礼，口称许师姐，随后请四人
# 随他回宗门"不到 40 字，260 字对绝大多数单句/单个小段落都绰绰有余、
# 几乎不会触发截断；即使 MAX_ENTRIES(12) 全部落在保底层这种极端场景，
# 12×260=3120 字仍明显小于 MAX_CHARS(6000)，保底层因此永远不需要再跟
# flex 层抢字数预算——这正是修复根因一的关键：保底层的收录与截断只取决于
# 单段自身长度，不取决于其它段落已经用掉多少字数，天然确定性、跟处理
# 顺序无关。不是靠放大 MAX_ENTRIES/MAX_CHARS 绕过问题，两个既有上限常量
# 原样不变。
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS = 260
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_TRUNCATION_MARK = "…"


def _prep_pack_functional_candidate_roster(bible: Bible) -> dict[str, list[str]]:
    """候选面快照：规范名 -> [规范名, 已确认别名...]。``bible.characters[].
    aliases`` 里的每一条本就是全书分析阶段模型申报 + 代码核验通过后才落库
    的确认别名（app.schemas.CharacterAlias，见该类 docstring），这里不需要
    重新核验证据锚点，直接读取文本即可——核验是全书分析阶段
    （app.stages.generate_bible）的职责，不在本文件重复。跟
    app.stages._alias_verdict_roster 同一构造，两个模块不互相导入内部
    函数，各自独立实现一份。"""
    return {
        character.name: [character.name, *(alias.text for alias in character.aliases)]
        for character in bible.characters
    }


def _prep_pack_functional_candidate_names(
    source_text: str, roster: dict[str, list[str]],
) -> list[str]:
    """结构判据，零语义：规范名或其任一已确认别名逐字子串命中本集
    ``source_text``，即算该角色在本集"出场"，候选入选。不针对任何具体
    人名/姓氏做特判。返回值按 ``roster``（即 bible.characters 原始登记
    顺序构造的字典，Python 字典保序）顺序去重——一个角色只要任一称谓命中
    就只计入一次。"""
    return [
        name for name, surface_forms in roster.items()
        if any(form and form in source_text for form in surface_forms)
    ]


def _prep_pack_functional_candidate_label_segments(
    character_mentions: list[dict[str, Any]], label: str,
) -> set[int]:
    """标签 -> 该标签自己申报的段号并集（2.0.0，直接替代 1.8.1 引入的
    ``_prep_pack_functional_candidate_event_span_segments``；见
    PREP_PACK_VERSION 上方 2.0.0 大注释"新增 props"一节之前那段、
    _prep_pack_functional_candidate_dossier 的完整说明）：结构判据，零语义。

    1.8.1 引入"事件跨度"这层间接锚点的根因是：标签本身常是模型转述/综合出
    的描述短语（真实事故："银色长袍女子"原文逐字出现 0 次），靠标签字面
    匹配原文定位卷宗会打空；2.0.0 下每条 ``_ModelCharacterMention`` 已经
    直接自报 ``segment_indexes``（这个提及自己声称在哪些段落画面出场，
    已经过 _prep_pack_gate_segment_indexes 的结构闸——落在它自己所属 chunk
    范围内，但刻意不要求逐字命中，见该函数说明：这正是"银色长袍女子"这类
    合成标签仍能被正确定位到自己声称的段落的原因），不再需要"先分事件、
    再从事件的粗粒度跨度反推段号"这层间接：同一个 label 字符串在多条提及
    （可能来自不同 chunk）里出现过的全部 segment_indexes 取并集，就是比
    事件跨度更精确（不含跨度内不相关的中间段落）的同一份材料。"""
    matched: set[int] = set()
    for mention in character_mentions:
        if str(mention.get("display_name") or "").strip() != label:
            continue
        matched.update(int(index) for index in mention.get("segment_indexes") or [])
    return matched


def _prep_pack_functional_candidate_anchor_pool(
    segments: list[SourceSegment], label: str,
    candidate_anchor_texts: dict[str, list[str]],
    event_span_indexes: list[int], event_span_index_set: set[int],
) -> tuple[list[int], list[int], dict[str, list[int]]]:
    """A 侧第二层（label 逐字命中段）与 B 侧（候选锚点段落，按候选公平
    轮转合并）的联合检索，1.8.2 新增，见 PREP_PACK_VERSION 上方 1.8.2
    大注释、_prep_pack_functional_candidate_dossier 的完整说明。两者共用
    同一次 segments 扫描（避免重复遍历）：``label`` 逐字命中的段落只在
    "不属于事件跨度"时才归入 A 侧的 ``label_text_indexes``（事件跨度内的
    label 命中已经算 A 侧了，不需要重复计入）；候选锚点匹配对全部段落
    执行，不因为某段已被 ``event_span_index_set`` 收录就跳过（1.8.4 修复，
    见 PREP_PACK_VERSION 上方 1.8.4 大注释：真实事故——候选"许清"确认别名
    "许师姐"命中的两处段落都恰好落在事件跨度并集内部，旧版在这里直接
    `continue` 跳过候选匹配，导致这个候选从"每候选保底"的输入集合
    ``per_candidate_indexes`` 里彻底消失，保底对它形同虚设）。这意味着
    某段落现在可能同时是事件跨度成员、又是某个候选的锚点段——这是
    有意允许的重叠（该候选的"每候选保底"需要知道这段属于它，不管这段
    是否也在事件跨度集合里；见 _prep_pack_functional_candidate_dossier
    对 ``guaranteed_b_anchor``/``primary_index_set`` 重叠时的优先级说明）。

    1.8.3 起额外返回 ``per_candidate_indexes``（分组结果本身，未经轮转
    合并）——供 ``_prep_pack_functional_candidate_dossier`` 计算"每个候选
    至少一段"这一按候选粒度的硬性保底（见该函数 docstring 与 PREP_PACK_
    VERSION 上方 1.8.3 大注释）：那个保底必须精确知道某个候选自己最近的
    锚点段落是哪一条，轮转合并之后的 ``anchor_pool_ordered`` 只是"混合好
    的一份列表"，不再能反查某一段究竟满足了哪个候选的保底，所以两者都要
    返回。

    B 侧排序是本函数真正新增的部分——1.8.1 把全部候选的锚点段落混在一起
    按"离案发现场的邻近度"整体排序，本轮真实事故的第二个根因正出在这里：
    整体排序下，本章出场次数越多的候选，越多段落挤进排序靠前的位置，一旦
    配额有限（受 A 侧保底挤压后更是如此），出场次数少的候选会被排到全部
    落选——跟"A 侧全收挤没 B 侧"是同一个"主角淹没预算"陷阱，只是这次发生
    在 B 侧内部的候选粒度，光靠 A/B 两侧保底配额堵不住。

    做法：先按候选分组，每个候选自己的锚点段落仍按"离主锚点（事件跨度
    段落；事件跨度为空时退回 label 命中段落）的邻近度升序、距离相同段号
    升序"排序——邻近度规则本身不变，只是分组后各自排序，不再混在一起
    整体排序；随后按"每个候选轮流各出一段"合并：候选①最近的一段、候选②
    最近的一段、……、候选①第二近的一段、候选②第二近的一段、……
    （``candidate_anchor_texts`` 的 key 顺序即调用方 ``candidates`` 的既有
    确定性顺序，见 _prep_pack_functional_candidate_names 的 roster 保序
    说明）。这样任何前缀配额下的候选出现次数最多相差 1——不管某个候选在
    原文里反复出场多少次，只要还没轮到把其它候选的锚点段落全部轮完，它
    都不会占用超过"自己的份额+1"的位置。同一段落同时命中多个候选（原文
    同段提到两人）按候选顺序谁先轮到归谁，其余候选这一轮空转、不影响
    自己后续轮次的进度，也不重复计入卷宗或重复占用配额。"""
    label_text_indexes: list[int] = []
    per_candidate_indexes: dict[str, list[int]] = {name: [] for name in candidate_anchor_texts}
    for index, seg in enumerate(segments):
        if index not in event_span_index_set:
            if label and label in seg.text:
                label_text_indexes.append(index)
                continue
        # 段落落在事件跨度内时不再跳过候选匹配（1.8.4 核心修复，见本函数
        # docstring"跨度吞并候选锚点"一节）：跳过只对 label 逐字匹配这一
        # 分支有意义（事件跨度本身已经是 A 侧主锚点，label 命中不需要再
        # 重复计入 label_text_indexes），候选锚点匹配必须覆盖全部段落，
        # 否则候选自己唯一的证据段落只要恰好落在事件跨度范围内，就永远
        # 进不了 per_candidate_indexes，每候选保底对它形同虚设。
        for name, forms in candidate_anchor_texts.items():
            if any(form and form in seg.text for form in forms):
                per_candidate_indexes[name].append(index)
    # 邻近度参照点：优先事件跨度段落（"离案发现场的远近"）；事件跨度为空
    # 时退回 label 命中段落——事件跨度缺失/为空的防御性回退，跟 1.8.1 的
    # 既有语义完全一致。两者都为空时（label 是转述短语、原文无字面）候选
    # 段落没有邻近度参照点，保持扫描得到的文档顺序，等价于改造前的既有
    # 行为，不引入新的失败模式。
    proximity_anchor = event_span_indexes or label_text_indexes
    if proximity_anchor:
        for indexes in per_candidate_indexes.values():
            indexes.sort(
                key=lambda index: (min(abs(index - anchor) for anchor in proximity_anchor), index),
            )
    candidate_order = list(candidate_anchor_texts.keys())
    seen: set[int] = set()
    anchor_pool_ordered: list[int] = []
    max_round = max((len(indexes) for indexes in per_candidate_indexes.values()), default=0)
    for round_idx in range(max_round):
        for name in candidate_order:
            indexes = per_candidate_indexes[name]
            if round_idx >= len(indexes):
                continue
            index = indexes[round_idx]
            if index in seen:
                continue
            seen.add(index)
            anchor_pool_ordered.append(index)
    return label_text_indexes, anchor_pool_ordered, per_candidate_indexes


def _prep_pack_functional_candidate_truncate_segment(text: str, anchor: str) -> str:
    """确定性截断（1.8.3 新增，见 PREP_PACK_VERSION 上方 1.8.3 大注释根因一、
    _prep_pack_functional_candidate_dossier 的完整说明）：保底层的段落绝不
    因为字数超限被整条丢弃——某个候选唯一的锚点证据段如果恰好很长（大段
    外貌/环境描写），必须截断而不是排除，模型才有机会看到它。

    ``anchor`` 是这段文本之所以入选保底层的那个具体触发词（A 侧：``label``
    本身；B 侧：命中该候选的那个规范名/别名字面串），用来定位"核心句"——
    先用中文常见句子终止符（。！？换行）把 ``text`` 切成句子，取包含
    ``anchor`` 的那一句；这句本身仍超过目标长度时，以 ``anchor`` 在句中的
    位置为中心继续裁剪，保证锚点词始终留在截断结果里（截掉的是锚点词
    两侧的上下文，不是锚点词本身）。裁剪掉的一侧加省略标记，让下游读者/
    模型知道这不是段落全文。``anchor`` 为空或在 ``text`` 里根本找不到
    （防御性：调用方按约定只会传入确实命中该段的锚点词，但不假设这个约定
    一定成立，找不到时不崩、不猜句子边界）时退回"从头部截断到目标长度"这
    个更保守的兜底，不做任何"哪句更重要"的语义判断。

    不针对任何具体人名/称谓做特判——``anchor`` 完全是调用方传入的字符串
    参数，本函数只做纯字符串定位与切片，是结构操作，不是语义理解。"""
    limit = _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS
    if len(text) <= limit:
        return text
    mark = _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_TRUNCATION_MARK
    anchor_pos = text.find(anchor) if anchor else -1
    if anchor_pos < 0:
        return text[:limit].rstrip() + mark
    # 用常见中文句子终止符切句边界，取锚点词所在的那一句：从头扫描终止符，
    # 锚点词之前最近的终止符是句首，锚点词之后最近的终止符是句尾。
    start, end = 0, len(text)
    for match in re.finditer(r"[。！？\n]", text):
        boundary = match.end()
        if boundary <= anchor_pos:
            start = boundary
        else:
            end = boundary
            break
    if end - start > limit:
        # 核心句本身仍超限：以锚点词在句中的位置为中心继续裁剪，锚点词
        # 始终落在裁剪窗口内。
        local_pos = anchor_pos - start
        half = max(0, (limit - len(anchor)) // 2)
        crop_start = start + max(0, local_pos - half)
        crop_end = min(end, crop_start + limit)
        crop_start = max(start, crop_end - limit)
        start, end = crop_start, crop_end
    core = text[start:end].strip()
    prefix = mark if start > 0 else ""
    suffix = mark if end < len(text) else ""
    return f"{prefix}{core}{suffix}"


def _prep_pack_functional_candidate_dossier(
    segments: list[SourceSegment], label: str,
    candidate_anchor_texts: dict[str, list[str]],
    event_span_segments: set[int] = frozenset(),
) -> list[dict[str, Any]]:
    """裁决卷宗检索（1.8.3 保底粒度下沉到"每个候选"，见 PREP_PACK_VERSION
    上方 1.8.3 大注释的完整根因——真实事故：1.8.2 的 A/B 两侧保底配额确实
    让"许师姐"那一段挤进了卷宗，但只挤进 1 段，且被候选轮转顺序里排最前的
    主角类候选占了，真正的目标候选一段都没拿到）。

    两侧证据来源基本不变（跟 1.8.1/1.8.2 一致，这里不重复根因，只重复
    形状）：
    - A 侧＝``primary_indexes``＝两层主锚点的并集：①``event_span_segments``
      （该标签所属事件的 source_span 覆盖段落，见
      _prep_pack_functional_candidate_event_span_segments）②``label``
      逐字命中原文的段落（未被①收录的部分）；
    - B 侧＝候选（规范名∪已确认别名）逐字命中的段落，按候选分组，见
      _prep_pack_functional_candidate_anchor_pool 的完整说明。1.8.4 起
      B 侧不再排除事件跨度内的段落（见该函数 docstring 与 PREP_PACK_
      VERSION 上方 1.8.4 大注释）——A、B 两侧因此可能重叠：某个候选的
      锚点段恰好也落在事件跨度内是允许的、甚至是这次要修的真实事故本身
      （候选"许清"的锚点段落在事件跨度内部，1.8.1-1.8.3 因为 B 侧扫描
      跳过事件跨度内的段落而对它完全不可见）。

    1.8.3 的核心改动——按候选粒度的硬性保底：``candidate_anchor_texts``
    保序遍历，每个确有锚点证据（``per_candidate_indexes[name]`` 非空）的
    候选，独立取自己离主锚点最近的那一段（``indexes[0]``，已在
    anchor_pool 里按邻近度排好序）纳入保底层——不是"B 侧保底 N 条"这个
    笼统的位置数字，而是"每个候选各自的保底"，谁的锚点证据都不会被另一个
    候选或 A 侧挤没。两个候选的最近段恰好是同一段（原文同段提到两人）时
    天然去重——那一段同时满足两者的保底，不重复计入。A 侧的既有保底
    （``_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES``）这次
    优先让位给每候选保底：先扣掉每候选保底已经用掉的条数名额，A 侧保底
    只取"剩余名额"与"自身可用条数"两者较小值——A 侧段落越多，让位越明显，
    但只要还有剩余名额就仍有代表段（标签指涉对象所在的现场本身也是判别
    必需材料，不能因为改成"每候选保底"就彻底清零）。

    保底层（A 侧保底 + 每候选保底）在收录阶段一律直接收录，绝不因为字数
    预算不够被跳过——这是修复的第二个关键点，也是本轮事故的直接原因（见
    1.8.3 大注释根因一：1.8.2 的"配额位置"并不保证"配额一定进得去卷宗"）。
    单段文本超过 ``_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_
    ENTRY_MAX_CHARS`` 时用 ``_prep_pack_functional_candidate_truncate_
    segment`` 做确定性截断（保留含锚点词的核心句 + 省略标记），不整段
    丢弃——截断只取决于该段自身长度，不取决于其它段落已经用掉多少字数，
    因此天然与处理顺序无关、确定性可复现，也不会因为保底层本身而突破
    MAX_CHARS（见该常量上方注释的预算测算）。保底层收录完毕后，剩余的
    "flex"名额才按原有优先级规则继续分配：A 侧剩余部分（仍是"主锚点必须
    优先"的既有语义）在前、B 侧剩余部分（仍按候选轮转顺序）在后，这部分
    维持 1.8.2 既有语义——字数预算不够时按原样跳过、不截断（不是任何
    候选的唯一证据，缺了也不影响"每候选至少一段"这个硬要求）。这不是
    简单地把 MAX_ENTRIES/MAX_CHARS 两个上限放大，两个常量原样不变，只是
    同一份预算内部的分配规则更细颗粒度。

    退化场景（不崩、不引入新失败模式，跟 1.8.1/1.8.2 同一纪律）：某个
    候选没有任何锚点证据时（理论上不应发生，候选正是靠 anchor 命中本集
    才入选的——见 _prep_pack_functional_candidate_names，候选集单一来源，
    1.8.4 回退了 1.8.3 曾短暂引入的"人物谱注册区间"乙类来源，见
    PREP_PACK_VERSION 上方 1.8.4 大注释），该候选保底跳过；主锚点整体为空
    时 A 侧保底与 flex 都退化为 0，B 侧可用满 MAX_ENTRIES 预算；两侧都为空
    时返回空列表，交由调用方按既有防御性分支处理。"""
    total_segments = len(segments)
    # 主锚点第一层：段号来自调用方传入的事件跨度集合，可能包含越界/非法
    # 值（防御性输入，不假设调用方一定传的是干净数据）——落在
    # [1, total_segments] 之外的一律丢弃；转 0-based 并按段号升序排序，
    # 截断顺序完全由段号本身决定，不依赖 set 的遍历顺序（确定性纪律）。
    event_span_indexes = sorted(
        {index - 1 for index in event_span_segments if 1 <= index <= total_segments},
    )
    event_span_index_set = set(event_span_indexes)
    label_text_indexes, anchor_pool_ordered, per_candidate_indexes = (
        _prep_pack_functional_candidate_anchor_pool(
            segments, label, candidate_anchor_texts, event_span_indexes, event_span_index_set,
        )
    )
    primary_indexes = event_span_indexes + label_text_indexes
    primary_index_set = event_span_index_set | set(label_text_indexes)

    # 每候选保底（1.8.3 核心改动，见本函数 docstring）：candidate_anchor_
    # texts 保序遍历，每个确有锚点证据的候选取自己最近的一段；与另一候选
    # 共享同一段时天然去重（该段同时满足两者的保底）。同时记录命中该段的
    # 具体锚点词（该候选的哪个规范名/别名字面命中了这段文本），供下面截断
    # 时定位核心句。
    guaranteed_b_indexes: list[int] = []
    guaranteed_b_anchor: dict[int, str] = {}
    guaranteed_b_seen: set[int] = set()
    for name in candidate_anchor_texts:
        indexes = per_candidate_indexes.get(name) or []
        if not indexes:
            continue
        pick = indexes[0]
        if pick in guaranteed_b_seen:
            continue
        guaranteed_b_seen.add(pick)
        guaranteed_b_indexes.append(pick)
        pick_text = segments[pick].text
        guaranteed_b_anchor[pick] = next(
            (form for form in candidate_anchor_texts[name] if form and form in pick_text), "",
        )
    overflow_b = [index for index in anchor_pool_ordered if index not in guaranteed_b_seen]

    # A 侧保底名额优先让位给每候选保底（见本函数 docstring）：先扣掉每
    # 候选保底已经用掉的条数名额，A 侧保底只取剩余名额、自身可用条数、
    # 既有 MIN_SIDE_ENTRIES 三者的较小值。
    remaining_slots = max(
        0, _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES - len(guaranteed_b_indexes),
    )
    reserve_a = min(
        _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES, len(primary_indexes), remaining_slots,
    )
    guaranteed_a, overflow_a = primary_indexes[:reserve_a], primary_indexes[reserve_a:]

    guaranteed_indexes = guaranteed_a + guaranteed_b_indexes
    flex_indexes = overflow_a + overflow_b

    selected: list[int] = []
    used_chars = 0
    rendered: dict[int, str] = {}

    # 保底层：一律收录，绝不因为字数超限被跳过（本轮修复的直接根因，见
    # 本函数 docstring）；单段超过 GUARANTEED_ENTRY_MAX_CHARS 时做确定性
    # 截断，不整段丢弃。
    for index in guaranteed_indexes:
        if len(selected) >= _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES:
            break
        if index in rendered:
            continue
        seg_text = segments[index].text
        # 候选自身锚点词优先于 label（1.8.4）：候选保底段现在可能同时落在
        # 事件跨度内（见 anchor_pool 的 docstring），此时截断核心句必须仍然
        # 围绕这个候选真正命中的锚点词（如"许师姐"），不能被"这段也在事件
        # 跨度里所以用 label 定位"覆盖掉——label 未必逐字出现在这段里，用它
        # 当锚点会退化成从头截断，可能把候选证据本身截没。
        anchor_hint = (
            guaranteed_b_anchor[index] if index in guaranteed_b_anchor
            else (label if index in primary_index_set else "")
        )
        text = _prep_pack_functional_candidate_truncate_segment(seg_text, anchor_hint)
        selected.append(index)
        rendered[index] = text
        used_chars += len(text)

    # flex 层：维持 1.8.2 既有语义——补充材料，不是任何候选的唯一证据，
    # 预算不够就跳过，不截断。
    for index in flex_indexes:
        if len(selected) >= _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES:
            break
        if index in rendered:
            continue
        seg_text = segments[index].text
        if selected and used_chars + len(seg_text) > _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS:
            continue
        selected.append(index)
        rendered[index] = seg_text
        used_chars += len(seg_text)

    selected.sort()
    return [
        {"segment_index": index + 1, "text": rendered[index]}
        for index in selected
    ]


class _PrepPackFunctionalCandidateVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 候选判别（见本节顶部大注释）：不是"标签是不是候选 A"的是非题，而是在
    # 候选集（本集出场的全部人物谱角色 + "都不是/无法确定"）里选一个。schema
    # 层面用 enum 收紧到 _prep_pack_functional_candidate_call 构造的候选集
    # （与段号 enum 同一写法，参照 app/portraits.py _current_identity_
    # schema() 给 evidence_ref 注入 enum 的写法）。
    selected_candidate: str
    # 钉证判据（见本节顶部大注释）：模型只需引用卷宗目录里某一条的段号，不
    # 要求逐字复述原文。schema 层面用 enum 把候选值限定为本次卷宗实际收录
    # 的段号集合，代码层面 _prep_pack_functional_candidate_pin_segment 再做
    # 一次结构性核验。
    supporting_segment_index: int
    # 可选的观测字段，供人工复核参考，不作为通过与否的判据。
    supporting_quote: str = ""


async def _prep_pack_functional_candidate_call(
    *, label: str, dossier: list[dict[str, Any]], candidates: list[str],
    episode_id: str, project_id: str | None,
) -> _PrepPackFunctionalCandidateVerdict:
    """唯一一次模型调用：只给卷宗原文与候选人名单，不点名"你猜是不是某个
    候选"——把"这个标签到底指候选里的哪一位"完全交给模型自己独立判别，
    与 app.stages._alias_verdict_call 同一范式（本文件独立实现，两个模块
    不互相导入内部函数）。

    候选集单一来源（1.8.4 回退，见 PREP_PACK_VERSION 上方 1.8.4 大注释）：
    ``candidates`` 只是"规范名或已确认别名在本集原文逐字出现"这一甲类
    判据的结果，不再有分区展示的乙类（人物谱注册区间覆盖本集但原文未
    点名）——那一类候选天然没有本集原文里的锚点段落，"钉证仍须钉住真实
    卷宗段落"这道保险对它们原理上不成立（卷宗里没有它的锚点段，模型只能
    钉在任意一段无关证据上，钉证因此只证明"这段话真实存在"，证明不了
    "这段话支持这个指代关系"），真实数据已经出现赵武刚（人物谱登记本集
    活跃，但原文一次都没提到他）被误判为"绿袍男子"的事故（method=
    candidate_verdict）。"""
    catalog = "\n\n".join(
        f"[段{item['segment_index']}] {item['text']}" for item in dossier
    )
    segment_indexes = [item["segment_index"] for item in dossier]
    candidate_options = [*candidates, _PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    prompt = f"""下面是本集原文中的段落（含前后语境，出现顺序不代表任何推断结论），
每段前面标了段号：
{catalog}

本集出场的人物谱角色候选（判别范围仅限这些人，不要引入候选之外的人）：
{candidate_list}

任务：仅依据以上原文段落本身，判断标签"{label}"最可能指候选中的哪一位本人。
- selected_candidate 必须从候选列表中选一个精确姓名，或者在证据不足以确定具体是谁时
  选"{_PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL}"；不要因为某个候选在段落里出现
  次数多就倾向选他，只依据原文是否真的能确定"{label}"说的就是他本人；
- supporting_segment_index 必须填上面某一段落标注的段号（取值只能是 {segment_indexes}
  之一），选你得出这个结论最主要依据的那一段，不要凭空填一个没在目录里出现的段号；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，不要求逐字
  精确，留空也可以。
只输出符合 Schema 的 JSON。"""
    schema = _PrepPackFunctionalCandidateVerdict.model_json_schema()
    # 参照 app/portraits.py _current_identity_schema() 给 evidence_ref 注入
    # enum 的写法：候选段号、候选人名单都收紧到本次实际可用的集合，模型在
    # 协议层面就选不出卷宗外的段号或候选集之外的人；真正生效的核验仍在
    # _prep_pack_functional_candidate_pin_segment 与
    # _prep_pack_resolve_functional_extra_candidate 里做代码侧结构校验
    # （provider 对 enum 的遵守不是可证明保证）。
    schema["properties"]["supporting_segment_index"]["enum"] = segment_indexes
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    operation_id = (
        f"episode_prep_pack:{episode_id}:functional_extra_candidate_verdict:"
        + evidence_repository.content_hash({
            "label": label, "candidates": candidates,
            "dossier": [item["segment_index"] for item in dossier],
        })
    )
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_PrepPackFunctionalCandidateVerdict,
        validate=None,
        operation_id=operation_id,
        max_tokens=500,
        # 低温：这道闸的语义判断要稳定——同一份卷宗重跑不该一次选中一次
        # 不确定（跟 stages.py 同一考量）。
        temperature=0.0,
        format_retry_limit=1,
        semantic_retry_limit=1,
        output_schema=schema,
        call_meta={
            "stage": "未解析角色候选判别",
            "stage_key": "episode_prep_pack_functional_extra_candidate_verdict",
            "call_role": "stage_generate",
            "call_role_label": "未解析角色候选判别",
            "expected_json": True,
            "project_id": project_id,
            "episode_id": episode_id,
            "label": label,
            "candidates": candidates,
        },
    )


def _prep_pack_functional_candidate_pin_segment(
    dossier: list[dict[str, Any]], segment_index: Any,
) -> dict[str, Any] | None:
    """钉证：结构性校验，不要求模型逐字复述原文（见本节顶部大注释）。模型
    只需要在响应里选一个段号，这里核对该段号是否落在本次卷宗实际收录的
    段号集合内——命中即视为钉证通过，因为卷宗内容本身就是代码检索出的
    真实原文，模型选中某一条不存在"编造"或"转录出错"的空间。非法输入
    （不是整数、或不在集合内）一律返回 None，交由调用方按无效裁决拒绝。"""
    try:
        target = int(segment_index)
    except (TypeError, ValueError):
        return None
    for item in dossier:
        if item["segment_index"] == target:
            return item
    return None


async def _prep_pack_resolve_functional_extra_candidate(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    label: str, source_text: str, segments: list[SourceSegment], bible: Bible,
    character_mentions: list[dict[str, Any]],
) -> dict[str, Any]:
    """未解析标签的候选判别入口：候选集为空、卷宗为空、模型选"都不是/
    无法确定"、选中值不在候选集内（协议层已经不可能，这里仍防御性核验）、
    段号钉证失败、候选在本集没有可用定妆照、或这次改名会与跨集别名注册表
    冲突（复用既有 _prep_pack_cross_episode_alias_conflict，同一套"不确定
    不绑"纪律），一律 ``resolved=False``——调用方维持原行为，标签留在
    skip_character_names 正常落 functional_extras，绝不猜。

    ``character_mentions``（2.0.0，取代 1.8.1 引入的 ``events`` 参数——
    调用方 _resolve_assets 自己的扁平提及列表原样传入）：用于
    _prep_pack_functional_candidate_label_segments 算出这个标签自己申报
    （且已逐段核验）的段落，作为卷宗检索的主锚点——见该函数与
    _prep_pack_functional_candidate_dossier 的完整根因说明（标签字面定位
    在"标签是模型转述短语"时会打空，提及自报的段号不依赖字面命中）。

    返回值恒为 dict（1.10.0 起不再用 ``None`` 表示失败，见 PREP_PACK_
    VERSION 上方大注释"顺带修一处可观测性缺口"一节）：``resolved`` 是否
    真的绑定成功；``attempted`` 是否真的发起过一次候选判别模型调用（候选集
    非空且卷宗非空才会调用模型——调用方据此区分"从未获得候选判别机会"与
    "候选判别跑过但没选中"两种此前坍缩成同一个 method="discovery" 值、
    只能翻 provider_calls 反推的情形，见 _pass 对 functional_extras 的
    provenance.candidate_verdict_attempted 处理）。``resolved=True`` 时
    额外带 ``canonical_name``/``segment_index``/``text``：``canonical_name``
    供调用方写入 character_rename（重新走既有的具名解析路线，自然带出正确
    的 portrait_id/identity_id/visual_entity_id）；``segment_index``/
    ``text`` 是钉证命中的卷宗证据，供调用方写入 provenance 锚点（``text``
    是代码检索出的真实原文，不是模型转录，天然满足自校验的逐字命中要求）。

    候选集单一来源（1.8.4 回退，见 PREP_PACK_VERSION 上方 1.8.4 大注释）：
    ``candidates``＝规范名或已确认别名在本集原文逐字出现的人物谱角色（见
    _prep_pack_functional_candidate_names）。1.8.3 曾短暂扩展为"逐字命中∪
    人物谱注册区间覆盖本集"两类并集，已回退——乙类候选没有本集原文里的
    锚点段落，"钉证仍须钉住真实卷宗段落"这道保险对它们原理上不成立（卷宗
    里根本不存在它的锚点段，模型只能钉在任意一段无关证据上，钉证只能证明
    "这段话真实存在"，证明不了"这段话支持这个指代关系"），真实数据已经
    出现赵武刚（人物谱登记本集活跃，原文一次都没提到他）被误判为"绿袍
    男子"的事故（method=candidate_verdict）。"""
    not_attempted = {"resolved": False, "attempted": False}
    attempted_no_bind = {"resolved": False, "attempted": True}
    roster = _prep_pack_functional_candidate_roster(bible)
    candidates = _prep_pack_functional_candidate_names(source_text, roster)
    if not candidates:
        return not_attempted
    # 1.8.2：改传"候选名 -> 该候选自己的锚点文本"分组字典（而非拍平成一个
    # 集合），供 _prep_pack_functional_candidate_dossier 的 B 侧按候选做
    # 公平轮转合并，见该函数与 _prep_pack_functional_candidate_anchor_pool
    # 的完整说明。字典按 candidates 既有确定性顺序构造（保序，见
    # _prep_pack_functional_candidate_names 的 roster 保序说明）。
    candidate_anchor_texts = {name: roster.get(name, []) for name in candidates}
    event_span_segments = _prep_pack_functional_candidate_label_segments(
        character_mentions, label,
    )
    dossier = _prep_pack_functional_candidate_dossier(
        segments, label, candidate_anchor_texts, event_span_segments,
    )
    if not dossier:
        return not_attempted
    response = await _prep_pack_functional_candidate_call(
        label=label, dossier=dossier, candidates=candidates,
        episode_id=episode_id, project_id=project_id,
    )
    if response.selected_candidate not in candidates:
        return attempted_no_bind
    pinned = _prep_pack_functional_candidate_pin_segment(
        dossier, response.supporting_segment_index,
    )
    if pinned is None:
        return attempted_no_bind
    canonical_name = response.selected_candidate
    if not _resolve_portrait_id(conn, project_id, canonical_name, episode_no):
        return attempted_no_bind
    conflicting_name = _prep_pack_cross_episode_alias_conflict(
        conn, project_id, episode_id,
        alias=label, canonical_name=canonical_name, bible=bible,
    )
    if conflicting_name:
        return attempted_no_bind
    return {
        "resolved": True, "attempted": True,
        "canonical_name": canonical_name,
        "segment_index": pinned["segment_index"], "text": pinned["text"],
    }


async def _resolve_assets(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    source_text: str,
    character_mentions: list[dict[str, Any]],
    scene_mentions: list[dict[str, Any]],
    prop_mentions: list[dict[str, Any]],
    run_id: str | None,
    appellation_resolutions: list[dict[str, Any]] | None = None,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
    list[str], dict[str, int],
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
]:
    """Resolve every character/scene mention (invariant③); merge props.

    2.0.0: ``events`` (grouped-by-event) became three flat mention lists
    (``character_mentions``/``scene_mentions``/``prop_mentions`` -- see
    PREP_PACK_VERSION's 2.0.0 note). Every mention already carries its own
    ``segment_indexes`` (structurally chunk-scope-verified by
    _prep_pack_gate_segment_indexes before it ever reaches this function --
    NOT literal-text-verified, that stays this function's own job via the
    existing per-method gates below, unchanged from pre-2.0.0) --
    what used to be ``event_ids`` bookkeeping on each manifest entry is now
    the union of every contributing mention's ``segment_indexes``.

    Every character/scene mention goes through the same resolution attempt.
    The old chunk-extraction model's own ``is_background_extra`` guess (a
    *different*, earlier model call that never looked at the bible) is gone
    from the 2.0.0 schema entirely -- treating it as an exemption from
    resolution was exactly the bug a real EP2 run surfaced:
    "小胖子" (7 on-screen appearances, real dialogue) was tagged
    is_background_extra=true by the chunk extractor and, under the previous
    build of this function, skipped before ever reaching resolution -- even
    though "小胖子" is 李富贵, already in the bible with a portrait. The
    correct flow (per invariant③: resolve to an existing asset OR a
    deterministic generic-extra class, never a third "silently absent"
    option) is: direct name match against character_portraits/scene_references
    first (cheap, no model call) -> unresolved mentions (whatever the
    extractor guessed about them) go through the discovery/disambiguation
    mechanism inherited from the heavy pipeline (_discover_new_characters /
    _discover_new_scenes below) -> only discovery explicitly failing *on that
    specific name* (_discovery_errored_names) may still hard-block it. Pass 1
    is direct-match only; pass 2 re-resolves after discovery using whatever it
    newly registered (new cards+portraits, a known-alias -> canonical-name
    rename e.g. "小胖子" -> "李富贵", or a functional/no-asset disposition).

    A resolved character's manifest entry carries an ``aliases`` list: the
    distinct raw mention strings (e.g. ["小胖子"]) that resolved to it via a
    rename, for P1 storyboard prompts to use.

    Second real-run finding (EP13, coordinator-reviewed): app.portraits'
    identity discovery does its own independent read of the source text and
    phrases/scopes its own candidates differently from prep_pack's chunk
    extraction -- discovery resolved "外宗弟子" as functional_identity while
    the published chunk extraction said "一名外宗弟子" for what is plainly the
    same one-off crowd concept; several other occupation-title mentions
    ("养丹坊掌柜", "宝阁执事", "围观弟子") got no matching disposition at all
    by exact string, even though discovery ran cleanly (its own ``errors``
    was empty) and *did* resolve the real new character "曹阳" (a portrait was
    generated) in the very same call. Per app.portraits' own long-standing
    rule (portraits.py:1727,7340 -- an unconfirmed-real-name one-off keeps its
    own source label and gets a typed functional identity, never silently
    dropped nor renamed to something generic), a mention that discovery
    neither resolved nor explicitly failed on defaults to a functional extra
    under its own raw text -- not a card, not a portrait, not a gate error.
    The only thing that still hard-blocks after discovery runs is
    _discovery_errored_names: discovery said something concrete about that
    *specific* name (a confirmed real identity whose card generation itself
    failed, or an exception) -- "消歧和发现都没能给出任何归类结论" is the one
    state this function will not paper over.

    Episodes where pass 1 already resolves every mention by exact name never
    call discovery at all (``stats``' counters stay at 0) -- but note this is
    now a narrower case than "no new characters": any mention that is not an
    exact known name (a genuine one-off extra with no real name, not just a
    new named character) also routes through discovery so it can receive a
    real disposition instead of being assumed one way or the other.

    ``appellation_resolutions`` (2.0.1 bug fix, see the note above
    ``_prep_pack_build_appellation_map``): an optional out-parameter -- when
    the caller passes a list, this function extends it in place with one
    record per successfully-resolved character mention (``raw_mention``,
    ``segment_indexes``, ``identity_id``, ``canonical_appellation``), read
    directly off the same manifest entry each mention just resolved to
    inside ``_pass()``. This is deliberately NOT folded into this function's
    own return tuple: that tuple is asserted against verbatim (fixed arity,
    no ``*_``) by dozens of existing tests, and this is a pure additive
    capability for one caller (``_generate_prep_pack_once``'s appellation_map
    construction) -- an optional, ignore-by-default parameter keeps every
    existing caller's contract untouched. Defaults to ``None`` (no
    recording), which is what every call site except
    ``_generate_prep_pack_once`` uses.
    """
    stats = {"character_discovery_calls": 0, "scene_discovery_calls": 0}
    # 场景别名锚定（1.5.1，task①）：一次性加载，供 _pass 内的场景解析复用。
    bible = _load_project_bible(conn, project_id)
    segments = index_source_segments(source_text)
    scene_alias_anchors: list[dict[str, Any]] = []
    # 跨集别名一致性（1.5.2，task②）：见 _prep_pack_cross_episode_alias_conflict
    # 上方注释。
    rejected_alias_conflicts: list[dict[str, Any]] = []
    # 身份绑定审判程序的进程内判决缓存（第29轮，见 _prep_pack_verify_
    # true_name_hypothesis 上方完整说明第 4 点）：同一次 _resolve_assets
    # 调用内，同一个 (subject_kind, alias, suspected_true_name) 组合只真正
    # 发起一次模型裁决调用，两遍 _pass()（pass1/pass2）与同一遍内的多个
    # 事件共用同一份缓存；角色分支与场景分支也共用这同一个字典对象，键里
    # 的 subject_kind 就是两域的隔离手段（独立评审发现的 minor：旧版键只有
    # (alias, suspected_true_name)，角色域与场景域撞名会复用错误域的裁决）。
    true_name_verdict_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def _pass(
        skip_character_names: set[str],
        character_rename: dict[str, str],
        scene_rename: dict[str, str],
        non_person_names: set[str] = frozenset(),
        *,
        newly_added_character_names: frozenset[str] = frozenset(),
        newly_added_scene_names: frozenset[str] = frozenset(),
        resolution_evidence_by_label: dict[str, str] | None = None,
        candidate_verdict_pins: dict[str, dict[str, Any]] | None = None,
        functional_candidate_attempted_names: frozenset[str] = frozenset(),
    ) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], list[str], list[str], list[str],
        list[dict[str, Any]], list[dict[str, Any]],
    ]:
        resolution_evidence_by_label = resolution_evidence_by_label or {}
        # 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大注释、
        # _prep_pack_resolve_functional_extra_candidate 的完整说明）：
        # label -> 钉证命中的卷宗记录（{"segment_index", "text"}），供下面
        # method 判定分支单独标记 "candidate_verdict"、并直接复用钉证段落
        # 本身（代码检索出的真实原文）作为 anchor_phrase，不依赖模型转录。
        candidate_verdict_pins = candidate_verdict_pins or {}
        # functional_candidate_attempted_names（1.10.0，缺陷 A 顺带修复的
        # 可观测性缺口，见 PREP_PACK_VERSION 上方大注释）：这批标签虽然没被
        # 候选判别选中（否则会在 candidate_verdict_pins 里），但确实发起过
        # 一次模型调用——供 method="discovery" 的 functional_extras 条目
        # 标注 candidate_verdict_attempted=True，跟"候选集/卷宗为空，从未
        # 获得候选判别机会"（此参数不含该标签，provenance 里这个字段留空）
        # 区分开。
        characters: dict[str, dict[str, Any]] = {}
        scenes: dict[str, dict[str, Any]] = {}
        functional_extras: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        unresolved_characters: list[str] = []
        unresolved_scenes: list[str] = []
        # appellation_map 真源（2.0.1 bug fix，见 _prep_pack_build_
        # appellation_map 上方大注释）：每条真正走到 characters.setdefault
        # 那一步的角色提及，在这里原地记一行它自己刚刚解析出的结论
        # （identity_id/canonical_appellation 直接取自那个 entry，不是
        # 事后另算）。跟 characters/scenes 同一条"pass2 整体替换 pass1"
        # 规则——这个列表每次 _pass() 调用都是全新的，不跨两遍累加。
        character_appellation_rows: list[dict[str, Any]] = []
        # 1.5.0 观测记录：每条模型申报的 suspected_true_name 假设最终是被核验
        # 采信还是拒绝，都记一条（不影响门禁本身，见函数上方注释）。
        true_name_hints: list[dict[str, Any]] = []
        # K 并发预热（任务②，见 _prep_pack_collect_true_name_verification_
        # requests 上方大注释）：先把这一遍会用到的全部 (subject_kind, alias,
        # suspected_true_name) 三元组去重收集齐，减掉 true_name_verdict_cache
        # 里已经有的（pass2 复用 pass1 缓存，语义不变），剩下的一次性并发
        # 核验，把结果写进同一份 true_name_verdict_cache。下面主循环逐条
        # await 的既有调用不动，命中的是已经写热的缓存。
        pending_true_name_requests = [
            key for key in _prep_pack_collect_true_name_verification_requests(
                character_mentions, scene_mentions, character_rename, scene_rename,
            )
            if key not in true_name_verdict_cache
        ]
        if pending_true_name_requests:
            await _prep_pack_gather_concurrent([
                _prep_pack_verify_true_name_hypothesis(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text,
                    alias=alias, suspected_true_name=suspected_true_name,
                    subject_kind=subject_kind, bible=bible,
                    resolve_fn=(
                        _resolve_portrait_id if subject_kind == "character"
                        else _resolve_scene_reference_id
                    ),
                    run_id=run_id, verdict_cache=true_name_verdict_cache,
                )
                for subject_kind, alias, suspected_true_name in pending_true_name_requests
            ])
        for mention in character_mentions:
            name = str(mention["display_name"] or "").strip()
            if not name:
                errors.append("存在空白角色名")
                continue
            mention_segment_indexes = sorted(
                {int(index) for index in mention.get("segment_indexes") or []}
            )
            resolved_name = character_rename.get(name, name)
            # provenance（1.6.0）：记录这次改名到底走的是哪条路径——
            # via_alias_registry 单独标记 task① 的注册表命中（跟
            # character_rename/suspected_true_name 都不是同一件事，
            # method 要区分 alias vs resolution）。
            via_alias_registry = False
            # 第30轮 RCA（真实 EP2/6/8 回归：resolution_forward 空
            # forward_chapter_label/anchor_phrase）：这里曾经用
            # "resolved_name == suspected_true_name" 反推"是否经过真名
            # 核验"，但 resolved_name 也可能通过 character_rename（角色
            # 发现/消歧，完全独立的另一条路径）恰好也算出同一个真名——
            # 两条路径殊途同归到同一个名字，不代表这次核验真的跑过；一旦
            # 走的是 character_rename 这条路，下面 if 判据里的
            # suspected_true_name != resolved_name 从一开始就是 False，
            # 核验分支被跳过，true_name_pinned_quote/chapter_idx 从未被
            # 赋值，停在初始空值——但后面的 method 判定分支当年只看
            # "resolved_name == suspected_true_name" 这个结果状态，认定
            # 它是核验通过，于是带着两个空值直接产出 resolution_forward。
            # 跟场景侧的 via_suspected_true_name 同一个修法：只在真正跑
            # 过核验且 accepted 时才置位，用这个专用布尔判定，不再用状态
            # 反推过程。
            via_suspected_true_name = False
            true_name_dual_anchor: bool | None = None
            suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
            true_name_pinned_quote = ""
            true_name_pinned_chapter_idx: int | None = None
            if suspected_true_name and suspected_true_name != resolved_name:
                verification = await _prep_pack_verify_true_name_hypothesis(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text,
                    alias=name, suspected_true_name=suspected_true_name,
                    subject_kind="character", bible=bible,
                    resolve_fn=_resolve_portrait_id, run_id=run_id,
                    verdict_cache=true_name_verdict_cache,
                )
                if verification["accepted"]:
                    resolved_name = suspected_true_name
                    via_suspected_true_name = True
                    true_name_pinned_quote = verification["pinned_quote"]
                    true_name_pinned_chapter_idx = verification["pinned_chapter_idx"]
                    true_name_dual_anchor = verification["dual_anchor"]
                    true_name_hints.append({
                        "kind": "character", "mention": name,
                        "suspected_true_name": suspected_true_name, "status": "accepted",
                        "dual_anchor": true_name_dual_anchor,
                    })
                else:
                    true_name_hints.append({
                        "kind": "character", "mention": name,
                        "suspected_true_name": suspected_true_name, "status": "rejected",
                        "reason": verification["reason"],
                    })
            # 缺陷 B 修复（1.10.0，见 PREP_PACK_VERSION 上方大注释的完整
            # 根因）：skip_character_names 短路必须排在 suspected_true_
            # name 核验之后、且核验通过时不得短路。角色发现
            # （_discover_new_characters）是本函数之外一次独立的全集
            # 重新通读，产出的 source_label 只是字符串，可能跟本提及的
            # 原始 name 恰好撞同一个字面量却指向完全不相关的判定（真实
            # EP5 事故：pass1 已核验通过"许姓女子"→"许清"、钉证成功、
            # accepted=True；同一轮 pass2 因角色发现独立判定另一处
            # "许姓女子"字面为功能性群演，把这个字符串加进了
            # skip_character_names——旧代码里这个 continue 排在核验之前
            # 执行，短路掉了本该重新核验（或至少复用 true_name_verdict_
            # cache 里 pass1 已经算好的 accepted 判决）的机会，pass1 的
            # 结论被静默作废，4 条提及从未进 unresolved_characters，也就
            # 从未有机会触发以"许姓女子"为标的的候选判别）。已核验通过的
            # 信号必须优先于"角色发现独立通读凑巧撞出同名功能簇"这个更
            # 弱的兜底信号，故 continue 的条件追加
            # `and not via_suspected_true_name`；缓存复用则由上面
            # _prep_pack_verify_true_name_hypothesis 内部的
            # true_name_verdict_cache 命中自动生效（pass1 已经缓存过的
            # (subject_kind, alias, suspected_true_name) 组合，pass2
            # 重新执行到这里时直接命中缓存，不会真的再发一次模型调用）。
            if name in skip_character_names and not via_suspected_true_name:
                if name not in non_person_names:
                    # provenance（1.6.0）：这批 functional_extras 全部
                    # 来自本轮的角色发现（要么发现明确判定 skip，要么是
                    # 发现跑过之后"既没归类也没报错"的默认兜底——两种
                    # 情形都必然经过了发现调用，method 统一记 discovery，
                    # 锚点用触发发现的原始描述 name 自身在本集原文里的
                    # 出现位置，找不到就是空锚点（见 _prep_pack_local_
                    # text_anchor）。
                    extra_anchor_segments, extra_anchor_phrase = (
                        _prep_pack_local_text_anchor(segments, [name])
                    )
                    # visual_entity_id（1.7.0，层三）：未具名/群演分支，
                    # 种子只取 source_label（这批群演自己的原文标签，即
                    # dict 键 name）+ scope_qualifier（prep_pack 自己的
                    # 事件链抽取模型不产出这个字段，留空——跟 K 决议提示
                    # 词规则8"留空即唯一指向一个人"同一语义，不是遗漏）。
                    # 同一原文标签跨集重复出现时这里恒定，是"未具名角色
                    # 也有跨集稳定视觉实体"这条设计判据的落地点。
                    # label 本身不替换（不同于角色侧 display_appellation）：
                    # 见 _prep_pack_provenance 上方 2.0.0 说明——label 是
                    # 这个群演唯一的展示/连接键。label_literal（1.11.0）
                    # 已在 2.0.0 撤下（纯范围收窄，不是结构性恒真——见
                    # _prep_pack_gate_segment_indexes 上方说明）。
                    extra = functional_extras.setdefault(name, {
                        "segment_indexes": [],
                        "visual_entity_id": visual_entity_id_for_resolution({
                            "source_label": name, "scope_qualifier": "",
                        }),
                        "provenance": _prep_pack_provenance(
                            "discovery", extra_anchor_segments, extra_anchor_phrase,
                            candidate_verdict_attempted=(
                                name in functional_candidate_attempted_names
                            ),
                        ),
                    })
                    extra["segment_indexes"] = sorted(
                        set(extra["segment_indexes"]) | set(mention_segment_indexes)
                    )
                continue
            # 跨集别名一致性（task②，见 _prep_pack_cross_episode_alias_
            # conflict 上方注释，真实 EP3 回归："小胖子"曾被误改绑到项目内
            # 另一个已发布分集绑定的"王有材"，本集完全没有"王有材"的文本
            # 依据）：任何改名（不管来自身份消歧的 character_rename 还是
            # 上面刚核验通过的 suspected_true_name）落地前，检查这个称谓
            # 是否已经在项目内被绑定给了别人——是就拒绝这次改绑，回退到
            # name 自己的常规解析路线（不静默接受任何一边），并记入
            # rejected_alias_conflicts（观测）。
            if resolved_name != name:
                conflicting_name = _prep_pack_cross_episode_alias_conflict(
                    conn, project_id, episode_id,
                    alias=name, canonical_name=resolved_name, bible=bible,
                )
                if conflicting_name:
                    rejected_alias_conflicts.append({
                        "mention": name,
                        "attempted_canonical_name": resolved_name,
                        "conflicting_canonical_name": conflicting_name,
                    })
                    resolved_name = name
            portrait_id = _resolve_portrait_id(conn, project_id, resolved_name, episode_no)
            if not portrait_id:
                # 角色别名注册表读侧（task①，真实第24轮 EP3 回归
                # ERR-20260824-d0830a，见 _prep_pack_lookup_character_
                # alias_canonical_name 上方注释）：裸精确匹配失败后，查
                # 项目内已发布分集的别名注册表——这个称谓是否已经在别的
                # 分集里被确立过归属（"小胖子"经 EP2 一次消歧确立即归
                # 李富贵，EP3+ 应直接复用，不必每集重新赌一次消歧模型
                # 调用）。复用 _prep_pack_cross_episode_alias_conflict
                # 同一套冲突拒绝逻辑守住多目标——命中但被判定跟别的分集
                # 已确立的归属冲突就不绑定，回退到常规解析路线（回炉
                # discovery）。
                aliased_name = _prep_pack_lookup_character_alias_canonical_name(
                    conn, project_id, episode_id, name, bible=bible,
                )
                if aliased_name and aliased_name != resolved_name:
                    conflicting_name = _prep_pack_cross_episode_alias_conflict(
                        conn, project_id, episode_id,
                        alias=name, canonical_name=aliased_name, bible=bible,
                    )
                    if conflicting_name:
                        rejected_alias_conflicts.append({
                            "mention": name,
                            "attempted_canonical_name": aliased_name,
                            "conflicting_canonical_name": conflicting_name,
                        })
                    else:
                        resolved_name = aliased_name
                        via_alias_registry = True
                        portrait_id = _resolve_portrait_id(
                            conn, project_id, resolved_name, episode_no,
                        )
            if not portrait_id:
                errors.append(
                    f"角色「{name}」（段 {mention_segment_indexes}）未解析到已有 "
                    "portrait_id，身份消歧也未能将其归类为已有角色或确定性群演"
                )
                if name not in unresolved_characters:
                    unresolved_characters.append(name)
                continue
            # 称谓证据闸语义精化（1.5.x task②，真实第24轮 EP3 回归
            # ERR-20260824-d0830a）：「穿杂役衫的魁梧大汉」经消歧正确解析到
            # 赵武刚，却被本闸拦下——原文对这个人只有分散的描述性叙述，模型
            # 综合出的这个名词短语天然不可能逐字出现在原文里，这不是幻觉
            # 归属的形状。resolved_name != name（came_via_resolution）精确
            # 区分两种情形，用的正是已经在算的同一个信号，不需要另外记状态：
            #   - 裸直接命中（resolved_name == name，没有经过 character_
            #     rename/suspected_true_name/别名注册表任何一条改名路径）：
            #     反幻觉主防线不动，称谓本身仍必须逐字出现在原文——这是
            #     真实 EP5 丹鬼案要拦的唯一形状（模型直接写下一个谱内已有
            #     名字，未经任何消歧）。
            #   - 经消歧/发现/别名注册表解析绑定（resolved_name != name）：
            #     合法性由那条解析路径自身的证据链承担（身份消歧模型看的
            #     就是本集原文；suspected_true_name 有自己独立的逐字/前瞻
            #     窗口核验；别名注册表命中继承的是别的分集当时已经过同一套
            #     证据核验的结论）——不再重复要求 name 本身逐字出现。
            came_via_resolution = resolved_name != name
            literal_evidence = _prep_pack_mention_has_text_evidence(name, source_text)
            if not came_via_resolution and not literal_evidence:
                errors.append(
                    f"角色「{name}」（段 {mention_segment_indexes}）解析到已有角色"
                    f"「{resolved_name}」（portrait_id={portrait_id}），但称谓「{name}」"
                    "未逐字出现在本集原文中，缺少称谓证据，门禁具名拦截"
                )
                continue
            # provenance method（1.6.0，第25轮收口）：discovery（本次
            # 新建卡）优先于其它任何改名信号判定——即便新建卡的同时也发生
            # 了改名（罕见但可能：消歧把"大汉"判成新建角色"赵武刚"），
            # "这是一张这次才出现的新卡"是更具体、更有信息量的事实。
            # 其次是 alias（task① 注册表命中）、resolution（消歧/真名
            # 核验改名）、最后是 direct（裸命中，未经任何改名）。
            forward_chapter_label = ""
            if resolved_name in newly_added_character_names:
                method = "discovery"
                anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
                    segments, [name],
                )
            elif via_alias_registry:
                method = "alias"
                anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
                    segments, [name],
                )
            elif via_suspected_true_name:
                # 身份绑定审判程序核验通过（第29轮，见
                # _prep_pack_verify_true_name_hypothesis 上方完整
                # 说明）：钉住的支撑句（true_name_pinned_quote）如果
                # 真的落在本集自己的段落里，就是最贴切的本地锚点
                # （它已经在裁决时被模型独立确认"确定是同一人"，比
                # 泛泛的候选搜索更有信息量）；如果钉住的支撑句来自
                # 全书别的章节（本集原文里找不到这句话），说明这条
                # 绑定的真实依据不在本集，method 单独标记
                # "resolution_forward"，空锚（anchor_segments）豁免
                # 合法，但 anchor_phrase 仍然记这句被钉住的支撑句本身
                # （第30轮 RCA 修正：过去这里误写成空字符串——anchor_
                # phrase 是"引用的是哪句话"，anchor_segments 才是"这句
                # 话在不在本集本地"，两者不是一回事，空 anchor_segments
                # 不代表 anchor_phrase 也该是空的），连同裁决真正引用的
                # 章节号一并写进 provenance，供审计核对。
                local_index = _prep_pack_first_evidence_segment(
                    segments, true_name_pinned_quote,
                )
                if local_index is not None:
                    method = "resolution"
                    anchor_segments = [local_index]
                    anchor_phrase = true_name_pinned_quote
                else:
                    method = "resolution_forward"
                    anchor_segments, anchor_phrase = [], true_name_pinned_quote
                    if true_name_pinned_chapter_idx is not None:
                        forward_chapter_label = f"第 {true_name_pinned_chapter_idx} 章"
            elif name in candidate_verdict_pins:
                # 未解析角色标签候选判别命中（1.8.0，见 PREP_PACK_
                # VERSION 上方大注释、_prep_pack_resolve_functional_
                # extra_candidate 的完整说明）：method 单独标记，不
                # 复用泛化的 "resolution"——那个标签专指 discovery 自身
                # 消歧结果（resolution_evidence_by_label 的来源），这里
                # 走的是另一条独立证据链：本函数内代码检索卷宗 → 候选
                # 选择题 → 段号钉证，provenance 要如实标注两者的区别，
                # 供审计区分走哪条核验路径复核。anchor_phrase 直接取
                # 钉证命中的卷宗段落原文本身（代码检索出的真实原文，
                # 不是模型转录），天然满足自校验的逐字命中要求。这个
                # elif 分支必须排在 via_suspected_true_name 之后、
                # came_via_resolution 之前——同一提及若恰好也核验通过
                # 了 suspected_true_name，那条更具体的证据链优先（elif
                # 链短路，不会执行到这里）。
                method = "candidate_verdict"
                pin = candidate_verdict_pins[name]
                anchor_segments = [pin["segment_index"]]
                anchor_phrase = pin["text"]
            elif came_via_resolution:
                method = "resolution"
                anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
                    segments,
                    [resolution_evidence_by_label.get(name, ""), suspected_true_name, name],
                )
            else:
                method = "direct"
                anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
                    segments, [name],
                )
            # visual_entity_id / display_appellation（1.7.0，层三，见设计
            # 文档 §4.3）：characters[] 条目永远是已解析到 portrait_id 的
            # 具名角色（1.3.0 冻结的既有含义），resolution="future_identity"
            # + canonical_name=resolved_name 走 visual_entity_id_for_
            # resolution 的具名分支，恒等于 f"bible:{resolved_name}"——跟
            # identity_id 同格式，是刻意的（设计文档 §4.2"零迁移成本"）。
            # display_appellation 取这个 portrait_id 在本集第一次出现时的
            # 原始提及文本 name（画面取图看 visual_entity_id，字幕/称呼看
            # display_appellation，两者分离——本集只说本集措辞，不提前
            # 剧透 display_name 这个全局规范名）。
            # 标签接地（1.11.0 引入，1.11.1 撤回替换手段，2.0.0 撤下整个
            # label_literal 字段——纯范围收窄，不是结构性恒真，见
            # PREP_PACK_VERSION 上方 2.0.0 大注释与 _prep_pack_gate_
            # segment_indexes 上方说明）：display_appellation 永远保持
            # name（不替换，跟 1.11.1 撤回后的最终结论一致）：唯一可能的
            # 确定性替换来源是 anchor_phrase，但 anchor_phrase 是"钉证命中
            # 的证据段落"，不是"称谓"，真实 EP1 数据坐实二者不可互换。
            display_appellation = name
            entry = characters.setdefault(portrait_id, {
                "identity_id": f"bible:{resolved_name}",
                "display_name": resolved_name,
                "portrait_id": portrait_id,
                "segment_indexes": [],
                "aliases": [],
                "visual_entity_id": visual_entity_id_for_resolution({
                    "resolution": "future_identity",
                    "canonical_name": resolved_name,
                }),
                "display_appellation": display_appellation,
                "provenance": _prep_pack_provenance(
                    method, anchor_segments, anchor_phrase,
                    forward_chapter_label=forward_chapter_label,
                    dual_anchor=(true_name_dual_anchor if via_suspected_true_name else None),
                ),
            })
            entry["segment_indexes"] = sorted(
                set(entry["segment_indexes"]) | set(mention_segment_indexes)
            )
            # appellation_map 真源（2.0.1 bug fix）：这条提及已经真正走到
            # 这里——有 portrait_id、通过了称谓证据闸——就是一条真实的
            # "模糊称谓 -> 身份"结论，读的是 entry 自己（跟 asset_manifest.
            # characters[] 发布出去的是同一个字典对象）刚定下的
            # identity_id/display_name，不是另算一遍。这一行故意不看
            # aliases（下面几行）：aliases 的字面证据门槛是为了保护跨集
            # 别名注册表不被合成标签污染，是另一个维度的判据，"能不能安全
            # 进注册表"不等于"这条提及有没有解析出身份"——把 aliases 当成
            # 后者的真源用，正是"穿杂役衫的魁梧大汉"这类合成描述提及在旧
            # 实现里从 appellation_map 里静默消失的根因。
            character_appellation_rows.append({
                "raw_mention": name,
                "segment_indexes": list(mention_segment_indexes),
                "identity_id": entry["identity_id"],
                "canonical_appellation": entry["display_name"],
            })
            # 别名注册仍只登记逐字出现于原文的称谓（task②，见上方门禁
            # 注释）：组合/综合描述短语（"穿杂役衫的魁梧大汉"）合法通过了
            # 门禁，但绝不能进别名库——别名注册表是 task① 直接信任的读侧
            # 数据源，一旦被模型综合出的合成词污染，将来会被当成"这就是
            # 原文真实用过的称呼"重新播种给别的分集。
            if came_via_resolution and literal_evidence and name not in entry["aliases"]:
                entry["aliases"].append(name)
        for mention in scene_mentions:
            name = str(mention["display_name"] or "").strip()
            if not name:
                errors.append("存在空白场景名")
                continue
            mention_segment_indexes = sorted(
                {int(index) for index in mention.get("segment_indexes") or []}
            )
            # 2.0.2：这条提及自己申报的逐字引文（见 _ModelSceneMention.quote
            # 与 PREP_PACK_VERSION 上方 2.0.2 大注释）——resolution/discovery
            # 锚点候选表下面会用到，独立于 name/canonical_scene_name 本身。
            scene_quote = str(mention.get("quote") or "").strip()
            resolved_via_discovery = name in scene_rename
            resolved_name = scene_rename.get(name, name)
            via_suspected_true_name = False
            true_name_dual_anchor: bool | None = None
            true_name_pinned_quote = ""
            true_name_pinned_chapter_idx: int | None = None
            suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
            if (
                suspected_true_name
                and suspected_true_name != resolved_name
                and not resolved_via_discovery
            ):
                verification = await _prep_pack_verify_true_name_hypothesis(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text,
                    alias=name, suspected_true_name=suspected_true_name,
                    subject_kind="scene", bible=bible,
                    resolve_fn=_resolve_scene_reference_id, run_id=run_id,
                    verdict_cache=true_name_verdict_cache,
                )
                if verification["accepted"]:
                    resolved_name = suspected_true_name
                    via_suspected_true_name = True
                    true_name_pinned_quote = verification["pinned_quote"]
                    true_name_pinned_chapter_idx = verification["pinned_chapter_idx"]
                    true_name_dual_anchor = verification["dual_anchor"]
                    true_name_hints.append({
                        "kind": "scene", "mention": name,
                        "suspected_true_name": suspected_true_name, "status": "accepted",
                        "dual_anchor": true_name_dual_anchor,
                    })
                else:
                    true_name_hints.append({
                        "kind": "scene", "mention": name,
                        "suspected_true_name": suspected_true_name, "status": "rejected",
                        "reason": verification["reason"],
                    })
            scene_reference_id, canonical_scene_name = (
                _prep_pack_resolve_scene_reference_with_alias(
                    conn, project_id, episode_no, resolved_name, bible,
                )
            )
            # 场景证据闸（1.4.2，见 _prep_pack_mention_has_text_evidence 上方
            # 注释）：只在"裸直接命中"（这个 label 从未被场景发现处理过，即
            # 不在 scene_rename 里——用"是否是该字典的 key"判定，不能用
            # resolved_name == name 的字符串比较：发现判定新场景的规范名恰好
            # 与原始 label 相同也是完全合法的结果，比如新建场景直接沿用了
            # 提及原文，字符串相等不代表没被发现处理过）时核验——一旦这个
            # label 真的经过发现，就信任发现自己更细致的判定，不再重复核验
            # （新建场景的规范名通常是 AI 综合描述出的标签，本就不会逐字出现
            # 在原文里，用同一条子串检查会误伤合法的新建）。没证据 → 当作未
            # 解析，走场景发现（本例应新建"靠山宗外围山峰"），不是直接拒绝
            # ——场景侧允许回炉重新判定，跟角色侧"具名拦截"不对称是刻意的：
            # 新建场景的代价低，发现机制本身就是给"裸命中没证据"设计的下一步。
            if (
                scene_reference_id
                and not resolved_via_discovery
                and not _prep_pack_mention_has_text_evidence(name, source_text)
            ):
                scene_reference_id = None
            if not scene_reference_id:
                errors.append(
                    f"场景「{name}」（段 {mention_segment_indexes}）未解析到已有 "
                    "scene_reference_id"
                )
                if name not in unresolved_scenes:
                    unresolved_scenes.append(name)
                continue
            # 场景别名锚定（task①）：证据闸已经确认这次绑定成立——把本集
            # 实际用到的原文措辞（name）记为该场景的新别名（幂等，见
            # _prep_pack_register_scene_alias_if_new），别名库随集数增长；
            # 观测元数据记录锚定来源段号，不影响任何门禁判断。
            registered = _prep_pack_register_scene_alias_if_new(
                conn, project_id,
                canonical_name=canonical_scene_name, wording=name,
            )
            if registered:
                scene_alias_anchors.append({
                    "canonical_scene_name": canonical_scene_name,
                    "alias": name,
                    "anchor_segment": _prep_pack_first_evidence_segment(segments, name),
                })
            resolved_name = canonical_scene_name
            # provenance method（1.6.0）：跟角色侧同一优先级顺序——
            # discovery（本次新建场景）优先于其它任何信号；然后是经
            # 发现匹配到已有场景（resolution）；然后是 suspected_
            # true_name 核验通过（resolution）；然后是场景别名回退命中
            # （alias，_prep_pack_resolve_scene_reference_with_alias 内部
            # 的 match_scene_name 回退分支）；最后是裸直接命中（direct）。
            scene_forward_chapter_label = ""
            scene_source_episode_no: int | None = None
            # 场景绑定的锚点候选（第28轮 ERR-20260824，v3 审计
            # A2_scene_no_text_evidence 25 条；2.0.2 恢复第三路候选，见
            # PREP_PACK_VERSION 上方 2.0.2 大注释——48e01ff 砍 event_chain
            # 时曾把这里收窄成只剩 [canonical_scene_name, name]，两路都是
            # 模型综合出的合成标签时结构上必然两路皆空，是当晚引入的真实
            # 回归，不是本条注释历史上就接受的设计）：resolution/discovery
            # 两支试 [canonical_scene_name, name, scene_quote]——发现新建
            # 场景、或消歧把一个提及判给已有场景时，模型申报的规范名
            # （canonical_scene_name）本身可能才是原文里真正出现的措辞
            # （label 是提及方式，canonical 是模型综合出的标签，反之亦然，
            # 取决于具体场景），scene_quote 是这条提及自己申报、经
            # _prep_pack_local_text_anchor 逐字核验的独立证据（isomorphic
            # 于旧 event_chain[].source_evidence[].quote，见 _ModelSceneMention.
            # quote 上方注释）——不是同义反复：它不是 name/canonical_scene_
            # name 的重复或变体，是模型对"这段原文写的是不是这个地点"这个
            # 独立问题的另一次单独申报，真假不由申报本身决定，由它是否
            # 逐字命中原文决定。三路候选都试过仍找不到，才是真的没有本集
            # 依据（下面 has_scene_anchor 会拦截，不再像 1.6.0 最初实现
            # 那样静默放行空锚）。
            if canonical_scene_name in newly_added_scene_names:
                scene_method = "discovery"
                scene_anchor_segments, scene_anchor_phrase = (
                    _prep_pack_local_text_anchor(
                        segments, [canonical_scene_name, name, scene_quote],
                    )
                )
            elif resolved_via_discovery:
                scene_method = "resolution"
                scene_anchor_segments, scene_anchor_phrase = (
                    _prep_pack_local_text_anchor(
                        segments, [canonical_scene_name, name, scene_quote],
                    )
                )
            elif via_suspected_true_name:
                # 跟角色侧同一套判定（第29轮，见
                # _prep_pack_verify_true_name_hypothesis 上方完整
                # 说明）：钉住的支撑句落在本集自己的段落里就是最贴切
                # 的本地锚点；落在全书别的章节，本集内没有锚点是
                # 正确的，不是缺陷，method 单独标记
                # "resolution_forward"，anchor_phrase 仍记这句被钉住的
                # 支撑句本身（第30轮 RCA 修正，跟角色侧同一处漏改：空的
                # 只该是 anchor_segments 这个"本地段号"，不该连带着把
                # anchor_phrase 这句话本身也清空），把裁决真正引用的
                # 章节号写进 provenance。
                local_index = _prep_pack_first_evidence_segment(
                    segments, true_name_pinned_quote,
                )
                if local_index is not None:
                    scene_method = "resolution"
                    scene_anchor_segments = [local_index]
                    scene_anchor_phrase = true_name_pinned_quote
                else:
                    scene_method = "resolution_forward"
                    scene_anchor_segments, scene_anchor_phrase = [], true_name_pinned_quote
                    if true_name_pinned_chapter_idx is not None:
                        scene_forward_chapter_label = (
                            f"第 {true_name_pinned_chapter_idx} 章"
                        )
            elif canonical_scene_name != name:
                # 2.0.2：恢复传入这条提及自己的 quote 作为第三方参数
                # scene_event_evidence_quotes（该形参名未改——语义仍是
                # "候选独立证据引文列表"，只是来源粒度从"事件"下沉到
                # "提及"，见该函数 docstring 与 PREP_PACK_VERSION 上方
                # 2.0.2 大注释）；找不到独立证据时函数自身仍会诚实降级为
                # alias_inherited，不在这里改判据。
                (
                    scene_method, scene_anchor_segments, scene_anchor_phrase,
                    scene_source_episode_no,
                ) = _prep_pack_scene_alias_provenance(
                    conn, segments, scene_reference_id,
                    canonical_scene_name, [scene_quote],
                )
            else:
                scene_method = "direct"
                scene_anchor_segments, scene_anchor_phrase = (
                    _prep_pack_local_text_anchor(segments, [name])
                )
            entry = scenes.setdefault(scene_reference_id, {
                "scene_id": f"scene:{resolved_name}",
                "display_name": resolved_name,
                "scene_reference_id": scene_reference_id,
                "segment_indexes": [],
                "provenance": _prep_pack_provenance(
                    scene_method, scene_anchor_segments, scene_anchor_phrase,
                    forward_chapter_label=scene_forward_chapter_label,
                    source_episode_no=scene_source_episode_no,
                    dual_anchor=(true_name_dual_anchor if via_suspected_true_name else None),
                ),
            })
            entry["segment_indexes"] = sorted(
                set(entry["segment_indexes"]) | set(mention_segment_indexes)
            )
        return (
            characters, scenes, functional_extras, errors,
            unresolved_characters, unresolved_scenes, true_name_hints,
            character_appellation_rows,
        )

    (
        characters, scenes, functional_extras, errors, unresolved_chars, unresolved_scenes,
        true_name_hints, character_appellation_rows,
    ) = await _pass(set(), {}, {})
    # 1.5.0：假设核验发生在每一遍 _pass() 内部；一个假设被拒绝的提及若同时触发
    # 发现（走到下面这个分支），第二遍 _pass() 的返回值会整体替换第一遍的——
    # 但第一遍已经记下的 true_name_hints 不该因此凭空消失（红灯 4b 明确要求
    # "rejected 计数=1" 且"走新场景发现"同时成立）。第一遍的记录单独保留，
    # 最后跟第二遍的合并去重。
    true_name_hints_pass1 = true_name_hints

    if unresolved_chars or unresolved_scenes:
        skip_character_names: set[str] = set()
        character_rename: dict[str, str] = {}
        scene_rename: dict[str, str] = {}
        non_person_names: set[str] = set()
        discovery_diagnostics: list[str] = []
        # provenance（1.6.0）：discovery 新建的角色/场景名单，以及角色消歧
        # 自带的证据引文（source_quote/evidence），供 _pass 判定 method 与
        # 计算 resolution 分支的锚点——见调用点上方各自的完整说明。
        newly_added_character_names: frozenset[str] = frozenset()
        newly_added_scene_names: frozenset[str] = frozenset()
        resolution_evidence_by_label: dict[str, str] = {}
        # 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大注释、
        # _prep_pack_resolve_functional_extra_candidate 的完整说明）：
        # label -> 钉证命中的卷宗记录，供下面 _pass() 的 method 判定分支
        # 单独标记 "candidate_verdict"。
        candidate_verdict_pins: dict[str, dict[str, Any]] = {}
        # 发起过候选判别模型调用、但没有选中任何候选的标签集合（1.10.0，
        # 缺陷 A 顺带修复的可观测性缺口，见 PREP_PACK_VERSION 上方大注释）：
        # 供 _pass() 把 functional_extras 条目的 provenance 标注
        # candidate_verdict_attempted=True，跟"候选集/卷宗为空、从未获得
        # 候选判别机会"的 method="discovery" 条目区分开。
        functional_candidate_attempted_names: set[str] = set()

        if unresolved_chars:
            stats["character_discovery_calls"] += 1
            discovery_result = await _run_async_step(
                run_id, "episode_prep_pack_character_discovery",
                lambda: _discover_new_characters(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text, run_id=run_id,
                ),
            )
            skip_character_names, character_rename, non_person_names = (
                _character_discovery_dispositions(discovery_result)
            )
            newly_added_character_names = frozenset(
                str(item.get("name") or "").strip()
                for item in (discovery_result.get("added") or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            )
            resolution_evidence_by_label = {
                str(item.get("source_label") or "").strip(): str(
                    item.get("source_quote") or item.get("evidence") or ""
                ).strip()
                for item in (discovery_result.get("resolutions") or [])
                if isinstance(item, dict) and str(item.get("source_label") or "").strip()
            }
            errored_names = _discovery_errored_names(discovery_result, unresolved_chars)
            # Coordinator-mandated default: anything discovery neither
            # resolved (rename) nor explicitly disposed of (skip) nor
            # explicitly failed on (errored_names) is a typed functional
            # identity under its own source label, not a block -- but only
            # once directly-resolvable names are ruled out first (discovery
            # may have just committed a portrait under this exact raw name,
            # e.g. a genuinely new character discovery carded this call; that
            # must resolve normally, not get swept into the fallback).
            for name in unresolved_chars:
                if name in skip_character_names or name in character_rename or name in errored_names:
                    continue
                if _resolve_portrait_id(conn, project_id, name, episode_no):
                    continue
                skip_character_names.add(name)
            discovery_diagnostics.extend(str(e) for e in discovery_result.get("errors") or [])

            # 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大
            # 注释、_prep_pack_resolve_functional_extra_candidate 的完整
            # 说明）：discovery 与上面的兜底默认都没能给出任何归类结论、
            # 即将落 functional_extras 的标签（skip_character_names 减去
            # non_person_names——非人物标签不该被判给任何真人候选），补一
            # 次代码检索卷宗 + 模型候选判别 + 段号钉证。命中就把
            # skip_character_names 里的这条移进 character_rename，让它
            # 重新走既有的具名解析路线（自然带出正确的 portrait_id/
            # identity_id/visual_entity_id；display_appellation 仍由下面
            # _pass 内的 name 本身承担，本集原文措辞不受这次改名影响，不
            # 提前剧透）。
            #
            # M 并发化（任务②，见 PREP_PACK_VERSION 上方大注释；10 集测量：
            # 38 次调用、串行 166.8 秒，6 并发估算 49.2 秒）：每个候选标签
            # 各自独立——_prep_pack_resolve_functional_extra_candidate 只读
            # DB/本集只读材料（bible/segments/character_mentions），不读、不写
            # skip_character_names/character_rename/candidate_verdict_pins
            # 这些正在本函数里改的共享状态，标签之间没有互相依赖，可以安全
            # 并发发起。但共享写回必须保持确定性（不得让并发完成顺序决定
            # 最终产物，跟 K 的"先聚合、再按序写"同一条纪律）：先按
            # unresolved_chars 的原始出场顺序筛出候选名单，asyncio.gather
            # 并发拿到全部结果（gather 按传入顺序、不是完成顺序返回，见
            # _prep_pack_gather_concurrent 上方注释），再用 zip 按同一份
            # 原始顺序逐个把结果写回 skip_character_names/character_rename/
            # candidate_verdict_pins/functional_candidate_attempted_names
            # 这几个共享容器——同一输入不管并发时哪个任务先完成，写回这一步
            # 本身单线程顺序执行，产物逐字节可复现。
            candidate_names = [
                name for name in unresolved_chars
                if name in skip_character_names and name not in non_person_names
            ]
            candidate_resolutions = await _prep_pack_gather_concurrent([
                _prep_pack_resolve_functional_extra_candidate(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, label=name, source_text=source_text,
                    segments=segments, bible=bible,
                    character_mentions=character_mentions,
                )
                for name in candidate_names
            ])
            for name, resolution in zip(candidate_names, candidate_resolutions, strict=True):
                if resolution["attempted"]:
                    functional_candidate_attempted_names.add(name)
                if not resolution["resolved"]:
                    continue
                skip_character_names.discard(name)
                character_rename[name] = resolution["canonical_name"]
                candidate_verdict_pins[name] = resolution

        if unresolved_scenes:
            stats["scene_discovery_calls"] += 1
            scene_discovery_result = await _run_async_step(
                run_id, "episode_prep_pack_scene_discovery",
                lambda: _discover_new_scenes(
                    conn, project_id=project_id, episode_no=episode_no,
                    labels=unresolved_scenes,
                ),
            )
            scene_rename = dict(scene_discovery_result.get("resolved_names") or {})
            newly_added_scene_names = frozenset(
                str(item.get("name") or "").strip()
                for item in (scene_discovery_result.get("added") or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            )
            discovery_diagnostics.extend(str(e) for e in scene_discovery_result.get("errors") or [])

        (
            characters, scenes, functional_extras, errors, unresolved_chars, unresolved_scenes,
            true_name_hints_pass2, character_appellation_rows,
        ) = await _pass(
            skip_character_names, character_rename, scene_rename, non_person_names,
            newly_added_character_names=newly_added_character_names,
            newly_added_scene_names=newly_added_scene_names,
            resolution_evidence_by_label=resolution_evidence_by_label,
            candidate_verdict_pins=candidate_verdict_pins,
            functional_candidate_attempted_names=frozenset(
                functional_candidate_attempted_names,
            ),
        )
        # 合并两遍，按内容去重（同一个提及在两遍里都核验出相同结论是正常的、
        # 无害的重复计算，不该在观测数据里出现两条一模一样的记录）。
        combined = true_name_hints_pass1 + true_name_hints_pass2
        seen: set[tuple[str, str, str, str]] = set()
        true_name_hints = []
        for hint in combined:
            key = (hint["kind"], hint["mention"], hint["suspected_true_name"], hint["status"])
            if key not in seen:
                seen.add(key)
                true_name_hints.append(hint)
        if errors and discovery_diagnostics:
            errors = list(errors) + [
                f"发现阶段诊断：{message}" for message in discovery_diagnostics[:5]
            ]

    functional_extras_payload = [
        {
            "label": label,
            "segment_indexes": data["segment_indexes"],
            "visual_entity_id": data["visual_entity_id"],
            "provenance": data["provenance"],
        }
        for label, data in functional_extras.items()
    ]
    props_payload = _prep_pack_build_prop_manifest(prop_mentions, segments)
    # appellation_map 真源出参（2.0.1 bug fix，见本函数 docstring
    # ``appellation_resolutions`` 一节与 _prep_pack_build_appellation_map
    # 上方大注释）：只在调用方真的传了列表时才写，默认 None 不记录，
    # 不影响这个函数自己的返回元组形状。
    if appellation_resolutions is not None:
        appellation_resolutions.extend(character_appellation_rows)
    return (
        list(characters.values()), list(scenes.values()), props_payload, functional_extras_payload,
        errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    )


# 2.0.0 新增：道具没有世界书图像素材库，不需要身份消歧/发现，也不需要
# suspected_true_name 声明-核验通道——一个道具就是它自己（结构判据，零
# 语义），按 label 精确字符串去重合并 segment_indexes 即可。
#
# 道具没有 characters/scenes 那样的"经解析路径绑定可豁免逐字"这条路
# （没有身份消歧、没有候选判别——道具的 label 就是它唯一的名字，不存在
# "解析成另一个规范名"这件事），因此每一个道具都等价于角色侧的"裸直接
# 命中"，反幻觉主防线必须适用：只保留 label 真的逐字出现在该段落原文里的
# segment_indexes（跟角色侧"称谓证据闸"同一判据，_prep_pack_gate_segment_
# indexes 的结构闸不做这一步是因为它对全部三种资产统一处理、且要给
# characters/scenes 的解析路径留豁免空间——道具没有这个豁免需求，在这里
# 单独把关不冲突）。一个道具的全部段号都验不过字面证据，整条提及丢弃（不
# 计入清单，不阻断发布——跟 scene 侧"没证据就当未解析"同一处置，不是
# "空口提名也发布"）。
def _prep_pack_build_prop_manifest(
    prop_mentions: list[dict[str, Any]], segments: list[SourceSegment],
) -> list[dict[str, Any]]:
    props: dict[str, dict[str, Any]] = {}
    for mention in prop_mentions:
        label = str(mention.get("label") or "").strip()
        if not label:
            continue
        segment_indexes = sorted(
            index for index in {int(i) for i in mention.get("segment_indexes") or []}
            if 1 <= index <= len(segments) and label in segments[index - 1].text
        )
        if not segment_indexes:
            continue
        entry = props.setdefault(label, {
            "label": label,
            "description": str(mention.get("description") or "").strip(),
            "segment_indexes": [],
            "provenance": _prep_pack_provenance("direct", [segment_indexes[0]], label),
        })
        entry["segment_indexes"] = sorted(
            set(entry["segment_indexes"]) | set(segment_indexes)
        )
    return list(props.values())


def _begin_step(run_id: str | None, step_key: str, *, iteration_no: int = 1) -> str | None:
    if not run_id:
        return None
    step_id = evidence_repository.create_step(
        run_id, step_key,
        iteration_no=iteration_no,
        agent_name="episode_prep_pack",
        contract_version=get_contract("screenplay").version,
    )
    transition_step(step_id, "PENDING", "READY", "输入已就绪", conn=None)
    transition_step(step_id, "READY", "RUNNING", "步骤开始", conn=None)
    return step_id


def _finish_step(step_id: str | None, exc: BaseException | None) -> None:
    if not step_id:
        return
    if exc is not None:
        transition_step(
            step_id, "RUNNING", "FAILED", str(exc)[:1000],
            decision="escalate", error_code=type(exc).__name__.upper(), conn=None,
        )
        return
    transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept", conn=None)


def _run_sync_step(run_id: str | None, step_key: str, fn):
    """Wrap one deterministic (non-model-call) unit of work as an observable
    step, reusing the same create_step/transition_step machinery as the
    model-calling steps below -- so it shows up in the same observability
    trace with a registered business name (see
    app.orchestration.engine._STEP_PRESENTATIONS)."""
    step_id = _begin_step(run_id, step_key)
    try:
        result = fn()
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _run_async_step(run_id: str | None, step_key: str, fn):
    """Async twin of ``_run_sync_step`` for one observable awaited unit of
    work (e.g. an app.portraits/app.scenes discovery call) that is not itself
    a structured model call through ``_call_structured``."""
    step_id = _begin_step(run_id, step_key)
    try:
        result = await fn()
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _call_structured(
    *,
    run_id: str | None,
    step_key: str,
    prompt: str,
    model_type: type[BaseModel],
    schema_name: str,
    operation_id: str,
    max_tokens: int,
    call_meta: dict[str, Any],
    iteration_no: int = 1,
    output_schema: dict[str, Any] | None = None,
    temperature: float = 0.2,
) -> Any:
    """``output_schema`` (1.10.0, 缺陷 A 修复引入)：可选的手写 JSON Schema
    覆盖——真名裁决候选判别需要把 selected_candidate/supporting_entry_index
    收紧到本次卷宗实际算出的 enum（参照 _prep_pack_functional_candidate_call
    对 model_gateway.chat_structured 的直接调用写法），而不是走
    ``_response_format``/``require_response_format`` 这条固定 schema 路径。
    传入时用 ``output_schema`` 直接驱动 provider 调用；不传（默认）时行为与
    改动前逐字节一致。这是同一个 step 封装（_begin_step/_finish_step 观测
    埋点）下的一个可选分支，不是新建一条调用路径。"""
    step_id = _begin_step(run_id, step_key, iteration_no=iteration_no)
    trace = current_trace()
    ctx = bind_trace(run_id, step_id, trace.trace_id) if run_id else nullcontext()
    try:
        with ctx:
            if output_schema is not None:
                result = await model_gateway.chat_structured(
                    [{"role": "user", "content": prompt}],
                    model_type=model_type,
                    validate=None,
                    operation_id=operation_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    format_retry_limit=1,
                    semantic_retry_limit=1,
                    call_meta=call_meta,
                    output_schema=output_schema,
                )
            else:
                result = await model_gateway.chat_structured(
                    [{"role": "user", "content": prompt}],
                    model_type=model_type,
                    validate=None,
                    operation_id=operation_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    format_retry_limit=1,
                    semantic_retry_limit=1,
                    call_meta=call_meta,
                    response_format=_response_format(model_type, schema_name),
                    require_response_format=True,
                )
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _extract_chunk(
    *,
    episode_id: str,
    episode_no: int,
    chunk_index: int,
    chunk: list[tuple[int, SourceSegment]],
    known_characters: list[str],
    known_scenes: list[str],
    attempt_hint: str,
    run_id: str | None,
    confirmed_title_indexes: set[int] | None = None,
) -> _ChunkResponse:
    """2.0.0: 映射台的唯一模型调用——不再产出叙事内容（无 summary/事件/
    台词/hook/cliffhanger），只做资源发现+映射所需的原始素材申报：本段
    出现的人物/场景/道具，以及它们各自真正"画面出场"（不是被提及/回忆/
    转述）的段号。segment_indexes 是模型自己的语义判断（见 PREP_PACK_
    VERSION 上方 2.0.0 大注释"名字出现≠人在场"一节），下游
    _prep_pack_gate_segment_indexes 只做结构性核验（申报的段号是否落在
    这次调用真正看到的 chunk 范围内），不重复要求逐字命中——那道更细的
    证据闸留给 _resolve_assets 按 method 分支各自核验（见该函数与
    _prep_pack_gate_segment_indexes 各自的完整说明）。"""
    rendered = _render_chunk(chunk)
    hint = f"\n上一次尝试未通过校验，请修正：{attempt_hint}\n" if attempt_hint else ""
    # 1.9.0（继续保留，见 PREP_PACK_VERSION 上方大注释）：把确定性算出的
    # 章节标题段号作为既成事实告知模型，而不是让它再判断一遍——这些段号
    # 已经被 app.source_excerpt.chapter_title_segment_indexes 从
    # chapters.title 这个数据库锚点确定性算出，会无条件计入 coverage_
    # ledger.paratext（见 _prep_pack_build_coverage_ledger）。本 chunk
    # 不含任何这类段号时 chunk_title_indexes 为空，confirmed_title_
    # section 保持空字符串。
    chunk_title_indexes = sorted(
        index for index, _segment in chunk
        if index in (confirmed_title_indexes or ())
    )
    confirmed_title_section = ""
    if chunk_title_indexes:
        shown = "、".join(str(index) for index in chunk_title_indexes)
        confirmed_title_section = (
            f"编号 {shown} 已由系统确定性判定为本集所属章节的标题（排版元素，不是"
            "故事内容），已计入副文本账：不要为它们申报人物/场景/道具，也不要在"
            "paratext_segments 里重复申报。\n\n"
        )
    prompt = f"""你在为一部网络小说改编的短剧做素材映射准备（不改编台词、不生成分镜、不写剧情摘要）。

任务：通读下面按顺序编号的原文片段（编号即 segment_index，本段范围 {chunk[0][0]}~{chunk[-1][0]}），
申报三类素材：

- characters：本段原文中画面里真正出场（不是被别人提起、回忆、转述、听说）的角色，每个给
  {{"display_name": "角色称谓", "suspected_true_name": "你认为的真名，不确定就填 null",
  "segment_indexes": [该角色真正在画面中出场的编号列表]}}；
  已登记角色名（仅供拼写对齐——如果原文本身就是这样称呼这个角色的，写法要跟登记名
  保持一致；原文没有这样称呼，就不要往上面靠）：{known_characters}；
- scenes：本段原文中角色实际所在的场景/地点，每个给 {{"display_name": "场景名",
  "suspected_true_name": "你认为的正名，不确定就填 null", "segment_indexes": [该场景实际
  在画面中出现的编号列表], "quote": "从上面 segment_indexes 任一编号原文中逐字摘录的一段
  原文（不超过约60字），要能证明这里写的就是这个地点——不得改写/概括/跨编号拼接；这个场景
  在本段确实没有可摘录的原文依据就填空字符串，绝不编造"}}；已登记场景名（仅供拼写对齐，同
  上一条的原则）：{known_scenes}；
- props：本段原文中画面里明确出现、有辨识度的物品/道具（不是随口一提，例如武器、信物、
  法宝、书信等），每个给 {{"label": "道具名称", "description": "这个道具的外观/特征简述",
  "segment_indexes": [该道具实际出现的编号列表]}}；没有就给空列表，不要为了填满而虚构。

segment_indexes 判据（硬性，对 characters/scenes/props 都适用）：只申报这个人物/场景/道具
真正在画面中出场/出现的编号——原文只是提到这个名字、被别的角色回忆/转述/听说、或只是
背景知识提及，都不算"出场"，不要申报那个编号；反之，只要真的在画面中出场，哪怕只是一句带过，
也要如实申报，不要漏报。

场景的持续性（仅适用于 scenes，硬性）：一个场景一旦在某个编号成立，只要后续编号里情节仍在
同一地点发生——哪怕那些编号没有再次提到地点名称或做任何环境描写，只是人物的对话/动作/
心理，这些编号依然属于这个场景，要一并计入它的 segment_indexes，不能因为某个编号本身没有
复述地点就漏报；只有当情节明确转移到另一个地点、或原文本身已经写明离开/切换（例如出门、
关门、赶路前往别处），才停止把新的编号计入这个场景、改记到新地点名下。一段原文里，人物
所在的地点几乎总是连续的，不要把 scenes 的申报窄化成"只在地点被提到的那一句"。

命名纪律（关于 characters/scenes 的 display_name，硬性）：
- display_name 必须逐字使用本段原文中出现的称谓——原文写"灰袍老者"就填"灰袍老者"，
  禁止填任何本段原文没有出现过的名字，哪怕你认为自己知道这个人物/地点的"真名"；
  display_name 永远不能被下面这条替换；
- characters 的 display_name 取词优先级（硬性）：如果本段原文中这个角色存在任何
  称谓性表述——人名、尊称、绰号等原文里其他人或旁白直接用来称呼这个人本人、
  能把他从人群里单独指认出来的说法——display_name 必须逐字采用其中一个，不允许
  改用你自己综合出的外貌、衣着、动作等描述性短语去代替，哪怕那个描述性短语同样
  逐字出现在原文里、哪怕你觉得它在这段更醒目或更容易辨认；只是概括这个人所属
  群体/类别、换成同一类别里的另一个人也同样适用的泛称（哪怕形式上像称呼），不算
  称谓性表述，仍属于描述性表述。原因：称谓才可能跟这个角色积累的其它信息对上号，
  描述性短语（含泛称）哪怕逐字为真，也只是这一段独有、认不出具体是谁的说法，会让
  一个本来可以确定身份的角色被迫退化成每次都要重新判断一遍是谁；只有当这个角色
  在本段原文里通篇只有描述性表述、完全没有任何称谓性表述时，才允许 display_name
  使用描述性短语——这种情况合法，不要因为这条优先级去勉强杜撰一个称谓出来；
- 如果这个角色在本段原文中存在不止一种称谓性表述，取本段内出现次数最多的那一个；
  次数相同就取最先出现的那一个；同一角色在本段全程都按这条规则统一取值一个
  display_name，不要换着用不同的称谓（可以有多个 segment_indexes，但 display_name
  只有一个）；
- 先验知识申报通道：你有可能在训练语料里读过这部小说——如果知道某个称谓背后的真名
  或正式名称，把它填进对应 mention 的 suspected_true_name（不确定就填 null，不要瞎猜
  硬填）；这只是申报，你的猜测会被本集原文/后续章节的文本证据核验，核验不过就不会
  被采用，绝不会被静默相信；
- 场景地点的 display_name 一律使用原文自己的描述词，不得替换成你认为等价的其他
  地名（哪怕原文的地点和你知道的某个地名指的是同一个地方，也只能照抄原文怎么说，
  真名假设同样走 suspected_true_name）。

另外给出 paratext_segments：本段编号中，属于"非故事内容"的编号列表——章节标题、
作者对读者说的话（求收藏/求推荐/月票/上架/加更/催更等）、网站公告，这些不是故事
叙述本身（人物动作/对白/心理/场景描写都不算，哪怕它们提到类似字眼也不算）。你自己
就能判断哪些是——按内容本身判断，不用管它们在本段的位置。没有就给空列表。
{confirmed_title_section}{hint}
原文（本段共 {len(chunk)} 个编号片段）：
{rendered}
"""
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_event_chain_chunk",
        iteration_no=chunk_index,
        prompt=prompt,
        model_type=_ChunkResponse,
        schema_name="episode_prep_pack_chunk_v4",
        operation_id=f"episode_prep_pack:{episode_id}:chunk:{chunk_index}",
        max_tokens=8000,
        call_meta={
            "stage_key": "episode_prep_pack_event_chain",
            "episode_id": episode_id,
            "chunk_index": chunk_index,
        },
    )



# ---------------------------------------------------------------------------
# One generation attempt
# ---------------------------------------------------------------------------

# 2.0.0 新增：coverage_ledger 五账投影，直接基于已核验的 segment_indexes
# 并集计算，不再经过 app.validators.build_prep_pack_span_ledger 那套事件
# 跨度账本（该函数留在原地不动，见 PREP_PACK_VERSION 上方 2.0.0 大注释）。
# 语义：
#   delivered = 至少一条已核验（_prep_pack_gate_segment_indexes 通过）的
#     人物/场景/道具提及真正在此段出场；
#   paratext = 确定性章节标题段（deterministic_title_indexes）∪ 模型自报
#     且未与 delivered 冲突的 paratext_segments（冲突时 delivered 优先，
#     冲突记录进 rejected_paratext_claims 供观测，不静默吞掉）；
#   retained_as_context = 既不 delivered 也不 paratext 的其余全部段——
#     纯叙事推进、无新增可映射资产的段落，合法状态，不是缺陷；
#   merged/proven_duplicates 恒空（沿用既有惯例，这两账从未真正使用过）；
#   uncovered = 上面三账之外的段——结构上必然为空（total_segments 内每一
#     段要么 delivered、要么 paratext、要么 retained_as_context，穷尽三分
#     没有第四种可能），保留这个账户 + assert_prep_pack_coverage_complete
#     调用是纵深防御，不是这个函数本身可能产出非空 uncovered。
def _prep_pack_build_coverage_ledger(
    total_segments: int,
    delivered_indexes: set[int],
    paratext_indexes: set[int],
) -> tuple[dict[str, Any], list[int]]:
    all_indexes = set(range(1, total_segments + 1))
    delivered = delivered_indexes & all_indexes
    paratext_claims = paratext_indexes & all_indexes
    rejected_paratext_claims = sorted(paratext_claims & delivered)
    paratext = paratext_claims - delivered
    retained = all_indexes - delivered - paratext
    uncovered = all_indexes - delivered - paratext - retained
    ledger = {
        "total_segments": total_segments,
        "delivered": sorted(delivered),
        "merged": [],
        "retained_as_context": sorted(retained),
        "proven_duplicates": [],
        "paratext": sorted(paratext),
        "uncovered": sorted(uncovered),
    }
    return ledger, rejected_paratext_claims


# 2.0.3 新增（见 PREP_PACK_VERSION 上方 2.0.3 大注释的完整案情）：场景专项
# 覆盖账，跟上面五账并列、互不干扰的独立视角——五账的 delivered 只要角色/
# 场景/道具任一维度覆盖到某段就算 delivered，天然看不见"场景这一个维度
# 单独漏覆盖、角色/道具仍覆盖"的情形，这正是 EP4 真实回归暴露的缺陷：54
# 段章节里 scenes 只覆盖到 20 段，21~54 段因为角色提及（主角孟浩本人）
# 仍然贯穿在场，五账的 delivered/uncovered 完全看不出场景那部分已经断供，
# 这个缺口一路悄悄传导到分镜台三态告警才第一次现形。本账目让它在映射台
# 自己的产出里就可见。
#
# 不做的事（刻意）：不拦截、不重新定义"delivered"的既有语义、不往
# assert_prep_pack_coverage_complete 那道门禁塞新的阻断条件（该门禁签名
# 只读 ledger["uncovered"]，本账目是全新键，结构上不可能触发它）、不对
# scene_uncovered 做任何解释性判断——scene_uncovered 非空可能是真的漏报，
# 也可能是这些段落本来就没有场景描写（纯心理活动、纯对白），两种情况在
# 数据层面无法区分，交付判据仍然是逐条对原文，这里只负责让分母/分子可见，
# 不越权下结论。也不做"没覆盖就借用上一个场景的 segment_indexes 顺延"这
# 类兜底——那是编造场景归属，比空着更危险，比空着更难被发现是假的。
def _prep_pack_scene_coverage_account(
    total_segments: int,
    scene_delivered_indexes: set[int],
    paratext_indexes: set[int],
) -> dict[str, Any]:
    all_indexes = set(range(1, total_segments + 1))
    scene_delivered = scene_delivered_indexes & all_indexes
    paratext = paratext_indexes & all_indexes
    scene_uncovered = all_indexes - scene_delivered - paratext
    return {
        "total_segments": total_segments,
        "scene_delivered": sorted(scene_delivered),
        "scene_uncovered": sorted(scene_uncovered),
    }


# 2.0.0 新增，2.0.1 重做真源（协调方复核确认的 bug，见下段"2.0.1 根因"）：
# appellation_map 把每条原文里的模糊称谓摊平成逐段的 (raw_mention,
# segment_index) -> (identity_id, canonical_appellation) 映射表。
#
# 2.0.1 根因（测试缺口补齐过程中发现，协调方独立复现确认）：2.0.0 最初实现
# 拿 characters[].aliases 当"这个身份在本集被叫过的全部说法"的真源反查
# character_mentions——但 aliases 只登记逐字出现于原文的称谓（_resolve_
# assets 内 came_via_resolution and literal_evidence 双重门槛，见
# test_composite_description_resolved_via_discovery_bypasses_literal_gate：
# "穿杂役衫的魁梧大汉"经消歧正确解析到赵武刚、真实发布进 asset_manifest.
# characters，但明确不进 aliases）。aliases 担保的是"能不能安全进跨集别名
# 注册表而不污染它"，不是"这条提及有没有解析出身份"——拿前者的真源冒充
# 后者用，漏掉的恰好是模糊/描述性称谓，而那正是这张表存在的全部理由（"那
# 少年""小胖子""李管事"这类）。
#
# 修法：appellation_map 不再对 characters[]/character_mentions 做事后
# 反查，直接消费 _resolve_assets 在解析过程中就已经算出的结论——每条
# 提及在 _pass() 里真正解析到 portrait_id、通过称谓证据闸的那一刻，就地
# 记一行（见 _resolve_assets 内 character_appellation_rows 与其
# docstring 的 ``appellation_resolutions`` 出参说明），identity_id/
# canonical_appellation 直接读自它自己刚写入的 manifest entry——跟
# asset_manifest.characters[] 发布的是同一个字典对象，结构上保证两处不会
# 各说各话，不是靠这里再校验一遍。aliases 的既有语义（跨集别名注册表的
# 保护门槛）完全不受影响，未解析到身份的提及（落 functional_extras 的
# 那些）在 _pass() 里从未走到记录这一步，天然不出现在这张表里。
def _prep_pack_build_appellation_map(
    character_appellation_resolutions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for resolution in character_appellation_resolutions:
        raw_mention = str(resolution.get("raw_mention") or "").strip()
        identity_id = str(resolution.get("identity_id") or "")
        canonical_appellation = str(resolution.get("canonical_appellation") or "")
        if not raw_mention or not identity_id or not canonical_appellation:
            continue
        for segment_index in resolution.get("segment_indexes") or []:
            rows.append({
                "raw_mention": raw_mention,
                "segment_index": int(segment_index),
                "identity_id": identity_id,
                "canonical_appellation": canonical_appellation,
            })
    return rows


async def _generate_prep_pack_once(
    *,
    episode_id: str,
    episode_no: int,
    project_id: str,
    chapter_indexes: list[int],
    source_text: str,
    run_id: str | None,
    attempt_hint: str,
) -> tuple[
    dict[str, Any], list[int], list[dict[str, Any]],
    list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None,
]:
    conn = get_conn()
    segments = index_source_segments(source_text)
    chunks = _chunk_segments(segments)
    known_characters = _known_character_names(conn, project_id, episode_no)
    known_scenes = _known_scene_names(conn, project_id, episode_no)
    # 1.9.0 (kept in 2.0.0, see PREP_PACK_VERSION's 1.9.0 note above):
    # DB-anchored chapter titles for this episode's own chapters -- fed to
    # both _extract_chunk (prompt injection, told to the model as an
    # already-decided fact) and _prep_pack_build_coverage_ledger (the
    # actual deterministic paratext account). Chapters with no DB title
    # contribute nothing here.
    chapter_titles = _prep_pack_chapter_titles(conn, project_id, chapter_indexes)
    deterministic_title_indexes = chapter_title_segment_indexes(segments, chapter_titles)

    character_mentions: list[dict[str, Any]] = []
    scene_mentions: list[dict[str, Any]] = []
    prop_mentions: list[dict[str, Any]] = []
    # 1.4.1 (kept in 2.0.0): the model's own paratext claim, aggregated
    # across all chunks and scoped to each chunk's own global segment
    # indexes (an index outside a chunk's own range is structurally
    # untrustworthy -- that chunk's model call never saw that segment).
    declared_paratext_segments: set[int] = set()

    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_global_indexes = {index for index, _segment in chunk}
        chunk_by_index = {index: segment for index, segment in chunk}
        response = await _extract_chunk(
            episode_id=episode_id,
            episode_no=episode_no,
            chunk_index=chunk_index,
            chunk=chunk,
            known_characters=known_characters,
            known_scenes=known_scenes,
            attempt_hint=attempt_hint,
            run_id=run_id,
            confirmed_title_indexes=deterministic_title_indexes,
        )
        declared_paratext_segments.update(
            index for index in response.paratext_segments if index in chunk_global_indexes
        )
        for mention in response.characters:
            valid_indexes = _prep_pack_gate_segment_indexes(
                mention.display_name, mention.segment_indexes,
                chunk_global_indexes, chunk_by_index,
            )
            if not valid_indexes:
                continue
            character_mentions.append({
                "display_name": mention.display_name.strip(),
                "suspected_true_name": mention.suspected_true_name,
                "segment_indexes": valid_indexes,
            })
        for mention in response.scenes:
            valid_indexes = _prep_pack_gate_segment_indexes(
                mention.display_name, mention.segment_indexes,
                chunk_global_indexes, chunk_by_index,
            )
            if not valid_indexes:
                continue
            scene_mentions.append({
                "display_name": mention.display_name.strip(),
                "suspected_true_name": mention.suspected_true_name,
                "segment_indexes": valid_indexes,
                # 2.0.2：该提及自己申报的逐字引文，见 _ModelSceneMention.quote
                # 上方注释与 PREP_PACK_VERSION 上方 2.0.2 大注释。不做结构闸
                # （不要求落在 valid_indexes 范围内）——它本来就要在下游经
                # _prep_pack_local_text_anchor 全书逐字复核，跟 canonical_
                # scene_name/name 两个既有候选走的是同一条核验路径，不重复
                # 造一遍。
                "quote": mention.quote.strip(),
            })
        for mention in response.props:
            valid_indexes = _prep_pack_gate_segment_indexes(
                mention.label, mention.segment_indexes,
                chunk_global_indexes, chunk_by_index,
            )
            if not valid_indexes:
                continue
            prop_mentions.append({
                "label": mention.label.strip(),
                "description": mention.description.strip(),
                "segment_indexes": valid_indexes,
            })

    if not character_mentions and not scene_mentions and not prop_mentions:
        raise PrepPackGateError("本集未发现任何人物/场景/道具", had_events=False)

    # 角色单项退化的可见性信号（第31轮真实回归 EP7，ep_621d93ac1231；不
    # 拦截，只留痕）：上面这道门禁是 OR 判据——character_mentions/scene_
    # mentions/prop_mentions 任一非空就放行，天然覆盖不到"角色这一项单独
    # 退化为零、其它维度仍有实质内容"的情形。真实事故正是这种：本集主角
    # 在原文单块 45 段内出场约 43 次，chunk 抽取的原始响应第一次调用本已
    # 正确报出该角色，但那次调用所在的 run 中途被打断，同一 run_id 重新
    # 整体起跑后，chunk 抽取的原始 JSON 结构中途缺了一段，本地格式修复
    # candidate 被 app.harness.model_gateway._latest_json_authority_root
    # 误判成末尾一个只含 scenes 的孤立片段（详见该函数与
    # ERR-20260824-7ab7cb 的既有说明），格式修复调用据此"忠实"地只交回
    # scenes、把 characters/props 一并修没了——scene_mentions 非空使上面
    # 那道门禁直接放行，角色维度归零这件事从此再没有任何信号能被看见。
    # 判据纯数据推导，不认名字：known_characters 非空说明本项目已有登记
    # 角色谱、这一集理应有角色可映射；scene_mentions/prop_mentions 任一
    # 非空说明这段原文确有实质内容被成功抽取，不是"这段原文本来就没有
    # 角色出场"（例如纯风景过场）——两个条件同时成立时 character_mentions
    # 仍整段为空就是可疑信号。只记录进 _publish_prep_pack 的 evaluation.
    # evidence（同 rejected_paratext_claims 等既有观测字段的路子），不
    # raise：既定方向是必被看见，不是必被拦住，交付判据仍然是逐条对原文。
    character_manifest_anomaly = (
        {
            "known_character_count": len(known_characters),
            "scene_mention_count": len(scene_mentions),
            "prop_mention_count": len(prop_mentions),
        }
        if known_characters and not character_mentions and (scene_mentions or prop_mentions)
        else None
    )

    delivered_indexes: set[int] = set()
    for mention in (*character_mentions, *scene_mentions, *prop_mentions):
        delivered_indexes.update(mention["segment_indexes"])
    paratext_indexes = set(deterministic_title_indexes) | declared_paratext_segments
    ledger, rejected_paratext_claims = _prep_pack_build_coverage_ledger(
        len(segments), delivered_indexes, paratext_indexes,
    )
    # 2.0.3（见 PREP_PACK_VERSION 上方 2.0.3 大注释）：跟上面五账并列的
    # 场景专项覆盖账，读的是 scene_mentions 自己的 segment_indexes 并集
    # ——跟 delivered_indexes 同一个数据源（模型申报、已过结构闸），不是
    # 发布后的 asset_manifest.scenes 重新算一遍；两者在一次成功发布里
    # 恒等（_resolve_assets 只会把已声明的 mention 解析进 manifest 或
    # 让整个生成因 asset_errors 失败重试，不会把已声明的 mention 悄悄
    # 丢弃却仍然发布成功），用前者可以在 _resolve_assets 调用之前就算好，
    # 不需要为了这一个账目改动下面的调用顺序。
    scene_delivered_indexes: set[int] = set()
    for mention in scene_mentions:
        scene_delivered_indexes.update(mention["segment_indexes"])
    ledger["scene_coverage"] = _prep_pack_scene_coverage_account(
        len(segments), scene_delivered_indexes, paratext_indexes,
    )
    try:
        assert_prep_pack_coverage_complete(ledger)
    except ValueError as exc:
        # 结构上不应发生（见 _prep_pack_build_coverage_ledger 的三分穷尽
        # 论证）——留作纵深防御，不静默吞掉一个理论上不可能出现的账本矛盾。
        raise PrepPackGateError(str(exc)) from exc

    # appellation_map 真源出参（2.0.1 bug fix，见 _prep_pack_build_
    # appellation_map 上方大注释）：这个函数是 _resolve_assets 的唯一
    # 生产调用点，传一份空列表进去，_resolve_assets 在解析每条角色提及
    # 时原地写入，调用返回后就是这一集完整、真实的解析结论。
    character_appellation_resolutions: list[dict[str, Any]] = []
    (
        characters, scenes, props, functional_extras, asset_errors, discovery_stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = await _run_async_step(
        run_id, "episode_prep_pack_asset_mapping",
        lambda: _resolve_assets(
            conn, project_id=project_id, episode_id=episode_id, episode_no=episode_no,
            source_text=source_text,
            character_mentions=character_mentions, scene_mentions=scene_mentions,
            prop_mentions=prop_mentions, run_id=run_id,
            appellation_resolutions=character_appellation_resolutions,
        ),
    )
    if asset_errors:
        raise PrepPackGateError(
            "资产映射未能 100% 解析（已尝试身份/场景发现，调用次数："
            f"角色 {discovery_stats['character_discovery_calls']}、"
            f"场景 {discovery_stats['scene_discovery_calls']}）："
            + "；".join(asset_errors[:10])
        )

    asset_manifest = {
        "characters": characters, "scenes": scenes, "props": props,
        "functional_extras": functional_extras,
    }
    # provenance 发布前自校验（1.6.0，第25轮收口）：见
    # _prep_pack_verify_manifest_provenance 上方完整说明——每一条非空
    # anchor_phrase 必须真的逐字命中它自己 anchor_segments 指向的原文段，
    # 不成立即门禁拦，不静默发布一份自称有证据、实际验不过的 manifest。
    provenance_errors = _prep_pack_verify_manifest_provenance(
        segments, asset_manifest, source_text,
    )
    if provenance_errors:
        raise PrepPackGateError(
            "资产来源证明自校验失败：" + "；".join(provenance_errors[:10])
        )

    appellation_map = _prep_pack_build_appellation_map(character_appellation_resolutions)

    payload = {
        "prep_pack_version": PREP_PACK_VERSION,
        "episode_no": episode_no,
        "episode_scope": {
            "chapter_indexes": chapter_indexes,
            "source_segment_count": len(segments),
        },
        "asset_manifest": asset_manifest,
        "appellation_map": appellation_map,
        "coverage_ledger": ledger,
    }
    return (
        payload, rejected_paratext_claims, true_name_hints,
        scene_alias_anchors, rejected_alias_conflicts, character_manifest_anomaly,
    )


# ---------------------------------------------------------------------------
# Atomic publish (原子发布 + 完成证书)
# ---------------------------------------------------------------------------

def _publish_prep_pack(
    *,
    episode_id: str,
    payload: dict[str, Any],
    run_id: str | None,
    rejected_paratext_claims: list[int] | None = None,
    true_name_hints: list[dict[str, Any]] | None = None,
    scene_alias_anchors: list[dict[str, Any]] | None = None,
    rejected_alias_conflicts: list[dict[str, Any]] | None = None,
    character_manifest_anomaly: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn = get_conn()
    contract = get_contract("screenplay")
    episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError("待发布剧集不存在")

    step_id = (
        evidence_repository.create_step(
            run_id, "episode_prep_pack_publish",
            agent_name="episode_prep_pack",
            contract_version=contract.version,
        )
        if run_id else None
    )
    if step_id:
        transition_step(step_id, "PENDING", "READY", "输入已就绪", conn=None)
        transition_step(step_id, "READY", "RUNNING", "步骤开始", conn=None)
    artifact_row = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="episode_prep_pack",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T2",
            content=payload,
            contract_version=contract.version,
        ),
        step_run_id=step_id,
    )
    artifact_id = str(artifact_row["id"])
    artifact_hash = str(artifact_row["content_hash"])

    input_fingerprint = evidence_repository.content_hash({
        "episode_id": episode_id,
        "episode_scope": payload["episode_scope"],
    })

    if conn.in_transaction:
        raise RuntimeError("分集映射包发布前存在未收口事务")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE artifacts SET status='validated' WHERE id=? AND status='candidate'",
            (artifact_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("待发布 working Artifact 状态发生冲突")

        evaluation_row = evidence_repository.create_evaluation(
            artifact_id,
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name=_QA_EVALUATOR_NAME,
                evaluator_version=QA_PROFILE_VERSION,
                status="passed",
                hard_gate_passed=True,
                evaluation_role="score_only",
                runtime_blocking=False,
                retry_eligible=False,
                score=100.0,
                issues=[],
                evidence={
                    "prep_pack_version": PREP_PACK_VERSION,
                    "coverage_uncovered": payload["coverage_ledger"]["uncovered"],
                    # 1.4.1 (kept in 2.0.0): a segment the model claimed was
                    # paratext but that also carries verified asset evidence
                    # -- the evidence wins, this claim is vetoed back to
                    # ordinary content. Observability only, never part of
                    # the frozen artifact payload itself (see
                    # _prep_pack_build_coverage_ledger's
                    # rejected_paratext_claims).
                    "rejected_paratext_claims": rejected_paratext_claims or [],
                    # 1.5.0: every suspected_true_name hypothesis's outcome
                    # (accepted+bound or rejected+discarded) -- observability
                    # only, see _prep_pack_verify_true_name_hypothesis.
                    "true_name_hints": true_name_hints or [],
                    # 1.5.1 (task①): every scene alias newly registered this
                    # episode (Bible.scenes[].aliases persistence itself
                    # already happened synchronously in _resolve_assets;
                    # this is observability only, records the anchor
                    # segment for audit).
                    "scene_alias_anchors": scene_alias_anchors or [],
                    # 1.5.2 (task②): every rebind rejected because the same
                    # alias string was already bound to a DIFFERENT character
                    # elsewhere in this project (see
                    # _prep_pack_cross_episode_alias_conflict, real EP3
                    # regression: "小胖子" wrongly rebound to "王有材").
                    "rejected_alias_conflicts": rejected_alias_conflicts or [],
                    # 第31轮真实回归 EP7, ep_621d93ac1231, version NOT
                    # re-bumped -- pure observability addition, no schema/
                    # prompt-contract/resolution-logic change (same footnote
                    # convention as the 1.5.2 "version NOT re-bumped" notes
                    # above): non-null only when character_mentions came back
                    # empty from chunk extraction while known_characters and
                    # (scene_mentions or prop_mentions) were both non-empty --
                    # a suspicious single-dimension degeneration that the "any
                    # one of characters/scenes/props non-empty" gate above
                    # cannot see (see _generate_prep_pack_once's comment at
                    # that same gate). Observability only, never blocks
                    # publish -- see that comment for why this data-derived
                    # signal exists and why it stays non-blocking.
                    "character_manifest_anomaly": character_manifest_anomaly,
                },
            ),
            step_run_id=step_id,
            conn=conn,
            commit=False,
        )

        cert = issue_completion_certificate(
            kind="screenplay",
            scope_id=episode_id,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            input_fingerprint=input_fingerprint,
            contract_version=contract.version,
            qa_profile_version=QA_PROFILE_VERSION,
            evaluation_ids=[str(evaluation_row["id"])],
            blockers=0,
            must_fix_issues=0,
            production_revision_id=None,
            conn=conn,
            commit=False,
        )
        verify_completion_certificate(
            cert,
            expected_artifact_id=artifact_id,
            expected_artifact_hash=artifact_hash,
            expected_input_fingerprint=input_fingerprint,
            expected_contract_version=contract.version,
            conn=conn,
        )
        assert_publish_has_certificate(
            kind="screenplay", episode_id=episode_id, certificate_id=cert.certificate_id,
        )

        conn.execute(
            "UPDATE artifacts SET status='approved', trust_level='T2' WHERE id=?",
            (artifact_id,),
        )
        episode_cursor = conn.execute(
            "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', "
            "screenplay_error=NULL, screenplay_updated_at=?, screenplay_artifact_id=?, "
            "published_screenplay_artifact_id=?, screenplay_completion_certificate_id=?, "
            "active_screenplay_run_id=NULL, status='planned', script_error=NULL "
            "WHERE id=?",
            (
                json.dumps(payload, ensure_ascii=False),
                now(),
                artifact_id,
                artifact_id,
                cert.certificate_id,
                episode_id,
            ),
        )
        if episode_cursor.rowcount != 1:
            raise ValueError("分集映射包发布 episode 更新发生冲突")
        # 2.0.0：不再预写 episodes.cliffhanger/hook（payload 不再携带这两个
        # 字段，见 PREP_PACK_VERSION 上方 2.0.0 大注释）——这两列本来就会被
        # app/production/publish.py 在真正发布时用 script.ending_hook（发布
        # 时的权威来源）覆盖，prep_pack 阶段不再预写不是能力回退。
        consume_completion_certificate(cert.certificate_id, conn=conn, commit=False)
        conn.commit()
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if step_id:
            transition_step(
                step_id, "RUNNING", "FAILED", str(exc)[:1000],
                decision="escalate", error_code=type(exc).__name__.upper(), conn=None,
            )
        raise
    if step_id:
        transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept", conn=None)
    return {
        "episode_id": episode_id,
        "artifact_id": artifact_id,
        "certificate_id": cert.certificate_id,
        "status": "ready",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_episode_prep_pack(
    *,
    episode_id: str,
    episode: dict[str, Any],
    source_text: str,
    run_id: str | None,
) -> dict[str, Any]:
    """Generate + atomically publish one episode's episode_prep_pack.

    Bounded retry (contract.max_iterations, currently 2): each attempt
    regenerates the whole pack from scratch -- there is no partial-checkpoint
    repair loop (that heavier design was explicitly retired, see
    docs/TRANSFORM_FREEZE_PLAN.md §3/§6). If the last attempt still fails a
    hard gate, the run fails with the gate's error message.

    退化重试护栏（第23轮真实回归 ERR-20260824-7ab7cb，2.0.0 起 had_events
    语义改为"这次尝试有没有发现任何人物/场景/道具提及"，机制本身不变）：
    真实事故形态——尝试1 拿到一批真实提及，一路通过资产映射，只在后面
    某道门禁被拒；尝试2 重新抽取时模型这次的原始 JSON 本身在中途缺了一段
    结构（截断/自愈失败），修复重试拿到的候选又被误判，模型据此"忠实"地
    把提及列表修回空——整批提及退化为零。旧逻辑里 attempt_hint/last_error
    每轮无条件覆盖，尝试2 的"本集未发现任何素材"就这样悄悄盖掉了尝试1
    更有信息量的失败原因。护栏：一旦本运行内任何一次尝试发现过素材
    （PrepPackGateError.had_events=True），后续任何退化为零（had_events=
    False）的尝试都不得被当成普通失败静默采纳——必须把两次的失败原因合并
    成一条具名错误，明说"这是一次退化重试，不是从未发现过素材"。只有本
    运行内全部尝试都是零素材，才维持原始的终态报错。
    """
    contract = get_contract("screenplay")
    project_id = str(episode["project_id"])
    episode_no = int(episode["episode_no"])
    try:
        raw_chapters = episode.get("source_chapters") or []
        chapter_indexes = (
            json.loads(raw_chapters or "[]")
            if isinstance(raw_chapters, str)
            else list(raw_chapters)
        )
    except (TypeError, ValueError):
        chapter_indexes = []
    chapter_indexes = [int(idx) for idx in chapter_indexes]

    attempt_hint = ""
    last_error: Exception | None = None
    prior_attempt_had_events = False
    prior_attempt_reason = ""
    for attempt in range(1, max(1, contract.max_iterations) + 1):
        try:
            (
                payload, rejected_paratext_claims, true_name_hints,
                scene_alias_anchors, rejected_alias_conflicts,
                character_manifest_anomaly,
            ) = await _generate_prep_pack_once(
                episode_id=episode_id,
                episode_no=episode_no,
                project_id=project_id,
                chapter_indexes=chapter_indexes,
                source_text=source_text,
                run_id=run_id,
                attempt_hint=attempt_hint,
            )
            _publish_prep_pack(
                episode_id=episode_id, payload=payload, run_id=run_id,
                rejected_paratext_claims=rejected_paratext_claims,
                true_name_hints=true_name_hints,
                scene_alias_anchors=scene_alias_anchors,
                rejected_alias_conflicts=rejected_alias_conflicts,
                character_manifest_anomaly=character_manifest_anomaly,
            )
            return payload
        except PrepPackGateError as exc:
            had_events = bool(getattr(exc, "had_events", True))
            if not had_events and prior_attempt_had_events:
                # 退化重试护栏（ERR-20260824-7ab7cb）：本次重试事件链归零，
                # 但此前一次尝试确实抽到过事件——拒绝让这次退化静默覆盖，
                # 把两次的失败原因合并成一条具名错误。
                exc = PrepPackGateError(
                    "本次重试事件链退化为空，拒绝采纳该退化结果（此前一次"
                    f"尝试已抽到事件，但因以下原因被拒：{prior_attempt_reason}）；"
                    f"本次重试事件抽取本身失败：{exc}",
                    had_events=False,
                )
            last_error = exc
            attempt_hint = str(exc)[:2000]
            if had_events:
                prior_attempt_had_events = True
                prior_attempt_reason = str(exc)[:500]
            continue
    raise last_error if last_error is not None else RuntimeError("分集映射包生成失败")
