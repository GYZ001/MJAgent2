"""AI-assisted episode video-plan generation.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale). This file
holds exactly one function -- ``generate_episode_plan`` is a single
~645-line function in the pre-split source; splitting it further would
change its control flow, so it is moved whole (see the ``function_lines``
and ``line_count`` baseline entries for this file in
``app/FILE_CONVENTIONS.toml``).
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.video_plan.prev_frame_reference import prev_frame_reference_enabled, scene_chain_dependencies
from app.db import get_conn, log_provider_call, new_id, now

from .capability_snapshot import current_capability_snapshot
from .models import (
    EpisodeVideoGenerationPlan,
    PlannerShotAnalysis,
    SHOT_RELATION_ENUM_CONTRACT,
    ShotVideoGenerationPlan,
    VideoGenerationMode,
)
from .normalize import (
    _scene_identity,
    apply_scene_boundary_strategy,
    normalize_ai_shot_plan_candidate,
)
from .planner_contract import (
    DEPENDENCY_ENUM_CONTRACT,
    cached_window_is_valid,
    planner_output_contract,
    planner_system_prompt,
)
from .primitives import VideoPlanValidationError, _hash, _json
from .publish import load_latest_plan, publish_plan
from .release_manifest import (
    bind_plan_release_identity,
    canonical_shot_contract_fingerprint,
    current_storyboard_release_manifest,
)
from .shot_row import _shot_model_from_row, _shot_planner_payload
from .validate import validate_episode_plan


async def generate_episode_plan(
    episode_id: str,
    *,
    force: bool = False,
    conn=None,
    deterministic_only: bool = False,
) -> EpisodeVideoGenerationPlan:
    """Ask AI once for the whole episode, then publish only a deterministic-valid plan.

    ``deterministic_only=True`` skips the AI relation-classification call
    entirely and publishes an all-``REFERENCE_IMAGE_MODE`` plan straight from
    the shot rows (same shape as the pre-existing single-shot special case
    below, generalized to N shots). Real upstream-contract checks (shots
    exist, storyboard is published, per-shot asset resolution) still run and
    can still raise -- those are genuine preconditions, not AI self-report.

    Used by the quick "generate video" entry points
    (``app.domain.video_ops._generate_episode_core`` /
    ``_generate_shot_core``): the AI call's mode/dependency output is
    discarded unconditionally by ``app.media_exec.enqueue`` for every episode
    with ``narrative_authority_required=False`` -- 100% of episodes in the
    current dataset (verified: 0/2259 episodes carry a ``narrative_plan``) --
    so gating those user-facing actions on that call succeeding/parsing/being
    confident enough is pure risk with no corresponding benefit; three
    independent real failures this way (self-reported uncertainty, low
    confidence, an enum literal outside the closed Literal set for a genuine
    film term the model used) each blocked "generate all videos" or single-shot
    generate for episodes whose actual generation never consulted the plan's
    mode decision anyway. The AI-classified path stays intact and is still used
    by the explicit ``/video-generation-plan`` endpoints and by
    ``app.video_supervisor``/``app.completion_grant``, where a failed plan
    should continue to surface as a real, visible error.
    """
    from app import hiagent
    from app.harness import model_gateway
    from app.schemas import extract_json

    db = conn or get_conn()
    episode = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not episode:
        raise ValueError(f"分集不存在：{episode_id}")
    project = db.execute(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],),
    ).fetchone()
    from app.visual_styles import _project_bible_or_placeholder

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
    if deterministic_only:
        scene_chain = scene_chain_dependencies(list(rows)) if prev_frame_reference_enabled() else {}
        shot_plans = [
            ShotVideoGenerationPlan(
                shot_plan_id=new_id("svp"),
                episode_video_plan_id=plan_id,
                plan_revision=next_revision,
                source_storyboard_revision_id=revision_id,
                shot_id=str(row["id"]),
                published_shot_id=str(payload["shot_id"]),
                shot_no=index + 1,
                mode=VideoGenerationMode.REFERENCE_IMAGE_MODE,
                depends_on_shot_id=scene_chain.get(str(row["id"])),
                state_dependency="start_only" if scene_chain.get(str(row["id"])) else "none",
                reason_codes=(
                    ["FIRST_SHOT_NO_PREDECESSOR"] if index == 0
                    else ["DETERMINISTIC_REFERENCE_IMAGE_PLAN"]
                ),
                confidence=1.0,
                estimated_latency_ms=690_000,
                capability_snapshot_id=snapshot.id,
                input_revision_fingerprints={
                    "asset_revisions": asset_fingerprints.get(str(row["id"]), ""),
                },
            )
            for index, (row, payload) in enumerate(zip(rows, shot_payload))
        ]
        plan = EpisodeVideoGenerationPlan(
            episode_video_plan_id=plan_id,
            episode_id=episode_id,
            plan_revision=next_revision,
            source_storyboard_revision_id=revision_id,
            capability_snapshot_id=snapshot.id,
            planner_provider="deterministic",
            planner_model="reference-image-only-no-ai-call",
            planner_prompt_fingerprint=_hash({
                "shot_ids": [str(row["id"]) for row in rows],
                "capability_snapshot_id": snapshot.id,
                "reason": "quick_generation_entry_point_skips_ai_mode_planning",
            }),
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
    capability_payload = snapshot.model_dump(mode="json")
    prompt_payload = {
        "task": "plan_episode_video_generation_modes",
        "source_storyboard_revision_id": revision_id,
        "storyboard_release_manifest": release_manifest,
        "capability_snapshot": capability_payload,
        "relation_enum_contract": SHOT_RELATION_ENUM_CONTRACT,
        "dependency_enum_contract": DEPENDENCY_ENUM_CONTRACT,
        "shots": shot_payload,
    }
    # 提示词与输出契约同源于 planner_contract：合法值从 Pydantic 字面量派生，带语义的
    # 正面陈述在那边维护（见该模块 docstring 里 2026-09-03 的 end_only 事故）。
    system = planner_system_prompt()
    output_contract = planner_output_contract()
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
            if not cached_window_is_valid(response):
                # 台账里 status=OK 只说明供应商调用成功，不说明输出合法；同一份提示词
                # 会一直命中这条坏响应，用户重试也会原样失败——跳过它，重新问模型。
                response = None
                continue
            cached_call_id = int(cached["id"])
            break
        if response is None:
            response = await model_gateway.chat(
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
                timeout_s=7200,
                estimated_latency_ms=analysis.estimated_latency_ms,
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
