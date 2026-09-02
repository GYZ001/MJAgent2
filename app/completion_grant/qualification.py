"""发布资格快照：把剧本/分镜/叙事复核/生成计划压成一份可比对的指纹。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
)

RELEASE_QUALIFICATION_VERSION = "video-completion-release-qualification.v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _content_fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _legacy_screenplay_projection_material(
    episode: Any,
    *,
    mode: str,
    projection_hash: str,
) -> dict[str, Any]:
    """Content-address every legacy fact without pretending it is certified."""
    from app.evidence import repository as evidence_repository

    keys = set(episode.keys())
    artifact_id = str(
        (episode["published_screenplay_artifact_id"] if "published_screenplay_artifact_id" in keys else "")
        or (episode["screenplay_artifact_id"] if "screenplay_artifact_id" in keys else "")
        or ""
    )
    binding: dict[str, Any] = {
        "artifact_id": artifact_id,
        "compatibility": "legacy_projection_only",
    }
    artifact = evidence_repository.get_artifact(artifact_id) if artifact_id else None
    if artifact is not None:
        try:
            current_hash = evidence_repository.content_hash(
                artifact.get("content"), artifact.get("file_path")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("legacy screenplay artifact cannot be content-addressed") from exc
        if not artifact.get("content_hash") or artifact.get("content_hash") != current_hash:
            raise ValueError("legacy screenplay artifact content hash drifted")
        binding.update({
            "content_hash": current_hash,
            "type": artifact.get("type"),
            "scope_type": artifact.get("scope_type"),
            "scope_id": artifact.get("scope_id"),
            "status": artifact.get("status"),
            "contract_version": artifact.get("contract_version"),
        })
    return {
        "mode": mode,
        "immutable_authority_required": False,
        "narrative_authority_required": False,
        "projection_hash": projection_hash,
        "artifact": binding,
    }


def _screenplay_release_material(episode_id: str, *, conn) -> dict[str, Any]:
    """Resolve screenplay authority without allowing a mutable downgrade.

    Historical projection-only episodes remain readable through an explicit
    compatibility contract.  The first durable production revision,
    certificate, or narrative review makes the immutable resolver mandatory.
    """
    from app.production.screenplay_authority import (
        episode_requires_immutable_screenplay_authority,
        resolve_current_screenplay_authority,
        resolve_downstream_screenplay,
    )

    episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    immutable_required = episode_requires_immutable_screenplay_authority(
        episode,
        conn=conn,
    )
    try:
        context = resolve_downstream_screenplay(episode_id, conn=conn)
    except ValueError:
        if immutable_required:
            raise
        raw = ""
        try:
            raw = str(episode["screenplay_json"] or "")
        except (KeyError, IndexError):
            pass
        return _legacy_screenplay_projection_material(
            episode,
            mode="legacy_plan_null_projection_absent" if not raw else "legacy_plan_null",
            projection_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
    if not context.immutable_authority_required:
        return _legacy_screenplay_projection_material(
            episode,
            mode="legacy_plan_null",
            projection_hash=_content_fingerprint(
                context.screenplay.model_dump(mode="json")
            ),
        )
    resolved = resolve_current_screenplay_authority(
        episode_id,
        conn=conn,
        require_narrative=context.narrative_authority_required,
    )
    revision_id = ""
    try:
        revision_id = str(episode["screenplay_production_revision_id"] or "")
    except (KeyError, IndexError):
        pass
    return {
        "mode": "immutable_narrative" if context.narrative_authority_required else "immutable",
        "immutable_authority_required": True,
        "narrative_authority_required": context.narrative_authority_required,
        "published_screenplay_artifact_id": resolved.artifact_id,
        "published_screenplay_artifact_hash": resolved.artifact_hash,
        "screenplay_completion_certificate_id": resolved.certificate_id,
        "screenplay_production_revision_id": revision_id,
        "screenplay_input_fingerprint": resolved.input_fingerprint,
    }


def _storyboard_release_material(
    episode_id: str,
    *,
    conn,
    legacy_plan_null: bool,
) -> dict[str, Any]:
    from app.evidence import repository as evidence_repository
    from app.video_plan import (
        canonical_shot_contract_fingerprint,
        current_storyboard_release_manifest,
    )

    try:
        manifest = current_storyboard_release_manifest(episode_id, conn=conn)
    except (TypeError, ValueError):
        if not legacy_plan_null:
            raise
        # Explicit historical compatibility: some pre-contract rows contain
        # nullable camera/scene fields and cannot instantiate today's Shot
        # model. Bind every persisted column instead of weakening validation
        # for modern releases.
        episode = conn.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        legacy_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no,id",
            (episode_id,),
        ).fetchall()
        artifact_id = str(
            (
                episode["published_storyboard_artifact_id"]
                if "published_storyboard_artifact_id" in episode.keys()
                else None
            )
            or episode["storyboard_artifact_id"]
            or ""
        )
        if not artifact_id:
            raise ValueError("legacy storyboard projection has no release pointer")
        raw_projection_hash = _content_fingerprint(
            [dict(row) for row in legacy_rows]
        )
        manifest = {
            "published_storyboard_artifact_id": artifact_id,
            "published_storyboard_artifact_hash": raw_projection_hash,
            "completion_certificate_id": str(
                episode["storyboard_completion_certificate_id"] or ""
            ) if "storyboard_completion_certificate_id" in episode.keys() else "",
            "narrative_review_artifact_id": str(
                episode["narrative_review_artifact_id"] or ""
            ) if "narrative_review_artifact_id" in episode.keys() else "",
        }
        manifest["release_qualification_hash"] = _content_fingerprint({
            "manifest_version": "storyboard-release-manifest.legacy-plan-null.v1",
            "episode_id": episode_id,
            **manifest,
        })
    artifact_id = manifest["published_storyboard_artifact_id"]
    artifact = evidence_repository.get_artifact(artifact_id)
    if artifact is None:
        artifact_binding: dict[str, Any] = {
            "compatibility": "legacy_projection_pointer",
            "artifact_id": artifact_id,
            "content_hash": manifest["published_storyboard_artifact_hash"],
        }
    else:
        try:
            current_hash = evidence_repository.content_hash(
                artifact.get("content"), artifact.get("file_path")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("current storyboard artifact cannot be content-addressed") from exc
        if (
            not artifact.get("content_hash")
            or artifact.get("content_hash") != current_hash
            or current_hash != manifest["published_storyboard_artifact_hash"]
        ):
            raise ValueError("current storyboard artifact content hash drifted")
        artifact_binding = {
            "artifact_id": artifact_id,
            "content_hash": current_hash,
            "type": artifact.get("type"),
            "scope_type": artifact.get("scope_type"),
            "scope_id": artifact.get("scope_id"),
            "status": artifact.get("status"),
            "contract_version": artifact.get("contract_version"),
        }
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no, id",
        (episode_id,),
    ).fetchall()
    shot_projection: list[dict[str, Any]] = []
    for row in rows:
        try:
            contract_hash = canonical_shot_contract_fingerprint(row)
            projection_mode = "canonical_shot_contract"
        except (TypeError, ValueError):
            if not legacy_plan_null:
                raise
            contract_hash = _content_fingerprint(dict(row))
            projection_mode = "legacy_complete_database_row"
        shot_projection.append({
            "database_shot_id": str(row["id"]),
            "shot_uid": str(row["shot_uid"] or "") if "shot_uid" in row.keys() else "",
            "shot_no": int(row["shot_no"]),
            "projection_mode": projection_mode,
            "canonical_contract_hash": contract_hash,
        })
    return {
        **manifest,
        "artifact": artifact_binding,
        "shots_authority_projection_hash": _content_fingerprint(shot_projection),
        "shots_authority_projection": shot_projection,
    }


def _narrative_review_material(
    episode_id: str,
    *,
    screenplay_material: dict[str, Any],
) -> dict[str, Any]:
    """Keep the legacy qualification slot explicitly score-only.

    Storyboard release authority is already verified by
    ``_storyboard_release_material``. Optional audience scoring is not an
    authored input and must not invalidate a paid-work grant when it changes.
    """
    return {
        "required": False,
        "verified": True,
        "evaluation_role": "score_only",
        "episode_id": episode_id,
        "narrative_project": bool(
            screenplay_material.get("narrative_authority_required")
        ),
    }


def _generation_plan_material(
    episode_id: str,
    *,
    conn,
    applicable: bool | None,
) -> dict[str, Any]:
    from app.video_plan import (
        capability_snapshot_by_id,
        load_latest_plan,
        shot_video_execution_contract_fingerprint,
        video_plan_provider_selection_is_current,
        verify_episode_plan_is_current,
    )

    if applicable is False:
        return {"applicable": False, "compatibility": "plan_pending_at_grant_issue"}
    plan = load_latest_plan(episode_id, conn=conn)
    if plan is None:
        if applicable:
            raise ValueError("current episode video generation plan is missing")
        return {"applicable": False, "compatibility": "plan_pending_at_grant_issue"}
    if plan.status != "valid" or not verify_episode_plan_is_current(
        plan,
        conn=conn,
        mark_stale=False,
    ) or not video_plan_provider_selection_is_current(plan, conn=conn):
        if applicable is None:
            return {
                "applicable": False,
                "compatibility": "plan_pending_at_grant_issue",
            }
        raise ValueError("current episode video generation plan is not valid")
    snapshot = capability_snapshot_by_id(plan.capability_snapshot_id, conn=conn)
    if snapshot is None:
        raise ValueError("video generation plan capability snapshot is missing")
    return {
        "applicable": True,
        "episode_video_plan_id": plan.episode_video_plan_id,
        "plan_revision": int(plan.plan_revision),
        "source_storyboard_revision_id": plan.source_storyboard_revision_id,
        "release_qualification_hash": plan.release_qualification_hash,
        "capability_snapshot_id": plan.capability_snapshot_id,
        "capability_snapshot_hash": _content_fingerprint(
            snapshot.model_dump(mode="json")
        ),
        "planner_provider": plan.planner_provider,
        "planner_model": plan.planner_model,
        "planner_prompt_fingerprint": plan.planner_prompt_fingerprint,
        "authoritative_shot_count": len(plan.shots),
        "shot_execution_contracts": [
            {
                "shot_id": shot.shot_id,
                "shot_plan_id": shot.shot_plan_id,
                "contract_hash": shot_video_execution_contract_fingerprint(shot),
            }
            for shot in plan.shots
        ],
    }


def current_video_completion_qualification(
    episode_id: str,
    *,
    generation_plan_applicable: bool | None = None,
    conn,
) -> tuple[dict[str, Any], str]:
    """Build the complete release contract rechecked before every paid stage."""
    db = conn
    episode = db.execute(
        "SELECT id,project_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if episode is None:
        raise ValueError(f"episode not found: {episode_id}")
    screenplay = _screenplay_release_material(episode_id, conn=db)
    storyboard = _storyboard_release_material(
        episode_id,
        conn=db,
        legacy_plan_null=str(screenplay.get("mode") or "").startswith("legacy_plan_null"),
    )
    material = {
        "qualification_version": RELEASE_QUALIFICATION_VERSION,
        "episode_id": episode_id,
        "project_id": str(episode["project_id"]),
        "screenplay_authority": screenplay,
        "storyboard_authority": storyboard,
        "narrative_review_authority": _narrative_review_material(
            episode_id,
            screenplay_material=screenplay,
        ),
        "generation_plan": _generation_plan_material(
            episode_id,
            conn=db,
            applicable=generation_plan_applicable,
        ),
    }
    return material, _content_fingerprint(material)
