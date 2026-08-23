from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import api, db, errors as app_errors, storyboard_workspace, task_registry
from app.capabilities.direct import enter_handler
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.narrative import NARRATIVE_CONTRACT_VERSION
from app.narrative_review import NarrativeReviewError, run_blind_audience_review
from app.production.publish import publish_screenplay
from app.production.patch import (
    load_screenplay_from_artifact,
    screenplay_artifact_payload,
)
from app.production.revision import ensure_production_revision, mark_baseline_generated
from app.production.screenplay_authority import (
    SCREENPLAY_QA_PROFILE_VERSION,
    assert_screenplay_matches_validated_v7_source,
    resolve_current_screenplay_authority,
    screenplay_authority_material,
    screenplay_bible_payload,
    screenplay_authority_fingerprint,
)
from app.schemas import ActionAgency, Bible, Character, NarrativeIdentityContract, World
from app.screenplay_scene_shards import (
    SCREENPLAY_MERGED_IR_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
)
from tests.test_narrative_continuity import _board, _screenplay
from tests.test_narrative_review import _persist_review_projection


MERGED_IR_ARTIFACT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_generation_ir_merged_art_949de359c598.json"
)


def _bind_fixture_ir_identities(ir) -> list[Character]:
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
    return characters


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


def _published_episode() -> dict:
    return dict(db.get_conn().execute(
        "SELECT * FROM episodes WHERE id='episode-generic'"
    ).fetchone())


def test_invalid_certificate_recommends_resume_only_when_published_revalidation_is_eligible(
    monkeypatch,
) -> None:
    _screenplay_value, artifact, _authority = _published_case()
    episode = _published_episode()
    monkeypatch.setattr(api, "_screenplay_ready", lambda _episode: False)

    state = api._screenplay_authority_state(
        episode,
        shot_count=0,
        production={},
    )

    assert state["code"] == "qa_certificate_invalid"
    assert state["can_resume"] is True
    assert state["recommended_action"] == "resume_screenplay"
    assert db.get_conn().execute(
        "SELECT status FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()["status"] == "approved"

    revision = api._prepare_published_screenplay_revalidation(episode)
    assert revision.baseline_done is True
    assert revision.working_artifact_id == artifact["id"]


@pytest.mark.asyncio
async def test_legacy_published_revalidation_rebuilds_current_contract(
    monkeypatch,
) -> None:
    _screenplay_value, artifact, _authority = _published_case()
    monkeypatch.setattr(api, "_screenplay_ready", lambda _episode: False)
    monkeypatch.setattr(api, "_require_harness_engine", lambda _project_id: None)

    class Recorder:
        run_id = "run-revalidation"

        def cancel(self, _message: str) -> None:
            raise AssertionError("successful revalidation must not cancel")

    monkeypatch.setattr(
        api,
        "_new_screenplay_recorder",
        lambda *_args, **_kwargs: Recorder(),
    )

    def capture_spawn(_kind, _key, coro, *, project_id=None):
        coro.close()

    monkeypatch.setattr(task_registry, "spawn", capture_spawn)

    with enter_handler():
        result = await api.resume_screenplay("episode-generic", body={})

    assert result["status"] == "queued"
    assert result["run_id"] == "run-revalidation"
    assert result["mode"] == "baseline_rebuild"
    assert result["revision_id"]
    revision = db.get_conn().execute(
        "SELECT baseline_generation_count,baseline_artifact_id,working_artifact_id "
        "FROM production_revisions "
        "WHERE episode_id='episode-generic' AND status='active'"
    ).fetchone()
    assert tuple(revision) == (0, None, None)


@pytest.mark.parametrize(
    ("published_pointer", "artifact_status", "expected_code"),
    [
        pytest.param("", None, "published_screenplay_missing", id="missing"),
        pytest.param(
            "missing-artifact",
            None,
            "published_screenplay_artifact_missing",
            id="artifact-missing",
        ),
        pytest.param(None, "candidate", "published_screenplay_not_approved", id="nonapproved"),
        pytest.param(None, "stale", "published_screenplay_not_approved", id="stale"),
    ],
)
def test_invalid_certificate_does_not_recommend_unexecutable_action(
    monkeypatch,
    published_pointer,
    artifact_status,
    expected_code,
) -> None:
    _screenplay_value, artifact, _authority = _published_case()
    conn = db.get_conn()
    if published_pointer is not None:
        conn.execute(
            "UPDATE episodes SET published_screenplay_artifact_id=? "
            "WHERE id='episode-generic'",
            (published_pointer,),
        )
    if artifact_status is not None:
        conn.execute(
            "UPDATE artifacts SET status=? WHERE id=?",
            (artifact_status, artifact["id"]),
        )
    conn.commit()
    episode = _published_episode()
    monkeypatch.setattr(api, "_screenplay_ready", lambda _episode: False)

    state = api._screenplay_authority_state(
        episode,
        shot_count=0,
        production={},
    )

    assert state["code"] == expected_code
    assert state["can_resume"] is False
    assert state["recommended_action"] == "refresh"
    with pytest.raises(HTTPException) as caught:
        api._prepare_published_screenplay_revalidation(episode)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == expected_code


def test_unknown_revalidation_check_fails_closed_without_executable_action(
    monkeypatch,
) -> None:
    _published_case()
    episode = _published_episode()
    monkeypatch.setattr(api, "_screenplay_ready", lambda _episode: False)

    def fail_unknown(**_kwargs):
        raise RuntimeError("unknown validation failure")

    monkeypatch.setattr(
        "app.production.screenplay_authority.assert_screenplay_matches_validated_v7_source",
        fail_unknown,
    )

    state = api._screenplay_authority_state(
        episode,
        shot_count=0,
        production={},
    )

    assert state["code"] == "published_screenplay_revalidation_check_failed"
    assert state["can_resume"] is False
    assert state["recommended_action"] == "refresh"
    with pytest.raises(HTTPException) as caught:
        api._prepare_published_screenplay_revalidation(episode)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == state["code"]


def test_typed_source_drift_has_priority_without_marking_artifact_stale(
    monkeypatch,
) -> None:
    _screenplay_value, artifact, _authority = _published_case()
    episode = _published_episode()
    monkeypatch.setattr(api, "_screenplay_ready", lambda _episode: False)
    observed: dict[str, object] = {}

    def fail_typed(**kwargs):
        observed["mark_stale"] = kwargs["mark_stale"]
        raise app_errors.ArtifactNeedsRebuildError(
            artifact_id=artifact["id"],
            artifact_type="screenplay_document",
            reason="validated-v6 source drift",
        )

    monkeypatch.setattr(
        "app.production.screenplay_authority.assert_screenplay_matches_validated_v7_source",
        fail_typed,
    )

    state = api._screenplay_authority_state(
        episode,
        shot_count=0,
        production={},
    )

    assert state["code"] == "ARTIFACT_NEEDS_REBUILD"
    assert state["can_resume"] is False
    assert state["recommended_action"] == "refresh"
    assert observed["mark_stale"] is False
    assert db.get_conn().execute(
        "SELECT status FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()["status"] == "approved"


def test_first_episode_baseline_resume_keeps_production_checkpoint_action(
    monkeypatch,
) -> None:
    _published_case()
    episode = _published_episode()
    monkeypatch.setattr(api, "_screenplay_ready", lambda _episode: False)

    state = api._screenplay_authority_state(
        episode,
        shot_count=0,
        production={
            "can_resume_baseline": True,
            "stage_stop_reason": "failed",
        },
    )

    assert episode["episode_no"] == 1
    assert state["code"] == "workflow_failed_recoverable"
    assert state["can_resume"] is True
    assert state["recommended_action"] == "resume_screenplay"


def _source_projection_case(
    *,
    episode_id: str = "episode-source-projection",
    project_id: str = "project-source-projection",
    reuse_episode: bool = False,
) -> dict:
    from app.screenplay_ir import (
        IR_VERSION,
        ScreenplayGenerationIR,
        compile_screenplay_ir,
    )

    # Build the current authority fixture from typed event ownership.  The old
    # production merged fixture intentionally remains an incompatible-rebuild
    # fixture; it must never be promoted by reading character names from prose.
    from tests.test_screenplay_ir import _ir_payload

    payload = _ir_payload()
    payload["format_version"] = IR_VERSION
    payload["identities"] = [payload["identities"][0]]
    payload["identities"][0].update({
        "display_name": "Hero",
        "authority_id": "bible:Hero",
    })
    payload["scenes"] = [payload["scenes"][0]]
    payload["scenes"][0]["character_keys"] = ["g"]
    payload["scenes"][0]["units"][0]["text"] = (
        "Hero独自在咖啡厅等待同行者，不时看向门口。"
    )
    payload["scenes"][0]["units"][1]["speaker_key"] = "g"
    payload["scenes"][0]["units"][1]["text"] = "再等十分钟。"
    payload["scenes"][0]["units"][1]["source_text"] = "再等十分钟。"
    payload["events"] = [payload["events"][0]]
    payload["events"][0]["actor_keys"] = ["g"]
    payload["events"][0]["target_keys"] = []
    event_relations = {
        str(event["key"]): {
            "actors": list(event.get("actor_keys") or []),
            "targets": list(event.get("target_keys") or []),
        }
        for event in payload["events"]
    }
    payload["events"] = []
    payload["beats"] = []
    payload["coverage"] = []
    payload["source_audit_annotations"] = []
    payload["source_scene_owners"] = {}
    for source_index, scene in enumerate(payload["scenes"], start=1):
        source_id = f"SRC{source_index:04d}"
        payload["source_scene_owners"][source_id] = scene["key"]
        relations = event_relations[scene["units"][0]["event_key"]]
        for unit_index, unit in enumerate(scene["units"], start=1):
            speaker = str(unit.get("speaker_key") or "")
            actor_keys = [speaker] if speaker else list(relations["actors"])
            unit.update({
                "event_key": f"{scene['key']}:event:{unit_index:03d}",
                "unit_key": f"{scene['key']}:{source_id}:{unit_index:03d}:unit",
                "source_segment_ids": [source_id],
                "actor_keys": actor_keys,
                "target_keys": list(relations["targets"]),
                "onscreen_entity_keys": list(dict.fromkeys([
                    *actor_keys,
                    *relations["targets"],
                ])),
                "participant_deliveries": [],
                "state_subject_key": speaker or actor_keys[0],
                "environment_only": False,
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
            })
    payload["source_semantics"] = {
        source_id: {
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "disposition": "deliver",
            "projection_policy": "picture",
        }
        for source_id in payload["source_scene_owners"]
    }
    payload["source_ownership_hash"] = hashlib.sha256(
        json.dumps(
            {
                "source_scene_owners": payload["source_scene_owners"],
                "source_semantics": payload["source_semantics"],
                "scene_derivations": payload.get("scene_derivations") or [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        ).hexdigest()
    ir = ScreenplayGenerationIR.model_validate(payload)
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

    characters = _bind_fixture_ir_identities(ir)
    world = World(visual_style_canonical="统一动画电影风格")
    conn = db.get_conn()
    if reuse_episode:
        project = conn.execute(
            "SELECT bible_json FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        current_bible = Bible.model_validate_json(project["bible_json"])
        known_names = {character.name for character in current_bible.characters}
        current_bible.characters.extend(
            character
            for character in characters
            if character.name not in known_names
        )
        bible = current_bible
        conn.execute(
            "UPDATE projects SET bible_json=? WHERE id=?",
            (bible.model_dump_json(), project_id),
        )
        conn.execute("DELETE FROM chapters WHERE project_id=?", (project_id,))
        conn.execute(
            "INSERT INTO chapters(project_id,idx,title,content) VALUES(?,?,?,?)",
            (project_id, 1, "Fixture", source_body),
        )
        conn.execute(
            """UPDATE episodes
                  SET source_chapters='[1]',
                      target_duration_s=1800,
                      screenplay_character_resolutions='[]'
                WHERE id=?""",
            (episode_id,),
        )
    else:
        bible = Bible(world=world, characters=characters)
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
        contract_version="screenplay-narrative-blueprint.v4",
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
        "contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
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
        contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
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
        contract_version=SCREENPLAY_MERGED_IR_VERSION,
    ))
    return {
        "episode_id": episode_id,
        "compiled": compiled,
        "merged_artifact_id": merged["id"],
    }


@pytest.mark.parametrize(
    "semantic_level",
    ["source", "unit"],
)
def test_current_screenplay_artifact_requires_complete_ir_semantics(
    semantic_level: str,
) -> None:
    case = _source_projection_case()
    conn = db.get_conn()
    merged = evidence_repository.get_artifact(
        case["merged_artifact_id"],
        conn=conn,
    )
    assert merged is not None
    content = deepcopy(merged["content"])
    if semantic_level == "source":
        source_id = next(iter(content["source_semantics"]))
        content["source_semantics"][source_id].pop("projection_policy")
    else:
        content["scenes"][0]["units"][0].pop("render_policy")
    conn.execute(
        "UPDATE artifacts SET content_json=?,content_hash=? WHERE id=?",
        (
            json.dumps(content, ensure_ascii=False),
            evidence_repository.content_hash(content),
            merged["id"],
        ),
    )
    conn.commit()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-generic",
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[merged["id"]],
        contract_version="4.0.0",
    ))

    with pytest.raises(
        ValueError,
        match="ARTIFACT_NEEDS_REBUILD",
    ) as caught:
        load_screenplay_from_artifact(artifact["id"])

    assert getattr(caught.value, "code", None) == "ARTIFACT_NEEDS_REBUILD"


def _assert_artifact_needs_rebuild(artifact_id: str) -> None:
    with pytest.raises(
        ValueError,
        match="ARTIFACT_NEEDS_REBUILD",
    ) as caught:
        load_screenplay_from_artifact(artifact_id)
    assert getattr(caught.value, "code", None) == "ARTIFACT_NEEDS_REBUILD"


def test_current_screenplay_artifact_plan_null_needs_rebuild() -> None:
    case = _source_projection_case()
    payload = screenplay_artifact_payload(case["compiled"])
    payload["narrative_plan"] = None
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-generic",
        status="candidate",
        trust_level="T1",
        content=payload,
        parent_artifact_ids=[case["merged_artifact_id"]],
        contract_version="4.0.0",
    ))

    _assert_artifact_needs_rebuild(artifact["id"])


def test_current_screenplay_artifact_empty_parent_lineage_needs_rebuild() -> None:
    case = _source_projection_case()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[],
        contract_version="4.0.0",
    ))

    _assert_artifact_needs_rebuild(artifact["id"])


def test_current_screenplay_artifact_complete_lineage_loads() -> None:
    case = _source_projection_case()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[case["merged_artifact_id"]],
        contract_version="4.0.0",
    ))

    restored = load_screenplay_from_artifact(artifact["id"])

    assert restored.narrative_plan is not None


def test_current_screenplay_artifact_missing_parent_needs_rebuild() -> None:
    case = _source_projection_case()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=["art-missing-parent"],
        contract_version="4.0.0",
    ))

    _assert_artifact_needs_rebuild(artifact["id"])


def test_current_screenplay_artifact_missing_shard_needs_rebuild() -> None:
    case = _source_projection_case()
    conn = db.get_conn()
    merged = evidence_repository.get_artifact(case["merged_artifact_id"])
    assert merged is not None
    parent_ids = [
        parent_id
        for parent_id in merged["parent_artifact_ids"]
        if (evidence_repository.get_artifact(parent_id) or {}).get("type")
        != "screenplay_scene_shard"
    ]
    conn.execute(
        "UPDATE artifacts SET parent_artifact_ids_json=? WHERE id=?",
        (json.dumps(parent_ids), merged["id"]),
    )
    conn.commit()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[merged["id"]],
        contract_version="4.0.0",
    ))

    _assert_artifact_needs_rebuild(artifact["id"])


def test_current_screenplay_artifact_broken_lineage_needs_rebuild() -> None:
    case = _source_projection_case()
    merged = evidence_repository.get_artifact(case["merged_artifact_id"])
    assert merged is not None
    shard_id = next(
        parent_id
        for parent_id in merged["parent_artifact_ids"]
        if (evidence_repository.get_artifact(parent_id) or {}).get("type")
        == "screenplay_scene_shard"
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET scope_id='different-episode' WHERE id=?",
        (shard_id,),
    )
    conn.commit()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[merged["id"]],
        contract_version="4.0.0",
    ))

    _assert_artifact_needs_rebuild(artifact["id"])


@pytest.mark.parametrize(
    ("lineage_case", "should_rebuild"),
    [
        ("all_old_shards", True),
        ("mixed_shards", True),
        ("old_merged_current_shards", True),
        ("tampered_merged_hash", True),
        ("tampered_shard_hash", True),
        ("tampered_direct_authority_parent", True),
        ("complete_current", False),
    ],
)
def test_validated_v7_source_authority_requires_complete_current_lineage(
    lineage_case: str,
    should_rebuild: bool,
) -> None:
    case = _source_projection_case(
        episode_id=f"episode-lineage-{lineage_case}",
        project_id=f"project-lineage-{lineage_case}",
    )
    conn = db.get_conn()
    merged = evidence_repository.get_artifact(case["merged_artifact_id"])
    assert merged is not None
    shard_ids = [
        parent_id for parent_id in merged["parent_artifact_ids"]
        if (evidence_repository.get_artifact(parent_id) or {}).get("type")
        == "screenplay_scene_shard"
    ]
    assert shard_ids
    if lineage_case == "all_old_shards":
        conn.execute(
            "UPDATE artifacts SET contract_version=? WHERE id=?",
            ("screenplay-scene-shard.v9", shard_ids[0]),
        )
    elif lineage_case == "mixed_shards":
        old_shard = evidence_repository.create_artifact(EvidenceArtifact(
            type="screenplay_scene_shard",
            scope_type="episode",
            scope_id=case["episode_id"],
            status="validated",
            trust_level="T1",
            content={"contract_version": "screenplay-scene-shard.v9"},
            contract_version="screenplay-scene-shard.v9",
        ))
        conn.execute(
            "UPDATE artifacts SET parent_artifact_ids_json=? WHERE id=?",
            (
                json.dumps([*merged["parent_artifact_ids"], old_shard["id"]]),
                merged["id"],
            ),
        )
    elif lineage_case == "old_merged_current_shards":
        conn.execute(
            "UPDATE artifacts SET contract_version=? WHERE id=?",
            ("screenplay-generation-ir-merged.v8", merged["id"]),
        )
    elif lineage_case == "tampered_merged_hash":
        tampered = deepcopy(merged["content"])
        tampered["_tampered"] = True
        conn.execute(
            "UPDATE artifacts SET content_json=? WHERE id=?",
            (json.dumps(tampered, ensure_ascii=False), merged["id"]),
        )
    elif lineage_case == "tampered_shard_hash":
        tampered = deepcopy(
            evidence_repository.get_artifact(shard_ids[0])["content"]
        )
        tampered["_tampered"] = True
        conn.execute(
            "UPDATE artifacts SET content_json=? WHERE id=?",
            (json.dumps(tampered, ensure_ascii=False), shard_ids[0]),
        )
    elif lineage_case == "tampered_direct_authority_parent":
        authority_parent = next(
            evidence_repository.get_artifact(parent_id)
            for parent_id in merged["parent_artifact_ids"]
            if (evidence_repository.get_artifact(parent_id) or {}).get("type")
            in {
                "screenplay_narrative_blueprint",
                "screenplay_identity_registry",
                "screenplay_envelope",
            }
        )
        tampered = deepcopy(authority_parent["content"])
        tampered["_tampered"] = True
        conn.execute(
            "UPDATE artifacts SET content_json=? WHERE id=?",
            (json.dumps(tampered, ensure_ascii=False), authority_parent["id"]),
        )
    conn.commit()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[merged["id"]],
        contract_version="4.0.0",
    ))
    persisted = evidence_repository.get_artifact(artifact["id"])
    assert persisted is not None

    if should_rebuild:
        with pytest.raises(app_errors.ArtifactNeedsRebuildError):
            assert_screenplay_matches_validated_v7_source(
                episode_id=case["episode_id"],
                artifact=persisted,
                screenplay=case["compiled"],
                conn=conn,
            )
    else:
        assert_screenplay_matches_validated_v7_source(
            episode_id=case["episode_id"],
            artifact=persisted,
            screenplay=case["compiled"],
            conn=conn,
        )


def _drift_to_contextual_actor(screenplay):
    drifted = screenplay.model_copy(deep=True)
    plan = drifted.narrative_plan
    assert plan is not None
    action = plan.atomic_actions[0]
    action.actor_ids = [*action.actor_ids, "ID-08"]
    action.text_provenance.identity_keys = list(dict.fromkeys([
        *action.actor_ids,
        *action.target_ids,
    ]))
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


def test_publish_rejects_contextual_drift_from_validated_v7_source() -> None:
    from app.errors import ArtifactNeedsRebuildError

    case = _source_projection_case()
    drifted = _drift_to_contextual_actor(case["compiled"])

    with pytest.raises(
        ArtifactNeedsRebuildError,
        match="需要重建.*source projection",
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
    assert all(
        action["actor_ids"] or action["target_ids"]
        for action in published_projection
    )
    assert not any(
        identity_id == "ID-08"
        for action in published_projection
        for identity_id in [*action["actor_ids"], *action["target_ids"]]
    )


def test_revalidation_marks_invalid_current_action_projection_stale() -> None:
    from app.errors import ArtifactNeedsRebuildError

    case = _source_projection_case()
    conn = db.get_conn()
    merged = evidence_repository.get_artifact(
        case["merged_artifact_id"],
        conn=conn,
    )
    assert merged is not None
    shard_id = next(
        parent_id
        for parent_id in merged["parent_artifact_ids"]
        if evidence_repository.get_artifact(parent_id, conn=conn)["type"]
        == "screenplay_scene_shard"
    )
    shard = evidence_repository.get_artifact(shard_id, conn=conn)
    assert shard is not None

    merged_content = deepcopy(merged["content"])
    merged_units = [
        unit
        for scene in merged_content["scenes"]
        for unit in scene["units"]
    ]
    unit_index = next(
        index for index, unit in enumerate(merged_units)
        if unit.get("kind") == "action"
    )
    invalid_unit = merged_units[unit_index]
    invalid_unit["actor_keys"] = []
    invalid_unit["state_subject_key"] = ""
    invalid_unit["environment_only"] = False
    invalid_unit["action_agency"] = {
        "kind": "unattributed",
        "identity_bearing": False,
        "source_segment_ids": list(invalid_unit["source_segment_ids"]),
    }

    shard_content = deepcopy(shard["content"])
    shard_unit = [
        unit
        for scene in shard_content["scenes"]
        for unit in scene["units"]
    ][unit_index]
    shard_unit.update(deepcopy(invalid_unit))
    for artifact_id, content in (
        (merged["id"], merged_content),
        (shard["id"], shard_content),
    ):
        conn.execute(
            "UPDATE artifacts SET content_json=?,content_hash=? WHERE id=?",
            (
                json.dumps(content, ensure_ascii=False),
                evidence_repository.content_hash(content),
                artifact_id,
            ),
        )

    published_screenplay = case["compiled"].model_copy(deep=True)
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="approved",
        trust_level="T2",
        content=screenplay_artifact_payload(published_screenplay),
        parent_artifact_ids=[merged["id"]],
        contract_version="4.0.0",
    ))
    conn.execute(
        "UPDATE episodes SET screenplay_artifact_id=?,"
        "published_screenplay_artifact_id=?,screenplay_status='ready' "
        "WHERE id=?",
        (artifact["id"], artifact["id"], case["episode_id"]),
    )
    conn.commit()

    with pytest.raises(
        ArtifactNeedsRebuildError,
        match="需要重建",
    ):
        assert_screenplay_matches_validated_v7_source(
            episode_id=case["episode_id"],
            artifact=artifact,
            screenplay=published_screenplay,
            conn=conn,
        )

    stale = conn.execute(
        "SELECT status,stale_reason FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()
    assert stale["status"] == "stale"
    assert "ARTIFACT_NEEDS_REBUILD" in stale["stale_reason"]


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


@pytest.mark.parametrize(
    ("collection", "field"),
    [
        ("narrative_plan.events", "narrative_layer"),
        ("source_coverage", "projection_policy"),
    ],
)
def test_current_screenplay_artifact_requires_explicit_semantics(
    collection: str,
    field: str,
) -> None:
    payload = screenplay_artifact_payload(_screenplay())
    if collection == "source_coverage":
        payload["source_coverage"] = [{
            "source_segment_id": "SRC0001",
            "disposition": "audit_only",
            "projection_policy": "audit_only",
            "beat_ids": [],
        }]
    target = payload
    for part in collection.split("."):
        target = target[part]
    target[0].pop(field)
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-generic",
        status="candidate",
        trust_level="T1",
        content=payload,
        contract_version="4.0.0",
    ))

    with pytest.raises(
        ValueError,
        match="ARTIFACT_NEEDS_REBUILD",
    ) as caught:
        load_screenplay_from_artifact(artifact["id"])

    assert getattr(caught.value, "code", None) == "ARTIFACT_NEEDS_REBUILD"
    row = db.get_conn().execute(
        "SELECT status,stale_reason FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()
    assert row["status"] == "stale"
    assert field in row["stale_reason"]


def test_current_screenplay_artifact_with_participant_deliveries_loads() -> None:
    case = _source_projection_case()
    payload = screenplay_artifact_payload(case["compiled"])
    assert all(
        "participant_deliveries" in action
        for action in payload["narrative_plan"]["atomic_actions"]
    )
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=payload,
        parent_artifact_ids=[case["merged_artifact_id"]],
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
    from app.screenplay_ir import ScreenplayGenerationIR

    fixture = json.loads(MERGED_IR_ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    fixture_ir = ScreenplayGenerationIR.model_validate(fixture["content"])
    fixture_characters = [
        character.model_dump(mode="json")
        for character in _bind_fixture_ir_identities(fixture_ir)
        if character.name != "Hero"
    ]
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
        }, *fixture_characters],
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
    case = _source_projection_case(
        episode_id="episode-generic",
        project_id="project-generic",
        reuse_episode=True,
    )
    current_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-generic",
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[case["merged_artifact_id"]],
        contract_version=contract_version,
    ))
    artifact.clear()
    artifact.update(current_artifact)
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
    projection = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='project-generic'"
    ).fetchone()["bible_json"])
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


def test_legacy_nonempty_resolution_certificate_keeps_raw_v1_fingerprint() -> None:
    _published_case()
    conn = db.get_conn()
    legacy_rows = [{
        "source_label": "青衣人",
        "canonical_name": "青衣人",
        "resolution": "functional_identity",
        "identity_group": "current-1:F1",
    }]
    raw = json.dumps(legacy_rows, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        "UPDATE episodes SET screenplay_character_resolutions=? "
        "WHERE id='episode-generic'",
        (raw,),
    )
    conn.commit()

    material = screenplay_authority_material(
        "episode-generic",
        contract_version="3.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    before = screenplay_authority_fingerprint(
        "episode-generic",
        contract_version="3.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    from app.portraits import load_screenplay_character_resolutions

    normalized = load_screenplay_character_resolutions(conn, "episode-generic")
    after = screenplay_authority_fingerprint(
        "episode-generic",
        contract_version="3.0.0",
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )

    assert material["character_resolutions"] == legacy_rows
    assert normalized[0]["authority_id"].startswith("functional:")
    assert before == after


def test_recovery_compile_receives_normalized_resolution_projection(
    monkeypatch,
) -> None:
    case = _source_projection_case()
    conn = db.get_conn()
    raw_rows = [{
        "source_label": "旧称谓",
        "canonical_name": "旧称谓",
        "resolution": "functional_identity",
        "identity_group": "current-1:F1",
        "decision_provenance": "manual",
    }]
    conn.execute(
        "UPDATE episodes SET screenplay_character_resolutions=? WHERE id=?",
        (json.dumps(raw_rows, ensure_ascii=False), case["episode_id"]),
    )
    conn.commit()
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=case["episode_id"],
        status="candidate",
        trust_level="T1",
        content=screenplay_artifact_payload(case["compiled"]),
        parent_artifact_ids=[case["merged_artifact_id"]],
        contract_version="4.0.0",
    ))
    captured: dict = {}

    def capture_compile(_merged_ir, *, episode, source_text, bible):
        captured["episode"] = episode
        return case["compiled"].model_copy(deep=True)

    monkeypatch.setattr(
        "app.screenplay_ir.compile_screenplay_ir",
        capture_compile,
    )

    restored = load_screenplay_from_artifact(artifact["id"])
    assert_screenplay_matches_validated_v7_source(
        episode_id=case["episode_id"],
        artifact=evidence_repository.get_artifact(artifact["id"]),
        screenplay=restored,
        conn=conn,
    )

    assert restored.id == case["compiled"].id
    assert captured["episode"]["character_resolutions"][0]["authority_id"].startswith(
        "functional:"
    )


@pytest.mark.parametrize("extra_drift", [False, True])
def test_duration_recovery_rejects_non_storyboard_or_combined_drift(
    extra_drift: bool,
) -> None:
    _published_case()
    conn = db.get_conn()
    if extra_drift:
        # `hook`/`cliffhanger` are normalized to a fixed "" in authority
        # material (they are editable display metadata, never production
        # input -- see the fingerprint-exclusion rationale on
        # screenplay_authority_material's `constraints` dict), so drifting
        # them no longer counts as "other" drift for this test. Use `title`,
        # a field that *is* still tracked, so this still proves the duration
        # recovery path stays narrow and does not paper over unrelated drift.
        conn.execute(
            "UPDATE episodes SET target_duration_s=60,title='changed' "
            "WHERE id='episode-generic'"
        )
    else:
        conn.execute(
            "UPDATE episodes SET target_duration_s=55 WHERE id='episode-generic'"
        )
    conn.commit()

    with pytest.raises(ValueError):
        resolve_current_screenplay_authority("episode-generic")


def test_authority_fingerprint_ignores_cliffhanger_tampering() -> None:
    """独立 review 复现用例：篡改 episodes.cliffhanger 不再影响权威指纹。

    problem 2 的修复不是"识别并拒绝篡改"，而是把 hook/cliffhanger 彻底移出
    指纹材料——它们是 docs/PROMPT_SPEC.md 定义的可编辑展示元数据，从来就不
    该是权威指纹的一部分（真正的剧本/事件证据仍由 artifact_hash 与
    full_script_text/events 等字段保护，未被这项改动触碰）。因此这里的预期
    结果是 resolve_current_screenplay_authority 在篡改后依然成功解析——
    而不是像旧版"值盲容忍"逻辑那样，只因为字符串*恰好*能让候选指纹重新算出
    同一个值才放行。
    """
    _published_case()
    conn = db.get_conn()
    conn.execute(
        "UPDATE episodes SET cliffhanger=? WHERE id='episode-generic'",
        ("ATTACKER INJECTED TEXT UNRELATED TO SCRIPT",),
    )
    conn.execute(
        "UPDATE episodes SET hook=? WHERE id='episode-generic'",
        ("ANOTHER INJECTED VALUE",),
    )
    conn.commit()

    resolved = resolve_current_screenplay_authority("episode-generic")
    assert resolved.screenplay is not None


def test_authority_fingerprint_recovers_legacy_empty_hook_certificate() -> None:
    """历史证书（签发时 hook/cliffhanger 恒为 "" ——唯一建集写入路径
    app/planning.py 的行为）在这次改动后必须继续正常解析，不因剔除这两个
    字段而失效。这里额外确认 DB 值也保持在 "" 上，模拟从未被 D9 的跨集镜像
    写回触碰过的历史行——最常见、也是唯一在这次改动之前真实出现过的状态。
    """
    _published_case()
    episode = _published_episode()
    assert episode["hook"] in (None, "")
    assert episode["cliffhanger"] in (None, "")

    resolved = resolve_current_screenplay_authority("episode-generic")
    assert resolved.screenplay is not None


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


def test_unbound_historical_contract_plan_null_loads_as_legacy_display() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan = None
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-generic",
        status="validated",
        trust_level="T2",
        content=screenplay_artifact_payload(screenplay),
        contract_version="screenplay-legacy-published.v1",
    ))

    restored = load_screenplay_from_artifact(artifact["id"])

    assert restored.narrative_plan is None


def test_published_historical_contract_plan_null_needs_rebuild() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan = None
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-generic",
        status="validated",
        trust_level="T2",
        content=screenplay_artifact_payload(screenplay),
        contract_version="screenplay-legacy-published.v1",
    ))
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,created_at) VALUES(?,?,?)",
        ("project-legacy-bound", "Legacy bound", db.now()),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,screenplay_json,
               screenplay_artifact_id,published_screenplay_artifact_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "episode-generic",
            "project-legacy-bound",
            1,
            "Legacy bound",
            screenplay.model_dump_json(),
            artifact["id"],
            artifact["id"],
            db.now(),
        ),
    )
    conn.commit()

    _assert_artifact_needs_rebuild(artifact["id"])


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


# ---------------------------------------------------------------------------
# episodes.hook / episodes.cliffhanger 写入端（跨集叙事承接）
# ---------------------------------------------------------------------------


def _publish_episode_with_hook(
    *,
    project_id: str,
    episode_id: str,
    episode_no: int,
    ending_hook: str = "",
    embed_hook_in_source: bool = True,
) -> tuple[dict, dict]:
    """Publish a minimal narrative-authority screenplay for one episode.

    Mirrors the proven-working setup in
    tests.test_narrative_review._persist_review_projection (same one-chapter
    project scaffolding and runtime_gate evaluation shape), but parameterized
    so several sequential episodes can share one project and be published
    (or republished) independently. Returns (artifact, publish_result).
    """
    conn = db.get_conn()
    if conn.execute(
        "SELECT 1 FROM projects WHERE id=?", (project_id,),
    ).fetchone() is None:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at) VALUES(?,?,?,?)",
            (project_id, project_id, "created", db.now()),
        )
        conn.execute(
            """INSERT INTO chapters(project_id,idx,title,content)
               VALUES(?,1,'Chapter 1','An observable change occurs.')""",
            (project_id,),
        )
    if conn.execute(
        "SELECT 1 FROM episodes WHERE id=?", (episode_id,),
    ).fetchone() is None:
        conn.execute(
            """INSERT INTO episodes(
                   id,project_id,episode_no,source_chapters,status,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (episode_id, project_id, episode_no, "[1]", "scripted", db.now()),
        )
    conn.commit()

    screenplay = _screenplay()
    screenplay.episode_no = episode_no
    screenplay.ending_hook = ending_hook
    if ending_hook and embed_hook_in_source:
        # publish_screenplay() re-validates ending_hook against
        # full_script_text before writing it into episodes.cliffhanger /
        # the next episode's episodes.hook (problem 1's structural
        # grounding gate). These tests exercise the write-back mechanics,
        # not grounding itself, so the fixture's source text must actually
        # contain the hook -- an empty full_script_text would otherwise get
        # every hook cleared to "" here, same as any other ungroundable
        # content. embed_hook_in_source=False deliberately keeps that
        # mismatch, for tests that exercise the rejection/observability path
        # instead.
        screenplay.full_script_text = f"（正文）{ending_hook}"
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id=episode_id,
        status="approved",
        trust_level="T2",
        content=screenplay.model_dump(mode="json"),
        contract_version=NARRATIVE_CONTRACT_VERSION,
    ))
    fingerprint = screenplay_authority_fingerprint(
        episode_id,
        contract_version=NARRATIVE_CONTRACT_VERSION,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    revision = ensure_production_revision(
        episode_id=episode_id,
        kind="screenplay",
        input_fingerprint=fingerprint,
        contract_version=NARRATIVE_CONTRACT_VERSION,
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
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            score=100,
            evidence={"authority_input_fingerprint": fingerprint},
        ),
    )
    result = publish_screenplay(
        episode_id=episode_id,
        revision_id=revision.id,
        artifact_id=artifact["id"],
        artifact_hash=artifact["content_hash"],
        evaluation_ids=[qa["id"]],
        input_fingerprint=fingerprint,
        contract_version=NARRATIVE_CONTRACT_VERSION,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        clear_downstream=False,
    )
    return artifact, result


def _episode_row(episode_id: str) -> dict:
    return dict(db.get_conn().execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,),
    ).fetchone())


def test_publish_writes_cliffhanger_and_mirrors_next_episode_hook() -> None:
    project_id = "project-hook-mirror"
    _publish_episode_with_hook(
        project_id=project_id, episode_id="ep-hook-mirror-2", episode_no=2,
    )
    _publish_episode_with_hook(
        project_id=project_id,
        episode_id="ep-hook-mirror-1",
        episode_no=1,
        ending_hook="神秘来电在深夜再次响起，屏幕上显示的是三年前失联的号码。",
    )

    ep1 = _episode_row("ep-hook-mirror-1")
    ep2 = _episode_row("ep-hook-mirror-2")
    assert ep1["cliffhanger"] == "神秘来电在深夜再次响起，屏幕上显示的是三年前失联的号码。"
    assert ep2["hook"] == "神秘来电在深夜再次响起，屏幕上显示的是三年前失联的号码。"


def test_publish_without_next_episode_row_does_not_raise() -> None:
    # No episode_no=2 row exists for this project; the mirror UPDATE must be a
    # harmless no-op (rowcount=0), not an error.
    _publish_episode_with_hook(
        project_id="project-hook-no-next",
        episode_id="ep-hook-no-next-1",
        episode_no=1,
        ending_hook="仓库大门在身后缓缓关闭。",
    )
    ep1 = _episode_row("ep-hook-no-next-1")
    assert ep1["cliffhanger"] == "仓库大门在身后缓缓关闭。"


def test_publish_empty_ending_hook_writes_and_clears_empty_string() -> None:
    project_id = "project-hook-empty"
    _publish_episode_with_hook(
        project_id=project_id, episode_id="ep-hook-empty-2", episode_no=2,
    )
    # First publish carries a real hook forward...
    _publish_episode_with_hook(
        project_id=project_id,
        episode_id="ep-hook-empty-1",
        episode_no=1,
        ending_hook="他转身看向仍未熄灭的灯。",
    )
    assert _episode_row("ep-hook-empty-2")["hook"] == "他转身看向仍未熄灭的灯。"

    # ...republishing with an empty ending_hook (model judged the story
    # complete) must overwrite both fields back to "", not leave stale text.
    _publish_episode_with_hook(
        project_id=project_id,
        episode_id="ep-hook-empty-1",
        episode_no=1,
        ending_hook="",
    )
    assert _episode_row("ep-hook-empty-1")["cliffhanger"] == ""
    assert _episode_row("ep-hook-empty-2")["hook"] == ""


def test_republishing_earlier_episode_does_not_cascade_into_next_episode_artifact() -> None:
    project_id = "project-no-cascade"
    # EP6's row must exist before EP5 publishes, so EP5's mirror-write has a
    # real target row (mirroring the production order: EP5 publishes, EP6's
    # own screenplay/storyboard get produced afterward using EP6.hook).
    artifact6, _ = _publish_episode_with_hook(
        project_id=project_id,
        episode_id="ep-no-cascade-6",
        episode_no=6,
        ending_hook="",
    )
    artifact5_a, _ = _publish_episode_with_hook(
        project_id=project_id,
        episode_id="ep-no-cascade-5",
        episode_no=5,
        # Real prose, not a bare "A": a single character is too short for the
        # bigram grounding check (app.validators.ending_hook_is_grounded) to
        # ever match a longer haystack, which would trivially clear it back
        # to "" here regardless of full_script_text containing it verbatim.
        ending_hook="灯还亮着，走廊尽头传来脚步声。",
    )
    ep6_before = _episode_row("ep-no-cascade-6")
    assert ep6_before["hook"] == "灯还亮着，走廊尽头传来脚步声。"
    assert ep6_before["screenplay_artifact_id"] == artifact6["id"]

    artifact5_b, _ = _publish_episode_with_hook(
        project_id=project_id,
        episode_id="ep-no-cascade-5",
        episode_no=5,
        ending_hook="门锁忽然从外面转动，屋里的人瞬间屏住呼吸。",
    )
    assert artifact5_b["id"] != artifact5_a["id"]

    ep6_after = _episode_row("ep-no-cascade-6")
    assert ep6_after["hook"] == "门锁忽然从外面转动，屋里的人瞬间屏住呼吸。"
    assert ep6_after["screenplay_json"] == ep6_before["screenplay_json"]
    assert ep6_after["screenplay_artifact_id"] == ep6_before["screenplay_artifact_id"]
    assert ep6_after["screenplay_artifact_id"] == artifact6["id"]


def test_publish_recheck_rejection_leaves_observable_evidence() -> None:
    """清空静默性回归测试：publish_screenplay() 复核判定 ending_hook 编造并
    清空时，以前完全不留痕迹（app/production/publish.py:356-359 直接
    `ending_hook_value = ""`）——数据上无法区分"原文真的没钩子"和"被误杀"，
    EP4 269 条原子事件那次就是这样被人工偶然发现的。现在必须能在
    provider_calls 里查到这次清空的证据：被清空的钩子原文、两层覆盖率实测值、
    最佳匹配 event id/窗口。

    embed_hook_in_source=False 让 full_script_text 里完全不含这条钩子的任何
    文字，确保 publish_screenplay() 的复核判据必定判定为编造（不依赖具体
    Tier A/Tier B 数值细节，只需要"确实被拒绝了"这一事实成立）。
    """
    fabricated_hook = "外星飞船在城市上空缓缓降落，全城陷入一片死寂。"
    _publish_episode_with_hook(
        project_id="project-hook-observability",
        episode_id="ep-hook-observability-1",
        episode_no=1,
        ending_hook=fabricated_hook,
        embed_hook_in_source=False,
    )

    # 清空动作本身必须生效：episodes.cliffhanger 不能残留编造内容。
    assert _episode_row("ep-hook-observability-1")["cliffhanger"] == ""

    rows = db.get_conn().execute(
        """SELECT meta FROM provider_calls
            WHERE kind='ending_hook_grounding_rejected' AND status='REJECTED'
            ORDER BY id DESC""",
    ).fetchall()
    assert rows, "ending_hook 被判定编造并清空时必须留下可查的观测记录"
    meta = json.loads(rows[0]["meta"])
    assert meta["episode_id"] == "ep-hook-observability-1"
    assert meta["source"] == "screenplay_publish_recheck"
    assert meta["hook_text"] == fabricated_hook
    assert meta["tier"] in ("layer1_fail", "ungrounded")
    assert isinstance(meta["layer1_coverage"], (int, float))
