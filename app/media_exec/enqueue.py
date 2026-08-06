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
        conn.execute(
            "UPDATE shot_versions SET status='running',error=NULL,image_inputs=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), row["version_id"]),
        )
        conn.execute(
            """UPDATE jobs
                  SET status='waiting_provider',error=NULL,
                      lease_owner=NULL,lease_expires_at=NULL,next_retry_at=?,
                      provider_create_state='accepted',provider_non_cancellable=1,
                      updated_at=?
                WHERE id=? AND status='stale'""",
            (now(), now(), row["job_id"]),
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
        if not video_modes.reference_gallery_matches_keyframe_contract(meta):
            # 关键帧 prompt 合同升级后，未编辑的旧图不再默认污染新视频。
            # 文件仍保留供审计，新版本只是重新生成唯一叙事关键帧。
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
        text = str(exc).lower()
        return any(token in text for token in ("locked", "busy", "tempor", "timeout", "i/o"))
    # 内容通常是确定性的，但 source_excerpt 泄漏有本地结构迁移与最终 prompt
    # 消毒两层恢复路径。若这两层因旧数据形态未命中，仍应给升级后的代码/并发编辑
    # 至少一次持久化重试机会，不能首轮 409 就永久结束镜头。
    if "source_excerpt 原文内容" in str(exc):
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
    retryable = _preflight_failure_is_retryable(exc)
    retry_count = int(row["retry_count"] or 0)
    configured_limit = int(config.VIDEO_PREFLIGHT_MAX_RETRIES)
    max_retries = min(int(row["max_retries"] or configured_limit), configured_limit)
    if retryable and retry_count < max_retries:
        attempt = retry_count + 1
        delay = float(config.VIDEO_PREFLIGHT_RETRY_BASE_DELAY) * (2 ** retry_count)
        if "source_excerpt 原文内容" in message:
            reason = (
                f"检测到提示词原文残留，系统正在执行安全消毒并将在约 {int(delay)} 秒后"
                f"自动重试（{attempt}/{max_retries}）"
            )
        else:
            reason = (
                f"视频输入校验遇到瞬时故障，系统将在约 {int(delay)} 秒后"
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


def _canonical_functional_speaker(name: str) -> str:
    """把明确的场所前缀角色收敛为角色策略允许的通用功能标签。"""
    import re

    value = (name or "").strip()
    prefixes = ("宝阁", "店铺", "客栈", "宗门", "山门", "府中", "府内")
    if value.endswith("管事") and value[:-2] in prefixes:
        return "管事"
    # 小说常以衣着/外貌 + 临时身份 + 甲乙区分一次性角色，例如“绿袍修士乙”。
    # 这不是可跨镜追踪的真名，不能临时铸造成角色圣经实体；收敛为既有功能路人
    # 标签，衣着与职业语义仍由 action_desc / 首尾帧保留。
    match = re.fullmatch(
        r".{1,6}(?:修士|男子|女子|青年|少年|少女|老者|大汉|陌生人)(甲|乙|丙|丁)",
        value,
    )
    if match:
        return f"路人{match.group(1)}"
    return value


def _auto_normalize_functional_speaker(
    shot_id: str,
    error_text: str,
) -> dict[str, Any] | None:
    if not any(
        marker in error_text
        for marker in ("功能性路人", "角色合同残留", "角色圣经")
    ):
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT characters,dialogues,shot_contract_json FROM shots WHERE id=?",
        (shot_id,),
    ).fetchone()
    if not row:
        return None
    try:
        characters = [
            str(item).strip()
            for item in json.loads(row["characters"] or "[]")
            if str(item).strip()
        ]
        dialogues = [
            item for item in json.loads(row["dialogues"] or "[]")
            if isinstance(item, dict)
        ]
        contract = json.loads(row["shot_contract_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(contract, dict):
        contract = {}
    candidates = {
        value: _canonical_functional_speaker(value)
        for value in [
            *characters,
            *(str(item.get("speaker") or "").strip() for item in dialogues),
            *(str(item).strip() for item in (contract.get("characters_visible") or [])),
        ]
        if value
    }
    replacements = {
        old: new for old, new in candidates.items()
        if new and new != old
    }
    if len(replacements) != 1:
        return None
    old, new = next(iter(replacements.items()))
    characters = list(dict.fromkeys(replacements.get(item, item) for item in characters))
    for item in dialogues:
        speaker = str(item.get("speaker") or "").strip()
        if speaker in replacements:
            item["speaker"] = replacements[speaker]
    for key in ("characters_visible", "audio_cast"):
        contract[key] = list(dict.fromkeys(
            replacements.get(str(item).strip(), str(item).strip())
            for item in (contract.get(key) or [])
            if str(item).strip()
        ))
    timeline = []
    for item in (contract.get("audio_timeline") or []):
        if not isinstance(item, dict):
            continue
        updated = dict(item)
        speaker_id = str(updated.get("speaker_id") or "").strip()
        if speaker_id in replacements:
            updated["speaker_id"] = replacements[speaker_id]
        timeline.append(updated)
    contract["audio_timeline"] = timeline
    conn.execute(
        """UPDATE shots
           SET characters=?, dialogues=?, shot_contract_json=?
           WHERE id=?""",
        (
            json.dumps(characters, ensure_ascii=False),
            json.dumps(dialogues, ensure_ascii=False),
            json.dumps(contract, ensure_ascii=False),
            shot_id,
        ),
    )
    conn.commit()
    from app.observability.metrics import inc
    inc(
        "video_preflight_auto_repair_total",
        shot_id=shot_id,
        repair="functional_speaker_normalized",
        from_label=old,
        to_label=new,
    )
    return {
        "repair": "functional_speaker_normalized",
        "from_label": old,
        "to_label": new,
    }


def _minimum_spoken_duration(text: str, current_duration: int) -> int | None:
    import unicodedata

    chars = sum(
        1 for ch in (text or "")
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )
    start = max(int(current_duration or 0), int(config.VIDEO_DURATION_MIN_S))
    for duration in range(start, int(config.VIDEO_DURATION_MAX_S) + 1):
        if chars <= config.max_spoken_chars_for_duration(duration):
            return duration
    return None


def _auto_expand_source_dialogue_duration(
    shot_id: str,
    error_text: str,
) -> dict[str, Any] | None:
    """原文台词已结构化但当前时长不足时，扩到可容纳它的最短合法时长。"""
    if "口播上限" not in error_text:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT duration_s,source_excerpt,dialogues FROM shots WHERE id=?",
        (shot_id,),
    ).fetchone()
    if not row:
        return None
    try:
        dialogues = [
            item for item in json.loads(row["dialogues"] or "[]")
            if isinstance(item, dict) and str(item.get("line") or "").strip()
        ]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if len(dialogues) != 1:
        return None
    line = str(dialogues[0]["line"]).strip()
    excerpt = str(row["source_excerpt"] or "").strip()
    if len(line) < 24 or not excerpt or not (
        excerpt[:24] in line or line[:24] in excerpt
    ):
        return None
    current = int(row["duration_s"] or config.VIDEO_DURATION_MIN_S)
    required = _minimum_spoken_duration(line, current)
    if required is None or required <= current:
        return None
    conn.execute(
        "UPDATE shots SET duration_s=? WHERE id=?",
        (required, shot_id),
    )
    conn.commit()
    from app.observability.metrics import inc
    inc(
        "video_preflight_auto_repair_total",
        shot_id=shot_id,
        repair="source_dialogue_duration",
        from_duration=current,
        to_duration=required,
    )
    return {
        "repair": "source_dialogue_duration",
        "line": line,
        "from_duration_s": current,
        "to_duration_s": required,
    }


def _auto_repair_embedded_source_dialogue(shot_id: str, error_text: str) -> dict[str, Any] | None:
    """只修复可确定的“原文台词误塞进画面描述”结构错误。

    没有明确引号、原文连续命中或可信说话人时不改数据，避免猜测性改写。
    """
    if "source_excerpt 原文内容" not in error_text:
        return None
    import re

    conn = get_conn()
    row = conn.execute(
        """SELECT action_desc, source_excerpt, dialogues, characters, shot_contract_json,
                  duration_s
           FROM shots WHERE id=?""",
        (shot_id,),
    ).fetchone()
    if not row:
        return None
    action = str(row["action_desc"] or "")
    excerpt = str(row["source_excerpt"] or "").strip()
    if len(excerpt) < 24:
        return None
    quoted = None
    for match in re.finditer(r"[「“\"]([^」”\"]{24,})[」”\"]", action):
        line = match.group(1).strip()
        if excerpt[:24] in line or line[:24] in excerpt:
            quoted = (match, line)
            break
    if quoted is None:
        return None
    match, line = quoted
    try:
        dialogues = json.loads(row["dialogues"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        dialogues = []
    existing = next(
        (
            item for item in dialogues
            if isinstance(item, dict) and str(item.get("line") or "").strip() == line
        ),
        None,
    )
    speaker = str((existing or {}).get("speaker") or "").strip()
    characters = [
        str(item).strip()
        for item in json.loads(row["characters"] or "[]")
        if str(item).strip()
    ]
    marker_pos = max(
        action.rfind("待修复台词信息", max(0, match.start() - 40), match.start()),
        action.rfind("待修复对白", max(0, match.start() - 40), match.start()),
    )
    if not speaker:
        stripped = action.lstrip()
        leading = sorted(
            (name for name in characters if stripped.startswith(name)),
            key=len,
            reverse=True,
        )
        if leading:
            speaker = leading[0]
        else:
            prefix = action[:match.start()]
            candidates = []
            for name in characters:
                pattern = re.compile(
                    re.escape(name)
                    + r"[^，。；]{0,24}(?:开口|说道|说出|喝道|问道|回答|警告|威胁|低声|高喊)"
                )
                found = list(pattern.finditer(prefix))
                if found:
                    candidates.append((found[-1].start(), name))
            if candidates:
                speaker = max(candidates)[1]
    if not speaker and marker_pos >= 0:
        # 历史分镜可能漏写 characters，但“待修复台词信息”明确表明这是结构迁移，
        # 可从画面句首的施事者确定说话人；普通引号文本不走此推断。
        subject = re.match(
            r"\s*([\u3400-\u9fffA-Za-z0-9·]{2,12}?)(?="
            r"面色|神色|表情|收起|抬|转|直视|看向|开口|低声|高声|"
            r"说道|警告|威胁|冷笑|皱眉|站|走)",
            action,
        )
        if subject:
            speaker = subject.group(1).strip()
    if not speaker:
        return None
    speaker = _canonical_functional_speaker(speaker)

    remove_start = match.start()
    if marker_pos >= 0:
        remove_start = marker_pos
    left = action[:remove_start].rstrip(" \t：:，,；;")
    right = action[match.end():].lstrip(" \t：:，,；;")
    if right and all(
        ch.isspace() or ch in "。.!！?？；;，,"
        for ch in right
    ):
        right = ""
    repaired_action = "；".join(part for part in (left, right) if part).strip()
    repaired_action = repaired_action.rstrip("；;，,") + (
        "。" if repaired_action and repaired_action[-1] not in "。.!！?？" else ""
    )
    if not repaired_action or repaired_action == action:
        return None
    if existing is None:
        dialogues.append({
            "speaker": speaker,
            "line": line,
            "emotion": "平静",
            "delivery": "spoken_dialogue",
        })
    if speaker not in characters:
        characters.append(speaker)
    try:
        contract = json.loads(row["shot_contract_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        contract = {}
    if isinstance(contract, dict):
        visible = [
            str(item).strip()
            for item in (contract.get("characters_visible") or [])
            if str(item).strip()
        ]
        audio_cast = [
            str(item).strip()
            for item in (contract.get("audio_cast") or [])
            if str(item).strip()
        ]
        if speaker not in visible:
            visible.append(speaker)
        if speaker not in audio_cast:
            audio_cast.append(speaker)
        contract["characters_visible"] = visible
        contract["audio_cast"] = audio_cast
        # 下一次 preflight 根据刚迁移的 dialogues 确定性重建时间轴。
        contract["audio_timeline"] = []
        contract["spoken_contract_status"] = "coherent"
    current_duration = int(row["duration_s"] or config.VIDEO_DURATION_MIN_S)
    required_duration = _minimum_spoken_duration(line, current_duration)
    if required_duration is None:
        return None
    conn.execute(
        """UPDATE shots
           SET action_desc=?, dialogues=?, characters=?, shot_contract_json=?, duration_s=?
           WHERE id=?""",
        (
            repaired_action,
            json.dumps(dialogues, ensure_ascii=False),
            json.dumps(characters, ensure_ascii=False),
            json.dumps(contract, ensure_ascii=False),
            required_duration,
            shot_id,
        ),
    )
    conn.commit()
    from app.observability.metrics import inc
    inc("video_preflight_auto_repair_total", shot_id=shot_id, repair="embedded_dialogue")
    return {
        "repair": "embedded_dialogue",
        "speaker": speaker,
        "line": line,
        "action_desc": repaired_action,
        "from_duration_s": current_duration,
        "to_duration_s": required_duration,
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
    """持久化校验状态，并对可确定的结构错误执行有界修复链后重试。"""
    authority_context = _assert_enqueue_storyboard_authority(shot_id)
    narrative_authority = authority_context.narrative_authority_required
    if narrative_authority and (prompt_override or "").strip():
        raise ValueError(
            "[NARRATIVE_PROMPT_OVERRIDE_REQUIRES_CANDIDATE] 叙事权威镜头不允许用"
            "自由文本覆盖已发布的分镜语义；请通过受控分镜候选修订后重新发布"
        )
    preflight_job_id = _begin_video_preflight_job(
        shot_id, supervisor_run_id=supervisor_run_id,
    )
    kwargs = {
        "prompt_override": prompt_override,
        "extra_negative": extra_negative,
        "reroll": reroll,
        "critique": critique,
        "after_shot_id": after_shot_id,
        "auto_retake_count": auto_retake_count,
        "supervisor_run_id": supervisor_run_id,
        "dependency_snapshot": dependency_snapshot,
        "critique_sources": critique_sources,
        "preflight_job_id": preflight_job_id,
    }
    repairs: list[dict[str, Any]] = []
    if prompt_override is None and not narrative_authority:
        # “待修复台词信息「…」”是已知的旧分镜结构。先迁移再编译，可避免
        # 故意制造一次 409；error_text 仅作为规则选择器，不代表此处已失败。
        repair = _auto_repair_embedded_source_dialogue(
            shot_id, "source_excerpt 原文内容",
        )
        if repair:
            repairs.append(repair)

    def repair_payload() -> dict[str, Any] | None:
        if not repairs:
            return None
        if len(repairs) == 1:
            return repairs[0]
        return {
            "repair": "composite_preflight_repair",
            "count": len(repairs),
            "repairs": list(repairs),
        }

    # 一条旧分镜可能同时有“原文对白夹在 action”“时长不足”“临时角色标签”
    # 三类问题。每次修复后重新从数据库加载 Shot，再走完整门禁；有界循环防止
    # 修复器互相打架或脏数据导致无限重放。
    local_repair_limit = 4
    result = None
    last_exc: Exception | None = None
    for _round in range(local_repair_limit + 1):
        payload = repair_payload()
        attempt_kwargs = dict(kwargs)
        if payload:
            attempt_kwargs["preflight_repair"] = payload
        try:
            result = _enqueue_shot_impl(shot_id, **attempt_kwargs)
            break
        except Exception as exc:
            last_exc = exc
            repair = None
            if (
                prompt_override is None
                and not narrative_authority
                and len(repairs) < local_repair_limit
            ):
                repair = _auto_repair_embedded_source_dialogue(shot_id, str(exc))
                if repair is None:
                    repair = _auto_expand_source_dialogue_duration(shot_id, str(exc))
                if repair is None:
                    repair = _auto_normalize_functional_speaker(shot_id, str(exc))
            if repair is None:
                failure = _mark_video_preflight_failure(preflight_job_id, exc)
                if failure.get("retry_scheduled"):
                    result = {
                        "reused": False,
                        "job_id": preflight_job_id,
                        "task_accepted": True,
                        **failure,
                    }
                    break
                raise
            repairs.append(repair)
    if result is None:
        assert last_exc is not None
        failure = _mark_video_preflight_failure(preflight_job_id, last_exc)
        if failure.get("retry_scheduled"):
            result = {
                "reused": False,
                "job_id": preflight_job_id,
                "task_accepted": True,
                **failure,
            }
        else:
            raise last_exc
    if result.get("reused"):
        _close_reused_preflight_job(preflight_job_id)
    repair = result.get("preflight_repair") or repair_payload()
    if repair:
        result["auto_repaired"] = True
        result["preflight_repair"] = repair
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
    幂等：相同 idem_key 的成功版本直接复用（reroll 时跳过复用）。"""
    from app.compiler import CompileError, compile_prompt
    from app.continuity import (
        derive_continuity_mode,
        effective_state_out,
        forbidden_prompt_content_errors,
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
    prev_shot = _load_shot_model(prev_row) if prev_row else None
        boundary_prev_row = conn.execute(
            "SELECT * FROM shots WHERE id=? AND episode_id=?",
            boundary_relation_action,
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

    raw_prompt_text = (prompt_override if prompt_override else
                       compile_prompt(shot, bible, extra_negative,
                                  with_refs=True,
                                  from_scene=False,
                                  chained=bool(chain_after_shot_id),
                                  critique=critique, prev_tail_action=None,
                                  screenplay=screenplay))
                                  screenplay=screenplay,
                                  video_generation_mode=(
                                      shot_plan.mode.value if shot_plan is not None else decision.mode
                                  ),
                                  first_frame_source=first_frame_source,
                                  boundary_relation_edit=boundary_relation_edit,
                                  boundary_relation_action=boundary_relation_action,
                                  boundary_start_state=boundary_start_state))
    raw_source_errors = [
        error for error in forbidden_prompt_content_errors(raw_prompt_text, shot)
        if "原文章节摘录" in error or "source_excerpt 原文内容" in error
    ]
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
        raise CompileError("；".join(preflight_errors))

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
            if preflight_repair:
                result["preflight_repair"] = preflight_repair
            return result

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
        "after_shot_id": chain_after_shot_id,
        "after_version_id": chain_after_version_id,
        "after_shot_no": None,
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
    # 补齐模式：优先读视频 grant 的 budget_cap_cny
    budget_limit = float(get_setting("episode_cost_limit_cny") or 100)
    try:
        from app.completion_grant import active_video_grant_budget_cap
        grant_cap = active_video_grant_budget_cap(ep["id"])
        if grant_cap is not None:
            budget_limit = float(grant_cap)
    except Exception:  # noqa: BLE001
        pass
            image_meta["reference_gallery_contract_override"] = True
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, created_at, image_inputs) "
        "VALUES(?,?,?,?,?, 'queued', ?, ?)",
        (version_id, shot_id, version_no, prompt_text, key, now(),
         json.dumps(image_meta, ensure_ascii=False)))
    job_id = preflight_job_id or new_id("job")
    budget_limit = episode_video_budget_limit(str(ep["id"]))
    run_id, step_run_id = ensure_media_trace(
        workflow_type="video_generation", scope_id=shot_id,
        input_value={"prompt": prompt_text, "version": version_no}, budget_limit_cny=budget_limit,
    )
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
    estimate = (
        shot_cost_cny(shot.duration_s)
        + config.IMAGE_PRICE_PER_UNIT * video_modes.estimated_keyframe_generation_count()
    )
        set_pipeline_stage(job_id, media_stages.STAGE_JOB_QUEUED, conn=conn)
    except Exception:  # noqa: BLE001
        pass
    conn.execute("UPDATE episodes SET status='generating' WHERE id=? AND status='confirmed'", (ep["id"],))
    conn.commit()
    from app.video_cost_model import initial_shot_generation_cost

    estimate = initial_shot_generation_cost(float(shot.duration_s))
    try:
        reserved = media_scheduler.reserve_budget(
            job_id, ep["id"], estimate, budget_limit, conn=conn
        )
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
