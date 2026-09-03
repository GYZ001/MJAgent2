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

# 2.0.0: align_source_excerpt/bigram_coverage/assert_prep_pack_span_union_
# matches_ledger/build_prep_pack_span_ledger were only ever used by the
# event_chain/hook/cliffhanger machinery this version removes (quote
# alignment for source_evidence/key_lines, hook/cliffhanger grounding, and
# the event-span coverage ledger respectively) -- no longer imported here.
# build_prep_pack_span_ledger/assert_prep_pack_span_union_matches_ledger
# stay defined in app/validators.py, unused-but-not-deleted (same "dormant,
# not deleted" precedent as app/production/screenplay_repair.py), still
# exercised directly by tests/test_prep_pack_coverage.py.
PREP_PACK_VERSION = "2.0.4"  # 1.1.0: event_chain entries carry source_span (P1 storyboard needs it).
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
#      scene_uncovered，语义见 _prep_pack_build_coverage_ledger 的 docstring，
#      WS6 起与五账合并计算）：不影响、不参与既有五账或 assert_prep_pack_
#      coverage_complete 门禁（该门禁只读 ledger["uncovered"]），单纯让"这一章
#      有多少段落完全没有任何场景归属"在映射台自己的产出里就可见，不用
#      等分镜台的三态告警才第一次被看见。scene_uncovered 非空是合法状态
#      （例如确实没有场景描写的纯心理/纯对白段），这里只记账、不拦截、
#      不用上一段落的场景往后续段落填充——那是伪造归属，比空着更危险。
# 2.0.4（paratext 判定机制归一，logs/paratext_single_source_plan.md，
# prompt-contract 变更，版本推进）：本文件在 1.4.1 之外，独立发明了第三套
# paratext 判据——_extract_chunk 每个 chunk 无条件自报 paratext_segments
# 字段（自己的措辞、默认 temperature=0.2），与世界书 _chapters_without_
# paratext 用的 app.source_paratext.PARATEXT_RULE（temperature=0.0）完全
# 不同源，且从未互相对照。这两套机制对同一份原文（世界书 scope 覆盖的
# 31/1616 章）各判一次，互不知道对方存在；其余 1585 章只有这一套机制
# 判过。改造后：paratext 判定统一为"按章一次、持久化"（chapters.
# paratext_json，PARATEXT_RULE，见 app.source_paratext.chapter_paratext_
# offsets）——世界书和映射台谁先问到某一章，谁就替后来者把这一章算好；
# 本文件不再让模型自报，_ChunkResponse 删除 paratext_segments 字段，
# _extract_chunk 提示词删除对应段落；_generate_prep_pack_once 改为把
# 该集涉及章节持久化的偏移平移到本集 source_text 坐标（与
# app.domain.common._episode_source_blocks 共用同一份拼接口径），投影
# 成确定性的 paratext 段号集合，取代原来的模型自报收集。coverage_ledger.
# paratext 的形状不变（仍是 flat [int] list），下游 storyboard_pack.
# _paratext_segment_indexes 只读这个账、不重新判定，不受影响。同一批
# 持久化偏移也替换了 _discover_new_characters 内部原来独立发起的一次
# strip_paratext 模型调用（世界书覆盖到的章现在零模型调用，命中持久化
# 缓存）。
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

