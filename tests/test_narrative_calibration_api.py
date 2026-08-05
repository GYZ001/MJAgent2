from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact
from conftest import SessionTestClient
from tests.test_narrative_publish_gate import _reviewed_publish_candidate


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "narrative-calibration-api.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()


@pytest.mark.asyncio
async def test_human_calibration_http_flow_freezes_before_target_scoring(
    monkeypatch,
) -> None:
    await _reviewed_publish_candidate(monkeypatch)
    from app.main import app

    client = SessionTestClient(TestClient(app))
    protocol_response = client.get(
        "/api/episodes/episode-generic/narrative-calibration/protocol"
    )
    assert protocol_response.status_code == 200
    protocol = protocol_response.json()
    assert protocol["protocol"]["watch_once"] is True
    assert protocol["protocol"]["source_and_targets_hidden_before_freeze"] is True
    prior_id = protocol["audience_priors"][0]["audience_prior_id"]

    freeze_response = client.post(
        "/api/episodes/episode-generic/narrative-calibration/freeze",
        json={
            "participant_id_hash": "participant-api-test",
            "audience_prior_id": prior_id,
            "watched_once": True,
            "watch_count": 1,
            "replay_or_seek_used": False,
            "source_material_seen": False,
            "target_answers_seen": False,
            "director_intent_seen": False,
            "spontaneous_recall_frozen": True,
            "spontaneous_recall": {
                "free_text": "The viewer recalls one visible causal change.",
            },
            "content_dimensions": {
                "genre": "api-fixture",
                "form": "single-watch",
            },
        },
    )
    assert freeze_response.status_code == 200
    frozen = freeze_response.json()
    assert frozen["freeze_artifact_id"]
    assert frozen["target_contract"]

    observation_response = client.post(
        "/api/episodes/episode-generic/narrative-calibration/observations",
        json={
            "freeze_artifact_id": frozen["freeze_artifact_id"],
            "neutral_followup_observations": [],
            "target_delta_observations": [
                {
                    "audience_prior_id": frozen["audience_prior_id"],
                    "target_delta_id": target["target_delta_id"],
                    "observed_score": 0.9,
                    "observed_interpretation": {
                        "free_text": "The intended causal relation was understood.",
                    },
                }
                for target in frozen["target_contract"]
            ],
        },
    )
    assert observation_response.status_code == 200
    assert observation_response.json()["status"] == "validated"

    status_response = client.get("/api/narrative-calibration")
    assert status_response.status_code == 200
    assert status_response.json()["ready"] is True


@pytest.mark.asyncio
async def test_ai_one_watch_simulation_activates_without_human_claims(
    monkeypatch,
) -> None:
    case = await _reviewed_publish_candidate(monkeypatch)
    conn = db.get_conn()
    previous_shot_artifact = evidence_repository.get_artifact(
        case["shot_artifacts"][0]
    )
    assert previous_shot_artifact is not None
    rebound_shot_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_shot",
            scope_type=previous_shot_artifact["scope_type"],
            scope_id=previous_shot_artifact["scope_id"],
            status="candidate",
            trust_level="T1",
            content=previous_shot_artifact["content"],
            parent_artifact_ids=[previous_shot_artifact["id"]],
            contract_version=previous_shot_artifact["contract_version"],
        )
    )
    rebound_shot_artifact = evidence_repository.commit_artifact(
        None,
        rebound_shot_artifact["id"],
        [Evaluation(
            evaluator_type="deterministic",
            evaluator_name="test_storyboard_projection_rebind",
            evaluator_version="test.v1",
            status="passed",
            hard_gate_passed=True,
            score=100,
        )],
    )
    conn.execute(
        "UPDATE shots SET storyboard_artifact_id=? WHERE storyboard_artifact_id=?",
        (rebound_shot_artifact["id"], previous_shot_artifact["id"]),
    )
    conn.execute(
        "UPDATE artifacts SET status='superseded' WHERE id=?",
        (case["calibration_artifact"]["id"],),
    )
    conn.commit()
    from app.main import app

    client = SessionTestClient(TestClient(app))
    response = client.post(
        "/api/episodes/episode-generic/narrative-calibration/ai-simulate",
        json={},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["activated"] is True
    assert payload["authority_mode"] == "ai_simulation"
    assert payload["model_pass_threshold"] == pytest.approx(0.8)
    assert payload["minimum_predicted_score"] == pytest.approx(0.95)
    artifact = conn.execute(
        "SELECT type,trust_level,content_json FROM artifacts WHERE id=?",
        (payload["artifact_id"],),
    ).fetchone()
    assert artifact["type"] == "ai_one_watch_simulation_report"
    assert artifact["trust_level"] == "T2"
    assert '"human_observation_count":0' in artifact["content_json"]
    ai_parents = conn.execute(
        "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
        (payload["artifact_id"],),
    ).fetchone()["parent_artifact_ids_json"]
    assert case["report_artifact"]["id"] not in ai_parents
    rebound_report_id = json.loads(ai_parents)[0]
    rebound_report = conn.execute(
        "SELECT type,status FROM artifacts WHERE id=?",
        (rebound_report_id,),
    ).fetchone()
    assert rebound_report["type"] == "narrative_review_report"
    assert rebound_report["status"] == "validated"

    status = client.get("/api/narrative-calibration").json()
    assert status["ready"] is True
    assert status["authority_mode"] == "ai_simulation"
