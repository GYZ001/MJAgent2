"""Human one-watch calibration API and current-review evidence resolution."""
from __future__ import annotations

from typing import Any

from fastapi import Body, HTTPException

from app.db import get_conn, new_id
from app.evidence import repository as evidence_repository
from app.narrative_calibration import (
    DEFAULT_CROSS_CONTENT_DIMENSIONS,
    GLOBAL_CALIBRATION_SCOPE_ID,
    CalibrationContractError,
    HumanOneWatchFreeze,
    HumanOneWatchObservation,
    ModelTargetEstimate,
    build_calibration_report,
    persist_ai_one_watch_simulation_authority,
    persist_calibration_report,
    persist_human_one_watch_freeze,
    persist_human_one_watch_observation,
    require_current_calibration_authority,
)
from app.narrative_review import verify_persisted_narrative_review
from app.production.screenplay_authority import resolve_current_screenplay_authority
from app.schemas import NarrativeReviewReport, Storyboard

try:
    router
except NameError:  # pragma: no cover - direct module import compatibility
    from app.domain.common import router


def _current_calibration_review_bundle(
    episode_id: str,
) -> tuple[Any, Any, NarrativeReviewReport, dict[str, Any], list[str]]:
    """Resolve the exact current board/review pair used by human observation."""
    conn = get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise HTTPException(404, "剧集不存在")
    try:
        authority = resolve_current_screenplay_authority(
            episode_id,
            conn=conn,
            require_narrative=True,
        )
    except Exception as exc:
        raise HTTPException(409, f"当前已发布剧本权威链无效：{exc}") from exc
    row = conn.execute(
        """SELECT id FROM artifacts
           WHERE type='narrative_review_report'
             AND scope_type='episode' AND scope_id=?
             AND status NOT IN ('stale','rejected','superseded','needs_revision')
           ORDER BY version DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(409, "本集尚无可用于真人一次观看的冷观众审读报告")
    report_artifact = evidence_repository.get_artifact(str(row["id"]))
    if report_artifact is None:
        raise HTTPException(409, "当前冷观众审读报告不存在")
    try:
        report = NarrativeReviewReport.model_validate(
            report_artifact.get("content") or {}
        )
    except Exception as exc:
        raise HTTPException(409, f"当前冷观众审读报告无法解析：{exc}") from exc
    review_parent_artifacts = [
        evidence_repository.get_artifact(str(parent_id))
        for parent_id in (report_artifact.get("parent_artifact_ids") or [])
    ]
    review_inputs = [
        item
        for item in review_parent_artifacts
        if item is not None
        and item.get("type") == "storyboard_review_input"
        and item.get("status")
        not in {"stale", "rejected", "superseded", "needs_revision"}
    ]
    if len(review_inputs) != 1:
        raise HTTPException(409, "当前冷观众报告缺少唯一不可变分镜审读输入")
    try:
        board = Storyboard.model_validate(review_inputs[0].get("content") or {})
    except Exception as exc:
        raise HTTPException(409, f"当前分镜审读输入无法解析：{exc}") from exc
    artifact_ids = list(dict.fromkeys([
        str(report_artifact["id"]),
        *[
            str(item)
            for item in (report_artifact.get("parent_artifact_ids") or [])
            if str(item)
        ],
    ]))
    try:
        verified_id = verify_persisted_narrative_review(
            episode_id=episode_id,
            screenplay=authority.screenplay,
            board=board,
            report=report,
            artifact_ids=artifact_ids,
        )
    except Exception as exc:
        raise HTTPException(409, f"当前冷观众审读证据链已失效：{exc}") from exc
    if verified_id != report_artifact["id"]:
        raise HTTPException(409, "当前冷观众审读报告指针漂移")
    return authority.screenplay, board, report, report_artifact, artifact_ids


def _human_observation_artifacts(
    *,
    scope_id: str,
    review_artifact_id: str,
) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        """SELECT id FROM artifacts
           WHERE type='human_one_watch_observation'
             AND scope_type='episode' AND scope_id=?
             AND status NOT IN ('stale','rejected','superseded','needs_revision')
           ORDER BY version,id""",
        (scope_id,),
    ).fetchall()
    artifacts: list[dict[str, Any]] = []
    for row in rows:
        artifact = evidence_repository.get_artifact(str(row["id"]))
        if artifact is None:
            continue
        content = artifact.get("content") or {}
        if content.get("narrative_review_artifact_id") != review_artifact_id:
            continue
        artifacts.append(artifact)
    return artifacts


def _target_contract_for_prior(screenplay, audience_prior_id: str) -> list[dict[str, Any]]:
    plan = screenplay.narrative_plan
    assert plan is not None
    return [
        {
            "experience_intent_id": intent.experience_intent_id,
            "audience_prior_id": path.audience_prior_id,
            "target_delta_id": delta.target_delta_id,
            "dimension": delta.dimension,
            "custom_dimension": delta.custom_dimension,
            "description": delta.description,
            "deadline_event_id": delta.deadline_event_id,
        }
        for intent in plan.experience_intents
        for path in intent.audience_paths
        if path.audience_prior_id == audience_prior_id
        for delta in path.target_deltas
    ]


@router.get("/narrative-calibration")
def narrative_calibration_status():
    try:
        authority = require_current_calibration_authority()
    except CalibrationContractError as exc:
        row = get_conn().execute(
            """SELECT id FROM artifacts
               WHERE type IN (
                   'human_one_watch_calibration_report',
                   'ai_one_watch_simulation_report'
               )
                 AND scope_type='calibration' AND scope_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (GLOBAL_CALIBRATION_SCOPE_ID,),
        ).fetchone()
        latest = (
            evidence_repository.get_artifact(str(row["id"]))
            if row is not None else None
        )
        return {
            "status": "needs_review",
            "ready": False,
            "blockers": exc.errors,
            "latest_artifact_id": latest.get("id") if latest else None,
            "latest_report": latest.get("content") if latest else None,
        }
    return {
        "status": "calibrated",
        "ready": True,
        "artifact_id": authority.artifact_id,
        "artifact_hash": authority.artifact_hash,
        "authority_mode": authority.authority_mode,
        "model_pass_threshold": authority.model_pass_threshold,
        "report": authority.report.model_dump(mode="json"),
    }


@router.post("/episodes/{episode_id}/narrative-calibration/ai-simulate")
def activate_ai_one_watch_simulation(episode_id: str):
    conn = get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise HTTPException(404, "剧集不存在")
    try:
        screenplay_authority = resolve_current_screenplay_authority(
            episode_id,
            conn=conn,
            require_narrative=True,
        )
    except Exception as exc:
        raise HTTPException(409, f"当前已发布剧本权威链无效：{exc}") from exc
    from app.domain.storyboard_ops import (
        _board_from_shot_rows,
        _ensure_current_storyboard_shot_artifacts,
    )
    from app.narrative_review import (
        NarrativeReviewError,
        rebind_unchanged_narrative_review,
    )
    from app.storyboard_workspace import repair_generated_source_bindings

    source_repair = repair_generated_source_bindings(episode_id)
    if source_repair["unresolved_shot_nos"]:
        raise HTTPException(422, {
            "code": "storyboard_source_evidence_unresolved",
            "message": "以下镜头仍无法绑定授权原文",
            "shot_nos": source_repair["unresolved_shot_nos"],
        })
    shot_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    board = _board_from_shot_rows(
        shot_rows,
        int(episode["episode_no"]),
    )
    _ensure_current_storyboard_shot_artifacts(
        conn,
        episode_id,
        board,
    )

    report = None
    report_artifact = None
    artifact_ids: list[str] = []
    rows = conn.execute(
        """SELECT id FROM artifacts
           WHERE type='narrative_review_report'
             AND scope_type='episode' AND scope_id=?
           ORDER BY version DESC""",
        (episode_id,),
    ).fetchall()
    for row in rows:
        candidate = evidence_repository.get_artifact(str(row["id"]))
        if candidate is None:
            continue
        try:
            parsed = NarrativeReviewReport.model_validate(
                candidate.get("content") or {}
            )
        except Exception:
            continue
        if parsed.decision != "pass":
            continue
        gate = conn.execute(
            """SELECT 1 FROM evaluations
               WHERE artifact_id=?
                 AND evaluator_name='narrative_blind_comparator'
                 AND evaluation_role='runtime_gate'
                 AND runtime_blocking=1
                 AND status='passed' AND hard_gate_passed=1
               LIMIT 1""",
            (candidate["id"],),
        ).fetchone()
        if gate is None:
            continue
        report = parsed
        report_artifact = candidate
        artifact_ids = list(dict.fromkeys([
            str(candidate["id"]),
            *[
                str(item)
                for item in (candidate.get("parent_artifact_ids") or [])
                if str(item)
            ],
        ]))
        break
    if report is None or report_artifact is None:
        raise HTTPException(409, "本集尚无可复用的已通过冷观众审读报告")

    try:
        verified_report_id = verify_persisted_narrative_review(
            episode_id=episode_id,
            screenplay=screenplay_authority.screenplay,
            board=board,
            report=report,
            artifact_ids=artifact_ids,
        )
    except NarrativeReviewError:
        try:
            report, artifact_ids = rebind_unchanged_narrative_review(
                episode_id=episode_id,
                screenplay=screenplay_authority.screenplay,
                board=board,
                report=report,
                artifact_ids=artifact_ids,
            )
        except NarrativeReviewError as exc:
            raise HTTPException(409, {
                "code": "narrative_review_rebind_failed",
                "message": "当前分镜感知输入已变化，不能复用旧审读",
                "errors": exc.errors,
            }) from exc
        verified_report_id = next(
            artifact_id
            for artifact_id in artifact_ids
            if (
                (candidate := evidence_repository.get_artifact(artifact_id))
                is not None
                and candidate.get("type") == "narrative_review_report"
            )
        )
        report_artifact = evidence_repository.get_artifact(verified_report_id)
        assert report_artifact is not None
    try:
        current = require_current_calibration_authority()
        if current.authority_mode in {"ai_simulation", "waived"}:
            failed = [
                item.target_delta_id
                for item in report.target_delta_results
                if item.predicted_score is None
                or float(item.predicted_score) < current.model_pass_threshold
            ]
            if not failed:
                return {
                    "activated": True,
                    "reused": True,
                    "artifact_id": current.artifact_id,
                    "authority_mode": current.authority_mode,
                    "model_pass_threshold": current.model_pass_threshold,
                    "message": "当前 AI 一次观看模拟权威仍有效",
                }
    except CalibrationContractError:
        pass
    try:
        artifact = persist_ai_one_watch_simulation_authority(
            report,
            narrative_review_artifact_id=verified_report_id,
        )
        authority = require_current_calibration_authority(
            expected_artifact_id=str(artifact["id"]),
        )
    except CalibrationContractError as exc:
        raise HTTPException(422, {
            "code": "ai_one_watch_simulation_invalid",
            "message": str(exc),
            "errors": exc.errors,
        }) from exc
    return {
        "activated": True,
        "reused": False,
        "artifact_id": authority.artifact_id,
        "authority_mode": authority.authority_mode,
        "model_pass_threshold": authority.model_pass_threshold,
        "minimum_predicted_score": authority.report.sample_summary.get(
            "minimum_predicted_score"
        ),
        "message": (
            "AI 一次观看模拟已通过并激活"
            if authority.authority_mode == "ai_simulation"
            else "AI 模拟不足，额外校准层已取消；冷观众硬门禁继续生效"
        ),
    }


@router.get("/episodes/{episode_id}/narrative-calibration/protocol")
def narrative_calibration_protocol(episode_id: str):
    screenplay, _board, report, report_artifact, _ids = (
        _current_calibration_review_bundle(episode_id)
    )
    plan = screenplay.narrative_plan
    assert plan is not None
    observations = _human_observation_artifacts(
        scope_id=plan.scope_id,
        review_artifact_id=str(report_artifact["id"]),
    )
    counts: dict[str, int] = {}
    for artifact in observations:
        prior_id = str((artifact.get("content") or {}).get("audience_prior_id") or "")
        counts[prior_id] = counts.get(prior_id, 0) + 1
    return {
        "episode_id": episode_id,
        "scope_id": plan.scope_id,
        "narrative_review_artifact_id": report_artifact["id"],
        "review_decision": report.decision,
        "audience_priors": [
            {
                "audience_prior_id": prior.audience_prior_id,
                "audience_description": prior.audience_description,
                "existing_observation_count": counts.get(
                    prior.audience_prior_id, 0,
                ),
            }
            for prior in plan.audience_priors
        ],
        "required_dimension_axes": list(DEFAULT_CROSS_CONTENT_DIMENSIONS),
        "protocol": {
            "watch_once": True,
            "replay_or_seek_forbidden": True,
            "source_and_targets_hidden_before_freeze": True,
            "first_pass_must_be_frozen": True,
        },
    }


@router.post("/episodes/{episode_id}/narrative-calibration/freeze")
def freeze_human_one_watch(
    episode_id: str,
    body: dict | None = Body(None),
):
    payload = body if isinstance(body, dict) else {}
    screenplay, _board, _report, report_artifact, _ids = (
        _current_calibration_review_bundle(episode_id)
    )
    plan = screenplay.narrative_plan
    assert plan is not None
    try:
        freeze = HumanOneWatchFreeze.model_validate({
            **payload,
            "observation_id": str(
                payload.get("observation_id") or new_id("humanwatch")
            ),
            "scope_id": plan.scope_id,
            "narrative_review_artifact_id": report_artifact["id"],
        })
        artifact = persist_human_one_watch_freeze(
            freeze,
            screenplay=screenplay,
            narrative_review_artifact_ids=[report_artifact["id"]],
        )
    except (CalibrationContractError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "freeze_artifact_id": artifact["id"],
        "observation_id": freeze.observation_id,
        "audience_prior_id": freeze.audience_prior_id,
        "target_contract": _target_contract_for_prior(
            screenplay,
            freeze.audience_prior_id,
        ),
        "message": "首次自由复述已冻结，现在可进行中性追问与逐目标观察记录",
    }


@router.post("/episodes/{episode_id}/narrative-calibration/observations")
def finalize_human_one_watch(
    episode_id: str,
    body: dict | None = Body(None),
):
    payload = body if isinstance(body, dict) else {}
    freeze_artifact_id = str(payload.get("freeze_artifact_id") or "")
    freeze_artifact = evidence_repository.get_artifact(freeze_artifact_id)
    if freeze_artifact is None:
        raise HTTPException(422, "首轮冻结 Artifact 不存在")
    try:
        freeze = HumanOneWatchFreeze.model_validate(
            freeze_artifact.get("content") or {}
        )
    except Exception as exc:
        raise HTTPException(422, f"首轮冻结 Artifact 无效：{exc}") from exc
    screenplay, _board, _report, report_artifact, _ids = (
        _current_calibration_review_bundle(episode_id)
    )
    if freeze.narrative_review_artifact_id != report_artifact["id"]:
        raise HTTPException(409, "首轮冻结绑定的审读版本已失效，请重新观看")
    try:
        observation = HumanOneWatchObservation.model_validate({
            **freeze.model_dump(mode="json"),
            "neutral_followup_observations": list(
                payload.get("neutral_followup_observations") or []
            ),
            "target_delta_observations": list(
                payload.get("target_delta_observations") or []
            ),
            "confidence": payload.get("confidence", freeze.confidence),
        })
        artifact = persist_human_one_watch_observation(
            observation,
            screenplay=screenplay,
            narrative_review_artifact_ids=[report_artifact["id"]],
            frozen_recall_artifact_id=freeze_artifact_id,
        )
    except (CalibrationContractError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "artifact_id": artifact["id"],
        "observation_id": observation.observation_id,
        "status": "validated",
        "message": "真人一次观看观察已纳入校准样本",
    }


@router.post("/narrative-calibration/rebuild")
def rebuild_narrative_calibration(body: dict | None = Body(None)):
    payload = body if isinstance(body, dict) else {}
    episode_ids = list(dict.fromkeys(
        str(item) for item in (payload.get("episode_ids") or []) if str(item)
    ))
    if not episode_ids:
        episode_ids = [
            str(row["id"])
            for row in get_conn().execute(
                """SELECT DISTINCT e.id
                     FROM episodes e
                     JOIN artifacts a
                       ON a.type='human_one_watch_observation'
                      AND a.scope_type='episode'
                      AND a.scope_id=e.id
                      AND a.status NOT IN
                          ('stale','rejected','superseded','needs_revision')
                    ORDER BY e.id"""
            ).fetchall()
        ]
    if len(episode_ids) < 2:
        raise HTTPException(422, "跨作品校准至少需要两集不同内容")
    requested_axes = list(dict.fromkeys(
        str(item).strip()
        for item in (
            payload.get("required_dimension_axes")
            or DEFAULT_CROSS_CONTENT_DIMENSIONS
        )
        if str(item).strip()
    ))
    if not set(DEFAULT_CROSS_CONTENT_DIMENSIONS).issubset(requested_axes):
        raise HTTPException(422, "校准必须覆盖 genre 与 form 两个跨内容维度")

    screenplays = []
    observations = []
    model_estimates = []
    observation_artifact_ids: list[str] = []
    review_artifact_ids: list[str] = []
    for episode_id in episode_ids:
        screenplay, _board, report, report_artifact, _ids = (
            _current_calibration_review_bundle(episode_id)
        )
        plan = screenplay.narrative_plan
        assert plan is not None
        screenplays.append(screenplay)
        review_artifact_id = str(report_artifact["id"])
        review_artifact_ids.append(review_artifact_id)
        artifacts = _human_observation_artifacts(
            scope_id=plan.scope_id,
            review_artifact_id=review_artifact_id,
        )
        for artifact in artifacts:
            try:
                observation = HumanOneWatchObservation.model_validate(
                    artifact.get("content") or {}
                )
            except Exception as exc:
                raise HTTPException(
                    409, f"真人观察 Artifact {artifact['id']} 无效：{exc}",
                ) from exc
            observations.append(observation)
            observation_artifact_ids.append(str(artifact["id"]))
        for result in report.target_delta_results:
            if result.predicted_score is None:
                raise HTTPException(
                    409,
                    f"审读报告 {review_artifact_id} 缺少逐目标 predicted_score，需重新盲审",
                )
            model_estimates.append(ModelTargetEstimate(
                scope_id=plan.scope_id,
                audience_prior_id=result.audience_prior_id,
                target_delta_id=result.target_delta_id,
                predicted_score=float(result.predicted_score),
                narrative_review_artifact_id=review_artifact_id,
                estimate_context={
                    "review_decision": report.decision,
                    "result": result.result,
                },
            ))
    try:
        report = build_calibration_report(
            calibration_report_id=new_id("calibration"),
            calibration_scope_id=GLOBAL_CALIBRATION_SCOPE_ID,
            screenplays=screenplays,
            observations=observations,
            model_estimates=model_estimates,
            required_dimension_axes=requested_axes,
        )
        activation_fingerprint = evidence_repository.content_hash(
            report.model_dump(mode="json"),
        )
        artifact = None
        if payload.get("activate") is True:
            if str(payload.get("expected_report_fingerprint") or "") != (
                activation_fingerprint
            ):
                raise CalibrationContractError([
                    "[CALIBRATION_ACTIVATION_PREVIEW_STALE] "
                    "激活前必须提交当前校准预览指纹；样本变化后需重新预览"
                ])
            artifact = persist_calibration_report(
                report,
                observation_artifact_ids=observation_artifact_ids,
                narrative_review_artifact_ids=review_artifact_ids,
            )
    except CalibrationContractError as exc:
        raise HTTPException(422, {
            "code": "narrative_calibration_invalid",
            "message": str(exc),
            "errors": exc.errors,
        }) from exc
    return {
        "activated": bool(
            artifact is not None and artifact.get("status") == "approved"
        ),
        "artifact_id": artifact.get("id") if artifact else None,
        "activation_fingerprint": activation_fingerprint,
        "report": report.model_dump(mode="json"),
    }
