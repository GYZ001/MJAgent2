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
change; see the task brief / docs/TRANSFORM_FREEZE_PLAN.md §3):
{
  "prep_pack_version": "1.5.0",
  "episode_no": int,
  "episode_scope": {"chapter_indexes": [int], "source_segment_count": int},
  "event_chain": [{
      "event_id": str, "order": int, "summary": str,
      # source_span carries the EXTENDED value (see app.validators.
      # build_prep_pack_span_ledger's 语义分离 note, 1.5.0/ERR-20260824-22cb1c):
      # adjacent events' source_span may legitimately OVERLAP by a segment or
      # two -- that overlap is delivery-evidence spillover (a later event's
      # own verified quote reached one segment into a shared transition),
      # NOT a narrative-boundary claim. P1 storyboard consumers must not
      # treat source_span overlap as "these two events cover the same beat
      # twice"; the model's own declared span (not published here, only the
      # extended result is) is the actual narrative-order claim, and that
      # never overlaps by construction (see coverage_ledger's fatal rules).
      "source_span": {"from_segment": int, "to_segment": int},
      "source_evidence": [{"segment_index": int, "quote": str}],
      "key_lines": [
          {"speaker": str, "line": str, "segment_index": int, "speaker_ref": str},
      ],
  }],
  "asset_manifest": {
      "characters": [{"identity_id": str, "display_name": str,
                       "portrait_id": str, "event_ids": [str], "aliases": [str],
                       "visual_entity_id": str, "display_appellation": str}],
      "scenes": [{"scene_id": str, "display_name": str,
                  "scene_reference_id": str, "event_ids": [str]}],
      "functional_extras": [{"label": str, "event_ids": [str],
                              "visual_entity_id": str}],
  },
  "coverage_ledger": {"total_segments": int, "delivered": [int], "merged": [int],
      "retained_as_context": [int],
      "proven_duplicates": [{"segment_index": int, "duplicate_of_segment_index": int}],
      "paratext": [int], "uncovered": [int]},
  "hook": str, "cliffhanger": str,
}

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

import json
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
from app.source_excerpt import SourceSegment, align_source_excerpt, index_source_segments
from app.textmatch import bigram_coverage
from app.validators import (
    assert_prep_pack_coverage_complete,
    assert_prep_pack_span_union_matches_ledger,
    build_prep_pack_span_ledger,
    match_scene_name,
)

PREP_PACK_VERSION = "1.8.0"  # 1.1.0: event_chain entries carry source_span (P1 storyboard needs it).
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
QA_PROFILE_VERSION = "prep-pack-qa-gate-1"
_QA_EVALUATOR_NAME = "screenplay_production_qa"
_CHUNK_MAX_CHARS = 6000
_HOOK_GROUNDING_COVERAGE = 0.06
# Segment-scoped verbatim check, NOT align_source_excerpt's generic 8-char
# default. Real EP1 output proved the 8-char floor silently rejects correct
# short exact quotes (e.g. "靠山宗。", 4 content chars) -- the search here is
# already scoped to one small segment, so a short but exact match is
# meaningful evidence, not a coincidental generic overlap.
QUOTE_MIN_MATCH_CHARS = 2

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

class _ModelSourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_index: int
    quote: str


class _ModelKeyLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker: str
    line: str
    segment_index: int


class _ModelCharacterMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    is_background_extra: bool
    # 1.5.0: model-declared prior-knowledge hypothesis (real EP5 finding:
    # outright banning this discarded a genuinely CORRECT guess -- see
    # _prep_pack_verify_true_name_hypothesis below). display_name must still
    # be the verbatim in-episode term of address; this field is never used
    # to replace it, only as an unverified candidate for _pass to check.
    suspected_true_name: str | None


class _ModelSceneMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    suspected_true_name: str | None  # 1.5.0, isomorphic to the character field above


class _ModelEventSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_segment: int
    to_segment: int


class _ModelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    summary: str
    source_span: _ModelEventSpan
    source_evidence: list[_ModelSourceEvidence]
    key_lines: list[_ModelKeyLine]
    characters: list[_ModelCharacterMention]
    scenes: list[_ModelSceneMention]


class _ChunkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[_ModelEvent]
    # 1.4.1: the model's own paratext claim for this chunk (chapter title /
    # author's note segments it deliberately did not turn into events) --
    # untrusted like every other model claim in this module; see
    # app.validators.build_prep_pack_span_ledger's three deterministic gates,
    # which decide what actually lands in coverage_ledger.paratext. Required
    # (not defaulted), matching every other field's strict-schema convention
    # in this module -- an empty list is a legal, explicit "none in this
    # chunk", not an omission.
    paratext_segments: list[int]


class _HookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hook: str
    hook_event_id: str
    cliffhanger: str
    cliffhanger_event_id: str


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
    "集"），混装单位会重蹈第28轮排查过的"同一数据两个真源"覆辙。两者都是
    纯附加字段，其它 method 不带这些 key，不影响既有消费者（payload 冻结
    纪律照旧）。"""
    provenance = {
        "method": method,
        "anchor_segments": list(anchor_segments),
        "anchor_phrase": anchor_phrase,
    }
    if forward_chapter_label:
        provenance["forward_chapter_label"] = forward_chapter_label
    if source_episode_no is not None:
        provenance["source_episode_no"] = source_episode_no
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
    自校验红灯，作为这条不变量的回归防线。"""
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
#   2) 裁决（模型，唯一一次调用，_prep_pack_true_name_verdict）：独立
#      调用，只给卷宗原文，不携带任何一方"是同一人"或"不是同一人"的
#      推理引导——问"仅依据以下原文段落，判断称谓 X 与人名 Y 是否同一个
#      人"，结论三选一 same/different/uncertain，并要求逐字引用支撑句。
#   3) 钉证（代码，_prep_pack_pin_dossier_quote）：模型引用的支撑句必须
#      逐字存在于卷宗某一条里（防止模型凭空编造一句"证词"）；verdict 必须
#      是 same 且引句核验通过，才算核验通过。uncertain/different/引句核验
#      失败，一律不采信——默认安全侧，不确定就不绑，回退到 alias 自身的
#      常规解析路线（未被发现进一步归类时自然落为群演，见 _pass 里
#      unresolved_characters 的处理，不需要这里单独再写一条"走群演"分支）。
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
    _prep_pack_sample_dossier_entries_within_budget 做确定性采样。"""
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


class _PrepPackTrueNameVerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["same", "different", "uncertain"]
    supporting_quote: str


# 裁决提示词按 subject_kind 分域的措辞表（独立评审 blocker：本函数被角色
# 分支 resolve_fn=_resolve_portrait_id 与场景分支 resolve_fn=
# _resolve_scene_reference_id 共用，旧版提示词硬编码"是否指同一个人"——
# 场景假设走到这里时模型被问"这两个是不是同一个人"，语义错误，裁决不可靠。
# noun_label 是卷宗引言里第二个待查证字符串的名词身份；same_subject 是
# "是否指同一 X"里的 X，同时出现在任务句与 verdict 三个取值的中文释义里，
# 结构（卷宗引用、uncertain 安全默认）跟改动前完全一致，只换名词。）
_TRUE_NAME_VERDICT_SUBJECT_COPY: dict[str, dict[str, str]] = {
    "character": {"noun_label": "人名", "same_subject": "同一个人"},
    "scene": {"noun_label": "地点名", "same_subject": "同一个场景或地点"},
}


async def _prep_pack_true_name_verdict(
    *, run_id: str | None, episode_id: str,
    subject_kind: Literal["character", "scene"],
    alias: str, true_name: str, dossier: list[dict[str, Any]],
) -> _PrepPackTrueNameVerdictResponse:
    """2) 裁决：唯一一次模型调用，只给卷宗原文，不携带任何一方的推理
    引导——不说"我怀疑 X 就是 Y"，只客观陈述两个字符串，把"是否同一
    人/场景"这个语义判断完全交给模型自己独立做出。``subject_kind`` 只
    决定问的是"同一个人"还是"同一个场景或地点"这一个名词，卷宗引用/
    uncertain 安全默认等结构完全不变（见 _TRUE_NAME_VERDICT_SUBJECT_COPY
    上方注释）。"""
    copy = _TRUE_NAME_VERDICT_SUBJECT_COPY[subject_kind]
    noun_label = copy["noun_label"]
    same_subject = copy["same_subject"]
    catalog = "\n\n".join(
        f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
        for item in dossier
    )
    prompt = f"""下面是从原著全书范围内检索到的原文段落，与称谓"{alias}"或{noun_label}
"{true_name}"其中至少一个有关（出现顺序不代表任何推断结论）：
{catalog}

任务：仅依据以上原文段落本身，判断称谓"{alias}"与{noun_label}"{true_name}"是否指
{same_subject}。
- verdict 填 same（确定是{same_subject}）/different（确定不是{same_subject}）/uncertain
  （原文不足以确定）三选一，无法确定就如实填 uncertain，不要勉强给出
  same 或 different；
- supporting_quote 必须是上面某一段落里逐字存在的一句原文，作为你得出
  这个结论的依据。
只输出符合 Schema 的 JSON。"""
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_true_name_verdict",
        prompt=prompt,
        model_type=_PrepPackTrueNameVerdictResponse,
        schema_name="episode_prep_pack_true_name_verdict_v1",
        operation_id=(
            f"episode_prep_pack:{episode_id}:true_name_verdict:"
            + evidence_repository.content_hash({
                "subject_kind": subject_kind,
                "alias": alias, "true_name": true_name,
                "dossier": [
                    (item["chapter_idx"], item["segment_index"]) for item in dossier
                ],
            })
        ),
        max_tokens=500,
        call_meta={
            "stage_key": "episode_prep_pack_true_name_verdict",
            "episode_id": episode_id,
            "subject_kind": subject_kind,
        },
    )


def _prep_pack_pin_dossier_quote(
    dossier: list[dict[str, Any]], quote: str,
) -> dict[str, Any] | None:
    """3) 钉证：模型引用的支撑句必须逐字存在于卷宗某一条里，防止模型
    凭空编造一句原文里根本没有的"证词"。返回命中的那条卷宗记录（带
    chapter_idx，供 provenance 定位），没有命中返回 None。"""
    quote = str(quote or "").strip()
    if not quote:
        return None
    for item in dossier:
        if quote in item["text"]:
            return item
    return None


async def _prep_pack_verify_true_name_hypothesis(
    conn, *, project_id: str, episode_id: str, episode_no: int, source_text: str,
    alias: str, suspected_true_name: str,
    subject_kind: Literal["character", "scene"], resolve_fn, run_id: str | None,
    verdict_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify (not trust) a model-declared ``suspected_true_name`` guess via
    the dossier trial procedure documented above. Returns a dict:
    ``accepted``；不通过时的 ``reason``（rejected_no_dossier/
    rejected_verdict_different/rejected_verdict_uncertain/
    rejected_quote_not_pinned，都归入观测的 rejected_verdicts 概念）；
    通过时的 ``pinned_quote``/``pinned_chapter_idx``（分别供 provenance.
    anchor_phrase 与 method="resolution"/"resolution_forward" 判定，见
    调用点）。``subject_kind`` 区分角色分支（resolve_fn=_resolve_portrait_id）
    与场景分支（resolve_fn=_resolve_scene_reference_id）——两者共用本函数
    与下面的裁决调用，但问的语义不同（"同一个人" vs "同一个场景或地点"，
    见 _prep_pack_true_name_verdict 的 _TRUE_NAME_VERDICT_SUBJECT_COPY），
    调用点各自传对。``verdict_cache`` 是 _resolve_assets 级别按
    (subject_kind, alias, suspected_true_name) 缓存的判决结果，同一次
    生成内重复出现的同一对提及不重复发起模型调用；subject_kind 纳入键是
    因为角色循环与场景循环共用同一个缓存字典，不按域隔离会导致跨域撞名
    时复用错误域的裁决（独立评审发现的 minor）。"""
    empty = {
        "accepted": False, "reason": "", "pinned_quote": "",
        "pinned_chapter_idx": None,
    }
    if not suspected_true_name:
        return empty
    if resolve_fn(conn, project_id, suspected_true_name, episode_no) is None:
        return empty
    cache_key = (subject_kind, alias, suspected_true_name)
    if verdict_cache is not None and cache_key in verdict_cache:
        return verdict_cache[cache_key]

    dossier = _prep_pack_true_name_dossier(conn, project_id, alias, suspected_true_name)
    if not dossier:
        result = {**empty, "reason": "rejected_no_dossier"}
        if verdict_cache is not None:
            verdict_cache[cache_key] = result
        return result
    response = await _prep_pack_true_name_verdict(
        run_id=run_id, episode_id=episode_id, subject_kind=subject_kind,
        alias=alias, true_name=suspected_true_name, dossier=dossier,
    )
    if response.verdict != "same":
        result = {**empty, "reason": f"rejected_verdict_{response.verdict}"}
        if verdict_cache is not None:
            verdict_cache[cache_key] = result
        return result
    pinned = _prep_pack_pin_dossier_quote(dossier, response.supporting_quote)
    if pinned is None:
        result = {**empty, "reason": "rejected_quote_not_pinned"}
        if verdict_cache is not None:
            verdict_cache[cache_key] = result
        return result
    result = {
        "accepted": True, "reason": "", "pinned_quote": pinned["text"],
        "pinned_chapter_idx": pinned["chapter_idx"],
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

_PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL = "都不是/无法确定"
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES = 12  # 单条候选判别卷宗最多收录的段落数
_PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS = 6000  # 单条候选判别卷宗最多收录的总字符数


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


def _prep_pack_functional_candidate_dossier(
    segments: list[SourceSegment], label: str, anchor_texts: set[str],
) -> list[dict[str, Any]]:
    """裁决卷宗检索：跟 app.stages._alias_verdict_dossier 同一套三层优先级
    （both 全收 → text_only 全收 → anchor_only 按离最近的 both/text_only
    段落远近补足预算），这里的"章"就是本集 ``segments``（``index_source_
    segments(source_text)`` 的结果）本身，不需要先定位桥接章。
    ``anchor_texts`` 必须覆盖全部候选的规范名∪已确认别名（调用方负责传
    全，不只是被测标签自己）——否则模型看不到其它候选各自的出场证据，
    选择题就名存实亡（见本节顶部大注释）。

    ``label`` 是原始提及文本本身（如"银色长袍女子"）——它不保证逐字出现在
    本集原文里（事件链抽取模型有时会转述/综合），both/text_only 两类因此
    可能为空；这时优先级退化为只剩 anchor_only（不再有"离标签最近"这个
    参照点，按文档顺序返回，见 priority_indexes 为空的分支）。只要候选集
    非空，anchor_only 必然非空——候选正是靠 anchor 命中本集才入选的（见
    _prep_pack_functional_candidate_names），不会出现"候选非空但卷宗为空"
    这种情况；调用方仍需处理空列表这个防御性分支。"""
    both_indexes: list[int] = []
    text_only_indexes: list[int] = []
    anchor_only_indexes: list[int] = []
    for index, seg in enumerate(segments):
        has_text = bool(label) and label in seg.text
        has_anchor = any(anchor and anchor in seg.text for anchor in anchor_texts)
        if has_text and has_anchor:
            both_indexes.append(index)
        elif has_text:
            text_only_indexes.append(index)
        elif has_anchor:
            anchor_only_indexes.append(index)
    priority_indexes = both_indexes + text_only_indexes
    if priority_indexes:
        anchor_only_ordered = sorted(
            anchor_only_indexes,
            key=lambda index: (min(abs(index - anchor) for anchor in priority_indexes), index),
        )
    else:
        anchor_only_ordered = anchor_only_indexes
    ordered_candidates = priority_indexes + anchor_only_ordered
    selected: list[int] = []
    used_chars = 0
    for index in ordered_candidates:
        if len(selected) >= _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES:
            break
        seg_text = segments[index].text
        if selected and used_chars + len(seg_text) > _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS:
            continue
        selected.append(index)
        used_chars += len(seg_text)
    selected.sort()
    return [
        {"segment_index": index + 1, "text": segments[index].text}
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
    不互相导入内部函数）。"""
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
) -> dict[str, Any] | None:
    """未解析标签的候选判别入口：候选集为空、卷宗为空、模型选"都不是/
    无法确定"、选中值不在候选集内（协议层已经不可能，这里仍防御性核验）、
    段号钉证失败、候选在本集没有可用定妆照、或这次改名会与跨集别名注册表
    冲突（复用既有 _prep_pack_cross_episode_alias_conflict，同一套"不确定
    不绑"纪律），一律返回 None——调用方维持原行为，标签留在
    skip_character_names 正常落 functional_extras，绝不猜。

    返回非 None 时是 ``{"canonical_name": ..., "segment_index": ...,
    "text": ...}``：``canonical_name`` 供调用方写入 character_rename（重新
    走既有的具名解析路线，自然带出正确的 portrait_id/identity_id/
    visual_entity_id）；``segment_index``/``text`` 是钉证命中的卷宗证据，
    供调用方写入 provenance 锚点（``text`` 是代码检索出的真实原文，不是
    模型转录，天然满足自校验的逐字命中要求）。"""
    roster = _prep_pack_functional_candidate_roster(bible)
    candidates = _prep_pack_functional_candidate_names(source_text, roster)
    if not candidates:
        return None
    anchor_texts = {form for name in candidates for form in roster.get(name, [])}
    dossier = _prep_pack_functional_candidate_dossier(segments, label, anchor_texts)
    if not dossier:
        return None
    response = await _prep_pack_functional_candidate_call(
        label=label, dossier=dossier, candidates=candidates,
        episode_id=episode_id, project_id=project_id,
    )
    if response.selected_candidate not in candidates:
        return None
    pinned = _prep_pack_functional_candidate_pin_segment(
        dossier, response.supporting_segment_index,
    )
    if pinned is None:
        return None
    canonical_name = response.selected_candidate
    if not _resolve_portrait_id(conn, project_id, canonical_name, episode_no):
        return None
    conflicting_name = _prep_pack_cross_episode_alias_conflict(
        conn, project_id, episode_id,
        alias=label, canonical_name=canonical_name, bible=bible,
    )
    if conflicting_name:
        return None
    return {
        "canonical_name": canonical_name,
        "segment_index": pinned["segment_index"], "text": pinned["text"],
    }


async def _resolve_assets(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    source_text: str, events: list[dict[str, Any]], run_id: str | None,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, int],
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
]:
    """Resolve every event's characters/scenes (invariant③).

    Every character/scene mention goes through the same resolution attempt,
    regardless of the event-chain extraction model's own ``is_background_extra``
    guess on that mention. That flag is advisory prose from a *different*,
    earlier model call that never looked at the bible -- treating it as an
    exemption from resolution is exactly the bug a real EP2 run surfaced:
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
    ) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], list[str], list[str], list[str],
        list[dict[str, Any]],
    ]:
        resolution_evidence_by_label = resolution_evidence_by_label or {}
        # 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大注释、
        # _prep_pack_resolve_functional_extra_candidate 的完整说明）：
        # label -> 钉证命中的卷宗记录（{"segment_index", "text"}），供下面
        # method 判定分支单独标记 "candidate_verdict"、并直接复用钉证段落
        # 本身（代码检索出的真实原文）作为 anchor_phrase，不依赖模型转录。
        candidate_verdict_pins = candidate_verdict_pins or {}
        characters: dict[str, dict[str, Any]] = {}
        scenes: dict[str, dict[str, Any]] = {}
        functional_extras: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        unresolved_characters: list[str] = []
        unresolved_scenes: list[str] = []
        # 1.5.0 观测记录：每条模型申报的 suspected_true_name 假设最终是被核验
        # 采信还是拒绝，都记一条（不影响门禁本身，见函数上方注释）。
        true_name_hints: list[dict[str, Any]] = []
        for event in events:
            event_id = event["event_id"]
            for mention in event["characters"]:
                name = str(mention["display_name"] or "").strip()
                if not name:
                    errors.append(f"事件 {event_id} 存在空白角色名")
                    continue
                if name in skip_character_names:
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
                        extra = functional_extras.setdefault(name, {
                            "event_ids": [],
                            "visual_entity_id": visual_entity_id_for_resolution({
                                "source_label": name, "scope_qualifier": "",
                            }),
                            "provenance": _prep_pack_provenance(
                                "discovery", extra_anchor_segments, extra_anchor_phrase,
                            ),
                        })
                        if event_id not in extra["event_ids"]:
                            extra["event_ids"].append(event_id)
                    continue
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
                suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
                true_name_pinned_quote = ""
                true_name_pinned_chapter_idx: int | None = None
                if suspected_true_name and suspected_true_name != resolved_name:
                    verification = await _prep_pack_verify_true_name_hypothesis(
                        conn, project_id=project_id, episode_id=episode_id,
                        episode_no=episode_no, source_text=source_text,
                        alias=name, suspected_true_name=suspected_true_name,
                        subject_kind="character",
                        resolve_fn=_resolve_portrait_id, run_id=run_id,
                        verdict_cache=true_name_verdict_cache,
                    )
                    if verification["accepted"]:
                        resolved_name = suspected_true_name
                        via_suspected_true_name = True
                        true_name_pinned_quote = verification["pinned_quote"]
                        true_name_pinned_chapter_idx = verification["pinned_chapter_idx"]
                        true_name_hints.append({
                            "kind": "character", "mention": name,
                            "suspected_true_name": suspected_true_name, "status": "accepted",
                        })
                    else:
                        true_name_hints.append({
                            "kind": "character", "mention": name,
                            "suspected_true_name": suspected_true_name, "status": "rejected",
                            "reason": verification["reason"],
                        })
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
                        f"事件 {event_id} 的角色「{name}」未解析到已有 portrait_id，"
                        "身份消歧也未能将其归类为已有角色或确定性群演"
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
                        f"事件 {event_id} 的角色「{name}」解析到已有角色「{resolved_name}」"
                        f"（portrait_id={portrait_id}），但称谓「{name}」未逐字出现在本集"
                        "原文中，缺少称谓证据，门禁具名拦截"
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
                entry = characters.setdefault(portrait_id, {
                    "identity_id": f"bible:{resolved_name}",
                    "display_name": resolved_name,
                    "portrait_id": portrait_id,
                    "event_ids": [],
                    "aliases": [],
                    "visual_entity_id": visual_entity_id_for_resolution({
                        "resolution": "future_identity",
                        "canonical_name": resolved_name,
                    }),
                    "display_appellation": name,
                    "provenance": _prep_pack_provenance(
                        method, anchor_segments, anchor_phrase,
                        forward_chapter_label=forward_chapter_label,
                    ),
                })
                if event_id not in entry["event_ids"]:
                    entry["event_ids"].append(event_id)
                # 别名注册仍只登记逐字出现于原文的称谓（task②，见上方门禁
                # 注释）：组合/综合描述短语（"穿杂役衫的魁梧大汉"）合法通过了
                # 门禁，但绝不能进别名库——别名注册表是 task① 直接信任的读侧
                # 数据源，一旦被模型综合出的合成词污染，将来会被当成"这就是
                # 原文真实用过的称呼"重新播种给别的分集。
                if came_via_resolution and literal_evidence and name not in entry["aliases"]:
                    entry["aliases"].append(name)
            for mention in event["scenes"]:
                name = str(mention["display_name"] or "").strip()
                if not name:
                    errors.append(f"事件 {event_id} 存在空白场景名")
                    continue
                resolved_via_discovery = name in scene_rename
                resolved_name = scene_rename.get(name, name)
                via_suspected_true_name = False
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
                        subject_kind="scene",
                        resolve_fn=_resolve_scene_reference_id, run_id=run_id,
                        verdict_cache=true_name_verdict_cache,
                    )
                    if verification["accepted"]:
                        resolved_name = suspected_true_name
                        via_suspected_true_name = True
                        true_name_pinned_quote = verification["pinned_quote"]
                        true_name_pinned_chapter_idx = verification["pinned_chapter_idx"]
                        true_name_hints.append({
                            "kind": "scene", "mention": name,
                            "suspected_true_name": suspected_true_name, "status": "accepted",
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
                        f"事件 {event_id} 的场景「{name}」未解析到已有 scene_reference_id"
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
                        "event_id": event_id,
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
                # A2_scene_no_text_evidence 25 条）：resolution/discovery
                # 两支之前只试 [name]（触发发现/消歧的原始 label）——但
                # 发现新建场景、或消歧把一个提及判给已有场景时，模型申报的
                # 规范名（canonical_scene_name）本身可能才是原文里真正出现
                # 的措辞（label 是提及方式，canonical 是模型综合出的标签，
                # 反之亦然，取决于具体场景），单试一种会漏掉另一种真实
                # 存在的锚点；还应该试这个场景所涉当前事件 source_evidence
                # 里的地点描述短语——消歧/发现凭的就是这些证据判断"这是
                # 哪个场景"，那些证据文本本身可能包含比 label/canonical
                # 更完整的逐字地点描述。三路候选都试过仍找不到，才是真的
                # 没有本集依据（下面 has_scene_anchor 会拦截，不再像
                # 1.6.0 最初实现那样静默放行空锚）。
                scene_event_evidence_quotes = [
                    str(evidence.get("quote") or "").strip()
                    for evidence in (event.get("source_evidence") or [])
                    if isinstance(evidence, dict)
                ]
                if canonical_scene_name in newly_added_scene_names:
                    scene_method = "discovery"
                    scene_anchor_segments, scene_anchor_phrase = (
                        _prep_pack_local_text_anchor(
                            segments,
                            [canonical_scene_name, name, *scene_event_evidence_quotes],
                        )
                    )
                elif resolved_via_discovery:
                    scene_method = "resolution"
                    scene_anchor_segments, scene_anchor_phrase = (
                        _prep_pack_local_text_anchor(
                            segments,
                            [canonical_scene_name, name, *scene_event_evidence_quotes],
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
                    (
                        scene_method, scene_anchor_segments, scene_anchor_phrase,
                        scene_source_episode_no,
                    ) = _prep_pack_scene_alias_provenance(
                        conn, segments, scene_reference_id,
                        canonical_scene_name, scene_event_evidence_quotes,
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
                    "event_ids": [],
                    "provenance": _prep_pack_provenance(
                        scene_method, scene_anchor_segments, scene_anchor_phrase,
                        forward_chapter_label=scene_forward_chapter_label,
                        source_episode_no=scene_source_episode_no,
                    ),
                })
                if event_id not in entry["event_ids"]:
                    entry["event_ids"].append(event_id)
        return (
            characters, scenes, functional_extras, errors,
            unresolved_characters, unresolved_scenes, true_name_hints,
        )

    characters, scenes, functional_extras, errors, unresolved_chars, unresolved_scenes, true_name_hints = (
        await _pass(set(), {}, {})
    )
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
            # 提前剧透）。按 unresolved_chars 的原始出场顺序遍历，保证同一
            # 输入任何时候重跑判别顺序一致；未命中的维持原行为，留在
            # skip_character_names 正常落 functional_extras。
            for name in unresolved_chars:
                if name not in skip_character_names or name in non_person_names:
                    continue
                resolution = await _prep_pack_resolve_functional_extra_candidate(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, label=name, source_text=source_text,
                    segments=segments, bible=bible,
                )
                if resolution is None:
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

        characters, scenes, functional_extras, errors, unresolved_chars, unresolved_scenes, true_name_hints_pass2 = (
            await _pass(
                skip_character_names, character_rename, scene_rename, non_person_names,
                newly_added_character_names=newly_added_character_names,
                newly_added_scene_names=newly_added_scene_names,
                resolution_evidence_by_label=resolution_evidence_by_label,
                candidate_verdict_pins=candidate_verdict_pins,
            )
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
            "event_ids": data["event_ids"],
            "visual_entity_id": data["visual_entity_id"],
            "provenance": data["provenance"],
        }
        for label, data in functional_extras.items()
    ]
    return (
        list(characters.values()), list(scenes.values()), functional_extras_payload,
        errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    )


# ---------------------------------------------------------------------------
# Speaker roster resolution (1.5.0, real EP2 finding): key_lines[].speaker was
# free text with zero validation -- a key line's speaker was written as "韩宗"
# (a character absent until chapter 5) for what was actually 绿袍男子. "本集
# 有谁" already has a single, gated source of truth by the time this runs: the
# resolved asset roster (characters + functional_extras from _resolve_assets,
# themselves already gated by the 1.4.2 evidence gate + 1.5.0 true-name
# verification above). Speaker resolution is therefore a pure, deterministic
# LOOKUP against that roster -- no new discovery, no new model call, no
# independent hypothesis mechanism of its own.
# ---------------------------------------------------------------------------

def _prep_pack_build_speaker_roster(
    characters: list[dict[str, Any]], functional_extras: list[dict[str, Any]],
) -> dict[str, str]:
    """Every string a speaker could legitimately be written as this episode,
    mapped to a ``speaker_ref``: a bound character's own ``display_name`` or
    any of its recorded ``aliases`` -> ``"bible:<display_name>"`` (mirrors
    asset_manifest.characters[].identity_id); a functional extra's own
    ``label`` -> ``"extra:<label>"``. Episode-wide, not per-event-scoped --
    anyone on screen anywhere this episode is a legal speaker anywhere else
    in the same episode (deliberate scope simplification, not a per-event
    presence check)."""
    roster: dict[str, str] = {}
    for character in characters:
        display_name = str(character.get("display_name") or "")
        ref = str(character.get("identity_id") or f"bible:{display_name}")
        if display_name:
            roster[display_name] = ref
        for alias in character.get("aliases") or []:
            if alias:
                roster[str(alias)] = ref
    for extra in functional_extras:
        label = str(extra.get("label") or "")
        if label:
            roster[label] = f"extra:{label}"
    return roster


def _prep_pack_all_project_character_names(conn, project_id: str) -> set[str]:
    """全谱扫描（1.5.2 语义精化，真实第21轮 EP1 回归 ERR-20260824-34347a）：
    不限本集 ep_start/ep_end 范围的完整项目人物谱名单——跟 _prep_pack_build_
    speaker_roster（本集名册）是两个不同的集合，用来区分 speaker 落空的两种
    截然不同的情形（见 _prep_pack_resolve_key_line_speakers 的三分支）：
    "项目里真有这个角色，只是没在本集出场"（幻觉归属）vs"项目里压根没有这个
    名字"（纯描述性称谓，合法一次性群演）。
    """
    rows = conn.execute(
        "SELECT DISTINCT character_name FROM character_portraits WHERE project_id=?",
        (project_id,),
    ).fetchall()
    return {str(row["character_name"]) for row in rows if row["character_name"]}


def _prep_pack_resolve_key_line_speakers(
    payload_events: list[dict[str, Any]],
    roster: dict[str, str],
    *,
    all_project_character_names: set[str],
    functional_extras: list[dict[str, Any]],
    characters: list[dict[str, Any]] | None = None,
) -> tuple[list[str], int]:
    """不对称三分支门禁（1.5.2 语义精化，真实第21轮 EP1 回归
    ERR-20260824-34347a）：speaker 与事件自己的 characters[] 表是同一个模型
    的两次独立措辞，字符串逐字相等天然脆弱（"被困者"/"王有材"/"被困少年"
    可能同指一人）——全拦会把合法的措辞漂移一并打死，全放又退回"韩宗"式
    幻觉。跟"洞即删戏（缺失致命）、良性重叠（冗余归一）"同构的思路：
      a) speaker 命中本集资产名册（display_name/alias/群演 label，见
         _prep_pack_build_speaker_roster）-> 正常引用，现状保留；
      b) speaker 命中项目全谱人物名（不限本集，见
         _prep_pack_all_project_character_names）但不在本集名册 ->
         **致命具名阻断**——这是真正要拦的目标：模型把项目里某个真实存在、
         但没有出现在本集的角色写成了说话人（真实 EP2 案例："韩宗"第5章
         才出场，本集却被写成了台词说话人）；
      c) speaker 与全谱零碰撞（纯描述性称谓，如"被困者"）-> **吸收为
         functional_extras**（label=speaker 原文措辞，event_ids 记这条台词
         所属事件），不阻断——这正是 app.portraits 一直以来的 typed
         functional identity 正统语义：一次性、有台词、没有稳定真名的角色，
         不是错误，是合法的资产类别。就地追加进传入的 functional_extras
         列表（与 payload 最终装配用的是同一个列表对象），并把这次吸收
         写回 roster，同一说话人在本集后续台词里直接走分支 a）。
    Mutates each key_line dict in place, adding ``speaker_ref``. Returns
    ``(block_messages, absorbed_speakers_count)`` -- the count is
    observability only (see caller's Evaluation.evidence wiring).
    """
    errors: list[str] = []
    absorbed_count = 0
    functional_extras_by_label = {
        str(extra.get("label") or ""): extra for extra in functional_extras
    }
    # speaker_provenance（协调方形状对齐指令，1.6.0）：event_chain[].key_lines
    # 每条也要带 provenance——一条台词的说话人到底是靠什么绑定的，直接继承
    # 它绑定到的那个资产（角色/群演）自己的 provenance，不是重新算一份。
    # ref -> provenance 的映射从当前已确定的 characters/functional_extras
    # 建立一次；分支 c 就地吸收新群演时同步补写这份映射，保证同一说话人在
    # 本集后续台词里走分支 a 也能查到刚吸收出来的 provenance。
    provenance_by_ref: dict[str, dict[str, Any]] = {}
    for character in characters or []:
        ref = str(character.get("identity_id") or "")
        provenance = character.get("provenance")
        if ref and isinstance(provenance, dict):
            provenance_by_ref[ref] = provenance
    for extra in functional_extras:
        label = str(extra.get("label") or "")
        provenance = extra.get("provenance")
        if label and isinstance(provenance, dict):
            provenance_by_ref[f"extra:{label}"] = provenance
    for event in payload_events:
        event_id = event.get("event_id")
        for key_line in event.get("key_lines") or []:
            speaker = str(key_line.get("speaker") or "").strip()
            if not speaker:
                errors.append(f"事件 {event_id} 的台词说话人为空，门禁具名阻断")
                continue
            ref = roster.get(speaker)
            if ref is not None:
                key_line["speaker_ref"] = ref
                provenance = provenance_by_ref.get(ref)
                if (
                    isinstance(provenance, dict)
                    and not provenance.get("anchor_segments")
                    and ref.startswith("extra:")
                ):
                    # discovery 类群演做 speaker 时的空锚回填（协调方第30轮
                    # ①，v4 审计31条）：这类 functional_extra 是角色发现
                    # 判定 skip/群演落地时创建的（method="discovery"），
                    # 触发发现的原始描述短语（见角色循环里 extra_anchor_
                    # phrase 的取法，只试 [name] 一个候选）未必逐字出现在
                    # 原文——但它这次确实开口说了台词，锚点跟 absorbed_
                    # speaker 同一套现成证据源：台词所在事件 source_
                    # evidence 段号 ∪ 这条台词自己的 segment_index，不需要
                    # 另外编一份。就地回填共享的 provenance 字典（跟
                    # functional_extras 清单里那一条是同一个对象），
                    # asset_manifest 自身与这条 key_line 的 speaker_
                    # provenance 同步获得锚点。
                    fallback_segments = sorted({
                        int(item["segment_index"])
                        for item in (event.get("source_evidence") or [])
                        if isinstance(item, dict) and item.get("segment_index") is not None
                    } | (
                        {int(key_line["segment_index"])}
                        if key_line.get("segment_index") is not None else set()
                    ))
                    if fallback_segments:
                        provenance["anchor_segments"] = fallback_segments
                        provenance["anchor_phrase"] = str(key_line.get("line") or "").strip()
                key_line["speaker_provenance"] = provenance
                continue
            if speaker in all_project_character_names:
                errors.append(
                    f"事件 {event_id} 的台词说话人「{speaker}」是项目人物谱中已有的"
                    "角色，但未出现在本集资产名册中，疑似幻觉归属，门禁具名阻断"
                )
                continue
            extra = functional_extras_by_label.get(speaker)
            if extra is None:
                # provenance（1.6.0）：吸收分支自己的锚点——"台词所在事件的
                # 证据段"（协调方原话）：事件自身 source_evidence 的段号，
                # 并入这条台词自己的 segment_index（key_line.line 是这条
                # 台词自己那一段对齐后的逐字摘录，必然逐字命中它自己的
                # segment_index，并入集合保证锚点必然可自校验通过，不是
                # 凭空编造）。anchor_phrase 用这条台词本身的逐字摘录。
                absorbed_segments = sorted({
                    int(item["segment_index"])
                    for item in (event.get("source_evidence") or [])
                    if isinstance(item, dict) and item.get("segment_index") is not None
                } | (
                    {int(key_line["segment_index"])}
                    if key_line.get("segment_index") is not None else set()
                ))
                # visual_entity_id（1.7.0，层三）：跟角色循环里的吸收分支
                # 同一构造——source_label 取 speaker 原文措辞，scope_qualifier
                # 留空（本函数是纯确定性名册查找，不产出/不消费这个字段）。
                extra = {
                    "label": speaker, "event_ids": [],
                    "visual_entity_id": visual_entity_id_for_resolution({
                        "source_label": speaker, "scope_qualifier": "",
                    }),
                    "provenance": _prep_pack_provenance(
                        "absorbed_speaker", absorbed_segments,
                        str(key_line.get("line") or "").strip(),
                    ),
                }
                functional_extras_by_label[speaker] = extra
                functional_extras.append(extra)
                provenance_by_ref[f"extra:{speaker}"] = extra["provenance"]
            if event_id not in extra["event_ids"]:
                extra["event_ids"].append(event_id)
            new_ref = f"extra:{speaker}"
            key_line["speaker_ref"] = new_ref
            key_line["speaker_provenance"] = provenance_by_ref.get(new_ref)
            roster[speaker] = new_ref
            absorbed_count += 1
    return errors, absorbed_count


def _prep_pack_prose_lint_warnings(
    *, payload_events: list[dict[str, Any]], hook: str, cliffhanger: str,
    known_names: list[str], roster_names: set[str],
) -> list[dict[str, Any]]:
    """Observability-level lint (NOT fatal, 1.5.0): a bible-registered proper
    noun appearing in free prose (event summary / hook / cliffhanger) that is
    NOT part of this episode's own roster is flagged for human review, not
    blocked -- "mentioned but not on screen" (e.g. a absent mentor recalled
    in narration) is a legitimate real scenario, not a naming-hallucination
    bug by itself; only an actual asset BIND without evidence is (see the
    1.4.2/1.5.0 gates above, which stay hard). ``known_names`` is this
    project's registered character/scene names scoped to this episode's own
    ep_start/ep_end window (the same list already fetched for the extraction
    prompt) -- a scope approximation of "谱内专名", not the full unscoped
    bible; acceptable for an observability-only signal."""
    warnings: list[dict[str, Any]] = []

    def _scan(field: str, text: str, event_id: str | None) -> None:
        for name in known_names:
            if len(name) >= 2 and name in text and name not in roster_names:
                warnings.append({
                    "field": field, "name": name, "event_id": event_id,
                    "excerpt": text[:80],
                })

    for event in payload_events:
        _scan("summary", str(event.get("summary") or ""), event.get("event_id"))
    _scan("hook", hook, None)
    _scan("cliffhanger", cliffhanger, None)
    return warnings


def _begin_step(run_id: str | None, step_key: str, *, iteration_no: int = 1) -> str | None:
    if not run_id:
        return None
    step_id = evidence_repository.create_step(
        run_id, step_key,
        iteration_no=iteration_no,
        agent_name="episode_prep_pack",
        contract_version=get_contract("screenplay").version,
    )
    transition_step(step_id, "PENDING", "READY", "输入已就绪")
    transition_step(step_id, "READY", "RUNNING", "步骤开始")
    return step_id


def _finish_step(step_id: str | None, exc: BaseException | None) -> None:
    if not step_id:
        return
    if exc is not None:
        transition_step(
            step_id, "RUNNING", "FAILED", str(exc)[:1000],
            decision="escalate", error_code=type(exc).__name__.upper(),
        )
        return
    transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept")


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
) -> Any:
    step_id = _begin_step(run_id, step_key, iteration_no=iteration_no)
    trace = current_trace()
    ctx = bind_trace(run_id, step_id, trace.trace_id) if run_id else nullcontext()
    try:
        with ctx:
            result = await model_gateway.chat_structured(
                [{"role": "user", "content": prompt}],
                model_type=model_type,
                validate=None,
                operation_id=operation_id,
                max_tokens=max_tokens,
                temperature=0.2,
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
) -> _ChunkResponse:
    rendered = _render_chunk(chunk)
    hint = f"\n上一次尝试未通过校验，请修正：{attempt_hint}\n" if attempt_hint else ""
    prompt = f"""你在为一部网络小说改编的短剧准备第 {episode_no} 集的事件链（不改编台词、不生成分镜）。

任务：把下面按顺序编号的原文片段（编号即 segment_index，本段范围 {chunk[0][0]}~{chunk[-1][0]}）
划分成一串按时间顺序排列的事件。每个事件必须给出：
- event_id：形如 "ev_001" 的字符串，本集内不重复，按发生顺序编号；
- summary：一句话概述该事件；
- source_span：{{"from_segment": 起始编号, "to_segment": 结束编号}}，声明该事件覆盖的原文
  编号闭区间；
- source_evidence：至少一条 {{"segment_index": span 范围内的编号, "quote": "该编号原文中的
  逐字引文片段"}}，quote 必须逐字取自该编号原文（可摘录其中一部分），不得改写、概括或跨编号
  拼接，segment_index 必须落在本事件自己的 source_span 内；
- key_lines：如果该事件包含台词，逐条给出 {{"speaker": "说话人", "line": "台词原文逐字摘录",
  "segment_index": span 范围内的编号}}，line 同样必须逐字取自该编号原文；没有台词就给空列表；
  speaker 请尽量使用跟本事件 characters 列表里同一个人一致的措辞（同一个人不要在
  characters 里写一种称呼、在 key_lines 里换另一种称呼）；
- characters：该事件中出场的角色，每个给 {{"display_name": "角色名", "is_background_extra": 布尔,
  "suspected_true_name": "你认为的真名，不确定就填 null"}}；
  已登记角色名（仅供拼写对齐——如果原文本身就是这样称呼这个角色的，写法要跟登记名
  保持一致；原文没有这样称呼，就不要往上面靠）：{known_characters}；
  没有姓名、不影响剧情走向的纯背景群演（路人、杂役等）标 is_background_extra=true，
  display_name 写功能性描述（如"围观弟子"）即可，不要虚构成主要角色；
- scenes：该事件发生的场景，每个给 {{"display_name": "场景名", "suspected_true_name": "你认为的
  正名，不确定就填 null"}}；已登记场景名（仅供拼写对齐，同上一条的原则）：{known_scenes}。

命名纪律（关于 characters/scenes 的 display_name，硬性）：
- display_name 必须逐字使用本段原文中出现的称谓——原文写"灰袍老者"就填"灰袍老者"，
  禁止填任何本段原文没有出现过的名字，哪怕你认为自己知道这个人物/地点的"真名"；
  display_name 永远不能被下面这条替换；
- 先验知识申报通道：你有可能在训练语料里读过这部小说——如果知道某个称谓背后的真名
  或正式名称，把它填进对应 mention 的 suspected_true_name（不确定就填 null，不要瞎猜
  硬填）；这只是申报，你的猜测会被本集原文/后续章节的文本证据核验，核验不过就不会
  被采用，绝不会被静默相信；
- 场景地点的 display_name 一律使用原文自己的描述词，不得替换成你认为等价的其他
  地名（哪怕原文的地点和你知道的某个地名指的是同一个地方，也只能照抄原文怎么说，
  真名假设同样走 suspected_true_name）。

另外给出 paratext_segments：本段编号中，属于"非故事内容"的编号列表——章节标题、
作者对读者说的话（求收藏/求推荐/月票/上架/加更/催更等）、网站公告，这些不是故事
叙述本身（人物动作/对白/心理/场景描写都不算，哪怕它们提到类似字眼也不算），不需要
为它们创建事件。你自己就能判断哪些是——按内容本身判断，不用管它们在本段的位置。
没有就给空列表。

硬性要求（关于 source_span）：
- 除 paratext_segments 声明的编号外，所有事件的 span 首尾相接，必须完整覆盖本段
  其余全部编号 {chunk[0][0]}~{chunk[-1][0]}，不允许任何编号既不在某个事件的 span
  内、也不在 paratext_segments 里——那等于把那段原文删掉了；
- 相邻事件允许共享一个边界编号（例如事件 A 的 to_segment=20，事件 B 的 from_segment=20），
  但不允许区间交叉或倒退（后一个事件的 from_segment 不能小于前一个事件的 to_segment）；
- 不要为了省事把一大段编号塞进一个事件——跨度明显大于平均值的事件，请至少给两条分别落在
  该跨度前半和后半的 source_evidence，证明你确实看过整段内容而不是笼统打包。
{hint}
原文（本段共 {len(chunk)} 个编号片段）：
{rendered}
"""
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_event_chain_chunk",
        iteration_no=chunk_index,
        prompt=prompt,
        model_type=_ChunkResponse,
        schema_name="episode_prep_pack_chunk_v3",
        operation_id=f"episode_prep_pack:{episode_id}:chunk:{chunk_index}",
        max_tokens=8000,
        call_meta={
            "stage_key": "episode_prep_pack_event_chain",
            "episode_id": episode_id,
            "chunk_index": chunk_index,
        },
    )


async def _extract_hook_cliffhanger(
    *,
    episode_id: str,
    episode_no: int,
    events: list[dict[str, Any]],
    attempt_hint: str,
    run_id: str | None,
) -> _HookResponse:
    compact = [
        {"event_id": event["event_id"], "order": event["order"], "summary": event["summary"]}
        for event in events
    ]
    hint = f"\n上一次尝试未通过校验，请修正：{attempt_hint}\n" if attempt_hint else ""
    prompt = f"""下面是短剧第 {episode_no} 集按顺序排列的事件摘要列表（JSON）：
{compact}

任务：
- hook：本集开场钩子，一句话，必须紧扣列表里靠前的某个真实事件，不得脱离事件链凭空编造；
  hook_event_id 填它最贴合的那个 event_id。
- cliffhanger：本集结尾悬念，一句话，必须紧扣列表里靠后的某个真实事件，同样不得凭空编造；
  cliffhanger_event_id 填它最贴合的那个 event_id。
两者都不能为空。
{hint}
"""
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_hook_cliffhanger",
        prompt=prompt,
        model_type=_HookResponse,
        schema_name="episode_prep_pack_hook_v1",
        operation_id=f"episode_prep_pack:{episode_id}:hook",
        max_tokens=1500,
        call_meta={
            "stage_key": "episode_prep_pack_hook_cliffhanger",
            "episode_id": episode_id,
        },
    )


def _validate_hook_grounding(
    text: str, event_id: str, events_by_id: dict[str, dict[str, Any]], *, label: str,
) -> None:
    stripped = (text or "").strip()
    if not stripped:
        raise PrepPackGateError(f"{label} 为空")
    event = events_by_id.get(event_id)
    if event is None:
        raise PrepPackGateError(f"{label}_event_id={event_id!r} 不是事件链中的真实 event_id")
    order = event["order"]
    window = [
        e for e in events_by_id.values()
        if abs(e["order"] - order) <= 2
    ]
    haystack = "。".join(e["summary"] for e in window)
    coverage = bigram_coverage(stripped, haystack)
    if coverage < _HOOK_GROUNDING_COVERAGE:
        raise PrepPackGateError(
            f"{label}「{stripped}」与其接地事件 {event_id} 及相邻事件的文本重合度过低"
            f"（{coverage:.3f} < {_HOOK_GROUNDING_COVERAGE}），疑似编造"
        )


# ---------------------------------------------------------------------------
# One generation attempt
# ---------------------------------------------------------------------------

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
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
    list[dict[str, Any]], list[dict[str, Any]], int,
]:
    conn = get_conn()
    segments = index_source_segments(source_text)
    chunks = _chunk_segments(segments)
    known_characters = _known_character_names(conn, project_id, episode_no)
    known_scenes = _known_scene_names(conn, project_id, episode_no)

    raw_events: list[dict[str, Any]] = []  # fed to build_prep_pack_span_ledger
    events: list[dict[str, Any]] = []  # payload-shaped, built after the gate passes
    # 1.4.1: the model's own paratext claim, aggregated across all chunks --
    # untrusted until app.validators.build_prep_pack_span_ledger's three
    # deterministic gates run over it (see that function's module comment).
    declared_paratext_segments: list[int] = []
    event_counter = 0

    for chunk_index, chunk in enumerate(chunks, start=1):
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
        )
        declared_paratext_segments.extend(response.paratext_segments)
        for model_event in response.events:
            event_counter += 1
            event_id = f"ev_{event_counter:03d}"
            raw_events.append({
                "event_id": event_id,
                "order": event_counter,
                "from_segment": model_event.source_span.from_segment,
                "to_segment": model_event.source_span.to_segment,
                "source_evidence": [
                    {"segment_index": e.segment_index, "quote": e.quote}
                    for e in model_event.source_evidence
                ],
                "key_lines": [
                    {"segment_index": k.segment_index} for k in model_event.key_lines
                ],
            })
            events.append({
                "event_id": event_id,
                "order": event_counter,
                "summary": model_event.summary,
                "chunk_by_index": chunk_by_index,
                "model_event": model_event,
            })

    if not raw_events:
        raise PrepPackGateError("本集未抽取到任何事件", had_events=False)

    ledger, ledger_errors, span_extensions, rejected_paratext_claims = build_prep_pack_span_ledger(
        source_text, events=raw_events, declared_paratext_segments=declared_paratext_segments,
    )
    if ledger_errors:
        raise PrepPackGateError(
            "事件跨度账本存在无效声明：" + "；".join(ledger_errors[:10])
        )
    # Deterministic span extension (ERR-20260824-9babad): a verified quote
    # just outside the raw declared span widens that event's own span --
    # publish the widened boundary, not the raw declaration, so downstream
    # (P1 storyboard) sees the span the ledger actually validated against.
    extended_span_by_event_id = {
        item["event_id"]: (item["from"], item["to"]) for item in span_extensions
    }
    try:
        assert_prep_pack_coverage_complete(ledger)
    except ValueError as exc:
        by_index = {index: segment for index, segment in enumerate(segments, start=1)}
        quoted = "；".join(
            f"编号{index}「{by_index[index].text[:60]}」"
            for index in ledger.get("uncovered") or []
            if index in by_index
        )
        raise PrepPackGateError(
            f"{exc}\n请检查事件的 source_span 是否首尾相接、完整覆盖本集全部编号，"
            f"以下编号未落在任何事件的 span 内：{quoted}"
        ) from exc

    # Gate passed: now build the payload-shaped event_chain, aligning each
    # quote/key_line's excerpt for byte-accurate provenance (reusing the same
    # low-threshold alignment the gate itself used).
    payload_events: list[dict[str, Any]] = []
    for event in events:
        model_event = event["model_event"]
        chunk_by_index = event["chunk_by_index"]
        aligned_evidence: list[dict[str, Any]] = []
        for evidence in model_event.source_evidence:
            source_segment = chunk_by_index.get(evidence.segment_index)
            if source_segment is None:
                continue
            aligned = align_source_excerpt(
                evidence.quote, source_segment.text, min_match_chars=QUOTE_MIN_MATCH_CHARS,
            )
            aligned_evidence.append({
                "segment_index": evidence.segment_index,
                "quote": aligned.excerpt if aligned is not None else evidence.quote,
            })
        aligned_key_lines: list[dict[str, Any]] = []
        for key_line in model_event.key_lines:
            source_segment = chunk_by_index.get(key_line.segment_index)
            aligned = (
                align_source_excerpt(
                    key_line.line, source_segment.text, min_match_chars=QUOTE_MIN_MATCH_CHARS,
                )
                if source_segment is not None else None
            )
            aligned_key_lines.append({
                "speaker": key_line.speaker,
                "line": aligned.excerpt if aligned is not None else key_line.line,
                "segment_index": key_line.segment_index,
            })
        extended = extended_span_by_event_id.get(event["event_id"])
        final_from, final_to = (
            extended if extended is not None
            else (model_event.source_span.from_segment, model_event.source_span.to_segment)
        )
        payload_events.append({
            "event_id": event["event_id"],
            "order": event["order"],
            "summary": event["summary"],
            "source_span": {
                "from_segment": final_from,
                "to_segment": final_to,
            },
            "source_evidence": aligned_evidence,
            "key_lines": aligned_key_lines,
            "characters": [
                {
                    "display_name": c.display_name, "is_background_extra": c.is_background_extra,
                    "suspected_true_name": c.suspected_true_name,
                }
                for c in model_event.characters
            ],
            "scenes": [
                {"display_name": s.display_name, "suspected_true_name": s.suspected_true_name}
                for s in model_event.scenes
            ],
        })

    try:
        assert_prep_pack_span_union_matches_ledger(
            event_spans=[event["source_span"] for event in payload_events],
            ledger=ledger,
        )
    except ValueError as exc:
        # Not a model-variance problem (retrying would reproduce it
        # deterministically) but PrepPackGateError keeps the failure mode
        # uniform with every other gate here rather than a bespoke raise.
        raise PrepPackGateError(str(exc)) from exc

    (
        characters, scenes, functional_extras, asset_errors, discovery_stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = await _run_async_step(
        run_id, "episode_prep_pack_asset_mapping",
        lambda: _resolve_assets(
            conn, project_id=project_id, episode_id=episode_id, episode_no=episode_no,
            source_text=source_text, events=payload_events, run_id=run_id,
        ),
    )
    if asset_errors:
        raise PrepPackGateError(
            "资产映射未能 100% 解析（已尝试身份/场景发现，调用次数："
            f"角色 {discovery_stats['character_discovery_calls']}、"
            f"场景 {discovery_stats['scene_discovery_calls']}）："
            + "；".join(asset_errors[:10])
        )

    # 1.5.2：本集资产名册（characters+functional_extras）此刻已确定性落定，
    # 台词说话人解析走同一份名册 + 项目全谱（不对称三分支，见
    # _prep_pack_resolve_key_line_speakers 上方注释；真实 EP2 回归："韩宗"
    # 第5章才出场却被写成本集说话人，须致命拦截；真实 EP1 回归
    # ERR-20260824-34347a："被困者"这类纯描述性称谓不应该被一同拦下，应
    # 吸收为群演）。
    all_project_character_names = _prep_pack_all_project_character_names(conn, project_id)
    speaker_roster = _prep_pack_build_speaker_roster(characters, functional_extras)
    speaker_errors, absorbed_speakers_count = _run_sync_step(
        run_id, "episode_prep_pack_speaker_resolution",
        lambda: _prep_pack_resolve_key_line_speakers(
            payload_events, speaker_roster,
            all_project_character_names=all_project_character_names,
            functional_extras=functional_extras,
            characters=characters,
        ),
    )
    if speaker_errors:
        raise PrepPackGateError(
            "台词说话人未能全部解析到本集资产名册：" + "；".join(speaker_errors[:10])
        )

    hook_response = await _extract_hook_cliffhanger(
        episode_id=episode_id,
        episode_no=episode_no,
        events=payload_events,
        attempt_hint=attempt_hint,
        run_id=run_id,
    )
    events_by_id = {event["event_id"]: event for event in payload_events}
    _validate_hook_grounding(
        hook_response.hook, hook_response.hook_event_id, events_by_id, label="hook",
    )
    _validate_hook_grounding(
        hook_response.cliffhanger, hook_response.cliffhanger_event_id, events_by_id,
        label="cliffhanger",
    )

    # 1.5.0 散文字段 lint（观测级，不致命，见 _prep_pack_prose_lint_warnings
    # 上方注释）：谱内专名出现在 summary/hook/cliffhanger 里但本集没出场，
    # 记入观测供人审，不阻断——"被提及未出场"是合法场景。
    roster_names = set(speaker_roster) | {
        str(scene.get("display_name") or "") for scene in scenes if scene.get("display_name")
    }
    lint_warnings = _prep_pack_prose_lint_warnings(
        payload_events=payload_events,
        hook=hook_response.hook, cliffhanger=hook_response.cliffhanger,
        known_names=known_characters + known_scenes, roster_names=roster_names,
    )

    asset_manifest = {
        "characters": characters, "scenes": scenes, "functional_extras": functional_extras,
    }
    # provenance 发布前自校验（1.6.0，第25轮收口）：见
    # _prep_pack_verify_manifest_provenance 上方完整说明——每一条非空
    # anchor_phrase 必须真的逐字命中它自己 anchor_segments 指向的原文段，
    # 不成立即门禁拦，不静默发布一份自称有证据、实际验不过的 manifest。
    provenance_errors = _prep_pack_verify_manifest_provenance(segments, asset_manifest)
    if provenance_errors:
        raise PrepPackGateError(
            "资产来源证明自校验失败：" + "；".join(provenance_errors[:10])
        )

    payload = {
        "prep_pack_version": PREP_PACK_VERSION,
        "episode_no": episode_no,
        "episode_scope": {
            "chapter_indexes": chapter_indexes,
            "source_segment_count": len(segments),
        },
        "event_chain": [
            {
                "event_id": event["event_id"],
                "order": event["order"],
                "summary": event["summary"],
                "source_span": event["source_span"],
                "source_evidence": event["source_evidence"],
                "key_lines": event["key_lines"],
            }
            for event in payload_events
        ],
        "asset_manifest": asset_manifest,
        "coverage_ledger": ledger,
        "hook": hook_response.hook.strip(),
        "cliffhanger": hook_response.cliffhanger.strip(),
    }
    return (
        payload, rejected_paratext_claims, true_name_hints, lint_warnings,
        scene_alias_anchors, rejected_alias_conflicts, absorbed_speakers_count,
    )


# ---------------------------------------------------------------------------
# Atomic publish (原子发布 + 完成证书)
# ---------------------------------------------------------------------------

def _publish_prep_pack(
    *,
    episode_id: str,
    payload: dict[str, Any],
    run_id: str | None,
    rejected_paratext_claims: list[dict[str, Any]] | None = None,
    true_name_hints: list[dict[str, Any]] | None = None,
    lint_warnings: list[dict[str, Any]] | None = None,
    scene_alias_anchors: list[dict[str, Any]] | None = None,
    rejected_alias_conflicts: list[dict[str, Any]] | None = None,
    absorbed_speakers_count: int = 0,
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
        transition_step(step_id, "PENDING", "READY", "输入已就绪")
        transition_step(step_id, "READY", "RUNNING", "步骤开始")
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
        raise RuntimeError("分集准备包发布前存在未收口事务")
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
                    # 1.4.1: model's paratext claims that were vetoed back to
                    # ordinary content -- observability only, never part of
                    # the frozen artifact payload itself (see
                    # app.validators.build_prep_pack_span_ledger's
                    # rejected_paratext_claims docstring).
                    "rejected_paratext_claims": rejected_paratext_claims or [],
                    # 1.5.0: every suspected_true_name hypothesis's outcome
                    # (accepted+bound or rejected+discarded) -- observability
                    # only, see _prep_pack_verify_true_name_hypothesis.
                    "true_name_hints": true_name_hints or [],
                    # 1.5.0: prose-field lint warnings (NOT fatal, see
                    # _prep_pack_prose_lint_warnings) -- for human review.
                    "lint_warnings": lint_warnings or [],
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
                    # 1.5.2 (real round-21 EP1 finding ERR-20260824-34347a):
                    # how many key_line speakers were absorbed into
                    # functional_extras (purely descriptive terms with zero
                    # collision against the full project character bible,
                    # e.g. "被困者") rather than blocked or silently trusted.
                    "absorbed_speakers_count": absorbed_speakers_count,
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
            raise ValueError("分集准备包发布 episode 更新发生冲突")
        cliffhanger_value = payload["cliffhanger"]
        conn.execute(
            "UPDATE episodes SET cliffhanger=? WHERE id=?",
            (cliffhanger_value, episode_id),
        )
        conn.execute(
            "UPDATE episodes SET hook=? WHERE project_id=? AND episode_no=?",
            (cliffhanger_value, episode["project_id"], episode["episode_no"] + 1),
        )
        consume_completion_certificate(cert.certificate_id, conn=conn, commit=False)
        conn.commit()
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if step_id:
            transition_step(
                step_id, "RUNNING", "FAILED", str(exc)[:1000],
                decision="escalate", error_code=type(exc).__name__.upper(),
            )
        raise
    if step_id:
        transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept")
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

    退化重试护栏（第23轮真实回归 ERR-20260824-7ab7cb）：EP3 的真实事故——
    尝试1 的事件链抽取拿到 13 个事件，一路通过跨度账本/覆盖完整性/资产映射，
    只在后面某道门禁被拒；尝试2 重新抽取事件链时，模型这次的原始 JSON 本身
    在中途缺了一段结构（截断/自愈失败），格式修复重试拿到的候选又因
    app.harness.model_gateway._latest_json_authority_root 把候选文本尾部一个
    被截断结构"意外重新闭合"出来的嵌套片段误判成独立 root，修复提示词里
    只剩下这个无意义的候选，模型据此"忠实"地把 events 修回空列表——事件链
    整个退化为零。旧逻辑里 attempt_hint/last_error 每轮无条件覆盖，尝试2 的
    "本集未抽取到任何事件"就这样悄悄盖掉了尝试1 更有信息量的失败原因，
    最终报出的错误让人以为这一集彻头彻尾没有事件，实际上只是重试把已经
    抽到的事件弄丢了。护栏：一旦本运行内任何一次尝试抽到过事件
    （PrepPackGateError.had_events=True），后续任何退化为零事件
    （had_events=False）的尝试都不得被当成普通失败静默采纳——必须把两次
    的失败原因合并成一条具名错误，明说"这是一次退化重试，不是从未抽到过
    事件"。只有本运行内全部尝试都是零事件，才维持原始的
    "本集未抽取到任何事件"作为终态。
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
                payload, rejected_paratext_claims, true_name_hints, lint_warnings,
                scene_alias_anchors, rejected_alias_conflicts, absorbed_speakers_count,
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
                true_name_hints=true_name_hints, lint_warnings=lint_warnings,
                scene_alias_anchors=scene_alias_anchors,
                rejected_alias_conflicts=rejected_alias_conflicts,
                absorbed_speakers_count=absorbed_speakers_count,
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
    raise last_error if last_error is not None else RuntimeError("分集准备包生成失败")
