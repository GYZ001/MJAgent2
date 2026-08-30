"""Versioned three-mode video planning and deterministic execution contracts.

``app/video_plan.py`` (3,406 lines / 52 top-level defs) was one file until it
was split by concern into this package: Pydantic models/enums, shared
canonical-JSON/hash/row-access primitives, AI shot-plan normalization,
DB-row-to-typed-shot builders, the immutable storyboard release manifest,
provider capability probing/snapshot persistence, plan validation, AI-assisted
plan generation, publish/load, staleness verification, provider-submission
authority, per-shot mode-attempt bookkeeping, local replan/reconciliation, and
provider-media publication.

This file is the sole stable entry point: every existing
``from app.video_plan import X`` / ``import app.video_plan`` /
``video_plan.X`` call site across the repo must keep working unmodified --
every symbol (including every plain stdlib/third-party name the original file
imported at module level, since those were reachable as
``video_plan.<name>`` too) is explicitly re-exported below using the
``name as name`` PEP 484 explicit-re-export form, matching the precedent set
by ``app/validators/__init__.py`` and ``app/screenplay_ir/__init__.py``
(``from .x import *`` is forbidden by the ``star_import`` gate in
``app/FILE_CONVENTIONS.toml``). The ``__all__`` list at the bottom is
unchanged from the pre-split file verbatim (it was already a curated subset,
not every re-exported name -- see the many external call sites that import
names outside it, e.g. ``current_storyboard_release_manifest``). Add new
video-plan logic to the concern-matching submodule, not back into this file.
"""
from __future__ import annotations

from .models import (
    Any as Any,
    AssetSource as AssetSource,
    BaseModel as BaseModel,
    Enum as Enum,
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    Field as Field,
    Literal as Literal,
    PlanAssetRequirement as PlanAssetRequirement,
    PlannerShotAnalysis as PlannerShotAnalysis,
    ProviderVideoCapabilitySnapshot as ProviderVideoCapabilitySnapshot,
    SHOT_RELATION_ENUM_CONTRACT as SHOT_RELATION_ENUM_CONTRACT,
    ShotRelations as ShotRelations,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    VideoGenerationMode as VideoGenerationMode,
    VideoInputIntent as VideoInputIntent,
    model_validator as model_validator,
    time as time,
)

from .primitives import (
    Any as Any,
    VideoPlanValidationError as VideoPlanValidationError,
    _hash as _hash,
    _json as _json,
    _row_value as _row_value,
    hashlib as hashlib,
    json as json,
)

from .normalize import (
    Any as Any,
    AssetSource as AssetSource,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    VideoGenerationMode as VideoGenerationMode,
    _RELATION_ALIASES as _RELATION_ALIASES,
    _is_scene_entry as _is_scene_entry,
    _json as _json,
    _row_value as _row_value,
    _scene_identity as _scene_identity,
    apply_scene_boundary_strategy as apply_scene_boundary_strategy,
    normalize_ai_shot_plan_candidate as normalize_ai_shot_plan_candidate,
)

from .shot_row import (
    Any as Any,
    _row_value as _row_value,
    _shot_model_from_row as _shot_model_from_row,
    _shot_planner_payload as _shot_planner_payload,
    json as json,
)

from .release_manifest import (
    Any as Any,
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    _hash as _hash,
    _row_value as _row_value,
    _shot_model_from_row as _shot_model_from_row,
    authoritative_storyboard_plan_cost as authoritative_storyboard_plan_cost,
    bind_plan_release_identity as bind_plan_release_identity,
    canonical_shot_contract_fingerprint as canonical_shot_contract_fingerprint,
    current_storyboard_release_manifest as current_storyboard_release_manifest,
    get_conn as get_conn,
    json as json,
    shot_video_execution_contract_fingerprint as shot_video_execution_contract_fingerprint,
)

from .capability_snapshot import (
    Any as Any,
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    ProviderVideoCapabilitySnapshot as ProviderVideoCapabilitySnapshot,
    VideoGenerationMode as VideoGenerationMode,
    VideoInputIntent as VideoInputIntent,
    _json as _json,
    _snapshot_from_row as _snapshot_from_row,
    capability_allows as capability_allows,
    capability_snapshot_by_id as capability_snapshot_by_id,
    current_capability_snapshot as current_capability_snapshot,
    failed_minimax_h3_snapshot as failed_minimax_h3_snapshot,
    get_conn as get_conn,
    json as json,
    minimax_h3_snapshot_from_probe as minimax_h3_snapshot_from_probe,
    minimax_h3_snapshot_matches_runtime as minimax_h3_snapshot_matches_runtime,
    new_id as new_id,
    now as now,
    record_minimax_h3_probe_snapshot as record_minimax_h3_probe_snapshot,
    save_capability_snapshot as save_capability_snapshot,
    video_plan_provider_selection_is_current as video_plan_provider_selection_is_current,
)

from .validate import (
    Any as Any,
    AssetSource as AssetSource,
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    ProviderVideoCapabilitySnapshot as ProviderVideoCapabilitySnapshot,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    VideoGenerationMode as VideoGenerationMode,
    VideoPlanValidationError as VideoPlanValidationError,
    _row_value as _row_value,
    canonical_shot_contract_fingerprint as canonical_shot_contract_fingerprint,
    capability_allows as capability_allows,
    get_setting as get_setting,
    json as json,
    validate_episode_plan as validate_episode_plan,
)

from .generate import (
    Any as Any,
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    PlannerShotAnalysis as PlannerShotAnalysis,
    SHOT_RELATION_ENUM_CONTRACT as SHOT_RELATION_ENUM_CONTRACT,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    ValidationError as ValidationError,
    VideoGenerationMode as VideoGenerationMode,
    VideoPlanValidationError as VideoPlanValidationError,
    _hash as _hash,
    _json as _json,
    _scene_identity as _scene_identity,
    _shot_model_from_row as _shot_model_from_row,
    _shot_planner_payload as _shot_planner_payload,
    apply_scene_boundary_strategy as apply_scene_boundary_strategy,
    bind_plan_release_identity as bind_plan_release_identity,
    canonical_shot_contract_fingerprint as canonical_shot_contract_fingerprint,
    current_capability_snapshot as current_capability_snapshot,
    current_storyboard_release_manifest as current_storyboard_release_manifest,
    generate_episode_plan as generate_episode_plan,
    get_conn as get_conn,
    json as json,
    load_latest_plan as load_latest_plan,
    log_provider_call as log_provider_call,
    new_id as new_id,
    normalize_ai_shot_plan_candidate as normalize_ai_shot_plan_candidate,
    now as now,
    publish_plan as publish_plan,
    validate_episode_plan as validate_episode_plan,
)

from .publish import (
    Any as Any,
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    VideoGenerationMode as VideoGenerationMode,
    _json as _json,
    _load_plan_parent as _load_plan_parent,
    _row_value as _row_value,
    _shot_plan_from_row as _shot_plan_from_row,
    get_conn as get_conn,
    json as json,
    load_latest_plan as load_latest_plan,
    load_plan_by_id as load_plan_by_id,
    new_id as new_id,
    now as now,
    publish_plan as publish_plan,
)

from .staleness import (
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    VideoPlanValidationError as VideoPlanValidationError,
    _mark_episode_video_plan_stale as _mark_episode_video_plan_stale,
    capability_snapshot_by_id as capability_snapshot_by_id,
    current_storyboard_release_manifest as current_storyboard_release_manifest,
    get_conn as get_conn,
    now as now,
    validate_episode_plan as validate_episode_plan,
    verify_episode_plan_is_current as verify_episode_plan_is_current,
)

from .submission_authority import (
    Any as Any,
    ProviderVideoCapabilitySnapshot as ProviderVideoCapabilitySnapshot,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    VideoGenerationMode as VideoGenerationMode,
    VideoPlanValidationError as VideoPlanValidationError,
    _mark_episode_video_plan_stale as _mark_episode_video_plan_stale,
    active_plan_is_current as active_plan_is_current,
    assert_video_provider_submission_authority as assert_video_provider_submission_authority,
    capability_allows as capability_allows,
    capability_snapshot_by_id as capability_snapshot_by_id,
    current_capability_snapshot as current_capability_snapshot,
    get_conn as get_conn,
    load_latest_plan as load_latest_plan,
    verify_episode_plan_is_current as verify_episode_plan_is_current,
)

from .mode_attempt import (
    Any as Any,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    VideoGenerationMode as VideoGenerationMode,
    _shot_plan_from_row as _shot_plan_from_row,
    active_plan_is_current as active_plan_is_current,
    get_conn as get_conn,
    get_shot_plan as get_shot_plan,
    json as json,
    load_latest_plan as load_latest_plan,
    mode_audit_for_job as mode_audit_for_job,
    new_id as new_id,
    now as now,
    record_mode_attempt as record_mode_attempt,
    shot_video_execution_contract_fingerprint as shot_video_execution_contract_fingerprint,
    verify_episode_plan_is_current as verify_episode_plan_is_current,
)

from .replan import (
    Any as Any,
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    Path as Path,
    _hash as _hash,
    _json as _json,
    _release_unsubmitted_paused_reservations_for_adopted_shot as _release_unsubmitted_paused_reservations_for_adopted_shot,
    capability_snapshot_by_id as capability_snapshot_by_id,
    create_local_replan_revision as create_local_replan_revision,
    current_storyboard_release_manifest as current_storyboard_release_manifest,
    get_conn as get_conn,
    hashlib as hashlib,
    json as json,
    load_latest_plan as load_latest_plan,
    new_id as new_id,
    now as now,
    publish_plan as publish_plan,
    reconcile_adopted_revision as reconcile_adopted_revision,
    validate_episode_plan as validate_episode_plan,
    verify_episode_plan_is_current as verify_episode_plan_is_current,
)

from .publication_service import (
    Any as Any,
    Path as Path,
    ProviderMediaPublicationService as ProviderMediaPublicationService,
    _json as _json,
    get_conn as get_conn,
    get_setting as get_setting,
    hashlib as hashlib,
    httpx as httpx,
    ipaddress as ipaddress,
    json as json,
    mimetypes as mimetypes,
    new_id as new_id,
    now as now,
    quote as quote,
    socket as socket,
    subprocess as subprocess,
    urljoin as urljoin,
    urlparse as urlparse,
)

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
