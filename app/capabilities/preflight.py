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


def video_clear_episode(args) -> PreflightResult:
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
    conn = get_conn()
    shot = conn.execute(
        "SELECT id, shot_no, episode_id FROM shots WHERE id=?", (args.shot_id,)
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
    # 粗估：单镜视频默认成本上限，实际以预算预留为准
    estimated = 3.6
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
    pending = conn.execute(
        """SELECT COUNT(*) AS c FROM shots
           WHERE episode_id=? AND (adopted_version_id IS NULL OR adopted_version_id='')""",
        (args.episode_id,),
    ).fetchone()["c"]
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (args.episode_id,)
    ).fetchone()["c"]
    estimated = round(max(int(pending or 0), 1) * 3.6, 2)
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
    return PreflightResult(
        command="storyboard.confirm",
        allowed=True,
        risk=RiskLevel.R3_DESTRUCTIVE,
        summary=f"确认第 {ep['episode_no']} 集分镜（{shots} 镜）并解锁付费视频阶段",
        affected=AffectedScope(episodes=[args.episode_id], shot_count=int(shots or 0)),
        warnings=["确认后将允许产生付费媒体任务"],
        state_fingerprint=_fp({"episode_id": args.episode_id, "status": ep["status"], "shots": shots}),
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy.ALWAYS,
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
    conn = get_conn()
    ep = conn.execute(
        "SELECT id, episode_no, status, screenplay_artifact_id, screenplay_updated_at FROM episodes WHERE id=?",
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
    shots = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (args.episode_id,)
    ).fetchone()["c"]
    versions = conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""",
        (args.episode_id,),
    ).fetchone()["c"]
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
            invalidated_artifacts=int(versions or 0),
        ),
        warnings=["修改剧本会清空本集分镜与媒体"] if has_downstream else [],
        state_fingerprint=_fp({
            "episode_id": args.episode_id,
            "artifact": ep["screenplay_artifact_id"],
            "updated_at": ep["screenplay_updated_at"],
            "shots": shots,
            "expected_version": getattr(args, "expected_version", None),
        }),
        requires_confirmation=False,
        confirmation_policy=ConfirmationPolicy.WHEN_IMPACT,
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
    "video.clear_episode": video_clear_episode,
    "video.clear_shot": video_clear_shot,
    "video.generate_shot": video_generate_shot,
    "video.generate_episode": video_generate_episode,
    "storyboard.confirm": storyboard_confirm,
    "shot.update": shot_update,
    "screenplay.update": screenplay_update,
    "bible.update": bible_update,
    "delivery.review": delivery_review,
}
