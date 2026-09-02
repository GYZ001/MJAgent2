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
``video_plan.X`` call site across the repo must keep working unmodified.
Every symbol is re-exported from its **true source** exactly once: symbols
defined inside this package are exported from the submodule that actually
defines them; symbols from other modules (``app.db``) are imported directly
from there, not borrowed via a submodule that happened to import them too.
Plain stdlib/typing/pydantic names (``Any``, ``BaseModel``, ``Enum``,
``Field``, ``Literal``, ``Path``, ``ValidationError``, ``hashlib``,
``ipaddress``, ``json``, ``mimetypes``, ``model_validator``,
``urllib.parse.quote/urljoin/urlparse``, ``socket``, ``subprocess``,
``time``) are no longer re-exported as package attributes -- they are
submodule implementation details, not this package's public API, and a
repo-wide grep found no ``video_plan.<name>`` reads or monkeypatch targets
depending on them. ``httpx`` is kept (as a plain ``import httpx as httpx``
of the real shared module singleton, not borrowed from a submodule) because
``tests/test_video_plan_monkeypatch_guard.py`` names ``video_plan.httpx`` as
the one exempted "attribute on a shared singleton" pattern.
Uses ``name as name`` PEP 484 explicit-re-export form throughout
(``from .x import *`` is forbidden by the ``star_import`` gate in
``app/FILE_CONVENTIONS.toml``). The ``__all__`` list at the bottom is
unchanged from the pre-split file verbatim (it was already a curated subset,
not every re-exported name -- see the many external call sites that import
names outside it, e.g. ``current_storyboard_release_manifest``). Add new
video-plan logic to the concern-matching submodule, not back into this file.
"""
from __future__ import annotations

import httpx as httpx

from app.db import (
    get_conn as get_conn,
    get_setting as get_setting,
    log_provider_call as log_provider_call,
    new_id as new_id,
    now as now,
)

from .capability_snapshot import (
    _snapshot_from_row as _snapshot_from_row,
    capability_allows as capability_allows,
    capability_snapshot_by_id as capability_snapshot_by_id,
    current_capability_snapshot as current_capability_snapshot,
    failed_minimax_h3_snapshot as failed_minimax_h3_snapshot,
    minimax_h3_snapshot_from_probe as minimax_h3_snapshot_from_probe,
    minimax_h3_snapshot_matches_runtime as minimax_h3_snapshot_matches_runtime,
    record_minimax_h3_probe_snapshot as record_minimax_h3_probe_snapshot,
    save_capability_snapshot as save_capability_snapshot,
    video_plan_provider_selection_is_current as video_plan_provider_selection_is_current,
)
from .generate import (
    generate_episode_plan as generate_episode_plan,
)
from .mode_attempt import (
    active_plan_is_current as active_plan_is_current,
    get_shot_plan as get_shot_plan,
    mode_audit_for_job as mode_audit_for_job,
    record_mode_attempt as record_mode_attempt,
)
from .models import (
    AssetSource as AssetSource,
    EpisodeVideoGenerationPlan as EpisodeVideoGenerationPlan,
    PlanAssetRequirement as PlanAssetRequirement,
    PlannerShotAnalysis as PlannerShotAnalysis,
    ProviderVideoCapabilitySnapshot as ProviderVideoCapabilitySnapshot,
    SHOT_RELATION_ENUM_CONTRACT as SHOT_RELATION_ENUM_CONTRACT,
    ShotRelations as ShotRelations,
    ShotVideoGenerationPlan as ShotVideoGenerationPlan,
    VideoGenerationMode as VideoGenerationMode,
    VideoInputIntent as VideoInputIntent,
)
from .normalize import (
    _RELATION_ALIASES as _RELATION_ALIASES,
    _is_scene_entry as _is_scene_entry,
    _scene_identity as _scene_identity,
    apply_scene_boundary_strategy as apply_scene_boundary_strategy,
    normalize_ai_shot_plan_candidate as normalize_ai_shot_plan_candidate,
)
from .primitives import (
    VideoPlanValidationError as VideoPlanValidationError,
    _hash as _hash,
    _json as _json,
    _row_value as _row_value,
)
from .publication_service import (
    ProviderMediaPublicationService as ProviderMediaPublicationService,
)
from .publish import (
    _load_plan_parent as _load_plan_parent,
    _shot_plan_from_row as _shot_plan_from_row,
    load_latest_plan as load_latest_plan,
    load_plan_by_id as load_plan_by_id,
    publish_plan as publish_plan,
)
from .release_manifest import (
    bind_plan_release_identity as bind_plan_release_identity,
    canonical_shot_contract_fingerprint as canonical_shot_contract_fingerprint,
    current_storyboard_release_manifest as current_storyboard_release_manifest,
    shot_video_execution_contract_fingerprint as shot_video_execution_contract_fingerprint,
)
from .replan import (
    _release_unsubmitted_paused_reservations_for_adopted_shot as _release_unsubmitted_paused_reservations_for_adopted_shot,
    create_local_replan_revision as create_local_replan_revision,
    reconcile_adopted_revision as reconcile_adopted_revision,
)
from .shot_row import (
    _shot_model_from_row as _shot_model_from_row,
    _shot_planner_payload as _shot_planner_payload,
)
from .staleness import (
    _mark_episode_video_plan_stale as _mark_episode_video_plan_stale,
    verify_episode_plan_is_current as verify_episode_plan_is_current,
)
from .submission_authority import (
    assert_video_provider_submission_authority as assert_video_provider_submission_authority,
)
from .validate import (
    validate_episode_plan as validate_episode_plan,
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
