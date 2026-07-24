"""视频生成队列（PRD §4.5）：asyncio worker、幂等、成本熔断、重启恢复、自动质检与重抽。"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app import config, errors, hiagent, video_modes
from app.atomic_io import atomic_copy, atomic_write_bytes
from app.artifacts import (_adopted_video_paths, _invalidate_final_video,
                           clear_episode_artifacts, clear_shot_artifacts,
                           delete_episode_shots, delete_project_episodes,
                           delete_video_version, invalidate_episode_final,
                           invalidate_shot_video_derivatives,
                           purge_character_video_artifacts,
                           purge_project_video_artifacts, purge_shot_videos)
from app.compiler import ensure_source_excerpt_in_prompt, idem_key as make_idem_key, sanitize_seedance_prompt, shot_cost_cny
from app.db import get_conn, get_setting, log_provider_call, new_id, now, rows_to_dicts
from app.hiagent import ProviderError
from app.evidence import media as media_evidence
from app.orchestration import media_scheduler
from app.orchestration.media_runs import ensure_media_trace, mark_media_job_state

__all__ = [
    "clear_episode_artifacts", "clear_shot_artifacts", "delete_episode_shots",
    "delete_project_episodes", "delete_video_version", "invalidate_episode_final",
    "invalidate_shot_video_derivatives", "purge_character_video_artifacts",
    "purge_project_video_artifacts", "purge_shot_videos", "stop_shot_video_tasks",
]

_queue: asyncio.Queue[str] = asyncio.Queue()
_poll_queue: asyncio.Queue[str] = asyncio.Queue()
_workers: list[asyncio.Task] = []
_poll_workers: list[asyncio.Task] = []
_worker_target = 0
_poll_worker_target = 0
_dispatcher_task: asyncio.Task | None = None
# 延迟重排任务的强引用，避免被 GC 回收（asyncio 不持有后台任务的引用）。
_retry_tasks: set[asyncio.Task] = set()

_DISPATCH_INTERVAL_SECONDS = 1.0
_DISPATCH_BACKLOG_PER_WORKER = 2


class LeaseLost(RuntimeError):
    """The current process was fenced by recovery or another worker claim."""


def _assert_job_lease(job_id: str, owner: str, *, lease_seconds: float = 180.0) -> None:
    if not media_scheduler.renew_lease(job_id, owner, lease_seconds=lease_seconds):
        raise LeaseLost(f"job lease lost: {job_id} / {owner}")


def _enqueue_for_current_status(job_id: str) -> None:
    """Route provider polling away from expensive reference/video preparation.

    Both queues still use the same durable job row and CAS lease.  The split is
    only scheduling priority: a completed provider task must not sit behind a
    whole episode of image generation before it can be downloaded and adopted.
    """
    row = get_conn().execute(
        """SELECT j.status, v.provider_task_id
           FROM jobs j LEFT JOIN shot_versions v ON v.id=j.version_id
           WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    target = (
        _poll_queue
        if row and (row["status"] == "waiting_provider" or row["provider_task_id"])
        else _queue
    )
    target.put_nowait(job_id)


def _reference_gallery_ready(raw_meta: str | None) -> bool:
    try:
        meta = json.loads(raw_meta or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(meta.get("reference_images")) and meta.get("reference_generation_complete") is not False


def _auto_retake(raw_meta: str | None) -> bool:
    try:
        return int(json.loads(raw_meta or "{}").get("auto_retake_count") or 0) > 0
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _dispatch_due_jobs() -> dict[str, int]:
    """Continuously rebuild the runnable queues from durable job state.

    Priority is intentionally stage-aware:
    1. provider handles are always routed to the isolated poll queue;
    2. continuity-unblocked first-pass shots outrank auto retakes;
    3. blocked shots may prepare references only as spare bounded work;
    4. blocked shots whose references are already ready stay out of the hot
       queue until their predecessor succeeds, avoiding 15-second bounce loops.
    """
    conn = get_conn()
    stamp = now()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id, j.status, j.created_at, j.after_shot_id,
                  v.provider_task_id, v.image_inputs, s.shot_no
           FROM jobs j
           LEFT JOIN shot_versions v ON v.id=j.version_id
           LEFT JOIN shots s ON s.id=j.shot_id
           WHERE j.kind='video'
             AND j.status IN ('queued','waiting_provider')
             AND (j.next_retry_at IS NULL OR j.next_retry_at<=?)
             AND j.cancellation_requested=0 AND j.abandoned=0""",
        (stamp,),
    ).fetchall())

    poll_candidates: list[dict[str, Any]] = []
    main_candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    blocked_reference_candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    continuity_cache: dict[str, bool] = {}

    for row in rows:
        if row.get("status") == "waiting_provider" or row.get("provider_task_id"):
            poll_candidates.append(row)
            continue
        after_shot_id = row.get("after_shot_id")
        if after_shot_id:
            ready = continuity_cache.get(after_shot_id)
            if ready is None:
                from app.media_pipeline.scheduler import continuity_anchor_ready
                ready = continuity_anchor_ready(conn, after_shot_id)[0]
                continuity_cache[after_shot_id] = ready
        else:
            ready = True
        refs_ready = _reference_gallery_ready(row.get("image_inputs"))
        is_retake = _auto_retake(row.get("image_inputs"))
        age_key = float(row.get("created_at") or stamp)
        shot_key = int(row.get("shot_no") or 10**9)
        if ready:
            # First pass before retake; among first-pass jobs, a prepared gallery
            # can submit immediately and therefore gets the shortest path.
            rank = 2 if is_retake else (0 if refs_ready else 1)
            main_candidates.append(((rank, age_key, shot_key), row))
        elif not refs_ready:
            # Useful speculative work, but never ahead of a runnable video.
            rank = 1 if is_retake else 0
            blocked_reference_candidates.append(((rank, age_key, shot_key), row))

    poll_candidates.sort(key=lambda row: float(row.get("created_at") or stamp))
    main_candidates.sort(key=lambda item: item[0])
    blocked_reference_candidates.sort(key=lambda item: item[0])

    poll_capacity = max(1, _poll_worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    poll_slots = max(0, poll_capacity - _poll_queue.qsize())
    main_capacity = max(1, _worker_target or 1) * _DISPATCH_BACKLOG_PER_WORKER
    main_slots = max(0, main_capacity - _queue.qsize())

    poll_enqueued = 0
    for row in poll_candidates[:poll_slots]:
        _poll_queue.put_nowait(row["id"])
        poll_enqueued += 1

    chosen = [row for _, row in main_candidates[:main_slots]]
    remaining = max(0, main_slots - len(chosen))
    if remaining:
        from app.media_pipeline.retry_policy import prepared_reference_backlog
        speculative_limit = min(remaining, prepared_reference_backlog())
        chosen.extend(row for _, row in blocked_reference_candidates[:speculative_limit])
    for row in chosen:
        _queue.put_nowait(row["id"])

    return {"poll": poll_enqueued, "main": len(chosen), "due": len(rows)}


async def _durable_dispatcher() -> None:
    """DB-backed dispatcher; in-memory queue loss heals within one interval."""
    try:
        while True:
            try:
                _dispatch_due_jobs()
                # Recreate an unexpectedly dead worker without changing the
                # configured target. Worker loops catch job errors themselves,
                # so this is primarily protection against lifecycle regressions.
                if _worker_target > 0:
                    ensure_workers(_worker_target)
            except Exception as exc:  # noqa: BLE001 dispatcher must remain alive
                errors.record_and_format(exc, action="durable_media_dispatch")
            await asyncio.sleep(_DISPATCH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        return


def _start_durable_dispatcher() -> None:
    global _dispatcher_task
    if _dispatcher_task is not None and not _dispatcher_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _dispatcher_task = loop.create_task(_durable_dispatcher(), name="durable-media-dispatcher")


def _drain_memory_queue(queue: asyncio.Queue[str]) -> None:
    """Drop startup duplicates; every durable row is rediscovered immediately."""
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        else:
            queue.task_done()


async def _requeue_after(job_id: str, delay: float) -> None:
    """冷却 delay 秒后把 job 重新投入队列。状态已先置回 queued，故进程重启时
    recover_and_start 也能兜底重排，不依赖本协程存活。"""
    try:
        await asyncio.sleep(delay)
        _enqueue_for_current_status(job_id)
    except asyncio.CancelledError:
        pass


def _schedule_job_retry(
    job_id: str, exc: ProviderError, *, lease_owner: str | None = None
) -> bool:
    """瞬时（可重试）上游故障时把 job 延迟重排，返回是否已安排重试。
    超过 VIDEO_JOB_MAX_RETRIES 后返回 False，交由调用方走永久失败逻辑。"""
    if not getattr(exc, "retryable", False):
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT retry_count, max_retries, lease_owner FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if lease_owner and (not row or row["lease_owner"] != lease_owner):
        return False
    attempt = int(row["retry_count"] or 0) + 1 if row else 1
    max_retries = int(row["max_retries"] or config.VIDEO_JOB_MAX_RETRIES) if row else config.VIDEO_JOB_MAX_RETRIES
    if attempt > max_retries:
        return False
    delay = config.VIDEO_JOB_RETRY_BASE_DELAY * (2 ** (attempt - 1))
    note = (f"大模型/外部服务瞬时故障，已自动排队第 {attempt}/{max_retries} 次重试"
            f"（约 {int(delay)} 秒后）。无需处理；若多次重试后仍失败才需关注错误码。")
    updated = conn.execute(
        """UPDATE jobs SET status='queued', error=?, retry_count=?, next_retry_at=?,
                  lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?"""
        + (" AND lease_owner=?" if lease_owner else ""),
        (note, attempt, now() + delay, now(), job_id, *([lease_owner] if lease_owner else [])),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
        (job_id,),
    )
    conn.commit()
    job = conn.execute("SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job:
        mark_media_job_state(job["run_id"], job["step_run_id"], "queued", note)
    task = asyncio.get_running_loop().create_task(_requeue_after(job_id, delay))
    _retry_tasks.add(task)
    task.add_done_callback(_retry_tasks.discard)
    return True


def _defer_provider_poll(
    job_id: str,
    task_id: str,
    *,
    lease_owner: str,
    delay: float | None = None,
) -> bool:
    """供应商仍在生成时释放 worker，并持久化安排下一次状态查询。

    Phase 1：状态写入 waiting_provider（不再占 worker 槽）；单次 poll 后即调用本函数。
    这不是一次 provider retry：不会新建付费任务，也不消耗 retry_count。
    provider_task_id 已持久化，下一次只会继续轮询同一个任务。
    """
    conn = get_conn()
    wait = max(0.0, float(
        config.VIDEO_POLL_INTERVAL if delay is None else delay
    ))
    due = now() + wait
    note = (
        f"供应商任务 {task_id} 仍在生成，已释放本地 worker；"
        f"约 {int(wait)} 秒后自动继续查询，不会重复提交或产生新任务。"
    )
    updated = conn.execute(
        """UPDATE jobs SET status='waiting_provider', error=?, next_retry_at=?,
                  lease_owner=NULL, lease_expires_at=NULL, updated_at=?
           WHERE id=? AND status='running' AND lease_owner=?
             AND cancellation_requested=0 AND abandoned=0""",
        (note, due, now(), job_id, lease_owner),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE budget_reservations SET status='reserved' "
        "WHERE job_id=? AND status='running'",
        (job_id,),
    )
    conn.commit()
    job = conn.execute(
        "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if job:
        mark_media_job_state(job["run_id"], job["step_run_id"], "queued", note)
    task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
    _retry_tasks.add(task)
    task.add_done_callback(_retry_tasks.discard)
    return True


# ---------- 落盘路径 ----------

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
    """
    conn = get_conn()
    active = conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE episode_id=? AND kind='video'
             AND status IN ('queued','running','waiting_provider','waiting_retry')""",
        (episode_id,),
    ).fetchone()["c"]
    if active:
        return False
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
             AND status IN ('queued','running','waiting_provider','waiting_retry')
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


# ---------- 入队 ----------

def _load_shot_model(shot_row) -> "object":
    from app.schemas import Shot
    return Shot(
        shot_no=shot_row["shot_no"], duration_s=shot_row["duration_s"], shot_size=shot_row["shot_size"],
        camera_move=shot_row["camera_move"], scene_setting=shot_row["scene_setting"],
        scene_name=(shot_row["scene_name"] if "scene_name" in shot_row.keys() else "") or "",
        characters=json.loads(shot_row["characters"] or "[]"), action_desc=shot_row["action_desc"],
        first_frame_desc=(shot_row["first_frame_desc"] if "first_frame_desc" in shot_row.keys() else "") or "",
        last_frame_desc=(shot_row["last_frame_desc"] if "last_frame_desc" in shot_row.keys() else "") or "",
        source_excerpt=shot_row["source_excerpt"] or "",
        narration=shot_row["narration"], dialogues=json.loads(shot_row["dialogues"] or "[]"),
        transition=shot_row["transition"] or "硬切", continuity_from_prev=bool(shot_row["continuity_from_prev"]),
    )


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
        return {
            "source_version_id": version["id"],
            "revision": meta.get("reference_gallery_revision"),
            "edited": bool(meta.get("reference_gallery_edited")),
            "reference_images": refs,
            "fingerprint": _reference_gallery_fingerprint(meta),
        }
    return None


def _append_reference_notes_from_dicts(prompt_text: str, refs: list[dict]) -> str:
    lines: list[str] = []
    for idx, ref in enumerate(_usable_reference_dicts({"reference_images": refs}), start=1):
        label = ref.get("type") or "reference"
        source = str(ref.get("source") or "unknown").replace("_", " ")
        chars = f"; related characters: {', '.join(ref.get('relatedCharacterIds') or [])}" if ref.get("relatedCharacterIds") else ""
        lines.append(f"Reference image {idx}: use as {label}; source: {source}{chars}.")
    if not lines:
        return prompt_text
    note = (" Use the provided reference images as follows: " + " ".join(lines)
            + video_modes.REFERENCE_SINGLE_INSTANCE_NOTE)
    return prompt_text if note in prompt_text else prompt_text + note


def scene_generation_kinds(shot_row, requested: list[str] | None = None) -> list[str]:
    """旧关键帧 API 保留名；任何调用方都应改走参考图视频入口。"""
    raise ValueError("关键帧功能已下线；请从参考图视频入口直接生成本镜视频")


def shot_keyframes_ready(shot_row) -> bool:
    """兼容旧工具脚本；关键帧链路已下线，恒为 False。"""
    return False


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
    prev_scene = (_row_value(shot_row, "scene_setting") or "").strip()
    next_scene = (_row_value(next_shot, "scene_setting") or "").strip()
    if prev_scene == next_scene:
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


def enqueue_shot(shot_id: str, *, prompt_override: str | None = None,
                 extra_negative: list[str] | None = None, reroll: bool = False,
                 critique: list[str] | None = None, after_shot_id: str | None = None,
                 auto_retake_count: int = 0) -> dict:
    """为镜头创建参考图模式视频版本并入队。
    critique：上一版 AI 评语问题，作为本次必须改正项写入 prompt。
    幂等：相同 idem_key 的成功版本直接复用（reroll 时跳过复用）。"""
    from app.compiler import compile_prompt
    from app.schemas import Bible

    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise ValueError(f"镜头不存在：{shot_id}")
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot_row["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if not bool(_row_value(project, "harness_engine_enabled", 1)):
        raise ValueError("该项目的 Harness Engine 已由灰度开关隔离")
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise ValueError("分镜脚本未确认，不能生成视频（PRD 原则 P3：贵的环节前人工把关）")

    bible = Bible.model_validate(json.loads(project["bible_json"]))
    shot = _load_shot_model(shot_row)
    decision = _decision_from_mode_plan(shot_row) or video_modes.default_reference_decision()
    if decision.mode != video_modes.REFERENCE_IMAGE_MODE:
        decision = video_modes.default_reference_decision()

    # 跨镜连贯：接上镜时把上一镜动作作为承接线索写入 prompt
    prev_tail_action = None
    if after_shot_id:
        pr = conn.execute("SELECT action_desc FROM shots WHERE id=?", (after_shot_id,)).fetchone()
        prev_tail_action = pr["action_desc"] if pr else None

    outgoing_transition = _outgoing_transition_context(conn, shot_row)
    incoming_transition = None
    if int(shot_row["shot_no"]) > 1 and not bool(shot_row["continuity_from_prev"]):
        incoming_transition = _transition_value(shot_row)
        if incoming_transition == "硬切":
            incoming_transition = None

    prompt_text = (prompt_override if prompt_override else
                   compile_prompt(shot, bible, extra_negative,
                                  with_refs=True,
                                  from_scene=False,
                                  chained=False,
                                  critique=critique, prev_tail_action=prev_tail_action,
                                  with_last_frame=False,
                                  incoming_transition=incoming_transition,
                                  outgoing_transition=outgoing_transition["transition"] if outgoing_transition else None,
                                  next_scene=outgoing_transition["next_scene"] if outgoing_transition else None,
                                  next_first_frame_desc=outgoing_transition["next_first_frame_desc"] if outgoing_transition else None))
    prompt_text = ensure_source_excerpt_in_prompt(prompt_text, shot)

    # 参考图是分镜级素材。重抽、改词或带评语只创建新视频版本，不能重新跑参考图生成。
    reference_gallery = _load_reference_gallery(conn, shot_row)

    key_material = prompt_text + f"|mode:{video_modes.REFERENCE_IMAGE_MODE}|plan:{video_modes.decision_to_dict(decision)}|after:{after_shot_id or ''}"
    # 只有人工编辑会改变视频输入并打破原幂等键；未编辑画廊沿用历史幂等行为，
    # 普通重复点击仍直接复用已有成功视频。
    if reference_gallery and reference_gallery["revision"] is not None:
        key_material += (
            f"|reference_gallery:{reference_gallery['source_version_id']}"
            f"@{reference_gallery['revision']}:{reference_gallery['fingerprint']}"
        )
    if reroll:
        key = make_idem_key(key_material + f"#reroll{time.time()}")
    else:
        key = make_idem_key(key_material)
        # 复用成功版；同时挡住仍在排队/运行中的同键任务，避免双击重复付费。
        existing = conn.execute(
            "SELECT * FROM shot_versions WHERE shot_id=? AND idem_key=? "
            "AND status IN ('succeeded','queued','running','waiting_provider',"
            "'waiting_retry','waiting_human','paused_budget') "
            "ORDER BY CASE status WHEN 'succeeded' THEN 0 ELSE 1 END, version_no DESC "
            "LIMIT 1",
            (shot_id, key)).fetchone()
        if existing:
            return {"reused": True, "version_id": existing["id"]}

    if reference_gallery:
        prompt_text = _append_reference_notes_from_dicts(
            prompt_text, reference_gallery["reference_images"])

    version_no = (conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS m FROM shot_versions WHERE shot_id=?",
        (shot_id,)).fetchone()["m"]) + 1
    version_id = new_id("ver")
    image_meta = {
        "mode": decision.mode,
        "mode_decision": video_modes.decision_to_dict(decision),
        "after_shot_id": after_shot_id,
        "after_shot_no": None,
        "incoming_transition": incoming_transition,
        "outgoing_transition": outgoing_transition,
        "auto_retake_count": max(0, int(auto_retake_count)),
    }
    if reference_gallery:
        image_meta["reference_images"] = reference_gallery["reference_images"]
        image_meta["reference_gallery_source_version_id"] = reference_gallery["source_version_id"]
        image_meta["reference_gallery_fingerprint"] = reference_gallery["fingerprint"]
        if reference_gallery["revision"] is not None:
            image_meta["reference_gallery_revision"] = reference_gallery["revision"]
        if reference_gallery["edited"]:
            image_meta["reference_gallery_edited"] = True
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, created_at, image_inputs) "
        "VALUES(?,?,?,?,?, 'queued', ?, ?)",
        (version_id, shot_id, version_no, prompt_text, key, now(),
         json.dumps(image_meta, ensure_ascii=False)))
    job_id = new_id("job")
    budget_limit = float(get_setting("episode_cost_limit_cny") or 100)
    run_id, step_run_id = ensure_media_trace(
        workflow_type="video_generation", scope_id=shot_id,
        input_value={"prompt": prompt_text, "version": version_no}, budget_limit_cny=budget_limit,
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, shot_id, version_id, episode_id, project_id, status, created_at, "
        "updated_at, after_shot_id, run_id, step_run_id) "
        "VALUES(?, 'video', ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
        (
            job_id, shot_id, version_id, ep["id"], project["id"], now(), now(), after_shot_id,
            run_id, step_run_id,
        ))
    conn.execute("UPDATE episodes SET status='generating' WHERE id=? AND status='confirmed'", (ep["id"],))
    conn.commit()
    estimate = shot_cost_cny(shot.duration_s) + config.IMAGE_PRICE_PER_UNIT * 2
    reserved = media_scheduler.reserve_budget(
        job_id, ep["id"], estimate, budget_limit, conn=conn
    )
    if not reserved:
        _set_version(version_id, status="paused_budget")
        _set_job(job_id, "paused_budget", "集预算不足，任务已暂停")
        reconcile_episode_generation_status(ep["id"])
        return {
            "reused": False, "version_id": version_id, "job_id": job_id,
            "paused_budget": True,
        }
    _enqueue_for_current_status(job_id)
    return {"reused": False, "version_id": version_id, "job_id": job_id}


# ---------- 场景关键帧：生成 K 候选 + VLM 评审 + 自动放行 ----------

def enqueue_scene(shot_id: str, *, kinds: list[str] | None = None) -> dict:
    """已下线的旧关键帧入口；保留函数名只为旧调用方得到明确错误。"""
    raise ValueError("关键帧功能已下线；请从参考图视频入口直接生成本镜视频")


async def critique_version(version_id: str) -> list[str]:
    """取某视频版本的问题清单（AI 评语）：优先用已存的 QA issues；
    若该版本还没质检过，则现场抽帧跑一次 VLM 评审，并回存。供「带评语重生」避免重复犯错。"""
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        return []
    if v["qa_json"]:
        issues = (json.loads(v["qa_json"]) or {}).get("issues") or []
        if issues:
            return list(issues)
    if not v["video_path"] or not Path(v["video_path"]).exists():
        return []
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (v["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    try:
        from app.stages import qa_shot
        bible = json.loads(project["bible_json"])
        anchor_map = {c["name"]: c["appearance_canonical"] for c in bible["characters"]}
        anchors = [anchor_map[n] for n in json.loads(shot["characters"] or "[]") if n in anchor_map]
        frames = _extract_frames(v["video_path"])
        if not frames:
            return []
        qa = await qa_shot(frames, shot["action_desc"], shot["scene_setting"], anchors)
        _set_version(version_id, qa_json=json.dumps(qa, ensure_ascii=False))
        return list(qa.get("issues") or [])
    except Exception:  # noqa: BLE001 评语失败不阻塞重生
        return []


# ---------- 执行 ----------

def _set_job(
    job_id: str,
    status: str,
    error: str | None = None,
    *,
    lease_owner: str | None = None,
) -> bool:
    conn = get_conn()
    terminal = status in {"succeeded", "failed", "cancelled", "abandoned", "paused_budget"}
    if terminal:
        cursor = conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=?" + (" AND lease_owner=?" if lease_owner else ""),
            (status, error, now(), job_id, *([lease_owner] if lease_owner else [])),
        )
    else:
        cursor = conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?"
            + (" AND lease_owner=?" if lease_owner else ""),
            (status, error, now(), job_id, *([lease_owner] if lease_owner else [])),
        )
    if cursor.rowcount != 1:
        conn.rollback()
        return False
    conn.commit()
    row = conn.execute("SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row:
        mark_media_job_state(row["run_id"], row["step_run_id"], status, error)
    return True


def _set_version(version_id: str, **fields) -> None:
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE shot_versions SET {cols} WHERE id=?", (*fields.values(), version_id))
    conn.commit()


def _is_seedance_text_sensitive(message: str | None) -> bool:
    text = (message or "").lower()
    return (
        "inputtextsensitivecontentdetected" in text
        or "sensitive information" in text
        or "sensitive content" in text
        or "输入文本" in (message or "")
        or "敏感" in (message or "")
    )


_SEEDANCE_COPYRIGHT_MAX_RETRIES = 2


def _is_seedance_copyright_restricted(message: str | None) -> bool:
    text = (message or "").lower()
    return "copyright" in text or "版权" in (message or "")


def _provider_submitted_at(conn, job, task_id: str) -> float:
    """返回 provider 首次接受当前视频 task 的时间，并为旧任务补齐持久字段。

    轮询预算必须基于这个绝对时间，不能在 worker 重启后重新开始计时。
    """
    persisted = _row_value(job, "provider_submitted_at")
    if persisted:
        return float(persisted)
    operation_id = _row_value(job, "provider_operation_id")
    provider_call = conn.execute(
        """SELECT MIN(ts) AS submitted_at FROM provider_calls
           WHERE kind='video_create' AND status='OK'
             AND (operation_id=? OR meta LIKE ?)""",
        (operation_id, f"%{task_id}%"),
    ).fetchone()
    submitted_at = (
        float(provider_call["submitted_at"])
        if provider_call and provider_call["submitted_at"] is not None
        else float(_row_value(job, "attempt_started_at") or time.time())
    )
    conn.execute(
        "UPDATE jobs SET provider_submitted_at=? WHERE id=?",
        (submitted_at, job["id"]),
    )
    conn.commit()
    return submitted_at


def _ip_genericization_terms(conn, project_id: str) -> tuple[tuple[str, str], ...]:
    """把版权角色专名替换成中性代称（角色甲/乙…），降低 Seedance 输出版权误判概率。
    仅在平台已返回版权限制后的自动重提里使用。"""
    project = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        return ()
    try:
        chars = json.loads(project["bible_json"]).get("characters", [])
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    labels = "甲乙丙丁戊己庚辛壬癸"
    names = sorted({(c.get("name") or "").strip() for c in chars if (c.get("name") or "").strip()},
                   key=len, reverse=True)  # 先长后短，避免短名先替换截断长名
    return tuple((name, f"角色{labels[i]}" if i < len(labels) else f"角色{i + 1}")
                 for i, name in enumerate(names))


def _video_image_inputs_from_meta(meta: dict) -> list[tuple[str, str]]:
    meta["mode"] = video_modes.REFERENCE_IMAGE_MODE
    return video_modes.build_seedance_image_inputs(meta)


async def _prepare_reference_mode_inputs(conn, job, version, shot, ep, meta: dict, prompt_text: str) -> tuple[dict, str]:
    if meta.get("mode") != video_modes.REFERENCE_IMAGE_MODE:
        return meta, prompt_text
    # Historical galleries predate this marker and are complete.  A gallery
    # explicitly marked incomplete is a streamed checkpoint from an interrupted
    # generation and must resume instead of being mistaken for the final set.
    if meta.get("reference_images") and meta.get("reference_generation_complete") is not False:
        return meta, prompt_text
    from app.schemas import Bible

    project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    # 本集视图：关键帧文字锚点与参考图按集取覆盖该集的分段定妆照（同段同源）
    from app.portraits import bible_for_episode
    bible = bible_for_episode(job["project_id"], bible, ep["episode_no"])
    shot_model = _load_shot_model(shot)
    prev_shot = conn.execute("SELECT * FROM shots WHERE id=?", (meta.get("after_shot_id"),)).fetchone() if meta.get("after_shot_id") else None
    # 复用入队时已确定的模式决策，不在生成时再跑一次 LLM 选择：既省每镜一次文本调用，
    # 又避免模式在入队与执行之间无谓翻转（决策应在入队时一次定死）。
    decision = video_modes.dict_to_decision(meta.get("mode_decision") or {})
    if decision.mode != video_modes.REFERENCE_IMAGE_MODE:
        decision = video_modes.default_reference_decision()
    meta["mode"] = video_modes.REFERENCE_IMAGE_MODE
    # ── 第 1 次尝试：生成参考图 ──
    shot_id = job["shot_id"]
    rejection_details: list[dict[str, Any]] = []
    rejected_assets: list = []  # 质检未通过的参考图（带图片），存入 meta 供废弃画廊展示

    def _persist_reference_progress(current_assets: list, current_rejected: list) -> None:
        """Checkpoint each completed reference so the polling UI can render it."""
        meta["mode"] = video_modes.REFERENCE_IMAGE_MODE
        meta["mode_decision"] = video_modes.decision_to_dict(decision)
        meta["reference_generation_complete"] = False
        meta["reference_images"] = (
            [a.public_dict() for a in current_assets]
            + [a.public_dict() for a in current_rejected]
        )
        _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))

    assets = await video_modes.build_reference_assets(
        conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
        shot_id=shot_id, shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot,
        rejection_details=rejection_details, rejected_out=rejected_assets,
        on_progress=_persist_reference_progress)

    # ── 第 1 次失败：记录原始失败原因并重试 1 次 ──
    if not assets:
        log_provider_call(
            "reference_image_mode_attempt_1_failed", config.MODEL_TEXT, "REFERENCE_ATTEMPT_FAILED",
            None, 0, meta={
                "shot_id": shot_id,
                "attempt": 1,
                "original_failure_reason": f"第 1 次参考图生成未产出可用资产（{len(rejection_details)} 张被拒绝）",
                "rejection_details": rejection_details[:5],
            })

        # ── 第 2 次尝试：重试 ──
        retry_rejection: list[dict[str, Any]] = []
        # 重试会覆盖第 1 次尝试写入的同名参考图文件，故重置废弃列表，只保留与最终 assets 对应的本轮废弃图。
        rejected_assets = []
        assets = await video_modes.build_reference_assets(
            conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
            shot_id=shot_id, shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot,
            rejection_details=retry_rejection, rejected_out=rejected_assets,
            on_progress=_persist_reference_progress)
        rejection_details.extend(retry_rejection)

        if assets:
            log_provider_call(
                "reference_image_mode_retry_success", config.MODEL_TEXT, "REFERENCE_RETRY_SUCCESS",
                None, 0, meta={"shot_id": shot_id, "attempt": 2, "count": len(assets)})
        else:
            log_provider_call(
                "reference_image_mode_retry_failed", config.MODEL_TEXT, "REFERENCE_RETRY_FAILED",
                None, 0, meta={
                    "shot_id": shot_id,
                    "attempt": 2,
                    "total_rejection_count": len(rejection_details),
                    "rejection_details": rejection_details[:10],
                    "original_failure_reason": f"参考图模式 2 次尝试均未产出可用资产（共 {len(rejection_details)} 张被拒绝）",
                })

    # ── 参考图模式成功 ──
    if assets:
        meta["mode"] = video_modes.REFERENCE_IMAGE_MODE
        meta["mode_decision"] = video_modes.decision_to_dict(decision)
        # 选用图 + 质检未通过的废弃图（selectedForSeedance=False）一并存档：前者喂模型，后者只展示。
        meta["reference_images"] = [a.public_dict() for a in assets] + [a.public_dict() for a in rejected_assets]
        meta["reference_generation_complete"] = True
        meta.pop("first_frame_path", None)
        meta.pop("last_frame_path", None)
        meta.pop("first_frame_scene_id", None)
        meta.pop("last_frame_scene_id", None)
        prompt_text = video_modes.append_reference_prompt_notes(prompt_text, assets)
        try:
            from app.media_pipeline.reference_store import upsert_reference_set_from_meta
            upsert_reference_set_from_meta(
                shot_id=shot_id, version_id=version["id"], meta=meta, conn=conn,
            )
        except Exception:  # noqa: BLE001 参考图集落库失败不阻断视频
            pass
        _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
        return meta, prompt_text

    # ── 参考图模式彻底失败（2 次均失败）—— 记录原始失败原因 ──
    ref_failure_reason = (
        f"参考图模式 2 次尝试均未产出可用资产 "
        f"（共 {len(rejection_details)} 张被拒绝）"
    )
    log_provider_call(
        "reference_image_mode_original_failure", config.MODEL_TEXT, "REFERENCE_MODE_ORIGINAL_FAILURE",
        None, 0, meta={
            "shot_id": shot_id,
            "original_failure_reason": ref_failure_reason,
            "rejection_count": len(rejection_details),
            "rejection_details": rejection_details[:10],
        })

    meta["reference_failure_logs"] = (meta.get("reference_failure_logs") or []) + [{
        "mode": video_modes.REFERENCE_IMAGE_MODE,
        "original_failure_reason": ref_failure_reason,
        "rejection_count": len(rejection_details),
        "rejection_details": rejection_details[:10],
        "prompt": prompt_text[:500],
    }]
    meta["reference_generation_complete"] = True
    _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
    raise ProviderError(f"视频生成任务失败：参考图模式未产出可用参考图（{ref_failure_reason}）")


async def _run_job(job_id: str, *, lease_owner: str | None = None) -> None:
    conn = get_conn()
    owner = lease_owner or f"direct-{id(asyncio.current_task())}"
    if lease_owner is None:
        if not media_scheduler.claim_job(job_id, owner, lease_seconds=180.0):
            return
        run_row = conn.execute(
            "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if run_row:
            mark_media_job_state(run_row["run_id"], run_row["step_run_id"], "running")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job["status"] != "running" or job["lease_owner"] != owner:
        return
    if job["kind"] != "video":
        # 旧版关键帧 job 可能在升级前已持久化。它们不再恢复或执行，避免继续消耗图片额度，
        # 同时清除造成前端长期显示“生成中”的遗留状态。
        conn.execute("UPDATE shots SET scene_status='none' WHERE id=?", (job["shot_id"],))
        conn.commit()
        if _set_job(
            job["id"], "cancelled", "关键帧功能已下线；请从参考图视频入口重新生成",
            lease_owner=owner,
        ):
            media_scheduler.settle_budget(job["id"], 0.0, success=False)
        return
    version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (job["version_id"],)).fetchone()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (job["episode_id"],)).fetchone()

    # 视频固定参考图模式：不再使用首/尾帧作为 Seedance 输入。
    meta = json.loads(version["image_inputs"] or "{}")

    started = time.time()
    try:
        task_id = version["provider_task_id"]
        provider_submitted_at = (
            _provider_submitted_at(conn, job, task_id) if task_id else None
        )
        result = None
        provider_operation_id = f"video-create-{version['id']}"
        if task_id:
            conn.execute(
                "UPDATE jobs SET provider_operation_id=?, provider_create_state='accepted', "
                "provider_non_cancellable=1 WHERE id=?",
                (provider_operation_id, job_id),
            )
            conn.commit()
        _set_version(version["id"], status="running")
        prompt_text = ensure_source_excerpt_in_prompt(version["prompt_text"], _load_shot_model(shot))
        if prompt_text != version["prompt_text"]:
            _set_version(version["id"], prompt_text=prompt_text)
        meta["mode"] = video_modes.REFERENCE_IMAGE_MODE
        meta.pop("first_frame_path", None)
        meta.pop("last_frame_path", None)
        meta.pop("first_frame_scene_id", None)
        meta.pop("last_frame_scene_id", None)
        meta, prompt_text = await _prepare_reference_mode_inputs(conn, job, version, shot, ep, meta, prompt_text)
        _assert_job_lease(job_id, owner)

        # 连续镜调度级依赖：无可用尾帧时不得提交 Seedance
        if job["after_shot_id"] and not version["provider_task_id"]:
            from app.media_pipeline.scheduler import continuity_anchor_ready
            ready, reason = continuity_anchor_ready(conn, job["after_shot_id"])
            if not ready:
                wait = 15.0
                note = reason or "等待上一镜连续锚点"
                status = "waiting_human" if "人工" in note else "queued"
                conn.execute(
                    """UPDATE jobs SET status=?, error=?, next_retry_at=?,
                              lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE id=? AND lease_owner=?""",
                    (status, note, now() + wait, now(), job_id, owner),
                )
                conn.execute(
                    "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
                    (job_id,),
                )
                conn.commit()
                if status == "queued":
                    task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
                    _retry_tasks.add(task)
                    task.add_done_callback(_retry_tasks.discard)
                return

        # 视频提交配额：首轮优先，重抽限额
        if not version["provider_task_id"]:
            from app.media_pipeline.scheduler import can_admit_video_submit
            is_retake = int(meta.get("auto_retake_count") or 0) > 0
            ok, reason = can_admit_video_submit(
                episode_id=job["episode_id"], project_id=job["project_id"], is_auto_retake=is_retake,
            )
            if not ok:
                wait = 20.0
                conn.execute(
                    """UPDATE jobs SET status='queued', error=?, next_retry_at=?,
                              lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE id=? AND lease_owner=?""",
                    (reason or "等待视频槽位", now() + wait, now(), job_id, owner),
                )
                conn.execute(
                    "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
                    (job_id,),
                )
                conn.commit()
                task = asyncio.get_running_loop().create_task(_requeue_after(job_id, wait))
                _retry_tasks.add(task)
                task.add_done_callback(_retry_tasks.discard)
                return

        safety_retry_used = bool(meta.get("seedance_safety_retry"))
        copyright_retries = int(meta.get("seedance_copyright_retries") or 0)
        image_inputs: list[tuple[str, str]] | None = None

        while True:
            if not task_id:  # 重启恢复时可能已有 task_id，直接续轮询
                _assert_job_lease(job_id, owner)
                if image_inputs is None:
                    # first_frame + last_frame 均来自已过审关键图；缺任一张即失败，不做艺术兜底替换。
                    image_inputs = _video_image_inputs_from_meta(meta)
                    if meta.get("mode") == video_modes.REFERENCE_IMAGE_MODE:
                        meta["reference_image_used"] = bool(image_inputs)
                        meta["first_frame_used"] = False
                        meta["last_frame_used"] = False
                    else:
                        meta["first_frame_used"] = bool(image_inputs)
                        meta["last_frame_used"] = any(role == "last_frame" for _, role in image_inputs)
                    _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
                try:
                    conn.execute(
                        "UPDATE jobs SET provider_operation_id=?, provider_create_state='submitting', "
                        "updated_at=? WHERE id=?",
                        (provider_operation_id, now(), job_id),
                    )
                    conn.commit()
                    from app.media_pipeline.concurrency import (
                        report_congestion, report_healthy, semaphore_for,
                    )
                    from app.media_pipeline import stages as media_stages
                    async with semaphore_for(media_stages.RESOURCE_VIDEO_SUBMIT):
                        try:
                            task_id = await hiagent.create_video_task(
                                prompt_text,
                                image_urls=image_inputs,
                                call_meta={
                                    "asset_kind": "video",
                                    "episode_id": ep["id"],
                                    "episode_no": ep["episode_no"],
                                    "shot_id": shot["id"],
                                    "shot_no": shot["shot_no"],
                                    "version_id": version["id"],
                                    "version_no": version["version_no"],
                                    "operation_id": provider_operation_id,
                                })
                            report_healthy(media_stages.RESOURCE_VIDEO_SUBMIT)
                        except ProviderError as submit_exc:
                            if getattr(submit_exc, "retryable", False) or "429" in str(submit_exc):
                                report_congestion(media_stages.RESOURCE_VIDEO_SUBMIT, reason="submit")
                            raise
                    _assert_job_lease(job_id, owner)
                except ProviderError as exc:
                    _assert_job_lease(job_id, owner)
                    conn.execute(
                        "UPDATE jobs SET provider_create_state=?, updated_at=? WHERE id=?",
                        ("unknown" if exc.retryable else "not_started", now(), job_id),
                    )
                    conn.commit()
                    if _is_seedance_text_sensitive(str(exc)) and not safety_retry_used:
                        prompt_text = sanitize_seedance_prompt(prompt_text, aggressive=True)
                        safety_retry_used = True
                        meta["seedance_safety_retry"] = True
                        meta["seedance_safety_reason"] = str(exc)[:300]
                        _set_version(version["id"], prompt_text=prompt_text, provider_task_id=None,
                                     image_inputs=json.dumps(meta, ensure_ascii=False))
                        continue
                    if _is_seedance_copyright_restricted(str(exc)) and copyright_retries < _SEEDANCE_COPYRIGHT_MAX_RETRIES:
                        copyright_retries += 1
                        if copyright_retries == 1:
                            prompt_text = sanitize_seedance_prompt(
                                prompt_text, aggressive=True,
                                extra_terms=_ip_genericization_terms(conn, job["project_id"]))
                        meta["seedance_copyright_retries"] = copyright_retries
                        meta["seedance_copyright_reason"] = str(exc)[:300]
                        _set_version(version["id"], prompt_text=prompt_text, provider_task_id=None,
                                     image_inputs=json.dumps(meta, ensure_ascii=False))
                        continue
                    raise
                # Persist the paid provider handle and the non-cancellable flag in
                # one local transaction. The stable Idempotency-Key covers the
                # unavoidable provider-accepted/local-commit crash window.
                conn.execute(
                    "UPDATE shot_versions SET provider_task_id=? WHERE id=?",
                    (task_id, version["id"]),
                )
                conn.execute(
                    "UPDATE jobs SET provider_operation_id=?, provider_create_state='accepted', "
                    "provider_non_cancellable=1, provider_submitted_at=?, updated_at=? WHERE id=?",
                    (provider_operation_id, now(), now(), job_id),
                )
                conn.commit()
                provider_submitted_at = conn.execute(
                    "SELECT provider_submitted_at FROM jobs WHERE id=?", (job_id,)
                ).fetchone()["provider_submitted_at"]

            # Phase 1：单次查询后立即释放 worker；供应商仍在跑则写入 waiting_provider。
            # 不再用 15 分钟连续占槽窗口（VIDEO_POLL_BUDGET 已置 0）。
            state = conn.execute(
                "SELECT cancellation_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if state and state["cancellation_requested"]:
                media_scheduler.settle_budget(job_id, 0.0, success=False)
                return
            _assert_job_lease(job_id, owner)
            from app.media_pipeline.concurrency import (
                report_congestion, report_healthy, semaphore_for,
            )
            from app.media_pipeline import stages as media_stages
            async with semaphore_for(media_stages.RESOURCE_VIDEO_POLL):
                try:
                    result = await hiagent.poll_video_task(
                        task_id,
                        call_meta={
                            "asset_kind": "video",
                            "episode_id": ep["id"],
                            "episode_no": ep["episode_no"],
                            "shot_id": shot["id"],
                            "shot_no": shot["shot_no"],
                            "version_id": version["id"],
                            "version_no": version["version_no"],
                            "task_id": task_id,
                        })
                    report_healthy(media_stages.RESOURCE_VIDEO_POLL)
                except ProviderError as poll_exc:
                    if getattr(poll_exc, "retryable", False) or "429" in str(poll_exc):
                        report_congestion(media_stages.RESOURCE_VIDEO_POLL, reason="poll")
                    raise
            _assert_job_lease(job_id, owner)
            if result is None or result["status"] not in ("succeeded", "failed"):
                provider_age = time.time() - float(provider_submitted_at or time.time())
                if provider_age >= config.VIDEO_PROVIDER_MAX_WAIT:
                    raise ProviderError(
                        f"供应商任务 {task_id} 已持续运行 "
                        f"{provider_age / 3600:.1f} 小时，超过系统保护上限；"
                        "任务可能卡在上游，请联系供应商核查"
                    )
                if _defer_provider_poll(job_id, task_id, lease_owner=owner):
                    return
                raise LeaseLost(f"provider poll defer lost lease: {job_id} / {owner}")
            if result["status"] == "failed":
                error_text = result["error"][:400]
                if _is_seedance_text_sensitive(error_text) and not safety_retry_used:
                    prompt_text = sanitize_seedance_prompt(prompt_text, aggressive=True)
                    safety_retry_used = True
                    task_id = None
                    meta["seedance_safety_retry"] = True
                    meta["seedance_safety_reason"] = error_text
                    _set_version(version["id"], prompt_text=prompt_text, provider_task_id=None,
                                 image_inputs=json.dumps(meta, ensure_ascii=False))
                    continue
                if _is_seedance_copyright_restricted(error_text) and copyright_retries < _SEEDANCE_COPYRIGHT_MAX_RETRIES:
                    copyright_retries += 1
                    if copyright_retries == 1:  # 首次重提：去掉版权专名 + 激进改写，降低输出与原 IP 相似度
                        prompt_text = sanitize_seedance_prompt(
                            prompt_text, aggressive=True,
                            extra_terms=_ip_genericization_terms(conn, job["project_id"]))
                    task_id = None  # 再次重提靠重新生成的随机性（同一镜其它版本可成功即说明判定是概率性的）
                    meta["seedance_copyright_retries"] = copyright_retries
                    meta["seedance_copyright_reason"] = error_text
                    _set_version(version["id"], prompt_text=prompt_text, provider_task_id=None,
                                 image_inputs=json.dumps(meta, ensure_ascii=False))
                    continue
                raise ProviderError(f"Seedance 任务失败：{error_text}")
            break

        _assert_job_lease(job_id, owner)
        dest = _video_path(job["project_id"], ep["episode_no"], shot["shot_no"], version["version_no"])
        await hiagent.download(result["video_url"], str(dest))
        _assert_job_lease(job_id, owner)
        latency = round(time.time() - started, 1)
        cost = shot_cost_cny(shot["duration_s"])
        _set_version(version["id"], status="succeeded", video_path=str(dest),
                     last_frame_url=result["last_frame_url"], cost_cny=cost, latency_s=latency)
        # 评审墙产生了新片段，旧的整集合成视频即过期 → 删除，避免成片台展示陈旧成品
        _invalidate_final_video(job["project_id"], ep["episode_no"])
        # 自动 QA 可能跑满 VLM 读超时（默认 300s），超过默认 180s lease 会被 sweeper
        # 抢占：原协程仍会跑完但无法 settle，新 worker 则对已成功版本重跑付费链路。
        _assert_job_lease(
            job_id,
            owner,
            lease_seconds=max(180.0, float(config.TIMEOUT_VLM_READ) + 60.0),
        )
        await _maybe_auto_qa(job, version["id"], str(dest))
        _assert_job_lease(job_id, owner)
        media_evidence.record_video_candidate(
            version["id"], step_run_id=_row_value(job, "step_run_id")
        )
        technical = json.loads(conn.execute(
            "SELECT technical_validation_json FROM shot_versions WHERE id=?", (version["id"],)
        ).fetchone()["technical_validation_json"] or "{}")
        if not technical.get("passed"):
            raise ProviderError("视频文件技术校验失败，候选不可采用")
        media_evidence.select_best_video_candidate(job["shot_id"])
        if _set_job(job_id, "succeeded", lease_owner=owner):
            media_scheduler.settle_budget(job_id, cost, success=True)
            reconcile_episode_generation_status(job["episode_id"])
    except LeaseLost:
        return
    except (ProviderError, Exception) as exc:  # noqa: BLE001 失败要响：原文进日志，前端给码+分类
        if not media_scheduler.renew_lease(job_id, owner, lease_seconds=180.0):
            return
        public = errors.record_and_format(
            exc, action="shot_video_generate",
            context={"shot_id": job["shot_id"], "version_id": version["id"], "job_id": job_id})
        # 上游瞬时故障（超时/网络/限流/5xx）先 job 级延迟重排，扛过分钟级抖动；
        # 重试次数耗尽或不可重试的错误才永久判失败。
        if isinstance(exc, ProviderError) and _schedule_job_retry(
            job_id, exc, lease_owner=owner
        ):
            _set_version(version["id"], status="queued")
            return
        _set_version(version["id"], status="failed", error=public)
        if _set_job(job_id, "failed", public, lease_owner=owner):
            media_scheduler.settle_budget(job_id, 0.0, success=False)
            reconcile_episode_generation_status(job["episode_id"])


async def _maybe_auto_qa(job, version_id: str, video_path: str) -> None:
    """自动质检 + 一次自动重抽（QA 失败不阻塞流程，只标记未质检）。"""
    if get_setting("auto_qa") != "true" or not shutil.which("ffmpeg"):
        return
    conn = get_conn()
    try:
        frames = _extract_frames(video_path)
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        bible = json.loads(project["bible_json"])
        anchor_map = {c["name"]: c["appearance_canonical"] for c in bible["characters"]}
        anchors = [anchor_map[n] for n in json.loads(shot["characters"] or "[]") if n in anchor_map]
        from app.stages import qa_shot
        qa = await qa_shot(frames, shot["action_desc"], shot["scene_setting"], anchors)
        _set_version(version_id, qa_json=json.dumps(qa, ensure_ascii=False))
        # QA 非标准输出虽可恢复部分分数，但证据不足以触发付费重抽。
        # 这种情况保留结果供人工确认，避免把 VLM 格式错误当成视频质量错误。
        if qa.get("qa_recovered"):
            log_provider_call(
                "vlm_qa", config.MODEL_VLM, "QA_RECOVERED_NO_RETAKE", None, 0,
                meta={"shot_id": job["shot_id"], "version_id": version_id, "qa": qa})
            return
        threshold = float(get_setting("auto_retake_threshold") or 0.6)
        version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
        meta = json.loads(version["image_inputs"] or "{}") if version else {}
        if 0 <= qa.get("overall", -1) < threshold and meta.get("mode") == video_modes.REFERENCE_IMAGE_MODE:
            logs = meta.get("reference_failure_logs") or []
            # 只留轻量元信息做审计；绝不把带 base64 url 的参考图整份塞进日志，
            # 否则会在 image_inputs 里成倍堆积 base64，撑爆单集响应体积。
            log_refs = [
                {k: v for k, v in (r or {}).items() if k not in ("url", "path")}
                for r in (meta.get("reference_images") or [])
            ]
            logs.append({
                "mode": video_modes.REFERENCE_IMAGE_MODE,
                "reason": "Video QA failed after reference image mode.",
                "reference_images": log_refs,
                "prompt": version["prompt_text"][:500],
                "qa": qa,
            })
            meta["reference_failure_logs"] = logs
            meta["retry_reason"] = "视频质检未通过，已复用同一组参考图自动重抽视频。"
            _set_version(version_id, image_inputs=json.dumps(meta, ensure_ascii=False))
            auto_retake_count = int(meta.get("auto_retake_count") or 0)
            from app.media_pipeline.retry_policy import decide_qa_retake
            decision = decide_qa_retake(
                auto_retake_count=auto_retake_count,
                qa_overall=float(qa.get("overall", -1)),
                threshold=threshold,
            )
            if decision.allow:
                enqueue_shot(
                    job["shot_id"],
                    extra_negative=qa.get("issues", [])[:3],
                    reroll=True,
                    after_shot_id=job["after_shot_id"],
                    auto_retake_count=decision.attempt,
                )
            return
        if 0 <= qa.get("overall", -1) < threshold and version["version_no"] == 1 and qa.get("issues"):
            # 旧非参考图路径兜底；参考图模式由上面统一策略覆盖
            if meta.get("mode") != video_modes.REFERENCE_IMAGE_MODE:
                enqueue_shot(job["shot_id"], extra_negative=qa["issues"][:3],
                             after_shot_id=job["after_shot_id"])
    except Exception as exc:  # noqa: BLE001 QA 异常只记录，不影响已落盘的视频
        _set_version(version_id, qa_json=json.dumps({"overall": -1, "issues": [f"质检未完成：{exc}"]}, ensure_ascii=False))
        log_provider_call("vlm_qa", config.MODEL_VLM, "QA_ERROR", None, 0, error=str(exc))


def _extract_frames(video_path: str) -> list[str]:
    """ffmpeg 抽 首/中/尾 3 帧，返回 base64 列表。"""
    frames = []
    with tempfile.TemporaryDirectory() as td:
        for i, pos in enumerate(("0", "50%", "99%")):
            out = Path(td) / f"f{i}.jpg"
            cmd = ["ffmpeg", "-y", "-loglevel", "error"]
            if pos == "0":
                cmd += ["-i", video_path, "-vf", "select=eq(n\\,0)", "-vframes", "1"]
            else:
                # 用 ffprobe 拿时长再定位
                dur = float(subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
                    capture_output=True, text=True, check=True).stdout.strip() or 5)
                ts = dur * (0.5 if pos == "50%" else 0.97)
                cmd += ["-ss", f"{ts:.2f}", "-i", video_path, "-vframes", "1"]
            cmd += ["-q:v", "4", str(out)]
            subprocess.run(cmd, check=True, capture_output=True)
            frames.append(hiagent.encode_image_file(str(out)))
    return frames


# ---------- worker 生命周期 ----------

async def _worker_loop(name: str, queue: asyncio.Queue[str] | None = None) -> None:
    work_queue = queue or _queue
    while True:
        job_id = await work_queue.get()
        try:
            claim = media_scheduler.claim_job(job_id, name, lease_seconds=180.0)
            if claim:
                row = get_conn().execute(
                    "SELECT run_id, step_run_id FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if row:
                    mark_media_job_state(row["run_id"], row["step_run_id"], "running")
                await _run_job(job_id, lease_owner=name)
        except Exception as exc:  # noqa: BLE001 worker 永不死亡，但错误必须落库
            public = errors.record_and_format(exc, action="worker_loop", context={"job_id": job_id})
            if _set_job(job_id, "failed", public, lease_owner=name):
                media_scheduler.settle_budget(job_id, 0.0, success=False)
        finally:
            work_queue.task_done()


def recover_and_start(loop_concurrency: int | None = None) -> None:
    """启动时恢复队列（PRD §4.5 验收：中途杀进程重启后队列状态可恢复）。"""
    from app.media_pipeline.bootstrap import start_media_pipeline
    from app.media_pipeline.concurrency import channel_limit
    from app.media_pipeline import stages as media_stages

    start_media_pipeline()
    decommission_legacy_keyframe_jobs()
    # Reconcile expired durable leases, then rebuild scheduling exclusively from
    # DB state. Startup recovery may have pre-enqueued dozens of duplicate IDs;
    # discarding those in-memory copies is safe because jobs are durable.
    media_scheduler.recoverable_jobs()
    _drain_memory_queue(_queue)
    _drain_memory_queue(_poll_queue)
    conn = get_conn()
    generating_episode_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM episodes WHERE status='generating'"
        ).fetchall()
    ]
    for episode_id in generating_episode_ids:
        reconcile_episode_generation_status(episode_id)
    n = loop_concurrency or max(
        channel_limit(media_stages.RESOURCE_VIDEO_SUBMIT),
        channel_limit(media_stages.RESOURCE_REFERENCE),
    )
    ensure_workers(n)
    _start_durable_dispatcher()
    _dispatch_due_jobs()


def _recover_one_media_job(
    conn, job_id: str, run_id: str | None, step_run_id: str | None, reason: str
) -> bool:
    """把一个卡住的媒体 job 复位回 queued，等待持久调度器接管：
    - running/queued job 统一回到 queued，清空旧 lease；持久化 retry 到期时间保留
    - Run 立即进入 WAITING_RETRY，监控页显示“恢复排队中”
    - 被中断的 Step 保持 FAILED 审计终态，并创建 iteration+1 的 READY attempt
    返回 True 表示实际复位过；False 表示 job 已不存在或被并发改动（调用方忽略）。"""
    cursor = conn.execute(
        "UPDATE jobs SET status='queued', lease_owner=NULL, lease_expires_at=NULL, "
        "error=NULL, updated_at=? "
        "WHERE id=? AND status IN ('running','queued','waiting_provider') "
        "AND cancellation_requested=0 AND abandoned=0",
        (now(), job_id),
    )
    if cursor.rowcount != 1:
        return False
    try:
        from app.orchestration.state_machine import transition_run, transition_step

        run = conn.execute(
            "SELECT status FROM workflow_runs WHERE id=?", (run_id,)
        ).fetchone() if run_id else None
        if run and run["status"] in {"RUNNING", "PAUSED_EXTERNAL"}:
            transition_run(
                run_id, run["status"], "WAITING_RETRY", reason,
                failure_code=(
                    "SERVICE_RESTART" if run["status"] == "PAUSED_EXTERNAL" else "LEASE_EXPIRED"
                ),
                conn=conn,
            )
        old_step = conn.execute(
            "SELECT * FROM step_runs WHERE id=?", (step_run_id,)
        ).fetchone() if step_run_id else None
        if old_step:
            previous_status = old_step["status"]
            if previous_status == "RUNNING":
                transition_step(
                    step_run_id, "RUNNING", "FAILED", reason,
                    decision="retry", error_code="LEASE_EXPIRED", conn=conn,
                )
            if previous_status in {"RUNNING", "FAILED"}:
                iteration = conn.execute(
                    "SELECT COALESCE(MAX(iteration_no),0)+1 AS n FROM step_runs "
                    "WHERE run_id=? AND step_key=?",
                    (run_id, old_step["step_key"]),
                ).fetchone()["n"]
                new_step_id = new_id("step")
                conn.execute(
                    """INSERT INTO step_runs(
                           id, run_id, step_key, iteration_no, parent_step_run_id, status,
                           agent_name, contract_version, prompt_version, policy_version,
                           input_artifact_ids_json, context_manifest_json
                       ) VALUES(?,?,?,?,?,'PENDING',?,?,?,?,?,?)""",
                    (
                        new_step_id, run_id, old_step["step_key"], int(iteration), step_run_id,
                        old_step["agent_name"], old_step["contract_version"],
                        old_step["prompt_version"], old_step["policy_version"],
                        old_step["input_artifact_ids_json"] or "[]",
                        old_step["context_manifest_json"] or "{}",
                    ),
                )
                transition_step(new_step_id, "PENDING", "READY", reason, conn=conn)
                conn.execute(
                    "UPDATE jobs SET step_run_id=? WHERE id=?", (new_step_id, job_id)
                )
                conn.execute(
                    "INSERT INTO run_events(id, run_id, step_run_id, ts, event_type, severity, "
                    "message, payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_id("evt"), run_id, new_step_id, now(), "MEDIA_RECOVERY_QUEUED",
                        "warning", reason,
                        json.dumps(
                            {"job_id": job_id, "previous_step_run_id": step_run_id},
                            ensure_ascii=False,
                        ),
                    ),
                )
    except Exception:  # noqa: BLE001 legacy/minimal schemas still recover the durable job itself
        pass
    # The durable dispatcher will see this row within one second. Avoid directly
    # flooding the FIFO when startup/sweeper recovers an entire episode.
    return True


def decommission_legacy_keyframe_jobs() -> int:
    """取消升级前遗留的关键帧任务并清掉镜头的假运行状态。

    关键帧候选不再属于视频生成链路。已完成的历史图片暂不删除，便于审计；
    但 queued/running/paused_budget 任务必须停止，且不能被启动恢复重新入队。
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT id FROM jobs
           WHERE kind='scene' AND status IN ('queued','running','paused_budget')"""
    ).fetchall()
    for row in rows:
        try:
            media_scheduler.request_cancel(row["id"])
        except Exception:  # noqa: BLE001 兼容旧库里缺少编排审计记录的任务
            conn.execute(
                """UPDATE jobs SET status='cancelled', cancellation_requested=1,
                          lease_owner=NULL, lease_expires_at=NULL, reserved_cost_cny=0,
                          error=?, updated_at=? WHERE id=?""",
                ("关键帧功能已下线；请从参考图视频入口重新生成", now(), row["id"]),
            )
            conn.execute(
                """UPDATE budget_reservations SET status='released', settled_at=?,
                          actual_cost_cny=0 WHERE job_id=?""",
                (now(), row["id"]),
            )
    conn.execute("UPDATE shots SET scene_status='none' WHERE scene_status!='none'")
    conn.commit()
    return len(rows)


def recover_media_jobs() -> int:
    """启动时恢复因服务重启被中断的媒体任务。

    init_db() 在重启时把所有 status='RUNNING' 的 workflow_runs 标为 PAUSED_EXTERNAL +
    failure_code='SERVICE_RESTART'，同时把对应 step_runs 标 FAILED；但底层 jobs 表的
    lease（默认 180s）在重启那一刻往往还没过期，media_scheduler.recoverable_jobs()
    只扫 status='running' AND lease_expires_at<now 的 job，因此不会重新入队——
    结果就是用户看到的"任务卡在'服务重启，可从安全检查点恢复'"。

    本函数把这些 job 显式复位回 queued；数据库驱动的持久调度器会在下一轮重新
    发现它们。run 从 PAUSED_EXTERNAL 转回 WAITING_RETRY，旧 FAILED step 保留为
    审计历史，并创建 iteration+1 的 READY step 供 worker 接管。

    边界：不恢复 PAUSED_BUDGET（预算不足，需显式 retry_paused 释放预算后重试）；
         不恢复 FAILED/CANCELLED（真正报错或人工取消）。"""
    decommission_legacy_keyframe_jobs()
    conn = get_conn()
    rows = rows_to_dicts(conn.execute(
        """SELECT j.id AS job_id, j.run_id, j.step_run_id
           FROM jobs j
           JOIN workflow_runs wr ON wr.id=j.run_id
           WHERE j.status IN ('running','queued')
             AND wr.status='PAUSED_EXTERNAL'
             AND wr.failure_code='SERVICE_RESTART'
             AND j.cancellation_requested=0
             AND j.abandoned=0""",
    ))
    resumed = 0
    for r in rows:
        if _recover_one_media_job(
            conn, r["job_id"], r["run_id"], r["step_run_id"], "服务重启后自动恢复任务"
        ):
            resumed += 1
    if resumed:
        conn.commit()
    return resumed


_SWEEPER_INTERVAL_SECONDS = 60.0
_sweeper_task: asyncio.Task | None = None


async def _stale_lease_sweeper(interval_seconds: float = _SWEEPER_INTERVAL_SECONDS) -> None:
    """周期性回收卡死的媒体 job 的过期 lease。

    worker 进程被 kill -9、容器 OOM、协程异常退出等情况会让 job 卡在
    status='running' 且 lease_expires_at<now；recoverable_jobs() 只在启动时扫一次，
    启动后过期的 lease 不会被自动回收。本协程每 interval_seconds 秒扫一次，
    把过期 lease 的 job 复位回 queued，交给持久调度器在下一轮重新发现。

    幂等：多次扫到同一 job 时，第二次 CAS 会因 status 已是 'queued' 而 rowcount=0，
    不会重复恢复。"""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                conn = get_conn()
                stamp = now()
                rows = rows_to_dicts(conn.execute(
                    """SELECT id, run_id, step_run_id FROM jobs
                       WHERE status='running'
                         AND lease_expires_at IS NOT NULL
                         AND lease_expires_at < ?
                         AND cancellation_requested=0
                         AND abandoned=0""",
                    (stamp,),
                ))
                if not rows:
                    continue
                resumed = 0
                for r in rows:
                    if _recover_one_media_job(
                        conn, r["id"], r["run_id"], r["step_run_id"],
                        "lease 过期，自动回收并重新入队",
                    ):
                        resumed += 1
                if resumed:
                    conn.commit()
            except Exception:  # noqa: BLE001 周期任务不能死
                pass
    except asyncio.CancelledError:
        return


def start_stale_lease_sweeper(interval_seconds: float = _SWEEPER_INTERVAL_SECONDS) -> None:
    """启动周期 lease 回收协程；多次调用幂等（已有任务在跑则不重启）。
    覆盖 worker 崩溃/OOM 等非服务重启场景下的中断恢复需求。"""
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _sweeper_task = loop.create_task(_stale_lease_sweeper(interval_seconds))
    _retry_tasks.add(_sweeper_task)
    _sweeper_task.add_done_callback(_retry_tasks.discard)


def ensure_workers(n: int) -> None:
    """把常驻生成 worker 池扩容或缩容到目标 n，并同步独立轮询 worker。"""
    global _worker_target, _poll_worker_target
    n = max(0, int(n))
    _worker_target = n
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    alive = [t for t in _workers if not t.done()]
    _workers.clear()
    _workers.extend(alive)
    while len(_workers) < n:
        _workers.append(loop.create_task(_worker_loop(f"w{len(_workers)}")))
    while len(_workers) > n:
        task = _workers.pop()
        task.cancel()

    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.concurrency import channel_limit

    poll_n = channel_limit(media_stages.RESOURCE_VIDEO_POLL)
    _poll_worker_target = poll_n
    alive_poll = [t for t in _poll_workers if not t.done()]
    _poll_workers.clear()
    _poll_workers.extend(alive_poll)
    while len(_poll_workers) < poll_n:
        index = len(_poll_workers)
        _poll_workers.append(loop.create_task(_worker_loop(f"poll{index}", _poll_queue)))
    while len(_poll_workers) > poll_n:
        task = _poll_workers.pop()
        task.cancel()


async def stop() -> None:
    """优雅停机：取消常驻 worker 循环。否则 uvicorn --reload/退出时会卡在
    'Waiting for connections to close'——常驻 while-True 任务不退出，停机就挂起。"""
    global _sweeper_task, _dispatcher_task, _worker_target, _poll_worker_target
    try:
        from app.media_pipeline.bootstrap import stop_media_pipeline
        await stop_media_pipeline()
    except Exception:  # noqa: BLE001
        pass
    if _sweeper_task is not None:
        _sweeper_task.cancel()
    if _dispatcher_task is not None:
        _dispatcher_task.cancel()
        try:
            await _dispatcher_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _dispatcher_task = None
    for t in _retry_tasks:
        t.cancel()
    if _retry_tasks:
        await asyncio.gather(*tuple(_retry_tasks), return_exceptions=True)
    _retry_tasks.clear()
    for t in _workers:
        t.cancel()
    for t in _poll_workers:
        t.cancel()
    for t in (*_workers, *_poll_workers):
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _workers.clear()
    _poll_workers.clear()
    _worker_target = 0
    _poll_worker_target = 0
    _drain_memory_queue(_queue)
    _drain_memory_queue(_poll_queue)


def retry_paused(episode_id: str) -> int:
    """成本上限调高后，恢复因预算暂停的任务。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, reserved_cost_cny, kind FROM jobs WHERE episode_id=? AND status='paused_budget'",
        (episode_id,),
    ).fetchall()
    resumed = 0
    for r in rows:
        estimate = float(r["reserved_cost_cny"] or 0)
        if estimate <= 0:
            estimate = config.IMAGE_PRICE_PER_UNIT if r["kind"] == "scene" else 1.0
        if media_scheduler.reserve_budget(
            r["id"], episode_id, estimate,
            float(get_setting("episode_cost_limit_cny") or 100), conn=conn,
        ):
            conn.execute(
                "UPDATE jobs SET status='queued', error=NULL, next_retry_at=NULL, updated_at=? WHERE id=?",
                (now(), r["id"]),
            )
            conn.commit()
            _enqueue_for_current_status(r["id"])
            resumed += 1
    return resumed


# ---------- 成片台：汇总状态 / 拼接 / 导出 ----------

def episode_mix_status(episode_id: str) -> dict:
    """返回：每镜是否已有成片（采用版），以及整体状态。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return {"ready": False, "shots_total": 0, "shots_ready": 0, "shots": []}
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    ready = 0
    out = []
    for s in shots:
        vid = None
        if s["adopted_version_id"]:
            v = conn.execute(
                "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
                (s["adopted_version_id"],)).fetchone()
            if v and v["video_path"]:
                from app.config import PROJECTS_DIR
                rel_path = Path(v["video_path"]).relative_to(PROJECTS_DIR).as_posix()
                vid = f"/media/{rel_path}"
                ready += 1
        out.append({"shot_id": s["id"], "shot_no": s["shot_no"],
                    "duration_s": s["duration_s"], "video_url": vid,
                    "has_adopted": bool(vid)})
    return {
        "episode_id": ep["id"],
        "title": ep["title"],
        "episode_no": ep["episode_no"],
        "shots_total": len(shots),
        "shots_ready": ready,
        "ready": len(shots) > 0 and ready == len(shots),
        "final_video_url": _existing_final_url(ep),
        "shots": out,
    }


def _existing_final_url(ep_row) -> str | None:
    from app.config import PROJECTS_DIR
    final_path = _final_video_path(ep_row["project_id"], ep_row["episode_no"])
    if final_path.exists():
        rel_path = final_path.relative_to(PROJECTS_DIR).as_posix()
        return f"/media/{rel_path}"
    return None


def _final_video_path(project_id: str, episode_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final"
    d.mkdir(parents=True, exist_ok=True)
    return d / "episode.mp4"


def concatenate_episode(episode_id: str) -> dict:
    """把本集所有已采用的镜头顺序拼接成一个 MP4。
    返回 {video_url, shots, total_duration_s}。若系统未装 ffmpeg 则返回占位说明。
    """
    from pathlib import Path as _P
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError("剧集不存在")
    pieces = _adopted_video_paths(episode_id)
    if not pieces:
        raise ValueError("本集没有任何已采用的视频片段，先生成/采用后再试")

    # 各镜时长 5~10s 不一，拿不到实测时长时用分镜 duration_s 之和兜底。
    est_total_dur = conn.execute(
        """SELECT COALESCE(SUM(s.duration_s), 0) AS d
           FROM shots s WHERE s.episode_id=? AND s.adopted_version_id IS NOT NULL""",
        (episode_id,)).fetchone()["d"]

    final_path = _final_video_path(ep["project_id"], ep["episode_no"])
    if not shutil.which("ffmpeg"):
        # 缺 ffmpeg 的保底：回传首个片段 URL，前端提示用户安装 ffmpeg
        first = next(p for p in pieces if p[1])
        from app.config import PROJECTS_DIR
        rel_path = Path(first[1]).relative_to(PROJECTS_DIR).as_posix()
        return {
            "video_url": f"/media/{rel_path}",
            "shots": len(pieces),
            "total_duration_s": est_total_dur or config.DEFAULT_VIDEO_DURATION_S * len(pieces),
            "ffmpeg_missing": True,
            "note": "服务端缺少 ffmpeg，已临时回退为首个片段的直链；请安装 ffmpeg 后重新合成",
        }

    # 用 concat demuxer 优先无重编码直粘（画质无损）；但 -c copy 要求各片段编码参数
    # （像素格式/timebase/SAR/profile）完全一致，否则会失败或花屏。一旦失败，回退重编码兜底。
    with tempfile.TemporaryDirectory() as td:
        listfile = _P(td) / "list.txt"
        lines = []
        for _, vpath in pieces:
            # concat demuxer 要求绝对路径并转义单引号
            safe = vpath.replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        listfile.write_text("\n".join(lines), encoding="utf-8")
        silent_video = _P(td) / "concat.mp4"
        concat_in = ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(listfile)]
        try:
            subprocess.run(
                concat_in + ["-c", "copy", "-movflags", "+faststart", str(silent_video)],
                check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # 片段编码参数不一致导致 -c copy 失败 → 重编码兜底（画质损失极小，但保证能拼成整集）
            subprocess.run(
                concat_in + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(silent_video)],
                check=True, capture_output=True)
        atomic_copy(silent_video, final_path)

    total_dur = 0
    try:
        for _, vpath in pieces:
            raw = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", vpath], capture_output=True, text=True, check=True
            ).stdout.strip()
            total_dur += float(raw) if raw else 0
    except (subprocess.CalledProcessError, ValueError):
        total_dur = est_total_dur or config.DEFAULT_VIDEO_DURATION_S * len(pieces)

    from app.config import PROJECTS_DIR
    rel_path = final_path.relative_to(PROJECTS_DIR).as_posix()
    return {
        "video_url": f"/media/{rel_path}",
        "shots": len(pieces),
        "total_duration_s": round(total_dur, 1),
        "ffmpeg_missing": False,
    }
