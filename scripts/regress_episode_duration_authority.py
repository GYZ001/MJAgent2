"""Replay one production screenplay Artifact through duration authority on a DB copy."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db
from app.completion_grant import issue_video_completion_grant
from app.domain.storyboard_ops import _insert_storyboard_shot
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.narrative_priority import compile_authoritative_delivery_outline
from app.production.patch import load_screenplay_from_artifact
from app.production.publish import publish_storyboard
from app.production.revision import (
    ensure_production_revision,
    mark_baseline_generated,
)
from app.schemas import Shot, Storyboard, StoryboardOutlineShot
from app.storyboard_authority import (
    persist_storyboard_outline_authority,
    resolve_storyboard_outline_authority,
)
from app.video_plan import authoritative_storyboard_plan_cost
from scripts.replay_episode_artifact import replay


def _reset_connection(db_path: Path) -> None:
    current = getattr(db._local, "conn", None)
    if current is not None:
        current.close()
    db._local = threading.local()
    db.DB_PATH = db_path
    db.DATA_DIR = db_path.parent
    config.DB_PATH = db_path
    config.DATA_DIR = db_path.parent


def _shot_from_outline(
    brief: StoryboardOutlineShot,
    *,
    final: bool,
) -> Shot:
    shared = {
        key: value
        for key, value in brief.model_dump(mode="json").items()
        if key in Shot.model_fields
    }
    action = str(brief.beat or brief.primary_action or brief.covers or "").strip()
    return Shot.model_validate({
        **shared,
        "shot_no": int(brief.shot_no),
        "duration_s": int(brief.duration_s or 0),
        "shot_size": brief.camera_size or "中景",
        "camera_move": brief.camera_movement or "固定",
        "characters": list(brief.characters_visible),
        "action_desc": action or f"执行权威镜头任务 {brief.shot_id}",
        "first_frame_desc": brief.state_in or action or "本镜起始状态",
        "last_frame_desc": brief.state_out or action or "本镜完成状态",
        "source_excerpt": "",
        "is_final": final,
    })


def run_regression(
    *,
    source_db: Path,
    artifact_id: str,
    project_id: str | None,
    copy_path: Path | None,
    expected_duration_s: int,
    expected_cost_cny: float,
    expected_story_events: int,
    expected_source_segments: int,
) -> dict:
    source_db = source_db.resolve()
    source_stat_before = source_db.stat()
    replay_result = replay(
        db_path=source_db,
        artifact_id=artifact_id,
        project_id=project_id,
    )
    checks = {
        "duration": replay_result["authoritative_target_duration_s"]
        == expected_duration_s,
        "cost": replay_result["authoritative_first_pass_budget_cny"]
        == expected_cost_cny,
        "story_events": replay_result["story_event_coverage"]
        == {"covered": expected_story_events, "total": expected_story_events},
        "source_segments": (
            replay_result["source_coverage"]["covered"]
            == expected_source_segments
            and replay_result["source_coverage"]["total"]
            == expected_source_segments
        ),
        "screenplay_gate": not replay_result["screenplay_errors"],
        "outline_gate": not replay_result["outline_errors"],
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("production Artifact replay mismatch: " + ", ".join(failed))

    if copy_path is None:
        copy_dir = Path(tempfile.mkdtemp(prefix="manju-duration-authority-"))
        copy_path = copy_dir / source_db.name
    else:
        copy_path = copy_path.expanduser().resolve()
        copy_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_db, copy_path)
    _reset_connection(copy_path)
    db.init_db()
    conn = db.get_conn()

    artifact = evidence_repository.get_artifact(artifact_id, conn=conn)
    if artifact is None:
        raise RuntimeError(f"copied DB missing Artifact: {artifact_id}")
    episode_id = str(artifact["scope_id"])
    episode_before = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if episode_before is None:
        raise RuntimeError(f"copied DB missing episode: {episode_id}")
    planning_duration_s = int(episode_before["target_duration_s"] or 0)
    screenplay = load_screenplay_from_artifact(artifact_id)
    project = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?",
        (episode_before["project_id"],),
    ).fetchone()
    from app.schemas import Bible

    bible = (
        Bible.model_validate_json(project["bible_json"])
        if project is not None and project["bible_json"]
        else None
    )
    _projected, outline, _audit = compile_authoritative_delivery_outline(
        screenplay,
        bible=bible,
    )
    authority = persist_storyboard_outline_authority(
        episode_id,
        outline,
        conn=conn,
    )

    # Simulate a process restart: discard the process-local connection and
    # reconstruct the exact authority solely from the copied database.
    _reset_connection(copy_path)
    conn = db.get_conn()
    restarted = resolve_storyboard_outline_authority(
        episode_id,
        conn=conn,
    )
    board = Storyboard(
        episode_no=int(outline.episode_no),
        shots=[
            _shot_from_outline(
                brief,
                final=index == len(outline.shots) - 1,
            )
            for index, brief in enumerate(outline.shots)
        ],
    )
    existing_shots = int(conn.execute(
        "SELECT COUNT(*) AS count FROM shots WHERE episode_id=?",
        (episode_id,),
    ).fetchone()["count"])
    if existing_shots:
        raise RuntimeError(
            "production Artifact regression requires an episode without formal shots"
        )
    for shot in board.shots:
        _insert_storyboard_shot(
            conn,
            episode_id,
            screenplay,
            shot,
            str(episode_before["screenplay_artifact_id"] or ""),
        )
    conn.commit()

    contract_version = get_contract("storyboard").version
    board_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard_document",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T2",
        content=board.model_dump(mode="json"),
        parent_artifact_ids=[authority.artifact_id, artifact_id],
        contract_version=contract_version,
    ))
    full_gate = evidence_repository.create_evaluation(
        board_artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="storyboard_full_gate",
            evaluator_version=contract_version,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="score_only",
            runtime_blocking=False,
            retry_eligible=False,
            score=100,
            evidence={
                "production_artifact_replay": artifact_id,
                "outline_fingerprint": authority.fingerprint,
            },
        ),
    )
    revision = ensure_production_revision(
        episode_id=episode_id,
        kind="storyboard",
        input_fingerprint=authority.fingerprint,
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=board_artifact["id"],
        working_artifact_id=board_artifact["id"],
    )
    publish_result = publish_storyboard(
        episode_id=episode_id,
        revision_id=revision.id,
        artifact_id=board_artifact["id"],
        artifact_hash=board_artifact["content_hash"],
        evaluation_ids=[full_gate["id"]],
        shots_payload=board.model_dump(mode="json")["shots"],
        outline_json=authority.canonical_json,
        input_fingerprint=authority.fingerprint,
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
    )
    cost_basis = authoritative_storyboard_plan_cost(
        episode_id,
        conn=conn,
    )
    grant, _token = issue_video_completion_grant(
        episode_id=episode_id,
        project_id=str(episode_before["project_id"]),
        storyboard_artifact_id=publish_result["artifact_id"],
        shots_total=len(board.shots),
    )
    if (
        restarted.authoritative_duration_s != expected_duration_s
        or cost_basis["estimated_cost_cny"] != expected_cost_cny
        or grant.budget_cap_cny != expected_cost_cny
    ):
        raise RuntimeError("copied DB authority/cost/grant regression mismatch")

    source_stat_after = source_db.stat()
    source_unchanged = (
        source_stat_before.st_size == source_stat_after.st_size
        and source_stat_before.st_mtime_ns == source_stat_after.st_mtime_ns
    )
    if not source_unchanged:
        raise RuntimeError("source production DB metadata changed during regression")
    return {
        "source_db": str(source_db),
        "isolated_db_copy": str(copy_path),
        "source_db_unchanged": source_unchanged,
        "provider_calls_made": 0,
        "episode_id": episode_id,
        "screenplay_artifact_id": artifact_id,
        "planning_duration_s": planning_duration_s,
        "authoritative_duration_s": restarted.authoritative_duration_s,
        "outline_revision": restarted.revision,
        "outline_fingerprint": restarted.fingerprint,
        "shot_count": cost_basis["shot_count"],
        "video_plan_cost_cny": cost_basis["estimated_cost_cny"],
        "grant_budget_cap_cny": grant.budget_cap_cny,
        "story_event_coverage": replay_result["story_event_coverage"],
        "information_coverage": replay_result["information_coverage"],
        "source_coverage": {
            "covered": replay_result["source_coverage"]["covered"],
            "total": replay_result["source_coverage"]["total"],
        },
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--copy", type=Path)
    parser.add_argument("--expected-duration-s", type=int, required=True)
    parser.add_argument("--expected-cost-cny", type=float, required=True)
    parser.add_argument("--expected-story-events", type=int, required=True)
    parser.add_argument("--expected-source-segments", type=int, required=True)
    args = parser.parse_args()
    result = run_regression(
        source_db=args.db,
        artifact_id=args.artifact_id,
        project_id=args.project_id,
        copy_path=args.copy,
        expected_duration_s=args.expected_duration_s,
        expected_cost_cny=args.expected_cost_cny,
        expected_story_events=args.expected_story_events,
        expected_source_segments=args.expected_source_segments,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
