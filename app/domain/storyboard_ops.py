from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

def _shot_contract_json(shot: Shot) -> str:
    from app.continuity import shot_contract_dict
    return json.dumps(shot_contract_dict(shot), ensure_ascii=False)


def _apply_contract_to_public_shot(target: dict) -> None:
    from app.continuity import apply_shot_contract, max_speech_chars, spoken_chars_from_shot
    shot = Shot(
        shot_no=target["shot_no"],
        duration_s=target["duration_s"],
        shot_size=target["shot_size"],
        camera_move=target["camera_move"],
        scene_setting=target["scene_setting"],
        scene_name=target.get("scene_name") or "",
        characters=target.get("characters") or [],
        action_desc=target["action_desc"],
        first_frame_desc=target.get("first_frame_desc") or "",
        last_frame_desc=target.get("last_frame_desc") or "",
        source_excerpt=target.get("source_excerpt") or "",
        narration=target.get("narration"),
        dialogues=target.get("dialogues") or [],
        transition=target.get("transition") or "硬切",
        continuity_from_prev=bool(target.get("continuity_from_prev")),
        continuity_mode=target.get("continuity_mode") or "",
        observed_state_out=target.get("observed_state_out") or "",
    )
    apply_shot_contract(shot, target.get("shot_contract_json"))
    for key, value in shot.model_dump(mode="json").items():
        if key in target or key in {
            "story_event_id", "purpose", "spine_beat_ids", "key_line_ids", "information_ids",
            "new_information_ids", "reinforcement_info_ids", "spoken_contract_status",
            "state_in", "primary_action", "emotion_beat", "state_out", "observed_state_out",
            "continuity_mode", "characters_visible", "audio_cast", "audio_timeline",
            "required_text", "reference_roles", "do_not_repeat", "risk_tags",
            "prompt_contract_version", "legacy_unvalidated", "camera_angle",
            "spatial_anchor", "is_final",
        }:
            target[key] = value
    target["spoken_content_chars"] = spoken_chars_from_shot(shot)
    target["spoken_limit"] = max_speech_chars(int(target.get("duration_s") or shot.duration_s))
    target["has_legacy_narration"] = bool((target.get("narration") or "").strip())


def _insert_storyboard_shot(conn, episode_id: str, screenplay: EpisodeScreenplay, shot: Shot) -> str:
    shot_id = new_id("shot")
    shot.action_desc = normalize_action_desc(shot.action_desc)
    conn.execute(
        "INSERT INTO shots(id, episode_id, script_id, shot_no, duration_s, shot_size, camera_move, scene_setting, scene_name, characters, action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, dialogues, transition, continuity_from_prev, shot_contract_json, continuity_mode, observed_state_out, storyboard_artifact_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (shot_id, episode_id, screenplay.id, shot.shot_no, shot.duration_s, shot.shot_size, shot.camera_move,
         shot.scene_setting, shot.scene_name or None, json.dumps(shot.characters, ensure_ascii=False), shot.action_desc,
         shot.first_frame_desc, shot.last_frame_desc, shot.source_excerpt, shot.narration,
         json.dumps([d.model_dump() for d in shot.dialogues], ensure_ascii=False),
         shot.transition, int(shot.continuity_from_prev), _shot_contract_json(shot),
         shot.continuity_mode, shot.observed_state_out,
         getattr(shot, "evidence_artifact_id", None)))
    return shot_id


def _sync_storyboard_shot_timing(conn, episode_id: str, board: Storyboard) -> None:
    for shot in board.shots:
        conn.execute(
            "UPDATE shots SET duration_s=?, transition=?, continuity_from_prev=?, last_frame_desc=?, shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE episode_id=? AND shot_no=?",
            (shot.duration_s, shot.transition, int(shot.continuity_from_prev), shot.last_frame_desc,
             _shot_contract_json(shot), shot.continuity_mode, shot.observed_state_out,
             episode_id, shot.shot_no),
        )


def _persist_storyboard_character_policy_repairs(
    conn, episode_id: str, board: Storyboard, changes: list[dict]
) -> list[str]:
    """Persist deterministic repairs as derived T1 candidates, preserving lineage.

    The character-policy evaluation only proves this normalization, not every storyboard
    gate, so the derived artifact must not be committed as T2 on its own.
    """
    material = [change for change in changes if change.get("mutated")]
    if not material:
        return []
    contract_version = get_contract("storyboard").version
    artifact_ids: list[str] = []
    by_shot = {shot.shot_no: shot for shot in board.shots}
    for shot_no in dict.fromkeys(int(change["shot_no"]) for change in material):
        row = conn.execute(
            "SELECT id, storyboard_artifact_id FROM shots WHERE episode_id=? AND shot_no=?",
            (episode_id, shot_no),
        ).fetchone()
        shot = by_shot.get(shot_no)
        if row is None or shot is None:
            continue
        shot_changes = [change for change in material if int(change["shot_no"]) == shot_no]
        previous_artifact_id = row["storyboard_artifact_id"]
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_shot",
            scope_type="storyboard_checkpoint",
            scope_id=f"{episode_id}:{shot_no}",
            status="candidate",
            trust_level="T1",
            content=shot.model_dump(mode="json"),
            parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
            contract_version=contract_version,
        ))
        evidence_repository.create_evaluation(
            artifact["id"],
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="storyboard_character_policy",
                evaluator_version=contract_version,
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={
                    "policy": "functional_extra_v1",
                    "scope": "character_policy_only",
                    "changes": shot_changes,
                },
            ),
        )
        if previous_artifact_id:
            evidence_repository.invalidate_descendants(
                previous_artifact_id,
                f"镜头角色合同已由 {artifact['id']} 修订",
                exclude_ids={str(artifact["id"])},
            )
        has_runtime_derivatives = conn.execute(
            """SELECT EXISTS(SELECT 1 FROM shot_versions WHERE shot_id=?)
                      OR EXISTS(SELECT 1 FROM shot_scenes WHERE shot_id=?) AS present""",
            (row["id"], row["id"]),
        ).fetchone()["present"]
        if has_runtime_derivatives:
            worker.clear_shot_artifacts(row["id"])
        conn.execute(
            """UPDATE shots SET characters=?, action_desc=?, first_frame_desc=?,
               last_frame_desc=?, narration=?, dialogues=?, shot_contract_json=?,
               continuity_mode=?, observed_state_out=?, storyboard_artifact_id=? WHERE id=?""",
            (
                json.dumps(shot.characters, ensure_ascii=False),
                shot.action_desc,
                shot.first_frame_desc,
                shot.last_frame_desc,
                shot.narration,
                json.dumps([dialogue.model_dump() for dialogue in shot.dialogues], ensure_ascii=False),
                _shot_contract_json(shot),
                shot.continuity_mode,
                shot.observed_state_out,
                artifact["id"],
                row["id"],
            ),
        )
        artifact_ids.append(str(artifact["id"]))
        log_provider_call(
            "storyboard_character_policy",
            config.MODEL_TEXT,
            "CHARACTER_POLICY_REPAIRED",
            None,
            0,
            meta={
                "episode_id": episode_id,
                "shot_no": shot_no,
                "contract_version": contract_version,
                "artifact_id": artifact["id"],
                "changes": shot_changes,
            },
        )
    conn.commit()
    return artifact_ids


def _finalize_storyboard_evidence(episode_id: str, board: Storyboard) -> str:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    shot_rows = conn.execute(
        "SELECT storyboard_artifact_id FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    parents = [
        artifact_id for artifact_id in (
            project["bible_artifact_id"], ep["screenplay_artifact_id"],
            *(row["storyboard_artifact_id"] for row in shot_rows),
        ) if artifact_id
    ]
    contract_version = get_contract("storyboard").version
    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T2",
        content=board.model_dump(mode="json"),
        parent_artifact_ids=parents,
        contract_version=contract_version,
    ))
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="storyboard_full_gate",
        evaluator_version=contract_version,
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={
            "shot_count": len(board.shots),
            "duration_range_s": [config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S],
            "duration_decided_by": "model",
            "checkpoint_artifact_ids": parents,
        },
    )
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [evaluation])
    conn.execute(
        "UPDATE episodes SET storyboard_artifact_id=? WHERE id=?", (artifact["id"], episode_id)
    )
    conn.commit()
    return str(artifact["id"])


def _soft_gap_continue_residual(residual: list[str]) -> bool:
    """是否仅为「暂不能收尾 / 继续补镜」软缺口（可清 is_final 续跑的那一类）。"""
    return (
        len(residual) == 1
        and "暂不能收尾" in residual[0]
        and "继续补镜" in residual[0]
    )


def _can_continue_for_soft_gap(
    *,
    is_final: bool,
    completed_count: int,
    planned_count: int,
    max_shots: int,
    residual: list[str],
) -> bool:
    """软缺口是否允许再开下一镜。

    有大纲时：只有计划里还剩未执行节拍才允许续跑（covers 语义拆分胀长后 planned_count 会变大）。
    计划已跑完、或已到软预算/硬上限时，禁止再发明大纲外幻觉镜。
    """
    from app.renderability import SHOT_SOFT_MAX

    if not is_final:
        return False
    if completed_count >= max_shots or completed_count >= SHOT_SOFT_MAX:
        return False
    # planned_count>0：大纲驱动；已达当前计划长度则禁止计划外补镜。
    if planned_count > 0 and completed_count >= planned_count:
        return False
    return _soft_gap_continue_residual(residual)


def _reconcile_storyboard_plan(conn, episode_id: str, episode_no: int,
                              outline: StoryboardOutline | None, completed: list[Shot],
                              persisted_total: int) -> tuple[int, int, str] | None:
    """让落库大纲成为唯一事实源，消除"规划十几镜却分镜24"的困惑。

    逐镜阶段大纲会被就地改写：①covers 不可单镜完成时 _maybe_split_outline_covers 会插入新节拍；
    ②模型判断单镜超过 10 秒仍演不完而继续拆镜、镜头数超出计划长度。两种情况下内存 outline 都会领先于
    落库的 storyboard_outline_json，导致前端 storyboard_planned_shots 显示陈旧的初始估算。

    本函数在每提交一镜后把当前计划追平实际镜头数并回写 DB，使规划数随逐镜细化实时自更新、
    单调不减且始终 ≥ 已通过镜头数。返回 (from_total, to_total, reason) 供事件记录；无变化返回 None。
    """
    if outline is None:
        return None
    appended = False
    # 模型拆镜超出计划、但未触发 covers 自动拆分：补占位节拍，让计划长度追平实际。
    if len(outline.shots) < len(completed):
        appended = True
        for shot in completed[len(outline.shots):]:
            raw = (shot.action_desc or shot.narration or "").strip()
            beat = "".join(raw.split())[:60] or "逐镜细化新增镜头"
            outline.shots.append(StoryboardOutlineShot(
                shot_no=len(outline.shots) + 1,
                scene_setting=shot.scene_setting or "",
                beat=beat,
                covers="",
            ))
    to_total = len(outline.shots)
    if to_total == persisted_total:
        return None
    conn.execute("UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
                 (outline.model_dump_json(), episode_id))
    conn.commit()
    reason = "shot_overflow" if appended else "covers_split"
    log_provider_call(
        "storyboard_plan_revised", config.MODEL_TEXT, "PLAN_REVISED", None, 0,
        meta={"episode_id": episode_id, "episode_no": episode_no, "stage": "分镜脚本",
              "from": persisted_total, "to": to_total,
              "actual_shots": len(completed), "reason": reason})
    return (persisted_total, to_total, reason)


async def _storyboard_task(
    episode_id: str,
    *,
    resume: bool = True,
    completion_mode: str = "ready_for_manual_confirm",
    completion_grant_id: str | None = None,
    run_id: str | None = None,
):
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    try:
        conn.execute("UPDATE episodes SET status='scripting', script_error=NULL, storyboard_warning=NULL WHERE id=?", (episode_id,))
        conn.commit()
        ep_data = dict(ep)
        screenplay = _load_screenplay(ep)
        if screenplay is None or ep["screenplay_status"] != "ready":
            raise StageError("分镜脚本", ["请先生成并确认本集可拍剧本，再展开分镜"])
        p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        bible = _project_bible_or_placeholder(p)
        # 定妆照按集反应式维护（在分镜展开前）
        from app.portraits import ensure_cards_for_screenplay
        disc = await ensure_cards_for_screenplay(
            ep["project_id"], ep["episode_no"], screenplay, bible,
        )
        if disc.get("blocking_errors"):
            raise StageError("新人物发现", list(disc["blocking_errors"]))
        for warning in disc.get("errors") or []:
            errors.log_error(
                None,
                action="storyboard_character_maintenance_warning",
                context={
                    "project_id": ep["project_id"],
                    "episode_id": episode_id,
                    "episode_no": ep["episode_no"],
                },
                message=warning,
            )
        if disc.get("added") or disc.get("redrawn"):
            p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
            bible = _project_bible_or_placeholder(p)
        try:
            from app.scenes import ensure_scenes_for_storyboard
            sdisc = await ensure_scenes_for_storyboard(ep["project_id"], ep["episode_no"], screenplay, bible)
            if sdisc.get("added"):
                p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
                bible = _project_bible_or_placeholder(p)
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(
                exc,
                action="storyboard_scene_lib_degraded",
                context={
                    "project_id": ep["project_id"],
                    "episode_id": episode_id,
                    "episode_no": ep["episode_no"],
                },
            )
            conn.execute(
                "UPDATE episodes SET storyboard_warning=? WHERE id=?",
                (f"场景库维护失败，已按现有库继续分镜：{public}", episode_id),
            )
            conn.commit()

        # 集级 Supervisor：大纲 → 逐镜 → 整集校验 → 修复 / 自动确认
        from app.storyboard_supervisor import run_storyboard_supervisor
        mode = completion_mode if completion_mode in {
            "ready_for_manual_confirm", "auto_confirm",
        } else "ready_for_manual_confirm"
        # 从 episode 列回读（API 启动时写入）
        try:
            ep_mode = ep["storyboard_completion_mode"]
            if ep_mode in {"ready_for_manual_confirm", "auto_confirm"}:
                mode = ep_mode
        except (KeyError, IndexError, TypeError):
            pass
        grant_id = completion_grant_id
        await run_storyboard_supervisor(
            episode_id,
            resume=resume,
            completion_mode=mode,  # type: ignore[arg-type]
            completion_grant_id=grant_id,
            run_id=run_id,
            preflight_done=True,
        )
    except (StageError, Exception) as exc:  # noqa: BLE001
        rec = errors.log_error(exc, action="storyboard_generate", context={"episode_id": episode_id})
        saved = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
        # Supervisor 已把 WAITING_* 写为 scripted+script_error；此处只处理未捕获异常
        ep_now = conn.execute("SELECT status, script_error FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if ep_now and ep_now["status"] in {"scripted", "confirmed", "scripting"} and ep_now["script_error"]:
            return
        if saved:
            note = (
                f"追加镜生成失败，已保留前 {saved} 个 QA 通过镜头，可人工补写最后一镜、修改后确认，"
                f"或重新生成分镜。（{rec.code} · {rec.error_id}）"
            )
            conn.execute("UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                         (note[:800], episode_id))
        else:
            conn.execute("UPDATE episodes SET status='script_failed', script_error=? WHERE id=?",
                         (rec.public, episode_id))
        conn.commit()


def recover_storyboard_tasks() -> int:
    """恢复中断的分镜任务；优先读 Supervisor Checkpoint。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, project_id FROM episodes "
        "WHERE status='scripting' AND screenplay_status='ready' AND screenplay_json IS NOT NULL"
    ).fetchall()
    resumed = 0
    for row in rows:
        if not task_registry.active("storyboard", row["id"]):
            parent = conn.execute(
                "SELECT id FROM workflow_runs WHERE workflow_type='storyboard' "
                "AND scope_type='episode' AND scope_id=? AND status='PAUSED_EXTERNAL' "
                "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            # 用户取消的 run 永不自动恢复
            cancelled = conn.execute(
                """SELECT id FROM workflow_runs
                   WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
                     AND status='CANCELLED' ORDER BY updated_at DESC LIMIT 1""",
                (row["id"],),
            ).fetchone()
            if cancelled and not parent:
                # 若最近是取消且无 PAUSED_EXTERNAL，跳过
                latest = conn.execute(
                    """SELECT status FROM workflow_runs
                       WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
                       ORDER BY updated_at DESC LIMIT 1""",
                    (row["id"],),
                ).fetchone()
                if latest and latest["status"] == "CANCELLED":
                    continue
            recorder = _new_storyboard_recorder(
                row["id"], requested_by="system", trigger_type="resume",
                parent_run_id=parent["id"] if parent else None,
            )
            task_registry.spawn(
                "storyboard", row["id"],
                _recorded_storyboard_task(row["id"], recorder, resume=True),
                project_id=row["project_id"],
            )
            resumed += 1
    return resumed


def _new_storyboard_recorder(
    episode_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
    completion_mode: str = "ready_for_manual_confirm",
) -> WorkflowRecorder:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    checkpoints = rows_to_dicts(conn.execute(
        "SELECT shot_no, storyboard_artifact_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall())
    contract = get_contract("storyboard")
    return WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            ep["screenplay_artifact_id"], ep["storyboard_outline_json"], checkpoints
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={
            "supervisor": True,
            "completion_mode": completion_mode,
            "checkpoint": "supervisor_and_per_shot",
            "max_iterations_per_shot": contract.max_iterations,
            "max_inner_iterations": 4,
            "max_repair_epochs_per_activation": 6,
            "blocker_warning_candidate_allowed": False,
            "provider_retry": {
                "max_retries_per_call": config.TEXT_PROVIDER_MAX_RETRIES,
                "base_delay_s": config.TEXT_PROVIDER_RETRY_BASE_DELAY,
                "strategy": "bounded_exponential_backoff_same_request",
            },
        },
        config_snapshot={"storyboard_shot_max_tokens": config.STORYBOARD_SHOT_MAX_TOKENS},
        parent_run_id=parent_run_id,
    )


async def _recorded_storyboard_task(
    episode_id: str,
    recorder: WorkflowRecorder,
    *,
    resume: bool,
    completion_mode: str = "ready_for_manual_confirm",
    completion_grant_id: str | None = None,
) -> None:
    recorder.start()
    try:
        conn = get_conn()
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        project = conn.execute("SELECT bible_artifact_id FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        input_ids = [
            artifact_id for artifact_id in (
                project["bible_artifact_id"] if project else None,
                ep["screenplay_artifact_id"],
            ) if artifact_id
        ]
        context = ContextPack(goal="集级 Supervisor：生成整集分镜直至通过并可自动确认")
        if ep["screenplay_json"]:
            context.add_text(
                "screenplay", ep["screenplay_json"],
                source_artifact_id=ep["screenplay_artifact_id"], limit=24000,
            )
        await recorder.step(
            "storyboard",
            lambda: _storyboard_task(
                episode_id,
                resume=resume,
                completion_mode=completion_mode,
                completion_grant_id=completion_grant_id,
                run_id=recorder.run_id,
            ),
            contract_key="storyboard",
            agent_name="storyboard_supervisor",
            input_artifact_ids=input_ids,
            context_manifest=context.manifest(),
        )
        result = conn.execute(
            "SELECT status, script_error, storyboard_artifact_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if result and result["status"] == "confirmed":
            recorder.succeed("分镜已自动确认（尚未产生视频费用）")
        elif result and result["status"] == "scripted" and result["storyboard_artifact_id"] and not result["script_error"]:
            recorder.succeed("分镜已完成，等待人工确认")
        elif result and result["status"] == "scripted" and result["script_error"]:
            # WAITING_HUMAN / 授权失效等：不是 PARTIAL 业务伪造成功，记为等待态成功保留
            recorder.partial(result["script_error"])
        elif result and result["status"] == "scripting":
            recorder.partial(result["script_error"] or "Supervisor 暂停，待恢复")
        else:
            recorder.fail(RuntimeError(result["script_error"] if result else "分镜生成失败"))
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:
        recorder.fail(exc)
        raise


@router.post("/episodes/{episode_id}/storyboard")
async def start_storyboard(episode_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route
    payload = {"episode_id": episode_id, "mode": "fresh", **(body or {})}
    routed = await ui_route("storyboard.generate", payload)
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中")
    if not _screenplay_ready(ep):
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    body = body or {}
    completion_mode = body.get("completion_mode") or "ready_for_manual_confirm"
    if completion_mode not in {"ready_for_manual_confirm", "auto_confirm"}:
        raise HTTPException(422, "completion_mode 只能是 ready_for_manual_confirm 或 auto_confirm")
    completion_grant_id = body.get("completion_grant_id")
    conn = get_conn()
    if completion_mode == "auto_confirm":
        from app.completion_grant import issue_completion_grant
        p = conn.execute("SELECT bible_artifact_id FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
        grant, _token = issue_completion_grant(
            episode_id=episode_id,
            project_id=ep["project_id"],
            screenplay_artifact_id=ep["screenplay_artifact_id"] or "",
            bible_artifact_id=p["bible_artifact_id"] if p else None,
            impact_snapshot={
                "unlocks_paid_video": True,
                "auto_submit_video": False,
                "scope": "episode_storyboard_only",
            },
        )
        completion_grant_id = grant.grant_id
    # 迁移列可能尚未存在：容错写入
    try:
        conn.execute(
            "UPDATE episodes SET status='scripting', script_error=NULL, "
            "storyboard_completion_mode=?, active_storyboard_run_id=NULL WHERE id=?",
            (completion_mode, episode_id),
        )
    except Exception:  # noqa: BLE001
        conn.execute(
            "UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?",
            (episode_id,),
        )
    conn.commit()
    recorder = _new_storyboard_recorder(episode_id, completion_mode=completion_mode)
    try:
        conn.execute(
            "UPDATE episodes SET active_storyboard_run_id=? WHERE id=?",
            (recorder.run_id, episode_id),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    task_registry.spawn(
        "storyboard", episode_id,
        _recorded_storyboard_task(
            episode_id, recorder, resume=False,
            completion_mode=completion_mode,
            completion_grant_id=completion_grant_id,
        ),
        project_id=ep["project_id"],
    )
    return {
        "status": "accepted" if completion_mode == "auto_confirm" else "scripting",
        "run_id": recorder.run_id,
        "goal": "generate_and_confirm" if completion_mode == "auto_confirm" else "generate_ready",
        "completion_mode": completion_mode,
        "completion_grant_id": completion_grant_id,
        "resource_uri": f"manju://runs/{recorder.run_id}",
    }


@router.post("/episodes/{episode_id}/storyboard/resume")
async def resume_storyboard(episode_id: str, body: dict | None = Body(None)):
    """从 Supervisor Checkpoint / 已验证前缀恢复。"""
    from app.capabilities.dispatch import ui_route
    payload = {"episode_id": episode_id, "mode": "resume", **(body or {})}
    routed = await ui_route("storyboard.generate", payload)
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting" or task_registry.active("storyboard", episode_id):
        raise HTTPException(409, "分镜正在生成中")
    if not _screenplay_ready(ep):
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    conn = get_conn()
    saved = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"]
    from app.storyboard_supervisor import load_latest_checkpoint
    cp = load_latest_checkpoint(episode_id)
    if not saved and (cp is None or cp.validated_prefix_end <= 0):
        raise HTTPException(409, "当前没有可恢复的 Supervisor / 逐镜 checkpoint，请重新生成分镜")
    last_row = conn.execute(
        "SELECT shot_no, shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no DESC LIMIT 1",
        (episode_id,),
    ).fetchone()
    if last_row and last_row["shot_contract_json"] and not (cp and cp.phase in {
        "WAITING_HUMAN", "WAITING_AUTHORIZATION", "PAUSED_EXTERNAL", "REPAIRING", "CONFIRMING",
    }):
        try:
            last_contract = json.loads(last_row["shot_contract_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            last_contract = {}
        if bool(last_contract.get("is_final")) and ep["status"] not in {"scripted"}:
            pass  # Supervisor 可能需要整集校验/确认，允许恢复
        elif bool(last_contract.get("is_final")) and not (body or {}).get("force"):
            # 已收束且无待修复时，默认禁止盲目续跑加镜
            if cp is None or cp.phase == "SUCCEEDED":
                raise HTTPException(
                    409,
                    f"第 {last_row['shot_no']} 镜已标记收束（is_final），禁止再续跑追加镜头；"
                    "若要重做请点击「重新生成分镜」",
                )
    parent = conn.execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    try:
        completion_mode = ep["storyboard_completion_mode"] or "ready_for_manual_confirm"
    except (KeyError, IndexError, TypeError):
        completion_mode = (body or {}).get("completion_mode") or "ready_for_manual_confirm"
    grant_id = (body or {}).get("completion_grant_id") or (cp.completion_grant_id if cp else None)
    conn.execute(
        "UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,)
    )
    conn.commit()
    recorder = _new_storyboard_recorder(
        episode_id,
        trigger_type="resume",
        parent_run_id=parent["id"] if parent else None,
        completion_mode=completion_mode,
    )
    task_registry.spawn(
        "storyboard",
        episode_id,
        _recorded_storyboard_task(
            episode_id, recorder, resume=True,
            completion_mode=completion_mode,
            completion_grant_id=grant_id,
        ),
        project_id=ep["project_id"],
    )
    return {
        "status": "scripting",
        "run_id": recorder.run_id,
        "resumed_from_shot": int(saved),
        "next_shot_no": int(saved) + 1,
        "completion_mode": completion_mode,
    }


async def _storyboard_guarded(episode_id: str, sem: asyncio.Semaphore):
    """带并发上限的分镜任务，用于批量生成时不一次性打爆模型网关。"""
    async with sem:
        recorder = _new_storyboard_recorder(episode_id, trigger_type="batch")
        await _recorded_storyboard_task(episode_id, recorder, resume=True)
        return recorder.run_id


@router.post("/projects/{project_id}/storyboard-all")
async def start_storyboard_all(project_id: str):
    """为本项目所有【待分镜】(planned) 剧集批量生成分镜，限并发逐集触发。
    必须是 async def：sync 路由跑在无事件循环的线程池里，asyncio.create_task 会抛
    'no running event loop'，导致状态已置为 scripting 但任务从未启动（前端显示分镜中、模型却收不到请求）。
    同时回收状态卡在 scripting 但无在跑任务的孤儿集，便于一键修复。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("storyboard.generate_batch", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, status, screenplay_status, screenplay_json FROM episodes WHERE project_id=? AND status IN ('planned','scripting','script_failed') ORDER BY episode_no",
        (project_id,)).fetchall()
    # 待分镜的；以及卡在“分镜中”却没有在跑任务的孤儿（需重新触发）
    ids = [
        r["id"] for r in rows
        if r["screenplay_status"] == "ready" and r["screenplay_json"]
        and (r["status"] in ("planned", "script_failed")
             or not task_registry.active("storyboard", r["id"]))
    ]
    if not ids:
        raise HTTPException(409, "没有可展开分镜的剧集（需先生成剧本，且状态为待分镜/分镜失败/卡住的分镜中）")
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"UPDATE episodes SET status='scripting', script_error=NULL WHERE id IN ({placeholders})", ids)
    conn.commit()
    sem = asyncio.Semaphore(max(int(get_setting("storyboard_concurrency") or 2), 1))
    run_ids: list[str] = []
    for eid in ids:
        recorder = _new_storyboard_recorder(eid, trigger_type="batch")
        run_ids.append(recorder.run_id)
        task_registry.spawn(
            "storyboard", eid,
            _storyboard_guarded_recorded(eid, sem, recorder),
            project_id=project_id,
        )
    return {"started": len(ids), "run_ids": run_ids}


async def _storyboard_guarded_recorded(
    episode_id: str, sem: asyncio.Semaphore, recorder: WorkflowRecorder
) -> None:
    async with sem:
        await _recorded_storyboard_task(episode_id, recorder, resume=True)


@router.post("/episodes/{episode_id}/storyboard/cancel")
async def cancel_storyboard(episode_id: str):
    """手动取消正在进行的分镜生成请求，解除 scripting 锁定，便于重新发起。
    用于模型侧卡死/异常导致状态长期停留在“分镜中”的情况。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("storyboard.cancel", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    if ep["status"] != "scripting":
        raise HTTPException(409, "当前没有正在进行的分镜生成")
    await task_registry.cancel_and_wait("storyboard", episode_id)
    from app.completion_grant import revoke_active_grants_for_episode
    revoke_active_grants_for_episode(episode_id)
    conn = get_conn()
    has_shots = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
    # 取消时分镜必然未走到尾钩（is_final 后状态已是 scripted、不在 scripting）。已生成的镜头是
    # 半截分镜，不能冒充"待确认"完成态——置为 script_failed 并写清原因，保留已生成镜头供查看/续作。
    if has_shots:
        conn.execute(
            "UPDATE episodes SET status='script_failed', script_error=? WHERE id=?",
            (f"分镜生成已手动取消：已保留 {has_shots} 个逐镜 checkpoint，恢复时将从下一镜继续。", episode_id))
        conn.commit()
        return {"status": "script_failed", "shots": has_shots}
    conn.execute("UPDATE episodes SET status='planned', script_error=NULL WHERE id=?", (episode_id,))
    conn.commit()
    return {"status": "planned"}


async def _plan_one_shot(shot_row) -> dict:
    """返回固定参考图模式计划；不再调用 LLM 做模式选择。"""
    from app import video_modes
    return video_modes.decision_to_dict(video_modes.default_reference_decision())


async def _ensure_shot_mode_plan(conn, shot_id: str, *, force: bool = False) -> None:
    """生成前确保该镜已有固定参考图模式计划。

    旧版可能残留首/尾关键帧模式计划；这类计划不能因为字段非空就被复用，
    必须原地升级为固定参考图模式，保证所有新视频只走同一条链路。
    """
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        return
    if not force and shot_row["mode_plan"]:
        try:
            existing = json.loads(shot_row["mode_plan"])
        except (TypeError, ValueError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and existing.get("mode") == "REFERENCE_IMAGE_MODE":
            return
    plan_dict = await _plan_one_shot(shot_row)
    conn.execute("UPDATE shots SET mode_plan=? WHERE id=?",
                 (json.dumps(plan_dict, ensure_ascii=False), shot_id))
    conn.commit()


_MAX_PUBLIC_IMAGE_INPUT_CHARS = 1_000_000


def _public_shot_versions(conn, shot_id: str, *, include_inputs: bool) -> list[dict]:
    if include_inputs:
        rows = conn.execute(
            """SELECT id, shot_id, version_no, prompt_text, status, error,
                      video_path, qa_json, cost_cny, latency_s, artifact_id,
                      adoption_reason, technical_validation_json, created_at,
                      provider_task_id,
                      CASE WHEN length(image_inputs) <= ? THEN image_inputs END AS image_inputs,
                      CASE WHEN length(image_inputs) > ? THEN 1 ELSE 0 END AS image_inputs_omitted
               FROM shot_versions WHERE shot_id=? ORDER BY version_no DESC""",
            (_MAX_PUBLIC_IMAGE_INPUT_CHARS, _MAX_PUBLIC_IMAGE_INPUT_CHARS, shot_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, shot_id, version_no, '' AS prompt_text, status, error,
                      video_path, qa_json, cost_cny, latency_s, artifact_id,
                      adoption_reason, technical_validation_json, created_at,
                      provider_task_id, NULL AS image_inputs
               FROM shot_versions WHERE shot_id=? ORDER BY version_no DESC""",
            (shot_id,),
        ).fetchall()
    versions = rows_to_dicts(rows)
    from app.config import PROJECTS_DIR
    for version in versions:
        version["qa"] = json.loads(version["qa_json"]) if version["qa_json"] else None
        version.pop("qa_json", None)
        meta = json.loads(version.get("image_inputs") or "{}") if include_inputs else {}
        inputs_omitted = bool(version.pop("image_inputs_omitted", 0))
        refs = [
            _public_reference_image(ref)
            for ref in (meta.get("reference_images") or [])
            if isinstance(ref, dict)
        ]
        version["image_inputs"] = {
            "first_frame_used": bool(meta.get("first_frame_used")),
            "first_frame_src": meta.get("first_frame_src"),
            "first_frame_scene_id": meta.get("first_frame_scene_id"),
            "first_frame_image_url": _media_url(meta.get("first_frame_path")),
            "last_frame_used": bool(meta.get("last_frame_used")),
            "last_frame_src": meta.get("last_frame_src"),
            "last_frame_scene_id": meta.get("last_frame_scene_id"),
            "last_frame_image_url": _media_url(meta.get("last_frame_path")),
            "mode": meta.get("mode"),
            "mode_decision": meta.get("mode_decision"),
            "reference_image_used": bool(meta.get("reference_image_used")),
            "reference_images": refs,
            "reference_failure_logs": [
                _public_failure_log(item)
                for item in (meta.get("reference_failure_logs") or [])
                if isinstance(item, dict)
            ],
            "fallback_reason": meta.get("fallback_reason"),
            "retry_reason": meta.get("retry_reason"),
            "omitted_for_size": inputs_omitted,
        }
        if version.get("video_path"):
            try:
                rel_path = Path(version["video_path"]).relative_to(PROJECTS_DIR).as_posix()
                version["video_url"] = f"/media/{rel_path}"
            except ValueError:
                version["video_url"] = None
    return versions


@router.get("/episodes/{episode_id}")
def episode_detail(episode_id: str, view: str | None = None):
    """Return episode data shaped for the requesting workspace.

    The legacy/default response remains complete for MCP and API consumers.
    UI workspaces opt into a narrow view so screenplay, storyboard, and cinema
    pages never touch historical media JSON.
    """
    if view not in (None, "script", "board", "wall", "cinema"):
        raise HTTPException(400, f"未知分集视图：{view}")
    full = view is None
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
    script = _load_screenplay(ep) if full or view in ("script", "board") else None
    ep["screenplay"] = script.model_dump() if script and (full or view == "script") else None
    ep["screenplay_mode"] = _screenplay_mode(script)
    artifact_id = ep.get("screenplay_artifact_id")
    artifact = (
        evidence_repository.get_artifact(artifact_id)
        if artifact_id and (full or view == "script") else None
    )
    if artifact:
        artifact.pop("content_json", None)
        artifact.pop("content", None)
        artifact["evaluations"] = evidence_repository.get_evaluations(artifact_id)
    ep["screenplay_evidence"] = artifact
    storyboard_artifact_id = ep.get("storyboard_artifact_id")
    storyboard_artifact = (
        evidence_repository.get_artifact(storyboard_artifact_id)
        if storyboard_artifact_id and (full or view == "board") else None
    )
    if storyboard_artifact:
        storyboard_artifact.pop("content_json", None)
        storyboard_artifact.pop("content", None)
        storyboard_artifact["evaluations"] = evidence_repository.get_evaluations(
            storyboard_artifact_id
        )
    ep["storyboard_evidence"] = storyboard_artifact
    ep.pop("screenplay_json", None)
    # 分镜大纲（先规划后逐镜填充）：透出给前端做 已通过 k / 计划 N 镜 的进度展示
    outline = None
    if full or view == "board":
        try:
            outline = json.loads(ep.get("storyboard_outline_json") or "null")
        except (TypeError, ValueError):
            outline = None
    ep.pop("storyboard_outline_json", None)
    ep["storyboard_outline"] = outline
    ep["storyboard_planned_shots"] = len(outline["shots"]) if outline and outline.get("shots") else None
    # Supervisor 运行面板数据（PRD §14.2）
    if full or view == "board":
        from app.storyboard_supervisor import load_latest_checkpoint
        from app.storyboard_control import control_snapshot

        cp = load_latest_checkpoint(episode_id)
        ep["supervisor"] = None
        if cp is not None:
            repair = cp.last_repair or {}
            ep["supervisor"] = {
                "phase": cp.phase,
                "goal": cp.goal,
                "completion_mode": cp.completion_mode,
                "repair_epoch": cp.repair_epoch,
                "validated_prefix_end": cp.validated_prefix_end,
                "next_shot_no": cp.next_shot_no,
                "expected_total": cp.expected_total or ep["storyboard_planned_shots"] or 0,
                "outcome": cp.outcome,
                "last_repair": repair,
                "strategy": repair.get("strategy"),
                "frontier": repair.get("invalidation_frontier"),
                "issue_codes": repair.get("issue_codes") or [],
                "completion_grant_id": cp.completion_grant_id,
                "pending_control": control_snapshot(episode_id),
            }
        try:
            ep["active_storyboard_run_id"] = ep.get("active_storyboard_run_id")
            ep["storyboard_completion_mode"] = ep.get("storyboard_completion_mode") or "ready_for_manual_confirm"
        except Exception:  # noqa: BLE001
            ep["active_storyboard_run_id"] = None
            ep["storyboard_completion_mode"] = "ready_for_manual_confirm"
    shot_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"])
    ep["shot_count"] = shot_count
    if view in ("script", "cinema"):
        ep["shots"] = []
        ep["pipeline_summary"] = None
        return ep

    shot_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    # 预估只按模型选择的实际分镜时长累计；单集不设总时长产品上限。
    ep["cost_cny"] = worker.episode_cost(episode_id)
    ep["cost_limit_cny"] = float(get_setting("episode_cost_limit_cny") or 100)
    shots = rows_to_dicts(shot_rows)
    version_counts = {}
    if view == "board" and shots:
        count_rows = conn.execute(
            """SELECT v.shot_id, COUNT(*) AS version_count
               FROM shot_versions v
               JOIN shots s ON s.id=v.shot_id
               WHERE s.episode_id=? GROUP BY v.shot_id""",
            (episode_id,),
        ).fetchall()
        version_counts = {row["shot_id"]: int(row["version_count"]) for row in count_rows}
    pipeline_statuses = {}
    pipeline_summary = None
    if full or view == "wall":
        try:
            from app.media_pipeline.status import episode_pipeline_statuses
            pipeline_statuses, pipeline_summary = episode_pipeline_statuses(episode_id, conn=conn)
        except Exception:  # noqa: BLE001
            pipeline_statuses, pipeline_summary = {}, None
    for s in shots:
        s["characters"] = json.loads(s["characters"] or "[]")
        s["dialogues"] = json.loads(s["dialogues"] or "[]")
        _apply_contract_to_public_shot(s)
        from app.continuity import information_items_for_shot
        s["new_information_items"] = information_items_for_shot(s, script)
        s["est_cost_cny"] = shot_cost_cny(s["duration_s"])
        if s.get("storyboard_artifact_id") and (full or view == "board"):
            shot_artifact = evidence_repository.get_artifact(s["storyboard_artifact_id"])
            if shot_artifact:
                shot_artifact.pop("content_json", None)
                shot_artifact.pop("content", None)
                shot_artifact["evaluations"] = evidence_repository.get_evaluations(
                    s["storyboard_artifact_id"]
                )
            s["storyboard_evidence"] = shot_artifact
        else:
            s["storyboard_evidence"] = None
        # mode_plan 存的是 JSON 文本，解析成对象供前端只读展示模型决策
        try:
            s["mode_plan"] = json.loads(s["mode_plan"]) if s.get("mode_plan") else None
        except (TypeError, ValueError):
            s["mode_plan"] = None
        # 新链路只使用参考图；旧关键帧字段仅保留在数据库中做历史兼容，不再对外暴露或参与状态判断。
        for legacy_key in (
            "approved_scene_id", "approved_head_scene_id", "approved_tail_scene_id", "scene_status",
        ):
            s.pop(legacy_key, None)
        s["video_stale"] = False
        if view == "board":
            s["version_count"] = version_counts.get(s["id"], 0)
            s["versions"] = []
            s["pipeline"] = None
            continue

        s["versions"] = _public_shot_versions(conn, s["id"], include_inputs=full)
        s["pipeline"] = pipeline_statuses.get(s["id"])
    ep["shots"] = shots
    ep["pipeline_summary"] = pipeline_summary
    return ep


@router.get("/shots/{shot_id}/review")
def shot_review_detail(shot_id: str):
    """Load the expensive review gallery for one selected shot only."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "镜头不存在")
    shot = dict(row)
    shot["characters"] = json.loads(shot["characters"] or "[]")
    shot["dialogues"] = json.loads(shot["dialogues"] or "[]")
    _apply_contract_to_public_shot(shot)
    from app.continuity import information_items_for_shot
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (shot["episode_id"],)
    ).fetchone()
    screenplay = _load_screenplay(dict(episode_row)) if episode_row else None
    shot["new_information_items"] = information_items_for_shot(shot, screenplay)
    shot["est_cost_cny"] = shot_cost_cny(shot["duration_s"])
    shot["video_stale"] = False
    for legacy_key in (
        "approved_scene_id", "approved_head_scene_id", "approved_tail_scene_id", "scene_status",
    ):
        shot.pop(legacy_key, None)
    try:
        shot["mode_plan"] = json.loads(shot["mode_plan"]) if shot.get("mode_plan") else None
    except (TypeError, ValueError):
        shot["mode_plan"] = None
    shot["storyboard_evidence"] = None
    shot["versions"] = _public_shot_versions(conn, shot_id, include_inputs=True)
    try:
        from app.media_pipeline.status import shot_pipeline_status
        shot["pipeline"] = shot_pipeline_status(shot_id, conn=conn)
    except Exception:  # noqa: BLE001
        shot["pipeline"] = None
    return shot


@router.put("/shots/{shot_id}")
async def edit_shot(shot_id: str, body: dict):
    from app.capabilities.dispatch import ui_route
    expected_version = body.get("expected_version")
    patch = {k: v for k, v in body.items() if k != "expected_version"}
    routed = await ui_route(
        "shot.update",
        {"shot_id": shot_id, "patch": patch, "expected_version": expected_version},
    )
    if routed is not None:
        return routed
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    current_version = shot["storyboard_artifact_id"] or ""
    if expected_version is not None and str(expected_version) != str(current_version):
        raise HTTPException(
            409,
            f"镜头版本冲突：当前版本 {current_version or '空'}，请求基于 {expected_version}，请刷新后重试",
        )
    merged = dict(shot)
    merged["characters"] = json.loads(merged["characters"] or "[]")
    merged["dialogues"] = json.loads(merged["dialogues"] or "[]")
    merged["continuity_from_prev"] = bool(merged["continuity_from_prev"])
    _apply_contract_to_public_shot(merged)
    editable_keys = (
        "duration_s", "shot_size", "camera_move", "scene_setting", "characters",
        "action_desc", "first_frame_desc", "last_frame_desc", "source_excerpt", "narration",
        "dialogues", "transition", "continuity_from_prev",
        "story_event_id", "purpose", "spine_beat_ids", "key_line_ids", "information_ids",
        "new_information_ids", "reinforcement_info_ids", "spoken_contract_status",
        "state_in", "primary_action", "emotion_beat", "state_out", "observed_state_out",
        "continuity_mode", "characters_visible", "audio_cast", "audio_timeline",
        "required_text", "reference_roles", "do_not_repeat", "risk_tags",
        "prompt_contract_version", "legacy_unvalidated", "camera_angle",
        "spatial_anchor", "is_final",
    )
    for key in editable_keys:
        if key in body:
            merged[key] = body[key]
    # 时长 clamp 到产品侧合法区间；缺省/非法时回退默认时长。
    merged["duration_s"] = clip_duration_value(merged.get("duration_s"))
    instance, errors = schema_errors(Shot, {k: merged[k] for k in (
        "shot_no", *editable_keys,
    )})
    if errors:
        raise HTTPException(422, "；".join(errors))
    instance.action_desc = normalize_action_desc(instance.action_desc)
    # 产品禁止旁白：保存时强制清空 narration，并从 timeline 剥离 narration 轨。
    instance.narration = ""
    if instance.audio_timeline:
        instance.audio_timeline = [item for item in instance.audio_timeline if item.type != "narration"]
    # VAL-422：人工编辑必须重新通过确定性业务校验；「人改过」≠ hard gate 通过。
    from app.continuity import (
        speech_capacity_errors, spoken_contract_coherence_errors, shot_id_space_errors,
        state_chain_errors,
    )
    from app.spoken_contract import (
        RULE_SPOKEN_CAPACITY,
        synchronize_spoken_contract,
        spoken_text_of,
    )
    from app.validators import (
        validate_storyboard_shot_covers_outline,
        validate_storyboard_preserves_key_content,
        key_line_delivery_errors,
    )
    episode_id = shot["episode_id"]
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    screenplay = _load_screenplay(ep) if ep is not None else None
    sync = synchronize_spoken_contract(
        instance,
        changed_fields={k for k in ("dialogues", "audio_timeline") if k in body},
    )
    # 容量只走 speech_capacity_errors，避免与 sync 内 capacity_issue 重复报告。
    business_errors: list[str] = [
        issue.message for issue in sync.issues
        if issue.severity == "blocker" and issue.rule_id != RULE_SPOKEN_CAPACITY
    ]
    business_errors.extend(speech_capacity_errors(instance))
    business_errors.extend(spoken_contract_coherence_errors(instance))
    business_errors.extend(shot_id_space_errors(instance))
    business_errors.extend(key_line_delivery_errors(instance, screenplay))

    outline = None
    if ep is not None and ep["storyboard_outline_json"]:
        try:
            outline = StoryboardOutline.model_validate_json(ep["storyboard_outline_json"])
        except Exception:  # noqa: BLE001
            outline = None
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    board = _board_from_shot_rows(rows, ep["episode_no"] if ep else 1)
    # 用编辑后的镜头替换同号位，再跑相邻状态链 / 大纲 covers / 收束整集校验。
    replaced = False
    for idx, existing in enumerate(board.shots):
        if existing.shot_no == instance.shot_no:
            board.shots[idx] = instance
            replaced = True
            break
    if not replaced:
        board.shots.append(instance)
        board.shots.sort(key=lambda s: s.shot_no)

    if outline and outline.shots:
        brief = next((s for s in outline.shots if s.shot_no == instance.shot_no), None)
        if brief is not None and (brief.covers or "").strip():
            prior_text = "".join(
                (s.action_desc or "") + spoken_text_of(s)
                for s in board.shots if s.shot_no < instance.shot_no
            )
            later = "；".join(
                (s.covers or "") for s in outline.shots if s.shot_no > instance.shot_no
            )
            business_errors.extend(validate_storyboard_shot_covers_outline(
                instance, brief.covers, instance.shot_no,
                prior_text=prior_text, later_planned_covers=later,
            ))

    # 相邻窗口状态链：只保留「本镜」相关诊断，避免旧邻镜缺字段误伤本次保存。
    neighbor_nos = {instance.shot_no - 1, instance.shot_no, instance.shot_no + 1}
    neighbor_board = Storyboard(
        episode_no=board.episode_no,
        shots=[s for s in board.shots if s.shot_no in neighbor_nos],
    )
    if neighbor_board.shots and (
        (instance.state_in or "").strip() or (instance.state_out or "").strip()
    ):
        tag = f"shot_no={instance.shot_no}"
        business_errors.extend(
            err for err in state_chain_errors(neighbor_board) if tag in err
        )

    is_final_edit = bool(instance.is_final) or (
        outline is not None and outline.shots
        and instance.shot_no >= len(outline.shots)
    )
    if is_final_edit and screenplay is not None:
        business_errors.extend(validate_storyboard_preserves_key_content(board, screenplay))

    # 去重：同一文案只报一次
    deduped: list[str] = []
    seen_err: set[str] = set()
    for msg in business_errors:
        if msg in seen_err:
            continue
        seen_err.add(msg)
        deduped.append(msg)
    if deduped:
        # 失败时保留草稿 Artifact，不覆盖已通过 checkpoint。
        draft_artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_shot",
            scope_type="storyboard_checkpoint",
            scope_id=f"{episode_id}:{shot['shot_no']}",
            status="needs_revision",
            trust_level="T1",
            content=instance.model_dump(mode="json"),
            parent_artifact_ids=[shot["storyboard_artifact_id"]] if shot["storyboard_artifact_id"] else [],
            contract_version=get_contract("storyboard").version,
        ))
        evidence_repository.create_evaluation(
            draft_artifact["id"],
            Evaluation(
                evaluator_type="human",
                evaluator_name="storyboard_editor",
                evaluator_version="1.0.0",
                status="failed",
                hard_gate_passed=False,
                score=0,
                evidence={
                    "decision": "authored_or_reviewed",
                    "shot_id": shot_id,
                    "issues": deduped[:12],
                },
            ),
        )
        raise HTTPException(422, {
            "code": "SHOT_EDIT_VALIDATION_FAILED",
            "category": "validation",
            "message": "；".join(deduped),
            "issues": deduped,
            "draft_artifact_id": draft_artifact["id"],
            "checkpoint_preserved": True,
        })

    # 业务硬校验已通过：人工证据只记录 authored；hard gate 由确定性 Evaluation 单独证明。
    conn.execute(
        "UPDATE shots SET duration_s=?, shot_size=?, camera_move=?, scene_setting=?, characters=?, action_desc=?, first_frame_desc=?, last_frame_desc=?, source_excerpt=?, narration=?, dialogues=?, transition=?, continuity_from_prev=?, shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE id=?",
        (instance.duration_s, instance.shot_size, instance.camera_move, instance.scene_setting,
         json.dumps(instance.characters, ensure_ascii=False), instance.action_desc, instance.first_frame_desc, instance.last_frame_desc,
         instance.source_excerpt, instance.narration,
         json.dumps([d.model_dump() for d in instance.dialogues], ensure_ascii=False),
         instance.transition, int(instance.continuity_from_prev), _shot_contract_json(instance),
         instance.continuity_mode, instance.observed_state_out, shot_id))
    conn.commit()
    previous_artifact_id = shot["storyboard_artifact_id"]
    manual_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="storyboard_shot",
        scope_type="storyboard_checkpoint",
        scope_id=f"{episode_id}:{shot['shot_no']}",
        status="validated",
        trust_level="T2",
        content=instance.model_dump(mode="json"),
        parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
        contract_version=get_contract("storyboard").version,
    ))
    contract_version = get_contract("storyboard").version
    manual_artifact = evidence_repository.commit_artifact(
        None,
        manual_artifact["id"],
        [
            Evaluation(
                evaluator_type="human",
                evaluator_name="storyboard_editor",
                evaluator_version="1.0.0",
                status="passed",
                hard_gate_passed=False,
                score=100,
                evidence={"decision": "authored_or_reviewed", "shot_id": shot_id},
            ),
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="storyboard_shot_business_gate",
                evaluator_version=contract_version,
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={"shot_id": shot_id, "spoken_contract_status": instance.spoken_contract_status},
            ),
        ],
    )
    conn.execute(
        "UPDATE shots SET storyboard_artifact_id=? WHERE id=?",
        (manual_artifact["id"], shot_id),
    )
    conn.commit()
    # 任一分镜字段都参与参考图或视频 prompt。保存后统一清理全部旧衍生产物，
    # 避免 done/generating 状态继续展示旧成片；剧集必须重新确认后才能花钱生成。
    invalidated = worker.clear_shot_artifacts(shot_id)
    conn.execute("UPDATE episodes SET status='scripted' WHERE id=?", (episode_id,))
    conn.commit()
    impact = evidence_repository.get_lineage(previous_artifact_id or manual_artifact["id"])
    return {
        "ok": True,
        "invalidated": invalidated,
        "artifact_id": manual_artifact["id"],
        "impact": {
            "stale_descendant_ids": [
                item["id"] for item in impact["descendants"] if item["status"] == "stale"
            ],
            "requires_reconfirm": True,
            "paid_media_invalidated": bool(invalidated),
        },
    }


@router.get("/episodes/{episode_id}/spoken-contract/audit")
def audit_episode_spoken_contract(episode_id: str):
    """只读审计本集口播合同（PRD §6.1）：不写库。"""
    _episode_or_404(episode_id)
    from app.spoken_contract import audit_legacy_spoken_contract, validate_spoken_contract
    from app.observability.metrics import inc

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    board = _board_from_shot_rows(rows, 1)
    results = []
    conflict_count = 0
    for shot in board.shots:
        status = audit_legacy_spoken_contract(shot)
        issues = [i.model_dump(mode="json") for i in validate_spoken_contract(shot)]
        if status == "conflict" or any(i["code"] == "SPOKEN_CONTRACT_CONFLICT" for i in issues):
            conflict_count += 1
            inc("spoken_contract_conflict_total", episode_id=episode_id, shot_no=shot.shot_no, source="audit")
        results.append({
            "shot_no": shot.shot_no,
            "spoken_contract_status": status,
            "legacy_unvalidated": bool(shot.legacy_unvalidated),
            "issues": issues,
            "repair_options": [
                "rebuild_timeline_from_dialogues",
                "rebuild_dialogues_from_timeline",
            ] if status == "conflict" else [],
        })
    return {
        "episode_id": episode_id,
        "conflict_count": conflict_count,
        "shots": results,
    }


@router.post("/episodes/{episode_id}/migrate-shot-ids")
def migrate_episode_shot_ids(episode_id: str, body: dict | None = Body(None)):
    """把误写入 story_event_id 的 S* 迁移到 spine_beat_ids（PRD §6.2）。"""
    _episode_or_404(episode_id)
    dry_run = bool((body or {}).get("dry_run", False))
    from app.continuity import migrate_shot_id_spaces, shot_contract_dict

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall()
    board = _board_from_shot_rows(rows, 1)
    by_no = {int(r["shot_no"]): r for r in rows}
    changed = []
    for shot in board.shots:
        actions = migrate_shot_id_spaces(shot)
        if not actions:
            continue
        changed.append({"shot_no": shot.shot_no, "actions": actions})
        if dry_run:
            continue
        row = by_no.get(shot.shot_no)
        if row is None:
            continue
        conn.execute(
            "UPDATE shots SET shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE id=?",
            (
                json.dumps(shot_contract_dict(shot), ensure_ascii=False),
                shot.continuity_mode,
                shot.observed_state_out,
                row["id"],
            ),
        )
    if not dry_run and changed:
        conn.commit()
    return {"episode_id": episode_id, "dry_run": dry_run, "changed": changed}


@router.post("/shots/{shot_id}/resolve-spoken-conflict")
async def resolve_spoken_conflict(shot_id: str, body: dict):
    """人工选择口播基准并同步（PRD §6.1 / §7.2）。

    choice:
      - rebuild_timeline_from_dialogues
      - rebuild_dialogues_from_timeline
    若镜头已有付费视频，必须 set invalidate_media=true，否则 409。
    """
    choice = (body or {}).get("choice") or ""
    invalidate_media = bool((body or {}).get("invalidate_media", False))
    if choice not in {"rebuild_timeline_from_dialogues", "rebuild_dialogues_from_timeline"}:
        raise HTTPException(422, "choice 必须是 rebuild_timeline_from_dialogues 或 rebuild_dialogues_from_timeline")
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    has_paid = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM shot_versions WHERE shot_id=? AND status IN ('done','generating','pending')) AS present",
        (shot_id,),
    ).fetchone()["present"]
    if has_paid and not invalidate_media:
        raise HTTPException(
            409,
            "本镜已有付费视频产物；请确认 invalidate_media=true 使旧视频失效后再改口播基准",
        )
    # 复用 edit_shot：只改一侧字段，触发 synchronize_spoken_contract 定向重建。
    patch: dict = {"expected_version": shot["storyboard_artifact_id"]}
    if choice == "rebuild_timeline_from_dialogues":
        patch["dialogues"] = json.loads(shot["dialogues"] or "[]")
    else:
        from app.continuity import apply_shot_contract
        temp = Shot(
            shot_no=shot["shot_no"], duration_s=shot["duration_s"], shot_size=shot["shot_size"],
            camera_move=shot["camera_move"], scene_setting=shot["scene_setting"],
            characters=json.loads(shot["characters"] or "[]"),
            action_desc=shot["action_desc"], first_frame_desc=shot["first_frame_desc"] or "",
            last_frame_desc=shot["last_frame_desc"] or "", source_excerpt=shot["source_excerpt"] or "",
            narration=shot["narration"], dialogues=json.loads(shot["dialogues"] or "[]"),
            transition=shot["transition"] or "硬切",
            continuity_from_prev=bool(shot["continuity_from_prev"]),
        )
        apply_shot_contract(temp, shot["shot_contract_json"] if "shot_contract_json" in shot.keys() else None)
        patch["audio_timeline"] = [item.model_dump(mode="json") for item in (temp.audio_timeline or [])]
    result = await edit_shot(shot_id, patch)
    from app.observability.metrics import inc
    inc(
        "spoken_contract_conflict_total",
        episode_id=shot["episode_id"],
        shot_no=shot["shot_no"],
        source="resolved",
        choice=choice,
    )
    return {"ok": True, "choice": choice, **(result if isinstance(result, dict) else {})}


def _storyboard_residual_hint(residual: list[str]) -> str:
    """Return an actionable repair hint for the current validation failures."""
    text = "；".join(residual)
    hints: list[str] = []
    if "口播上限" in text or "念不完" in text:
        hints.append("请拆成相邻镜头分担台词，或精简非关键口水话（口播只计台词纯文字、不计标点）")
    if "角色圣经中不存在" in text or "既不在角色圣经" in text or "圣经角色为" in text:
        hints.append("请在监制房把该角色补入角色圣经，或改由圣经角色完成该动作")
    if "未落实本镜大纲 covers" in text or "只停留在大纲" in text:
        hints.append("请在 action_desc/narration/dialogues 写出该事实，同义改写即可（如\"成绩\"可写成\"测出七段\"、\"追捧\"可写成\"赞叹欢呼\"）")
    if not hints:
        hints.append("请修改该镜后从下一镜继续，或点击「重新生成整版」")
    return "；".join(hints)


def _storyboard_loop_exit_text(exit_reason: str) -> str:
    """Translate the actual AgentLoop exit reason without misreporting exhaustion."""
    return {
        "max_iterations": "已达到重试上限",
        "no_quality_gain": "连续修复无质量提升，修复循环已停止",
        "stalled": "连续输出相同问题，修复循环已停止",
    }.get(exit_reason, "修复循环未通过")


def _board_from_shot_rows(rows, episode_no: int) -> Storyboard:
    """Restore a Storyboard from persisted shot rows for confirmation and validation."""
    from app.continuity import apply_shot_contract
    shots = []
    for r in rows:
        shot = Shot(
            shot_no=r["shot_no"], duration_s=r["duration_s"], shot_size=r["shot_size"], camera_move=r["camera_move"],
            scene_setting=r["scene_setting"], scene_name=(r["scene_name"] if "scene_name" in r.keys() else "") or "",
            characters=json.loads(r["characters"] or "[]"),
            action_desc=r["action_desc"], first_frame_desc=r["first_frame_desc"] or "", last_frame_desc=r["last_frame_desc"] or "",
            source_excerpt=r["source_excerpt"] or "",
            narration=r["narration"], dialogues=json.loads(r["dialogues"] or "[]"),
            transition=r["transition"] or "硬切", continuity_from_prev=bool(r["continuity_from_prev"]),
            continuity_mode=(r["continuity_mode"] if "continuity_mode" in r.keys() else "") or "",
            observed_state_out=(r["observed_state_out"] if "observed_state_out" in r.keys() else "") or "",
        )
        if "shot_contract_json" in r.keys() and r["shot_contract_json"]:
            apply_shot_contract(shot, r["shot_contract_json"])
        shots.append(shot)
    return Storyboard(episode_no=episode_no, shots=shots)

__all__ = [name for name in globals() if not name.startswith("__")]
