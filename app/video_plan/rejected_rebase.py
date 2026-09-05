"""被供应商确定性拒绝的镜头不再阻塞它的下游依赖（2026-09-05 我欲封天第 15 集）。

第 16 镜在 3 个独立任务上收到逐字相同的拒绝 → ``provider_create_state='model_rejected'``，
按约定视为跳过、成片不含本镜；但计划里第 17 镜 ``depends_on_shot_id`` 指向第 16 镜，
``video_plan_dependencies.upstream_adopted_version_id`` 永远等不到，覆盖账本判
``dependency_ready=False``，第 17–21 镜整条链一镜没派发就收口成 PARTIAL。

判据挂在「这件事本身」：一个镜头一旦 ``model_rejected`` 且没有采纳版本，所有依赖它的
镜头改为依赖它自己的上游（沿链跳过连续被拒的镜头）；上游为空就把依赖标成
``dropped_after_rejection``、不再等锚点。

**只改运行时的依赖解析表 ``video_plan_dependencies``，不动已发布的 ``shot_video_generation_plans``。**
第一版改了计划行的 ``depends_on_shot_id``，它在 ``shot_video_execution_contract_fingerprint``
里，完成授权用这个指纹复核发布资格，改一下整集就 RELEASE_QUALIFICATION_CHANGED（第 15 集
实测两次运行连续因此收口）。读方（覆盖账本、入队链依赖、派发锚点）一律以依赖行为准，
计划行只是没有依赖行时的回退。幂等：重复调用不再产生变化。写在调用方连接上、不提交。
"""
from __future__ import annotations

from typing import Any

from app.db import now

DROPPED_DEPENDENCY_KIND = "dropped_after_rejection"


def rejected_unadopted_shot_ids(conn: Any, episode_id: str) -> set[str]:
    rows = conn.execute(
        """SELECT DISTINCT s.id FROM shots s JOIN jobs j ON j.shot_id=s.id
            WHERE s.episode_id=? AND s.adopted_version_id IS NULL
              AND j.kind='video' AND j.provider_create_state='model_rejected'""",
        (episode_id,),
    ).fetchall()
    return {str(row["id"]) for row in rows}


def effective_dependency(conn: Any, *, episode_video_plan_id: str, shot_id: str,
                         planned_dependency: str | None) -> str | None:
    """本镜当前真正等待的上游：依赖行优先（可能已改挂/已放弃），没有依赖行才用计划行。"""
    row = conn.execute(
        """SELECT depends_on_shot_id, dependency_kind FROM video_plan_dependencies
            WHERE episode_video_plan_id=? AND shot_id=? ORDER BY created_at LIMIT 1""",
        (episode_video_plan_id, shot_id),
    ).fetchone()
    if row is None:
        return planned_dependency
    if str(row["dependency_kind"] or "") == DROPPED_DEPENDENCY_KIND:
        return None
    return str(row["depends_on_shot_id"]) if row["depends_on_shot_id"] else planned_dependency


def _resolve_past_rejected(shot_id: str, upstream: dict[str, str | None], rejected: set[str]) -> str | None:
    seen: set[str] = set()
    current: str | None = shot_id
    while current is not None and current in rejected and current not in seen:
        seen.add(current)
        current = upstream.get(current)
    return current


def _apply_rebase(conn: Any, *, dep_id: str, shot_plan_id: str, target: str | None, adopted: str | None) -> None:
    if target is None:
        conn.execute(
            """UPDATE video_plan_dependencies
                  SET dependency_kind=?, upstream_adopted_version_id=NULL, resolved_at=? WHERE id=?""",
            (DROPPED_DEPENDENCY_KIND, now(), dep_id),
        )
    else:
        conn.execute(
            """UPDATE video_plan_dependencies
                  SET depends_on_shot_id=?, upstream_adopted_version_id=?, resolved_at=? WHERE id=?""",
            (target, adopted, now() if adopted else None, dep_id),
        )
    if target is None or adopted:
        # status 不在执行契约指纹里（release_manifest 明确剔除），与 replan 采纳同步同款写法。
        conn.execute(
            "UPDATE shot_video_generation_plans SET status='ready', updated_at=? WHERE id=?",
            (now(), shot_plan_id),
        )


def rebase_dependencies_past_rejected_shots(conn: Any, episode_id: str) -> list[dict[str, Any]]:
    """把依赖被拒镜头的依赖行改挂到被拒镜头自己的上游；返回改动清单（空表示无事可做）。"""
    plan = conn.execute(
        """SELECT id FROM episode_video_generation_plans
            WHERE episode_id=? AND status='valid'
            ORDER BY plan_revision DESC, created_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    if plan is None:
        return []
    rejected = rejected_unadopted_shot_ids(conn, episode_id)
    if not rejected:
        return []
    rows = conn.execute(
        """SELECT p.id AS shot_plan_id, p.shot_id, p.depends_on_shot_id AS planned,
                  d.id AS dep_id, d.depends_on_shot_id AS effective, d.dependency_kind
             FROM shot_video_generation_plans p
             LEFT JOIN video_plan_dependencies d ON d.shot_plan_id=p.id
            WHERE p.episode_video_plan_id=? ORDER BY p.shot_no""",
        (plan["id"],),
    ).fetchall()
    upstream: dict[str, str | None] = {}
    for row in rows:
        dropped = str(row["dependency_kind"] or "") == DROPPED_DEPENDENCY_KIND
        upstream[str(row["shot_id"])] = None if dropped else str(row["effective"] or row["planned"] or "") or None
    changes: list[dict[str, Any]] = []
    for row in rows:
        shot_id = str(row["shot_id"])
        dependency = upstream.get(shot_id)
        if not dependency or dependency not in rejected or not row["dep_id"]:
            continue
        target = _resolve_past_rejected(dependency, upstream, rejected)
        adopted = None
        if target is not None:
            shot = conn.execute("SELECT adopted_version_id FROM shots WHERE id=?", (target,)).fetchone()
            adopted = shot["adopted_version_id"] if shot else None
        _apply_rebase(conn, dep_id=str(row["dep_id"]), shot_plan_id=str(row["shot_plan_id"]),
                      target=target, adopted=adopted)
        upstream[shot_id] = target
        changes.append({"shot_id": shot_id, "from": dependency, "to": target, "ready": target is None or bool(adopted)})
    return changes
