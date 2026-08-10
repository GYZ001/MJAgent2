"""领域预检：读取真实数据库状态，生成 Impact 与 state_fingerprint（PRD §6.3 / §8.2）。

默认 Bus 预检不含领域状态；本模块为高风险与 WHEN_IMPACT 命令提供准确影响摘要，
使批准卡可展示费用、失效产物与前置条件，且状态变化会使批准失效。
"""
from __future__ import annotations

from typing import Any

from app.capabilities import policy
from app.capabilities.schemas import (
    AffectedScope,
    ConfirmationPolicy,
    PreconditionCheck,
    PreflightResult,
    RiskLevel,
)
from app.db import get_conn


def _fp(parts: dict[str, Any]) -> str:
    return policy.state_fingerprint(parts)


def project_delete(args) -> PreflightResult:
    conn = get_conn()
    row = conn.execute("SELECT id, name, status FROM projects WHERE id=?", (args.project_id,)).fetchone()
    if not row:
        return PreflightResult(
            command="project.delete",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="项目不存在",
            state_fingerprint=_fp({"project_id": args.project_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="项目不存在",
        )
    ep_count = conn.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE project_id=?", (args.project_id,)
    ).fetchone()["c"]
    shot_count = conn.execute(
        """SELECT COUNT(*) AS c FROM shots s
           JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?""",
        (args.project_id,),
    ).fetchone()["c"]
    return PreflightResult(
        command="project.delete",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary=f"将永久删除项目「{row['name']}」及其全部剧集/镜头/产物",
        affected=AffectedScope(
            projects=[args.project_id],
            shot_count=int(shot_count or 0),
            extra={"episode_count": int(ep_count or 0)},
        ),
        warnings=["此操作不可恢复"],
        state_fingerprint=_fp({
            "project_id": args.project_id,
            "status": row["status"],
            "episodes": ep_count,
            "shots": shot_count,
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
    )


def episode_plan(args) -> PreflightResult:
    from app import planning

    conn = get_conn()
    project = conn.execute(
        "SELECT id, name, plan_status FROM projects WHERE id=?",
        (args.project_id,),
    ).fetchone()
    if not project:
        return PreflightResult(
            command="episode.plan",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="项目不存在",
            state_fingerprint=_fp({"project_id": args.project_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="not_found",
            denial_message="项目不存在",
        )

    episodes = conn.execute(
        """SELECT id, episode_no, status, screenplay_status,
                  screenplay_artifact_id, storyboard_artifact_id, delivery_artifact_id
           FROM episodes WHERE project_id=? ORDER BY episode_no""",
        (args.project_id,),
    ).fetchall()
    episode_ids = [row["id"] for row in episodes]
    if episode_ids and not args.replace_existing:
        return PreflightResult(
            command="episode.plan",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary=f"项目已有 {len(episode_ids)} 集，重新分集必须明确确认替换",
            affected=AffectedScope(
                projects=[args.project_id],
                episodes=episode_ids,
                extra={"episode_count": len(episode_ids)},
            ),
            state_fingerprint=_fp({
                "project_id": args.project_id,
                "plan_status": project["plan_status"],
                "episodes": [dict(row) for row in episodes],
                "replace_existing": False,
            }),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="REPLAN_CONFIRMATION_REQUIRED",
            denial_message="项目已有分集；确认影响后，以 replace_existing=true 重新提交",
        )

    blockers = planning.replan_blockers(conn, args.project_id)
    if blockers["blocked"]:
        return PreflightResult(
            command="episode.plan",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="项目仍有可继续或正在运行的下游任务，不能重新分集",
            affected=AffectedScope(
                projects=[args.project_id],
                episodes=episode_ids,
                extra=blockers,
            ),
            state_fingerprint=_fp({
                "project_id": args.project_id,
                "plan_status": project["plan_status"],
                "blockers": blockers,
            }),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="REPLAN_ACTIVE_WORK",
            denial_message="请先在对应工作台或任务中心结束、取消下游任务，再重新分集",
        )

    chapter_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM chapters WHERE project_id=?",
        (args.project_id,),
    ).fetchone()["c"])
    shot_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM shots s
           JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?""",
        (args.project_id,),
    ).fetchone()["c"])
    media_versions = int(conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id
           JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?""",
        (args.project_id,),
    ).fetchone()["c"])
    packages = [
        row["id"] for row in conn.execute(
            """SELECT dp.id FROM delivery_packages dp
               JOIN episodes e ON e.id=dp.episode_id
               WHERE e.project_id=? ORDER BY dp.created_at, dp.id""",
            (args.project_id,),
        ).fetchall()
    ]
    projected_artifacts = sum(
        bool(row["screenplay_artifact_id"])
        + bool(row["storyboard_artifact_id"])
        + bool(row["delivery_artifact_id"])
        for row in episodes
    )
    invalidated = projected_artifacts + media_versions + len(packages)
    replacing = bool(episode_ids)
    warnings = []
    if replacing:
        warnings = [
            "现有剧本、分镜、视频和交付投影将从项目中移除；历史运行证据仍保留",
            "重新分集采用本地章节规则，不调用模型，也不会产生新的模型费用",
        ]
    return PreflightResult(
        command="episode.plan",
        allowed=True,
        risk=RiskLevel.R2_MATERIAL if replacing else RiskLevel.R1_REVERSIBLE,
        summary=(
            f"将替换项目「{project['name']}」现有 {len(episode_ids)} 集，"
            f"并按 {chapter_count} 个原文章节重新建立分集"
            if replacing
            else f"将按 {chapter_count} 个原文章节创建分集"
        ),
        affected=AffectedScope(
            projects=[args.project_id],
            episodes=episode_ids,
            shot_count=shot_count,
            invalidated_artifacts=invalidated,
            packages=packages,
            extra={
                "episode_count": len(episode_ids),
                "chapter_count": chapter_count,
                "media_versions": media_versions,
                "deterministic_local_rule": True,
            },
        ),
        warnings=warnings,
        state_fingerprint=_fp({
            "project_id": args.project_id,
            "plan_status": project["plan_status"],
            "chapter_count": chapter_count,
            "episodes": [dict(row) for row in episodes],
            "shot_count": shot_count,
            "media_versions": media_versions,
            "packages": packages,
            "replace_existing": bool(args.replace_existing),
        }),
        requires_confirmation=False,
        confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
    )


def video_clear_episode(args) -> PreflightResult:
    from app.completion_grant import provider_task_clearance_snapshot

    conn = get_conn()
    ep = conn.execute("SELECT id, episode_no, project_id, status FROM episodes WHERE id=?", (args.episode_id,)).fetchone()
    if not ep:
        return PreflightResult(
            command="video.clear_episode",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    versions = conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
        (args.episode_id,),
    ).fetchone()["c"]
    shots = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (args.episode_id,)
    ).fetchone()["c"]
    clearance = provider_task_clearance_snapshot(
        episode_id=args.episode_id,
        conn=conn,
    )
    if not clearance["safe_to_clear"]:
        return PreflightResult(
            command="video.clear_episode",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="供应商付费任务尚未终态，本次未清空任何资源",
            affected=AffectedScope(
                episodes=[args.episode_id],
                shot_count=int(shots or 0),
                invalidated_artifacts=int(versions or 0),
                extra=clearance,
            ),
            warnings=["保留任务句柄与费用账本，待供应商状态收敛后可重试"],
            state_fingerprint=_fp({
                "episode_id": args.episode_id,
                "status": ep["status"],
                "versions": versions,
                "shots": shots,
                "provider_clearance": clearance,
            }),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="PROVIDER_TASKS_NOT_TERMINAL",
            denial_message=(
                "供应商付费任务尚未终态；请按恢复状态继续轮询或核对创建结果"
            ),
        )
    return PreflightResult(
        command="video.clear_episode",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary=f"将清空第 {ep['episode_no']} 集全部视频与参考图产物",
        affected=AffectedScope(
            episodes=[args.episode_id],
            shot_count=int(shots or 0),
            invalidated_artifacts=int(versions or 0),
            versions=[],
        ),
        warnings=["付费媒体将被删除，需重新生成"],
        state_fingerprint=_fp({
            "episode_id": args.episode_id,
            "status": ep["status"],
            "versions": versions,
            "shots": shots,
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
    )


def video_clear_shot(args) -> PreflightResult:
    from app.completion_grant import provider_task_clearance_snapshot

    conn = get_conn()
    shot = conn.execute(
        "SELECT id, shot_no, episode_id, storyboard_artifact_id FROM shots WHERE id=?",
        (args.shot_id,),
    ).fetchone()
    if not shot:
        return PreflightResult(
            command="video.clear_shot",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="镜头不存在",
            state_fingerprint=_fp({"shot_id": args.shot_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="镜头不存在",
        )
    versions = conn.execute(
        "SELECT COUNT(*) AS c FROM shot_versions WHERE shot_id=?", (args.shot_id,)
    ).fetchone()["c"]
    clearance = provider_task_clearance_snapshot(
        shot_ids=[args.shot_id],
        conn=conn,
    )
    if not clearance["safe_to_clear"]:
        return PreflightResult(
            command="video.clear_shot",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="供应商付费任务尚未终态，本次未清空任何资源",
            affected=AffectedScope(
                episodes=[shot["episode_id"]],
                shots=[args.shot_id],
                shot_count=1,
                invalidated_artifacts=int(versions or 0),
                extra=clearance,
            ),
            warnings=["保留任务句柄与费用账本，待供应商状态收敛后可重试"],
            state_fingerprint=_fp({
                "shot_id": args.shot_id,
                "artifact": shot["storyboard_artifact_id"],
                "versions": versions,
                "provider_clearance": clearance,
            }),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="PROVIDER_TASKS_NOT_TERMINAL",
            denial_message=(
                "供应商付费任务尚未终态；请按恢复状态继续轮询或核对创建结果"
            ),
        )
    return PreflightResult(
        command="video.clear_shot",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary=f"将清空第 {shot['shot_no']} 镜的视频与参考图",
        affected=AffectedScope(
            episodes=[shot["episode_id"]],
            shots=[args.shot_id],
            shot_count=1,
            invalidated_artifacts=int(versions or 0),
        ),
        state_fingerprint=_fp({
            "shot_id": args.shot_id,
            "artifact": shot["storyboard_artifact_id"],
            "versions": versions,
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
    )


def video_generate_shot(args) -> PreflightResult:
    from app.video_cost_model import initial_shot_generation_cost

    conn = get_conn()
    shot = conn.execute(
        "SELECT id, shot_no, episode_id, duration_s FROM shots WHERE id=?", (args.shot_id,)
    ).fetchone()
    if not shot:
        return PreflightResult(
            command="video.generate_shot",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="镜头不存在",
            state_fingerprint=_fp({"shot_id": args.shot_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="镜头不存在",
        )
    ep = conn.execute(
        "SELECT id, status, episode_no FROM episodes WHERE id=?", (shot["episode_id"],)
    ).fetchone()
    confirmed = bool(ep and ep["status"] in {"confirmed", "generating", "done", "mixed"})
    estimated = initial_shot_generation_cost(float(shot["duration_s"] or 0))
    return PreflightResult(
        command="video.generate_shot",
        allowed=True,
        risk=RiskLevel.R2_MATERIAL,
        summary=f"将为第 {ep['episode_no'] if ep else '?'} 集第 {shot['shot_no']} 镜生成视频（预计约 ¥{estimated:.1f}）",
        estimated_cost_cny=estimated,
        affected=AffectedScope(episodes=[shot["episode_id"]], shots=[args.shot_id], shot_count=1),
        preconditions=[
            PreconditionCheck(
                key="storyboard_confirmed",
                passed=confirmed,
                message="分镜已确认" if confirmed else "分镜尚未确认，无法进入付费视频阶段",
            )
        ],
        warnings=[] if confirmed else ["分镜未确认：执行可能被领域层拒绝"],
        state_fingerprint=_fp({
            "shot_id": args.shot_id,
            "episode_status": ep["status"] if ep else None,
            "reroll": bool(getattr(args, "reroll", False)),
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
    )


def video_generate_episode(args) -> PreflightResult:
    from app.video_cost_model import initial_shot_generation_cost

    conn = get_conn()
    ep = conn.execute(
        "SELECT id, episode_no, status FROM episodes WHERE id=?", (args.episode_id,)
    ).fetchone()
    if not ep:
        return PreflightResult(
            command="video.generate_episode",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    try:
        from app import task_registry
        if task_registry.active("video_completion", args.episode_id):
            return PreflightResult(
                command="video.generate_episode",
                allowed=False,
                risk=RiskLevel.R2_MATERIAL,
                summary="全片补齐 Supervisor 运行中",
                state_fingerprint=_fp({"episode_id": args.episode_id, "supervisor": True}),
                requires_confirmation=False,
                confirmation_policy=ConfirmationPolicy.ALWAYS,
                denial_code="conflict",
                denial_message="全片补齐 Supervisor 运行中，请等待完成或取消后再用快速生成",
            )
    except Exception:  # noqa: BLE001
        pass
    pending = conn.execute(
        """SELECT COUNT(*) AS c FROM shots
           WHERE episode_id=?
             AND (adopted_version_id IS NULL OR adopted_version_id='')""",
        (args.episode_id,),
    ).fetchone()["c"]
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (args.episode_id,)
    ).fetchone()["c"]
    payable_rows = conn.execute(
        """SELECT s.duration_s FROM shots s
            WHERE s.episode_id=?
              AND (s.adopted_version_id IS NULL OR s.adopted_version_id='')
              AND NOT EXISTS (
                  SELECT 1 FROM shot_versions v
                   WHERE v.shot_id=s.id AND v.status='succeeded'
                     AND v.video_path IS NOT NULL AND v.video_path!=''
              )""",
        (args.episode_id,),
    ).fetchall()
    estimated = round(sum(
        initial_shot_generation_cost(float(row["duration_s"] or 0))
        for row in payable_rows
    ), 2)
    confirmed = ep["status"] in {"confirmed", "generating", "done", "mixed"}
    return PreflightResult(
        command="video.generate_episode",
        allowed=True,
        risk=RiskLevel.R2_MATERIAL,
        summary=f"将生成第 {ep['episode_no']} 集约 {pending} 个待办镜头（预计约 ¥{estimated:.1f}）",
        estimated_cost_cny=estimated,
        affected=AffectedScope(episodes=[args.episode_id], shot_count=int(pending or 0)),
        preconditions=[
            PreconditionCheck(key="storyboard_confirmed", passed=confirmed, message="分镜已确认" if confirmed else "分镜未确认"),
            PreconditionCheck(key="has_shots", passed=int(total or 0) > 0, message=f"共 {total} 镜"),
        ],
        state_fingerprint=_fp({
            "episode_id": args.episode_id,
            "status": ep["status"],
            "pending": pending,
            "total": total,
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
    )


def video_complete_episode(args) -> PreflightResult:
    import math
    conn = get_conn()
    ep = conn.execute(
        "SELECT id, episode_no, status, storyboard_artifact_id FROM episodes WHERE id=?",
        (args.episode_id,),
    ).fetchone()
    if not ep:
        return PreflightResult(
            command="video.complete_episode",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    # Supervisor 已在跑时，resume/抬额允许；fresh 仍可被核心拒绝
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (args.episode_id,)
    ).fetchone()["c"]
    uncovered = conn.execute(
        """SELECT COUNT(*) AS c FROM shots
           WHERE episode_id=?
             AND (adopted_version_id IS NULL OR adopted_version_id='')""",
        (args.episode_id,),
    ).fetchone()["c"]
    grades = {"A": 0, "B": 0, "C": int(uncovered or 0)}
    try:
        from app.video_supervisor import rebuild_coverage_ledger
        ledger = rebuild_coverage_ledger(args.episode_id)
        uncovered = ledger.grades.get("C", uncovered)
        grades = ledger.grades
    except Exception:  # noqa: BLE001
        pass
    estimated = round(max(int(uncovered or 0), 1) * 3.6 * 1.6, 2)
    try:
        from app.video_cost_model import predict_episode_completion_cost
        from app.video_supervisor import rebuild_coverage_ledger
        ledger = rebuild_coverage_ledger(args.episode_id)
        uncovered_ids = [e.shot_id for e in ledger.entries if e.grade == "C" or e.video_stale or e.chain_stale]
        pred = predict_episode_completion_cost(args.episode_id, uncovered_shot_ids=uncovered_ids)
        if pred.get("expected_cny"):
            estimated = float(pred["expected_cny"])
    except Exception:  # noqa: BLE001
        pass
    confirmed = ep["status"] in {"confirmed", "generating", "done", "mixed"}
    allow_edit = bool(getattr(args, "allow_storyboard_edit", False))
    risk = RiskLevel.R3_DESTRUCTIVE if allow_edit else RiskLevel.R2_MATERIAL
    cap_arg = getattr(args, "budget_cap_cny", None)
    wall_arg = getattr(args, "wall_clock_cap_s", None)
    cap = 150.0 if cap_arg is None else cap_arg
    wall = 14400 if wall_arg is None else wall_arg
    fallback = getattr(args, "max_fallback_shots", None)
    if fallback is None:
        fallback = max(1, int(math.ceil(int(total or 0) * 0.2)))
    summary = (
        f"补齐第 {ep['episode_no']} 集到全片可用："
        f"当前 A={grades.get('A', 0)} B={grades.get('B', 0)} 未覆盖={grades.get('C', uncovered)}；"
        f"授权预算 ¥{float(cap):.0f}，时长墙 {float(wall)/3600:.0f}h，B 级配额 {fallback}"
    )
    if allow_edit:
        summary += "；已授权微调分镜"
    summary += "。不会自动拼接成片或创建交付包。"
    return PreflightResult(
        command="video.complete_episode",
        allowed=True,
        risk=risk,
        summary=summary,
        estimated_cost_cny=estimated,
        affected=AffectedScope(episodes=[args.episode_id], shot_count=int(uncovered or 0)),
        preconditions=[
            PreconditionCheck(
                key="storyboard_confirmed",
                passed=confirmed,
                message="分镜已确认" if confirmed else "分镜未确认",
            ),
            PreconditionCheck(
                key="has_shots",
                passed=int(total or 0) > 0,
                message=f"共 {total} 镜",
            ),
        ],
        state_fingerprint=_fp({
            "episode_id": args.episode_id,
            "status": ep["status"],
            "storyboard_artifact_id": ep["storyboard_artifact_id"],
            "grades": grades,
            "budget_cap_cny": cap,
            "wall_clock_cap_s": wall,
            "max_fallback_shots": fallback,
            "allow_storyboard_edit": allow_edit,
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
    )


def video_complete_project(args) -> PreflightResult:
    conn = get_conn()
    project = conn.execute(
        "SELECT id, name FROM projects WHERE id=?", (args.project_id,)
    ).fetchone()
    if not project:
        return PreflightResult(
            command="video.complete_project",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="项目不存在",
            state_fingerprint=_fp({"project_id": args.project_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="项目不存在",
        )
    rows = conn.execute(
        """SELECT id, episode_no, status FROM episodes
           WHERE project_id=? ORDER BY episode_no""",
        (args.project_id,),
    ).fetchall()
    episode_ids = getattr(args, "episode_ids", None)
    if episode_ids:
        wanted = set(episode_ids)
        rows = [r for r in rows if r["id"] in wanted]
    eligible = [r for r in rows if r["status"] in {"confirmed", "generating", "done", "mixed"}]
    global_arg = getattr(args, "global_budget_cap_cny", None)
    per_arg = getattr(args, "per_episode_cap_cny", None)
    global_cap = float(500 if global_arg is None else global_arg)
    per_cap = float(150 if per_arg is None else per_arg)
    allow_edit = bool(getattr(args, "allow_storyboard_edit", False))
    estimated = round(min(global_cap, max(1, len(eligible)) * per_cap * 0.65), 2)
    try:
        from app.video_cost_model import predict_episode_completion_cost
        from app.video_supervisor import rebuild_coverage_ledger
        total_est = 0.0
        for r in eligible:
            ledger = rebuild_coverage_ledger(r["id"])
            if ledger.covered_within_quota():
                continue
            uncovered_ids = [
                e.shot_id for e in ledger.entries
                if e.grade == "C" or e.video_stale or e.chain_stale
            ]
            pred = predict_episode_completion_cost(r["id"], uncovered_shot_ids=uncovered_ids)
            total_est += float(pred.get("expected_cny") or 0)
        if total_est > 0:
            estimated = min(global_cap, total_est)
    except Exception:  # noqa: BLE001
        pass
    risk = RiskLevel.R3_DESTRUCTIVE if allow_edit else RiskLevel.R2_MATERIAL
    summary = (
        f"跨集补齐「{project['name']}」："
        f"{len(eligible)}/{len(rows)} 集可补齐，全局预算 ¥{global_cap:.0f}，"
        f"单集上限 ¥{per_cap:.0f}。串行启动，不自动成片/交付。"
    )
    return PreflightResult(
        command="video.complete_project",
        allowed=bool(eligible),
        risk=risk,
        summary=summary if eligible else "没有可补齐的已确认剧集",
        estimated_cost_cny=estimated if eligible else 0,
        affected=AffectedScope(
            projects=[args.project_id],
            episodes=[r["id"] for r in eligible],
            shot_count=0,
        ),
        preconditions=[
            PreconditionCheck(
                key="has_eligible_episodes",
                passed=bool(eligible),
                message=f"可补齐 {len(eligible)} 集" if eligible else "无可补齐剧集",
            ),
        ],
        state_fingerprint=_fp({
            "project_id": args.project_id,
            "episode_ids": [r["id"] for r in eligible],
            "global_budget_cap_cny": global_cap,
            "per_episode_cap_cny": per_cap,
            "allow_storyboard_edit": allow_edit,
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
        denial_code=None if eligible else "no_eligible_episodes",
        denial_message=None if eligible else "没有可补齐的已确认剧集",
    )


def storyboard_confirm(args) -> PreflightResult:
    conn = get_conn()
    ep = conn.execute(
        "SELECT id, episode_no, status FROM episodes WHERE id=?", (args.episode_id,)
    ).fetchone()
    if not ep:
        return PreflightResult(
            command="storyboard.confirm",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    shots = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (args.episode_id,)
    ).fetchone()["c"]
    forced = bool(getattr(args, "force", False))
    return PreflightResult(
        command="storyboard.confirm",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary=(
            f"带风险强行确认第 {ep['episode_no']} 集分镜（{shots} 镜）并解锁付费视频阶段"
            if forced else f"确认第 {ep['episode_no']} 集分镜（{shots} 镜）并解锁付费视频阶段"
        ),
        affected=AffectedScope(episodes=[args.episode_id], shot_count=int(shots or 0)),
        warnings=(
            ["仍有待修复必检问题；强行确认会记录人工风险承担", "确认后将允许产生付费媒体任务"]
            if forced else ["确认后将允许产生付费媒体任务"]
        ),
        state_fingerprint=_fp({"episode_id": args.episode_id, "status": ep["status"], "shots": shots}),
        requires_confirmation=False,
        confirmation_policy=ConfirmationPolicy.NEVER,
    )


def shot_update(args) -> PreflightResult:
    conn = get_conn()
    shot = conn.execute(
        "SELECT id, shot_no, episode_id, storyboard_artifact_id FROM shots WHERE id=?",
        (args.shot_id,),
    ).fetchone()
    if not shot:
        return PreflightResult(
            command="shot.update",
            allowed=False,
            risk=RiskLevel.R1_REVERSIBLE,
            summary="镜头不存在",
            state_fingerprint=_fp({"shot_id": args.shot_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="not_found",
            denial_message="镜头不存在",
        )
    media = conn.execute(
        "SELECT COUNT(*) AS c FROM shot_versions WHERE shot_id=?", (args.shot_id,)
    ).fetchone()["c"]
    scenes = conn.execute(
        "SELECT COUNT(*) AS c FROM shot_scenes WHERE shot_id=?", (args.shot_id,)
    ).fetchone()["c"]
    invalidated = int(media or 0) + int(scenes or 0)
    return PreflightResult(
        command="shot.update",
        allowed=True,
        risk=RiskLevel.R2_MATERIAL if invalidated else RiskLevel.R1_REVERSIBLE,
        summary=f"保存第 {shot['shot_no']} 镜修改"
        + (f"；将失效 {invalidated} 个媒体产物" if invalidated else ""),
        affected=AffectedScope(
            episodes=[shot["episode_id"]],
            shots=[args.shot_id],
            shot_count=1,
            invalidated_artifacts=invalidated,
        ),
        warnings=["保存后需重新确认分镜才能付费生成"] if invalidated else [],
        state_fingerprint=_fp({
            "shot_id": args.shot_id,
            "artifact": shot["storyboard_artifact_id"],
            "media": media,
            "scenes": scenes,
            "expected_version": getattr(args, "expected_version", None),
        }),
        requires_confirmation=False,
        confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
    )


def screenplay_update(args) -> PreflightResult:
    import json
    from app.evidence import repository as evidence_repository

    conn = get_conn()
    ep = conn.execute(
        "SELECT id, episode_no, status, screenplay_json, screenplay_artifact_id, "
        "screenplay_updated_at, active_screenplay_run_id, active_storyboard_run_id, "
        "screenplay_publish_fence "
        "FROM episodes WHERE id=?",
        (args.episode_id,),
    ).fetchone()
    if not ep:
        return PreflightResult(
            command="screenplay.update",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    active_screenplay_run = evidence_repository.get_active_scoped_run(
        ep["active_screenplay_run_id"],
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=args.episode_id,
        conn=conn,
    )
    if active_screenplay_run:
        return PreflightResult(
            command="screenplay.update",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="剧本流程仍在运行，不能覆盖其工作副本",
            affected=AffectedScope(
                episodes=[args.episode_id],
                extra={
                    "active_run_id": active_screenplay_run["id"],
                    "active_run_status": active_screenplay_run["status"],
                },
            ),
            state_fingerprint=_fp({
                "episode_id": args.episode_id,
                "active_run": active_screenplay_run,
            }),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="SCREENPLAY_TASK_ACTIVE",
            denial_message="剧本流程正在运行；请先停止并等待任务退出",
        )
    current_version = ep["screenplay_artifact_id"] or ""
    expected_version = getattr(args, "expected_version", None)
    if expected_version is not None and str(expected_version) != str(current_version):
        conflict_before = json.loads(ep["screenplay_json"] or "{}")
        conflict_after = dict(getattr(args, "screenplay", None) or {})
        conflict_diff = [
            {"field": key, "section": {
                "plot_spine": "主线", "full_script_text": "正文",
                "scene_outline": "场次", "source_basis": "依据",
            }.get(key, "依据与状态")}
            for key in sorted(set(conflict_before) | set(conflict_after))
            if key not in {"created_at", "updated_at"}
            and conflict_before.get(key) != conflict_after.get(key)
        ]
        return PreflightResult(
            command="screenplay.update",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="当前发布版已变化，不能用陈旧草稿覆盖",
            affected=AffectedScope(
                episodes=[args.episode_id],
                extra={
                    "conflict": True,
                    "expected_version": expected_version,
                    "current_version": current_version,
                    "diff": conflict_diff,
                },
            ),
            state_fingerprint=_fp({
                "episode_id": args.episode_id,
                "artifact": current_version,
                "expected_version": expected_version,
            }),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="version_conflict",
            denial_message="剧本版本冲突：草稿已保留，请查看差异后重新建立基线",
        )
    before = json.loads(ep["screenplay_json"] or "{}")
    after = dict(getattr(args, "screenplay", None) or {})
    for payload in (before, after):
        payload.pop("created_at", None)
        payload.pop("updated_at", None)
    changed_fields = sorted(
        key for key in (set(before) | set(after)) if before.get(key) != after.get(key)
    )
    if not changed_fields:
        return PreflightResult(
            command="screenplay.update",
            allowed=True,
            risk=RiskLevel.R1_REVERSIBLE,
            summary="剧本内容无变化，不会创建新版本或清空下游",
            affected=AffectedScope(
                episodes=[args.episode_id], extra={"unchanged": True, "diff": []}
            ),
            state_fingerprint=_fp({
                "episode_id": args.episode_id,
                "artifact": current_version,
                "unchanged": True,
            }),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
        )
    shots = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (args.episode_id,)
    ).fetchone()["c"]
    versions = conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
        (args.episode_id,),
    ).fetchone()["c"]
    references = conn.execute(
        """SELECT COUNT(*) AS c FROM shot_scenes ss
           JOIN shots s ON s.id=ss.shot_id WHERE s.episode_id=?""",
        (args.episode_id,),
    ).fetchone()["c"]
    active_run_kinds: list[str] = []
    if ep["status"] == "scripting" or ep["active_storyboard_run_id"]:
        active_run_kinds.append("storyboard")
    try:
        from app import task_registry
        for kind in ("storyboard", "video_completion"):
            if task_registry.active(kind, args.episode_id) and kind not in active_run_kinds:
                active_run_kinds.append(kind)
    except Exception:  # noqa: BLE001 -- 持久状态仍作为保守回退
        pass
    active_runs = len(active_run_kinds)
    has_downstream = int(shots or 0) > 0
    return PreflightResult(
        command="screenplay.update",
        allowed=True,
        risk=RiskLevel.R2_MATERIAL if has_downstream else RiskLevel.R1_REVERSIBLE,
        summary=f"保存第 {ep['episode_no']} 集剧本"
        + (f"；将清空 {shots} 镜及 {versions} 个媒体产物" if has_downstream else ""),
        affected=AffectedScope(
            episodes=[args.episode_id],
            shot_count=int(shots or 0),
            invalidated_artifacts=int(versions or 0) + int(references or 0),
            extra={
                "diff": changed_fields,
                "active_runs": active_runs,
                "active_run_kinds": active_run_kinds,
                "reference_images": int(references or 0),
                "media_versions": int(versions or 0),
                "rerun_scope": "本集全部分镜及派生媒体" if has_downstream else "无",
                "stop_downstream_first": bool(active_runs),
            },
        ),
        warnings=(
            (["发布前将先建立写入栅栏，取消并等待下游任务终止"] if active_runs else [])
            + (["修改剧本会清空本集分镜与媒体"] if has_downstream else [])
        ),
        state_fingerprint=_fp({
            "episode_id": args.episode_id,
            "artifact": ep["screenplay_artifact_id"],
            "updated_at": ep["screenplay_updated_at"],
            "shots": shots,
            "versions": versions,
            "references": references,
            "active_run": ep["active_storyboard_run_id"],
            "fence": ep["screenplay_publish_fence"],
            "expected_version": expected_version,
            "diff": changed_fields,
        }),
        requires_confirmation=False,
        confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
    )


def screenplay_repair_draft(args) -> PreflightResult:
    """草稿修复最终可能替换发布版，复用保存影响口径并使用独立命令身份。"""
    result = screenplay_update(args)
    result.command = "screenplay.repair_draft"
    if result.allowed:
        result.summary = result.summary.replace("保存", "修复并发布", 1)
        result.warnings = [
            "Repair 只修改 QA 指出的问题；每次 Patch 后都会重新执行只读 QA",
            *result.warnings,
        ]
    return result


def screenplay_generate(args) -> PreflightResult:
    """Read-only generation sizing and reusable-artifact projection."""
    from app.evidence import repository as evidence_repository

    conn = get_conn()
    ep = conn.execute(
        """SELECT id,episode_no,project_id,screenplay_status,
                  screenplay_artifact_id,storyboard_artifact_id,
                  active_screenplay_run_id,source_chapters
             FROM episodes WHERE id=?""",
        (args.episode_id,),
    ).fetchone()
    if not ep:
        return PreflightResult(
            command="screenplay.generate",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    from app.domain.screenplay_ops import _screenplay_generation_preflight

    projection = _screenplay_generation_preflight(args.episode_id)
    input_projection = dict(projection.get("input") or {})
    cast_impact = dict(projection.get("cast_impact") or {})
    reusable = dict(projection.get("reusable_validated_artifacts") or {})
    run = evidence_repository.get_active_scoped_run(
        ep["active_screenplay_run_id"],
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=args.episode_id,
        conn=conn,
    )
    active_run = bool(run)
    downstream_impact = bool(ep["screenplay_artifact_id"] or ep["storyboard_artifact_id"])
    return PreflightResult(
        command="screenplay.generate",
        allowed=not active_run,
        risk=RiskLevel.R2_MATERIAL,
        summary=(
            f"第 {ep['episode_no']} 集将按 {input_projection.get('source_segment_count', 0)} 个 SRC，"
            f"预计 {input_projection.get('estimated_blueprint_shards', 0)} 个蓝图片、"
            f"{input_projection.get('estimated_scene_writing_shards', 0)} 个场次写作片生成"
        ),
        affected=AffectedScope(
            projects=[ep["project_id"]],
            episodes=[args.episode_id],
            invalidated_artifacts=int(downstream_impact),
            extra={
                **input_projection,
                "possible_character_cards": cast_impact.get("candidate_count"),
                "cast_requires_model_resolution": cast_impact.get(
                    "requires_model_resolution", True
                ),
                "reusable_validated_artifacts": reusable,
            },
        ),
        warnings=(
            ["重新发布完整剧本后，下游分镜/媒体可能失效"]
            if downstream_impact else []
        ),
        state_fingerprint=_fp({
            "episode": dict(ep),
            "active_run": run,
            "input": input_projection,
            "reusable": reusable,
        }),
        requires_confirmation=downstream_impact,
        confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
        denial_code="SCREENPLAY_ALREADY_RUNNING" if active_run else None,
        denial_message="本集已有剧本任务运行中或等待恢复" if active_run else None,
    )


def screenplay_resume(args) -> PreflightResult:
    """Read-only distinction between pre-document and document resume modes."""
    conn = get_conn()
    ep = conn.execute(
        """SELECT id,episode_no,project_id,screenplay_status,
                  active_screenplay_run_id,working_screenplay_artifact_id
             FROM episodes WHERE id=?""",
        (args.episode_id,),
    ).fetchone()
    if not ep:
        return PreflightResult(
            command="screenplay.resume",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    from app.domain.screenplay_ops import _screenplay_production_state

    state = _screenplay_production_state(args.episode_id)
    can_baseline = bool(state.get("can_resume_baseline"))
    can_repair = bool(state.get("can_resume_repair"))
    allowed = bool((can_baseline or can_repair) and not state.get("task_active"))
    mode = "baseline" if can_baseline else "finalize" if can_repair else "none"
    return PreflightResult(
        command="screenplay.resume",
        allowed=allowed,
        risk=RiskLevel.R2_MATERIAL,
        summary=(
            "继续首版场次生成" if mode == "baseline"
            else "继续完整剧本校验" if mode == "finalize"
            else "当前没有可继续的剧本流程"
        ),
        affected=AffectedScope(
            projects=[ep["project_id"]],
            episodes=[args.episode_id],
            extra={
                "resume_mode": mode,
                "phase": state.get("phase"),
                "shard_progress": state.get("shard_progress") or {},
            },
        ),
        state_fingerprint=_fp({"episode": dict(ep), "production": state}),
        requires_confirmation=False,
        confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
        denial_code=None if allowed else "SCREENPLAY_NOT_RESUMABLE",
        denial_message=None if allowed else "当前没有可继续的剧本流程，或任务仍在运行",
    )


def screenplay_delete(args) -> PreflightResult:
    conn = get_conn()
    ep = conn.execute(
        "SELECT id, episode_no, status, screenplay_json, screenplay_status, "
        "screenplay_artifact_id, working_screenplay_artifact_id, "
        "published_screenplay_artifact_id, screenplay_production_revision_id, "
        "screenplay_completion_certificate_id, active_screenplay_run_id, "
        "screenplay_updated_at "
        "FROM episodes WHERE id=?",
        (args.episode_id,),
    ).fetchone()
    if not ep:
        return PreflightResult(
            command="screenplay.delete",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    active_revision = conn.execute(
        "SELECT id, baseline_generation_count, working_artifact_id "
        "FROM production_revisions "
        "WHERE episode_id=? AND kind='screenplay' AND status='active' "
        "ORDER BY updated_at DESC LIMIT 1",
        (args.episode_id,),
    ).fetchone()
    shots = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (args.episode_id,)
    ).fetchone()["c"]
    has_clearable_state = bool(
        ep["screenplay_json"]
        or ep["screenplay_artifact_id"]
        or ep["working_screenplay_artifact_id"]
        or ep["published_screenplay_artifact_id"]
        or ep["screenplay_production_revision_id"]
        or ep["screenplay_completion_certificate_id"]
        or ep["active_screenplay_run_id"]
        or active_revision
        or int(shots or 0)
    )
    if not has_clearable_state:
        return PreflightResult(
            command="screenplay.delete",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="本集没有可删除的剧本",
            state_fingerprint=_fp({
                "episode_id": args.episode_id,
                "screenplay_status": ep["screenplay_status"],
                "active_revision": None,
                "active_run": None,
                "empty": True,
            }),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="invalid_state",
            denial_message="本集没有可删除的剧本",
        )
    versions = conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
        (args.episode_id,),
    ).fetchone()["c"]
    warnings = ["删除后当前剧本不可恢复；历史证据仅供审计，不能直接还原"]
    if int(shots or 0) or int(versions or 0):
        warnings.append("本集分镜、参考图、视频和成片会一并清空")
    return PreflightResult(
        command="screenplay.delete",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary=f"删除第 {ep['episode_no']} 集当前剧本"
        + (f"；同时清空 {shots} 镜和 {versions} 个媒体版本" if shots or versions else ""),
        affected=AffectedScope(
            episodes=[args.episode_id],
            shot_count=int(shots or 0),
            invalidated_artifacts=int(versions or 0),
        ),
        warnings=warnings,
        state_fingerprint=_fp({
            "episode_id": args.episode_id,
            "status": ep["status"],
            "screenplay_status": ep["screenplay_status"],
            "artifact": ep["screenplay_artifact_id"],
            "working_artifact": ep["working_screenplay_artifact_id"],
            "published_artifact": ep["published_screenplay_artifact_id"],
            "revision_id": ep["screenplay_production_revision_id"],
            "completion_certificate_id": ep["screenplay_completion_certificate_id"],
            "active_run": ep["active_screenplay_run_id"],
            "active_revision": dict(active_revision) if active_revision else None,
            "updated_at": ep["screenplay_updated_at"],
            "shots": shots,
            "versions": versions,
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
    )


def bible_update(args) -> PreflightResult:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, bible_version, bible_artifact_id, bible_status FROM projects WHERE id=?",
        (args.project_id,),
    ).fetchone()
    if not row:
        return PreflightResult(
            command="bible.update",
            allowed=False,
            risk=RiskLevel.R2_MATERIAL,
            summary="项目不存在",
            state_fingerprint=_fp({"project_id": args.project_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
            denial_code="not_found",
            denial_message="项目不存在",
        )
    portraits = conn.execute(
        "SELECT COUNT(*) AS c FROM character_portraits WHERE project_id=?", (args.project_id,)
    ).fetchone()["c"]
    scenes = conn.execute(
        "SELECT COUNT(*) AS c FROM scene_references WHERE project_id=?", (args.project_id,)
    ).fetchone()["c"]
    invalidated = int(portraits or 0) + int(scenes or 0)
    return PreflightResult(
        command="bible.update",
        allowed=True,
        risk=RiskLevel.R2_MATERIAL if invalidated else RiskLevel.R1_REVERSIBLE,
        summary="保存人物谱"
        + (f"；可能失效 {invalidated} 个定妆/场景产物" if invalidated else ""),
        affected=AffectedScope(
            projects=[args.project_id],
            invalidated_artifacts=invalidated,
        ),
        state_fingerprint=_fp({
            "project_id": args.project_id,
            "bible_version": row["bible_version"],
            "artifact": row["bible_artifact_id"],
            "portraits": portraits,
            "scenes": scenes,
        }),
        requires_confirmation=False,
        confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
    )


def delivery_review(args) -> PreflightResult:
    conn = get_conn()
    ep = conn.execute(
        "SELECT id, episode_no, delivery_status FROM episodes WHERE id=?", (args.episode_id,)
    ).fetchone()
    if not ep:
        return PreflightResult(
            command="delivery.review",
            allowed=False,
            risk=RiskLevel.R3_DESTRUCTIVE,
            summary="剧集不存在",
            state_fingerprint=_fp({"episode_id": args.episode_id, "missing": True}),
            requires_confirmation=False,
            confirmation_policy=ConfirmationPolicy.ALWAYS,
            denial_code="not_found",
            denial_message="剧集不存在",
        )
    decision = getattr(args, "decision", None) or getattr(args, "action", None) or "unknown"
    packages = conn.execute(
        "SELECT id FROM delivery_packages WHERE episode_id=? ORDER BY created_at DESC LIMIT 5",
        (args.episode_id,),
    ).fetchall()
    package_ids = [p["id"] for p in packages]
    return PreflightResult(
        command="delivery.review",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary=f"对第 {ep['episode_no']} 集执行交付决定：{decision}",
        affected=AffectedScope(episodes=[args.episode_id], packages=package_ids),
        warnings=["交付决定不可由含糊指令触发，请确认决定类型"],
        state_fingerprint=_fp({
            "episode_id": args.episode_id,
            "delivery_status": ep["delivery_status"],
            "decision": decision,
            "packages": package_ids,
        }),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
    )


# catalog 挂载用：命令名 → preflight 函数
PREFLIGHT_MAP: dict[str, Any] = {
    "project.delete": project_delete,
    "episode.plan": episode_plan,
    "video.clear_episode": video_clear_episode,
    "video.clear_shot": video_clear_shot,
    "video.generate_shot": video_generate_shot,
    "video.generate_episode": video_generate_episode,
    "video.complete_episode": video_complete_episode,
    "video.complete_project": video_complete_project,
    "storyboard.confirm": storyboard_confirm,
    "shot.update": shot_update,
    "screenplay.update": screenplay_update,
    "screenplay.generate": screenplay_generate,
    "screenplay.resume": screenplay_resume,
    "screenplay.repair_draft": screenplay_repair_draft,
    "screenplay.delete": screenplay_delete,
    "bible.update": bible_update,
    "delivery.review": delivery_review,
}
