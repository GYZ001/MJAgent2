"""Deterministic execution-contract validation for one episode video plan.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale). This file
holds exactly one function -- ``validate_episode_plan`` is a single
~350-line function in the pre-split source; splitting it further would
change its control flow, so it is moved whole (see the ``function_lines``
baseline entry for this file in ``app/FILE_CONVENTIONS.toml``).
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_setting

from .models import (
    AssetSource,
    EpisodeVideoGenerationPlan,
    ProviderVideoCapabilitySnapshot,
    ShotVideoGenerationPlan,
    VideoGenerationMode,
)
from .prev_frame_reference import prev_frame_reference_enabled
from .primitives import VideoPlanValidationError, _row_value
from .release_manifest import canonical_shot_contract_fingerprint
from .capability_snapshot import capability_allows


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

    # 2026-08-26: this used to be a scene-entry/continuation dichotomy loop
    # (_is_scene_entry() -> REFERENCE_IMAGE_MODE else required FIRST_FRAME_MODE
    # with a previous-adopted-tail boundary, checked here via
    # SCENE_ENTRY_MODE_MISMATCH/IN_SCENE_MODE_MISMATCH/SCENE_BOUNDARY_*).  Removed
    # along with apply_scene_boundary_strategy's FIRST_FRAME_MODE production (see
    # its docstring): that was the *only* producer of FIRST_FRAME_MODE on the
    # active generation path, and this loop applied to every shot regardless of
    # mode, so it would have wrongly rejected any legitimate VIDEO_INPUT_MODE or
    # FIRST_LAST_FRAME_MODE shot at a "continuation" position too (caught by this
    # file's own test suite: a VIDEO_INPUT_MODE fixture shot at index 1 with no
    # explicit scene identity started failing SCENE_ENTRY_MODE_MISMATCH once this
    # loop was broadened to "every shot must be REFERENCE_IMAGE_MODE" instead of
    # deleted outright). Per-mode structural correctness (including for
    # FIRST_FRAME_MODE shots that can still arrive via manual override or the
    # legacy narrative_authority_required=True path -- enqueue.py:1486-1497 is
    # unchanged) is still enforced below by the mode dispatch
    # (``if item.mode == REFERENCE_IMAGE_MODE: ... elif ... FIRST_FRAME_MODE: ...
    # elif ... FIRST_LAST_FRAME_MODE: ... elif ... VIDEO_INPUT_MODE: ...``); the
    # first-shot invariant (index 0 must be REFERENCE_IMAGE_MODE, no dependency)
    # is enforced separately just above by FIRST_SHOT_NO_PREDECESSOR /
    # FIRST_SHOT_HAS_DEPENDENCY.

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
            if item.video_input_intent is not None or (item.depends_on_shot_id and not prev_frame_reference_enabled()):
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

    # 金额不再构成生成拦截（会员分档时长制，非按金额计费）：这里原本用
    # app.video_cost_model（现只剩 initial_shot_generation_cost 一个纯记账
    # 函数，供 enqueue.py/system_api.py 算预留金额，未整文件删除）重算
    # estimated_cost/max_cost 覆盖 AI 生成阶段给出的估算值——那只是一个不再
    # 有下游消费者的记账数字，见 CLAUDE.md「Retiring Features」与本次
    # 「成本预算拦截体系退场」。estimated_cost/max_cost 字段本身已从
    # ShotVideoGenerationPlan/EpisodeVideoGenerationPlan 删除。

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
