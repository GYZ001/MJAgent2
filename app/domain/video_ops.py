from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

try:
    _board_from_shot_rows
except NameError:  # pragma: no cover - direct module import
    from app.domain.storyboard_ops import _board_from_shot_rows

def _shot_contract_json(shot: Shot) -> str:
    from app.continuity import shot_contract_dict
    return json.dumps(shot_contract_dict(shot), ensure_ascii=False)


def _uses_previous_tail_frame_for_model(shot: Shot, prev: Shot | None = None) -> bool:
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame
    return uses_previous_tail_frame(derive_continuity_mode(shot, prev))


class ConfirmationEvaluation:
    """只读确认评估结果；不写数据库。"""

    __slots__ = ("passed", "errors", "issues", "board", "compact_target", "estimated_cost_cny")

    def __init__(
        self,
        *,
        passed: bool,
        errors: list[str],
        issues: list,
        board: Storyboard,
        compact_target: int,
        estimated_cost_cny: float,
    ):
        self.passed = passed
        self.errors = errors
        self.issues = issues
        self.board = board
        self.compact_target = compact_target
        self.estimated_cost_cny = estimated_cost_cny


def evaluate_storyboard_for_confirmation(
    episode,
    storyboard: Storyboard,
    screenplay: EpisodeScreenplay | None,
    bible: Bible,
    *,
    has_real_bible: bool = True,
    target_duration_s: int | None = None,
) -> ConfirmationEvaluation:
    """与 confirm_episode_core 同源的只读确认评估（不写库）。

    Supervisor 与确认门必须共用此函数，避免「Supervisor 认为通过、确认门又用另一套规则失败」。
    """
    from app.evaluations.issues import issues_from_messages
    from app.validators import prefer_default_shot_durations

    board = Storyboard(episode_no=storyboard.episode_no, shots=list(storyboard.shots))
    normalize_offbible_characters(board, bible)
    normalize_continuity(board)
    prefer_default_shot_durations(board)
    normalize_transition_visuals(board)
    compact_target = _compact_episode_target(
        target_duration_s if target_duration_s is not None else episode["target_duration_s"]
    )
    actual_total = sum(int(s.duration_s or 0) for s in board.shots)
    compact_target = _compact_episode_target(actual_total or compact_target)

    errors = validate_storyboard(board, bible, compact_target)
    if screenplay is not None:
        errors.extend(validate_storyboard_soundtrack(board, screenplay, compact_target))
        errors.extend(validate_storyboard_preserves_key_content(board, screenplay))
    if has_real_bible and not errors:
        try:
            for s in board.shots:
                compile_prompt(s, bible)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Prompt 编译失败：{exc}")
    try:
        ep_id = episode["id"]
    except Exception:  # noqa: BLE001
        ep_id = getattr(episode, "id", "") or ""
    # VAL-422 可观测性：确认门才首次发现的容量/口播冲突（理想应为 0）。
    try:
        from app.observability.metrics import inc
        for err in errors:
            if "口播上限" in err or "台词纯文字" in err:
                inc("confirm_first_seen_capacity_error_total", episode_id=ep_id)
            if "分叉" in err or "SPOKEN_CONTRACT" in err or "口播合同" in err:
                inc("confirm_first_seen_spoken_conflict_total", episode_id=ep_id)
    except Exception:  # noqa: BLE001
        pass
    issues = issues_from_messages(errors, subject=f"episode:{ep_id}")
    est = sum(shot_cost_cny(s.duration_s) for s in board.shots)
    return ConfirmationEvaluation(
        passed=not errors,
        errors=errors,
        issues=issues,
        board=board,
        compact_target=compact_target,
        estimated_cost_cny=round(est, 2),
    )


def confirm_episode_core(episode_id: str, *, decided_by: str = "user", reason: str | None = None) -> dict:
    """人工/自动确认门：全量业务校验通过才进入 confirmed。
    失败抛 ValueError（消息面向 UI）；供路由与 Supervisor 复用。
    """
    ep = _episode_or_404(episode_id)
    conn = get_conn()
    compact_target = _compact_episode_target(ep["target_duration_s"])
    if compact_target != ep["target_duration_s"]:
        conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
        conn.commit()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    has_real_bible = bool((p["bible_json"] or "").strip())
    bible = _project_bible_or_placeholder(p)
    shots_rows = conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall()
    if not shots_rows:
        raise ValueError("本集还没有分镜脚本")
    board = _board_from_shot_rows(shots_rows, ep["episode_no"])
    shots = board.shots
    character_changes = normalize_offbible_characters(board, bible)
    character_artifact_ids = _persist_storyboard_character_policy_repairs(
        conn, episode_id, board, character_changes
    )
    before = [
        (
            s.continuity_from_prev, s.transition, s.duration_s, s.shot_size, s.camera_move,
            s.continuity_mode, s.observed_state_out,
            (r["shot_contract_json"] if "shot_contract_json" in r.keys() else "") or "",
        )
        for r, s in zip(shots_rows, shots)
    ]
    normalize_continuity(board)
    from app.validators import prefer_default_shot_durations
    prefer_default_shot_durations(board)
    normalize_transition_visuals(board)
    actual_total = sum(int(s.duration_s or 0) for s in shots)
    synced_target = _compact_episode_target(actual_total or compact_target)
    if synced_target != compact_target:
        compact_target = synced_target
        conn.execute("UPDATE episodes SET target_duration_s=? WHERE id=?", (compact_target, episode_id))
    normalized_fields_changed = False
    for r, s, (old_cont, old_trans, old_dur, old_size, old_move, old_mode, old_observed, old_contract) in zip(shots_rows, shots, before):
        if (old_cont != s.continuity_from_prev or old_trans != s.transition or old_dur != s.duration_s
                or old_size != s.shot_size or old_move != s.camera_move
                or old_mode != s.continuity_mode or old_observed != s.observed_state_out
                or old_contract != _shot_contract_json(s)
                or (r["last_frame_desc"] or "") != s.last_frame_desc):
            normalized_fields_changed = True
            conn.execute(
                "UPDATE shots SET continuity_from_prev=?, transition=?, duration_s=?, shot_size=?, camera_move=?, last_frame_desc=?, shot_contract_json=?, continuity_mode=?, observed_state_out=? WHERE id=?",
                (int(s.continuity_from_prev), s.transition, s.duration_s, s.shot_size, s.camera_move,
                 s.last_frame_desc, _shot_contract_json(s), s.continuity_mode, s.observed_state_out, r["id"]))
    conn.commit()

    screenplay = _load_screenplay(ep)
    # 重新从当前（可能已归一）镜头构建 board，再跑同源只读评估。
    board = _board_from_shot_rows(
        conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall(),
        ep["episode_no"],
    )
    evaluation = evaluate_storyboard_for_confirmation(
        ep, board, screenplay, bible,
        has_real_bible=has_real_bible,
        target_duration_s=compact_target,
    )
    if not evaluation.passed:
        raise ValueError(json.dumps(evaluation.errors, ensure_ascii=False))
    board = evaluation.board
    compact_target = evaluation.compact_target
    est = evaluation.estimated_cost_cny
    shots = board.shots

    # 幂等：已 confirmed 且 artifact hash 相同 → 直接成功；hash 不同则拒绝覆盖。
    storyboard_artifact_id = ep["storyboard_artifact_id"]
    content_hash = None
    if character_artifact_ids or normalized_fields_changed or not storyboard_artifact_id:
        storyboard_artifact_id = _finalize_storyboard_evidence(episode_id, board)
    if storyboard_artifact_id:
        art = conn.execute(
            "SELECT content_hash FROM artifacts WHERE id=?", (storyboard_artifact_id,)
        ).fetchone()
        content_hash = art["content_hash"] if art else None

    if ep["status"] == "confirmed":
        if storyboard_artifact_id and content_hash:
            existing_gate = conn.execute(
                "SELECT id FROM gate_decisions WHERE artifact_id=? AND gate_key='storyboard' AND decision='approve'",
                (storyboard_artifact_id,),
            ).fetchone()
            if existing_gate:
                return {
                    "confirmed": True,
                    "idempotent": True,
                    "estimated_cost_cny": est,
                    "shot_count": len(shots),
                    "total_duration_s": sum(s.duration_s for s in shots),
                    "target_duration_s": compact_target,
                }
        raise ValueError(
            "本集已确认但分镜内容已变化；禁止覆盖已确认分镜，请先撤销确认或新建修订"
        )

    idempotency_key = f"{episode_id}:{content_hash or storyboard_artifact_id or 'none'}"
    existing = conn.execute(
        "SELECT id FROM gate_decisions WHERE artifact_id=? AND gate_key='storyboard' AND decision='approve'",
        (storyboard_artifact_id,),
    ).fetchone() if storyboard_artifact_id else None
    if storyboard_artifact_id and not existing:
        human_eval = Evaluation(
            evaluator_type="human",
            evaluator_name="storyboard_reviewer",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={
                "decision": "approve",
                "shot_count": len(shots),
                "decided_by": decided_by,
                "idempotency_key": idempotency_key,
            },
        )
        evidence_repository.commit_artifact(None, storyboard_artifact_id, [human_eval])
        conn.execute(
            """INSERT INTO gate_decisions(
                   id, artifact_id, gate_key, decision, decided_by, reason, created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                new_id("gate"), storyboard_artifact_id, "storyboard", "approve", decided_by,
                reason or "分镜全量确定性校验通过并确认", now(),
            ),
        )
    conn.execute("UPDATE episodes SET status='confirmed' WHERE id=?", (episode_id,))
    conn.commit()
    return {
        "confirmed": True,
        "estimated_cost_cny": est,
        "shot_count": len(shots),
        "total_duration_s": sum(s.duration_s for s in shots),
        "target_duration_s": compact_target,
        "idempotency_key": idempotency_key,
    }


@router.post("/episodes/{episode_id}/confirm")
async def confirm_episode(episode_id: str):
    """运行确定性确认门；校验通过后把剧集推进到 confirmed。"""
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("storyboard.confirm", {"episode_id": episode_id}, initiator="ui")
    return respond_ui(result)


@router.post("/episodes/{episode_id}/clear-artifacts")
async def clear_episode_artifacts(episode_id: str):
    """清空整集所有镜头的参考图、视频与模型分析，并回退到「已确认」。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    return worker.clear_episode_artifacts(episode_id)


@router.post("/shots/{shot_id}/clear-artifacts")
async def clear_shot_artifacts(shot_id: str):
    """清空单个镜头的参考图、视频与模型分析。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.clear_shot", {"shot_id": shot_id})
    if routed is not None:
        return routed
    conn = get_conn()
    if not conn.execute("SELECT id FROM shots WHERE id=?", (shot_id,)).fetchone():
        raise HTTPException(404, "镜头不存在")
    return worker.clear_shot_artifacts(shot_id)


@router.delete("/versions/{version_id}")
async def delete_version(version_id: str):
    """删除一个已生成的视频版本（含文件）。若是采用版则清空采用、使本集成品失效。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.delete_version", {"version_id": version_id})
    if routed is not None:
        return routed
    conn = get_conn()
    v = conn.execute("SELECT id FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    shot_id = worker.delete_video_version(version_id)
    return {"deleted": version_id, "shot_id": shot_id}


def _set_reference_image_used(
    version_id: str, ref_id: str, *, use: bool, override_reason: str | None = None,
) -> dict:
    """素材画廊里把某张参考图标记为「废弃」或「恢复使用」。
    废弃后该图不再喂给视频模型（见 video_modes.build_seedance_image_inputs），仅留作展示。"""
    conn = get_conn()
    v = conn.execute("SELECT image_inputs FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "视频版本不存在")
    meta = json.loads(v["image_inputs"] or "{}")
    refs = meta.get("reference_images") or []
    target = next((r for r in refs if r.get("id") == ref_id), None)
    if target is None:
        raise HTTPException(404, "参考图不存在")
    if use and target.get("rejectReason") and not (override_reason or "").strip():
        raise HTTPException(400, "恢复质检淘汰的参考图必须填写覆盖理由")
    changed = target.get("deleted") != (not use) or target.get("selectedForSeedance") != use
    target["deleted"] = not use
    target["selectedForSeedance"] = use
    if use and (override_reason or "").strip():
        target["restoreOverrideReason"] = override_reason.strip()
        target["restoredAt"] = now()
        changed = True
    meta["reference_images"] = refs
    if changed:
        meta["reference_gallery_revision"] = now()
        meta["reference_gallery_edited"] = True
    conn.execute("UPDATE shot_versions SET image_inputs=? WHERE id=?",
                 (json.dumps(meta, ensure_ascii=False), version_id))
    conn.commit()
    return {
        "version_id": version_id,
        "ref_id": ref_id,
        "deleted": not use,
        "override_reason": (override_reason or "").strip() or None,
    }


@router.delete("/versions/{version_id}/reference-images/{ref_id}")
async def discard_reference_image(version_id: str, ref_id: str):
    """废弃一张参考图：移入废弃画廊，且后续调用视频模型时不再使用它。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "reference.review",
        {"version_id": version_id, "ref_id": ref_id, "action": "discard"},
    )
    if routed is not None:
        return routed
    return _set_reference_image_used(version_id, ref_id, use=False)


@router.post("/versions/{version_id}/reference-images/{ref_id}/restore")
async def restore_reference_image(version_id: str, ref_id: str, body: dict | None = Body(None)):
    """把废弃画廊里的参考图恢复为可用（重新计入喂给视频模型的参考图）。
    若该图曾被 QA 淘汰，body.override_reason 必填，写入审计字段。"""
    from app.capabilities.dispatch import ui_route
    body = body or {}
    routed = await ui_route(
        "reference.review",
        {
            "version_id": version_id,
            "ref_id": ref_id,
            "action": "restore",
            "override_reason": body.get("override_reason"),
        },
    )
    if routed is not None:
        return routed
    return _set_reference_image_used(
        version_id, ref_id, use=True, override_reason=body.get("override_reason"),
    )


# ----- 视频生成（固定参考图模式） -----

def _shot_by_no(episode_id: str, shot_no: int):
    return get_conn().execute(
        "SELECT id FROM shots WHERE episode_id=? AND shot_no=?", (episode_id, shot_no)).fetchone()


@router.post("/episodes/{episode_id}/generate")
async def generate_episode(episode_id: str, body: dict | None = None):
    """批量生成整集视频（固定参考图模式）：每个视频任务内部生成/复用参考图并提交 Seedance。
    body.from_shot_no：只从该镜起、沿其连续段往后重生（中途改动后用）。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.generate_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise HTTPException(409, "分镜脚本未确认（先在工作台点击确认分镜）")
    # Supervisor 运行期间拒绝快速模式，避免重复付费
    try:
        mode = ep["video_completion_mode"]
    except (KeyError, IndexError, TypeError):
        mode = None
    if mode == "complete" and task_registry.active("video_completion", episode_id):
        raise HTTPException(409, "全片补齐 Supervisor 运行中，请使用补齐模式或等待完成")
    conn = get_conn()
    shots_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,)).fetchall()
    board = _board_from_shot_rows(shots_rows, ep["episode_no"])
    shots = [
        {"row": dict(row), "shot": board.shots[idx], "prev": board.shots[idx - 1] if idx > 0 else None}
        for idx, row in enumerate(shots_rows)
    ]
    from_no = (body or {}).get("from_shot_no")
    if from_no is not None:
        try:
            from_no = int(from_no)
        except (TypeError, ValueError):
            pass
    if from_no:
        selected = []
        for i, s in enumerate(shots):
            if s["row"]["shot_no"] == from_no:
                selected = [s]
                for nxt in shots[i + 1:]:
                    if _uses_previous_tail_frame_for_model(nxt["shot"], nxt["prev"]):
                        selected.append(nxt)
                    else:
                        break
                break
        if not selected:
            raise HTTPException(404, f"未找到镜 {from_no}")
    else:
        selected = shots
    # 不再预先清空 adopted_version_id：新版本成功并通过技术门禁后由
    # select_best_video_candidate 比较切换；任务失败时保留原可交付采用结果。
    # 固定参考图模式：批量生成前确保每个选中镜都有固定参考图计划。
    for s in selected:
        await _ensure_shot_mode_plan(conn, s["row"]["id"])
    results = []
    for s in selected:
        after = None
        if _uses_previous_tail_frame_for_model(s["shot"], s["prev"]) and s["row"]["shot_no"] > 1:
            pr = _shot_by_no(episode_id, s["row"]["shot_no"] - 1)
            after = pr["id"] if pr else None
        try:
            r = worker.enqueue_shot(s["row"]["id"], after_shot_id=after)
            # 幂等命中（已有相同成片）：若当前无采用版，把复用版标为采用
            if r.get("reused") and r.get("version_id"):
                row = conn.execute(
                    "SELECT adopted_version_id FROM shots WHERE id=?", (s["row"]["id"],)
                ).fetchone()
                if not row or not row["adopted_version_id"]:
                    conn.execute(
                        "UPDATE shots SET adopted_version_id=? WHERE id=?",
                        (r["version_id"], s["row"]["id"]),
                    )
            results.append({"shot_id": s["row"]["id"], **r})
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(exc, action="enqueue_shot",
                                              context={"shot_id": s["row"]["id"], "episode_id": episode_id})
            issue_codes: list[str] = []
            try:
                from app.video_issues import issues_from_enqueue_error, persist_shot_issue
                issues = issues_from_enqueue_error(
                    exc, shot_id=s["row"]["id"], shot_no=s["row"]["shot_no"],
                )
                issue_codes = [i.code for i in issues]
                persist_shot_issue(
                    episode_id=episode_id,
                    shot_id=s["row"]["id"],
                    shot_no=s["row"]["shot_no"],
                    issues=issues,
                    source="generate_episode_enqueue",
                )
            except Exception:  # noqa: BLE001
                pass
            results.append({
                "shot_id": s["row"]["id"],
                "error": public,
                "issue_codes": issue_codes,
            })
    conn.commit()
    return {"enqueued": results}


async def _generate_shot_core(shot_id: str, body: dict) -> dict:
    """单镜生成视频的领域逻辑，供 REST 路由与 ``video.generate_shot`` Command Handler 共用。"""
    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise HTTPException(404, "镜头不存在")
    # 带 AI 评语重生：取「当前采用版 / 最新成功版」的问题清单（必要时现场跑评审），
    # 作为本次必须改正项写入 prompt，避免模型再犯同样的错。
    critique = None
    if body.get("with_critique"):
        ref = None
        if shot_row["adopted_version_id"]:
            ref = conn.execute("SELECT id FROM shot_versions WHERE id=? AND status='succeeded'",
                               (shot_row["adopted_version_id"],)).fetchone()
        if not ref:
            ref = conn.execute(
                "SELECT id FROM shot_versions WHERE shot_id=? AND status='succeeded' ORDER BY version_no DESC LIMIT 1",
                (shot_id,)).fetchone()
        if ref:
            critique = await worker.critique_version(ref["id"])
    # 固定参考图模式：生成前确保已有固定参考图计划。
    await _ensure_shot_mode_plan(conn, shot_id)
    # 同场景接上镜时，参考图模式可复用上一镜可用素材作为参考。
    after = None
    prev_row = None
    prev_shot = None
    if shot_row["shot_no"] > 1:
        prev_row = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
            (shot_row["episode_id"], shot_row["shot_no"] - 1),
        ).fetchone()
    if prev_row:
        models = _board_from_shot_rows([prev_row, shot_row], 0).shots
        prev_shot, shot_model = models[0], models[1]
    else:
        shot_model = _board_from_shot_rows([shot_row], 0).shots[0]
    if _uses_previous_tail_frame_for_model(shot_model, prev_shot) and shot_row["shot_no"] > 1:
        pr = _shot_by_no(shot_row["episode_id"], shot_row["shot_no"] - 1)
        after = pr["id"] if pr else None
    try:
        return worker.enqueue_shot(
            shot_id,
            prompt_override=body.get("prompt_override"),
            extra_negative=body.get("extra_negative"),
            reroll=bool(body.get("reroll")) or bool(body.get("with_critique")),
            critique=critique, after_shot_id=after)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/shots/{shot_id}/generate")
async def generate_shot(shot_id: str, body: dict | None = None):
    from app.capabilities.dispatch import dispatch, respond_ui

    body = body or {}
    result = await dispatch(
        "video.generate_shot",
        {
            "shot_id": shot_id,
            "prompt_override": body.get("prompt_override"),
            "reroll": bool(body.get("reroll")),
            "critique": "with_critique" if body.get("with_critique") else None,
        },
        initiator="ui",
    )
    return respond_ui(result)


@router.post("/shots/{shot_id}/video/stop")
async def stop_shot_video(shot_id: str):
    """立即停止本镜全部排队中或运行中的视频任务；重复调用安全。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.stop_shot", {"shot_id": shot_id})
    if routed is not None:
        return routed
    try:
        return worker.stop_shot_video_tasks(shot_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _adopt_version_core(shot_id: str, body: dict) -> dict:
    """人工采用视频版本的领域逻辑，供 REST 路由与 ``video.adopt_version`` Command Handler 共用。"""
    version_id = body.get("version_id")
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=? AND shot_id=?", (version_id, shot_id)).fetchone()
    if not v or v["status"] != "succeeded":
        raise HTTPException(409, "该版本不存在或未成功")
    from app.evidence import media as media_evidence

    try:
        artifact = media_evidence.record_video_candidate(version_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"候选证据创建失败：{exc}") from exc
    technical = json.loads(v["technical_validation_json"] or "{}")
    if not technical:
        refreshed = conn.execute(
            "SELECT technical_validation_json FROM shot_versions WHERE id=?", (version_id,)
        ).fetchone()
        technical = json.loads(refreshed["technical_validation_json"] or "{}")
    if not technical.get("passed"):
        raise HTTPException(409, "视频技术门禁未通过，不能人工采用")
    qa = json.loads(v["qa_json"] or "{}")
    observed_state_out = qa.get("observed_state_out")
    if observed_state_out:
        media_evidence.merge_observed_state_out_into_shot_contract(shot_id, str(observed_state_out))
    reason = str(body.get("reason") or "人工横向比较后采用").strip()
    evidence_repository.commit_artifact(
        None,
        artifact["id"],
        [Evaluation(
            evaluator_type="human", evaluator_name=str(body.get("decided_by") or "user"),
            evaluator_version="1.0.0", status="passed", hard_gate_passed=True,
            score=100, evidence={"decision": "adopt", "reason": reason},
        )],
    )
    shot = conn.execute("SELECT episode_id, adopted_version_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.execute("UPDATE shot_versions SET adoption_reason=? WHERE id=?", (reason, version_id))
    conn.execute(
        """INSERT INTO gate_decisions(
               id, artifact_id, gate_key, decision, decided_by, reason, created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            new_id("gate"), artifact["id"], "video_adoption", "approve",
            str(body.get("decided_by") or "user"), reason, now(),
        ),
    )
    conn.commit()
    if shot and shot["adopted_version_id"] != version_id:
        worker.invalidate_episode_final(shot["episode_id"])
    return {"adopted": version_id, "artifact_id": artifact["id"], "reason": reason}


@router.post("/shots/{shot_id}/adopt")
async def adopt_version(shot_id: str, body: dict):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch(
        "video.adopt_version",
        {"shot_id": shot_id, "version_id": body.get("version_id"), "reason": body.get("reason")},
        initiator="ui",
    )
    return respond_ui(result)


@router.post("/episodes/{episode_id}/resume")
async def resume_episode(episode_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("video.resume_episode", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    return {"resumed_jobs": worker.retry_paused(episode_id)}


def _ensure_video_episode_columns(conn=None) -> None:
    db = conn or get_conn()
    for stmt in (
        "ALTER TABLE episodes ADD COLUMN active_video_run_id TEXT",
        "ALTER TABLE episodes ADD COLUMN video_completion_mode TEXT NOT NULL DEFAULT 'quick'",
        "ALTER TABLE episodes ADD COLUMN video_control_json TEXT",
    ):
        try:
            db.execute(stmt)
            db.commit()
        except Exception:  # noqa: BLE001
            pass


async def _recorded_video_completion_task(
    episode_id: str,
    recorder,
    *,
    resume: bool,
    grant_id: str | None,
    budget_cap_cny: float | None = None,
    wall_clock_cap_s: float | None = None,
    allow_fallback_adopt: bool = True,
    max_fallback_shots: int | None = None,
    allow_storyboard_edit: bool = False,
):
    import asyncio
    from app.video_supervisor import run_video_completion_supervisor
    recorder.start()
    try:
        result = await run_video_completion_supervisor(
            episode_id,
            resume=resume,
            grant_id=grant_id,
            run_id=recorder.run_id,
            budget_cap_cny=budget_cap_cny,
            wall_clock_cap_s=wall_clock_cap_s,
            allow_fallback_adopt=allow_fallback_adopt,
            max_fallback_shots=max_fallback_shots,
            allow_storyboard_edit=allow_storyboard_edit,
        )
        if result.phase == "SUCCEEDED_COVERED":
            recorder.succeed(result.outcome or "SUCCEEDED_COVERED")
        elif result.phase == "CANCELLED":
            recorder.cancel()
        else:
            recorder.partial(result.outcome or result.phase)
        return result
    except asyncio.CancelledError:
        recorder.cancel()
        raise
    except Exception as exc:
        recorder.fail(exc)
        raise


@router.post("/episodes/{episode_id}/video-completion")
async def complete_episode(episode_id: str, body: dict | None = None):
    """启动集级视频补齐 Supervisor（补齐到全片可用）。"""
    from app.capabilities.dispatch import ui_route
    payload = {"episode_id": episode_id, **(body or {})}
    routed = await ui_route("video.complete_episode", payload)
    if routed is not None:
        return routed
    return await _complete_episode_core(episode_id, body or {})


async def _complete_episode_core(episode_id: str, body: dict) -> dict:
    from app.completion_grant import (
        DEFAULT_VIDEO_BUDGET_CAP_CNY,
        DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
        default_max_fallback_shots,
        issue_video_completion_grant,
        bump_video_grant_budget,
        get_video_grant,
    )
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.video_supervisor import (
        FIRST_PASS_BUDGET_FRACTION,
        MAX_ATTEMPTS_PER_SHOT,
        MAX_CHAIN_CASCADE_DEPTH,
        MAX_REPAIR_EPOCHS,
        MIN_ATTEMPTS_PER_SHOT,
    )

    ep = _episode_or_404(episode_id)
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise HTTPException(409, "分镜脚本未确认（先在工作台点击确认分镜）")
    _ensure_video_episode_columns()
    mode = body.get("mode") or "fresh"
    if mode not in {"fresh", "resume"}:
        raise HTTPException(422, "mode 只能是 fresh 或 resume")

    if task_registry.active("video_completion", episode_id):
        raise HTTPException(409, "全片补齐 Supervisor 已在运行")

    conn = get_conn()
    shots_total = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"]
    if int(shots_total or 0) <= 0:
        raise HTTPException(409, "本集尚无分镜")

    budget_cap = body.get("budget_cap_cny")
    wall_cap = body.get("wall_clock_cap_s")
    allow_fallback = body.get("allow_fallback_adopt", True)
    max_fallback = body.get("max_fallback_shots")
    allow_edit = bool(body.get("allow_storyboard_edit", False))
    grant_id = body.get("completion_grant_id")

    # resume + 追加预算
    add_budget = body.get("add_budget_cny")
    add_wall = body.get("add_wall_clock_s")
    if mode == "resume" and grant_id and (add_budget or add_wall):
        bump_video_grant_budget(
            grant_id,
            add_cny=float(add_budget or 0),
            add_wall_s=float(add_wall or 0),
        )

    if mode == "fresh" or not grant_id:
        grant, _token = issue_video_completion_grant(
            episode_id=episode_id,
            project_id=ep["project_id"],
            storyboard_artifact_id=ep["storyboard_artifact_id"] or "",
            budget_cap_cny=float(budget_cap) if budget_cap is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY,
            wall_clock_cap_s=float(wall_cap) if wall_cap is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
            allow_fallback_adopt=bool(allow_fallback),
            max_fallback_shots=(
                int(max_fallback) if max_fallback is not None
                else default_max_fallback_shots(int(shots_total))
            ),
            allow_storyboard_edit=allow_edit,
            shots_total=int(shots_total),
            impact_snapshot={
                "mode": "complete_episode_video",
                "auto_concatenate": False,
                "auto_delivery": False,
            },
        )
        grant_id = grant.grant_id
        budget_cap = grant.budget_cap_cny
        wall_cap = grant.wall_clock_cap_s
        max_fallback = grant.max_fallback_shots
    else:
        existing = get_video_grant(grant_id)
        if existing:
            budget_cap = existing.budget_cap_cny
            wall_cap = existing.wall_clock_cap_s
            max_fallback = existing.max_fallback_shots
            allow_fallback = existing.allow_fallback_adopt
            allow_edit = existing.allow_storyboard_edit

    conn.execute(
        "UPDATE episodes SET video_completion_mode='complete', active_video_run_id=NULL WHERE id=?",
        (episode_id,),
    )
    conn.commit()

    cap = float(budget_cap if budget_cap is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY)
    recorder = WorkflowRecorder.create(
        workflow_type="episode_video_completion",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=fingerprint(
            ep["storyboard_artifact_id"], grant_id, mode,
        ),
        requested_by="user",
        trigger_type="manual",
        budget_limit_cny=cap,
        policy_snapshot={
            "supervisor": "video_completion",
            "budget_cap_cny": cap,
            "wall_clock_cap_s": float(wall_cap if wall_cap is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S),
            "first_pass_budget_fraction": FIRST_PASS_BUDGET_FRACTION,
            "min_attempts_per_shot": MIN_ATTEMPTS_PER_SHOT,
            "max_attempts_per_shot": MAX_ATTEMPTS_PER_SHOT,
            "max_repair_epochs": MAX_REPAIR_EPOCHS,
            "max_chain_cascade_depth": MAX_CHAIN_CASCADE_DEPTH,
            "allow_fallback_adopt": bool(allow_fallback),
            "max_fallback_shots": int(max_fallback or 0),
            "allow_storyboard_edit": allow_edit,
        },
    )
    conn.execute(
        "UPDATE episodes SET active_video_run_id=? WHERE id=?",
        (recorder.run_id, episode_id),
    )
    conn.commit()

    task_registry.spawn(
        "video_completion", episode_id,
        _recorded_video_completion_task(
            episode_id, recorder,
            resume=(mode == "resume"),
            grant_id=grant_id,
            budget_cap_cny=cap,
            wall_clock_cap_s=float(wall_cap) if wall_cap is not None else None,
            allow_fallback_adopt=bool(allow_fallback),
            max_fallback_shots=int(max_fallback) if max_fallback is not None else None,
            allow_storyboard_edit=allow_edit,
        ),
        project_id=ep["project_id"],
    )
    return {
        "status": "accepted",
        "run_id": recorder.run_id,
        "goal": "complete_episode_video",
        "completion_grant_id": grant_id,
        "resource_uri": f"manju://runs/{recorder.run_id}",
    }


@router.get("/episodes/{episode_id}/video-completion")
def get_video_completion(episode_id: str):
    """只读：最新 checkpoint 公开投影 + 覆盖台账。"""
    _episode_or_404(episode_id)
    _ensure_video_episode_columns()
    from app.video_supervisor import (
        load_latest_checkpoint,
        public_checkpoint_projection,
        rebuild_coverage_ledger,
    )
    from app.video_cost_model import predict_episode_completion_cost
    cp = load_latest_checkpoint(episode_id)
    ledger = rebuild_coverage_ledger(episode_id, cp=cp)
    proj = public_checkpoint_projection(cp) or {}
    proj["ledger"] = {
        "shots_total": ledger.shots_total,
        "grades": ledger.grades,
        "coverage_rate": ledger.coverage_rate,
        "fallback_quota": ledger.fallback_quota,
        "cost_spent": ledger.cost_spent,
        "entries": [e.model_dump(mode="json") for e in ledger.entries],
    }
    try:
        uncovered_ids = [e.shot_id for e in ledger.entries if e.grade == "C"]
        proj["cost_forecast"] = predict_episode_completion_cost(
            episode_id, uncovered_shot_ids=uncovered_ids,
        )
    except Exception:  # noqa: BLE001
        proj["cost_forecast"] = None
    conn = get_conn()
    ep = conn.execute(
        "SELECT active_video_run_id, video_completion_mode FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    try:
        proj["active_video_run_id"] = ep["active_video_run_id"] if ep else None
        proj["video_completion_mode"] = ep["video_completion_mode"] if ep else "quick"
    except (KeyError, IndexError, TypeError):
        proj["active_video_run_id"] = None
        proj["video_completion_mode"] = "quick"
    proj["running"] = task_registry.active("video_completion", episode_id)
    return proj


@router.post("/projects/{project_id}/video-completion")
async def complete_project_videos(project_id: str, body: dict | None = None):
    """跨集批量补齐：在全局预算内按集顺序启动 Supervisor。"""
    from app.capabilities.dispatch import ui_route
    payload = {"project_id": project_id, **(body or {})}
    routed = await ui_route("video.complete_project", payload)
    if routed is not None:
        return routed
    return await _complete_project_videos_core(project_id, body or {})


async def _complete_project_videos_core(project_id: str, body: dict) -> dict:
    """全局预算编排：按 episode_no 顺序分配 per-episode cap，串行启动未覆盖集。"""
    import asyncio
    conn = get_conn()
    project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, "项目不存在")

    global_cap = float(body.get("global_budget_cap_cny") or 500)
    per_cap = float(body.get("per_episode_cap_cny") or 150)
    wall_cap = float(body.get("wall_clock_cap_s") or 4 * 3600)
    allow_fallback = bool(body.get("allow_fallback_adopt", True))
    allow_edit = bool(body.get("allow_storyboard_edit", False))
    episode_ids = body.get("episode_ids")

    rows = conn.execute(
        """SELECT id, episode_no, status, storyboard_artifact_id FROM episodes
           WHERE project_id=? ORDER BY episode_no""",
        (project_id,),
    ).fetchall()
    if episode_ids:
        wanted = set(episode_ids)
        rows = [r for r in rows if r["id"] in wanted]
    eligible = [
        r for r in rows
        if r["status"] in {"confirmed", "generating", "done"}
    ]
    if not eligible:
        raise HTTPException(409, "没有可补齐的已确认剧集")

    spent_row = conn.execute(
        """SELECT COALESCE(SUM(v.cost_cny),0) AS c
           FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE e.project_id=? AND v.status='succeeded'""",
        (project_id,),
    ).fetchone()
    project_spent = float(spent_row["c"] if spent_row else 0)
    remaining_global = max(0.0, global_cap - project_spent)

    plan = []
    allocated = 0.0
    from app.video_supervisor import rebuild_coverage_ledger
    for r in eligible:
        if task_registry.active("video_completion", r["id"]):
            plan.append({
                "episode_id": r["id"], "episode_no": r["episode_no"],
                "status": "already_running", "allocated_cny": 0,
            })
            continue
        try:
            ledger = rebuild_coverage_ledger(r["id"])
            if ledger.covered_within_quota():
                plan.append({
                    "episode_id": r["id"], "episode_no": r["episode_no"],
                    "status": "already_covered", "allocated_cny": 0,
                })
                continue
        except Exception:  # noqa: BLE001
            pass
        room = remaining_global - allocated
        if room < 5:
            plan.append({
                "episode_id": r["id"], "episode_no": r["episode_no"],
                "status": "skipped_budget", "allocated_cny": 0,
            })
            continue
        ep_cap = min(per_cap, room)
        plan.append({
            "episode_id": r["id"], "episode_no": r["episode_no"],
            "status": "queued", "allocated_cny": ep_cap,
        })
        allocated += ep_cap

    started = []
    queue = [p for p in plan if p["status"] == "queued"]

    async def _run_one(item: dict) -> dict:
        room_now = max(0.0, global_cap - _project_video_spent(project_id))
        if room_now < 5:
            item["status"] = "skipped_budget"
            return item
        item["allocated_cny"] = min(float(item["allocated_cny"]), room_now)
        result = await _complete_episode_core(item["episode_id"], {
            "mode": "fresh",
            "budget_cap_cny": item["allocated_cny"],
            "wall_clock_cap_s": wall_cap,
            "allow_fallback_adopt": allow_fallback,
            "allow_storyboard_edit": allow_edit,
        })
        item["status"] = "started"
        item["run_id"] = result.get("run_id")
        item["completion_grant_id"] = result.get("completion_grant_id")
        return item

    if queue:
        first = await _run_one(queue[0])
        started.append(first)
        rest = queue[1:]
        if rest:
            async def _chain(items=rest):
                for item in items:
                    # 等待项目内任意集级 supervisor 空闲
                    while any(
                        task_registry.active("video_completion", p["episode_id"])
                        for p in plan
                        if p.get("episode_id") and p.get("status") in {"queued", "started", "already_running"}
                    ):
                        await asyncio.sleep(5)
                    try:
                        await _run_one(item)
                    except Exception as exc:  # noqa: BLE001
                        item["status"] = "failed"
                        item["error"] = str(exc)[:200]
                    while task_registry.active("video_completion", item["episode_id"]):
                        await asyncio.sleep(8)

            task_registry.spawn(
                "video_completion_project", project_id, _chain(), project_id=project_id,
            )

    return {
        "status": "accepted",
        "project_id": project_id,
        "global_budget_cap_cny": global_cap,
        "project_spent_cny": project_spent,
        "remaining_cny": remaining_global,
        "plan": plan,
        "started": started,
    }


def _project_video_spent(project_id: str) -> float:
    row = get_conn().execute(
        """SELECT COALESCE(SUM(v.cost_cny),0) AS c
           FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE e.project_id=? AND v.status='succeeded'""",
        (project_id,),
    ).fetchone()
    return float(row["c"] if row else 0)


# ---------- 成片台：预览 / 拼接 / 导出 ----------

@router.get("/episodes/{episode_id}/mix-status")
def mix_status(episode_id: str):
    """按镜号顺序返回每镜成片 URL、整体进度、已合成成品（若有）。"""
    _episode_or_404(episode_id)
    return worker.episode_mix_status(episode_id)


@router.post("/episodes/{episode_id}/concatenate")
async def concatenate(episode_id: str):
    """把本集所有已采用的视频片段按镜号顺序拼接成一个 MP4。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("delivery.concatenate", {"episode_id": episode_id})
    if routed is not None:
        return routed
    _episode_or_404(episode_id)
    try:
        return worker.concatenate_episode(episode_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"ffmpeg 合成失败：{exc}")

__all__ = [name for name in globals() if not name.startswith("__")]
