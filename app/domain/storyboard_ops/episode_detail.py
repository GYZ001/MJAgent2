"""整集分镜详情投影（分镜台/复核台共用的镜头列表+状态聚合出口）。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 mutation_primitives/resume_state/staleness/status_snapshot。单个函数
_episode_detail_projection 323 行，是分镜台详情字段的唯一权威聚合点，拆分会打散字段间依赖顺序，不做拆分（移动未拆分）。
"""
from __future__ import annotations

import json

from app import (
    errors,
    worker,
)
from app.db import (
    get_conn,
    get_setting,
    rows_to_dicts,
)
from app.domain.common import (
    SCREENPLAY_WORKSPACE_WITHHELD_FIELDS,
    _episode_or_404,
    _load_screenplay,
    _project_bible_or_placeholder,
    episode_prep_pack_payload,
    router,
    screenplay_workspace_projection,
)
from app.evidence import repository as evidence_repository
from fastapi import HTTPException

from .current_portraits import attach_current_character_portraits
from .mutation_primitives import _apply_contract_to_public_shot
from .public_shot_versions import _public_shot_versions
from .resume_state import (
    _storyboard_checkpoint_matches_screenplay,
    _storyboard_has_material,
)
from .staleness import _shot_video_is_stale
from .status_snapshot import _storyboard_status_snapshot


@router.get("/episodes/{episode_id}/storyboard/status")
def storyboard_status(episode_id: str):
    detail = episode_detail(episode_id, view="board")
    return detail["storyboard_status"]

@router.get("/episodes/{episode_id}")
def episode_detail(episode_id: str, view: str | None = None):
    """Return episode data shaped for the requesting workspace.

    The legacy/default response remains complete for MCP and API consumers.
    UI workspaces opt into a narrow view so screenplay, storyboard, and cinema
    pages never touch historical media JSON.
    """
    if view not in (None, "script", "board", "wall", "cinema"):
        raise HTTPException(400, f"未知分集视图：{view}")
    if view in (None, "board"):
        from app.storyboard_workspace import reconcile_cancelled_storyboard_run
        reconcile_cancelled_storyboard_run(episode_id)
    with evidence_repository.artifact_read_scope():
        detail = _episode_detail_projection(episode_id, view)
    # 不进 _episode_detail_projection 本体（该函数的行数棘轮已按现状封顶，
    # 见文件顶部 docstring）：这一步只是给已投影出的 prep_pack/shots 角色
    # 条目就地加两个展示用字段，不改变投影函数自身的字段依赖顺序。
    attach_current_character_portraits(detail, view)
    return detail

def _episode_detail_projection(episode_id: str, view: str | None) -> dict:
    """Read-only projection body; the caller owns reconciliation and scoping."""
    full = view is None
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
    screenplay_rebuild_error = None
    try:
        script = _load_screenplay(ep) if full or view in ("script", "board") else None
    except errors.ArtifactNeedsRebuildError as exc:
        if view not in {"script", "board"}:
            raise
        screenplay_rebuild_error = exc
        script = None
    screenplay_payload = (
        script.model_dump() if script and (full or view in ("script", "board")) else None
    )
    if view == "script":
        # 映射台不读也不改叙事蓝图；不下发它，写回时由服务端从权威补齐。
        screenplay_payload = screenplay_workspace_projection(screenplay_payload)
        ep["screenplay_withheld_fields"] = list(SCREENPLAY_WORKSPACE_WITHHELD_FIELDS)
    ep["screenplay"] = screenplay_payload
    # episode_prep_pack（screenplay 契约 6.0.0+，见 docs/TRANSFORM_FREEZE_PLAN.md）
    # 是与 EpisodeScreenplay 完全不同的形状；script 为 None 且原始 JSON 命中新形状时
    # 走这个专用投影字段，而不是让前端从 ep["screenplay"]=null 里读不到任何内容。
    ep["prep_pack"] = (
        episode_prep_pack_payload(ep)
        if script is None and (full or view in ("script", "board"))
        else None
    )
    ep["scene_options"] = []
    if full or view in ("board", "wall"):
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
        ).fetchone()
        project_bible = _project_bible_or_placeholder(project)
        if full or view == "board":
            ep["scene_options"] = [
                scene.name for scene in (project_bible.scenes or []) if (scene.name or "").strip()
            ]
    if full or view == "script":
        from app.domain.screenplay_ops import (
            _screenplay_authority_state,
        )
    ep.pop("screenplay_required_dialogues", None)
    ep.pop("screenplay_required_dialogue_occurrences", None)
    artifact_id = ep.get("screenplay_artifact_id")
    artifact = (
        evidence_repository.get_artifact(artifact_id)
        if artifact_id and (full or view == "script") else None
    )
    if artifact:
        artifact.pop("content_json", None)
        artifact.pop("content", None)
        artifact["evaluations"] = evidence_repository.get_evaluations(artifact_id)
        if (
            screenplay_rebuild_error is not None
            and artifact.get("id") == screenplay_rebuild_error.artifact_id
        ):
            artifact["stale_code"] = screenplay_rebuild_error.code
    ep["screenplay_evidence"] = artifact
    if full or view == "script":
        from app.production.revision import screenplay_production_state
        ep["screenplay_production"] = screenplay_production_state(episode_id)
    else:
        ep["screenplay_production"] = None
    storyboard_artifact_id = ep.get("storyboard_artifact_id")
    storyboard_artifact = (
        evidence_repository.get_artifact(storyboard_artifact_id)
        if storyboard_artifact_id and (full or view == "board") else None
    )
    if storyboard_artifact:
        storyboard_artifact.pop("content_json", None)
        storyboard_artifact.pop("content", None)
        storyboard_artifact["evaluations"] = evidence_repository.get_evaluations(
            storyboard_artifact_id
        )
    ep["storyboard_evidence"] = storyboard_artifact
    # 页面投影不回传整份 screenplay_json，但 screenplay_state 的权威判定必须看得见它：
    # 先 pop 再判定会让 _screenplay_ready 因「没有页面投影」直接 fail-closed，于是
    # 同一时刻 GET /episodes/{id}?view=script 报 qa_certificate_invalid，
    # 而 GET /episodes/{id}/screenplay/status 报 ready —— 两个端点对同一集给出
    # 互相矛盾的权威状态（映射台恰好把两者合并展示）。
    screenplay_projection_json = ep.pop("screenplay_json", None)
    # 分镜大纲（先规划后逐镜填充）：透出给前端做 已通过 k / 计划 N 镜 的进度展示
    outline = None
    outline_json_for_gate = (
        ep.get("storyboard_outline_json")
        if full or view == "board"
        else None
    )
    if full or view == "board":
        try:
            outline = json.loads(outline_json_for_gate or "null")
        except (TypeError, ValueError):
            outline = None
    ep.pop("storyboard_outline_json", None)
    ep["storyboard_outline"] = outline
    ep["storyboard_planned_shots"] = len(outline["shots"]) if outline and outline.get("shots") else None
    # Supervisor 运行面板数据（PRD §14.2）
    if full or view == "board":
        from app.storyboard_supervisor import load_latest_checkpoint
        from app.storyboard_control import control_snapshot

        cp = load_latest_checkpoint(episode_id)
        stale_checkpoint_ignored = False
        if (
            cp is not None
            and not _storyboard_checkpoint_matches_screenplay(cp, ep)
            and not (_storyboard_has_material(episode_id, ep) or outline)
        ):
            stale_checkpoint_ignored = True
            cp = None
        if (
            stale_checkpoint_ignored
            and ep.get("script_error")
            == "上游剧本已变更，自动完成授权失效，请重新授权后继续"
        ):
            # The database keeps old checkpoint artifacts as audit evidence after
            # a screenplay publish clears its downstream projection.  Do not leak
            # that historical pause/error into the new screenplay's board view.
            ep["script_error"] = None
        ep["supervisor"] = None
        if cp is not None:
            repair = cp.last_repair or {}
            ep["supervisor"] = {
                "phase": cp.phase,
                "repair_epoch": cp.repair_epoch,
                "lifetime_repair_count": cp.repair_epoch,
                "activation_no": cp.activation_no,
                "activation_attempt_count": cp.activation_attempt_count,
                "activation_attempt_limit": 6,
                "validated_prefix_end": cp.validated_prefix_end,
                "next_shot_no": cp.next_shot_no,
                "expected_total": cp.expected_total or ep["storyboard_planned_shots"] or 0,
                "outcome": cp.outcome,
                "last_repair": repair,
                "strategy": repair.get("strategy"),
                "frontier": repair.get("invalidation_frontier"),
                "issue_codes": repair.get("issue_codes") or [],
                "pending_control": control_snapshot(episode_id),
            }
        try:
            ep["active_storyboard_run_id"] = ep.get("active_storyboard_run_id")
        except Exception:  # noqa: BLE001
            ep["active_storyboard_run_id"] = None
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"])
    ep["shot_count"] = shot_count
    if full or view == "script":
        ep["screenplay_state"] = _screenplay_authority_state(
            {**ep, "screenplay_json": screenplay_projection_json},
            shot_count=shot_count,
            production=ep.get("screenplay_production"),
            rebuild_error=screenplay_rebuild_error,
        )
    else:
        ep["screenplay_state"] = None
    if view in ("script", "cinema"):
        ep["shots"] = []
        ep["pipeline_summary"] = None
        return ep

    shot_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    # 冷观众审读驱动的叙事指标面板（NarrativeReadinessPanel）已随观众深读/
    # 校准校验功能一起下线（用户拍板）：这里曾经的 compute_narrative_metrics
    # 调用只在 script.narrative_plan is not None 时才执行，对 prep_pack
    # （契约 6.0.0+）分集永远是 None，本就是死分支，不留兼容。
    ep["narrative_metrics"] = None
    # 预估只按模型选择的实际分镜时长累计；单集不设总时长产品上限。
    ep["cost_cny"] = worker.episode_cost(episode_id)
    ep["cost_limit_cny"] = float(get_setting("episode_cost_limit_cny") or 100)
    shots = rows_to_dicts(shot_rows)
    version_counts = {}
    if view == "board" and shots:
        count_rows = conn.execute(
            """SELECT v.shot_id, COUNT(*) AS version_count
               FROM shot_versions v
               JOIN shots s ON s.id=v.shot_id
               WHERE s.episode_id=? GROUP BY v.shot_id""",
            (episode_id,),
        ).fetchall()
        version_counts = {row["shot_id"]: int(row["version_count"]) for row in count_rows}
    pipeline_statuses = {}
    pipeline_summary = None
    if full or view == "wall":
        try:
            from app.media_pipeline.status import episode_pipeline_statuses
            pipeline_statuses, pipeline_summary = episode_pipeline_statuses(episode_id, conn=conn)
        except Exception:  # noqa: BLE001
            pipeline_statuses, pipeline_summary = {}, None
    for s in shots:
        s["characters"] = json.loads(s["characters"] or "[]")
        s["dialogues"] = json.loads(s["dialogues"] or "[]")
        _apply_contract_to_public_shot(s)
        from app.continuity import information_items_for_shot
        s["new_information_items"] = information_items_for_shot(s, script)
        from app.video_cost_model import initial_shot_generation_cost

        s["est_cost_cny"] = initial_shot_generation_cost(s["duration_s"])
        if s.get("storyboard_artifact_id") and (full or view == "board"):
            shot_artifact = evidence_repository.get_artifact(s["storyboard_artifact_id"])
            if shot_artifact:
                shot_artifact.pop("content_json", None)
                shot_artifact.pop("content", None)
                shot_artifact["evaluations"] = evidence_repository.get_evaluations(
                    s["storyboard_artifact_id"]
                )
            s["storyboard_evidence"] = shot_artifact
        else:
            s["storyboard_evidence"] = None
        if full or view == "board":
            from app.storyboard_workspace import source_binding_for_shot
            s["source_binding"] = source_binding_for_shot(s["id"])
        # mode_plan 存的是 JSON 文本，解析成对象供前端只读展示模型决策
        try:
            s["mode_plan"] = json.loads(s["mode_plan"]) if s.get("mode_plan") else None
        except (TypeError, ValueError):
            s["mode_plan"] = None
        # 新链路只使用参考图；旧关键帧字段仅保留在数据库中做历史兼容，不再对外暴露或参与状态判断。
        for legacy_key in (
            "approved_scene_id", "approved_head_scene_id", "approved_tail_scene_id", "scene_status",
        ):
            s.pop(legacy_key, None)
        s["video_stale"] = _shot_video_is_stale(conn, s, ep.get("storyboard_artifact_id"))
        if view == "board":
            s["version_count"] = version_counts.get(s["id"], 0)
            s["versions"] = []
            s["pipeline"] = None
            continue

        s["versions"] = _public_shot_versions(conn, s["id"], include_inputs=full)
        if s.get("adopted_version_id") and not any(
            version["id"] == s["adopted_version_id"] for version in s["versions"]
        ):
            s["delivery_fallback_active"] = True
            s["adopted_version_id"] = None
        s["pipeline"] = pipeline_statuses.get(s["id"])
        s["video_status"] = (
            s["pipeline"].get("video_status") if s["pipeline"] else None
        )
        # 透出 grade / fallback，供生成台 A/B 分色
        try:
            from app.evidence.media import grade_shot_video
            graded = grade_shot_video(s["id"])
            s["video_grade"] = graded.get("grade")
            s["fallback_reason"] = graded.get("fallback_reason")
            s["continuity_degraded"] = bool(graded.get("continuity_degraded"))
        except Exception:  # noqa: BLE001
            s["video_grade"] = None
            s["fallback_reason"] = None
            s["continuity_degraded"] = False
    ep["shots"] = shots
    if full or view == "board":
        status_episode = {
            **ep,
            # The public response omits the raw JSON, but the shared full gate
            # must still receive the approved outline readability windows.
            "storyboard_outline_json": outline_json_for_gate,
        }
        ep["storyboard_status"] = _storyboard_status_snapshot(
            status_episode,
            shots,
            ep.get("supervisor"),
            script,
            screenplay_rebuild_error,
        )
        if ep["storyboard_status"].pop("_obsolete_policy_repair", False):
            ep["script_error"] = None
        # 任务计时以服务端 run 为准：localStorage 起点在运行中刷新后会永久搁浅，
        # 下一个任务复用旧起点会显示出「已等待 1244 分」这类虚高时长。
        # 不走 active_storyboard_run_id：该指针在任务结束时被清空，取最近一次 run
        # 才能在完成后继续显示「本次耗时」。
        ep["storyboard_status"].update(
            {
                f"task_{key}": value
                for key, value in evidence_repository.latest_run_timing(
                    workflow_type="storyboard",
                    scope_type="episode",
                    scope_id=episode_id,
                    conn=conn,
                ).items()
            }
        )
        # 逐镜耗时（累计全部重试迭代），按 shot_no 归集。
        ep["shot_timings"] = evidence_repository.storyboard_shot_timings(
            episode_id=episode_id,
            conn=conn,
        )
    ep["pipeline_summary"] = pipeline_summary
    # 视频补齐 Supervisor 面板（生成台）
    if full or view == "wall":
        # 整集视频生成的总计时；单条视频的耗时随 version 一起下发。
        ep["video_task_timing"] = evidence_repository.latest_run_timing(
            workflow_type="episode_video_completion",
            scope_type="episode",
            scope_id=episode_id,
            conn=conn,
        )
        try:
            from app.completion_grant import (
                episode_video_completion_budget_requirement,
            )
            ep["video_budget"] = episode_video_completion_budget_requirement(
                episode_id,
                conn=conn,
            )
        except Exception:  # noqa: BLE001
            ep["video_budget"] = None
        try:
            from app.video_supervisor import load_latest_checkpoint, public_checkpoint_projection
            vcp = load_latest_checkpoint(episode_id)
            ep["video_supervisor"] = public_checkpoint_projection(vcp)
            try:
                ep["active_video_run_id"] = ep.get("active_video_run_id")
                ep["video_completion_mode"] = ep.get("video_completion_mode") or "quick"
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            ep["video_supervisor"] = None
    return ep

@router.get("/shots/{shot_id}/review")
def shot_review_detail(shot_id: str):
    """Load the expensive review gallery for one selected shot only."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "镜头不存在")
    shot = dict(row)
    shot["characters"] = json.loads(shot["characters"] or "[]")
    shot["dialogues"] = json.loads(shot["dialogues"] or "[]")
    _apply_contract_to_public_shot(shot)
    from app.continuity import information_items_for_shot
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (shot["episode_id"],)
    ).fetchone()
    screenplay = None
    if episode_row is not None:
        from app.production.screenplay_authority import (
            episode_requires_immutable_screenplay_authority,
            resolve_downstream_screenplay,
        )

        try:
            screenplay = resolve_downstream_screenplay(
                str(episode_row["id"]), conn=conn,
            ).screenplay
        except ValueError as exc:
            if episode_requires_immutable_screenplay_authority(
                episode_row, conn=conn,
            ):
                published_id = str(
                    episode_row["published_screenplay_artifact_id"] or ""
                )
                projected_id = str(episode_row["screenplay_artifact_id"] or "")
                if not published_id or published_id != projected_id:
                    raise HTTPException(
                        409, f"当前剧本权威链无法验证，不能展示评审详情：{exc}",
                    ) from exc
                try:
                    from app.production.patch import load_screenplay_from_artifact

                    screenplay = load_screenplay_from_artifact(published_id)
                except Exception as artifact_exc:
                    raise HTTPException(
                        409,
                        "当前剧本权威链无法验证，且已发布剧本 Artifact 不可读取："
                        f"{artifact_exc}",
                    ) from artifact_exc
            # Explicit plan-null legacy rows keep their historical review
            # behavior; they do not have an immutable authority contract.
            else:
                screenplay = _load_screenplay(dict(episode_row))
    shot["new_information_items"] = information_items_for_shot(shot, screenplay)
    from app.video_cost_model import initial_shot_generation_cost

    shot["est_cost_cny"] = initial_shot_generation_cost(shot["duration_s"])
    shot["video_stale"] = _shot_video_is_stale(
        conn, shot, episode_row["storyboard_artifact_id"] if episode_row else None
    )
    for legacy_key in (
        "approved_scene_id", "approved_head_scene_id", "approved_tail_scene_id", "scene_status",
    ):
        shot.pop(legacy_key, None)
    try:
        shot["mode_plan"] = json.loads(shot["mode_plan"]) if shot.get("mode_plan") else None
    except (TypeError, ValueError):
        shot["mode_plan"] = None
    shot["storyboard_evidence"] = None
    shot["versions"] = _public_shot_versions(conn, shot_id, include_inputs=True)
    if shot.get("adopted_version_id") and not any(
        version["id"] == shot["adopted_version_id"] for version in shot["versions"]
    ):
        shot["delivery_fallback_active"] = True
        shot["adopted_version_id"] = None
    try:
        from app.media_pipeline.status import shot_pipeline_status
        shot["pipeline"] = shot_pipeline_status(shot_id, conn=conn)
    except Exception:  # noqa: BLE001
        shot["pipeline"] = None
    shot["video_status"] = (
        shot["pipeline"].get("video_status") if shot["pipeline"] else None
    )
    try:
        from app.evidence.media import grade_shot_video
        graded = grade_shot_video(shot_id)
        shot["video_grade"] = graded.get("grade")
        shot["fallback_reason"] = graded.get("fallback_reason")
        shot["continuity_degraded"] = bool(graded.get("continuity_degraded"))
    except Exception:  # noqa: BLE001
        shot["video_grade"] = None
        shot["fallback_reason"] = None
        shot["continuity_degraded"] = False
    return shot
