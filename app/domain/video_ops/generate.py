"""整集/单镜头视频生成的实际发起。

从 app/domain/video_ops.py 按原样搬移；依赖 confirmation_gate。单个函数 _generate_episode_core 351 行，是逐镜生成
派发的唯一权威顺序，不做拆分（移动未拆分）。
"""
from __future__ import annotations

import json

from app import (
    errors,
    task_registry,
    worker,
)
from app.db import get_conn
from app.domain.common import (
    _episode_or_404,
    router,
)
from app.domain.review_wall import (
    _review_assert_positive_action,
    _review_assert_shot_positive,
)
from app.domain.storyboard_ops import _board_from_shot_rows
from fastapi import HTTPException
from pathlib import Path

from .confirmation_gate import _assert_storyboard_generation_gate


def _shot_by_no(episode_id: str, shot_no: int):
    return get_conn().execute(
        "SELECT id FROM shots WHERE episode_id=? AND shot_no=?", (episode_id, shot_no)).fetchone()

@router.post("/episodes/{episode_id}/generate")
async def generate_episode(episode_id: str, body: dict | None = None):
    """先生成并校验整集三模式计划，再按素材依赖 DAG 安全入队。"""
    from app.capabilities.dispatch import ui_route
    payload = dict(body) if isinstance(body, dict) else {}
    routed = await ui_route("video.generate_episode", {
        "episode_id": episode_id,
        "idempotency_key": payload.get("idempotency_key"),
        "request_id": payload.get("request_id"),
        # 这两个字段决定「只补齐待办」还是「全量重跑」、以及资格陈旧性校验；
        # 命令总线走 ConfirmationPolicy.ALWAYS 是真实用户路径的必经之路，漏转发
        # 会让「生成所有视频」弹窗承诺的 only_incomplete 语义在总线层失效
        # （见 I.VideoGenerateEpisodeInput 与 h_video.generate_episode）。
        "only_incomplete": bool(payload.get("only_incomplete") or False),
        "qualification_version": payload.get("qualification_version"),
    })
    if routed is not None:
        return routed
    return await _generate_episode_core(episode_id, payload)

def _adopt_reused_completed_version(
    conn,
    *,
    shot_id: str,
    version_id: str,
) -> bool:
    """Adopt an idempotently reused version only when it is deliverable."""
    version = conn.execute(
        """SELECT status,video_path,technical_validation_json
             FROM shot_versions
            WHERE id=? AND shot_id=?""",
        (version_id, shot_id),
    ).fetchone()
    if (
        not version
        or version["status"] != "succeeded"
        or not version["video_path"]
        or not Path(str(version["video_path"])).is_file()
    ):
        return False
    try:
        technical = json.loads(version["technical_validation_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if technical and not technical.get("passed"):
        return False
    updated = conn.execute(
        """UPDATE shots SET adopted_version_id=?
            WHERE id=? AND (adopted_version_id IS NULL OR adopted_version_id='')""",
        (version_id, shot_id),
    )
    return updated.rowcount == 1

async def _generate_episode_core(episode_id: str, body: dict) -> dict:
    """Create/reuse jobs for an episode; ``only_incomplete`` powers Continue."""
    ep = _episode_or_404(episode_id)
    qualification = _review_assert_positive_action(
        episode_id, body.get("qualification_version"),
    )
    # 曾经这里还有一次独立的 `ep["status"] not in (...)` 白名单复查，要求
    # episodes.status 已推进到 confirmed/generating/done 才放行——这条检查
    # 与上面 `_review_assert_positive_action` 判的是同一件事，但判据不同源：
    # 分镜台 2.0.0（app.production.storyboard_pack）生成完成后只落
    # status='scripted'，从不自动推进到那个白名单，会把刚才已经判定
    # eligible_for_production=True 的分集在这里重新拦一次。真正的资格判断
    # 已经交给上面这次调用（产物是否完整 + 上游是否仍在跑 + 资产是否合格），
    # 不需要再挂一份 status 白名单。
    _assert_storyboard_generation_gate(episode_id)
    # Supervisor 运行期间拒绝快速模式，避免重复付费
    try:
        mode = ep["video_completion_mode"]
    except (KeyError, IndexError, TypeError):
        mode = None
    if mode == "complete" and task_registry.active("video_completion", episode_id):
        raise HTTPException(409, "全片补齐 Supervisor 运行中，请使用补齐模式或等待完成")
    conn = get_conn()
    operation_key = str(body.get("idempotency_key") or "").strip()
    operation_fingerprint = str(body.get("operation_request_fingerprint") or "").strip()
    operation_owner = str(body.get("operation_claim_token") or "").strip()
    operation_binding: dict = {}
    if operation_key and operation_fingerprint:
        from app.video_command_operations import read_video_command_operation_binding

        operation_binding = read_video_command_operation_binding(
            command="video.generate_episode",
            idempotency_key=operation_key,
            request_fingerprint=operation_fingerprint,
        )
    shots_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,)).fetchall()
    from app.video_plan import (
        VideoPlanValidationError,
        generate_episode_plan,
        load_plan_by_id,
        load_latest_plan,
        verify_episode_plan_is_current,
    )
    try:
        bound_plan_id = str(operation_binding.get("plan_id") or "")
        plan = (
            load_plan_by_id(bound_plan_id, conn=conn)
            if bound_plan_id
            else load_latest_plan(episode_id, conn=conn)
        )
        requested_plan_id = body.get("plan_id")
        if requested_plan_id:
            if not plan or plan.episode_video_plan_id != requested_plan_id:
                raise HTTPException(409, "请求执行的计划不是当前有效 revision")
            if not verify_episode_plan_is_current(plan, conn=conn):
                raise HTTPException(409, "请求执行的计划已不符合当前生成台输入策略，请重新生成计划")
        elif not bound_plan_id:
            # Quick generation must never be gated on the AI mode-planning call:
            # its mode/dependency output is discarded unconditionally by
            # app.media_exec.enqueue for every episode with
            # narrative_authority_required=False (100% of the current dataset).
            # Skip the AI call and publish a deterministic all-reference plan
            # instead -- real upstream-contract checks (shots exist, storyboard
            # published, assets resolvable) still run inside generate_episode_plan
            # and can still raise. The AI-classified path is unchanged for the
            # explicit /video-generation-plan endpoints and for
            # app.video_supervisor/app.completion_grant.
            plan = await generate_episode_plan(
                episode_id, force=bool(body.get("force_replan")), conn=conn,
                deterministic_only=True,
            )
    except VideoPlanValidationError as exc:
        raise HTTPException(409, {
            "status": "BLOCKED_UPSTREAM_CONTRACT",
            "blockers": exc.issues,
        }) from exc
    if not plan:
        raise HTTPException(409, "视频模式计划不存在")
    bound_plan_stale = bool(bound_plan_id and plan.status != "valid")
    if plan.status != "valid" and not bound_plan_stale:
        raise HTTPException(409, "视频模式计划尚未通过确定性校验")
    plan_by_shot = {item.shot_id: item for item in plan.shots}
    board = _board_from_shot_rows(shots_rows, ep["episode_no"])
    shots = []
    previous_shot = None
    previous_shot_row = None
    for idx, row in enumerate(shots_rows):
        current = board.shots[idx]
        shots.append({
            "row": dict(row),
            "shot": current,
            "prev": previous_shot,
            "prev_row": dict(previous_shot_row) if previous_shot_row is not None else None,
        })
        previous_shot = current
        previous_shot_row = row
    from_no = body.get("from_shot_no")
    if from_no is not None:
        try:
            from_no = int(from_no)
        except (TypeError, ValueError):
            pass
    if from_no:
        selected = []
        for s in shots:
            if s["row"]["shot_no"] == from_no:
                selected_ids = {s["row"]["id"]}
                changed = True
                while changed:
                    changed = False
                    for item in plan.shots:
                        if (
                            item.depends_on_shot_id in selected_ids
                            and item.shot_id not in selected_ids
                        ):
                            selected_ids.add(item.shot_id)
                            changed = True
                selected = [
                    item for item in shots if item["row"]["id"] in selected_ids
                ]
                break
        if not selected:
            raise HTTPException(404, f"未找到镜 {from_no}")
    else:
        selected = shots
    completed_count = 0
    if body.get("only_incomplete"):
        completed_ids = {
            row["id"] for row in conn.execute(
                """SELECT s.id FROM shots s
                   WHERE s.episode_id=? AND (
                       s.adopted_version_id IS NOT NULL OR EXISTS(
                           SELECT 1 FROM shot_versions v
                           WHERE v.shot_id=s.id AND v.status='succeeded'
                             AND v.video_path IS NOT NULL AND v.video_path!=''
                       )
                   )""",
                (episode_id,),
            ).fetchall()
        }
        completed_count = sum(1 for item in selected if item["row"]["id"] in completed_ids)
        selected = [item for item in selected if item["row"]["id"] not in completed_ids]
    bound_selected_ids = [
        str(item) for item in (operation_binding.get("selected_shot_ids") or []) if item
    ]
    if bound_selected_ids:
        by_id = {str(item["row"]["id"]): item for item in shots}
        selected = [by_id[item] for item in bound_selected_ids if item in by_id]
    elif operation_key and operation_fingerprint and operation_owner:
        from app.video_command_operations import bind_video_command_operation

        bind_video_command_operation(
            command="video.generate_episode",
            idempotency_key=operation_key,
            request_fingerprint=operation_fingerprint,
            claim_token=operation_owner,
            binding={
                "plan_id": plan.episode_video_plan_id,
                "plan_revision": plan.plan_revision,
                "selected_shot_ids": [str(item["row"]["id"]) for item in selected],
                "enqueued": [],
            },
            conn=conn,
            merge=True,
        )
        conn.commit()
    if bound_plan_stale and operation_binding.get("enqueued"):
        recovered_results = list(operation_binding.get("enqueued") or [])
        outcome = {
            "episode_video_plan_id": plan.episode_video_plan_id,
            "plan_revision": plan.plan_revision,
            "mode_distribution": {},
            "critical_path_latency_ms": plan.critical_path_latency_ms,
            "estimated_cost": plan.estimated_cost,
            "enqueued": recovered_results,
            "skipped_completed": 0,
            "selected_shots": len(bound_selected_ids),
            "recovered_partial_operation": True,
            "remaining_requires_new_idempotency_key": True,
        }
        from app.video_command_operations import bind_video_command_operation

        bind_video_command_operation(
            command="video.generate_episode",
            idempotency_key=operation_key,
            request_fingerprint=operation_fingerprint,
            claim_token=operation_owner,
            binding={"operation_complete": True, "result": outcome},
            conn=conn,
            merge=True,
        )
        conn.commit()
        return outcome
    # Quick generation must not create one doomed paid-version record per shot.
    # The completion supervisor owns the self-healing asset preparation path.
    from app.multiview import scan_episode_reference_asset_gaps
    from app.domain.common import _project_bible_or_placeholder
    from app.production.screenplay_authority import resolve_downstream_screenplay
    from app.schemas import EpisodeScreenplay

    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
    ).fetchone()
    bible = _project_bible_or_placeholder(project)
    try:
        screenplay = resolve_downstream_screenplay(episode_id, conn=conn).screenplay
    except ValueError as exc:
        # Only parse the raw projection lazily, on this failure path -- a
        # healthy episode_prep_pack episode (screenplay contract 6.0.0+)
        # never reaches here (resolve_downstream_screenplay already resolves
        # it). EpisodeScreenplay.model_validate(raw_payload) would raise on a
        # prep_pack payload's extra keys (EpisodeScreenplay is
        # extra="forbid"); prep_pack never has narrative_plan by
        # construction, so skip straight to that fallback instead of parsing.
        from app.production.screenplay_authority import is_prep_pack_payload

        raw_payload = json.loads(ep["screenplay_json"])
        if is_prep_pack_payload(raw_payload):
            projection = EpisodeScreenplay(episode_no=int(ep["episode_no"] or 0))
        else:
            projection = EpisodeScreenplay.model_validate(raw_payload)
        if projection.narrative_plan is not None:
            raise HTTPException(
                409, f"当前叙事剧本权威链无法验证：{exc}",
            ) from exc
        screenplay = projection
    asset_gaps = scan_episode_reference_asset_gaps(
        project_id=ep["project_id"],
        episode_no=int(ep["episode_no"]),
        shots=[(item["row"]["id"], item["shot"]) for item in selected],
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    if asset_gaps["blockers"]:
        names = [
            *(f"人物「{name}」" for name in asset_gaps["characters"]),
            *(f"场景「{name}」" for name in asset_gaps["scenes"]),
        ]
        summary = "、".join(names) or "本集生产资产"
        raise HTTPException(
            409,
            f"{summary}尚未就绪。为避免整集批量失败，请使用“补齐到全片可用”，系统会先补齐资产再生成视频。",
        )
    # 不再预先清空 adopted_version_id：新版本成功并通过技术门禁后由
    # select_best_video_candidate 比较切换；任务失败时保留原可交付采用结果。
    results = [dict(item) for item in (operation_binding.get("enqueued") or [])]
    bound_enqueued_ids = {
        str(item.get("shot_id") or "") for item in results if item.get("shot_id")
    }
    pending_selected = [
        item for item in selected
        if str(item["row"]["id"]) not in bound_enqueued_ids
    ]
    for s in pending_selected:
        if operation_key and operation_fingerprint and operation_owner:
            from app.video_command_operations import renew_video_command_operation

            renew_video_command_operation(
                command="video.generate_episode",
                idempotency_key=operation_key,
                request_fingerprint=operation_fingerprint,
                claim_token=operation_owner,
            )
        shot_plan = plan_by_shot.get(s["row"]["id"])
        if not shot_plan:
            results.append({
                "shot_id": s["row"]["id"],
                "error": "当前计划未覆盖该镜头，已阻止入队",
                "issue_codes": ["VIDEO_PLAN_SHOT_MISSING"],
            })
            continue
        after = shot_plan.depends_on_shot_id
        try:
            r = worker.enqueue_shot(
                s["row"]["id"], after_shot_id=after,
                dependency_snapshot=qualification,
                operation_idempotency_key=(
                    operation_key or None
                ),
                operation_request_fingerprint=operation_fingerprint or None,
                operation_claim_token=operation_owner or None,
                operation_command="video.generate_episode",
            )
            # enqueue_shot also reports active/paused same-key jobs as reused.
            # Only an already deliverable completed version may be adopted here.
            if r.get("reused") and r.get("version_id"):
                _adopt_reused_completed_version(
                    conn,
                    shot_id=s["row"]["id"],
                    version_id=r["version_id"],
                )
            results.append({"shot_id": s["row"]["id"], **r})
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(exc, action="enqueue_shot",
                                              context={"shot_id": s["row"]["id"], "episode_id": episode_id})
            issue_codes: list[str] = []
            try:
                from app.video_issues import issues_from_enqueue_error, persist_shot_issue
                issues = issues_from_enqueue_error(
                    exc, shot_id=s["row"]["id"], shot_no=s["row"]["shot_no"],
                )
                issue_codes = [i.code for i in issues]
                persist_shot_issue(
                    episode_id=episode_id,
                    shot_id=s["row"]["id"],
                    shot_no=s["row"]["shot_no"],
                    issues=issues,
                    source="generate_episode_enqueue",
                )
            except Exception:  # noqa: BLE001
                pass
            results.append({
                "shot_id": s["row"]["id"],
                "error": public,
                "issue_codes": issue_codes,
            })
    outcome = {
        "episode_video_plan_id": plan.episode_video_plan_id,
        "plan_revision": plan.plan_revision,
        "mode_distribution": {
            mode: sum(1 for item in plan.shots if item.mode.value == mode)
            for mode in (
                "REFERENCE_IMAGE_MODE",
                "FIRST_FRAME_MODE",
                "FIRST_LAST_FRAME_MODE",
                "VIDEO_INPUT_MODE",
            )
        },
        "critical_path_latency_ms": plan.critical_path_latency_ms,
        "estimated_cost": plan.estimated_cost,
        "enqueued": results,
        "skipped_completed": completed_count,
        "selected_shots": len(selected),
    }
    if operation_key and operation_fingerprint and operation_owner:
        from app.video_command_operations import bind_video_command_operation

        bind_video_command_operation(
            command="video.generate_episode",
            idempotency_key=operation_key,
            request_fingerprint=operation_fingerprint,
            claim_token=operation_owner,
            binding={"operation_complete": True, "result": outcome},
            conn=conn,
            merge=True,
        )
    conn.commit()
    return outcome

async def _generate_shot_core(shot_id: str, body: dict) -> dict:
    """单镜生成视频的领域逻辑，供 REST 路由与 ``video.generate_shot`` Command Handler 共用。"""
    conn = get_conn()
    if (
        body.get("idempotency_key")
        and body.get("operation_request_fingerprint")
        and body.get("operation_claim_token")
    ):
        from app.video_command_operations import renew_video_command_operation

        renew_video_command_operation(
            command="video.generate_shot",
            idempotency_key=str(body["idempotency_key"]),
            request_fingerprint=str(body["operation_request_fingerprint"]),
            claim_token=str(body["operation_claim_token"]),
        )
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise HTTPException(404, "镜头不存在")
    _assert_storyboard_generation_gate(shot_row["episode_id"])
    qualification = _review_assert_shot_positive(
        shot_id, body.get("qualification_version"),
    )
    from app.video_plan import (
        VideoPlanValidationError,
        create_local_replan_revision,
        generate_episode_plan,
    )
    try:
        # Same rationale as _generate_episode_core: single-shot generate also
        # triggered a full-episode AI mode-planning call, so one bad model
        # output anywhere in the episode locked both entry points for every
        # shot. Skip the AI call here too; real upstream-contract checks still
        # run and can still raise.
        plan = await generate_episode_plan(
            shot_row["episode_id"], conn=conn, deterministic_only=True,
        )
        if (
            body.get("reroll")
            or body.get("prompt_override")
        ):
            replan_reason = (
                "prompt_override_redo"
                if body.get("prompt_override")
                else "single_shot_reroll"
            )
            plan = create_local_replan_revision(
                shot_id,
                reason=replan_reason,
                conn=conn,
                idempotency_key=body.get("idempotency_key"),
            )
    except VideoPlanValidationError as exc:
        raise HTTPException(409, {
            "status": "BLOCKED_UPSTREAM_CONTRACT",
            "blockers": exc.issues,
        }) from exc
    shot_plan = next(
        (item for item in plan.shots if item.shot_id == shot_id),
        None,
    )
    if not shot_plan:
        raise HTTPException(409, "当前有效视频模式计划未覆盖该镜头")
    after = shot_plan.depends_on_shot_id
    try:
        return worker.enqueue_shot(
            shot_id,
            prompt_override=body.get("prompt_override"),
            extra_negative=body.get("extra_negative"),
            reroll=bool(body.get("reroll")),
            after_shot_id=after,
            dependency_snapshot=qualification,
            operation_idempotency_key=body.get("idempotency_key"),
            operation_request_fingerprint=body.get("operation_request_fingerprint"),
            operation_claim_token=body.get("operation_claim_token"))
    except ValueError as exc:
        raise HTTPException(409, str(exc))

@router.post("/shots/{shot_id}/generate")
async def generate_shot(shot_id: str, body: dict | None = None):
    from app.capabilities.dispatch import dispatch, respond_ui

    body = body or {}
    result = await dispatch(
        "video.generate_shot",
        {
            "shot_id": shot_id,
            "prompt_override": body.get("prompt_override"),
            "reroll": bool(body.get("reroll")),
            "qualification_version": body.get("qualification_version"),
            "idempotency_key": body.get("idempotency_key"),
            "request_id": body.get("request_id"),
        },
        initiator="ui",
    )
    return respond_ui(result)

@router.post("/shots/{shot_id}/video/stop")
async def stop_shot_video(shot_id: str):
    """立即停止本镜全部排队中或运行中的视频任务；重复调用安全。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.stop_shot", {"shot_id": shot_id})
    if routed is not None:
        return routed
    try:
        return worker.stop_shot_video_tasks(shot_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
