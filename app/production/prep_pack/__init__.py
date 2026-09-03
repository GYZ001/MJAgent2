"""Lightweight episode_prep_pack generation & atomic publish (package).

原 app/production/prep_pack.py（5,289 行）按关注点拆分为本包下的多个模块：
冻结契约常量与版本历史、门禁异常（contracts）、模型响应 schema（schemas）、
分块与已知名称查询（chunking）、画像/场景引用的确定性 DB 绑定
（asset_lookup）、资产来源证明门禁——anchor_phrase 那套（provenance）、别名
归属与跨集别名冲突（alias_resolution）、真名假设的整书卷宗与独立判别
（true_name）、新角色/新场景发现（discovery）、未解析功能性标签候选卷宗
（functional_candidates）与候选判别（functional_candidate_verdict）、主资产
解析编排 `_resolve_assets`（resolve_assets）、分块抽取与 step/telemetry
（chunk_extraction）、单次生成编排 `_generate_prep_pack_once`
（generate_once）、原子发布 `_publish_prep_pack`（publish）、Run/Step 入口
`run_episode_prep_pack`（entry）、未解析/自校验失败场景的就地降级
（scene_degrade，WS6 追加，见该模块 docstring）。

本文件是唯一的稳定入口：全仓所有 `from app.production.prep_pack import X` /
`import app.production.prep_pack` / `prep_pack.X` 使用方式必须不经改动继续
可用——下面按来源模块显式再导出每一个符号（禁止 `from .x import *`，见
app/FILE_CONVENTIONS.toml 的 star_import 闸门）。新增映射台逻辑请加进对应
关注点的子模块，不要加回本文件。

资产来源证明门禁（anchor_phrase，provenance.py）是本包承载的核心正确性
判据，拆分只做了逐字搬移，判据本身一个字都没有改动。

拆包陷阱（monkeypatch）：`monkeypatch.setattr(prep_pack, "name", stub)` 只改
包级重导出，不影响子模块内部已绑定的同名引用——子模块互相调用走的是各自
`from .x import name` 落下的本地绑定，不经过包属性查找。真实测试打桩必须用
tests/conftest.py 的 patch_prep_pack_everywhere()（同构于
patch_stages_everywhere()，见 app/stages/__init__.py 与
tests/test_stages_monkeypatch_guard.py 的先例）。「模块对象本身」的属性打桩
（例如 `prep_pack.model_gateway.chat_structured`，见
tests/test_prep_pack_asset_discovery.py 里的大量用法）不受影响——那是在
`app.harness.model_gateway` 这个共享模块对象上打桩，每个子模块的
`from app.harness import model_gateway` 都指向同一个对象。
"""
from __future__ import annotations

from .alias_resolution import (
    _prep_pack_bible_alias_conflicting_owner,
    _prep_pack_bible_alias_owner,
    _prep_pack_cross_episode_alias_conflict,
    _prep_pack_cross_episode_alias_conflict_legacy_scan,
    _prep_pack_lookup_character_alias_canonical_name,
    _prep_pack_lookup_character_alias_canonical_name_legacy_scan,
    json,
)
from .asset_lookup import (
    Bible,
    _prep_pack_register_scene_alias_if_new,
    _prep_pack_resolve_scene_reference_with_alias,
    _prep_pack_scene_reference_origin_episode,
    _resolve_portrait_id,
    _resolve_scene_reference_id,
    match_scene_name,
)
from .chunk_extraction import (
    _begin_step,
    _call_structured,
    _extract_chunk,
    _finish_step,
    _run_async_step,
    _run_sync_step,
    bind_trace,
    current_trace,
    get_contract,
    nullcontext,
    transition_step,
)
from .chunking import (
    SourceSegment,
    _chunk_segments,
    _known_character_names,
    _known_scene_names,
    _prep_pack_chapter_titles,
    _prep_pack_character_shortlist,
    _prep_pack_gate_segment_indexes,
    _render_chunk,
)
from .contracts import (
    PREP_PACK_VERSION,
    PrepPackGateError,
    QA_PROFILE_VERSION,
    _CHUNK_MAX_CHARS,
    _FALLBACK_VISUAL_STYLE,
    _FUNCTIONAL_RESOLUTION_KINDS,
    _QA_EVALUATOR_NAME,
    annotations,
)
from .discovery import (
    _character_discovery_dispositions,
    _discover_new_characters,
    _discover_new_scenes,
    _discovery_errored_names,
    _load_project_bible,
)
from .entry import run_episode_prep_pack
from .functional_candidate_verdict import (
    _PrepPackFunctionalCandidateVerdict,
    _prep_pack_functional_candidate_call,
    _prep_pack_functional_candidate_pin_segment,
    _prep_pack_resolve_functional_extra_candidate,
    model_gateway,
)
from .functional_candidates import (
    _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_GUARANTEED_ENTRY_MAX_CHARS,
    _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_CHARS,
    _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MAX_ENTRIES,
    _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_MIN_SIDE_ENTRIES,
    _PREP_PACK_FUNCTIONAL_CANDIDATE_DOSSIER_TRUNCATION_MARK,
    _PREP_PACK_FUNCTIONAL_CANDIDATE_NO_MATCH_LABEL,
    _prep_pack_functional_candidate_anchor_pool,
    _prep_pack_functional_candidate_dossier,
    _prep_pack_functional_candidate_label_segments,
    _prep_pack_functional_candidate_names,
    _prep_pack_functional_candidate_roster,
    _prep_pack_functional_candidate_truncate_segment,
)
from .generate_once import (
    _generate_prep_pack_once,
    _prep_pack_build_appellation_map,
    _prep_pack_build_coverage_ledger,
    _prep_pack_finalize_scene_coverage,
    assert_prep_pack_coverage_complete,
    chapter_title_segment_indexes,
    get_conn,
)
from .provenance import (
    _PREP_PACK_QUOTATION_MARKS,
    _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR,
    _PREP_PACK_TERMINAL_MARKS,
    _PREP_PACK_WHITESPACE_RE,
    _prep_pack_citation_forms,
    _prep_pack_first_evidence_segment,
    _prep_pack_local_text_anchor,
    _prep_pack_locate_phrase,
    _prep_pack_locate_verbatim,
    _prep_pack_mention_has_text_evidence,
    _prep_pack_provenance,
    _prep_pack_scene_alias_provenance,
    _prep_pack_verify_manifest_provenance,
    re,
)
from .publish import (
    Evaluation,
    EvidenceArtifact,
    _publish_prep_pack,
    assert_publish_has_certificate,
    consume_completion_certificate,
    issue_completion_certificate,
    now,
    verify_completion_certificate,
)
from .resolve_assets import (
    _prep_pack_build_prop_manifest,
    _resolve_assets,
    visual_entity_id_for_resolution,
)
from .scene_degrade import (
    degrade_scene_provenance_failures,
    degrade_unresolved_scene,
    resolution_hint,
    resolved_scene_delivered_indexes,
    split_scene_errors,
)
from .schemas import (
    Any,
    BaseModel,
    ConfigDict,
    _ChunkResponse,
    _ModelCharacterMention,
    _ModelPropMention,
    _ModelSceneMention,
    _response_format,
)
from .true_name import (
    Literal,
    _PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL,
    _PrepPackTrueNameVerdictResponse,
    _TRUE_NAME_VERDICT_SUBJECT_COPY,
    _prep_pack_collect_true_name_verification_requests,
    _prep_pack_gather_concurrent,
    _prep_pack_sample_dossier_entries_within_budget,
    _prep_pack_true_name_dossier,
    _prep_pack_true_name_pin_dossier_entry,
    _prep_pack_true_name_verdict,
    _prep_pack_true_name_verdict_candidates,
    _prep_pack_true_name_verdict_roster,
    _prep_pack_verify_true_name_hypothesis,
    asyncio,
    evidence_repository,
    index_source_segments,
)
