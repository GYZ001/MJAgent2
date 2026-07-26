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
    # 仅在此刻清空下游（修订发布）；首次无下游时 noop
    if clear_downstream:
        from app import worker
        worker.delete_episode_shots(episode_id)
        try:
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=NULL, storyboard_artifact_id=NULL, "
                "storyboard_warning=NULL, published_storyboard_artifact_id=NULL, "
                "working_storyboard_artifact_id=NULL WHERE id=?",
                (episode_id,),
            )
        except Exception:  # noqa: BLE001
            conn.execute(
                "UPDATE episodes SET storyboard_outline_json=NULL, storyboard_artifact_id=NULL, "
                "storyboard_warning=NULL WHERE id=?",
                (episode_id,),
            )

    ledger_json = json.dumps({
        "episode_premise": script.episode_premise,
        "events": [e.model_dump() for e in (script.events or [])],
        "information_ledger": [i.model_dump() for i in (script.information_ledger or [])],
        "voice_bible": [v.model_dump() for v in (script.voice_bible or [])],
        "approved_adaptations": list(script.approved_adaptations or []),
        "forbidden_additions": list(script.forbidden_additions or []),
    }, ensure_ascii=False)

    # commit artifact approved
    conn.execute(
        "UPDATE artifacts SET status='approved', trust_level='T2' WHERE id=?",
        (artifact_id,),
    )

    conn.execute(
        "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', screenplay_error=NULL, "
        "screenplay_updated_at=?, screenplay_artifact_id=?, story_ledger_json=?, "
        "status='planned', script_error=NULL WHERE id=?",
        (script.model_dump_json(), now(), artifact_id, ledger_json, episode_id),
    )
    set_published_artifact(revision_id, artifact_id, certificate_id=cert.certificate_id)
    consume_completion_certificate(cert.certificate_id)
    conn.commit()
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
    set_published_artifact(revision_id, artifact_id, certificate_id=cert.certificate_id)
    consume_completion_certificate(cert.certificate_id)
    conn.commit()
    return {
        "episode_id": episode_id,
        "artifact_id": artifact_id,
        "certificate_id": cert.certificate_id,
        "shot_count": len(shots_payload),
        "status": "scripted",
    }


def can_issue_certificate(issues: list) -> bool:
    return blocker_count(issues) == 0 and must_fix_count(issues) == 0
