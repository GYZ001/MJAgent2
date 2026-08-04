from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import db
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
