"""原子发布：Working → Published，消费完成凭证。"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, now
from app.production.certificate import (
    assert_publish_has_certificate,
    consume_completion_certificate,
    issue_completion_certificate,
    verify_completion_certificate,
)
from app.production.patch import load_screenplay_from_artifact
from app.production.revision import get_production_revision, set_published_artifact
from app.production.structured_issues import blocker_count, must_fix_count


def publish_screenplay(
    *,
    episode_id: str,
    revision_id: str,
    artifact_id: str,
    artifact_hash: str,
    evaluation_ids: list[str],
    input_fingerprint: str = "",
    contract_version: str = "",
    qa_profile_version: str = "",
    clear_downstream: bool = True,
) -> dict[str, Any]:
    """签发凭证并原子发布剧本到页面投影。内部修复不得调用此函数的下游清空以外路径。"""
    rev = get_production_revision(revision_id)
    if rev is None:
        raise ValueError("production revision 不存在")
    if rev.working_artifact_id != artifact_id:
        raise ValueError("只能发布当前 working Artifact")

    script = load_screenplay_from_artifact(artifact_id)
    conn = get_conn()
    if script.narrative_plan is not None:
        from app.narrative import validate_screenplay_narrative
        from app.production.screenplay_authority import screenplay_authority_fingerprint

        narrative_errors = validate_screenplay_narrative(
            script,
            require=True,
            expected_scope_id=episode_id,
        )
        if narrative_errors:
            raise ValueError("剧本叙事硬门禁未通过：" + "；".join(narrative_errors[:6]))
        authority_fingerprint = screenplay_authority_fingerprint(
            episode_id,
            conn=conn,
            contract_version=contract_version or rev.contract_version,
            qa_profile_version=qa_profile_version or rev.qa_profile_version,
        )
        if (input_fingerprint or rev.input_fingerprint) != authority_fingerprint:
            raise ValueError("剧本 revision 未绑定当前原文/Bible/人物决议/改编约束指纹")
        if not evaluation_ids:
            raise ValueError("叙事剧本发布缺少当前 QA Evaluation")
        marks = ",".join("?" for _ in evaluation_ids)
        qa_rows = conn.execute(
            f"SELECT evaluator_name,evidence_json FROM evaluations WHERE id IN ({marks})",
            evaluation_ids,
        ).fetchall()
        authority_evidence = []
        for row in qa_rows:
            if row["evaluator_name"] != "screenplay_production_qa":
                continue
            try:
                authority_evidence.append(json.loads(row["evidence_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                authority_evidence.append({})
        if len(authority_evidence) != 1 or authority_evidence[0].get(
            "authority_input_fingerprint"
        ) != authority_fingerprint:
            raise ValueError("剧本 QA 证据未精确绑定当前权威输入指纹")

    artifact = conn.execute(
        "SELECT status FROM artifacts WHERE id=?",
        (artifact_id,),
    ).fetchone()
    if artifact is None:
        raise ValueError("待发布 working Artifact 不存在")
    original_status = str(artifact["status"] or "")
    if original_status not in {"candidate", "working", "validated", "approved"}:
        raise ValueError("待发布 working Artifact 状态不可用")
    if original_status in {"candidate", "working"}:
        conn.execute(
            "UPDATE artifacts SET status='validated' WHERE id=? AND status=?",
            (artifact_id, original_status),
        )
    try:
        cert = issue_completion_certificate(
            kind="screenplay",
            scope_id=episode_id,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            input_fingerprint=input_fingerprint or rev.input_fingerprint,
            contract_version=contract_version or rev.contract_version,
            qa_profile_version=qa_profile_version or rev.qa_profile_version,
            evaluation_ids=evaluation_ids,
            blockers=0,
            must_fix_issues=0,
            production_revision_id=revision_id,
        )
    except Exception:
        if original_status in {"candidate", "working"}:
            conn.execute(
                "UPDATE artifacts SET status=? WHERE id=?",
                (original_status, artifact_id),
            )
            conn.commit()
        raise
    verify_completion_certificate(
        cert,
        expected_artifact_id=artifact_id,
        expected_artifact_hash=artifact_hash,
        expected_input_fingerprint=input_fingerprint or rev.input_fingerprint or None,
        expected_contract_version=contract_version or rev.contract_version or None,
    )
    assert_publish_has_certificate(
        kind="screenplay", episode_id=episode_id, certificate_id=cert.certificate_id,
    )

    previous = conn.execute(
        "SELECT screenplay_artifact_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    previous_artifact_id = previous["screenplay_artifact_id"] if previous else None
    # 仅在此刻清空下游（修订发布）；首次无下游时 noop
    if clear_downstream:
        from app import worker
        worker.delete_episode_shots(episode_id)
        conn.execute(
            """UPDATE episodes SET storyboard_outline_json=NULL, storyboard_artifact_id=NULL,
                      storyboard_warning=NULL, published_storyboard_artifact_id=NULL,
                      working_storyboard_artifact_id=NULL, active_storyboard_run_id=NULL,
                      storyboard_production_revision_id=NULL,
                      storyboard_completion_certificate_id=NULL,
                      narrative_status='needs_review', narrative_review_artifact_id=NULL,
                      active_video_run_id=NULL, video_control_json=NULL,
                      delivery_artifact_id=NULL, delivery_status='not_ready'
                WHERE id=?""",
            (episode_id,),
        )

    # commit artifact approved
    conn.execute(
        "UPDATE artifacts SET status='approved', trust_level='T2' WHERE id=?",
        (artifact_id,),
    )

    conn.execute(
        "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', screenplay_error=NULL, "
        "screenplay_updated_at=?, screenplay_artifact_id=?, "
        "published_screenplay_artifact_id=?, "
        "status='planned', script_error=NULL WHERE id=?",
        (script.model_dump_json(), now(), artifact_id, artifact_id, episode_id),
    )
    set_published_artifact(
        revision_id,
        artifact_id,
        certificate_id=cert.certificate_id,
        conn=conn,
        commit=False,
    )
    consume_completion_certificate(cert.certificate_id, conn=conn, commit=False)
    conn.execute("DELETE FROM screenplay_drafts WHERE episode_id=?", (episode_id,))
    conn.commit()
    if previous_artifact_id and previous_artifact_id != artifact_id:
        from app.evidence import repository as evidence_repository
        evidence_repository.invalidate_descendants(
            previous_artifact_id,
            f"上游剧本已由 {artifact_id} 替代",
            exclude_ids={artifact_id},
        )
    return {
        "episode_id": episode_id,
        "artifact_id": artifact_id,
        "certificate_id": cert.certificate_id,
        "status": "ready",
    }


def publish_storyboard(
    *,
    episode_id: str,
    revision_id: str,
    artifact_id: str,
    artifact_hash: str,
    evaluation_ids: list[str],
    shots_payload: list[dict[str, Any]],
    outline_json: str | None = None,
    input_fingerprint: str = "",
    contract_version: str = "",
    qa_profile_version: str = "",
) -> dict[str, Any]:
    """整集分镜原子发布到正式 shots 表。"""
    rev = get_production_revision(revision_id)
    if rev is None:
        raise ValueError("production revision 不存在")
    if rev.working_artifact_id != artifact_id:
        raise ValueError("只能发布当前 working Artifact")
    planned_total = 0
    if outline_json:
        try:
            planned_total = len(json.loads(outline_json).get("shots") or [])
        except (TypeError, ValueError, json.JSONDecodeError):
            planned_total = 0
    if not shots_payload:
        raise ValueError("没有任何分镜产物可发布")
    if planned_total and len(shots_payload) != planned_total:
        raise ValueError(f"分镜数量与计划不同：已完成 {len(shots_payload)}/{planned_total} 镜")
    if not bool(shots_payload[-1].get("is_final")):
        raise ValueError("最终镜未标记收束，禁止发布未结束的分镜")

    conn = get_conn()
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if episode_row is None:
        raise ValueError("待发布分镜所属剧集不存在")
    from app.evidence import repository as evidence_repository
    from app.schemas import Storyboard

    board = Storyboard.model_validate({
        "episode_no": int(episode_row["episode_no"]),
        "shots": shots_payload,
    })
    board_artifact = evidence_repository.get_artifact(artifact_id)
    if (
        board_artifact is None
        or board_artifact.get("type") not in {"storyboard", "storyboard_document"}
        or board_artifact.get("scope_type") != "episode"
        or board_artifact.get("scope_id") != episode_id
        or board_artifact.get("status")
        in {"stale", "rejected", "superseded", "needs_revision"}
    ):
        raise ValueError("待发布分镜 Artifact 类型、范围或状态无效")
    try:
        artifact_board = Storyboard.model_validate(board_artifact.get("content"))
    except Exception as exc:  # noqa: BLE001 - immutable artifact boundary
        raise ValueError(f"待发布分镜 Artifact 内容无法解析：{exc}") from exc
    if artifact_board.model_dump(mode="json") != board.model_dump(mode="json"):
        raise ValueError("待发布 shots_payload 与完成凭证绑定的 Artifact 内容不一致")
    if episode_row["screenplay_json"]:
        from app.narrative import validate_storyboard_narrative
        from app.schemas import StoryboardOutline
        from app.production.screenplay_authority import (
            resolve_downstream_screenplay,
        )

        screenplay_context = resolve_downstream_screenplay(episode_id, conn=conn)
        screenplay = screenplay_context.screenplay
        if screenplay_context.narrative_authority_required:
            outline = StoryboardOutline.model_validate_json(outline_json) if outline_json else None
            narrative_errors = validate_storyboard_narrative(
                board,
                screenplay,
                outline=outline,
                complete=True,
                expected_scope_id=episode_id,
            )
            if narrative_errors:
                raise ValueError(
                    "分镜叙事硬门禁未通过：" + "；".join(narrative_errors[:6])
                )
            from app.narrative_review import (
                NarrativeReviewError,
                verify_persisted_narrative_review,
            )
            from app.schemas import NarrativeReviewReport

            parent_ids = list((board_artifact or {}).get("parent_artifact_ids") or [])
            review_artifacts = [
                evidence_repository.get_artifact(parent_id) for parent_id in parent_ids
            ]
            report_artifacts = [
                item for item in review_artifacts
                if item is not None and item.get("type") == "narrative_review_report"
            ]
            if len(report_artifacts) != 1:
                raise ValueError("待发布分镜没有唯一、不可变的冷观众审读报告父证据")
            try:
                persisted_report = NarrativeReviewReport.model_validate(
                    report_artifacts[0].get("content")
                )
                verify_persisted_narrative_review(
                    episode_id=episode_id,
                    screenplay=screenplay,
                    board=board,
                    report=persisted_report,
                    artifact_ids=parent_ids,
                )
            except (NarrativeReviewError, ValueError) as exc:
                raise ValueError(f"冷观众审读证据链无效，禁止发布分镜：{exc}") from exc
            if not evaluation_ids:
                raise ValueError("分镜叙事发布缺少冷观众 runtime-gate Evaluation")
            marks = ",".join("?" for _ in evaluation_ids)
            review_rows = conn.execute(
                f"""SELECT evaluator_name,status,hard_gate_passed,evaluation_role
                       FROM evaluations
                      WHERE artifact_id=? AND id IN ({marks})""",
                (artifact_id, *evaluation_ids),
            ).fetchall()
            if not any(
                row["evaluator_name"] == "narrative_blind_comparator"
                and row["evaluation_role"] == "runtime_gate"
                and row["status"] == "passed"
                and bool(row["hard_gate_passed"])
                for row in review_rows
            ):
                raise ValueError("冷观众多先验审读未通过或已失效，禁止发布分镜")

    cert = issue_completion_certificate(
        kind="storyboard",
        scope_id=episode_id,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        input_fingerprint=input_fingerprint or rev.input_fingerprint,
        contract_version=contract_version or rev.contract_version,
        qa_profile_version=qa_profile_version or rev.qa_profile_version,
        evaluation_ids=evaluation_ids,
        blockers=0,
        must_fix_issues=0,
        production_revision_id=revision_id,
    )
    verify_completion_certificate(
        cert,
        expected_artifact_id=artifact_id,
        expected_artifact_hash=artifact_hash,
    )
    assert_publish_has_certificate(
        kind="storyboard", episode_id=episode_id, certificate_id=cert.certificate_id,
    )

    # 正式投影：由调用方已准备好 shots 行；这里更新指针与状态
    if outline_json is not None:
        conn.execute(
            "UPDATE episodes SET storyboard_outline_json=? WHERE id=?",
            (outline_json, episode_id),
        )
    conn.execute(
        "UPDATE episodes SET status='scripted', script_error=NULL, storyboard_warning=NULL, "
        "storyboard_artifact_id=? WHERE id=?",
        (artifact_id, episode_id),
    )
    set_published_artifact(
        revision_id,
        artifact_id,
        certificate_id=cert.certificate_id,
        conn=conn,
        commit=False,
    )
    consume_completion_certificate(cert.certificate_id, conn=conn, commit=False)
    conn.commit()
    return {
        "episode_id": episode_id,
        "artifact_id": artifact_id,
        "certificate_id": cert.certificate_id,
        "shot_count": len(shots_payload),
        "status": "scripted",
    }


def can_issue_certificate(issues: list) -> bool:
    """QA 是只读门禁：任何 blocker / must-fix 都必须先由 Repair 生成新候选。"""
    return blocker_count(issues) == 0 and must_fix_count(issues) == 0
