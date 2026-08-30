"""Publish a validated episode video plan, and load a published plan back
from the database.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale).
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, new_id, now

from .models import EpisodeVideoGenerationPlan, ShotVideoGenerationPlan, VideoGenerationMode
from .primitives import _json, _row_value


def publish_plan(plan: EpisodeVideoGenerationPlan, *, conn=None) -> EpisodeVideoGenerationPlan:
    db = conn or get_conn()
    db.execute(
        """UPDATE episode_video_generation_plans
              SET status='superseded'
            WHERE episode_id=? AND status IN ('valid','draft')""",
        (plan.episode_id,),
    )
    db.execute(
        """INSERT INTO episode_video_generation_plans(
               id,episode_id,plan_revision,source_storyboard_revision_id,
               published_storyboard_artifact_id,published_storyboard_artifact_hash,
               completion_certificate_id,narrative_review_artifact_id,
               narrative_calibration_artifact_id,
               release_qualification_hash,
               capability_snapshot_id,status,planner_provider,planner_model,
               planner_prompt_fingerprint,blockers_json,estimated_latency_ms,
               estimated_cost,critical_path_latency_ms,safe_parallelism_ratio,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            plan.episode_video_plan_id, plan.episode_id, plan.plan_revision,
            plan.source_storyboard_revision_id,
            plan.published_storyboard_artifact_id,
            plan.published_storyboard_artifact_hash,
            plan.completion_certificate_id,
            plan.narrative_review_artifact_id,
            plan.narrative_calibration_artifact_id,
            plan.release_qualification_hash,
            plan.capability_snapshot_id,
            plan.status, plan.planner_provider, plan.planner_model,
            plan.planner_prompt_fingerprint, _json(plan.blockers),
            plan.estimated_latency_ms, plan.estimated_cost,
            plan.critical_path_latency_ms, plan.safe_parallelism_ratio,
            plan.created_at,
        ),
    )
    for item in plan.shots:
        item.episode_video_plan_id = plan.episode_video_plan_id
        item.plan_revision = plan.plan_revision
        item.shot_plan_id = item.shot_plan_id or new_id("svp")
        upstream_adopted_version_id = None
        if item.depends_on_shot_id:
            upstream = db.execute(
                "SELECT adopted_version_id FROM shots WHERE id=?",
                (item.depends_on_shot_id,),
            ).fetchone()
            upstream_adopted_version_id = (
                upstream["adopted_version_id"] if upstream else None
            )
            if upstream_adopted_version_id:
                item.input_revision_fingerprints[
                    "upstream_adopted_video_revision"
                ] = str(upstream_adopted_version_id)
        db.execute(
            """INSERT INTO shot_video_generation_plans(
                   id,episode_video_plan_id,shot_id,shot_no,planned_mode,actual_mode,
                   video_input_intent,depends_on_shot_id,relations_json,state_dependency,
                   motion_dependency,required_assets_json,reason_codes_json,confidence,
                   unknown_dimensions_json,fallback_order_json,max_attempts,max_cost,
                   timeout_s,estimated_latency_ms,estimated_cost,critical_path_group,
                   capability_snapshot_id,input_fingerprints_json,status,
                   degraded_from_mode,degraded_to_mode,degraded_reason,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.shot_plan_id, plan.episode_video_plan_id, item.shot_id, item.shot_no,
                item.mode.value, item.actual_mode.value if item.actual_mode else None,
                item.video_input_intent.value if item.video_input_intent else None,
                item.depends_on_shot_id, _json(item.relations.model_dump(mode="json")),
                item.state_dependency, item.motion_dependency,
                _json([asset.model_dump(mode="json") for asset in item.required_assets]),
                _json(item.reason_codes), item.confidence, _json(item.unknown_dimensions),
                _json([mode.value for mode in item.fallback_order]), item.max_attempts,
                item.max_cost, item.timeout_s, item.estimated_latency_ms,
                item.estimated_cost, item.critical_path_group,
                item.capability_snapshot_id, _json(item.input_revision_fingerprints),
                item.status, item.degraded_from_mode.value if item.degraded_from_mode else None,
                item.degraded_to_mode.value if item.degraded_to_mode else None,
                item.degraded_reason, now(), now(),
            ),
        )
        if item.depends_on_shot_id:
            db.execute(
                """INSERT INTO video_plan_dependencies(
                       id,episode_video_plan_id,shot_plan_id,shot_id,
                       depends_on_shot_id,dependency_kind,
                       upstream_adopted_version_id,resolved_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("vdep"), plan.episode_video_plan_id, item.shot_plan_id,
                    item.shot_id, item.depends_on_shot_id,
                    "adopted_video"
                    if item.mode == VideoGenerationMode.VIDEO_INPUT_MODE
                    else "adopted_tail_frame",
                    upstream_adopted_version_id,
                    now() if upstream_adopted_version_id else None,
                    now(),
                ),
            )
        db.execute(
            "UPDATE shots SET mode_plan=? WHERE id=?",
            (_json(item.model_dump(mode="json")), item.shot_id),
        )
    if conn is None:
        db.commit()
    return plan


def _shot_plan_from_row(row: Any, parent: Any) -> ShotVideoGenerationPlan:
    return ShotVideoGenerationPlan.model_validate({
        "shot_plan_id": row["id"],
        "episode_video_plan_id": row["episode_video_plan_id"],
        "plan_revision": parent["plan_revision"],
        "source_storyboard_revision_id": parent["source_storyboard_revision_id"],
        "shot_id": row["shot_id"],
        "published_shot_id": row["shot_id"],
        "shot_no": row["shot_no"],
        "mode": row["planned_mode"],
        "planned_mode": row["planned_mode"],
        "actual_mode": row["actual_mode"],
        "video_input_intent": row["video_input_intent"],
        "depends_on_shot_id": row["depends_on_shot_id"],
        "relations": json.loads(row["relations_json"] or "{}"),
        "state_dependency": row["state_dependency"],
        "motion_dependency": row["motion_dependency"],
        "required_assets": json.loads(row["required_assets_json"] or "[]"),
        "reason_codes": json.loads(row["reason_codes_json"] or "[]"),
        "confidence": row["confidence"],
        "unknown_dimensions": json.loads(row["unknown_dimensions_json"] or "[]"),
        "fallback_order": json.loads(row["fallback_order_json"] or "[]"),
        "max_attempts": row["max_attempts"],
        "max_cost": row["max_cost"],
        "timeout_s": row["timeout_s"],
        "estimated_latency_ms": row["estimated_latency_ms"],
        "estimated_cost": row["estimated_cost"],
        "critical_path_group": row["critical_path_group"],
        "capability_snapshot_id": row["capability_snapshot_id"],
        "input_revision_fingerprints": json.loads(row["input_fingerprints_json"] or "{}"),
        "status": row["status"],
        "degraded_from_mode": row["degraded_from_mode"],
        "degraded_to_mode": row["degraded_to_mode"],
        "degraded_reason": row["degraded_reason"],
    })


def _load_plan_parent(parent, *, db) -> EpisodeVideoGenerationPlan | None:
    if not parent:
        return None
    rows = db.execute(
        """SELECT * FROM shot_video_generation_plans
           WHERE episode_video_plan_id=? ORDER BY shot_no""",
        (parent["id"],),
    ).fetchall()
    return EpisodeVideoGenerationPlan(
        episode_video_plan_id=parent["id"],
        episode_id=parent["episode_id"],
        plan_revision=parent["plan_revision"],
        source_storyboard_revision_id=parent["source_storyboard_revision_id"],
        published_storyboard_artifact_id=_row_value(parent, "published_storyboard_artifact_id", ""),
        published_storyboard_artifact_hash=_row_value(parent, "published_storyboard_artifact_hash", ""),
        completion_certificate_id=_row_value(parent, "completion_certificate_id", ""),
        narrative_review_artifact_id=_row_value(parent, "narrative_review_artifact_id", ""),
        narrative_calibration_artifact_id=_row_value(parent, "narrative_calibration_artifact_id", ""),
        release_qualification_hash=_row_value(parent, "release_qualification_hash", ""),
        capability_snapshot_id=parent["capability_snapshot_id"],
        status=parent["status"],
        planner_provider=parent["planner_provider"] or "",
        planner_model=parent["planner_model"] or "",
        planner_prompt_fingerprint=parent["planner_prompt_fingerprint"] or "",
        blockers=json.loads(parent["blockers_json"] or "[]"),
        estimated_latency_ms=parent["estimated_latency_ms"],
        estimated_cost=parent["estimated_cost"],
        critical_path_latency_ms=parent["critical_path_latency_ms"],
        safe_parallelism_ratio=parent["safe_parallelism_ratio"],
        created_at=parent["created_at"],
        shots=[_shot_plan_from_row(row, parent) for row in rows],
    )


def load_plan_by_id(plan_id: str, *, conn=None) -> EpisodeVideoGenerationPlan | None:
    db = conn or get_conn()
    parent = db.execute(
        "SELECT * FROM episode_video_generation_plans WHERE id=?",
        (plan_id,),
    ).fetchone()
    return _load_plan_parent(parent, db=db)


def load_latest_plan(episode_id: str, *, conn=None) -> EpisodeVideoGenerationPlan | None:
    db = conn or get_conn()
    parent = db.execute(
        """SELECT * FROM episode_video_generation_plans
           WHERE episode_id=? AND status IN ('valid','blocked','stale')
           ORDER BY plan_revision DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    return _load_plan_parent(parent, db=db)
