"""Production Repair 不变量与局部 Patch 测试。"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from app import db, errors
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.narrative_blueprint import (
    BLUEPRINT_PROMPT_VERSION,
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    blueprint_authority_validator_fingerprint,
)
from app.production.certificate import (
    issue_completion_certificate,
    verify_completion_certificate,
)
from app.production.policy import FullRegenDenied, assert_baseline_allowed, assert_patch_ops_allowed
from app.production.revision import (
    bind_unpublished_revision_metadata,
    ensure_production_revision,
    mark_baseline_generated,
    mark_first_evaluation,
    rebind_input_fingerprint,
)
from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION
from app.production.screenplay_document import (
    document_to_screenplay,
    screenplay_to_document,
    apply_field_patch,
)
from app.production.structured_issues import enrich_issues, issue_set_hash, structured_issue
from app.repair_router import route_issues, strategy_for_level, normalize_strategy
from app.schemas import (
    Bible,
    Character,
    EpisodeScreenplay,
    KeyDialogueChain,
    KeyDialogueTurn,
    NarrativeContinuityPlan,
    NarrativeEvent,
    PlotSpine,
    PlotSpineBeat,
    ScriptScene,
    SourceCoverageDecision,
    VoiceCanonical,
    World,
)
from app.screenplay_ir import (
    IR_COMPILER_VERSION,
    IR_VERSION,
    ScreenplayGenerationIR,
)
from app.screenplay_scene_shards import (
    SCREENPLAY_ENVELOPE_VERSION,
    SCREENPLAY_MERGED_IR_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
    ScreenplayEnvelopeExperience,
    ScreenplayEnvelopeIR,
    ScreenplayEnvelopeMetadata,
    blueprint_content_hash,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "prod-repair.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?,?,?)",
        ("proj_p", "生产测试", "created", db.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, created_at, screenplay_status) "
        "VALUES(?,?,?,?,?,?)",
        ("ep_p", "proj_p", 1, "第一集", db.now(), "pending"),
    )
    conn.commit()
    yield


def _minimal_script(**overrides) -> EpisodeScreenplay:
    data = dict(
        episode_no=1,
        title="测试",
        logline="主角要赢",
        dramatic_question="他能否赢？",
        protagonist_goal="赢得资格",
        obstacle="对手阻挠",
        stakes="",
        key_lines=["甲：开始"],
        key_plot_points=["对峙升级"],
        plot_spine=PlotSpine(
            episode_premise="主角要赢",
            spine_beats=[PlotSpineBeat(beat_id="S01", who="甲", does="应战", turn="局势紧张")],
            must_keep_ending="对峙未决",
            drop_list=["闲聊", "风景"],
        ),
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 场地",
                story_function="开端",
                characters=["甲"],
                summary="对峙开始",
                conflict="敌意",
                turn="升级",
                source_basis="原文开头",
            )
        ],
        full_script_text="【场1】夜 / 场地\n甲站在场地中央。\n甲：开始",
        emotional_curve="紧张",
        ending_hook="无集级钩子",
        source_basis="原文",
    )
    data.update(overrides)
    return EpisodeScreenplay(**data)


def _recovery_narrative_plan() -> NarrativeContinuityPlan:
    return NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "propositions": [{
            "proposition_id": "P-identity-jia",
            "semantic_identity_key": "identity-jia",
            "canonical_statement": "甲是本集持续可见且发言的主角。",
            "narrative_domain": "adapted_story",
            "entity_ids": ["character-jia"],
        }],
        "identity_contracts": [{
            "identity_id": "character-jia",
            "display_name": "甲",
            "kind": "named_character",
            "visual_policy": "canonical",
            "visual_canonical": "测试场地中的稳定主角形象",
            "asset_requirement": "required",
            "voice_ids": ["甲"],
            "evidence": {
                "proposition_ids": ["P-identity-jia"],
                "rationale": "剧本场景和对白持续由该命名角色承担。",
            },
        }],
    })


def _test_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _create_current_working_artifact(script: EpisodeScreenplay) -> dict:
    from app.evidence import repository as evidence_repository
    from app.production.patch import screenplay_artifact_payload

    blueprint_value = NarrativeBlueprint(episode_no=1, nodes=[])
    blueprint_hash = blueprint_content_hash(blueprint_value)
    identities: list[dict] = []
    identity_hash = _test_hash(identities)
    blueprint = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_narrative_blueprint",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T1",
        content=blueprint_value.model_dump(mode="json"),
        contract_version=BLUEPRINT_VERSION,
        prompt_version=BLUEPRINT_PROMPT_VERSION,
        model_snapshot={
            "shard_policy_version": BLUEPRINT_SHARD_POLICY_VERSION,
            "local_authority_validator_version": (
                BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
            ),
            "split_manifest_version": BLUEPRINT_SPLIT_MANIFEST_VERSION,
            "source_corpus_hash": "test-source-corpus",
            "validator_fingerprint": (
                blueprint_authority_validator_fingerprint()
            ),
        },
    ))
    identity = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_identity_registry",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T1",
        content={
            "contract_version": "screenplay-identity-registry.v1",
            "identity_registry_hash": identity_hash,
            "identities": identities,
        },
        parent_artifact_ids=[blueprint["id"]],
        contract_version="screenplay-identity-registry.v1",
    ))
    envelope_value = ScreenplayEnvelopeIR(
        episode_no=1,
        metadata=ScreenplayEnvelopeMetadata(),
        experience=ScreenplayEnvelopeExperience(),
        blueprint_hash=blueprint_hash,
        identity_registry_hash=identity_hash,
    )
    envelope_raw = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_envelope_raw",
        scope_type="episode",
        scope_id="ep_p",
        status="candidate",
        trust_level="T0",
        content={"attempts": []},
        parent_artifact_ids=[blueprint["id"], identity["id"]],
        contract_version=SCREENPLAY_ENVELOPE_VERSION,
    ))
    envelope = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_envelope",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T1",
        content=envelope_value.model_dump(mode="json"),
        parent_artifact_ids=[envelope_raw["id"]],
        contract_version=SCREENPLAY_ENVELOPE_VERSION,
    ))
    creative_hash = "a" * 64
    shard_payload = {
        "contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
        "episode_no": 1,
        "shard_id": "SS-test",
        "scene_plan_keys": [],
        "scenes": [],
        "consumed_source_ids": [],
        "unresolved_participants": [],
        "blueprint_hash": blueprint_hash,
        "identity_registry_hash": identity_hash,
        "source_ownership_hash": "ownership",
        "identity_scaffold_hash": "identity",
        "generation_scaffold_hash": "generation",
    }
    reviewed_shard_content_hash = evidence_repository.content_hash(
        shard_payload
    )
    shard_raw = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard_raw",
        scope_type="episode",
        scope_id="ep_p",
        status="candidate",
        trust_level="T0",
        content={
            "semantic_review_evidence": {
                "contract_version": SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
                "initial_creative_hash": creative_hash,
                "reviewed_creative_hash": creative_hash,
                "reviewed_shard_content_hash": reviewed_shard_content_hash,
                "phases": [{
                    "phase": "initial",
                    "creative_hash": creative_hash,
                    "reviews": [{"findings": []}, {"findings": []}],
                    "consensus": [],
                }],
            },
        },
        parent_artifact_ids=[blueprint["id"], identity["id"]],
        contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
    ))
    shard = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T1",
        content=shard_payload,
        parent_artifact_ids=[shard_raw["id"]],
        contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
        model_snapshot={
            "semantic_review_version": SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
            "reviewed_creative_hash": creative_hash,
            "reviewed_shard_content_hash": reviewed_shard_content_hash,
        },
    ))
    merged_value = ScreenplayGenerationIR(
        format_version=IR_VERSION,
        episode_no=1,
        source_semantics={},
        source_audit_annotations=[],
    ).model_dump(mode="json")
    merged = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_generation_ir_merged",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T1",
        content=merged_value,
        parent_artifact_ids=[
            blueprint["id"], identity["id"], envelope["id"], shard["id"],
        ],
        contract_version=SCREENPLAY_MERGED_IR_VERSION,
        model_snapshot={
            "blueprint_hash": blueprint_hash,
            "identity_registry_hash": identity_hash,
        },
    ))
    return evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(script),
        parent_artifact_ids=[merged["id"]],
        contract_version=get_contract("screenplay").version,
        model_snapshot={
            "compiler_version": IR_COMPILER_VERSION,
            "source_merged_content_hash": merged["content_hash"],
        },
    ))


def test_post_baseline_checkpoint_keeps_live_recovered_shard_progress() -> None:
    from types import SimpleNamespace
    from app.production import screenplay_repair

    refreshed = screenplay_repair._checkpoint_after_baseline_generation(
        {
            "planner_version": "planner-v1",
            "shard_progress": {"total": 8, "validated": 7, "failed": 1},
            "shards": [{"shard_id": "SS006", "status": "failed"}],
        },
        SimpleNamespace(checkpoint_json={
            "planner_version": "planner-v1",
            "shard_progress": {"total": 8, "validated": 8, "failed": 0},
            "shards": [{
                "shard_id": "SS006",
                "status": "validated",
                "normalized_artifact_id": "art-ss006",
            }],
        }),
    )

    assert refreshed["shard_progress"] == {
        "total": 8,
        "validated": 8,
        "failed": 0,
    }
    assert refreshed["shards"][0]["status"] == "validated"


def test_contract_upgrade_supersedes_active_revision_instead_of_resuming_old_loop() -> None:
    old = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        input_fingerprint="source-v1",
        contract_version="2.0.0",
        qa_profile_version="qa-v1",
        resume=False,
    )

    new = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        input_fingerprint="source-v1",
        contract_version="3.0.0",
        qa_profile_version="qa-v1",
        resume=True,
    )

    assert new.id != old.id
    assert new.contract_version == "3.0.0"
    old_row = db.get_conn().execute(
        "SELECT status FROM production_revisions WHERE id=?",
        (old.id,),
    ).fetchone()
    assert old_row["status"] == "superseded"


def test_revision_authority_rebind_requires_current_working_artifact() -> None:
    from app.evidence import repository as evidence_repository

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        input_fingerprint="before",
        resume=False,
    )
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="candidate",
        trust_level="T1",
        content={"title": "测试"},
    ))
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )

    rebound = rebind_input_fingerprint(
        revision.id,
        input_fingerprint="after",
        expected_working_artifact_id=artifact["id"],
    )

    assert rebound.input_fingerprint == "after"
    with pytest.raises(ValueError, match="CAS 冲突"):
        rebind_input_fingerprint(
            revision.id,
            input_fingerprint="stale",
            expected_working_artifact_id="art_stale",
        )


def test_unpublished_revision_metadata_only_fills_blank_values() -> None:
    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="storyboard",
        resume=False,
    )

    bound = bind_unpublished_revision_metadata(
        revision.id,
        input_fingerprint="board-v1",
        contract_version="3.0.0",
        qa_profile_version="storyboard-full-gate-2",
    )

    assert bound.input_fingerprint == "board-v1"
    assert bound.contract_version == "3.0.0"
    assert bound.qa_profile_version == "storyboard-full-gate-2"
    assert bind_unpublished_revision_metadata(
        revision.id,
        input_fingerprint="board-v1",
        contract_version="3.0.0",
        qa_profile_version="storyboard-full-gate-2",
    ).id == revision.id
    with pytest.raises(ValueError, match="已绑定其他版本"):
        bind_unpublished_revision_metadata(
            revision.id,
            input_fingerprint="board-v2",
            contract_version="3.0.0",
            qa_profile_version="storyboard-full-gate-2",
        )


def test_screenplay_qa_is_read_only_and_blocks_production_issues() -> None:
    from app.production.screenplay_repair import run_screenplay_qa

    script = _minimal_script()
    before = script.model_dump_json()

    issues, evaluation = run_screenplay_qa(
        script,
        bible=Bible(characters=[], world=World(visual_style_canonical="测试画风")),
        source_text="甲：开始",
        episode={
            "episode_no": 1,
            "target_duration_s": 50,
        },
    )

    assert script.model_dump_json() == before
    assert issues
    assert evaluation.status == "failed"
    assert evaluation.evaluation_role == "runtime_gate"
    assert evaluation.runtime_blocking is True
    assert evaluation.hard_gate_passed is False
    assert evaluation.retry_eligible is True


def test_unresolved_character_identity_is_reported_by_runtime_gate() -> None:
    from app.production.screenplay_repair import (
        non_waivable_screenplay_issues,
        run_screenplay_qa,
    )

    script = _minimal_script()
    issues, evaluation = run_screenplay_qa(
        script,
        bible=Bible(
            characters=[],
            world=World(visual_style_canonical="测试画风"),
        ),
        source_text="甲：开始",
        episode={
            "episode_no": 1,
            "target_duration_s": 50,
        },
    )
    # 即使是脱离持久项目的 fixture，结构 must_fix 也必须保持 runtime gate。
    assert non_waivable_screenplay_issues(issues)
    assert evaluation.runtime_blocking is True

    issues, evaluation = run_screenplay_qa(
        script,
        bible=Bible(
            characters=[Character(
                name="萧炎",
                role="主角",
                appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
            )],
            world=World(visual_style_canonical="测试画风"),
        ),
        source_text="甲：开始",
        episode={
            "episode_no": 1,
            "target_duration_s": 50,
        },
    )
    hard = non_waivable_screenplay_issues(issues)
    assert hard
    assert any(
        issue.code == "CHARACTER_IDENTITY_UNRESOLVED"
        for issue in hard
    )
    assert evaluation.evaluation_role == "runtime_gate"
    assert evaluation.runtime_blocking is True
    assert evaluation.retry_eligible is True


@pytest.mark.asyncio
async def test_retry_exhaustion_never_publishes_unresolved_character_identity(monkeypatch):
    from app.production import screenplay_repair

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        contract_version=get_contract("screenplay").version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    script.narrative_plan = _recovery_narrative_plan()
    script.voice_bible = [VoiceCanonical(speaker_id="甲", voice_canonical="稳定男声")]
    artifact = _create_current_working_artifact(script)
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    mark_first_evaluation(revision.id, "eval_existing")
    issue = structured_issue(
        code="CHARACTER_IDENTITY_UNRESOLVED",
        message="剧本人物身份未解决：「青衣人」",
        subject="screenplay",
        path="/character_identities",
        rule_id="character_identity_must_resolve_before_publish",
        stage="screenplay",
    )
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="test",
        evaluator_version="1",
        status="failed",
        hard_gate_passed=False,
        evaluation_role="business_safety",
        runtime_blocking=True,
        score=0,
        issues=[issue],
    )
    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_qa",
        lambda *_args, **_kwargs: ([issue], evaluation),
    )
    monkeypatch.setattr(screenplay_repair, "plan_screenplay_patch", lambda *_args, **_kwargs: [])

    async def no_llm_patch(*_args, **_kwargs):
        return []

    monkeypatch.setattr(screenplay_repair, "_llm_field_patch", no_llm_patch)
    monkeypatch.setattr(
        "app.portraits.screenplay_unknown_identity_errors",
        lambda *_args, **_kwargs: [],
    )

    def forbidden_publish(**_kwargs):
        raise AssertionError("人物身份 blocker 不得发布")

    monkeypatch.setattr(screenplay_repair, "publish_screenplay", forbidden_publish)

    with pytest.raises(
        screenplay_repair.ScreenplayIdentityGateError,
        match="人物身份预检未通过",
    ):
        await screenplay_repair.run_screenplay_production(
            episode_id="ep_p",
            episode={
                "id": "ep_p",
                "project_id": "proj_p",
                "episode_no": 1,
                "target_duration_s": 50,
            },
            source_text="原文",
            bible=Bible(
                characters=[Character(
                    name="萧炎",
                    role="主角",
                    appearance_canonical="黑发少年，玄色劲装，目光坚定",
                )],
                world=World(visual_style_canonical="测试画风"),
            ),
            resume=True,
        )

    row = db.get_conn().execute(
        "SELECT screenplay_status, screenplay_error FROM episodes WHERE id='ep_p'"
    ).fetchone()
    assert row["screenplay_status"] == "failed"
    assert "人物身份" in row["screenplay_error"]


def test_baseline_counter_and_policy_denies_second_generate():
    rev = ensure_production_revision(episode_id="ep_p", kind="screenplay", resume=False)
    assert rev.baseline_generation_count == 0
    mark_baseline_generated(rev.id, baseline_artifact_id="art_1")
    rev = mark_first_evaluation(rev.id, "eval_1")
    assert rev.baseline_done
    assert rev.first_evaluation_done
    with pytest.raises(FullRegenDenied) as exc:
        assert_baseline_allowed(rev, command="screenplay.generate", episode_id="ep_p")
    assert "FULL_REGEN_AFTER_QA_DENIED" in str(exc.value)


def test_patch_ops_reject_root_replace_and_delete_all():
    with pytest.raises(FullRegenDenied):
        assert_patch_ops_allowed([{"op": "replace", "path": "/", "value": {}}])
    with pytest.raises(FullRegenDenied):
        assert_patch_ops_allowed([
            {"op": "replace_field", "path": "scene_blocks", "value": [{"x": 1}, {"y": 2}]},
        ])
    with pytest.raises(FullRegenDenied):
        assert_patch_ops_allowed([
            {"op": "delete_node", "target": {"kind": "scene", "id": "*"}},
        ])
    # 合法单字段
    assert_patch_ops_allowed([
        {"op": "replace_field", "path": "stakes", "value": "失败将失去资格"},
    ])


def test_create_narrative_node_does_not_treat_event_reference_as_node_identity():
    from app.production.patch import PatchOperation, _create_node

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan(
            scope_id="ep_p",
            events=[NarrativeEvent(event_id="E-5")],
        ),
    )
    operation = PatchOperation(
        op="create_node",
        target={
            "kind": "narrative_node",
            "collection": "setup_payoff_contracts",
            "id": "SP-2",
        },
        value={
            "setup_payoff_id": "SP-2",
            "setup_proposition_ids": ["P-1"],
            "setup_event_ids": ["E-1"],
            "payoff_event_ids": ["E-5"],
            "retention_deadline_event_id": "E-5",
        },
    )

    patched, touched = _create_node(screenplay_to_document(script), operation)

    assert patched.narrative_plan.setup_payoff_contracts[0].setup_payoff_id == "SP-2"
    assert patched.narrative_plan.setup_payoff_contracts[0].retention_deadline_event_id == "E-5"
    assert touched == ["narrative:setup_payoff_contracts:SP-2"]


def test_narrative_graph_normalizer_repairs_unique_source_span_and_event_aliases():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    chapter = "前文。又落榜了……后文。"
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-1",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": "又落榜了……",
            }],
            "adaptation_decisions": [{
                "adaptation_decision_id": "AD-1",
                "affected_event_ids": ["E1"],
            }],
            "dramatic_questions": [{
                "dramatic_question_id": "DQ-1",
                "question_text": "他会如何选择？",
                "open_anchor": {"type": "event", "id": "E1"},
                "resolution_anchor": {"type": "event", "id": "E2"},
            }],
            "evidence": [{
                "evidence_id": "EV-1",
                "anchor": {"type": "event", "id": "E1"},
                "observable_claim": "他再次落榜。",
            }],
            "state_facts": [{
                "fact_id": "F-1",
                "proposition_id": "P-1",
                "subject_id": "甲",
                "predicate_id": "state",
            }],
            "events": [
                {"event_id": "E-1", "precondition_fact_ids": ["F-1"]},
                {"event_id": "E-2"},
            ],
            "character_beliefs": [{
                "character_belief_id": "CB-1",
                "character_id": "甲",
                "anchor": {"type": "event", "id": "E1"},
                "beliefs": [{
                    "proposition_id": "P-1",
                    "stance": "disbelieved",
                }],
            }],
            "setup_payoff_contracts": [{
                "setup_payoff_id": "SP-1",
                "setup_event_ids": ["E1"],
                "payoff_event_ids": ["E2"],
                "retention_deadline_event_id": "E2",
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    source = script.narrative_plan.source_evidence[0]
    assert source.source_span.start == chapter.index(source.verbatim_excerpt)
    assert source.source_span.end == source.source_span.start + len(
        source.verbatim_excerpt
    )
    assert script.narrative_plan.adaptation_decisions[0].affected_event_ids == [
        "E-1"
    ]
    assert script.narrative_plan.dramatic_questions[0].open_anchor.id == "E-1"
    assert script.narrative_plan.dramatic_questions[0].resolution_anchor.id == "E-2"
    assert script.narrative_plan.evidence[0].anchor.id == "E-1"
    payoff = script.narrative_plan.setup_payoff_contracts[0]
    assert payoff.setup_event_ids == ["E-1"]
    assert payoff.payoff_event_ids == ["E-2"]
    assert payoff.retention_deadline_event_id == "E-2"
    assert script.narrative_plan.events[0].proposition_ids == ["P-1"]
    assert script.narrative_plan.character_beliefs[0].beliefs[0].stance == "rejected"
    assert {item["kind"] for item in changes} == {
        "source_span",
        "event_ref",
        "event_refs",
        "event_proposition_refs",
        "belief_stance",
    }


def test_narrative_normalizer_closes_action_facts_and_removes_noop_deltas():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "propositions": [{
                "proposition_id": "P1",
                "semantic_identity_key": "state-change",
                "canonical_statement": "The state changes.",
                "narrative_domain": "adapted_story",
            }],
            "state_facts": [
                {
                    "fact_id": "F1",
                    "proposition_id": "P1",
                    "subject_id": "char-a",
                    "predicate_id": "state",
                },
                {
                    "fact_id": "F2",
                    "proposition_id": "P1",
                    "subject_id": "char-a",
                    "predicate_id": "state",
                },
                {
                    "fact_id": "F3",
                    "proposition_id": "P1",
                    "subject_id": "char-a",
                    "predicate_id": "state",
                },
            ],
            "events": [{
                "event_id": "E1",
                "action_ids": ["A1", "A2"],
            }],
            "atomic_actions": [
                {
                    "action_id": "A1",
                    "actor_ids": ["char-a"],
                    "participant_deliveries": [],
                    "semantic_intent": "Begin changing the state.",
                    "precondition_fact_ids": ["F1"],
                    "effects_add": ["F2"],
                    "effects_remove": ["F1"],
                    "completion_condition": "The intermediate state is visible.",
                },
                {
                    "action_id": "A2",
                    "actor_ids": ["char-a"],
                    "participant_deliveries": [],
                    "semantic_intent": "Finish changing the state.",
                    "precondition_fact_ids": ["F2"],
                    "effects_add": ["F3"],
                    "effects_remove": ["F2"],
                    "completion_condition": "The final state is visible.",
                },
            ],
            "character_beliefs": [{
                "character_belief_id": "CB1",
                "character_id": "char-a",
                "anchor": {"type": "event", "id": "E1"},
                "beliefs": [{
                    "proposition_id": "P1",
                    "stance": "confirmed",
                }],
            }],
            "audience_priors": [{
                "audience_prior_id": "AP1",
                "scope_id": "ep_p",
                "audience_description": "A viewer.",
            }, {
                "audience_prior_id": "AP2",
                "scope_id": "ep_p",
                "audience_description": "A contextual viewer.",
            }],
            "audience_states": [
                {
                    "audience_state_id": "AS-IN",
                    "audience_prior_id": "AP1",
                    "anchor": {"type": "event", "id": "E1"},
                    "beliefs": [{
                        "proposition_id": "P1",
                        "stance": "unknown",
                    }],
                },
                {
                    "audience_state_id": "AS-OUT",
                    "audience_prior_id": "AP1",
                    "anchor": {"type": "event", "id": "E1"},
                    "beliefs": [{
                        "proposition_id": "P1",
                        "stance": "committed",
                    }],
                },
                {
                    "audience_state_id": "AS2",
                    "audience_prior_id": "AP2",
                    "anchor": {"type": "event", "id": "E1"},
                },
            ],
            "experience_intents": [{
                "experience_intent_id": "XI1",
                "scope_id": "ep_p",
                "anchor_event_ids": ["E1"],
                "attention_target_ids": ["P1"],
                "director_objective": "Register the event.",
                "audience_paths": [{
                    "audience_path_id": "XP1",
                    "audience_prior_id": "AP1",
                    "audience_state_in_id": "AS-IN",
                    "audience_state_out_target_id": "AS-OUT",
                    "target_deltas": [{
                        "target_delta_id": "XD-NOOP",
                        "dimension": "attention",
                        "description": "No actual attention change.",
                        "from_state": {"attention_residue_ids": []},
                        "to_state": {"attention_residue_ids": []},
                        "deadline_event_id": "E1",
                        "primary_delivery_window_id": "RW1",
                    }],
                }],
            }],
            "assimilation_tasks": [{
                "assimilation_task_id": "AT1",
                "experience_intent_id": "XI1",
                "audience_path_id": "XP1",
                "target_delta_id": "XD-NOOP",
                "satisfaction_criteria": "No-op task.",
            }],
            "readability_windows": [{
                "readability_window_id": "RW1",
                "event_ids": ["E1"],
                "target_delta_ids": ["XD-NOOP"],
            }],
            "scene_contracts": [{
                "scene_id": "SC1",
                "audience_state_paths": [{
                    "audience_prior_id": "AP1",
                    "audience_state_in_id": "AS-IN",
                    "audience_state_out_target_id": "AS-OUT",
                }],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={},
    )

    event = script.narrative_plan.events[0]
    assert event.precondition_fact_ids == ["F1"]
    assert event.effects_add == ["F3"]
    assert event.effects_remove == ["F1"]
    assert script.narrative_plan.character_beliefs[0].beliefs[0].stance == "believed"
    assert script.narrative_plan.audience_states[1].beliefs[0].stance == "believed"
    deltas = script.narrative_plan.experience_intents[0].audience_paths[0].target_deltas
    assert [delta.target_delta_id for delta in deltas] == ["XP1-belief"]
    assert script.narrative_plan.readability_windows[0].target_delta_ids == [
        "XP1-belief"
    ]
    assert script.narrative_plan.assimilation_tasks == []
    assert {
        path.audience_prior_id
        for path in script.narrative_plan.experience_intents[0].audience_paths
    } == {"AP1", "AP2"}
    ap2_path = next(
        path
        for path in script.narrative_plan.experience_intents[0].audience_paths
        if path.audience_prior_id == "AP2"
    )
    assert len(ap2_path.target_deltas) == 1
    assert ap2_path.target_deltas[0].dimension == "attention"
    assert ap2_path.audience_state_in_id != ap2_path.audience_state_out_target_id
    assert {
        path.audience_prior_id
        for path in script.narrative_plan.scene_contracts[0].audience_state_paths
    } == {"AP1", "AP2"}
    ap2_scene_path = next(
        path
        for path in script.narrative_plan.scene_contracts[0].audience_state_paths
        if path.audience_prior_id == "AP2"
    )
    assert ap2_scene_path.audience_state_in_id == "AS2"
    assert ap2_scene_path.audience_state_out_target_id == "AS2"
    assert {change["kind"] for change in changes} >= {
        "event_action_fact_refs",
        "belief_stance",
        "coarse_audience_path",
        "coarse_scene_audience_path",
        "no_change_target_delta_removed",
        "removed_delta_window_refs",
        "removed_delta_assimilation_tasks",
    }


def test_screenplay_document_patch_stakes_only():
    script = _minimal_script()
    doc = screenplay_to_document(script)
    patched, touched = apply_field_patch(
        doc, path="stakes", value="失败将失去资格与尊严",
        target={"kind": "metadata", "id": "stakes"},
    )
    out = document_to_screenplay(patched)
    assert out.stakes == "失败将失去资格与尊严"
    assert "meta:stakes" in touched
    # 无关场次标题保持
    assert out.scene_outline[0].scene_heading == script.scene_outline[0].scene_heading


# --- spine_beat 补丁落地回归测试（975fa3a）-----------------------------------
# 原缺陷：resolve_field_patch_target/apply_field_patch 都没有 spine_beat 分支，
# 补丁落到 _set_by_dotted 兜底后被写到文档根级或被 model_validate 静默丢弃，
# apply_field_patch 全程不抛异常且 touched 返回"成功"，但文档实际原封不动。
# 下面的测试专门断言字段值真的改变了，而不是只看 touched/是否抛异常。

def _spine_beats_script() -> EpisodeScreenplay:
    return _minimal_script(
        plot_spine=PlotSpine(
            episode_premise="主角要在三次交锋中逆转局势",
            spine_beats=[
                PlotSpineBeat(beat_id="S01", who="甲", does="应战开局", turn="局势紧张"),
                PlotSpineBeat(beat_id="S02", who="乙", does="出手反击", turn="局势胶着"),
                PlotSpineBeat(beat_id="S03", who="甲", does="扭转局势", turn="局势逆转"),
            ],
            must_keep_ending="甲反败为胜",
            drop_list=["闲聊", "风景"],
        ),
    )


def test_spine_beat_patch_actually_lands_on_target_beat_only():
    """核心回归：target={kind:spine_beat,id:<真实id>} 必须真的改到该 beat 的字段，
    且不能波及其它 beat（原缺陷是 touched 报告成功但文档只字未改）。"""
    doc = screenplay_to_document(_spine_beats_script())
    original_beats = {beat.beat_id: beat.does for beat in doc.plot_spine.spine_beats}

    patched, touched = apply_field_patch(
        doc,
        path="does",
        value="乙改变策略，绝地反击",
        target={"kind": "spine_beat", "id": "S02"},
    )

    beats_by_id = {beat.beat_id: beat for beat in patched.plot_spine.spine_beats}
    assert beats_by_id["S02"].does == "乙改变策略，绝地反击"
    assert beats_by_id["S02"].does != original_beats["S02"]
    # 其它 beat 未被波及
    assert beats_by_id["S01"].does == original_beats["S01"]
    assert beats_by_id["S03"].does == original_beats["S03"]
    assert "S02" in touched
    # 传入的原始 doc 不应被就地修改（apply_field_patch 应在副本上操作）
    assert doc.plot_spine.spine_beats[1].does == original_beats["S02"]


@pytest.mark.parametrize("alias", ["spine_beats[2]", "spine_beat_2", "2"])
def test_spine_beat_patch_resolves_all_three_id_aliases(alias):
    """"spine_beats[2]"/"spine_beat_2"/裸"2" 都是同一个 0-based 索引约定，
    三者必须命中同一条 beat（第三条，beat_id="S03"）。"""
    doc = screenplay_to_document(_spine_beats_script())

    patched, touched = apply_field_patch(
        doc,
        path="does",
        value=f"改写-{alias}",
        target={"kind": "spine_beat", "id": alias},
    )

    assert patched.plot_spine.spine_beats[2].beat_id == "S03"
    assert patched.plot_spine.spine_beats[2].does == f"改写-{alias}"
    # 前两条 beat 不受影响
    assert patched.plot_spine.spine_beats[0].does == "应战开局"
    assert patched.plot_spine.spine_beats[1].does == "出手反击"
    assert "S03" in touched


def test_spine_beat_patch_via_dotted_path_drills_into_list_element():
    """不给 target，只给 path="plot_spine.spine_beats[2].does" 时也必须下钻到
    真实的第三条 beat，而不是在 plot_spine 下新建一个字面量
    "spine_beats[2]" 键（这是原 _set_by_dotted 的确切缺陷形态）。"""
    doc = screenplay_to_document(_spine_beats_script())

    patched, touched = apply_field_patch(
        doc,
        path="plot_spine.spine_beats[2].does",
        value="通过点路径改写",
    )

    assert patched.plot_spine.spine_beats[2].beat_id == "S03"
    assert patched.plot_spine.spine_beats[2].does == "通过点路径改写"
    # 列表长度不变，没有多出一个伪造元素/键
    assert len(patched.plot_spine.spine_beats) == 3
    assert [b.beat_id for b in patched.plot_spine.spine_beats] == ["S01", "S02", "S03"]
    assert patched.plot_spine.spine_beats[0].does == "应战开局"
    assert patched.plot_spine.spine_beats[1].does == "出手反击"
    assert touched


def test_spine_beat_patch_raises_when_target_beat_does_not_exist():
    """解析不到目标 beat 时必须显式抛错，不能静默返回"成功"。"""
    doc = screenplay_to_document(_spine_beats_script())

    with pytest.raises(KeyError):
        apply_field_patch(
            doc,
            path="does",
            value="不存在的目标",
            target={"kind": "spine_beat", "id": "S999"},
        )

    with pytest.raises(KeyError):
        apply_field_patch(
            doc,
            path="does",
            value="越界索引",
            target={"kind": "spine_beat", "id": "9999"},
        )


def test_field_patch_raises_on_unknown_root_path():
    """路径首段不是 ScreenplayDocument 字段时，根级兜底必须显式抛错，
    不能像原缺陷那样把值写进文档根级并报告成功。"""
    doc = screenplay_to_document(_spine_beats_script())

    with pytest.raises(KeyError):
        apply_field_patch(
            doc,
            path="totally_unknown_root_field",
            value="不该落地的值",
            target={},
        )


def test_plot_spine_scalar_field_patch_still_uses_legacy_dotted_path():
    """回归保护：plot_spine.episode_premise 这类非索引标量字段不应被新增的
    targets_spine_beat 判断误伤，仍要走原有的 _set_by_dotted 路径正常写入。"""
    doc = screenplay_to_document(_spine_beats_script())
    original_does = [beat.does for beat in doc.plot_spine.spine_beats]

    patched, touched = apply_field_patch(
        doc,
        path="plot_spine.episode_premise",
        value="新的一集前提：三次交锋后逆转",
    )

    assert patched.plot_spine.episode_premise == "新的一集前提：三次交锋后逆转"
    assert touched == ["plot_spine"]
    # spine_beats 完全未受影响
    assert [beat.does for beat in patched.plot_spine.spine_beats] == original_does


def test_source_coverage_field_patch_lands_and_rejects_unknown_id():
    """source_coverage 写侧同类缺口：补丁要真的落地到对应 segment，
    不存在的 id 必须显式抛错。"""
    script = _minimal_script(
        source_coverage=[
            SourceCoverageDecision(
                source_segment_id="SRC0001",
                disposition="deliver",
                projection_policy="picture",
                beat_ids=["S01"],
            ),
            SourceCoverageDecision(
                source_segment_id="SRC0002",
                disposition="deliver",
                projection_policy="picture",
                beat_ids=["S01"],
            ),
        ],
    )
    doc = screenplay_to_document(script)

    patched, touched = apply_field_patch(
        doc,
        path="reason",
        value="补充说明依据",
        target={"kind": "source_coverage", "id": "SRC0002"},
    )

    items_by_id = {item.source_segment_id: item for item in patched.source_coverage}
    assert items_by_id["SRC0002"].reason == "补充说明依据"
    # 未涉及的 segment 不受影响
    assert items_by_id["SRC0001"].reason == ""
    assert "SRC0002" in touched

    with pytest.raises(KeyError):
        apply_field_patch(
            doc,
            path="reason",
            value="不存在的 segment",
            target={"kind": "source_coverage", "id": "SRC-404"},
        )


def test_screenplay_projection_separates_action_labels_and_deduplicates_dialogue():
    script = _minimal_script(
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="任务",
            turns=[KeyDialogueTurn(
                speaker="甲",
                line="今晚七点以前完成。",
                function="announcement",
                source_text="今晚七点以前完成。",
            )],
        )],
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲侧身让出门缝，看向乙：今晚七点以前完成。\n"
            "甲：今晚七点以前完成。\n"
            "银幕上出现画面：旧钟楼在雨中亮起。\n"
            "陌生杀手：你们来晚了。"
        ),
    )

    result = document_to_screenplay(screenplay_to_document(script))

    assert result.full_script_text.count("甲：今晚七点以前完成。") == 1
    assert "甲侧身让出门缝，看向乙：" not in result.full_script_text
    assert "银幕上出现画面，旧钟楼在雨中亮起。" in result.full_script_text
    assert "银幕上出现画面：" not in result.full_script_text
    assert "陌生杀手：你们来晚了。" in result.full_script_text


def test_sync_repair_entry_never_maps_issue_content_to_operations():
    from app.production.screenplay_repair import plan_screenplay_patch

    first = structured_issue(
        code="ARBITRARY_A", message="任意问题 A", subject="screenplay",
        path="/arbitrary/a", stage="screenplay",
    )
    second = structured_issue(
        code="ARBITRARY_B", message="完全不同的问题 B", subject="screenplay",
        path="/arbitrary/b", stage="screenplay",
    )

    assert plan_screenplay_patch(first, _minimal_script()) == []
    assert plan_screenplay_patch(second, _minimal_script()) == []



def test_dialogue_chain_turn_patch_changes_only_opening_source_anchor():
    script = _minimal_script(
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲站在场地中央。\n"
            "测验员：萧炎，斗之力，三段！级别：低级！"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="测验结果公开",
            turns=[KeyDialogueTurn(
                speaker="测验员",
                line="萧炎，斗之力，三段！级别：低级！",
                function="announcement",
                source_text="萧炎，斗之力，三段！级别：低级！",
            )],
        )],
    )
    doc = screenplay_to_document(script)

    patched, touched = apply_field_patch(
        doc,
        path="source_text",
        value="斗之力，三段！",
        target={
            "kind": "dialogue_chain_turn",
            "chain_id": "DC1",
            "turn_index": 0,
        },
    )
    out = document_to_screenplay(patched)

    assert out.dialogue_chains[0].turns[0].source_text == "斗之力，三段！"
    assert out.dialogue_chains[0].turns[0].line == "萧炎，斗之力，三段！级别：低级！"
    assert out.full_script_text == script.full_script_text
    assert touched == ["DC1-T1", "DC1"]


def test_dialogue_chain_replacement_only_selects_existing_body_turns() -> None:
    from app.production.screenplay_repair import (
        _dialogue_chain_replacement_is_local,
        _normalize_dialogue_source_references,
    )

    script = _minimal_script(
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲：救命。\n"
            "乙：你怎么在这里？\n"
            "丙（大声），飞！\n"
            "甲：别听他说，我是被抓来的。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="获救说明",
                turns=[
                    KeyDialogueTurn(
                        speaker="甲",
                        line="救命。",
                        function="trigger",
                        source_text="救命。",
                    ),
                    KeyDialogueTurn(
                        speaker="甲",
                        line="别听他说，我是被抓来的。",
                        function="response",
                        source_text="别听他说，我是被抓来的。",
                    ),
                ],
            ),
        ],
    )
    document = screenplay_to_document(script)
    repaired_turns = [
        {
            "speaker": "甲",
            "line": "救命。",
            "function": "trigger",
            "source_text": "救命。",
        },
        {
            "speaker": "乙",
            "line": "你怎么在这里？",
            "function": "question",
            "source_text": "你怎么在这里？",
        },
        {
            "speaker": "丙",
            "line": "飞！",
            "function": "statement",
            "source_text": "飞！",
        },
        {
            "speaker": "甲",
            "line": "别听他说，我是被抓来的。",
            "function": "response",
            "source_text": "别听他说，我是被抓来的。",
        },
    ]

    assert _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC1",
        turns=repaired_turns,
    )
    assert not _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC1",
        turns=repaired_turns + [{
            "speaker": "乙",
            "line": "正文中不存在的新增对白。",
            "function": "statement",
            "source_text": "原文叙述",
        }],
    )
    assert not _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC1",
        turns=repaired_turns[1:],
    )
    patched, _ = apply_field_patch(
        document,
        path="turns",
        value=repaired_turns,
        target={"kind": "future_dialogue_container", "id": "DC1"},
    )
    projected = document_to_screenplay(patched)
    assert "丙：飞！" in projected.full_script_text
    assert "丙（大声），飞！" not in projected.full_script_text

    source = (
        "“不止是我，还有附近县其他几人，我们都在这里，"
        "孟兄先别说了，快救我们出去。”"
    )
    normalized = _normalize_dialogue_source_references(
        {
            "speaker": "甲",
            "line": "不止是我，还有附近县其他几人，孟兄快救我们出去。",
            "source_text": "不止是我，还有附近县其他几人，孟兄快救我们出去。",
        },
        source,
    )
    assert normalized["source_text"] in source
    assert "我们都在这里" in normalized["source_text"]


def test_empty_dialogue_chain_accepts_only_bounded_source_grounded_turns() -> None:
    from app.production.screenplay_repair import _dialogue_chain_replacement_is_local

    source = "五哥，我不是故意离开的。希望你不要恨我。"
    document = screenplay_to_document(_minimal_script(
        voice_bible=[{
            "speaker_id": "旁白",
            "voice_canonical": "中性平稳的读信语气",
            "role_type": "narrator",
        }],
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC2",
            topic="信件说明",
            turns=[],
        )],
    ))
    turns = [{
        "speaker": "旁白",
        "line": "五哥，我不是故意离开的。",
        "function": "statement",
        "source_text": "五哥，我不是故意离开的。",
    }]

    assert _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC2",
        turns=turns,
        source_text=source,
    )
    assert not _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC2",
        turns=[{**turns[0], "speaker": "未声明人物"}],
        source_text=source,
    )
    assert not _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC2",
        turns=[{**turns[0], "line": "甲" * 37}],
        source_text=source,
    )
    assert not _dialogue_chain_replacement_is_local(
        document,
        chain_id="DC2",
        turns=[{**turns[0], "source_text": "原文中不存在"}],
        source_text=source,
    )


def test_document_projection_inserts_missing_chain_turn_next_to_sibling() -> None:
    script = _minimal_script(
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 放映室",
                story_function="更换保险管并恢复供电",
                characters=["林澈", "苏禾"],
                summary="林澈确认安全后示意苏禾合闸。",
                conflict="设备能否安全通电",
                turn="放映机恢复供电",
                source_basis="原文合闸段落",
            ),
        ],
        full_script_text=(
            "【场1】夜 / 放映室\n"
            "林澈盖好保险仓，退到安全线外。\n"
            "林澈：保险仓盖好了，退到安全线外。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC2",
                topic="确认合闸时机",
                turns=[
                    KeyDialogueTurn(
                        speaker="林澈",
                        line="保险仓盖好了，退到安全线外。",
                        function="statement",
                        source_text="林澈盖好保险仓、退到安全线外。",
                    ),
                    KeyDialogueTurn(
                        speaker="苏禾",
                        line="合闸。",
                        function="decision",
                        source_text="苏禾合上电闸。",
                    ),
                ],
            ),
        ],
    )

    projected = document_to_screenplay(screenplay_to_document(script))
    projected_twice = document_to_screenplay(screenplay_to_document(projected))

    assert "林澈：保险仓盖好了，退到安全线外。\n苏禾：合闸。" in projected.full_script_text
    assert projected.full_script_text.count("苏禾：合闸。") == 1
    assert projected_twice.full_script_text.count("苏禾：合闸。") == 1


def test_document_projection_places_unmatched_turn_in_semantic_scene() -> None:
    script = _minimal_script(
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】日 / 院内",
                story_function="发现屋内异常",
                characters=["钟成"],
                summary="钟成在院内发现异常后准备进屋。",
                conflict="屋内情况不明",
                turn="钟成决定撬锁",
                source_basis="钟成发现门锁。",
            ),
            ScriptScene(
                scene_no=2,
                scene_heading="【场2】日 / 钟成家中",
                story_function="通过信件交付真相",
                characters=["钟成"],
                summary="钟成收到小晶的信并读完。",
                conflict="钟成是否相信信中解释",
                turn="信件改变钟成的决定",
                source_basis="小晶写信解释离开的原因。",
            ),
        ],
        full_script_text=(
            "【场1】日 / 院内\n"
            "钟成在门外想起小晶，却没有见到她。\n"
            "【场2】日 / 钟成家中\n"
            "钟成收到小晶的信，拆开后逐行阅读。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC2",
            topic="小晶给钟成的信",
            turns=[KeyDialogueTurn(
                speaker="旁白",
                line="五哥，我不是故意离开的，希望你不要恨我。",
                function="statement",
                source_text="五哥，我不是故意离开的，希望你不要恨我。",
            )],
        )],
    )

    projected = document_to_screenplay(screenplay_to_document(script))

    scene_two = projected.full_script_text.index("【场2】")
    letter_turn = projected.full_script_text.index("旁白：五哥")
    assert letter_turn > scene_two


def test_document_projection_removes_legacy_cross_scene_prefixed_duplicate() -> None:
    script = _minimal_script(
        scene_outline=[
            ScriptScene(
                scene_no=1,
                scene_heading="【场1】夜 / 山顶",
                story_function="建立等待状态",
                characters=["甲"],
                summary="甲在山顶等待消息并观察远处动静。",
                conflict="消息迟迟没有出现",
                turn="远处传来呼喊",
                source_basis="甲在山顶等待消息。",
            ),
            ScriptScene(
                scene_no=2,
                scene_heading="【场2】夜 / 山腰",
                story_function="完成问答并交付真相",
                characters=["甲", "乙"],
                summary="乙追问真相，甲说明自己被人抓来。",
                conflict="乙不确定甲是否可信",
                turn="甲交代被抓经过",
                source_basis="甲说明自己被人抓来。",
            ),
        ],
        full_script_text=(
            "【场1】夜 / 山顶\n"
            "甲：甲：别听他说，我是被抓来的。\n"
            "【场2】夜 / 山腰\n"
            "乙：你怎么在这里？\n"
            "甲：别听他说，我是被抓来的。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="被抓经过",
                turns=[
                    KeyDialogueTurn(
                        speaker="乙",
                        line="你怎么在这里？",
                        function="question",
                        source_text="你怎么在这里？",
                    ),
                    KeyDialogueTurn(
                        speaker="甲",
                        line="别听他说，我是被抓来的。",
                        function="response",
                        source_text="别听他说，我是被抓来的。",
                    ),
                ],
            ),
        ],
    )

    projected = document_to_screenplay(screenplay_to_document(script))

    assert "甲：甲：别听他说" not in projected.full_script_text
    assert projected.full_script_text.count("甲：别听他说，我是被抓来的。") == 1
    assert projected.scene_outline[1].scene_no == 2


















@pytest.mark.asyncio
async def test_screenplay_repair_delegates_every_issue_to_semantic_planner(
    monkeypatch,
) -> None:
    from app.production import screenplay_repair
    from app.production.patch import PatchOperation

    issue = structured_issue(
        code="ARBITRARY_RELATION_GAP",
        message="开放关系缺口",
        subject="screenplay",
        path="/arbitrary/relation",
        stage="screenplay",
    )
    expected = [PatchOperation(
        op="replace_field",
        path="stakes",
        value="候选值",
        target={"kind": "metadata", "id": "stakes"},
    )]
    calls = []

    async def semantic_planner(*args, **kwargs):
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(screenplay_repair, "_llm_field_patch", semantic_planner)
    operations = await screenplay_repair._plan_screenplay_repair_operations(
        issue,
        _minimal_script(),
        source_text="原文",
        strategy_history={},
    )

    assert operations == expected
    assert len(calls) == 1













def test_full_regen_denied_is_a_policy_conflict_not_a_media_error():
    assert errors.classify(FullRegenDenied("denied")) == (
        "conflict",
        "FULL-REGEN-DENIED",
    )


@pytest.mark.asyncio
async def test_existing_baseline_resumes_qa_without_calling_full_generation(monkeypatch):
    from app import stages
    from app.production import screenplay_authority, screenplay_repair

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        contract_version=get_contract("screenplay").version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    script.narrative_plan = _recovery_narrative_plan()
    script.voice_bible = [VoiceCanonical(speaker_id="甲", voice_canonical="稳定男声")]
    artifact = _create_current_working_artifact(script)
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    mark_first_evaluation(revision.id, "eval_existing")

    async def forbidden_baseline(*_args, **_kwargs):
        raise AssertionError("resume must not call full screenplay generation")

    monkeypatch.setattr(stages, "generate_screenplay_baseline", forbidden_baseline)
    monkeypatch.setattr(
        screenplay_authority,
        "screenplay_authority_fingerprint",
        lambda *_args, **_kwargs: "authority-test",
    )
    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_qa",
        lambda *_args, **_kwargs: (
            [],
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="test",
                evaluator_version="1",
                status="passed",
                hard_gate_passed=True,
                score=100,
                    evidence={
                        "authority_input_fingerprint": "authority-test",
                    },
            ),
        ),
    )
    published: list[str] = []

    def fake_publish(**kwargs):
        published.append(kwargs["artifact_id"])
        return {"status": "ready", "artifact_id": kwargs["artifact_id"]}

    monkeypatch.setattr(screenplay_repair, "publish_screenplay", fake_publish)

    result = await screenplay_repair.run_screenplay_production(
        episode_id="ep_p",
        episode={
            "id": "ep_p",
            "project_id": "proj_p",
            "episode_no": 1,
            "target_duration_s": 50,
        },
        source_text="原文",
        bible=Bible(
            characters=[Character(
                name="甲",
                role="主角",
                appearance_canonical="测试场地中的稳定主角形象",
            )],
            world=World(visual_style_canonical="测试画风"),
        ),
        resume=True,
    )

    assert result.title == script.title
    resumed = screenplay_repair.get_production_revision(revision.id)
    assert resumed is not None
    assert published == [resumed.working_artifact_id]
    assert resumed.working_artifact_id != artifact["id"]
    assert resumed.baseline_generation_count == 1
    assert resumed.checkpoint_json["phase"] == "SUCCEEDED"




@pytest.mark.asyncio
async def test_runtime_qa_repairs_then_stops_without_publishing(monkeypatch):
    from app.production import screenplay_repair
    from app.production.patch import PatchOperation, PatchResult

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        contract_version=get_contract("screenplay").version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    script.narrative_plan = _recovery_narrative_plan()
    script.voice_bible = [VoiceCanonical(speaker_id="甲", voice_canonical="稳定男声")]
    artifact = _create_current_working_artifact(script)
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    mark_first_evaluation(revision.id, "eval_existing")
    issue = structured_issue(
        code="BUSINESS_RULE_FAILED",
        message="测试同一问题没有进展",
        subject="screenplay",
        path="/test",
        rule_id="stalled",
        stage="screenplay",
    )
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="test",
        evaluator_version="1",
        status="failed",
        hard_gate_passed=False,
        score=90,
        issues=[issue],
    )
    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_qa",
        lambda *_args, **_kwargs: ([issue], evaluation),
    )
    planned = 0

    def fake_plan(*_args, **_kwargs):
        nonlocal planned
        planned += 1
        return [PatchOperation(
            op="replace_field",
            path=f"attempt_{planned}",
            value="same",
            target={"kind": "test", "id": str(planned)},
        )]

    attempts = 0

    def fake_apply(request, *, episode_id, character_resolutions=None):
        del character_resolutions
        nonlocal attempts
        attempts += 1
        return PatchResult(
            ok=False,
            before_artifact_id=request.expected_artifact_id,
            error="no-op Patch 已拒绝",
            failure_kind="no_op",
        )

    monkeypatch.setattr(screenplay_repair, "plan_screenplay_patch", fake_plan)
    monkeypatch.setattr(screenplay_repair, "apply_screenplay_patch", fake_apply)
    monkeypatch.setattr(
        screenplay_repair,
        "publish_screenplay",
        lambda **_kwargs: {"artifact_id": artifact["id"], "status": "ready"},
    )

    with pytest.raises(screenplay_repair.ScreenplayNarrativeGateError):
        await screenplay_repair.run_screenplay_production(
            episode_id="ep_p",
            episode={
                "id": "ep_p",
                "project_id": "proj_p",
                "episode_no": 1,
                "target_duration_s": 50,
            },
            source_text="原文",
            bible=Bible(
                characters=[],
                world=World(visual_style_canonical="测试画风"),
            ),
            resume=True,
        )

    assert attempts > 0
    assert planned > 0
    completed = screenplay_repair.get_production_revision(revision.id)
    assert completed is not None
    assert completed.working_artifact_id
    assert completed.checkpoint_json["phase"] == "WAITING_HUMAN"


@pytest.mark.asyncio
async def test_invalid_modern_narrative_graph_enters_patch_loop(monkeypatch):
    from app.production import screenplay_repair

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        contract_version=get_contract("screenplay").version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    script = _minimal_script(
        stakes="失败将失去资格",
        narrative_plan=NarrativeContinuityPlan(scope_id="ep_p"),
    )
    artifact = _create_current_working_artifact(script)
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )

    def reached_patch_loop(*_args, **_kwargs):
        raise RuntimeError("entered narrative patch loop")

    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_qa",
        reached_patch_loop,
    )
    monkeypatch.setattr(
        "app.portraits.screenplay_unknown_identity_errors",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(RuntimeError, match="entered narrative patch loop"):
        await screenplay_repair.run_screenplay_production(
            episode_id="ep_p",
            episode={
                "id": "ep_p",
                "project_id": "proj_p",
                "episode_no": 1,
                "target_duration_s": 50,
            },
            source_text="原文",
            bible=Bible(
                characters=[],
                world=World(visual_style_canonical="测试画风"),
            ),
            resume=True,
        )


@pytest.mark.asyncio
async def test_resume_replays_persisted_identity_before_first_qa(monkeypatch):
    from app.production import screenplay_repair
    from app.portraits import apply_screenplay_character_resolutions

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        contract_version=get_contract("screenplay").version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    script.narrative_plan = _recovery_narrative_plan()
    script.voice_bible = [VoiceCanonical(speaker_id="甲", voice_canonical="稳定男声")]
    script.scene_outline[0].characters.append("青衣人")
    script.full_script_text += "\n青衣人：此路不通。"
    artifact = _create_current_working_artifact(script)
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )

    identity_replay_count = 0

    def apply_identity_once(candidate, resolutions):
        nonlocal identity_replay_count
        identity_replay_count += 1
        return apply_screenplay_character_resolutions(candidate, resolutions)

    def stop_after_identity(candidate, **_kwargs):
        assert "青衣人" not in candidate.scene_outline[0].characters
        assert "路人甲" in candidate.scene_outline[0].characters
        assert "路人甲：此路不通。" in candidate.full_script_text
        raise RuntimeError("identity replay verified")

    monkeypatch.setattr(
        "app.portraits.apply_screenplay_character_resolutions",
        apply_identity_once,
    )
    monkeypatch.setattr(screenplay_repair, "run_screenplay_qa", stop_after_identity)

    with pytest.raises(RuntimeError, match="identity replay verified"):
        await screenplay_repair.run_screenplay_production(
            episode_id="ep_p",
            episode={
                "id": "ep_p",
                "project_id": "proj_p",
                "episode_no": 1,
                "target_duration_s": 50,
                "character_resolutions": [{
                    "source_label": "青衣人",
                        "canonical_name": "路人甲",
                        "resolution": "functional_extra",
                        "decision_provenance": "manual",
                    }],
            },
            source_text="青衣人拦路。",
            bible=Bible(characters=[], world=World(visual_style_canonical="测试画风")),
            resume=True,
        )

    updated = screenplay_repair.get_production_revision(revision.id)
    assert updated is not None
    assert updated.working_artifact_id != artifact["id"]
    assert identity_replay_count == 1


@pytest.mark.asyncio
async def test_identity_replay_with_unchanged_payload_reaches_qa(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production import screenplay_repair
    from app.portraits import apply_screenplay_character_resolutions

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        contract_version=get_contract("screenplay").version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    script.narrative_plan = _recovery_narrative_plan()
    script.voice_bible = [VoiceCanonical(speaker_id="甲", voice_canonical="稳定男声")]
    artifact = _create_current_working_artifact(script)
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )

    def report_non_material_change(candidate, _resolutions):
        before = screenplay_repair.screenplay_artifact_payload(candidate)
        changes = apply_screenplay_character_resolutions(candidate, [{
            "source_label": "许师姐",
            "canonical_name": "许清",
            "resolution": "future_identity",
        }])
        assert screenplay_repair.screenplay_artifact_payload(candidate) == before
        return changes or [{
            "source_label": "许师姐",
            "canonical_name": "许清",
            "resolution": "future_identity",
        }]

    def stop_at_qa(*_args, **_kwargs):
        raise RuntimeError("qa reached")

    monkeypatch.setattr(
        "app.portraits.apply_screenplay_character_resolutions",
        report_non_material_change,
    )
    monkeypatch.setattr(screenplay_repair, "run_screenplay_qa", stop_at_qa)

    with pytest.raises(RuntimeError, match="qa reached"):
        await screenplay_repair.run_screenplay_production(
            episode_id="ep_p",
            episode={
                "id": "ep_p",
                "project_id": "proj_p",
                "episode_no": 1,
                "target_duration_s": 50,
                "character_resolutions": [{
                    "source_label": "许师姐",
                    "canonical_name": "许清",
                    "resolution": "future_identity",
                }],
            },
            source_text="原文",
            bible=Bible(characters=[], world=World(visual_style_canonical="测试画风")),
            resume=True,
        )

    updated = screenplay_repair.get_production_revision(revision.id)
    assert updated is not None
    derived = evidence_repository.get_artifact(updated.working_artifact_id)
    assert derived is not None
    assert derived["version"] == artifact["version"] + 1


@pytest.mark.asyncio
async def test_recorded_repair_resume_skips_character_discovery_model_call(monkeypatch):
    from app.domain import screenplay_ops

    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        resume=False,
    )
    script = _minimal_script(stakes="失败将失去资格")
    script.narrative_plan = _recovery_narrative_plan()
    working = _create_current_working_artifact(script)
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=working["id"],
        working_artifact_id=working["id"],
    )

    async def forbidden_discovery(*_args, **_kwargs):
        raise AssertionError("repair resume must skip character discovery")

    captured_preflight: list[dict] = []

    async def fake_screenplay_task(_episode_id, *, preflight_result=None):
        captured_preflight.append(preflight_result)
        return _minimal_script(stakes="失败将失去资格")

    monkeypatch.setattr(
        screenplay_ops,
        "_screenplay_character_discovery",
        forbidden_discovery,
    )
    monkeypatch.setattr(screenplay_ops, "_screenplay_task", fake_screenplay_task)
    recorder = screenplay_ops._new_screenplay_recorder(
        "ep_p",
        trigger_type="resume",
    )
    db.get_conn().execute(
        "UPDATE episodes SET screenplay_status='queued',active_screenplay_run_id=? "
        "WHERE id='ep_p'",
        (recorder.run_id,),
    )
    db.get_conn().commit()

    result = await screenplay_ops._recorded_screenplay_task("ep_p", recorder)

    assert result is not None
    assert captured_preflight == [{
        "added": [],
        "resolutions": [],
        "skipped": "baseline_identity_already_resolved",
    }]


def test_structured_issue_has_stable_path():
    issues = enrich_issues([
        Issue(
            code="DRAMATIC_CONTRACT_INCOMPLETE",
            severity=IssueSeverity.BLOCKER,
            subject="screenplay",
            message="stakes 不能为空",
            repairable=True,
        )
    ], stage="screenplay")
    assert issues[0].evidence.get("path")
    assert issues[0].evidence.get("must_fix") is True
    assert issue_set_hash(issues)


def test_certificate_binds_hash_and_rejects_mismatch(monkeypatch):
    from app.evidence import repository as evidence_repository

    art = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id="ep_p",
            status="validated",
            trust_level="T2",
            content={"ok": True, "stakes": "代价"},
        )
    )
    h = art["content_hash"] or evidence_repository.content_hash(art["content"])
    evaluation = evidence_repository.create_evaluation(
        art["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version="screenplay-qa-1",
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            score=100,
        ),
    )
    cert = issue_completion_certificate(
        kind="screenplay",
        scope_id="ep_p",
        artifact_id=art["id"],
        artifact_hash=h,
        contract_version="1",
        qa_profile_version="screenplay-qa-1",
        evaluation_ids=[evaluation["id"]],
    )
    verify_completion_certificate(cert, expected_artifact_hash=h)
    with pytest.raises(ValueError):
        verify_completion_certificate(cert, expected_artifact_hash="deadbeef")
    db.get_conn().execute(
        "UPDATE artifacts SET content_json=? WHERE id=?",
        (json.dumps({"ok": True, "stakes": "已篡改"}, ensure_ascii=False), art["id"]),
    )
    db.get_conn().commit()
    with pytest.raises(ValueError, match="内容指纹漂移"):
        verify_completion_certificate(cert)
    db.get_conn().execute(
        "UPDATE artifacts SET content_json=? WHERE id=?",
        (json.dumps(art["content"], ensure_ascii=False), art["id"]),
    )
    db.get_conn().commit()
    db.get_conn().execute(
        "UPDATE artifacts SET status='stale' WHERE id=?",
        (art["id"],),
    )
    db.get_conn().commit()
    with pytest.raises(ValueError, match="artifact 范围或当前状态已失效"):
        verify_completion_certificate(cert)
    verify_completion_certificate(
        cert,
        allow_stale_artifact_for_revision=True,
    )


def test_certificate_issue_rejects_tampered_artifact_outer_hash() -> None:
    from app.evidence import repository as evidence_repository

    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T2",
        content={"ok": True, "stakes": "代价"},
    ))
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET content_json=? WHERE id=?",
        (json.dumps({"ok": True, "stakes": "已篡改"}, ensure_ascii=False), artifact["id"]),
    )
    conn.commit()

    with pytest.raises(ValueError, match="不得绑定内容指纹漂移"):
        issue_completion_certificate(
            kind="screenplay",
            scope_id="ep_p",
            artifact_id=artifact["id"],
            artifact_hash=artifact["content_hash"],
        )


def test_update_working_artifact_cas_rejects_tampered_current_payload() -> None:
    from app.evidence import repository as evidence_repository
    from app.production.revision import update_working_artifact

    revision = ensure_production_revision(
        episode_id="ep_p", kind="screenplay", resume=False
    )
    current = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T2",
        content={"screenplay_metadata": {"episode_no": 1}},
    ))
    replacement = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T2",
        content={"screenplay_metadata": {"episode_no": 1}, "next": True},
    ))
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=current["id"],
        working_artifact_id=current["id"],
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET content_json=? WHERE id=?",
        (json.dumps({"tampered": True}), current["id"]),
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="content hash drift"):
        update_working_artifact(
            revision.id,
            replacement["id"],
            expected_hash=current["content_hash"],
        )
    assert conn.execute(
        "SELECT working_artifact_id FROM production_revisions WHERE id=?",
        (revision.id,),
    ).fetchone()[0] == current["id"]


def test_certificate_derives_blockers_from_evaluation() -> None:
    from app.evidence import repository as evidence_repository

    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T1",
        content={"screenplay_metadata": {"episode_no": 1}},
        contract_version="2.0.0",
    ))
    issue = structured_issue(
        code="SPINE_MISSING",
        message="主线节拍未交付",
        subject="screenplay",
        path="/plot_spine",
        rule_id="spine_delivery",
        stage="screenplay",
    )
    evaluation = evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version="screenplay-qa-gate-2",
            status="failed",
            hard_gate_passed=False,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            issues=[issue],
        ),
    )

    with pytest.raises(ValueError, match="仍含 1 个 blocker"):
        issue_completion_certificate(
            kind="screenplay",
            scope_id="ep_p",
            artifact_id=artifact["id"],
            artifact_hash=artifact["content_hash"],
            contract_version="2.0.0",
            qa_profile_version="screenplay-qa-gate-2",
            evaluation_ids=[evaluation["id"]],
            blockers=0,
            must_fix_issues=0,
        )


def test_modern_certificate_rejects_missing_narrative_plan() -> None:
    from app.evidence import repository as evidence_repository

    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T1",
        content={"screenplay_metadata": {"episode_no": 1}},
        contract_version="4.0.0",
    ))
    evaluation = evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version="screenplay-qa-gate-2",
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            issues=[],
        ),
    )

    with pytest.raises(ValueError, match="要求 narrative_plan"):
        issue_completion_certificate(
            kind="screenplay",
            scope_id="ep_p",
            artifact_id=artifact["id"],
            artifact_hash=artifact["content_hash"],
            contract_version="4.0.0",
            qa_profile_version="screenplay-qa-gate-2",
            evaluation_ids=[evaluation["id"]],
        )


def test_repair_router_no_longer_emits_redo_or_replan():
    assert strategy_for_level("L3") == "insert_shot"
    assert strategy_for_level("L4") == "split_shot"
    assert normalize_strategy("redo_suffix") == "redo_suffix"
    assert normalize_strategy("replan_outline") == "replan_outline"

    plan = route_issues([
        structured_issue(
            code="SPINE_MISSING",
            message="第 5 镜缺少 must_keep spine",
            subject="shot:5",
            path="/shots/5",
            related_node_ids=["shot:5"],
            stage="storyboard",
        )
    ], validated_prefix_end=4, next_shot_no=5)
    assert plan.strategy == "repair_window"
    assert plan.needs_semantic_selection is True
    assert "insert_shot" in {candidate.strategy for candidate in plan.candidates}
    assert all(
        candidate.strategy not in {"redo_suffix", "replan_outline"}
        for candidate in plan.candidates
    )
    assert plan.strategy not in {"redo_suffix", "replan_outline"}

    capacity = route_issues([
        structured_issue(
            code="SPOKEN_CAPACITY_EXCEEDED",
            message="第 9 镜必保留台词超过 10 秒容量，请拆镜",
            subject="shot:9",
            path="/shots/9",
            stage="storyboard",
        )
    ], validated_prefix_end=8, next_shot_no=9, current_level="L4")
    assert capacity.strategy == "repair_window"
    assert capacity.needs_semantic_selection is True
    assert "split_adjacent_shot" in {
        candidate.strategy for candidate in capacity.candidates
    }
    assert all(
        candidate.strategy not in {"redo_suffix", "replan_outline"}
        for candidate in capacity.candidates
    )








def test_apply_screenplay_patch_cas_and_noop(monkeypatch):
    from app.evidence import repository as evidence_repository
    from app.production.patch import PatchOperation, PatchRequest, apply_screenplay_patch
    from app.production.patch import screenplay_artifact_payload

    rev = ensure_production_revision(episode_id="ep_p", kind="screenplay", resume=False)
    script = _minimal_script(stakes="")
    payload = screenplay_artifact_payload(script)
    art = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id="ep_p",
            status="candidate",
            trust_level="T1",
            content=payload,
        )
    )
    mark_baseline_generated(rev.id, baseline_artifact_id=art["id"], working_artifact_id=art["id"])
    before_hash = art["content_hash"] or evidence_repository.content_hash(art["content"])

    ok = apply_screenplay_patch(
        PatchRequest(
            production_revision_id=rev.id,
            expected_artifact_id=art["id"],
            expected_hash=before_hash,
            operations=[PatchOperation(op="replace_field", path="stakes", value="失败失去资格")],
            idempotency_key="k1",
        ),
        episode_id="ep_p",
    )
    assert ok.ok
    assert ok.after_artifact_id
    assert ok.after_hash != before_hash

    # no-op
    art2 = evidence_repository.get_artifact(ok.after_artifact_id)
    h2 = art2["content_hash"] or evidence_repository.content_hash(art2["content"])
    noop = apply_screenplay_patch(
        PatchRequest(
            production_revision_id=rev.id,
            expected_artifact_id=ok.after_artifact_id,
            expected_hash=h2,
            operations=[PatchOperation(op="replace_field", path="stakes", value="失败失去资格")],
            idempotency_key="k2",
        ),
        episode_id="ep_p",
    )
    assert not noop.ok
    assert "no-op" in (noop.error or "")


def test_apply_screenplay_patch_rejects_tampered_baseline_before_write() -> None:
    from app.evidence import repository as evidence_repository
    from app.production.patch import PatchOperation, PatchRequest, apply_screenplay_patch
    from app.production.patch import screenplay_artifact_payload

    rev = ensure_production_revision(episode_id="ep_p", kind="screenplay", resume=False)
    payload = screenplay_artifact_payload(_minimal_script(stakes=""))
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T2",
        content=payload,
    ))
    mark_baseline_generated(
        rev.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    tampered = dict(payload)
    tampered["_tampered"] = True
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET content_json=? WHERE id=?",
        (json.dumps(tampered, ensure_ascii=False), artifact["id"]),
    )
    conn.commit()
    before_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    result = apply_screenplay_patch(
        PatchRequest(
            production_revision_id=rev.id,
            expected_artifact_id=artifact["id"],
            expected_hash=artifact["content_hash"],
            operations=[
                PatchOperation(
                    op="replace_field",
                    path="stakes",
                    value="不得被应用",
                )
            ],
        ),
        episode_id="ep_p",
    )

    assert not result.ok
    assert result.failure_kind == "invalid_artifact"
    assert "存储指纹漂移" in (result.error or "")
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == before_count


def test_source_span_normalizer_maps_hard_wrapped_import_text():
    from app.narrative import (
        normalize_source_evidence_text,
        validate_screenplay_narrative,
    )
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    chapter = "开头。\n阿宾考上私立专校，在学校旁边租了间学\n生房，只在周末回家。\n结尾。"
    excerpt = "阿宾考上私立专校，在学校旁边租了间学生房，只在周末回家。"
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-1",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": excerpt,
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    evidence = script.narrative_plan.source_evidence[0]
    raw_slice = chapter[evidence.source_span.start:evidence.source_span.end]
    assert normalize_source_evidence_text(raw_slice) == (
        normalize_source_evidence_text(excerpt)
    )
    assert any(change["kind"] == "source_span" for change in changes)
    validation_errors = validate_screenplay_narrative(
        script,
        require=True,
        expected_scope_id="ep_p",
        authorized_source_chapters={"1": chapter},
    )
    assert not any(
        "SOURCE_SPAN_EXACT_MISMATCH" in error for error in validation_errors
    )


def test_narrative_normalizer_closes_unique_effect_and_perceiver_refs():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "propositions": [{
                "proposition_id": "P1",
                "semantic_identity_key": "result-visible",
                "canonical_statement": "甲完成了动作",
                "narrative_domain": "adapted_story",
                "entity_ids": ["char-a"],
            }],
            "events": [{
                "event_id": "E1",
                "proposition_ids": ["P1"],
                "action_ids": ["A1"],
                "effects_add": ["F2"],
            }],
            "atomic_actions": [{
                "action_id": "A1",
                "actor_ids": ["char-a"],
                "participant_deliveries": [],
                "semantic_intent": "完成动作",
                "effects_add": ["F2"],
                "completion_condition": "动作结果可见",
                "temporal_phases": [{
                    "phase_id": "A1/P1",
                    "start_condition": "动作开始",
                    "end_condition": "动作完成",
                    "estimated_min_s": 1.0,
                }],
            }],
            "evidence": [{
                "evidence_id": "EV1",
                "anchor": {"type": "event", "id": "E1"},
                "observable_claim": "甲完成动作",
                "perceivable_by": ["audience"],
                "supports_proposition_ids": ["P1"],
            }],
            "character_beliefs": [{
                "character_belief_id": "CB1",
                "character_id": "char-a",
                "anchor": {"type": "event", "id": "E1"},
                "perceived_evidence_ids": ["EV1"],
                "decision_proposition_ids": ["P1"],
                "decision_basis_ids": ["EV1"],
                "decision_action_ids": ["A1"],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={},
    )

    fact = next(
        item for item in script.narrative_plan.state_facts
        if item.fact_id == "F2"
    )
    evidence = script.narrative_plan.evidence[0]
    assert fact.proposition_id == "P1"
    assert fact.subject_id == "char-a"
    assert "char-a" in evidence.perceivable_by
    assert {item["kind"] for item in changes} >= {
        "missing_effect_fact",
        "evidence_perceiver",
    }


def test_narrative_normalizer_projects_unique_arc_inference_to_setup_promise():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "propositions": [
                {
                    "proposition_id": "P-SETUP",
                    "semantic_identity_key": "setup",
                    "canonical_statement": "观众先看到明确铺垫",
                    "narrative_domain": "adapted_story",
                },
                {
                    "proposition_id": "P-INFERENCE",
                    "semantic_identity_key": "inference",
                    "canonical_statement": "结尾形成推论",
                    "narrative_domain": "adapted_story",
                },
            ],
            "setup_payoff_contracts": [{
                "setup_payoff_id": "SP-1",
                "setup_proposition_ids": ["P-SETUP"],
                "intended_inference_ids": ["P-INFERENCE"],
            }],
            "arc_contracts": [{
                "arc_id": "ARC-1",
                "promise_proposition_ids": ["P-INFERENCE"],
                "payoff_contract_ids": ["SP-1"],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={},
    )

    assert script.narrative_plan.arc_contracts[0].promise_proposition_ids == [
        "P-SETUP"
    ]
    assert any(
        change["kind"] == "arc_promise_setup_projection"
        for change in changes
    )


def test_narrative_normalizer_keeps_ambiguous_arc_promise_for_repair():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "propositions": [
                {
                    "proposition_id": proposition_id,
                    "semantic_identity_key": proposition_id.casefold(),
                    "canonical_statement": proposition_id,
                    "narrative_domain": "adapted_story",
                }
                for proposition_id in ("P-SETUP-1", "P-SETUP-2", "P-INFERENCE")
            ],
            "setup_payoff_contracts": [{
                "setup_payoff_id": "SP-1",
                "setup_proposition_ids": ["P-SETUP-1", "P-SETUP-2"],
                "intended_inference_ids": ["P-INFERENCE"],
            }],
            "arc_contracts": [{
                "arc_id": "ARC-1",
                "promise_proposition_ids": ["P-INFERENCE"],
                "payoff_contract_ids": ["SP-1"],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={},
    )

    assert script.narrative_plan.arc_contracts[0].promise_proposition_ids == [
        "P-INFERENCE"
    ]
    assert not any(
        change["kind"] == "arc_promise_setup_projection"
        for change in changes
    )


def test_narrative_normalizer_removes_unbacked_promise_when_question_remains():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "propositions": [{
                "proposition_id": "P-GOAL",
                "semantic_identity_key": "goal",
                "canonical_statement": "主角希望拿到资格",
                "narrative_domain": "adapted_story",
            }],
            "arc_contracts": [{
                "arc_id": "ARC-1",
                "core_question_ids": ["DQ-1"],
                "promise_proposition_ids": ["P-GOAL"],
                "payoff_contract_ids": [],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={},
    )

    arc = script.narrative_plan.arc_contracts[0]
    assert arc.core_question_ids == ["DQ-1"]
    assert arc.promise_proposition_ids == []
    assert any(
        change["kind"] == "arc_unsupported_promise_removed"
        and change["unsupported"] == ["P-GOAL"]
        for change in changes
    )


def test_spine_spoken_clause_accepts_visible_action_performance():
    from app.validators import validate_screenplay_spine_delivery

    script = _minimal_script(
        plot_spine=PlotSpine(
            episode_premise="胡太太邀请阿宾帮忙整理家",
            spine_beats=[PlotSpineBeat(
                beat_id="S05",
                who="胡太太",
                does="邀请阿宾帮忙整理家，许诺晚上请他吃饭，阿宾答应",
                turn="两人开始共同整理",
                must_keep=True,
            )],
            must_keep_ending="两人开始共同整理客厅",
            drop_list=["无关闲聊", "重复环境描写"],
        ),
    )
    action_text = (
        "胡太太邀请阿宾帮忙整理家，许诺晚上请他吃饭，"
        "阿宾点头答应，两人开始搬动家具。"
    )

    assert validate_screenplay_spine_delivery(
        script,
        action_text=action_text,
    ) == []


def test_source_span_normalizer_expands_one_uniquely_proven_elision():
    from app.narrative import (
        normalize_source_evidence_text,
        validate_screenplay_narrative,
    )
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    first = "胡太太请阿宾帮忙拿包裹，两人一起走到六楼客厅。"
    omitted = "阿宾上楼时看见窗外景色，又在门口停留了一会儿。"
    second = "胡太太询问阿宾下午是否有空，请他一起整理家具。"
    chapter = f"前文。{first}{omitted}{second}后文。"
    excerpt = first + second
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-1",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": excerpt,
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    evidence = script.narrative_plan.source_evidence[0]
    raw_slice = chapter[evidence.source_span.start:evidence.source_span.end]
    assert normalize_source_evidence_text(raw_slice) == (
        normalize_source_evidence_text(evidence.verbatim_excerpt)
    )
    assert omitted in evidence.verbatim_excerpt
    assert any(
        change["kind"] == "source_excerpt_expanded" for change in changes
    )
    validation_errors = validate_screenplay_narrative(
        script,
        require=True,
        expected_scope_id="ep_p",
        authorized_source_chapters={"1": chapter},
    )
    assert not any(
        "SOURCE_SPAN_EXACT_MISMATCH" in error for error in validation_errors
    )


def test_source_span_normalizer_expands_long_unique_elision():
    from app.narrative import normalize_source_evidence_text
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    first = "调查员进入仓库并确认门锁完好，随后开始逐项核对货架。"
    omitted = "中间保存完整授权正文。" * 400
    second = "调查员在最深处找到遗失的蓝色档案盒并完成登记。"
    chapter = first + omitted + second
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-LONG",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": first + "……" + second,
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    evidence = script.narrative_plan.source_evidence[0]
    raw_slice = chapter[evidence.source_span.start:evidence.source_span.end]
    assert normalize_source_evidence_text(raw_slice) == (
        normalize_source_evidence_text(evidence.verbatim_excerpt)
    )
    assert omitted in evidence.verbatim_excerpt
    assert any(
        change["kind"] == "source_excerpt_expanded" for change in changes
    )


def test_source_span_normalizer_uses_linked_proposition_for_duplicate_excerpt():
    from app.production.screenplay_repair import (
        _normalize_screenplay_narrative_graph,
    )

    excerpt = "咚咚"
    first = "旧仓库的木钟发出咚咚声，值班员随手关上了门。"
    second = "调查员追到地下室，听见咚咚声后找到了被困的同伴。"
    chapter = first + "无关过渡。" * 80 + second
    expected_start = chapter.rindex(excerpt)
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan.model_validate({
            "scope_id": "ep_p",
            "source_evidence": [{
                "source_evidence_id": "SE-DUP",
                "source_span": {"chapter_id": "1", "start": 0, "end": 2},
                "verbatim_excerpt": excerpt,
            }],
            "propositions": [{
                "proposition_id": "P-RESCUE",
                "canonical_statement": "调查员追到地下室并找到被困的同伴",
                "narrative_domain": "source_canon",
                "direct_source_evidence_ids": ["SE-DUP"],
            }],
        }),
    )

    changes = _normalize_screenplay_narrative_graph(
        script,
        authorized_source_chapters={"1": chapter},
    )

    evidence = script.narrative_plan.source_evidence[0]
    assert evidence.source_span.start == expected_start
    assert evidence.source_span.end == expected_start + len(excerpt)
    assert any(change["kind"] == "source_span" for change in changes)


def test_gate_failure_summary_prioritizes_actual_failed_issue():
    from app.production.screenplay_repair import _gate_failure_message

    backlog = structured_issue(
        code="KEY_LINE_MISSING",
        message="对白链跨场",
        subject="screenplay",
        path="/dialogue_chains",
        stage="screenplay",
    )
    failed = structured_issue(
        code="AUDIENCE_BELIEF_DIFF_UNASSIGNED",
        message="观众信念变化缺少命题绑定",
        subject="screenplay",
        path="/audience_paths/XP-1",
        stage="screenplay",
    )

    message = _gate_failure_message([backlog, failed], failed_issue=failed)

    assert "自动修复停止于 AUDIENCE_BELIEF_DIFF_UNASSIGNED" in message
    assert message.index(failed.message) < message.index(backlog.message)


def test_issue_selection_preserves_validator_order_when_metadata_ties():
    from app.production.screenplay_repair import _choose_issue

    first = structured_issue(
        code="Z_FIRST_BY_VALIDATOR",
        message="上游问题",
        subject="screenplay",
        stage="screenplay",
    )
    second = structured_issue(
        code="A_SECOND_BY_VALIDATOR",
        message="下游问题",
        subject="screenplay",
        stage="screenplay",
    )

    assert _choose_issue([first, second]) is first


def test_narrative_patch_retargets_ancestor_to_named_direct_field_owner():
    from app.production.screenplay_repair import _resolve_narrative_patch_owner

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "experience_intents": [{
            "experience_intent_id": "XI-1",
            "scope_id": "ep_p",
            "director_objective": "测试观众路径",
            "audience_paths": [
                {
                    "audience_path_id": "XP-AP1-1",
                    "audience_prior_id": "AP-1",
                    "audience_state_in_id": "AS-1-IN",
                    "audience_state_out_target_id": "AS-1-OUT",
                    "target_deltas": [],
                },
                {
                    "audience_path_id": "XP-AP2-1",
                    "audience_prior_id": "AP-2",
                    "audience_state_in_id": "AS-2-IN",
                    "audience_state_out_target_id": "AS-2-OUT",
                    "target_deltas": [],
                },
            ],
        }],
    }).model_dump(mode="json")
    issue = structured_issue(
        code="AUDIENCE_BELIEF_DIFF_UNASSIGNED",
        message="XP-AP2-1 信念变化没有绑定相应命题",
        subject="screenplay",
        path="/audience_belief_diff_unassigned",
        stage="screenplay",
    )

    resolved = _resolve_narrative_patch_owner(
        plan["experience_intents"],
        node_id="XI-1",
        patch_field="target_deltas",
        issue=issue,
    )

    assert resolved is not None
    owner, owner_id = resolved
    assert owner_id == "XP-AP2-1"
    assert owner["audience_path_id"] == "XP-AP2-1"


def test_narrative_node_alias_resolves_unique_schema_collection():
    from app.production.screenplay_repair import (
        _narrative_collection_for_node,
    )

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "events": [
            {"event_id": "E3", "effects_add": ["F-5"]},
            {"event_id": "E4", "effects_add": ["F-5"]},
        ],
    }).model_dump(mode="json")

    assert _narrative_collection_for_node(plan, "E3") == "events"
    assert _narrative_collection_for_node(plan, "missing") is None


def test_new_narrative_node_infers_collection_from_stable_identity_field():
    from app.production.screenplay_repair import (
        _narrative_collection_for_new_node,
    )

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "character_beliefs": [{
            "character_belief_id": "CB-1",
            "character_id": "甲",
            "anchor": {"type": "event", "id": "E1"},
        }],
    }).model_dump(mode="json")
    new_belief = {
        "character_belief_id": "CB-2",
        "character_id": "乙",
        "anchor": {"type": "event", "id": "E2"},
    }

    assert _narrative_collection_for_new_node(
        plan,
        "CB-2",
        new_belief,
    ) == "character_beliefs"


def test_new_belief_candidate_routes_to_narrative_preflight():
    from app.production.screenplay_repair import (
        _candidate_targets_narrative_graph,
    )

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "character_beliefs": [{
            "character_belief_id": "CB-1",
            "character_id": "甲",
            "anchor": {"type": "event", "id": "E1"},
        }],
    }).model_dump(mode="json")
    candidate = {
        "operations": [{
            "op": "create_node",
            "target": {
                "kind": "character_belief",
                "id": "CB-2",
                "parent_id": "narrative_plan",
                "parent_field": "character_beliefs",
            },
            "value": {
                "character_belief_id": "CB-2",
                "character_id": "乙",
                "anchor": {"type": "event", "id": "E2"},
            },
        }],
    }

    assert _candidate_targets_narrative_graph(candidate, plan) is True


def test_top_level_narrative_parent_is_removed_with_explicit_collection():
    from app.production.screenplay_repair import (
        _normalize_top_level_narrative_parent,
    )

    target = _normalize_top_level_narrative_parent(
        {
            "kind": "character_belief",
            "collection": "character_beliefs",
            "id": "CB-2",
            "parent_id": "narrative_plan",
            "parent_field": "character_beliefs",
        },
        collection="character_beliefs",
        plan_data={"scope_id": "ep_p"},
    )

    assert "parent_id" not in target
    assert "parent_field" not in target
    assert target["collection"] == "character_beliefs"


def test_event_fact_patch_expands_to_its_single_atomic_action():
    from app.production.patch import PatchOperation
    from app.production.screenplay_repair import (
        _expand_single_action_event_closure,
    )

    plan = NarrativeContinuityPlan.model_validate({
        "scope_id": "ep_p",
        "events": [{
            "event_id": "E4",
            "action_ids": ["A2"],
            "effects_add": ["F-5"],
        }],
        "atomic_actions": [{
            "action_id": "A2",
            "actor_ids": ["甲"],
            "participant_deliveries": [],
            "semantic_intent": "完成动作",
            "effects_add": ["F-5"],
            "completion_condition": "动作完成",
            "temporal_phases": [{
                "phase_id": "A2/P1",
                "start_condition": "开始",
                "end_condition": "完成",
                "estimated_min_s": 1.0,
            }],
        }],
    }).model_dump(mode="json")
    operations = [PatchOperation(
        op="replace_field",
        path="effects_add",
        value=[],
        target={"kind": "event", "id": "E4"},
    )]

    expanded = _expand_single_action_event_closure(operations, plan)

    assert len(expanded) == 2
    derived = expanded[1]
    assert derived.path == "effects_add"
    assert derived.value == []
    assert derived.target["collection"] == "atomic_actions"
    assert derived.target["id"] == "A2"


def test_dialogue_turn_target_derives_chain_and_index_from_stable_turn_id():
    from app.production.screenplay_document import screenplay_to_document
    from app.production.screenplay_repair import (
        _resolve_dialogue_chain_turn_target,
    )

    script = _minimal_script(dialogue_chains=[KeyDialogueChain(
        chain_id="DC3",
        topic="整理家",
        turns=[
            KeyDialogueTurn(
                speaker="甲",
                line="开始整理。",
                function="statement",
                source_text="开始整理。",
            ),
            KeyDialogueTurn(
                speaker="乙",
                line="好的。",
                function="response",
                source_text="好的。",
            ),
        ],
    )])

    target = _resolve_dialogue_chain_turn_target(
        screenplay_to_document(script),
        target={"kind": "dialogue_chain_turn", "id": "DC3-T2"},
        patch_field="line",
    )

    assert target is not None
    assert target["chain_id"] == "DC3"
    assert target["turn_index"] == 1


def test_candidate_executability_uses_production_patch_path():
    from app.production.screenplay_repair import (
        _candidate_is_executable,
    )

    document = screenplay_to_document(_minimal_script())
    destructive = {
        "operations": [{
            "op": "replace_field",
            "path": "dialogue_turns",
            "target": {"kind": "scene", "id": "SC01"},
            "value": [],
        }],
    }
    bounded = {
        "operations": [{
            "op": "replace",
            "path": "text",
            "target": {"kind": "future_action_type", "id": "AC01-01"},
            "value": "甲在场地中央站定，准备应战。",
        }],
    }

    assert _candidate_is_executable(destructive, document) is False
    assert _candidate_is_executable(bounded, document) is True


def test_document_candidate_preflight_selects_passing_minimal_subset():
    from app.production.screenplay_repair import _preflight_document_candidate
    from app.validators import validate_screenplay

    source = (
        "“救命。” “你是谁？” “还有其他人。” "
        "“你们怎么来的？” “飞！” “别听他胡说，因为我们是被抓来的。” "
        "“我不信。”"
    )
    script = _minimal_script(
        full_script_text=(
            "【场1】夜 / 场地\n"
            "甲：救命。\n"
            "乙：你是谁？\n"
            "甲：还有其他人。\n"
            "乙：你们怎么来的？\n"
            "丙（大声），飞！\n"
            "甲：别听他胡说，因为我们是被抓来的。\n"
            "乙：我不信。"
        ),
        dialogue_chains=[
            KeyDialogueChain(
                chain_id="DC1",
                topic="说明来历",
                turns=[
                    KeyDialogueTurn(
                        speaker="甲",
                        line="救命。",
                        function="trigger",
                        source_text="救命。",
                    ),
                    KeyDialogueTurn(
                        speaker="乙",
                        line="你是谁？",
                        function="question",
                        source_text="你是谁？",
                    ),
                    KeyDialogueTurn(
                        speaker="甲",
                        line="别听他胡说，因为我们是被抓来的。",
                        function="response",
                        source_text="别听他胡说，因为我们是被抓来的。",
                    ),
                    KeyDialogueTurn(
                        speaker="乙",
                        line="我不信。",
                        function="response",
                        source_text="我不信。",
                    ),
                ],
            ),
        ],
    )
    script = document_to_screenplay(screenplay_to_document(script))
    errors = validate_screenplay(
        script,
        Bible(characters=[], world=World(visual_style_canonical="测试画风")),
        expected_beats=1,
        episode_no=1,
        source_text=source,
        require_dialogue_chains=True,
    )
    message = next(error for error in errors if "主线对白上下文断裂" in error)
    issue = structured_issue(
        code="KEY_LINE_MISSING",
        message=message,
        subject="screenplay",
        path="/dialogue_chains",
        rule_id="key_line_context",
        stage="screenplay",
    )
    candidate = {
        "candidate_id": "CAND-PASSING-BACKUP",
        "expected_narrative_gain": 1.0,
        "destructive_cost": 0.1,
        "operations": [
            {
                "op": "replace",
                "path": "turns",
                "target": {"kind": "future_dialogue_type", "id": "DC1"},
                "value": [
                    *[
                        turn.model_dump(mode="json")
                        for turn in script.dialogue_chains[0].turns[:2]
                    ],
                    {
                        "speaker": "乙",
                        "line": "你们怎么来的？",
                        "function": "question",
                        "source_text": "你们怎么来的？",
                    },
                    {
                        "speaker": "丙",
                        "line": "飞！",
                        "function": "statement",
                        "source_text": "飞！",
                    },
                    *[
                        turn.model_dump(mode="json")
                        for turn in script.dialogue_chains[0].turns[2:]
                    ],
                ],
            },
            {
                "op": "replace",
                "path": "unknown_field",
                "target": {"kind": "future_node", "id": "UNKNOWN"},
                "value": "ignored",
            },
        ],
    }

    operations = _preflight_document_candidate(
        candidate,
        document=screenplay_to_document(script),
        source_text=source,
        issue=issue,
    )

    assert len(operations) == 1
    assert operations[0].path == "turns"
    assert operations[0].target["id"] == "DC1"


@pytest.mark.asyncio
async def test_semantic_patch_retries_rejected_and_duplicate_candidates(monkeypatch):
    from app.production import screenplay_repair
    from app.production.patch import PatchOperation

    issue = structured_issue(
        code="AUDIENCE_BELIEF_DIFF_UNASSIGNED",
        message="XP-1 缺少命题绑定",
        subject="screenplay",
        path="/audience_paths/XP-1",
        stage="screenplay",
    )
    rejected = PatchOperation(
        op="replace_field",
        path="stakes",
        value="失败代价",
        target={"kind": "metadata", "id": "stakes"},
    )
    fresh = PatchOperation(
        op="replace_field",
        path="obstacle",
        value="新增阻力",
        target={"kind": "metadata", "id": "obstacle"},
    )
    attempts: list[tuple[int, list[str]]] = []

    async def fake_once(*_args, planner_attempt=1, rejection_feedback=None, **_kwargs):
        attempts.append((planner_attempt, list(rejection_feedback or [])))
        if planner_attempt == 1:
            return []
        if planner_attempt == 2:
            return [rejected]
        return [fresh]

    monkeypatch.setattr(screenplay_repair, "_llm_field_patch_once", fake_once)

    operations = await screenplay_repair._llm_field_patch(
        issue,
        _minimal_script(),
        source_text="原文",
        strategy_history=["fill_stakes"],
    )

    assert operations == [fresh]
    assert [attempt for attempt, _feedback in attempts] == [1, 2, 3]
    assert not attempts[0][1]
    assert "未通过本地结构" in attempts[1][1][0]
    assert "fill_stakes" in attempts[2][1][-1]


@pytest.mark.asyncio
async def test_semantic_patch_budget_preserves_local_prompt_and_truncation_error(
    monkeypatch,
):
    from app import hiagent
    from app.harness import model_gateway
    from app.production import screenplay_repair

    issue = structured_issue(
        code="EVENT_EFFECT_MISSING",
        message="E-LOCAL 缺少结果状态",
        subject="screenplay",
        path="/events/E-LOCAL",
        related_node_ids=["E-LOCAL"],
        stage="screenplay",
    )
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan(
            scope_id="ep_test",
            events=[
                NarrativeEvent(event_id="E-LOCAL"),
                NarrativeEvent(event_id="E-UNRELATED"),
            ],
        ),
    )
    captured: dict = {}
    truncation = hiagent.ProviderError(
        "模型输出因响应 token 预算耗尽而截断",
        failure_kind=hiagent.ProviderFailureKind.OUTPUT_TRUNCATED,
        delivery_state="responded",
        replay_safe=False,
    )

    async def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        raise truncation

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError) as raised:
        await screenplay_repair._llm_field_patch_once(
            issue,
            script,
            source_text="局部事件原文",
        )

    assert raised.value is truncation
    kwargs = captured["kwargs"]
    assert kwargs["max_tokens"] == 8192
    assert kwargs["call_meta"]["requested_max_tokens"] == 8192
    prompt = json.loads(captured["messages"][1]["content"])
    scoped_events = prompt["screenplay_document"]["narrative_plan"]["events"]
    assert [event["event_id"] for event in scoped_events] == ["E-LOCAL"]
    assert prompt["screenplay_document"]["narrative_graph_id_index"]["events"] == [
        "E-LOCAL",
        "E-UNRELATED",
    ]


@pytest.mark.asyncio
async def test_semantic_patch_repairs_unescaped_inner_quotes(monkeypatch):
    from app import schemas
    from app.harness import model_gateway
    from app.production import screenplay_repair

    issue = structured_issue(
        code="MUST_KEEP_SPINE_BEAT_NOT_PERFORMED",
        message='节拍"许清判定资质"未完整演出',
        subject="screenplay",
        path="/scene_blocks/SC03",
        stage="screenplay",
    )
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan(scope_id="ep_test"),
    )
    options: dict = {}

    async def fake_chat(*_args, **_kwargs):
        return '{"candidate_plans":[]}'

    def fake_extract(text, **kwargs):
        options.update(kwargs)
        return {"candidate_plans": []}

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(schemas, "extract_json", fake_extract)

    result = await screenplay_repair._llm_field_patch_once(
        issue,
        script,
        source_text="许清说道：有些资质。",
    )

    assert result == []
    assert options["repair_unescaped_inner_quotes"] is True


@pytest.mark.asyncio
async def test_semantic_patch_prompt_declares_dialogue_turn_contract(monkeypatch):
    import json

    from app import config
    from app.harness import model_gateway
    from app.production import screenplay_repair

    issue = structured_issue(
        code="KEY_LINE_MISSING",
        message="dialogue_chains[0].turns 需包含 1~8 个连续话轮",
        subject="screenplay",
        path="/dialogue_chains",
        stage="screenplay",
    )
    script = _minimal_script(
        narrative_plan=NarrativeContinuityPlan(scope_id="ep_test"),
    )
    prompts: list[dict] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(json.loads(messages[1]["content"]))
        return '{"candidate_plans":[]}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = await screenplay_repair._llm_field_patch_once(
        issue,
        script,
        source_text="五哥，我不是故意离开的。",
    )

    assert result == []
    contract = prompts[0]["operation_contract"]["dialogue_chain_turns"]
    assert str(config.MAX_SPOKEN_CHARS_PER_SHOT) in contract["line"]
    assert "response" in contract["function"]
    assert "authorized_source_excerpt" in contract["source_text"]
    rules = "\n".join(prompts[0]["rules"])
    assert "禁止输出 narration" in rules


def test_candidate_issue_diff_allows_aggregate_error_to_shrink() -> None:
    from app.production.screenplay_repair import _introduced_issue_messages

    baseline = [structured_issue(
        code="SPINE_MISSING",
        message="缺失 4 条主线节拍",
        subject="screenplay",
        path="/plot_spine",
        rule_id="message_before",
        stage="screenplay",
    )]
    reduced = [structured_issue(
        code="SPINE_MISSING",
        message="缺失 2 条主线节拍",
        subject="screenplay",
        path="/plot_spine",
        rule_id="message_before",
        stage="screenplay",
    )]

    assert _introduced_issue_messages(baseline, reduced) == []
    assert _introduced_issue_messages(baseline, [structured_issue(
        code="SPINE_MISSING",
        message="另一条规则的新错误",
        subject="screenplay",
        path="/plot_spine",
        rule_id="message_after",
        stage="screenplay",
    )]) == ["另一条规则的新错误"]
    assert _introduced_issue_messages(baseline, [
        *reduced,
        structured_issue(
            code="SPINE_MISSING",
            message="另一条独立缺失",
            subject="screenplay",
            path="/plot_spine",
            rule_id="second_slot",
            stage="screenplay",
        ),
    ]) == ["另一条独立缺失"]
    assert _introduced_issue_messages(baseline, [structured_issue(
        code="SPINE_MISSING",
        message="新场次缺失",
        subject="screenplay",
        path="/scene_blocks/SC04",
        stage="screenplay",
    )]) == ["新场次缺失"]


def test_screenplay_narrative_gate_is_quality_error():
    from app.production.screenplay_repair import ScreenplayNarrativeGateError

    assert errors.classify(ScreenplayNarrativeGateError("门禁未通过")) == (
        "quality_gate",
        "QA",
    )


@pytest.mark.asyncio
async def test_recorded_narrative_gate_preserves_repair_state_and_fails_run(
    monkeypatch,
):
    from app.domain import screenplay_ops
    from app.production import screenplay_repair

    message = "剧本工作稿已保留，但叙事/质量硬门禁仍未通过，禁止发布"

    async def fake_discovery(*_args, **_kwargs):
        return {"added": [], "resolutions": [], "warnings": []}

    async def fake_production(*_args, **_kwargs):
        from app.evidence import repository as evidence_repository
        from app.production.revision import save_checkpoint

        conn = db.get_conn()
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id="ep_p",
            status="validated",
            trust_level="T2",
            content={"candidate": "repair-working"},
        ))
        revision = ensure_production_revision(
            episode_id="ep_p",
            kind="screenplay",
            resume=False,
        )
        mark_baseline_generated(
            revision.id,
            baseline_artifact_id=artifact["id"],
            working_artifact_id=artifact["id"],
        )
        save_checkpoint(revision.id, {
            "phase": "WAITING_HUMAN",
            "working_artifact_id": artifact["id"],
            "yield_reason": "narrative_gate_needs_review",
        })
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
            "working_screenplay_artifact_id=?, screenplay_production_revision_id=? "
            "WHERE id='ep_p'",
            (message, artifact["id"], revision.id),
        )
        conn.commit()
        raise screenplay_repair.ScreenplayNarrativeGateError(message)

    monkeypatch.setattr(
        screenplay_ops,
        "_screenplay_character_discovery",
        fake_discovery,
    )
    monkeypatch.setattr(
        screenplay_repair,
        "run_screenplay_production",
        fake_production,
    )
    recorder = screenplay_ops._new_screenplay_recorder("ep_p")
    db.get_conn().execute(
        "UPDATE episodes SET screenplay_status='queued',active_screenplay_run_id=? "
        "WHERE id='ep_p'",
        (recorder.run_id,),
    )
    db.get_conn().commit()

    result = await screenplay_ops._recorded_screenplay_task("ep_p", recorder)

    assert result is None
    episode = db.get_conn().execute(
        "SELECT screenplay_status, screenplay_error,working_screenplay_artifact_id,"
        "screenplay_production_revision_id FROM episodes WHERE id='ep_p'"
    ).fetchone()
    run = db.get_conn().execute(
        "SELECT status, failure_code, failure_message FROM workflow_runs WHERE id=?",
        (recorder.run_id,),
    ).fetchone()
    step = db.get_conn().execute(
        "SELECT status, error_code FROM step_runs "
        "WHERE run_id=? AND step_key='screenplay_document'",
        (recorder.run_id,),
    ).fetchone()
    error_log = db.get_conn().execute(
        "SELECT code, category FROM error_logs WHERE action='screenplay_repair' "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()

    assert episode["screenplay_status"] == "repairing"
    assert episode["screenplay_error"] == message
    assert episode["working_screenplay_artifact_id"]
    assert episode["screenplay_production_revision_id"]
    assert db.get_conn().execute(
        "SELECT status FROM production_revisions WHERE id=?",
        (episode["screenplay_production_revision_id"],),
    ).fetchone()["status"] == "active"
    assert run["status"] == "FAILED"
    assert run["failure_code"] == "SCREENPLAYNARRATIVEGATEERROR"
    assert run["failure_message"] == message
    assert step["status"] == "FAILED"
    assert step["error_code"] == "SCREENPLAYNARRATIVEGATEERROR"
    assert error_log["code"] == "QA"
    assert error_log["category"] == "quality_gate"


@pytest.mark.asyncio
async def test_active_recovery_run_reuses_prebaseline_identity_checkpoint(
    monkeypatch,
) -> None:
    from app.domain import screenplay_ops
    from app.evidence import repository as evidence_repository
    from app.production.revision import save_checkpoint

    blueprint_value = NarrativeBlueprint(episode_no=1, nodes=[])
    blueprint = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_narrative_blueprint",
        scope_type="episode",
        scope_id="ep_p",
        status="validated",
        trust_level="T1",
        content=blueprint_value.model_dump(mode="json"),
        contract_version=BLUEPRINT_VERSION,
        prompt_version=BLUEPRINT_PROMPT_VERSION,
        model_snapshot={
            "shard_policy_version": BLUEPRINT_SHARD_POLICY_VERSION,
            "local_authority_validator_version": (
                BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
            ),
            "split_manifest_version": BLUEPRINT_SPLIT_MANIFEST_VERSION,
            "source_corpus_hash": "test-source-corpus",
            "validator_fingerprint": (
                blueprint_authority_validator_fingerprint()
            ),
        },
    ))
    revision = ensure_production_revision(
        episode_id="ep_p",
        kind="screenplay",
        resume=False,
    )
    save_checkpoint(revision.id, {
        "phase": "IDENTITY_FREEZE",
        "blueprint_artifact_id": blueprint["id"],
        "blueprint_hash": blueprint_content_hash(blueprint_value),
    })
    recorder = screenplay_ops._new_screenplay_recorder(
        "ep_p",
        requested_by="recovery",
        trigger_type="resume",
    )
    conn = db.get_conn()
    from app.portraits import (
        AUTOMATIC_IDENTITY_DECISION_PROVENANCE,
        FUTURE_IDENTITY_DECISION_VERSION,
        IDENTITY_DISCOVERY_CONTRACT_VERSION,
        CURRENT_IDENTITY_LITERAL_PROVENANCE,
        STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        _current_identity_evidence_records,
        screenplay_identity_scope_fingerprint,
    )

    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) VALUES(?,?,?,?)",
        ("proj_p", 1, "第一章", "守卫守在山门。"),
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]' WHERE id='ep_p'"
    )
    conn.commit()
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE id='ep_p'"
    ).fetchone()
    recovery_source = screenplay_ops._episode_source_text(conn, episode_row)
    current_scope = screenplay_identity_scope_fingerprint(1, recovery_source)
    stale_v13_scope = evidence_repository.content_hash({
        "contract_version": "screenplay-identity-discovery.v13",
        "episode_no": 1,
        "source_text": recovery_source,
    })
    assert IDENTITY_DISCOVERY_CONTRACT_VERSION == (
        "screenplay-identity-discovery.v15"
    )
    current_receipt = _current_identity_evidence_records(recovery_source)[0]
    current_label = str(current_receipt["text"])[:2]
    conn.execute(
        "UPDATE episodes SET screenplay_character_resolutions=? WHERE id='ep_p'",
        (json.dumps([
            {
                "source_label": current_label,
                "canonical_name": current_label,
                "resolution": "functional_identity",
                "identity_group": "current-1:F1",
                "identity_scope_fingerprint": current_scope,
                "decision_provenance": AUTOMATIC_IDENTITY_DECISION_PROVENANCE,
                "decision_contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                "structural_identity_policy_version": (
                    STRUCTURAL_IDENTITY_COVERAGE_VERSION
                ),
                "source_label_provenance": (
                    CURRENT_IDENTITY_LITERAL_PROVENANCE
                ),
                "source_evidence_receipt": current_receipt,
                "source_evidence_receipts": [current_receipt],
                "source_segment_id": current_receipt["source_segment_id"],
                "source_segment_ids": [current_receipt["source_segment_id"]],
                "source_quote": current_receipt["text"],
            },
            {
                "source_label": "stale-v11-auto",
                "canonical_name": "stale-v11-auto",
                "resolution": "functional_identity",
                "identity_group": "current-1:F2",
                "identity_scope_fingerprint": stale_v13_scope,
                "decision_provenance": AUTOMATIC_IDENTITY_DECISION_PROVENANCE,
                "decision_contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                "structural_identity_policy_version": (
                    STRUCTURAL_IDENTITY_COVERAGE_VERSION
                ),
            },
            {
                "source_label": "manual-kept",
                "canonical_name": "manual-kept",
                "resolution": "functional_identity",
                "decision_provenance": "manual",
            },
            {
                "source_label": "bible-kept",
                "canonical_name": "bible-kept",
                "resolution": "future_identity",
                "decision_provenance": "bible",
            },
        ], ensure_ascii=False),),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='queued',active_screenplay_run_id=? "
        "WHERE id='ep_p'",
        (recorder.run_id,),
    )
    conn.commit()

    async def forbidden_discovery(*_args, **_kwargs):
        raise AssertionError("resumable Baseline must reuse persisted identity")

    async def fake_task(_episode_id, *, preflight_result=None):
        assert preflight_result["skipped"] == "prebaseline_identity_checkpoint_reused"
        assert {
            item["source_label"]
            for item in preflight_result["resolutions"]
        } == {current_label, "manual-kept", "bible-kept"}
        script = _minimal_script()
        conn.execute(
            "UPDATE episodes SET screenplay_status='ready',screenplay_json=? "
            "WHERE id='ep_p'",
            (script.model_dump_json(),),
        )
        conn.commit()
        return script

    monkeypatch.setattr(
        screenplay_ops,
        "_screenplay_character_discovery",
        forbidden_discovery,
    )
    monkeypatch.setattr(screenplay_ops, "_screenplay_task", fake_task)

    result = await screenplay_ops._recorded_screenplay_task("ep_p", recorder)

    assert isinstance(result, EpisodeScreenplay)
    assert conn.execute(
        "SELECT status FROM workflow_runs WHERE id=?",
        (recorder.run_id,),
    ).fetchone()["status"] == "SUCCEEDED"


def test_activation_retry_grant_travels_to_a_newly_created_revision() -> None:
    """授权属于这一次运行，运行新建 revision 时它必须跟着走。

    Production EP4: activation issued a retry grant bound to the then-active
    revision, the worker immediately superseded that revision and created a
    new one (0.06s apart), and the resolution check reported
    BLUEPRINT_RESOLUTION_GRANT_DRIFT every single round.
    """
    from app.production import screenplay_repair

    source = pathlib.Path(screenplay_repair.__file__).read_text(
        encoding="utf-8"
    )
    call = source[source.index("rev = ensure_production_revision("):]
    call = call[: call.index(")\n")]

    assert "grant_id=_activation_retry_grant_id(run_id)" in call


def test_activation_retry_grant_lookup_is_defensive() -> None:
    """没有 run、没有快照、快照损坏，都只是"没有授权"，不该炸。"""
    from app.production import screenplay_repair

    assert screenplay_repair._activation_retry_grant_id(None) is None
    assert screenplay_repair._activation_retry_grant_id("run-does-not-exist") is None
