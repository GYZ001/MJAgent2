from __future__ import annotations

import json

import pytest

from app import api, db, worker
from app.continuity import PROMPT_CONTRACT_VERSION
from app.evidence import repository as evidence_repository
from app.narrative_review import run_blind_audience_review
from app.production.certificate import (
    consume_completion_certificate,
    issue_completion_certificate,
)
from app.production.revision import (
    ensure_production_revision,
    mark_baseline_generated,
    set_published_artifact,
)
from app.schemas import StoryboardOutline, StoryboardOutlineShot
from tests.test_narrative_publish_gate import (
    _artifact,
    _install_global_calibration,
    _install_passing_review_model,
    _runtime_gate,
)
from tests.test_narrative_continuity import _board, _screenplay
from tests.test_narrative_review import _persist_review_projection


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "narrative-review-wall.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


async def _published_authority_case(monkeypatch) -> dict:
    screenplay = _screenplay()
    screenplay.full_script_text = "A complete source-grounded screenplay projection."
    board = _board()
    board.shots[-1].is_final = True
    for shot in board.shots:
        shot.prompt_contract_version = PROMPT_CONTRACT_VERSION
    screenplay_artifact, _shot_artifacts = _persist_review_projection(
        screenplay, board,
    )
    outline = StoryboardOutline(
        episode_no=board.episode_no,
        shots=[
            StoryboardOutlineShot.model_validate({
                key: value
                for key, value in shot.model_dump(mode="json").items()
                if key in StoryboardOutlineShot.model_fields
            })
            for shot in board.shots
        ],
    )
    from app.storyboard_authority import persist_storyboard_outline_authority

    persist_storyboard_outline_authority(
        "episode-generic",
        outline,
    )
    _install_passing_review_model(monkeypatch)
    _observations, _report, review_artifact_ids = await run_blind_audience_review(
        episode_id="episode-generic",
        screenplay=screenplay,
        board=board,
        screenplay_artifact_id=screenplay_artifact["id"],
    )
    report_artifact = next(
        artifact
        for artifact_id in review_artifact_ids
        if (artifact := evidence_repository.get_artifact(artifact_id)) is not None
        and artifact["type"] == "narrative_review_report"
    )
    calibration_artifact = _install_global_calibration(report_artifact["id"])
    working_candidate = _artifact(
        artifact_type="storyboard_document",
        content=board.model_dump(mode="json"),
        parent_artifact_ids=[
            *review_artifact_ids,
            calibration_artifact["id"],
        ],
    )
    revision = ensure_production_revision(
        episode_id="episode-generic",
        kind="storyboard",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=working_candidate["id"],
        working_artifact_id=working_candidate["id"],
    )
    exact_review = _runtime_gate(
        working_candidate["id"], evaluator_name="narrative_blind_comparator",
    )
    full_gate = _runtime_gate(
        working_candidate["id"], evaluator_name="storyboard_full_gate",
    )
    case = {
        "screenplay_artifact": screenplay_artifact,
        "working_candidate": working_candidate,
        "report_artifact": report_artifact,
        "calibration_artifact": calibration_artifact,
        "revision": revision,
        "exact_review": exact_review,
        "full_gate": full_gate,
        "evaluation_ids": [exact_review["id"], full_gate["id"]],
        "board": board,
    }
    conn = db.get_conn()
    contract_version = "narrative-continuity.v1"
    conn.execute(
        "UPDATE artifacts SET contract_version=? WHERE id=?",
        (
            contract_version,
            case["working_candidate"]["id"],
        ),
    )
    report_row = conn.execute(
        "SELECT prompt_version FROM artifacts WHERE id=?",
        (case["report_artifact"]["id"],),
    ).fetchone()
    comparator_version = str(report_row["prompt_version"] or "")
    assert comparator_version
    conn.execute(
        "UPDATE evaluations SET evaluator_version=? WHERE id=?",
        (comparator_version, case["exact_review"]["id"]),
    )
    conn.execute(
        "UPDATE evaluations SET evaluator_version=? WHERE id=?",
        (contract_version, case["full_gate"]["id"]),
    )
    conn.execute(
        """UPDATE production_revisions
              SET input_fingerprint='storyboard-input-v1',
                  contract_version=?,qa_profile_version='storyboard-full-gate-2'
            WHERE id=?""",
        (contract_version, case["revision"].id),
    )
    conn.commit()

    storyboard_certificate = issue_completion_certificate(
        kind="storyboard",
        scope_id="episode-generic",
        artifact_id=case["working_candidate"]["id"],
        artifact_hash=case["working_candidate"]["content_hash"],
        input_fingerprint="storyboard-input-v1",
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
        evaluation_ids=case["evaluation_ids"],
        production_revision_id=case["revision"].id,
    )
    consume_completion_certificate(storyboard_certificate.certificate_id)
    set_published_artifact(
        case["revision"].id,
        case["working_candidate"]["id"],
        certificate_id=storyboard_certificate.certificate_id,
    )
    conn.execute(
        """UPDATE episodes
              SET status='confirmed',
                  storyboard_artifact_id=?,published_storyboard_artifact_id=?,
                  narrative_status='ready',narrative_review_artifact_id=?,
                  narrative_calibration_artifact_id=?
            WHERE id='episode-generic'""",
        (
            case["working_candidate"]["id"],
            case["working_candidate"]["id"],
            case["report_artifact"]["id"],
            case["calibration_artifact"]["id"],
        ),
    )
    conn.commit()
    return case


def _captured_review_snapshot(snapshot: dict) -> dict:
    """Mirror the immutable subset persisted by the enqueue boundary."""
    return {
        key: snapshot.get(key)
        for key in (
            "qualification_version",
            "published_screenplay_artifact_id",
            "confirmed_storyboard_artifact_id",
            "screenplay_revision",
            "storyboard_revision",
            "asset_inputs",
            "asset_soft_warnings",
        )
    }


def _insert_version(*, version_id: str, snapshot: dict | None) -> str:
    conn = db.get_conn()
    shot_id = conn.execute(
        "SELECT id FROM shots WHERE episode_id='episode-generic' ORDER BY shot_no LIMIT 1"
    ).fetchone()["id"]
    meta = {}
    if snapshot is not None:
        meta["review_dependency_snapshot"] = snapshot
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES(?,?,99,'prompt',?,'running',?,?)""",
        (
            version_id,
            shot_id,
            f"idem-{version_id}",
            json.dumps(meta, ensure_ascii=False),
            db.now(),
        ),
    )
    conn.commit()
    return shot_id


@pytest.mark.asyncio
async def test_authority_qualification_binds_release_hashes_review_certificate_and_projection(
    monkeypatch,
) -> None:
    case = await _published_authority_case(monkeypatch)

    snapshot = api._review_upstream_snapshot("episode-generic")
    authority = snapshot["narrative_authority"]

    assert snapshot["eligible_for_production"] is True
    assert snapshot["narrative_authority_required"] is True
    assert snapshot["narrative_authority_verified"] is True
    assert snapshot["qualification_version"].startswith(
        f"{snapshot['narrative_authority_version']}:"
    )
    assert authority["published_screenplay_artifact_hash"] == case[
        "screenplay_artifact"
    ]["content_hash"]
    assert authority["published_storyboard_artifact_hash"] == case[
        "working_candidate"
    ]["content_hash"]
    assert authority["narrative_review_artifact_id"] == case["report_artifact"]["id"]
    assert authority["narrative_review_verified"] is True
    assert authority["screenplay_certificate_verified"] is True
    assert authority["storyboard_certificate_verified"] is True
    assert authority["storyboard_completion_authority_verified"] is True
    from app.evidence.repository import content_hash
    from app.narrative import storyboard_authority_projection

    assert authority["shots_projection_hash"] == content_hash(
        storyboard_authority_projection(case["board"])
    )
    assert authority["shots_projection_verified"] is True


@pytest.mark.asyncio
async def test_narrative_worker_rejects_missing_dependency_snapshot(monkeypatch) -> None:
    await _published_authority_case(monkeypatch)
    shot_id = _insert_version(version_id="version-without-snapshot", snapshot=None)

    with pytest.raises(worker.ReviewDependencyFence, match="SNAPSHOT_MISSING"):
        worker._assert_review_dependency_fence(
            {"episode_id": "episode-generic", "shot_id": shot_id},
            "version-without-snapshot",
            "worker_start",
        )


@pytest.mark.asyncio
async def test_worker_fences_current_shots_projection_drift_before_provider_use(
    monkeypatch,
) -> None:
    await _published_authority_case(monkeypatch)
    snapshot = api._review_upstream_snapshot("episode-generic")
    shot_id = _insert_version(
        version_id="version-bound-to-authority",
        snapshot=_captured_review_snapshot(snapshot),
    )
    worker._assert_review_dependency_fence(
        {"episode_id": "episode-generic", "shot_id": shot_id},
        "version-bound-to-authority",
        "provider_submit",
    )

    conn = db.get_conn()
    conn.execute(
        "UPDATE shots SET action_desc=action_desc || ' drift' WHERE id=?",
        (shot_id,),
    )
    conn.commit()

    with pytest.raises(worker.ReviewDependencyFence) as stale:
        worker._assert_review_dependency_fence(
            {"episode_id": "episode-generic", "shot_id": shot_id},
            "version-bound-to-authority",
            "provider_poll",
        )
    detail = json.loads(str(stale.value))
    assert detail["code"] == "NARRATIVE_STORYBOARD_AUTHORITY_INVALID"
    assert "shots" in detail["message"] or "Storyboard Artifact" in detail["message"]


@pytest.mark.asyncio
async def test_worker_ignores_optional_review_score_change_before_provider_submit(
    monkeypatch,
) -> None:
    case = await _published_authority_case(monkeypatch)
    snapshot = api._review_upstream_snapshot("episode-generic")
    shot_id = _insert_version(
        version_id="version-with-invalidated-gate",
        snapshot=_captured_review_snapshot(snapshot),
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE evaluations SET status='failed',hard_gate_passed=0 WHERE id=?",
        (case["exact_review"]["id"],),
    )
    conn.commit()

    worker._assert_review_dependency_fence(
        {"episode_id": "episode-generic", "shot_id": shot_id},
        "version-with-invalidated-gate",
        "provider_submit",
    )


@pytest.mark.asyncio
async def test_parallel_narrative_job_ignores_sibling_gallery_outputs(
    monkeypatch,
) -> None:
    await _published_authority_case(monkeypatch)
    snapshot = api._review_upstream_snapshot("episode-generic")
    shot_id = _insert_version(
        version_id="version-before-sibling-gallery",
        snapshot=_captured_review_snapshot(snapshot),
    )
    current = {
        **snapshot,
        "qualification_version": (
            f"{snapshot['narrative_authority_version']}:gallery-output-changed"
        ),
        "asset_inputs": [{
            "shot_id": "another-shot",
            "ref_id": "new-sibling-output",
            "gate_status": "scored",
        }],
    }
    monkeypatch.setattr(api, "_review_upstream_snapshot", lambda *_a, **_k: current)

    worker._assert_review_dependency_fence(
        {"episode_id": "episode-generic", "shot_id": shot_id},
        "version-before-sibling-gallery",
        "provider_input_adoption",
    )


def test_legacy_plan_null_worker_keeps_missing_snapshot_compatibility(monkeypatch) -> None:
    from tests.test_review_wall_prd import _conn

    conn = _conn()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('legacy-v','s1',1,'prompt','legacy-k','running','{}',0)"""
    )
    conn.commit()

    worker._assert_review_dependency_fence(
        {"episode_id": "e", "shot_id": "s1"},
        "legacy-v",
        "worker_start",
    )
