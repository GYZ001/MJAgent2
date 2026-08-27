from __future__ import annotations
from app.auth.principal import current_actor_name

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

from app.visual_styles import (
    DEFAULT_VISUAL_STYLE_NAME,
    default_visual_style_prompt,
    visual_style_options,
    visual_style_prompt,
)


def _project_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}


def _supports_bible_style_name(conn) -> bool:
    return "bible_style_name" in _project_columns(conn)


def _normalize_visual_style_name(style_name: str | None) -> str:
    name = (style_name or DEFAULT_VISUAL_STYLE_NAME).strip()
    if visual_style_prompt(name) is None:
        raise HTTPException(422, "请选择有效的统一画面风格")
    return name


def _visual_style_prompt_or_default(style_name: str | None) -> str:
    name = _normalize_visual_style_name(style_name)
    return visual_style_prompt(name) or default_visual_style_prompt()


def _parse_json_value(value, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _normalize_character_selection(value) -> list[str] | None:
    if value in (None, ""):
        return None
    raw_items = value
    if isinstance(value, str):
        parsed = _parse_json_value(value)
        raw_items = parsed if isinstance(parsed, list) else value.split(",")
    if not isinstance(raw_items, list):
        raise HTTPException(422, "characters 必须是角色名数组")
    names: list[str] = []
    for item in raw_items:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names or None


def _refs_target_payload(only_character: str | None, only_characters: list[str] | None) -> str | None:
    if only_characters:
        return json.dumps(only_characters, ensure_ascii=False)
    return only_character


def _decode_refs_target(value: str | None) -> tuple[str | None, list[str] | None]:
    parsed = _parse_json_value(value)
    if isinstance(parsed, list):
        names = _normalize_character_selection(parsed)
        if names:
            return (names[0] if len(names) == 1 else None), names
    # 历史单角色 refs_target 为纯字符串，不升格为 only_characters 列表
    target = str(value or "").strip() or None
    return target, None


def _quote_stale(precheck: dict, message: str = "费用预检已过期或范围变化，请重新确认") -> HTTPException:
    return HTTPException(
        409,
        detail={
            "code": "QUOTE_STALE",
            "message": message,
            "precheck": precheck,
        },
    )


def _payment_confirm_required(precheck: dict | None = None) -> HTTPException:
    detail = {
        "code": "PAYMENT_CONFIRM_REQUIRED",
        "message": "必须先完成费用预检并显式确认（confirm=true）",
    }
    if precheck is not None:
        detail["precheck"] = precheck
    return HTTPException(409, detail=detail)


def _ensure_character_payment_quotes(conn) -> None:
    """兼容单测中的最小化 schema；正式数据库由 app.db.SCHEMA 创建。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS character_payment_quotes (
            quote_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            action TEXT NOT NULL,
            scope_fingerprint TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            expires_at REAL NOT NULL,
            consumed_task_id TEXT,
            consumed_run_id TEXT,
            created_at REAL NOT NULL,
            consumed_at REAL
        )"""
    )


def _issue_payment_quote(precheck: dict) -> dict:
    """将付费预检签发为有时效、可消费的服务端凭证。"""
    issued = dict(precheck)
    issued["quote_id"] = new_id("quote")
    issued["computed_at"] = now()
    issued["quote_expires_at"] = issued["computed_at"] + 300
    conn = get_conn()
    _ensure_character_payment_quotes(conn)
    conn.execute(
        "INSERT INTO character_payment_quotes(quote_id,project_id,action,scope_fingerprint,"
        "payload_json,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            issued["quote_id"], issued["project_id"], issued["action"],
            issued["scope_fingerprint"], json.dumps(issued, ensure_ascii=False),
            issued["quote_expires_at"], issued["computed_at"],
        ),
    )
    conn.commit()
    return issued


def _validate_payment_quote(project_id: str, quote_id: str | None, current: dict):
    if not quote_id:
        raise _quote_stale(current, "费用预检缺失，请重新确认")
    conn = get_conn()
    _ensure_character_payment_quotes(conn)
    row = conn.execute(
        "SELECT * FROM character_payment_quotes WHERE quote_id=? AND project_id=?",
        (quote_id, project_id),
    ).fetchone()
    if not row:
        raise _quote_stale(current)
    if row["consumed_at"] is not None:
        return row
    if float(row["expires_at"] or 0) < now():
        raise _quote_stale(current, "费用预检已过期，请重新确认")
    if row["action"] != current.get("action") or row["scope_fingerprint"] != current.get("scope_fingerprint"):
        raise _quote_stale(current)
    return row


def _consume_payment_quote(quote_id: str, *, task_id: str, run_id: str | None) -> None:
    conn = get_conn()
    _ensure_character_payment_quotes(conn)
    conn.execute(
        "UPDATE character_payment_quotes SET consumed_task_id=?, consumed_run_id=?, consumed_at=? "
        "WHERE quote_id=? AND consumed_at IS NULL",
        (task_id, run_id, now(), quote_id),
    )
    conn.commit()


def _new_refs_recorder(
    project_id: str,
    only_character: str | None,
    only_characters: list[str] | None,
    *,
    resume: bool,
    fresh_after: float | None,
    parent_run_id: str | None,
    requested_by: str,
    trigger_type: str,
) -> WorkflowRecorder:
    return WorkflowRecorder.create(
        workflow_type="character_references",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(
            project_id, only_character, only_characters, "character_references",
            resume, fresh_after,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        config_snapshot={
            "only_character": only_character,
            "only_characters": only_characters,
            "resume": resume,
            "fresh_after": fresh_after,
        },
        budget_limit_cny=float(get_setting("episode_cost_limit_cny") or 100),
        parent_run_id=parent_run_id,
    )


def _active_refs_run(project_id: str):
    return get_conn().execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='character_references'
             AND scope_type='project'
             AND scope_id=?
             AND status='RUNNING'
           ORDER BY updated_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()


def _refs_generation_busy(project_id: str) -> bool:
    return _refs_task_active(project_id) or _active_refs_run(project_id) is not None


def _start_refs_generation(
    project_id: str,
    only_character: str | None,
    *,
    only_characters: list[str] | None = None,
    resume: bool = False,
    fresh_after: float | None = None,
    parent_run_id: str | None = None,
) -> dict | None:
    """启动定妆照任务。

    返回可追踪的任务与 run id；已有同项目任务时返回 None。
    """
    if _refs_generation_busy(project_id):
        return None
    conn = get_conn()
    previous = conn.execute(
        "SELECT refs_status,refs_error,refs_target,refs_resume,refs_batch_started_at "
        "FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    target_payload = _refs_target_payload(only_character, only_characters)
    # ``fresh_after`` controls which ready packs resume may skip.  The operation
    # batch timestamp is separate and always durable so paid image calls can be
    # reused after a restart, including gap-only resume batches.
    previous_batch_started_at = previous["refs_batch_started_at"] if previous else None
    batch_started_at = (
        fresh_after
        if fresh_after is not None
        else previous_batch_started_at
        if resume and previous_batch_started_at is not None
        else now()
    )
    persisted_resume = 1 if resume and fresh_after is None else 0
    if target_payload is None:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=NULL, "
            "refs_resume=?, refs_batch_started_at=? WHERE id=?",
            (persisted_resume, batch_started_at, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=?, "
            "refs_resume=?, refs_batch_started_at=? WHERE id=?",
            (target_payload, persisted_resume, batch_started_at, project_id),
        )
    conn.commit()
    requested_by = "system" if resume else "user"
    trigger_type = "resume" if resume else "manual"
    recorder = None
    try:
        recorder = _new_refs_recorder(
            project_id, only_character, only_characters,
            resume=resume, fresh_after=batch_started_at, parent_run_id=parent_run_id,
            requested_by=requested_by, trigger_type=trigger_type,
        )
        task_registry.spawn(
            "refs", project_id,
            _refs_task(
                project_id, only_character, only_characters=only_characters,
                resume=resume, fresh_after=fresh_after,
                operation_started_at=batch_started_at,
                parent_run_id=parent_run_id,
                requested_by=requested_by, trigger_type=trigger_type, recorder=recorder,
            ),
            project_id=project_id,
        )
    except Exception as exc:
        if previous:
            conn.execute(
                "UPDATE projects SET refs_status=?,refs_error=?,refs_target=?,"
                "refs_resume=?,refs_batch_started_at=? WHERE id=?",
                (
                    previous["refs_status"], previous["refs_error"], previous["refs_target"],
                    previous["refs_resume"], previous["refs_batch_started_at"], project_id,
                ),
            )
            conn.commit()
        if recorder is not None:
            try:
                recorder.cancel("定妆任务未能启动，项目状态已回滚", conn=None)
            except Exception:  # noqa: BLE001
                pass
        raise ValueError("定妆任务未能启动，原状态和费用凭证已保留，请重试") from exc
    return {
        "status": "accepted",
        "task_id": f"refs:{project_id}",
        "run_id": recorder.run_id,
    }

def _start_scene_refs_generation(
    project_id: str,
    only_scene: str | list[str] | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
) -> bool:
    """启动场景图素材库生成任务。已有同项目任务在跑则返回 False。"""
    if _scene_refs_task_active(project_id):
        return False
    conn = get_conn()
    project_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    batch_column_supported = "scene_refs_batch_started_at" in project_columns
    previous = conn.execute(
        "SELECT scene_refs_status,scene_refs_error,scene_refs_target,"
        + ("scene_refs_batch_started_at" if batch_column_supported else "NULL AS scene_refs_batch_started_at")
        + " FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    batch_started_at = (
        previous["scene_refs_batch_started_at"]
        if resume and previous and previous["scene_refs_batch_started_at"] is not None
        else now()
    )
    target_payload = (
        json.dumps(only_scene, ensure_ascii=False)
        if isinstance(only_scene, list) else only_scene
    )
    if batch_column_supported:
        conn.execute(
            "UPDATE projects SET scene_refs_status='running',scene_refs_error=NULL,"
            "scene_refs_target=?,scene_refs_batch_started_at=? WHERE id=?",
            (target_payload, batch_started_at, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET scene_refs_status='running',scene_refs_error=NULL,"
            "scene_refs_target=? WHERE id=?",
            (target_payload, project_id),
        )
    conn.commit()
    try:
        task_registry.spawn(
            "scene_refs", project_id,
            _scene_refs_task(
                project_id, only_scene, resume=resume, parent_run_id=parent_run_id,
                operation_started_at=batch_started_at,
                requested_by="system" if resume else "user",
                trigger_type="resume" if resume else "manual",
            ),
            project_id=project_id,
        )
    except Exception as exc:
        if previous:
            if batch_column_supported:
                conn.execute(
                    "UPDATE projects SET scene_refs_status=?,scene_refs_error=?,scene_refs_target=?,"
                    "scene_refs_batch_started_at=? WHERE id=?",
                    (
                        previous["scene_refs_status"], previous["scene_refs_error"],
                        previous["scene_refs_target"], previous["scene_refs_batch_started_at"],
                        project_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE projects SET scene_refs_status=?,scene_refs_error=?,scene_refs_target=? "
                    "WHERE id=?",
                    (
                        previous["scene_refs_status"], previous["scene_refs_error"],
                        previous["scene_refs_target"], project_id,
                    ),
                )
            conn.commit()
        raise ValueError("场景图任务未能启动，原状态和费用凭证已保留，请重试") from exc
    return True


def _start_scene_bible_preparation(
    project_id: str,
    *,
    parent_run_id: str | None = None,
    requested_by: str = "system",
    trigger_type: str = "automatic",
) -> bool:
    """后台准备免费场景清单；图片生成仍必须经过独立费用确认。"""
    if _scene_assets_task_active(project_id):
        return False
    conn = get_conn()
    previous = conn.execute(
        "SELECT scene_refs_status,scene_refs_error FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    conn.execute(
        "UPDATE projects SET scene_refs_status='running',scene_refs_error=NULL WHERE id=?",
        (project_id,),
    )
    conn.commit()
    try:
        task_registry.spawn(
            "scene_bible",
            project_id,
            _scene_bible_task(
                project_id,
                parent_run_id=parent_run_id,
                requested_by=requested_by,
                trigger_type=trigger_type,
            ),
            project_id=project_id,
        )
    except Exception as exc:
        if previous:
            conn.execute(
                "UPDATE projects SET scene_refs_status=?,scene_refs_error=? WHERE id=?",
                (previous["scene_refs_status"], previous["scene_refs_error"], project_id),
            )
            conn.commit()
        raise ValueError("场景设定任务未能启动，原状态已保留，请重试") from exc
    return True


def _decode_scene_target(value: str | list[str] | None) -> str | list[str] | None:
    if not isinstance(value, str):
        return value
    parsed = _parse_json_value(value)
    if isinstance(parsed, list):
        names = [str(item).strip() for item in parsed if str(item).strip()]
        return list(dict.fromkeys(names)) or None
    return value.strip() or None


async def _scene_bible_task(
    project_id: str,
    *,
    parent_run_id: str | None = None,
    requested_by: str = "system",
    trigger_type: str = "automatic",
) -> None:
    """生成并落库场景清单，不绕过费用确认自动生成图片。"""
    from app.stages import generate_scene_bible
    conn = get_conn()
    recorder = WorkflowRecorder.create(
        workflow_type="scene_bible",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, "scene_bible"),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={"max_iterations": 4, "warning_blocks_downstream": True},
        parent_run_id=parent_run_id,
    )
    try:
        recorder.start()
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if not p or not p["bible_json"]:
            raise ValueError("人物谱不存在，不能生成场景 Bible")
        bible = Bible.model_validate(json.loads(p["bible_json"]))
        # 初始场景清单只取前 N 章：避免一上来就铺满全片场景；更靠后的新场景留到分镜阶段反应式补图。
        from app.scenes import SCENE_BIBLE_CHAPTER_WINDOW
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx LIMIT ?",
            (project_id, SCENE_BIBLE_CHAPTER_WINDOW)).fetchall())
        _, scenes = await recorder.step(
            "scene_bible",
            lambda: generate_scene_bible(chapters, bible, project_id=project_id),
            contract_key="scene_bible",
            agent_name="scene_bible",
        )
        # 重读 bible（人物谱可能已被并发流程更新），只覆盖 scenes 字段后回写。
        p2 = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        data = json.loads(p2["bible_json"]) if p2 and p2["bible_json"] else bible.model_dump()
        data["scenes"] = [s.model_dump() for s in scenes]
        conn.execute(
            "UPDATE projects SET bible_json=?,scene_refs_status='idle',scene_refs_error=NULL "
            "WHERE id=?",
            (json.dumps(data, ensure_ascii=False), project_id),
        )
        conn.commit()
        recorder.succeed("场景设定已准备，场景图等待费用确认", conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，场景设定任务等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:  # noqa: BLE001 场景设定失败不阻断人物谱主流程
        recorder.fail(exc, conn=None)
        public = errors.record_and_format(exc, action="scene_bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


def recover_bible_tasks() -> int:
    """启动时恢复人物谱任务（对齐 worker.recover_and_start 的语义）：
    进程重启/reload 会丢掉内存里的 asyncio.Task，但 DB 仍是 running。
    与其在下次访问时判孤儿并报错，不如用持久化的 feedback 重新拉起任务续跑。"""
    conn = get_conn()
    style_column = "bible_style_name" if _supports_bible_style_name(conn) else "NULL AS bible_style_name"
    rows = conn.execute(
        f"SELECT id, bible_feedback, {style_column} FROM projects WHERE bible_status='running'"
    ).fetchall()
    resumed = 0
    for r in rows:
        pid = r["id"]
        if _bible_task_active(pid):
            continue
        feedback = r["bible_feedback"] or ""
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='character_bible' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (pid,),
        ).fetchone()
        recorder = None
        try:
            recorder = _new_bible_recorder(
                pid, trigger_type="resume", requested_by="system",
                parent_run_id=parent["id"] if parent else None,
                style_name=r["bible_style_name"],
            )
            task_registry.spawn(
                "bible",
                pid,
                _recorded_bible_task(
                    pid, feedback, recorder, trigger_full_refs=True,
                    style_name=r["bible_style_name"],
                ),
                project_id=pid,
            )
            resumed += 1
        except Exception as exc:  # one project must not block all startup recovery
            public = errors.record_and_format(
                exc,
                action="bible_recovery_spawn",
                context={"project_id": pid},
            )
            conn.execute(
                "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
                (f"人物谱自动恢复未能启动，原文和反馈已保留，可重新发起。{public}", pid),
            )
            conn.commit()
            if recorder is not None:
                try:
                    recorder.cancel("人物谱恢复任务未能启动", conn=None)
                except Exception:  # noqa: BLE001
                    pass
    return resumed


def recover_character_ref_tasks() -> int:
    """Resume portrait batches without changing their original refresh semantics."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, refs_target, refs_resume, refs_batch_started_at
           FROM projects p
           WHERE refs_status='running'
              OR EXISTS (
                  SELECT 1 FROM workflow_runs wr
                   WHERE wr.workflow_type='character_references'
                     AND wr.scope_type='project'
                     AND wr.scope_id=p.id
                     AND wr.status='PAUSED_EXTERNAL'
                     AND wr.recovered_by_run_id IS NULL
              )"""
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["id"]
        if _refs_task_active(project_id):
            continue
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='character_references' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        only_character, only_characters = _decode_refs_target(row["refs_target"])
        was_gap_resume = bool(row["refs_resume"])
        fresh_after = None if was_gap_resume else row["refs_batch_started_at"]
        try:
            if _start_refs_generation(
                project_id,
                only_character,
                only_characters=only_characters,
                resume=True,
                fresh_after=fresh_after,
                parent_run_id=parent["id"] if parent else None,
            ):
                resumed += 1
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(
                exc, action="refs_recovery_spawn", context={"project_id": project_id},
            )
            conn.execute(
                "UPDATE projects SET refs_status='failed',refs_error=? WHERE id=?",
                (f"定妆自动恢复未能启动，已完成素材仍保留，可重试缺口。{public}", project_id),
            )
            conn.commit()
    return resumed


def recover_scene_ref_tasks() -> int:
    """Resume persisted scene-asset work after a reload or process restart.

    Scene generation is idempotent: approved references are skipped, so an
    interrupted batch safely continues from the first missing scene instead of
    regenerating accepted assets.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT id,bible_json,bible_status,scene_refs_status,scene_refs_target "
        "FROM projects WHERE scene_refs_status='running' "
        "OR (scene_refs_status='idle' AND bible_status='ready')"
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["id"]
        # A recovered character-bible task will start a fresh scene pipeline
        # after committing its new Bible.  Starting from the old Bible here
        # would race it and could generate obsolete assets.
        if (row["bible_status"] == "running"
                or _scene_assets_task_active(project_id)):
            continue
        try:
            bible = json.loads(row["bible_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            bible = {}
        if bible.get("scenes"):
            if row["scene_refs_status"] != "running":
                continue
            parent = conn.execute(
                "SELECT id FROM workflow_runs WHERE workflow_type='scene_references' "
                "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
                "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            try:
                if _start_scene_refs_generation(
                    project_id,
                    _decode_scene_target(row["scene_refs_target"]),
                    resume=True,
                    parent_run_id=parent["id"] if parent else None,
                ):
                    resumed += 1
            except Exception as exc:  # noqa: BLE001
                public = errors.record_and_format(
                    exc, action="scene_refs_recovery_spawn",
                    context={"project_id": project_id},
                )
                conn.execute(
                    "UPDATE projects SET scene_refs_status='failed',scene_refs_error=? WHERE id=?",
                    (f"场景图自动恢复未能启动，已完成素材仍保留，可重试缺口。{public}", project_id),
                )
                conn.commit()
            continue
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='scene_bible' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        try:
            if _start_scene_bible_preparation(
                project_id,
                parent_run_id=parent["id"] if parent else None,
                requested_by="system",
                trigger_type="resume" if row["scene_refs_status"] == "running" else "automatic",
            ):
                resumed += 1
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(
                exc, action="scene_bible_recovery_spawn", context={"project_id": project_id},
            )
            conn.execute(
                "UPDATE projects SET scene_refs_status='failed',scene_refs_error=? WHERE id=?",
                (f"场景清单自动恢复未能启动，可重新发起。{public}", project_id),
            )
            conn.commit()
    return resumed

async def _bible_task(
    project_id: str,
    feedback: str = "",
    *,
    trigger_full_refs: bool = True,
    style_name: str | None = None,
):
    conn = get_conn()
    try:
        if style_name is None and _supports_bible_style_name(conn):
            style_row = conn.execute(
                "SELECT bible_style_name FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            style_name = style_row["bible_style_name"] if style_row else None
        style_name = _normalize_visual_style_name(style_name)
        style_prompt = _visual_style_prompt_or_default(style_name)
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)).fetchall())
        timeout_s = max(int(get_setting("bible_task_timeout_s") or BIBLE_TASK_TIMEOUT_S), 60)
        # 重新谱写时按角色名保留已有定妆照（重生圣经不应丢失一致性锚点）
        old_row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        old_style = None
        old_bible = None
        if old_row and old_row["bible_json"]:
            old_bible = json.loads(old_row["bible_json"])
        from app import model_registry
        from app.harness.text_provider_scope import stage_text_provider

        resolved_text_provider = None
        if "bible_text_provider" in _project_columns(conn):
            provider_row = conn.execute(
                "SELECT bible_text_provider FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            resolved_text_provider = model_registry.resolve_stage_text_provider(
                provider_row["bible_text_provider"] if provider_row else None
            )
        with stage_text_provider(resolved_text_provider):
            bible = await asyncio.wait_for(
                generate_bible(
                    chapters, feedback=feedback, previous_bible=old_bible,
                    project_id=project_id, visual_style_prompt=style_prompt,
                ),
                timeout=timeout_s,
            )
        if old_bible:
            old_style = (old_bible.get("world") or {}).get("visual_style_canonical")
            old_refs = {c.get("name"): c.get("ref_image_path")
                        for c in old_bible.get("characters", [])}
            for c in bible.characters:
                c.ref_image_path = old_refs.get(c.name) or None
        # 重谱后画风变化 → 旧画风定妆照与旧视频全部作废（否则图像信号会把新画风拉回旧画风）
        if old_style and bible.world.visual_style_canonical != old_style:
            _purge_for_style_change(project_id, bible)
        residual = list(getattr(bible, "residual_errors", []) or [])
        artifact_id = getattr(bible, "evidence_artifact_id", None)
        bible_status = "warning" if residual else "ready"
        bible_error = (
            "人物谱存在阻塞问题，允许人工修订，但不会进入下游：" + "；".join(residual[:8])
            if residual else None
        )
        # A few unit tests intentionally use a minimal legacy schema.  Production
        # databases always receive the incremental migration in app.db, while the
        # fallback keeps the stage function independently testable.
        project_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        final_project_status = "bible_ready"
        if "plan_status" in project_columns:
            plan_row = conn.execute(
                "SELECT plan_status FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if plan_row and plan_row["plan_status"] == "ready":
                final_project_status = "planned"
        if "bible_artifact_id" in project_columns:
            conn.execute(
                "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_status=?, "
                "bible_error=?, bible_artifact_id=?, status=? WHERE id=?",
                (
                    bible.model_dump_json(), bible_status, bible_error, artifact_id,
                    final_project_status if not residual else "created", project_id,
                ))
        else:
            conn.execute(
                "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_status=?, "
                "bible_error=?, status=? WHERE id=?",
                (
                    bible.model_dump_json(), bible_status, bible_error,
                    final_project_status if not residual else "created", project_id,
                ))
        conn.commit()
        if trigger_full_refs and not residual:
            try:
                _start_refs_generation(project_id, None)
            except Exception as exc:  # noqa: BLE001 bible remains deliverable
                public = errors.record_and_format(
                    exc, action="refs_spawn_after_bible", context={"project_id": project_id},
                )
                conn.execute(
                    "UPDATE projects SET refs_status='failed',refs_error=? WHERE id=?",
                    (f"人物谱已完成，但定妆任务未能启动，可直接重试定妆。{public}", project_id),
                )
                conn.commit()
            if "scene_refs_status" in project_columns:
                try:
                    _start_scene_bible_preparation(project_id)
                except Exception as exc:  # noqa: BLE001 bible remains deliverable
                    public = errors.record_and_format(
                        exc, action="scene_bible_spawn_after_bible",
                        context={"project_id": project_id},
                    )
                    conn.execute(
                        "UPDATE projects SET scene_refs_status='failed',scene_refs_error=? WHERE id=?",
                        (f"人物谱已完成，但场景设定未能启动，可在场景库重试。{public}", project_id),
                    )
                    conn.commit()
    except asyncio.TimeoutError:
        conn.execute(
            "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
            (f"人物谱解析/修复超时（超过 {timeout_s} 秒），请重新谱写。", project_id),
        )
        conn.commit()
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            raise
        row = conn.execute("SELECT bible_status FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["bible_status"] == "running":
            conn.execute(
                "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
                (BIBLE_INTERRUPTED_ERROR, project_id),
            )
            conn.commit()
        raise
    except (StageError, Exception) as exc:  # noqa: BLE001
        # 回滚必须在 errors.record_and_format() 之前（同一根因见
        # app.domain.storyboard_ops._storyboard_task 顶层 except 上方的大注释：
        # app.db.insert_error_log 在这同一个 task 缓存连接上落一条 error_logs 行
        # 并 conn.commit()，谁先调用谁就先把此刻挂起的写入定型）。这条 try 里画风
        # 变更时会走到 _purge_for_style_change → worker.purge_project_video_
        # artifacts，后者对全项目逐镜头 DELETE shot_versions/shot_scenes/jobs、
        # 逐集回退状态，整段过程故意不提交，只在处理完全部镜头后 commit 一次；
        # 中途任何一步失败（文件 I/O、约束冲突等）都会把尚未提交的部分 DELETE
        # 留在这个连接上。这里如果先记日志再回滚，日志落库的隐式 commit 会把这份
        # 半成品（部分镜头的视频记录已删、其余镜头未处理）直接定型进库，且波及的
        # 是整个项目而不止一集——这正是本文件同类 purge 调用（_refs_task 的
        # errors.record_and_format 同理）必须先回滚的原因。回滚只丢弃这次失败尝试
        # 自己产生的未提交写入，不影响更早已经各自 commit 过的检查点。
        if conn.in_transaction:
            conn.rollback()
        public = errors.record_and_format(exc, action="bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?", (public, project_id))
        conn.commit()


def _new_bible_recorder(
    project_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
    style_name: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    chapters = rows_to_dicts(conn.execute(
        "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)
    ).fetchall())
    if style_name is None and _supports_bible_style_name(conn):
        row = conn.execute("SELECT bible_style_name FROM projects WHERE id=?", (project_id,)).fetchone()
        style_name = row["bible_style_name"] if row else None
    style_name = _normalize_visual_style_name(style_name)
    project = conn.execute(
        "SELECT bible_version, bible_feedback FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    return WorkflowRecorder.create(
        workflow_type="character_bible",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(
            chapters, project["bible_version"] if project else 0,
            project["bible_feedback"] if project else None, style_name,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={"max_iterations": 4, "warning_blocks_downstream": True},
        config_snapshot={"style_name": style_name},
        parent_run_id=parent_run_id,
    )


async def _recorded_bible_task(
    project_id: str,
    feedback: str,
    recorder: WorkflowRecorder,
    *,
    trigger_full_refs: bool,
    style_name: str | None = None,
) -> None:
    recorder.start()
    try:
        conn = get_conn()
        chapters = rows_to_dicts(conn.execute(
            "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)
        ).fetchall())
        context = ContextPack(goal="生成可追溯人物圣经")
        context.add_text("chapters", "\n\n".join(ch["content"] for ch in chapters), limit=60000)
        await recorder.step(
            "character_bible",
            lambda: _bible_task(
                project_id, feedback, trigger_full_refs=trigger_full_refs,
                style_name=style_name,
            ),
            contract_key="character_bible",
            agent_name="character_bible",
            context_manifest=context.manifest(),
        )
        row = conn.execute("SELECT bible_status, bible_error FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["bible_status"] == "ready":
            recorder.succeed("人物谱已通过确定性门禁", conn=None)
        elif row and row["bible_status"] == "warning":
            recorder.partial(row["bible_error"] or "人物谱需要人工修订", conn=None)
        else:
            recorder.fail(RuntimeError(row["bible_error"] if row else "人物谱生成失败"), conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，人物谱任务等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:
        recorder.fail(exc, conn=None)
        raise


async def _start_bible_core(
    project_id: str,
    feedback: str,
    *,
    confirm: bool = False,
    quote_id: str | None = None,
    require_quote_id: bool = False,
    style_name: str | None = None,
) -> dict:
    """启动人物谱生成的领域逻辑，供 REST 路由与 ``bible.generate`` Command Handler 共用。"""
    p = _project_or_404(project_id)
    _require_harness_engine(project_id)
    if p["bible_status"] == "running" and _bible_task_active(project_id):
        raise HTTPException(409, "角色圣经正在生成中")
    if p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中，请先停止后再重生人物谱")
    feedback = feedback.strip()
    if len(feedback) > 2000:
        raise HTTPException(400, "打回要求过长，请控制在 2000 字以内")
    style_name = _normalize_visual_style_name(style_name)
    precheck = _compute_bible_generate_precheck(project_id, style_name=style_name)
    if not confirm:
        raise _payment_confirm_required(precheck)
    quote_row = _validate_payment_quote(project_id, quote_id, precheck)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "task_id": quote_row["consumed_task_id"], "run_id": quote_row["consumed_run_id"],
        }
    conn = get_conn()
    # 持久化 feedback：进程重启后 recover_bible_tasks 能用相同入参续跑，而非中断报错
    if _supports_bible_style_name(conn):
        conn.execute(
            "UPDATE projects SET bible_status='running', bible_error=NULL, "
            "bible_feedback=?, bible_style_name=? WHERE id=?",
            (feedback, style_name, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET bible_status='running', bible_error=NULL, bible_feedback=? WHERE id=?",
            (feedback, project_id),
        )
    conn.commit()
    recorder = None
    try:
        recorder = _new_bible_recorder(project_id, style_name=style_name)
        task_registry.spawn(
            "bible",
            project_id,
            _recorded_bible_task(
                project_id, feedback, recorder, trigger_full_refs=True,
                style_name=style_name,
            ),
            project_id=project_id,
        )
    except Exception as exc:
        if _supports_bible_style_name(conn):
            conn.execute(
                "UPDATE projects SET bible_status=?, bible_error=?, "
                "bible_feedback=?, bible_style_name=? WHERE id=?",
                (
                    p["bible_status"],
                    p["bible_error"],
                    p.get("bible_feedback"),
                    p.get("bible_style_name"),
                    project_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE projects SET bible_status=?, bible_error=?, bible_feedback=? WHERE id=?",
                (
                    p["bible_status"],
                    p["bible_error"],
                    p.get("bible_feedback"),
                    project_id,
                ),
            )
        conn.commit()
        if recorder is not None:
            try:
                recorder.cancel("人物谱任务未能启动，项目状态已回滚", conn=None)
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "BIBLE_START_FAILED",
            "message": "人物谱任务未能启动，项目原状态和费用凭证均已保留，请重试",
            "action": "retry_generate",
        }) from exc
    _consume_payment_quote(
        str(quote_id), task_id=f"bible:{project_id}", run_id=recorder.run_id,
    )
    return {"status": "running", "task_id": f"bible:{project_id}", "run_id": recorder.run_id}


@router.post("/projects/{project_id}/bible")
async def start_bible(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, respond_ui

    payload = _as_body_dict(body)
    feedback = str(payload.get("feedback") or "")
    result = await dispatch(
        "bible.generate",
        {
            "project_id": project_id,
            "feedback": feedback,
            "confirm": payload.get("confirm") is True,
            "quote_id": payload.get("quote_id"),
            "require_quote_id": True,
            "style_name": payload.get("style_name"),
            "idempotency_key": payload.get("idempotency_key") or payload.get("quote_id"),
        },
        initiator="ui",
    )
    return respond_ui(result)


async def _cancel_bible_core(project_id: str) -> dict:
    """停止人物谱生成的领域逻辑，供 REST 路由与 ``bible.cancel`` Command Handler 共用。
    若人物谱尚未完成，停止后不会继续触发后续定妆照任务。"""
    p = _project_or_404(project_id)
    stopped = await task_registry.cancel_and_wait("bible", project_id)
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET bible_status='idle', bible_error=NULL, bible_feedback=NULL WHERE id=?",
        (project_id,),
    )
    conn.commit()
    was_running = p["bible_status"] == "running"
    return {"stopped": stopped or was_running}


@router.post("/projects/{project_id}/bible/cancel")
async def cancel_bible(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("bible.cancel", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)


def _purge_for_style_change(project_id: str, instance: "Bible") -> dict:
    """画风变更的连锁失效：清理全项目旧画风视频产物，并作废旧画风定妆照
    （旧定妆照/旧尾帧是比文字 prompt 更强的画风信号，残留会把新画风拉回旧画风）。"""
    purged = worker.purge_project_video_artifacts(project_id)
    refs_cleared = 0
    for c in instance.characters:
        if c.ref_image_path:
            try:
                Path(c.ref_image_path).unlink()
            except OSError:
                pass
            c.ref_image_path = None
            refs_cleared += 1
    # 画风变更 → 旧画风场景图同样是强画风信号，连带作废（落盘文件 + 分段表），并清空 bible.scenes 的图路径。
    scene_refs_cleared = 0
    for sc in getattr(instance, "scenes", None) or []:
        if sc.ref_image_path:
            try:
                Path(sc.ref_image_path).unlink()
            except OSError:
                pass
            sc.ref_image_path = None
            scene_refs_cleared += 1
    conn = get_conn()
    # 画风变更 → 旧画风的分段定妆照全部作废，重新定妆后由分镜阶段按集反应式重建分段。
    conn.execute("DELETE FROM character_portraits WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM scene_references WHERE project_id=?", (project_id,))
    conn.execute("UPDATE projects SET refs_status='idle', scene_refs_status='idle' WHERE id=?", (project_id,))
    conn.commit()
    return {**purged, "refs_cleared": refs_cleared, "scene_refs_cleared": scene_refs_cleared}


def _parse_bible_write_body(body: dict) -> tuple[dict, object, bool, str | None]:
    """拆出 bible 正文、expected_version、confirm 标志与影响预检指纹。"""
    expected_version = body.get("expected_version")
    confirm = body.get("confirm") is True
    impact_fp = body.get("impact_preview_fingerprint")
    if "bible" in body and isinstance(body.get("bible"), dict):
        bible_body = dict(body["bible"])
    else:
        skip = {
            "expected_version", "confirm", "impact_preview_fingerprint",
            "quote_id", "dry_run",
        }
        bible_body = {k: v for k, v in body.items() if k not in skip}
    if "expected_version" in bible_body:
        expected_version = bible_body.pop("expected_version", expected_version)
    if "confirm" in bible_body:
        confirm = bible_body.pop("confirm") is True or confirm
    if "impact_preview_fingerprint" in bible_body:
        impact_fp = bible_body.pop("impact_preview_fingerprint", impact_fp)
    return bible_body, expected_version, confirm, impact_fp


def _bible_conflict_detail(p: dict, expected_version) -> dict:
    server_bible = None
    if p.get("bible_json"):
        try:
            server_bible = json.loads(p["bible_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            server_bible = None
    return {
        "code": "BIBLE_VERSION_CONFLICT",
        "message": (
            f"人物谱版本冲突：当前版本 {p.get('bible_version')}，"
            f"请求基于 {expected_version}，请刷新后重试"
        ),
        "current_version": int(p.get("bible_version") or 0),
        "expected_version": expected_version,
        "server_bible": server_bible,
        "character_names": [
            c.get("name") for c in (server_bible or {}).get("characters", []) if c.get("name")
        ],
    }


def _classify_bible_changes(old_bible: dict | None, new_bible: dict) -> list[str]:
    """区分仅文字 / 角色外观 / 全局画风变更，供定稿影响预检展示。"""
    changes: list[str] = []
    old = old_bible or {}
    old_style = (old.get("world") or {}).get("visual_style_canonical")
    new_style = (new_bible.get("world") or {}).get("visual_style_canonical")
    if old_style and new_style and old_style != new_style:
        changes.append("global_style")
    old_chars = {c.get("name"): c for c in old.get("characters", []) if c.get("name")}
    new_chars = {c.get("name"): c for c in new_bible.get("characters", []) if c.get("name")}
    appearance_changed = False
    text_changed = False
    if set(old_chars) != set(new_chars):
        text_changed = True
    for name, nc in new_chars.items():
        oc = old_chars.get(name) or {}
        if (oc.get("appearance_canonical") or "") != (nc.get("appearance_canonical") or ""):
            appearance_changed = True
        for field in ("personality", "speech_style", "role", "portrait_prompt_override"):
            if (oc.get(field) or "") != (nc.get(field) or ""):
                text_changed = True
        if (oc.get("relationships") or []) != (nc.get("relationships") or []):
            text_changed = True
    if appearance_changed:
        changes.append("character_appearance")
    if text_changed and "character_appearance" not in changes:
        changes.append("text_only")
    elif text_changed:
        changes.append("text_fields")
    if not changes:
        changes.append("text_only")
    return changes


def _artifact_type_counts(artifact_ids: list[str]) -> dict[str, int]:
    if not artifact_ids:
        return {}
    conn = get_conn()
    counts: dict[str, int] = {}
    for i in range(0, len(artifact_ids), 400):
        chunk = artifact_ids[i:i + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT type, COUNT(*) AS c FROM artifacts WHERE id IN ({placeholders}) GROUP BY type",
            chunk,
        ).fetchall()
        for row in rows:
            counts[row["type"]] = counts.get(row["type"], 0) + int(row["c"])
    return counts


def compute_bible_impact_preview(
    project_id: str,
    bible_body: dict,
    *,
    expected_version=None,
) -> dict:
    """定稿前只读影响预检：不写库、不失效下游。"""
    from app.config import IMAGE_PRICE_PER_UNIT
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    p = _project_or_404(project_id)
    current_version = int(p.get("bible_version") or 0)
    if expected_version is not None and int(expected_version) != current_version:
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))

    instance, errors = schema_errors(Bible, bible_body)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    v_errors = validate_bible(instance)
    if v_errors:
        raise HTTPException(422, "；".join(v_errors))

    old_bible = json.loads(p["bible_json"]) if p.get("bible_json") else None
    new_bible = instance.model_dump(mode="json")
    change_types = _classify_bible_changes(old_bible, new_bible)
    style_changed = "global_style" in change_types
    previous_artifact_id = p.get("bible_artifact_id")
    stale_ids = (
        evidence_repository.list_descendants(previous_artifact_id)
        if previous_artifact_id else []
    )
    by_type = _artifact_type_counts(stale_ids)
    conn = get_conn()
    stale_assets: list[dict] = []
    if stale_ids:
        for i in range(0, min(len(stale_ids), 100), 400):
            chunk = stale_ids[i:i + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT id, type, status, scope_type, scope_id FROM artifacts WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            found = {row["id"]: dict(row) for row in rows}
            stale_assets.extend(found[asset_id] for asset_id in chunk if asset_id in found)
    portraits = conn.execute(
        "SELECT COUNT(*) AS c FROM character_portraits WHERE project_id=?", (project_id,)
    ).fetchone()["c"]
    scenes = conn.execute(
        "SELECT COUNT(*) AS c FROM scene_references WHERE project_id=?", (project_id,)
    ).fetchone()["c"]
    char_count = len(instance.characters)
    views_per = len(CHARACTER_REQUIRED_VIEWS)
    rebuild_images = 0
    if style_changed:
        rebuild_images = char_count * views_per + int(scenes or 0) * 2
    elif "character_appearance" in change_types:
        old_chars = {
            c.get("name"): c for c in (old_bible or {}).get("characters", []) if c.get("name")
        }
        affected = 0
        for c in new_bible.get("characters", []):
            name = c.get("name")
            oc = old_chars.get(name) or {}
            if (oc.get("appearance_canonical") or "") != (c.get("appearance_canonical") or ""):
                affected += 1
        rebuild_images = affected * views_per
    unit = float(IMAGE_PRICE_PER_UNIT)
    estimated = round(rebuild_images * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed_at = now()
    fingerprint_payload = {
        "project_id": project_id,
        "bible_version": current_version,
        "bible_artifact_id": previous_artifact_id,
        "change_types": change_types,
        "stale_descendant_ids": stale_ids,
        "portraits": int(portraits or 0),
        "scenes": int(scenes or 0),
        "rebuild_images": rebuild_images,
    }
    preview_fp = fingerprint(fingerprint_payload)
    return {
        "project_id": project_id,
        "bible_version": current_version,
        "computed_at": computed_at,
        "fingerprint": preview_fp,
        "change_types": change_types,
        "style_changed": style_changed,
        "stale_descendant_ids": stale_ids,
        "stale_assets": stale_assets,
        "stale_assets_truncated": len(stale_ids) > 100,
        "stale_count": len(stale_ids),
        "by_artifact_type": by_type,
        "paid_assets": {
            "character_portraits": int(portraits or 0),
            "scene_references": int(scenes or 0),
        },
        "rebuild": {
            "image_count": rebuild_images,
            "unit_price_cny": unit,
            "estimated_cost_cny": estimated,
            "max_retry_budget_cny": max_retry,
            "note": "费用来自服务端口径；实际生成以任务账单为准",
        },
        "requires_reconfirm": bool(stale_ids),
        "paid_media_invalidated": bool(style_changed or stale_ids),
        "old_asset_policy": "定稿后下游证据标记失效；画风变更会作废旧定妆/场景图",
    }


def compute_refs_cost_precheck(
    project_id: str,
    *,
    character: str | None = None,
    characters: list[str] | None = None,
    resume: bool = False,
    view_role: str | None = None,
) -> dict:
    """人物定妆/单视角付费预检（只读）。"""
    from app.config import IMAGE_PRICE_PER_UNIT
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    bible = json.loads(p["bible_json"])
    bible_characters = bible.get("characters") or []
    selected_names = _normalize_character_selection(characters)
    if character and selected_names and character not in selected_names:
        raise HTTPException(422, "character 与 characters 范围不一致")
    if character:
        bible_characters = [c for c in bible_characters if c.get("name") == character]
        if not bible_characters:
            raise HTTPException(404, f"角色不存在：{character}")
    elif selected_names:
        by_name = {c.get("name"): c for c in bible_characters if c.get("name")}
        missing = [name for name in selected_names if name not in by_name]
        if missing:
            raise HTTPException(404, f"角色不存在：{missing[0]}")
        bible_characters = [by_name[name] for name in selected_names]
    views_per = 1 if view_role else len(CHARACTER_REQUIRED_VIEWS)
    conn = get_conn()
    missing_roles: list[dict] = []
    image_count = 0
    if view_role:
        image_count = 1
        missing_roles.append({
            "character": character, "view_role": view_role, "reason": "单视角重做",
        })
    elif resume:
        for c in bible_characters:
            name = c.get("name")
            row = conn.execute(
                """SELECT id, pack_status FROM character_portraits
                   WHERE project_id=? AND character_name=? AND ep_end IS NULL
                   ORDER BY ep_start DESC LIMIT 1""",
                (project_id, name),
            ).fetchone()
            if not row or row["pack_status"] not in (None, "ready"):
                image_count += views_per
                missing_roles.append({
                    "character": name, "views": list(CHARACTER_REQUIRED_VIEWS),
                    "reason": "缺包或未通过",
                })
                continue
            view_rows = conn.execute(
                "SELECT view_role, status, image_path FROM character_portrait_views WHERE portrait_id=?",
                (row["id"],),
            ).fetchall()
            have = {
                v["view_role"] for v in view_rows
                if v["status"] == "ready" and v["image_path"]
            }
            need = [r for r in CHARACTER_REQUIRED_VIEWS if r not in have]
            if need:
                image_count += len(need)
                missing_roles.append({
                    "character": name, "views": need, "reason": "缺失视角",
                })
    else:
        image_count = len(bible_characters) * views_per
        for c in bible_characters:
            missing_roles.append({
                "character": c.get("name"),
                "views": list(CHARACTER_REQUIRED_VIEWS) if not view_role else [view_role],
                "reason": "整包生成",
            })
    unit = float(IMAGE_PRICE_PER_UNIT)
    estimated = round(image_count * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed_at = now()
    scope_fingerprint = fingerprint({
        "project_id": project_id,
        "character": character,
        "characters": selected_names,
        "resume": resume,
        "view_role": view_role,
        "image_count": image_count,
        "unit": unit,
        "bible_version": p.get("bible_version"),
    })
    return {
        "quote_id": scope_fingerprint,
        "scope_fingerprint": scope_fingerprint,
        "computed_at": computed_at,
        "quote_expires_at": computed_at + 300,
        "project_id": project_id,
        "action": (
            "regenerate_view" if view_role
            else ("resume_missing" if resume else ("regenerate_pack" if character else "generate_all"))
        ),
        "character": character,
        "characters": selected_names,
        "view_role": view_role,
        "character_count": len(bible_characters),
        "views_per_character": views_per,
        "image_count": image_count,
        "unit_price_cny": unit,
        "estimated_cost_cny": estimated,
        "max_retry_budget_cny": max_retry,
        "budget_cap_cny": max_retry,
        "scope": missing_roles,
        "old_asset_policy": (
            "已落盘且可读取的视角保留；技术失败不替换当前采用包"
            if resume else
            "使用最新角色设定与全局画风生成；新包三视角文件齐全并可读取后替换旧包，质量评分只作提示"
        ),
        "idempotency_hint": "同一 quote_id 重复确认不会扩大范围；服务端仍做最终校验",
        "stop_policy": "可停止；已扣费步骤不退款，已完成成品保留",
    }


@router.post("/projects/{project_id}/bible/impact-preview")
async def bible_impact_preview(project_id: str, body: dict):
    """定稿人物谱前的只读影响预检。"""
    bible_body, expected_version, _, _ = _parse_bible_write_body(body or {})
    return compute_bible_impact_preview(
        project_id, bible_body, expected_version=expected_version,
    )


@router.post("/projects/{project_id}/refs/precheck")
async def refs_cost_precheck(project_id: str, body: dict | None = None):
    """定妆照/造型包付费预检。"""
    payload = body or {}
    return _issue_payment_quote(compute_refs_cost_precheck(
        project_id,
        character=payload.get("character"),
        characters=_normalize_character_selection(payload.get("characters")),
        resume=bool(payload.get("resume", False)),
        view_role=payload.get("view_role"),
    ))


def _compute_bible_generate_precheck(project_id: str, *, style_name: str | None = None) -> dict:
    """计算首次人物谱+定妆范围；不签发可执行凭证。"""
    from app.config import IMAGE_PRICE_PER_UNIT
    from app.multiview import CHARACTER_REQUIRED_VIEWS
    from app.stages import BIBLE_MUST_COVER_MAX

    style_name = _normalize_visual_style_name(style_name)
    p = _project_or_404(project_id)
    unit = float(IMAGE_PRICE_PER_UNIT)
    views_per = len(CHARACTER_REQUIRED_VIEWS)
    # 首版谱写按必收名单上限估算；若已有 bible 则用真实角色数
    if p.get("bible_json"):
        bible = json.loads(p["bible_json"])
        chars = bible.get("characters") or []
        char_count = len(chars)
        names = [c.get("name") for c in chars if c.get("name")]
        estimate_note = "基于当前人物谱角色数"
    else:
        char_count = BIBLE_MUST_COVER_MAX
        names = []
        estimate_note = (
            f"尚无人物谱，按首版必收名单上限 {BIBLE_MUST_COVER_MAX} 角色估算；"
            "谱写完成后按真实角色数出图"
        )
    image_count = char_count * views_per
    estimated = round(image_count * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed_at = now()
    scope_fingerprint = fingerprint({
        "project_id": project_id,
        "action": "generate_bible_and_refs",
        "character_count": char_count,
        "image_count": image_count,
        "unit": unit,
        "bible_version": p.get("bible_version"),
        "style_name": style_name,
    })
    return {
        "quote_id": scope_fingerprint,
        "scope_fingerprint": scope_fingerprint,
        "computed_at": computed_at,
        "quote_expires_at": computed_at + 300,
        "project_id": project_id,
        "action": "generate_bible_and_refs",
        "style_name": style_name,
        "character_count": char_count,
        "character_names": names,
        "views_per_character": views_per,
        "image_count": image_count,
        "unit_price_cny": unit,
        "estimated_cost_cny": estimated,
        "max_retry_budget_cny": max_retry,
        "budget_cap_cny": max_retry,
        "estimated_duration_min": [max(3, char_count), max(8, char_count * 3)],
        "estimate_note": estimate_note,
        "old_asset_policy": "停止后保留已落盘成品；未开始项可稍后补齐",
        "stop_policy": "可按阶段停止谱写或定妆；已扣费步骤不退款",
        "scope": [
            {"character": n or f"角色{i+1}", "views": list(CHARACTER_REQUIRED_VIEWS), "reason": "首次/重生"}
            for i, n in enumerate(names or [None] * char_count)
        ],
    }


@router.post("/projects/{project_id}/bible/generate-precheck")
async def bible_generate_precheck(project_id: str, body: dict | None = None):
    """签发首次生成人物谱+定妆的服务端费用凭证。"""
    payload = body or {}
    return _issue_payment_quote(_compute_bible_generate_precheck(
        project_id, style_name=payload.get("style_name"),
    ))


@router.get("/projects/{project_id}/bible/visual-styles")
async def bible_visual_styles(project_id: str):
    _project_or_404(project_id)
    return {
        "default": DEFAULT_VISUAL_STYLE_NAME,
        "items": visual_style_options(),
    }


@router.get("/projects/{project_id}/refs/gaps")
async def refs_gaps(project_id: str):
    """扫描定妆缺口：按角色/视角列出缺失原因。"""
    quote = _issue_payment_quote(compute_refs_cost_precheck(project_id, resume=True))
    return {
        "project_id": project_id,
        "missing_count": len(quote.get("scope") or []),
        "image_count": quote.get("image_count"),
        "items": quote.get("scope") or [],
        "precheck": quote,
    }


@router.get("/projects/{project_id}/refs/progress")
async def refs_progress(project_id: str):
    """定妆细粒度进度：完成/当前/缺失/失败分项。"""
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    p = _project_or_404(project_id)
    effective_refs_status = "running" if _refs_generation_busy(project_id) else p.get("refs_status")
    if not p.get("bible_json"):
        return {
            "project_id": project_id,
            "refs_status": effective_refs_status,
            "total": 0,
            "ready": 0,
            "failed": 0,
            "missing": 0,
            "items": [],
        }
    bible = json.loads(p["bible_json"])
    conn = get_conn()
    items = []
    ready = failed = missing = 0
    for c in bible.get("characters") or []:
        name = c.get("name")
        row = conn.execute(
            """SELECT id, pack_status FROM character_portraits
               WHERE project_id=? AND character_name=? AND ep_end IS NULL
               ORDER BY ep_start DESC LIMIT 1""",
            (project_id, name),
        ).fetchone()
        if not row:
            missing += 1
            items.append({"character": name, "status": "missing", "missing_views": list(CHARACTER_REQUIRED_VIEWS)})
            continue
        views = conn.execute(
            "SELECT view_role, status FROM character_portrait_views WHERE portrait_id=?",
            (row["id"],),
        ).fetchall()
        have = {v["view_role"] for v in views if v["status"] == "ready"}
        need = [r for r in CHARACTER_REQUIRED_VIEWS if r not in have]
        pack = row["pack_status"] or "unknown"
        if pack == "ready" and not need:
            ready += 1
            status = "ready"
        elif pack == "failed" or need:
            if pack == "failed":
                failed += 1
                status = "failed"
            else:
                missing += 1
                status = "missing"
        else:
            status = pack
        items.append({
            "character": name,
            "status": status,
            "pack_status": pack,
            "missing_views": need,
            "current": effective_refs_status == "running" and (
                p.get("refs_target") == name or not p.get("refs_target")
            ),
        })
    return {
        "project_id": project_id,
        "refs_status": effective_refs_status,
        "refs_target": p.get("refs_target"),
        "total": len(items),
        "ready": ready,
        "failed": failed,
        "missing": missing,
        "items": items,
        "updated_at": now(),
    }


@router.post("/projects/{project_id}/bible/draft")
async def save_bible_draft(project_id: str, body: dict):
    """保存人物谱草稿（不定稿、不失效下游、不升版本）。"""
    p = _project_or_404(project_id)
    expected_version = body.get("expected_version")
    if expected_version is not None and int(expected_version) != int(p.get("bible_version") or 0):
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))
    draft = body.get("bible") if isinstance(body.get("bible"), dict) else {
        k: v for k, v in (body or {}).items()
        if k not in {"expected_version", "confirm", "impact_preview_fingerprint"}
    }
    conn = get_conn()
    # 兼容旧库：无列时写入 bible_feedback 旁路字段不可行，使用独立列迁移
    try:
        conn.execute(
            "UPDATE projects SET bible_draft_json=?, bible_draft_updated_at=? WHERE id=?",
            (json.dumps(draft, ensure_ascii=False), now(), project_id),
        )
    except Exception:
        conn.execute("ALTER TABLE projects ADD COLUMN bible_draft_json TEXT")
        conn.execute("ALTER TABLE projects ADD COLUMN bible_draft_updated_at REAL")
        conn.execute(
            "UPDATE projects SET bible_draft_json=?, bible_draft_updated_at=? WHERE id=?",
            (json.dumps(draft, ensure_ascii=False), now(), project_id),
        )
    conn.commit()
    return {
        "saved": True,
        "draft": True,
        "bible_version": int(p.get("bible_version") or 0),
        "updated_at": now(),
    }


@router.get("/projects/{project_id}/bible/draft")
async def get_bible_draft(project_id: str):
    p = _project_or_404(project_id)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT bible_draft_json, bible_draft_updated_at, bible_version FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
    except Exception:
        return {"draft": None, "bible_version": int(p.get("bible_version") or 0)}
    draft = None
    if row and row["bible_draft_json"]:
        try:
            draft = json.loads(row["bible_draft_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            draft = None
    return {
        "draft": draft,
        "updated_at": row["bible_draft_updated_at"] if row else None,
        "bible_version": int((row["bible_version"] if row else p.get("bible_version")) or 0),
    }


def _auto_change_payload(item: dict) -> dict:
    payload = item.get("payload")
    return payload if isinstance(payload, dict) else {}


def _auto_change_character_card(item: dict) -> dict | None:
    payload = _auto_change_payload(item)
    candidates = [
        payload.get("character"),
        payload.get("character_card"),
        item.get("character_card"),
    ]
    if isinstance(item.get("character"), dict):
        candidates.append(item.get("character"))
    if item.get("appearance_canonical") or payload.get("appearance_canonical"):
        candidates.append({**payload, **item})
    for card in candidates:
        if not isinstance(card, dict):
            continue
        name = (
            card.get("name")
            or payload.get("character_name")
            or item.get("character_name")
            or (item.get("character") if isinstance(item.get("character"), str) else None)
        )
        if not name:
            continue
        merged = dict(card)
        merged["name"] = name
        merged.setdefault("role", payload.get("role") or item.get("role") or "重要配角")
        merged.setdefault("appearance_canonical", payload.get("appearance_canonical") or item.get("appearance_canonical") or "")
        merged.setdefault("personality", payload.get("personality") or item.get("personality") or "")
        merged.setdefault("speech_style", payload.get("speech_style") or item.get("speech_style") or "")
        merged.setdefault("relationships", payload.get("relationships") or item.get("relationships") or [])
        return merged
    return None


def _auto_change_portrait_id(change_id: str, item: dict | None = None) -> str | None:
    if change_id.startswith("portrait:"):
        return change_id.split(":", 1)[1]
    payload = _auto_change_payload(item or {})
    return (
        payload.get("portrait_id")
        or payload.get("previous_portrait_id")
        or payload.get("base_portrait_id")
        or (item or {}).get("portrait_id")
        or (item or {}).get("previous_portrait_id")
        or (item or {}).get("base_portrait_id")
    )


@router.get("/projects/{project_id}/auto-changes")
async def list_auto_changes(project_id: str):
    """自动变更/待审队列（人物发现与漂移记录）。"""
    _project_or_404(project_id)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
    except Exception:
        return {"items": []}
    items = []
    if row and row["bible_auto_changes_json"]:
        try:
            items = json.loads(row["bible_auto_changes_json"]) or []
        except (TypeError, ValueError, json.JSONDecodeError):
            items = []
    # 同时从定妆 change_json 汇总漂移记录
    portraits = conn.execute(
        """SELECT id, character_name, ep_start, change_json, pack_status, created_at
           FROM character_portraits WHERE project_id=? AND change_json IS NOT NULL
           ORDER BY created_at DESC LIMIT 50""",
        (project_id,),
    ).fetchall()
    for r in portraits:
        try:
            change = json.loads(r["change_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            change = {}
        items.append({
            "id": f"portrait:{r['id']}",
            "kind": "appearance_drift",
            "status": change.get("review_status") or "recorded",
            "character": r["character_name"],
            "ep_start": r["ep_start"],
            "reason": change.get("reason"),
            "change_dimensions": change.get("change_dimensions") or [],
            "persistence": change.get("persistence"),
            "pack_status": r["pack_status"],
            "created_at": r["created_at"],
            "source": "portrait_change",
        })
    return {"items": items}


@router.post("/projects/{project_id}/auto-changes/{change_id}/decide")
async def decide_auto_change(project_id: str, change_id: str, body: dict | None = None):
    """批准/拒绝/回滚自动变更记录。"""
    project = _project_or_404(project_id)
    payload = body or {}
    decision = payload.get("decision") or "approve"
    if decision not in {"approve", "reject", "rollback", "merge"}:
        raise HTTPException(422, "decision 须为 approve/reject/rollback/merge")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        items = json.loads(row["bible_auto_changes_json"] or "[]") if row and row["bible_auto_changes_json"] else []
    except Exception:
        conn.execute("ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT")
        items = []
    found = False
    matched_item = None
    for item in items:
        if item.get("id") == change_id:
            item["status"] = decision
            item["decided_at"] = now()
            item["decision_reason"] = payload.get("reason") or ""
            if decision == "merge":
                item["merge_into_character"] = payload.get("merge_into_character")
                item["merge_into_scene"] = payload.get("merge_into_scene")
            if payload.get("ep_start") is not None:
                try:
                    item["ep_start"] = max(1, int(payload["ep_start"]))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(422, "ep_start 必须是正整数") from exc
            found = True
            matched_item = item
            break
    action_result: dict = {}
    if change_id.startswith("portrait:"):
        portrait_id = change_id.split(":", 1)[1]
        prow = conn.execute(
            "SELECT change_json, pack_status FROM character_portraits WHERE id=? AND project_id=?",
            (portrait_id, project_id),
        ).fetchone()
        if prow:
            try:
                change = json.loads(prow["change_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                change = {}
            change["review_status"] = decision
            change["decision_reason"] = payload.get("reason") or ""
            change["decided_at"] = now()
            if decision == "approve":
                change["review_status"] = "approved"
            conn.execute(
                "UPDATE character_portraits SET change_json=? WHERE id=?",
                (json.dumps(change, ensure_ascii=False), portrait_id),
            )
            if decision == "reject" and prow["pack_status"] != "ready":
                conn.execute(
                    "UPDATE character_portraits SET pack_status='rejected' WHERE id=?",
                    (portrait_id,),
                )
            found = True
            action_result["portrait_id"] = portrait_id
    if matched_item and decision == "approve":
        kind = matched_item.get("kind")
        if kind in {"new_character", "character_discovery", "new_bible_character"}:
            card = _auto_change_character_card(matched_item)
            if card:
                bible = json.loads(project["bible_json"] or '{"characters":[],"world":{"visual_style_canonical":""}}')
                if not any(c.get("name") == card.get("name") for c in bible.get("characters", [])):
                    bible.setdefault("characters", []).append(card)
                    instance, errors = schema_errors(Bible, bible)
                    if errors:
                        raise HTTPException(422, "；".join(errors))
                    revision = _commit_bible_revision(
                        project_id, project, instance, reason=f"批准新增角色：{card.get('name')}"
                    )
                    action_result.update({
                        "bible_version": revision["bible_version"],
                        "artifact_id": revision["artifact_id"],
                        "added_character": card.get("name"),
                    })
        elif kind == "appearance_drift":
            portrait_id = _auto_change_portrait_id(change_id, matched_item)
            if portrait_id:
                prow = conn.execute(
                    "SELECT change_json FROM character_portraits WHERE id=? AND project_id=?",
                    (portrait_id, project_id),
                ).fetchone()
                if prow:
                    change = _parse_json_value(prow["change_json"], {})
                    if not isinstance(change, dict):
                        change = {}
                    change["review_status"] = "approved"
                    change["decision_reason"] = payload.get("reason") or ""
                    change["decided_at"] = now()
                    conn.execute(
                        "UPDATE character_portraits SET change_json=? WHERE id=?",
                        (json.dumps(change, ensure_ascii=False), portrait_id),
                    )
                    found = True
                    action_result["portrait_id"] = portrait_id
        elif kind == "scene_discovery":
            scene = _auto_change_payload(matched_item).get("scene")
            if isinstance(scene, dict) and scene.get("name"):
                bible = json.loads(project["bible_json"] or '{}')
                if not any(item.get("name") == scene["name"] for item in bible.get("scenes", [])):
                    bible.setdefault("scenes", []).append(scene)
                    instance, validation_errors = schema_errors(Bible, bible)
                    if validation_errors:
                        raise HTTPException(422, "；".join(validation_errors))
                    conn.execute(
                        "UPDATE projects SET bible_json=?,bible_version=bible_version+1 WHERE id=?",
                        (instance.model_dump_json(), project_id),
                    )
                    action_result.update({
                        "added_scene": scene["name"], "requires_payment_confirmation": True,
                        "message": "场景锚点已批准入库；出图仍需在场景库完成费用预检",
                    })
        elif kind == "scene_state_change":
            scene_name = str(matched_item.get("scene") or "").strip()
            change_payload = _auto_change_payload(matched_item)
            new_canonical = str(change_payload.get("new_scene_canonical") or "").strip()
            ep_start = max(1, int(matched_item.get("ep_start") or 1))
            bible = json.loads(project["bible_json"] or '{}')
            target_scene = next(
                (item for item in bible.get("scenes", []) if item.get("name") == scene_name), None,
            )
            if not target_scene or not new_canonical:
                raise HTTPException(422, "场景状态变化缺少目标场景或新锚点")
            target_scene["pending_state_canonical"] = new_canonical
            target_scene["pending_state_ep_start"] = ep_start
            instance, validation_errors = schema_errors(Bible, bible)
            if validation_errors:
                raise HTTPException(422, "；".join(validation_errors))
            conn.execute(
                "UPDATE projects SET bible_json=?,bible_version=bible_version+1 WHERE id=?",
                (instance.model_dump_json(), project_id),
            )
            current_ref = _scene_current_row(conn, project_id, scene_name)
            if current_ref and "change_json" in current_ref.keys():
                ref_change = _parse_json_value(current_ref["change_json"], {}) or {}
                ref_change.update({
                    "pending_redraw": True, "pending_state_canonical": new_canonical,
                    "pending_state_ep_start": ep_start, "approved_change_id": change_id,
                    "approved_at": now(),
                })
                conn.execute(
                    "UPDATE scene_references SET change_json=? WHERE id=?",
                    (json.dumps(ref_change, ensure_ascii=False), current_ref["id"]),
                )
            action_result.update({
                "approved_scene_change": scene_name,
                "requires_payment_confirmation": True,
                "pending_state_ep_start": ep_start,
                "message": "状态变化锚点已保存为待重绘版本；仍需在场景库完成费用预检",
            })
    elif matched_item and decision == "rollback":
        portrait_id = _auto_change_portrait_id(change_id, matched_item)
        if portrait_id:
            row = conn.execute(
                "SELECT * FROM character_portraits WHERE id=? AND project_id=?",
                (portrait_id, project_id),
            ).fetchone()
            if row:
                target_id = (
                    _auto_change_payload(matched_item).get("previous_portrait_id")
                    or _auto_change_payload(matched_item).get("base_portrait_id")
                    or matched_item.get("previous_portrait_id")
                    or matched_item.get("base_portrait_id")
                    or row["base_portrait_id"]
                )
                target = None
                if target_id:
                    target = conn.execute(
                        "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=?",
                        (target_id, project_id, row["character_name"]),
                    ).fetchone()
                if target is None:
                    target = conn.execute(
                        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? AND id<>? "
                        "AND (pack_status IS NULL OR pack_status='ready') ORDER BY created_at DESC LIMIT 1",
                        (project_id, row["character_name"], portrait_id),
                    ).fetchone()
                if target:
                    action_result.update(_set_current_portrait(
                        conn,
                        project_id,
                        row["character_name"],
                        target,
                        reason=payload.get("reason") or "自动变更回滚",
                        decision="rollback",
                    ))
                    found = True
    elif matched_item and decision == "merge":
        is_scene_change = str(matched_item.get("kind") or "").startswith("scene_")
        target_name = payload.get("merge_into_scene") if is_scene_change else payload.get("merge_into_character")
        if not target_name:
            raise HTTPException(422, "merge 需要明确合并目标")
        bible = json.loads(project["bible_json"] or '{"characters":[]}')
        collection = bible.get("scenes", []) if is_scene_change else bible.get("characters", [])
        if not any(c.get("name") == target_name for c in collection):
            raise HTTPException(422, f"合并目标不存在：{target_name}")
        matched_item["decision_reason"] = (
            (payload.get("reason") or "").strip()
            or f"合并到已有{'场景' if is_scene_change else '角色'}：{target_name}"
        )
        action_result["merge_into_scene" if is_scene_change else "merge_into_character"] = target_name
    if not found:
        raise HTTPException(404, "自动变更记录不存在")
    conn.execute(
        "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
        (json.dumps(items, ensure_ascii=False), project_id),
    )
    conn.commit()
    return {"ok": True, "change_id": change_id, "decision": decision, **action_result}





@router.put("/projects/{project_id}/bible")
async def edit_bible(project_id: str, body: dict):
    from app.capabilities.dispatch import ui_route

    bible_body, expected_version, confirm, impact_fp = _parse_bible_write_body(body or {})

    routed = await ui_route(
        "bible.update",
        {
            "project_id": project_id,
            "bible": bible_body,
            "expected_version": expected_version,
            "confirm": confirm,
            "impact_preview_fingerprint": impact_fp,
        },
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if expected_version is None:
        raise HTTPException(
            409,
            detail={
                "code": "EXPECTED_VERSION_REQUIRED",
                "message": "定稿人物谱必须携带 expected_version，以防止并发覆盖",
                "current_version": int(p.get("bible_version") or 0),
            },
        )
    if int(expected_version) != int(p.get("bible_version") or 0):
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))
    if not confirm:
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_CONFIRM_REQUIRED",
                "message": "必须先完成定稿影响预检并显式确认（confirm=true）",
            },
        )
    try:
        preview = compute_bible_impact_preview(
            project_id, bible_body, expected_version=expected_version,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_PREVIEW_UNAVAILABLE",
                "message": f"定稿影响预检失败，已阻止正式定稿：{exc}",
            },
        ) from exc
    if not impact_fp or impact_fp != preview.get("fingerprint"):
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_PREVIEW_STALE",
                "message": "影响预检已过期或缺失，请重新预检后再定稿",
                "preview": preview,
            },
        )

    instance, errors = schema_errors(Bible, bible_body)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    errors = validate_bible(instance)
    if errors:
        raise HTTPException(422, "；".join(errors))
    old_style = None
    if p["bible_json"]:
        old_style = (json.loads(p["bible_json"]).get("world") or {}).get("visual_style_canonical")
    style_changed = bool(old_style) and instance.world.visual_style_canonical != old_style
    purge_info = _purge_for_style_change(project_id, instance) if style_changed else None
    conn = get_conn()
    previous_artifact_id = p.get("bible_artifact_id")
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id=project_id,
        status="validated",
        trust_level="T4",
        content=instance.model_dump(mode="json"),
        parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
        contract_version="character-bible-1.0.0",
    ))
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="human",
        evaluator_name="bible_editor",
        evaluator_version="1.0.0",
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={"decision": "manual_edit", "style_changed": style_changed},
    )])
    stale_ids = evidence_repository.invalidate_descendants(
        previous_artifact_id,
        "人物谱已人工修订，需要重新复验下游产物",
        exclude_ids={artifact["id"]},
    ) if previous_artifact_id else []
    conn.execute(
        "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_artifact_id=?, "
        "bible_status='ready', bible_error=NULL WHERE id=?",
        (instance.model_dump_json(), artifact["id"], project_id),
    )
    conn.execute(
        "INSERT INTO gate_decisions(id, artifact_id, gate_key, decision, decided_by, reason, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (new_id("gate"), artifact["id"], "character_bible", "approve", "bible_editor", "人工修订并定稿", now()),
    )
    conn.commit()
    return {
        "bible_version_bumped": True,
        "style_changed": style_changed,
        "purged": purge_info,
        "artifact_id": artifact["id"],
        "bible_version": int(p.get("bible_version") or 0) + 1,
        "impact": {
            "stale_descendant_ids": stale_ids,
            "requires_reconfirm": bool(stale_ids),
            "paid_media_invalidated": bool(style_changed or stale_ids),
            "by_artifact_type": _artifact_type_counts(stale_ids),
            "change_types": preview.get("change_types"),
            "rebuild": preview.get("rebuild"),
        },
    }


def _commit_bible_revision(project_id: str, p: dict, instance: "Bible", *, reason: str) -> dict:
    previous_artifact_id = p.get("bible_artifact_id")
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="character_bible",
        scope_type="project",
        scope_id=project_id,
        status="validated",
        trust_level="T4",
        content=instance.model_dump(mode="json"),
        parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
        contract_version="character-bible-1.0.0",
    ))
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="human",
        evaluator_name="bible_editor",
        evaluator_version="1.0.0",
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={"decision": "manual_edit", "reason": reason},
    )])
    stale_ids = evidence_repository.invalidate_descendants(
        previous_artifact_id,
        "人物谱已人工修订，需要重新复验下游产物",
        exclude_ids={artifact["id"]},
    ) if previous_artifact_id else []
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_artifact_id=?, "
        "bible_status='ready', bible_error=NULL WHERE id=?",
        (instance.model_dump_json(), artifact["id"], project_id),
    )
    conn.execute(
        "INSERT INTO gate_decisions(id, artifact_id, gate_key, decision, decided_by, reason, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (new_id("gate"), artifact["id"], "character_bible", "approve", "bible_editor", reason, now()),
    )
    conn.commit()
    return {
        "artifact_id": artifact["id"],
        "stale_descendant_ids": stale_ids,
        "by_artifact_type": _artifact_type_counts(stale_ids),
        "bible_version": int(p.get("bible_version") or 0) + 1,
    }


@router.put("/projects/{project_id}/characters/{character_name}")
async def edit_character(project_id: str, character_name: str, body: dict):
    """角色级保存：只替换指定角色对象，并按 bible_version 做乐观并发控制。"""
    payload = body or {}
    expected_version = payload.get("expected_version")
    if expected_version is None:
        expected_version = (payload.get("character") or {}).get("expected_version") if isinstance(payload.get("character"), dict) else None
    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    if expected_version is None:
        raise HTTPException(
            409,
            detail={
                "code": "EXPECTED_VERSION_REQUIRED",
                "message": "保存角色必须携带 expected_version，以防止并发覆盖",
                "current_version": int(p.get("bible_version") or 0),
            },
        )
    if int(expected_version) != int(p.get("bible_version") or 0):
        raise HTTPException(409, detail=_bible_conflict_detail(p, expected_version))

    character_body = payload.get("character")
    if not isinstance(character_body, dict):
        raise HTTPException(422, "character 必须是角色对象")
    character_body = dict(character_body)
    character_body.setdefault("name", character_name)
    if character_body.get("name") != character_name:
        raise HTTPException(422, "角色 name 与路径 character_name 不一致")

    next_bible = json.loads(p["bible_json"])
    target_idx = next(
        (idx for idx, item in enumerate(next_bible.get("characters", [])) if item.get("name") == character_name),
        None,
    )
    if target_idx is None:
        raise HTTPException(404, f"角色不存在：{character_name}")
    next_bible["characters"][target_idx] = character_body

    instance, errors = schema_errors(Bible, next_bible)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    errors = validate_bible(instance)
    if errors:
        raise HTTPException(422, "；".join(errors))

    try:
        preview = compute_bible_impact_preview(
            project_id, next_bible, expected_version=expected_version,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_PREVIEW_UNAVAILABLE",
                "message": f"定稿影响预检失败，已阻止正式定稿：{exc}",
            },
        ) from exc
    impact_fp = payload.get("impact_preview_fingerprint")
    if payload.get("confirm") is not True or not impact_fp:
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_CONFIRM_REQUIRED",
                "message": "任何角色定稿变更都必须先完成影响预检并显式确认",
                "preview": preview,
            },
        )
    if impact_fp != preview.get("fingerprint"):
        raise HTTPException(
            409,
            detail={
                "code": "IMPACT_PREVIEW_STALE",
                "message": "影响预检已过期或缺失，请重新预检后再保存",
                "preview": preview,
            },
        )

    revision = _commit_bible_revision(project_id, p, instance, reason=f"人工保存角色：{character_name}")
    return {
        "saved": True,
        "character": character_name,
        "bible_version": revision["bible_version"],
        "artifact_id": revision["artifact_id"],
        "impact": {
            "change_types": preview.get("change_types"),
            "stale_descendant_ids": revision["stale_descendant_ids"],
            "by_artifact_type": revision["by_artifact_type"],
            "rebuild": preview.get("rebuild"),
        },
    }


@router.put("/projects/{project_id}/characters/{character_name}/portrait")
async def edit_portrait_prompt(project_id: str, character_name: str, body: dict):
    """更新单个角色的画像描述（定妆照生成词）。传空字符串/null 恢复为默认合成描述。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "portrait.update_prompt",
        {"project_id": project_id, "character": character_name, "prompt": (body.get("portrait_prompt") or "")},
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    prompt_text = (body.get("portrait_prompt") or "").strip()
    if prompt_text and not 10 <= len(prompt_text) <= 400:
        raise HTTPException(422, f"画像描述长度 {len(prompt_text)} 字，要求 10~400 字（留空则恢复默认）")
    bible = json.loads(p["bible_json"])
    target = next((c for c in bible.get("characters", []) if c.get("name") == character_name), None)
    if target is None:
        raise HTTPException(404, f"角色不存在：{character_name}")
    target["portrait_prompt_override"] = prompt_text or None
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(bible, ensure_ascii=False), project_id))
    conn.commit()
    return {"saved": True, "reset_to_default": not prompt_text}


def _portrait_views_for(conn, portrait_id: str) -> list[dict]:
    try:
        rows = rows_to_dicts(conn.execute(
            "SELECT * FROM character_portrait_views WHERE portrait_id=? "
            "ORDER BY view_role, selected DESC, (status='ready') DESC, created_at DESC",
            (portrait_id,),
        ).fetchall())
    except Exception:  # noqa: BLE001
        return []
    views: list[dict] = []
    seen_roles: set[str] = set()
    for row in rows:
        view_role = str(row.get("view_role") or "")
        if view_role in seen_roles:
            continue
        seen_roles.add(view_role)
        qa = _parse_json_value(row.get("qa_json"), {})
        views.append({
            "id": row.get("id"),
            "view_role": row.get("view_role"),
            "framing": row.get("framing"),
            "status": row.get("status"),
            "selected": bool(row.get("selected", 1)),
            "image_url": _media_url(row.get("image_path")),
            "qa": qa,
            "qa_overall": qa.get("overall") if isinstance(qa, dict) else None,
        })
    return views


def _portrait_candidate_payload(row, views: list[dict] | None = None) -> dict:
    group_qa = _parse_json_value(row["group_qa_json"] if "group_qa_json" in row.keys() else None, {})
    change = _parse_json_value(row["change_json"] if "change_json" in row.keys() else None, {})
    view_items = views if views is not None else _portrait_views_for(get_conn(), row["id"])
    return {
        "id": row["id"],
        "portrait_id": row["id"],
        "project_id": row["project_id"],
        "character_name": row["character_name"],
        "ep_start": row["ep_start"],
        "ep_end": row["ep_end"],
        "historical": int(row["ep_start"] or 0) <= 0 or row["ep_end"] is not None,
        "is_current": row["ep_end"] is None,
        "appearance": row["appearance"],
        "prompt": row["prompt"],
        "base_portrait_id": row["base_portrait_id"],
        "bible_version": row["bible_version"],
        "artifact_id": row["artifact_id"] if "artifact_id" in row.keys() else None,
        "pack_status": row["pack_status"] if "pack_status" in row.keys() else None,
        "group_qa": group_qa,
        "change": change,
        "image_url": _media_url(row["image_path"]),
        "views": view_items,
        "created_at": row["created_at"],
    }


def _portrait_artifact_candidate_payload(conn, row) -> dict:
    """Expose generated front-image candidates that failed before a pack existed.

    These artifacts are deliberately not adoptable as production portraits: they
    have not completed the three required views.  They remain visible so a user
    can inspect the actual image and QA evidence instead of seeing a misleading
    provider failure with an empty candidate list.
    """
    content = _parse_json_value(row["content_json"] if "content_json" in row.keys() else None, {})
    if not isinstance(content, dict):
        content = {}
    try:
        evaluations = conn.execute(
            "SELECT * FROM evaluations WHERE artifact_id=? ORDER BY created_at DESC",
            (row["id"],),
        ).fetchall()
    except Exception:  # noqa: BLE001 - compatibility with historical/minimal schemas
        evaluations = []

    model_eval = next((item for item in evaluations if item["evaluator_type"] == "model"), None)
    file_eval = next((item for item in evaluations if item["evaluator_type"] == "file"), None)
    evidence = _parse_json_value(
        model_eval["evidence_json"] if model_eval and "evidence_json" in model_eval.keys() else None,
        {},
    )
    qa = dict(evidence.get("qa") or {}) if isinstance(evidence, dict) else {}
    raw_issues = _parse_json_value(
        model_eval["issues_json"] if model_eval and "issues_json" in model_eval.keys() else None,
        [],
    )
    hard: list[str] = []
    warnings: list[str] = []
    for issue in raw_issues if isinstance(raw_issues, list) else []:
        if isinstance(issue, dict):
            message = str(issue.get("message") or issue.get("code") or "").strip()
            severity = str(issue.get("severity") or "").lower()
            if not message:
                continue
            if severity in {"blocker", "critical", "error"}:
                hard.append(message)
            else:
                warnings.append(message)
        elif str(issue).strip():
            warnings.append(str(issue).strip())
    if model_eval is not None:
        if model_eval["score"] is not None and not isinstance(qa.get("overall"), (int, float)):
            qa["overall"] = float(model_eval["score"]) / 100.0
        failed = not bool(model_eval["hard_gate_passed"])
        qa["status"] = "failed" if failed else str(model_eval["status"] or "unverified")
        if failed and not hard:
            hard.append("人物一致性 QA 未通过")
    elif file_eval is not None and not bool(file_eval["hard_gate_passed"]):
        qa["status"] = "failed"
        hard.append("图片技术校验未通过")
    else:
        qa.setdefault("status", "unverified")
    qa["hard_failures"] = list(dict.fromkeys([*(qa.get("hard_failures") or []), *hard]))
    qa["issues"] = list(dict.fromkeys([*(qa.get("issues") or []), *warnings]))

    return {
        "id": row["id"],
        "artifact_id": row["id"],
        "project_id": str(row["scope_id"] or "").split(":", 1)[0],
        "character_name": content.get("character_name"),
        "candidate_kind": "single_image",
        "attempt": content.get("attempt"),
        "status": "failed" if qa.get("status") == "failed" else "unverified",
        "pack_status": "not_built",
        "group_qa": qa,
        "qa": qa,
        "image_url": _media_url(row["file_path"] if "file_path" in row.keys() else None),
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
        "adoptable": False,
        "blocked_reason": (
            "该图只完成了正面单图阶段，尚未形成正面、3/4 面、侧面三视角包；"
            "可用于人工复核和重新生成，但不能直接标记为生产可用定妆包。"
        ),
    }


def _portrait_gate_lists(row, views: list[dict]) -> tuple[list[str], list[str]]:
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    group_qa = _parse_json_value(row["group_qa_json"] if "group_qa_json" in row.keys() else None, {})
    hard: list[str] = []
    soft: list[str] = []
    if isinstance(group_qa, dict):
        hard.extend(str(x) for x in (group_qa.get("hard_failures") or []) if str(x).strip())
        soft.extend(str(x) for x in (group_qa.get("issues") or []) if str(x).strip())
        if group_qa.get("status") and group_qa.get("status") != "ready":
            hard.append(f"group_qa_status={group_qa.get('status')}")
        hard.extend(str(x) for x in (group_qa.get("failed_views") or []) if str(x).strip())
        for view in group_qa.get("views") or []:
            if not isinstance(view, dict):
                continue
            hard.extend(str(x) for x in (view.get("hard_failures") or []) if str(x).strip())
            soft.extend(str(x) for x in (view.get("issues") or []) if str(x).strip())
    for view in views:
        qa = view.get("qa") if isinstance(view.get("qa"), dict) else {}
        hard.extend(str(x) for x in (qa.get("hard_failures") or []) if str(x).strip())
        soft.extend(str(x) for x in (qa.get("issues") or []) if str(x).strip())
        if view.get("status") != "ready" or not (view.get("image_path") or view.get("image_url")):
            hard.append(f"{view.get('view_role')}:status={view.get('status') or 'missing'}")
    ready_roles = {
        view.get("view_role") for view in views
        if view.get("view_role") in CHARACTER_REQUIRED_VIEWS
        and view.get("status") == "ready" and (view.get("image_path") or view.get("image_url"))
    }
    for missing_role in CHARACTER_REQUIRED_VIEWS:
        if missing_role not in ready_roles:
            hard.append(f"missing_required_view={missing_role}")
    pack_status = row["pack_status"] if "pack_status" in row.keys() else None
    if pack_status and pack_status != "ready":
        hard.append(f"pack_status={pack_status}")
    return list(dict.fromkeys(hard)), list(dict.fromkeys(soft))


def _set_current_portrait(
    conn,
    project_id: str,
    character_name: str,
    row,
    *,
    reason: str,
    decision: str,
) -> dict:
    stamp = now()
    target_start = int(row["ep_start"] or 1)
    adopted_start = target_start
    if target_start <= 0:
        # 初始包历史版本使用负数槽位避开 (project, character, ep_start)
        # 唯一约束；回滚时先将当前 ep=1 版本移入新历史槽，再恢复目标。
        minimum = conn.execute(
            "SELECT MIN(ep_start) AS value FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=0",
            (project_id, character_name),
        ).fetchone()
        history_start = int(minimum["value"] if minimum and minimum["value"] is not None else 0) - 1
        conn.execute(
            "UPDATE character_portraits SET ep_start=?, ep_end=0 "
            "WHERE project_id=? AND character_name=? AND id<>? AND ep_end IS NULL",
            (history_start, project_id, character_name, row["id"]),
        )
        adopted_start = 1
    else:
        ep_end = max(target_start - 1, 0)
        conn.execute(
            "UPDATE character_portraits SET ep_end=? "
            "WHERE project_id=? AND character_name=? AND id<>? AND ep_end IS NULL",
            (ep_end, project_id, character_name, row["id"]),
        )
    change = _parse_json_value(row["change_json"] if "change_json" in row.keys() else None, {})
    if not isinstance(change, dict):
        change = {}
    change.update({
        "review_status": decision,
        "adoption_reason": reason,
        "decided_at": stamp,
    })
    conn.execute(
        "UPDATE character_portraits SET ep_start=?, ep_end=NULL, change_json=? WHERE id=?",
        (adopted_start, json.dumps(change, ensure_ascii=False), row["id"]),
    )
    prow = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if prow and prow["bible_json"]:
        bible = json.loads(prow["bible_json"])
        for character in bible.get("characters", []):
            if character.get("name") == character_name:
                character["ref_image_path"] = row["image_path"]
                break
        conn.execute(
            "UPDATE projects SET bible_json=? WHERE id=?",
            (json.dumps(bible, ensure_ascii=False), project_id),
        )
    artifact_id = row["artifact_id"] if "artifact_id" in row.keys() else None
    if artifact_id:
        conn.execute(
            "INSERT INTO gate_decisions(id, artifact_id, gate_key, decision, decided_by, reason, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (new_id("gate"), artifact_id, "portrait_adoption", decision, "bible_editor", reason, stamp),
        )
    conn.commit()
    return {"portrait_id": row["id"], "character_name": character_name, "ep_start": adopted_start}


def _adopt_portrait_by_id(
    project_id: str,
    character_name: str,
    portrait_id: str,
    *,
    reason: str,
    bypass_soft: bool = False,
    decision: str = "approve",
) -> dict:
    _project_or_404(project_id)
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=?",
        (portrait_id, project_id, character_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "造型版本不存在")
    image_path = str(row["image_path"] or "").strip()
    if not image_path or not Path(image_path).is_file():
        raise HTTPException(409, {
            "code": "PORTRAIT_FILE_UNAVAILABLE",
            "message": "候选定妆主图文件不可用",
        })
    views = _portrait_views_for(conn, portrait_id)
    hard, soft = _portrait_gate_lists(row, views)
    del bypass_soft
    quality_warnings = list(dict.fromkeys([*hard, *soft]))
    result = _set_current_portrait(
        conn, project_id, character_name, row, reason=reason, decision=decision,
    )
    return {
        **result,
        "soft_warnings": quality_warnings,
        "gate_retry_exhausted": bool(hard),
        "candidate": _portrait_candidate_payload(row, views),
    }


@router.get("/projects/{project_id}/characters/{character_name}/portrait-candidates")
async def list_portrait_candidates(project_id: str, character_name: str):
    """列出角色定妆候选与历史包。"""
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "ORDER BY ep_start DESC, created_at DESC",
        (project_id, character_name),
    ).fetchall()
    portrait_items = [_portrait_candidate_payload(row) for row in rows]
    attached_artifact_ids = {
        str(row["artifact_id"]) for row in rows
        if "artifact_id" in row.keys() and row["artifact_id"]
    }
    try:
        artifact_rows = conn.execute(
            "SELECT * FROM artifacts WHERE type='character_portrait' "
            "AND scope_type='reference_asset' AND scope_id=? "
            "ORDER BY created_at DESC LIMIT 30",
            (f"{project_id}:{character_name}:1",),
        ).fetchall()
    except Exception:  # noqa: BLE001 - old databases may not have evidence tables
        artifact_rows = []
    raw_candidates = [
        _portrait_artifact_candidate_payload(conn, row)
        for row in artifact_rows if str(row["id"]) not in attached_artifact_ids
    ]
    return {
        "project_id": project_id,
        "character_name": character_name,
        "items": [*portrait_items, *raw_candidates],
    }


@router.post("/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/adopt")
async def adopt_portrait_candidate(
    project_id: str, character_name: str, portrait_id: str, body: dict | None = None,
):
    payload = body or {}
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(422, "采纳候选必须填写 reason")
    result = _adopt_portrait_by_id(
        project_id,
        character_name,
        portrait_id,
        reason=reason,
        bypass_soft=payload.get("bypass_soft") is True,
        decision="approve",
    )
    return {"adopted": True, **result}


@router.post("/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/rollback")
async def rollback_portrait_candidate(
    project_id: str, character_name: str, portrait_id: str, body: dict | None = None,
):
    payload = body or {}
    reason = str(payload.get("reason") or "回滚到上一可用定妆包").strip()
    _project_or_404(project_id)
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=?",
        (portrait_id, project_id, character_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "造型版本不存在")
    target = None
    if row["base_portrait_id"]:
        target = conn.execute(
            "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=? "
            "AND (pack_status IS NULL OR pack_status='ready')",
            (row["base_portrait_id"], project_id, character_name),
        ).fetchone()
    if target is None:
        target = conn.execute(
            "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? AND id<>? "
            "AND (pack_status IS NULL OR pack_status='ready') "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id, character_name, portrait_id),
        ).fetchone()
    if target is None:
        raise HTTPException(409, "没有可回滚的 ready 定妆包")
    result = _adopt_portrait_by_id(
        project_id,
        character_name,
        target["id"],
        reason=reason,
        bypass_soft=True,
        decision="rollback",
    )
    return {"rolled_back": True, "from_portrait_id": portrait_id, **result}


# ---------- 角色定妆照（人物跨集一致性） ----------
# 注：初始定妆在此生成（generate_refs，适用集 1~ 至今）；已有角色的外观漂移重绘已改为分镜阶段
# 按集反应式处理（见 portraits.ensure_cards_for_screenplay），不再有"每 20 集全量轮询"步骤。


async def _refs_task(
    project_id: str,
    only_character: str | None,
    *,
    only_characters: list[str] | None = None,
    resume: bool = False,
    fresh_after: float | None = None,
    operation_started_at: float | None = None,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
    recorder: WorkflowRecorder | None = None,
):
    from app.refs import generate_refs
    conn = get_conn()
    recorder = recorder or _new_refs_recorder(
        project_id, only_character, only_characters,
        resume=resume, fresh_after=fresh_after, parent_run_id=parent_run_id,
        requested_by=requested_by, trigger_type=trigger_type,
    )
    try:
        recorder.start()
        # 新包结构完整后才使旧定妆的下游产物失效；质量评分不参与采用资格。
        # 这样技术失败或中止不会破坏当前可用链路。
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if only_characters:
            names = only_characters
        elif only_character:
            names = [only_character]
        elif p and p["bible_json"]:
            names = [c["name"] for c in json.loads(p["bible_json"]).get("characters", [])]
        else:
            names = []
        await recorder.step(
            "character_references",
            lambda: generate_refs(
                project_id, only_character, only_characters=only_characters,
                resume=resume, fresh_after=fresh_after,
                operation_started_at=operation_started_at,
            ),
            agent_name="reference_asset_loop",
        )
        if not resume:
            worker.purge_character_video_artifacts(project_id, names)
        conn.execute(
            "UPDATE projects SET refs_status='ready', refs_error=NULL, refs_target=NULL, "
            "refs_batch_started_at=NULL WHERE id=?",
            (project_id,),
        )
        conn.commit()
        recorder.succeed("人物参考资产已生成且结构完整", conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，定妆任务等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:  # noqa: BLE001
        # 回滚必须在本 except 块的第一条语句做——不止要早于 errors.record_and_format()，
        # 还要早于上面这行 recorder.fail(exc)。WorkflowRecorder.fail() 内部调用
        # app.orchestration.state_machine.transition_run(..., conn=None)，后者
        # ``db = conn or get_conn()`` 取的是同一个 task 缓存连接，并且在
        # ``conn is None`` 时自己 db.commit()——跟 app.db.insert_error_log 是完全
        # 同一类隐式提交，谁先调用谁就先把此刻挂起的写入定型。这条 try 里
        # ``if not resume: worker.purge_character_video_artifacts(...)`` 对本项目
        # 命中角色的镜头逐条 DELETE shot_versions/shot_scenes/jobs、回退所属剧集
        # 状态，整段不提交，只在处理完全部镜头后 conn.commit() 一次；中途失败会把
        # 未提交的部分 DELETE 留在这个连接上。如果不把回滚提到 recorder.fail(exc)
        # 之前，这一行自己的隐式 commit 就会先把半成品定型进库——回滚只丢弃这次
        # 失败尝试自己产生的未提交写入，不影响更早已经各自 commit 过的检查点。
        if conn.in_transaction:
            conn.rollback()
        recorder.fail(exc, conn=None)
        public = errors.record_and_format(exc, action="refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET refs_status='failed', refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


@router.post("/projects/{project_id}/refs")
async def start_refs(project_id: str, body: dict | None = None):
    from app.capabilities.dispatch import ui_route
    payload = body or {}
    selected_names = _normalize_character_selection(payload.get("characters"))
    routed = await ui_route(
        "portrait.generate",
        {
            "project_id": project_id,
            "character": payload.get("character"),
            "characters": selected_names,
            "resume": bool(payload.get("resume", False)),
            "confirm": payload.get("confirm") is True,
            "quote_id": payload.get("quote_id"),
            "idempotency_key": payload.get("idempotency_key") or payload.get("quote_id"),
        },
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _refs_generation_busy(project_id) or p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中")
    only = payload.get("character")
    if only and selected_names and only not in selected_names:
        raise HTTPException(422, "character 与 characters 范围不一致")
    resume = bool(payload.get("resume", False))
    quote_character = only if not selected_names else None
    quote = compute_refs_cost_precheck(
        project_id, character=quote_character, characters=selected_names, resume=resume
    )
    if payload.get("confirm") is not True:
        raise _payment_confirm_required(quote)
    quote_id = payload.get("quote_id")
    quote_row = _validate_payment_quote(project_id, quote_id, quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "quote_id": quote_id, "task_id": quote_row["consumed_task_id"],
            "run_id": quote_row["consumed_run_id"], "precheck": quote,
        }
    generation_only = selected_names[0] if selected_names and len(selected_names) == 1 else only
    started = _start_refs_generation(
        project_id,
        generation_only,
        only_characters=selected_names,
        resume=resume,
    )
    if not started:
        raise HTTPException(409, "定妆照正在生成中")
    _consume_payment_quote(
        str(quote_id), task_id=started["task_id"], run_id=started["run_id"],
    )
    return {**started, "quote_id": quote_id, "precheck": quote}


@router.post("/projects/{project_id}/refs/cancel")
async def cancel_refs(project_id: str):
    """停止定妆照生成。已落盘的定妆照保留，状态置回空闲。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("portrait.cancel", {"project_id": project_id})
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    stopped = await task_registry.cancel_and_wait("refs", project_id)
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET refs_status='idle', refs_error=NULL, refs_target=NULL, "
        "refs_batch_started_at=NULL WHERE id=?", (project_id,))
    conn.commit()
    was_running = p["refs_status"] == "running"
    return {"stopped": stopped or was_running}


# ---------- 场景图素材库（跨集场景一致性） ----------
# 注：初始批量出图在此（scenes.generate_scene_refs，适用集 1~ 至今）；库外新场景的反应式发现
# 已挂在分镜阶段（见 scenes.ensure_scenes_for_storyboard），不在此轮询。


def _normalize_scene_selection(value) -> list[str] | None:
    if value in (None, ""):
        return None
    raw = value
    if isinstance(value, str):
        parsed = _parse_json_value(value)
        raw = parsed if isinstance(parsed, list) else value.split(",")
    if not isinstance(raw, list):
        raise HTTPException(422, "scenes 必须是场景名数组")
    names = [str(item).strip() for item in raw if str(item).strip()]
    return list(dict.fromkeys(names)) or None


def _scene_required_roles(scene: dict) -> list[str]:
    from app.multiview import SCENE_REQUIRED_VIEWS
    roles = list(SCENE_REQUIRED_VIEWS)
    requested = scene.get("required_views") or []
    if isinstance(requested, str):
        requested = [requested]
    if scene.get("action_zone_required") or "action_zone" in requested:
        roles.append("action_zone")
    return list(dict.fromkeys(roles))


def _scene_current_row(conn, project_id: str, scene_name: str):
    return conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=? "
        "ORDER BY (ep_end IS NULL) DESC, ep_start DESC, created_at DESC LIMIT 1",
        (project_id, scene_name),
    ).fetchone()


def _scene_row_gate(row) -> dict:
    if not row:
        return {}
    for column in ("group_qa_json", "qa_json"):
        if column not in row.keys() or not row[column]:
            continue
        parsed = _parse_json_value(row[column], {})
        if isinstance(parsed, dict):
            return parsed
    return {}


def scan_scene_asset_gaps(project_id: str) -> dict:
    """只读扫描；不会创建任务、调用供应商或写账单。"""
    from app.multiview import scene_primary_is_usable
    from app.scene_policy import scene_asset_state

    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        return {"project_id": project_id, "total": 0, "items": [], "counts": {}}
    bible = json.loads(p["bible_json"])
    conn = get_conn()
    items: list[dict] = []
    counts = {"missing": 0, "hard_failure": 0, "warning": 0, "interrupted": 0, "unverified": 0}
    for scene in bible.get("scenes") or []:
        name = str(scene.get("name") or "")
        required = _scene_required_roles(scene)
        row = _scene_current_row(conn, project_id, name)
        if not row:
            item = {"scene": name, "category": "missing", "reason": "尚无场景视角包", "views": required}
            counts["missing"] += 1
            items.append(item)
            continue
        views = rows_to_dicts(conn.execute(
            "SELECT view_role,status,image_path,qa_json FROM scene_reference_views WHERE scene_reference_id=?",
            (row["id"],),
        ).fetchall())
        ready_roles = {v["view_role"] for v in views if v.get("status") == "ready" and v.get("image_path")}
        missing_roles = [role for role in required if role not in ready_roles]
        gate = _scene_row_gate(row)
        has_image = bool(row["image_path"])
        primary_usable = scene_primary_is_usable(row, views)
        # The gap scanner serves the video-production path, not the internal
        # multi-view QA dashboard.  Once the establishing image is usable,
        # optional reverse/action views and soft QA warnings are not a user
        # blocking gap.
        if primary_usable:
            continue
        state = scene_asset_state(
            row["pack_status"] if "pack_status" in row.keys() else None,
            gate,
            has_image=has_image,
            primary_usable=primary_usable,
        )
        hard = [str(x) for x in (gate.get("hard_failures") or []) if str(x).strip()]
        failed_views = [
            str(v.get("view_role")) for v in (gate.get("views") or [])
            if isinstance(v, dict) and v.get("status") in {"failed", "unverified"}
        ]
        if missing_roles:
            category, reason, repair = "missing", "缺少必需视角", missing_roles
        elif state == "failed":
            category, reason = "hard_failure", "；".join(hard[:4]) or "整包硬门禁未通过"
            repair = failed_views or required
        elif state == "warning":
            category = "warning"
            reason = (
                "主图尚未确认可用；多视角包待补齐或待验证"
                if row["pack_status"] == "failed"
                else "；".join((gate.get("warnings") or gate.get("issues") or [])[:4])
            )
            repair = missing_roles or failed_views
        elif state == "unverified":
            category, reason, repair = "unverified", "未按新版硬门禁完成验证", required
        elif p.get("scene_refs_status") == "failed":
            category, reason, repair = "interrupted", "最近一次场景任务中断或失败", []
        else:
            continue
        counts[category] += 1
        items.append({
            "scene": name,
            "scene_reference_id": row["id"],
            "category": category,
            "reason": reason,
            "views": list(dict.fromkeys(repair)),
            "hard_failures": hard,
            "warnings": gate.get("warnings") or gate.get("issues") or [],
            "pack_status": row["pack_status"] if "pack_status" in row.keys() else None,
        })
    return {"project_id": project_id, "total": len(items), "items": items, "counts": counts, "read_only": True}


def compute_scene_cost_precheck(
    project_id: str,
    *,
    scenes: list[str] | None = None,
    resume: bool = False,
    view_role: str | None = None,
    scene_reference_id: str | None = None,
    action: str | None = None,
    scene_payloads: list[dict] | None = None,
) -> dict:
    """所有场景图片付费入口共用的服务端范围/费用预检。"""
    from app.config import IMAGE_PRICE_PER_UNIT

    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成人物谱")
    bible = json.loads(p["bible_json"])
    source_scenes = scene_payloads if scene_payloads is not None else list(bible.get("scenes") or [])
    selected = _normalize_scene_selection(scenes)
    if selected:
        by_name = {str(item.get("name") or ""): item for item in source_scenes}
        missing = [name for name in selected if name not in by_name]
        if missing:
            raise HTTPException(404, f"场景不存在：{missing[0]}")
        source_scenes = [by_name[name] for name in selected]
    scope: list[dict] = []
    if view_role:
        if len(source_scenes) != 1:
            raise HTTPException(422, "单视角预检必须明确一个场景")
        scene = source_scenes[0]
        scope.append({
            "scene": scene.get("name"), "scene_reference_id": scene_reference_id,
            "views": [view_role], "view_role": view_role, "reason": "单视角重做",
        })
    elif resume:
        gaps = scan_scene_asset_gaps(project_id)
        allowed = set(selected or [str(s.get("name") or "") for s in source_scenes])
        source_by_name = {str(scene.get("name") or ""): scene for scene in source_scenes}
        for item in gaps["items"]:
            if item["scene"] not in allowed or item["category"] == "warning":
                continue
            # 当前补齐实现以临时完整包复验后原子切换，报价必须覆盖完整合同视角；
            # 只修一个视角请走详情内“单视角重做”入口。
            contract_views = _scene_required_roles(source_by_name.get(item["scene"], {}))
            scope.append({
                "scene": item["scene"],
                "scene_reference_id": item.get("scene_reference_id"),
                "views": contract_views,
                "suggested_failed_views": item.get("views") or [],
                "reason": f"{item.get('reason')}；整包文件齐全并可读取后原子切换",
                "category": item.get("category"),
            })
    else:
        for scene in source_scenes:
            scope.append({
                "scene": scene.get("name"),
                "views": _scene_required_roles(scene),
                "reason": "首次生成" if action == "generate_bible_and_refs" else "整包重生",
            })
    image_count = sum(len(item.get("views") or []) for item in scope)
    unit = float(IMAGE_PRICE_PER_UNIT)
    estimated = round(image_count * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed = now()
    scope_fp = fingerprint({
        "project_id": project_id,
        "action": action or ("regenerate_view" if view_role else ("resume_missing" if resume else "regenerate_pack")),
        "scope": scope,
        "unit": unit,
        "bible_version": p.get("bible_version"),
    })
    return {
        "quote_id": scope_fp,
        "scope_fingerprint": scope_fp,
        "computed_at": computed,
        "quote_expires_at": computed + 300,
        "project_id": project_id,
        "action": action or ("regenerate_view" if view_role else ("resume_missing" if resume else "regenerate_pack")),
        "scene_count": len(scope),
        "actual_view_count": image_count,
        "views_per_scene": max((len(item.get("views") or []) for item in scope), default=0),
        "image_count": image_count,
        "unit_price_cny": unit,
        "estimated_cost_cny": estimated,
        "max_retry_budget_cny": max_retry,
        "budget_cap_cny": max_retry,
        "max_retries": 2,
        "estimated_duration_min": [max(1, image_count), max(3, image_count * 3)],
        "scope": scope,
        "old_asset_policy": "新包文件齐全并可读取后原子切换；质量评分只作提示，切换前旧采用包继续服务下游",
        "idempotency_hint": "同一有效报价重复确认只受理一个任务；范围或价格扩大必须重新确认",
        "stop_policy": "可停止；已开始步骤可能计费，结构完整并落盘的资产保留",
    }


@router.get("/projects/{project_id}/scene-refs/gaps")
async def scene_refs_gaps(project_id: str):
    return scan_scene_asset_gaps(project_id)


@router.post("/projects/{project_id}/scene-refs/precheck")
async def scene_refs_precheck(project_id: str, body: dict | None = None):
    payload = _as_body_dict(body)
    return _issue_payment_quote(compute_scene_cost_precheck(
        project_id,
        scenes=_normalize_scene_selection(payload.get("scenes")),
        resume=bool(payload.get("resume", False)),
        view_role=payload.get("view_role"),
        scene_reference_id=payload.get("scene_reference_id"),
        action=payload.get("action"),
    ))


def _scene_refs_progress_payload(project_id: str) -> dict:
    p = _project_or_404(project_id)
    gaps = scan_scene_asset_gaps(project_id)
    target = _decode_scene_target(p.get("scene_refs_target"))
    all_scenes = (json.loads(p["bible_json"]).get("scenes") or []) if p.get("bible_json") else []
    target_names = set(target if isinstance(target, list) else ([target] if isinstance(target, str) else []))
    progress_scenes = [scene for scene in all_scenes if not target_names or scene.get("name") in target_names]
    total = len(progress_scenes)
    problematic = {item["scene"]: item for item in gaps["items"]}
    items = []
    ready = failed = missing = unverified = 0
    for scene in progress_scenes:
        name = scene.get("name")
        gap = problematic.get(name)
        if not gap:
            status = "ready"; ready += 1
        elif gap["category"] == "missing":
            status = "missing"; missing += 1
        elif gap["category"] == "hard_failure":
            status = "failed"; failed += 1
        else:
            status = "unverified"; unverified += 1
        items.append({"scene": name, "status": status, **({"detail": gap} if gap else {})})
    run = next((item for item in evidence_repository.list_runs(project_id=project_id, limit=50)
                if item.get("workflow_type") in {"scene_references", "scene_view_redo"}), None)
    run_id = (run or {}).get("id")
    steps = evidence_repository.get_steps(run_id) if run_id else []
    active_step = next((step for step in reversed(steps)
                        if step.get("status") in {"queued", "running", "waiting"}), None)
    latest_step = active_step or (steps[-1] if steps else None)
    latest_call = None
    successful_images = 0
    if run_id:
        conn = get_conn()
        calls = rows_to_dicts(conn.execute(
            "SELECT * FROM provider_calls WHERE run_id=? ORDER BY id", (run_id,),
        ).fetchall())
        successful_images = sum(
            1 for call in calls
            if call.get("status") in {"SUCCEEDED", "succeeded", "success"}
            and "image" in str(call.get("kind") or "").lower()
        )
        latest_call = calls[-1] if calls else None
    call_meta = _parse_json_value((latest_call or {}).get("meta"), {})
    if not isinstance(call_meta, dict):
        call_meta = {}
    fallback_scene = target[0] if isinstance(target, list) and target else (target if isinstance(target, str) else None)
    configured = (run or {}).get("config_snapshot") or {}
    spent = float((run or {}).get("cost_cny") or 0)
    if spent <= 0 and successful_images:
        spent = successful_images * float(config.IMAGE_PRICE_PER_UNIT)
    return {
        "project_id": project_id, "total": total, "ready": ready, "failed": failed,
        "missing": missing, "unverified": unverified, "remaining": max(0, total - ready),
        "refs_status": p.get("scene_refs_status"), "refs_target": target,
        "run_id": run_id,
        "phase": (latest_step or {}).get("step_name") or (latest_call or {}).get("kind") or p.get("scene_refs_status"),
        "current_scene": call_meta.get("scene_name") or configured.get("scene_name") or fallback_scene,
        "current_view": call_meta.get("view_role") or configured.get("view_role"),
        "attempt": int((latest_call or {}).get("attempt_no") or 0),
        "spent_cny": round(spent, 2),
        "items": items, "updated_at": now(),
    }


@router.get("/projects/{project_id}/scene-refs/progress")
async def scene_refs_progress(project_id: str):
    return _scene_refs_progress_payload(project_id)


def _scene_review_snapshot(conn, project_id: str) -> list[dict]:
    rows = rows_to_dicts(conn.execute(
        "SELECT id,scene_name,artifact_id,input_fingerprint,pack_status,created_at "
        "FROM scene_references WHERE project_id=? AND ep_end IS NULL ORDER BY scene_name,id",
        (project_id,),
    ).fetchall())
    snapshot: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        version = str(row.get("artifact_id") or row.get("input_fingerprint") or row["id"])
        key = (row["id"], version)
        if key in seen:
            continue
        seen.add(key)
        snapshot.append({
            "scene_reference_id": row["id"], "adopted_version": version,
            "scene_name": row["scene_name"], "old_status": row.get("pack_status"),
        })
    return snapshot


def _insert_scene_review_items(conn, batch_id: str, snapshot: list[dict]) -> int:
    added = 0
    for item in snapshot:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO scene_review_items(id,batch_id,scene_reference_id,adopted_version,"
            "scene_name,old_status,result_status,disposition) VALUES(?,?,?,?,?,?,?,?)",
            (new_id("scene_review_item"), batch_id, item["scene_reference_id"], item["adopted_version"],
             item["scene_name"], item.get("old_status"), "queued", "pending"),
        )
        added += max(0, cursor.rowcount)
    return added


async def _evaluate_scene_review_item(conn, row, *, enforce: bool) -> tuple[str, dict]:
    from app.multiview import (
        SCENE_REQUIRED_VIEWS, list_scene_views, review_scene_pack_consistency, review_scene_view,
    )
    from app.scene_policy import normalize_scene_pack_qa

    scene = conn.execute("SELECT * FROM scene_references WHERE id=?", (row["scene_reference_id"],)).fetchone()
    if not scene:
        return "unverified", {"uncertainties": ["复验期间资产已不存在"], "status": "unverified"}
    views = list_scene_views(scene["id"], conn=conn)
    old_group = _parse_json_value(scene["group_qa_json"] if "group_qa_json" in scene.keys() else None, {})
    required = list((old_group or {}).get("required_views") or SCENE_REQUIRED_VIEWS)
    actual = [str(view.get("view_role") or "") for view in views if view.get("image_path")]
    single_results: list[dict] = []
    for view in views:
        if view.get("view_role") not in required or not view.get("image_path"):
            continue
        single_results.append(await review_scene_view(
            view["image_path"], scene["state_canonical"] if "state_canonical" in scene.keys()
            and scene["state_canonical"] else (scene["scene_canonical"] or ""), view["view_role"],
        ))
    required_views = [view for view in views if view.get("view_role") in required and view.get("image_path")]
    if len(required_views) >= 2:
        group = await review_scene_pack_consistency(required_views, scene["scene_canonical"] or "")
    else:
        group = normalize_scene_pack_qa({}, required_roles=required, actual_roles=actual)
    hard = list(group.get("hard_failures") or [])
    uncertain = list(group.get("uncertainties") or [])
    warnings = list(group.get("warnings") or group.get("issues") or [])
    for item in single_results:
        hard.extend(item.get("hard_failures") or [])
        uncertain.extend(item.get("uncertainties") or [])
        warnings.extend(item.get("warnings") or [])
    hard = list(dict.fromkeys(str(item) for item in hard if str(item).strip()))
    uncertain = list(dict.fromkeys(str(item) for item in uncertain if str(item).strip()))
    warnings = list(dict.fromkeys(str(item) for item in warnings if str(item).strip()))
    status = "hard_failed" if hard else ("unverified" if uncertain else ("warning" if warnings else "passed"))
    evidence = {
        **group, "hard_failures": hard, "uncertainties": uncertain, "warnings": warnings,
        "single_view_results": single_results, "result_status": status,
    }
    if enforce:
        change = _parse_json_value(scene["change_json"] if "change_json" in scene.keys() else None, {}) or {}
        change.update({
            "new_references_blocked": False, "reviewed_by_batch": row["batch_id"],
            "reviewed_at": now(), "review_result": status,
            "runtime_blocking": False,
            "gate_retry_exhausted": status in {"hard_failed", "unverified"},
        })
        conn.execute(
            "UPDATE scene_references SET group_qa_json=?,change_json=? WHERE id=?",
            (json.dumps(evidence, ensure_ascii=False), json.dumps(change, ensure_ascii=False), scene["id"]),
        )
    return status, evidence


async def _run_scene_review_batch(batch_id: str) -> None:
    from app.scene_policy import SCENE_QA_POLICY_VERSION, SCENE_QA_RULE_VERSION

    conn = get_conn()
    batch = conn.execute("SELECT * FROM scene_review_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        return
    conn.execute("UPDATE scene_review_batches SET status='running',started_at=COALESCE(started_at,?) WHERE id=?",
                 (now(), batch_id))
    conn.commit()
    try:
        while True:
            pending = conn.execute(
                "SELECT * FROM scene_review_items WHERE batch_id=? AND result_status='queued' ORDER BY scene_name,id",
                (batch_id,),
            ).fetchall()
            for item in pending:
                try:
                    status, evidence = await _evaluate_scene_review_item(
                        conn, item,
                        enforce=(not bool(batch["shadow_mode"]) and bool(batch["block_new_references"])),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    status, evidence = "unverified", {
                        "status": "unverified", "uncertainties": [f"复验未完成：{type(exc).__name__}"],
                        "policy_version": SCENE_QA_POLICY_VERSION, "rule_version": SCENE_QA_RULE_VERSION,
                    }
                conn.execute(
                    "UPDATE scene_review_items SET result_status=?,evidence_json=?,evaluated_at=? WHERE id=?",
                    (status, json.dumps(evidence, ensure_ascii=False), now(), item["id"]),
                )
                conn.commit()
            # 把签收截止前的新采用/切换包加入增量快照，按包版本去重。
            current = _scene_review_snapshot(conn, batch["project_id"])
            before = conn.execute("SELECT COUNT(*) n FROM scene_review_items WHERE batch_id=?", (batch_id,)).fetchone()["n"]
            added = _insert_scene_review_items(conn, batch_id, current)
            if added:
                existing_incremental = _parse_json_value(batch["incremental_snapshot_json"], []) or []
                known = {(item.get("scene_reference_id"), item.get("adopted_version")) for item in existing_incremental}
                delta = [item for item in current if (item["scene_reference_id"], item["adopted_version"]) not in known]
                conn.execute(
                    "UPDATE scene_review_batches SET incremental_snapshot_json=? WHERE id=?",
                    (json.dumps([*existing_incremental, *delta], ensure_ascii=False), batch_id),
                )
                conn.commit()
                batch = conn.execute("SELECT * FROM scene_review_batches WHERE id=?", (batch_id,)).fetchone()
                continue
            if before == conn.execute("SELECT COUNT(*) n FROM scene_review_items WHERE batch_id=?", (batch_id,)).fetchone()["n"]:
                break
        counts = {row["result_status"]: row["n"] for row in conn.execute(
            "SELECT result_status,COUNT(*) n FROM scene_review_items WHERE batch_id=? GROUP BY result_status",
            (batch_id,),
        ).fetchall()}
        denominator = sum(counts.values())
        evaluated = denominator - counts.get("queued", 0)
        conn.execute(
            "UPDATE scene_review_batches SET status='succeeded',cutoff_at=?,denominator=?,evaluated=?,"
            "passed=?,warning=?,hard_failed=?,unverified=?,finished_at=? WHERE id=?",
            (now(), denominator, evaluated, counts.get("passed", 0), counts.get("warning", 0),
             counts.get("hard_failed", 0), counts.get("unverified", 0), now(), batch_id),
        )
        conn.commit()
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            conn.execute(
                "UPDATE scene_review_batches SET status='queued',finished_at=NULL WHERE id=?",
                (batch_id,),
            )
        else:
            conn.execute(
                "UPDATE scene_review_batches SET status='stopped',finished_at=? WHERE id=?",
                (now(), batch_id),
            )
        conn.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        conn.execute("UPDATE scene_review_batches SET status='failed',finished_at=? WHERE id=?", (now(), batch_id))
        conn.commit()
        errors.record_and_format(exc, action="scene_history_review", context={"batch_id": batch_id})


def _scene_review_payload(conn, batch_id: str, *, include_items: bool = True) -> dict:
    row = conn.execute("SELECT * FROM scene_review_batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(404, "历史复验批次不存在")
    out = dict(row)
    out["baseline_snapshot"] = _parse_json_value(out.pop("baseline_snapshot_json"), [])
    out["incremental_snapshot"] = _parse_json_value(out.pop("incremental_snapshot_json"), [])
    out["coverage"] = (out["evaluated"] / out["denominator"]) if out["denominator"] else 1.0
    if include_items:
        out["items"] = []
        for item in rows_to_dicts(conn.execute(
            "SELECT * FROM scene_review_items WHERE batch_id=? ORDER BY scene_name,id", (batch_id,),
        ).fetchall()):
            item["evidence"] = _parse_json_value(item.pop("evidence_json"), {})
            out["items"].append(item)
    return out


@router.post("/projects/{project_id}/scene-reviews", status_code=202)
async def start_scene_history_review(project_id: str, body: dict | None = None):
    from app.scene_policy import SCENE_QA_POLICY_VERSION, SCENE_QA_RULE_VERSION

    payload = _as_body_dict(body)
    _project_or_404(project_id)
    conn = get_conn()
    active = conn.execute(
        "SELECT id FROM scene_review_batches WHERE project_id=? AND status IN ('queued','running') "
        "ORDER BY created_at DESC LIMIT 1", (project_id,),
    ).fetchone()
    if active:
        return {**_scene_review_payload(conn, active["id"], include_items=False), "idempotent_replay": True}
    baseline = _scene_review_snapshot(conn, project_id)
    batch_id = new_id("scene_review")
    conn.execute(
        "INSERT INTO scene_review_batches(id,project_id,status,policy_version,rule_version,"
        "baseline_snapshot_json,incremental_snapshot_json,denominator,shadow_mode,block_new_references,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, project_id, "queued", SCENE_QA_POLICY_VERSION, SCENE_QA_RULE_VERSION,
         json.dumps(baseline, ensure_ascii=False), "[]", len(baseline),
         int(payload.get("shadow_mode", True)), int(payload.get("block_new_references", False)), now()),
    )
    _insert_scene_review_items(conn, batch_id, baseline)
    conn.commit()
    coro = _run_scene_review_batch(batch_id)
    try:
        task_registry.spawn(
            "scene_history_review", batch_id, coro, project_id=project_id,
        )
    except Exception as exc:
        coro.close()
        conn.execute(
            "UPDATE scene_review_batches SET status='failed',finished_at=? WHERE id=?",
            (now(), batch_id),
        )
        conn.commit()
        raise HTTPException(503, detail={
            "code": "SCENE_REVIEW_START_FAILED",
            "message": "场景复验任务未能启动，批次快照已保留，可重新发起",
            "batch_id": batch_id,
            "retryable": True,
        }) from exc
    return {**_scene_review_payload(conn, batch_id, include_items=False), "status": "accepted", "task_id": f"scene_history_review:{batch_id}"}


@router.get("/projects/{project_id}/scene-reviews")
async def list_scene_history_reviews(project_id: str):
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM scene_review_batches WHERE project_id=? ORDER BY created_at DESC", (project_id,),
    ).fetchall()
    return {"project_id": project_id, "items": [_scene_review_payload(conn, row["id"], include_items=False) for row in rows]}


@router.get("/projects/{project_id}/scene-reviews/{batch_id}")
async def get_scene_history_review(project_id: str, batch_id: str):
    _project_or_404(project_id)
    payload = _scene_review_payload(get_conn(), batch_id)
    if payload["project_id"] != project_id:
        raise HTTPException(404, "历史复验批次不存在")
    return payload


@router.post("/projects/{project_id}/scene-reviews/{batch_id}/cancel")
async def cancel_scene_history_review(project_id: str, batch_id: str):
    _project_or_404(project_id)
    payload = _scene_review_payload(get_conn(), batch_id, include_items=False)
    if payload["project_id"] != project_id:
        raise HTTPException(404, "历史复验批次不存在")
    stopped = await task_registry.cancel_and_wait("scene_history_review", batch_id)
    return {"stopped": stopped, "batch_id": batch_id}


@router.post("/projects/{project_id}/scene-reviews/{batch_id}/items/{item_id}/disposition")
async def dispose_scene_history_review_item(
    project_id: str, batch_id: str, item_id: str, body: dict | None = None,
):
    """记录复验处置；不删除历史图片、证据或账单。"""
    _project_or_404(project_id)
    payload = _as_body_dict(body)
    action = str(payload.get("action") or "").strip()
    if action not in {"accepted_risk", "repair_planned", "repaired", "false_positive", "deferred"}:
        raise HTTPException(422, "未知复验处置动作")
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(422, "复验处置必须填写原因")
    conn = get_conn()
    batch = conn.execute(
        "SELECT * FROM scene_review_batches WHERE id=? AND project_id=?", (batch_id, project_id),
    ).fetchone()
    item = conn.execute(
        "SELECT * FROM scene_review_items WHERE id=? AND batch_id=?", (item_id, batch_id),
    ).fetchone()
    if not batch or not item:
        raise HTTPException(404, "复验批次或条目不存在")
    disposition = json.dumps({
        "action": action, "reason": reason,
        "decided_by": str(payload.get("decided_by") or "user"), "decided_at": now(),
    }, ensure_ascii=False)
    conn.execute("UPDATE scene_review_items SET disposition=? WHERE id=?", (disposition, item_id))
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM scene_review_items WHERE batch_id=? AND disposition!='pending'",
        (batch_id,),
    ).fetchone()["n"]
    conn.execute(
        "UPDATE scene_review_batches SET disposition_count=? WHERE id=?", (count, batch_id),
    )
    conn.commit()
    return {"disposed": True, "item_id": item_id, "disposition": _parse_json_value(disposition, {})}


def recover_scene_review_tasks() -> int:
    """服务重启后以同一稳定批次 ID 继续未完成复验。"""
    conn = get_conn()
    rows = conn.execute("SELECT id,project_id FROM scene_review_batches WHERE status IN ('queued','running')").fetchall()
    resumed = 0
    for row in rows:
        if task_registry.active("scene_history_review", row["id"]):
            continue
        coro = _run_scene_review_batch(row["id"])
        try:
            task_registry.spawn(
                "scene_history_review", row["id"], coro, project_id=row["project_id"],
            )
            resumed += 1
        except Exception:
            coro.close()
            conn.execute(
                "UPDATE scene_review_batches SET status='failed',finished_at=? WHERE id=?",
                (now(), row["id"]),
            )
            conn.commit()
    return resumed


async def _scene_refs_task(
    project_id: str,
    only_scene: str | list[str] | None,
    *,
    resume: bool = False,
    operation_started_at: float | None = None,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
):
    from app.scenes import SceneCandidateReviewRequired, generate_scene_refs
    conn = get_conn()
    recorder = WorkflowRecorder.create(
        workflow_type="scene_references",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, only_scene, "scene_references"),
        requested_by=requested_by,
        trigger_type=trigger_type,
        budget_limit_cny=float(get_setting("episode_cost_limit_cny") or 100),
        parent_run_id=parent_run_id,
    )
    try:
        recorder.start()
        await recorder.step(
            "scene_references",
            lambda: generate_scene_refs(
                project_id,
                only_scene,
                resume=resume,
                operation_started_at=operation_started_at,
            ),
            agent_name="reference_asset_loop",
        )
        conn.execute(
            "UPDATE projects SET scene_refs_status='ready',scene_refs_error=NULL,"
            "scene_refs_batch_started_at=NULL WHERE id=?",
            (project_id,),
        )
        conn.commit()
        recorder.succeed("场景参考资产已生成并通过证据门禁", conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，人物单视角重做等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except SceneCandidateReviewRequired as exc:
        message = str(exc)[:1200]
        recorder.partial(message, conn=None)
        conn.execute(
            "UPDATE projects SET scene_refs_status='warning', scene_refs_error=? WHERE id=?",
            (message, project_id),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc, conn=None)
        public = errors.record_and_format(exc, action="scene_refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


@router.post("/projects/{project_id}/scene-bible/preview")
async def preview_scene_bible(project_id: str):
    """只生成可编辑的场景清单与真实视角报价；不出图、不替换资产。"""
    from app.stages import generate_scene_bible

    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    bible = Bible.model_validate(json.loads(p["bible_json"]))
    from app.scenes import SCENE_BIBLE_CHAPTER_WINDOW
    chapters = rows_to_dicts(get_conn().execute(
        "SELECT * FROM chapters WHERE project_id=? ORDER BY idx LIMIT ?",
        (project_id, SCENE_BIBLE_CHAPTER_WINDOW),
    ).fetchall())
    scenes = await generate_scene_bible(chapters, bible, project_id=project_id)
    scene_payloads = [scene.model_dump(mode="json") for scene in scenes]
    quote = _issue_payment_quote(compute_scene_cost_precheck(
        project_id,
        scenes=[scene["name"] for scene in scene_payloads],
        action="generate_bible_and_refs",
        scene_payloads=scene_payloads,
    ))
    return {"project_id": project_id, "scenes": scene_payloads, "precheck": quote, "generates_images": False}


@router.post("/projects/{project_id}/scene-bible/precheck")
async def scene_bible_precheck(project_id: str, body: dict | None = None):
    payload = _as_body_dict(body)
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise HTTPException(422, "必须提交已确认的场景清单")
    names = [str(item.get("name") or "").strip() for item in scenes if isinstance(item, dict)]
    if len(names) != len(scenes) or not all(names) or len(names) != len(set(names)):
        raise HTTPException(422, "场景名称不能为空或重复")
    if any(not 30 <= len(str(item.get("scene_canonical") or "").strip()) <= 80 for item in scenes):
        raise HTTPException(422, "每个场景锚点必须为 30~80 字")
    project = _project_or_404(project_id)
    candidate_bible = json.loads(project["bible_json"] or '{}')
    candidate_bible["scenes"] = scenes
    instance, validation_errors = schema_errors(Bible, candidate_bible)
    if validation_errors:
        raise HTTPException(422, "；".join(validation_errors))
    normalized_scenes = [scene.model_dump(mode="json") for scene in instance.scenes]
    return _issue_payment_quote(compute_scene_cost_precheck(
        project_id, scenes=names, action="generate_bible_and_refs", scene_payloads=normalized_scenes,
    ))


@router.post("/projects/{project_id}/scene-bible", status_code=202)
async def start_scene_bible(project_id: str, body: dict | None = None):
    """（重新）生成场景圣经并触发场景图批量出图。人物谱必须先就绪。"""
    from app.capabilities.dispatch import ui_route
    payload = _as_body_dict(body)
    # 带服务端报价的正式确认直接进入本路由的报价/幂等校验；旧能力入口仍走 Command Bus。
    formal_request = any(
        key in payload
        for key in ("scenes", "confirm", "quote_id", "idempotency_key", "request_id")
    )
    if not formal_request:
        routed = await ui_route("scene.generate_bible", {"project_id": project_id})
        if routed is not None:
            return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _scene_assets_task_active(project_id):
        raise HTTPException(409, "场景图正在生成中")
    confirmed_scenes = payload.get("scenes")
    if not isinstance(confirmed_scenes, list) or not confirmed_scenes:
        raise HTTPException(409, detail={
            "code": "SCENE_PREVIEW_REQUIRED",
            "message": "必须先预览并确认场景清单，再执行费用确认",
        })
    names = [str(item.get("name") or "").strip() for item in confirmed_scenes if isinstance(item, dict)]
    if not names or len(names) != len(set(names)):
        raise HTTPException(422, "场景清单名称不能为空或重复")
    if any(not 30 <= len(str(item.get("scene_canonical") or "").strip()) <= 80 for item in confirmed_scenes):
        raise HTTPException(422, "每个场景锚点必须为 30~80 字")
    candidate_bible = json.loads(p["bible_json"] or '{}')
    candidate_bible["scenes"] = confirmed_scenes
    bible_instance, validation_errors = schema_errors(Bible, candidate_bible)
    if validation_errors:
        raise HTTPException(422, "；".join(validation_errors))
    confirmed_scenes = [scene.model_dump(mode="json") for scene in bible_instance.scenes]
    quote = compute_scene_cost_precheck(
        project_id, scenes=names, action="generate_bible_and_refs", scene_payloads=confirmed_scenes,
    )
    if payload.get("confirm") is not True:
        raise _payment_confirm_required(quote)
    quote_row = _validate_payment_quote(project_id, payload.get("quote_id"), quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "quote_id": payload.get("quote_id"), "task_id": quote_row["consumed_task_id"],
            "run_id": quote_row["consumed_run_id"],
        }
    conn = get_conn()
    current = json.loads(p["bible_json"])
    current["scenes"] = confirmed_scenes
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(current, ensure_ascii=False), project_id),
    )
    conn.commit()
    if not _start_scene_refs_generation(project_id, names):
        raise HTTPException(409, "场景图正在生成中")
    task_id = f"scene_refs:{project_id}"
    _consume_payment_quote(str(payload.get("quote_id")), task_id=task_id, run_id=None)
    return {"status": "accepted", "task_id": task_id, "quote_id": payload.get("quote_id"), "precheck": quote}


@router.post("/projects/{project_id}/scene-refs", status_code=202)
async def start_scene_refs(project_id: str, body: dict | None = None):
    """（重新）生成场景图。需先有场景圣经（bible.scenes 非空）。可带 only 单场景重做。"""
    from app.capabilities.dispatch import ui_route
    payload = _as_body_dict(body)
    formal_request = any(
        key in payload
        for key in ("scenes", "resume", "confirm", "quote_id", "idempotency_key", "request_id")
    )
    if not formal_request:
        routed = await ui_route(
            "scene.generate_refs",
            {"project_id": project_id, "scene_name": payload.get("scene")},
        )
        if routed is not None:
            return routed
    p = _project_or_404(project_id)
    if not p["bible_json"] or not json.loads(p["bible_json"]).get("scenes"):
        raise HTTPException(409, "还没有场景圣经，请先生成场景清单")
    if _scene_assets_task_active(project_id):
        raise HTTPException(409, "场景图正在生成中")
    selected = _normalize_scene_selection(payload.get("scenes"))
    only = payload.get("scene")
    if only and selected and only not in selected:
        raise HTTPException(422, "scene 与 scenes 范围不一致")
    if only and not selected:
        selected = [str(only)]
    resume = bool(payload.get("resume", not bool(only)))
    quote = compute_scene_cost_precheck(project_id, scenes=selected, resume=resume)
    if payload.get("confirm") is not True:
        raise _payment_confirm_required(quote)
    quote_row = _validate_payment_quote(project_id, payload.get("quote_id"), quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "quote_id": payload.get("quote_id"), "task_id": quote_row["consumed_task_id"],
            "run_id": quote_row["consumed_run_id"], "precheck": quote,
        }
    targets: str | list[str] | None = selected if selected else None
    if not _start_scene_refs_generation(project_id, targets, resume=resume):
        raise HTTPException(409, "场景图正在生成中")
    task_id = f"scene_refs:{project_id}"
    _consume_payment_quote(str(payload.get("quote_id")), task_id=task_id, run_id=None)
    return {"status": "accepted", "task_id": task_id, "quote_id": payload.get("quote_id"), "precheck": quote}


@router.post("/projects/{project_id}/scene-refs/cancel")
async def cancel_scene_refs(project_id: str):
    """停止场景图生成。已落盘的场景图保留，状态置回空闲。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("scene.cancel_refs", {"project_id": project_id})
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    stopped_bible = await task_registry.cancel_and_wait("scene_bible", project_id)
    stopped_refs = await task_registry.cancel_and_wait("scene_refs", project_id)
    stopped = stopped_bible or stopped_refs
    final_progress = _scene_refs_progress_payload(project_id)
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET scene_refs_status='idle',scene_refs_error=NULL,"
        "scene_refs_target=NULL,scene_refs_batch_started_at=NULL WHERE id=?",
        (project_id,))
    conn.commit()
    was_running = p["scene_refs_status"] == "running"
    final_progress["refs_status"] = "idle"
    return {
        "stopped": stopped or was_running,
        "partial_results_preserved": True,
        "progress": final_progress,
    }


@router.put("/projects/{project_id}/scenes/{scene_name}/prompt")
async def edit_scene_prompt(project_id: str, scene_name: str, body: dict):
    """更新单个场景的场景图生成词。传空字符串/null 恢复为默认合成描述。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "scene.update_prompt",
        {"project_id": project_id, "scene_name": scene_name, "prompt": (body.get("scene_prompt") or "")},
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    prompt_text = (body.get("scene_prompt") or "").strip()
    if prompt_text and not 10 <= len(prompt_text) <= 400:
        raise HTTPException(422, f"场景图描述长度 {len(prompt_text)} 字，要求 10~400 字（留空则恢复默认）")
    bible = json.loads(p["bible_json"])
    target = next((s for s in bible.get("scenes", []) if s.get("name") == scene_name), None)
    if target is None:
        raise HTTPException(404, f"场景不存在：{scene_name}")
    target["scene_prompt_override"] = prompt_text or None
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(bible, ensure_ascii=False), project_id))
    conn.commit()
    return {"saved": True, "reset_to_default": not prompt_text}


@router.put("/projects/{project_id}/scenes/{scene_name}")
async def edit_scene_anchor(project_id: str, scene_name: str, body: dict):
    """结构化保存场景锚点；只改文字并标记待重绘，不产生图片费用。"""
    p = _project_or_404(project_id)
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经")
    expected = body.get("expected_version")
    if expected is None or int(expected) != int(p.get("bible_version") or 0):
        raise HTTPException(409, detail={
            "code": "BIBLE_VERSION_CONFLICT", "message": "场景锚点已被其他操作更新，请刷新后重试",
            "current_version": int(p.get("bible_version") or 0),
        })
    bible = json.loads(p["bible_json"])
    target = next((scene for scene in bible.get("scenes", []) if scene.get("name") == scene_name), None)
    if target is None:
        raise HTTPException(404, f"场景不存在：{scene_name}")
    canonical = str(body.get("scene_canonical") or "").strip()
    if not 30 <= len(canonical) <= 80:
        raise HTTPException(422, "完整场景锚点要求 30~80 字")
    location = str(body.get("location_kind") or target.get("location_kind") or "").strip()
    if location and location not in {"室内", "室外", "其他"}:
        raise HTTPException(422, "location_kind 须为室内/室外/其他")
    target.update({
        "scene_canonical": canonical, "location_kind": location,
        "space": str(body.get("space") or "").strip(),
        "time_of_day": str(body.get("time_of_day") or "").strip(),
        "lighting": str(body.get("lighting") or "").strip(),
        "landmarks": [str(item).strip() for item in (body.get("landmarks") or []) if str(item).strip()],
    })
    instance, validation_errors = schema_errors(Bible, bible)
    if validation_errors:
        raise HTTPException(422, "；".join(validation_errors))
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET bible_json=?,bible_version=bible_version+1 WHERE id=?",
        (instance.model_dump_json(), project_id),
    )
    current = _scene_current_row(conn, project_id, scene_name)
    if current:
        change = _parse_json_value(current["change_json"] if "change_json" in current.keys() else None, {}) or {}
        change.update({"description_changed": True, "pending_redraw": True, "changed_at": now()})
        conn.execute("UPDATE scene_references SET change_json=? WHERE id=?",
                     (json.dumps(change, ensure_ascii=False), current["id"]))
    conn.commit()
    return {"saved": True, "bible_version": int(p.get("bible_version") or 0) + 1, "pending_redraw": True, "generated": False}


async def _run_portrait_view_redo(
    project_id: str,
    character_name: str,
    portrait_id: str,
    view_role: str,
    recorder: WorkflowRecorder,
) -> None:
    from app.multiview import regenerate_character_view, pack_result_ok

    recorder.start()
    try:
        async def _op():
            return await regenerate_character_view(
                project_id=project_id, portrait_id=portrait_id, view_role=view_role,
            )

        result = await recorder.step(
            "portrait_view_redo", _op, agent_name="portrait_view_redo",
        )
        if isinstance(result, tuple):
            result = result[1]
        if not pack_result_ok(result):
            recorder.fail(RuntimeError(
                f"视角重做未通过：{view_role}（status={(result or {}).get('status')}）"
            ), conn=None)
            return
        recorder.succeed(f"{character_name}/{view_role} 视角已重做并通过整包 QA", conn=None)
    except asyncio.CancelledError:
        recorder.cancel(conn=None)
        raise
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc, conn=None)


def _start_portrait_view_redo(
    project_id: str,
    character_name: str,
    portrait_id: str,
    view_role: str,
    *,
    quote_id: str | None,
    budget_limit_cny: float,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
) -> dict | None:
    task_key = f"{portrait_id}:{view_role}"
    if task_registry.active("portrait_view_redo", task_key):
        return None
    recorder = WorkflowRecorder.create(
        workflow_type="portrait_view_redo",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, portrait_id, view_role, quote_id),
        requested_by=requested_by,
        trigger_type=trigger_type,
        config_snapshot={
            "task_key": task_key, "character_name": character_name,
            "portrait_id": portrait_id, "view_role": view_role, "quote_id": quote_id,
            "budget_limit_cny": budget_limit_cny,
        },
        budget_limit_cny=budget_limit_cny,
        parent_run_id=parent_run_id,
    )
    coro = _run_portrait_view_redo(
        project_id, character_name, portrait_id, view_role, recorder,
    )
    try:
        task_registry.spawn(
            "portrait_view_redo", task_key, coro, project_id=project_id,
        )
    except Exception as exc:
        coro.close()
        try:
            recorder.cancel("人物单视角重做未能启动", conn=None)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("人物单视角重做任务未能启动，旧定妆包和费用凭证均已保留") from exc
    return {
        "status": "accepted", "task_id": f"portrait_view_redo:{task_key}",
        "run_id": recorder.run_id, "portrait_id": portrait_id,
        "view_role": view_role, "character_name": character_name,
    }


def recover_portrait_view_redo_tasks() -> int:
    """重建进程重启时丢失的单视角异步任务。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, scope_id, config_snapshot_json FROM workflow_runs "
        "WHERE workflow_type='portrait_view_redo' AND status='PAUSED_EXTERNAL' "
        "AND recovered_by_run_id IS NULL ORDER BY updated_at"
    ).fetchall()
    resumed = 0
    for row in rows:
        config = _parse_json_value(row["config_snapshot_json"], {})
        if not isinstance(config, dict):
            continue
        character_name = str(config.get("character_name") or "").strip()
        portrait_id = str(config.get("portrait_id") or "").strip()
        view_role = str(config.get("view_role") or "").strip()
        if not character_name or not portrait_id or not view_role:
            continue
        try:
            started = _start_portrait_view_redo(
                row["scope_id"], character_name, portrait_id, view_role,
                quote_id=config.get("quote_id"),
                budget_limit_cny=float(config.get("budget_limit_cny") or 1),
                parent_run_id=row["id"], requested_by="system", trigger_type="resume",
            )
            if started:
                resumed += 1
        except Exception:
            continue
    return resumed


@router.post("/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/views/{view_role}/regenerate")
async def regenerate_character_view_route(
    project_id: str, character_name: str, portrait_id: str, view_role: str,
    body: dict | None = None,
):
    """人物谱单视角重做：持久异步任务，立即返回 accepted + run_id。"""
    from app.capabilities.dispatch import ui_route

    payload = body or {}
    routed = await ui_route(
        "portrait.regenerate_view",
        {
            "project_id": project_id, "character_name": character_name,
            "portrait_id": portrait_id, "view_role": view_role,
            "confirm": payload.get("confirm") is True,
            "quote_id": payload.get("quote_id"),
            "idempotency_key": payload.get("idempotency_key") or payload.get("quote_id"),
        },
    )
    if routed is not None:
        return routed
    _project_or_404(project_id)
    if payload.get("confirm") is not True:
        raise HTTPException(
            409,
            detail={
                "code": "PAYMENT_CONFIRM_REQUIRED",
                "message": "必须先完成费用预检并显式确认（confirm=true）",
            },
        )
    quote = compute_refs_cost_precheck(
        project_id, character=character_name, view_role=view_role,
    )
    quote_id = payload.get("quote_id")
    quote_row = _validate_payment_quote(project_id, quote_id, quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "task_id": quote_row["consumed_task_id"], "run_id": quote_row["consumed_run_id"],
            "portrait_id": portrait_id, "view_role": view_role,
            "character_name": character_name, "precheck": quote,
        }
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM character_portraits WHERE id=? AND project_id=? AND character_name=?",
        (portrait_id, project_id, character_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "造型版本不存在")

    task_key = f"{portrait_id}:{view_role}"
    if task_registry.active("portrait_view_redo", task_key):
        active_runs = evidence_repository.list_runs(active=True, project_id=project_id, limit=20)
        existing = next(
            (
                r for r in active_runs
                if r.get("workflow_type") == "portrait_view_redo"
                and (r.get("config_snapshot") or {}).get("task_key") == task_key
            ),
            None,
        )
        return {
            "status": "accepted",
            "task_id": f"portrait_view_redo:{task_key}",
            "run_id": (existing or {}).get("id"),
            "portrait_id": portrait_id,
            "view_role": view_role,
            "message": "该视角重做任务已在运行",
        }

    try:
        started = _start_portrait_view_redo(
            project_id, character_name, portrait_id, view_role,
            quote_id=str(quote_id),
            budget_limit_cny=float(quote.get("max_retry_budget_cny") or 1),
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not started:
        raise HTTPException(409, "该视角重做任务已在运行")
    _consume_payment_quote(
        str(quote_id), task_id=started["task_id"], run_id=started["run_id"],
    )
    return {
        **started, "precheck": quote,
        "message": "单视角重做任务已受理，可刷新查看进度",
    }


async def _run_scene_view_redo(
    project_id: str,
    scene_name: str,
    scene_reference_id: str,
    view_role: str,
    recorder: WorkflowRecorder,
) -> None:
    from app.multiview import pack_result_ok, regenerate_scene_view

    recorder.start()
    try:
        result = await recorder.step(
            "generate_and_single_view_qa_and_pack_qa",
            lambda: regenerate_scene_view(
                project_id=project_id, scene_reference_id=scene_reference_id, view_role=view_role,
            ),
            agent_name="scene_view_redo",
        )
        if not pack_result_ok(result):
            recorder.fail(RuntimeError(
                f"视角重做未通过：{view_role}（status={(result or {}).get('status')}）"
            ), conn=None)
            return
        recorder.succeed(f"{scene_name}/{view_role} 已通过单图及整包 QA 并原子替换", conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，场景单视角重做等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc, conn=None)


def _start_scene_view_redo(
    project_id: str,
    scene_name: str,
    scene_reference_id: str,
    view_role: str,
    *,
    quote_id: str | None,
    budget_limit_cny: float,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
) -> dict | None:
    task_key = f"{scene_reference_id}:{view_role}"
    if task_registry.active("scene_view_redo", task_key):
        return None
    recorder = WorkflowRecorder.create(
        workflow_type="scene_view_redo", scope_type="project", scope_id=project_id,
        input_fingerprint=fingerprint(project_id, scene_reference_id, view_role, quote_id),
        requested_by=requested_by, trigger_type=trigger_type,
        config_snapshot={
            "task_key": task_key, "scene_name": scene_name,
            "scene_reference_id": scene_reference_id, "view_role": view_role,
            "quote_id": quote_id, "budget_limit_cny": budget_limit_cny,
        },
        budget_limit_cny=budget_limit_cny,
        parent_run_id=parent_run_id,
    )
    coro = _run_scene_view_redo(
        project_id, scene_name, scene_reference_id, view_role, recorder,
    )
    try:
        task_registry.spawn(
            "scene_view_redo", task_key, coro, project_id=project_id,
        )
    except Exception as exc:
        coro.close()
        try:
            recorder.cancel("场景单视角重做未能启动", conn=None)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("场景单视角重做任务未能启动，旧场景包和费用凭证均已保留") from exc
    return {
        "status": "accepted", "task_id": f"scene_view_redo:{task_key}",
        "run_id": recorder.run_id, "scene_reference_id": scene_reference_id,
        "scene_name": scene_name, "view_role": view_role,
    }


def recover_scene_view_redo_tasks() -> int:
    """服务重启后从持久运行记录恢复场景单视角重做。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id,scope_id,config_snapshot_json FROM workflow_runs "
        "WHERE workflow_type='scene_view_redo' AND status='PAUSED_EXTERNAL' "
        "AND recovered_by_run_id IS NULL ORDER BY updated_at"
    ).fetchall()
    resumed = 0
    for row in rows:
        snapshot = _parse_json_value(row["config_snapshot_json"], {})
        if not isinstance(snapshot, dict):
            continue
        scene_name = str(snapshot.get("scene_name") or "").strip()
        scene_reference_id = str(snapshot.get("scene_reference_id") or "").strip()
        view_role = str(snapshot.get("view_role") or "").strip()
        if not scene_name or not scene_reference_id or not view_role:
            continue
        try:
            started = _start_scene_view_redo(
                row["scope_id"], scene_name, scene_reference_id, view_role,
                quote_id=snapshot.get("quote_id"),
                budget_limit_cny=float(snapshot.get("budget_limit_cny") or 1),
                parent_run_id=row["id"], requested_by="system", trigger_type="resume",
            )
            if started:
                resumed += 1
        except Exception:
            continue
    return resumed


@router.post(
    "/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/views/{view_role}/regenerate",
    status_code=202,
)
async def regenerate_scene_view_route(
    project_id: str, scene_name: str, scene_reference_id: str, view_role: str,
    body: dict | None = None,
):
    """场景库单视角重做：预检后异步受理，不在 HTTP 请求中等待生成/整包 QA。"""
    from app.capabilities.dispatch import ui_route
    payload = _as_body_dict(body)
    if not payload.get("quote_id"):
        routed = await ui_route(
            "scene.regenerate_view",
            {
                "project_id": project_id, "scene_name": scene_name,
                "scene_reference_id": scene_reference_id, "view_role": view_role,
                "confirm": payload.get("confirm") is True, "quote_id": payload.get("quote_id"),
            },
        )
        if routed is not None:
            return routed
    _project_or_404(project_id)
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM scene_references WHERE id=? AND project_id=? AND scene_name=?",
        (scene_reference_id, project_id, scene_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "场景版本不存在")
    quote = compute_scene_cost_precheck(
        project_id, scenes=[scene_name], view_role=view_role,
        scene_reference_id=scene_reference_id, action="regenerate_view",
    )
    if payload.get("confirm") is not True:
        raise _payment_confirm_required(quote)
    quote_row = _validate_payment_quote(project_id, payload.get("quote_id"), quote)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "task_id": quote_row["consumed_task_id"], "run_id": quote_row["consumed_run_id"],
            "precheck": quote,
        }
    task_key = f"{scene_reference_id}:{view_role}"
    if task_registry.active("scene_view_redo", task_key):
        active_runs = evidence_repository.list_runs(active=True, project_id=project_id, limit=50)
        existing = next((run for run in active_runs if run.get("workflow_type") == "scene_view_redo"
                         and (run.get("config_snapshot") or {}).get("task_key") == task_key), None)
        return {
            "status": "accepted", "task_id": f"scene_view_redo:{task_key}",
            "run_id": (existing or {}).get("id"), "precheck": quote,
        }
    started = _start_scene_view_redo(
        project_id, scene_name, scene_reference_id, view_role,
        quote_id=str(payload.get("quote_id")),
        budget_limit_cny=float(quote.get("max_retry_budget_cny") or 1),
    )
    if not started:
        raise HTTPException(409, "该场景视角重做任务已在运行")
    _consume_payment_quote(
        str(payload.get("quote_id")), task_id=started["task_id"], run_id=started["run_id"],
    )
    return {
        **started,
        "precheck": quote, "message": "单视角重做任务已受理，可刷新恢复进度",
    }


@router.post("/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/views/{view_role}/regenerate/cancel")
async def cancel_scene_view_regeneration(
    project_id: str, scene_name: str, scene_reference_id: str, view_role: str,
):
    _project_or_404(project_id)
    task_key = f"{scene_reference_id}:{view_role}"
    stopped = await task_registry.cancel_and_wait("scene_view_redo", task_key)
    return {"stopped": stopped, "task_id": f"scene_view_redo:{task_key}", "old_asset_preserved": True}


@router.post("/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}/adopt")
async def adopt_scene_candidate_route(
    project_id: str, scene_name: str, artifact_id: str, body: dict | None = None,
):
    """手动采纳场景候选图为主图。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "scene.adopt_candidate",
        {
            "project_id": project_id,
            "scene_name": scene_name,
            "artifact_id": artifact_id,
            "reason": (body or {}).get("reason") or "",
        },
    )
    if routed is not None:
        return routed
    _project_or_404(project_id)
    from app.scenes import adopt_scene_candidate
    try:
        return await adopt_scene_candidate(
            project_id,
            scene_name,
            artifact_id,
            reason=str((body or {}).get("reason") or ""),
            decided_by=current_actor_name(),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc) or "候选不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}/review")
async def review_scene_candidate_route(
    project_id: str, scene_name: str, artifact_id: str,
):
    """只重验已落盘候选的 QA，不重新生图、不扣图片生成费。"""
    _project_or_404(project_id)
    from app.scenes import review_scene_candidate
    try:
        return await review_scene_candidate(project_id, scene_name, artifact_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc) or "候选不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}/manual-review")
async def manual_review_scene_candidate_route(
    project_id: str, scene_name: str, artifact_id: str, body: dict | None = None,
):
    """只允许对缺失/未验证证据做带审计的人工复核；明确硬失败不可覆盖。"""
    _project_or_404(project_id)
    payload = body or {}
    from app.scenes import manually_review_and_adopt_scene_candidate
    try:
        return await manually_review_and_adopt_scene_candidate(
            project_id,
            scene_name,
            artifact_id,
            confirmations=payload.get("confirmations") if isinstance(payload.get("confirmations"), dict) else {},
            reason=str(payload.get("reason") or ""),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc) or "候选不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/rollback")
async def rollback_scene_reference(
    project_id: str, scene_name: str, scene_reference_id: str, body: dict | None = None,
):
    """将历史通过包复制为当前包；同一事务更新视角、证据和审计原因。"""
    _project_or_404(project_id)
    conn = get_conn()
    target = conn.execute(
        "SELECT * FROM scene_references WHERE id=? AND project_id=? AND scene_name=?",
        (scene_reference_id, project_id, scene_name),
    ).fetchone()
    if not target:
        raise HTTPException(404, "场景历史版本不存在")
    # Score-only：回滚只要求目标包存在且必需视角齐全，不复跑 QA 硬门禁（PRD QA-SO #21）。
    from app.multiview import SCENE_REQUIRED_VIEWS, list_scene_views, missing_required_views
    views = list_scene_views(target["id"], conn=conn)
    if missing_required_views(views, SCENE_REQUIRED_VIEWS):
        raise HTTPException(409, "历史包缺少必需视角文件，不能回滚为当前版本")
    current = conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=? AND ep_end IS NULL "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, scene_name),
    ).fetchone()
    if not current:
        raise HTTPException(409, "当前场景版本不存在")
    if current["id"] == target["id"]:
        return {"rolled_back": True, "idempotent_replay": True, "scene_reference_id": current["id"]}
    reason = str(_as_body_dict(body).get("reason") or "回滚到历史通过场景包").strip()
    # 覆盖当前行前先复制完整当前包到新的负数历史槽，确保回滚也可反向回滚。
    from app.multiview import clone_scene_views
    minimum = conn.execute(
        "SELECT MIN(ep_start) AS value FROM scene_references "
        "WHERE project_id=? AND scene_name=? AND ep_start<=0",
        (project_id, scene_name),
    ).fetchone()
    history_start = int(minimum["value"] if minimum and minimum["value"] is not None else 0) - 1
    prior_history_id = new_id("scene")
    columns = [
        "id", "project_id", "scene_name", "ep_start", "ep_end", "scene_canonical", "prompt",
        "image_path", "qa_json", "base_scene_id", "bible_version", "artifact_id", "pack_status",
        "group_qa_json", "state_canonical", "input_fingerprint", "change_json", "created_at",
    ]
    available = {item[1] for item in conn.execute("PRAGMA table_info(scene_references)").fetchall()}
    columns = [column for column in columns if column in available]
    values = {column: current[column] if column in current.keys() else None for column in columns}
    values.update({
        "id": prior_history_id, "ep_start": history_start, "ep_end": 0,
        "base_scene_id": current["id"], "created_at": now(),
    })
    conn.execute(
        f"INSERT INTO scene_references({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    clone_scene_views(conn, source_scene_id=current["id"], dest_scene_id=prior_history_id)
    fields = (
        "scene_canonical", "prompt", "image_path", "qa_json", "bible_version", "artifact_id",
        "pack_status", "group_qa_json", "state_canonical", "input_fingerprint",
    )
    change = _parse_json_value(target["change_json"], {}) if "change_json" in target.keys() else {}
    if not isinstance(change, dict):
        change = {}
    change.update({
        "rollback_from": prior_history_id, "rollback_source": target["id"],
        "reason": reason, "rolled_back_at": now(),
    })
    assignments = ",".join(f"{field}=?" for field in fields)
    values = [target[field] if field in target.keys() else None for field in fields]
    conn.execute(
        f"UPDATE scene_references SET {assignments},change_json=? WHERE id=?",
        (*values, json.dumps(change, ensure_ascii=False), current["id"]),
    )
    conn.execute("DELETE FROM scene_reference_views WHERE scene_reference_id=?", (current["id"],))
    target_views = conn.execute(
        "SELECT * FROM scene_reference_views WHERE scene_reference_id=?", (target["id"],),
    ).fetchall()
    for view in target_views:
        conn.execute(
            "INSERT INTO scene_reference_views(id,scene_reference_id,view_role,camera_axis,image_path,prompt,"
            "qa_json,artifact_id,base_view_id,status,selected,input_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("sview"), current["id"], view["view_role"], view["camera_axis"], view["image_path"],
             view["prompt"], view["qa_json"], view["artifact_id"], view["id"], view["status"],
             view["selected"], view["input_fingerprint"], now()),
        )
    if target["artifact_id"]:
        conn.execute(
            "INSERT INTO gate_decisions(id,artifact_id,gate_key,decision,decided_by,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (new_id("gate"), target["artifact_id"], "scene_reference_rollback", "rollback", "scene_editor", reason, now()),
        )
    conn.commit()
    return {"rolled_back": True, "scene_reference_id": current["id"], "source_scene_reference_id": target["id"], "reason": reason}


__all__ = [name for name in globals() if not name.startswith("__")]
