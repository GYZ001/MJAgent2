"""Versioned three-mode video planning and deterministic execution contracts."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import socket
import subprocess
import time
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlparse

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.db import get_conn, get_setting, log_provider_call, new_id, now


class VideoGenerationMode(str, Enum):
    REFERENCE_IMAGE_MODE = "REFERENCE_IMAGE_MODE"
    FIRST_FRAME_MODE = "FIRST_FRAME_MODE"
    FIRST_LAST_FRAME_MODE = "FIRST_LAST_FRAME_MODE"
    VIDEO_INPUT_MODE = "VIDEO_INPUT_MODE"


class VideoInputIntent(str, Enum):
    CONTINUE_PREVIOUS_TAKE = "CONTINUE_PREVIOUS_TAKE"
    MOTION_REFERENCE = "MOTION_REFERENCE"
    CAMERA_REFERENCE = "CAMERA_REFERENCE"
    RHYTHM_REFERENCE = "RHYTHM_REFERENCE"
    AUDIO_REFERENCE = "AUDIO_REFERENCE"


class AssetSource(str, Enum):
    ASSET_REVISION = "ASSET_REVISION"
    STATIC_BOUNDARY_ASSET = "STATIC_BOUNDARY_ASSET"
    PREVIOUS_STATIC_TAIL = "PREVIOUS_STATIC_TAIL"
    PREVIOUS_ADOPTED_TAIL = "PREVIOUS_ADOPTED_TAIL"
    PREVIOUS_ADOPTED_VIDEO = "PREVIOUS_ADOPTED_VIDEO"


class PlanAssetRequirement(BaseModel):
    role: Literal[
        "identity_reference",
        "scene_reference",
        "prop_reference",
        "style_reference",
        "first_frame",
        "last_frame",
        "previous_adopted_video",
        "motion_reference_video",
        "camera_reference_video",
        "audio_reference_video",
    ]
    source: AssetSource
    asset_revision_id: str | None = None
    source_shot_id: str | None = None
    fingerprint: str | None = None


class ProviderVideoCapabilitySnapshot(BaseModel):
    id: str
    provider: str
    model: str
    region: str = ""
    gateway: str = ""
    api_version: str = ""
    supports_reference_image: bool = True
    supports_first_frame: bool = False
    supports_last_frame: bool = False
    supports_first_last_pair: bool = False
    supports_reference_video: bool = False
    supports_true_video_continuation: bool = False
    supports_return_last_frame: bool = False
    supports_data_url_by_media_type: dict[str, bool] = Field(default_factory=dict)
    requires_web_url_by_media_type: dict[str, bool] = Field(default_factory=dict)
    mutually_exclusive_input_roles: list[list[str]] = Field(default_factory=list)
    duration_limits: dict[str, Any] = Field(default_factory=dict)
    size_limits: dict[str, Any] = Field(default_factory=dict)
    format_limits: dict[str, Any] = Field(default_factory=dict)
    probe_time: float
    probe_task_id: str | None = None
    probe_result: str = "unverified"
    technical_success: bool = False
    semantic_continuation_success: bool = False


class ShotRelations(BaseModel):
    temporal: Literal["same_moment", "elapsed", "jump", "new_domain", "unknown"] = "unknown"
    spatial: Literal["same_space", "adjacent_space", "new_space", "unknown"] = "unknown"
    edit: Literal[
        "continuous_take",
        "match_cut",
        "angle_cut",
        "reaction_cut",
        "reverse_angle",
        "insert_cut",
        "montage",
        "scene_cut",
        "unknown",
    ] = "unknown"
    action: Literal[
        "continues_same_action",
        "starts_new_action",
        "shows_result",
        "observes_result",
        "no_action",
        "unknown",
    ] = "unknown"


class PlannerShotAnalysis(BaseModel):
    """AI-owned relational facts; executable mode and assets are compiler-owned."""

    shot_id: str
    relations: ShotRelations = Field(default_factory=ShotRelations)
    state_dependency: Literal[
        "none", "start_only", "start_and_end", "full_trajectory",
    ] = "none"
    motion_dependency: Literal[
        "none", "pose", "trajectory", "camera", "rhythm", "audio",
    ] = "none"
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    unknown_dimensions: list[str] = Field(default_factory=list)
    estimated_latency_ms: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0.0, ge=0.0)


SHOT_RELATION_ENUM_CONTRACT: dict[str, list[str]] = {
    "temporal": ["same_moment", "elapsed", "jump", "new_domain", "unknown"],
    "spatial": ["same_space", "adjacent_space", "new_space", "unknown"],
    "edit": [
        "continuous_take",
        "match_cut",
        "angle_cut",
        "reaction_cut",
        "reverse_angle",
        "insert_cut",
        "montage",
        "scene_cut",
        "unknown",
    ],
    "action": [
        "continues_same_action",
        "starts_new_action",
        "shows_result",
        "observes_result",
        "no_action",
        "unknown",
    ],
}


class ShotVideoGenerationPlan(BaseModel):
    shot_plan_id: str = ""
    episode_video_plan_id: str = ""
    plan_revision: int = 1
    source_storyboard_revision_id: str
    shot_id: str
    published_shot_id: str
    shot_no: int
    mode: VideoGenerationMode
    video_input_intent: VideoInputIntent | None = None
    depends_on_shot_id: str | None = None
    relations: ShotRelations = Field(default_factory=ShotRelations)
    state_dependency: Literal["none", "start_only", "start_and_end", "full_trajectory"] = "none"
    motion_dependency: Literal["none", "pose", "trajectory", "camera", "rhythm", "audio"] = "none"
    required_assets: list[PlanAssetRequirement] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    unknown_dimensions: list[str] = Field(default_factory=list)
    fallback_order: list[VideoGenerationMode] = Field(default_factory=list)
    max_attempts: int = Field(default=2, ge=1, le=8)
    max_cost: float = Field(default=0.0, ge=0.0)
    timeout_s: float = Field(default=7200.0, ge=30.0)
    estimated_latency_ms: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0.0, ge=0.0)
    critical_path_group: str | None = None
    capability_snapshot_id: str
    input_revision_fingerprints: dict[str, str] = Field(default_factory=dict)
    planned_mode: VideoGenerationMode | None = None
    actual_mode: VideoGenerationMode | None = None
    degraded_from_mode: VideoGenerationMode | None = None
    degraded_to_mode: VideoGenerationMode | None = None
    degraded_reason: str | None = None
    status: str = "planned"

    @model_validator(mode="after")
    def _sync_planned_mode(self) -> "ShotVideoGenerationPlan":
        self.planned_mode = self.mode
        return self


class EpisodeVideoGenerationPlan(BaseModel):
    episode_video_plan_id: str
    episode_id: str
    plan_revision: int
    source_storyboard_revision_id: str
    published_storyboard_artifact_id: str = ""
    published_storyboard_artifact_hash: str = ""
    completion_certificate_id: str = ""
    narrative_review_artifact_id: str = ""
    narrative_calibration_artifact_id: str = ""
    release_qualification_hash: str = ""
    capability_snapshot_id: str
    status: Literal["draft", "valid", "blocked", "superseded", "stale"] = "draft"
    planner_provider: str = ""
    planner_model: str = ""
    planner_prompt_fingerprint: str = ""
    shots: list[ShotVideoGenerationPlan] = Field(default_factory=list)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    estimated_latency_ms: int = 0
    estimated_cost: float = 0.0
    critical_path_latency_ms: int = 0
    safe_parallelism_ratio: float = 1.0
    created_at: float = Field(default_factory=time.time)


_RELATION_ALIASES: dict[str, dict[str, str]] = {
    "temporal": {
        "episode_start": "new_domain",
        "continuous": "same_moment",
        "time_skip_brief": "elapsed",
    },
    "spatial": {
        "establishing": "new_space",
        "same_scene_reposition": "same_space",
        "scene_change": "new_space",
        "same_scene_reverse_angle": "same_space",
        "same_scene": "same_space",
    },
    "edit": {
        "none": "unknown",
        "same_scene_cut": "angle_cut",
        "scene_change": "scene_cut",
    },
    "action": {
        "origin": "starts_new_action",
        "new_action_same_scene": "starts_new_action",
        "new_action_with_trajectory": "starts_new_action",
        "new_scene_action": "starts_new_action",
        "reaction_insert": "observes_result",
        "transformative_action_phase": "starts_new_action",
        "new_scene_action_with_pose_change": "starts_new_action",
        "action_phase_state_change": "starts_new_action",
    },
}


def normalize_ai_shot_plan_candidate(
    value: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Canonicalize redundant planner fields without changing its selected mode."""
    normalized = dict(value)
    changes: list[dict[str, Any]] = []
    relations = normalized.get("relations")
    if isinstance(relations, dict):
        relations = dict(relations)
        normalized["relations"] = relations
        for field, aliases in _RELATION_ALIASES.items():
            current = str(relations.get(field) or "")
            replacement = aliases.get(current)
            if replacement is None:
                continue
            relations[field] = replacement
            changes.append({
                "field": f"relations.{field}",
                "from": current,
                "to": replacement,
            })
        domain_changed = bool(
            relations.get("temporal") == "new_domain"
            or relations.get("spatial") == "new_space"
            or relations.get("edit") == "scene_cut"
        )
        if (
            domain_changed
            and normalized.get("mode")
            != VideoGenerationMode.REFERENCE_IMAGE_MODE.value
        ):
            previous_mode = normalized.get("mode")
            normalized.update({
                "mode": VideoGenerationMode.REFERENCE_IMAGE_MODE.value,
                "video_input_intent": None,
                "depends_on_shot_id": None,
                "required_assets": [],
                "state_dependency": "none",
                "motion_dependency": "none",
            })
            reason_codes = list(normalized.get("reason_codes") or [])
            if "SCENE_DOMAIN_CHANGED" not in reason_codes:
                reason_codes.append("SCENE_DOMAIN_CHANGED")
            normalized["reason_codes"] = reason_codes
            changes.append({
                "field": "mode",
                "from": previous_mode,
                "to": VideoGenerationMode.REFERENCE_IMAGE_MODE.value,
                "reason": "scene_domain_requires_recomposition",
            })
    if normalized.get("mode") == VideoGenerationMode.REFERENCE_IMAGE_MODE.value:
        assets = normalized.get("required_assets")
        if isinstance(assets, list):
            versioned_reference_roles = {
                "identity_reference",
                "scene_reference",
            }
            kept = [
                item
                for item in assets
                if (
                    isinstance(item, dict)
                    and item.get("role") in versioned_reference_roles
                    and item.get("source") == AssetSource.ASSET_REVISION.value
                )
            ]
            if kept != assets:
                normalized["required_assets"] = kept
                changes.append({
                    "field": "required_assets",
                    "reason": "generic_reference_resolved_at_execution",
                })
    if normalized.get("mode") in {
        VideoGenerationMode.FIRST_FRAME_MODE.value,
        VideoGenerationMode.FIRST_LAST_FRAME_MODE.value,
    }:
        assets = normalized.get("required_assets")
        first_frame = next((
            item for item in assets
            if isinstance(item, dict) and item.get("role") == "first_frame"
        ), None) if isinstance(assets, list) else None
        desired_dependency: str | None
        if (
            first_frame
            and first_frame.get("source") == AssetSource.PREVIOUS_ADOPTED_TAIL.value
            and first_frame.get("source_shot_id")
        ):
            desired_dependency = str(first_frame["source_shot_id"])
        elif (
            first_frame
            and first_frame.get("source") in {
                AssetSource.STATIC_BOUNDARY_ASSET.value,
                AssetSource.PREVIOUS_STATIC_TAIL.value,
            }
        ):
            desired_dependency = None
        else:
            desired_dependency = normalized.get("depends_on_shot_id")
        if normalized.get("depends_on_shot_id") != desired_dependency:
            changes.append({
                "field": "depends_on_shot_id",
                "from": normalized.get("depends_on_shot_id"),
                "to": desired_dependency,
                "reason": "derived_from_first_frame_source",
            })
            normalized["depends_on_shot_id"] = desired_dependency
    fallback_order = normalized.get("fallback_order")
    if fallback_order:
        normalized["fallback_order"] = []
        changes.append({
            "field": "fallback_order",
            "from": fallback_order,
            "to": [],
            "reason": "automatic_mode_fallback_disabled",
        })
    return normalized, changes


def _is_scene_entry(
    *,
    index: int,
    item: ShotVideoGenerationPlan,
    previous: ShotVideoGenerationPlan | None,
    scene_identity_by_shot_id: dict[str, str],
) -> bool:
    if index == 0 or previous is None:
        return True
    current_scene = scene_identity_by_shot_id.get(item.shot_id, "").strip()
    previous_scene = scene_identity_by_shot_id.get(previous.shot_id, "").strip()
    if current_scene and previous_scene:
        return current_scene != previous_scene
    return bool(
        item.relations.temporal == "new_domain"
        or item.relations.spatial == "new_space"
        or item.relations.edit == "scene_cut"
    )


def _scene_identity(row: Any) -> str:
    """Keep location and time separate in storage, but joint for cut continuity."""
    scene_name = str(_row_value(row, "scene_name", "") or "").strip()
    scene_time = str(_row_value(row, "scene_time", "") or "").strip()
    if not scene_name and not scene_time:
        return ""
    return _json([scene_name, scene_time])


def apply_scene_boundary_strategy(
    shots: list[ShotVideoGenerationPlan],
    *,
    scene_identity_by_shot_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Turn scene relations into a deterministic shared-boundary execution plan."""
    changes: list[dict[str, Any]] = []
    ordered = sorted(shots, key=lambda item: item.shot_no)
    previous: ShotVideoGenerationPlan | None = None
    scene_identities = scene_identity_by_shot_id or {}
    for index, item in enumerate(ordered):
        scene_entry = _is_scene_entry(
            index=index,
            item=item,
            previous=previous,
            scene_identity_by_shot_id=scene_identities,
        )
        if scene_entry:
            previous_mode = item.mode
            item.mode = VideoGenerationMode.REFERENCE_IMAGE_MODE
            item.planned_mode = item.mode
            item.video_input_intent = None
            item.depends_on_shot_id = None
            item.state_dependency = "none"
            item.motion_dependency = "none"
            item.required_assets = [
                asset for asset in item.required_assets
                if asset.role in {
                    "identity_reference",
                    "scene_reference",
                }
            ]
            reason_code = (
                "FIRST_SHOT_NO_PREDECESSOR"
                if index == 0 else "SCENE_ENTRY_REFERENCE_IMAGE"
            )
            if reason_code not in item.reason_codes:
                item.reason_codes.append(reason_code)
            if previous_mode != item.mode:
                changes.append({
                    "shot_id": item.shot_id,
                    "field": "mode",
                    "from": previous_mode.value,
                    "to": item.mode.value,
                    "reason": "scene_entry_requires_reference_image",
                })
            previous = item
            continue

        if previous is None:
            continue
        previous_mode = item.mode
        item.mode = VideoGenerationMode.FIRST_FRAME_MODE
        item.planned_mode = item.mode
        item.video_input_intent = None
        first_source = AssetSource.PREVIOUS_ADOPTED_TAIL
        item.depends_on_shot_id = previous.shot_id
        item.required_assets = [
            PlanAssetRequirement(
                role="first_frame",
                source=first_source,
                source_shot_id=previous.shot_id,
            ),
        ]
        reason_code = "IN_SCENE_PREVIOUS_VIDEO_TAIL"
        if reason_code not in item.reason_codes:
            item.reason_codes.append(reason_code)
        if previous_mode != item.mode:
            changes.append({
                "shot_id": item.shot_id,
                "field": "mode_and_boundary_source",
                "from": previous_mode.value,
                "to": item.mode.value,
                "first_frame_source": first_source.value,
                "reason": "shared_scene_boundary_strategy",
            })
        previous = item
    return changes


class VideoPlanValidationError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = issues
        super().__init__(json.dumps(issues, ensure_ascii=False))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key] if key in row.keys() else default
    except (AttributeError, KeyError, TypeError):
        return default


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


def _snapshot_from_row(row: Any) -> ProviderVideoCapabilitySnapshot:
    payload = json.loads(row["capabilities_json"] or "{}")
    return ProviderVideoCapabilitySnapshot.model_validate({
        **payload,
        "id": row["id"],
        "provider": row["provider"],
        "model": row["model"],
        "region": row["region"] or "",
        "gateway": row["gateway"] or "",
        "api_version": row["api_version"] or "",
        "probe_time": row["probe_time"],
        "probe_task_id": row["probe_task_id"],
        "probe_result": row["probe_result"],
        "technical_success": bool(row["technical_success"]),
        "semantic_continuation_success": bool(row["semantic_continuation_success"]),
    })


def minimax_h3_snapshot_from_probe(
    probe: dict[str, Any],
    *,
    provider: str,
    model: str,
) -> ProviderVideoCapabilitySnapshot:
    """Build an immutable capability snapshot from live H3 discovery data."""
    modes = probe.get("modes") if isinstance(probe.get("modes"), dict) else {}
    accelerations = (
        probe.get("accelerations")
        if isinstance(probe.get("accelerations"), dict)
        else {}
    )
    turbo_profiles = (
        probe.get("turbo_profiles")
        if isinstance(probe.get("turbo_profiles"), dict)
        else {}
    )
    vae_profiles = (
        probe.get("video_vae_profiles")
        if isinstance(probe.get("video_vae_profiles"), dict)
        else {}
    )
    api_version = str(probe.get("api_version") or "").strip()
    selected_acceleration = str(probe.get("acceleration") or "").strip()
    selected_profile = str(probe.get("turbo_profile") or "").strip()
    selected_vae = str(probe.get("video_vae") or "").strip()
    technical_success = bool(probe.get("ok"))
    turbo_step_values = [
        value
        for value in turbo_profiles.values()
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    return ProviderVideoCapabilitySnapshot(
        id=new_id("cap"),
        provider=provider,
        model=model,
        gateway="minimax_h3",
        api_version=api_version,
        supports_reference_image=bool(modes.get("reference_images")),
        supports_first_frame=bool(modes.get("keyframes")),
        supports_last_frame=bool(modes.get("keyframes")),
        supports_first_last_pair=bool(modes.get("keyframes")),
        supports_reference_video=bool(modes.get("reference_video")),
        supports_true_video_continuation=False,
        supports_return_last_frame=False,
        supports_data_url_by_media_type={"image": True, "video": False},
        requires_web_url_by_media_type={"image": False, "video": True},
        mutually_exclusive_input_roles=[
            ["reference_image", "first_frame"],
            ["reference_image", "last_frame"],
            ["reference_image", "reference_video"],
            ["first_frame", "reference_video"],
            ["last_frame", "reference_video"],
        ],
        duration_limits={"min_s": 0.2, "max_s": 15},
        size_limits={"min": 32, "max": 4096, "multiple": 32},
        format_limits={
            "capability_source": "live_health",
            "base_url": str(probe.get("base_url") or "").rstrip("/"),
            "output": "mp4",
            "video_codec": "h264",
            "fps": 24,
            "audio": "stereo",
            "reference_images_max": 9,
            "reference_videos_max": 3,
            "accelerations": [
                name for name, ready in accelerations.items() if ready
            ],
            "default_acceleration": selected_acceleration,
            "turbo_profiles": turbo_profiles,
            "default_turbo_profile": selected_profile or None,
            "turbo_steps": (
                {
                    "min": min(turbo_step_values),
                    "max": max(turbo_step_values),
                    "default": probe.get("steps"),
                }
                if turbo_step_values
                else {}
            ),
            "video_vae_profiles": [
                name for name, ready in vae_profiles.items() if ready
            ],
            "default_video_vae": selected_vae,
            "te_speed_available": bool(probe.get("te_speed_available")),
        },
        probe_time=now(),
        probe_result=(
            f"live_health:{api_version}:{selected_acceleration}:"
            f"{selected_profile or 'default'}:{selected_vae}"
        ),
        technical_success=technical_success,
        semantic_continuation_success=False,
    )


def record_minimax_h3_probe_snapshot(
    probe: dict[str, Any],
    *,
    provider: str,
    model: str,
    conn=None,
) -> ProviderVideoCapabilitySnapshot:
    """Persist live discovery only when the executable capability contract changed."""
    db = conn or get_conn()
    candidate = minimax_h3_snapshot_from_probe(
        probe,
        provider=provider,
        model=model,
    )
    row = db.execute(
        """SELECT * FROM provider_video_capability_snapshots
           WHERE provider=? AND model=?
           ORDER BY probe_time DESC, created_at DESC LIMIT 1""",
        (provider, model),
    ).fetchone()
    if row:
        saved = _snapshot_from_row(row)
        excluded = {
            "id", "probe_time", "probe_task_id", "probe_result",
        }
        saved_contract = saved.model_dump(
            mode="json",
            exclude=excluded,
        )
        candidate_contract = candidate.model_dump(
            mode="json",
            exclude=excluded,
        )
        if saved_contract == candidate_contract:
            return saved
    return save_capability_snapshot(candidate, conn=conn)


def failed_minimax_h3_snapshot(
    *,
    provider: str,
    model: str,
    error: Exception,
    connection=None,
) -> ProviderVideoCapabilitySnapshot:
    from app import minimax_h3

    conn = connection or minimax_h3.default_connection()
    return ProviderVideoCapabilitySnapshot(
        id=new_id("cap"),
        provider=provider,
        model=model,
        gateway="minimax_h3",
        supports_reference_image=False,
        supports_first_frame=False,
        supports_last_frame=False,
        supports_first_last_pair=False,
        supports_reference_video=False,
        supports_true_video_continuation=False,
        supports_return_last_frame=False,
        supports_data_url_by_media_type={"image": True, "video": False},
        requires_web_url_by_media_type={"image": False, "video": True},
        format_limits={
            "capability_source": "live_health_error",
            "base_url": conn.base_url,
            "default_acceleration": conn.acceleration,
            "default_turbo_profile": (
                conn.turbo_profile if conn.acceleration == "turbo" else None
            ),
            "default_video_vae": conn.video_vae,
        },
        probe_time=now(),
        probe_result=f"live_health_failed:{type(error).__name__}:{error}"[:500],
        technical_success=False,
        semantic_continuation_success=False,
    )


def minimax_h3_snapshot_matches_runtime(
    snapshot: ProviderVideoCapabilitySnapshot,
    connection=None,
) -> bool:
    from app import minimax_h3

    conn = connection or minimax_h3.default_connection()
    limits = snapshot.format_limits
    source = str(limits.get("capability_source") or "")
    same_runtime = bool(
        str(limits.get("base_url") or "").rstrip("/") == conn.base_url
        and limits.get("default_acceleration") == conn.acceleration
        and limits.get("default_turbo_profile")
        == (conn.turbo_profile if conn.acceleration == "turbo" else None)
        and limits.get("default_video_vae") == conn.video_vae
    )
    if not same_runtime:
        return False
    if source == "live_health":
        return True
    if source == "live_health_error":
        return now() - float(snapshot.probe_time or 0) < 30
    return False


def current_capability_snapshot(
    *,
    provider: str | None = None,
    model: str | None = None,
    conn=None,
) -> ProviderVideoCapabilitySnapshot:
    """Return the latest measured snapshot for the selected provider/model."""
    from app import hiagent, video_providers

    db = conn or get_conn()
    resolved_provider = provider or hiagent.active_provider("video")
    resolved_model = model or hiagent.active_model("video", resolved_provider)
    row = db.execute(
        """SELECT * FROM provider_video_capability_snapshots
           WHERE provider=? AND model=?
           ORDER BY probe_time DESC, created_at DESC LIMIT 1""",
        (resolved_provider, resolved_model),
    ).fetchone()
    adapter = video_providers.resolve(resolved_provider)
    if row:
        saved = _snapshot_from_row(row)
        if adapter.capability_snapshot_is_current(saved):
            return saved

    snapshot = adapter.capability_snapshot(
        provider=resolved_provider,
        model=resolved_model,
    )
    save_capability_snapshot(snapshot, conn=db)
    if conn is None:
        db.commit()
    return snapshot


def capability_snapshot_by_id(
    snapshot_id: str,
    *,
    conn=None,
) -> ProviderVideoCapabilitySnapshot | None:
    db = conn or get_conn()
    row = db.execute(
        "SELECT * FROM provider_video_capability_snapshots WHERE id=?",
        (snapshot_id,),
    ).fetchone()
    return _snapshot_from_row(row) if row else None


def video_plan_provider_selection_is_current(
    plan: EpisodeVideoGenerationPlan,
    *,
    conn=None,
) -> bool:
    """Return whether the plan is executable by the provider selected now.

    Storyboard/release validity and provider selection are separate authorities:
    a plan may remain content-current while the operator switches the active
    video provider.  Grant issue and Supervisor preflight use this cheap,
    read-only comparison so they can rebind before any payable work instead of
    discovering the drift independently in every media worker.
    """
    from app import hiagent

    snapshot = capability_snapshot_by_id(plan.capability_snapshot_id, conn=conn)
    if snapshot is None:
        return False
    active_provider = hiagent.active_provider("video")
    active_model = hiagent.active_model("video", active_provider)
    if snapshot.provider != active_provider or snapshot.model != active_model:
        return False
    latest_row = (conn or get_conn()).execute(
        """SELECT * FROM provider_video_capability_snapshots
           WHERE provider=? AND model=?
           ORDER BY probe_time DESC,created_at DESC LIMIT 1""",
        (active_provider, active_model),
    ).fetchone()
    if latest_row is None:
        return False
    latest = _snapshot_from_row(latest_row)
    return bool(
        latest.technical_success
        and all(
            capability_allows(latest, item.mode, item.video_input_intent)
            for item in plan.shots
        )
    )


def save_capability_snapshot(
    snapshot: ProviderVideoCapabilitySnapshot,
    *,
    conn=None,
) -> ProviderVideoCapabilitySnapshot:
    db = conn or get_conn()
    data = snapshot.model_dump(mode="json")
    capabilities = {
        key: value
        for key, value in data.items()
        if key not in {
            "id", "provider", "model", "region", "gateway", "api_version",
            "probe_time", "probe_task_id", "probe_result", "technical_success",
            "semantic_continuation_success",
        }
    }
    db.execute(
        """INSERT INTO provider_video_capability_snapshots(
               id,provider,model,region,gateway,api_version,capabilities_json,
               probe_time,probe_task_id,probe_result,technical_success,
               semantic_continuation_success,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            snapshot.id, snapshot.provider, snapshot.model, snapshot.region,
            snapshot.gateway, snapshot.api_version, _json(capabilities),
            snapshot.probe_time, snapshot.probe_task_id, snapshot.probe_result,
            int(snapshot.technical_success),
            int(snapshot.semantic_continuation_success), now(),
        ),
    )
    if conn is None:
        db.commit()
    return snapshot


def capability_allows(
    snapshot: ProviderVideoCapabilitySnapshot,
    mode: VideoGenerationMode,
    intent: VideoInputIntent | None = None,
) -> bool:
    if mode == VideoGenerationMode.REFERENCE_IMAGE_MODE:
        return snapshot.supports_reference_image
    if mode == VideoGenerationMode.FIRST_FRAME_MODE:
        return snapshot.supports_first_frame
    if mode == VideoGenerationMode.FIRST_LAST_FRAME_MODE:
        return bool(
            snapshot.supports_first_frame
            and snapshot.supports_last_frame
            and snapshot.supports_first_last_pair
        )
    if mode == VideoGenerationMode.VIDEO_INPUT_MODE:
        if not snapshot.supports_reference_video:
            return False
        if intent == VideoInputIntent.CONTINUE_PREVIOUS_TAKE:
            return bool(
                snapshot.supports_true_video_continuation
                and snapshot.semantic_continuation_success
            )
        return intent is not None
    return False


def validate_episode_plan(
    plan: EpisodeVideoGenerationPlan,
    shot_rows: list[Any],
    snapshot: ProviderVideoCapabilitySnapshot,
    *,
    release_manifest: dict[str, Any] | None = None,
) -> EpisodeVideoGenerationPlan:
    issues: list[dict[str, Any]] = []
    if release_manifest is not None:
        release_fields = (
            "published_storyboard_artifact_id",
            "published_storyboard_artifact_hash",
            "completion_certificate_id",
            "release_qualification_hash",
        )
        for field in release_fields:
            if getattr(plan, field, "") != release_manifest[field]:
                issues.append({
                    "code": "STORYBOARD_RELEASE_MANIFEST_STALE",
                    "field": field,
                    "stored": getattr(plan, field, ""),
                    "current": release_manifest[field],
                })
        if plan.source_storyboard_revision_id != release_manifest[
            "published_storyboard_artifact_id"
        ]:
            issues.append({
                "code": "STORYBOARD_RELEASE_IDENTITY_MISMATCH",
                "stored": plan.source_storyboard_revision_id,
                "current": release_manifest["published_storyboard_artifact_id"],
            })
    by_id = {str(row["id"]): row for row in shot_rows}
    if release_manifest is not None:
        authoritative_duration_s = int(
            release_manifest.get("authoritative_duration_s") or 0
        )
        projected_duration_s = sum(
            int(row["duration_s"] or 0) for row in shot_rows
        )
        if (
            authoritative_duration_s
            and projected_duration_s != authoritative_duration_s
        ):
            issues.append({
                "code": "OUTLINE_DURATION_AUTHORITY_STALE",
                "stored": projected_duration_s,
                "current": authoritative_duration_s,
            })
    aliases: dict[str, str] = {}
    for row in shot_rows:
        db_id = str(row["id"])
        aliases[db_id] = db_id
        for key in ("shot_uid",):
            value = str(_row_value(row, key, "") or "").strip()
            if value:
                aliases[value] = db_id
        try:
            contract = json.loads(_row_value(row, "shot_contract_json", "") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            contract = {}
        published_id = str(contract.get("shot_id") or "").strip()
        if published_id:
            aliases[published_id] = db_id

    normalized: list[ShotVideoGenerationPlan] = []
    seen: set[str] = set()
    for item in plan.shots:
        resolved = aliases.get(item.shot_id) or aliases.get(item.published_shot_id)
        if not resolved or resolved not in by_id:
            issues.append({
                "code": "UNKNOWN_SHOT_ID",
                "shot_id": item.shot_id,
                "required_owner": "storyboard",
            })
            continue
        if resolved in seen:
            issues.append({"code": "DUPLICATE_SHOT_PLAN", "shot_id": resolved})
            continue
        item.shot_id = resolved
        item.shot_no = int(by_id[resolved]["shot_no"])
        item.published_shot_id = (
            str(item.published_shot_id or "").strip()
            or str(_row_value(by_id[resolved], "shot_uid", "") or "").strip()
            or resolved
        )
        if item.depends_on_shot_id:
            dependency = aliases.get(item.depends_on_shot_id)
            if not dependency:
                issues.append({
                    "code": "UNKNOWN_DEPENDENCY",
                    "shot_id": resolved,
                    "depends_on_shot_id": item.depends_on_shot_id,
                })
            else:
                item.depends_on_shot_id = dependency
        for asset in item.required_assets:
            if not asset.source_shot_id:
                continue
            source_shot = aliases.get(asset.source_shot_id)
            if not source_shot:
                issues.append({
                    "code": "UNKNOWN_ASSET_SOURCE_SHOT",
                    "shot_id": resolved,
                    "source_shot_id": asset.source_shot_id,
                })
            else:
                asset.source_shot_id = source_shot
        seen.add(resolved)
        normalized.append(item)

    expected = set(by_id)
    if seen != expected:
        issues.append({
            "code": "SHOT_COVERAGE_INCOMPLETE",
            "missing_shot_ids": sorted(expected - seen),
            "extra_shot_ids": sorted(seen - expected),
        })
    normalized.sort(key=lambda item: item.shot_no)
    plan.shots = normalized
    if normalized and normalized[0].mode != VideoGenerationMode.REFERENCE_IMAGE_MODE:
        issues.append({"code": "FIRST_SHOT_NO_PREDECESSOR", "shot_id": normalized[0].shot_id})
    if normalized and normalized[0].depends_on_shot_id:
        issues.append({"code": "FIRST_SHOT_HAS_DEPENDENCY", "shot_id": normalized[0].shot_id})

    previous_item: ShotVideoGenerationPlan | None = None
    scene_identity_by_shot_id = {
        shot_id: _scene_identity(row)
        for shot_id, row in by_id.items()
    }
    for index, item in enumerate(normalized):
        scene_entry = _is_scene_entry(
            index=index,
            item=item,
            previous=previous_item,
            scene_identity_by_shot_id=scene_identity_by_shot_id,
        )
        if scene_entry:
            if item.mode != VideoGenerationMode.REFERENCE_IMAGE_MODE:
                issues.append({
                    "code": "SCENE_ENTRY_MODE_MISMATCH",
                    "shot_id": item.shot_id,
                    "expected_mode": VideoGenerationMode.REFERENCE_IMAGE_MODE.value,
                })
            previous_item = item
            continue
        if item.mode != VideoGenerationMode.FIRST_FRAME_MODE:
            issues.append({
                "code": "IN_SCENE_MODE_MISMATCH",
                "shot_id": item.shot_id,
                "expected_mode": VideoGenerationMode.FIRST_FRAME_MODE.value,
            })
            previous_item = item
            continue
        first = next(
            (asset for asset in item.required_assets if asset.role == "first_frame"),
            None,
        )
        expected_source = AssetSource.PREVIOUS_ADOPTED_TAIL
        if first is None or first.source != expected_source:
            issues.append({
                "code": "SCENE_BOUNDARY_SOURCE_MISMATCH",
                "shot_id": item.shot_id,
                "expected_source": expected_source.value,
            })
        if (
            first is not None
            and previous_item is not None
            and first.source_shot_id != previous_item.shot_id
        ):
            issues.append({
                "code": "SCENE_BOUNDARY_PREVIOUS_SHOT_MISMATCH",
                "shot_id": item.shot_id,
                "expected_source_shot_id": previous_item.shot_id,
            })
        expected_dependency = previous_item.shot_id if previous_item is not None else None
        if item.depends_on_shot_id != expected_dependency:
            issues.append({
                "code": "SCENE_VIDEO_DEPENDENCY_MISMATCH",
                "shot_id": item.shot_id,
                "expected_depends_on_shot_id": expected_dependency,
            })
        previous_item = item

    graph: dict[str, str | None] = {}
    for item in normalized:
        graph[item.shot_id] = item.depends_on_shot_id
        row = by_id[item.shot_id]
        if item.source_storyboard_revision_id != plan.source_storyboard_revision_id:
            issues.append({"code": "STORYBOARD_REVISION_MISMATCH", "shot_id": item.shot_id})
        if item.capability_snapshot_id != snapshot.id:
            issues.append({"code": "CAPABILITY_SNAPSHOT_MISMATCH", "shot_id": item.shot_id})
        if not capability_allows(snapshot, item.mode, item.video_input_intent):
            issues.append({
                "code": "PROVIDER_CAPABILITY_UNVERIFIED",
                "shot_id": item.shot_id,
                "mode": item.mode.value,
                "intent": item.video_input_intent.value if item.video_input_intent else None,
                "required_owner": "provider_capability",
            })
        try:
            confidence_floor = float(
                get_setting("video_plan_confidence_floor") or 0.55
            )
        except (TypeError, ValueError):
            confidence_floor = 0.55
        if item.confidence < max(0.0, min(1.0, confidence_floor)):
            issues.append({
                "code": "MODE_PLAN_CONFIDENCE_TOO_LOW",
                "shot_id": item.shot_id,
                "confidence": item.confidence,
                "threshold": confidence_floor,
            })
        if item.unknown_dimensions and (
            get_setting("video_plan_allow_unknown_dimensions") or "false"
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            issues.append({
                "code": "MODE_PLAN_RELATION_UNKNOWN",
                "shot_id": item.shot_id,
                "unknown_dimensions": item.unknown_dimensions,
            })
        if item.depends_on_shot_id:
            dep_row = by_id.get(item.depends_on_shot_id)
            if not dep_row or int(dep_row["shot_no"]) >= int(row["shot_no"]):
                issues.append({
                    "code": "DEPENDENCY_NOT_UPSTREAM",
                    "shot_id": item.shot_id,
                    "depends_on_shot_id": item.depends_on_shot_id,
                })
        roles = [asset.role for asset in item.required_assets]
        if item.mode == VideoGenerationMode.REFERENCE_IMAGE_MODE:
            if item.video_input_intent is not None or item.depends_on_shot_id:
                issues.append({"code": "REFERENCE_MODE_ROLE_CONFLICT", "shot_id": item.shot_id})
            if any(role in {"first_frame", "last_frame"} or role.endswith("_video") for role in roles):
                issues.append({"code": "REFERENCE_MODE_ROLE_CONFLICT", "shot_id": item.shot_id})
            if any(role not in {"identity_reference", "scene_reference"} for role in roles):
                issues.append({"code": "REFERENCE_LIBRARY_ROLE_INVALID", "shot_id": item.shot_id})
            if any(
                asset.source == AssetSource.ASSET_REVISION
                and not asset.asset_revision_id
                for asset in item.required_assets
            ):
                issues.append({"code": "REFERENCE_ASSET_REVISION_MISSING", "shot_id": item.shot_id})
        elif item.mode == VideoGenerationMode.FIRST_FRAME_MODE:
            if item.video_input_intent is not None:
                issues.append({"code": "FIRST_FRAME_HAS_VIDEO_INTENT", "shot_id": item.shot_id})
            if roles != ["first_frame"]:
                issues.append({"code": "FIRST_FRAME_ROLE_CONFLICT", "shot_id": item.shot_id})
            first = item.required_assets[0] if len(item.required_assets) == 1 else None
            if first and first.source != AssetSource.PREVIOUS_ADOPTED_TAIL:
                issues.append({"code": "FIRST_FRAME_SOURCE_INVALID", "shot_id": item.shot_id})
            if not item.depends_on_shot_id:
                issues.append({"code": "FIRST_FRAME_DEPENDENCY_MISSING", "shot_id": item.shot_id})
            if (
                first and first.source_shot_id
                and first.source_shot_id != item.depends_on_shot_id
            ):
                issues.append({"code": "FIRST_FRAME_SOURCE_SHOT_MISMATCH", "shot_id": item.shot_id})
        elif item.mode == VideoGenerationMode.FIRST_LAST_FRAME_MODE:
            if item.video_input_intent is not None:
                issues.append({"code": "FIRST_LAST_HAS_VIDEO_INTENT", "shot_id": item.shot_id})
            if roles.count("first_frame") != 1 or roles.count("last_frame") != 1:
                issues.append({"code": "FIRST_LAST_FRAME_MISSING", "shot_id": item.shot_id})
            if any("reference" in role or role.endswith("_video") for role in roles):
                issues.append({"code": "FIRST_LAST_ROLE_CONFLICT", "shot_id": item.shot_id})
            first = next((asset for asset in item.required_assets if asset.role == "first_frame"), None)
            last = next((asset for asset in item.required_assets if asset.role == "last_frame"), None)
            if first and first.source not in {
                AssetSource.STATIC_BOUNDARY_ASSET,
                AssetSource.PREVIOUS_STATIC_TAIL,
                AssetSource.PREVIOUS_ADOPTED_TAIL,
            }:
                issues.append({"code": "FIRST_FRAME_SOURCE_INVALID", "shot_id": item.shot_id})
            if last and last.source != AssetSource.STATIC_BOUNDARY_ASSET:
                issues.append({"code": "LAST_FRAME_SOURCE_INVALID", "shot_id": item.shot_id})
            needs_upstream = bool(first and first.source == AssetSource.PREVIOUS_ADOPTED_TAIL)
            if needs_upstream != bool(item.depends_on_shot_id):
                issues.append({"code": "FIRST_FRAME_DEPENDENCY_MISMATCH", "shot_id": item.shot_id})
            if (
                first and first.source_shot_id
                and first.source == AssetSource.PREVIOUS_ADOPTED_TAIL
                and first.source_shot_id != item.depends_on_shot_id
            ):
                issues.append({"code": "FIRST_FRAME_SOURCE_SHOT_MISMATCH", "shot_id": item.shot_id})
            if (
                first
                and first.source == AssetSource.PREVIOUS_STATIC_TAIL
                and not first.source_shot_id
            ):
                issues.append({"code": "STATIC_TAIL_SOURCE_SHOT_MISSING", "shot_id": item.shot_id})
            if (
                first
                and first.source == AssetSource.PREVIOUS_STATIC_TAIL
                and first.source_shot_id
            ):
                source_row = by_id.get(first.source_shot_id)
                if (
                    source_row is None
                    or int(source_row["shot_no"]) != int(row["shot_no"]) - 1
                ):
                    issues.append({
                        "code": "STATIC_TAIL_SOURCE_NOT_PREVIOUS_SHOT",
                        "shot_id": item.shot_id,
                        "source_shot_id": first.source_shot_id,
                    })
        elif item.mode == VideoGenerationMode.VIDEO_INPUT_MODE:
            if item.video_input_intent is None or not item.depends_on_shot_id:
                issues.append({"code": "VIDEO_INPUT_CONTRACT_INCOMPLETE", "shot_id": item.shot_id})
            if roles != ["previous_adopted_video"]:
                issues.append({"code": "VIDEO_INPUT_ROLE_CONFLICT", "shot_id": item.shot_id})
            video_asset = item.required_assets[0] if len(item.required_assets) == 1 else None
            if (
                video_asset
                and video_asset.source != AssetSource.PREVIOUS_ADOPTED_VIDEO
            ):
                issues.append({"code": "VIDEO_INPUT_SOURCE_INVALID", "shot_id": item.shot_id})
            if (
                video_asset and video_asset.source_shot_id
                and video_asset.source_shot_id != item.depends_on_shot_id
            ):
                issues.append({"code": "VIDEO_INPUT_SOURCE_SHOT_MISMATCH", "shot_id": item.shot_id})
        if item.fallback_order:
            issues.append({
                "code": "AUTOMATIC_MODE_FALLBACK_DISABLED",
                "shot_id": item.shot_id,
            })
        expected_fp = canonical_shot_contract_fingerprint(row)
        stored_fp = item.input_revision_fingerprints.get("shot_contract")
        if not stored_fp or stored_fp != expected_fp:
            issues.append({
                "code": "SHOT_CONTRACT_FINGERPRINT_STALE",
                "shot_id": item.shot_id,
                "stored": stored_fp,
                "current": expected_fp,
            })

    for start in graph:
        cursor = start
        path: set[str] = set()
        while cursor:
            if cursor in path:
                issues.append({"code": "DEPENDENCY_CYCLE", "shot_id": start})
                break
            path.add(cursor)
            cursor = graph.get(cursor)

    if issues:
        raise VideoPlanValidationError(issues)

    from app.video_cost_model import initial_shot_generation_cost

    for item in normalized:
        duration_s = float(by_id[item.shot_id]["duration_s"] or 5)
        authoritative_cost = initial_shot_generation_cost(duration_s)
        item.estimated_cost = authoritative_cost
        item.max_cost = max(float(item.max_cost or 0), authoritative_cost)

    from app import video_providers

    adapter = video_providers.resolve(snapshot.provider)
    serial_provider = adapter.serial_generation
    if serial_provider:
        for item in normalized:
            duration_s = float(by_id[item.shot_id]["duration_s"] or 5)
            item.estimated_latency_ms = 1000 * adapter.estimated_generation_seconds(
                item.mode.value,
                duration_s,
            )
            item.timeout_s = float(adapter.generation_timeout_seconds(
                item.mode.value,
                duration_s,
            ))

    depths: dict[str, int] = {}
    latency_paths: dict[str, int] = {}
    for item in normalized:
        parent = item.depends_on_shot_id
        depths[item.shot_id] = (depths.get(parent, -1) + 1) if parent else 0
        latency_paths[item.shot_id] = (
            latency_paths.get(parent, 0) + item.estimated_latency_ms
            if parent else item.estimated_latency_ms
        )
        item.critical_path_group = f"depth-{depths[item.shot_id]}"
    plan.estimated_latency_ms = sum(item.estimated_latency_ms for item in normalized)
    plan.estimated_cost = round(sum(item.estimated_cost for item in normalized), 4)
    plan.critical_path_latency_ms = (
        plan.estimated_latency_ms
        if serial_provider
        else max(latency_paths.values(), default=0)
    )
    plan.safe_parallelism_ratio = round(
        sum(1 for item in normalized if not item.depends_on_shot_id) / max(1, len(normalized)),
        4,
    )
    plan.status = "valid"
    return plan


def _shot_planner_payload(row: Any) -> dict[str, Any]:
    try:
        contract = json.loads(_row_value(row, "shot_contract_json", "") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        contract = {}
    return {
        "shot_id": str(contract.get("shot_id") or _row_value(row, "shot_uid", "") or row["id"]),
        "database_shot_id": row["id"],
        "shot_no": row["shot_no"],
        "duration_s": row["duration_s"],
        "scene_time": _row_value(row, "scene_time", ""),
        "scene_name": _row_value(row, "scene_name", "") or row["scene_setting"],
        "shot_size": row["shot_size"],
        "camera_move": row["camera_move"],
        "action_desc": row["action_desc"],
        "first_frame_desc": _row_value(row, "first_frame_desc", ""),
        "last_frame_desc": _row_value(row, "last_frame_desc", ""),
        "dialogues": json.loads(row["dialogues"] or "[]"),
        "transition": row["transition"],
        "continuity_mode": _row_value(row, "continuity_mode", ""),
        "state_in": contract.get("state_in"),
        "state_out": contract.get("state_out"),
        "planned_state_in_fact_ids": contract.get("planned_state_in_fact_ids") or [],
        "planned_state_out_fact_ids": contract.get("planned_state_out_fact_ids") or [],
        "primary_action_id": contract.get("primary_action_id"),
        "supporting_action_ids": contract.get("supporting_action_ids") or [],
        "action_phase_ids": contract.get("action_phase_ids") or [],
        "completed_before_action_ids": contract.get("completed_before_action_ids") or [],
        "completed_before_action_phase_ids": (
            contract.get("completed_before_action_phase_ids") or []
        ),
        "capacity_budget": contract.get("capacity_budget"),
        "visible_entity_ids": contract.get("visible_entity_ids") or [],
        "offscreen_action_actor_ids": contract.get("offscreen_action_actor_ids") or [],
        "offscreen_action_target_ids": contract.get("offscreen_action_target_ids") or [],
        "action_participant_deliveries": (
            contract.get("action_participant_deliveries") or []
        ),
        "event_ids": contract.get("event_ids") or [],
        "boundary_from_previous": contract.get("narrative_boundary_from_previous"),
    }


def _shot_model_from_row(row: Any):
    from app.continuity import apply_shot_contract
    from app.schemas import Shot

    shot = Shot(
        shot_no=row["shot_no"],
        shot_uid=_row_value(row, "shot_uid", "") or "",
        duration_s=row["duration_s"],
        shot_size=row["shot_size"],
        camera_move=row["camera_move"],
        scene_time=_row_value(row, "scene_time", "") or "",
        scene_setting=row["scene_setting"],
        scene_name=_row_value(row, "scene_name", "") or "",
        characters=json.loads(row["characters"] or "[]"),
        action_desc=row["action_desc"],
        first_frame_desc=_row_value(row, "first_frame_desc", "") or "",
        last_frame_desc=_row_value(row, "last_frame_desc", "") or "",
        source_excerpt=_row_value(row, "source_excerpt", "") or "",
        narration=row["narration"],
        dialogues=json.loads(row["dialogues"] or "[]"),
        transition=row["transition"] or "硬切",
        continuity_from_prev=bool(row["continuity_from_prev"]),
        continuity_mode=_row_value(row, "continuity_mode", "") or "",
        observed_state_out=_row_value(row, "observed_state_out", "") or "",
    )
    apply_shot_contract(shot, _row_value(row, "shot_contract_json", ""))
    return shot


async def generate_episode_plan(
    episode_id: str,
    *,
    force: bool = False,
    conn=None,
) -> EpisodeVideoGenerationPlan:
    """Ask AI once for the whole episode, then publish only a deterministic-valid plan."""
    from app import hiagent
    from app.schemas import extract_json

    db = conn or get_conn()
    episode = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not episode:
        raise ValueError(f"分集不存在：{episode_id}")
    project = db.execute(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],),
    ).fetchone()
    from app.domain.common import _project_bible_or_placeholder

    bible = _project_bible_or_placeholder(project)
    rows = db.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    if not rows:
        raise VideoPlanValidationError([{
            "code": "BLOCKED_UPSTREAM_CONTRACT",
            "shot_id": None,
            "missing_or_conflicting_fields": ["shots"],
            "required_owner": "storyboard",
        }])
    release_manifest = current_storyboard_release_manifest(episode_id, conn=db)
    # A production revision is workflow metadata, not content identity.  Plans
    # bind the exact approved storyboard Artifact instead.
    revision_id = release_manifest["published_storyboard_artifact_id"]
    if not revision_id:
        raise VideoPlanValidationError([{
            "code": "BLOCKED_UPSTREAM_CONTRACT",
            "shot_id": None,
            "missing_or_conflicting_fields": ["source_storyboard_revision_id"],
            "required_owner": "storyboard",
        }])
    current = load_latest_plan(episode_id, conn=db)
    snapshot = current_capability_snapshot(conn=db)
    narrative_actions: dict[str, dict[str, Any]] = {}
    from app.production.screenplay_authority import resolve_downstream_screenplay

    try:
        screenplay_context = resolve_downstream_screenplay(episode_id, conn=db)
    except ValueError:
        # ``current_storyboard_release_manifest`` above already rejects every
        # durable/modern authority downgrade.  Only a genuinely projection-less
        # historical episode can reach this compatibility branch.
        screenplay_context = None
    if (
        screenplay_context is not None
        and screenplay_context.narrative_authority_required
        and screenplay_context.screenplay.narrative_plan is not None
    ):
        narrative_actions = {
            action.action_id: action.model_dump(mode="json")
            for action in screenplay_context.screenplay.narrative_plan.atomic_actions
        }
    authoritative_screenplay = (
        screenplay_context.screenplay if screenplay_context is not None else None
    )
    next_revision = int(db.execute(
        "SELECT COALESCE(MAX(plan_revision),0)+1 n FROM episode_video_generation_plans WHERE episode_id=?",
        (episode_id,),
    ).fetchone()["n"])
    plan_id = new_id("evp")
    shot_payload = []
    asset_fingerprints: dict[str, str] = {}
    asset_resolution_issues: list[dict[str, Any]] = []
    from app.multiview import resolve_shot_asset_dependencies

    for row in rows:
        payload = _shot_planner_payload(row)
        bound_action_ids = [
            *([payload.get("primary_action_id")] if payload.get("primary_action_id") else []),
            *(payload.get("supporting_action_ids") or []),
        ]
        payload["atomic_action_contracts"] = [
            narrative_actions[action_id]
            for action_id in bound_action_ids
            if action_id in narrative_actions
        ]
        try:
            shot_model = _shot_model_from_row(row)
            manifest = resolve_shot_asset_dependencies(
                project_id=episode["project_id"],
                episode_no=int(episode["episode_no"]),
                shot_id=row["id"],
                shot=shot_model,
                scene_name=shot_model.scene_name or None,
                conn=db,
                bible=bible,
                screenplay=authoritative_screenplay,
            )
        except Exception as exc:  # asset service failures remain visible to the planner
            manifest = {
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            }
            asset_resolution_issues.append({
                "code": "ASSET_REVISION_RESOLUTION_FAILED",
                "shot_id": str(row["id"]),
                "evidence": manifest["error"],
                "required_owner": "asset",
            })
        payload["asset_revisions"] = {
            "characters": manifest.get("characters") or [],
            "scene": manifest.get("scene"),
            "input_fingerprint": manifest.get("input_fingerprint"),
            "status": manifest.get("status"),
        }
        if str(manifest.get("status") or "").lower() in {
            "unavailable", "blocked", "failed",
        }:
            asset_resolution_issues.append({
                "code": "ASSET_REVISION_NOT_READY",
                "shot_id": str(row["id"]),
                "evidence": manifest.get("error") or manifest.get("status"),
                "required_owner": "asset",
            })
        fingerprint = str(manifest.get("input_fingerprint") or _hash(manifest))
        asset_fingerprints[str(row["id"])] = fingerprint
        asset_fingerprints[str(payload["shot_id"])] = fingerprint
        shot_payload.append(payload)
    if asset_resolution_issues:
        raise VideoPlanValidationError(asset_resolution_issues)
    if (
        current
        and current.source_storyboard_revision_id == revision_id
        and current.capability_snapshot_id == snapshot.id
        and not force
        and all(
            item.input_revision_fingerprints.get("asset_revisions")
            == asset_fingerprints.get(item.shot_id)
            for item in current.shots
        )
    ):
        candidate = current.model_copy(deep=True)
        if candidate.status == "stale":
            candidate.status = "valid"
            for item in candidate.shots:
                item.status = (
                    "degraded"
                    if item.degraded_to_mode is not None
                    else "planned"
                )
        try:
            validate_episode_plan(
                candidate,
                list(rows),
                snapshot,
                release_manifest=release_manifest,
            )
        except VideoPlanValidationError:
            current.status = "stale"
            db.execute(
                "UPDATE episode_video_generation_plans SET status='stale' WHERE id=?",
                (current.episode_video_plan_id,),
            )
        else:
            if current.status == "stale":
                db.execute(
                    "UPDATE episode_video_generation_plans SET status='valid' "
                    "WHERE id=? AND status='stale'",
                    (current.episode_video_plan_id,),
                )
                for item in candidate.shots:
                    db.execute(
                        """UPDATE shot_video_generation_plans
                              SET status=?,updated_at=? WHERE id=?""",
                        (item.status, now(), item.shot_plan_id),
                    )
                db.commit()
            return candidate
    if current:
        # A storyboard may be re-signed because its publication/calibration
        # evidence changed while every executable shot and bound asset stayed
        # byte-for-byte equivalent.  Preserve the already validated semantic
        # plan in that case and bind it to the fresh release identity.  Sending
        # the entire unchanged episode back through the model is both slower
        # and vulnerable to provider context limits on long episodes.
        current_by_shot_id = {item.shot_id: item for item in current.shots}
        unchanged_execution_inputs = len(current_by_shot_id) == len(rows) and all(
            (
                (item := current_by_shot_id.get(str(row["id"]))) is not None
                and item.input_revision_fingerprints.get("shot_contract")
                == canonical_shot_contract_fingerprint(row)
                and item.input_revision_fingerprints.get("asset_revisions")
                == asset_fingerprints.get(str(row["id"]))
            )
            for row in rows
        )
        if unchanged_execution_inputs:
            candidate = current.model_copy(deep=True)
            candidate.episode_video_plan_id = plan_id
            candidate.plan_revision = next_revision
            candidate.source_storyboard_revision_id = revision_id
            candidate.capability_snapshot_id = snapshot.id
            candidate.status = "draft"
            candidate.planner_provider = "deterministic"
            candidate.planner_model = (
                "unchanged-execution-release-rebind"
                if current.capability_snapshot_id == snapshot.id
                else "compatible-capability-rebind"
            )
            candidate.planner_prompt_fingerprint = _hash({
                "parent_plan_id": current.episode_video_plan_id,
                "parent_plan_revision": current.plan_revision,
                "release_manifest": release_manifest,
                "capability_snapshot_id": snapshot.id,
                "shot_contract_fingerprints": {
                    str(row["id"]): canonical_shot_contract_fingerprint(row)
                    for row in rows
                },
                "asset_fingerprints": asset_fingerprints,
            })
            candidate.created_at = now()
            for item in candidate.shots:
                item.shot_plan_id = new_id("svp")
                item.episode_video_plan_id = plan_id
                item.plan_revision = next_revision
                item.source_storyboard_revision_id = revision_id
                item.capability_snapshot_id = snapshot.id
                item.status = (
                    "degraded"
                    if item.degraded_to_mode is not None
                    else "planned"
                )
            bind_plan_release_identity(candidate, list(rows), release_manifest)
            try:
                validate_episode_plan(
                    candidate,
                    list(rows),
                    snapshot,
                    release_manifest=release_manifest,
                )
            except VideoPlanValidationError:
                # The new provider cannot execute the existing semantic modes;
                # fall through to the normal planner instead of weakening or
                # silently changing the plan.
                pass
            else:
                publish_plan(candidate, conn=db)
                log_provider_call(
                    "episode_video_mode_plan_release_rebind",
                    candidate.planner_model,
                    "REUSED",
                    None,
                    0,
                    meta={
                        "episode_id": episode_id,
                        "plan_revision": next_revision,
                        "source_plan_id": current.episode_video_plan_id,
                        "shot_count": len(rows),
                    },
                )
                db.commit()
                return candidate
    if len(rows) == 1:
        only_row = rows[0]
        item = ShotVideoGenerationPlan(
            shot_plan_id=new_id("svp"),
            episode_video_plan_id=plan_id,
            plan_revision=next_revision,
            source_storyboard_revision_id=revision_id,
            shot_id=str(only_row["id"]),
            published_shot_id=str(shot_payload[0]["shot_id"]),
            shot_no=1,
            mode=VideoGenerationMode.REFERENCE_IMAGE_MODE,
            reason_codes=["FIRST_SHOT_NO_PREDECESSOR"],
            confidence=1.0,
            estimated_latency_ms=690_000,
            estimated_cost=0.0,
            capability_snapshot_id=snapshot.id,
            input_revision_fingerprints={
                "asset_revisions": asset_fingerprints.get(str(only_row["id"]), ""),
            },
        )
        plan = EpisodeVideoGenerationPlan(
            episode_video_plan_id=plan_id,
            episode_id=episode_id,
            plan_revision=next_revision,
            source_storyboard_revision_id=revision_id,
            capability_snapshot_id=snapshot.id,
            planner_provider="deterministic",
            planner_model="first-shot-invariant",
            planner_prompt_fingerprint=_hash({
                "first_shot": shot_payload[0],
                "capability_snapshot_id": snapshot.id,
            }),
            shots=[item],
        )
        bind_plan_release_identity(plan, list(rows), release_manifest)
        validate_episode_plan(
            plan,
            list(rows),
            snapshot,
            release_manifest=release_manifest,
        )
        publish_plan(plan, conn=db)
        db.commit()
        return plan
    capability_payload = snapshot.model_dump(mode="json")
    prompt_payload = {
        "task": "plan_episode_video_generation_modes",
        "source_storyboard_revision_id": revision_id,
        "storyboard_release_manifest": release_manifest,
        "capability_snapshot": capability_payload,
        "relation_enum_contract": SHOT_RELATION_ENUM_CONTRACT,
        "shots": shot_payload,
    }
    system = (
        "你是视频生产工具层规划器，只能引用输入中的 shot/database_shot ID，不得改写剧情、"
        "新增/删除/调换镜头。输入可能是整集的一个按请求体大小切分的窗口，只输出输入 shots。"
        "你只负责分析每镜的时空、剪辑、动作阶段、状态依赖、运动依赖与置信度；"
        "不得输出执行模式、镜头依赖、视频输入意图或 required_assets。"
        "执行模式和素材合同由程序根据真实场景顺序统一编译。关系判断只基于时空、剪辑、动作阶段、"
        "状态依赖和运动依赖，禁止按人物名、地点名、题材、打斗词或动作词表决定模式。"
        "new_domain、new_space 或 scene_cut 表示新场景首镜。"
        "relations 四个字段必须逐字使用 relation_enum_contract 对应数组中的枚举值，禁止自造同义词。"
        "只输出 JSON，不要 Markdown。"
    )
    output_contract = (
        "\n输出：{\"shots\":[{\"shot_id\":数据库或发布shot ID,"
        "\"relations\":{\"temporal\":\"same_moment|elapsed|jump|new_domain|unknown\","
        "\"spatial\":\"same_space|adjacent_space|new_space|unknown\","
        "\"edit\":\"continuous_take|match_cut|angle_cut|reaction_cut|reverse_angle|insert_cut|montage|scene_cut|unknown\","
        "\"action\":\"continues_same_action|starts_new_action|shows_result|observes_result|no_action|unknown\"},"
        "\"state_dependency\":\"none|start_only|start_and_end|full_trajectory\","
        "\"motion_dependency\":\"none|pose|trajectory|camera|rhythm|audio\","
        "\"reason_codes\":[通用关系码],\"confidence\":0到1,\"unknown_dimensions\":[],"
        "\"estimated_latency_ms\":整数,\"estimated_cost\":数字}]}"
    )
    planner_payload_base = {
        key: value for key, value in prompt_payload.items() if key != "shots"
    }
    planner_windows: list[list[dict[str, Any]]] = []
    current_window: list[dict[str, Any]] = []
    # Partition only by serialized request size.  No character, location,
    # genre or action-name routing is involved, and validation still requires
    # exact whole-episode coverage after the windows are recombined.
    planner_window_char_budget = 42_000
    for shot in shot_payload:
        candidate_window = [*current_window, shot]
        candidate_payload = {**planner_payload_base, "shots": candidate_window}
        if current_window and len(_json(candidate_payload)) > planner_window_char_budget:
            planner_windows.append(current_window)
            current_window = [shot]
        else:
            current_window = candidate_window
    if current_window:
        planner_windows.append(current_window)

    cached_rows = db.execute(
        """SELECT id,request_json,response_json FROM provider_calls
           WHERE kind='chat' AND status IN ('OK','SUCCESS','SUCCEEDED')
             AND json_valid(meta)
             AND json_extract(meta,'$.stage')='episode_video_mode_plan'
             AND json_extract(meta,'$.episode_id')=?
             AND response_json IS NOT NULL
           ORDER BY id DESC LIMIT 32""",
        (episode_id,),
    ).fetchall()
    active_model = hiagent.active_model("text")
    raw_shots: list[Any] = []
    for window_index, window_shots in enumerate(planner_windows):
        window_payload = {
            **planner_payload_base,
            "planning_window": {
                "index": window_index + 1,
                "count": len(planner_windows),
                "shot_start": window_shots[0]["shot_no"],
                "shot_end": window_shots[-1]["shot_no"],
            },
            "shots": window_shots,
        }
        user = _json(window_payload) + output_contract
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        response: str | None = None
        cached_call_id: int | None = None
        for cached in cached_rows:
            try:
                request_payload = json.loads(cached["request_json"] or "{}")
                response_payload = json.loads(cached["response_json"] or "{}")
                if (
                    request_payload.get("model") != active_model
                    or request_payload.get("messages") != messages
                ):
                    continue
                response = str(
                    response_payload["choices"][0]["message"]["content"]
                )
            except (
                KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError,
            ):
                continue
            cached_call_id = int(cached["id"])
            break
        if response is None:
            response = await hiagent.chat(
                messages,
                temperature=0.1,
                max_tokens=max(4096, min(20000, len(window_shots) * 900)),
                call_meta={
                    "stage": "episode_video_mode_plan",
                    "episode_id": episode_id,
                    "plan_revision": next_revision,
                    "window_index": window_index + 1,
                    "window_count": len(planner_windows),
                    "contract_version": "episode-video-plan.v2",
                    "planner_prompt_fingerprint": _hash(window_payload),
                    "planner_episode_fingerprint": _hash(prompt_payload),
                    "operation_id": (
                        "op_video_plan_" + _hash({
                            "model": active_model,
                            "messages": messages,
                        })[:24]
                    ),
                    "reuse_successful_operation": True,
                },
            )
        else:
            log_provider_call(
                "episode_video_mode_plan_cache",
                active_model,
                "REUSED",
                None,
                0,
                meta={
                    "episode_id": episode_id,
                    "plan_revision": next_revision,
                    "window_index": window_index + 1,
                    "window_count": len(planner_windows),
                    "source_provider_call_id": cached_call_id,
                },
            )
        parsed = extract_json(response)
        window_raw_shots = parsed.get("shots") if isinstance(parsed, dict) else None
        if not isinstance(window_raw_shots, list):
            raise VideoPlanValidationError([{
                "code": "AI_PLAN_SCHEMA_INVALID",
                "window_index": window_index + 1,
                "evidence": response[:500],
            }])
        raw_shots.extend(window_raw_shots)
    shot_plans: list[ShotVideoGenerationPlan] = []
    for index, raw in enumerate(raw_shots):
        if not isinstance(raw, dict):
            raise VideoPlanValidationError([{"code": "AI_PLAN_SCHEMA_INVALID", "index": index}])
        raw, normalizations = normalize_ai_shot_plan_candidate(raw)
        if normalizations:
            log_provider_call(
                "episode_video_mode_plan_normalization",
                active_model,
                "NORMALIZED",
                None,
                0,
                meta={
                    "episode_id": episode_id,
                    "plan_revision": next_revision,
                    "index": index,
                    "changes": normalizations,
                },
            )
        shot_id = str(raw.get("shot_id") or "")
        try:
            analysis = PlannerShotAnalysis.model_validate(raw)
            shot_plan = ShotVideoGenerationPlan(
                shot_plan_id=new_id("svp"),
                episode_video_plan_id=plan_id,
                plan_revision=next_revision,
                source_storyboard_revision_id=revision_id,
                shot_id=analysis.shot_id,
                published_shot_id=analysis.shot_id,
                shot_no=index + 1,
                mode=VideoGenerationMode.REFERENCE_IMAGE_MODE,
                video_input_intent=None,
                depends_on_shot_id=None,
                relations=analysis.relations,
                state_dependency=analysis.state_dependency,
                motion_dependency=analysis.motion_dependency,
                required_assets=[],
                reason_codes=analysis.reason_codes,
                confidence=analysis.confidence,
                unknown_dimensions=analysis.unknown_dimensions,
                fallback_order=[],
                max_cost=max(analysis.estimated_cost * 2, 1.0),
                timeout_s=7200,
                estimated_latency_ms=analysis.estimated_latency_ms,
                estimated_cost=analysis.estimated_cost,
                capability_snapshot_id=snapshot.id,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise VideoPlanValidationError([{
                "code": "AI_PLAN_SCHEMA_INVALID",
                "index": index,
                "shot_id": shot_id,
                "evidence": str(exc)[:1000],
            }]) from exc
        shot_plans.append(shot_plan)
        shot_plans[-1].input_revision_fingerprints["asset_revisions"] = (
            asset_fingerprints.get(shot_id, "")
        )
    planner_shot_numbers = {
        str(identifier): int(payload["shot_no"])
        for payload in shot_payload
        for identifier in (
            payload.get("shot_id"),
            payload.get("database_shot_id"),
        )
        if identifier
    }
    for item in shot_plans:
        if item.shot_id in planner_shot_numbers:
            item.shot_no = planner_shot_numbers[item.shot_id]
    planner_scene_identities = {
        str(identifier): _scene_identity(row)
        for payload, row in zip(shot_payload, rows)
        for identifier in (
            payload.get("shot_id"),
            payload.get("database_shot_id"),
        )
        if identifier
    }
    boundary_changes = apply_scene_boundary_strategy(
        shot_plans,
        scene_identity_by_shot_id=planner_scene_identities,
    )
    if boundary_changes:
        log_provider_call(
            "episode_video_boundary_strategy",
            active_model,
            "NORMALIZED",
            None,
            0,
            meta={
                "episode_id": episode_id,
                "plan_revision": next_revision,
                "changes": boundary_changes,
            },
        )
    plan = EpisodeVideoGenerationPlan(
        episode_video_plan_id=plan_id,
        episode_id=episode_id,
        plan_revision=next_revision,
        source_storyboard_revision_id=revision_id,
        capability_snapshot_id=snapshot.id,
        planner_provider=hiagent.active_provider("text"),
        planner_model=hiagent.active_model("text"),
        planner_prompt_fingerprint=_hash(prompt_payload),
        shots=shot_plans,
    )
    bind_plan_release_identity(plan, list(rows), release_manifest)
    validate_episode_plan(
        plan,
        list(rows),
        snapshot,
        release_manifest=release_manifest,
    )
    publish_plan(plan, conn=db)
    db.commit()
    return plan


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


def verify_episode_plan_is_current(
    plan: EpisodeVideoGenerationPlan,
    *,
    conn=None,
    mark_stale: bool = True,
) -> bool:
    """Revalidate the immutable release and every canonical shot fingerprint."""
    db = conn or get_conn()
    if plan.status != "valid":
        return False
    rows = db.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (plan.episode_id,),
    ).fetchall()
    snapshot = capability_snapshot_by_id(plan.capability_snapshot_id, conn=db)
    try:
        if snapshot is None:
            raise VideoPlanValidationError([{"code": "CAPABILITY_SNAPSHOT_MISSING"}])
        manifest = current_storyboard_release_manifest(plan.episode_id, conn=db)
        validate_episode_plan(
            plan,
            list(rows),
            snapshot,
            release_manifest=manifest,
        )
    except (ValueError, VideoPlanValidationError):
        if mark_stale:
            _mark_episode_video_plan_stale(plan, conn=db)
            if conn is None:
                db.commit()
        return False
    return True


def _mark_episode_video_plan_stale(
    plan: EpisodeVideoGenerationPlan,
    *,
    conn,
) -> None:
    """Persist one fail-closed state for every projection of an episode plan."""
    conn.execute(
        "UPDATE episode_video_generation_plans SET status='stale' WHERE id=?",
        (plan.episode_video_plan_id,),
    )
    conn.execute(
        "UPDATE shot_video_generation_plans SET status='stale',updated_at=? "
        "WHERE episode_video_plan_id=?",
        (now(), plan.episode_video_plan_id),
    )


def assert_video_provider_submission_authority(
    *,
    shot_id: str,
    shot_plan_id: str,
    actual_mode: VideoGenerationMode | str,
    expected_capability_snapshot_id: str | None = None,
    conn=None,
) -> tuple[ShotVideoGenerationPlan, ProviderVideoCapabilitySnapshot]:
    """Authorize the last reversible boundary before a paid video submission.

    The check is intentionally mode-agnostic.  It resolves the current episode
    plan and then validates its complete storyboard release manifest and every
    canonical shot contract fingerprint.  It also compares the selected shot
    against the newest capability observation for the provider/model that the
    video client would use *now*.  Provider/model switches, failed probes, and
    capability withdrawals therefore stale the whole plan instead of letting a
    worker submit from an old positive snapshot.
    """
    from app import hiagent

    db = conn or get_conn()
    shot = db.execute(
        """SELECT s.episode_id AS episode_id, e.target_video_model AS target_video_model
             FROM shots s JOIN episodes e ON e.id=s.episode_id
            WHERE s.id=?""",
        (shot_id,),
    ).fetchone()
    issues: list[dict[str, Any]] = []
    # Episode/generation binding is mode-agnostic and independent of the plan:
    # the operator selects a video model per episode on the storyboard page,
    # and every provider submission must be re-checked against it here, at the
    # last reversible boundary, because the globally active provider (Model
    # Center) can drift after enqueue while a job sits queued.  A mismatch is
    # a caller bug (stale binding, or someone flipped the global provider
    # underneath a bound episode) and must be rejected, never silently
    # rewritten — the two providers' prompt dialects are incompatible.
    active_provider = hiagent.active_provider("video")
    if shot is not None:
        from app import video_providers

        episode_bound_provider = str(shot["target_video_model"] or "").strip() or "hiagent"
        # 按适配器族比较，不按 provider key 原始字符串比：自建实例（custom:xxx）
        # 复用内置协议实现，字符串比较会把"同协议、不同连接"误判成绑定不一致
        # （本机部署的历史模型迁移已经把内嵌 Seedance/MiniMax H3 包装成了
        # custom:<id>，字符串比较在这台机器上会 100% 误判，见
        # video_providers.same_family）。下面的能力快照解析仍使用未归一化的
        # 原始 active_provider，保持与改动前完全一致的行为。
        if not video_providers.same_family(episode_bound_provider, active_provider):
            issues.append({
                "code": "VIDEO_SUBMISSION_EPISODE_MODEL_BINDING_MISMATCH",
                "shot_id": shot_id,
                "episode_bound_provider": episode_bound_provider,
                "active_provider": active_provider,
            })
    plan = load_latest_plan(str(shot["episode_id"]), conn=db) if shot else None
    if shot is None:
        issues.append({"code": "VIDEO_SUBMISSION_SHOT_MISSING", "shot_id": shot_id})
    elif plan is None:
        issues.append({
            "code": "VIDEO_SUBMISSION_PLAN_MISSING",
            "shot_id": shot_id,
            "shot_plan_id": shot_plan_id,
        })
    elif not verify_episode_plan_is_current(plan, conn=db):
        issues.append({
            "code": "VIDEO_SUBMISSION_PLAN_STALE",
            "episode_video_plan_id": plan.episode_video_plan_id,
            "shot_plan_id": shot_plan_id,
        })

    selected = (
        next((item for item in plan.shots if item.shot_id == shot_id), None)
        if plan is not None
        else None
    )
    if selected is None:
        issues.append({
            "code": "VIDEO_SUBMISSION_SHOT_PLAN_MISSING",
            "shot_id": shot_id,
            "shot_plan_id": shot_plan_id,
        })
    elif selected.shot_plan_id != shot_plan_id:
        if not active_plan_is_current(shot_plan_id, conn=db):
            issues.append({
                "code": "VIDEO_SUBMISSION_SHOT_PLAN_STALE",
                "shot_id": shot_id,
                "stored": shot_plan_id,
                "current": selected.shot_plan_id,
            })

    try:
        submitted_mode = VideoGenerationMode(actual_mode)
    except ValueError:
        submitted_mode = None
        issues.append({
            "code": "VIDEO_SUBMISSION_MODE_INVALID",
            "shot_id": shot_id,
            "actual_mode": str(actual_mode),
        })

    latest_snapshot: ProviderVideoCapabilitySnapshot | None = None
    if selected is not None:
        if (
            expected_capability_snapshot_id
            and expected_capability_snapshot_id != selected.capability_snapshot_id
        ):
            issues.append({
                "code": "VIDEO_SUBMISSION_CAPABILITY_BINDING_STALE",
                "shot_id": shot_id,
                "stored": expected_capability_snapshot_id,
                "current": selected.capability_snapshot_id,
            })
        if submitted_mode is not None and submitted_mode != selected.mode:
            issues.append({
                "code": "VIDEO_SUBMISSION_MODE_PLAN_MISMATCH",
                "shot_id": shot_id,
                "planned_mode": selected.mode.value,
                "actual_mode": submitted_mode.value,
            })

        bound_snapshot = capability_snapshot_by_id(
            selected.capability_snapshot_id,
            conn=db,
        )
        active_model = hiagent.active_model("video", active_provider)
        latest_snapshot = current_capability_snapshot(
            provider=active_provider,
            model=active_model,
            conn=db,
        )
        if bound_snapshot is None:
            issues.append({
                "code": "VIDEO_SUBMISSION_CAPABILITY_SNAPSHOT_MISSING",
                "shot_id": shot_id,
                "snapshot_id": selected.capability_snapshot_id,
            })
        elif (
            bound_snapshot.provider != latest_snapshot.provider
            or bound_snapshot.model != latest_snapshot.model
        ):
            issues.append({
                "code": "VIDEO_SUBMISSION_PROVIDER_SELECTION_STALE",
                "shot_id": shot_id,
                "planned_provider": bound_snapshot.provider,
                "planned_model": bound_snapshot.model,
                "active_provider": latest_snapshot.provider,
                "active_model": latest_snapshot.model,
            })
        if not latest_snapshot.technical_success:
            issues.append({
                "code": "VIDEO_SUBMISSION_LATEST_PROBE_FAILED",
                "shot_id": shot_id,
                "snapshot_id": latest_snapshot.id,
                "probe_result": latest_snapshot.probe_result,
            })
        if submitted_mode is not None and not capability_allows(
            latest_snapshot,
            submitted_mode,
            selected.video_input_intent,
        ):
            issues.append({
                "code": "VIDEO_SUBMISSION_CAPABILITY_WITHDRAWN",
                "shot_id": shot_id,
                "snapshot_id": latest_snapshot.id,
                "mode": submitted_mode.value,
                "intent": (
                    selected.video_input_intent.value
                    if selected.video_input_intent
                    else None
                ),
            })

    if issues:
        if plan is not None:
            _mark_episode_video_plan_stale(plan, conn=db)
            # This assertion is a terminal paid-work fence.  When it rejects,
            # there is no successful caller transaction to commit later.  A
            # worker-thread connection would otherwise retain the stale-plan
            # UPDATE and hold SQLite's single writer lock indefinitely.
            db.commit()
        raise VideoPlanValidationError(issues)
    assert selected is not None and latest_snapshot is not None
    return selected, latest_snapshot


def get_shot_plan(shot_id: str, *, conn=None) -> ShotVideoGenerationPlan | None:
    db = conn or get_conn()
    shot = db.execute(
        "SELECT episode_id FROM shots WHERE id=?",
        (shot_id,),
    ).fetchone()
    if not shot:
        return None
    plan = load_latest_plan(str(shot["episode_id"]), conn=db)
    if plan is None or not verify_episode_plan_is_current(plan, conn=db):
        return None
    return next((item for item in plan.shots if item.shot_id == shot_id), None)


def record_mode_attempt(
    *,
    version_id: str,
    shot_plan: ShotVideoGenerationPlan,
    actual_mode: VideoGenerationMode,
    status: str,
    provider_task_id: str | None = None,
    error: str | None = None,
    conn=None,
) -> str:
    db = conn or get_conn()
    running = db.execute(
        """SELECT id FROM video_generation_attempts
           WHERE version_id=? AND status='provider_running'
             AND COALESCE(provider_task_id,'')=COALESCE(?,'')
           ORDER BY attempt_no DESC LIMIT 1""",
        (version_id, provider_task_id),
    ).fetchone()
    terminal_running = (
        db.execute(
            """SELECT id FROM video_generation_attempts
               WHERE version_id=? AND status='provider_running'
               ORDER BY attempt_no DESC LIMIT 1""",
            (version_id,),
        ).fetchone()
        if status != "provider_running" else None
    )
    existing = running or terminal_running
    attempt_id = existing["id"] if existing else new_id("vattempt")
    if existing:
        db.execute(
            """UPDATE video_generation_attempts SET actual_mode=?,status=?,
                      provider_task_id=?,error=?,updated_at=? WHERE id=?""",
            (actual_mode.value, status, provider_task_id, error, now(), attempt_id),
        )
    else:
        attempt_no = int(db.execute(
            "SELECT COALESCE(MAX(attempt_no),0)+1 n FROM video_generation_attempts WHERE version_id=?",
            (version_id,),
        ).fetchone()["n"])
        db.execute(
            """INSERT INTO video_generation_attempts(
                   id,shot_plan_id,version_id,attempt_no,planned_mode,actual_mode,
                   video_input_intent,status,provider_task_id,error,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                attempt_id, shot_plan.shot_plan_id, version_id, attempt_no,
                shot_plan.mode.value, actual_mode.value,
                shot_plan.video_input_intent.value if shot_plan.video_input_intent else None,
                status, provider_task_id, error, now(), now(),
            ),
        )
    db.execute(
        """UPDATE shot_video_generation_plans
              SET actual_mode=?,status=?,updated_at=? WHERE id=?""",
        (actual_mode.value, status, now(), shot_plan.shot_plan_id),
    )
    if conn is None:
        db.commit()
    return attempt_id


def active_plan_is_current(shot_plan_id: str, *, conn=None) -> bool:
    db = conn or get_conn()
    row = db.execute(
        """SELECT sp.*,ep.status AS episode_status,ep.episode_id,
                  ep.plan_revision,ep.source_storyboard_revision_id
           FROM shot_video_generation_plans sp
           JOIN episode_video_generation_plans ep ON ep.id=sp.episode_video_plan_id
           WHERE sp.id=?""",
        (shot_plan_id,),
    ).fetchone()
    if (
        not row
        or row["status"] in {"stale", "superseded"}
    ):
        return False
    plan = load_latest_plan(str(row["episode_id"]), conn=db)
    if plan is None or not verify_episode_plan_is_current(plan, conn=db):
        return False
    current = next(
        (item for item in plan.shots if item.shot_id == row["shot_id"]),
        None,
    )
    if current is None:
        return False
    if row["episode_status"] == "valid":
        return current.shot_plan_id == shot_plan_id
    previous = _shot_plan_from_row(row, row)
    return (
        shot_video_execution_contract_fingerprint(previous)
        == shot_video_execution_contract_fingerprint(current)
    )


def create_local_replan_revision(
    shot_id: str,
    *,
    reason: str,
    conn=None,
    idempotency_key: str | None = None,
) -> EpisodeVideoGenerationPlan:
    """Create a new plan revision while changing only one shot's input identity."""
    db = conn or get_conn()
    shot = db.execute(
        "SELECT episode_id FROM shots WHERE id=?",
        (shot_id,),
    ).fetchone()
    if not shot:
        raise ValueError(f"镜头不存在：{shot_id}")
    current = load_latest_plan(shot["episode_id"], conn=db)
    if (
        not current
        or current.status != "valid"
        or not verify_episode_plan_is_current(current, conn=db)
    ):
        raise ValueError("单镜重做缺少当前有效的视频模式计划")
    operation_key = str(idempotency_key or "").strip()
    operation_fingerprint = (
        _hash({
            "episode_id": str(shot["episode_id"]),
            "shot_id": shot_id,
            "reason": reason,
            "idempotency_key": operation_key,
        })
        if operation_key
        else ""
    )
    if operation_fingerprint:
        existing = db.execute(
            """SELECT id FROM episode_video_generation_plans
               WHERE episode_id=? AND planner_model='local-shot-replan'
                 AND planner_prompt_fingerprint=?
               ORDER BY plan_revision DESC LIMIT 1""",
            (shot["episode_id"], operation_fingerprint),
        ).fetchone()
        if existing and current.episode_video_plan_id == str(existing["id"]):
            return current
    next_revision = int(db.execute(
        "SELECT COALESCE(MAX(plan_revision),0)+1 n FROM episode_video_generation_plans WHERE episode_id=?",
        (shot["episode_id"],),
    ).fetchone()["n"])
    replacement = current.model_copy(deep=True)
    replacement.episode_video_plan_id = new_id("evp")
    replacement.plan_revision = next_revision
    replacement.status = "draft"
    replacement.created_at = now()
    replacement.blockers = []
    replacement.planner_provider = "deterministic"
    replacement.planner_model = "local-shot-replan"
    replacement.planner_prompt_fingerprint = operation_fingerprint or _hash({
        "source_plan_id": current.episode_video_plan_id,
        "shot_id": shot_id,
        "reason": reason,
        "plan_revision": next_revision,
    })
    target = None
    for item in replacement.shots:
        item.shot_plan_id = new_id("svp")
        item.episode_video_plan_id = replacement.episode_video_plan_id
        item.plan_revision = next_revision
        if item.shot_id != shot_id:
            continue
        target = item
        item.actual_mode = None
        item.status = "planned"
        item.reason_codes = [*item.reason_codes, "LOCAL_REPLAN_FOR_REDO"]
        item.input_revision_fingerprints["local_replan_revision"] = _hash({
            "reason": reason,
            "revision": next_revision,
            "created_at": replacement.created_at,
        })
    if target is None:
        raise ValueError("当前视频模式计划未覆盖待重做镜头")
    snapshot = capability_snapshot_by_id(
        replacement.capability_snapshot_id, conn=db,
    )
    rows = db.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (shot["episode_id"],),
    ).fetchall()
    if snapshot is None:
        raise ValueError("当前视频模式计划的能力快照不存在")
    manifest = current_storyboard_release_manifest(str(shot["episode_id"]), conn=db)
    validate_episode_plan(
        replacement,
        list(rows),
        snapshot,
        release_manifest=manifest,
    )
    publish_plan(replacement, conn=db)
    current_by_shot_id = {item.shot_id: item for item in current.shots}
    for item in replacement.shots:
        if item.shot_id == shot_id:
            continue
        previous = current_by_shot_id.get(item.shot_id)
        if previous is None:
            continue
        boundary_rows = db.execute(
            """SELECT * FROM video_boundary_assets
               WHERE shot_plan_id=? AND qa_status='passed'
               ORDER BY created_at""",
            (previous.shot_plan_id,),
        ).fetchall()
        for boundary in boundary_rows:
            path = str(boundary["path"] or "")
            if not path or not Path(path).is_file():
                continue
            db.execute(
                """INSERT OR IGNORE INTO video_boundary_assets(
                       id,episode_video_plan_id,shot_plan_id,shot_id,role,source,
                       source_revision_id,source_shot_id,source_adopted_version_id,
                       path,url,sha256,mime,width,height,qa_status,qa_json,
                       fingerprint,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("vba"),
                    replacement.episode_video_plan_id,
                    item.shot_plan_id,
                    item.shot_id,
                    boundary["role"],
                    boundary["source"],
                    boundary["source_revision_id"],
                    boundary["source_shot_id"],
                    boundary["source_adopted_version_id"],
                    path,
                    boundary["url"],
                    boundary["sha256"],
                    boundary["mime"],
                    boundary["width"],
                    boundary["height"],
                    boundary["qa_status"],
                    boundary["qa_json"],
                    boundary["fingerprint"],
                    now(),
                ),
            )
    db.commit()
    return replacement


def _release_unsubmitted_paused_reservations_for_adopted_shot(
    db,
    *,
    shot_id: str,
    adopted_version_id: str,
) -> int:
    """Release only obsolete local work that provably never reached the provider."""
    rows = db.execute(
        """SELECT j.id
             FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
            WHERE j.shot_id=? AND j.kind='video' AND j.status='paused'
              AND j.version_id!=?
              AND j.provider_non_cancellable=0
              AND j.provider_create_state='not_started'
              AND (v.provider_task_id IS NULL OR v.provider_task_id='')
              AND NOT EXISTS (
                  SELECT 1 FROM provider_calls pc
                   WHERE pc.operation_id=j.provider_operation_id
                     AND pc.kind='video_create' AND pc.status='OK'
              )
              AND EXISTS (
                  SELECT 1 FROM budget_reservations br
                   WHERE br.job_id=j.id AND br.status IN ('reserved','running')
              )""",
        (shot_id, adopted_version_id),
    ).fetchall()
    job_ids = [str(row["id"]) for row in rows]
    if not job_ids:
        return 0
    placeholders = ",".join("?" * len(job_ids))
    stamp = now()
    db.execute(
        f"""UPDATE budget_reservations
               SET status='released',settled_at=?,actual_cost_cny=0
             WHERE job_id IN ({placeholders})
               AND status IN ('reserved','running')""",
        (stamp, *job_ids),
    )
    db.execute(
        f"""UPDATE jobs SET reserved_cost_cny=0,updated_at=?
             WHERE id IN ({placeholders})""",
        (stamp, *job_ids),
    )
    return len(job_ids)


def reconcile_adopted_revision(
    shot_id: str,
    adopted_version_id: str,
    *,
    conn=None,
) -> dict[str, Any]:
    """Bind a first adoption or stale only descendants that consumed an older adoption."""
    db = conn or get_conn()
    shot = db.execute(
        "SELECT episode_id,adopted_version_id FROM shots WHERE id=?",
        (shot_id,),
    ).fetchone()
    if shot is None:
        raise ValueError(f"镜头不存在：{shot_id}")
    current_plan = load_latest_plan(str(shot["episode_id"]), conn=db)
    if current_plan is None:
        return {"bound": 0, "stale_shot_ids": []}
    if not verify_episode_plan_is_current(current_plan, conn=db):
        raise ValueError("当前视频模式计划已过期，禁止同步采用关系")

    current_adoption = str(shot["adopted_version_id"] or "")
    if adopted_version_id == "__unadopted__":
        if current_adoption:
            raise ValueError("镜头尚有当前采用版本，禁止伪造取消采用同步")
    else:
        adopted = db.execute(
            "SELECT shot_id,status FROM shot_versions WHERE id=?",
            (adopted_version_id,),
        ).fetchone()
        if adopted is None:
            raise ValueError("采用版本不存在")
        if adopted["shot_id"] != shot_id:
            raise ValueError("采用版本不属于当前镜头")
        if adopted["status"] != "succeeded":
            raise ValueError("只能同步已成功的采用版本")
        if current_adoption != adopted_version_id:
            raise ValueError("采用版本与 shots.adopted_version_id 当前指针不一致")
        _release_unsubmitted_paused_reservations_for_adopted_shot(
            db,
            shot_id=shot_id,
            adopted_version_id=adopted_version_id,
        )

    deps = db.execute(
        """SELECT * FROM video_plan_dependencies
           WHERE episode_video_plan_id=? AND depends_on_shot_id=?""",
        (current_plan.episode_video_plan_id, shot_id),
    ).fetchall()
    stale_roots: list[str] = []
    bound = 0
    for dep in deps:
        old = dep["upstream_adopted_version_id"]
        if adopted_version_id == "__unadopted__":
            stale_roots.append(dep["shot_id"])
            continue
        if not old:
            db.execute(
                """UPDATE video_plan_dependencies
                      SET upstream_adopted_version_id=?,resolved_at=?
                    WHERE id=?""",
                (adopted_version_id, now(), dep["id"]),
            )
            row = db.execute(
                "SELECT input_fingerprints_json FROM shot_video_generation_plans WHERE id=?",
                (dep["shot_plan_id"],),
            ).fetchone()
            published_fingerprints = (
                json.loads(row["input_fingerprints_json"] or "{}")
                if row else {}
            )
            execution_fingerprints = {
                **published_fingerprints,
                "upstream_adopted_video_revision": adopted_version_id,
            }
            db.execute(
                """UPDATE shot_video_generation_plans
                      SET status='ready',updated_at=? WHERE id=?""",
                (now(), dep["shot_plan_id"]),
            )
            waiting_jobs = db.execute(
                """SELECT j.id,j.version_id,v.idem_key,v.image_inputs
                   FROM jobs j
                   JOIN shot_versions v ON v.id=j.version_id
                   WHERE j.shot_id=? AND j.kind='video'
                     AND j.status IN ('queued','waiting_retry')
                     AND j.provider_non_cancellable=0
                     AND (v.provider_task_id IS NULL OR v.provider_task_id='')
                     AND json_valid(v.image_inputs)
                     AND json_extract(v.image_inputs,'$.shot_plan_id')=?""",
                (dep["shot_id"], dep["shot_plan_id"]),
            ).fetchall()
            for job in waiting_jobs:
                try:
                    meta = json.loads(job["image_inputs"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    meta = {}
                meta["upstream_adopted_video_revision"] = adopted_version_id
                meta["after_version_id"] = adopted_version_id
                meta["input_revision_fingerprints"] = execution_fingerprints
                meta["plan_status"] = "ready"
                idem = hashlib.sha256(
                    (
                        str(job["idem_key"] or "")
                        + "|upstream_adopted_video_revision:"
                        + adopted_version_id
                    ).encode("utf-8")
                ).hexdigest()
                db.execute(
                    """UPDATE shot_versions SET idem_key=?,image_inputs=?
                       WHERE id=?""",
                    (idem, _json(meta), job["version_id"]),
                )
                db.execute(
                    "UPDATE jobs SET after_version_id=?,updated_at=? WHERE id=?",
                    (adopted_version_id, now(), job["id"]),
                )
            bound += 1
        elif old != adopted_version_id:
            stale_roots.append(dep["shot_id"])

    stale: set[str] = set()
    queue = list(stale_roots)
    while queue:
        current_shot_id = queue.pop(0)
        if current_shot_id in stale:
            continue
        stale.add(current_shot_id)
        queue.extend(
            row["shot_id"]
            for row in db.execute(
                """SELECT shot_id FROM video_plan_dependencies
                   WHERE episode_video_plan_id=? AND depends_on_shot_id=?""",
                (current_plan.episode_video_plan_id, current_shot_id),
            ).fetchall()
        )
    for descendant in stale:
        db.execute(
            """UPDATE shot_video_generation_plans SET status='stale',updated_at=?
               WHERE episode_video_plan_id=? AND shot_id=?""",
            (now(), current_plan.episode_video_plan_id, descendant),
        )
        jobs = db.execute(
            """SELECT j.id,j.version_id,j.provider_non_cancellable,v.image_inputs
               FROM jobs j LEFT JOIN shot_versions v ON v.id=j.version_id
               WHERE j.shot_id=? AND j.kind='video'
                 AND j.status IN ('queued','running','waiting_provider','waiting_retry')""",
            (descendant,),
        ).fetchall()
        for job in jobs:
            try:
                meta = json.loads(job["image_inputs"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                meta = {}
            meta["stale"] = True
            meta["stale_reason"] = "upstream_adopted_revision_changed"
            meta["stale_upstream_shot_id"] = shot_id
            meta["stale_upstream_version_id"] = adopted_version_id
            if job["version_id"]:
                db.execute(
                    "UPDATE shot_versions SET image_inputs=? WHERE id=?",
                    (_json(meta), job["version_id"]),
                )
            if not job["provider_non_cancellable"]:
                db.execute(
                    """UPDATE jobs SET status='stale',cancellation_requested=1,
                              abandoned=1,error=?,updated_at=? WHERE id=?""",
                    ("上游采用版本已变化，当前任务已失效", now(), job["id"]),
                )
                if job["version_id"]:
                    db.execute(
                        "UPDATE shot_versions SET status='stale',error=? WHERE id=?",
                        ("上游采用版本已变化，当前候选已失效", job["version_id"]),
                    )
    if conn is None:
        db.commit()
    return {"bound": bound, "stale_shot_ids": sorted(stale)}


def mode_audit_for_job(job_id: str, *, conn=None) -> dict[str, Any] | None:
    db = conn or get_conn()
    row = db.execute(
        """SELECT j.id AS job_id,j.status AS job_status,j.reason_code,j.reason_text,
                  v.id AS version_id,v.provider_task_id,v.image_inputs,
                  sp.*,ep.plan_revision,ep.source_storyboard_revision_id,
                  ep.capability_snapshot_id AS episode_capability_snapshot_id
           FROM jobs j
           LEFT JOIN shot_versions v ON v.id=j.version_id
           LEFT JOIN shot_video_generation_plans sp
             ON sp.id=CASE WHEN json_valid(v.image_inputs)
                           THEN json_extract(v.image_inputs,'$.shot_plan_id') END
           LEFT JOIN episode_video_generation_plans ep ON ep.id=sp.episode_video_plan_id
           WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    try:
        meta = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    return {
        "job_id": row["job_id"],
        "job_status": row["job_status"],
        "version_id": row["version_id"],
        "provider_task_id": row["provider_task_id"],
        "shot_plan_id": meta.get("shot_plan_id"),
        "plan_revision": row["plan_revision"],
        "source_storyboard_revision_id": row["source_storyboard_revision_id"],
        "capability_snapshot_id": row["episode_capability_snapshot_id"],
        "planned_mode": row["planned_mode"] or meta.get("planned_mode") or meta.get("mode"),
        "actual_mode": row["actual_mode"] or meta.get("actual_mode"),
        "video_input_intent": row["video_input_intent"] or meta.get("video_input_intent"),
        "depends_on_shot_id": row["depends_on_shot_id"] or meta.get("after_shot_id"),
        "status": row["status"] or meta.get("plan_status"),
        "degraded_from_mode": row["degraded_from_mode"],
        "degraded_to_mode": row["degraded_to_mode"],
        "degraded_reason": row["degraded_reason"],
        "input_fingerprints": (
            json.loads(row["input_fingerprints_json"] or "{}")
            if row["input_fingerprints_json"] else meta.get("input_revision_fingerprints") or {}
        ),
        "reason_code": row["reason_code"],
        "reason_text": row["reason_text"],
        "stale": bool(meta.get("stale")),
        "stale_reason": meta.get("stale_reason"),
    }


class ProviderMediaPublicationService:
    """Publish project media through an explicitly configured, provider-readable URL."""

    @staticmethod
    def _assert_web_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("参考视频必须发布为可访问的 http(s) Web URL")
        host = parsed.hostname.lower()
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            raise ValueError("参考视频 URL 不能指向本机或局域网主机")
        addresses: set[str] = set()
        try:
            addresses.add(str(ipaddress.ip_address(host)))
        except ValueError:
            try:
                addresses.update(
                    item[4][0]
                    for item in socket.getaddrinfo(
                        host, parsed.port or (443 if parsed.scheme == "https" else 80),
                        type=socket.SOCK_STREAM,
                    )
                )
            except socket.gaierror as exc:
                raise ValueError(f"参考视频 URL 主机无法解析：{host}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            ):
                raise ValueError("参考视频 URL 不能指向私网、链路本地或保留地址")

    @staticmethod
    async def _check_accessible(url: str) -> None:
        timeout = httpx.Timeout(connect=10, read=20, write=10, pool=10)
        current = url
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _hop in range(6):
                ProviderMediaPublicationService._assert_web_url(current)
                response = await client.get(
                    current, headers={"Range": "bytes=0-1"},
                )
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("参考视频重定向缺少 Location")
                    current = urljoin(current, location)
                    continue
                if response.status_code not in {200, 206}:
                    raise ValueError(
                        f"参考视频 URL 不可读取（HTTP {response.status_code}）"
                    )
                return
        raise ValueError("参考视频 URL 重定向次数过多")

    @staticmethod
    async def _remote_metadata(url: str) -> dict[str, Any]:
        try:
            limit = int(get_setting("provider_media_max_download_bytes") or 512 * 1024 * 1024)
        except (TypeError, ValueError):
            limit = 512 * 1024 * 1024
        digest = hashlib.sha256()
        size = 0
        mime = "application/octet-stream"
        timeout = httpx.Timeout(connect=10, read=120, write=10, pool=10)
        current = url
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _hop in range(6):
                ProviderMediaPublicationService._assert_web_url(current)
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("参考视频重定向缺少 Location")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise ValueError(
                            f"参考视频内容不可完整读取（HTTP {response.status_code}）"
                        )
                    mime = response.headers.get("content-type", mime).split(";", 1)[0]
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("参考视频超过媒体发布服务允许的大小")
                        digest.update(chunk)
                    return {
                        "sha256": digest.hexdigest(),
                        "size_bytes": size,
                        "mime": mime,
                    }
        raise ValueError("参考视频 URL 重定向次数过多")

    @staticmethod
    def _media_metadata(path: Path) -> dict[str, Any]:
        raw = path.read_bytes()
        metadata: dict[str, Any] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "format=duration:stream=width,height,codec_name",
                    "-of", "json", str(path),
                ],
                capture_output=True, text=True, timeout=30, check=True,
            )
            probe = json.loads(result.stdout or "{}")
            metadata["duration_s"] = float((probe.get("format") or {}).get("duration") or 0)
            video = next(
                (item for item in probe.get("streams") or [] if item.get("width")),
                {},
            )
            metadata["width"] = video.get("width")
            metadata["height"] = video.get("height")
            metadata["codec"] = video.get("codec_name")
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
        return metadata

    async def publish(
        self,
        *,
        source_revision_id: str,
        source_url: str | None = None,
        local_path: str | None = None,
        expires_at: float | None = None,
        conn=None,
    ) -> dict[str, Any]:
        db = conn or get_conn()
        if not str(source_revision_id or "").strip():
            raise ValueError("媒体发布必须绑定非空 source_revision_id")
        metadata: dict[str, Any] = {}
        if source_url:
            url = source_url.strip()
            await self._check_accessible(url)
            metadata = await self._remote_metadata(url)
            sha = metadata["sha256"]
            mime = metadata["mime"]
        elif local_path:
            path = Path(local_path).resolve()
            if not path.is_file():
                raise ValueError("待发布媒体文件不存在")
            metadata = self._media_metadata(path)
            public_base = (get_setting("provider_media_public_base_url") or "").strip().rstrip("/")
            projects_root = Path(get_setting("projects_dir") or "").resolve() if get_setting("projects_dir") else None
            if not public_base:
                raise ValueError(
                    "本地参考视频尚未配置供应商可访问的对象存储或 provider_media_public_base_url"
                )
            if projects_root and path.is_relative_to(projects_root):
                relative = path.relative_to(projects_root)
            else:
                from app.config import PROJECTS_DIR
                try:
                    relative = path.relative_to(PROJECTS_DIR.resolve())
                except ValueError as exc:
                    raise ValueError("本地媒体不在项目媒体目录，禁止匿名外传") from exc
            url = f"{public_base}/{quote(relative.as_posix(), safe='/')}"
            await self._check_accessible(url)
            sha = metadata["sha256"]
            mime = metadata["mime"]
        else:
            raise ValueError("source_url 与 local_path 至少提供一项")
        publication_id = new_id("pmp")
        expiry = float(expires_at or now() + 6 * 3600)
        if expiry <= now() + 1800:
            raise ValueError("媒体 URL 有效期不足，必须覆盖排队和生成窗口")
        db.execute(
            """INSERT INTO provider_media_publications(
                   id,source_revision_id,source_url,local_path,published_url,
                   sha256,mime,duration_s,width,height,url_expires_at,status,
                   metadata_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                publication_id, source_revision_id, source_url, local_path, url,
                sha, mime, metadata.get("duration_s"), metadata.get("width"),
                metadata.get("height"), expiry, "ready", _json(metadata), now(), now(),
            ),
        )
        if conn is None:
            db.commit()
        return {
            "id": publication_id,
            "source_revision_id": source_revision_id,
            "published_url": url,
            "sha256": sha,
            "mime": mime,
            "url_expires_at": expiry,
            **metadata,
        }


__all__ = [
    "AssetSource",
    "EpisodeVideoGenerationPlan",
    "PlanAssetRequirement",
    "ProviderMediaPublicationService",
    "ProviderVideoCapabilitySnapshot",
    "ShotRelations",
    "ShotVideoGenerationPlan",
    "VideoGenerationMode",
    "VideoInputIntent",
    "VideoPlanValidationError",
    "active_plan_is_current",
    "assert_video_provider_submission_authority",
    "capability_allows",
    "capability_snapshot_by_id",
    "create_local_replan_revision",
    "current_capability_snapshot",
    "generate_episode_plan",
    "get_shot_plan",
    "load_plan_by_id",
    "load_latest_plan",
    "mode_audit_for_job",
    "publish_plan",
    "reconcile_adopted_revision",
    "record_mode_attempt",
    "save_capability_snapshot",
    "validate_episode_plan",
]
