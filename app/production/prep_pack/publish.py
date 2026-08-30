"""_publish_prep_pack: atomic publish of a generation attempt plus its
completion certificate.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

import json
from app.db import (
    get_conn,
    now,
)
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import (
    Evaluation,
    EvidenceArtifact,
)
from app.orchestration.state_machine import transition_step
from app.production.certificate import (
    assert_publish_has_certificate,
    consume_completion_certificate,
    issue_completion_certificate,
    verify_completion_certificate,
)
from typing import Any

from .contracts import (
    PREP_PACK_VERSION,
    QA_PROFILE_VERSION,
    _QA_EVALUATOR_NAME,
)


def _publish_prep_pack(
    *,
    episode_id: str,
    payload: dict[str, Any],
    run_id: str | None,
    rejected_paratext_claims: list[int] | None = None,
    true_name_hints: list[dict[str, Any]] | None = None,
    scene_alias_anchors: list[dict[str, Any]] | None = None,
    rejected_alias_conflicts: list[dict[str, Any]] | None = None,
    character_manifest_anomaly: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn = get_conn()
    contract = get_contract("screenplay")
    episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if episode is None:
        raise ValueError("待发布剧集不存在")

    step_id = (
        evidence_repository.create_step(
            run_id, "episode_prep_pack_publish",
            agent_name="episode_prep_pack",
            contract_version=contract.version,
        )
        if run_id else None
    )
    if step_id:
        transition_step(step_id, "PENDING", "READY", "输入已就绪", conn=None)
        transition_step(step_id, "READY", "RUNNING", "步骤开始", conn=None)
    artifact_row = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="episode_prep_pack",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T2",
            content=payload,
            contract_version=contract.version,
        ),
        step_run_id=step_id,
    )
    artifact_id = str(artifact_row["id"])
    artifact_hash = str(artifact_row["content_hash"])

    input_fingerprint = evidence_repository.content_hash({
        "episode_id": episode_id,
        "episode_scope": payload["episode_scope"],
    })

    if conn.in_transaction:
        raise RuntimeError("分集映射包发布前存在未收口事务")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE artifacts SET status='validated' WHERE id=? AND status='candidate'",
            (artifact_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("待发布 working Artifact 状态发生冲突")

        evaluation_row = evidence_repository.create_evaluation(
            artifact_id,
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name=_QA_EVALUATOR_NAME,
                evaluator_version=QA_PROFILE_VERSION,
                status="passed",
                hard_gate_passed=True,
                evaluation_role="score_only",
                runtime_blocking=False,
                retry_eligible=False,
                score=100.0,
                issues=[],
                evidence={
                    "prep_pack_version": PREP_PACK_VERSION,
                    "coverage_uncovered": payload["coverage_ledger"]["uncovered"],
                    # 1.4.1 (kept in 2.0.0): a segment the model claimed was
                    # paratext but that also carries verified asset evidence
                    # -- the evidence wins, this claim is vetoed back to
                    # ordinary content. Observability only, never part of
                    # the frozen artifact payload itself (see
                    # _prep_pack_build_coverage_ledger's
                    # rejected_paratext_claims).
                    "rejected_paratext_claims": rejected_paratext_claims or [],
                    # 1.5.0: every suspected_true_name hypothesis's outcome
                    # (accepted+bound or rejected+discarded) -- observability
                    # only, see _prep_pack_verify_true_name_hypothesis.
                    "true_name_hints": true_name_hints or [],
                    # 1.5.1 (task①): every scene alias newly registered this
                    # episode (Bible.scenes[].aliases persistence itself
                    # already happened synchronously in _resolve_assets;
                    # this is observability only, records the anchor
                    # segment for audit).
                    "scene_alias_anchors": scene_alias_anchors or [],
                    # 1.5.2 (task②): every rebind rejected because the same
                    # alias string was already bound to a DIFFERENT character
                    # elsewhere in this project (see
                    # _prep_pack_cross_episode_alias_conflict, real EP3
                    # regression: "小胖子" wrongly rebound to "王有材").
                    "rejected_alias_conflicts": rejected_alias_conflicts or [],
                    # 第31轮真实回归 EP7, ep_621d93ac1231, version NOT
                    # re-bumped -- pure observability addition, no schema/
                    # prompt-contract/resolution-logic change (same footnote
                    # convention as the 1.5.2 "version NOT re-bumped" notes
                    # above): non-null only when character_mentions came back
                    # empty from chunk extraction while known_characters and
                    # (scene_mentions or prop_mentions) were both non-empty --
                    # a suspicious single-dimension degeneration that the "any
                    # one of characters/scenes/props non-empty" gate above
                    # cannot see (see _generate_prep_pack_once's comment at
                    # that same gate). Observability only, never blocks
                    # publish -- see that comment for why this data-derived
                    # signal exists and why it stays non-blocking.
                    "character_manifest_anomaly": character_manifest_anomaly,
                },
            ),
            step_run_id=step_id,
            conn=conn,
            commit=False,
        )

        cert = issue_completion_certificate(
            kind="screenplay",
            scope_id=episode_id,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            input_fingerprint=input_fingerprint,
            contract_version=contract.version,
            qa_profile_version=QA_PROFILE_VERSION,
            evaluation_ids=[str(evaluation_row["id"])],
            blockers=0,
            must_fix_issues=0,
            production_revision_id=None,
            conn=conn,
            commit=False,
        )
        verify_completion_certificate(
            cert,
            expected_artifact_id=artifact_id,
            expected_artifact_hash=artifact_hash,
            expected_input_fingerprint=input_fingerprint,
            expected_contract_version=contract.version,
            conn=conn,
        )
        assert_publish_has_certificate(
            kind="screenplay", episode_id=episode_id, certificate_id=cert.certificate_id,
        )

        conn.execute(
            "UPDATE artifacts SET status='approved', trust_level='T2' WHERE id=?",
            (artifact_id,),
        )
        episode_cursor = conn.execute(
            "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', "
            "screenplay_error=NULL, screenplay_updated_at=?, screenplay_artifact_id=?, "
            "published_screenplay_artifact_id=?, screenplay_completion_certificate_id=?, "
            "active_screenplay_run_id=NULL, status='planned', script_error=NULL "
            "WHERE id=?",
            (
                json.dumps(payload, ensure_ascii=False),
                now(),
                artifact_id,
                artifact_id,
                cert.certificate_id,
                episode_id,
            ),
        )
        if episode_cursor.rowcount != 1:
            raise ValueError("分集映射包发布 episode 更新发生冲突")
        # 2.0.0：不再预写 episodes.cliffhanger/hook（payload 不再携带这两个
        # 字段，见 PREP_PACK_VERSION 上方 2.0.0 大注释）——这两列本来就会被
        # app/production/publish.py 在真正发布时用 script.ending_hook（发布
        # 时的权威来源）覆盖，prep_pack 阶段不再预写不是能力回退。
        consume_completion_certificate(cert.certificate_id, conn=conn, commit=False)
        conn.commit()
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if step_id:
            transition_step(
                step_id, "RUNNING", "FAILED", str(exc)[:1000],
                decision="escalate", error_code=type(exc).__name__.upper(), conn=None,
            )
        raise
    if step_id:
        transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept", conn=None)
    return {
        "episode_id": episode_id,
        "artifact_id": artifact_id,
        "certificate_id": cert.certificate_id,
        "status": "ready",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

