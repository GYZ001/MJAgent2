from __future__ import annotations

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *

try:
    _board_from_shot_rows
except NameError:  # pragma: no cover - direct module import
    from app.domain.storyboard_ops import _board_from_shot_rows

def confirm_episode_core(episode_id: str) -> dict:
    """人工确认门（PRD P3）的纯逻辑：全量业务校验通过才进入 confirmed。
    失败抛 ValueError（消息面向 UI）；供路由与一键全自动复用，避免逻辑分叉。"""
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
    # 确认门同样跑确定性连贯归一，并把修正后的 continuity/transition 写回库，
    # 保证人工编辑过的分镜在进入生成前也满足"同场景接上镜/换场明确转场"的铁律。
    before = [(s.continuity_from_prev, s.transition, s.duration_s, s.shot_size, s.camera_move) for s in shots]
    normalize_continuity(board)
    # 确认门保留模型/人工选择的时长，由全量校验验证 5~10s 合同与对应口播预算。
    normalize_transition_visuals(board)
    normalized_fields_changed = False
    for r, s, (old_cont, old_trans, old_dur, old_size, old_move) in zip(shots_rows, shots, before):
        if (old_cont != s.continuity_from_prev or old_trans != s.transition or old_dur != s.duration_s
                or old_size != s.shot_size or old_move != s.camera_move
                or (r["last_frame_desc"] or "") != s.last_frame_desc):
            normalized_fields_changed = True
            conn.execute(
                "UPDATE shots SET continuity_from_prev=?, transition=?, duration_s=?, shot_size=?, camera_move=?, last_frame_desc=? WHERE id=?",
                (int(s.continuity_from_prev), s.transition, s.duration_s, s.shot_size, s.camera_move,
                 s.last_frame_desc, r["id"]))
    conn.commit()
    errors = validate_storyboard(board, bible, compact_target)
    screenplay = _load_screenplay(ep)
    if screenplay is not None:
        errors.extend(validate_storyboard_soundtrack(board, screenplay, compact_target))
        errors.extend(validate_storyboard_preserves_key_content(board, screenplay))
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    # 预编译全部 prompt，把参数错误拦在花钱之前
    if has_real_bible:
        try:
            for s in shots:
                compile_prompt(s, bible)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Prompt 编译失败：{exc}")
    est = sum(shot_cost_cny(s.duration_s) for s in shots)
    storyboard_artifact_id = ep["storyboard_artifact_id"]
    if character_artifact_ids or normalized_fields_changed or not storyboard_artifact_id:
        # Any deterministic rewrite changes the adopted content hash. Rebuild the full-board
        # T2 artifact from current checkpoint parents before attaching the human T4 decision.
        storyboard_artifact_id = _finalize_storyboard_evidence(episode_id, board)
    if storyboard_artifact_id:
        human_eval = Evaluation(
            evaluator_type="human",
            evaluator_name="storyboard_reviewer",
            evaluator_version="1.0.0",
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={"decision": "approve", "shot_count": len(shots)},
        )
        evidence_repository.commit_artifact(None, storyboard_artifact_id, [human_eval])
        conn.execute(
            """INSERT INTO gate_decisions(
                   id, artifact_id, gate_key, decision, decided_by, reason, created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                new_id("gate"), storyboard_artifact_id, "storyboard", "approve", "user",
                "分镜全量确定性校验通过并人工确认", now(),
            ),
        )
    conn.execute("UPDATE episodes SET status='confirmed' WHERE id=?", (episode_id,))
    conn.commit()
    return {
        "confirmed": True,
        "estimated_cost_cny": round(est, 2),
        "shot_count": len(shots),
        "total_duration_s": sum(s.duration_s for s in shots),
        "target_duration_s": compact_target,
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
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        "SELECT id, shot_no, continuity_from_prev FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,)).fetchall())
    from_no = (body or {}).get("from_shot_no")
    if from_no:
        selected = []
        for i, s in enumerate(shots):
            if s["shot_no"] == from_no:
                selected = [s]
                for nxt in shots[i + 1:]:
                    if nxt["continuity_from_prev"]:
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
        await _ensure_shot_mode_plan(conn, s["id"])
    results = []
    for s in selected:
        after = None
        if s["continuity_from_prev"] and s["shot_no"] > 1:
            pr = _shot_by_no(episode_id, s["shot_no"] - 1)
            after = pr["id"] if pr else None
        try:
            r = worker.enqueue_shot(s["id"], after_shot_id=after)
            # 幂等命中（已有相同成片）：若当前无采用版，把复用版标为采用
            if r.get("reused") and r.get("version_id"):
                row = conn.execute(
                    "SELECT adopted_version_id FROM shots WHERE id=?", (s["id"],)
                ).fetchone()
                if not row or not row["adopted_version_id"]:
                    conn.execute(
                        "UPDATE shots SET adopted_version_id=? WHERE id=?",
                        (r["version_id"], s["id"]),
                    )
            results.append({"shot_id": s["id"], **r})
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(exc, action="enqueue_shot",
                                              context={"shot_id": s["id"], "episode_id": episode_id})
            results.append({"shot_id": s["id"], "error": public})
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
    if shot_row["continuity_from_prev"] and shot_row["shot_no"] > 1:
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
