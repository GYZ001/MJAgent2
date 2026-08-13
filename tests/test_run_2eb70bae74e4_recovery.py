from __future__ import annotations

import json
import hashlib

import pytest

from app import db
from app.evidence import repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.narrative_blueprint import (
    BLUEPRINT_PROMPT_VERSION,
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    blueprint_authority_validator_fingerprint,
)
from app.production.grant import issue_production_grant
from app.production.patch import screenplay_artifact_payload
from app.production.revision import (
    get_production_revision,
    mark_baseline_generated,
    recover_screenplay_working_authority,
    resolve_screenplay_resume_eligibility,
    save_checkpoint,
    update_working_artifact,
)
from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION
from app.production.structured_issues import structured_issue
from app.schemas import Bible, World
from app.screenplay_ir import IR_COMPILER_VERSION, IR_VERSION
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
from tests.test_production_repair import _minimal_script, _recovery_narrative_plan


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "run-2eb70-recovery.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES(?,?,?,?)",
        ("proj_run_2eb", "我欲封天恢复回归", "created", db.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,screenplay_status,"
        "status,created_at) VALUES(?,?,?,?,?,?,?)",
        ("ep_run_2eb", "proj_run_2eb", 1, "第一集", "failed", "planned", db.now()),
    )
    conn.commit()


def _script(*, polluted: bool):
    script = _minimal_script(stakes="失败将失去资格")
    script.id = "ep_run_2eb"
    script.narrative_plan = _recovery_narrative_plan().model_copy(
        update={"scope_id": "ep_run_2eb"},
        deep=True,
    )
    script.scene_outline[0].characters = ["旁白"] if polluted else []
    script.full_script_text = "【场1】夜 / 山谷\n月光落在空旷山谷。"
    script.voice_bible = []
    return script


def _shard_content(
    *,
    shard_id: str,
    blueprint_hash: str,
    identity_hash: str,
    source_hash: str,
    boundary_hash: str,
    generation_hash: str,
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
        "identity_registry_hash": identity_hash,
        "source_ownership_hash": "ownership",
        "identity_scaffold_hash": f"identity:{shard_id}",
        "generation_scaffold_hash": generation_hash,
    }


def _seed_recovery(*, polluted_working: bool, shard_count: int = 4) -> dict:
    from app.production.revision import ensure_production_revision

    clean = _script(polluted=False)
    polluted = _script(polluted=polluted_working)
    blueprint_value = NarrativeBlueprint(episode_no=1, nodes=[])
    blueprint_hash = blueprint_content_hash(blueprint_value)
    identities: list[dict] = []
    identity_hash = hashlib.sha256(
        json.dumps(
            identities,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    blueprint = repository.create_artifact(EvidenceArtifact(
        type="screenplay_narrative_blueprint",
        scope_type="episode",
        scope_id="ep_run_2eb",
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
    identity = repository.create_artifact(EvidenceArtifact(
        type="screenplay_identity_registry",
        scope_type="episode",
        scope_id="ep_run_2eb",
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
    envelope_raw = repository.create_artifact(EvidenceArtifact(
        type="screenplay_envelope_raw",
        scope_type="episode",
        scope_id="ep_run_2eb",
        status="candidate",
        trust_level="T0",
        content={"attempts": []},
        parent_artifact_ids=[blueprint["id"], identity["id"]],
        contract_version=SCREENPLAY_ENVELOPE_VERSION,
    ))
    envelope = repository.create_artifact(EvidenceArtifact(
        type="screenplay_envelope",
        scope_type="episode",
        scope_id="ep_run_2eb",
        status="validated",
        trust_level="T1",
        content=envelope_value.model_dump(mode="json"),
        parent_artifact_ids=[envelope_raw["id"]],
        contract_version=SCREENPLAY_ENVELOPE_VERSION,
    ))
    shard_rows: list[dict] = []
    shards: list[dict] = []
    for index in range(1, shard_count + 1):
        shard_id = f"SS{index:03d}"
        source_hash = f"source:{index}"
        boundary_hash = f"boundary:{index}"
        generation_hash = f"generation:{index}"
        creative_hash = f"{index:064x}"
        semantic_review_evidence = {
            "contract_version": SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
            "initial_creative_hash": creative_hash,
            "reviewed_creative_hash": creative_hash,
            "phases": [{
                "creative_hash": creative_hash,
                "reviews": [{"issues": []}, {"issues": []}],
                "consensus": [],
            }],
        }
        raw = repository.create_artifact(EvidenceArtifact(
            type="screenplay_scene_shard_raw",
            scope_type="episode",
            scope_id="ep_run_2eb",
            status="candidate",
            trust_level="T0",
            content={
                "shard_id": shard_id,
                "attempts": [],
                "generation_scaffold_hash": generation_hash,
                "semantic_review_evidence": semantic_review_evidence,
            },
            parent_artifact_ids=[blueprint["id"], identity["id"]],
            contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
            model_snapshot={
                "semantic_review_version": (
                    SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                ),
                "reviewed_creative_hash": creative_hash,
            },
        ))
        shard = repository.create_artifact(EvidenceArtifact(
            type="screenplay_scene_shard",
            scope_type="episode",
            scope_id="ep_run_2eb",
            status="validated",
            trust_level="T1",
            content=_shard_content(
                shard_id=shard_id,
                blueprint_hash=blueprint_hash,
                identity_hash=identity_hash,
                source_hash=source_hash,
                boundary_hash=boundary_hash,
                generation_hash=generation_hash,
            ),
            parent_artifact_ids=[raw["id"]],
            contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
            model_snapshot={
                "semantic_review_version": (
                    SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                ),
                "reviewed_creative_hash": creative_hash,
            },
        ))
        shards.append(shard)
        shard_rows.append({
            "shard_id": shard_id,
            "status": "validated",
            "normalized_artifact_id": shard["id"],
            "source_hash": source_hash,
            "boundary_hash": boundary_hash,
            "generation_scaffold_hash": generation_hash,
        })
    merged_content = {
        "format_version": IR_VERSION,
        "episode_no": 1,
        "source_semantics": {},
        "source_audit_annotations": [],
    }
    merged = repository.create_artifact(EvidenceArtifact(
        type="screenplay_generation_ir_merged",
        scope_type="episode",
        scope_id="ep_run_2eb",
        status="validated",
        trust_level="T1",
        content=merged_content,
        parent_artifact_ids=[
            blueprint["id"], identity["id"], envelope["id"],
            *[shard["id"] for shard in shards],
        ],
        contract_version=SCREENPLAY_MERGED_IR_VERSION,
        model_snapshot={
            "blueprint_hash": blueprint_hash,
            "identity_registry_hash": identity_hash,
        },
    ))
    baseline = repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="ep_run_2eb",
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(clean),
        parent_artifact_ids=[merged["id"]],
        contract_version=get_contract("screenplay").version,
        model_snapshot={
            "compiler_version": IR_COMPILER_VERSION,
            "source_merged_content_hash": merged["content_hash"],
        },
    ))
    working = baseline
    if polluted_working:
        repair_patch = repository.create_artifact(EvidenceArtifact(
            type="screenplay_patch",
            scope_type="episode",
            scope_id="ep_run_2eb",
            status="validated",
            trust_level="T1",
            content={"run_id": "run_2eb70bae74e4", "value": ["旁白"]},
            parent_artifact_ids=[baseline["id"]],
            contract_version="screenplay-patch.v1",
        ))
        working = repository.create_artifact(EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id="ep_run_2eb",
            status="candidate",
            trust_level="T1",
            content=screenplay_artifact_payload(polluted),
            parent_artifact_ids=[baseline["id"], repair_patch["id"]],
            contract_version=get_contract("screenplay").version,
            model_snapshot={
                "repair_run_id": "run_2eb70bae74e4",
                "compiler_version": IR_COMPILER_VERSION,
            },
        ))
    revision = ensure_production_revision(
        episode_id="ep_run_2eb",
        kind="screenplay",
        input_fingerprint="old-gate-2-input",
        contract_version=get_contract("screenplay").version,
        qa_profile_version="screenplay-qa-gate-2",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=baseline["id"],
        working_artifact_id=baseline["id"],
    )
    if working["id"] != baseline["id"]:
        update_working_artifact(
            revision.id,
            working["id"],
            expected_hash=baseline["content_hash"],
        )
    checkpoint = {
        "phase": "STRUCTURE_VALIDATION",
        "blueprint_artifact_id": blueprint["id"],
        "blueprint_hash": blueprint_hash,
        "identity_artifact_id": identity["id"],
        "identity_registry_hash": identity_hash,
        "envelope_artifact_id": envelope["id"],
        "shards": shard_rows,
        "merged_ir_artifact_id": merged["id"],
        "issue_strategy_history": {
            "SC01.characters": ["fill", "exhausted", "exhausted"],
        },
        "patch_artifact_ids": ["art_f8cc4d91189a"],
        "open_issue_ids": ["SC01.characters"],
    }
    save_checkpoint(revision.id, checkpoint)
    return {
        "revision": get_production_revision(revision.id),
        "baseline": baseline,
        "working": working,
        "merged": merged,
        "shards": shards,
        "checkpoint": checkpoint,
        "clean": clean,
    }


def _install_gate3_qa(monkeypatch, *, input_fingerprint: str = "gate3-input"):
    from app.production import screenplay_repair

    issue = structured_issue(
        code="SCENE_CHARACTER_NOT_AUTHORIZED",
        message="SC01 characters 含未授权旁白",
        subject="screenplay",
        path="/scene_outline/SC01/characters",
        rule_id="scene_character_identity_authority",
        must_fix=True,
        stage="screenplay",
    )

    def fake_qa(script, **kwargs):
        polluted = any(
            "旁白" in scene.characters for scene in script.scene_outline
        )
        issues = [issue] if polluted else []
        artifact_hash = str(kwargs.get("artifact_hash") or "")
        return issues, Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="failed" if polluted else "passed",
            hard_gate_passed=not polluted,
            evaluation_role="runtime_gate" if polluted else "score_only",
            score_status="scored",
            runtime_blocking=polluted,
            score=0 if polluted else 100,
            issues=issues,
            evidence={
                "artifact_id": kwargs.get("artifact_id"),
                "artifact_hash": artifact_hash,
                "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
                "authority_input_fingerprint": input_fingerprint,
            },
        )

    monkeypatch.setattr(screenplay_repair, "run_screenplay_qa", fake_qa)
    return input_fingerprint


def _episode() -> dict:
    return {
        "id": "ep_run_2eb",
        "project_id": "proj_run_2eb",
        "episode_no": 1,
        "target_duration_s": 50,
        "character_resolutions": [],
        "authorized_source_chapters": {},
    }


def test_real_run_polluted_working_rebuilds_from_four_shard_merged_ir(
    monkeypatch,
) -> None:
    from app.production import screenplay_repair

    seeded = _seed_recovery(polluted_working=True, shard_count=4)
    input_fingerprint = _install_gate3_qa(monkeypatch)
    monkeypatch.setattr(
        "app.screenplay_ir.compile_screenplay_ir",
        lambda *_args, **_kwargs: seeded["clean"].model_copy(deep=True),
    )
    eligibility = resolve_screenplay_resume_eligibility("ep_run_2eb")
    assert eligibility.reason_code == "WORKING_REVALIDATION_REQUIRED"
    assert eligibility.reusable_checkpoint["merged_ir_artifact_id"] == (
        seeded["merged"]["id"]
    )
    assert len(eligibility.reusable_checkpoint["shards"]) == 4

    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_artifact_id='published-art',"
        "screenplay_completion_certificate_id='published-cert',"
        "screenplay_production_revision_id='published-rev' WHERE id=?",
        ("ep_run_2eb",),
    )
    conn.commit()
    old_checkpoint_json = json.dumps(
        seeded["revision"].checkpoint_json,
        ensure_ascii=False,
        sort_keys=True,
    )
    recovered = screenplay_repair._revalidate_or_rebuild_resume_working(
        episode_id="ep_run_2eb",
        episode=_episode(),
        source_text="月光落在空旷山谷。",
        bible=Bible(characters=[], world=World(visual_style_canonical="水墨")),
        revision=seeded["revision"],
        entry_eligibility=eligibility,
        input_fingerprint=input_fingerprint,
        contract_version=get_contract("screenplay").version,
        run_id=None,
    )

    assert recovered.id != seeded["revision"].id
    assert recovered.working_artifact_id != seeded["working"]["id"]
    assert recovered.baseline_artifact_id == recovered.working_artifact_id
    replacement = repository.get_artifact(recovered.working_artifact_id)
    assert replacement is not None
    assert replacement["parent_artifact_ids"] == [seeded["merged"]["id"]]
    assert replacement["model_snapshot"]["compiler_version"] == IR_COMPILER_VERSION
    assert replacement["model_snapshot"]["source_merged_content_hash"] == seeded["merged"]["content_hash"]
    assert recovered.qa_profile_version == SCREENPLAY_QA_PROFILE_VERSION
    assert recovered.first_evaluation_id is None
    assert recovered.checkpoint_json["issue_strategy_history"] == {}
    assert recovered.checkpoint_json["patch_artifact_ids"] == []
    assert recovered.checkpoint_json["recovery_history"][-1]["action"] == "working_rebuilt"
    old = get_production_revision(seeded["revision"].id)
    assert old is not None and old.status == "superseded"
    assert old.qa_profile_version == "screenplay-qa-gate-2"
    assert json.dumps(old.checkpoint_json, ensure_ascii=False, sort_keys=True) == old_checkpoint_json
    episode_row = conn.execute(
        "SELECT screenplay_artifact_id,screenplay_completion_certificate_id,"
        "screenplay_production_revision_id,working_screenplay_artifact_id "
        "FROM episodes WHERE id=?",
        ("ep_run_2eb",),
    ).fetchone()
    assert tuple(episode_row) == (
        "published-art", "published-cert", "published-rev", recovered.working_artifact_id,
    )
    assert resolve_screenplay_resume_eligibility("ep_run_2eb").reason_code == "WORKING_COMPATIBLE"
    with pytest.raises(RuntimeError, match="不再 active"):
        update_working_artifact(
            seeded["revision"].id,
            seeded["working"]["id"],
        )


def test_legal_old_working_is_reused_in_new_revision_and_gets_fresh_grant(
    monkeypatch,
) -> None:
    from app.production import screenplay_repair

    seeded = _seed_recovery(polluted_working=False)
    old_grant, _ = issue_production_grant(
        episode_id="ep_run_2eb",
        project_id="proj_run_2eb",
        production_revision_id=seeded["revision"].id,
        kind="screenplay",
    )
    seeded["revision"] = get_production_revision(seeded["revision"].id)
    input_fingerprint = _install_gate3_qa(monkeypatch)
    eligibility = resolve_screenplay_resume_eligibility("ep_run_2eb")
    recovered = screenplay_repair._revalidate_or_rebuild_resume_working(
        episode_id="ep_run_2eb",
        episode=_episode(),
        source_text="月光落在空旷山谷。",
        bible=Bible(characters=[], world=World(visual_style_canonical="水墨")),
        revision=seeded["revision"],
        entry_eligibility=eligibility,
        input_fingerprint=input_fingerprint,
        contract_version=get_contract("screenplay").version,
        run_id=None,
    )
    assert recovered.working_artifact_id == seeded["working"]["id"]
    assert recovered.grant_id is None
    fresh_grant, _ = issue_production_grant(
        episode_id="ep_run_2eb",
        project_id="proj_run_2eb",
        production_revision_id=recovered.id,
        kind="screenplay",
    )
    recovered = get_production_revision(recovered.id)
    assert recovered is not None and recovered.grant_id == fresh_grant.grant_id
    assert old_grant.production_revision_id != fresh_grant.production_revision_id
    assert len(db.get_conn().execute(
        "SELECT id FROM production_revisions WHERE episode_id=? AND status='active'",
        ("ep_run_2eb",),
    ).fetchall()) == 1


@pytest.mark.parametrize("tamper", ["missing", "replacement"])
def test_dynamic_shard_parent_set_must_match_exactly(tamper: str) -> None:
    seeded = _seed_recovery(polluted_working=True, shard_count=4)
    conn = db.get_conn()
    if tamper == "missing":
        conn.execute(
            "UPDATE artifacts SET status='stale' WHERE id=?",
            (seeded["shards"][2]["id"],),
        )
    else:
        parents = list(seeded["merged"]["parent_artifact_ids"])
        parents[-1] = "art-replacement-shard"
        conn.execute(
            "UPDATE artifacts SET parent_artifact_ids_json=? WHERE id=?",
            (json.dumps(parents), seeded["merged"]["id"]),
        )
    conn.commit()
    eligibility = resolve_screenplay_resume_eligibility("ep_run_2eb")
    assert "merged_ir_artifact_id" not in eligibility.reusable_checkpoint


def test_old_state_subject_checkpoint_requires_baseline_rebuild() -> None:
    seeded = _seed_recovery(polluted_working=False)
    conn = db.get_conn()
    old_shard_id = seeded["shards"][0]["id"]
    conn.execute(
        "UPDATE artifacts SET contract_version=? WHERE id=?",
        ("screenplay-scene-shard.v9", old_shard_id),
    )
    conn.commit()

    eligibility = resolve_screenplay_resume_eligibility("ep_run_2eb")

    assert eligibility.mode == "baseline_rebuild"
    assert eligibility.revision_action == "rebase"
    assert eligibility.reason_code == "MIXED_CHECKPOINT_REQUIRES_REBUILD"
    assert eligibility.working_compatible is False
    assert "merged_ir_artifact_id" not in eligibility.reusable_checkpoint


def test_current_blueprint_checkpoint_remains_reusable() -> None:
    seeded = _seed_recovery(polluted_working=False)

    eligibility = resolve_screenplay_resume_eligibility("ep_run_2eb")

    assert eligibility.reusable_checkpoint["blueprint_artifact_id"] == (
        seeded["checkpoint"]["blueprint_artifact_id"]
    )
    assert eligibility.reusable_checkpoint["blueprint_hash"] == (
        seeded["checkpoint"]["blueprint_hash"]
    )


def test_same_scene_duplicate_source_checkpoint_requires_baseline_rebuild() -> None:
    seeded = _seed_recovery(polluted_working=False)
    duplicate_blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [
            {
                "key": node_key,
                "source_segment_ids": ["SRC0001"],
                "summary": "空镜建立山谷",
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
                "temporal_domain_key": "present",
                "time_label": "夜",
                "time_relation": "episode_start",
                "location_key": "valley",
                "location_label": "山谷",
                "environment_source_unit_keys": ["SRC0001:unit:001"],
                "action_logic": "月光落在空旷山谷",
            }
            for node_key in ("n1", "n1-duplicate")
        ],
    })
    duplicate_content = duplicate_blueprint.model_dump(mode="json")
    duplicate_blueprint_hash = blueprint_content_hash(duplicate_blueprint)
    conn = db.get_conn()
    blueprint_artifact_id = seeded["checkpoint"]["blueprint_artifact_id"]
    conn.execute(
        "UPDATE artifacts SET content_json=?,content_hash=? WHERE id=?",
        (
            json.dumps(duplicate_content, ensure_ascii=False, sort_keys=True),
            repository.content_hash(duplicate_content),
            blueprint_artifact_id,
        ),
    )
    checkpoint = dict(seeded["checkpoint"])
    checkpoint["blueprint_hash"] = duplicate_blueprint_hash
    conn.execute(
        "UPDATE production_revisions SET checkpoint_json=? WHERE id=?",
        (
            json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
            seeded["revision"].id,
        ),
    )
    conn.commit()
    artifact_before = conn.execute(
        "SELECT content_json,content_hash,status FROM artifacts WHERE id=?",
        (blueprint_artifact_id,),
    ).fetchone()
    episode_before = conn.execute(
        "SELECT screenplay_artifact_id,working_screenplay_artifact_id,"
        "screenplay_completion_certificate_id,screenplay_production_revision_id "
        "FROM episodes WHERE id='ep_run_2eb'",
    ).fetchone()

    eligibility = resolve_screenplay_resume_eligibility("ep_run_2eb")

    assert eligibility.mode == "baseline_rebuild"
    assert eligibility.reason_code == "MIXED_CHECKPOINT_REQUIRES_REBUILD"
    assert "blueprint_artifact_id" not in eligibility.reusable_checkpoint
    assert conn.execute(
        "SELECT content_json,content_hash,status FROM artifacts WHERE id=?",
        (blueprint_artifact_id,),
    ).fetchone() == artifact_before
    assert conn.execute(
        "SELECT screenplay_artifact_id,working_screenplay_artifact_id,"
        "screenplay_completion_certificate_id,screenplay_production_revision_id "
        "FROM episodes WHERE id='ep_run_2eb'",
    ).fetchone() == episode_before


@pytest.mark.parametrize(
    "tamper", ["hash", "lineage", "fingerprint", "role", "evaluator"],
)
def test_recovery_rejects_untrusted_replacement_or_evaluation(tamper: str) -> None:
    seeded = _seed_recovery(polluted_working=False)
    replacement = seeded["working"]
    if tamper == "lineage":
        replacement = repository.create_artifact(EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id="ep_run_2eb",
            status="candidate",
            trust_level="T1",
            content=screenplay_artifact_payload(seeded["clean"]),
            parent_artifact_ids=[seeded["baseline"]["id"]],
            contract_version=get_contract("screenplay").version,
        ))
    evaluation = repository.create_evaluation(
        replacement["id"],
        Evaluation(
            evaluator_type=(
                "model" if tamper == "evaluator" else "deterministic"
            ),
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role=(
                "runtime_gate" if tamper == "role" else "score_only"
            ),
            score_status="scored",
            runtime_blocking=False,
            issues=[],
            evidence={
                "artifact_id": replacement["id"],
                "artifact_hash": replacement["content_hash"],
                "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
                "authority_input_fingerprint": (
                    "wrong-input" if tamper == "fingerprint" else "gate3-input"
                ),
            },
        ),
    )
    with pytest.raises((ValueError, RuntimeError)):
        recover_screenplay_working_authority(
            seeded["revision"].id,
            replacement["id"],
            expected_working_artifact_id=seeded["working"]["id"],
            expected_working_hash=seeded["working"]["content_hash"],
            expected_checkpoint_hash=hashlib.sha256(
                json.dumps(
                    seeded["revision"].checkpoint_json,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            expected_first_evaluation_id=seeded["revision"].first_evaluation_id,
            expected_replacement_hash=(
                "wrong-hash" if tamper == "hash" else replacement["content_hash"]
            ),
            trusted_merged_ir_artifact_id=(
                seeded["merged"]["id"] if tamper == "lineage" else ""
            ),
            revalidation_evaluation_id=evaluation["id"],
            input_fingerprint="gate3-input",
            contract_version=get_contract("screenplay").version,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
            checkpoint={"phase": "STRUCTURE_VALIDATION"},
        )
    assert get_production_revision(seeded["revision"].id).status == "active"


def test_episode_pointer_cas_conflict_rolls_back_revision_switch() -> None:
    seeded = _seed_recovery(polluted_working=False)
    evaluation = repository.create_evaluation(
        seeded["working"]["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="score_only",
            score_status="scored",
            runtime_blocking=False,
            issues=[],
            evidence={
                "artifact_id": seeded["working"]["id"],
                "artifact_hash": seeded["working"]["content_hash"],
                "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
                "authority_input_fingerprint": "gate3-input",
            },
        ),
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET working_screenplay_artifact_id='stale-worker' WHERE id=?",
        ("ep_run_2eb",),
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="episode pointer CAS"):
        recover_screenplay_working_authority(
            seeded["revision"].id,
            seeded["working"]["id"],
            expected_working_artifact_id=seeded["working"]["id"],
            expected_working_hash=seeded["working"]["content_hash"],
            expected_checkpoint_hash=hashlib.sha256(
                json.dumps(
                    seeded["revision"].checkpoint_json,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            expected_first_evaluation_id=seeded["revision"].first_evaluation_id,
            expected_replacement_hash=seeded["working"]["content_hash"],
            trusted_merged_ir_artifact_id="",
            revalidation_evaluation_id=evaluation["id"],
            input_fingerprint="gate3-input",
            contract_version=get_contract("screenplay").version,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
            checkpoint={"phase": "STRUCTURE_VALIDATION"},
        )
    old = get_production_revision(seeded["revision"].id)
    assert old is not None and old.status == "active"
    assert db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM production_revisions WHERE episode_id=?",
        ("ep_run_2eb",),
    ).fetchone()["c"] == 1
    conn.execute("UPDATE episodes SET screenplay_error='connection-usable' WHERE id=?", ("ep_run_2eb",))
    conn.commit()
    assert conn.execute(
        "SELECT screenplay_error FROM episodes WHERE id=?", ("ep_run_2eb",)
    ).fetchone()["screenplay_error"] == "connection-usable"


def test_checkpoint_cas_conflict_rejects_stale_recovery_snapshot() -> None:
    seeded = _seed_recovery(polluted_working=False)
    evaluation = repository.create_evaluation(
        seeded["working"]["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="score_only",
            score_status="scored",
            runtime_blocking=False,
            issues=[],
            evidence={
                "artifact_id": seeded["working"]["id"],
                "artifact_hash": seeded["working"]["content_hash"],
                "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
                "authority_input_fingerprint": "gate3-input",
            },
        ),
    )
    with pytest.raises(RuntimeError, match="eligibility/working CAS"):
        recover_screenplay_working_authority(
            seeded["revision"].id,
            seeded["working"]["id"],
            expected_working_artifact_id=seeded["working"]["id"],
            expected_working_hash=seeded["working"]["content_hash"],
            expected_checkpoint_hash="stale-checkpoint",
            expected_first_evaluation_id=seeded["revision"].first_evaluation_id,
            expected_replacement_hash=seeded["working"]["content_hash"],
            trusted_merged_ir_artifact_id="",
            revalidation_evaluation_id=evaluation["id"],
            input_fingerprint="gate3-input",
            contract_version=get_contract("screenplay").version,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
            checkpoint={"phase": "STRUCTURE_VALIDATION"},
        )
    assert get_production_revision(seeded["revision"].id).status == "active"


def test_pre_cas_failure_retry_reuses_recovery_document_and_evaluation(
    monkeypatch,
) -> None:
    from app.observability.tracing import bind_trace
    from app.production import screenplay_repair
    from app.production.revision import (
        recover_screenplay_working_authority as real_recover,
    )

    seeded = _seed_recovery(polluted_working=True)
    input_fingerprint = _install_gate3_qa(monkeypatch)
    monkeypatch.setattr(
        "app.screenplay_ir.compile_screenplay_ir",
        lambda *_args, **_kwargs: seeded["clean"].model_copy(deep=True),
    )
    eligibility = resolve_screenplay_resume_eligibility("ep_run_2eb")
    run_id = repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="ep_run_2eb",
        input_fingerprint=input_fingerprint,
    )
    step_id = repository.create_step(run_id, "screenplay.recovery")
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET active_screenplay_run_id=? WHERE id=?",
        (run_id, "ep_run_2eb"),
    )
    conn.commit()

    def fail_after_durable_evidence(*_args, **_kwargs):
        raise RuntimeError("simulated owner/CAS failure")

    monkeypatch.setattr(
        screenplay_repair,
        "recover_screenplay_working_authority",
        fail_after_durable_evidence,
    )
    with bind_trace(run_id, step_id):
        with pytest.raises(RuntimeError, match="simulated owner/CAS failure"):
            screenplay_repair._revalidate_or_rebuild_resume_working(
                episode_id="ep_run_2eb",
                episode=_episode(),
                source_text="月光落在空旷山谷。",
                bible=Bible(
                    characters=[],
                    world=World(visual_style_canonical="水墨"),
                ),
                revision=seeded["revision"],
                entry_eligibility=eligibility,
                input_fingerprint=input_fingerprint,
                contract_version=get_contract("screenplay").version,
                run_id=None,
            )
    recovery_rows = conn.execute(
        "SELECT id,created_by_step_run_id FROM artifacts "
        "WHERE type='screenplay_document' AND scope_id=? "
        "AND model_snapshot_json LIKE '%screenplay-working-recovery.v1%'",
        ("ep_run_2eb",),
    ).fetchall()
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["created_by_step_run_id"] == step_id
    evaluation_rows = conn.execute(
        "SELECT id,step_run_id FROM evaluations WHERE artifact_id=? "
        "AND evaluator_version=?",
        (recovery_rows[0]["id"], SCREENPLAY_QA_PROFILE_VERSION),
    ).fetchall()
    assert len(evaluation_rows) == 1
    assert evaluation_rows[0]["step_run_id"] == step_id

    monkeypatch.setattr(
        screenplay_repair,
        "recover_screenplay_working_authority",
        real_recover,
    )
    with bind_trace(run_id, step_id):
        recovered = screenplay_repair._revalidate_or_rebuild_resume_working(
            episode_id="ep_run_2eb",
            episode=_episode(),
            source_text="月光落在空旷山谷。",
            bible=Bible(
                characters=[],
                world=World(visual_style_canonical="水墨"),
            ),
            revision=seeded["revision"],
            entry_eligibility=eligibility,
            input_fingerprint=input_fingerprint,
            contract_version=get_contract("screenplay").version,
            run_id=None,
        )
    assert recovered.working_artifact_id == recovery_rows[0]["id"]
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM artifacts WHERE type='screenplay_document' "
        "AND scope_id=? AND model_snapshot_json LIKE "
        "'%screenplay-working-recovery.v1%'",
        ("ep_run_2eb",),
    ).fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM evaluations WHERE artifact_id=? "
        "AND evaluator_version=?",
        (recovery_rows[0]["id"], SCREENPLAY_QA_PROFILE_VERSION),
    ).fetchone()["c"] == 1
