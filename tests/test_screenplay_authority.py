from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import api, db, errors as app_errors, storyboard_workspace
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.narrative_review import NarrativeReviewError, run_blind_audience_review
from app.production.publish import publish_screenplay
from app.production.patch import (
    load_screenplay_from_artifact,
    screenplay_artifact_payload,
)
from app.production.revision import ensure_production_revision, mark_baseline_generated
from app.production.screenplay_authority import (
    SCREENPLAY_QA_PROFILE_VERSION,
    resolve_current_screenplay_authority,
    screenplay_authority_material,
    screenplay_bible_payload,
    screenplay_authority_fingerprint,
)
from app.schemas import ActionAgency, Bible, Character, NarrativeIdentityContract, World
from tests.test_narrative_continuity import _board, _screenplay
from tests.test_narrative_review import _persist_review_projection


MERGED_IR_ARTIFACT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_generation_ir_merged_art_949de359c598.json"
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-authority.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _published_case():
    screenplay = _screenplay()
    artifact, _shot_artifacts = _persist_review_projection(screenplay, _board())
    authority = resolve_current_screenplay_authority("episode-generic")
    return screenplay, artifact, authority


def _source_projection_case() -> dict:
    from app.screenplay_ir import ScreenplayGenerationIR, compile_screenplay_ir

    fixture = json.loads(MERGED_IR_ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    ir = ScreenplayGenerationIR.model_validate(fixture["content"])
    source_by_id: dict[str, list[str]] = {}
    for scene in ir.scenes:
        for unit in scene.units:
            for source_id in unit.source_segment_ids:
                source_by_id.setdefault(source_id, [])
                text = (unit.source_text or unit.text).strip()
                if text and text not in source_by_id[source_id]:
                    source_by_id[source_id].append(text)
    source_ids = list(ir.source_scene_owners)
    source_body = "\n\n".join(
        " ".join(source_by_id[source_id]) or f"来源段 {source_id}"
        for source_id in source_ids
    )

    characters: list[Character] = []
    for identity in ir.identities:
        if identity.key == "narrator":
            continue
        identity.authority_id = f"bible:{identity.display_name}"
        identity.kind = "named_character"
        identity.role_type = "named_character"
        identity.visual_policy = "canonical"
        identity.asset_requirement = "required"
        identity.visual_canonical = (
            identity.visual_canonical or f"{identity.display_name}的稳定外形"
        )
        characters.append(Character(
            name=identity.display_name,
            role="角色",
            appearance_canonical=identity.visual_canonical,
            personality="稳定",
            speech_style="自然",
        ))
    bible = Bible(
        world=World(visual_style_canonical="统一动画电影风格"),
        characters=characters,
    )

    episode_id = "episode-source-projection"
    project_id = "project-source-projection"
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        (project_id, "source projection", bible.model_dump_json(), db.now()),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) VALUES(?,?,?,?)",
        (project_id, 1, "Fixture", source_body),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               status,screenplay_status,screenplay_character_resolutions,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            episode_id,
            project_id,
            1,
            "Fixture",
            "[1]",
            1800,
            "planned",
            "pending",
            "[]",
            db.now(),
        ),
    )
    conn.commit()
    episode = dict(conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone())
    source_text = f"【Fixture】\n{source_body}"
    episode["authorized_source_chapters"] = {"1": source_body}
    episode["character_resolutions"] = []
    compiled = compile_screenplay_ir(
        ir.model_copy(deep=True),
        episode=episode,
        source_text=source_text,
        bible=bible,
    )

    blueprint = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_narrative_blueprint",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content={"fixture": True},
        contract_version="screenplay-narrative-blueprint.v3",
    ))
    identity_registry = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_identity_registry",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content={"fixture": True},
        contract_version="screenplay-identity-registry.v1",
    ))
    envelope = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_envelope",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content={"fixture": True},
        contract_version="screenplay-envelope.v1",
    ))

    merged_content = ir.model_dump(mode="json")
    for scene in merged_content["scenes"]:
        for unit in scene["units"]:
            unit.pop("action_agency", None)
    shard_content = {
        "contract_version": "screenplay-scene-shard.v6",
        "episode_no": 1,
        "shard_id": "SS001",
        "scene_plan_keys": [scene["key"] for scene in merged_content["scenes"]],
        "scenes": merged_content["scenes"],
        "consumed_source_ids": source_ids,
        "unresolved_participants": [],
        "source_hash": "",
        "boundary_hash": "",
        "blueprint_hash": "",
        "identity_registry_hash": "",
        "source_ownership_hash": "",
        "identity_scaffold_hash": "",
        "generation_scaffold_hash": "",
    }
    shard = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_scene_shard",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content=shard_content,
        contract_version="screenplay-scene-shard.v6",
    ))
    merged = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_generation_ir_merged",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content=merged_content,
        parent_artifact_ids=[
            blueprint["id"],
            identity_registry["id"],
            envelope["id"],
            shard["id"],
        ],
        contract_version="screenplay-generation-ir-merged.v5",
    ))
    return {
        "episode_id": episode_id,
        "compiled": compiled,
        "merged_artifact_id": merged["id"],
    }


def _drift_to_contextual_actor(screenplay):
    drifted = screenplay.model_copy(deep=True)
    plan = drifted.narrative_plan
    assert plan is not None
    action = next(
        item for item in plan.atomic_actions
        if not item.actor_ids and not item.target_ids
    )
    action.actor_ids = ["ID-08"]
    action.action_agency = ActionAgency(
        kind="character",
        identity_bearing=True,
        source_segment_ids=list(action.action_agency.source_segment_ids),
    )
    event = next(
        item for item in plan.events
        if action.action_id in item.action_ids
    )
    event.onscreen_entity_ids = ["ID-08"]
    plan.identity_contracts.append(NarrativeIdentityContract(
        identity_id="ID-08",
        display_name="来源未归属的场景参与者",
        kind="source_backed_scene_context_actor",
        visual_policy="contextual",
        visual_canonical="仅用于复现旧发布漂移的场景参与者",
        asset_requirement="optional",
    ))
    return drifted


def _publish_source_projection_case(case: dict, screenplay) -> tuple[dict, dict]:
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="validated",
        trust_level="T2",
        content=screenplay_artifact_payload(screenplay),
        parent_artifact_ids=[case["merged_artifact_id"]],
        contract_version="4.0.0",
    ))
    fingerprint = screenplay_authority_fingerprint(
        case["episode_id"],
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    revision = ensure_production_revision(
        episode_id=case["episode_id"],
        kind="screenplay",
        input_fingerprint=fingerprint,
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    qa = evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="score_only",
            runtime_blocking=False,
            score=100,
            evidence={"authority_input_fingerprint": fingerprint},
        ),
    )
    result = publish_screenplay(
        episode_id=case["episode_id"],
        revision_id=revision.id,
        artifact_id=artifact["id"],
        artifact_hash=artifact["content_hash"],
        evaluation_ids=[qa["id"]],
        input_fingerprint=fingerprint,
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=False,
    )
    return artifact, result


def _action_agency_projection(screenplay) -> list[dict]:
    plan = screenplay.narrative_plan
    assert plan is not None
    return [
        {
            "action_id": action.action_id,
            "actor_ids": list(action.actor_ids),
            "target_ids": list(action.target_ids),
            "action_agency": action.action_agency.model_dump(mode="json"),
        }
        for action in plan.atomic_actions
    ]


def test_publish_rejects_contextual_drift_from_validated_v6_source() -> None:
    from app.errors import ArtifactNeedsRebuildError

    case = _source_projection_case()
    drifted = _drift_to_contextual_actor(case["compiled"])

    with pytest.raises(
        ArtifactNeedsRebuildError,
        match="source projection.*需要重建",
    ):
        _publish_source_projection_case(case, drifted)

    row = db.get_conn().execute(
        "SELECT status,stale_reason FROM artifacts "
        "WHERE type='screenplay_document' AND scope_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (case["episode_id"],),
    ).fetchone()
    assert row["status"] == "stale"
    assert "ARTIFACT_NEEDS_REBUILD" in row["stale_reason"]


def test_compile_publish_action_projection_hash_is_identical() -> None:
    case = _source_projection_case()
    artifact, _result = _publish_source_projection_case(
        case,
        case["compiled"],
    )

    published = load_screenplay_from_artifact(artifact["id"])
    compiled_projection = _action_agency_projection(case["compiled"])
    published_projection = _action_agency_projection(published)

    assert evidence_repository.content_hash(compiled_projection) == (
        evidence_repository.content_hash(published_projection)
    )
    assert sum(
        not action["actor_ids"] and not action["target_ids"]
        for action in published_projection
    ) == 12
    assert not any(
        identity_id == "ID-08"
        for action in published_projection
        for identity_id in [*action["actor_ids"], *action["target_ids"]]
    )


@pytest.mark.asyncio
async def test_startup_marks_source_projection_drift_stale_and_blocks_storyboard() -> None:
    from app.production.screenplay_document import (
        ScreenplayDocument,
        document_to_screenplay,
    )

    case = _source_projection_case()
    artifact, published = _publish_source_projection_case(
        case,
        case["compiled"],
    )
    drifted = _drift_to_contextual_actor(
        load_screenplay_from_artifact(artifact["id"])
    )
    drifted_payload = screenplay_artifact_payload(drifted)
    drifted_hash = evidence_repository.content_hash(drifted_payload)
    drifted_projection = document_to_screenplay(
        ScreenplayDocument.model_validate(drifted_payload)
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET content_json=?,content_hash=? WHERE id=?",
        (
            json.dumps(drifted_payload, ensure_ascii=False),
            drifted_hash,
            artifact["id"],
        ),
    )
    conn.execute(
        "UPDATE completion_certificates SET artifact_hash=? WHERE id=?",
        (drifted_hash, published["certificate_id"]),
    )
    conn.execute(
        "UPDATE episodes SET screenplay_json=?,screenplay_status='failed',"
        "screenplay_error=?,active_screenplay_run_id=NULL WHERE id=?",
        (
            drifted_projection.model_dump_json(),
            "模拟服务重启前的旧状态",
            case["episode_id"],
        ),
    )
    conn.commit()

    assert api.recover_screenplay_tasks() == 0

    episode = conn.execute(
        "SELECT screenplay_status,screenplay_error FROM episodes WHERE id=?",
        (case["episode_id"],),
    ).fetchone()
    persisted_artifact = conn.execute(
        "SELECT status,stale_reason FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()
    assert episode["screenplay_status"] == "failed"
    assert "ARTIFACT_NEEDS_REBUILD" in episode["screenplay_error"]
    assert persisted_artifact["status"] == "stale"
    assert "source projection" in persisted_artifact["stale_reason"]

    with pytest.raises(HTTPException) as blocked:
        await api.start_storyboard(case["episode_id"])
    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "ARTIFACT_NEEDS_REBUILD"


def test_cached_screenplay_artifact_model_is_isolated_from_mutable_readers() -> None:
    screenplay, artifact, _authority = _published_case()

    first_reader = load_screenplay_from_artifact(artifact["id"])
    first_reader.title = "caller-local mutation"
    first_reader.narrative_plan.events[0].effects_add.append("caller-local-fact")

    second_reader = load_screenplay_from_artifact(artifact["id"])
    resolved = resolve_current_screenplay_authority("episode-generic")

    assert first_reader is not second_reader
    assert second_reader.model_dump(mode="json") == screenplay.model_dump(mode="json")
    assert resolved.screenplay.model_dump(mode="json") == screenplay.model_dump(
        mode="json",
    )


def test_legacy_screenplay_artifact_without_participant_deliveries_needs_rebuild() -> None:
    screenplay = _screenplay()
    payload = screenplay_artifact_payload(screenplay)
    payload["narrative_plan"]["contract_version"] = "narrative-continuity.v1"
    payload["narrative_plan"]["atomic_actions"] = [{
        "action_id": "A-legacy",
        "actor_ids": ["character-1"],
        "target_ids": ["entity-1"],
        "semantic_intent": "Change the observable state.",
        "completion_condition": "The changed state is visible.",
    }]
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-generic",
        status="candidate",
        trust_level="T1",
        content=payload,
        contract_version="4.0.0",
    ))

    with pytest.raises(ValueError, match="ARTIFACT_NEEDS_REBUILD") as caught:
        load_screenplay_from_artifact(artifact["id"])

    assert getattr(caught.value, "code", None) == "ARTIFACT_NEEDS_REBUILD"
    assert app_errors.classify(caught.value) == (
        "conflict",
        "ARTIFACT-REBUILD",
    )
    row = db.get_conn().execute(
        "SELECT status,stale_reason FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()
    assert row["status"] == "stale"
    assert "participant_deliveries" in row["stale_reason"]


def test_current_screenplay_artifact_with_participant_deliveries_loads() -> None:
    screenplay = _screenplay()
    payload = screenplay_artifact_payload(screenplay)
    payload["narrative_plan"]["contract_version"] = "narrative-continuity.v2"
    payload["narrative_plan"]["atomic_actions"] = [{
        "action_id": "A-current",
        "actor_ids": ["character-1"],
        "target_ids": ["entity-1"],
        "participant_deliveries": [],
        "semantic_intent": "Change the observable state.",
        "completion_condition": "The changed state is visible.",
    }]
    assert all(
        "participant_deliveries" in action
        for action in payload["narrative_plan"]["atomic_actions"]
    )
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-generic",
        status="candidate",
        trust_level="T1",
        content=payload,
        contract_version="4.0.0",
    ))

    restored = load_screenplay_from_artifact(artifact["id"])

    assert restored.narrative_plan is not None
    assert restored.narrative_plan.contract_version == "narrative-continuity.v2"
    assert db.get_conn().execute(
        "SELECT status FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()["status"] == "candidate"


def test_revalidation_candidate_switches_revision_pointer_only_when_published() -> None:
    screenplay, artifact, authority = _published_case()
    from app.production.certificate import get_completion_certificate

    conn = db.get_conn()
    before = conn.execute(
        "SELECT screenplay_production_revision_id,"
        "screenplay_completion_certificate_id FROM episodes "
        "WHERE id='episode-generic'"
    ).fetchone()
    old_certificate = get_completion_certificate(authority.certificate_id)
    assert old_certificate is not None

    candidate = ensure_production_revision(
        episode_id="episode-generic",
        kind="screenplay",
        input_fingerprint=authority.input_fingerprint,
        contract_version=str(artifact["contract_version"]),
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    mark_baseline_generated(
        candidate.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )

    during = conn.execute(
        "SELECT screenplay_production_revision_id,"
        "screenplay_completion_certificate_id FROM episodes "
        "WHERE id='episode-generic'"
    ).fetchone()
    assert tuple(during) == tuple(before)

    published = publish_screenplay(
        episode_id="episode-generic",
        revision_id=candidate.id,
        artifact_id=artifact["id"],
        artifact_hash=artifact["content_hash"],
        evaluation_ids=old_certificate.evaluation_ids,
        input_fingerprint=authority.input_fingerprint,
        contract_version=str(artifact["contract_version"]),
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=False,
    )

    after = conn.execute(
        "SELECT screenplay_production_revision_id,"
        "screenplay_completion_certificate_id FROM episodes "
        "WHERE id='episode-generic'"
    ).fetchone()
    assert after["screenplay_production_revision_id"] == candidate.id
    assert after["screenplay_completion_certificate_id"] == published["certificate_id"]
    resolved = resolve_current_screenplay_authority("episode-generic")
    assert resolved.certificate_id == published["certificate_id"]
    assert resolved.screenplay.model_dump(mode="json") == screenplay.model_dump(mode="json")


def test_identity_normalization_failure_preserves_published_resolver(
    monkeypatch,
) -> None:
    screenplay, artifact, authority = _published_case()
    from app import portraits
    from app.domain.common import _project_bible_or_placeholder
    from app.domain.screenplay_ops import _prepare_published_screenplay_revalidation
    from app.production.screenplay_repair import (
        _complete_screenplay_from_working_artifact,
    )

    conn = db.get_conn()
    before = conn.execute(
        "SELECT screenplay_artifact_id,published_screenplay_artifact_id,"
        "screenplay_production_revision_id,screenplay_completion_certificate_id "
        "FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    episode = dict(conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone())
    candidate = _prepare_published_screenplay_revalidation(episode)
    project = conn.execute(
        "SELECT * FROM projects WHERE id='project-generic'"
    ).fetchone()

    def fail_identity_normalization(*_args, **_kwargs):
        raise RuntimeError("injected identity normalization failure")

    monkeypatch.setattr(
        portraits,
        "apply_screenplay_character_resolutions",
        fail_identity_normalization,
    )
    with pytest.raises(RuntimeError, match="injected identity normalization failure"):
        _complete_screenplay_from_working_artifact(
            episode_id="episode-generic",
            episode=dict(conn.execute(
                "SELECT * FROM episodes WHERE id='episode-generic'"
            ).fetchone()),
            source_text=authority.source_text,
            bible=_project_bible_or_placeholder(project),
            revision_id=candidate.id,
            run_id=None,
            checkpoint=dict(candidate.checkpoint_json),
            activation_no=1,
        )

    after = conn.execute(
        "SELECT screenplay_artifact_id,published_screenplay_artifact_id,"
        "screenplay_production_revision_id,screenplay_completion_certificate_id "
        "FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert conn.execute(
        "SELECT status FROM production_revisions WHERE id=?",
        (candidate.id,),
    ).fetchone()["status"] == "active"
    resolved = resolve_current_screenplay_authority("episode-generic")
    assert resolved.artifact_id == artifact["id"]
    assert resolved.certificate_id == authority.certificate_id
    assert resolved.screenplay.model_dump(mode="json") == screenplay.model_dump(mode="json")


def _seed_test_bible_authority() -> tuple[dict, dict]:
    bible = {
        "world": {
            "visual_style_canonical": "统一国风动画电影画风与自然光影",
            "era": "古代",
            "genre": "剧情",
        },
        "characters": [{
            "name": "Hero",
            "role": "主角",
            "appearance_canonical": "黑发青年，深色长衣，身形挺拔，佩戴一枚旧玉佩",
            "personality": "沉稳",
            "speech_style": "简洁",
            "relationships": [],
        }],
        "scenes": [],
    }
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id="project-generic",
        status="approved",
        trust_level="T4",
        content=bible,
        contract_version="character-bible-1.0.0",
    ))
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=?,bible_artifact_id=? "
        "WHERE id='project-generic'",
        (json.dumps(bible, ensure_ascii=False), artifact["id"]),
    )
    conn.commit()
    return bible, artifact


def _republish_as_screenplay_v4(artifact: dict) -> object:
    contract_version = "4.0.0"
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET contract_version=? WHERE id=?",
        (contract_version, artifact["id"]),
    )
    conn.commit()
    input_fingerprint = screenplay_authority_fingerprint(
        "episode-generic",
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    revision = ensure_production_revision(
        episode_id="episode-generic",
        kind="screenplay",
        input_fingerprint=input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    qa_gate = evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            score=100,
            evidence={"authority_input_fingerprint": input_fingerprint},
        ),
    )
    publish_screenplay(
        episode_id="episode-generic",
        revision_id=revision.id,
        artifact_id=artifact["id"],
        artifact_hash=artifact["content_hash"],
        evaluation_ids=[qa_gate["id"]],
        input_fingerprint=input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=False,
    )
    return resolve_current_screenplay_authority("episode-generic")


@pytest.mark.parametrize(
    "drift",
    [
        "published_pointer",
        "artifact_payload",
        "projection",
        "certificate",
        "revision",
        "evaluation",
        "evaluation_fingerprint",
        "source",
    ],
)
def test_resolver_fails_closed_on_every_authority_layer_drift(drift: str) -> None:
    screenplay, artifact, authority = _published_case()
    conn = db.get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone()

    if drift == "published_pointer":
        conn.execute(
            "UPDATE episodes SET published_screenplay_artifact_id='other' "
            "WHERE id='episode-generic'"
        )
    elif drift == "artifact_payload":
        changed = screenplay.model_copy(deep=True)
        changed.title = "Mutated after publication"
        # Even coordinated mutation of both JSON projections cannot preserve an
        # old Artifact hash/certificate.
        payload = changed.model_dump(mode="json")
        conn.execute(
            "UPDATE artifacts SET content_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), artifact["id"]),
        )
        conn.execute(
            "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
            (changed.model_dump_json(),),
        )
    elif drift == "projection":
        changed = screenplay.model_copy(deep=True)
        changed.title = "Projection only drift"
        conn.execute(
            "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
            (changed.model_dump_json(),),
        )
    elif drift == "certificate":
        conn.execute(
            "UPDATE completion_certificates SET input_fingerprint='drift' WHERE id=?",
            (authority.certificate_id,),
        )
    elif drift == "revision":
        conn.execute(
            "UPDATE production_revisions SET status='superseded' WHERE id=?",
            (episode["screenplay_production_revision_id"],),
        )
    elif drift == "evaluation":
        conn.execute(
            "UPDATE evaluations SET status='failed',hard_gate_passed=0 "
            "WHERE artifact_id=? AND evaluator_name='screenplay_production_qa'",
            (artifact["id"],),
        )
    elif drift == "evaluation_fingerprint":
        conn.execute(
            "UPDATE evaluations SET evidence_json='{}' "
            "WHERE artifact_id=? AND evaluator_name='screenplay_production_qa'",
            (artifact["id"],),
        )
    elif drift == "source":
        conn.execute(
                """UPDATE chapters SET title=?,content=?
                    WHERE project_id=? AND idx=?""",
                ("Changed source", "New source authority.", "project-generic", 1),
        )
        conn.execute(
            "UPDATE episodes SET source_chapters='[1]' WHERE id='episode-generic'"
        )
    conn.commit()

    with pytest.raises(ValueError):
        resolve_current_screenplay_authority("episode-generic")


def test_resolver_recovers_legacy_storyboard_duration_contamination() -> None:
    _screenplay_value, _artifact, authority = _published_case()
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET target_duration_s=60 WHERE id='episode-generic'"
    )
    conn.commit()

    resolved = resolve_current_screenplay_authority("episode-generic")

    assert resolved.input_fingerprint == authority.input_fingerprint
    assert conn.execute(
        "SELECT target_duration_s FROM episodes WHERE id='episode-generic'"
    ).fetchone()["target_duration_s"] == 60


def test_startup_recovery_restores_valid_published_screenplay_status() -> None:
    _screenplay_value, _artifact, _authority = _published_case()
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_status='failed',screenplay_error=?,"
        "active_screenplay_run_id=NULL WHERE id='episode-generic'",
        ("现有完成凭证未通过当前生产门禁",),
    )
    conn.commit()

    assert api.recover_screenplay_tasks() == 0

    episode = conn.execute(
        "SELECT screenplay_status,screenplay_error "
        "FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    assert episode["screenplay_status"] == "ready"
    assert episode["screenplay_error"] is None


def test_contract_v4_tracks_composed_bible_projection_separately_from_base_artifact() -> None:
    _published_case()
    _seed_test_bible_authority()
    conn = db.get_conn()
    project = conn.execute(
        "SELECT bible_json,bible_artifact_id FROM projects WHERE id='project-generic'"
    ).fetchone()
    projection = json.loads(project["bible_json"])
    projection["world"]["visual_style_canonical"] = "统一动画电影画风与柔和自然光影"
    projection["characters"][0]["ref_image_path"] = "/local/portrait.png"
    projection["scenes"] = [{
        "name": "宗门广场",
        "scene_canonical": "动画电影风格的宗门广场，晨光照亮石阶与主殿，空间开阔且层次清晰",
        "ref_image_path": "/local/scene.png",
    }]
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()
    bible = Bible.model_validate(projection)

    material = screenplay_authority_material(
        "episode-generic",
        bible=bible,
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    artifact = evidence_repository.get_artifact(project["bible_artifact_id"])

    assert material["bible_content_hash"] == artifact["content_hash"]
    assert material["bible_projection_hash"] == evidence_repository.content_hash(
        screenplay_bible_payload(bible)
    )
    assert material["bible_projection_hash"] != material["bible_content_hash"]
    assert "ref_image_path" not in screenplay_bible_payload(bible)["characters"][0]
    assert "ref_image_path" not in screenplay_bible_payload(bible)["scenes"][0]


def test_authority_payload_keeps_retired_scene_hash_slot() -> None:
    raw = {
        "world": {"visual_style_canonical": "统一动画电影画风"},
        "characters": [],
        "scenes": [{
            "name": "山门外",
            "scene_canonical": "清晨山门外石阶与古树形成稳定空间结构",
        }],
    }
    runtime_dump = Bible.model_validate(raw).model_dump(mode="json")

    assert "forbidden_elements" not in runtime_dump["scenes"][0]
    assert screenplay_bible_payload(raw)["scenes"][0]["forbidden_elements"] == []
    assert screenplay_bible_payload(Bible.model_validate(raw))["scenes"][0][
        "forbidden_elements"
    ] == []


def test_contract_v4_rejects_stale_runtime_bible_against_current_projection() -> None:
    _published_case()
    _seed_test_bible_authority()
    conn = db.get_conn()
    project = conn.execute(
        "SELECT bible_json,bible_artifact_id FROM projects WHERE id='project-generic'"
    ).fetchone()
    stale_bible = Bible.model_validate(
        evidence_repository.get_artifact(project["bible_artifact_id"])["content"]
    )
    projection = json.loads(project["bible_json"])
    projection["world"]["visual_style_canonical"] = "更新后的统一动画电影风格与稳定光影"
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()

    with pytest.raises(ValueError, match="项目当前组合投影不一致"):
        screenplay_authority_material(
            "episode-generic",
            bible=stale_bible,
            contract_version="4.0.0",
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        )


def test_contract_v4_accepts_character_cards_appended_during_generation() -> None:
    _published_case()
    bible, _artifact = _seed_test_bible_authority()
    runtime_bible = Bible.model_validate(bible)
    projection = json.loads(json.dumps(bible))
    projection["characters"].append({
        "name": "New Ally",
        "role": "配角",
        "appearance_canonical": "黑发少女，浅色短袄，身形利落，随身携带一只旧布包",
        "personality": "谨慎",
        "speech_style": "直率",
        "relationships": [],
    })
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()

    material = screenplay_authority_material(
        "episode-generic",
        bible=runtime_bible,
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )

    assert material["bible_projection_hash"] == evidence_repository.content_hash(
        screenplay_bible_payload(projection)
    )
    assert material["bible_projection_hash"] != evidence_repository.content_hash(
        screenplay_bible_payload(runtime_bible)
    )


def test_contract_v4_accepts_scene_cards_and_aliases_appended_downstream() -> None:
    _published_case()
    bible, _artifact = _seed_test_bible_authority()
    runtime_bible = Bible.model_validate(bible)
    projection = json.loads(json.dumps(bible))
    projection["scenes"].append({
        "name": "山门外",
        "scene_canonical": "清晨山门外石阶与古树形成稳定空间结构",
        "aliases": ["山门 / 清晨"],
        "discovery_sources": ["后续分镜预取发现"],
    })
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()

    material = screenplay_authority_material(
        "episode-generic",
        bible=runtime_bible,
        contract_version="4.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )

    assert material["bible_projection_hash"] == evidence_repository.content_hash(
        screenplay_bible_payload(projection)
    )


def test_contract_v4_rejects_existing_character_mutation_during_generation() -> None:
    _published_case()
    bible, _artifact = _seed_test_bible_authority()
    runtime_bible = Bible.model_validate(bible)
    projection = json.loads(json.dumps(bible))
    projection["characters"][0]["appearance_canonical"] = (
        "银发青年，白色长衣，身形高挑，佩戴一枚崭新的金色令牌"
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()

    with pytest.raises(ValueError, match="项目当前组合投影不一致"):
        screenplay_authority_material(
            "episode-generic",
            bible=runtime_bible,
            contract_version="4.0.0",
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        )


@pytest.mark.parametrize("artifact_backed", [True, False])
def test_published_authority_survives_later_character_appends(
    artifact_backed: bool,
) -> None:
    _screenplay_value, published_artifact, _legacy_authority = _published_case()
    bible, _artifact = _seed_test_bible_authority()
    conn = db.get_conn()
    if not artifact_backed:
        conn.execute(
            "UPDATE projects SET bible_artifact_id=NULL WHERE id='project-generic'"
        )
        conn.commit()
    published_authority = _republish_as_screenplay_v4(published_artifact)
    projection = json.loads(json.dumps(bible))
    projection["characters"].append({
        "name": "New Ally",
        "role": "配角",
        "appearance_canonical": "黑发少女，浅色短袄，身形利落，随身携带一只旧布包",
        "personality": "谨慎",
        "speech_style": "直率",
        "relationships": [],
    })
    next_artifact_id = None
    if artifact_backed:
        next_artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="character_bible",
            scope_type="project",
            scope_id="project-generic",
            status="approved",
            trust_level="T4",
            content=projection,
            contract_version="character-bible-1.0.0",
        ))
        next_artifact_id = next_artifact["id"]
    conn.execute(
        "UPDATE projects SET bible_json=?,bible_artifact_id=? "
        "WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False), next_artifact_id),
    )
    conn.commit()

    resolved = resolve_current_screenplay_authority("episode-generic")

    assert resolved.artifact_id == published_artifact["id"]
    assert resolved.input_fingerprint == published_authority.input_fingerprint


def test_published_authority_survives_later_scene_discovery_updates() -> None:
    _screenplay_value, published_artifact, _legacy_authority = _published_case()
    bible, _artifact = _seed_test_bible_authority()
    published_authority = _republish_as_screenplay_v4(published_artifact)
    projection = json.loads(json.dumps(bible))
    projection["scenes"].append({
        "name": "山门外",
        "scene_canonical": "清晨山门外石阶与古树形成稳定空间结构",
        "aliases": ["山门 / 清晨"],
        "discovery_sources": ["分镜预取新增场景"],
    })
    next_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id="project-generic",
        status="approved",
        trust_level="T4",
        content=projection,
        contract_version="character-bible-1.0.0",
    ))
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=?,bible_artifact_id=? "
        "WHERE id='project-generic'",
        (
            json.dumps(projection, ensure_ascii=False),
            next_artifact["id"],
        ),
    )
    conn.commit()

    resolved = resolve_current_screenplay_authority("episode-generic")

    assert resolved.artifact_id == published_artifact["id"]
    assert resolved.input_fingerprint == published_authority.input_fingerprint


def test_published_authority_survives_multi_step_scene_append_lineage() -> None:
    _screenplay_value, published_artifact, _legacy_authority = _published_case()
    bible, bible_artifact = _seed_test_bible_authority()
    published_authority = _republish_as_screenplay_v4(published_artifact)
    portrait_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_portrait",
        scope_type="reference_asset",
        scope_id="project-generic:Hero:2",
        status="approved",
        trust_level="T2",
        content={
            "character_name": "Hero",
            "appearance": "黑发青年，深色长衣，身形挺拔，旧玉佩旁新增一道永久伤痕",
            "episode_start": 2,
            "change": {
                "change_dimensions": ["body"],
                "persistence": "persistent",
            },
        },
        parent_artifact_ids=[bible_artifact["id"]],
        contract_version="reference-1.0.0",
    ))
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,appearance,prompt,image_path,
               bible_version,artifact_id,pack_status,change_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "portrait-hero-ep2",
            "project-generic",
            "Hero",
            2,
            portrait_artifact["content"]["appearance"],
            "portrait prompt",
            "/tmp/hero-ep2.png",
            1,
            portrait_artifact["id"],
            "ready",
            json.dumps(portrait_artifact["content"]["change"]),
            2,
        ),
    )
    conn.commit()

    def persist_next(
        projection: dict,
        parent_id: str,
        *,
        operation: str,
        scene_name: str,
    ) -> dict:
        return evidence_repository.create_artifact(EvidenceArtifact(
            type="character_bible",
            scope_type="project",
            scope_id="project-generic",
            status="approved",
            trust_level="T2",
            content=projection,
            parent_artifact_ids=[parent_id],
            contract_version="character-bible-1.0.0",
            prompt_version="reactive-scene-bible-1.0.0",
            model_snapshot={
                "operation": operation,
                "scene_name": scene_name,
            },
        ))

    projection = json.loads(json.dumps(bible))
    projection["characters"][0]["appearance_canonical"] = (
        portrait_artifact["content"]["appearance"]
    )
    projection["scenes"].append({
        "name": "宗门广场",
        "scene_canonical": "清晨宗门广场石阶与主殿形成稳定空间结构",
        "aliases": [],
        "discovery_sources": ["分镜预取新增场景"],
    })
    current = persist_next(
        projection,
        bible_artifact["id"],
        operation="incremental_scene_add",
        scene_name="宗门广场",
    )
    projection = json.loads(json.dumps(projection))
    projection["scenes"][0]["aliases"].append("山门广场 / 清晨")
    current = persist_next(
        projection,
        current["id"],
        operation="incremental_scene_alias",
        scene_name="宗门广场",
    )
    projection = json.loads(json.dumps(projection))
    projection["scenes"].append({
        "name": "后山竹林",
        "scene_canonical": "后山竹林沿石径展开，薄雾维持稳定空间层次",
        "aliases": [],
        "discovery_sources": ["分镜预取新增场景"],
    })
    current = persist_next(
        projection,
        current["id"],
        operation="incremental_scene_add",
        scene_name="后山竹林",
    )
    projection = json.loads(json.dumps(projection))
    projection["scenes"][1]["aliases"].append("竹林 / 薄雾")
    current = persist_next(
        projection,
        current["id"],
        operation="incremental_scene_alias",
        scene_name="后山竹林",
    )
    conn.execute(
        "UPDATE projects SET bible_json=?,bible_artifact_id=? "
        "WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False), current["id"]),
    )
    conn.commit()

    resolved = resolve_current_screenplay_authority("episode-generic")

    assert resolved.artifact_id == published_artifact["id"]
    assert resolved.input_fingerprint == published_authority.input_fingerprint


def test_published_authority_walks_verified_bible_ancestors_before_asset_change() -> None:
    _screenplay_value, published_artifact, _legacy_authority = _published_case()
    bible, base_artifact = _seed_test_bible_authority()
    published_authority = _republish_as_screenplay_v4(published_artifact)

    appended = json.loads(json.dumps(bible))
    appended["scenes"].append({
        "name": "宗门广场",
        "scene_canonical": "清晨宗门广场石阶与主殿形成稳定空间结构",
        "aliases": [],
        "discovery_sources": ["分镜预取新增场景"],
    })
    child_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id="project-generic",
        status="approved",
        trust_level="T4",
        content=appended,
        parent_artifact_ids=[base_artifact["id"]],
        contract_version="character-bible-1.0.0",
    ))

    changed_appearance = "黑发青年，深色长衣，旧玉佩旁留有一道永久伤痕"
    portrait_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_portrait",
        scope_type="reference_asset",
        scope_id="project-generic:Hero:2",
        status="approved",
        trust_level="T2",
        content={
            "character_name": "Hero",
            "appearance": changed_appearance,
            "episode_start": 2,
            "change": {
                "change_dimensions": ["body"],
                "persistence": "persistent",
            },
        },
        parent_artifact_ids=[child_artifact["id"]],
        contract_version="reference-1.0.0",
    ))
    current_projection = json.loads(json.dumps(appended))
    current_projection["characters"][0][
        "appearance_canonical"
    ] = changed_appearance
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=?,bible_artifact_id=? "
        "WHERE id='project-generic'",
        (
            json.dumps(current_projection, ensure_ascii=False),
            child_artifact["id"],
        ),
    )
    conn.execute(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,appearance,prompt,image_path,
               bible_version,artifact_id,pack_status,change_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "portrait-hero-ancestor-chain",
            "project-generic",
            "Hero",
            2,
            changed_appearance,
            "portrait prompt",
            "/tmp/hero-ancestor-chain.png",
            2,
            portrait_artifact["id"],
            "ready",
            json.dumps(portrait_artifact["content"]["change"]),
            3,
        ),
    )
    conn.commit()

    resolved = resolve_current_screenplay_authority("episode-generic")

    assert resolved.artifact_id == published_artifact["id"]
    assert resolved.input_fingerprint == published_authority.input_fingerprint


def _seed_lagging_bible_pointer_then_append_alias(
    published_artifact: dict,
) -> tuple[object, dict]:
    bible, base_bible_artifact = _seed_test_bible_authority()
    published_projection = json.loads(json.dumps(bible))
    published_projection["characters"][0]["appearance_canonical"] += "。"
    published_projection["scenes"] = [{
        "name": "宗门广场",
        "scene_canonical": "清晨宗门广场石阶与主殿形成稳定空间结构",
        "aliases": [],
        "discovery_sources": ["剧本阶段发现"],
    }]
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(published_projection, ensure_ascii=False),),
    )
    conn.commit()
    published_authority = _republish_as_screenplay_v4(published_artifact)

    appended_projection = json.loads(json.dumps(published_projection))
    appended_projection["scenes"][0]["aliases"].append("山门广场 / 清晨")
    next_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id="project-generic",
        status="approved",
        trust_level="T2",
        content=appended_projection,
        parent_artifact_ids=[base_bible_artifact["id"]],
        contract_version="character-bible-1.0.0",
        prompt_version="reactive-scene-bible-1.0.0",
        model_snapshot={
            "operation": "incremental_scene_alias",
            "scene_name": "宗门广场",
        },
    ))
    conn.execute(
        "UPDATE projects SET bible_json=?,bible_artifact_id=? "
        "WHERE id='project-generic'",
        (
            json.dumps(appended_projection, ensure_ascii=False),
            next_artifact["id"],
        ),
    )
    conn.commit()
    return published_authority, appended_projection


def test_published_authority_recovers_projection_ahead_of_bible_artifact() -> None:
    _screenplay_value, published_artifact, _legacy_authority = _published_case()
    published_authority, _projection = (
        _seed_lagging_bible_pointer_then_append_alias(published_artifact)
    )

    resolved = resolve_current_screenplay_authority("episode-generic")

    assert resolved.artifact_id == published_artifact["id"]
    assert resolved.input_fingerprint == published_authority.input_fingerprint


def test_lagging_bible_recovery_rejects_mutation_after_recorded_append() -> None:
    _screenplay_value, published_artifact, _legacy_authority = _published_case()
    _published_authority, projection = (
        _seed_lagging_bible_pointer_then_append_alias(published_artifact)
    )
    projection["characters"][0]["appearance_canonical"] = (
        "银发青年，白色长衣，身形高挑，佩戴一枚崭新的金色令牌"
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()

    with pytest.raises(ValueError, match="input_fingerprint"):
        resolve_current_screenplay_authority("episode-generic")


def test_published_authority_rejects_later_existing_character_mutation() -> None:
    _screenplay_value, published_artifact, _legacy_authority = _published_case()
    bible, _artifact = _seed_test_bible_authority()
    _republish_as_screenplay_v4(published_artifact)
    projection = json.loads(json.dumps(bible))
    projection["characters"][0]["appearance_canonical"] = (
        "银发青年，白色长衣，身形高挑，佩戴一枚崭新的金色令牌"
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()

    with pytest.raises(ValueError, match="input_fingerprint"):
        resolve_current_screenplay_authority("episode-generic")


def test_legacy_contract_fingerprint_ignores_composed_projection_fields() -> None:
    _published_case()
    bible, _artifact = _seed_test_bible_authority()
    before = screenplay_authority_material(
        "episode-generic",
        bible=Bible.model_validate(bible),
        contract_version="3.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    projection = json.loads(json.dumps(bible))
    projection["world"]["visual_style_canonical"] = "后来组合进项目投影的新画风"
    projection["characters"][0]["ref_image_path"] = "/local/new-portrait.png"
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='project-generic'",
        (json.dumps(projection, ensure_ascii=False),),
    )
    conn.commit()

    after = screenplay_authority_material(
        "episode-generic",
        bible=Bible.model_validate(projection),
        contract_version="3.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )

    assert "bible_projection_hash" not in before
    assert after == before


@pytest.mark.parametrize("extra_drift", [False, True])
def test_duration_recovery_rejects_non_storyboard_or_combined_drift(
    extra_drift: bool,
) -> None:
    _published_case()
    conn = db.get_conn()
    if extra_drift:
        conn.execute(
            "UPDATE episodes SET target_duration_s=60,hook='changed' "
            "WHERE id='episode-generic'"
        )
    else:
        conn.execute(
            "UPDATE episodes SET target_duration_s=55 WHERE id='episode-generic'"
        )
    conn.commit()

    with pytest.raises(ValueError):
        resolve_current_screenplay_authority("episode-generic")


@pytest.mark.asyncio
async def test_blind_review_rejects_supplied_screenplay_drift_before_model_use() -> None:
    screenplay, artifact, _authority = _published_case()
    supplied = screenplay.model_copy(deep=True)
    supplied.title = "Caller-side mutable draft"

    with pytest.raises(NarrativeReviewError, match="REVIEW_INPUT_SCREENPLAY_DRIFT"):
        await run_blind_audience_review(
            episode_id="episode-generic",
            screenplay=supplied,
            board=_board(),
            screenplay_artifact_id=artifact["id"],
        )


def test_modern_published_plan_null_can_resolve_without_narrative_downgrade() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan = None
    artifact, _shot_artifacts = _persist_review_projection(screenplay, _board())
    contract_version = "screenplay-legacy-published.v1"
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET contract_version=? WHERE id=?",
        (contract_version, artifact["id"]),
    )
    conn.commit()
    input_fingerprint = screenplay_authority_fingerprint(
        "episode-generic",
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    revision = ensure_production_revision(
        episode_id="episode-generic",
        kind="screenplay",
        input_fingerprint=input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    qa_gate = evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="screenplay_production_qa",
            evaluator_version=SCREENPLAY_QA_PROFILE_VERSION,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            score=100,
            evidence={"authority_input_fingerprint": input_fingerprint},
        ),
    )
    publish_screenplay(
        episode_id="episode-generic",
        revision_id=revision.id,
        artifact_id=artifact["id"],
        artifact_hash=artifact["content_hash"],
        evaluation_ids=[qa_gate["id"]],
        input_fingerprint=input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=False,
    )

    resolved = resolve_current_screenplay_authority(
        "episode-generic",
        require_narrative=False,
    )
    assert resolved.screenplay.narrative_plan is None
    with pytest.raises(ValueError, match="缺少叙事权威合同"):
        resolve_current_screenplay_authority("episode-generic", require_narrative=True)


def test_common_readiness_rejects_modern_projection_plan_downgrade() -> None:
    screenplay, _artifact, _authority = _published_case()
    downgraded = screenplay.model_copy(deep=True)
    downgraded.narrative_plan = None
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
        (downgraded.model_dump_json(),),
    )
    conn.commit()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone()

    # Import through the composed domain namespace used by production routes.
    from app.domain import common

    assert common._screenplay_ready(episode) is False


@pytest.mark.parametrize("drift", ["projection_downgrade", "artifact_pointer"])
def test_storyboard_structure_rejects_authority_drift_before_preview(
    drift: str,
) -> None:
    screenplay, _artifact, _authority = _published_case()
    conn = db.get_conn()
    if drift == "projection_downgrade":
        downgraded = screenplay.model_copy(deep=True)
        downgraded.narrative_plan = None
        conn.execute(
            "UPDATE episodes SET screenplay_json=? WHERE id='episode-generic'",
            (downgraded.model_dump_json(),),
        )
    else:
        conn.execute(
            "UPDATE episodes SET screenplay_artifact_id='unpublished-draft' "
            "WHERE id='episode-generic'"
        )
    conn.commit()

    with pytest.raises(HTTPException) as caught:
        api.preview_storyboard_structure("episode-generic", {
            "operation": "duplicate_after",
            "shot_id": "shot-row-1",
            "target_index": 0,
        })

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "storyboard_screenplay_authority_invalid"
    assert conn.execute(
        "SELECT COUNT(*) FROM shots WHERE episode_id='episode-generic'"
    ).fetchone()[0] == len(_board().shots)


def test_narrative_semantic_edit_requires_candidate_release_pipeline() -> None:
    _published_case()
    session = storyboard_workspace.create_edit_session("shot-row-1")

    with pytest.raises(HTTPException) as caught:
        api.preview_shot_edit_impact("shot-row-1", {
            "edit_session_token": session["edit_session_token"],
            "changes": {"primary_action": "A different semantic action."},
        })

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "narrative_semantic_repair_required"
    action = caught.value.detail["action"]
    for stage in ("candidate", "全板叙事验证", "冷观众盲审", "原子发布"):
        assert stage in action


def test_narrative_shot_id_migration_is_dry_run_only() -> None:
    _published_case()
    conn = db.get_conn()
    row = conn.execute(
        "SELECT shot_contract_json FROM shots WHERE id='shot-row-1'"
    ).fetchone()
    contract = json.loads(row["shot_contract_json"] or "{}")
    contract["story_event_id"] = "S01"
    conn.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id='shot-row-1'",
        (json.dumps(contract, ensure_ascii=False),),
    )
    conn.commit()
    before = conn.execute(
        "SELECT shot_contract_json FROM shots WHERE id='shot-row-1'"
    ).fetchone()[0]

    preview = api.migrate_episode_shot_ids(
        "episode-generic", {"dry_run": True},
    )
    assert preview["changed"]
    with pytest.raises(HTTPException) as caught:
        api.migrate_episode_shot_ids("episode-generic", {"dry_run": False})

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "narrative_semantic_repair_required"
    assert conn.execute(
        "SELECT shot_contract_json FROM shots WHERE id='shot-row-1'"
    ).fetchone()[0] == before
