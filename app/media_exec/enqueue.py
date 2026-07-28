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
        """SELECT j.id, j.version_id, j.run_id, j.step_run_id,
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
        plans.append((row, "waiting_provider" if provider_task_id else "queued"))
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
        cursor = conn.execute(
            """UPDATE jobs
                  SET status=?, error=NULL, lease_owner=NULL, lease_expires_at=NULL,
                      next_retry_at=NULL, updated_at=?
                WHERE id=? AND status='paused' AND cancellation_requested=0 AND abandoned=0""",
            (next_status, now(), row["id"]),
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
            _enqueue_for_current_status(row["id"])
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


# ---------- 入队 ----------

def _load_shot_model(shot_row) -> "object":
    from app.continuity import apply_shot_contract
    from app.schemas import Shot
    shot = Shot(
        shot_no=shot_row["shot_no"], duration_s=shot_row["duration_s"], shot_size=shot_row["shot_size"],
        camera_move=shot_row["camera_move"], scene_setting=shot_row["scene_setting"],
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
                 auto_retake_count: int = 0,
                 supervisor_run_id: str | None = None,
                 dependency_snapshot: dict[str, Any] | None = None,
                 critique_sources: list[dict[str, Any]] | None = None) -> dict:
    """为镜头创建参考图模式视频版本并入队。
    critique：上一版 AI 评语问题，作为本次必须改正项写入 prompt。
    幂等：相同 idem_key 的成功版本直接复用（reroll 时跳过复用）。"""
    from app.compiler import CompileError, compile_prompt
    from app.continuity import (
        derive_continuity_mode,
        effective_state_out,
        preflight_seedance_gates,
        resolve_do_not_repeat_texts,
        shot_contract_dict,
        uses_previous_tail_frame,
    )
    from app.schemas import Bible, EpisodeScreenplay

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
    # Compile the paid video request from the accepted per-episode portrait
    # revision, not from a possibly older project-Bible appearance string.
    # The worker already resolves this view for keyframes; enqueue must use the
    # same source or the frozen video prompt can disagree with its reference pack.
    from app.portraits import bible_for_episode
    bible = bible_for_episode(ep["project_id"], bible, ep["episode_no"])
    shot = _load_shot_model(shot_row)
    screenplay = None
    if _row_value(ep, "screenplay_json"):
        try:
            screenplay = EpisodeScreenplay.model_validate(json.loads(ep["screenplay_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            screenplay = None
    prior_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? AND shot_no<? ORDER BY shot_no",
        (shot_row["episode_id"], int(shot_row["shot_no"])),
    ).fetchall()
    prior_shots = [_load_shot_model(row) for row in prior_rows]
    # Persisted legacy boards may contain snake_case ledger IDs. Resolve them
    # to Chinese semantics at the final model boundary; unresolved IDs vanish.
    shot.do_not_repeat = resolve_do_not_repeat_texts(shot, screenplay, prior_shots)
    decision = _decision_from_mode_plan(shot_row) or video_modes.default_reference_decision()
    if decision.mode != video_modes.REFERENCE_IMAGE_MODE:
        decision = video_modes.default_reference_decision()

    # 跨镜连贯只继承上一镜的实际/计划尾状态；不得把上一镜完整动作描述塞进 prompt。
    # after_shot_id 无效时回退到 shot_no-1，避免 action_continuation 在缺 prev 时被当成链首误杀。
    prev_row = None
    if after_shot_id:
        prev_row = conn.execute("SELECT * FROM shots WHERE id=?", (after_shot_id,)).fetchone()
    if prev_row is None and int(shot_row["shot_no"]) > 1:
        prev_row = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
            (shot_row["episode_id"], int(shot_row["shot_no"]) - 1),
        ).fetchone()
    prev_shot = _load_shot_model(prev_row) if prev_row else None
    continuity_mode = derive_continuity_mode(shot, prev_shot)
    shot.continuity_mode = continuity_mode
    prev_state_out = effective_state_out(prev_shot) if prev_shot else None
    prompt_prev_state_out = prev_state_out if uses_previous_tail_frame(continuity_mode) else None
    if prompt_prev_state_out:
        shot.state_in = prompt_prev_state_out
    chain_after_shot_id = (
        (prev_row["id"] if prev_row else None)
        if uses_previous_tail_frame(continuity_mode) else None
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

    preflight_errors = preflight_seedance_gates(shot, prev=prev_shot, prompt_text=None)
    if preflight_errors:
        raise CompileError("；".join(preflight_errors))

    prompt_text = (prompt_override if prompt_override else
                   compile_prompt(shot, bible, extra_negative,
                                  with_refs=True,
                                  from_scene=False,
                                  chained=bool(chain_after_shot_id),
                                  critique=critique, prev_tail_action=None,
                                  with_last_frame=False,
                                  incoming_transition=incoming_transition,
                                  outgoing_transition=outgoing_transition["transition"] if outgoing_transition else None,
                                  next_scene=outgoing_transition["next_scene"] if outgoing_transition else None,
                                  next_first_frame_desc=outgoing_transition["next_first_frame_desc"] if outgoing_transition else None,
                                  continuity_mode=continuity_mode,
                                  prev_state_out=prompt_prev_state_out))
    prompt_text = ensure_source_excerpt_in_prompt(prompt_text, shot)
    preflight_errors = preflight_seedance_gates(shot, prev=prev_shot, prompt_text=prompt_text)
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
        + f"|mode:{video_modes.REFERENCE_IMAGE_MODE}|plan:{video_modes.decision_to_dict(decision)}"
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
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
    }
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
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, created_at, image_inputs) "
        "VALUES(?,?,?,?,?, 'queued', ?, ?)",
        (version_id, shot_id, version_no, prompt_text, key, now(),
         json.dumps(image_meta, ensure_ascii=False)))
    job_id = new_id("job")
    # 补齐模式：优先读视频 grant 的 budget_cap_cny
    budget_limit = float(get_setting("episode_cost_limit_cny") or 100)
    try:
        from app.completion_grant import active_video_grant_budget_cap
        grant_cap = active_video_grant_budget_cap(ep["id"])
        if grant_cap is not None:
            budget_limit = float(grant_cap)
    except Exception:  # noqa: BLE001
        pass
    run_id, step_run_id = ensure_media_trace(
        workflow_type="video_generation", scope_id=shot_id,
        input_value={"prompt": prompt_text, "version": version_no}, budget_limit_cny=budget_limit,
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, shot_id, version_id, episode_id, project_id, status, created_at, "
        "updated_at, after_shot_id, after_version_id, run_id, owner_run_id, step_run_id) "
        "VALUES(?, 'video', ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id, shot_id, version_id, ep["id"], project["id"], now(), now(), chain_after_shot_id,
            chain_after_version_id, run_id, supervisor_run_id, step_run_id,
        ))
    try:
        from app.media_pipeline import stages as media_stages
        from app.media_pipeline.stage_state import set_pipeline_stage
        set_pipeline_stage(job_id, media_stages.STAGE_JOB_QUEUED, conn=conn)
    except Exception:  # noqa: BLE001
        pass
    conn.execute("UPDATE episodes SET status='generating' WHERE id=? AND status='confirmed'", (ep["id"],))
    conn.commit()
    estimate = (
        shot_cost_cny(shot.duration_s)
        + config.IMAGE_PRICE_PER_UNIT * video_modes.estimated_keyframe_generation_count()
    )
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
        return {
            "reused": False, "version_id": version_id, "job_id": job_id,
            "paused_budget": True,
        }
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
    return {
        "reused": False,
        "version_id": version_id,
        "job_id": job_id,
        "dispatch_deferred": dispatch_deferred,
        "task_accepted": True,
    }

__all__ = [name for name in globals() if not name.startswith("__")]
