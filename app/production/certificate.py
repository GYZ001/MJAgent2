"""Completion Certificate：绑定精确 Artifact hash 的完成凭证。"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.db import get_conn, new_id, now
from app.evidence import repository as evidence_repository
from app.production.metrics import record_certificate_issued, record_publish_without_certificate


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
    """签发完成凭证；剧本必须由同一 Artifact 的只读 QA runtime gate 背书。"""
    if not artifact_id or not artifact_hash:
        raise ValueError("完成凭证必须绑定 artifact_id 与 artifact_hash")

    # 校验 artifact 存在且 hash 一致
    art = evidence_repository.get_artifact(artifact_id)
    if not art:
        raise ValueError(f"artifact 不存在: {artifact_id}")
    stored_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
    if stored_hash != artifact_hash:
        raise ValueError("artifact_hash 与存储内容不一致，拒绝签发凭证")
    evaluation_ids = list(evaluation_ids or [])
    if kind == "screenplay":
        if blockers or must_fix_issues:
            raise ValueError("剧本仍有 blocker / must-fix，拒绝签发完成凭证")
        if not evaluation_ids:
            raise ValueError("剧本完成凭证必须绑定 QA Evaluation")
        marks = ",".join("?" for _ in evaluation_ids)
        rows = get_conn().execute(
            f"""SELECT id,artifact_id,status,hard_gate_passed,evaluation_role,runtime_blocking
                  FROM evaluations WHERE id IN ({marks})""",
            evaluation_ids,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        if len(by_id) != len(set(evaluation_ids)):
            raise ValueError("剧本完成凭证引用了不存在的 QA Evaluation")
        runtime_gate_passed = any(
            row["artifact_id"] == artifact_id
            and row["status"] == "passed"
            and bool(row["hard_gate_passed"])
            and row["evaluation_role"] == "runtime_gate"
            and bool(row["runtime_blocking"])
            for row in rows
        )
        if not runtime_gate_passed:
            raise ValueError("当前 Artifact 没有通过只读 QA runtime gate")

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


def get_completion_certificate(certificate_id: str) -> CompletionCertificate | None:
    ensure_completion_certificates_table()
    row = get_conn().execute(
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


def verify_completion_certificate(
    certificate: CompletionCertificate | str,
    *,
    expected_artifact_id: str | None = None,
    expected_artifact_hash: str | None = None,
    expected_input_fingerprint: str | None = None,
    expected_contract_version: str | None = None,
    expected_qa_profile_version: str | None = None,
) -> CompletionCertificate:
    cert = (
        get_completion_certificate(certificate)
        if isinstance(certificate, str)
        else certificate
    )
    if cert is None:
        raise ValueError("完成凭证不存在")
    if cert.consumed_at is not None:
        raise ValueError("完成凭证已被消费，不可重放")
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
    current_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
    if current_hash != cert.artifact_hash:
        raise ValueError("凭证绑定的 artifact 内容已变化")
    return cert


def consume_completion_certificate(certificate_id: str) -> CompletionCertificate:
    ensure_completion_certificates_table()
    conn = get_conn()
    cert = get_completion_certificate(certificate_id)
    if cert is None:
        raise ValueError("完成凭证不存在")
    if cert.consumed_at is not None:
        raise ValueError("完成凭证已被消费")
    stamp = now()
    conn.execute(
        "UPDATE completion_certificates SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
        (stamp, certificate_id),
    )
    if conn.total_changes == 0:
        raise ValueError("完成凭证消费冲突")
    conn.commit()
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
