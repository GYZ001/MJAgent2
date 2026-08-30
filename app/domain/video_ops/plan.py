"""整集视频生成计划的创建、校验、对账、覆盖与执行。

从 app/domain/video_ops.py 按原样搬移；依赖 confirmation_gate。
"""
from __future__ import annotations

from app.db import (
    get_conn,
    new_id,
    now,
)
from app.domain.common import (
    _episode_or_404,
    router,
)
from app.domain.review_wall import _review_sha
from fastapi import HTTPException

from .confirmation_gate import _assert_storyboard_generation_gate


@router.post("/episodes/{episode_id}/video-generation-plan")
async def create_episode_video_generation_plan(
    episode_id: str,
    body: dict | None = None,
):
    _episode_or_404(episode_id)
    _assert_storyboard_generation_gate(episode_id)
    from app.video_plan import VideoPlanValidationError, generate_episode_plan

    try:
        plan = await generate_episode_plan(
            episode_id, force=bool((body or {}).get("force")),
        )
    except VideoPlanValidationError as exc:
        raise HTTPException(409, {
            "status": "BLOCKED_UPSTREAM_CONTRACT",
            "blockers": exc.issues,
        }) from exc
    return plan.model_dump(mode="json")

@router.get("/episodes/{episode_id}/video-generation-plan")
def get_episode_video_generation_plan(episode_id: str):
    _episode_or_404(episode_id)
    from app.video_plan import load_latest_plan

    plan = load_latest_plan(episode_id)
    if not plan:
        return None
    return plan.model_dump(mode="json")

@router.post("/episodes/{episode_id}/video-generation-plan/validate")
def validate_episode_video_generation_plan(episode_id: str):
    _episode_or_404(episode_id)
    from app.video_plan import (
        VideoPlanValidationError,
        capability_snapshot_by_id,
        current_storyboard_release_manifest,
        load_latest_plan,
        validate_episode_plan,
    )

    conn = get_conn()
    plan = load_latest_plan(episode_id, conn=conn)
    if not plan:
        raise HTTPException(404, "本集尚未生成视频模式计划")
    snapshot = capability_snapshot_by_id(plan.capability_snapshot_id, conn=conn)
    if not snapshot:
        raise HTTPException(409, "计划引用的供应商能力快照不存在")
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    try:
        release_manifest = current_storyboard_release_manifest(
            episode_id,
            conn=conn,
        )
        validate_episode_plan(
            plan,
            list(rows),
            snapshot,
            release_manifest=release_manifest,
        )
    except (ValueError, VideoPlanValidationError) as exc:
        blockers = (
            exc.issues
            if isinstance(exc, VideoPlanValidationError)
            else [{"code": "STORYBOARD_RELEASE_AUTHORITY_STALE", "message": str(exc)}]
        )
        raise HTTPException(409, {"valid": False, "blockers": blockers}) from exc
    return {"valid": True, "plan": plan.model_dump(mode="json")}

@router.post("/episodes/{episode_id}/video-generation-plan/reconcile")
def reconcile_episode_video_generation_plan(
    episode_id: str,
    body: dict | None = None,
):
    _episode_or_404(episode_id)
    from app.video_plan import reconcile_adopted_revision

    conn = get_conn()
    payload = body or {}
    shot_id = payload.get("shot_id")
    version_id = payload.get("adopted_version_id")
    if shot_id:
        row = conn.execute(
            "SELECT adopted_version_id FROM shots WHERE id=? AND episode_id=?",
            (shot_id, episode_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "镜头不存在")
        adopted = version_id or row["adopted_version_id"]
        if not adopted:
            raise HTTPException(409, "该镜头尚未采用视频")
        result = reconcile_adopted_revision(shot_id, adopted, conn=conn)
        conn.commit()
        return result
    results = []
    for row in conn.execute(
        """SELECT id,adopted_version_id FROM shots
           WHERE episode_id=? AND adopted_version_id IS NOT NULL ORDER BY shot_no""",
        (episode_id,),
    ).fetchall():
        results.append(reconcile_adopted_revision(
            row["id"], row["adopted_version_id"], conn=conn,
        ))
    conn.commit()
    return {
        "episode_id": episode_id,
        "bound": sum(item["bound"] for item in results),
        "stale_shot_ids": sorted({
            shot for item in results for shot in item["stale_shot_ids"]
        }),
    }

@router.post("/episodes/{episode_id}/video-generation-plan/override")
def override_episode_video_generation_plan(
    episode_id: str,
    body: dict | None = None,
):
    _episode_or_404(episode_id)
    from app.video_plan import (
        PlanAssetRequirement,
        VideoGenerationMode,
        VideoInputIntent,
        VideoPlanValidationError,
        capability_snapshot_by_id,
        current_storyboard_release_manifest,
        load_latest_plan,
        publish_plan,
        validate_episode_plan,
    )

    payload = body or {}
    if not payload.get("reason"):
        raise HTTPException(422, "人工覆盖必须填写原因")
    conn = get_conn()
    current = load_latest_plan(episode_id, conn=conn)
    if not current:
        raise HTTPException(404, "本集尚未生成视频模式计划")
    target = next(
        (item for item in current.shots if item.shot_id == payload.get("shot_id")),
        None,
    )
    if not target:
        raise HTTPException(404, "待覆盖镜头不属于当前计划")
    try:
        override_mode = VideoGenerationMode(payload.get("mode"))
        override_intent = (
            VideoInputIntent(payload["video_input_intent"])
            if payload.get("video_input_intent") else None
        )
        override_assets = [
            PlanAssetRequirement.model_validate(asset)
            for asset in (payload.get("required_assets") or [])
        ]
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"人工覆盖模式或素材合同无效：{exc}") from exc
    next_revision = int(conn.execute(
        "SELECT COALESCE(MAX(plan_revision),0)+1 n FROM episode_video_generation_plans WHERE episode_id=?",
        (episode_id,),
    ).fetchone()["n"])
    replacement = current.model_copy(deep=True)
    replacement.episode_video_plan_id = new_id("evp")
    replacement.plan_revision = next_revision
    replacement.status = "draft"
    replacement.created_at = now()
    for item in replacement.shots:
        item.shot_plan_id = new_id("svp")
        item.episode_video_plan_id = replacement.episode_video_plan_id
        item.plan_revision = next_revision
        if item.shot_id != target.shot_id:
            continue
        item.mode = override_mode
        item.planned_mode = item.mode
        item.video_input_intent = override_intent
        item.depends_on_shot_id = payload.get("depends_on_shot_id")
        item.required_assets = override_assets
        item.reason_codes = [
            *item.reason_codes,
            "MANUAL_OPERATION_OVERRIDE",
        ]
        item.input_revision_fingerprints["manual_override_reason"] = _review_sha(
            payload["reason"]
        )
    snapshot = capability_snapshot_by_id(
        replacement.capability_snapshot_id, conn=conn,
    )
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    try:
        if not snapshot:
            raise VideoPlanValidationError([{"code": "CAPABILITY_SNAPSHOT_MISSING"}])
        release_manifest = current_storyboard_release_manifest(
            episode_id, conn=conn,
        )
        validate_episode_plan(
            replacement,
            list(rows),
            snapshot,
            release_manifest=release_manifest,
        )
    except (ValueError, VideoPlanValidationError) as exc:
        issues = exc.issues if isinstance(exc, VideoPlanValidationError) else [{"code": str(exc)}]
        raise HTTPException(409, {"valid": False, "blockers": issues}) from exc
    publish_plan(replacement, conn=conn)
    conn.commit()
    return replacement.model_dump(mode="json")

@router.post(
    "/episodes/{episode_id}/video-generation-plan/{plan_id}/execute"
)
async def execute_episode_video_generation_plan(
    episode_id: str,
    plan_id: str,
    body: dict | None = None,
):
    from app.video_plan import load_latest_plan

    # 快速失败：给出即时 404/409 反馈，避免用户走完一整轮审批往返才发现计划已过期。
    # 这只是 UX 优化，不是权威校验——权威校验在 plan_id 显式进入 dispatch 参数、
    # 传到 h_video.generate_episode 之后，由 api._generate_episode_core 在真正
    # 入队前用当时最新的 plan 状态重新核验（见 I.VideoGenerateEpisodeInput.plan_id
    # 与该函数里的 requested_plan_id 分支），这样才能收紧「预检和执行之间计划被
    # 替换」的 TOCTOU 窗口，而不是只在 REST 层做一次事后检查就再也不认这个 id。
    plan = load_latest_plan(episode_id)
    if not plan or plan.episode_video_plan_id != plan_id or plan.status != "valid":
        raise HTTPException(409, "只能执行当前有效的视频模式计划")
    from app.capabilities.dispatch import dispatch, respond_ui
    payload = body or {}
    result = await dispatch(
        "video.generate_episode",
        {
            "episode_id": episode_id,
            "plan_id": plan_id,
            "idempotency_key": payload.get("idempotency_key"),
            "request_id": payload.get("request_id"),
            "approval_token": payload.get("approval_token"),
        },
        initiator="ui",
    )
    return respond_ui(result)
