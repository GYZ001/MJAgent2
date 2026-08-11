"""剧本台按钮必须映射到真实的 Baseline / Patch 后端阶段。"""
from __future__ import annotations

import json
import hashlib

import pytest
from fastapi import HTTPException

from app import api, db, task_registry
from app.capabilities import ensure_catalog_loaded
from app.capabilities.direct import enter_handler
from app.capabilities.registry import get_registry
from app.evidence import repository
from app.harness.contracts import get_contract
from app.harness.types import EvidenceArtifact
from app.narrative_blueprint import BLUEPRINT_VERSION, NarrativeBlueprint
from app.observability.tracing import bind_trace
from app.production.patch import screenplay_artifact_payload
from app.production.revision import (
    ProductionRevisionOwnershipLost,
    ensure_production_revision,
    mark_baseline_generated,
    rebase_screenplay_revision_for_resume,
    resolve_screenplay_resume_eligibility,
    save_checkpoint,
    screenplay_production_state,
    set_published_artifact,
)
from app.screenplay_scene_shards import (
    SCREENPLAY_ENVELOPE_VERSION,
    SCREENPLAY_MERGED_IR_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
    ScreenplayEnvelopeExperience,
    ScreenplayEnvelopeIR,
    ScreenplayEnvelopeMetadata,
    ScreenplaySceneShardOwnershipLost,
    _assert_episode_owner,
    blueprint_content_hash,
)
from app.screenplay_ir import IR_VERSION, ScreenplayGenerationIR
from tests.test_narrative_continuity import _screenplay


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-controls.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','demo','created',?)",
        (db.now(),),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, screenplay_status, status, created_at) "
        "VALUES('e1','p1',1,'第一集','pending','planned',?)",
        (db.now(),),
    )
    conn.commit()
    yield


def _v8_shard_content(
    *,
    shard_id: str,
    generation_scaffold_hash: str,
    source_hash: str = "",
    boundary_hash: str = "",
    blueprint_hash: str = "",
    identity_registry_hash: str = "",
) -> dict:
    return {
        "contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
        "episode_no": 1,
        "shard_id": shard_id,
        "scene_plan_keys": [],
        "scenes": [],
        "consumed_source_ids": [],
        "unresolved_participants": [],
        "source_hash": source_hash,
        "boundary_hash": boundary_hash,
        "blueprint_hash": blueprint_hash,
        "identity_registry_hash": identity_registry_hash,
        "source_ownership_hash": "ownership",
        "identity_scaffold_hash": f"identity:{shard_id}",
        "generation_scaffold_hash": generation_scaffold_hash,
    }


def _structured_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _current_checkpoint_artifacts() -> dict[str, object]:
    blueprint_value = NarrativeBlueprint(episode_no=1, nodes=[])
    blueprint_hash = blueprint_content_hash(blueprint_value)
    identity_content = {
        "contract_version": "screenplay-identity-registry.v1",
        "identities": [],
    }
    identity_hash = _structured_hash(identity_content["identities"])
    identity_content["identity_registry_hash"] = identity_hash
    blueprint = repository.create_artifact(EvidenceArtifact(
        type="screenplay_narrative_blueprint",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=blueprint_value.model_dump(mode="json"),
        contract_version=BLUEPRINT_VERSION,
    ))
    identity = repository.create_artifact(EvidenceArtifact(
        type="screenplay_identity_registry",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=identity_content,
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
    envelope_raw = repository.create_artifact(EvidenceArtifact(
        type="screenplay_envelope_raw",
        scope_type="episode",
        scope_id="e1",
        status="candidate",
        trust_level="T0",
        content={"attempts": []},
        parent_artifact_ids=[blueprint["id"], identity["id"]],
        contract_version=SCREENPLAY_ENVELOPE_VERSION,
    ))
    envelope = repository.create_artifact(EvidenceArtifact(
        type="screenplay_envelope",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=envelope_value.model_dump(mode="json"),
        parent_artifact_ids=[envelope_raw["id"]],
        contract_version=SCREENPLAY_ENVELOPE_VERSION,
    ))
    return {
        "blueprint": blueprint,
        "blueprint_hash": blueprint_hash,
        "identity": identity,
        "identity_hash": identity_hash,
        "envelope": envelope,
    }


def _current_shard_artifact(
    authority: dict[str, object],
    *,
    shard_id: str,
    generation_scaffold_hash: str,
    source_hash: str = "",
    boundary_hash: str = "",
):
    raw = repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard_raw",
        scope_type="episode",
        scope_id="e1",
        status="candidate",
        trust_level="T0",
        content={
            "shard_id": shard_id,
            "attempts": [],
            "generation_scaffold_hash": generation_scaffold_hash,
        },
        parent_artifact_ids=[
            authority["blueprint"]["id"],
            authority["identity"]["id"],
        ],
        contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
    ))
    return repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=_v8_shard_content(
            shard_id=shard_id,
            generation_scaffold_hash=generation_scaffold_hash,
            source_hash=source_hash,
            boundary_hash=boundary_hash,
            blueprint_hash=str(authority["blueprint_hash"]),
            identity_registry_hash=str(authority["identity_hash"]),
        ),
        parent_artifact_ids=[raw["id"]],
        contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
    ))


def _current_working_artifact():
    authority = _current_checkpoint_artifacts()
    shard = _current_shard_artifact(
        authority,
        shard_id="SS-working",
        generation_scaffold_hash="generation:working",
    )
    merged_value = ScreenplayGenerationIR(
        format_version=IR_VERSION,
        episode_no=1,
        source_semantics={},
    ).model_dump(mode="json")
    merged = repository.create_artifact(EvidenceArtifact(
        type="screenplay_generation_ir_merged",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=merged_value,
        parent_artifact_ids=[
            authority["blueprint"]["id"],
            authority["identity"]["id"],
            authority["envelope"]["id"],
            shard["id"],
        ],
        contract_version=SCREENPLAY_MERGED_IR_VERSION,
        model_snapshot={
            "blueprint_hash": authority["blueprint_hash"],
            "identity_registry_hash": authority["identity_hash"],
        },
    ))
    return repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="e1",
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(_screenplay()),
        parent_artifact_ids=[merged["id"]],
        contract_version=get_contract("screenplay").version,
    ))


def _incompatible_working_revision():
    artifact = repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="e1",
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(_screenplay()),
        contract_version=get_contract("screenplay").version,
    ))
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        input_fingerprint="stale-working",
        contract_version=get_contract("screenplay").version,
        qa_profile_version="screenplay-qa-gate-2",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    return revision, artifact


def test_production_state_resumes_post_baseline_stages() -> None:
    initial = screenplay_production_state("e1")
    assert initial["operation"] == "baseline"
    assert initial["baseline_done"] is False
    assert initial["can_resume_repair"] is False

    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    save_checkpoint(revision.id, {"phase": "GENERATING_BASELINE"})
    working = _current_working_artifact()
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=working["id"],
        working_artifact_id=working["id"],
    )

    finalize = screenplay_production_state("e1")
    assert finalize["operation"] == "finalize"
    assert finalize["phase"] == "STRUCTURE_VALIDATION"
    assert finalize["baseline_done"] is True
    assert finalize["can_resume_repair"] is True
    assert [item["label"] for item in finalize["stages"]] == [
        "人物识别", "叙事蓝图", "身份冻结", "全局包络", "场次写作",
        "全局编译", "结构校验", "质量评分", "原子发布", "已完成",
    ]

    save_checkpoint(revision.id, {
        "phase": "SUCCEEDED",
        "quality_score": 42.0,
        "quality_issue_count": 3,
        "gate_retry_exhausted": True,
    })
    set_published_artifact(revision.id, working["id"])
    completed = screenplay_production_state("e1")
    assert completed["operation"] == "complete"
    assert completed["phase"] == "SUCCEEDED"
    assert all(item["status"] == "completed" for item in completed["stages"])
    assert completed["quality_score"] == 42.0
    assert completed["quality_issue_count"] == 3


def test_unknown_working_validation_error_fails_closed(monkeypatch) -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        input_fingerprint="current",
        contract_version=get_contract("screenplay").version,
        qa_profile_version="screenplay-qa-gate-2",
        resume=False,
    )
    working = _current_working_artifact()
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=working["id"],
        working_artifact_id=working["id"],
    )
    monkeypatch.setattr(
        "app.production.patch.screenplay_from_artifact_record",
        lambda _artifact: (_ for _ in ()).throw(RuntimeError("validator unavailable")),
    )

    eligibility = resolve_screenplay_resume_eligibility("e1")

    assert eligibility.mode == "none"
    assert eligibility.reason_code == "ELIGIBILITY_CHECK_FAILED"
    assert eligibility.resumable is False


def test_resume_eligibility_is_strictly_read_only() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    authority = _current_checkpoint_artifacts()
    save_checkpoint(revision.id, {
        "phase": "ENVELOPE_GENERATION",
        "blueprint_artifact_id": authority["blueprint"]["id"],
        "identity_artifact_id": authority["identity"]["id"],
        "envelope_artifact_id": authority["envelope"]["id"],
        "blueprint_hash": authority["blueprint_hash"],
        "identity_registry_hash": authority["identity_hash"],
    })
    conn = db.get_conn()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        eligibility = resolve_screenplay_resume_eligibility("e1", conn=conn)
    finally:
        conn.set_trace_callback(None)

    assert eligibility.mode == "baseline"
    writes = [
        statement for statement in statements
        if statement.lstrip().upper().startswith((
            "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "COMMIT",
        ))
    ]
    assert writes == []


def test_mixed_old_checkpoint_requires_rebuild_and_keeps_identity_as_input() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    blueprint_value = NarrativeBlueprint(episode_no=1, nodes=[])
    old_blueprint = repository.create_artifact(EvidenceArtifact(
        type="screenplay_narrative_blueprint",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=blueprint_value.model_dump(mode="json"),
        contract_version="screenplay-narrative-blueprint.v3",
    ))
    identities: list[dict] = []
    identity_hash = _structured_hash(identities)
    identity = repository.create_artifact(EvidenceArtifact(
        type="screenplay_identity_registry",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content={
            "contract_version": "screenplay-identity-registry.v1",
            "identity_registry_hash": identity_hash,
            "identities": identities,
        },
        contract_version="screenplay-identity-registry.v1",
    ))
    old_merged = repository.create_artifact(EvidenceArtifact(
        type="screenplay_generation_ir_merged",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content={
            "format_version": IR_VERSION,
            "episode_no": 1,
            "source_semantics": {},
        },
        contract_version="screenplay-generation-ir-merged.v6",
    ))
    save_checkpoint(revision.id, {
        "phase": "IR_MERGE",
        "blueprint_artifact_id": old_blueprint["id"],
        "blueprint_hash": blueprint_content_hash(blueprint_value),
        "identity_artifact_id": identity["id"],
        "identity_registry_hash": identity_hash,
        "merged_ir_artifact_id": old_merged["id"],
    })

    eligibility = resolve_screenplay_resume_eligibility("e1")

    assert eligibility.mode == "baseline_rebuild"
    assert eligibility.revision_action == "rebase"
    assert eligibility.reason_code == "MIXED_CHECKPOINT_REQUIRES_REBUILD"
    assert eligibility.reusable_checkpoint["identity_artifact_id"] == identity["id"]
    assert "blueprint_artifact_id" not in eligibility.reusable_checkpoint
    assert "merged_ir_artifact_id" not in eligibility.reusable_checkpoint


def test_baseline_rebuild_records_identity_only_as_reused_input() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    authority = _current_checkpoint_artifacts()
    old_envelope = repository.create_artifact(EvidenceArtifact(
        type="screenplay_envelope",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=ScreenplayEnvelopeIR(
            episode_no=1,
            metadata=ScreenplayEnvelopeMetadata(),
            experience=ScreenplayEnvelopeExperience(),
            blueprint_hash=str(authority["blueprint_hash"]),
            identity_registry_hash=str(authority["identity_hash"]),
        ).model_dump(mode="json"),
        parent_artifact_ids=[
            authority["blueprint"]["id"],
            authority["identity"]["id"],
        ],
        contract_version=SCREENPLAY_ENVELOPE_VERSION,
    ))
    save_checkpoint(revision.id, {
        "phase": "ENVELOPE_GENERATION",
        "blueprint_artifact_id": authority["blueprint"]["id"],
        "identity_artifact_id": authority["identity"]["id"],
        "envelope_artifact_id": old_envelope["id"],
        "blueprint_hash": authority["blueprint_hash"],
        "identity_registry_hash": authority["identity_hash"],
    })
    eligibility = resolve_screenplay_resume_eligibility("e1")
    conn = db.get_conn()
    conn.execute("BEGIN IMMEDIATE")
    rebuilt = rebase_screenplay_revision_for_resume(
        eligibility,
        conn=conn,
    )
    conn.commit()

    checkpoint = rebuilt.checkpoint_json
    assert checkpoint["resume_mode"] == "baseline_rebuild"
    assert checkpoint["phase"] == "BLUEPRINT_GENERATION"
    assert checkpoint["reused_inputs"]["identity_registry"] == {
        "artifact_id": authority["identity"]["id"],
        "identity_registry_hash": authority["identity_hash"],
    }
    for key in (
        "blueprint_artifact_id",
        "identity_artifact_id",
        "envelope_artifact_id",
        "shards",
        "merged_ir_artifact_id",
    ):
        assert key not in checkpoint


def test_production_state_distinguishes_technical_failure_from_pause() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    save_checkpoint(revision.id, {"phase": "SCENE_SHARD_GENERATION"})
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="failure",
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE workflow_runs SET status='FAILED',failure_code='RUNTIMEERROR' "
        "WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET active_screenplay_run_id=? WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    state = screenplay_production_state("e1")

    assert state["stage_stop_reason"] == "failed"
    assert state["stages"][state["stage_index"]]["status"] == "failed"


def test_production_state_marks_open_gate_as_blocked() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    save_checkpoint(revision.id, {
        "phase": "WAITING_HUMAN",
        "yield_reason": "narrative_gate_needs_review",
        "open_issue_ids": ["issue-1"],
    })

    state = screenplay_production_state("e1")

    assert state["phase"] == "STRUCTURE_VALIDATION"
    assert state["stage_stop_reason"] == "blocked"
    assert state["stages"][state["stage_index"]]["status"] == "blocked"


def test_production_state_exposes_resumable_scene_shard_checkpoint() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    authority = _current_checkpoint_artifacts()
    shard = _current_shard_artifact(
        authority,
        shard_id="SS001",
        generation_scaffold_hash="generation:SS001",
    )
    save_checkpoint(revision.id, {
        "phase": "SCENE_SHARD_GENERATION",
        "blueprint_artifact_id": authority["blueprint"]["id"],
        "identity_artifact_id": authority["identity"]["id"],
        "envelope_artifact_id": authority["envelope"]["id"],
        "blueprint_hash": authority["blueprint_hash"],
        "identity_registry_hash": authority["identity_hash"],
        "yield_reason": "user_cancelled",
        "shards": [
            {
                "shard_id": "SS001",
                "status": "validated",
                "normalized_artifact_id": shard["id"],
                "generation_scaffold_hash": "generation:SS001",
            },
            {"shard_id": "SS002", "status": "failed"},
            {"shard_id": "SS003", "status": "pending"},
        ],
    })
    state = screenplay_production_state("e1")
    assert state["operation"] == "baseline"
    assert state["baseline_done"] is False
    assert state["can_resume_baseline"] is True
    assert state["can_resume_repair"] is False
    assert state["shard_progress"] == {
        "total": 3,
        "validated": 1,
        "running": 0,
        "failed": 1,
    }
    assert state["yield_reason"] == "user_cancelled"


def test_production_state_does_not_count_deleted_checkpoint_artifacts() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    save_checkpoint(revision.id, {
        "phase": "IR_MERGE",
        "blueprint_artifact_id": "art-deleted-blueprint",
        "merged_ir_artifact_id": "art-deleted-merged",
        "shards": [{
            "shard_id": "SS001",
            "status": "validated",
            "normalized_artifact_id": "art-deleted-shard",
        }],
    })

    state = screenplay_production_state("e1")

    assert state["operation"] == "baseline_rebuild"
    assert state["mode_label"] == "按新合同重建剧本"
    assert state["can_resume_baseline"] is True
    assert state["has_resumable_baseline"] is False
    assert state["shard_progress"] == {
        "total": 1,
        "validated": 0,
        "running": 0,
        "failed": 0,
    }


def test_production_state_reconciles_recovered_shard_artifact() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    authority = _current_checkpoint_artifacts()
    shard = _current_shard_artifact(
        authority,
        shard_id="SS006",
        generation_scaffold_hash="generation:SS006",
        source_hash="source-6",
        boundary_hash="boundary-6",
    )
    assert repository.get_artifact(shard["id"]) is not None
    save_checkpoint(revision.id, {
        "phase": "STRUCTURE_VALIDATION",
        "blueprint_artifact_id": authority["blueprint"]["id"],
        "identity_artifact_id": authority["identity"]["id"],
        "blueprint_hash": authority["blueprint_hash"],
        "identity_registry_hash": authority["identity_hash"],
        "shards": [{
            "shard_id": "SS006",
            "status": "failed",
            "source_hash": "source-6",
            "boundary_hash": "boundary-6",
            "generation_scaffold_hash": "generation:SS006",
        }],
    })

    state = screenplay_production_state("e1")

    assert state["shard_progress"] == {
        "total": 1,
        "validated": 1,
        "running": 0,
        "failed": 0,
    }


def test_production_state_rejects_legacy_shard_as_resumable() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    shard = repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content={
            **_v8_shard_content(
                shard_id="SS007",
                generation_scaffold_hash="generation:SS007",
            ),
            "contract_version": "screenplay-scene-shard.v7",
        },
        contract_version="screenplay-scene-shard.v7",
    ))
    save_checkpoint(revision.id, {
        "phase": "SCENE_SHARD_GENERATION",
        "shards": [{
            "shard_id": "SS007",
            "status": "validated",
            "normalized_artifact_id": shard["id"],
            "generation_scaffold_hash": "generation:SS007",
        }],
    })

    state = screenplay_production_state("e1")

    assert state["operation"] == "baseline_rebuild"
    assert state["can_resume_baseline"] is True
    assert state["has_resumable_baseline"] is False
    assert state["shard_progress"]["validated"] == 0


@pytest.mark.parametrize(
    ("blueprint_hash", "identity_registry_hash"),
    [
        ("blueprint-wrong", "identity-v1"),
        ("blueprint-v1", "identity-wrong"),
    ],
)
def test_production_state_rejects_shard_authority_hash_mismatch(
    blueprint_hash: str,
    identity_registry_hash: str,
) -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    shard = repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=_v8_shard_content(
            shard_id="SS008",
            generation_scaffold_hash="generation:SS008",
            blueprint_hash=blueprint_hash,
            identity_registry_hash=identity_registry_hash,
        ),
        contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
    ))
    save_checkpoint(revision.id, {
        "phase": "SCENE_SHARD_GENERATION",
        "blueprint_hash": "blueprint-v1",
        "identity_registry_hash": "identity-v1",
        "shards": [{
            "shard_id": "SS008",
            "status": "validated",
            "normalized_artifact_id": shard["id"],
            "generation_scaffold_hash": "generation:SS008",
        }],
    })

    state = screenplay_production_state("e1")

    assert state["has_resumable_baseline"] is False
    assert state["operation"] == "baseline_rebuild"
    assert state["can_resume_baseline"] is True
    assert state["shard_progress"]["validated"] == 0


def test_production_state_requires_expected_shard_authority_hashes() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    shard = repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=_v8_shard_content(
            shard_id="SS009",
            generation_scaffold_hash="generation:SS009",
            blueprint_hash="blueprint-v1",
            identity_registry_hash="identity-v1",
        ),
        contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
    ))
    save_checkpoint(revision.id, {
        "phase": "SCENE_SHARD_GENERATION",
        "shards": [{
            "shard_id": "SS009",
            "status": "validated",
            "normalized_artifact_id": shard["id"],
            "generation_scaffold_hash": "generation:SS009",
        }],
    })

    state = screenplay_production_state("e1")

    assert state["has_resumable_baseline"] is False
    assert state["operation"] == "baseline_rebuild"
    assert state["can_resume_baseline"] is True
    assert state["shard_progress"]["validated"] == 0


def test_resume_route_has_a_distinct_capability() -> None:
    ensure_catalog_loaded()
    registry = get_registry()
    assert registry.rest_bindings[
        "POST /api/episodes/{episode_id}/screenplay/resume"
    ] == "screenplay.resume"
    assert registry.commands["screenplay.resume"].title == "继续剧本流程"


@pytest.mark.asyncio
async def test_resume_rebases_stale_working_once_and_deduplicates(
    monkeypatch,
) -> None:
    old_revision, old_artifact = _incompatible_working_revision()
    conn = db.get_conn()
    monkeypatch.setattr(api, "_require_harness_engine", lambda _project_id: None)
    created_recorders: list[str] = []

    class Recorder:
        run_id = "run-baseline-rebuild"

        def cancel(self, _message: str) -> None:
            raise AssertionError("successful resume must not cancel its run")

    def new_recorder(*_args, **_kwargs):
        created_recorders.append("created")
        return Recorder()

    def fake_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()

    monkeypatch.setattr(api, "_new_screenplay_recorder", new_recorder)
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    monkeypatch.setattr(
        api,
        "_screenplay_task_active",
        lambda _episode_id: (
            conn.execute(
                "SELECT active_screenplay_run_id FROM episodes WHERE id='e1'"
            ).fetchone()["active_screenplay_run_id"]
            == "run-baseline-rebuild"
        ),
    )

    with enter_handler():
        first = await api.resume_screenplay("e1", body={})
        second = await api.resume_screenplay("e1", body={})

    assert first["mode"] == "baseline_rebuild"
    assert first["run_id"] == "run-baseline-rebuild"
    assert second["deduplicated"] is True
    assert second["run_id"] == "run-baseline-rebuild"
    assert created_recorders == ["created"]
    revisions = conn.execute(
        "SELECT id,status,baseline_generation_count,working_artifact_id "
        "FROM production_revisions WHERE episode_id='e1' ORDER BY created_at",
    ).fetchall()
    assert revisions[0]["id"] == old_revision.id
    assert revisions[0]["status"] == "superseded"
    assert revisions[1]["id"] == first["revision_id"]
    assert revisions[1]["status"] == "active"
    assert revisions[1]["baseline_generation_count"] == 0
    assert revisions[1]["working_artifact_id"] is None
    stale = conn.execute(
        "SELECT status FROM artifacts WHERE id=?",
        (old_artifact["id"],),
    ).fetchone()
    assert stale["status"] == "stale"


def test_rebase_transition_rolls_back_when_task_registration_fails(
    monkeypatch,
) -> None:
    old_revision, old_artifact = _incompatible_working_revision()
    eligibility = resolve_screenplay_resume_eligibility("e1")
    conn = db.get_conn()

    class Recorder:
        run_id = "run-registration-failed"
        cancelled = False

        def cancel(self, _message: str) -> None:
            self.cancelled = True

    recorder = Recorder()

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(task_registry, "spawn", fail_spawn)
    with pytest.raises(RuntimeError, match="registry unavailable"):
        api._spawn_screenplay_activation(
            "e1",
            recorder,
            project_id="p1",
            status="queued",
            message=eligibility.label,
            resume_eligibility=eligibility,
        )

    revisions = conn.execute(
        "SELECT id,status FROM production_revisions "
        "WHERE episode_id='e1' ORDER BY created_at",
    ).fetchall()
    assert [(row["id"], row["status"]) for row in revisions] == [
        (old_revision.id, "active"),
    ]
    assert conn.execute(
        "SELECT status FROM artifacts WHERE id=?",
        (old_artifact["id"],),
    ).fetchone()["status"] == "candidate"
    episode = conn.execute(
        "SELECT screenplay_status,active_screenplay_run_id,"
        "working_screenplay_artifact_id FROM episodes WHERE id='e1'",
    ).fetchone()
    assert dict(episode) == {
        "screenplay_status": "pending",
        "active_screenplay_run_id": None,
        "working_screenplay_artifact_id": old_artifact["id"],
    }
    assert recorder.cancelled is True


@pytest.mark.asyncio
async def test_worker_refuses_stale_working_without_rebase(monkeypatch) -> None:
    _revision, _artifact = _incompatible_working_revision()
    conn = db.get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id='p1'").fetchone()
    bible = api._project_bible_or_placeholder(project)
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id='e1'").fetchone())
    monkeypatch.setattr(
        "app.production.screenplay_authority.screenplay_authority_fingerprint",
        lambda *_args, **_kwargs: "current-input",
    )
    from app.production.screenplay_repair import run_screenplay_production

    with pytest.raises(RuntimeError, match="未执行 rebase"):
        await run_screenplay_production(
            episode_id="e1",
            episode=episode,
            source_text="原文",
            bible=bible,
            resume=True,
        )


def test_screenplay_generation_preflight_sizes_source_without_side_effects() -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','林舟推门。\\n他说：别走。',12)"
    )
    conn.execute("UPDATE episodes SET source_chapters='[1]' WHERE id='e1'")
    conn.commit()
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    save_checkpoint(revision.id, {
        "blueprint_hash": "blueprint-v1",
        "identity_registry_hash": "identity-v1",
    })
    repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content=_v8_shard_content(
            shard_id="SS001",
            generation_scaffold_hash="generation:SS001",
            blueprint_hash="blueprint-v1",
            identity_registry_hash="identity-v1",
        ),
        contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
    ))
    repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T1",
        content={
            **_v8_shard_content(
                shard_id="SS002",
                generation_scaffold_hash="generation:SS002",
            ),
            "contract_version": "screenplay-scene-shard.v3",
        },
        contract_version="screenplay-scene-shard.v3",
    ))

    result = api._screenplay_generation_preflight("e1")

    assert result["action"] == "generate_screenplay"
    assert result["input"]["source_segment_count"] >= 1
    assert result["input"]["estimated_blueprint_shards"] >= 1
    assert result["input"]["estimated_scene_writing_shards"] >= 1
    assert result["reusable_validated_artifacts"][
        "screenplay_scene_shard"
    ] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM workflow_runs").fetchone()["c"] == 0


def test_screenplay_generate_preflight_allows_terminal_run_takeover() -> None:
    from app.capabilities.inputs import ScreenplayGenerateInput
    from app.capabilities.preflight import screenplay_generate

    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','林舟推门。',5)"
    )
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="failed-run",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='FAILED',failure_code='TEST' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]',active_screenplay_run_id=? WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    terminal = screenplay_generate(ScreenplayGenerateInput(episode_id="e1"))

    assert terminal.allowed is True
    assert terminal.denial_code is None

    conn.execute("UPDATE workflow_runs SET status='RUNNING' WHERE id=?", (run_id,))
    conn.commit()
    live = screenplay_generate(ScreenplayGenerateInput(episode_id="e1"))

    assert live.allowed is False
    assert live.denial_code == "SCREENPLAY_ALREADY_RUNNING"
    assert live.state_fingerprint != terminal.state_fingerprint


@pytest.mark.asyncio
async def test_persisted_active_run_blocks_manual_save_without_local_task() -> None:
    from app.capabilities.inputs import ScreenplayUpdateInput
    from app.capabilities.preflight import screenplay_update

    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="remote-worker",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='RUNNING' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='running',active_screenplay_run_id=? "
        "WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    preflight = screenplay_update(ScreenplayUpdateInput(
        episode_id="e1",
        screenplay={},
    ))
    state = screenplay_production_state("e1")

    assert task_registry.active("screenplay", "e1") is False
    assert preflight.allowed is False
    assert preflight.denial_code == "SCREENPLAY_TASK_ACTIVE"
    assert state["task_active"] is True
    with pytest.raises(ProductionRevisionOwnershipLost):
        ensure_production_revision(
            episode_id="e1",
            kind="screenplay",
            resume=False,
        )
    with enter_handler(), pytest.raises(HTTPException) as exc_info:
        await api.edit_screenplay("e1", body={})
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "screenplay_task_active"


@pytest.mark.asyncio
async def test_cancel_persisted_remote_screenplay_run_releases_owner() -> None:
    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="remote-worker",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='RUNNING' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='running',active_screenplay_run_id=? "
        "WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    with enter_handler():
        result = await api.cancel_screenplay("e1")

    episode = conn.execute(
        "SELECT screenplay_status,active_screenplay_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result["status"] == "pending"
    assert dict(episode) == {
        "screenplay_status": "pending",
        "active_screenplay_run_id": None,
    }
    assert repository.get_run(run_id)["status"] == "CANCELLED"


def test_old_run_cannot_write_revision_or_scene_shard_after_owner_is_cleared() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET active_screenplay_run_id=NULL WHERE id='e1'"
    )
    conn.commit()

    with bind_trace("run-old", "step-old"):
        with pytest.raises(ProductionRevisionOwnershipLost):
            save_checkpoint(revision.id, {"phase": "STALE_WRITE"})
        with pytest.raises(ScreenplaySceneShardOwnershipLost):
            _assert_episode_owner("e1")

    stored = conn.execute(
        "SELECT checkpoint_json FROM production_revisions WHERE id=?",
        (revision.id,),
    ).fetchone()
    assert json.loads(stored["checkpoint_json"] or "{}") == {}


@pytest.mark.asyncio
async def test_start_screenplay_replaces_terminal_run_owner(monkeypatch) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','林舟推门。',5)"
    )
    failed_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="failed-run",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='FAILED',failure_code='TEST' WHERE id=?",
        (failed_run_id,),
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]',screenplay_status='failed',"
        "active_screenplay_run_id=? WHERE id='e1'",
        (failed_run_id,),
    )
    conn.commit()

    class Recorder:
        run_id = "run_replacement"

        def cancel(self, _message: str) -> None:
            raise AssertionError("successful takeover must not cancel the new run")

    spawned: list[tuple[str, str]] = []

    def capture_spawn(kind, key, coro, *, project_id=None):
        spawned.append((kind, key))
        coro.close()

    monkeypatch.setattr(api, "_new_screenplay_recorder", lambda *args, **kwargs: Recorder())
    monkeypatch.setattr(task_registry, "spawn", capture_spawn)

    with enter_handler():
        result = await api.start_screenplay("e1", body={})

    episode = conn.execute(
        "SELECT screenplay_status,active_screenplay_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result["run_id"] == "run_replacement"
    assert dict(episode) == {
        "screenplay_status": "queued",
        "active_screenplay_run_id": "run_replacement",
    }
    assert spawned == [("screenplay", "e1")]


def test_clear_unpublished_ir_preserves_published_lineage() -> None:
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
    )
    step_id = repository.create_step(
        run_id,
        "screenplay.iteration",
    )
    unpublished = repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T2",
            content={"candidate": "retry-only"},
        ),
        step_run_id=step_id,
    )
    published_ir = repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T2",
            content={"candidate": "published-source"},
        ),
        step_run_id=step_id,
    )
    repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="e1",
        status="approved",
        trust_level="T4",
        content={"published": True},
        parent_artifact_ids=[published_ir["id"]],
    ))
    conn = db.get_conn()
    conn.execute(
        "UPDATE step_runs SET output_artifact_id=? WHERE id=?",
        (unpublished["id"], step_id),
    )
    conn.commit()

    assert api._clear_unpublished_screenplay_ir("e1") == 1
    assert repository.get_artifact(unpublished["id"]) is None
    assert repository.get_artifact(published_ir["id"]) is not None
    assert conn.execute(
        "SELECT output_artifact_id FROM step_runs WHERE id=?",
        (step_id,),
    ).fetchone()["output_artifact_id"] is None


def test_failed_recovery_run_clears_only_its_ir_lineage() -> None:
    parent_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
    )
    child_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
        parent_run_id=parent_run_id,
    )
    unrelated_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="other-input",
    )
    artifacts = []
    for run_id, label in (
        (parent_run_id, "parent"),
        (child_run_id, "child"),
        (unrelated_run_id, "unrelated"),
    ):
        step_id = repository.create_step(run_id, "screenplay.iteration")
        artifacts.append(repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_generation_ir",
                scope_type="episode",
                scope_id="e1",
                status="approved",
                trust_level="T2",
                content={"candidate": label},
            ),
            step_run_id=step_id,
        ))

    assert api._clear_unpublished_screenplay_ir(
        "e1",
        run_id=child_run_id,
    ) == 2
    assert repository.get_artifact(artifacts[0]["id"]) is None
    assert repository.get_artifact(artifacts[1]["id"]) is None
    assert repository.get_artifact(artifacts[2]["id"]) is not None


def test_runtime_failure_preserves_validated_baseline_recovery_point() -> None:
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        resume=False,
    )
    authority = _current_checkpoint_artifacts()
    save_checkpoint(revision.id, {
        "phase": "IDENTITY_FREEZE",
        "blueprint_artifact_id": authority["blueprint"]["id"],
        "blueprint_hash": authority["blueprint_hash"],
    })
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_status='running' WHERE id='e1'"
    )
    conn.commit()

    preserved = api._project_screenplay_runtime_failure(
        "e1",
        run_id=None,
        public_error="SYS · ERR-test",
    )

    episode = conn.execute(
        "SELECT screenplay_status,screenplay_error FROM episodes WHERE id='e1'"
    ).fetchone()
    assert preserved is True
    assert repository.get_artifact(authority["blueprint"]["id"]) is not None
    assert episode["screenplay_status"] == "repairing"
    assert "安全恢复点已保留" in episode["screenplay_error"]
    assert "ERR-test" in episode["screenplay_error"]


def test_atomic_claim_does_not_clear_current_owner_ir() -> None:
    owner_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="owner",
    )
    step_id = repository.create_step(
        owner_run_id,
        "screenplay.iteration",
    )
    ir = repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T2",
            content={"candidate": "current-owner"},
        ),
        step_run_id=step_id,
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_status='queued', "
        "active_screenplay_run_id=? WHERE id='e1'",
        (owner_run_id,),
    )
    conn.commit()

    class Recorder:
        run_id = "late-run"
        cancelled = False

        def cancel(self, _message: str) -> None:
            self.cancelled = True

    recorder = Recorder()
    with pytest.raises(api.StateConflict):
        api._spawn_screenplay_activation(
            "e1",
            recorder,
            project_id="p1",
            status="queued",
            message="late",
            clear_unpublished_ir=True,
        )

    assert recorder.cancelled is True
    assert repository.get_artifact(ir["id"]) is not None
    assert conn.execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id='e1'",
    ).fetchone()["active_screenplay_run_id"] == owner_run_id


def test_atomic_claim_rejects_manual_publish_fence() -> None:
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_publish_fence=1 WHERE id='e1'"
    )
    conn.commit()

    class Recorder:
        run_id = "run-blocked-by-publish"
        cancelled = False

        def cancel(self, _message: str) -> None:
            self.cancelled = True

    recorder = Recorder()
    with pytest.raises(api.StateConflict):
        api._spawn_screenplay_activation(
            "e1",
            recorder,
            project_id="p1",
            status="queued",
            message="must not claim",
        )

    assert recorder.cancelled is True
    assert conn.execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id='e1'"
    ).fetchone()["active_screenplay_run_id"] is None


@pytest.mark.asyncio
async def test_first_screenplay_spawn_failure_restores_state_and_legacy_columns(
    monkeypatch,
) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','第一章\\n林舟说：别走。',12)"
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]', screenplay_error='上次提示', "
        "screenplay_started_at=10, screenplay_updated_at=11, "
        "screenplay_character_resolutions=?, screenplay_required_dialogues=?, "
        "screenplay_required_dialogue_occurrences=? WHERE id='e1'",
        (
            json.dumps([{
                "source_label": "旧称谓",
                "canonical_name": "路人甲",
                "resolution": "functional_extra",
            }], ensure_ascii=False),
            json.dumps(["旧台词"], ensure_ascii=False),
            json.dumps(["legacy-occurrence"], ensure_ascii=False),
        ),
    )
    conn.commit()
    unpublished_ir = repository.create_artifact(EvidenceArtifact(
        type="screenplay_generation_ir",
        scope_type="episode",
        scope_id="e1",
        status="candidate",
        trust_level="T1",
        content={"candidate": "must-survive-spawn-failure"},
    ))

    class Recorder:
        run_id = "run_not_started"
        cancelled = False

        def cancel(self, _message: str) -> None:
            self.cancelled = True

    recorder = Recorder()

    def fail_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()
        raise RuntimeError("event loop unavailable")

    monkeypatch.setattr(api, "_new_screenplay_recorder", lambda *args, **kwargs: recorder)
    monkeypatch.setattr(task_registry, "spawn", fail_spawn)

    with enter_handler(), pytest.raises(HTTPException) as exc_info:
        await api.start_screenplay("e1", body={})

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["action"] == "retry_generate"
    row = conn.execute(
        "SELECT screenplay_status,screenplay_error,screenplay_started_at,"
        "screenplay_updated_at,active_screenplay_run_id,screenplay_required_dialogues,"
        "screenplay_required_dialogue_occurrences,screenplay_character_resolutions "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert row["screenplay_status"] == "pending"
    assert row["screenplay_error"] == "上次提示"
    assert row["screenplay_started_at"] == 10
    assert row["screenplay_updated_at"] == 11
    assert row["active_screenplay_run_id"] is None
    assert json.loads(row["screenplay_required_dialogues"]) == ["旧台词"]
    assert json.loads(row["screenplay_required_dialogue_occurrences"]) == ["legacy-occurrence"]
    assert json.loads(row["screenplay_character_resolutions"])[0][
        "source_label"
    ] == "旧称谓"
    assert recorder.cancelled is True
    assert repository.get_artifact(unpublished_ir["id"]) is not None


@pytest.mark.asyncio
async def test_fresh_screenplay_clears_stale_identity_and_legacy_dialogue_selection(
    monkeypatch,
) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) "
        "VALUES('p1',1,'第一章','第一章\\n小胖子站在门口。',12)"
    )
    conn.execute(
        "UPDATE episodes SET source_chapters='[1]',screenplay_status='failed',"
        "screenplay_character_resolutions=?,screenplay_required_dialogues=?,"
        "screenplay_required_dialogue_occurrences=? WHERE id='e1'",
        (
            json.dumps([{
                "source_label": "小胖子",
                "canonical_name": "路人甲",
                "resolution": "functional_extra",
            }], ensure_ascii=False),
            json.dumps(["旧台词"], ensure_ascii=False),
            json.dumps(["legacy-occurrence"], ensure_ascii=False),
        ),
    )
    conn.commit()

    class Recorder:
        run_id = "run_fresh"

    seen: dict[str, object] = {}

    def fake_spawn(_kind, _key, coro, *, project_id=None):
        row = conn.execute(
            "SELECT screenplay_character_resolutions,screenplay_required_dialogues,"
            "screenplay_required_dialogue_occurrences FROM episodes WHERE id='e1'"
        ).fetchone()
        seen["resolutions"] = json.loads(row["screenplay_character_resolutions"])
        seen["legacy_dialogues"] = json.loads(row["screenplay_required_dialogues"])
        seen["legacy_occurrences"] = json.loads(
            row["screenplay_required_dialogue_occurrences"]
        )
        coro.close()

    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: Recorder(),
    )
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    with enter_handler():
        result = await api.start_screenplay("e1", body={})

    assert result["mode"] == "baseline"
    assert seen["resolutions"] == []
    assert seen["legacy_dialogues"] == []
    assert seen["legacy_occurrences"] == []


def test_recovery_resumes_repair_interrupted_by_service_restart(monkeypatch) -> None:
    conn = db.get_conn()
    parent_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="repair",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL', failure_code='SERVICE_RESTART' "
        "WHERE id=?",
        (parent_run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', screenplay_error='修复到第 2 步', "
        "active_screenplay_run_id=? WHERE id='e1'",
        (parent_run_id,),
    )
    conn.commit()
    seen: dict[str, object] = {}

    class Recorder:
        run_id = "run_recovered"

    def fake_recorder(*_args, **kwargs):
        seen["parent_run_id"] = kwargs.get("parent_run_id")
        return Recorder()

    def fake_spawn(kind, key, coro, *, project_id=None):
        seen["spawn"] = (kind, key, project_id)
        coro.close()
        return None

    monkeypatch.setattr(api, "_new_screenplay_recorder", fake_recorder)
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    assert api.recover_screenplay_tasks() == 1
    row = conn.execute(
        "SELECT screenplay_status,screenplay_error,active_screenplay_run_id "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(row) == {
        "screenplay_status": "queued",
        "screenplay_error": "恢复首版生成已排队，等待文本生成槽位",
        "active_screenplay_run_id": "run_recovered",
    }
    assert seen == {
        "parent_run_id": parent_run_id,
        "spawn": ("screenplay", "e1", "p1"),
    }


def test_recovery_rebases_obsolete_contract_revision(monkeypatch) -> None:
    conn = db.get_conn()
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        input_fingerprint="old-input",
        contract_version="2.0.0",
        qa_profile_version="screenplay-qa-gate-2",
        resume=False,
    )
    parent_run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="repair",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL',failure_code='SERVICE_RESTART' "
        "WHERE id=?",
        (parent_run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing',active_screenplay_run_id=?,"
        "screenplay_production_revision_id=? WHERE id='e1'",
        (parent_run_id, revision.id),
    )
    conn.commit()
    class Recorder:
        run_id = "run-current-contract"

    def fake_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()

    monkeypatch.setattr(
        api, "_new_screenplay_recorder", lambda *_args, **_kwargs: Recorder(),
    )
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    assert api.recover_screenplay_tasks() == 1
    episode = conn.execute(
        "SELECT screenplay_status,screenplay_error,active_screenplay_run_id "
        "FROM episodes WHERE id='e1'",
    ).fetchone()
    assert episode["screenplay_status"] == "queued"
    assert episode["screenplay_error"].startswith("重建当前合同首版已排队")
    assert episode["active_screenplay_run_id"] == "run-current-contract"
    revisions = conn.execute(
        "SELECT id,status,baseline_generation_count FROM production_revisions "
        "WHERE episode_id='e1' ORDER BY created_at",
    ).fetchall()
    assert [(row["status"], row["baseline_generation_count"]) for row in revisions] == [
        ("superseded", 0),
        ("active", 0),
    ]


def test_recovery_rebases_legacy_working_artifact(
    monkeypatch,
) -> None:
    payload = screenplay_artifact_payload(_screenplay())
    payload["narrative_plan"]["contract_version"] = "narrative-continuity.v1"
    payload["narrative_plan"]["atomic_actions"] = [{
        "action_id": "A-legacy",
        "actor_ids": ["character-1"],
        "target_ids": ["entity-1"],
        "semantic_intent": "Change the observable state.",
        "completion_condition": "The changed state is visible.",
    }]
    artifact = repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="e1",
        status="candidate",
        trust_level="T1",
        content=payload,
        contract_version=get_contract("screenplay").version,
    ))
    revision = ensure_production_revision(
        episode_id="e1",
        kind="screenplay",
        input_fingerprint="legacy-working-artifact",
        contract_version=get_contract("screenplay").version,
        qa_profile_version="screenplay-qa-gate-2",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="legacy-working-artifact",
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL',failure_code='SERVICE_RESTART' "
        "WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing',active_screenplay_run_id=? "
        "WHERE id='e1'",
        (run_id,),
    )
    conn.commit()
    class Recorder:
        run_id = "run-rebuilt"

    def fake_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()

    monkeypatch.setattr(
        api, "_new_screenplay_recorder", lambda *_args, **_kwargs: Recorder(),
    )
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)

    assert api.recover_screenplay_tasks() == 1

    episode = conn.execute(
        "SELECT screenplay_status,screenplay_error,active_screenplay_run_id "
        "FROM episodes WHERE id='e1'",
    ).fetchone()
    assert episode["screenplay_status"] == "queued"
    assert episode["screenplay_error"].startswith("重建当前合同首版已排队")
    assert episode["active_screenplay_run_id"] == "run-rebuilt"
    stale = conn.execute(
        "SELECT status,stale_reason FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()
    assert stale["status"] == "stale"
    assert "participant_deliveries" in stale["stale_reason"]
    revisions = conn.execute(
        "SELECT id,status,baseline_generation_count,working_artifact_id "
        "FROM production_revisions WHERE episode_id='e1' ORDER BY created_at",
    ).fetchall()
    assert revisions[0]["id"] == revision.id
    assert revisions[0]["status"] == "superseded"
    assert revisions[1]["status"] == "active"
    assert revisions[1]["baseline_generation_count"] == 0
    assert revisions[1]["working_artifact_id"] is None


def test_recovery_does_not_restart_intentionally_paused_repair(monkeypatch) -> None:
    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="repair",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='PARTIAL', failure_code='PARTIAL_RESULT' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', "
        "screenplay_error='恢复点已保存，可继续', active_screenplay_run_id=? WHERE id='e1'",
        (run_id,),
    )
    conn.commit()
    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: pytest.fail("不应自动重启主动暂停的修复"),
    )

    assert api.recover_screenplay_tasks() == 0


def test_recovery_does_not_restart_persisted_cancellation(monkeypatch) -> None:
    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="cancelled",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='CANCELLED' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='running', "
        "screenplay_error='CANCELLING: 正在取消运行', active_screenplay_run_id=? "
        "WHERE id='e1'",
        (run_id,),
    )
    conn.commit()
    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: pytest.fail("用户取消的任务不应在重启后恢复"),
    )

    assert api.recover_screenplay_tasks() == 0


def test_recovery_failure_does_not_overwrite_concurrent_delete(monkeypatch) -> None:
    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="interrupted",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='PAUSED_EXTERNAL' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing',"
        "active_screenplay_run_id=? WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    def delete_then_fail(*_args, **_kwargs):
        conn.execute(
            "UPDATE episodes SET screenplay_status='pending',screenplay_error=NULL,"
            "active_screenplay_run_id=NULL WHERE id='e1'"
        )
        conn.commit()
        raise RuntimeError("delete won")

    monkeypatch.setattr(api, "_new_screenplay_recorder", delete_then_fail)

    assert api.recover_screenplay_tasks() == 0
    episode = conn.execute(
        "SELECT screenplay_status,screenplay_error,active_screenplay_run_id "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(episode) == {
        "screenplay_status": "pending",
        "screenplay_error": None,
        "active_screenplay_run_id": None,
    }


@pytest.mark.asyncio
async def test_batch_start_reports_partial_failure_without_stranding_episode(
    monkeypatch,
) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,screenplay_status,status,created_at) "
        "VALUES('e2','p1',2,'第二集','running','planned',?)",
        (db.now(),),
    )
    conn.commit()

    class Recorder:
        def __init__(self, run_id: str):
            self.run_id = run_id
            self.cancelled = False

        def cancel(self, _message: str) -> None:
            self.cancelled = True

    recorders: dict[str, Recorder] = {}

    def fake_recorder(episode_id: str, **_kwargs):
        recorder = Recorder(f"run_{episode_id}")
        recorders[episode_id] = recorder
        return recorder

    def fake_spawn(_kind, key, coro, *, project_id=None):
        coro.close()
        if key == "e2":
            raise RuntimeError("queue unavailable")
        return None

    monkeypatch.setattr(api, "_new_screenplay_recorder", fake_recorder)
    monkeypatch.setattr(task_registry, "spawn", fake_spawn)
    cleared: list[str] = []
    monkeypatch.setattr(
        api,
        "_clear_unpublished_screenplay_ir",
        lambda episode_id, **_kwargs: cleared.append(episode_id) or 0,
    )
    with enter_handler():
        result = await api.start_screenplay_all("p1")

    assert result["started"] == 1
    assert result["batch_run_id"].startswith("run_")
    assert result["retryable_failures"] == 1
    assert result["failed_to_start"][0]["episode_id"] == "e2"
    rows = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id,screenplay_status,active_screenplay_run_id FROM episodes ORDER BY id"
        ).fetchall()
    }
    assert rows["e1"]["screenplay_status"] == "queued"
    assert rows["e1"]["active_screenplay_run_id"] == "run_e1"
    assert rows["e2"]["screenplay_status"] == "failed"
    assert rows["e2"]["active_screenplay_run_id"] is None
    assert recorders["e2"].cancelled is True
    assert cleared == ["e1", "e2"]
    batch = conn.execute(
        "SELECT workflow_type,scope_id,status FROM workflow_runs WHERE id=?",
        (result["batch_run_id"],),
    ).fetchone()
    assert tuple(batch) == ("screenplay_batch", "p1", "RUNNING")


@pytest.mark.asyncio
async def test_batch_start_excludes_episode_owned_by_remote_active_run() -> None:
    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="remote-batch-owner",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='RUNNING' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='running',active_screenplay_run_id=? "
        "WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    with enter_handler(), pytest.raises(HTTPException) as exc_info:
        await api.start_screenplay_all("p1")

    assert exc_info.value.status_code == 409
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM workflow_runs WHERE workflow_type='screenplay_batch'"
    ).fetchone()["c"] == 0
    assert repository.get_run(run_id)["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_batch_cancel_stops_remote_runs_and_clears_exact_owner() -> None:
    conn = db.get_conn()
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="remote-batch-owner",
    )
    conn.execute(
        "UPDATE workflow_runs SET status='WAITING_AUTHORIZATION' WHERE id=?",
        (run_id,),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_status='running',active_screenplay_run_id=? "
        "WHERE id='e1'",
        (run_id,),
    )
    conn.commit()

    with enter_handler():
        result = await api.cancel_screenplay_all("p1")

    assert result["stopped"] == 1
    assert result["local_stopped"] == 0
    assert result["persisted_stopped"] == 1
    assert repository.get_run(run_id)["status"] == "CANCELLED"
    episode = conn.execute(
        "SELECT screenplay_status,active_screenplay_run_id FROM episodes WHERE id='e1'"
    ).fetchone()
    assert dict(episode) == {
        "screenplay_status": "pending",
        "active_screenplay_run_id": None,
    }
