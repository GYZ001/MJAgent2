"""Local replan revisions and adopted-revision reconciliation.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.db import get_conn, new_id, now

from .capability_snapshot import capability_snapshot_by_id
from .models import EpisodeVideoGenerationPlan
from .primitives import _hash, _json
from .publish import load_latest_plan, publish_plan
from .release_manifest import current_storyboard_release_manifest
from .staleness import verify_episode_plan_is_current
from .validate import validate_episode_plan


def create_local_replan_revision(
    shot_id: str,
    *,
    reason: str,
    conn=None,
    idempotency_key: str | None = None,
) -> EpisodeVideoGenerationPlan:
    """Create a new plan revision while changing only one shot's input identity."""
    db = conn or get_conn()
    shot = db.execute(
        "SELECT episode_id FROM shots WHERE id=?",
        (shot_id,),
    ).fetchone()
    if not shot:
        raise ValueError(f"镜头不存在：{shot_id}")
    current = load_latest_plan(shot["episode_id"], conn=db)
    if (
        not current
        or current.status != "valid"
        or not verify_episode_plan_is_current(current, conn=db)
    ):
        raise ValueError("单镜重做缺少当前有效的视频模式计划")
    operation_key = str(idempotency_key or "").strip()
    operation_fingerprint = (
        _hash({
            "episode_id": str(shot["episode_id"]),
            "shot_id": shot_id,
            "reason": reason,
            "idempotency_key": operation_key,
        })
        if operation_key
        else ""
    )
    if operation_fingerprint:
        existing = db.execute(
            """SELECT id FROM episode_video_generation_plans
               WHERE episode_id=? AND planner_model='local-shot-replan'
                 AND planner_prompt_fingerprint=?
               ORDER BY plan_revision DESC LIMIT 1""",
            (shot["episode_id"], operation_fingerprint),
        ).fetchone()
        if existing and current.episode_video_plan_id == str(existing["id"]):
            return current
    next_revision = int(db.execute(
        "SELECT COALESCE(MAX(plan_revision),0)+1 n FROM episode_video_generation_plans WHERE episode_id=?",
        (shot["episode_id"],),
    ).fetchone()["n"])
    replacement = current.model_copy(deep=True)
    replacement.episode_video_plan_id = new_id("evp")
    replacement.plan_revision = next_revision
    replacement.status = "draft"
    replacement.created_at = now()
    replacement.blockers = []
    replacement.planner_provider = "deterministic"
    replacement.planner_model = "local-shot-replan"
    replacement.planner_prompt_fingerprint = operation_fingerprint or _hash({
        "source_plan_id": current.episode_video_plan_id,
        "shot_id": shot_id,
        "reason": reason,
        "plan_revision": next_revision,
    })
    target = None
    for item in replacement.shots:
        item.shot_plan_id = new_id("svp")
        item.episode_video_plan_id = replacement.episode_video_plan_id
        item.plan_revision = next_revision
        if item.shot_id != shot_id:
            continue
        target = item
        item.actual_mode = None
        item.status = "planned"
        item.reason_codes = [*item.reason_codes, "LOCAL_REPLAN_FOR_REDO"]
        item.input_revision_fingerprints["local_replan_revision"] = _hash({
            "reason": reason,
            "revision": next_revision,
            "created_at": replacement.created_at,
        })
    if target is None:
        raise ValueError("当前视频模式计划未覆盖待重做镜头")
    snapshot = capability_snapshot_by_id(
        replacement.capability_snapshot_id, conn=db,
    )
    rows = db.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (shot["episode_id"],),
    ).fetchall()
    if snapshot is None:
        raise ValueError("当前视频模式计划的能力快照不存在")
    manifest = current_storyboard_release_manifest(str(shot["episode_id"]), conn=db)
    validate_episode_plan(
        replacement,
        list(rows),
        snapshot,
        release_manifest=manifest,
    )
    publish_plan(replacement, conn=db)
    current_by_shot_id = {item.shot_id: item for item in current.shots}
    for item in replacement.shots:
        if item.shot_id == shot_id:
            continue
        previous = current_by_shot_id.get(item.shot_id)
        if previous is None:
            continue
        boundary_rows = db.execute(
            """SELECT * FROM video_boundary_assets
               WHERE shot_plan_id=? AND qa_status='passed'
               ORDER BY created_at""",
            (previous.shot_plan_id,),
        ).fetchall()
        for boundary in boundary_rows:
            path = str(boundary["path"] or "")
            if not path or not Path(path).is_file():
                continue
            db.execute(
                """INSERT OR IGNORE INTO video_boundary_assets(
                       id,episode_video_plan_id,shot_plan_id,shot_id,role,source,
                       source_revision_id,source_shot_id,source_adopted_version_id,
                       path,url,sha256,mime,width,height,qa_status,qa_json,
                       fingerprint,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("vba"),
                    replacement.episode_video_plan_id,
                    item.shot_plan_id,
                    item.shot_id,
                    boundary["role"],
                    boundary["source"],
                    boundary["source_revision_id"],
                    boundary["source_shot_id"],
                    boundary["source_adopted_version_id"],
                    path,
                    boundary["url"],
                    boundary["sha256"],
                    boundary["mime"],
                    boundary["width"],
                    boundary["height"],
                    boundary["qa_status"],
                    boundary["qa_json"],
                    boundary["fingerprint"],
                    now(),
                ),
            )
    db.commit()
    return replacement


def _release_unsubmitted_paused_reservations_for_adopted_shot(
    db,
    *,
    shot_id: str,
    adopted_version_id: str,
) -> int:
    """Release only obsolete local work that provably never reached the provider."""
    rows = db.execute(
        """SELECT j.id
             FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
            WHERE j.shot_id=? AND j.kind='video' AND j.status='paused'
              AND j.version_id!=?
              AND j.provider_non_cancellable=0
              AND j.provider_create_state='not_started'
              AND (v.provider_task_id IS NULL OR v.provider_task_id='')
              AND NOT EXISTS (
                  SELECT 1 FROM provider_calls pc
                   WHERE pc.operation_id=j.provider_operation_id
                     AND pc.kind='video_create' AND pc.status='OK'
              )
              AND EXISTS (
                  SELECT 1 FROM budget_reservations br
                   WHERE br.job_id=j.id AND br.status IN ('reserved','running')
              )""",
        (shot_id, adopted_version_id),
    ).fetchall()
    job_ids = [str(row["id"]) for row in rows]
    if not job_ids:
        return 0
    placeholders = ",".join("?" * len(job_ids))
    stamp = now()
    db.execute(
        f"""UPDATE budget_reservations
               SET status='released',settled_at=?,actual_cost_cny=0
             WHERE job_id IN ({placeholders})
               AND status IN ('reserved','running')""",
        (stamp, *job_ids),
    )
    db.execute(
        f"""UPDATE jobs SET reserved_cost_cny=0,updated_at=?
             WHERE id IN ({placeholders})""",
        (stamp, *job_ids),
    )
    return len(job_ids)


def reconcile_adopted_revision(
    shot_id: str,
    adopted_version_id: str,
    *,
    conn=None,
) -> dict[str, Any]:
    """Bind a first adoption or stale only descendants that consumed an older adoption."""
    db = conn or get_conn()
    shot = db.execute(
        "SELECT episode_id,adopted_version_id FROM shots WHERE id=?",
        (shot_id,),
    ).fetchone()
    if shot is None:
        raise ValueError(f"镜头不存在：{shot_id}")
    current_plan = load_latest_plan(str(shot["episode_id"]), conn=db)
    if current_plan is None:
        return {"bound": 0, "stale_shot_ids": []}
    if not verify_episode_plan_is_current(current_plan, conn=db):
        raise ValueError("当前视频模式计划已过期，禁止同步采用关系")

    current_adoption = str(shot["adopted_version_id"] or "")
    if adopted_version_id == "__unadopted__":
        if current_adoption:
            raise ValueError("镜头尚有当前采用版本，禁止伪造取消采用同步")
    else:
        adopted = db.execute(
            "SELECT shot_id,status FROM shot_versions WHERE id=?",
            (adopted_version_id,),
        ).fetchone()
        if adopted is None:
            raise ValueError("采用版本不存在")
        if adopted["shot_id"] != shot_id:
            raise ValueError("采用版本不属于当前镜头")
        if adopted["status"] != "succeeded":
            raise ValueError("只能同步已成功的采用版本")
        if current_adoption != adopted_version_id:
            raise ValueError("采用版本与 shots.adopted_version_id 当前指针不一致")
        _release_unsubmitted_paused_reservations_for_adopted_shot(
            db,
            shot_id=shot_id,
            adopted_version_id=adopted_version_id,
        )

    deps = db.execute(
        """SELECT * FROM video_plan_dependencies
           WHERE episode_video_plan_id=? AND depends_on_shot_id=?""",
        (current_plan.episode_video_plan_id, shot_id),
    ).fetchall()
    stale_roots: list[str] = []
    bound = 0
    for dep in deps:
        old = dep["upstream_adopted_version_id"]
        if adopted_version_id == "__unadopted__":
            stale_roots.append(dep["shot_id"])
            continue
        if not old:
            db.execute(
                """UPDATE video_plan_dependencies
                      SET upstream_adopted_version_id=?,resolved_at=?
                    WHERE id=?""",
                (adopted_version_id, now(), dep["id"]),
            )
            row = db.execute(
                "SELECT input_fingerprints_json FROM shot_video_generation_plans WHERE id=?",
                (dep["shot_plan_id"],),
            ).fetchone()
            published_fingerprints = (
                json.loads(row["input_fingerprints_json"] or "{}")
                if row else {}
            )
            execution_fingerprints = {
                **published_fingerprints,
                "upstream_adopted_video_revision": adopted_version_id,
            }
            db.execute(
                """UPDATE shot_video_generation_plans
                      SET status='ready',updated_at=? WHERE id=?""",
                (now(), dep["shot_plan_id"]),
            )
            waiting_jobs = db.execute(
                """SELECT j.id,j.version_id,v.idem_key,v.image_inputs
                   FROM jobs j
                   JOIN shot_versions v ON v.id=j.version_id
                   WHERE j.shot_id=? AND j.kind='video'
                     AND j.status IN ('queued','waiting_retry')
                     AND j.provider_non_cancellable=0
                     AND (v.provider_task_id IS NULL OR v.provider_task_id='')
                     AND json_valid(v.image_inputs)
                     AND json_extract(v.image_inputs,'$.shot_plan_id')=?""",
                (dep["shot_id"], dep["shot_plan_id"]),
            ).fetchall()
            for job in waiting_jobs:
                try:
                    meta = json.loads(job["image_inputs"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    meta = {}
                meta["upstream_adopted_video_revision"] = adopted_version_id
                meta["after_version_id"] = adopted_version_id
                meta["input_revision_fingerprints"] = execution_fingerprints
                meta["plan_status"] = "ready"
                idem = hashlib.sha256(
                    (
                        str(job["idem_key"] or "")
                        + "|upstream_adopted_video_revision:"
                        + adopted_version_id
                    ).encode("utf-8")
                ).hexdigest()
                db.execute(
                    """UPDATE shot_versions SET idem_key=?,image_inputs=?
                       WHERE id=?""",
                    (idem, _json(meta), job["version_id"]),
                )
                db.execute(
                    "UPDATE jobs SET after_version_id=?,updated_at=? WHERE id=?",
                    (adopted_version_id, now(), job["id"]),
                )
            bound += 1
        elif old != adopted_version_id:
            stale_roots.append(dep["shot_id"])

    stale: set[str] = set()
    queue = list(stale_roots)
    while queue:
        current_shot_id = queue.pop(0)
        if current_shot_id in stale:
            continue
        stale.add(current_shot_id)
        queue.extend(
            row["shot_id"]
            for row in db.execute(
                """SELECT shot_id FROM video_plan_dependencies
                   WHERE episode_video_plan_id=? AND depends_on_shot_id=?""",
                (current_plan.episode_video_plan_id, current_shot_id),
            ).fetchall()
        )
    for descendant in stale:
        db.execute(
            """UPDATE shot_video_generation_plans SET status='stale',updated_at=?
               WHERE episode_video_plan_id=? AND shot_id=?""",
            (now(), current_plan.episode_video_plan_id, descendant),
        )
        jobs = db.execute(
            """SELECT j.id,j.version_id,j.provider_non_cancellable,v.image_inputs
               FROM jobs j LEFT JOIN shot_versions v ON v.id=j.version_id
               WHERE j.shot_id=? AND j.kind='video'
                 AND j.status IN ('queued','running','waiting_provider','waiting_retry')""",
            (descendant,),
        ).fetchall()
        for job in jobs:
            try:
                meta = json.loads(job["image_inputs"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                meta = {}
            meta["stale"] = True
            meta["stale_reason"] = "upstream_adopted_revision_changed"
            meta["stale_upstream_shot_id"] = shot_id
            meta["stale_upstream_version_id"] = adopted_version_id
            if job["version_id"]:
                db.execute(
                    "UPDATE shot_versions SET image_inputs=? WHERE id=?",
                    (_json(meta), job["version_id"]),
                )
            if not job["provider_non_cancellable"]:
                db.execute(
                    """UPDATE jobs SET status='stale',cancellation_requested=1,
                              abandoned=1,error=?,updated_at=? WHERE id=?""",
                    ("上游采用版本已变化，当前任务已失效", now(), job["id"]),
                )
                if job["version_id"]:
                    db.execute(
                        "UPDATE shot_versions SET status='stale',error=? WHERE id=?",
                        ("上游采用版本已变化，当前候选已失效", job["version_id"]),
                    )
    if conn is None:
        db.commit()
    return {"bound": bound, "stale_shot_ids": sorted(stale)}
