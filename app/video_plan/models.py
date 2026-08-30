"""Video-plan Pydantic models: enums, planner/execution contracts.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale). Pure data
models with no dependency on any other file in this package.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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
