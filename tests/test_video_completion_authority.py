from __future__ import annotations

import json

import pytest

from app import db
from app.completion_grant import (
    GrantValidationError,
    issue_video_completion_grant,
    validate_video_grant,
)
from app.schemas import Bible, Shot, Storyboard, World
from app.video_supervisor import (
    StoryboardRepairProposal,
    VideoSupervisorCheckpoint,
    _prepare_episode_reference_assets,
    _repair_authority_ids,
    _semantic_storyboard_repair_proposal,
    _ensure_supervisor_video_plan,
)
from tests.test_narrative_review_wall_fence import _published_authority_case


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "video-completion-authority.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _issue_published_grant(case: dict):
    episode = db.get_conn().execute(
        "SELECT project_id,storyboard_artifact_id FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    grant, _token = issue_video_completion_grant(
        episode_id="episode-generic",
        project_id=episode["project_id"],
        storyboard_artifact_id=episode["storyboard_artifact_id"],
        shots_total=len(case["board"].shots),
    )
    return grant


@pytest.mark.parametrize("drift", ["certificate", "shots"])
@pytest.mark.asyncio
async def test_asset_preparation_has_zero_calls_after_release_authority_drift(
    monkeypatch,
    drift: str,
) -> None:
    case = await _published_authority_case(monkeypatch)
    grant = _issue_published_grant(case)
    conn = db.get_conn()
    if drift == "certificate":
        certificate_id = conn.execute(
            "SELECT storyboard_completion_certificate_id FROM episodes "
            "WHERE id='episode-generic'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE completion_certificates SET artifact_hash='drifted' WHERE id=?",
            (certificate_id,),
        )
    else:
        conn.execute(
            "UPDATE shots SET action_desc=action_desc || ' authority drift' "
            "WHERE episode_id='episode-generic' AND shot_no=1"
        )
    conn.commit()

    calls: list[str] = []

    def forbidden_scan(_episode_id: str):
        calls.append("scan")
        raise AssertionError("authority verifier must run before asset scanning")

    async def forbidden_external(*_args, **_kwargs):
        calls.append("external")
        raise AssertionError("stale release must not call an asset provider")

    import app.multiview as multiview
    import app.refs as refs
    import app.scenes as scenes
    import app.video_supervisor as supervisor

    monkeypatch.setattr(supervisor, "_reference_asset_scan", forbidden_scan)
    monkeypatch.setattr(multiview, "complete_legacy_character_pack", forbidden_external)
    monkeypatch.setattr(multiview, "complete_legacy_scene_pack", forbidden_external)
    monkeypatch.setattr(refs, "generate_refs", forbidden_external)
    monkeypatch.setattr(scenes, "generate_scene_refs", forbidden_external)

    cp = VideoSupervisorCheckpoint(
        episode_id="episode-generic",
        grant_id=grant.grant_id,
        storyboard_artifact_id=grant.storyboard_artifact_id,
    )
    with pytest.raises(GrantValidationError) as fenced:
        await _prepare_episode_reference_assets(
            "episode-generic",
            cp=cp,
            run_id=None,
        )
    assert fenced.value.code in {
        "RELEASE_QUALIFICATION_INVALID",
        "RELEASE_QUALIFICATION_CHANGED",
    }
    assert calls == []


@pytest.mark.asyncio
async def test_optional_review_drift_does_not_revoke_video_grant(monkeypatch) -> None:
    case = await _published_authority_case(monkeypatch)
    grant = _issue_published_grant(case)
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET status='stale' WHERE id=?",
        (case["report_artifact"]["id"],),
    )
    conn.commit()

    validated = validate_video_grant(
        grant.grant_id,
        episode_id="episode-generic",
        storyboard_artifact_id=grant.storyboard_artifact_id,
    )

    assert validated.grant_id == grant.grant_id


def test_grant_recomputes_bound_plan_and_capability_snapshot(monkeypatch) -> None:
    from app.evidence import repository as evidence_repository
    import app.completion_grant as completion_grant
    import app.video_plan as video_plan
    from tests.test_video_plan_reconcile import _conn

    conn = _conn()
    for module in (completion_grant, evidence_repository, video_plan):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    grant, _token = issue_video_completion_grant(
        episode_id="e",
        project_id="p",
        storyboard_artifact_id="storyboard_rev_1",
        shots_total=2,
    )
    assert grant.episode_video_plan_id == "evp-1"
    assert grant.episode_video_plan_revision == 1
    assert grant.video_plan_release_hash
    assert grant.capability_snapshot_id == "cap-1"

    conn.execute(
        "UPDATE provider_video_capability_snapshots SET probe_result='drifted' WHERE id='cap-1'"
    )
    conn.commit()
    with pytest.raises(GrantValidationError) as stale:
        validate_video_grant(
            grant.grant_id,
            episode_id="e",
            storyboard_artifact_id="storyboard_rev_1",
        )
    assert stale.value.code == "RELEASE_QUALIFICATION_CHANGED"


@pytest.mark.asyncio
async def test_supervisor_checkpoint_acquires_exact_grant_plan_binding(monkeypatch) -> None:
    from app.evidence import repository as evidence_repository
    import app.completion_grant as completion_grant
    import app.video_plan as video_plan
    import app.video_supervisor as supervisor
    from tests.test_video_plan_reconcile import _conn

    conn = _conn()
    for module in (
        completion_grant,
        evidence_repository,
        video_plan,
        supervisor,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    grant, _token = issue_video_completion_grant(
        episode_id="e",
        project_id="p",
        storyboard_artifact_id="storyboard_rev_1",
        shots_total=2,
    )
    cp = VideoSupervisorCheckpoint(
        episode_id="e",
        grant_id=grant.grant_id,
        storyboard_artifact_id="storyboard_rev_1",
    )
    verified = await _ensure_supervisor_video_plan(cp)

    assert verified is not None
    assert cp.episode_video_plan_id == grant.episode_video_plan_id == "evp-1"
    assert cp.episode_video_plan_revision == grant.episode_video_plan_revision == 1
    assert cp.video_plan_release_hash == grant.video_plan_release_hash
    assert cp.capability_snapshot_id == grant.capability_snapshot_id == "cap-1"


@pytest.mark.asyncio
async def test_semantic_repair_accepts_unfamiliar_action_without_phrase_rules(
    monkeypatch,
) -> None:
    original = Shot(
        shot_no=1,
        shot_id="shot-authority-1",
        duration_s=5,
        shot_size="medium",
        camera_move="locked",
        scene_setting="abstract chamber",
        scene_name="abstract chamber",
        characters=["A"],
        action_desc="A crosses the chamber and seals the unstable aperture in one motion.",
        first_frame_desc="A faces an unstable aperture across the silent chamber.",
        last_frame_desc="A stands beside the sealed aperture after the movement resolves.",
        source_excerpt="A crosses the chamber and seals the unstable aperture before it expands.",
    )
    candidate = original.model_copy(
        update={
            "action_desc": (
                "A pivots beneath the unfamiliar lattice, redirects its momentum, "
                "and settles at the sealed boundary."
            )
        }
    )
    affected = _repair_authority_ids([original, candidate])
    proposal = StoryboardRepairProposal(
        proposal_id="semantic-proposal-unfamiliar",
        base_shot_id="database-shot-1",
        operation="replace",
        reason="The visible state change needs a single readable kinetic intention.",
        expected_total_duration_s=5,
        affected_authority=affected,
        candidate_shots=[candidate],
    )

    from app.harness import model_gateway
    import app.validators as validators

    async def fake_chat(*_args, **_kwargs):
        return json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(validators, "validate_storyboard", lambda *_a, **_k: [])
    monkeypatch.setattr(
        validators,
        "validate_storyboard_continuity_contract",
        lambda *_a, **_k: [],
    )
    bible = Bible(characters=[], world=World(visual_style_canonical="graphic"))
    result, candidate_board = await _semantic_storyboard_repair_proposal(
        board=Storyboard(episode_no=1, shots=[original]),
        shot_index=0,
        database_shot_id="database-shot-1",
        screenplay=None,
        bible=bible,
        target_duration_s=5,
        episode_id="legacy-episode",
        repair_plan=None,
        observed_issue_codes=["UNFAMILIAR_RELATION_FAILURE"],
    )
    assert result.proposal_id == "semantic-proposal-unfamiliar"
    assert candidate_board.shots[0].action_desc == candidate.action_desc
