from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app import config, errors, quota, quota_expiry, video_modes
from app.db import get_conn, new_id, now
from app.orchestration import media_scheduler
from app.orchestration.media_runs import mark_media_job_state

from . import enqueue_context, enqueue_persist, enqueue_prompt
from .common import episode_video_budget_limit

_LOGGER = logging.getLogger(__name__)

# ``_enqueue_for_current_status`` (defined in ``.dispatch`` since the 2026-08-30
# split of ``.worker_lifecycle``; ``.dispatch`` itself imports
# ``.worker_lifecycle`` at its own top level) is intentionally *not* imported
# here at module level: ``.worker_lifecycle`` imports ``.job_recovery`` at its
# own top level, and ``.job_recovery`` imports
# ``reconcile_episode_generation_status``/``recover_equivalent_stale_provider_jobs``
# from *this* file at its own top level -- an eager top-level ``from .dispatch
# import _enqueue_for_current_status`` here would close that into a real
# import cycle (enqueue -> dispatch -> worker_lifecycle -> job_recovery ->
# enqueue). The four call sites below do the import locally instead (resolved
# at call time, once every module involved has finished loading).


def _video_path(project_id: str, episode_no: int, shot_no: int, version_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "shots" / str(shot_no)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"v{version_no}.mp4"


# ---------- 成本熔断 ----------

def episode_cost(episode_id: str) -> float:
    row = get_conn().execute(
        """SELECT COALESCE(SUM(v.cost_cny), 0) AS c FROM shot_versions v
           JOIN shots s ON s.id = v.shot_id
           WHERE s.episode_id = ? AND v.status IN ('succeeded', 'running', 'queued')""",
        (episode_id,),
    ).fetchone()
    return float(row["c"])


def reconcile_episode_generation_status(episode_id: str) -> bool:
    """视频队列已无活动任务时，把剧集从假“生成中”恢复为“已确认”。

    单镜失败或预算暂停不应让整集永久处于运行态；真正完成并合成后仍由交付流程置为 done。
    全片补齐 Supervisor 仍在协调（含预检/等待授权）时不得降回 confirmed，否则生成台会误判为空闲。
    """
    conn = get_conn()
    try:
        # 视频时长额度退还：按产物信号收口（见 app/quota.py 模块文档），不追着
        # 视频流水线里 30+ 处 video_slot_active=0 的写点分别埋点——这个函数本身
        # 已经是几乎每次任务状态变化后都会被调用一次的既有收口点，足以覆盖。
        # 退还失败不能连累这个函数真正负责的"整集是否已无活动任务"判断。
        quota.reconcile_video_seconds_refunds(conn, episode_id)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("reconcile_video_seconds_refunds failed for %s", episode_id, exc_info=True)
    active = conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE episode_id=? AND kind='video'
             AND status IN ('queued','running','waiting_provider','waiting_retry')""",
        (episode_id,),
    ).fetchone()["c"]
    if active:
        return False
    ep = conn.execute(
        "SELECT video_completion_mode FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if ep and (ep["video_completion_mode"] or "quick") == "complete":
        try:
            from app import task_registry
            if task_registry.active("video_completion", episode_id):
                return False
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.video_supervisor import load_latest_checkpoint
            cp = load_latest_checkpoint(episode_id)
            # 仅在故意暂停/等待授权时保留 generating；崩溃中途不得锁死
            if cp is not None and cp.phase in {
                "PAUSED_EXTERNAL", "PAUSED_BUDGET",
                "WAITING_AUTHORIZATION", "WAITING_HUMAN",
            }:
                return False
        except Exception:  # noqa: BLE001
            pass
    changed = conn.execute(
        "UPDATE episodes SET status='confirmed' WHERE id=? AND status='generating'",
        (episode_id,),
    ).rowcount == 1
    conn.commit()
    return changed


def stop_shot_video_tasks(shot_id: str) -> dict[str, object]:
    """停止一个镜头的全部活动视频任务，并让页面状态立即脱离“生成中”。

    上游平台接单后的任务通常不可撤回：此时本地任务记为 abandoned，
    worker 立即停止轮询和落盘，但平台仍可能继续执行并产生费用。
    """
    conn = get_conn()
    shot = conn.execute(
        "SELECT id, episode_id FROM shots WHERE id=?", (shot_id,)
    ).fetchone()
    if not shot:
        raise ValueError(f"镜头不存在：{shot_id}")
    rows = conn.execute(
        """SELECT id FROM jobs
           WHERE shot_id=? AND kind='video'
             AND status IN (
               'queued','running','waiting_provider','waiting_retry',
               'waiting_human','paused'
             )
           ORDER BY created_at DESC""",
        (shot_id,),
    ).fetchall()
    results = [media_scheduler.request_cancel(row["id"]) for row in rows]
    stopped = [item for item in results if bool(item.get("cancelled"))]
    reconcile_episode_generation_status(shot["episode_id"])
    return {
        "shot_id": shot_id,
        "stopped_count": len(stopped),
        "provider_may_continue": any(
            bool(item.get("provider_may_continue")) for item in stopped
        ),
        "resume_supported": False,
        "jobs": results,
    }


def pause_episode_video_tasks(episode_id: str) -> dict[str, object]:
    """Durably pause every active video job in an episode.

    Unlike single-shot cancellation this is reversible.  A provider operation
    that has already been accepted may continue remotely, but its local worker
    loses the lease immediately and cannot write a result until the episode is
    resumed.
    """
    conn = get_conn()
    ep = conn.execute("SELECT id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"分集不存在：{episode_id}")
    rows = conn.execute(
        """SELECT id, version_id, run_id, step_run_id, provider_non_cancellable,
                  provider_operation_id
             FROM jobs
            WHERE episode_id=? AND kind='video'
              AND status IN ('queued','running','waiting_provider','waiting_retry','waiting','waiting_human')
              AND cancellation_requested=0 AND abandoned=0
            ORDER BY created_at""",
        (episode_id,),
    ).fetchall()
    already_paused_jobs = int(conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE episode_id=? AND kind='video' AND status='paused'
             AND cancellation_requested=0 AND abandoned=0""",
        (episode_id,),
    ).fetchone()["c"])
    paused = []
    for row in rows:
        cursor = conn.execute(
            """UPDATE jobs
                  SET status='paused', error='用户已暂停整集生成',
                      lease_owner=NULL, lease_expires_at=NULL, next_retry_at=NULL,
                      updated_at=?
                WHERE id=? AND status IN (
                    'queued','running','waiting_provider','waiting_retry','waiting','waiting_human'
                ) AND cancellation_requested=0 AND abandoned=0""",
            (now(), row["id"]),
        )
        if cursor.rowcount != 1:
            continue
        if row["version_id"]:
            conn.execute(
                """UPDATE shot_versions SET status='paused', error='用户已暂停整集生成'
                   WHERE id=? AND status IN (
                       'queued','running','waiting_provider','waiting_retry','waiting','waiting_human'
                   )""",
                (row["version_id"],),
            )
        conn.execute(
            "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
            (row["id"],),
        )
        paused.append(row)
    conn.execute(
        "UPDATE episodes SET status='confirmed' WHERE id=? AND status='generating'",
        (episode_id,),
    )
    conn.commit()
    for row in paused:
        mark_media_job_state(
            row["run_id"], row["step_run_id"], "paused", "用户已暂停整集生成",
        )
    return {
        "episode_id": episode_id,
        "paused_jobs": len(paused),
        "already_paused_jobs": already_paused_jobs,
        "provider_may_continue": any(
            bool(row["provider_non_cancellable"] or row["provider_operation_id"])
            for row in paused
        ),
        "resume_supported": True,
        "job_ids": [row["id"] for row in paused],
    }


def _recover_paused_provider_handle(conn, row) -> tuple[str, float] | None:
    operation_id = row["provider_operation_id"]
    if not operation_id:
        return None
    calls = conn.execute(
        """SELECT ts,response_json FROM provider_calls
           WHERE kind='video_create' AND status='OK' AND operation_id=?
             AND response_json IS NOT NULL
           ORDER BY id DESC""",
        (operation_id,),
    ).fetchall()
    for call in calls:
        try:
            payload = json.loads(call["response_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        task_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
        if task_id:
            return task_id, float(call["ts"])
    return None


def resume_episode_video_tasks(episode_id: str) -> dict[str, object]:
    """Resume jobs paused by :func:`pause_episode_video_tasks`."""
    conn = get_conn()
    ep = conn.execute("SELECT id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"分集不存在：{episode_id}")
    rows = conn.execute(
        """SELECT j.id, j.shot_id, j.version_id, j.run_id, j.step_run_id,
                  j.provider_operation_id, j.provider_create_state,
                  j.provider_non_cancellable, j.provider_submitted_at,
                  v.provider_task_id
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id=j.version_id
            WHERE j.episode_id=? AND j.kind='video' AND j.status='paused'
              AND j.cancellation_requested=0 AND j.abandoned=0
            ORDER BY j.created_at""",
        (episode_id,),
    ).fetchall()
    plans = []
    unresolved = []
    for row in rows:
        provider_task_id = row["provider_task_id"]
        provider_may_have_accepted = bool(
            not provider_task_id
            and (
                row["provider_non_cancellable"]
                or row["provider_create_state"] in {"accepted", "submitting", "unknown"}
            )
        )
        if provider_may_have_accepted:
            recovered = _recover_paused_provider_handle(conn, row)
            if not recovered:
                unresolved.append({
                    "job_id": row["id"],
                    "provider_operation_id": row["provider_operation_id"],
                    "reason": "供应商可能已接单，但本地尚未确认原任务号",
                })
                continue
            provider_task_id, submitted_at = recovered
            if row["version_id"]:
                conn.execute(
                    "UPDATE shot_versions SET provider_task_id=? WHERE id=?",
                    (provider_task_id, row["version_id"]),
                )
            conn.execute(
                """UPDATE jobs
                   SET provider_create_state='accepted', provider_non_cancellable=1,
                       provider_submitted_at=COALESCE(provider_submitted_at,?), updated_at=?
                   WHERE id=? AND status='paused'""",
                (submitted_at, now(), row["id"]),
            )
        plans.append((
            row,
            "waiting_retry"
            if not row["version_id"]
            else ("waiting_provider" if provider_task_id else "queued"),
        ))
    conn.commit()
    if unresolved:
        return {
            "episode_id": episode_id,
            "resumed_jobs": 0,
            "job_ids": [],
            "requires_provider_confirmation": True,
            "unresolved_provider_jobs": unresolved,
            "recovery_action": "请在任务中心核对这些任务；确认供应商后台无可继续任务后，再明确重试",
        }

    resumed = []
    for row, next_status in plans:
        retry_at = now() if not row["version_id"] else None
        cursor = conn.execute(
            """UPDATE jobs
                  SET status=?,error=NULL,video_slot_active=1,
                      lease_owner=NULL,lease_expires_at=NULL,
                      next_retry_at=?,updated_at=?
                WHERE id=? AND status='paused' AND cancellation_requested=0 AND abandoned=0""",
            (next_status, retry_at, now(), row["id"]),
        )
        if cursor.rowcount != 1:
            continue
        if row["version_id"]:
            conn.execute(
                """UPDATE shot_versions
                      SET status='queued',error=NULL,video_slot_active=1
                    WHERE id=? AND status='paused'""",
                (row["version_id"],),
            )
        resumed.append(row)
    if resumed:
        conn.execute("UPDATE episodes SET status='generating' WHERE id=?", (episode_id,))
    conn.commit()
    dispatch_deferred = []
    for row in resumed:
        try:
            if row["version_id"]:
                from .dispatch import _enqueue_for_current_status

                _enqueue_for_current_status(row["id"])
            else:
                enqueue_shot(row["shot_id"])
        except Exception as exc:  # durable dispatcher will rediscover the row
            errors.record_and_format(
                exc, action="resume_episode_video_dispatch",
                context={"episode_id": episode_id, "job_id": row["id"]},
            )
            dispatch_deferred.append(row["id"])
    return {
        "episode_id": episode_id,
        "resumed_jobs": len(resumed),
        "job_ids": [row["id"] for row in resumed],
        "dispatch_deferred_job_ids": dispatch_deferred,
        "durable_dispatch_pending": bool(dispatch_deferred),
    }


def recover_equivalent_stale_provider_jobs(episode_id: str) -> dict[str, object]:
    """Resume accepted provider tasks fenced only by an equivalent plan revision.

    The provider handle is immutable and already payable. Recovery rebinds the
    local execution projection to the current semantically identical shot plan,
    preserving the submitted plan ID in metadata for audit. No provider create
    call is made.
    """
    from app.video_plan import active_plan_is_current, get_shot_plan

    conn = get_conn()
    episode = conn.execute(
        "SELECT id FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if episode is None:
        raise ValueError(f"分集不存在：{episode_id}")
    rows = conn.execute(
        """SELECT j.id AS job_id,j.shot_id,j.version_id,j.run_id,j.step_run_id,
                  v.provider_task_id,v.image_inputs
             FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
            WHERE j.episode_id=? AND j.kind='video'
              AND j.status='stale' AND v.status='stale'
              AND j.provider_non_cancellable=1
              AND v.provider_task_id IS NOT NULL AND v.provider_task_id!=''
              AND j.cancellation_requested=0 AND j.abandoned=0
            ORDER BY j.created_at""",
        (episode_id,),
    ).fetchall()
    recovered = []
    budget_blocked = []
    for row in rows:
        # A newer/other usable candidate already satisfies this shot. Recovering
        # an older paid task would only create an extra candidate and could race
        # the default adoption path, so keep that task quarantined as stale.
        candidate_rows = conn.execute(
            """SELECT video_path FROM shot_versions
                WHERE shot_id=? AND id!=? AND status='succeeded'
                  AND video_path IS NOT NULL AND video_path!=''
                ORDER BY version_no DESC""",
            (row["shot_id"], row["version_id"]),
        ).fetchall()
        if any(
            Path(str(candidate["video_path"])).is_file()
            for candidate in candidate_rows
        ):
            continue
        try:
            meta = json.loads(row["image_inputs"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        submitted_shot_plan_id = str(meta.get("shot_plan_id") or "")
        if (
            not submitted_shot_plan_id
            or not active_plan_is_current(submitted_shot_plan_id, conn=conn)
        ):
            continue
        current = get_shot_plan(row["shot_id"], conn=conn)
        if current is None:
            continue
        actual_mode = str(
            meta.get("actual_mode") or meta.get("mode") or meta.get("planned_mode") or ""
        )
        if actual_mode != current.mode.value:
            continue
        reservation = conn.execute(
            "SELECT amount_cny FROM budget_reservations WHERE job_id=?",
            (row["job_id"],),
        ).fetchone()
        reservation_amount = float(
            reservation["amount_cny"] if reservation else 0
        )
        if not media_scheduler.reserve_budget(
            row["job_id"],
            episode_id,
            reservation_amount,
            episode_video_budget_limit(episode_id),
            conn=conn,
        ):
            budget_blocked.append(dict(row))
            continue

        meta.update({
            "submitted_shot_plan_id": submitted_shot_plan_id,
            "submitted_episode_video_plan_id": meta.get("episode_video_plan_id"),
            "shot_plan_id": current.shot_plan_id,
            "episode_video_plan_id": current.episode_video_plan_id,
            "plan_revision": current.plan_revision,
            "source_storyboard_revision_id": current.source_storyboard_revision_id,
            "capability_snapshot_id": current.capability_snapshot_id,
            "input_revision_fingerprints": dict(current.input_revision_fingerprints),
            "planned_mode": current.mode.value,
            "actual_mode": actual_mode,
            "stale_plan_recovered": True,
            "stale_plan_recovered_at": now(),
        })
        updated = conn.execute(
            """UPDATE jobs
                  SET status='waiting_provider',error=NULL,
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=?,
                      provider_create_state='accepted',provider_non_cancellable=1,
                      updated_at=?
                WHERE id=? AND status='stale'
                  AND cancellation_requested=0 AND abandoned=0""",
            (now(), now(), row["job_id"]),
        )
        if updated.rowcount != 1:
            conn.rollback()
            media_scheduler.settle_budget(row["job_id"], 0.0, success=False)
            continue
        conn.execute(
            "UPDATE shot_versions SET status='running',error=NULL,image_inputs=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), row["version_id"]),
        )
        conn.execute(
            """UPDATE video_generation_attempts
                  SET shot_plan_id=?,status='provider_running',error=NULL,updated_at=?
                WHERE version_id=? AND provider_task_id=?""",
            (
                current.shot_plan_id,
                now(),
                row["version_id"],
                row["provider_task_id"],
            ),
        )
        conn.execute(
            """UPDATE shot_video_generation_plans
                  SET actual_mode=?,status='provider_running',updated_at=?
                WHERE id=?""",
            (actual_mode, now(), current.shot_plan_id),
        )
        conn.execute(
            """UPDATE jobs
                  SET reserved_cost_cny=COALESCE(
                      (SELECT amount_cny FROM budget_reservations WHERE job_id=?),0
                  )
                WHERE id=?""",
            (row["job_id"], row["job_id"]),
        )
        recovered.append(dict(row))
        conn.commit()
    if recovered:
        conn.execute(
            "UPDATE episodes SET status='generating' WHERE id=?",
            (episode_id,),
        )
    conn.commit()
    for row in recovered:
        from .dispatch import _enqueue_for_current_status

        _enqueue_for_current_status(row["job_id"])
        mark_media_job_state(
            row["run_id"],
            row["step_run_id"],
            "waiting_provider",
            "等价计划 revision 已验证，继续轮询原供应商任务",
        )
    return {
        "episode_id": episode_id,
        "recovered_jobs": len(recovered),
        "job_ids": [row["job_id"] for row in recovered],
        "provider_task_ids": [row["provider_task_id"] for row in recovered],
        "budget_blocked_job_ids": [
            row["job_id"] for row in budget_blocked
        ],
        "provider_create_calls": 0,
    }


# ---------- 入队 ----------

def _load_shot_model(shot_row) -> "object":
    from app.continuity import apply_shot_contract
    from app.schemas import Shot
    shot = Shot(
        shot_uid=(
            shot_row["shot_uid"]
            if "shot_uid" in shot_row.keys()
            else None
        ) or "",
        shot_no=shot_row["shot_no"], duration_s=shot_row["duration_s"], shot_size=shot_row["shot_size"],
        camera_move=shot_row["camera_move"],
        scene_time=(shot_row["scene_time"] if "scene_time" in shot_row.keys() else "") or "",
        scene_setting=shot_row["scene_setting"],
        scene_name=(shot_row["scene_name"] if "scene_name" in shot_row.keys() else "") or "",
        characters=json.loads(shot_row["characters"] or "[]"), action_desc=shot_row["action_desc"],
        first_frame_desc=(shot_row["first_frame_desc"] if "first_frame_desc" in shot_row.keys() else "") or "",
        last_frame_desc=(shot_row["last_frame_desc"] if "last_frame_desc" in shot_row.keys() else "") or "",
        source_excerpt=(shot_row["source_excerpt"] if "source_excerpt" in shot_row.keys() else "") or "",
        narration=shot_row["narration"], dialogues=json.loads(shot_row["dialogues"] or "[]"),
        transition=shot_row["transition"] or "硬切", continuity_from_prev=bool(shot_row["continuity_from_prev"]),
        continuity_mode=(shot_row["continuity_mode"] if "continuity_mode" in shot_row.keys() else "") or "",
        observed_state_out=(shot_row["observed_state_out"] if "observed_state_out" in shot_row.keys() else "") or "",
    )
    if "shot_contract_json" in shot_row.keys() and shot_row["shot_contract_json"]:
        apply_shot_contract(shot, shot_row["shot_contract_json"])
    return shot


def _decision_from_mode_plan(shot_row):
    """把已持久化的模型决策（shots.mode_plan）转成 ShotVideoModeDecision；无则返回 None。"""
    try:
        raw = shot_row["mode_plan"] if "mode_plan" in shot_row.keys() else None
    except (TypeError, AttributeError):
        raw = None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("mode"):
        return None
    return video_modes.dict_to_decision(data)


def _row_value(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    return default


def _usable_reference_dicts(meta: dict) -> list[dict]:
    """Return reference images that would be sent to Seedance from stored gallery meta."""
    refs = meta.get("reference_images") or []
    return [r for r in refs if r.get("selectedForSeedance") and not r.get("deleted")]


def _reference_fingerprint_item(ref: dict) -> str:
    identity = ref.get("path") or ref.get("url") or ref.get("id") or ""
    path = ref.get("path")
    if path:
        try:
            p = Path(path)
            st = p.stat()
            identity = f"{identity}@{st.st_size}:{int(st.st_mtime_ns)}"
        except OSError:
            identity = f"{identity}@missing"
    return "|".join([
        str(ref.get("id") or ""),
        str(ref.get("type") or ""),
        str(ref.get("source") or ""),
        identity,
    ])


def _reference_gallery_fingerprint(meta: dict) -> str:
    """Stable-ish fingerprint for the current usable gallery set.

    The gallery edit API toggles selected/deleted flags inside image_inputs. Those
    flags change the actual images sent to Seedance, so they must affect reuse.
    """
    usable = _usable_reference_dicts(meta)
    return json.dumps([_reference_fingerprint_item(r) for r in usable], ensure_ascii=False, sort_keys=True)


def _load_reference_gallery(conn, shot_row) -> dict | None:
    """Return the shot-level reference gallery for a new video version.

    Reference images belong to the shot, not to one video attempt. Prefer the
    adopted version because that is the gallery shown as current in the review
    wall; if it has no gallery, fall back to the newest version that does. A
    failed video version is still a valid source because its reference-image
    generation and QA completed before the video provider was called.
    """
    adopted_version_id = _row_value(shot_row, "adopted_version_id")
    versions = []
    if adopted_version_id:
        adopted = conn.execute(
            "SELECT id, image_inputs FROM shot_versions WHERE id=? AND shot_id=?",
            (adopted_version_id, _row_value(shot_row, "id")),
        ).fetchone()
        if adopted:
            versions.append(adopted)
    versions.extend(conn.execute(
        """SELECT id, image_inputs FROM shot_versions
           WHERE shot_id=? AND id!=COALESCE(?, '')
           ORDER BY version_no DESC""",
        (_row_value(shot_row, "id"), adopted_version_id),
    ).fetchall())

    for version in versions:
        try:
            meta = json.loads(version["image_inputs"] or "{}")
        except (TypeError, ValueError):
            continue
        refs = meta.get("reference_images") or []
        if not refs:
            continue
        if not video_modes.reference_gallery_matches_library_policy(meta):
            # 旧画廊可能含生成关键帧；新版本只复用人物谱/场景库资产。
            continue
        frozen_manifest = meta.get("reference_manifest")
        if not isinstance(frozen_manifest, dict):
            frozen_manifest = next(
                (
                    ref.get("dependency_manifest") for ref in refs
                    if isinstance(ref, dict) and isinstance(ref.get("dependency_manifest"), dict)
                ),
                None,
            )
        return {
            "source_version_id": version["id"],
            "revision": meta.get("reference_gallery_revision"),
            "edited": bool(meta.get("reference_gallery_edited")),
            "contract_override": bool(meta.get("reference_gallery_contract_override")),
            "keyframe_prompt_contract_version": meta.get("keyframe_prompt_contract_version"),
            "keyframe_contract_fingerprint": (
                meta.get("keyframe_contract_fingerprint")
                or next(
                    (
                        ref.get("keyframe_contract_fingerprint") for ref in refs
                        if isinstance(ref, dict) and ref.get("keyframe_contract_fingerprint")
                    ),
                    None,
                )
            ),
            "keyframe_sequence": meta.get("keyframe_sequence"),
            "reference_images": refs,
            "reference_manifest": frozen_manifest,
            "fingerprint": _reference_gallery_fingerprint(meta),
        }
    return None


def _transition_value(shot_row) -> str:
    transition = (_row_value(shot_row, "transition") or "硬切").strip()
    return transition or "硬切"


def _outgoing_transition_context(conn, shot_row) -> dict | None:
    """下一镜如果是换场镜，则它的 transition 决定本镜结尾怎么收。"""
    if not shot_row:
        return None
    next_shot = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
        (_row_value(shot_row, "episode_id"), int(_row_value(shot_row, "shot_no", 0) or 0) + 1),
    ).fetchone()
    if not next_shot:
        return None
    if bool(_row_value(next_shot, "continuity_from_prev", 0)):
        return None
    prev_scene = (
        _row_value(shot_row, "scene_name") or _row_value(shot_row, "scene_setting") or ""
    ).strip()
    next_scene = (
        _row_value(next_shot, "scene_name") or _row_value(next_shot, "scene_setting") or ""
    ).strip()
    prev_time = (_row_value(shot_row, "scene_time") or "").strip()
    next_time = (_row_value(next_shot, "scene_time") or "").strip()
    if prev_scene == next_scene and prev_time == next_time:
        return None
    transition = _transition_value(next_shot)
    if transition == "硬切":
        return None
    return {
        "transition": transition,
        "next_scene": next_scene,
        "next_first_frame_desc": (_row_value(next_shot, "first_frame_desc") or "").strip(),
        "next_shot_no": _row_value(next_shot, "shot_no"),
    }


def _reused_reason_for_status(status: str | None) -> str:
    """把复用命中时挂着的实际状态翻成前端能诚实转述的原因。

    不写"输入未变化"这类猜测性文案——调用方从未真正比较过输入；这里只
    如实转述"为什么这次没有新建"：已交付、仍在自动处理中、或已经卡死转
    人工（正常情况下 waiting_human 不会再落到这里，因为它已从两层复用判据
    里都拿掉了；留着这个分支是防御性的，万一将来又出现别的持锁路径）。
    """
    if status == "succeeded":
        return "succeeded"
    if status == "waiting_human":
        return "stuck_needs_human"
    return "in_flight"


def _begin_video_preflight_job(
    shot_id: str,
    *,
    supervisor_run_id: str | None,
) -> dict[str, Any]:
    """先持久化轻量任务，再执行可能失败的输入校验。

    version_id 为空表示尚未进入付费媒体阶段；durable dispatcher 不会消费
    waiting_retry，因此进程中断也不会把半成品任务提交给供应商。
    """
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    conn = get_conn()
    job_id = new_id("job")
    claim_owner = new_id("preflight")
    stamp = now()
    retry_at = stamp + float(config.VIDEO_PREFLIGHT_VALIDATION_TIMEOUT)
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        shot = conn.execute(
            """SELECT s.id, s.episode_id, e.project_id
               FROM shots s JOIN episodes e ON e.id=s.episode_id
               WHERE s.id=?""",
            (shot_id,),
        ).fetchone()
        if not shot:
            raise ValueError(f"镜头不存在：{shot_id}")
        existing = conn.execute(
            """SELECT id,version_id,status,lease_owner,lease_expires_at
                 FROM jobs
                WHERE shot_id=? AND kind='video' AND video_slot_active=1
                LIMIT 1""",
            (shot_id,),
        ).fetchone()
        acquired = False
        if existing is not None:
            job_id = str(existing["id"])
            lease_inactive = (
                existing["lease_owner"] is None
                or existing["lease_expires_at"] is None
                or float(existing["lease_expires_at"]) <= stamp
            )
            if existing["version_id"] is None and lease_inactive:
                claimed = conn.execute(
                    """UPDATE jobs
                          SET status='waiting_retry',error=NULL,next_retry_at=?,
                              owner_run_id=COALESCE(?,owner_run_id),
                              lease_owner=?,lease_expires_at=?,updated_at=?
                        WHERE id=? AND video_slot_active=1 AND version_id IS NULL
                          AND (
                              lease_owner IS NULL OR lease_expires_at IS NULL
                              OR lease_expires_at<=?
                          )""",
                    (
                        retry_at,
                        supervisor_run_id,
                        claim_owner,
                        retry_at,
                        stamp,
                        job_id,
                        stamp,
                    ),
                )
                acquired = claimed.rowcount == 1
        else:
            # 每模块并发 + 视频时长额度：这是"这一镜的这一次尝试"真正诞生的
            # 时刻（新 job_id，video_slot_active 唯一索引保证同一镜同一时间只
            # 有一个活跃 job）。两项检查 + 15 秒预扣都在这个 INSERT 之前、同一个
            # BEGIN IMMEDIATE 事务里完成——超额时直接 raise，job 行不会被插入，
            # 外层 except 统一 rollback（CLAUDE.md：扣减与任务创建必须在同一
            # 事务里）。找不到归属账号（legacy-shared 兼容路径）时不拦截。
            owner_user_id = quota.owner_of_project(conn, shot["project_id"])
            if owner_user_id is not None:
                quota_expiry.assert_membership_active(conn, owner_user_id)
                active_jobs = quota.count_active_video_jobs(conn, owner_user_id)
                quota.check_module_concurrency(conn, owner_user_id, quota.MODULE_VIDEO, active_count=active_jobs)
                quota.reserve_video_seconds(conn, owner_user_id, attempt_key=job_id)
            conn.execute(
                """INSERT INTO jobs(
                       id,kind,shot_id,episode_id,project_id,status,
                       video_slot_active,owner_run_id,next_retry_at,
                       lease_owner,lease_expires_at,created_at,updated_at
                   ) VALUES(
                       ?,'video',?,?,?,'waiting_retry',1,?,?,?,?,?,?
                   )""",
                (
                    job_id,
                    shot_id,
                    shot["episode_id"],
                    shot["project_id"],
                    supervisor_run_id,
                    retry_at,
                    claim_owner,
                    retry_at,
                    stamp,
                    stamp,
                ),
            )
            existing = None
            acquired = True
        if not acquired:
            conn.commit()
            return {
                "acquired": False,
                "job_id": job_id,
                "version_id": existing["version_id"] if existing else None,
                "status": existing["status"] if existing else None,
                "claim_owner": None,
            }
        set_pipeline_stage(
            job_id,
            media_stages.STAGE_PREFLIGHT_VALIDATING,
            reason_code="VIDEO_PREFLIGHT_VALIDATING",
            reason_text="正在校验视频输入，尚未提交供应商",
            stage_progress={
                "attempt": int(conn.execute(
                    "SELECT retry_count FROM jobs WHERE id=?", (job_id,)
                ).fetchone()["retry_count"] or 0) + 1,
                "attempt_limit": int(config.VIDEO_PREFLIGHT_MAX_RETRIES) + 1,
                "unit": "preflight_attempt",
            },
            conn=conn,
        )
        conn.execute(
            "UPDATE episodes SET status='generating' WHERE id=? AND status='confirmed'",
            (shot["episode_id"],),
        )
        conn.commit()
        return {
            "acquired": True,
            "job_id": job_id,
            "version_id": None,
            "status": "waiting_retry",
            "claim_owner": claim_owner,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _preflight_failure_is_retryable(exc: Exception) -> bool:
    import sqlite3

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        return True
    return bool(getattr(exc, "retryable", False))


def _mark_video_preflight_failure(
    job_id: str,
    exc: Exception,
    *,
    claim_owner: str,
) -> dict[str, Any]:
    """把未创建 version 的校验失败保留为可重试或显式阻塞任务。"""
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage
    from app.observability.metrics import inc

    conn = get_conn()
    row = conn.execute(
        """SELECT version_id,retry_count,max_retries,episode_id,shot_id
             FROM jobs
            WHERE id=? AND video_slot_active=1 AND lease_owner=?""",
        (job_id, claim_owner),
    ).fetchone()
    if not row or row["version_id"]:
        return {"retry_scheduled": False, "status": None}
    message = (str(exc) or exc.__class__.__name__)[:2000]
    failure_kind = getattr(exc, "failure_kind", None)
    retryable = _preflight_failure_is_retryable(exc)
    retry_count = int(row["retry_count"] or 0)
    configured_limit = int(config.VIDEO_PREFLIGHT_MAX_RETRIES)
    max_retries = min(int(row["max_retries"] or configured_limit), configured_limit)
    if retryable and retry_count < max_retries:
        attempt = retry_count + 1
        delay = float(config.VIDEO_PREFLIGHT_RETRY_BASE_DELAY) * (2 ** retry_count)
        reason = (
            f"视频输入校验遇到可重试的结构化故障，系统将在约 {int(delay)} 秒后"
            f"自动重试（{attempt}/{max_retries}）"
        )
        next_retry_at = now() + delay
        conn.execute(
            """UPDATE jobs SET status='waiting_retry', error=?, retry_count=?,
                      next_retry_at=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND version_id IS NULL AND video_slot_active=1
                 AND lease_owner=?""",
            (reason, attempt, next_retry_at, now(), job_id, claim_owner),
        )
        set_pipeline_stage(
            job_id,
            media_stages.STAGE_PREFLIGHT_RETRY,
            reason_code="VIDEO_PREFLIGHT_RETRY",
            reason_text=reason,
            stage_progress={
                "attempt": attempt,
                "attempt_limit": max_retries,
                "unit": "preflight_attempt",
                "last_error": message,
                "failure_kind": failure_kind,
            },
            conn=conn,
        )
        metric = "video_preflight_retry_scheduled_total"
        state = {
            "retry_scheduled": True,
            "status": "waiting_retry",
            "retry_count": attempt,
            "max_retries": max_retries,
            "next_retry_at": next_retry_at,
            "reason": reason,
        }
    else:
        reason = f"视频输入校验未通过：{message}"
        conn.execute(
            """UPDATE jobs SET status='waiting_human', error=?, next_retry_at=NULL,
                      lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND version_id IS NULL AND video_slot_active=1
                 AND lease_owner=?""",
            (reason, now(), job_id, claim_owner),
        )
        set_pipeline_stage(
            job_id,
            media_stages.STAGE_PREFLIGHT_BLOCKED,
            reason_code="VIDEO_PREFLIGHT_BLOCKED",
            reason_text=reason,
            stage_progress={
                "attempt": retry_count + 1,
                "attempt_limit": max_retries + 1,
                "unit": "preflight_attempt",
                "last_error": message,
                "failure_kind": failure_kind,
            },
            conn=conn,
        )
        conn.execute(
            "UPDATE jobs SET stage_status='blocked' WHERE id=?", (job_id,)
        )
        metric = "video_preflight_blocked_total"
        state = {
            "retry_scheduled": False,
            "status": "waiting_human",
            "retry_count": retry_count,
            "max_retries": max_retries,
            "next_retry_at": None,
            "reason": reason,
        }
    conn.commit()
    reconcile_episode_generation_status(row["episode_id"])
    inc(metric, episode_id=row["episode_id"], shot_id=row["shot_id"])
    return state


def _close_reused_preflight_job(job_id: str, *, claim_owner: str) -> None:
    """幂等复用旧版本时关闭仅用于校验的空壳任务。"""
    conn = get_conn()
    conn.execute(
        """UPDATE jobs SET status='succeeded', error='输入未变化，已复用已有版本',
                  video_slot_active=0,next_retry_at=NULL,stage_status='complete',
                  lease_owner=NULL,lease_expires_at=NULL,updated_at=?
           WHERE id=? AND version_id IS NULL AND video_slot_active=1
             AND lease_owner=?""",
        (now(), job_id, claim_owner),
    )
    conn.commit()


def _resume_reused_paused_job(
    version_id: str,
    *,
    supervisor_run_id: str | None,
    dependency_snapshot: dict[str, Any] | None,
    preflight_job_id: str | None = None,
    preflight_owner: str | None = None,
) -> dict[str, Any] | None:
    """Transfer an exact interrupted attempt to the current completion Supervisor."""
    if not supervisor_run_id:
        return None
    conn = get_conn()
    row = conn.execute(
        """SELECT j.*, v.status AS version_status, v.provider_task_id,
                  v.image_inputs, e.active_video_run_id, e.video_completion_mode,
                  s.duration_s
             FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
             JOIN episodes e ON e.id=j.episode_id
             JOIN shots s ON s.id=j.shot_id
            WHERE j.version_id=? AND j.kind='video'
              AND (
                  (j.status='paused' AND j.cancellation_requested=0 AND j.abandoned=0)
                  OR (j.status='abandoned' AND j.provider_non_cancellable=1)
              )
            ORDER BY j.created_at DESC LIMIT 1""",
        (version_id,),
    ).fetchone()
    if row is None:
        return None
    if (
        row["video_completion_mode"] != "complete"
        or row["active_video_run_id"] != supervisor_run_id
    ):
        raise ValueError(
            "[VIDEO_SUPERVISOR_OWNERSHIP_STALE] 暂停任务不能移交给非当前整集生成运行"
        )

    try:
        meta = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    captured = meta.get("review_dependency_snapshot") or {}
    expected_qualification = str(captured.get("qualification_version") or "")
    current_snapshot = dependency_snapshot or {}
    upstream_keys = (
        "published_screenplay_artifact_id",
        "confirmed_storyboard_artifact_id",
        "screenplay_revision",
        "storyboard_revision",
    )
    upstream_equal = all(
        captured.get(key) == current_snapshot.get(key)
        for key in upstream_keys
    )
    expected_authority = (
        expected_qualification.split(":", 1)[0]
        if ":" in expected_qualification
        else None
    )
    current_requires_authority = bool(
        current_snapshot.get("narrative_authority_required")
    )
    authority_equal = bool(
        (
            not current_requires_authority
            and expected_authority is None
        )
        or (
            current_requires_authority
            and expected_authority
            and expected_authority
            == current_snapshot.get("narrative_authority_version")
            and current_snapshot.get("narrative_authority_verified")
        )
    )

    def asset_contract(items):
        return sorted(
            json.dumps(
                {
                    key: value
                    for key, value in item.items()
                    if key != "version_id"
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for item in items
            if item.get("shot_id") != row["shot_id"]
        )

    expected_assets = captured.get("asset_inputs") or []
    current_assets = current_snapshot.get("asset_inputs") or []
    assets_equal = bool(
        current_requires_authority
        or not expected_assets
        or asset_contract(expected_assets) == asset_contract(current_assets)
    )
    if not upstream_equal or not authority_equal or not assets_equal:
        raise ValueError(
            "[REVIEW_DEPENDENCY_STALE] 暂停任务绑定的发布依赖已变化，禁止直接恢复"
        )

    provider_task_id = str(row["provider_task_id"] or "").strip()
    submitted_at = row["provider_submitted_at"]
    provider_may_have_accepted = bool(
        row["provider_non_cancellable"]
        or row["provider_create_state"] in {"accepted", "submitting", "unknown"}
        or provider_task_id
    )
    if provider_may_have_accepted and (
        not provider_task_id or submitted_at is None
    ):
        recovered = _recover_paused_provider_handle(conn, row)
        if recovered is None:
            raise ValueError(
                "[VIDEO_PROVIDER_HANDLE_UNRESOLVED] 供应商可能已接单，但原任务号或"
                "提交时间尚未确认，禁止重复提交"
            )
        recovered_task_id, recovered_at = recovered
        if provider_task_id and provider_task_id != recovered_task_id:
            raise ValueError(
                "[VIDEO_PROVIDER_HANDLE_MISMATCH] 本地任务号与供应商账本不一致"
            )
        provider_task_id = recovered_task_id
        submitted_at = recovered_at

    if row["status"] == "abandoned" and not provider_task_id:
        raise ValueError(
            "[VIDEO_PROVIDER_HANDLE_REQUIRED] 已放弃任务没有供应商接单证据，禁止复活"
        )
    reservation = conn.execute(
        "SELECT status FROM budget_reservations WHERE job_id=?",
        (row["id"],),
    ).fetchone()
    reservation_active = bool(
        reservation is not None
        and reservation["status"] in {"reserved", "running"}
    )
    if not reservation_active and row["status"] == "abandoned" and provider_task_id:
        from app.video_cost_model import initial_shot_generation_cost

        reservation_active = media_scheduler.reserve_budget(
            row["id"],
            row["episode_id"],
            initial_shot_generation_cost(float(row["duration_s"] or 0)),
            episode_video_budget_limit(str(row["episode_id"])),
            conn=conn,
        )
    if not reservation_active:
        raise ValueError(
            "[VIDEO_BUDGET_RESERVATION_REQUIRED] 中断任务缺少有效预算预留，禁止恢复"
        )

    next_status = "waiting_provider" if provider_task_id else "queued"
    if preflight_job_id and preflight_owner:
        released = conn.execute(
            """UPDATE jobs
                  SET status='succeeded',video_slot_active=0,
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,
                      error='已将活动槽移交给原供应商任务',updated_at=?
                WHERE id=? AND version_id IS NULL AND video_slot_active=1
                  AND lease_owner=?""",
            (now(), preflight_job_id, preflight_owner),
        )
        if released.rowcount != 1:
            conn.rollback()
            raise ValueError("视频输入校验任务状态已变化，请刷新后重试")
    updated = conn.execute(
        """UPDATE jobs
              SET status=?,error=NULL,video_slot_active=1,owner_run_id=?,
                  provider_create_state=?, provider_non_cancellable=?,
                  provider_submitted_at=?, lease_owner=NULL,
                  lease_expires_at=NULL, next_retry_at=NULL,
                  cancellation_requested=0, abandoned=0, updated_at=?
            WHERE id=? AND status=?""",
        (
            next_status,
            supervisor_run_id,
            "accepted" if provider_task_id else "not_started",
            int(bool(provider_task_id)),
            submitted_at,
            now(),
            row["id"],
            row["status"],
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise ValueError("暂停任务状态已变化，请刷新后重试")
    if provider_task_id:
        conn.execute(
            "UPDATE shot_versions SET provider_task_id=? WHERE id=?",
            (provider_task_id, version_id),
        )
    conn.execute(
        """UPDATE shot_versions
              SET status='queued',error=NULL,video_slot_active=1
            WHERE id=? AND status IN ('paused','abandoned')""",
        (version_id,),
    )
    conn.execute(
        "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
        (row["id"],),
    )
    conn.commit()
    from .dispatch import _enqueue_for_current_status

    _enqueue_for_current_status(row["id"])
    return {
        "resumed": True,
        "job_id": row["id"],
        "provider_task_id": provider_task_id or None,
        "provider_already_accepted": bool(provider_task_id),
    }


def _assert_enqueue_storyboard_authority(shot_id: str):
    """Resolve the immutable screenplay and fence narrative paid work.

    This check intentionally runs before a preflight job is created and before
    any legacy row normalizer can mutate the storyboard projection.  Historical
    plan-null episodes keep their explicit compatibility path; once durable
    narrative authority exists, deleting a mutable pointer can never downgrade
    the episode back into that path.
    """
    conn = get_conn()
    shot = conn.execute(
        "SELECT episode_id FROM shots WHERE id=?", (shot_id,),
    ).fetchone()
    if shot is None:
        raise ValueError(f"镜头不存在：{shot_id}")
    episode_id = str(shot["episode_id"])
    from app.production.screenplay_authority import (
        DownstreamScreenplayContext,
        episode_requires_immutable_screenplay_authority,
        resolve_downstream_screenplay,
    )

    try:
        context = resolve_downstream_screenplay(episode_id, conn=conn)
    except ValueError as exc:
        episode = conn.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,),
        ).fetchone()
        raw_projection = (
            _row_value(episode, "screenplay_json") if episode is not None else None
        )
        if (
            episode is not None
            and not raw_projection
            and not episode_requires_immutable_screenplay_authority(
                episode, conn=conn,
            )
        ):
            # Truly historical jobs can predate even the mutable screenplay
            # projection.  This compatibility object is explicitly plan-null;
            # any durable Artifact/revision/review evidence above makes the
            # branch unreachable and the same deletion fail closed.
            from app.schemas import EpisodeScreenplay

            context = DownstreamScreenplayContext(
                screenplay=EpisodeScreenplay(
                    episode_no=int(_row_value(episode, "episode_no", 1) or 1),
                ),
                narrative_authority_required=False,
                immutable_authority_required=False,
            )
        else:
            raise ValueError(f"当前剧本权威链无法验证：{exc}") from exc
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if episode is None:
        raise ValueError(
            "[STORYBOARD_CONFIRMATION_REQUIRED] 分镜尚未人工确认，"
            "不能创建视频预检或付费任务"
        )
    if episode["status"] not in {"confirmed", "generating", "done", "mixed"}:
        # status 白名单是给老版逐镜叙事契约（含历史 plan-null 兼容分集）用的
        # 完整信号——那类行没有存量 prompt_text，只能靠人工确认把 status 推
        # 过去。分镜台 2.0.0（app.production.storyboard_pack）路径生成完成
        # 后只落 'scripted'，从不推进到这个白名单；这类分集改用产物本身
        # 判：本集是否已生成完整一套 prompt_text（storyboard_pack_prompts_
        # complete），不再要求先经过人工确认这道已经不做任何额外校验的仪式。
        from app.domain.common import (
            ensure_storyboard_pack_release_gate_decision,
            storyboard_pack_prompts_complete,
        )

        if not storyboard_pack_prompts_complete(conn, episode_id):
            raise ValueError(
                "[STORYBOARD_CONFIRMATION_REQUIRED] 分镜尚未人工确认，"
                "不能创建视频预检或付费任务"
            )
        # 这条管线里不再有人工点击的"确认视频提示词"仪式，产物信号一放行
        # 就在这个转换点补一条系统判定的 gate_decisions 审计行，回答"这一
        # 集是凭什么被放行的"。只记账不拦截：失败只记日志，不影响本次放行。
        ensure_storyboard_pack_release_gate_decision(conn, episode_id)
    if not context.narrative_authority_required:
        return context

    from app.domain.video_ops import _has_current_storyboard_completion_certificate

    if not _has_current_storyboard_completion_certificate(
        conn, episode,
    ):
        raise ValueError(
            "[NARRATIVE_CERTIFICATE_REQUIRED] 当前叙事分镜缺少与正式 "
            "Artifact、镜头投影和完成证书精确绑定的生产权威"
        )
    # Recompute the release manifest here as a content-addressed final check.
    # The per-shot plan verifier repeats it below and workers repeat it again at
    # every provider submission/write boundary.
    from app.video_plan import current_storyboard_release_manifest

    current_storyboard_release_manifest(episode_id, conn=conn)
    return context


def enqueue_shot(shot_id: str, *, prompt_override: str | None = None,
                 extra_negative: list[str] | None = None, reroll: bool = False,
                 critique: list[str] | None = None, after_shot_id: str | None = None,
                 auto_retake_count: int = 0,
                 supervisor_run_id: str | None = None,
                 dependency_snapshot: dict[str, Any] | None = None,
                 critique_sources: list[dict[str, Any]] | None = None,
                 operation_idempotency_key: str | None = None,
                 operation_request_fingerprint: str | None = None,
                 operation_claim_token: str | None = None,
                 operation_command: str = "video.generate_shot") -> dict:
    """持久化校验状态；不从错误文案推断或改写分镜数据。"""
    authority_context = _assert_enqueue_storyboard_authority(shot_id)
    if (
        authority_context.narrative_authority_required
        and (prompt_override or "").strip()
    ):
        raise ValueError(
            "[NARRATIVE_PROMPT_OVERRIDE_REQUIRES_CANDIDATE] 叙事权威镜头不允许用"
            "自由文本覆盖已发布的分镜语义；请通过受控分镜候选修订后重新发布"
        )
    preflight_claim = _begin_video_preflight_job(
        shot_id, supervisor_run_id=supervisor_run_id,
    )
    if not preflight_claim["acquired"]:
        result = {
            "reused": True,
            "job_id": preflight_claim["job_id"],
            "task_accepted": True,
            "active": True,
            "reused_reason": _reused_reason_for_status(preflight_claim.get("status")),
        }
        if preflight_claim.get("version_id"):
            result["version_id"] = preflight_claim["version_id"]
            resumed = _resume_reused_paused_job(
                str(preflight_claim["version_id"]),
                supervisor_run_id=supervisor_run_id,
                dependency_snapshot=dependency_snapshot,
            )
            if resumed:
                result.update(resumed)
        return result
    preflight_job_id = str(preflight_claim["job_id"])
    preflight_owner = str(preflight_claim["claim_owner"])
    try:
        result = _enqueue_shot_impl(
            shot_id,
            prompt_override=prompt_override,
            extra_negative=extra_negative,
            reroll=reroll,
            critique=critique,
            after_shot_id=after_shot_id,
            auto_retake_count=auto_retake_count,
            supervisor_run_id=supervisor_run_id,
            dependency_snapshot=dependency_snapshot,
            critique_sources=critique_sources,
            operation_idempotency_key=operation_idempotency_key,
            operation_request_fingerprint=operation_request_fingerprint,
            operation_claim_token=operation_claim_token,
            operation_command=operation_command,
            preflight_job_id=preflight_job_id,
            preflight_owner=preflight_owner,
        )
    except Exception as exc:
        failure = _mark_video_preflight_failure(
            preflight_job_id,
            exc,
            claim_owner=preflight_owner,
        )
        if not failure.get("retry_scheduled"):
            raise
        result = {
            "reused": False,
            "job_id": preflight_job_id,
            "task_accepted": True,
            **failure,
        }
    if result.get("reused"):
        _close_reused_preflight_job(
            preflight_job_id,
            claim_owner=preflight_owner,
        )
    return result


def _enqueue_shot_impl(shot_id: str, *, prompt_override: str | None = None,
                       extra_negative: list[str] | None = None, reroll: bool = False,
                       critique: list[str] | None = None, after_shot_id: str | None = None,
                       auto_retake_count: int = 0,
                       supervisor_run_id: str | None = None,
                       dependency_snapshot: dict[str, Any] | None = None,
                       critique_sources: list[dict[str, Any]] | None = None,
                       operation_idempotency_key: str | None = None,
                       operation_request_fingerprint: str | None = None,
                       operation_claim_token: str | None = None,
                       operation_command: str = "video.generate_shot",
                       preflight_job_id: str | None = None,
                       preflight_owner: str | None = None,
                       preflight_repair: dict[str, Any] | None = None) -> dict:
    """为镜头创建参考图模式视频版本并入队。
    critique：上一版 AI 评语问题，作为本次必须改正项写入 prompt。
    幂等：相同 idem_key 的成功版本直接复用（reroll 时跳过复用）。

    薄编排器：具体解析/编译/落库步骤见 ``.enqueue_context``/``.enqueue_prompt``/
    ``.enqueue_persist``（2026-09-01 从本函数原 743 行拆出，移动未重写）。
    """
    (
        target_video_provider, target_video_model,
        target_prompt_profile, target_prompt_fingerprint,
    ) = enqueue_context.resolve_target_video_profile()
    authority_context = _assert_enqueue_storyboard_authority(shot_id)
    if (
        authority_context.narrative_authority_required
        and (prompt_override or "").strip()
    ):
        raise ValueError(
            "[NARRATIVE_PROMPT_OVERRIDE_REQUIRES_CANDIDATE] 叙事权威镜头不允许用"
            "自由文本覆盖已审读的分镜语义；请通过受控分镜候选修订后重新发布"
        )
    conn = get_conn()
    shot_row, ep, project = enqueue_context.load_video_binding_context(
        conn, shot_id, target_video_provider,
    )
    bible, shot, is_storyboard_pack_shot, screenplay, prior_shots = (
        enqueue_context.resolve_shot_context(conn, shot_row, ep, project, authority_context)
    )
    shot_plan, decision = enqueue_context.resolve_mode_decision(
        conn, shot_id, shot_row, authority_context,
    )
    first_frame_requirement, first_frame_source, boundary_source_shot_id = (
        enqueue_context.resolve_first_frame_requirement(shot_plan)
    )
    prev_row, boundary_prev_row, prev_shot, prompt_context_row, planned_dependency_id = (
        enqueue_context.resolve_dependency_rows(
            conn, shot_row, shot_plan, after_shot_id, boundary_source_shot_id,
        )
    )
    previous_prompt_version, previous_prompt_text, previous_prompt_fingerprint = (
        enqueue_context.resolve_previous_prompt(conn, prompt_context_row)
    )
    continuity_mode = enqueue_context.apply_continuity_mode(shot, prev_shot, is_storyboard_pack_shot)
    (
        boundary_relation_edit, boundary_relation_action, boundary_relation_reason,
        boundary_start_state, prev_state_out,
    ) = enqueue_context.resolve_boundary_relation(
        shot, prev_shot, shot_plan, first_frame_requirement, first_frame_source, boundary_prev_row,
    )
    prompt_prev_state_out, chain_after_shot_id, chain_after_version_id = (
        enqueue_context.resolve_chain_dependency(
            shot, shot_plan, continuity_mode, prev_row, prev_state_out, planned_dependency_id,
        )
    )
    outgoing_transition, incoming_transition = enqueue_context.resolve_transitions(
        conn, shot_row, continuity_mode,
    )

    if is_storyboard_pack_shot:
        prompt_text = enqueue_prompt.storyboard_pack_prompt_text(shot)
    else:
        prompt_text, preflight_repair = enqueue_prompt.compile_legacy_prompt(
            shot, prev_shot, screenplay, bible, extra_negative, critique, preflight_repair,
            chain_after_shot_id=chain_after_shot_id, continuity_mode=continuity_mode,
            incoming_transition=incoming_transition, outgoing_transition=outgoing_transition,
            prompt_prev_state_out=prompt_prev_state_out, shot_plan=shot_plan, decision=decision,
            first_frame_source=first_frame_source, boundary_relation_edit=boundary_relation_edit,
            boundary_relation_action=boundary_relation_action, boundary_start_state=boundary_start_state,
            previous_prompt_text=previous_prompt_text,
        )

    # 参考图是分镜级素材。重抽、改词或带评语只创建新视频版本，不能重新跑参考图生成。
    reference_gallery, current_reference_manifest = enqueue_prompt.resolve_reference_gallery(
        conn, shot_id, shot_row, ep, shot, screenplay, bible,
    )

    key = enqueue_prompt.build_idem_key(
        prompt_text, decision, chain_after_shot_id, chain_after_version_id,
        target_prompt_fingerprint=target_prompt_fingerprint, prompt_override=prompt_override,
        previous_prompt_fingerprint=previous_prompt_fingerprint,
        current_reference_manifest=current_reference_manifest, reference_gallery=reference_gallery,
        reroll=reroll, operation_idempotency_key=operation_idempotency_key,
        supervisor_run_id=supervisor_run_id, auto_retake_count=auto_retake_count,
        critique=critique, critique_sources=critique_sources,
    )

    reused = enqueue_prompt.find_reusable_version(
        conn, shot_id, key, reroll=reroll, operation_idempotency_key=operation_idempotency_key,
        supervisor_run_id=supervisor_run_id, dependency_snapshot=dependency_snapshot,
        preflight_job_id=preflight_job_id, preflight_owner=preflight_owner,
        preflight_repair=preflight_repair, operation_request_fingerprint=operation_request_fingerprint,
        operation_claim_token=operation_claim_token, operation_command=operation_command,
        shot_plan=shot_plan,
    )
    if reused is not None:
        return reused

    version_id = new_id("ver")
    image_meta = enqueue_persist.build_base_image_meta(
        decision, shot, prompt_text, is_storyboard_pack_shot,
        chain_after_shot_id=chain_after_shot_id, chain_after_version_id=chain_after_version_id,
        continuity_mode=continuity_mode, prompt_prev_state_out=prompt_prev_state_out,
        incoming_transition=incoming_transition, outgoing_transition=outgoing_transition,
        auto_retake_count=auto_retake_count, supervisor_run_id=supervisor_run_id,
        target_prompt_profile=target_prompt_profile, target_video_provider=target_video_provider,
        target_video_model=target_video_model, prompt_override=prompt_override, critique=critique,
        previous_prompt_version=previous_prompt_version,
        previous_prompt_fingerprint=previous_prompt_fingerprint,
        previous_prompt_text=previous_prompt_text, first_frame_source=first_frame_source,
        boundary_source_shot_id=boundary_source_shot_id, boundary_relation_edit=boundary_relation_edit,
        boundary_relation_action=boundary_relation_action, boundary_relation_reason=boundary_relation_reason,
        boundary_start_state=boundary_start_state,
    )
    enqueue_persist.apply_shot_plan_meta(image_meta, shot_plan)
    enqueue_persist.apply_optional_meta(
        image_meta, preflight_repair=preflight_repair, dependency_snapshot=dependency_snapshot,
        critique_sources=critique_sources, reference_gallery=reference_gallery,
    )

    budget_limit = episode_video_budget_limit(str(ep["id"]))
    from app.video_cost_model import initial_shot_generation_cost

    estimate = initial_shot_generation_cost(float(shot.duration_s))
    persisted = enqueue_persist.persist_new_video_version(
        conn, shot_id=shot_id, version_id=version_id, prompt_text=prompt_text, key=key,
        image_meta=image_meta, preflight_job_id=preflight_job_id, preflight_owner=preflight_owner,
        ep=ep, project=project, chain_after_shot_id=chain_after_shot_id,
        chain_after_version_id=chain_after_version_id, supervisor_run_id=supervisor_run_id,
        estimate=estimate, budget_limit=budget_limit,
        operation_idempotency_key=operation_idempotency_key,
        operation_request_fingerprint=operation_request_fingerprint,
        operation_claim_token=operation_claim_token, operation_command=operation_command,
        shot_plan=shot_plan,
    )
    job_id = persisted["job_id"]
    if not persisted["reserved"]:
        reconcile_episode_generation_status(ep["id"])
        result = {
            "reused": False, "version_id": version_id, "job_id": job_id,
            "paused_budget": True,
        }
        if preflight_repair:
            result["preflight_repair"] = preflight_repair
        return result

    dispatch_deferred = enqueue_persist.dispatch_new_video_job(
        conn, job_id=job_id, shot_id=shot_id, episode_id=ep["id"],
    )
    result = {
        "reused": False,
        "version_id": version_id,
        "job_id": job_id,
        "dispatch_deferred": dispatch_deferred,
        "task_accepted": True,
    }
    if preflight_repair:
        result["preflight_repair"] = preflight_repair
    return result

__all__ = [name for name in globals() if not name.startswith("__")]
