"""剧本生成的发起与已发布剧本二创复核后的续跑。

从 app/domain/screenplay_ops.py 按原样搬移；依赖 task_body/run_control/activation/status_snapshot。
"""
from __future__ import annotations

from app import errors, quota
from app.db import (
    get_conn,
    now,
)
from app.domain.common import (
    _as_body_dict,
    _episode_or_404,
    _episode_source_text,
    _project_bible_or_placeholder,
    _require_harness_engine,
    _screenplay_ready,
    router,
)
from fastapi import (
    Body,
    HTTPException,
)

from .activation import _spawn_screenplay_activation
from .run_control import _screenplay_task_active
from .status_snapshot import (
    _published_screenplay_revalidation_eligibility,
    _screenplay_production_state,
)
from .task_body import _new_screenplay_recorder


@router.post("/episodes/{episode_id}/screenplay")
async def start_screenplay(episode_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route
    body = _as_body_dict(body)
    routed = await ui_route(
        "screenplay.generate",
        {
            "episode_id": episode_id,
            "idempotency_key": body.get("idempotency_key"),
        },
    )
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中，不能同时重写剧本")
    if ep["screenplay_status"] in {"queued", "running", "repairing"} and _screenplay_task_active(episode_id):
        return {
            "status": ep["screenplay_status"],
            "run_id": ep["active_screenplay_run_id"],
            "mode": "repair" if ep["screenplay_status"] == "repairing" else "baseline",
            "deduplicated": True,
        }

    # 已有 published 产品时仍要求显式删除；未发布恢复统一由 resolver 决定。
    from app.production.revision import resolve_screenplay_resume_eligibility
    eligibility = resolve_screenplay_resume_eligibility(episode_id)
    published_id = None
    try:
        published_id = ep["published_screenplay_artifact_id"] if "published_screenplay_artifact_id" in ep.keys() else None
    except Exception:  # noqa: BLE001
        published_id = None
    has_product = bool(ep["screenplay_json"]) and ep["screenplay_status"] in {"ready", "repairing"}
    if has_product and (
        eligibility.revision_id
        or published_id
        or ep["screenplay_status"] == "ready"
    ):
        if ep["screenplay_status"] == "ready":
            raise HTTPException(
                409,
                "本集已有通过凭证的剧本；如需重新生成，请先删除当前剧本。",
            )
        # repairing → 续跑 Repair（不新建 Baseline）
        pass

    resume_existing = eligibility.resumable
    resume_mode = eligibility.mode if resume_existing else "baseline"
    try:
        recorder = _new_screenplay_recorder(
            episode_id,
            trigger_type="resume" if resume_existing else "manual",
            parent_run_id=ep["active_screenplay_run_id"] if resume_existing else None,
        )
        _spawn_screenplay_activation(
            episode_id,
            recorder,
            project_id=ep["project_id"],
            status="queued",
            message=(
                f"{eligibility.label}已排队，等待文本生成槽位"
                if resume_existing
                else "剧本任务已排队，等待文本生成槽位"
            ),
            expected_active_run_id=(
                ep["active_screenplay_run_id"]
                if resume_existing else None
            ),
            clear_unpublished_ir=not resume_existing,
            resume_eligibility=eligibility if resume_existing else None,
            authorize_blueprint_retry=bool(
                body.get("authorize_blueprint_retry")
            ),
            expected_blueprint_unknown_receipts=(
                body.get("expected_blueprint_unknown_receipts")
                if isinstance(
                    body.get("expected_blueprint_unknown_receipts"), list
                )
                else None
            ),
        )
    except quota.QuotaExceeded:
        # 配额超限的 429（tier/limit/upgrade_path 详情）必须原样透传给前端
        # ——不能被下面的通用异常处理糊成一个不带这些信息的 503
        # （CLAUDE.md「拦住用户时必须给出路」）。
        raise
    except Exception as exc:
        cause = errors.log_error(
            exc,
            action="screenplay_start_activation",
            context={
                "episode_id": episode_id,
                "run_id": getattr(locals().get("recorder"), "run_id", None),
                "resume_existing": resume_existing,
            },
        )
        raise HTTPException(503, {
            "code": "SCREENPLAY_START_FAILED",
            "message": "剧本任务未能启动，原状态已恢复，请重试",
            "action": "retry_resume" if resume_existing else "retry_generate",
            "cause_error_id": cause.error_id,
        }) from exc
    return {
        "status": "queued",
        "run_id": recorder.run_id,
        "mode": resume_mode,
    }

def _prepare_published_screenplay_revalidation(ep: dict):
    """Create a new revision that revalidates immutable published content."""
    from app.harness.contracts import get_contract
    from app.production.revision import (
        ensure_production_revision,
        mark_baseline_generated,
        save_checkpoint,
    )
    from app.production.screenplay_authority import (
        SCREENPLAY_QA_PROFILE_VERSION,
        screenplay_authority_fingerprint,
    )

    episode_id = str(ep["id"])
    conn = get_conn()
    eligibility = _published_screenplay_revalidation_eligibility(ep, conn=conn)
    if not eligibility["eligible"]:
        error = eligibility.get("error")
        raise HTTPException(409, {
            "code": eligibility["code"],
            "message": eligibility["message"],
            "artifact_id": eligibility["artifact_id"],
            "action": "refresh",
        }) from error
    artifact_id = str(eligibility["artifact_id"])
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    bible = _project_bible_or_placeholder(project)
    source_text = _episode_source_text(conn, ep)
    contract = get_contract("screenplay")
    input_fingerprint = screenplay_authority_fingerprint(
        episode_id,
        conn=conn,
        source_text=source_text,
        bible=bible,
        contract_version=contract.version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
    )
    revision = ensure_production_revision(
        episode_id=episode_id,
        kind="screenplay",
        input_fingerprint=input_fingerprint,
        contract_version=contract.version,
        qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        resume=False,
    )
    revision = mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact_id,
        working_artifact_id=artifact_id,
    )
    save_checkpoint(revision.id, {
        "phase": "REVALIDATING_PUBLISHED",
        "working_artifact_id": artifact_id,
        "source_revision_id": ep.get("screenplay_production_revision_id"),
        "yield_reason": "upstream_input_fingerprint_changed",
    })
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing',screenplay_error=?,"
        "screenplay_updated_at=? WHERE id=?",
        ("上游版本已变化，正在重新校验已发布剧本", now(), episode_id),
    )
    conn.commit()
    return revision

@router.post("/episodes/{episode_id}/screenplay/resume")
async def resume_screenplay(episode_id: str, body: dict | None = Body(None)):
    """Continue either pre-Document shards or post-Document validation."""
    from app.capabilities.dispatch import ui_route
    from app.production.revision import (
        get_active_production_revision,
        resolve_screenplay_resume_eligibility,
    )

    body = _as_body_dict(body)
    routed = await ui_route("screenplay.resume", {
        "episode_id": episode_id,
        "idempotency_key": body.get("idempotency_key"),
    })
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中，不能同时修复剧本")
    if _screenplay_task_active(episode_id):
        active_state = _screenplay_production_state(episode_id)
        return {
            "status": ep["screenplay_status"],
            "run_id": ep["active_screenplay_run_id"],
            "mode": active_state.get("mode") or active_state.get("operation"),
            "deduplicated": True,
        }
    rev = get_active_production_revision(episode_id, "screenplay")
    if (
        rev is None
        and ep["screenplay_status"] == "ready"
        and ep["published_screenplay_artifact_id"]
        and not _screenplay_ready(ep)
    ):
        rev = _prepare_published_screenplay_revalidation(dict(ep))
    eligibility = resolve_screenplay_resume_eligibility(
        episode_id,
        revision=rev,
    )
    if not rev or not eligibility.resumable:
        raise HTTPException(409, "没有可继续的首版检查点或完整剧本工作副本")
    resume_mode = eligibility.mode

    try:
        recorder = _new_screenplay_recorder(
            episode_id,
            trigger_type="resume",
            parent_run_id=ep["active_screenplay_run_id"],
        )
        _spawn_screenplay_activation(
            episode_id,
            recorder,
            project_id=ep["project_id"],
            status="queued",
            message=(
                f"{eligibility.label}已排队，等待文本生成槽位"
            ),
            expected_active_run_id=ep["active_screenplay_run_id"],
            resume_eligibility=eligibility,
        )
    except quota.QuotaExceeded:
        raise
    except Exception as exc:
        raise HTTPException(503, {
            "code": "SCREENPLAY_RESUME_FAILED",
            "message": "剧本后续阶段未能启动，工作副本和恢复点均已保留，请稍后重试",
            "action": "retry_resume",
        }) from exc
    return {
        "status": "queued",
        "run_id": recorder.run_id,
        "revision_id": (
            get_active_production_revision(episode_id, "screenplay").id
        ),
        "mode": resume_mode,
    }
