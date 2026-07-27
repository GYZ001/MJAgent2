from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

def _start_refs_generation(
    project_id: str,
    only_character: str | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
) -> bool:
    """启动定妆照任务。

    返回值表示是否成功启动；若已有同项目定妆任务在跑，则直接返回 False。
    """
    if _refs_task_active(project_id):
        return False
    conn = get_conn()
    if only_character is None:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=NULL WHERE id=?",
            (project_id,),
        )
    else:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=? WHERE id=?",
            (only_character, project_id),
        )
    conn.commit()
    task_registry.spawn(
        "refs", project_id,
        _refs_task(
            project_id, only_character, resume=resume, parent_run_id=parent_run_id,
            requested_by="system" if resume else "user",
            trigger_type="resume" if resume else "manual",
        ),
        project_id=project_id,
    )
    return True

def _start_scene_refs_generation(
    project_id: str,
    only_scene: str | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
) -> bool:
    """启动场景图素材库生成任务。已有同项目任务在跑则返回 False。"""
    if _scene_refs_task_active(project_id):
        return False
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL, scene_refs_target=? WHERE id=?",
        (only_scene, project_id))
    conn.commit()
    task_registry.spawn(
        "scene_refs", project_id,
        _scene_refs_task(
            project_id, only_scene, resume=resume, parent_run_id=parent_run_id,
            requested_by="system" if resume else "user",
            trigger_type="resume" if resume else "manual",
        ),
        project_id=project_id,
    )
    return True


async def _scene_bible_and_refs(project_id: str) -> None:
    """场景圣经生成 + 落库 + 触发场景图批量出图（在人物谱定稿后调用，与定妆照并行）。
    场景圣经是增强项：失败只记录到 scene_refs_error，不影响人物谱/分集主流程。"""
    from app.stages import generate_scene_bible
    conn = get_conn()
    recorder = WorkflowRecorder.create(
        workflow_type="scene_bible",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, "scene_bible"),
        requested_by="user",
        policy_snapshot={"max_iterations": 4, "warning_blocks_downstream": True},
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
        conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                     (json.dumps(data, ensure_ascii=False), project_id))
        conn.commit()
        recorder.succeed("场景 Bible 已通过合同")
        if not _start_scene_refs_generation(project_id, None):
            raise RuntimeError("场景 Bible 已完成，但场景图任务未能启动")
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:  # noqa: BLE001 场景圣经失败不阻断主流程，仅透出状态
        recorder.fail(exc)
        public = errors.record_and_format(exc, action="scene_bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


def recover_bible_tasks() -> int:
    """启动时恢复人物谱任务（对齐 worker.recover_and_start 的语义）：
    进程重启/reload 会丢掉内存里的 asyncio.Task，但 DB 仍是 running。
    与其在下次访问时判孤儿并报错，不如用持久化的 feedback 重新拉起任务续跑。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, bible_feedback FROM projects WHERE bible_status='running'").fetchall()
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
        recorder = _new_bible_recorder(
            pid, trigger_type="resume", requested_by="system",
            parent_run_id=parent["id"] if parent else None,
        )
        _track_bible_task(
            pid,
            asyncio.get_running_loop().create_task(
                _recorded_bible_task(pid, feedback, recorder, trigger_full_refs=True)
            ),
        )
        resumed += 1
    return resumed


def recover_character_ref_tasks() -> int:
    """Resume initial portrait batches and skip per-character committed checkpoints."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, refs_target FROM projects WHERE refs_status='running'"
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
        if _start_refs_generation(
            project_id,
            row["refs_target"],
            resume=True,
            parent_run_id=parent["id"] if parent else None,
        ):
            resumed += 1
    return resumed


def recover_scene_ref_tasks() -> int:
    """Resume persisted scene-asset work after a reload or process restart.

    Scene generation is idempotent: approved references are skipped, so an
    interrupted batch safely continues from the first missing scene instead of
    regenerating accepted assets.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, bible_json, bible_status, scene_refs_target "
        "FROM projects WHERE scene_refs_status='running'"
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
            parent = conn.execute(
                "SELECT id FROM workflow_runs WHERE workflow_type='scene_references' "
                "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
                "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if _start_scene_refs_generation(
                project_id,
                row["scene_refs_target"],
                resume=True,
                parent_run_id=parent["id"] if parent else None,
            ):
                resumed += 1
            continue
        task_registry.spawn(
            "scene_bible", project_id, _scene_bible_and_refs(project_id), project_id=project_id
        )
        resumed += 1
    return resumed

async def _bible_task(project_id: str, feedback: str = "", *, trigger_full_refs: bool = True):
    conn = get_conn()
    try:
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)).fetchall())
        timeout_s = max(int(get_setting("bible_task_timeout_s") or BIBLE_TASK_TIMEOUT_S), 60)
        # 重新谱写时按角色名保留已有定妆照（重生圣经不应丢失一致性锚点）
        old_row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        old_style = None
        old_bible = None
        if old_row and old_row["bible_json"]:
            old_bible = json.loads(old_row["bible_json"])
        bible = await asyncio.wait_for(
            generate_bible(
                chapters, feedback=feedback, previous_bible=old_bible, project_id=project_id
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
            _start_refs_generation(project_id, None)
            # 场景圣经 + 场景图素材库（与定妆照并行）：跨集场景一致性的底稿。增强项，整段失败都不能影响人物谱主流程。
            if {"scene_refs_status", "scene_refs_error"}.issubset(project_columns):
                try:
                    conn.execute("UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL WHERE id=?",
                                 (project_id,))
                    conn.commit()
                    task_registry.spawn(
                        "scene_bible", project_id, _scene_bible_and_refs(project_id), project_id=project_id
                    )
                except Exception as exc:  # noqa: BLE001 场景库是增强项，但触发失败必须可见
                    public = errors.record_and_format(
                        exc, action="scene_bible_spawn", context={"project_id": project_id},
                    )
                    conn.execute(
                        "UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                        (f"场景圣经任务未能启动：{public}", project_id),
                    )
                    conn.commit()
    except asyncio.TimeoutError:
        conn.execute(
            "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
            (f"人物谱解析/修复超时（超过 {timeout_s} 秒），请重新谱写。", project_id),
        )
        conn.commit()
    except asyncio.CancelledError:
        row = conn.execute("SELECT bible_status FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["bible_status"] == "running":
            conn.execute(
                "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
                (BIBLE_INTERRUPTED_ERROR, project_id),
            )
            conn.commit()
        raise
    except (StageError, Exception) as exc:  # noqa: BLE001
        public = errors.record_and_format(exc, action="bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?", (public, project_id))
        conn.commit()


def _new_bible_recorder(
    project_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    chapters = rows_to_dicts(conn.execute(
        "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)
    ).fetchall())
    project = conn.execute(
        "SELECT bible_version, bible_feedback FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    return WorkflowRecorder.create(
        workflow_type="character_bible",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(
            chapters, project["bible_version"] if project else 0,
            project["bible_feedback"] if project else None,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={"max_iterations": 4, "warning_blocks_downstream": True},
        parent_run_id=parent_run_id,
    )


async def _recorded_bible_task(
    project_id: str,
    feedback: str,
    recorder: WorkflowRecorder,
    *,
    trigger_full_refs: bool,
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
            lambda: _bible_task(project_id, feedback, trigger_full_refs=trigger_full_refs),
            contract_key="character_bible",
            agent_name="character_bible",
            context_manifest=context.manifest(),
        )
        row = conn.execute("SELECT bible_status, bible_error FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["bible_status"] == "ready":
            recorder.succeed("人物谱已通过确定性门禁")
        elif row and row["bible_status"] == "warning":
            recorder.partial(row["bible_error"] or "人物谱需要人工修订")
        else:
            recorder.fail(RuntimeError(row["bible_error"] if row else "人物谱生成失败"))
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:
        recorder.fail(exc)
        raise


async def _start_bible_core(project_id: str, feedback: str) -> dict:
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
    conn = get_conn()
    # 持久化 feedback：进程重启后 recover_bible_tasks 能用相同入参续跑，而非中断报错
    conn.execute("UPDATE projects SET bible_status='running', bible_error=NULL, bible_feedback=? WHERE id=?",
                 (feedback, project_id))
    conn.commit()
    recorder = _new_bible_recorder(project_id)
    _track_bible_task(
        project_id,
        asyncio.create_task(
            _recorded_bible_task(project_id, feedback, recorder, trigger_full_refs=True)
        ),
    )
    return {"status": "running", "run_id": recorder.run_id}


@router.post("/projects/{project_id}/bible")
async def start_bible(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, respond_ui

    feedback = str(_as_body_dict(body).get("feedback") or "")
    result = await dispatch(
        "bible.generate", {"project_id": project_id, "feedback": feedback}, initiator="ui"
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
    characters = bible.get("characters") or []
    if character:
        characters = [c for c in characters if c.get("name") == character]
        if not characters:
            raise HTTPException(404, f"角色不存在：{character}")
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
        for c in characters:
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
        image_count = len(characters) * views_per
        for c in characters:
            missing_roles.append({
                "character": c.get("name"),
                "views": list(CHARACTER_REQUIRED_VIEWS) if not view_role else [view_role],
                "reason": "整包生成",
            })
    unit = float(IMAGE_PRICE_PER_UNIT)
    estimated = round(image_count * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed_at = now()
    quote_id = fingerprint({
        "project_id": project_id,
        "character": character,
        "resume": resume,
        "view_role": view_role,
        "image_count": image_count,
        "unit": unit,
        "bible_version": p.get("bible_version"),
    })
    return {
        "quote_id": quote_id,
        "computed_at": computed_at,
        "quote_expires_at": computed_at + 300,
        "project_id": project_id,
        "action": (
            "regenerate_view" if view_role
            else ("resume_missing" if resume else ("regenerate_pack" if character else "generate_all"))
        ),
        "character": character,
        "view_role": view_role,
        "character_count": len(characters),
        "views_per_character": views_per,
        "image_count": image_count,
        "unit_price_cny": unit,
        "estimated_cost_cny": estimated,
        "max_retry_budget_cny": max_retry,
        "budget_cap_cny": max_retry,
        "scope": missing_roles,
        "old_asset_policy": "已落盘且合格的视角保留；失败不替换当前采用包",
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
    return compute_refs_cost_precheck(
        project_id,
        character=payload.get("character"),
        resume=bool(payload.get("resume", False)),
        view_role=payload.get("view_role"),
    )


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


# ---------- 角色定妆照（人物跨集一致性） ----------
# 注：初始定妆在此生成（generate_refs，适用集 1~ 至今）；已有角色的外观漂移重绘已改为分镜阶段
# 按集反应式处理（见 portraits.ensure_cards_for_screenplay），不再有"每 20 集全量轮询"步骤。


async def _refs_task(
    project_id: str,
    only_character: str | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
):
    from app.refs import generate_refs
    conn = get_conn()
    recorder = WorkflowRecorder.create(
        workflow_type="character_references",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, only_character, "character_references"),
        requested_by=requested_by,
        trigger_type=trigger_type,
        budget_limit_cny=float(get_setting("episode_cost_limit_cny") or 100),
        parent_run_id=parent_run_id,
    )
    try:
        recorder.start()
        # 重做定妆照前，先清理旧人物图衍生的评审视频与成品（按受影响角色范围）
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if only_character:
            names = [only_character]
        elif p and p["bible_json"]:
            names = [c["name"] for c in json.loads(p["bible_json"]).get("characters", [])]
        else:
            names = []
        if not resume:
            worker.purge_character_video_artifacts(project_id, names)
        await recorder.step(
            "character_references",
            lambda: generate_refs(project_id, only_character, resume=resume),
            agent_name="reference_asset_loop",
        )
        conn.execute("UPDATE projects SET refs_status='ready', refs_error=NULL WHERE id=?", (project_id,))
        conn.commit()
        recorder.succeed("人物参考资产已生成并通过证据门禁")
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc)
        public = errors.record_and_format(exc, action="refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET refs_status='failed', refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


@router.post("/projects/{project_id}/refs")
async def start_refs(project_id: str, body: dict | None = None):
    from app.capabilities.dispatch import ui_route
    payload = body or {}
    routed = await ui_route(
        "portrait.generate",
        {
            "project_id": project_id,
            "character": payload.get("character"),
            "resume": bool(payload.get("resume", False)),
            "confirm": payload.get("confirm") is True,
            "quote_id": payload.get("quote_id"),
        },
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _refs_task_active(project_id) or p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中")
    if payload.get("confirm") is not True:
        raise HTTPException(
            409,
            detail={
                "code": "PAYMENT_CONFIRM_REQUIRED",
                "message": "必须先完成费用预检并显式确认（confirm=true）",
            },
        )
    only = payload.get("character")
    resume = bool(payload.get("resume", False))
    quote = compute_refs_cost_precheck(project_id, character=only, resume=resume)
    quote_id = payload.get("quote_id")
    if quote_id and quote_id != quote.get("quote_id"):
        raise HTTPException(
            409,
            detail={
                "code": "QUOTE_STALE",
                "message": "费用预检已过期或范围变化，请重新确认",
                "precheck": quote,
            },
        )
    _start_refs_generation(project_id, only, resume=resume)
    return {"status": "running", "quote_id": quote.get("quote_id"), "precheck": quote}


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
        "UPDATE projects SET refs_status='idle', refs_error=NULL, refs_target=NULL WHERE id=?", (project_id,))
    conn.commit()
    was_running = p["refs_status"] == "running"
    return {"stopped": stopped or was_running}


# ---------- 场景图素材库（跨集场景一致性） ----------
# 注：初始批量出图在此（scenes.generate_scene_refs，适用集 1~ 至今）；库外新场景的反应式发现
# 已挂在分镜阶段（见 scenes.ensure_scenes_for_storyboard），不在此轮询。


async def _scene_refs_task(
    project_id: str,
    only_scene: str | None,
    *,
    resume: bool = False,
    parent_run_id: str | None = None,
    requested_by: str = "user",
    trigger_type: str = "manual",
):
    from app.scenes import generate_scene_refs
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
            lambda: generate_scene_refs(project_id, only_scene, resume=resume),
            agent_name="reference_asset_loop",
        )
        conn.execute("UPDATE projects SET scene_refs_status='ready', scene_refs_error=NULL WHERE id=?", (project_id,))
        conn.commit()
        recorder.succeed("场景参考资产已生成并通过证据门禁")
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:  # noqa: BLE001
        recorder.fail(exc)
        public = errors.record_and_format(exc, action="scene_refs_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET scene_refs_status='failed', scene_refs_error=? WHERE id=?",
                     (public, project_id))
        conn.commit()


@router.post("/projects/{project_id}/scene-bible")
async def start_scene_bible(project_id: str):
    """（重新）生成场景圣经并触发场景图批量出图。人物谱必须先就绪。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("scene.generate_bible", {"project_id": project_id})
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _scene_assets_task_active(project_id):
        raise HTTPException(409, "场景图正在生成中")
    conn = get_conn()
    conn.execute("UPDATE projects SET scene_refs_status='running', scene_refs_error=NULL WHERE id=?", (project_id,))
    conn.commit()
    task_registry.spawn(
        "scene_bible", project_id, _scene_bible_and_refs(project_id), project_id=project_id
    )
    return {"status": "running"}


@router.post("/projects/{project_id}/scene-refs")
async def start_scene_refs(project_id: str, body: dict | None = None):
    """（重新）生成场景图。需先有场景圣经（bible.scenes 非空）。可带 only 单场景重做。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "scene.generate_refs",
        {"project_id": project_id, "scene_name": (body or {}).get("scene")},
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"] or not json.loads(p["bible_json"]).get("scenes"):
        raise HTTPException(409, "还没有场景圣经，请先生成场景清单")
    if _scene_assets_task_active(project_id):
        raise HTTPException(409, "场景图正在生成中")
    only = (body or {}).get("scene")
    _start_scene_refs_generation(project_id, only)
    return {"status": "running"}


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
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET scene_refs_status='idle', scene_refs_error=NULL, scene_refs_target=NULL WHERE id=?",
        (project_id,))
    conn.commit()
    was_running = p["scene_refs_status"] == "running"
    return {"stopped": stopped or was_running}


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
    if not payload.get("quote_id") or payload.get("quote_id") != quote.get("quote_id"):
        if payload.get("quote_id"):
            raise HTTPException(
                409,
                detail={
                    "code": "QUOTE_STALE",
                    "message": "费用预检已过期或范围变化，请重新确认",
                    "precheck": quote,
                },
            )
        # Agent/工具路径：confirm=true 且未带 quote_id 时采用当前服务端报价
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

    recorder = WorkflowRecorder.create(
        workflow_type="portrait_view_redo",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(project_id, portrait_id, view_role, quote.get("quote_id")),
        requested_by="user",
        trigger_type="manual",
        config_snapshot={
            "task_key": task_key,
            "character_name": character_name,
            "portrait_id": portrait_id,
            "view_role": view_role,
            "quote_id": quote.get("quote_id"),
        },
        budget_limit_cny=float(quote.get("max_retry_budget_cny") or 1),
    )

    async def _run_redo() -> None:
        from app.multiview import regenerate_character_view, pack_result_ok
        recorder.start()
        try:
            async def _op():
                return await regenerate_character_view(
                    project_id=project_id, portrait_id=portrait_id, view_role=view_role,
                )
            result = await recorder.step(
                "portrait_view_redo",
                _op,
                agent_name="portrait_view_redo",
            )
            if isinstance(result, tuple):
                result = result[1]
            if not pack_result_ok(result):
                recorder.fail(RuntimeError(
                    f"视角重做未通过：{view_role}（status={(result or {}).get('status')}）"
                ))
                return
            recorder.succeed(f"{character_name}/{view_role} 视角已重做并通过整包 QA")
        except asyncio.CancelledError:
            recorder.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            recorder.fail(exc)
            raise

    try:
        task_registry.spawn(
            "portrait_view_redo", task_key, _run_redo(), project_id=project_id,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        "status": "accepted",
        "task_id": f"portrait_view_redo:{task_key}",
        "run_id": recorder.run_id,
        "portrait_id": portrait_id,
        "view_role": view_role,
        "character_name": character_name,
        "precheck": quote,
        "message": "单视角重做任务已受理，可刷新查看进度",
    }


@router.post("/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/views/{view_role}/regenerate")
async def regenerate_scene_view_route(
    project_id: str, scene_name: str, scene_reference_id: str, view_role: str,
):
    """场景库单视角重做。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "scene.regenerate_view",
        {
            "project_id": project_id, "scene_name": scene_name,
            "scene_reference_id": scene_reference_id, "view_role": view_role,
        },
    )
    if routed is not None:
        return routed
    _project_or_404(project_id)
    from app.multiview import regenerate_scene_view, pack_result_ok
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM scene_references WHERE id=? AND project_id=? AND scene_name=?",
        (scene_reference_id, project_id, scene_name),
    ).fetchone()
    if not row:
        raise HTTPException(404, "场景版本不存在")
    try:
        result = await regenerate_scene_view(
            project_id=project_id, scene_reference_id=scene_reference_id, view_role=view_role,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, str(exc)) from exc
    if not pack_result_ok(result):
        raise HTTPException(409, f"视角重做未通过：{view_role}（status={result.get('status')}）")
    return result


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
            "reason": (body or {}).get("reason") or "人工采纳候选",
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
            reason=str((body or {}).get("reason") or "人工采纳候选"),
            decided_by=str((body or {}).get("decided_by") or "user"),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc) or "候选不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


__all__ = [name for name in globals() if not name.startswith("__")]
