"""被供应商确定性拒绝的镜头不再阻塞它的下游依赖（2026-09-05 我欲封天第 15 集）。

第 16 镜在 3 个独立任务上收到逐字相同的拒绝 → ``provider_create_state='model_rejected'``，
按约定视为跳过、成片不含本镜；但计划里第 17 镜 ``depends_on_shot_id`` 指向第 16 镜，
``video_plan_dependencies.upstream_adopted_version_id`` 永远等不到，覆盖账本判
``dependency_ready=False``，第 17–21 镜整条链一镜没派发就收口成 PARTIAL。

判据挂在「这件事本身」：一个镜头一旦 ``model_rejected`` 且没有采纳版本，所有依赖它的
镜头改为依赖它自己的上游（沿链跳过连续被拒的镜头）；上游为空就删掉依赖行、不再等锚点。
幂等：重复调用不再产生变化。写在调用方给的连接上、不提交——事务归调用方。
"""
from __future__ import annotations

from typing import Any

from app.db import now


def rejected_unadopted_shot_ids(conn: Any, episode_id: str) -> set[str]:
    rows = conn.execute(
        """SELECT DISTINCT s.id FROM shots s JOIN jobs j ON j.shot_id=s.id
            WHERE s.episode_id=? AND s.adopted_version_id IS NULL
              AND j.kind='video' AND j.provider_create_state='model_rejected'""",
        (episode_id,),
    ).fetchall()
    return {str(row["id"]) for row in rows}


def _resolve_past_rejected(shot_id: str, upstream: dict[str, str | None], rejected: set[str]) -> str | None:
    seen: set[str] = set()
    current: str | None = shot_id
    while current is not None and current in rejected and current not in seen:
        seen.add(current)
        current = upstream.get(current)
    return current


def rebase_dependencies_past_rejected_shots(conn: Any, episode_id: str) -> list[dict[str, Any]]:
    """把依赖被拒镜头的计划依赖改挂到被拒镜头自己的上游；返回改动清单（空表示无事可做）。"""
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
    upstream: dict[str, str | None] = {
        str(row["shot_id"]): (str(row["depends_on_shot_id"]) if row["depends_on_shot_id"] else None)
        for row in conn.execute(
            "SELECT shot_id, depends_on_shot_id FROM shot_video_generation_plans WHERE episode_video_plan_id=?",
            (plan["id"],),
        ).fetchall()
    }
    changes: list[dict[str, Any]] = []
    for shot_id, dependency in list(upstream.items()):
        if not dependency or dependency not in rejected:
            continue
        target = _resolve_past_rejected(dependency, upstream, rejected)
        adopted = None
        if target is not None:
            row = conn.execute("SELECT adopted_version_id FROM shots WHERE id=?", (target,)).fetchone()
            adopted = row["adopted_version_id"] if row else None
        ready = target is None or bool(adopted)
        conn.execute(
            """UPDATE shot_video_generation_plans
                  SET depends_on_shot_id=?, status=CASE WHEN ? THEN 'ready' ELSE status END, updated_at=?
                WHERE episode_video_plan_id=? AND shot_id=?""",
            (target, int(ready), now(), plan["id"], shot_id),
        )
        if target is None:
            conn.execute(
                "DELETE FROM video_plan_dependencies WHERE episode_video_plan_id=? AND shot_id=? AND depends_on_shot_id=?",
                (plan["id"], shot_id, dependency),
            )
        else:
            conn.execute(
                """UPDATE video_plan_dependencies
                      SET depends_on_shot_id=?, upstream_adopted_version_id=?, resolved_at=?
                    WHERE episode_video_plan_id=? AND shot_id=? AND depends_on_shot_id=?""",
                (target, adopted, now() if adopted else None, plan["id"], shot_id, dependency),
            )
        upstream[shot_id] = target
        changes.append({"shot_id": shot_id, "from": dependency, "to": target, "ready": ready})
    return changes
