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

    script = load_screenplay_from_artifact(artifact_id)
    conn = get_conn()
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
        "status='planned', script_error=NULL WHERE id=?",
        (script.model_dump_json(), now(), artifact_id, episode_id),
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
    if planned_total and len(shots_payload) != planned_total:
        raise ValueError(
            f"拒绝发布不完整分镜：已完成 {len(shots_payload)}/{planned_total} 镜"
        )
    if not shots_payload or not bool(shots_payload[-1].get("is_final")):
        raise ValueError("拒绝发布不完整分镜：最终镜缺失或未标记收束")

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

    conn = get_conn()
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
