from __future__ import annotations

try:
    _queue
except NameError:  # pragma: no cover - used when importing this module directly
    from app.media_exec.common import *

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
                  SET status=?, error=NULL, lease_owner=NULL, lease_expires_at=NULL,
                      next_retry_at=?, updated_at=?
                WHERE id=? AND status='paused' AND cancellation_requested=0 AND abandoned=0""",
            (next_status, retry_at, now(), row["id"]),
        )
        if cursor.rowcount != 1:
            continue
        if row["version_id"]:
            conn.execute(
                "UPDATE shot_versions SET status='queued', error=NULL WHERE id=? AND status='paused'",
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


def _append_reference_notes_from_dicts(prompt_text: str, refs: list[dict]) -> str:
    # Reused galleries must describe the exact provider pack, not every image
    # retained for keyframe generation/QA.
    packed_refs = video_modes.pack_reference_images_for_seedance(
        _usable_reference_dicts({"reference_images": refs}),
    )
    return video_modes.append_reference_prompt_notes_from_dicts(
        prompt_text,
        packed_refs,
    )


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


def _begin_video_preflight_job(
    shot_id: str,
    *,
    supervisor_run_id: str | None,
) -> str:
    """先持久化轻量任务，再执行可能失败的输入校验。

    version_id 为空表示尚未进入付费媒体阶段；durable dispatcher 不会消费
    waiting_retry，因此进程中断也不会把半成品任务提交给供应商。
    """
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage

    conn = get_conn()
    shot = conn.execute(
        """SELECT s.id, s.episode_id, e.project_id
           FROM shots s JOIN episodes e ON e.id=s.episode_id
           WHERE s.id=?""",
        (shot_id,),
    ).fetchone()
    if not shot:
        raise ValueError(f"镜头不存在：{shot_id}")
    existing = conn.execute(
        """SELECT id FROM jobs
           WHERE shot_id=? AND kind='video' AND version_id IS NULL
             AND status IN ('waiting_retry','waiting_human')
             AND cancellation_requested=0 AND abandoned=0
           ORDER BY created_at DESC LIMIT 1""",
        (shot_id,),
    ).fetchone()
    job_id = existing["id"] if existing else new_id("job")
    retry_at = now() + float(config.VIDEO_PREFLIGHT_VALIDATION_TIMEOUT)
    if existing:
        conn.execute(
            """UPDATE jobs
               SET status='waiting_retry', error=NULL, next_retry_at=?,
                   owner_run_id=COALESCE(?, owner_run_id),
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=?""",
            (retry_at, supervisor_run_id, now(), job_id),
        )
    else:
        conn.execute(
            """INSERT INTO jobs(
                   id, kind, shot_id, episode_id, project_id, status,
                   owner_run_id, next_retry_at, created_at, updated_at
               ) VALUES(?, 'video', ?, ?, ?, 'waiting_retry', ?, ?, ?, ?)""",
            (
                job_id, shot_id, shot["episode_id"], shot["project_id"],
                supervisor_run_id, retry_at, now(), now(),
            ),
        )
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
    return job_id


def _preflight_failure_is_retryable(exc: Exception) -> bool:
    import sqlite3

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        return True
    return bool(getattr(exc, "retryable", False))


def _mark_video_preflight_failure(job_id: str, exc: Exception) -> dict[str, Any]:
    """把未创建 version 的校验失败保留为可重试或显式阻塞任务。"""
    from app.media_pipeline import stages as media_stages
    from app.media_pipeline.stage_state import set_pipeline_stage
    from app.observability.metrics import inc

    conn = get_conn()
    row = conn.execute(
        "SELECT version_id, retry_count, max_retries, episode_id, shot_id FROM jobs WHERE id=?",
        (job_id,),
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
               WHERE id=? AND version_id IS NULL""",
            (reason, attempt, next_retry_at, now(), job_id),
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
               WHERE id=? AND version_id IS NULL""",
            (reason, now(), job_id),
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


def _close_reused_preflight_job(job_id: str) -> None:
    """幂等复用旧版本时关闭仅用于校验的空壳任务。"""
    conn = get_conn()
    conn.execute(
        """UPDATE jobs SET status='succeeded', error='输入未变化，已复用已有版本',
                  next_retry_at=NULL, stage_status='complete', updated_at=?
           WHERE id=? AND version_id IS NULL""",
        (now(), job_id),
    )
    conn.commit()


def _resume_reused_paused_job(
    version_id: str,
    *,
    supervisor_run_id: str | None,
    dependency_snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Transfer an exact paused attempt to the current completion Supervisor."""
    if not supervisor_run_id:
        return None
    conn = get_conn()
    row = conn.execute(
        """SELECT j.*, v.status AS version_status, v.provider_task_id,
                  v.image_inputs, e.active_video_run_id, e.video_completion_mode
             FROM jobs j
             JOIN shot_versions v ON v.id=j.version_id
             JOIN episodes e ON e.id=j.episode_id
            WHERE j.version_id=? AND j.kind='video' AND j.status='paused'
              AND j.cancellation_requested=0 AND j.abandoned=0
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
    current_qualification = str(
        (dependency_snapshot or {}).get("qualification_version") or ""
    )
    if expected_qualification != current_qualification:
        raise ValueError(
            "[REVIEW_DEPENDENCY_STALE] 暂停任务绑定的发布依赖已变化，禁止直接恢复"
        )

    reservation = conn.execute(
        "SELECT status FROM budget_reservations WHERE job_id=?",
        (row["id"],),
    ).fetchone()
    if (
        reservation is None
        or reservation["status"] not in {"reserved", "running"}
    ):
        raise ValueError(
            "[VIDEO_BUDGET_RESERVATION_REQUIRED] 暂停任务缺少有效预算预留，禁止恢复"
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

    next_status = "waiting_provider" if provider_task_id else "queued"
    updated = conn.execute(
        """UPDATE jobs
              SET status=?, error=NULL, owner_run_id=?,
                  provider_create_state=?, provider_non_cancellable=?,
                  provider_submitted_at=?, lease_owner=NULL,
                  lease_expires_at=NULL, next_retry_at=NULL, updated_at=?
            WHERE id=? AND status='paused'
              AND cancellation_requested=0 AND abandoned=0""",
        (
            next_status,
            supervisor_run_id,
            "accepted" if provider_task_id else "not_started",
            int(bool(provider_task_id)),
            submitted_at,
            now(),
            row["id"],
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
        "UPDATE shot_versions SET status='queued', error=NULL WHERE id=? AND status='paused'",
        (version_id,),
    )
    conn.execute(
        "UPDATE budget_reservations SET status='reserved' WHERE job_id=? AND status='running'",
        (row["id"],),
    )
    conn.commit()
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
    if episode is None or episode["status"] not in {
        "confirmed", "generating", "done", "mixed",
    }:
        raise ValueError(
            "[STORYBOARD_CONFIRMATION_REQUIRED] 分镜尚未人工确认，"
            "不能创建视频预检或付费任务"
        )
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
                 critique_sources: list[dict[str, Any]] | None = None) -> dict:
    """持久化校验状态；不从错误文案推断或改写分镜数据。"""
    _debug_enqueue_started = time.monotonic()
    # #region debug-point H:enqueue-boundaries
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"H","location":"app/media_exec/enqueue.py:enqueue_shot","msg":"[DEBUG] enqueue entry","data":{"shot_id":shot_id,"db_in_transaction":get_conn().in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    authority_context = _assert_enqueue_storyboard_authority(shot_id)
    if (
        authority_context.narrative_authority_required
        and (prompt_override or "").strip()
    ):
        raise ValueError(
            "[NARRATIVE_PROMPT_OVERRIDE_REQUIRES_CANDIDATE] 叙事权威镜头不允许用"
            "自由文本覆盖已发布的分镜语义；请通过受控分镜候选修订后重新发布"
        )
    preflight_job_id = _begin_video_preflight_job(
        shot_id, supervisor_run_id=supervisor_run_id,
    )
    # #region debug-point H:enqueue-boundaries
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"H","location":"app/media_exec/enqueue.py:enqueue_shot","msg":"[DEBUG] preflight persisted","data":{"shot_id":shot_id,"elapsed_ms":round((time.monotonic()-_debug_enqueue_started)*1000,1),"db_in_transaction":get_conn().in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
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
            preflight_job_id=preflight_job_id,
        )
    except Exception as exc:
        failure = _mark_video_preflight_failure(preflight_job_id, exc)
        if not failure.get("retry_scheduled"):
            raise
        result = {
            "reused": False,
            "job_id": preflight_job_id,
            "task_accepted": True,
            **failure,
        }
    if result.get("reused"):
        _close_reused_preflight_job(preflight_job_id)
    return result


def _enqueue_shot_impl(shot_id: str, *, prompt_override: str | None = None,
                       extra_negative: list[str] | None = None, reroll: bool = False,
                       critique: list[str] | None = None, after_shot_id: str | None = None,
                       auto_retake_count: int = 0,
                       supervisor_run_id: str | None = None,
                       dependency_snapshot: dict[str, Any] | None = None,
                       critique_sources: list[dict[str, Any]] | None = None,
                       preflight_job_id: str | None = None,
                       preflight_repair: dict[str, Any] | None = None) -> dict:
    """为镜头创建参考图模式视频版本并入队。
    critique：上一版 AI 评语问题，作为本次必须改正项写入 prompt。
    幂等：相同 idem_key 的成功版本直接复用（reroll 时跳过复用）。"""
    _debug_impl_started = time.monotonic()
    from app.compiler import (
        CompileError,
        VIDEO_PROMPT_CONTRACT_VERSION,
        compile_prompt,
    )
    from app.video_prompt_ai import AI_VIDEO_PROMPT_CONTRACT_VERSION
    from app.continuity import (
        derive_continuity_mode,
        effective_state_out,
        prompt_source_provenance_errors,
        preflight_seedance_gates,
        resolve_first_last_boundary_relation,
        resolve_do_not_repeat_texts,
        shot_contract_dict,
        uses_previous_tail_frame,
    )
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
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise ValueError(f"镜头不存在：{shot_id}")
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot_row["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if not bool(_row_value(project, "harness_engine_enabled", 1)):
        raise ValueError("该项目的 Harness Engine 已由灰度开关隔离")
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise ValueError("分镜脚本未确认，不能生成视频（PRD 原则 P3：贵的环节前人工把关）")

    from app.domain.common import _project_bible_or_placeholder

    bible = _project_bible_or_placeholder(project)
    # Compile the paid video request from the accepted per-episode portrait
    # revision, not from a possibly older project-Bible appearance string.
    # The worker already resolves this view for keyframes; enqueue must use the
    # same source or the frozen video prompt can disagree with its reference pack.
    from app.portraits import bible_for_episode
    bible = bible_for_episode(ep["project_id"], bible, ep["episode_no"])
    shot = _load_shot_model(shot_row)
    screenplay = authority_context.screenplay
    prior_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? AND shot_no<? ORDER BY shot_no",
        (shot_row["episode_id"], int(shot_row["shot_no"])),
    ).fetchall()
    prior_shots = [_load_shot_model(row) for row in prior_rows]
    # Persisted legacy boards may contain snake_case ledger IDs. Resolve them
    # to Chinese semantics at the final model boundary; unresolved IDs vanish.
    shot.do_not_repeat = resolve_do_not_repeat_texts(shot, screenplay, prior_shots)
    shot_plan = None
    if authority_context.narrative_authority_required:
        from app.video_plan import get_shot_plan

        shot_plan = get_shot_plan(shot_id, conn=conn)
        if shot_plan is None:
            raise ValueError(
                "[VIDEO_PLAN_REQUIRED] 叙事镜头必须绑定当前已验证的 "
                "EpisodeVideoGenerationPlan，禁止默认回退参考图模式"
            )
        decision = video_modes.dict_to_decision(
            shot_plan.model_dump(mode="json")
        )
    else:
        decision = (
            _decision_from_mode_plan(shot_row)
            or video_modes.default_reference_decision()
        )
        if decision.mode != video_modes.REFERENCE_IMAGE_MODE:
            decision = video_modes.default_reference_decision()

    first_frame_requirement = None
    first_frame_source = None
    boundary_source_shot_id = None
    if shot_plan is not None and shot_plan.mode.value in {
        video_modes.FIRST_FRAME_MODE,
        video_modes.FIRST_LAST_FRAME_MODE,
    }:
        first_frame_requirement = next(
            (
                item
                for item in shot_plan.required_assets
                if item.role == "first_frame"
            ),
            None,
        )
        if first_frame_requirement is not None:
            first_frame_source = first_frame_requirement.source.value
            boundary_source_shot_id = first_frame_requirement.source_shot_id

    # 跨镜连贯只继承上一镜的实际/计划尾状态；不得把上一镜完整动作描述塞进 prompt。
    # after_shot_id 无效时回退到 shot_no-1，避免 action_continuation 在缺 prev 时被当成链首误杀。
    prev_row = None
    planned_dependency_id = (
        str(shot_plan.depends_on_shot_id)
        if shot_plan is not None and shot_plan.depends_on_shot_id
        else None
    )
    if (
        shot_plan is not None
        and after_shot_id is not None
        and after_shot_id != planned_dependency_id
    ):
        raise ValueError("请求的前序镜头与已发布视频计划不一致")
    dependency_id = planned_dependency_id if shot_plan is not None else after_shot_id
    if dependency_id:
        prev_row = conn.execute(
            "SELECT * FROM shots WHERE id=? AND episode_id=?",
            (dependency_id, shot_row["episode_id"]),
        ).fetchone()
        if prev_row is None and shot_plan is not None:
            raise ValueError("视频计划引用的前序镜头不存在或不属于本集")
    if prev_row is None and shot_plan is None and int(shot_row["shot_no"]) > 1:
        prev_row = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? AND shot_no<? ORDER BY shot_no DESC LIMIT 1",
            (shot_row["episode_id"], int(shot_row["shot_no"])),
        ).fetchone()
    boundary_prev_row = None
    if boundary_source_shot_id:
        boundary_prev_row = conn.execute(
            "SELECT * FROM shots WHERE id=? AND episode_id=?",
            (boundary_source_shot_id, shot_row["episode_id"]),
        ).fetchone()
        if boundary_prev_row is None:
            raise ValueError("视频计划的首帧来源镜头不存在或不属于本集")
    continuity_prev_row = boundary_prev_row or prev_row
    prev_shot = (
        _load_shot_model(continuity_prev_row)
        if continuity_prev_row is not None
        else None
    )
    sequence_prev_row = None
    if int(shot_row["shot_no"]) > 1:
        sequence_prev_row = conn.execute(
            """SELECT * FROM shots
               WHERE episode_id=? AND shot_no<?
               ORDER BY shot_no DESC LIMIT 1""",
            (shot_row["episode_id"], int(shot_row["shot_no"])),
        ).fetchone()
    prompt_context_row = continuity_prev_row or sequence_prev_row
    previous_prompt_version = None
    if prompt_context_row is not None:
        adopted_version_id = _row_value(
            prompt_context_row,
            "adopted_version_id",
        )
        if adopted_version_id:
            previous_prompt_version = conn.execute(
                """SELECT id,prompt_text FROM shot_versions
                   WHERE id=? AND shot_id=?""",
                (adopted_version_id, prompt_context_row["id"]),
            ).fetchone()
        if previous_prompt_version is None:
            previous_prompt_version = conn.execute(
                """SELECT id,prompt_text FROM shot_versions
                   WHERE shot_id=? AND prompt_text IS NOT NULL
                   ORDER BY version_no DESC LIMIT 1""",
                (prompt_context_row["id"],),
            ).fetchone()
    previous_prompt_text = (
        str(previous_prompt_version["prompt_text"] or "")
        if previous_prompt_version is not None
        else ""
    )
    previous_prompt_fingerprint = (
        hashlib.sha256(previous_prompt_text.encode("utf-8")).hexdigest()
        if previous_prompt_text
        else ""
    )
    continuity_mode = derive_continuity_mode(shot, prev_shot)
    shot.continuity_mode = continuity_mode
    shot.continuity_from_prev = uses_previous_tail_frame(continuity_mode)
    if continuity_mode != "scene_change":
        shot.transition = "硬切"
    boundary_relation_edit = (
        shot_plan.relations.edit if shot_plan is not None else None
    )
    boundary_relation_action = (
        shot_plan.relations.action if shot_plan is not None else None
    )
    boundary_relation_reason = "planned_relation"
    if first_frame_requirement is not None:
        (
            boundary_relation_edit,
            boundary_relation_action,
            boundary_relation_reason,
        ) = resolve_first_last_boundary_relation(
            shot,
            prev_shot,
            planned_edit=boundary_relation_edit,
            planned_action=boundary_relation_action,
        )
    prev_state_out = effective_state_out(prev_shot) if prev_shot else None
    boundary_start_state = None
    if boundary_prev_row is not None and prev_shot is not None:
        if first_frame_source == "PREVIOUS_STATIC_TAIL":
            boundary_start_state = (
                (prev_shot.last_frame_desc or "").strip()
                or prev_state_out
            )
        else:
            boundary_start_state = prev_state_out
    planned_state_dependency = (
        shot_plan is not None and shot_plan.state_dependency != "none"
    )
    prompt_prev_state_out = (
        prev_state_out
        if planned_state_dependency or uses_previous_tail_frame(continuity_mode)
        else None
    )
    if prompt_prev_state_out:
        shot.state_in = prompt_prev_state_out
    chain_after_shot_id = (
        planned_dependency_id
        if shot_plan is not None
        else (
            (prev_row["id"] if prev_row else None)
            if uses_previous_tail_frame(continuity_mode) else None
        )
    )
    chain_after_version_id = (
        _row_value(prev_row, "adopted_version_id")
        if chain_after_shot_id else None
    )

    outgoing_transition = _outgoing_transition_context(conn, shot_row)
    incoming_transition = None
    if int(shot_row["shot_no"]) > 1 and not uses_previous_tail_frame(continuity_mode):
        incoming_transition = _transition_value(shot_row)
        if incoming_transition == "硬切":
            incoming_transition = None

    preflight_errors = preflight_seedance_gates(
        shot,
        prev=prev_shot,
        prompt_text=None,
        screenplay=screenplay,
    )
    if preflight_errors:
        raise CompileError("；".join(preflight_errors))

    # #region debug-point I:prompt-compile
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"I","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] compile prompt start","data":{"shot_id":shot_id,"elapsed_ms":round((time.monotonic()-_debug_impl_started)*1000,1),"db_in_transaction":get_conn().in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    raw_prompt_text = compile_prompt(
        shot,
        bible,
        extra_negative,
        with_refs=True,
        from_scene=False,
        chained=bool(chain_after_shot_id),
        critique=critique,
        prev_tail_action=None,
        with_last_frame=False,
        incoming_transition=incoming_transition,
        outgoing_transition=(
            outgoing_transition["transition"]
            if outgoing_transition else None
        ),
        next_scene=(
            outgoing_transition["next_scene"]
            if outgoing_transition else None
        ),
        next_first_frame_desc=(
            outgoing_transition["next_first_frame_desc"]
            if outgoing_transition else None
        ),
        continuity_mode=continuity_mode,
        prev_state_out=prompt_prev_state_out,
        voice_bible=screenplay.voice_bible,
        screenplay=screenplay,
        video_generation_mode=(
            shot_plan.mode.value if shot_plan is not None else decision.mode
        ),
        first_frame_source=first_frame_source,
        boundary_relation_edit=boundary_relation_edit,
        boundary_relation_action=boundary_relation_action,
        boundary_start_state=boundary_start_state,
        previous_prompt_text=previous_prompt_text,
    )
    # #region debug-point I:prompt-compile
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"I","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] compile prompt end","data":{"shot_id":shot_id,"elapsed_ms":round((time.monotonic()-_debug_impl_started)*1000,1),"db_in_transaction":get_conn().in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    raw_source_errors = prompt_source_provenance_errors(raw_prompt_text, shot)
    prompt_text = ensure_source_excerpt_in_prompt(raw_prompt_text, shot)
    if raw_source_errors:
        prompt_scrub = {
            "repair": "source_excerpt_prompt_scrub",
            "matched_rules": raw_source_errors,
        }
        if preflight_repair:
            preflight_repair = {
                **preflight_repair,
                "prompt_scrubbed": True,
                "prompt_scrub_rules": raw_source_errors,
            }
        else:
            preflight_repair = prompt_scrub
    preflight_errors = preflight_seedance_gates(
        shot,
        prev=prev_shot,
        prompt_text=prompt_text,
        screenplay=screenplay,
    )
    if preflight_errors:
        source_errors = prompt_source_provenance_errors(prompt_text, shot)
        raise CompileError(
            "；".join(preflight_errors),
            retryable=bool(source_errors),
            failure_kind="prompt_source_provenance" if source_errors else None,
        )

    # 参考图是分镜级素材。重抽、改词或带评语只创建新视频版本，不能重新跑参考图生成。
    reference_gallery = _load_reference_gallery(conn, shot_row)
    from app.multiview import manifest_revisions_match, resolve_shot_asset_dependencies

    current_reference_manifest = resolve_shot_asset_dependencies(
        project_id=ep["project_id"],
        episode_no=ep["episode_no"],
        shot_id=shot_id,
        shot=shot,
        scene_name=getattr(shot, "scene_name", "") or None,
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    if (
        reference_gallery
        and isinstance(reference_gallery.get("reference_manifest"), dict)
        and not manifest_revisions_match(
            reference_gallery["reference_manifest"], current_reference_manifest,
        )
    ):
        # A new portrait/scene revision invalidates the copied shot gallery even
        # when a user previously edited that gallery. The worker rechecks this
        # before provider submission; doing it here also prevents idempotency
        # from returning an old succeeded video before a new job is created.
        reference_gallery = None

    key_material = (
        prompt_text
        + f"|mode:{decision.mode}|plan:{video_modes.decision_to_dict(decision)}"
        + f"|after:{chain_after_shot_id or ''}"
        + f"|after_version:{chain_after_version_id or ''}"
        + f"|keyframe_prompt_contract:{video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION}"
        + f"|video_prompt_contract:{VIDEO_PROMPT_CONTRACT_VERSION}"
        + f"|ai_video_prompt_contract:{AI_VIDEO_PROMPT_CONTRACT_VERSION}"
        + f"|prompt_user_instruction:{(prompt_override or '').strip()}"
        + f"|previous_prompt:{previous_prompt_fingerprint}"
        + f"|reference_input_policy:{video_modes.REFERENCE_INPUT_POLICY_VERSION}"
        + f"|reference_dependencies:{current_reference_manifest.get('input_fingerprint') or ''}"
    )
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
            "'waiting_retry','waiting_human','paused_budget','paused') "
            "ORDER BY CASE status WHEN 'succeeded' THEN 0 ELSE 1 END, version_no DESC "
            "LIMIT 1",
            (shot_id, key)).fetchone()
        if existing:
            result = {"reused": True, "version_id": existing["id"]}
            if existing["status"] == "paused":
                resumed = _resume_reused_paused_job(
                    existing["id"],
                    supervisor_run_id=supervisor_run_id,
                    dependency_snapshot=dependency_snapshot,
                )
                if resumed:
                    result.update(resumed)
            if preflight_repair:
                result["preflight_repair"] = preflight_repair
            return result

    version_no = (conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS m FROM shot_versions WHERE shot_id=?",
        (shot_id,)).fetchone()["m"]) + 1
    version_id = new_id("ver")
    image_meta = {
        "mode": decision.mode,
        "mode_decision": video_modes.decision_to_dict(decision),
        "after_shot_id": chain_after_shot_id,
        "after_version_id": chain_after_version_id,
        "after_shot_no": None,
        "continuity_mode": continuity_mode,
        "prev_state_out": prompt_prev_state_out,
        "incoming_transition": incoming_transition,
        "outgoing_transition": outgoing_transition,
        "auto_retake_count": max(0, int(auto_retake_count)),
        "supervisor_run_id": supervisor_run_id,
        "shot_contract_json": json.dumps(shot_contract_dict(shot), ensure_ascii=False),
        "video_prompt_contract_version": VIDEO_PROMPT_CONTRACT_VERSION,
        "ai_video_prompt_required": True,
        "ai_video_prompt_contract_target": AI_VIDEO_PROMPT_CONTRACT_VERSION,
        "continuity_contract_prompt": prompt_text,
        "prompt_user_instruction": (prompt_override or "").strip(),
        "prompt_critique": [
            str(item).strip()
            for item in (critique or [])
            if str(item).strip()
        ],
        "previous_prompt_version_id": (
            previous_prompt_version["id"]
            if previous_prompt_version is not None
            else None
        ),
        "previous_prompt_fingerprint": previous_prompt_fingerprint or None,
        "previous_prompt_inherited": bool(
            previous_prompt_text and not prompt_override
        ),
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "reference_input_policy_version": video_modes.REFERENCE_INPUT_POLICY_VERSION,
        "boundary_prompt_contract": {
            "video_generation_mode": (
                shot_plan.mode.value if shot_plan is not None else decision.mode
            ),
            "first_frame_source": first_frame_source,
            "source_shot_id": boundary_source_shot_id,
            "relation_edit": boundary_relation_edit,
            "relation_action": boundary_relation_action,
            "relation_normalization_reason": boundary_relation_reason,
            "start_state": boundary_start_state,
        },
    }
    if shot_plan is not None:
        image_meta.update({
            "episode_video_plan_id": shot_plan.episode_video_plan_id,
            "shot_plan_id": shot_plan.shot_plan_id,
            "plan_revision": shot_plan.plan_revision,
            "source_storyboard_revision_id": shot_plan.source_storyboard_revision_id,
            "capability_snapshot_id": shot_plan.capability_snapshot_id,
            "input_revision_fingerprints": dict(
                shot_plan.input_revision_fingerprints
            ),
            "planned_mode": shot_plan.mode.value,
            "actual_mode": shot_plan.mode.value,
            "video_input_intent": (
                shot_plan.video_input_intent.value
                if shot_plan.video_input_intent is not None else None
            ),
            "depends_on_shot_id": shot_plan.depends_on_shot_id,
        })
    if preflight_repair:
        image_meta["preflight_auto_repair"] = preflight_repair
    if dependency_snapshot:
        # This immutable token is checked again by the worker before every
        # candidate/QA/adoption write.  Keeping it on the version also makes a
        # stale provider result explainable after process restarts.
        image_meta["review_dependency_snapshot"] = {
            "qualification_version": dependency_snapshot.get("qualification_version"),
            "published_screenplay_artifact_id": dependency_snapshot.get("published_screenplay_artifact_id"),
            "confirmed_storyboard_artifact_id": dependency_snapshot.get("confirmed_storyboard_artifact_id"),
            "screenplay_revision": dependency_snapshot.get("screenplay_revision"),
            "storyboard_revision": dependency_snapshot.get("storyboard_revision"),
            "asset_status": dependency_snapshot.get("asset_status"),
            "asset_inputs": dependency_snapshot.get("asset_inputs") or [],
            "asset_soft_warnings": dependency_snapshot.get("asset_soft_warnings") or [],
            "captured_at": dependency_snapshot.get("server_time"),
        }
    if critique_sources:
        image_meta["critique_sources"] = critique_sources
    if reference_gallery:
        image_meta["reference_images"] = reference_gallery["reference_images"]
        image_meta["reference_gallery_source_version_id"] = reference_gallery["source_version_id"]
        image_meta["reference_gallery_fingerprint"] = reference_gallery["fingerprint"]
        if reference_gallery.get("keyframe_contract_fingerprint"):
            image_meta["keyframe_contract_fingerprint"] = reference_gallery["keyframe_contract_fingerprint"]
        if isinstance(reference_gallery.get("keyframe_sequence"), dict):
            image_meta["keyframe_sequence"] = reference_gallery["keyframe_sequence"]
        if isinstance(reference_gallery.get("reference_manifest"), dict):
            image_meta["reference_manifest"] = reference_gallery["reference_manifest"]
            image_meta["reference_manifest_frozen"] = True
        if reference_gallery["revision"] is not None:
            image_meta["reference_gallery_revision"] = reference_gallery["revision"]
        if reference_gallery["edited"]:
            image_meta["reference_gallery_edited"] = True
        if reference_gallery.get("contract_override"):
            image_meta["reference_gallery_contract_override"] = True
    # #region debug-point H:persistence
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"H","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] version persistence start","data":{"shot_id":shot_id,"db_in_transaction":conn.in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, created_at, image_inputs) "
        "VALUES(?,?,?,?,?, 'queued', ?, ?)",
        (version_id, shot_id, version_no, prompt_text, key, now(),
         json.dumps(image_meta, ensure_ascii=False)))
    job_id = preflight_job_id or new_id("job")
    budget_limit = episode_video_budget_limit(str(ep["id"]))
    # #region debug-point H:persistence
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"H","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] media trace start","data":{"shot_id":shot_id,"db_in_transaction":conn.in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    run_id, step_run_id = ensure_media_trace(
        workflow_type="video_generation", scope_id=shot_id,
        input_value={"prompt": prompt_text, "version": version_no}, budget_limit_cny=budget_limit,
    )
    # #region debug-point H:persistence
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"H","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] media trace end","data":{"shot_id":shot_id,"db_in_transaction":conn.in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    if preflight_job_id:
        updated = conn.execute(
            """UPDATE jobs
               SET version_id=?, episode_id=?, project_id=?, status='queued',
                   error=NULL, next_retry_at=NULL, retry_count=0,
                   reason_code=NULL, reason_text=NULL, stage_progress_json=NULL,
                   after_shot_id=?, after_version_id=?, run_id=?,
                   owner_run_id=?, step_run_id=?,
                   lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND shot_id=? AND kind='video' AND version_id IS NULL""",
            (
                version_id, ep["id"], project["id"], chain_after_shot_id,
                chain_after_version_id, run_id, supervisor_run_id, step_run_id,
                now(), job_id, shot_id,
            ),
        )
        if updated.rowcount != 1:
            conn.rollback()
            raise ValueError("视频输入校验任务状态已变化，请刷新后重试")
    else:
        conn.execute(
            "INSERT INTO jobs(id, kind, shot_id, version_id, episode_id, project_id, status, created_at, "
            "updated_at, after_shot_id, after_version_id, run_id, owner_run_id, step_run_id) "
            "VALUES(?, 'video', ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id, shot_id, version_id, ep["id"], project["id"], now(), now(),
                chain_after_shot_id, chain_after_version_id, run_id,
                supervisor_run_id, step_run_id,
            ))
    try:
        from app.media_pipeline import stages as media_stages
        from app.media_pipeline.stage_state import set_pipeline_stage
        set_pipeline_stage(job_id, media_stages.STAGE_JOB_QUEUED, conn=conn)
    except Exception:  # noqa: BLE001
        pass
    conn.execute("UPDATE episodes SET status='generating' WHERE id=? AND status='confirmed'", (ep["id"],))
    # #region debug-point H:persistence
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"H","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] version persistence commit start","data":{"shot_id":shot_id,"db_in_transaction":conn.in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    conn.commit()
    # #region debug-point H:persistence
    with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"H","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] version persistence commit end","data":{"shot_id":shot_id,"db_in_transaction":conn.in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
    # #endregion
    from app.video_cost_model import initial_shot_generation_cost

    estimate = initial_shot_generation_cost(float(shot.duration_s))
    try:
        # #region debug-point J:budget-reserve
        with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"J","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] budget reserve start","data":{"shot_id":shot_id,"db_in_transaction":conn.in_transaction},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
        # #endregion
        reserved = media_scheduler.reserve_budget(
            job_id, ep["id"], estimate, budget_limit, conn=conn
        )
        # #region debug-point J:budget-reserve
        with __import__("contextlib").suppress(Exception): __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"video-dispatch-block","runId":"post-fix","hypothesisId":"J","location":"app/media_exec/enqueue.py:_enqueue_shot_impl","msg":"[DEBUG] budget reserve end","data":{"shot_id":shot_id,"db_in_transaction":conn.in_transaction,"reserved":bool(reserved)},"ts":int(time.time()*1000)}).encode(), headers={"Content-Type":"application/json"}), timeout=0.2).read()
        # #endregion
    except Exception as exc:
        public = errors.record_and_format(
            exc,
            action="video_budget_reserve",
            context={"job_id": job_id, "shot_id": shot_id, "episode_id": ep["id"]},
        )
        message = f"视频任务未能完成预算预留，尚未提交供应商，可直接重试。{public}"
        conn.execute(
            "UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
            (message, now(), job_id),
        )
        conn.execute(
            "UPDATE shot_versions SET status='failed', error=? WHERE id=?",
            (message, version_id),
        )
        conn.commit()
        mark_media_job_state(run_id, step_run_id, "failed", message)
        reconcile_episode_generation_status(ep["id"])
        raise ValueError(message) from exc
    if not reserved:
        _set_version(version_id, status="paused_budget")
        _set_job(job_id, "paused_budget", "集预算不足，任务已暂停")
        reconcile_episode_generation_status(ep["id"])
        result = {
            "reused": False, "version_id": version_id, "job_id": job_id,
            "paused_budget": True,
        }
        if preflight_repair:
            result["preflight_repair"] = preflight_repair
        return result
    dispatch_deferred = False
    try:
        _enqueue_for_current_status(job_id)
    except Exception as exc:  # durable dispatcher continuously rebuilds queues from jobs
        errors.record_and_format(
            exc,
            action="video_initial_dispatch",
            context={"job_id": job_id, "shot_id": shot_id, "episode_id": ep["id"]},
        )
        dispatch_deferred = True
        conn.execute(
            "UPDATE jobs SET error=?, updated_at=? WHERE id=? AND status='queued'",
            (
                "任务已写入持久队列；实时调度通知暂未送达，系统将自动重新发现，无需重复点击",
                now(),
                job_id,
            ),
        )
        conn.commit()
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
