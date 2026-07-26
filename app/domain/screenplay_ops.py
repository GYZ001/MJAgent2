from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

def _screenplay_task_active(episode_id: str) -> bool:
    return task_registry.active("screenplay", episode_id)


def _screenplay_fallback_status(ep) -> str:
    if not ep["screenplay_json"]:
        return "pending"
    artifact_id = ep["screenplay_artifact_id"] if "screenplay_artifact_id" in ep.keys() else None
    if not artifact_id:
        return "ready"
    artifact = evidence_repository.get_artifact(artifact_id)
    return "ready" if artifact and artifact["status"] == "approved" else "warning"


def recover_screenplay_tasks() -> int:
    """服务热更/重启后续跑状态为 running 的剧本任务，避免 UI 卡在生成中却没有真实调用。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, project_id FROM episodes WHERE screenplay_status='running'"
    ).fetchall()
    resumed = 0
    for row in rows:
        episode_id = row["id"]
        if _screenplay_task_active(episode_id):
            continue
        stamp = now()
        conn.execute(
            "UPDATE episodes SET screenplay_started_at=COALESCE(screenplay_started_at, ?), screenplay_updated_at=? WHERE id=?",
            (stamp, stamp, episode_id))
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='screenplay' "
            "AND scope_type='episode' AND scope_id=? ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        recorder = _new_screenplay_recorder(
            episode_id,
            requested_by="recovery",
            trigger_type="resume",
            parent_run_id=parent["id"] if parent else None,
        )
        task_registry.spawn(
            "screenplay",
            episode_id,
            _recorded_screenplay_task(episode_id, recorder),
            project_id=row["project_id"],
        )
        resumed += 1
    conn.commit()
    return resumed


async def _screenplay_character_discovery(
    episode_id: str,
    source_text: str,
    *,
    draft_text: str = "",
) -> dict:
    """Run the required incremental cast pass for one screenplay generation."""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("新人物发现", ["剧集不存在"])
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if not project or not (project["bible_json"] or "").strip():
        # Preserve the existing placeholder-bible workflow. Projects with a real
        # bible must pass discovery; placeholder projects can still draft text.
        return {
            "checked": 0, "candidates": [], "added": [], "skipped": [],
            "errors": [], "warnings": ["项目尚无人物谱，已跳过增量人物发现"],
        }
    bible = _project_bible_or_placeholder(project)
    from app.portraits import ensure_cards_for_text

    result = await ensure_cards_for_text(
        ep["project_id"],
        ep["episode_no"],
        source_text,
        bible,
        draft_text=draft_text,
    )
    if result.get("errors"):
        raise StageError("新人物发现", list(result["errors"]))
    for warning in result.get("warnings") or []:
        errors.log_error(
            None,
            action="screenplay_character_discovery_warning",
            context={
                "project_id": ep["project_id"],
                "episode_id": episode_id,
                "episode_no": ep["episode_no"],
            },
            message=warning,
        )
    return result


async def _screenplay_task(
    episode_id: str,
    *,
    preflight_result: dict | None = None,
) -> EpisodeScreenplay | None:
    """一次 Baseline + Production Repair Agent 局部自愈；仅证书通过后写入 published。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    try:
        ep_data = dict(ep)
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        source_text = _episode_source_text(conn, ep)
        if preflight_result is None:
            preflight_result = await _screenplay_character_discovery(episode_id, source_text)
        if preflight_result.get("added"):
            p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
            bible = _project_bible_or_placeholder(p)
        compact_target = _storyboard_target_for_source(ep_data.get("target_duration_s"), len(source_text))
        if compact_target != ep_data.get("target_duration_s"):
            conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
            conn.commit()
            ep_data["target_duration_s"] = compact_target
        prev = conn.execute(
            "SELECT cliffhanger FROM episodes WHERE project_id=? AND episode_no=?",
            (ep["project_id"], ep["episode_no"] - 1)).fetchone()

        # 标记修复中；禁止在修复过程中清空下游
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
            ("生产修复中：首次生成后仅局部 Patch", now(), episode_id),
        )
        conn.commit()

        from app.production.screenplay_repair import run_screenplay_production
        from app.observability.tracing import current_trace

        run_id = None
        try:
            run_id = current_trace().run_id
        except Exception:  # noqa: BLE001
            run_id = None

        # 若已有 published 且用户未 force 新 revision，resume 工作副本
        resume = True
        script = await run_screenplay_production(
            episode_id=episode_id,
            episode=ep_data,
            source_text=source_text,
            bible=bible,
            prev_ending=prev["cliffhanger"] if prev else "",
            run_id=run_id,
            resume=resume,
        )

        # run_screenplay_production 在成功时已 publish；若仍 repairing 则不要写成 ready
        row = conn.execute(
            "SELECT screenplay_status, screenplay_json FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row and row["screenplay_status"] == "ready" and row["screenplay_json"]:
            return _load_screenplay(row) or script

        # 未发布：保持 repairing，不写 warning 候选到页面交付位
        if row and row["screenplay_status"] == "repairing":
            # 工作副本仅存 working artifact；兼容字段不覆盖 published screenplay_json
            return script

        # 兜底：若 publish 已写入
        return script
    except asyncio.CancelledError:
        conn.execute(
            "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
            ("剧本生成已取消，可重新发起。", now(), episode_id))
        conn.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if msg.startswith("WAITING_INPUT"):
            conn.execute(
                "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (msg[:800], now(), episode_id))
            conn.commit()
            return None
        public = errors.record_and_format(exc, action="screenplay_generate", context={"episode_id": episode_id})
        conn.execute(
            "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
            (public, now(), episode_id))
        conn.commit()
        return None


def _new_screenplay_recorder(
    episode_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"episode not found: {episode_id}")
    project = conn.execute(
        "SELECT bible_version FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    source_text = _episode_source_text(conn, ep)
    return WorkflowRecorder.create(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            episode_id,
            ep["source_chapters"],
            source_text,
            project["bible_version"] if project else 0,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "contract": f"screenplay@{get_contract('screenplay').version}",
            "max_iterations": min(max(int(get_setting("max_repair_attempts") or 4), 1), 4),
            "stall_rounds": 2,
            "min_quality_gain": 0.03,
        },
        parent_run_id=parent_run_id,
    )


def _screenplay_context_pack(episode_id: str) -> tuple[list[str], dict]:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    source_text = _episode_source_text(conn, ep)
    bible_artifact = evidence_repository.latest_artifact(
        "character_bible", "project", ep["project_id"]
    )
    mapping_artifact = evidence_repository.latest_artifact(
        "episode_mapping", "project", ep["project_id"]
    )
    previous_artifact_id = ep["screenplay_artifact_id"]
    input_ids = [
        artifact_id
        for artifact_id in (
            bible_artifact["id"] if bible_artifact else None,
            mapping_artifact["id"] if mapping_artifact else None,
            previous_artifact_id,
        )
        if artifact_id
    ]
    pack = ContextPack(
        goal=f"生成第 {ep['episode_no']} 集可拍剧本",
        metadata={
            "episode_id": episode_id,
            "episode_no": ep["episode_no"],
            "contract_version": get_contract("screenplay").version,
        },
    )
    pack.add_text(
        "source_text",
        source_text,
        limit=SCREENPLAY_SOURCE_BUDGET_CHARS,
        truncation_strategy="head_with_truncation_notice",
    )
    bible_json = project["bible_json"] or "{}"
    pack.add_text(
        "character_bible",
        bible_json,
        limit=max(len(bible_json), 1),
        source_artifact_id=bible_artifact["id"] if bible_artifact else None,
        truncation_strategy="none",
    )
    return list(dict.fromkeys(input_ids)), pack.manifest()


async def _recorded_screenplay_task(
    episode_id: str,
    recorder: WorkflowRecorder,
) -> EpisodeScreenplay | None:
    async def operation(preflight: dict) -> EpisodeScreenplay:
        generated = await _screenplay_task(episode_id, preflight_result=preflight)
        if generated is None:
            row = get_conn().execute(
                "SELECT screenplay_error FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            raise RuntimeError(row["screenplay_error"] if row else "剧本任务未产生结果")
        return generated

    try:
        recorder.start()
        discovery_source = _episode_source_text(
            get_conn(),
            get_conn().execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone(),
        )
        _, preflight = await recorder.step(
            "character_discovery",
            lambda: _screenplay_character_discovery(episode_id, discovery_source),
            agent_name="screenplay_character_discovery",
            context_manifest={
                "episode_id": episode_id,
                "source_chars": len(discovery_source),
                "phase": "before_screenplay",
            },
        )
        # Discovery may advance bible_version. Refresh the persisted fingerprint and
        # context pack before the screenplay step so evidence describes the inputs
        # actually used by generation.
        fingerprint_ep = get_conn().execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        fingerprint_project = get_conn().execute(
            "SELECT bible_version FROM projects WHERE id=?", (fingerprint_ep["project_id"],)
        ).fetchone()
        get_conn().execute(
            "UPDATE workflow_runs SET input_fingerprint=?, updated_at=? WHERE id=?",
            (
                fingerprint(
                    episode_id,
                    fingerprint_ep["source_chapters"],
                    discovery_source,
                    fingerprint_project["bible_version"] if fingerprint_project else 0,
                ),
                now(),
                recorder.run_id,
            ),
        )
        get_conn().commit()
        input_artifact_ids, context_manifest = _screenplay_context_pack(episode_id)
        _, script = await recorder.step(
            "screenplay",
            lambda: operation(preflight),
            contract_key="screenplay",
            agent_name="screenplay_agent_loop",
            input_artifact_ids=input_artifact_ids,
            context_manifest=context_manifest,
        )
        row = get_conn().execute(
            "SELECT screenplay_status, screenplay_error FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not row:
            raise RuntimeError("剧本任务完成后剧集记录不存在")
        if row["screenplay_status"] == "ready":
            recorder.succeed("剧本已通过完成凭证并发布")
        elif row["screenplay_status"] == "repairing":
            recorder.partial(row["screenplay_error"] or "剧本自动修复中/等待续跑")
        elif row["screenplay_status"] == "warning":
            # 兼容旧数据：warning 不再是可交付终态
            recorder.partial(row["screenplay_error"] or "旧 warning 候选，已转入修复")
        else:
            recorder.succeed("剧本任务结束")
        return script
    except asyncio.CancelledError:
        recorder.cancel("剧本生成已取消")
        raise
    except Exception as exc:  # noqa: BLE001 -- failure is persisted for Run Center
        row = get_conn().execute(
            "SELECT screenplay_status FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row and row["screenplay_status"] == "running":
            public = errors.record_and_format(
                exc,
                action="screenplay_generate",
                context={"episode_id": episode_id, "phase": "character_discovery"},
            )
            get_conn().execute(
                "UPDATE episodes SET screenplay_status='failed', screenplay_error=?, screenplay_updated_at=? WHERE id=?",
                (public, now(), episode_id),
            )
            get_conn().commit()
        recorder.fail(exc)
        return None


@router.post("/episodes/{episode_id}/screenplay")
async def start_screenplay(episode_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route
    body = _as_body_dict(body)
    routed = await ui_route(
        "screenplay.generate",
        {"episode_id": episode_id, "force": bool(body.get("force"))},
    )
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中，不能同时重写剧本")
    if ep["screenplay_status"] == "running" and _screenplay_task_active(episode_id):
        raise HTTPException(409, "剧本正在生成中")
    if ep["screenplay_status"] == "repairing" and _screenplay_task_active(episode_id):
        raise HTTPException(409, "剧本正在自动修复中")

    # 已有 published / production revision：禁止 force 全量重生；应走 revise 或 resume repair
    from app.production.revision import get_active_production_revision
    active_rev = get_active_production_revision(episode_id, "screenplay")
    published_id = None
    try:
        published_id = ep["published_screenplay_artifact_id"] if "published_screenplay_artifact_id" in ep.keys() else None
    except Exception:  # noqa: BLE001
        published_id = None
    has_product = bool(ep["screenplay_json"]) and ep["screenplay_status"] in {"ready", "repairing", "warning"}
    force = bool(body.get("force"))
    if has_product and (active_rev or published_id or ep["screenplay_status"] == "ready"):
        if force:
            raise HTTPException(
                409,
                "已有可交付剧本，禁止全量重新生成。请使用「让 Agent 按要求迭代」"
                "（POST /episodes/{id}/screenplay/revise）从已发布版本创建工作分支。",
            )
        # 无 force：若 repairing 则续跑；若 ready 则 409
        if ep["screenplay_status"] == "ready":
            raise HTTPException(
                409,
                "本集已有通过凭证的剧本。如需修改请调用 /screenplay/revise。",
            )
        # repairing / warning → 续跑 Repair Agent（不新建 Baseline）
        pass

    conn = get_conn()
    started_at = now()
    conn.execute(
        "UPDATE episodes SET screenplay_status='running', screenplay_error=NULL, screenplay_started_at=?, screenplay_updated_at=? WHERE id=?",
        (started_at, started_at, episode_id))
    conn.commit()
    recorder = _new_screenplay_recorder(episode_id)
    try:
        conn.execute(
            "UPDATE episodes SET active_screenplay_run_id=? WHERE id=?",
            (recorder.run_id, episode_id),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    task_registry.spawn(
        "screenplay",
        episode_id,
        _recorded_screenplay_task(episode_id, recorder),
        project_id=ep["project_id"],
    )
    return {"status": "running", "run_id": recorder.run_id}


@router.post("/episodes/{episode_id}/screenplay/revise")
async def revise_screenplay(episode_id: str, body: dict | None = Body(None)):
    """从已发布版本克隆工作副本，创建新 production revision，仅局部收敛。"""
    body = _as_body_dict(body)
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if not ep["screenplay_json"] or ep["screenplay_status"] not in {"ready", "warning", "repairing"}:
        raise HTTPException(409, "没有可迭代的已有剧本，请先生成可交付剧本")
    if ep["screenplay_status"] == "running" and _screenplay_task_active(episode_id):
        raise HTTPException(409, "剧本任务进行中")

    from app.production.revision import ensure_production_revision, update_working_artifact
    from app.production.patch import screenplay_artifact_payload
    from app.harness.contracts import get_contract
    from app.harness.types import EvidenceArtifact

    script = _load_screenplay(ep)
    if not script:
        raise HTTPException(409, "无法加载已有剧本")
    # 归档旧 revision，新建
    rev = ensure_production_revision(
        episode_id=episode_id,
        kind="screenplay",
        contract_version=get_contract("screenplay").version,
        qa_profile_version="screenplay-qa-1",
        resume=False,
    )
    # 克隆 published 为 working baseline（计数记为已生成，避免再次完整生成）
    payload = screenplay_artifact_payload(script)
    art = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_document",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content=payload,
            parent_artifact_ids=[ep["screenplay_artifact_id"]] if ep["screenplay_artifact_id"] else [],
            contract_version=get_contract("screenplay").version,
        )
    )
    from app.production.revision import mark_baseline_generated, mark_first_evaluation
    mark_baseline_generated(rev.id, baseline_artifact_id=art["id"], working_artifact_id=art["id"])
    # 标记已做过 evaluation 占位，禁止完整生成；随后 Repair 会重跑 QA
    mark_first_evaluation(rev.id, f"revise-seed-{art['id']}")

    started_at = now()
    conn = get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_status='running', screenplay_error=?, screenplay_started_at=?, "
        "screenplay_updated_at=?, working_screenplay_artifact_id=? WHERE id=?",
        ("修订：从已发布版本局部收敛", started_at, started_at, art["id"], episode_id),
    )
    conn.commit()
    recorder = _new_screenplay_recorder(episode_id, trigger_type="revise")
    task_registry.spawn(
        "screenplay",
        episode_id,
        _recorded_screenplay_task(episode_id, recorder),
        project_id=ep["project_id"],
    )
    return {"status": "running", "run_id": recorder.run_id, "revision_id": rev.id, "mode": "revise"}


async def _screenplay_guarded(
    episode_id: str,
    sem: asyncio.Semaphore,
    recorder: WorkflowRecorder,
):
    async with sem:
        await _recorded_screenplay_task(episode_id, recorder)


@router.post("/projects/{project_id}/screenplay-all")
async def start_screenplay_all(project_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.generate_batch", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, screenplay_status, screenplay_json FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,)).fetchall()
    ids = [
        r["id"] for r in rows
        if (
            not r["screenplay_json"]
            or r["screenplay_status"] in ("pending", "failed", "warning", "repairing")
            or (r["screenplay_status"] == "running" and not task_registry.active("screenplay", r["id"]))
        )
        and r["screenplay_status"] != "ready"
    ]
    if not ids:
        raise HTTPException(409, "没有待生成剧本的剧集")
    placeholders = ",".join("?" for _ in ids)
    started_at = now()
    conn.execute(
        f"UPDATE episodes SET screenplay_status='running', screenplay_error=NULL, screenplay_started_at=?, screenplay_updated_at=? WHERE id IN ({placeholders})",
        [started_at, started_at, *ids])
    conn.commit()
    sem = asyncio.Semaphore(max(int(get_setting("storyboard_concurrency") or 2), 1))
    run_ids: list[str] = []
    for eid in ids:
        recorder = _new_screenplay_recorder(eid)
        run_ids.append(recorder.run_id)
        task_registry.spawn(
            "screenplay", eid, _screenplay_guarded(eid, sem, recorder), project_id=project_id
        )
    return {"started": len(ids), "run_ids": run_ids}


@router.post("/projects/{project_id}/screenplay-all/cancel")
async def cancel_screenplay_all(project_id: str):
    """停止本项目所有正在进行的剧本生成：取消在跑任务，未开跑的回退状态。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.cancel", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, screenplay_json FROM episodes WHERE project_id=? AND screenplay_status='running'",
        (project_id,)).fetchall()
    stopped = 0
    for r in rows:
        eid = r["id"]
        await task_registry.cancel_and_wait("screenplay", eid)
        full = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        fallback = _screenplay_fallback_status(full)
        conn.execute(
            "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=? WHERE id=?",
            (fallback, now(), eid))
        stopped += 1
    conn.commit()
    return {"stopped": stopped}


@router.post("/episodes/{episode_id}/screenplay/cancel")
async def cancel_screenplay(episode_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("screenplay.cancel", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    if ep["screenplay_status"] != "running":
        raise HTTPException(409, "当前没有正在进行的剧本生成")
    await task_registry.cancel_and_wait("screenplay", episode_id)
    conn = get_conn()
    fallback = _screenplay_fallback_status(ep)
    conn.execute(
        "UPDATE episodes SET screenplay_status=?, screenplay_error=NULL, screenplay_updated_at=? WHERE id=?",
        (fallback, now(), episode_id))
    conn.commit()
    return {"status": fallback}


@router.put("/episodes/{episode_id}/screenplay")
async def edit_screenplay(episode_id: str, body: dict):
    from app.capabilities.dispatch import ui_route
    payload = body.get("screenplay", body)
    expected_version = body.get("expected_version")
    routed = await ui_route(
        "screenplay.update",
        {
            "episode_id": episode_id,
            "screenplay": payload,
            "force": bool(body.get("force")),
            "reason": body.get("reason"),
            "expected_version": expected_version,
        },
    )
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    current_version = ep.get("screenplay_artifact_id") or ""
    if expected_version is not None and str(expected_version) != str(current_version):
        raise HTTPException(
            409,
            f"剧本版本冲突：当前版本 {current_version or '空'}，请求基于 {expected_version}，请刷新后重试",
        )
    payload = body.get("screenplay", body)
    force = bool(body.get("force"))
    instance, errors = schema_errors(EpisodeScreenplay, payload)
    if errors:
        raise HTTPException(422, "；".join(errors))
    conn = get_conn()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    bible = _project_bible_or_placeholder(p)
    expected = max(1, int(ep["target_duration_s"]) // config.VIDEO_DURATION_MIN_S)
    errors = validate_screenplay(instance, bible, expected, episode_no=ep["episode_no"])
    if errors:
        raise HTTPException(422, "；".join(errors))
    old_script = _load_screenplay(ep)
    instance = _prepare_screenplay_for_storage(
        ep, instance,
        keep_existing_id=(old_script.id if old_script else None),
        keep_created_at=(old_script.created_at if old_script else None),
    )
    has_shots = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"] > 0
    if has_shots and not force:
        raise HTTPException(409, "修改剧本会清空本集现有分镜、参考图、视频和成片，请确认后重试")
    if has_shots:
        worker.delete_episode_shots(episode_id)
    candidate = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="episode_screenplay",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content=instance.model_dump(mode="json"),
            parent_artifact_ids=(
                [ep["screenplay_artifact_id"]]
                if ep["screenplay_artifact_id"]
                else []
            ),
            contract_version=get_contract("screenplay").version,
        )
    )
    adopted = evidence_repository.commit_artifact(
        None,
        candidate["id"],
        [
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="screenplay_validator",
                evaluator_version="1.0.0",
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={"episode_id": episode_id, "source": "manual_edit"},
            ),
            Evaluation(
                evaluator_type="human",
                evaluator_name="screenplay_editor",
                evaluator_version="1.0.0",
                status="passed",
                hard_gate_passed=True,
                evidence={"decision": "approve", "source": "manual_edit"},
            ),
        ],
    )
    conn.execute(
        "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', screenplay_error=NULL, "
        "screenplay_artifact_id=?, status='planned', script_error=NULL WHERE id=?",
        (instance.model_dump_json(), adopted["id"], episode_id))
    conn.commit()
    return {"saved": True, "beats": len(instance.beats), "downstream_cleared": has_shots}

__all__ = [name for name in globals() if not name.startswith("__")]
