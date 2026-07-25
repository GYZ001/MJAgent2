from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

def _shot_contract_json(shot: Shot) -> str:
    from app.continuity import shot_contract_dict
    return json.dumps(shot_contract_dict(shot), ensure_ascii=False)


def _apply_contract_to_public_shot(target: dict) -> None:
    from app.continuity import apply_shot_contract
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
            "story_event_id", "purpose", "new_information_ids", "reinforcement_info_ids",
            "state_in", "primary_action", "emotion_beat", "state_out", "observed_state_out",
            "continuity_mode", "characters_visible", "audio_cast", "audio_timeline",
            "required_text", "reference_roles", "do_not_repeat", "risk_tags",
            "prompt_contract_version", "legacy_unvalidated", "camera_angle",
            "spatial_anchor", "is_final",
        }:
            target[key] = value


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


async def _storyboard_task(episode_id: str, *, resume: bool = True):
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
        # 定妆照按集反应式维护（在分镜展开前）：①新角色发现并补进人物谱——否则 validate_storyboard 会因
        # "角色圣经中不存在"把新角色从分镜里刷掉；②已有角色外观漂移则图生图重绘新段并同步 bible 锚点。
        # 新人物补卡失败是阻塞问题；已有角色漂移重绘失败是可见的非阻塞警告。
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
        # 场景图素材库按集反应式维护（分镜展开前）：剧本里出现、库里没有、够戏份的新场景 → 补入库 + 出图，
        # 使分镜能命中库内场景、validate_storyboard_scenes 通过。失败不阻断分镜（按现有库继续）。
        try:
            from app.scenes import ensure_scenes_for_storyboard
            sdisc = await ensure_scenes_for_storyboard(ep["project_id"], ep["episode_no"], screenplay, bible)
            if sdisc.get("added"):
                p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
                bible = _project_bible_or_placeholder(p)
        except Exception as exc:  # noqa: BLE001 场景库维护失败可见，但不阻断分镜
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
        source_text = _episode_source_text(conn, ep)
        compact_target = _storyboard_target_for_source(ep_data.get("target_duration_s"), len(source_text))
        if compact_target != ep_data.get("target_duration_s"):
            conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
            conn.commit()
            ep_data["target_duration_s"] = compact_target
        prev = conn.execute(
            "SELECT cliffhanger FROM episodes WHERE project_id=? AND episode_no=?",
            (ep["project_id"], ep["episode_no"] - 1)).fetchone()

        existing_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
        ).fetchall()
        if not resume:
            worker.delete_episode_shots(episode_id)
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=NULL, storyboard_artifact_id=NULL WHERE id=?",
                (episode_id,),
            )
            conn.commit()
            existing_rows = []
        # 先出整集分镜大纲定全局节奏，再逐镜按大纲填充——避免多镜停留同一情绪、剧情推进过慢。
        # 大纲是增强项：规划失败（如模型不可用）就回退到无大纲的纯逐镜生成，不阻断分镜。
        outline = None
        if resume and ep["storyboard_outline_json"]:
            try:
                from app.schemas import StoryboardOutline

                outline = StoryboardOutline.model_validate_json(ep["storyboard_outline_json"])
            except (TypeError, ValueError):
                outline = None
        if outline is None:
            try:
                outline = await generate_storyboard_outline(
                    ep_data, source_text, bible,
                    prev_ending=prev["cliffhanger"] if prev else "", screenplay=screenplay)
                conn.execute(
                    "UPDATE episodes SET storyboard_outline_json=?, storyboard_warning=NULL WHERE id=?",
                    (outline.model_dump_json(), episode_id),
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 大纲失败不阻断，但必须可见
                public = errors.record_and_format(
                    exc,
                    action="storyboard_outline_degraded",
                    context={
                        "project_id": ep["project_id"],
                        "episode_id": episode_id,
                        "episode_no": ep["episode_no"],
                    },
                )
                conn.execute(
                    "UPDATE episodes SET storyboard_warning=? WHERE id=?",
                    (f"分镜大纲失败，已退回纯逐镜生成：{public}", episode_id),
                )
                conn.commit()
                outline = None
        completed: list[Shot] = (
            list(_board_from_shot_rows(existing_rows, ep_data["episode_no"]).shots)
            if existing_rows else []
        )
        if completed:
            recovered_board = Storyboard(
                episode_no=ep_data["episode_no"], shots=list(completed)
            )
            character_changes = normalize_offbible_characters(recovered_board, bible)
            _persist_storyboard_character_policy_repairs(
                conn, episode_id, recovered_board, character_changes
            )
            completed = list(recovered_board.shots)
        if completed and outline and len(completed) >= len(outline.shots):
            recovered_board = Storyboard(episode_no=ep_data["episode_no"], shots=list(completed))
            recovered_errors = validate_storyboard(
                recovered_board, bible, ep_data["target_duration_s"]
            )
            recovered_errors.extend(validate_storyboard_soundtrack(
                recovered_board, screenplay, ep_data["target_duration_s"]
            ))
            recovered_errors.extend(validate_storyboard_preserves_key_content(recovered_board, screenplay))
            if not recovered_errors:
                _finalize_storyboard_evidence(episode_id, recovered_board)
                conn.execute(
                    "UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?", (episode_id,)
                )
                conn.commit()
                return
        final_feedback: list[str] | None = None
        _, max_shots = storyboard_shot_count_range(ep_data["target_duration_s"])
        # 落库大纲的当前长度（内存与 DB 此刻一致：resume 从 DB 加载、新规划刚回写）。
        planned_persisted = len(outline.shots) if (outline and outline.shots) else 0
        while True:
            draft = await generate_storyboard_next_shot(
                ep_data, source_text, bible,
                prev_ending=prev["cliffhanger"] if prev else "",
                screenplay=screenplay,
                completed_shots=completed,
                final_feedback=final_feedback,
                outline=outline,
            )
            # 与单镜 QA 使用同一套确定性归一口径后再落库。
            board = Storyboard(episode_no=ep_data["episode_no"], shots=[*completed, draft.shot])
            normalize_continuity(board)
            # 与逐镜 QA 同口径：实名角色服从角色圣经；功能性路人按确定性合同保留，其它圣经外名字剥离。
            # 每次分类都写入账本，避免“放宽角色限制”变成不可审计的静默绕过。
            for c in normalize_offbible_characters(board, bible):
                allowed_extra = bool(c.get("allowed_functional_extra"))
                log_provider_call(
                    "storyboard_character_policy", config.MODEL_TEXT,
                    "FUNCTIONAL_EXTRA_ALLOWED" if allowed_extra else "OFFBIBLE_NORMALIZED",
                    None, 0,
                    meta={"episode_id": episode_id, "episode_no": ep_data["episode_no"], "stage": "分镜脚本", **c})
            relieve_spoken_overflow(board)  # 与逐镜 QA 同口径：人群旁白降级为画面，单镜口播压回上限内
            normalize_transition_visuals(board)
            _sync_storyboard_shot_timing(conn, episode_id, board)
            shot = board.shots[-1]
            object.__setattr__(
                shot, "evidence_artifact_id", getattr(draft, "evidence_artifact_id", None)
            )
            _insert_storyboard_shot(conn, episode_id, screenplay, shot)
            completed = list(board.shots)
            conn.execute("UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,))
            conn.commit()
            # 计划自更新：把落库大纲追平实际镜头数，让前端"规划 N 镜"随逐镜拆镜细化实时更新（harness 事件留痕）。
            revision = _reconcile_storyboard_plan(
                conn, episode_id, ep_data["episode_no"], outline, completed, planned_persisted)
            if revision is not None:
                planned_persisted = revision[1]
            residual = list(getattr(draft, "residual_errors", []) or [])
            if residual:
                can_continue = (
                    bool(draft.is_final)
                    and len(completed) < max_shots
                    and len(residual) == 1
                    and "暂不能收尾" in residual[0]
                    and "继续补镜" in residual[0]
                )
                if can_continue:
                    # 这类 residual 的意思是"本镜不能当最后一镜"，不是本镜结构坏了；
                    # 保留它作为过渡镜，继续把缺失关键内容喂给后续镜头。
                    object.__setattr__(draft, "is_final", False)
                else:
                    note = (
                        f"镜{shot.shot_no:02d}{_storyboard_loop_exit_text(getattr(draft, 'loop_exit_reason', ''))}，"
                        "已作为「需修改镜头」保留在分镜台（逐镜 checkpoint 已保存，可从下一镜继续）；"
                        + _storyboard_residual_hint(residual)
                        + "。残余问题：" + "；".join(residual[:8])
                    )
                    conn.execute("UPDATE episodes SET status='scripted', script_error=? WHERE id=?",
                                 (note[:800], episode_id))
                    conn.commit()
                    break
            if draft.is_final:
                conn.execute("UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?", (episode_id,))
                conn.commit()
                _finalize_storyboard_evidence(
                    episode_id,
                    Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)),
                )
                break
            if len(completed) >= max_shots:
                raise StageError("分镜脚本", [f"已生成 {len(completed)} 镜但模型仍未收束到尾钩，请重试或人工补写最后一镜"])
            # 把"整集必保留台词/剧情点里还没落到镜头的部分"作为下一镜的补镜反馈，
            # 让缺口在后续镜头里逐步补齐，而不是拖到收尾镜才发现、再硬塞进单镜导致卡死。
            final_feedback = validate_storyboard_preserves_key_content(
                Storyboard(episode_no=ep_data["episode_no"], shots=list(completed)), screenplay) or None
    except (StageError, Exception) as exc:  # noqa: BLE001
        rec = errors.log_error(exc, action="storyboard_generate", context={"episode_id": episode_id})
        saved = conn.execute("SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)).fetchone()["c"]
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
    """恢复中断的分镜任务；从最后一个已提交的逐镜 checkpoint 继续。"""
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
            "checkpoint": "per_shot",
            "max_iterations_per_shot": contract.max_iterations,
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
        context = ContextPack(goal="按已确认剧本逐镜生成分镜并从 checkpoint 恢复")
        if ep["screenplay_json"]:
            context.add_text(
                "screenplay", ep["screenplay_json"],
                source_artifact_id=ep["screenplay_artifact_id"], limit=24000,
            )
        await recorder.step(
            "storyboard",
            lambda: _storyboard_task(episode_id, resume=resume),
            contract_key="storyboard",
            agent_name="storyboard",
            input_artifact_ids=input_ids,
            context_manifest=context.manifest(),
        )
        result = conn.execute(
            "SELECT status, script_error, storyboard_artifact_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if result and result["status"] == "scripted" and result["storyboard_artifact_id"]:
            recorder.succeed("分镜已完成并提交整版 Artifact")
        elif result and result["status"] == "scripted":
            recorder.partial(result["script_error"] or "分镜含需人工修订 checkpoint")
        else:
            recorder.fail(RuntimeError(result["script_error"] if result else "分镜生成失败"))
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:
        recorder.fail(exc)
        raise


@router.post("/episodes/{episode_id}/storyboard")
async def start_storyboard(episode_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "storyboard.generate", {"episode_id": episode_id, "mode": "fresh"},
    )
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["status"] == "scripting":
        raise HTTPException(409, "分镜正在生成中")
    if not _screenplay_ready(ep):
        raise HTTPException(409, "请先在剧本台生成本集可拍剧本")
    conn = get_conn()
    conn.execute("UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,))
    conn.commit()
    recorder = _new_storyboard_recorder(episode_id)
    task_registry.spawn(
        "storyboard", episode_id,
        _recorded_storyboard_task(episode_id, recorder, resume=False),
        project_id=ep["project_id"],
    )
    return {"status": "scripting", "run_id": recorder.run_id}


@router.post("/episodes/{episode_id}/storyboard/resume")
async def resume_storyboard(episode_id: str):
    """Continue after the last committed per-shot checkpoint without deleting it."""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "storyboard.generate", {"episode_id": episode_id, "mode": "resume"},
    )
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
    if not saved:
        raise HTTPException(409, "当前没有可恢复的逐镜 checkpoint，请重新生成分镜")
    parent = conn.execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    conn.execute(
        "UPDATE episodes SET status='scripting', script_error=NULL WHERE id=?", (episode_id,)
    )
    conn.commit()
    recorder = _new_storyboard_recorder(
        episode_id,
        trigger_type="resume",
        parent_run_id=parent["id"] if parent else None,
    )
    task_registry.spawn(
        "storyboard",
        episode_id,
        _recorded_storyboard_task(episode_id, recorder, resume=True),
        project_id=ep["project_id"],
    )
    return {
        "status": "scripting",
        "run_id": recorder.run_id,
        "resumed_from_shot": int(saved),
        "next_shot_no": int(saved) + 1,
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
    for key in ("duration_s", "shot_size", "camera_move", "scene_setting", "characters",
                "action_desc", "first_frame_desc", "last_frame_desc", "source_excerpt", "narration", "dialogues", "transition", "continuity_from_prev",
                "story_event_id", "purpose", "new_information_ids", "reinforcement_info_ids",
                "state_in", "primary_action", "emotion_beat", "state_out", "observed_state_out",
                "continuity_mode", "characters_visible", "audio_cast", "audio_timeline",
                "required_text", "reference_roles", "do_not_repeat", "risk_tags",
                "prompt_contract_version", "legacy_unvalidated", "camera_angle",
                "spatial_anchor", "is_final"):
        if key in body:
            merged[key] = body[key]
    # 时长 clamp 到产品侧合法区间；缺省/非法时回退默认时长。
    merged["duration_s"] = clip_duration_value(merged.get("duration_s"))
    instance, errors = schema_errors(Shot, {k: merged[k] for k in (
        "shot_no", "duration_s", "shot_size", "camera_move", "scene_setting", "characters",
        "action_desc", "first_frame_desc", "last_frame_desc", "source_excerpt", "narration", "dialogues", "transition", "continuity_from_prev",
        "story_event_id", "purpose", "new_information_ids", "reinforcement_info_ids",
        "state_in", "primary_action", "emotion_beat", "state_out", "observed_state_out",
        "continuity_mode", "characters_visible", "audio_cast", "audio_timeline",
        "required_text", "reference_roles", "do_not_repeat", "risk_tags",
        "prompt_contract_version", "legacy_unvalidated", "camera_angle",
        "spatial_anchor", "is_final")})
    if errors:
        raise HTTPException(422, "；".join(errors))
    instance.action_desc = normalize_action_desc(instance.action_desc)
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
        scope_id=f"{shot['episode_id']}:{shot['shot_no']}",
        status="validated",
        trust_level="T4",
        content=instance.model_dump(mode="json"),
        parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
        contract_version=get_contract("storyboard").version,
    ))
    manual_artifact = evidence_repository.commit_artifact(
        None,
        manual_artifact["id"],
        [Evaluation(
            evaluator_type="human",
            evaluator_name="storyboard_editor",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"decision": "manual_edit", "shot_id": shot_id},
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
    conn.execute("UPDATE episodes SET status='scripted' WHERE id=?", (shot["episode_id"],))
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


def _storyboard_residual_hint(residual: list[str]) -> str:
    """Return an actionable repair hint for the current validation failures."""
    text = "；".join(residual)
    hints: list[str] = []
    if "口播上限" in text or "念不完" in text:
        hints.append("请拆成相邻镜头分担台词，或精简人群议论旁白")
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
