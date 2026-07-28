from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

def _shot_contract_json(shot: Shot) -> str:
    from app.continuity import shot_contract_dict
    return json.dumps(shot_contract_dict(shot), ensure_ascii=False)


def _apply_contract_to_public_shot(target: dict) -> None:
    from app.continuity import apply_shot_contract, spoken_chars_from_shot
    from app.spoken_contract import max_speech_chars
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


def _assert_storyboard_write_authorized(
    conn, episode_id: str, expected_screenplay_artifact_id: str | None
) -> None:
    row = conn.execute(
        "SELECT screenplay_publish_fence, screenplay_artifact_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("分镜写入被拒绝：剧集已不存在")
    current = row["screenplay_artifact_id"] or ""
    expected = expected_screenplay_artifact_id or ""
    if row["screenplay_publish_fence"] or (expected and expected != current):
        errors.log_error(
            None,
            action="storyboard_stale_run_write_rejected",
            context={
                "episode_id": episode_id,
                "expected_screenplay_artifact_id": expected,
                "current_screenplay_artifact_id": current,
                "publish_fence": bool(row["screenplay_publish_fence"]),
            },
            message="被替代的分镜运行尝试写入，已拒绝",
        )
        raise RuntimeError("分镜写入被拒绝：上游剧本版本或发布栅栏已变化")


def _insert_storyboard_shot(
    conn,
    episode_id: str,
    screenplay: EpisodeScreenplay,
    shot: Shot,
    expected_screenplay_artifact_id: str | None = None,
) -> str:
    _assert_storyboard_write_authorized(conn, episode_id, expected_screenplay_artifact_id)
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


def _sync_storyboard_shot_timing(
    conn,
    episode_id: str,
    board: Storyboard,
    expected_screenplay_artifact_id: str | None = None,
) -> None:
    _assert_storyboard_write_authorized(conn, episode_id, expected_screenplay_artifact_id)
    for shot in board.shots:
        conn.execute(
            "UPDATE shots SET duration_s=?, transition=?, continuity_from_prev=?, last_frame_desc=?, shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE episode_id=? AND shot_no=?",
            (shot.duration_s, shot.transition, int(shot.continuity_from_prev), shot.last_frame_desc,
             _shot_contract_json(shot), shot.continuity_mode, shot.observed_state_out,
             episode_id, shot.shot_no),
        )


def _storyboard_start_preflight_payload(episode_id: str, mode: str) -> dict:
    from app.storyboard_supervisor import load_latest_checkpoint
    from app.storyboard_workspace import episode_fingerprint

    ep = _episode_or_404(episode_id)
    if not _screenplay_ready(ep):
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    conn = get_conn()
    shots = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,),
    ).fetchone()["c"])
    cp = load_latest_checkpoint(episode_id)
    planned = int(cp.expected_total or 0) if cp else 0
    if not planned and ep["storyboard_outline_json"]:
        try:
            planned = len(json.loads(ep["storyboard_outline_json"] or "{}").get("shots") or [])
        except (TypeError, ValueError, json.JSONDecodeError):
            planned = 0
    resume_from = (int(cp.next_shot_no) if cp else shots + 1) if mode == "resume" else 1
    kept = min(shots, max(0, resume_from - 1)) if mode == "resume" else 0
    remaining = max(0, planned - kept) if planned else None
    return {
        "episode_id": episode_id,
        "action": "resume" if mode == "resume" else "create",
        "screenplay_artifact_id": ep["screenplay_artifact_id"],
        "storyboard_artifact_id": ep["storyboard_artifact_id"],
        "checkpoint": {
            "available": bool(cp),
            "phase": cp.phase if cp else None,
            "resume_from_shot": resume_from,
        },
        "kept_validated_shots": kept,
        "planned_shots": planned or None,
        "remaining_shots": remaining,
        "impact": "保留已通过镜头，从安全检查点继续" if mode == "resume" else "创建新的分镜工作修订，不覆盖当前发布版",
        "estimated_wait_minutes": [max(1, (remaining or planned or 1)), max(2, (remaining or planned or 1) * 3)],
        "estimated_cost_cny": None,
        "estimate_note": "文本生成费用按实际调用结算；不会自动提交付费视频生成",
        "baseline_fingerprint": episode_fingerprint(episode_id),
    }


@router.post("/episodes/{episode_id}/storyboard/preflight")
def storyboard_start_preflight(episode_id: str, body: dict | None = Body(None)):
    from app.storyboard_workspace import create_preview

    body = _as_body_dict(body)
    mode = body.get("mode") or "create"
    if mode not in {"create", "fresh", "resume"}:
        raise HTTPException(422, "未知的分镜启动方式")
    normalized = "resume" if mode == "resume" else "create"
    payload = _storyboard_start_preflight_payload(episode_id, normalized)
    return create_preview(f"start:{normalized}", episode_id, payload)


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

        # 恢复旧 checkpoint 时先把模型产生的引号漂移/拼接式证据收敛为授权原文中的
        # 连续片段。严格匹配不足的内容保持未解决，仍由确认门禁拦截。
        if resume:
            from app.storyboard_workspace import repair_generated_source_bindings

            evidence_repair = repair_generated_source_bindings(episode_id)
            if evidence_repair["bound"]:
                log_provider_call(
                    "storyboard_source_evidence_repair",
                    config.MODEL_TEXT,
                    "SOURCE_EVIDENCE_REALIGNED",
                    None,
                    0,
                    meta={"episode_id": episode_id, **evidence_repair},
                )

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
    """恢复被服务重启中断的分镜任务，不接管用户主动暂停的 Run。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM episodes "
        "WHERE status='scripting' AND screenplay_status='ready' AND screenplay_json IS NOT NULL"
    ).fetchall()
    resumed = 0
    for row in rows:
        episode_id = row["id"]
        if task_registry.active("storyboard", episode_id):
            continue
        latest = conn.execute(
            """SELECT id,status,failure_code FROM workflow_runs
               WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
               ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        if latest:
            if latest["status"] in {"CREATED", "RUNNING"}:
                # A durable run may belong to another live service instance.
                continue
            if latest["status"] != "PAUSED_EXTERNAL" or latest["failure_code"] != "SERVICE_RESTART":
                # PARTIAL / WAITING_HUMAN / user_pause are explicit manual resume points.
                continue
            parent = latest
        else:
            # Legacy databases may have only the projection state and no run ledger.
            parent = None
        recorder = None
        try:
            recorder = _new_storyboard_recorder(
                episode_id, requested_by="system", trigger_type="resume",
                parent_run_id=parent["id"] if parent else None,
            )
            installed = conn.execute(
                "UPDATE episodes SET active_storyboard_run_id=? "
                "WHERE id=? AND status='scripting' AND active_storyboard_run_id IS ?",
                (recorder.run_id, episode_id, row["active_storyboard_run_id"]),
            )
            if installed.rowcount != 1:
                conn.rollback()
                recorder.cancel("分镜恢复启动权已变化，当前运行未启动")
                continue
            conn.commit()
            task_registry.spawn(
                "storyboard", episode_id,
                _recorded_storyboard_task(episode_id, recorder, resume=True),
                project_id=row["project_id"],
            )
            resumed += 1
        except Exception as exc:  # noqa: BLE001 - one bad episode must not block startup
            public = errors.record_and_format(
                exc,
                action="storyboard_recovery_spawn",
                context={"episode_id": episode_id, "previous_run_id": row["active_storyboard_run_id"]},
            )
            from app.storyboard_supervisor import load_latest_checkpoint
            checkpoint = load_latest_checkpoint(episode_id)
            shot_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
            ).fetchone()["c"])
            recoverable = bool(shot_count or (checkpoint and checkpoint.validated_prefix_end > 0))
            conn.execute(
                "UPDATE episodes SET status=?, script_error=?, active_storyboard_run_id=NULL WHERE id=?",
                (
                    "script_failed" if recoverable else "planned",
                    (
                        f"服务重启后的分镜恢复未能启动；"
                        f"{'已通过镜头和恢复点均已保留，可点击继续分镜' if recoverable else '剧本已保留，可重新生成分镜'}。"
                        f"{public}"
                    ),
                    episode_id,
                ),
            )
            conn.commit()
            if recorder is not None:
                try:
                    recorder.cancel("分镜恢复任务未能启动，已回滚到可重试状态")
                except Exception:  # noqa: BLE001
                    pass
    return resumed


def _shot_video_is_stale(conn, shot_row, episode_storyboard_id: str | None) -> bool:
    """分镜 Artifact 不一致，或采用版冻结的人物/场景版本已落后于本集最新，均判 stale。"""
    try:
        adopted = shot_row["adopted_version_id"]
    except (KeyError, IndexError, TypeError):
        adopted = None
    if not adopted:
        return False
    try:
        shot_art = shot_row["storyboard_artifact_id"]
    except (KeyError, IndexError, TypeError):
        shot_art = None
    if episode_storyboard_id and shot_art and shot_art != episode_storyboard_id:
        episode_art = conn.execute(
            "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
            (episode_storyboard_id,),
        ).fetchone()
        try:
            episode_parents = json.loads(
                episode_art["parent_artifact_ids_json"] or "[]"
            ) if episode_art else []
        except (TypeError, ValueError):
            episode_parents = []
        if shot_art not in episode_parents:
            return True
    ver = conn.execute(
        "SELECT artifact_id, image_inputs FROM shot_versions WHERE id=?", (adopted,)
    ).fetchone()
    if not ver or not ver["artifact_id"]:
        # 无 artifact 时仍可检查资产版本 stale
        if ver and _shot_adopted_assets_stale(conn, shot_row, ver):
            return True
        return False
    art = conn.execute(
        "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
        (ver["artifact_id"],),
    ).fetchone()
    if art:
        try:
            parents = json.loads(art["parent_artifact_ids_json"] or "[]")
        except (TypeError, ValueError):
            parents = []
        if episode_storyboard_id and parents:
            valid_storyboard_parents = {episode_storyboard_id}
            if shot_art:
                valid_storyboard_parents.add(shot_art)
            if not any(parent in valid_storyboard_parents for parent in parents):
                return True
    return _shot_adopted_assets_stale(conn, shot_row, ver)


def _shot_adopted_assets_stale(conn, shot_row, version_row) -> bool:
    """采用版 reference_manifest 中的人物/场景 revision 是否仍是本集当前生效版本。"""
    try:
        from app.multiview import (
            character_multiview_enabled, scene_multiview_enabled,
            manifest_asset_revision_ids, manifest_asset_view_fingerprints,
            portrait_row_for_episode, scene_row_for_episode,
        )
    except Exception:  # noqa: BLE001
        return False
    if not character_multiview_enabled() and not scene_multiview_enabled():
        return False
    meta = {}
    try:
        meta = json.loads(version_row["image_inputs"] or "{}") if version_row["image_inputs"] else {}
    except (TypeError, ValueError, KeyError):
        meta = {}
    manifest = meta.get("reference_manifest") if isinstance(meta, dict) else None
    if not isinstance(manifest, dict):
        # 回退：从首张带 dependency_manifest 的参考图读取
        for ref in (meta.get("reference_images") or []) if isinstance(meta, dict) else []:
            if isinstance(ref, dict) and isinstance(ref.get("dependency_manifest"), dict):
                manifest = ref["dependency_manifest"]
                break
    if not isinstance(manifest, dict):
        return False
    frozen_ids = manifest_asset_revision_ids(manifest)
    if not frozen_ids:
        return False
    try:
        episode_id = shot_row["episode_id"]
    except (KeyError, IndexError, TypeError):
        return False
    ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return False
    project_id = ep["project_id"]
    episode_no = ep["episode_no"]
    for key, frozen_rev in frozen_ids.items():
        if key.startswith("character:"):
            name = key.split(":", 1)[1]
            row = portrait_row_for_episode(project_id, name, episode_no)
            current_id = row["id"] if row else None
            if current_id != frozen_rev:
                return True
        elif key.startswith("scene:"):
            name = key.split(":", 1)[1]
            row = scene_row_for_episode(project_id, name, episode_no)
            current_id = row["id"] if row else None
            if current_id != frozen_rev:
                return True
    frozen_views = manifest_asset_view_fingerprints(manifest)
    for (kind, name, role), frozen_fp in frozen_views.items():
        if kind == "character":
            parent = portrait_row_for_episode(project_id, name, episode_no)
            table = "character_portrait_views"
            parent_column = "portrait_id"
        else:
            parent = scene_row_for_episode(project_id, name, episode_no)
            table = "scene_reference_views"
            parent_column = "scene_reference_id"
        if not parent:
            return True
        current = conn.execute(
            f"SELECT input_fingerprint FROM {table} "
            f"WHERE {parent_column}=? AND view_role=? AND status='ready'",
            (parent["id"], role),
        ).fetchone()
        current_fp = current["input_fingerprint"] if current else None
        if current_fp != frozen_fp:
            return True
    return False


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
                run_id=getattr(recorder, "run_id", None),
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
            run = evidence_repository.get_run(recorder.run_id)
            if not run or run.get("status") != "PAUSED_EXTERNAL":
                recorder.partial(result["script_error"] or "Supervisor 暂停，待恢复")
        else:
            recorder.fail(RuntimeError(result["script_error"] if result else "分镜生成失败"))
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，分镜运行等待自动续做")
        else:
            recorder.cancel()
        raise
    except Exception as exc:
        recorder.fail(exc)
        raise
    finally:
        # The workflow run remains available for audit/resume lineage, but it must
        # stop acting as a write lock once this coroutine has ended. The guarded
        # comparison avoids clearing a newer run that may have started meanwhile.
        try:
            cleanup_conn = get_conn()
            cleanup_conn.execute(
                "UPDATE episodes SET active_storyboard_run_id=NULL "
                "WHERE id=? AND active_storyboard_run_id=?",
                (episode_id, recorder.run_id),
            )
            cleanup_conn.commit()
        except Exception:  # noqa: BLE001
            pass


def _storyboard_generation_is_live(ep: dict) -> bool:
    """判断活动指针是否真的对应本进程/存储中的活跃分镜任务。

    ``episodes.status='scripting'`` 是 UI 投影，不是可靠的任务存活证明。旧 Run 已经
    PARTIAL/CANCELLED/FAILED 时若仍按该字段去重，继续按钮只会返回旧 run_id，页面进入
    “正在生成”但后台没有任务。进程内注册表优先；跨重启仅 CREATED/RUNNING Run 算活跃。
    """
    if task_registry.active("storyboard", ep["id"]):
        return True
    try:
        run_id = ep["active_storyboard_run_id"]
    except (KeyError, IndexError, TypeError):
        run_id = None
    if not run_id:
        return False
    from app.evidence import repository
    run = repository.get_run(run_id)
    return bool(run and run.get("status") in {"CREATED", "RUNNING"})


@router.post("/episodes/{episode_id}/storyboard")
async def start_storyboard(episode_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route
    body_was_explicit = isinstance(body, dict)
    body = _as_body_dict(body)
    payload = {"episode_id": episode_id, "mode": "fresh", **body}
    routed = await ui_route("storyboard.generate", payload)
    if routed is not None:
        return routed
    if body_was_explicit:
        from app.storyboard_workspace import require_preview
        require_preview(body.get("preflight_token"), "start:create", episode_id, consume=True)
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["screenplay_publish_fence"]:
        raise HTTPException(409, "剧本正在安全发布，暂不能启动新分镜任务")
    if _storyboard_generation_is_live(ep):
        return {
            "status": "scripting",
            "run_id": ep["active_storyboard_run_id"],
            "deduplicated": True,
        }
    if not _screenplay_ready(ep):
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    completion_mode = body.get("completion_mode") or "ready_for_manual_confirm"
    if completion_mode not in {"ready_for_manual_confirm", "auto_confirm"}:
        raise HTTPException(422, "completion_mode 只能是 ready_for_manual_confirm 或 auto_confirm")
    completion_grant_id = body.get("completion_grant_id")
    issued_grant_id = None
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
        issued_grant_id = grant.grant_id
    previous = {
        "status": ep["status"],
        "script_error": ep["script_error"],
        "active_storyboard_run_id": ep["active_storyboard_run_id"],
        "storyboard_completion_mode": ep["storyboard_completion_mode"],
    }
    cursor = conn.execute(
        "UPDATE episodes SET status='scripting', script_error=NULL, "
        "storyboard_completion_mode=?, active_storyboard_run_id=NULL "
        "WHERE id=? AND screenplay_publish_fence=0",
        (completion_mode, episode_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        if issued_grant_id:
            from app.completion_grant import revoke_grant
            revoke_grant(issued_grant_id)
        raise HTTPException(409, "剧本发布栅栏已生效，未启动分镜")
    conn.commit()
    recorder = None
    coro = None
    try:
        recorder = _new_storyboard_recorder(episode_id, completion_mode=completion_mode)
        conn.execute(
            "UPDATE episodes SET active_storyboard_run_id=? WHERE id=?",
            (recorder.run_id, episode_id),
        )
        conn.commit()
        coro = _recorded_storyboard_task(
            episode_id, recorder, resume=False,
            completion_mode=completion_mode,
            completion_grant_id=completion_grant_id,
        )
        task_registry.spawn(
            "storyboard", episode_id, coro, project_id=ep["project_id"],
        )
    except Exception as exc:
        if coro is not None:
            coro.close()
        conn.execute(
            "UPDATE episodes SET status=?, script_error=?, active_storyboard_run_id=?, "
            "storyboard_completion_mode=? WHERE id=?",
            (
                previous["status"],
                previous["script_error"],
                previous["active_storyboard_run_id"],
                previous["storyboard_completion_mode"],
                episode_id,
            ),
        )
        conn.commit()
        if issued_grant_id:
            from app.completion_grant import revoke_grant
            revoke_grant(issued_grant_id)
        if recorder is not None:
            try:
                recorder.cancel("分镜任务未能启动，剧集状态已回滚")
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "STORYBOARD_START_SPAWN_FAILED",
            "message": "分镜任务未能启动，剧本和原状态已保留，请重试",
            "recovery_action": "重新点击生成分镜；尚未开始逐镜生成",
            "episode_id": episode_id,
            "rolled_back": True,
            "recoverable": True,
        }) from exc
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
    body_was_explicit = isinstance(body, dict)
    body = _as_body_dict(body)
    payload = {"episode_id": episode_id, "mode": "resume", **body}
    routed = await ui_route("storyboard.generate", payload)
    if routed is not None:
        return routed
    if body_was_explicit:
        from app.storyboard_workspace import require_preview
        require_preview(body.get("preflight_token"), "start:resume", episode_id, consume=True)
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["screenplay_publish_fence"]:
        raise HTTPException(409, "剧本正在安全发布，暂不能继续分镜")
    if _storyboard_generation_is_live(ep):
        return {
            "status": "scripting",
            "run_id": ep["active_storyboard_run_id"],
            "deduplicated": True,
        }
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
        elif bool(last_contract.get("is_final")) and not body.get("force"):
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
    requested_mode = body.get("completion_mode")
    try:
        persisted_mode = ep["storyboard_completion_mode"] or "ready_for_manual_confirm"
    except (KeyError, IndexError, TypeError):
        persisted_mode = "ready_for_manual_confirm"
    completion_mode = requested_mode or persisted_mode
    if completion_mode not in {"ready_for_manual_confirm", "auto_confirm"}:
        raise HTTPException(422, "completion_mode 只能是 ready_for_manual_confirm 或 auto_confirm")
    grant_id = body.get("completion_grant_id") or (cp.completion_grant_id if cp else None)
    # 恢复预检中的自动确认勾选是一次新的明确授权。取消会撤销旧 grant，且旧 grant
    # 也可能已过期；若仍沿用空/旧凭据，任务会生成到底后才停在 WAITING_AUTHORIZATION。
    issued_grant_id = None
    if completion_mode == "auto_confirm" and (requested_mode == "auto_confirm" or not grant_id):
        from app.completion_grant import issue_completion_grant
        project = conn.execute(
            "SELECT bible_artifact_id FROM projects WHERE id=?", (ep["project_id"],)
        ).fetchone()
        grant, _token = issue_completion_grant(
            episode_id=episode_id,
            project_id=ep["project_id"],
            screenplay_artifact_id=ep["screenplay_artifact_id"] or "",
            bible_artifact_id=project["bible_artifact_id"] if project else None,
            impact_snapshot={
                "unlocks_paid_video": True,
                "auto_submit_video": False,
                "scope": "episode_storyboard_only",
                "authorization_source": "storyboard_resume_preflight",
            },
        )
        grant_id = grant.grant_id
        issued_grant_id = grant.grant_id
    previous = {
        "status": ep["status"],
        "script_error": ep["script_error"],
        "active_storyboard_run_id": ep["active_storyboard_run_id"],
        "storyboard_completion_mode": ep["storyboard_completion_mode"],
    }
    cursor = conn.execute(
        "UPDATE episodes SET status='scripting', script_error=NULL, storyboard_completion_mode=? "
        "WHERE id=? AND screenplay_publish_fence=0", (completion_mode, episode_id)
    )
    if cursor.rowcount != 1:
        conn.rollback()
        if issued_grant_id:
            from app.completion_grant import revoke_grant
            revoke_grant(issued_grant_id)
        raise HTTPException(409, "剧本发布栅栏已生效，未继续分镜")
    conn.commit()
    recorder = None
    coro = None
    try:
        recorder = _new_storyboard_recorder(
            episode_id,
            trigger_type="resume",
            parent_run_id=parent["id"] if parent else None,
            completion_mode=completion_mode,
        )
        # 任务注册前持久化指针，避免 Run 已启动但页面无法轮询或控制。
        conn.execute(
            "UPDATE episodes SET active_storyboard_run_id=? WHERE id=?",
            (recorder.run_id, episode_id),
        )
        conn.commit()
        coro = _recorded_storyboard_task(
            episode_id, recorder, resume=True,
            completion_mode=completion_mode,
            completion_grant_id=grant_id,
        )
        task_registry.spawn(
            "storyboard", episode_id, coro, project_id=ep["project_id"],
        )
    except Exception as exc:
        if coro is not None:
            coro.close()
        conn.execute(
            """UPDATE episodes
               SET active_storyboard_run_id=?, status=?, script_error=?,
                   storyboard_completion_mode=?
               WHERE id=? AND active_storyboard_run_id IS ?""",
            (
                previous["active_storyboard_run_id"],
                previous["status"],
                previous["script_error"],
                previous["storyboard_completion_mode"],
                episode_id,
                (
                    recorder.run_id
                    if recorder is not None
                    else previous["active_storyboard_run_id"]
                ),
            ),
        )
        conn.commit()
        if issued_grant_id:
            from app.completion_grant import revoke_grant
            revoke_grant(issued_grant_id)
        if recorder is not None:
            try:
                recorder.cancel("分镜继续任务未能启动，状态已回滚")
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "STORYBOARD_RESUME_SPAWN_FAILED",
            "message": "分镜继续任务未能启动，已回滚到可重试状态",
            "recovery_action": "请稍后重试；已通过镜头和 checkpoint 均已保留",
            "episode_id": episode_id,
            "run_id": recorder.run_id if recorder is not None else None,
            "rolled_back": True,
            "recoverable": True,
        }) from exc
    checkpoint_saved = int(cp.validated_prefix_end or 0) if cp else 0
    resumed_from_shot = max(int(saved), checkpoint_saved)
    checkpoint_next = int(cp.next_shot_no or 0) if cp else 0
    return {
        "status": "scripting",
        "run_id": recorder.run_id,
        "resumed_from_shot": resumed_from_shot,
        "next_shot_no": checkpoint_next or resumed_from_shot + 1,
        "checkpoint_only": bool(not saved and checkpoint_saved),
        "completion_mode": completion_mode,
        "completion_grant_id": grant_id,
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
        "SELECT id, status, script_error, screenplay_status, screenplay_json, screenplay_publish_fence, "
        "active_storyboard_run_id "
        "FROM episodes WHERE project_id=? AND status IN ('planned','scripting','script_failed') ORDER BY episode_no",
        (project_id,)).fetchall()
    # 待分镜的；以及卡在“分镜中”却没有在跑任务的孤儿（需重新触发）
    candidates = [
        r for r in rows
        if r["screenplay_status"] == "ready" and r["screenplay_json"]
        and not r["screenplay_publish_fence"]
        and not _storyboard_generation_is_live(dict(r))
    ]
    if not candidates:
        raise HTTPException(409, "没有可展开分镜的剧集（需先生成剧本，且状态为待分镜/分镜失败/卡住的分镜中）")
    sem = asyncio.Semaphore(max(int(get_setting("storyboard_concurrency") or 2), 1))
    run_ids: list[str] = []
    failed_to_start: list[dict] = []
    for candidate in candidates:
        eid = candidate["id"]
        recorder = None
        try:
            recorder = _new_storyboard_recorder(eid, trigger_type="batch")
        except Exception as exc:
            public = errors.record_and_format(
                exc, action="storyboard_batch_recorder",
                context={"project_id": project_id, "episode_id": eid},
            )
            failed_to_start.append({"episode_id": eid, "error": public, "retryable": True})
            continue
        installed = conn.execute(
            """UPDATE episodes
               SET status='scripting', script_error=NULL, active_storyboard_run_id=?
               WHERE id=? AND status=? AND active_storyboard_run_id IS ?
                 AND screenplay_publish_fence=0
                 AND NOT EXISTS (
                     SELECT 1 FROM workflow_runs AS wr
                     WHERE wr.id=episodes.active_storyboard_run_id
                       AND wr.status IN ('CREATED','RUNNING')
                 )""",
            (
                recorder.run_id,
                eid,
                candidate["status"],
                candidate["active_storyboard_run_id"],
            ),
        )
        if installed.rowcount != 1:
            conn.rollback()
            recorder.cancel("批量分镜启动权已变化，当前运行未启动")
            failed_to_start.append({
                "episode_id": eid,
                "error": "剧集状态刚刚发生变化，本次未接管",
                "retryable": True,
            })
            continue
        conn.commit()
        coro = _storyboard_guarded_recorded(eid, sem, recorder)
        try:
            task_registry.spawn(
                "storyboard", eid, coro, project_id=project_id,
            )
        except Exception as exc:
            coro.close()
            rollback_status = (
                "script_failed" if candidate["status"] == "scripting" else candidate["status"]
            )
            rollback_error = (
                "检测到上次分镜任务已中断；本次批量任务也未能启动，可继续重试"
                if candidate["status"] == "scripting"
                else candidate["script_error"]
            )
            conn.execute(
                """UPDATE episodes
                   SET active_storyboard_run_id=NULL, status=?, script_error=?
                   WHERE id=? AND active_storyboard_run_id=?""",
                (rollback_status, rollback_error, eid, recorder.run_id),
            )
            conn.commit()
            recorder.cancel("批量分镜任务未能启动，状态已回滚")
            public = errors.record_and_format(
                exc, action="storyboard_batch_spawn",
                context={"project_id": project_id, "episode_id": eid},
            )
            failed_to_start.append({"episode_id": eid, "error": public, "retryable": True})
            continue
        run_ids.append(recorder.run_id)
    if not run_ids:
        raise HTTPException(503, {
            "code": "STORYBOARD_BATCH_START_FAILED",
            "message": "批量分镜任务均未能启动，各集剧本和恢复点已保留，可直接重试",
            "failed_to_start": failed_to_start,
        })
    return {
        "started": len(run_ids),
        "run_ids": run_ids,
        "failed_to_start": failed_to_start,
        "retryable_failures": len(failed_to_start),
    }


async def _storyboard_guarded_recorded(
    episode_id: str, sem: asyncio.Semaphore, recorder: WorkflowRecorder
) -> None:
    async with sem:
        await _recorded_storyboard_task(episode_id, recorder, resume=True)


@router.post("/episodes/{episode_id}/storyboard/cancel")
async def cancel_storyboard(episode_id: str, body: dict | None = Body(None)):
    """手动取消正在进行的分镜生成请求，解除 scripting 锁定，便于重新发起。
    用于模型侧卡死/异常导致状态长期停留在“分镜中”的情况。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("storyboard.cancel", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    if ep["status"] != "scripting":
        return {
            "status": ep["status"],
            "deduplicated": True,
            "message": "任务已自然结束或此前已停止；当前状态保持不变",
        }
    await task_registry.cancel_and_wait("storyboard", episode_id)
    from app.storyboard_workspace import finalize_storyboard_cancellation
    return finalize_storyboard_cancellation(
        episode_id,
        run_id=ep["active_storyboard_run_id"],
        message="已从分镜台取消生成",
    )


def _storyboard_status_snapshot(
    ep: dict,
    shots: list[dict],
    supervisor: dict | None,
    screenplay: EpisodeScreenplay | None = None,
) -> dict:
    """返回供所有分镜台区域共同消费的 v1 原子状态投影。"""
    from app.storyboard_workspace import episode_fingerprint, monotonic_snapshot_version

    screenplay_ready = ep.get("screenplay_status") == "ready" and bool(ep.get("screenplay_artifact_id"))
    shot_count = len(shots)
    outline_count = 0
    try:
        outline_count = len(json.loads(ep.get("storyboard_outline_json") or "{}").get("shots") or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        outline_count = 0
    # 结构编辑会撤销整集分镜产物，并把新顺序写回大纲。此时旧 supervisor
    # checkpoint 只描述上一次生成，不能继续覆盖用户刚批准的新计划镜数。
    structural_draft = ep.get("storyboard_artifact_id") is None and outline_count > 0
    planned = int(
        (outline_count if structural_draft else 0)
        or (supervisor or {}).get("expected_total")
        or ep.get("storyboard_planned_shots")
        or outline_count
        or shot_count
        or 0
    )
    passed = min(
        shot_count,
        int((supervisor or {}).get("validated_prefix_end") or shot_count),
    )
    final_valid = bool(shots and shots[-1].get("is_final"))
    phase = str((supervisor or {}).get("phase") or "")
    running = ep.get("status") == "scripting" and phase not in {
        "PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_HUMAN", "WAITING_AUTHORIZATION", "WAITING_RETRY",
        "CANCELLED",
    }
    paused = phase in {"PAUSED_EXTERNAL", "PAUSED_BUDGET", "WAITING_HUMAN", "WAITING_AUTHORIZATION", "WAITING_RETRY"}
    confirmed = ep.get("status") in {"confirmed", "generating", "done"}
    terminal_structure = bool(
        ep.get("status") == "scripted"
        and shot_count > 0
        and planned == shot_count
        and passed == shot_count
        and final_valid
        and (
            not supervisor
            or phase in {"PREPARING_CONFIRM", "CONFIRMING", "SUCCEEDED"}
        )
    )
    gate_errors: list[str] = []
    score_warnings: list[str] = []
    if terminal_structure:
        try:
            from app.storyboard_workspace import verify_or_bind_existing_excerpt

            board = Storyboard(
                episode_no=int(ep["episode_no"]),
                shots=[Shot.model_validate(shot) for shot in shots],
            )
            project = get_conn().execute(
                "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
            ).fetchone()
            bible = _project_bible_or_placeholder(project)
            evaluation = evaluate_storyboard_for_confirmation(
                ep,
                board,
                screenplay,
                bible,
                has_real_bible=bool((project["bible_json"] or "").strip()) if project else False,
                record_metrics=False,
            )
            gate_errors.extend(evaluation.errors)
            score_warnings.extend(evaluation.warnings)
            for shot in shots:
                try:
                    verify_or_bind_existing_excerpt(
                        ep["id"], shot["id"], shot.get("source_excerpt") or "",
                        persist_legacy=False,
                    )
                except HTTPException as exc:
                    detail = exc.detail
                    gate_errors.append(
                        detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
                    )
        except Exception as exc:  # noqa: BLE001
            gate_errors.append(f"确认门禁暂不可用：{exc}")
    for index, shot in enumerate(shots):
        shot_no = int(shot.get("shot_no") or index + 1)
        localized = [
            message for message in gate_errors
            if f"shots[{index}]" in message or f"shot_no={shot_no}" in message
        ]
        # Score-only：质量 warning 仍挂到镜头供 UI 展示，但不进入确认硬门禁。
        localized_scores = [
            message for message in score_warnings
            if f"shots[{index}]" in message or f"shot_no={shot_no}" in message
        ]
        if localized_scores:
            shot["qa_warnings"] = localized_scores
        display = localized + localized_scores
        if display:
            shot["preflight_errors"] = display
    full_terminal = bool(terminal_structure and not gate_errors)
    invalid = bool(
        (running and confirmed)
        or (full_terminal and running)
        or (confirmed and not shots)
    )
    if invalid:
        state, headline, action = "syncing", "状态同步中，暂不可执行高影响操作", "refresh_status"
    elif not screenplay_ready:
        state, headline, action = "no_screenplay", "尚无可用于分镜的剧本", "go_screenplay"
    elif running:
        state, headline, action = "running", f"正在生成：已通过 {passed}/{planned or '—'} 镜", "view_progress"
    elif paused and not terminal_structure:
        state, headline, action = "paused", f"已暂停，已有 {passed} 镜通过", "resume_storyboard"
    elif terminal_structure and gate_errors:
        state, headline, action = "failed", f"还有 {len(gate_errors)} 个确认门禁问题，可继续修改", "resume_storyboard"
    elif ep.get("status") == "script_failed" or (ep.get("script_error") and not full_terminal):
        state, headline, action = "failed", f"生成停在第 {max(1, passed + 1)} 镜，可继续处理", "resume_storyboard"
    elif confirmed:
        state, headline, action = "confirmed", "当前分镜已确认", "go_review_wall"
    elif not shots:
        state, headline, action = "empty", "剧本已就绪，尚未生成分镜", "generate_storyboard"
    elif full_terminal:
        state, headline, action = "ready_to_confirm", f"{shot_count}/{planned} 镜已通过，等待确认", "confirm_storyboard"
    else:
        state, headline, action = "syncing", "分镜尚未达到完整终态", "refresh_status"
    fingerprint = episode_fingerprint(ep["id"])
    feature_flags = {
        "safe_readonly": str(get_setting("storyboard_workspace_safe_readonly") or "false").lower() == "true",
        "structure_edit": str(get_setting("storyboard_structure_edit_enabled") or "true").lower() == "true",
        "source_rebind": str(get_setting("storyboard_source_rebind_enabled") or "true").lower() == "true",
    }
    if feature_flags["safe_readonly"]:
        state = "syncing"
        headline = "分镜台处于安全只读模式，可继续审阅"
        action = "refresh_status"
    return {
        "contract_version": "storyboard-workspace.v1",
        "snapshot_version": monotonic_snapshot_version(ep["id"], fingerprint),
        "state_fingerprint": fingerprint,
        "state": state,
        "headline": headline,
        "screenplay_available": screenplay_ready,
        "task_phase": phase or None,
        "planned_shots": planned,
        "produced_shots": shot_count,
        "validated_shots": passed,
        "final_shot_valid": final_valid,
        "hard_gates_passed": full_terminal or confirmed,
        "hard_gate_issue_count": len(gate_errors),
        "hard_gate_issues": gate_errors[:30],
        "feature_flags": feature_flags,
        "confirmed": confirmed,
        "editable": bool(screenplay_ready and not running and not invalid and not feature_flags["safe_readonly"]),
        "confirmable": bool(full_terminal and not feature_flags["safe_readonly"]),
        "recommended_action": action,
        "write_block_reason": (
            "分镜正在生成或修复，请先暂停" if running
            else "状态组合不安全，请刷新" if invalid or state == "syncing"
            else None
        ),
    }


@router.get("/episodes/{episode_id}/storyboard/status")
def storyboard_status(episode_id: str):
    detail = episode_detail(episode_id, view="board")
    return detail["storyboard_status"]


@router.get("/episodes/{episode_id}/storyboard/source")
def storyboard_authorized_source(episode_id: str):
    from app.storyboard_workspace import chapter_sources
    _episode_or_404(episode_id)
    enabled = str(get_setting("storyboard_source_rebind_enabled") or "true").lower() == "true"
    return {
        "episode_id": episode_id,
        "enabled": enabled,
        "chapters": chapter_sources(episode_id) if enabled else [],
        "disabled_reason": None if enabled else "原文重绑定正在灰度回滚；现有证据仍可只读审阅",
    }


@router.post("/shots/{shot_id}/edit-session")
def start_shot_edit_session(shot_id: str):
    from app.storyboard_workspace import create_edit_session
    return create_edit_session(shot_id)


def _public_shot_editable_value(shot: dict, key: str):
    value = shot.get(key)
    if key in {"characters", "dialogues"} and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value


@router.post("/shots/{shot_id}/impact-preview")
def preview_shot_edit_impact(shot_id: str, body: dict):
    from app.storyboard_workspace import (
        create_preview, episode_fingerprint, require_edit_session, validate_source_binding,
    )

    session = require_edit_session(body.get("edit_session_token"), shot_id)
    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise HTTPException(404, "镜头不存在")
    shot = dict(shot_row)
    changes = dict(body.get("changes") or {})
    forbidden = {"id", "episode_id", "shot_no", "storyboard_artifact_id"}.intersection(changes)
    if forbidden:
        raise HTTPException(422, f"不可直接修改字段：{'、'.join(sorted(forbidden))}")
    if "source_excerpt" in changes:
        raise HTTPException(422, "原文证据不可自由输入，请从本集授权原文框选")
    if "source_binding" in changes:
        excerpt, normalized_binding = validate_source_binding(shot["episode_id"], changes["source_binding"])
        changes["source_binding"] = normalized_binding
        changes["source_excerpt"] = excerpt
    changed_fields = [
        key for key, value in changes.items()
        if key != "source_binding" and _public_shot_editable_value(shot, key) != value
    ]
    if not changed_fields:
        try:
            from app.observability.metrics import inc
            inc("storyboard_save_noop_total", episode_id=shot["episode_id"], shot_id=shot_id)
        except Exception:  # noqa: BLE001
            pass
        return {
            "unchanged": True,
            "changed_fields": [],
            "message": "结构化内容没有变化，不会创建新版本或失效下游",
        }
    version_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shot_versions WHERE shot_id=?", (shot_id,),
    ).fetchone()["c"])
    scene_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM shot_scenes WHERE shot_id=?", (shot_id,),
    ).fetchone()["c"])
    descendants: list[dict] = []
    if shot.get("storyboard_artifact_id"):
        descendants = evidence_repository.get_lineage(shot["storyboard_artifact_id"]).get("descendants") or []
    payload = {
        "unchanged": False,
        "changed_fields": changed_fields,
        "normalized_changes": changes,
        "baseline_artifact_id": session.get("baseline_artifact_id"),
        "baseline_content_hash": session["baseline_content_hash"],
        "requires_reconfirm": True,
        "paid_media_invalidated": bool(version_count or scene_count),
        "stale_descendant_ids": [str(item["id"]) for item in descendants if item.get("status") != "stale"],
        "stale_count": len(descendants),
        "by_artifact_type": {
            "参考图": scene_count,
            "视频版本": version_count,
            "证据链": len(descendants),
        },
        "revalidation_shots": sorted({max(1, int(shot["shot_no"]) - 1), int(shot["shot_no"]), int(shot["shot_no"]) + 1}),
        "rebuild": {
            "image_count": scene_count,
            "unit_price_cny": 0,
            "estimated_cost_cny": round(shot_cost_cny(int(changes.get("duration_s") or shot["duration_s"])), 2) if version_count else 0,
            "max_retry_budget_cny": round(shot_cost_cny(int(changes.get("duration_s") or shot["duration_s"])) * 2, 2) if version_count else 0,
            "note": "视频重生成费用按届时服务端费率重新报价",
        },
    }
    return create_preview(
        "shot_edit", shot["episode_id"], payload,
        shot_id=shot_id, baseline_fingerprint=episode_fingerprint(shot["episode_id"]),
    )


@router.get("/shots/{shot_id}/drafts")
def list_shot_edit_drafts(shot_id: str):
    shot = get_conn().execute("SELECT episode_id,shot_no FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    rows = get_conn().execute(
        """SELECT id,version,status,content_json,parent_artifact_ids_json,created_at
           FROM artifacts WHERE type='storyboard_shot' AND scope_type='storyboard_checkpoint'
             AND scope_id=? AND status='needs_revision' ORDER BY created_at DESC""",
        (f"{shot['episode_id']}:{shot['shot_no']}",),
    ).fetchall()
    items = []
    for row in rows:
        evaluations = evidence_repository.get_evaluations(row["id"])
        issues = []
        for evaluation in evaluations:
            evidence = evaluation.get("evidence") or {}
            issues.extend(evidence.get("issues") or [])
        items.append({
            "id": row["id"], "version": row["version"], "status": row["status"],
            "content": json.loads(row["content_json"] or "{}"),
            "baseline_artifact_ids": json.loads(row["parent_artifact_ids_json"] or "[]"),
            "issues": issues, "created_at": row["created_at"],
        })
    return {"items": items}


@router.delete("/shots/{shot_id}/drafts/{draft_id}")
def discard_shot_edit_draft(shot_id: str, draft_id: str):
    conn = get_conn()
    shot = conn.execute("SELECT episode_id,shot_no FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    scope = f"{shot['episode_id']}:{shot['shot_no']}"
    row = conn.execute(
        "SELECT id FROM artifacts WHERE id=? AND scope_id=? AND status='needs_revision'",
        (draft_id, scope),
    ).fetchone()
    if not row:
        raise HTTPException(404, "失败草稿不存在")
    conn.execute("UPDATE artifacts SET status='rejected' WHERE id=?", (draft_id,))
    conn.commit()
    return {"discarded": True, "published_unchanged": True}


def _structure_operation_plan(episode_id: str, body: dict) -> dict:
    if str(get_setting("storyboard_structure_edit_enabled") or "true").lower() != "true":
        raise HTTPException(409, "镜头结构调整当前未开放；可修改现有问题镜或继续 Agent 修复")
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise HTTPException(404, "剧集不存在")
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜运行中不能调整镜头结构，请先暂停")
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "当前没有可调整的镜头")
    operation = str(body.get("operation") or "")
    if operation not in {"add_after", "duplicate_after", "delete", "move"}:
        raise HTTPException(422, "结构操作必须是新增、复制、删除或移动")
    shot_id = str(body.get("shot_id") or "")
    index = next((idx for idx, row in enumerate(rows) if row["id"] == shot_id), -1)
    if index < 0:
        raise HTTPException(404, "目标镜头不存在")
    target_index = int(body.get("target_index", index))
    target_index = max(0, min(len(rows) - 1, target_index))
    contract = {}
    try:
        contract = json.loads(rows[index]["shot_contract_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        contract = {}
    if operation == "delete" and len(rows) == 1:
        raise HTTPException(409, "不能删除全剧唯一镜头")
    if operation == "delete" and contract.get("is_final") and not body.get("new_final_shot_id"):
        raise HTTPException(409, "删除最终镜前必须指定新的最终镜")
    new_count = len(rows) + (1 if operation in {"add_after", "duplicate_after"} else -1 if operation == "delete" else 0)
    old_order = [row["id"] for row in rows]
    preview_order = list(old_order)
    placeholder = "new-shot" if operation == "add_after" else "copy-shot"
    if operation in {"add_after", "duplicate_after"}:
        preview_order.insert(index + 1, placeholder)
    elif operation == "delete":
        preview_order.pop(index)
    else:
        moved = preview_order.pop(index)
        preview_order.insert(target_index, moved)
    affected_nos = sorted({
        max(1, index), index + 1, min(max(1, new_count), index + 2),
        target_index + 1,
    })
    version_count = int(conn.execute(
        """SELECT COUNT(*) AS c FROM shot_versions v JOIN shots s ON s.id=v.shot_id
           WHERE s.episode_id=?""", (episode_id,),
    ).fetchone()["c"])
    return {
        "operation": operation,
        "shot_id": shot_id,
        "target_index": target_index,
        "new_final_shot_id": body.get("new_final_shot_id"),
        "before_count": len(rows),
        "after_count": new_count,
        "before_order": old_order,
        "after_order": preview_order,
        "renumbered_shots": sum(1 for i, value in enumerate(preview_order) if i >= len(old_order) or value != old_order[i]),
        "revalidation_shots": affected_nos,
        "requires_reconfirm": True,
        "paid_media_invalidated": version_count > 0,
        "stale_count": version_count,
        "by_artifact_type": {"视频版本": version_count},
        "final_shot_impact": "将重新指定最终镜" if operation == "delete" and contract.get("is_final") else "最终镜保持唯一",
    }


@router.post("/episodes/{episode_id}/storyboard/structure-preview")
def preview_storyboard_structure(episode_id: str, body: dict):
    from app.storyboard_workspace import create_preview, episode_fingerprint
    payload = _structure_operation_plan(episode_id, body)
    return create_preview(
        "structure", episode_id, payload,
        shot_id=payload["shot_id"], baseline_fingerprint=episode_fingerprint(episode_id),
    )


def _set_row_final_contract(conn, shot_id: str, final: bool) -> None:
    row = conn.execute("SELECT shot_contract_json FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        return
    try:
        contract = json.loads(row["shot_contract_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        contract = {}
    contract["is_final"] = bool(final)
    conn.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id=?",
        (json.dumps(contract, ensure_ascii=False), shot_id),
    )


@router.post("/episodes/{episode_id}/storyboard/structure")
def apply_storyboard_structure(episode_id: str, body: dict):
    from app.storyboard_workspace import consume_preview, require_preview, source_binding_for_shot

    preview = require_preview(
        body.get("preview_token"), "structure", episode_id,
        shot_id=str(body.get("shot_id") or ""),
    )
    expected = {
        "operation": body.get("operation"),
        "shot_id": body.get("shot_id"),
        "target_index": int(body.get("target_index", preview.get("target_index", 0))),
        "new_final_shot_id": body.get("new_final_shot_id"),
    }
    for key, value in expected.items():
        if value != preview.get(key):
            raise HTTPException(409, "结构操作与已批准预览不一致，请重新预览")
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    previous_outline_by_id: dict[str, StoryboardOutlineShot] = {}
    try:
        previous_outline = StoryboardOutline.model_validate_json(ep["storyboard_outline_json"] or "{}")
        previous_outline_by_id = {
            row["id"]: previous_outline.shots[int(row["shot_no"]) - 1]
            for row in rows
            if 0 < int(row["shot_no"]) <= len(previous_outline.shots)
        }
    except (TypeError, ValueError, IndexError):
        previous_outline_by_id = {}
    by_id = {row["id"]: row for row in rows}
    target = by_id.get(str(body.get("shot_id")))
    if not target:
        raise HTTPException(404, "目标镜头不存在")
    operation = str(body["operation"])
    ordered_ids = [row["id"] for row in rows]
    created_id = None
    deleted_id = None
    if operation in {"add_after", "duplicate_after"}:
        source_model = _board_from_shot_rows([target], 1).shots[0].model_copy(deep=True)
        source_model.shot_no = int(target["shot_no"]) + 1
        source_model.is_final = False
        if operation == "add_after":
            source_model.dialogues = []
            source_model.audio_timeline = []
            source_model.primary_action = ""
            source_model.action_desc = "请补充本镜画面动作"
        screenplay = _load_screenplay(ep)
        if screenplay is None:
            raise HTTPException(409, "本集剧本不可用，不能新增镜头")
        conn.execute("UPDATE shots SET shot_no=-shot_no WHERE episode_id=?", (episode_id,))
        created_id = _insert_storyboard_shot(conn, episode_id, screenplay, source_model)
        insert_at = ordered_ids.index(target["id"]) + 1
        ordered_ids.insert(insert_at, created_id)
        binding = source_binding_for_shot(target["id"])
        if binding:
            conn.execute(
                """INSERT INTO storyboard_source_bindings(
                       shot_id,chapter_id,chapter_idx,source_version_hash,start_offset,end_offset,excerpt_hash,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    created_id, binding["chapter_id"], binding["chapter_idx"], binding["source_version_hash"],
                    binding["start_offset"], binding["end_offset"], binding["excerpt_hash"], now(),
                ),
            )
    elif operation == "delete":
        deleted_id = target["id"]
        ordered_ids.remove(deleted_id)
        conn.execute("DELETE FROM shots WHERE id=?", (deleted_id,))
        conn.execute("UPDATE shots SET shot_no=-shot_no WHERE episode_id=?", (episode_id,))
    else:
        ordered_ids.remove(target["id"])
        ordered_ids.insert(int(preview["target_index"]), target["id"])
        conn.execute("UPDATE shots SET shot_no=-shot_no WHERE episode_id=?", (episode_id,))
    for index, item_id in enumerate(ordered_ids, start=1):
        conn.execute("UPDATE shots SET shot_no=? WHERE id=?", (index, item_id))
    final_id = body.get("new_final_shot_id")
    if final_id:
        if final_id not in ordered_ids:
            raise HTTPException(422, "指定的新最终镜不存在")
        for item_id in ordered_ids:
            _set_row_final_contract(conn, item_id, item_id == final_id)
    else:
        finals = []
        for item_id in ordered_ids:
            row = conn.execute("SELECT shot_contract_json FROM shots WHERE id=?", (item_id,)).fetchone()
            try:
                if json.loads(row["shot_contract_json"] or "{}").get("is_final"):
                    finals.append(item_id)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if len(finals) != 1:
            for item_id in ordered_ids:
                _set_row_final_contract(conn, item_id, item_id == ordered_ids[-1])
    # 结构操作本身就是对“计划镜头序列”的修改。把新的连续顺序同步回唯一计划源，
    # 否则旧 checkpoint 的 expected_total 会令新增/删除后的工作区永远无法进入完整终态。
    current_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    outline_shots: list[StoryboardOutlineShot] = []
    for row in current_rows:
        model = _board_from_shot_rows([row], int(ep["episode_no"])).shots[0]
        prior = previous_outline_by_id.get(row["id"])
        if row["id"] == created_id and operation == "duplicate_after":
            prior = previous_outline_by_id.get(target["id"])
        brief = prior.model_copy(deep=True) if prior else StoryboardOutlineShot(shot_no=int(row["shot_no"]))
        brief.shot_no = int(row["shot_no"])
        brief.scene_setting = model.scene_setting or ""
        brief.beat = (model.action_desc or model.primary_action or "请补充本镜画面动作").strip()
        brief.covers = model.source_excerpt or ""
        brief.primary_action = model.primary_action or ""
        brief.emotion_beat = model.emotion_beat or ""
        brief.state_in = model.state_in or ""
        brief.state_out = model.state_out or ""
        brief.continuity_mode = model.continuity_mode or ""
        brief.duration_s = int(model.duration_s or 0) or None
        brief.characters_visible = list(model.characters_visible or [])
        brief.audio_cast = list(model.audio_cast or [])
        if row["id"] == created_id and operation == "add_after":
            brief.story_event_id = ""
            brief.spine_beat_ids = []
            brief.key_line_ids = []
            brief.information_ids = []
            brief.new_information_ids = []
        outline_shots.append(brief)
    updated_outline = StoryboardOutline(episode_no=int(ep["episode_no"]), shots=outline_shots)
    conn.execute(
        """UPDATE episodes
           SET status='scripted', script_error=NULL, storyboard_artifact_id=NULL,
               storyboard_outline_json=?
           WHERE id=?""",
        (updated_outline.model_dump_json(), episode_id),
    )
    conn.commit()
    invalidated = 0
    for item_id in ordered_ids:
        cleared = worker.clear_shot_artifacts(item_id) or {}
        invalidated += int(cleared.get("videos", 0)) + int(cleared.get("references", 0))
    consume_preview(str(body["preview_token"]))
    return {
        "ok": True,
        "operation": operation,
        "created_shot_id": created_id,
        "deleted_shot_id": deleted_id,
        "shot_count": len(ordered_ids),
        "invalidated": invalidated,
        "requires_reconfirm": True,
        "revalidation_shots": preview.get("revalidation_shots") or [],
    }


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
    reference_lineage: dict[str, list[str]] = {}
    if include_inputs:
        for version in versions:
            raw_meta = json.loads(version.get("image_inputs") or "{}")
            for raw_ref in raw_meta.get("reference_images") or []:
                ref_id = raw_ref.get("id") if isinstance(raw_ref, dict) else None
                if ref_id:
                    reference_lineage.setdefault(str(ref_id), []).append(str(version["id"]))
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
        for ref in refs:
            ref["referenced_by_version_ids"] = reference_lineage.get(str(ref.get("id")), [])
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
    if view in (None, "board"):
        from app.storyboard_workspace import reconcile_cancelled_storyboard_run
        reconcile_cancelled_storyboard_run(episode_id)
    full = view is None
    ep = dict(_episode_or_404(episode_id))
    conn = get_conn()
    ep["source_chapters"] = json.loads(ep["source_chapters"] or "[]")
    script = _load_screenplay(ep) if full or view in ("script", "board") else None
    ep["screenplay"] = script.model_dump() if script and (full or view in ("script", "board")) else None
    ep["screenplay_mode"] = _screenplay_mode(script)
    ep["required_dialogue_lines"] = _screenplay_required_dialogues(ep)
    if full or view == "script":
        from app.validators import source_dialogue_fragments
        from app.domain.screenplay_ops import (
            _screenplay_occurrences,
            _screenplay_required_occurrence_ids,
            _screenplay_status_snapshot,
        )

        source_text = _episode_source_text(conn, ep)
        ep["source_dialogue_lines"] = source_dialogue_fragments(source_text)
        ep["source_dialogue_occurrences"] = _screenplay_occurrences(
            source_text, ep["source_chapters"]
        )
        ep["required_dialogue_occurrence_ids"] = _screenplay_required_occurrence_ids(ep)
    else:
        ep["source_dialogue_lines"] = None
        ep["source_dialogue_occurrences"] = None
        ep["required_dialogue_occurrence_ids"] = []
    ep.pop("screenplay_required_dialogues", None)
    ep.pop("screenplay_required_dialogue_occurrences", None)
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
    if full or view == "script":
        from app.production.revision import screenplay_production_state
        ep["screenplay_production"] = screenplay_production_state(episode_id)
    else:
        ep["screenplay_production"] = None
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
    if full or view == "script":
        ep["screenplay_state"] = _screenplay_status_snapshot(
            ep, shot_count=shot_count, production=ep.get("screenplay_production")
        )
    else:
        ep["screenplay_state"] = None
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
        if full or view == "board":
            from app.storyboard_workspace import source_binding_for_shot
            s["source_binding"] = source_binding_for_shot(s["id"])
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
        s["video_stale"] = _shot_video_is_stale(conn, s, ep.get("storyboard_artifact_id"))
        if view == "board":
            s["version_count"] = version_counts.get(s["id"], 0)
            s["versions"] = []
            s["pipeline"] = None
            continue

        s["versions"] = _public_shot_versions(conn, s["id"], include_inputs=full)
        s["pipeline"] = pipeline_statuses.get(s["id"])
        s["video_status"] = (
            s["pipeline"].get("video_status") if s["pipeline"] else None
        )
        # 透出 grade / fallback，供生成台 A/B 分色
        try:
            from app.evidence.media import grade_shot_video
            graded = grade_shot_video(s["id"])
            s["video_grade"] = graded.get("grade")
            s["fallback_reason"] = graded.get("fallback_reason")
            s["continuity_degraded"] = bool(graded.get("continuity_degraded"))
        except Exception:  # noqa: BLE001
            s["video_grade"] = None
            s["fallback_reason"] = None
            s["continuity_degraded"] = False
    ep["shots"] = shots
    if full or view == "board":
        ep["storyboard_status"] = _storyboard_status_snapshot(
            ep, shots, ep.get("supervisor"), script,
        )
    ep["pipeline_summary"] = pipeline_summary
    # 视频补齐 Supervisor 面板（生成台）
    if full or view == "wall":
        try:
            from app.video_supervisor import load_latest_checkpoint, public_checkpoint_projection
            vcp = load_latest_checkpoint(episode_id)
            ep["video_supervisor"] = public_checkpoint_projection(vcp)
            try:
                ep["active_video_run_id"] = ep.get("active_video_run_id")
                ep["video_completion_mode"] = ep.get("video_completion_mode") or "quick"
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            ep["video_supervisor"] = None
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
    shot["video_stale"] = _shot_video_is_stale(
        conn, shot, episode_row["storyboard_artifact_id"] if episode_row else None
    )
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
    shot["video_status"] = (
        shot["pipeline"].get("video_status") if shot["pipeline"] else None
    )
    try:
        from app.evidence.media import grade_shot_video
        graded = grade_shot_video(shot_id)
        shot["video_grade"] = graded.get("grade")
        shot["fallback_reason"] = graded.get("fallback_reason")
        shot["continuity_degraded"] = bool(graded.get("continuity_degraded"))
    except Exception:  # noqa: BLE001
        shot["video_grade"] = None
        shot["fallback_reason"] = None
        shot["continuity_degraded"] = False
    return shot


@router.put("/shots/{shot_id}")
async def edit_shot(shot_id: str, body: dict):
    from app.capabilities.dispatch import ui_route
    expected_version = body.get("expected_version")
    meta_keys = {
        "expected_version", "edit_session_token", "preview_token",
        "baseline_content_hash", "change_source", "source_binding",
    }
    patch = {k: v for k, v in body.items() if k not in meta_keys}
    routed = await ui_route(
        "shot.update",
        {
            "shot_id": shot_id, "patch": patch, "expected_version": expected_version,
            "edit_session_token": body.get("edit_session_token"),
            "preview_token": body.get("preview_token"),
            "baseline_content_hash": body.get("baseline_content_hash"),
            "change_source": body.get("change_source") or "standard_edit",
            "source_binding": body.get("source_binding"),
        },
    )
    if routed is not None:
        return routed
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        raise HTTPException(404, "镜头不存在")
    from app.storyboard_workspace import (
        close_edit_session, consume_preview, persist_source_binding, require_edit_session,
        require_preview, validate_source_binding,
    )
    session = require_edit_session(body.get("edit_session_token"), shot_id)
    preview = require_preview(
        body.get("preview_token"), "shot_edit", shot["episode_id"], shot_id=shot_id,
    )
    if body.get("baseline_content_hash") != session["baseline_content_hash"]:
        raise HTTPException(409, "保存基线与进入编辑时不一致，请重新对比最新版")
    approved_changes = dict(preview.get("normalized_changes") or {})
    submitted_changes = dict(patch)
    source_binding = body.get("source_binding")
    normalized_source_binding = None
    if source_binding is not None:
        excerpt, normalized_source_binding = validate_source_binding(shot["episode_id"], source_binding)
        submitted_changes["source_excerpt"] = excerpt
    if submitted_changes != {k: v for k, v in approved_changes.items() if k != "source_binding"}:
        raise HTTPException(409, "保存内容与已批准的影响预览不一致，请重新预览")
    body = {
        **submitted_changes,
        "expected_version": expected_version,
        "edit_session_token": body.get("edit_session_token"),
        "preview_token": body.get("preview_token"),
        "baseline_content_hash": body.get("baseline_content_hash"),
        "change_source": body.get("change_source") or "standard_edit",
    }
    current_version = shot["storyboard_artifact_id"] or ""
    if expected_version is not None and str(expected_version) != str(current_version):
        raise HTTPException(
            409,
            f"镜头版本冲突：当前版本 {current_version or '空'}，请求基于 {expected_version}，请刷新后重试",
        )
    if not approved_changes:
        return {"ok": True, "unchanged": True, "artifact_id": current_version, "impact": {"stale_count": 0}}
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
    changed_fields = {key for key in submitted_changes if key != "source_binding"}
    sync = synchronize_spoken_contract(
        instance,
        changed_fields={k for k in ("dialogues", "audio_timeline") if k in changed_fields},
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
    if normalized_source_binding is not None:
        persist_source_binding(shot_id, normalized_source_binding)
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
    # Human authorship is provenance, not a hard gate. Record it separately so the
    # repository's all-hard-gates-must-pass commit rule only evaluates the actual
    # deterministic business gate.
    evidence_repository.create_evaluation(
        manual_artifact["id"],
        Evaluation(
            evaluator_type="human",
            evaluator_name="storyboard_editor",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=False,
            score=100,
            evidence={"decision": "authored_or_reviewed", "shot_id": shot_id},
        ),
    )
    manual_artifact = evidence_repository.commit_artifact(
        None,
        manual_artifact["id"],
        [Evaluation(
            evaluator_type="deterministic",
            evaluator_name="storyboard_shot_business_gate",
            evaluator_version=contract_version,
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"shot_id": shot_id, "spoken_contract_status": instance.spoken_contract_status},
        )],
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
    consume_preview(str(body["preview_token"]))
    close_edit_session(str(body["edit_session_token"]), "saved")
    try:
        from app.observability.metrics import inc
        inc(
            "storyboard_save_result_total", episode_id=episode_id, shot_id=shot_id,
            noop=False, validation="passed", source=body.get("change_source") or "standard_edit",
        )
    except Exception:  # noqa: BLE001
        pass
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
    dry_run = bool(_as_body_dict(body).get("dry_run", False))
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
    patch: dict = {
        "expected_version": shot["storyboard_artifact_id"],
        "edit_session_token": body.get("edit_session_token"),
        "baseline_content_hash": body.get("baseline_content_hash"),
        "preview_token": body.get("preview_token"),
        "change_source": "spoken_conflict_resolution",
    }
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
    patch["spoken_contract_status"] = "coherent"
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


@router.post("/shots/{shot_id}/spoken-conflict-preview")
def preview_spoken_conflict(shot_id: str, body: dict):
    from app.storyboard_workspace import create_edit_session

    choice = (body or {}).get("choice") or ""
    if choice not in {"rebuild_timeline_from_dialogues", "rebuild_dialogues_from_timeline"}:
        raise HTTPException(422, "请选择以台词或时间轴为准")
    conn = get_conn()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "镜头不存在")
    public = dict(row)
    public["characters"] = json.loads(public["characters"] or "[]")
    public["dialogues"] = json.loads(public["dialogues"] or "[]")
    _apply_contract_to_public_shot(public)
    changes = (
        {"dialogues": public["dialogues"], "spoken_contract_status": "coherent"}
        if choice == "rebuild_timeline_from_dialogues"
        else {"audio_timeline": public.get("audio_timeline") or [], "spoken_contract_status": "coherent"}
    )
    session = create_edit_session(shot_id)
    impact = preview_shot_edit_impact(shot_id, {
        "edit_session_token": session["edit_session_token"],
        "changes": changes,
    })
    if impact.get("unchanged"):
        # 即使其中一侧结构相同，也需要明确变更来源来完成冲突状态同步。
        raise HTTPException(409, "所选口播基准没有可重建内容，请选择另一侧或继续编辑")
    return {**impact, **session, "choice": choice}


def _storyboard_residual_hint(residual: list[str]) -> str:
    """Return an actionable repair hint for the current validation failures."""
    text = "；".join(residual)
    hints: list[str] = []
    if "口播上限" in text or "念不完" in text:
        hints.append("请在本镜台词区精简文案，或使用“在当前镜后新增”分担台词")
    if "角色圣经中不存在" in text or "既不在角色圣经" in text or "圣经角色为" in text:
        hints.append("请在本镜“画面角色”选择器中改选人物谱已有角色")
    if "未落实本镜大纲 covers" in text or "只停留在大纲" in text:
        hints.append("请在本镜“画面与动作”或“台词”中写出该剧情事实")
    if not hints:
        hints.append("请定位问题镜继续修改；如需自动处理，可在任务详情中选择继续生成或转人工")
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
