"""Provider video capability probing and snapshot persistence.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale).
``minimax_h3_snapshot_from_probe`` / ``failed_minimax_h3_snapshot`` /
``minimax_h3_snapshot_matches_runtime`` are the three functions
``app.minimax_h3`` (L3 provider adapter) imports from this package; see the
package docstring for the resulting upward layering edge this file isolates.
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, new_id, now

from .models import (
    EpisodeVideoGenerationPlan,
    ProviderVideoCapabilitySnapshot,
    VideoGenerationMode,
    VideoInputIntent,
)
from .primitives import _json


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
