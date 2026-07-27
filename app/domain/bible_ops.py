from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

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
    target = str(value or "").strip() or None
    return target, ([target] if target else None)


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


def _start_refs_generation(
    project_id: str,
    only_character: str | None,
    *,
    only_characters: list[str] | None = None,
    resume: bool = False,
    parent_run_id: str | None = None,
) -> bool:
    """启动定妆照任务。

    返回值表示是否成功启动；若已有同项目定妆任务在跑，则直接返回 False。
    """
    if _refs_task_active(project_id):
        return False
    conn = get_conn()
    target_payload = _refs_target_payload(only_character, only_characters)
    if target_payload is None:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=NULL WHERE id=?",
            (project_id,),
        )
    else:
        conn.execute(
            "UPDATE projects SET refs_status='running', refs_error=NULL, refs_target=? WHERE id=?",
            (target_payload, project_id),
        )
    conn.commit()
    task_registry.spawn(
        "refs", project_id,
        _refs_task(
            project_id, only_character, only_characters=only_characters,
            resume=resume, parent_run_id=parent_run_id,
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
        only_character, only_characters = _decode_refs_target(row["refs_target"])
        if _start_refs_generation(
            project_id,
            only_character,
            only_characters=only_characters,
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


async def _start_bible_core(
    project_id: str,
    feedback: str,
    *,
    confirm: bool = False,
    quote_id: str | None = None,
    require_quote_id: bool = False,
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
    precheck = await bible_generate_precheck(project_id)
    if not confirm:
        raise _payment_confirm_required(precheck)
    if quote_id and quote_id != precheck.get("quote_id"):
        raise _quote_stale(precheck)
    if require_quote_id and not quote_id:
        raise _quote_stale(precheck, "费用预检缺失，请重新确认")
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
    quote_id = fingerprint({
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
        "quote_id": quote_id,
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
        characters=_normalize_character_selection(payload.get("characters")),
        resume=bool(payload.get("resume", False)),
        view_role=payload.get("view_role"),
    )


@router.post("/projects/{project_id}/bible/generate-precheck")
async def bible_generate_precheck(project_id: str):
    """首次生成人物谱+定妆的费用与范围预估（只读）。"""
    from app.config import IMAGE_PRICE_PER_UNIT
    from app.multiview import CHARACTER_REQUIRED_VIEWS

    p = _project_or_404(project_id)
    unit = float(IMAGE_PRICE_PER_UNIT)
    views_per = len(CHARACTER_REQUIRED_VIEWS)
    # 初始谱写合同上限 8 角色；若已有 bible 则用真实角色数
    if p.get("bible_json"):
        bible = json.loads(p["bible_json"])
        chars = bible.get("characters") or []
        char_count = len(chars)
        names = [c.get("name") for c in chars if c.get("name")]
        estimate_note = "基于当前人物谱角色数"
    else:
        char_count = 8
        names = []
        estimate_note = "尚无人物谱，按初始上限 8 角色估算；谱写完成后按真实角色数出图"
    image_count = char_count * views_per
    estimated = round(image_count * unit, 2)
    max_retry = round(estimated * 1.5, 2)
    computed_at = now()
    quote_id = fingerprint({
        "project_id": project_id,
        "action": "generate_bible_and_refs",
        "character_count": char_count,
        "image_count": image_count,
        "unit": unit,
        "bible_version": p.get("bible_version"),
    })
    return {
        "quote_id": quote_id,
        "computed_at": computed_at,
        "quote_expires_at": computed_at + 300,
        "project_id": project_id,
        "action": "generate_bible_and_refs",
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


@router.get("/projects/{project_id}/refs/gaps")
async def refs_gaps(project_id: str):
    """扫描定妆缺口：按角色/视角列出缺失原因。"""
    quote = compute_refs_cost_precheck(project_id, resume=True)
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
    if not p.get("bible_json"):
        return {"project_id": project_id, "total": 0, "ready": 0, "failed": 0, "missing": 0, "items": []}
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
            "current": p.get("refs_target") == name,
        })
    return {
        "project_id": project_id,
        "refs_status": p.get("refs_status"),
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
        target_name = payload.get("merge_into_character")
        if not target_name:
            raise HTTPException(422, "merge 需要 merge_into_character")
        matched_item["decision_reason"] = (
            (payload.get("reason") or "").strip()
            or f"合并到已有角色：{target_name}"
        )
        action_result["merge_into_character"] = target_name
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
    old_character = dict(next_bible["characters"][target_idx])
    next_bible["characters"][target_idx] = character_body

    instance, errors = schema_errors(Bible, next_bible)
    if errors:
        raise HTTPException(422, "；".join(errors))
    from app.validators import validate_bible
    errors = validate_bible(instance)
    if errors:
        raise HTTPException(422, "；".join(errors))

    appearance_changed = (
        (old_character.get("appearance_canonical") or "")
        != (character_body.get("appearance_canonical") or "")
    )
    preview = None
    if appearance_changed:
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
                    "message": "角色外观变更必须先完成影响预检并显式确认",
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
            "change_types": preview.get("change_types") if preview else ["text_only"],
            "stale_descendant_ids": revision["stale_descendant_ids"],
            "by_artifact_type": revision["by_artifact_type"],
            "rebuild": preview.get("rebuild") if preview else None,
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
            "SELECT * FROM character_portrait_views WHERE portrait_id=? ORDER BY created_at",
            (portrait_id,),
        ).fetchall())
    except Exception:  # noqa: BLE001
        return []
    views: list[dict] = []
    for row in rows:
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


def _portrait_gate_lists(row, views: list[dict]) -> tuple[list[str], list[str]]:
    group_qa = _parse_json_value(row["group_qa_json"] if "group_qa_json" in row.keys() else None, {})
    hard: list[str] = []
    soft: list[str] = []
    if isinstance(group_qa, dict):
        hard.extend(str(x) for x in (group_qa.get("hard_failures") or []) if str(x).strip())
        soft.extend(str(x) for x in (group_qa.get("issues") or []) if str(x).strip())
        if group_qa.get("status") and group_qa.get("status") != "ready":
            soft.append(f"group_qa_status={group_qa.get('status')}")
        soft.extend(str(x) for x in (group_qa.get("failed_views") or []) if str(x).strip())
        for view in group_qa.get("views") or []:
            if not isinstance(view, dict):
                continue
            hard.extend(str(x) for x in (view.get("hard_failures") or []) if str(x).strip())
            soft.extend(str(x) for x in (view.get("issues") or []) if str(x).strip())
    for view in views:
        qa = view.get("qa") if isinstance(view.get("qa"), dict) else {}
        hard.extend(str(x) for x in (qa.get("hard_failures") or []) if str(x).strip())
        soft.extend(str(x) for x in (qa.get("issues") or []) if str(x).strip())
        if view.get("status") and view.get("status") != "ready":
            soft.append(f"{view.get('view_role')}:status={view.get('status')}")
    pack_status = row["pack_status"] if "pack_status" in row.keys() else None
    if pack_status and pack_status != "ready":
        soft.append(f"pack_status={pack_status}")
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
    ep_end = max(int(row["ep_start"] or 1) - 1, 0)
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
        "UPDATE character_portraits SET ep_end=NULL, change_json=? WHERE id=?",
        (json.dumps(change, ensure_ascii=False), row["id"]),
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
    return {"portrait_id": row["id"], "character_name": character_name, "ep_start": row["ep_start"]}


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
    views = _portrait_views_for(conn, portrait_id)
    hard, soft = _portrait_gate_lists(row, views)
    if hard:
        raise HTTPException(
            409,
            detail={
                "code": "PORTRAIT_HARD_FAILED",
                "message": "候选包存在硬失败，禁止采纳",
                "hard_failures": hard,
                "candidate": _portrait_candidate_payload(row, views),
            },
        )
    if soft and (not bypass_soft or not reason.strip()):
        raise HTTPException(
            409,
            detail={
                "code": "PORTRAIT_SOFT_WARNING_CONFIRM_REQUIRED",
                "message": "候选包存在软警告，需 bypass_soft=true 并填写 reason",
                "warnings": soft,
                "candidate": _portrait_candidate_payload(row, views),
            },
        )
    result = _set_current_portrait(
        conn, project_id, character_name, row, reason=reason, decision=decision,
    )
    return {**result, "soft_warnings": soft, "candidate": _portrait_candidate_payload(row, views)}


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
    return {
        "project_id": project_id,
        "character_name": character_name,
        "items": [_portrait_candidate_payload(row) for row in rows],
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
        input_fingerprint=fingerprint(project_id, only_character, only_characters, "character_references"),
        requested_by=requested_by,
        trigger_type=trigger_type,
        config_snapshot={
            "only_character": only_character,
            "only_characters": only_characters,
            "resume": resume,
        },
        budget_limit_cny=float(get_setting("episode_cost_limit_cny") or 100),
        parent_run_id=parent_run_id,
    )
    try:
        recorder.start()
        # 重做定妆照前，先清理旧人物图衍生的评审视频与成品（按受影响角色范围）
        p = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if only_characters:
            names = only_characters
        elif only_character:
            names = [only_character]
        elif p and p["bible_json"]:
            names = [c["name"] for c in json.loads(p["bible_json"]).get("characters", [])]
        else:
            names = []
        if not resume:
            worker.purge_character_video_artifacts(project_id, names)
        await recorder.step(
            "character_references",
            lambda: generate_refs(
                project_id, only_character, only_characters=only_characters, resume=resume
            ),
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
        },
    )
    if routed is not None:
        return routed
    p = _project_or_404(project_id)
    if not p["bible_json"]:
        raise HTTPException(409, "请先生成角色圣经")
    if _refs_task_active(project_id) or p["refs_status"] == "running":
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
    if not quote_id or quote_id != quote.get("quote_id"):
        raise _quote_stale(quote)
    generation_only = selected_names[0] if selected_names and len(selected_names) == 1 else only
    _start_refs_generation(
        project_id,
        generation_only,
        only_characters=selected_names,
        resume=resume,
    )
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
