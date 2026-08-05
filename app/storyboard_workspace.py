"""分镜台安全工作区契约。

这里集中管理单调状态快照、短期预览凭据、编辑租约和原文证据绑定。
模块不依赖 ``app.api``，避免兼容 facade 的循环导入。
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from fastapi import HTTPException

from app import config
from app.db import get_conn, now
from app.evidence import repository as evidence_repository
from app.source_excerpt import align_source_excerpt


PREVIEW_TTL_S = 10 * 60
EDIT_LEASE_TTL_S = 30 * 60


def _inc(name: str, **labels: Any) -> None:
    try:
        from app.observability.metrics import inc
        inc(name, **labels)
    except Exception:  # noqa: BLE001
        pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def episode_fingerprint(episode_id: str) -> str:
    conn = get_conn()
    ep = conn.execute(
        """SELECT id, project_id, source_chapters, status, screenplay_status, screenplay_artifact_id,
                  storyboard_artifact_id, active_storyboard_run_id,
                  storyboard_outline_json, screenplay_json
           FROM episodes WHERE id=?""",
        (episode_id,),
    ).fetchone()
    if not ep:
        raise HTTPException(404, "剧集不存在")
    shots = conn.execute(
        """SELECT id, shot_uid, shot_no, storyboard_artifact_id, duration_s, shot_contract_json,
                  action_desc, first_frame_desc, last_frame_desc, source_excerpt,
                  characters, dialogues, transition
           FROM shots WHERE episode_id=? ORDER BY shot_no""",
        (episode_id,),
    ).fetchall()
    try:
        chapter_indices = [int(value) for value in json.loads(ep["source_chapters"] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        chapter_indices = []
    chapter_versions: list[dict[str, Any]] = []
    if chapter_indices:
        marks = ",".join("?" for _ in chapter_indices)
        rows = conn.execute(
            f"SELECT id,idx,content FROM chapters WHERE project_id=? AND idx IN ({marks}) ORDER BY idx",
            (ep["project_id"], *chapter_indices),
        ).fetchall()
        chapter_versions = [
            {
                "id": row["id"],
                "idx": row["idx"],
                "source_version_hash": hashlib.sha256((row["content"] or "").encode("utf-8")).hexdigest(),
            }
            for row in rows
        ]
    bindings = conn.execute(
        """SELECT b.shot_id,b.chapter_id,b.source_version_hash,b.start_offset,b.end_offset,b.excerpt_hash
           FROM storyboard_source_bindings b
           JOIN shots s ON s.id=b.shot_id
           WHERE s.episode_id=? ORDER BY s.shot_no""",
        (episode_id,),
    ).fetchall()
    return digest({
        "episode": dict(ep),
        "shots": [dict(row) for row in shots],
        "source_chapter_versions": chapter_versions,
        "source_bindings": [dict(row) for row in bindings],
        # 确认预览包含预计视频成本；部署切换费率后，旧令牌必须自然失效。
        "confirmation_cost_basis": {
            "video_price_per_second": config.VIDEO_PRICE_PER_SECOND,
            "image_price_per_unit": config.IMAGE_PRICE_PER_UNIT,
        },
    })


def monotonic_snapshot_version(episode_id: str, fingerprint: str | None = None) -> int:
    fingerprint = fingerprint or episode_fingerprint(episode_id)
    conn = get_conn()
    row = conn.execute(
        "SELECT snapshot_version, state_fingerprint FROM storyboard_workspace_state WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    stamp = now()
    if row is None:
        conn.execute(
            "INSERT INTO storyboard_workspace_state(episode_id,snapshot_version,state_fingerprint,updated_at) VALUES(?,?,?,?)",
            (episode_id, 1, fingerprint, stamp),
        )
        version = 1
    elif row["state_fingerprint"] != fingerprint:
        version = int(row["snapshot_version"]) + 1
        conn.execute(
            "UPDATE storyboard_workspace_state SET snapshot_version=?,state_fingerprint=?,updated_at=? WHERE episode_id=?",
            (version, fingerprint, stamp, episode_id),
        )
    else:
        version = int(row["snapshot_version"])
    conn.commit()
    return version


def finalize_storyboard_cancellation(
    episode_id: str,
    *,
    run_id: str | None = None,
    message: str = "分镜生成已手动取消",
    paused: bool = False,
) -> dict[str, Any]:
    """将分镜取消收口到一个可恢复、可重复执行的终态。

    分镜既能从分镜台按集取消，也能从监制房按 Run 取消。两条路径必须同时
    结束 Run、Supervisor checkpoint 和 episodes.scripting 锁，否则页面会继续
    把已经取消的任务显示为运行中。若传入的是已被新 Run 替代的旧 run_id，绝不
    触碰新任务的集状态或 checkpoint。
    """
    from app.completion_grant import revoke_active_video_grants_for_episode
    from app.evidence import repository
    from app.orchestration.engine import WorkflowRecorder
    from app.storyboard_supervisor import (
        SupervisorCheckpoint,
        _recover_outline_from_current_artifact,
        load_latest_checkpoint,
        save_checkpoint,
    )

    conn = get_conn()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not episode:
        raise HTTPException(404, "剧集不存在")

    active_run_id = episode["active_storyboard_run_id"]
    effective_run_id = run_id or active_run_id
    if run_id and active_run_id and active_run_id != run_id:
        return {
            "status": episode["status"],
            "cancelled": True,
            "deduplicated": True,
            "superseded": True,
            "run_id": run_id,
        }

    run = repository.get_run(effective_run_id) if effective_run_id else None
    if run and run.get("status") in repository.ACTIVE_RUN_STATUSES:
        WorkflowRecorder(effective_run_id).cancel(message)

    revoke_active_video_grants_for_episode(episode_id)
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"])
    checkpoint = load_latest_checkpoint(episode_id)
    if checkpoint is not None and not episode["storyboard_outline_json"]:
        recovered_outline = _recover_outline_from_current_artifact(
            conn,
            episode,
            checkpoint,
        )
        if recovered_outline is not None:
            save_checkpoint(checkpoint, run_id=effective_run_id)
    checkpoint_created = False
    if checkpoint is None and paused:
        checkpoint = SupervisorCheckpoint(
            episode_id=episode_id,
            phase="PAUSED_EXTERNAL",
            outcome="PAUSED_BY_USER",
            validated_prefix_end=shot_count,
            next_shot_no=shot_count + 1,
        )
        checkpoint_created = True
    target_checkpoint_phase = "PAUSED_EXTERNAL" if paused else "CANCELLED"
    target_checkpoint_outcome = "PAUSED_BY_USER" if paused else "CANCELLED"
    if checkpoint is not None and (
        checkpoint_created or checkpoint.phase != target_checkpoint_phase
    ):
        checkpoint.phase = target_checkpoint_phase
        checkpoint.outcome = target_checkpoint_outcome
        save_checkpoint(checkpoint, run_id=effective_run_id)

    target_status = "scripting" if paused else ("script_failed" if shot_count else "planned")
    if paused:
        outline_note = (
            "，首版分镜大纲也已保留"
            if checkpoint is not None and checkpoint.outline_artifact_id
            else ""
        )
        script_error = (
            f"用户已暂停分镜任务：已保留 {shot_count} 个工作镜头和安全检查点"
            f"{outline_note}，"
            "可继续任务或清空分镜。"
        )
    else:
        script_error = (
            f"分镜生成已手动取消：已保留 {shot_count} 个逐镜 checkpoint，"
            f"恢复时将从第 {shot_count + 1} 镜继续。"
            if shot_count else None
        )
    # 只有仍处于 scripting 的当前任务可改变业务状态；重复取消只清理与本 Run
    # 匹配的遗留指针，不会把已经确认或已由其他流程推进的剧集倒退。
    conn.execute(
        """UPDATE episodes
           SET status=CASE WHEN status='scripting' THEN ? ELSE status END,
               script_error=CASE WHEN status='scripting' THEN ? ELSE script_error END,
               active_storyboard_run_id=CASE
                   WHEN active_storyboard_run_id IS NULL OR active_storyboard_run_id=? THEN NULL
                   ELSE active_storyboard_run_id
               END
           WHERE id=?""",
        (target_status, script_error, effective_run_id, episode_id),
    )
    conn.commit()
    current = conn.execute(
        "SELECT status,active_storyboard_run_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    return {
        "status": current["status"],
        "shots": shot_count,
        "cancelled": True,
        "deduplicated": episode["status"] != "scripting",
        "run_id": effective_run_id,
        "checkpoint_phase": checkpoint.phase if checkpoint is not None else None,
        "paused": paused,
    }


def reconcile_cancelled_storyboard_run(episode_id: str) -> dict[str, Any] | None:
    """读取分镜工作区前修复旧版本遗留的“Run 已取消、集仍在运行”状态。"""
    from app.evidence import repository

    episode = get_conn().execute(
        "SELECT status,active_storyboard_run_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if not episode or episode["status"] != "scripting" or not episode["active_storyboard_run_id"]:
        return None
    run = repository.get_run(episode["active_storyboard_run_id"])
    if not run or run.get("workflow_type") != "storyboard" or run.get("status") != "CANCELLED":
        return None
    return finalize_storyboard_cancellation(
        episode_id,
        run_id=episode["active_storyboard_run_id"],
        message="已同步历史分镜取消状态",
    )


def create_preview(
    action_type: str,
    episode_id: str,
    payload: dict[str, Any],
    *,
    shot_id: str | None = None,
    baseline_fingerprint: str | None = None,
    ttl_s: int = PREVIEW_TTL_S,
) -> dict[str, Any]:
    token = f"sbpv_{secrets.token_urlsafe(24)}"
    baseline = baseline_fingerprint or episode_fingerprint(episode_id)
    created = now()
    expires = created + max(30, int(ttl_s))
    conn = get_conn()
    conn.execute(
        """INSERT INTO storyboard_action_previews(
               token,action_type,episode_id,shot_id,baseline_fingerprint,payload_json,
               expires_at,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (token, action_type, episode_id, shot_id, baseline, _json(payload), expires, created),
    )
    conn.commit()
    _inc("storyboard_preview_created_total", action=action_type, episode_id=episode_id, has_shot=bool(shot_id))
    return {**payload, "preview_token": token, "preview_expires_at": expires, "baseline_fingerprint": baseline}


def require_preview(
    token: str | None,
    action_type: str,
    episode_id: str,
    *,
    shot_id: str | None = None,
    consume: bool = False,
) -> dict[str, Any]:
    if not token:
        _inc("storyboard_preview_rejected_total", action=action_type, reason="missing")
        raise HTTPException(428, "请先查看并批准最新影响预览")
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM storyboard_action_previews WHERE token=?", (token,),
    ).fetchone()
    if not row or row["action_type"] != action_type or row["episode_id"] != episode_id:
        raise HTTPException(409, "预览凭据与本次操作不匹配，请重新预览")
    if shot_id is not None and row["shot_id"] != shot_id:
        raise HTTPException(409, "预览凭据对应了其他镜头，请重新预览")
    if row["consumed_at"] is not None:
        _inc("storyboard_preview_rejected_total", action=action_type, reason="consumed")
        raise HTTPException(409, "该预览已使用，请重新预览")
    if float(row["expires_at"]) < now():
        _inc("storyboard_preview_rejected_total", action=action_type, reason="expired")
        raise HTTPException(409, "预览已过期，请重新预览")
    current = episode_fingerprint(episode_id)
    if current != row["baseline_fingerprint"]:
        _inc("storyboard_preview_rejected_total", action=action_type, reason="state_drift")
        raise HTTPException(409, "预览后分镜或费率基线已变化，请重新预览")
    if consume:
        conn.execute("UPDATE storyboard_action_previews SET consumed_at=? WHERE token=?", (now(), token))
        conn.commit()
    try:
        return json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        raise HTTPException(409, "预览数据损坏，请重新预览") from None


def consume_preview(token: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE storyboard_action_previews SET consumed_at=COALESCE(consumed_at,?) WHERE token=?",
        (now(), token),
    )
    conn.commit()


def shot_content_hash(shot_row) -> str:
    artifact_id = shot_row["storyboard_artifact_id"]
    if artifact_id:
        artifact = evidence_repository.get_artifact(artifact_id)
        if artifact and artifact.get("content_hash"):
            return str(artifact["content_hash"])
    keys = (
        "shot_no", "duration_s", "shot_size", "camera_move", "scene_setting",
        "characters", "action_desc", "first_frame_desc", "last_frame_desc",
        "source_excerpt", "narration", "dialogues", "transition",
        "continuity_from_prev", "shot_contract_json", "continuity_mode",
    )
    return digest({key: shot_row[key] for key in keys})


def _storyboard_run_active(conn, episode_id: str) -> bool:
    from app import task_registry

    if task_registry.active("storyboard", episode_id):
        return True
    active_statuses = tuple(sorted(evidence_repository.ACTIVE_RUN_STATUSES))
    marks = ",".join("?" for _ in active_statuses)
    row = conn.execute(
        f"""SELECT 1 FROM workflow_runs
              WHERE scope_type='episode' AND scope_id=?
                AND workflow_type='storyboard'
                AND status IN ({marks})
                AND recovered_by_run_id IS NULL
              LIMIT 1""",
        (episode_id, *active_statuses),
    ).fetchone()
    return bool(row)


def create_edit_session(shot_id: str) -> dict[str, Any]:
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    ep = conn.execute("SELECT status FROM episodes WHERE id=?", (shot["episode_id"],)).fetchone()
    if not ep or _storyboard_run_active(conn, shot["episode_id"]):
        raise HTTPException(409, "分镜正在生成或修复；请先暂停并等待进入可编辑状态")
    token = f"sblease_{secrets.token_urlsafe(24)}"
    baseline_hash = shot_content_hash(shot)
    expires = now() + EDIT_LEASE_TTL_S
    conn.execute(
        """INSERT INTO storyboard_edit_sessions(
               token,episode_id,shot_id,baseline_artifact_id,baseline_content_hash,
               status,expires_at,created_at
           ) VALUES(?,?,?,?,?,'active',?,?)""",
        (token, shot["episode_id"], shot_id, shot["storyboard_artifact_id"], baseline_hash, expires, now()),
    )
    conn.commit()
    _inc("storyboard_edit_started_total", episode_id=shot["episode_id"], shot_id=shot_id, running=False)
    return {
        "edit_session_token": token,
        "baseline_artifact_id": shot["storyboard_artifact_id"],
        "baseline_content_hash": baseline_hash,
        "lease_expires_at": expires,
    }


def require_edit_session(token: str | None, shot_id: str) -> dict[str, Any]:
    if not token:
        raise HTTPException(428, "编辑会话不存在，请重新进入编辑")
    conn = get_conn()
    row = conn.execute("SELECT * FROM storyboard_edit_sessions WHERE token=?", (token,)).fetchone()
    if not row or row["shot_id"] != shot_id or row["status"] != "active":
        raise HTTPException(409, "编辑会话无效，请重新进入编辑")
    if float(row["expires_at"]) < now():
        conn.execute("UPDATE storyboard_edit_sessions SET status='expired' WHERE token=?", (token,))
        conn.commit()
        raise HTTPException(409, "编辑租约已过期；本地内容仍保留，请重新取得编辑基线")
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    ep = conn.execute("SELECT status FROM episodes WHERE id=?", (row["episode_id"],)).fetchone()
    if not shot or not ep or _storyboard_run_active(conn, row["episode_id"]):
        raise HTTPException(409, "任务已恢复运行，当前草稿只能保留，不能发布")
    current_hash = shot_content_hash(shot)
    if (shot["storyboard_artifact_id"] or None) != (row["baseline_artifact_id"] or None) or current_hash != row["baseline_content_hash"]:
        _inc("storyboard_stale_edit_blocked_total", episode_id=row["episode_id"], shot_id=shot_id)
        raise HTTPException(409, {
            "code": "STALE_EDIT_BASELINE",
            "message": "编辑期间出现了新版本，发布已冻结；请对比最新版后迁移草稿",
            "baseline_artifact_id": row["baseline_artifact_id"],
            "current_artifact_id": shot["storyboard_artifact_id"],
            "baseline_hash": row["baseline_content_hash"],
            "current_hash": current_hash,
        })
    return dict(row)


def close_edit_session(token: str, status: str = "saved") -> None:
    conn = get_conn()
    conn.execute("UPDATE storyboard_edit_sessions SET status=? WHERE token=?", (status, token))
    conn.commit()


def chapter_sources(episode_id: str) -> list[dict[str, Any]]:
    conn = get_conn()
    ep = conn.execute("SELECT project_id,source_chapters FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise HTTPException(404, "剧集不存在")
    try:
        indices = [int(x) for x in json.loads(ep["source_chapters"] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        indices = []
    if not indices:
        return []
    marks = ",".join("?" for _ in indices)
    rows = conn.execute(
        f"SELECT id,idx,title,content FROM chapters WHERE project_id=? AND idx IN ({marks}) ORDER BY idx",
        (ep["project_id"], *indices),
    ).fetchall()
    return [
        {**dict(row), "source_version_hash": hashlib.sha256((row["content"] or "").encode("utf-8")).hexdigest()}
        for row in rows
    ]


def validate_source_binding(episode_id: str, binding: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    sources = {int(item["id"]): item for item in chapter_sources(episode_id)}
    try:
        chapter_id = int(binding.get("chapter_id"))
        start = int(binding.get("start_offset"))
        end = int(binding.get("end_offset"))
    except (TypeError, ValueError):
        raise HTTPException(422, "原文证据必须包含有效章节和起止位置") from None
    source = sources.get(chapter_id)
    if not source:
        raise HTTPException(422, "所选章节不属于本集授权原文")
    content = source["content"] or ""
    if start < 0 or end <= start or end > len(content):
        raise HTTPException(422, "原文证据偏移超出章节范围或不是连续片段")
    if binding.get("source_version_hash") != source["source_version_hash"]:
        raise HTTPException(409, "原文版本已变化，请重新框选证据")
    excerpt = content[start:end]
    expected_excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    if binding.get("excerpt_hash") not in (None, "", expected_excerpt_hash):
        raise HTTPException(422, "原文片段内容校验失败，请重新框选")
    normalized = {
        "chapter_id": chapter_id,
        "chapter_idx": int(source["idx"]),
        "source_version_hash": source["source_version_hash"],
        "start_offset": start,
        "end_offset": end,
        "excerpt_hash": expected_excerpt_hash,
    }
    return excerpt, normalized


def persist_source_binding(
    shot_id: str,
    normalized: dict[str, Any],
    *,
    conn=None,
    commit: bool = True,
) -> None:
    db_conn = conn or get_conn()
    db_conn.execute(
        """INSERT INTO storyboard_source_bindings(
               shot_id,chapter_id,chapter_idx,source_version_hash,start_offset,end_offset,
               excerpt_hash,updated_at
           ) VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(shot_id) DO UPDATE SET
               chapter_id=excluded.chapter_id,chapter_idx=excluded.chapter_idx,
               source_version_hash=excluded.source_version_hash,
               start_offset=excluded.start_offset,end_offset=excluded.end_offset,
               excerpt_hash=excluded.excerpt_hash,updated_at=excluded.updated_at""",
        (
            shot_id, normalized["chapter_id"], normalized["chapter_idx"],
            normalized["source_version_hash"], normalized["start_offset"],
            normalized["end_offset"], normalized["excerpt_hash"], now(),
        ),
    )
    if commit:
        db_conn.commit()


def realign_generated_source_binding(
    episode_id: str,
    shot_id: str,
    excerpt: str,
    *,
    conn=None,
    commit: bool = True,
) -> dict[str, Any]:
    """Atomically align and bind an automated repair's authorized source excerpt."""
    candidate, normalized = align_generated_source_evidence(episode_id, excerpt)
    db_conn = conn or get_conn()
    db_conn.execute(
        "UPDATE shots SET source_excerpt=? WHERE id=?",
        (candidate, shot_id),
    )
    persist_source_binding(
        shot_id, normalized, conn=db_conn, commit=commit,
    )
    return {**normalized, "source_excerpt": candidate}


def align_generated_source_evidence(
    episode_id: str,
    excerpt: str,
) -> tuple[str, dict[str, Any]]:
    """Resolve model-selected evidence to the strongest authorized contiguous slice."""
    candidate = (excerpt or "").strip()
    if not candidate:
        raise HTTPException(422, "自动修复候选缺少原文证据")
    matches = []
    for source in chapter_sources(episode_id):
        aligned = align_source_excerpt(candidate, source["content"] or "")
        if aligned is not None:
            matches.append((aligned.match_chars, int(aligned.exact), source, aligned))
    if not matches:
        raise HTTPException(422, "自动修复候选的原文证据不属于本集授权原文")
    _score, _exact, source, aligned = max(
        matches, key=lambda item: (item[0], item[1]),
    )
    candidate = aligned.excerpt
    normalized = {
        "chapter_id": int(source["id"]),
        "chapter_idx": int(source["idx"]),
        "source_version_hash": source["source_version_hash"],
        "start_offset": aligned.start_offset,
        "end_offset": aligned.end_offset,
        "excerpt_hash": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
    }
    return candidate, normalized


def source_binding_for_shot(shot_id: str) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM storyboard_source_bindings WHERE shot_id=?", (shot_id,),
    ).fetchone()
    return dict(row) if row else None


def repair_generated_source_bindings(episode_id: str) -> dict[str, Any]:
    """Bind or safely realign unbound AI-generated source excerpts.

    Only a sufficiently strong contiguous match in an authorized chapter is
    accepted.  Existing bindings are never rewritten here; version drift and
    human-authored bindings continue through the strict validation path.
    """
    conn = get_conn()
    sources = chapter_sources(episode_id)
    episode = conn.execute(
        "SELECT screenplay_json FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    try:
        screenplay_payload = json.loads(
            (episode["screenplay_json"] if episode else None) or "{}"
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        screenplay_payload = {}
    event_source_spans = {
        str(item.get("event_id") or ""): str(item.get("source_span") or "")
        for item in (screenplay_payload.get("events") or [])
        if isinstance(item, dict) and str(item.get("event_id") or "")
    }
    shots = conn.execute(
        """SELECT s.id,s.shot_no,s.source_excerpt,s.shot_contract_json
           FROM shots s
           LEFT JOIN storyboard_source_bindings b ON b.shot_id=s.id
           WHERE s.episode_id=? AND b.shot_id IS NULL
           ORDER BY s.shot_no""",
        (episode_id,),
    ).fetchall()
    bound = 0
    realigned = 0
    unresolved: list[int] = []
    for row in shots:
        candidate = (row["source_excerpt"] or "").strip()
        matches = []
        for source in sources:
            aligned = align_source_excerpt(candidate, source["content"] or "")
            if aligned is not None:
                matches.append((aligned.match_chars, int(aligned.exact), source, aligned))
        if not matches:
            try:
                contract = json.loads(row["shot_contract_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                contract = {}
            event_candidates: list[str] = []
            for event_id in contract.get("event_ids") or []:
                source_span = event_source_spans.get(str(event_id), "").strip()
                if not source_span:
                    continue
                _prefix, separator, span_text = source_span.partition("：")
                if not separator:
                    _prefix, separator, span_text = source_span.partition(":")
                event_candidates.append(
                    (span_text if separator else source_span).strip()
                )
            for event_candidate in event_candidates:
                for source in sources:
                    aligned = align_source_excerpt(
                        event_candidate,
                        source["content"] or "",
                    )
                    if aligned is not None:
                        matches.append(
                            (
                                aligned.match_chars,
                                int(aligned.exact),
                                source,
                                aligned,
                            )
                        )
        if not matches:
            unresolved.append(int(row["shot_no"]))
            continue
        _score, _exact, source, aligned = max(matches, key=lambda item: (item[0], item[1]))
        if aligned.excerpt != candidate:
            conn.execute(
                "UPDATE shots SET source_excerpt=? WHERE id=?",
                (aligned.excerpt, row["id"]),
            )
            realigned += 1
        excerpt_hash = hashlib.sha256(aligned.excerpt.encode("utf-8")).hexdigest()
        conn.execute(
            """INSERT INTO storyboard_source_bindings(
                   shot_id,chapter_id,chapter_idx,source_version_hash,start_offset,end_offset,
                   excerpt_hash,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                row["id"], int(source["id"]), int(source["idx"]),
                source["source_version_hash"], aligned.start_offset,
                aligned.end_offset, excerpt_hash, now(),
            ),
        )
        bound += 1
    conn.commit()
    if bound:
        _inc(
            "storyboard_source_evidence_auto_bound_total",
            episode_id=episode_id,
            bound=bound,
            realigned=realigned,
        )
    return {
        "bound": bound,
        "realigned": realigned,
        "unresolved_shot_nos": unresolved,
    }


def verify_or_bind_existing_excerpt(
    episode_id: str,
    shot_id: str,
    excerpt: str,
    *,
    persist_legacy: bool = True,
) -> dict[str, Any]:
    """校验历史证据；无绑定的老数据只在能精确定位连续原文时懒迁移。

    状态快照使用 ``persist_legacy=False`` 保持只读；真正的确认预览才提交可证明的
    历史绑定，避免普通轮询产生审计写入。
    """
    excerpt = (excerpt or "").strip()
    if not excerpt:
        raise HTTPException(422, "原文证据为空，请从本集原文重新框选")
    existing = source_binding_for_shot(shot_id)
    if existing:
        source = next(
            (item for item in chapter_sources(episode_id) if int(item["id"]) == int(existing["chapter_id"])),
            None,
        )
        if not source or source["source_version_hash"] != existing["source_version_hash"]:
            raise HTTPException(409, "原文版本已变化，已有证据需要重新绑定")
        actual = (source["content"] or "")[int(existing["start_offset"]):int(existing["end_offset"])]
        if actual != excerpt or hashlib.sha256(actual.encode("utf-8")).hexdigest() != existing["excerpt_hash"]:
            raise HTTPException(422, "原文证据与已绑定章节位置不一致")
        return existing
    matches: list[tuple[dict[str, Any], int]] = []
    for source in chapter_sources(episode_id):
        offset = (source["content"] or "").find(excerpt)
        if offset >= 0:
            matches.append((source, offset))
    if not matches:
        _inc("storyboard_source_evidence_rejected_total", episode_id=episode_id, reason="not_found")
        raise HTTPException(422, "现有原文证据无法在本集授权原文中定位，请重新框选")
    source, start = matches[0]
    normalized = {
        "chapter_id": int(source["id"]),
        "chapter_idx": int(source["idx"]),
        "source_version_hash": source["source_version_hash"],
        "start_offset": start,
        "end_offset": start + len(excerpt),
        "excerpt_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }
    if persist_legacy:
        persist_source_binding(shot_id, normalized)
    return normalized
