from __future__ import annotations

import json

import pytest

from app import db
from app.continuity import shot_contract_dict
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.narrative import (
    AUDIENCE_PERCEPTUAL_SURFACE_VERSION,
    NARRATIVE_CONTRACT_VERSION,
    audience_perceptual_surface,
    audience_perceptual_surface_hash,
)
from app.narrative_review import (
    BLIND_PERCEPTUAL_INPUT_ARTIFACT_TYPE,
    BLIND_READER_PROMPT_VERSION,
    NarrativeReviewError,
    run_blind_audience_review,
    verify_persisted_narrative_review,
)
from app.schemas import AudioTimelineItem, RequiredOnScreenText
from app.production.publish import publish_screenplay
from app.production.revision import ensure_production_revision, mark_baseline_generated
from app.production.screenplay_authority import (
    SCREENPLAY_QA_PROFILE_VERSION,
    screenplay_authority_fingerprint,
)
from tests.test_narrative_continuity import _board, _screenplay


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "narrative-review.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _observation(payload: dict) -> dict:
    contract = payload["output_contract"]
    return {
        "observation_id": contract["observation_id"],
        "audience_prior_id": contract["audience_prior_id"],
        "anchor": {"type": "sequence", "id": "episode-generic"},
        "spontaneous_recall": {
            "recognized_entities": ["entity-1"],
            "inferred_propositions": ["A visible result occurred."],
            "causal_hypotheses": ["The visible event caused the change."],
            "character_goal_hypotheses": [],
            "active_question_ids": [],
        },
        "neutral_followup_observations": [],
        "noticed_attention_target_ids": ["entity-1"],
        "spontaneous_supporting_evidence_ids": ["EV-1"],
        "supporting_evidence_ids": ["EV-1"],
        "confidence": 0.8,
    }


async def _passing_chat(messages, **kwargs):
    payload = json.loads(messages[1]["content"])
    if kwargs["call_meta"]["call_role"] == "blind_reader":
        return json.dumps(_observation(payload), ensure_ascii=False)
    contract = payload["output_contract"]
    return json.dumps(
        {
            **contract,
            "target_delta_results": [
                {
                    **item,
                    "result": "satisfied",
                    "supporting_evidence_ids": ["EV-1"],
                    "reason": "The frozen observation registered the visible result.",
                }
                for item in contract["target_delta_results"]
            ],
            "decision": "pass",
            "low_percentile_result": {"rate": 1.0},
            "reason": "Every prior path reached its target.",
        },
        ensure_ascii=False,
    )


def _persist_review_projection(screenplay, board, screenplay_artifact=None):
    if screenplay_artifact is None:
        screenplay_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_document",
                scope_type="episode",
                scope_id="episode-generic",
                status="approved",
                trust_level="T2",
                content=screenplay.model_dump(mode="json"),
                contract_version=(
                    NARRATIVE_CONTRACT_VERSION
                    if screenplay.narrative_plan is not None
                    else None
                ),
            )
        )
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES(?,?,?,?)",
        ("project-generic", "Generic", "created", db.now()),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,screenplay_json,screenplay_status,
               screenplay_artifact_id,status,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "episode-generic",
            "project-generic",
            board.episode_no,
            screenplay.model_dump_json(),
            "ready",
            screenplay_artifact["id"],
            "scripted",
            db.now(),
        ),
    )
    conn.commit()
    if screenplay.narrative_plan is not None:
        # Blind review is downstream of the published screenplay authority,
        # never of a merely approved test artifact.  Keep this common fixture
        # on the same certificate/revision path as production so old publish
        # and video tests cannot accidentally exercise a weaker boundary.
        conn.execute(
            "UPDATE artifacts SET contract_version=? WHERE id=?",
            (NARRATIVE_CONTRACT_VERSION, screenplay_artifact["id"]),
        )
        conn.commit()
        input_fingerprint = screenplay_authority_fingerprint(
            "episode-generic",
            contract_version=NARRATIVE_CONTRACT_VERSION,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        )
        revision = ensure_production_revision(
            episode_id="episode-generic",
            kind="screenplay",
            input_fingerprint=input_fingerprint,
            contract_version=NARRATIVE_CONTRACT_VERSION,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
            resume=False,
        )
        mark_baseline_generated(
            revision.id,
            baseline_artifact_id=screenplay_artifact["id"],
            working_artifact_id=screenplay_artifact["id"],
        )
        qa_gate = evidence_repository.create_evaluation(
            screenplay_artifact["id"],
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
            artifact_id=screenplay_artifact["id"],
            artifact_hash=screenplay_artifact["content_hash"],
            evaluation_ids=[qa_gate["id"]],
            input_fingerprint=input_fingerprint,
            contract_version=NARRATIVE_CONTRACT_VERSION,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
            clear_downstream=False,
        )
        screenplay_artifact = evidence_repository.get_artifact(screenplay_artifact["id"])
        assert screenplay_artifact is not None
    shot_artifact_ids = []
    for shot in board.shots:
        artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="storyboard_shot",
                scope_type="storyboard_checkpoint",
                scope_id=f"episode-generic:{shot.shot_no}",
                status="validated",
                trust_level="T2",
                content=shot.model_dump(mode="json"),
                parent_artifact_ids=[screenplay_artifact["id"]],
            )
        )
        shot_artifact_ids.append(artifact["id"])
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,shot_size,camera_move,
                   scene_setting,characters,action_desc,first_frame_desc,
                   last_frame_desc,source_excerpt,narration,dialogues,transition,
                   continuity_from_prev,shot_contract_json,storyboard_artifact_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"shot-row-{shot.shot_no}",
                "episode-generic",
                shot.shot_no,
                shot.duration_s,
                shot.shot_size,
                shot.camera_move,
                shot.scene_setting,
                json.dumps(shot.characters),
                shot.action_desc,
                shot.first_frame_desc,
                shot.last_frame_desc,
                shot.source_excerpt,
                shot.narration,
                json.dumps([item.model_dump(mode="json") for item in shot.dialogues]),
                shot.transition,
                int(shot.continuity_from_prev),
                json.dumps(shot_contract_dict(shot)),
                artifact["id"],
            ),
        )
    conn.commit()
    return screenplay_artifact, shot_artifact_ids


def test_audience_perceptual_surface_contains_only_perceivable_timed_tracks() -> None:
    screenplay = _screenplay()
    board = _board()
    shot = board.shots[0]
    shot.camera_angle = "侧面"
    shot.spatial_anchor = "主体位于画面左侧，出口位于右后方"
    shot.transition = "叠化"
    shot.audio_timeline = [
        AudioTimelineItem(
            start_s=0.8,
            end_s=2.4,
            type="offscreen_voice",
            speaker_id="character-1",
            text="A result is heard before it is seen.",
            lip_sync=False,
            emotion="惊恐",
        )
    ]
    shot.required_text = RequiredOnScreenText(
        surface="door plaque",
        exact_text="VISIBLE",
        appear_start_s=1.0,
        stable_until_s=3.0,
        style="high contrast",
    )
    prior = screenplay.narrative_plan.audience_priors[0]

    surface = audience_perceptual_surface(prior, screenplay, board)
    projected = surface["ordered_storyboard_as_seen"][0]

    assert surface["perceptual_surface_version"] == AUDIENCE_PERCEPTUAL_SURFACE_VERSION
    assert projected["visual_track"] == {
        "first_frame": shot.first_frame_desc,
        "visible_action": shot.action_desc,
        "last_frame": shot.last_frame_desc,
    }
    assert projected["camera_as_seen"] == {
        "shot_size": shot.shot_size,
        "angle": "侧面",
        "movement": shot.camera_move,
        "spatial_anchor": shot.spatial_anchor,
    }
    assert projected["edit_as_seen"]["incoming_transition"] == "episode_open"
    assert projected["edit_as_seen"]["outgoing_transition"] == "叠化"
    assert projected["audible_timeline"][0]["start_s"] == 0.8
    assert projected["audible_timeline"][0]["sound_type"] == "offscreen_voice"
    assert projected["on_screen_text_timeline"][0] == {
        "surface": "door plaque",
        "content": "VISIBLE",
        "appear_start_s": 1.0,
        "stable_until_s": 3.0,
        "visual_style": "high contrast",
        "reading_priority": "plot_critical",
    }
    serialized = json.dumps(surface, ensure_ascii=False)
    for forbidden_key in (
        "director_objective",
        "target_deltas",
        "planned_state_in_fact_ids",
        "planned_delta_add_fact_ids",
        "audience_state_paths",
        "reserved_future_event_ids",
    ):
        assert forbidden_key not in serialized
    assert audience_perceptual_surface_hash(surface) == audience_perceptual_surface_hash(
        audience_perceptual_surface(prior, screenplay, board)
    )


@pytest.mark.asyncio
async def test_blind_review_isolates_models_and_persists_lineage_before_comparison(
    monkeypatch,
) -> None:
    screenplay = _screenplay()
    board = _board()
    screenplay_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id="episode-generic",
            status="approved",
            trust_level="T2",
            content=screenplay.model_dump(mode="json"),
        )
    )
    _screenplay_artifact, shot_artifact_ids = _persist_review_projection(
        screenplay,
        board,
        screenplay_artifact,
    )
    call_roles: list[str] = []
    blind_inputs: list[dict] = []
    persisted_before_comparison = 0

    async def fake_chat(messages, **kwargs):
        nonlocal persisted_before_comparison
        role = kwargs["call_meta"]["call_role"]
        call_roles.append(role)
        payload = json.loads(messages[1]["content"])
        if role == "blind_reader":
            blind_inputs.append(payload["input"])
            return json.dumps(_observation(payload), ensure_ascii=False)

        persisted_before_comparison = db.get_conn().execute(
            "SELECT COUNT(*) AS count FROM artifacts "
            "WHERE type='blind_audience_observation' AND status='validated'"
        ).fetchone()["count"]
        contract = payload["output_contract"]
        results = [
            {
                **item,
                "result": "satisfied",
                "supporting_evidence_ids": ["EV-1"],
                "reason": "The frozen observation explicitly registered the visible result.",
            }
            for item in contract["target_delta_results"]
        ]
        return json.dumps(
            {
                **contract,
                "target_delta_results": results,
                "decision": "pass",
                "low_percentile_result": {"rate": 1.0},
                "reason": "Every prior path reached its target.",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.narrative_review.model_gateway.chat", fake_chat)

    observations, report, artifact_ids = await run_blind_audience_review(
        episode_id="episode-generic",
        screenplay=screenplay,
        board=board,
        screenplay_artifact_id=screenplay_artifact["id"],
    )

    assert call_roles == ["blind_reader", "blind_reader", "intent_comparator"]
    assert persisted_before_comparison == 2
    assert len(observations) == 2
    assert report.decision == "pass"
    assert len(artifact_ids) == 6

    for model_input in blind_inputs:
        serialized_input = json.dumps(model_input, ensure_ascii=False)
        for forbidden_key in (
            "source_evidence",
            "propositions",
            "director_objective",
            "target_deltas",
            "assimilation_tasks",
            "reserved_future_event_ids",
        ):
            assert forbidden_key not in serialized_input

    artifacts = [evidence_repository.get_artifact(artifact_id) for artifact_id in artifact_ids]
    by_type = {artifact["type"]: artifact for artifact in artifacts if artifact is not None}
    assert by_type["storyboard_review_input"]["parent_artifact_ids"] == [
        screenplay_artifact["id"],
        *shot_artifact_ids,
    ]
    observation_artifacts = [
        artifact for artifact in artifacts
        if artifact is not None and artifact["type"] == "blind_audience_observation"
    ]
    perceptual_input_artifacts = [
        artifact for artifact in artifacts
        if artifact is not None
        and artifact["type"] == BLIND_PERCEPTUAL_INPUT_ARTIFACT_TYPE
    ]
    assert len(perceptual_input_artifacts) == 2
    assert all(
        artifact["contract_version"] == AUDIENCE_PERCEPTUAL_SURFACE_VERSION
        and artifact["prompt_version"] == BLIND_READER_PROMPT_VERSION
        for artifact in perceptual_input_artifacts
    )
    assert {
        artifact["content"]["audience_prior_id"]
        for artifact in perceptual_input_artifacts
    } == {"AP-cold", "AP-context"}
    assert {
        artifact["content"]["perceptual_surface_hash"]
        for artifact in perceptual_input_artifacts
    } == {
        audience_perceptual_surface_hash(model_input)
        for model_input in blind_inputs
    }
    assert len(observation_artifacts) == 2
    assert all(artifact["status"] == "validated" for artifact in observation_artifacts)
    assert all(
        screenplay_artifact["id"] in artifact["parent_artifact_ids"]
        for artifact in observation_artifacts
    )
    report_artifact = by_type["narrative_review_report"]
    assert report_artifact["status"] == "validated"
    assert {artifact["id"] for artifact in observation_artifacts} <= set(
        report_artifact["parent_artifact_ids"]
    )
    assert {artifact["id"] for artifact in perceptual_input_artifacts} <= set(
        report_artifact["parent_artifact_ids"]
    )
    assert verify_persisted_narrative_review(
        episode_id="episode-generic",
        screenplay=screenplay,
        board=board,
        report=report,
        artifact_ids=artifact_ids,
    ) == report_artifact["id"]

    evaluation_rows = db.get_conn().execute(
        "SELECT artifact_id,evaluator_name,status,hard_gate_passed,evaluation_role "
        "FROM evaluations ORDER BY created_at,id"
    ).fetchall()
    isolation_rows = [
        row for row in evaluation_rows
        if row["evaluator_name"] == "blind_review_isolation_gate"
    ]
    comparator_rows = [
        row for row in evaluation_rows
        if row["evaluator_name"] == "narrative_blind_comparator"
    ]
    assert len(isolation_rows) == 2
    assert len(comparator_rows) == 2
    assert all(bool(row["hard_gate_passed"]) for row in evaluation_rows)
    assert all(row["evaluation_role"] == "runtime_gate" for row in evaluation_rows)


@pytest.mark.asyncio
async def test_target_leak_is_rejected_before_observation_persistence(monkeypatch) -> None:
    screenplay = _screenplay()
    board = _board()
    screenplay_artifact, _shot_artifact_ids = _persist_review_projection(
        screenplay,
        board,
    )

    async def leaking_chat(messages, **_kwargs):
        payload = json.loads(messages[1]["content"])
        observation = _observation(payload)
        observation["spontaneous_recall"]["director_objective"] = "leaked target"
        return json.dumps(observation, ensure_ascii=False)

    monkeypatch.setattr("app.narrative_review.model_gateway.chat", leaking_chat)

    with pytest.raises(NarrativeReviewError, match="BLIND_REVIEW_TARGET_LEAK"):
        await run_blind_audience_review(
            episode_id="episode-generic",
            screenplay=screenplay,
            board=board,
            screenplay_artifact_id=screenplay_artifact["id"],
        )

    persisted = db.get_conn().execute(
        "SELECT COUNT(*) AS count FROM artifacts WHERE type='blind_audience_observation'"
    ).fetchone()["count"]
    assert persisted == 0


@pytest.mark.asyncio
async def test_persisted_review_rejects_tampered_exact_per_prior_payload(
    monkeypatch,
) -> None:
    screenplay = _screenplay()
    board = _board()
    screenplay_artifact, _shot_artifact_ids = _persist_review_projection(
        screenplay,
        board,
    )
    monkeypatch.setattr("app.narrative_review.model_gateway.chat", _passing_chat)

    _observations, report, artifact_ids = await run_blind_audience_review(
        episode_id="episode-generic",
        screenplay=screenplay,
        board=board,
        screenplay_artifact_id=screenplay_artifact["id"],
    )
    perceptual_artifact = next(
        evidence_repository.get_artifact(artifact_id)
        for artifact_id in artifact_ids
        if evidence_repository.get_artifact(artifact_id)["type"]
        == BLIND_PERCEPTUAL_INPUT_ARTIFACT_TYPE
    )
    tampered = json.loads(json.dumps(perceptual_artifact["content"]))
    tampered["model_prompt_payload"]["input"]["ordered_storyboard_as_seen"][0][
        "visual_track"
    ]["last_frame"] = "A different, unreviewed ending frame."
    db.get_conn().execute(
        "UPDATE artifacts SET content_json=? WHERE id=?",
        (json.dumps(tampered, ensure_ascii=False), perceptual_artifact["id"]),
    )
    db.get_conn().commit()

    with pytest.raises(NarrativeReviewError, match="BLIND_PERCEPTUAL_PROMPT_HASH_INVALID"):
        verify_persisted_narrative_review(
            episode_id="episode-generic",
            screenplay=screenplay,
            board=board,
            report=report,
            artifact_ids=artifact_ids,
        )
