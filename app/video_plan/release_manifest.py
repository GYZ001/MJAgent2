"""Immutable storyboard release identity: cost quoting, canonical shot/plan
fingerprints, the release manifest, and binding a plan to that release.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale).
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn

from .models import EpisodeVideoGenerationPlan, ShotVideoGenerationPlan
from .primitives import _hash, _row_value
from .shot_row import _shot_model_from_row


def authoritative_storyboard_plan_cost(
    episode_id: str,
    *,
    conn=None,
) -> dict[str, Any]:
    """Quote one first pass from the exact released outline/shot version."""
    db = conn or get_conn()
    manifest = current_storyboard_release_manifest(episode_id, conn=db)
    rows = db.execute(
        "SELECT id,shot_no,duration_s FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if not rows:
        raise ValueError("当前分镜发布版没有正式 shots")
    duration_s = sum(int(row["duration_s"] or 0) for row in rows)
    authoritative_duration_s = int(
        manifest.get("authoritative_duration_s") or 0
    )
    if authoritative_duration_s and duration_s != authoritative_duration_s:
        raise ValueError(
            "视频计划 shots 时长与发布 outline authority 不一致"
        )
    from app.video_cost_model import initial_shot_generation_cost

    estimated_cost_cny = round(sum(
        initial_shot_generation_cost(float(row["duration_s"] or 0))
        for row in rows
    ), 6)
    return {
        "episode_id": episode_id,
        "published_storyboard_artifact_id": manifest[
            "published_storyboard_artifact_id"
        ],
        "release_qualification_hash": manifest["release_qualification_hash"],
        "outline_revision": int(manifest.get("outline_revision") or 0),
        "outline_fingerprint": str(
            manifest.get("outline_fingerprint") or ""
        ),
        "shot_count": len(rows),
        "authoritative_duration_s": (
            authoritative_duration_s or duration_s
        ),
        "estimated_cost_cny": estimated_cost_cny,
    }


def canonical_shot_contract_fingerprint(row: Any) -> str:
    """Hash the complete canonical Shot, not a partial/raw DB projection."""
    shot = _shot_model_from_row(row)
    return _hash(shot.model_dump(mode="json"))


def shot_video_execution_contract_fingerprint(
    plan: ShotVideoGenerationPlan,
) -> str:
    """Hash the reusable execution contract, excluding lifecycle identity/state.

    A local replan creates new database identities for the episode plan and all
    of its shot projections.  Work already queued for an unchanged shot remains
    safe only when the complete execution contract is semantically identical;
    target-shot changes (including any input fingerprint) therefore invalidate
    the old plan without relying on shot-number or reason-code exceptions.
    """
    payload = plan.model_dump(mode="json")
    for field in (
        "shot_plan_id",
        "episode_video_plan_id",
        "plan_revision",
        "actual_mode",
        "degraded_from_mode",
        "degraded_to_mode",
        "degraded_reason",
        "status",
    ):
        payload.pop(field, None)
    return _hash(payload)


def current_storyboard_release_manifest(
    episode_id: str,
    *,
    conn=None,
) -> dict[str, Any]:
    """Return the immutable storyboard release identity used by every plan."""
    db = conn or get_conn()
    episode = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError(f"分集不存在：{episode_id}")
    published_artifact_id = str(
        _row_value(episode, "published_storyboard_artifact_id", "") or ""
    )
    projected_artifact_id = str(
        _row_value(episode, "storyboard_artifact_id", "") or ""
    )
    certificate_id = str(
        _row_value(episode, "storyboard_completion_certificate_id", "") or ""
    )
    from app.production.screenplay_authority import (
        episode_requires_immutable_screenplay_authority,
        resolve_downstream_screenplay,
    )

    try:
        screenplay_context = resolve_downstream_screenplay(episode_id, conn=db)
    except ValueError:
        # A genuinely historical projection-less episode has no immutable
        # screenplay/review authority to downgrade and may retain the canonical
        # shots compatibility manifest. Once durable authority exists, however,
        # deleting or nulling the mutable projection must fail closed.
        durable_screenplay_release = any(
            str(_row_value(episode, field, "") or "")
            for field in (
                "published_screenplay_artifact_id",
                "screenplay_completion_certificate_id",
                "screenplay_production_revision_id",
                "narrative_review_artifact_id",
                "narrative_calibration_artifact_id",
            )
        )
        if (
            episode_requires_immutable_screenplay_authority(episode, conn=db)
            or durable_screenplay_release
        ):
            raise
        screenplay_context = None
    narrative_authority = bool(
        screenplay_context is not None
        and screenplay_context.narrative_authority_required
    )
    legacy_manifest_allowed = bool(
        screenplay_context is None
        or not screenplay_context.immutable_authority_required
    )
    if narrative_authority and (
        not published_artifact_id
        or published_artifact_id != projected_artifact_id
        or not certificate_id
    ):
        raise ValueError("叙事项目缺少当前分镜 Artifact 或完成凭证绑定")
    artifact_id = published_artifact_id or projected_artifact_id
    if not artifact_id:
        raise ValueError("当前分镜缺少已发布 Artifact")
    artifact = db.execute(
        """SELECT type,scope_type,scope_id,status,content_hash
           FROM artifacts WHERE id=?""",
        (artifact_id,),
    ).fetchone()
    artifact_valid = bool(
        artifact is not None
        and artifact["type"] in {"storyboard", "storyboard_document"}
        and artifact["scope_type"] == "episode"
        and artifact["scope_id"] == episode_id
        and artifact["status"]
        not in {"stale", "rejected", "superseded", "needs_revision"}
    )
    if not artifact_valid and not legacy_manifest_allowed:
        raise ValueError("当前分镜 Artifact 不是本集可发布权威版")
    if artifact_valid:
        artifact_hash = str(artifact["content_hash"] or "")
        if not artifact_hash:
            raise ValueError("当前分镜 Artifact 缺少内容哈希")
    else:
        # Explicit legacy boundary: old plan-null episodes may only have a
        # projection pointer.  Bind its exact canonical shots so even this
        # compatibility path cannot reuse a plan after content drift.
        rows = db.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        artifact_hash = _hash([
            _shot_model_from_row(row).model_dump(mode="json") for row in rows
        ])
    if narrative_authority:
        from app.production.certificate import (
            verify_current_storyboard_completion_authority,
        )
        from app.schemas import Storyboard

        rows = db.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        board = Storyboard(
            episode_no=int(_row_value(episode, "episode_no", 1) or 1),
            shots=[_shot_model_from_row(row) for row in rows],
        )
        verify_current_storyboard_completion_authority(
            episode=episode,
            current_storyboard_content=board.model_dump(mode="json"),
            conn=db,
        )
    from app.storyboard_authority import (
        OUTLINE_AUTHORITY_VERSION,
        resolve_storyboard_outline_authority,
    )

    outline_authority = None
    if (
        narrative_authority
        or str(_row_value(episode, "target_duration_authority", "") or "")
        == OUTLINE_AUTHORITY_VERSION
    ):
        outline_authority = resolve_storyboard_outline_authority(
            episode_id,
            conn=db,
            verify_shots=True,
        )
    qualification_hash = _hash({
        "manifest_version": "storyboard-release-manifest.v3",
        "episode_id": episode_id,
        "published_storyboard_artifact_id": artifact_id,
        "published_storyboard_artifact_hash": artifact_hash,
        "completion_certificate_id": certificate_id,
        "outline_revision": (
            outline_authority.revision if outline_authority is not None else 0
        ),
        "outline_fingerprint": (
            outline_authority.fingerprint if outline_authority is not None else ""
        ),
        "authoritative_duration_s": (
            outline_authority.authoritative_duration_s
            if outline_authority is not None else 0
        ),
    })
    return {
        "published_storyboard_artifact_id": artifact_id,
        "published_storyboard_artifact_hash": artifact_hash,
        "completion_certificate_id": certificate_id,
        # Deprecated compatibility fields. Optional QA must not alter release
        # identity or invalidate an already published generation plan.
        "narrative_review_artifact_id": "",
        "narrative_calibration_artifact_id": "",
        "outline_revision": (
            outline_authority.revision if outline_authority is not None else 0
        ),
        "outline_fingerprint": (
            outline_authority.fingerprint if outline_authority is not None else ""
        ),
        "outline_artifact_id": (
            outline_authority.artifact_id if outline_authority is not None else ""
        ),
        "authoritative_duration_s": (
            outline_authority.authoritative_duration_s
            if outline_authority is not None else 0
        ),
        "planning_duration_s": (
            outline_authority.planning_duration_s
            if outline_authority is not None else 0
        ),
        "release_qualification_hash": qualification_hash,
    }


def bind_plan_release_identity(
    plan: EpisodeVideoGenerationPlan,
    shot_rows: list[Any],
    manifest: dict[str, Any],
) -> EpisodeVideoGenerationPlan:
    """Construction-only binding; runtime validation never fills missing data."""
    plan.published_storyboard_artifact_id = manifest["published_storyboard_artifact_id"]
    plan.published_storyboard_artifact_hash = manifest["published_storyboard_artifact_hash"]
    plan.completion_certificate_id = manifest["completion_certificate_id"]
    plan.narrative_review_artifact_id = manifest["narrative_review_artifact_id"]
    plan.narrative_calibration_artifact_id = manifest[
        "narrative_calibration_artifact_id"
    ]
    plan.release_qualification_hash = manifest["release_qualification_hash"]
    by_id = {str(row["id"]): row for row in shot_rows}
    aliases: dict[str, str] = {}
    for row in shot_rows:
        database_id = str(row["id"])
        aliases[database_id] = database_id
        shot_uid = str(_row_value(row, "shot_uid", "") or "").strip()
        if shot_uid:
            aliases[shot_uid] = database_id
        try:
            contract = json.loads(_row_value(row, "shot_contract_json", "") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            contract = {}
        published_id = str(contract.get("shot_id") or "").strip()
        if published_id:
            aliases[published_id] = database_id
    for item in plan.shots:
        row = by_id.get(aliases.get(str(item.shot_id), str(item.shot_id)))
        if row is not None:
            item.input_revision_fingerprints["shot_contract"] = (
                canonical_shot_contract_fingerprint(row)
            )
    return plan
