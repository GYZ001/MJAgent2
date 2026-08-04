"""Completion Certificate：绑定精确 Artifact hash 的完成凭证。"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db import get_conn, new_id, now
from app.evidence import repository as evidence_repository
from app.production.metrics import record_certificate_issued, record_publish_without_certificate


_SCREENPLAY_GATE_EVALUATOR = "screenplay_production_qa"
_STORYBOARD_GATE_EVALUATOR = "storyboard_full_gate"
_NARRATIVE_REVIEW_EVALUATOR = "narrative_blind_comparator"
_CURRENT_QA_PROFILE = {
    "screenplay": "screenplay-qa-gate-2",
    "storyboard": "storyboard-full-gate-2",
}


class CompletionCertificate(BaseModel):
    certificate_id: str
    kind: Literal["screenplay", "storyboard"]
    scope_id: str
    artifact_id: str
    artifact_hash: str
    input_fingerprint: str = ""
    contract_version: str = ""
    qa_profile_version: str = ""
    evaluation_ids: list[str] = Field(default_factory=list)
    blockers: int = 0
    must_fix_issues: int = 0
    issued_at: float = 0.0
    consumed_at: float | None = None
    production_revision_id: str | None = None


def ensure_completion_certificates_table(conn=None) -> None:
    db = conn or get_conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS completion_certificates (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            artifact_hash TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL DEFAULT '',
            contract_version TEXT NOT NULL DEFAULT '',
            qa_profile_version TEXT NOT NULL DEFAULT '',
            evaluation_ids_json TEXT NOT NULL DEFAULT '[]',
            blockers INTEGER NOT NULL DEFAULT 0,
            must_fix_issues INTEGER NOT NULL DEFAULT 0,
            production_revision_id TEXT,
            issued_at REAL NOT NULL,
            consumed_at REAL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    db.commit()


def _load_evaluation_rows(evaluation_ids: list[str]) -> list[Any]:
    if not evaluation_ids:
        return []
    marks = ",".join("?" for _ in evaluation_ids)
    return list(get_conn().execute(
        f"""SELECT id,artifact_id,status,hard_gate_passed,evaluation_role,
                   evaluator_name,evaluator_version,runtime_blocking,issues_json
              FROM evaluations WHERE id IN ({marks})""",
        evaluation_ids,
    ).fetchall())


def _row_has_blocking_issue(row: Any) -> bool:
    try:
        issues = json.loads(row["issues_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return True
    if not isinstance(issues, list):
        return True
    return any(
        isinstance(issue, dict)
        and (
            str(issue.get("severity") or "").lower() == "blocker"
            or bool(issue.get("must_fix"))
        )
        for issue in issues
    )


def _is_passing_runtime_gate(row: Any) -> bool:
    return bool(
        row["evaluation_role"] == "runtime_gate"
        and bool(row["runtime_blocking"])
        and row["status"] == "passed"
        and bool(row["hard_gate_passed"])
        and not _row_has_blocking_issue(row)
    )


def _required_exact_runtime_gate(
    rows: list[Any],
    *,
    evaluator_name: str,
    evaluator_version: str,
) -> Any:
    named = [row for row in rows if row["evaluator_name"] == evaluator_name]
    if len(named) != 1:
        raise ValueError(
            f"完成凭证必须精确引用一个 {evaluator_name} runtime gate"
        )
    row = named[0]
    if not evaluator_version or row["evaluator_version"] != evaluator_version:
        raise ValueError(
            f"完成凭证的 {evaluator_name} evaluator_version 与当前契约不匹配"
        )
    if not _is_passing_runtime_gate(row):
        raise ValueError(
            f"完成凭证的 {evaluator_name} 必须是已通过且 runtime_blocking 的 runtime_gate"
        )
    return row


def _narrative_screenplay_for_artifact(
    *,
    kind: Literal["screenplay", "storyboard"],
    scope_id: str,
    artifact: dict[str, Any],
):
    """Return the authoritative screenplay only when this artifact uses it.

    A missing plan is the explicit legacy boundary.  A malformed non-empty
    projection is not silently downgraded to legacy.
    """
    from app.schemas import EpisodeScreenplay

    if kind == "screenplay":
        content = artifact.get("content")
        if not isinstance(content, dict):
            return None
        projection = content.get("_projection")
        narrative_payload = (
            projection.get("narrative_plan")
            if isinstance(projection, dict)
            else content.get("narrative_plan")
        )
        if narrative_payload is None:
            return None
        try:
            if "screenplay_metadata" in content:
                from app.production.screenplay_document import (
                    ScreenplayDocument,
                    document_to_screenplay,
                )

                return document_to_screenplay(ScreenplayDocument.model_validate(content))
            if isinstance(projection, dict):
                return EpisodeScreenplay.model_validate(projection)
            return EpisodeScreenplay.model_validate(content)
        except Exception as exc:  # noqa: BLE001 - immutable authority boundary
            raise ValueError(f"剧本 Artifact 的叙事权威契约无法验证：{exc}") from exc

    episode = get_conn().execute(
        "SELECT screenplay_json FROM episodes WHERE id=?",
        (scope_id,),
    ).fetchone()
    if episode is None:
        raise ValueError("分镜完成凭证所属剧集不存在")
    raw = episode["screenplay_json"]
    if not raw:
        return None
    try:
        screenplay = EpisodeScreenplay.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001 - malformed current projection fails closed
        raise ValueError(f"当前剧本投影无法验证：{exc}") from exc
    return screenplay if screenplay.narrative_plan is not None else None


def _validate_revision_binding(
    *,
    kind: Literal["screenplay", "storyboard"],
    scope_id: str,
    artifact_id: str,
    input_fingerprint: str,
    contract_version: str,
    qa_profile_version: str,
    production_revision_id: str | None,
    allow_published: bool = False,
) -> None:
    if not production_revision_id:
        raise ValueError("叙事完成凭证必须绑定 production revision")
    row = get_conn().execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (production_revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError("完成凭证绑定的 production revision 不存在")
    valid_statuses = {"active", "published"} if allow_published else {"active"}
    if (
        row["kind"] != kind
        or row["episode_id"] != scope_id
        or row["status"] not in valid_statuses
        or row["working_artifact_id"] != artifact_id
        or (
            row["status"] == "published"
            and row["published_artifact_id"] != artifact_id
        )
    ):
        raise ValueError("完成凭证未绑定当前 working/published revision")
    exact_fields = {
        "input_fingerprint": input_fingerprint,
        "contract_version": contract_version,
        "qa_profile_version": qa_profile_version,
    }
    for field, supplied in exact_fields.items():
        if str(row[field] or "") != str(supplied or ""):
            raise ValueError(f"完成凭证 {field} 与 production revision 不匹配")


def _validate_narrative_certificate_authority(
    *,
    kind: Literal["screenplay", "storyboard"],
    scope_id: str,
    artifact: dict[str, Any],
    rows: list[Any],
    contract_version: str,
    qa_profile_version: str,
) -> bool:
    screenplay = _narrative_screenplay_for_artifact(
        kind=kind,
        scope_id=scope_id,
        artifact=artifact,
    )
    if screenplay is None:
        return False

    artifact_contract = str(artifact.get("contract_version") or "")
    if not contract_version or contract_version != artifact_contract:
        raise ValueError("叙事完成凭证必须精确匹配当前 Artifact contract_version")
    if qa_profile_version != _CURRENT_QA_PROFILE[kind]:
        raise ValueError("叙事完成凭证的 qa_profile_version 与当前运行契约不匹配")

    if kind == "screenplay":
        _required_exact_runtime_gate(
            rows,
            evaluator_name=_SCREENPLAY_GATE_EVALUATOR,
            evaluator_version=qa_profile_version,
        )
        return True

    try:
        from app.narrative_review import (
            COMPARATOR_PROMPT_VERSION,
            verify_review_chain_for_storyboard_artifact,
        )

        report_id = verify_review_chain_for_storyboard_artifact(
            episode_id=scope_id,
            screenplay=screenplay,
            storyboard_artifact=artifact,
        )
    except Exception as exc:  # noqa: BLE001 - release evidence boundary
        raise ValueError(f"分镜完成凭证的冷观众证据链无效：{exc}") from exc
    report_artifact = evidence_repository.get_artifact(report_id)
    comparator_version = str((report_artifact or {}).get("prompt_version") or "")
    if comparator_version != COMPARATOR_PROMPT_VERSION:
        raise ValueError("冷观众审读报告的 comparator 版本与当前运行契约不匹配")
    _required_exact_runtime_gate(
        rows,
        evaluator_name=_STORYBOARD_GATE_EVALUATOR,
        evaluator_version=contract_version,
    )
    _required_exact_runtime_gate(
        rows,
        evaluator_name=_NARRATIVE_REVIEW_EVALUATOR,
        evaluator_version=comparator_version,
    )
    return True


def issue_completion_certificate(
    *,
    kind: Literal["screenplay", "storyboard"],
    scope_id: str,
    artifact_id: str,
    artifact_hash: str,
    input_fingerprint: str = "",
    contract_version: str = "",
    qa_profile_version: str = "",
    evaluation_ids: list[str] | None = None,
    blockers: int = 0,
    must_fix_issues: int = 0,
    production_revision_id: str | None = None,
) -> CompletionCertificate:
    """签发完成凭证；QA 仅作同一 Artifact 的评分报告。"""
    if not artifact_id or not artifact_hash:
        raise ValueError("完成凭证必须绑定 artifact_id 与 artifact_hash")

    # 校验 artifact 存在且 hash 一致
    art = evidence_repository.get_artifact(artifact_id)
    if not art:
        raise ValueError(f"artifact 不存在: {artifact_id}")
    if (
        art.get("scope_type") != "episode"
        or art.get("scope_id") != scope_id
        or art.get("status") not in {"validated", "approved"}
    ):
        raise ValueError("完成凭证只能绑定当前集的可用 validated/approved Artifact")
    stored_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
    if stored_hash != artifact_hash:
        raise ValueError("artifact_hash 与存储内容不一致，拒绝签发凭证")
    if int(blockers or 0) > 0 or int(must_fix_issues or 0) > 0:
        raise ValueError("完成凭证不能带有 blocker 或 must-fix")
    evaluation_ids = list(evaluation_ids or [])
    rows = _load_evaluation_rows(evaluation_ids)
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(set(evaluation_ids)):
        raise ValueError("完成凭证引用了不存在的 Evaluation")
    if any(row["artifact_id"] != artifact_id for row in rows):
        raise ValueError("完成凭证引用了其他 Artifact 的 Evaluation")
    runtime_gates = [
        row for row in rows
        if row["evaluation_role"] in {"runtime_gate", "business_safety"}
    ]
    if any(
        not bool(row["hard_gate_passed"])
        or row["status"] in {"failed", "error"}
        for row in runtime_gates
    ):
        raise ValueError("引用的 runtime gate 尚未通过，拒绝签发完成凭证")
    if any(_row_has_blocking_issue(row) for row in runtime_gates):
        raise ValueError("runtime gate 仍含 blocker 或 must-fix，拒绝签发完成凭证")

    narrative_authority = _validate_narrative_certificate_authority(
        kind=kind,
        scope_id=scope_id,
        artifact=art,
        rows=rows,
        contract_version=contract_version,
        qa_profile_version=qa_profile_version,
    )
    if narrative_authority:
        _validate_revision_binding(
            kind=kind,
            scope_id=scope_id,
            artifact_id=artifact_id,
            input_fingerprint=input_fingerprint,
            contract_version=contract_version,
            qa_profile_version=qa_profile_version,
            production_revision_id=production_revision_id,
        )

    ensure_completion_certificates_table()
    certificate_id = new_id("cert")
    issued_at = now()
    cert = CompletionCertificate(
        certificate_id=certificate_id,
        kind=kind,
        scope_id=scope_id,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        input_fingerprint=input_fingerprint,
        contract_version=contract_version,
        qa_profile_version=qa_profile_version,
        evaluation_ids=evaluation_ids,
        blockers=max(0, int(blockers or 0)),
        must_fix_issues=max(0, int(must_fix_issues or 0)),
        issued_at=issued_at,
        production_revision_id=production_revision_id,
    )
    conn = get_conn()
    conn.execute(
        """INSERT INTO completion_certificates(
            id, kind, scope_id, artifact_id, artifact_hash, input_fingerprint,
            contract_version, qa_profile_version, evaluation_ids_json,
            blockers, must_fix_issues, production_revision_id, issued_at, payload_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            certificate_id, kind, scope_id, artifact_id, artifact_hash, input_fingerprint,
            contract_version, qa_profile_version,
            json.dumps(cert.evaluation_ids, ensure_ascii=False),
            cert.blockers, cert.must_fix_issues, production_revision_id, issued_at,
            json.dumps(cert.model_dump(mode="json"), ensure_ascii=False),
        ),
    )
    # 同步作为 Artifact 类型 completion_certificate
    try:
        from app.harness.types import EvidenceArtifact
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="completion_certificate",
                scope_type="episode",
                scope_id=scope_id,
                status="approved",
                trust_level="T3",
                content=cert.model_dump(mode="json"),
                parent_artifact_ids=[artifact_id],
                contract_version=contract_version or None,
            )
        )
    except Exception:  # noqa: BLE001
        pass
    conn.commit()
    record_certificate_issued(kind=kind, episode_id=scope_id, certificate_id=certificate_id)
    return cert


def get_completion_certificate(certificate_id: str, *, conn=None) -> CompletionCertificate | None:
    if conn is None:
        ensure_completion_certificates_table()
    db = conn or get_conn()
    row = db.execute(
        "SELECT * FROM completion_certificates WHERE id=?", (certificate_id,)
    ).fetchone()
    if not row:
        return None
    try:
        eval_ids = json.loads(row["evaluation_ids_json"] or "[]")
    except json.JSONDecodeError:
        eval_ids = []
    return CompletionCertificate(
        certificate_id=row["id"],
        kind=row["kind"],
        scope_id=row["scope_id"],
        artifact_id=row["artifact_id"],
        artifact_hash=row["artifact_hash"],
        input_fingerprint=row["input_fingerprint"] or "",
        contract_version=row["contract_version"] or "",
        qa_profile_version=row["qa_profile_version"] or "",
        evaluation_ids=eval_ids,
        blockers=int(row["blockers"] or 0),
        must_fix_issues=int(row["must_fix_issues"] or 0),
        issued_at=float(row["issued_at"] or 0),
        consumed_at=row["consumed_at"],
        production_revision_id=row["production_revision_id"],
    )


def completion_certificate_has_narrative_evidence(certificate_id: str | None) -> bool:
    """Detect an existing narrative release without trusting mutable episode flags."""
    if not certificate_id:
        return False
    cert = get_completion_certificate(str(certificate_id))
    if cert is None:
        return False
    try:
        if any(
            row["evaluator_name"] == _NARRATIVE_REVIEW_EVALUATOR
            for row in _load_evaluation_rows(cert.evaluation_ids)
        ):
            return True
        artifact = evidence_repository.get_artifact(cert.artifact_id) or {}
        return any(
            (parent := evidence_repository.get_artifact(str(parent_id))) is not None
            and parent.get("type") == "narrative_review_report"
            for parent_id in artifact.get("parent_artifact_ids") or []
        )
    except Exception:  # noqa: BLE001 - detection is only an additional downgrade marker
        return False


def verify_completion_certificate(
    certificate: CompletionCertificate | str,
    *,
    expected_artifact_id: str | None = None,
    expected_artifact_hash: str | None = None,
    expected_input_fingerprint: str | None = None,
    expected_contract_version: str | None = None,
    expected_qa_profile_version: str | None = None,
    expected_kind: Literal["screenplay", "storyboard"] | None = None,
    expected_scope_id: str | None = None,
    expected_production_revision_id: str | None = None,
    allow_consumed: bool = False,
) -> CompletionCertificate:
    # The database row is authoritative even when the caller passes a model.
    # Otherwise a caller could construct a look-alike certificate object and
    # bypass the immutable evaluation set recorded at issuance.
    certificate_id = (
        certificate if isinstance(certificate, str) else certificate.certificate_id
    )
    cert = get_completion_certificate(certificate_id)
    if cert is None:
        raise ValueError("完成凭证不存在")
    if cert.consumed_at is not None and not allow_consumed:
        raise ValueError("完成凭证已被消费，不可重放")
    if expected_kind and cert.kind != expected_kind:
        raise ValueError("完成凭证 kind 不匹配")
    if expected_scope_id and cert.scope_id != expected_scope_id:
        raise ValueError("完成凭证 scope_id 不匹配")
    if (
        expected_production_revision_id
        and cert.production_revision_id != expected_production_revision_id
    ):
        raise ValueError("完成凭证 production_revision_id 不匹配")
    if expected_artifact_id and cert.artifact_id != expected_artifact_id:
        raise ValueError("完成凭证 artifact_id 不匹配")
    if expected_artifact_hash and cert.artifact_hash != expected_artifact_hash:
        raise ValueError("完成凭证 artifact_hash 不匹配")
    if expected_input_fingerprint and cert.input_fingerprint != expected_input_fingerprint:
        raise ValueError("完成凭证 input_fingerprint 已变化")
    if expected_contract_version and cert.contract_version != expected_contract_version:
        raise ValueError("完成凭证 contract_version 已变化")
    if expected_qa_profile_version and cert.qa_profile_version != expected_qa_profile_version:
        raise ValueError("完成凭证 qa_profile_version 已变化")
    # 再次核对 artifact 当前 hash
    art = evidence_repository.get_artifact(cert.artifact_id)
    if not art:
        raise ValueError("凭证绑定的 artifact 已不存在")
    if (
        art.get("scope_type") != "episode"
        or art.get("scope_id") != cert.scope_id
        or art.get("status") not in {"validated", "approved"}
    ):
        raise ValueError("凭证绑定的 artifact 范围或当前状态已失效")
    current_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
    if current_hash != cert.artifact_hash:
        raise ValueError("凭证绑定的 artifact 内容已变化")
    if cert.blockers or cert.must_fix_issues:
        raise ValueError("完成凭证仍含 blocker 或 must-fix")

    rows = _load_evaluation_rows(cert.evaluation_ids)
    if len({row["id"] for row in rows}) != len(set(cert.evaluation_ids)):
        raise ValueError("完成凭证引用的 Evaluation 已缺失")
    if any(row["artifact_id"] != cert.artifact_id for row in rows):
        raise ValueError("完成凭证的 Evaluation 与 Artifact 绑定已漂移")
    narrative_authority = _validate_narrative_certificate_authority(
        kind=cert.kind,
        scope_id=cert.scope_id,
        artifact=art,
        rows=rows,
        contract_version=cert.contract_version,
        qa_profile_version=cert.qa_profile_version,
    )
    if narrative_authority:
        runtime_gates = [
            row for row in rows
            if row["evaluation_role"] in {"runtime_gate", "business_safety"}
        ]
        if any(
            not bool(row["runtime_blocking"])
            or not bool(row["hard_gate_passed"])
            or row["status"] != "passed"
            or _row_has_blocking_issue(row)
            for row in runtime_gates
        ):
            raise ValueError("完成凭证的 runtime gate 已失效")
        _validate_revision_binding(
            kind=cert.kind,
            scope_id=cert.scope_id,
            artifact_id=cert.artifact_id,
            input_fingerprint=cert.input_fingerprint,
            contract_version=cert.contract_version,
            qa_profile_version=cert.qa_profile_version,
            production_revision_id=cert.production_revision_id,
            allow_published=allow_consumed,
        )
    return cert


def verify_current_storyboard_completion_authority(
    *,
    episode: Any,
    current_storyboard_content: dict[str, Any],
) -> CompletionCertificate:
    """Verify the consumed certificate that authorizes paid narrative work.

    Live re-evaluation is deliberately absent: this function accepts only the
    immutable certificate/evaluation/review lineage for the exact published
    storyboard projection.
    """
    data = dict(episode)
    from app.production.screenplay_authority import resolve_downstream_screenplay

    screenplay_context = resolve_downstream_screenplay(
        str(data.get("id") or ""),
        conn=get_conn(),
    )
    screenplay = screenplay_context.screenplay
    if not screenplay_context.narrative_authority_required:
        raise ValueError("当前剧集不使用叙事权威凭证")

    certificate_id = str(data.get("storyboard_completion_certificate_id") or "")
    artifact_id = str(data.get("storyboard_artifact_id") or "")
    revision_id = str(data.get("storyboard_production_revision_id") or "")
    if not certificate_id or not artifact_id or not revision_id:
        raise ValueError("当前叙事分镜缺少完成凭证、Artifact 或 production revision")
    if (
        data.get("narrative_status") != "ready"
        or not data.get("narrative_review_artifact_id")
        or not data.get("narrative_calibration_artifact_id")
    ):
        raise ValueError("当前叙事分镜尚未取得有效冷观众审读与真人校准结论")

    cert = verify_completion_certificate(
        certificate_id,
        expected_kind="storyboard",
        expected_scope_id=str(data.get("id") or ""),
        expected_artifact_id=artifact_id,
        expected_production_revision_id=revision_id,
        allow_consumed=True,
    )
    if cert.consumed_at is None:
        raise ValueError("当前叙事完成凭证尚未被原子发布消费")
    artifact = evidence_repository.get_artifact(artifact_id)
    from app.narrative import storyboard_authority_projection

    if (
        artifact is None
        or storyboard_authority_projection(artifact.get("content") or {})
        != storyboard_authority_projection(current_storyboard_content)
    ):
        raise ValueError("当前 shots 投影与完成凭证绑定的 Storyboard Artifact 不一致")
    try:
        from app.narrative_review import verify_review_chain_for_storyboard_artifact

        report_id = verify_review_chain_for_storyboard_artifact(
            episode_id=str(data.get("id") or ""),
            screenplay=screenplay,
            storyboard_artifact=artifact,
        )
    except Exception as exc:  # noqa: BLE001 - paid boundary fails closed
        raise ValueError(f"当前叙事审读证据链已失效：{exc}") from exc
    if report_id != data.get("narrative_review_artifact_id"):
        raise ValueError("当前剧集的冷观众审读指针已漂移")
    try:
        from app.narrative_calibration import (
            assert_report_meets_current_calibration,
        )
        from app.schemas import NarrativeReviewReport

        review_artifact = evidence_repository.get_artifact(str(report_id))
        report = NarrativeReviewReport.model_validate(
            (review_artifact or {}).get("content") or {}
        )
        calibration = assert_report_meets_current_calibration(
            report,
            expected_calibration_artifact_id=str(
                data.get("narrative_calibration_artifact_id") or ""
            ),
        )
    except Exception as exc:  # noqa: BLE001 - paid authority fails closed
        raise ValueError(f"当前真人一次观看校准权威已失效：{exc}") from exc
    if calibration.artifact_id not in set(artifact.get("parent_artifact_ids") or []):
        raise ValueError("当前分镜 Artifact 未绑定其真人校准权威")
    return cert


def consume_completion_certificate(
    certificate_id: str,
    *,
    conn=None,
    commit: bool = True,
) -> CompletionCertificate:
    if conn is None:
        ensure_completion_certificates_table()
    db = conn or get_conn()
    cert = get_completion_certificate(certificate_id, conn=db)
    if cert is None:
        raise ValueError("完成凭证不存在")
    if cert.consumed_at is not None:
        raise ValueError("完成凭证已被消费")
    stamp = now()
    cursor = db.execute(
        "UPDATE completion_certificates SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
        (stamp, certificate_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("完成凭证消费冲突")
    if commit:
        db.commit()
    cert.consumed_at = stamp
    return cert


def assert_publish_has_certificate(
    *,
    kind: str,
    episode_id: str,
    certificate_id: str | None,
) -> None:
    if not certificate_id:
        record_publish_without_certificate(kind=kind, episode_id=episode_id)
        raise ValueError("禁止无完成凭证发布")
