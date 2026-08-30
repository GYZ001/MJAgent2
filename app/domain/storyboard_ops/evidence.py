"""分镜镜头证据的当前性核验、发布证据状态判定与终态收口，以及软缺口续跑判据、计划自更新。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 scene_projection。
"""
from __future__ import annotations

import json

from app import config
from app.db import (
    get_conn,
    log_provider_call,
)
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import (
    Evaluation,
    EvidenceArtifact,
    Issue,
    IssueSeverity,
)
from app.schemas import (
    Shot,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
)

from .scene_projection import _sync_storyboard_scene_bindings


def _ensure_current_storyboard_shot_artifacts(
    conn,
    episode_id: str,
    board: Storyboard,
    *,
    commit: bool = True,
):
    """Bind every current shot to immutable evidence for its current number and content."""
    rows = conn.execute(
        "SELECT id,shot_no,source_excerpt,storyboard_artifact_id FROM shots "
        "WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if len(rows) != len(board.shots):
        raise RuntimeError(
            f"分镜证据对账失败：投影 {len(rows)} 镜，待发布内容 {len(board.shots)} 镜"
        )
    contract_version = get_contract("storyboard").version
    for row, shot in zip(rows, board.shots):
        if int(row["shot_no"]) != int(shot.shot_no):
            raise RuntimeError("分镜证据对账失败：镜头顺序与待发布内容不一致")
        content = shot.model_dump(mode="json")
        current_id = row["storyboard_artifact_id"]
        if _storyboard_shot_artifact_matches(
            conn, episode_id, shot, current_id,
        ):
            continue

        evaluation = Evaluation(
            evaluator_type="deterministic",
            evaluator_name="storyboard_projection_rebind",
            evaluator_version=contract_version,
            status="passed",
            hard_gate_passed=True,
            score=100,
            evidence={
                "episode_id": episode_id,
                "shot_no": int(shot.shot_no),
                "previous_artifact_id": current_id,
                "reason": "current shot projection and immutable evidence were realigned",
            },
        )
        artifact_input = EvidenceArtifact(
            type="storyboard_shot",
            scope_type="storyboard_checkpoint",
            scope_id=f"{episode_id}:{shot.shot_no}",
            status="candidate",
            trust_level="T1",
            content=content,
            parent_artifact_ids=[str(current_id)] if current_id else [],
            contract_version=contract_version,
        )
        if commit:
            artifact = evidence_repository.create_artifact(artifact_input)
            artifact = evidence_repository.commit_artifact(
                None, artifact["id"], [evaluation],
            )
        else:
            artifact = evidence_repository.create_and_commit_artifact_in_transaction(
                conn,
                artifact_input,
                [evaluation],
            )
        conn.execute(
            "UPDATE shots SET storyboard_artifact_id=? WHERE id=?",
            (artifact["id"], row["id"]),
        )
        if commit:
            conn.commit()

    return conn.execute(
        "SELECT id,shot_no,source_excerpt,storyboard_artifact_id FROM shots "
        "WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()

def _storyboard_shot_artifact_matches(
    conn,
    episode_id: str,
    shot: Shot,
    artifact_id: str | None,
) -> bool:
    if not artifact_id:
        return False
    current = conn.execute(
        """SELECT type,scope_type,scope_id,status,content_hash
             FROM artifacts WHERE id=?""",
        (artifact_id,),
    ).fetchone()
    return bool(
        current
        and current["type"] == "storyboard_shot"
        and current["scope_type"] == "storyboard_checkpoint"
        and current["scope_id"] == f"{episode_id}:{shot.shot_no}"
        and current["status"] == "approved"
        and current["content_hash"] == evidence_repository.content_hash(
            shot.model_dump(mode="json")
        )
    )

def _storyboard_shot_evidence_requires_rebind(
    conn,
    episode_id: str,
    board: Storyboard,
) -> bool:
    rows = conn.execute(
        "SELECT shot_no,storyboard_artifact_id FROM shots "
        "WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    if len(rows) != len(board.shots):
        return True
    return any(
        int(row["shot_no"]) != int(shot.shot_no)
        or not _storyboard_shot_artifact_matches(
            conn, episode_id, shot, row["storyboard_artifact_id"],
        )
        for row, shot in zip(rows, board.shots)
    )

def _storyboard_publication_evidence_state(
    episode: dict,
    board: Storyboard,
) -> tuple[bool, bool]:
    """Return ``(current, refinalize_only)`` for the bound release evidence.

    A calibration-authority update can stale an otherwise byte-identical
    Storyboard Artifact.  That is an evidence-lineage change, not a request to
    repair or regenerate shots.  Only exact authority-projection equality may
    take this refinalization path; real projection drift remains a hard gate.
    """
    artifact_id = str(episode.get("storyboard_artifact_id") or "")
    certificate_id = str(
        episode.get("storyboard_completion_certificate_id") or ""
    )
    revision_id = str(episode.get("storyboard_production_revision_id") or "")
    if not artifact_id or not certificate_id or not revision_id:
        return False, False
    artifact = evidence_repository.get_artifact(artifact_id)
    if (
        artifact is None
        or artifact.get("scope_type") != "episode"
        or artifact.get("scope_id") != episode.get("id")
    ):
        # Preserve the evaluator's typed hard-gate explanation for malformed
        # legacy fixtures and genuinely missing evidence.
        return True, False
    try:
        from app.narrative import storyboard_authority_projection

        projection_matches = storyboard_authority_projection(
            artifact.get("content") or {}
        ) == storyboard_authority_projection(board.model_dump(mode="json"))
    except Exception:  # noqa: BLE001 - malformed authority remains a hard gate
        return True, False
    if not projection_matches:
        return True, False
    try:
        # 叙事权威凭证校验（verify_current_storyboard_completion_authority）只
        # 对「当前仍要求叙事权威」的分集有意义——它自己会在
        # narrative_authority_required=False 时主动抛错（"当前剧集不使用叙事
        # 权威凭证"），这不是证据变质，是分类判据本身已经变了（典型场景：
        # 该集分镜/剧本已迁移到 prep_pack 6.0.0+ 合同，contract 设计上就不产出
        # narrative_plan，见 108e2c1 对 resolve_downstream_screenplay 的说明）。
        # 上面的 projection_matches 已经证明正文投影逐字一致；如果这里不预先
        # 判断分类，就会把"这项校验天然不适用"误判成"证据异常，禁止原地
        # 续跑"——一个内容完全没问题的已确认分集会被卡在一句用户既看不懂、
        # 也无处可核实的报错前，真实回归 ep_3d523ff4d0a4（EP1）复现。
        from app.production.screenplay_authority import (
            resolve_downstream_screenplay,
        )

        screenplay_context = resolve_downstream_screenplay(
            str(episode.get("id") or ""),
        )
        if not screenplay_context.narrative_authority_required:
            return True, False
        from app.production.certificate import (
            verify_current_storyboard_completion_authority,
        )

        verify_current_storyboard_completion_authority(
            episode=episode,
            current_storyboard_content=board.model_dump(mode="json"),
        )
    except Exception:  # noqa: BLE001 - exact content may safely reissue lineage
        return False, True
    return True, False

def _finalize_storyboard_evidence(
    episode_id: str,
    board: Storyboard,
) -> str:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    planned_total = 0
    try:
        planned_total = len(
            json.loads(ep["storyboard_outline_json"] or "{}").get("shots") or []
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        planned_total = 0
    findings: list[str] = []
    if not board.shots:
        raise RuntimeError("没有任何分镜产物可发布")
    if planned_total and len(board.shots) != planned_total:
        raise RuntimeError(f"分镜数量与计划不同：已完成 {len(board.shots)}/{planned_total} 镜")
    if not board.shots[-1].is_final:
        raise RuntimeError("最终镜未标记收束，禁止发布未结束的分镜")
    screenplay = None
    narrative_authority = False
    if ep["screenplay_json"]:
        from app.production.screenplay_authority import resolve_downstream_screenplay

        try:
            screenplay_context = resolve_downstream_screenplay(
                episode_id,
                conn=conn,
            )
        except Exception as exc:
            raise RuntimeError(f"分镜发布前剧本权威链无效：{exc}") from exc
        screenplay = screenplay_context.screenplay
        narrative_authority = screenplay_context.narrative_authority_required
    if narrative_authority:
        from app.narrative import (
            validate_storyboard_narrative,
        )

        narrative_errors = validate_storyboard_narrative(
            board,
            screenplay,
            outline=(
                StoryboardOutline.model_validate_json(ep["storyboard_outline_json"])
                if ep["storyboard_outline_json"]
                else None
            ),
            complete=True,
            expected_scope_id=episode_id,
        )
        if narrative_errors:
            raise RuntimeError(
                "分镜叙事硬门禁未通过：" + "；".join(narrative_errors[:8])
            )
        # 冷观众审读（narrative_review）与一次观看校准（narrative_calibration）
        # 已整体下线（用户拍板）：这里曾经的强制要求在删除前就已经是一个必死
        # 分支——本函数在活代码里的两个调用方（app.production.storyboard_pack.
        # run_storyboard_pack_generation、app.domain.video_ops._confirm_storyboard_
        # impl）都只按位置参数传 (episode_id, board)，从未提供过
        # narrative_review_report，所以 narrative_authority=True 时这里过去
        # 100% 抛 RuntimeError（分镜冷观众审读报告缺失，禁止发布）。删除这段
        # 不会让任何当前可达路径从"会拒绝"变成"会放行"——上面的
        # validate_storyboard_narrative 结构化叙事硬门禁（108e2c1 修的那道）
        # 原样保留，narrative_authority 的分类判据本身（screenplay_context.
        # narrative_authority_required）也原样保留，没有被本次改动放宽。
    if _sync_storyboard_scene_bindings(conn, episode_id, board):
        # 这是由当前门禁确定的派生外键修复，即使后续证据发布失败也应保留，
        # 避免下次重试继续读取已经证伪的历史场景绑定。
        conn.commit()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    shot_rows = _ensure_current_storyboard_shot_artifacts(
        conn, episode_id, board,
    )
    from app.storyboard_workspace import verify_or_bind_existing_excerpt
    for row in shot_rows:
        try:
            verify_or_bind_existing_excerpt(
                episode_id, row["id"], row["source_excerpt"] or "",
            )
        except Exception as exc:  # noqa: BLE001 - evidence finding is score-only at publish
            findings.append(f"镜头来源证据未绑定：{exc}")
    if narrative_authority and findings:
        raise RuntimeError(
            "分镜来源证据硬门禁未通过：" + "；".join(findings[:8])
        )
    shot_parent_ids: list[str] = []
    for row in shot_rows:
        artifact_id = row["storyboard_artifact_id"]
        if not artifact_id:
            continue
        shot_parent_ids.append(str(artifact_id))
        artifact_row = conn.execute(
            "SELECT parent_artifact_ids_json FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()
        if artifact_row:
            try:
                shot_parent_ids.extend(
                    str(item)
                    for item in json.loads(
                        artifact_row["parent_artifact_ids_json"] or "[]"
                    )
                    if item
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    parents = list(dict.fromkeys(
        str(artifact_id)
        for artifact_id in (
            project["bible_artifact_id"],
            ep["screenplay_artifact_id"],
            *shot_parent_ids,
        )
        if artifact_id
    ))
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
        status="warning" if findings else "passed",
        hard_gate_passed=not findings,
        evaluation_role="runtime_gate" if narrative_authority else "score_only",
        score_status="scored",
        runtime_blocking=narrative_authority,
        retry_eligible=False,
        score=max(0, 100 - 10 * len(findings)),
        issues=[Issue(
            code="STORYBOARD_GATE_EXHAUSTED_WARNING",
            severity=IssueSeverity.WARNING,
            subject=episode_id,
            message=message,
        ) for message in findings],
        evidence={
            "shot_count": len(board.shots),
            "duration_range_s": [config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S],
            "duration_decided_by": "model",
            "checkpoint_artifact_ids": parents,
            "gate_retry_exhausted": bool(findings),
            "findings": findings,
        },
    )
    artifact = evidence_repository.commit_artifact(None, artifact["id"], [evaluation])
    from app.production.publish import publish_storyboard
    from app.production.revision import (
        bind_unpublished_revision_metadata,
        ensure_production_revision,
        mark_baseline_generated,
        update_working_artifact,
    )

    revision = ensure_production_revision(
        episode_id=episode_id,
        kind="storyboard",
        input_fingerprint=evidence_repository.content_hash(board.model_dump(mode="json")),
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
        resume=True,
    )
    if revision.working_artifact_id:
        update_working_artifact(revision.id, artifact["id"])
    else:
        revision = mark_baseline_generated(
            revision.id,
            baseline_artifact_id=artifact["id"],
            working_artifact_id=artifact["id"],
        )
    revision = bind_unpublished_revision_metadata(
        revision.id,
        input_fingerprint=(
            revision.input_fingerprint
            or evidence_repository.content_hash(board.model_dump(mode="json"))
        ),
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
    )
    eval_rows = conn.execute(
        "SELECT id FROM evaluations WHERE artifact_id=? ORDER BY created_at",
        (artifact["id"],),
    ).fetchall()
    publish_storyboard(
        episode_id=episode_id,
        revision_id=revision.id,
        artifact_id=artifact["id"],
        artifact_hash=artifact.get("content_hash") or evidence_repository.content_hash(
            board.model_dump(mode="json")
        ),
        evaluation_ids=[str(row["id"]) for row in eval_rows],
        shots_payload=[shot.model_dump(mode="json") for shot in board.shots],
        outline_json=ep["storyboard_outline_json"],
        input_fingerprint=revision.input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=revision.qa_profile_version or "storyboard-full-gate-2",
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
    计划已跑完、或已到技术硬上限时，禁止再发明大纲外幻觉镜。
    """
    if not is_final:
        return False
    if completed_count >= max_shots:
        return False
    # planned_count>0：大纲驱动；已达当前计划长度则禁止计划外补镜。
    if planned_count > 0 and completed_count >= planned_count:
        return False
    return _soft_gap_continue_residual(residual)

def _reconcile_storyboard_plan(conn, episode_id: str, episode_no: int,
                              outline: StoryboardOutline | None, completed: list[Shot],
                              persisted_total: int) -> tuple[int, int, str] | None:
    """让落库大纲成为唯一事实源，消除"规划十几镜却分镜24"的困惑。

    逐镜阶段若模型判断单镜超过合法时长上限（config.VIDEO_DURATION_MAX_S）仍演不完而继续拆镜、镜头数超出计划长度，
    内存 outline 会领先于
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
                scene_time=shot.scene_time or "",
                scene_name=shot.scene_name or "",
                scene_setting=shot.scene_setting or "",
                beat=beat,
                covers="",
                duration_s=int(shot.duration_s or 0) or None,
            ))
    to_total = len(outline.shots)
    if to_total == persisted_total:
        return None
    from app.storyboard_authority import persist_storyboard_outline_projection

    persist_storyboard_outline_projection(
        episode_id,
        outline,
        conn=conn,
    )
    reason = "shot_overflow" if appended else "covers_split"
    log_provider_call(
        "storyboard_plan_revised", config.MODEL_TEXT, "PLAN_REVISED", None, 0,
        meta={"episode_id": episode_id, "episode_no": episode_no, "stage": "分镜脚本",
              "from": persisted_total, "to": to_total,
              "actual_shots": len(completed), "reason": reason})
    return (persisted_total, to_total, reason)
