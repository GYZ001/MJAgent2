"""Production Revision：以 revision 为粒度冻结一次 Baseline 生成配额。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db import get_conn, new_id, now

Kind = Literal["screenplay", "storyboard"]


@dataclass(frozen=True)
class ScreenplayResumeEligibility:
    mode: Literal[
        "none", "baseline", "baseline_rebuild", "finalize", "complete",
    ]
    label: str
    revision_id: str | None
    revision_action: Literal["none", "reuse", "rebase"]
    working_artifact_id: str | None
    working_compatible: bool
    reusable_checkpoint: dict[str, Any]
    reason_code: str
    reason: str

    @property
    def resumable(self) -> bool:
        return self.mode in {"baseline", "baseline_rebuild", "finalize"}

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "resumable": self.resumable}


class ProductionRevisionOwnershipLost(RuntimeError):
    """A traced screenplay worker no longer owns the episode write lease."""


def _assert_screenplay_write_owner(
    db,
    *,
    episode_id: str,
    kind: str,
    revision_id: str | None = None,
    allow_current_published: bool = False,
) -> None:
    if kind != "screenplay":
        return
    from app.observability.tracing import current_trace

    run_id = current_trace().run_id
    episode = db.execute(
        "SELECT active_screenplay_run_id,screenplay_production_revision_id "
        "FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not run_id:
        from app.evidence import repository as evidence_repository

        active = (
            evidence_repository.get_active_scoped_run(
                episode["active_screenplay_run_id"],
                workflow_type="screenplay",
                scope_type="episode",
                scope_id=episode_id,
                conn=db,
            )
            if episode
            else None
        )
        if active:
            raise ProductionRevisionOwnershipLost(
                f"manual screenplay write conflicts with active run {active['id']}"
            )
        return
    if episode and episode["active_screenplay_run_id"] == run_id:
        return
    if (
        allow_current_published
        and episode
        and revision_id
        and not episode["active_screenplay_run_id"]
        and episode["screenplay_production_revision_id"] == revision_id
    ):
        revision = db.execute(
            "SELECT status FROM production_revisions WHERE id=?",
            (revision_id,),
        ).fetchone()
        if revision and revision["status"] == "published":
            return
    raise ProductionRevisionOwnershipLost(
        f"screenplay worker {run_id} no longer owns episode {episode_id}"
    )


class ProductionRevision(BaseModel):
    id: str
    episode_id: str
    kind: Kind
    status: str = "active"
    baseline_generation_count: int = 0
    first_evaluation_id: str | None = None
    baseline_artifact_id: str | None = None
    working_artifact_id: str | None = None
    published_artifact_id: str | None = None
    grant_id: str | None = None
    input_fingerprint: str = ""
    contract_version: str = ""
    qa_profile_version: str = ""
    checkpoint_json: dict[str, Any] = Field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def first_evaluation_done(self) -> bool:
        return bool(self.first_evaluation_id)

    @property
    def baseline_done(self) -> bool:
        return self.baseline_generation_count >= 1


def ensure_production_revisions_table(conn=None) -> None:
    db = conn or get_conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS production_revisions (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            baseline_generation_count INTEGER NOT NULL DEFAULT 0,
            first_evaluation_id TEXT,
            baseline_artifact_id TEXT,
            working_artifact_id TEXT,
            published_artifact_id TEXT,
            grant_id TEXT,
            input_fingerprint TEXT NOT NULL DEFAULT '',
            contract_version TEXT NOT NULL DEFAULT '',
            qa_profile_version TEXT NOT NULL DEFAULT '',
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(episode_id, kind, id)
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_production_revisions_episode_kind "
        "ON production_revisions(episode_id, kind, updated_at DESC)"
    )
    db.commit()


def _row_to_revision(row) -> ProductionRevision | None:
    if row is None:
        return None
    data = dict(row)
    raw = data.get("checkpoint_json") or "{}"
    try:
        checkpoint = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        checkpoint = {}
    return ProductionRevision(
        id=data["id"],
        episode_id=data["episode_id"],
        kind=data["kind"],
        status=data.get("status") or "active",
        baseline_generation_count=int(data.get("baseline_generation_count") or 0),
        first_evaluation_id=data.get("first_evaluation_id"),
        baseline_artifact_id=data.get("baseline_artifact_id"),
        working_artifact_id=data.get("working_artifact_id"),
        published_artifact_id=data.get("published_artifact_id"),
        grant_id=data.get("grant_id"),
        input_fingerprint=data.get("input_fingerprint") or "",
        contract_version=data.get("contract_version") or "",
        qa_profile_version=data.get("qa_profile_version") or "",
        checkpoint_json=checkpoint if isinstance(checkpoint, dict) else {},
        created_at=float(data.get("created_at") or 0),
        updated_at=float(data.get("updated_at") or 0),
    )


def get_production_revision(revision_id: str) -> ProductionRevision | None:
    ensure_production_revisions_table()
    row = get_conn().execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    return _row_to_revision(row)


def get_active_production_revision(episode_id: str, kind: Kind) -> ProductionRevision | None:
    ensure_production_revisions_table()
    row = get_conn().execute(
        "SELECT * FROM production_revisions WHERE episode_id=? AND kind=? AND status='active' "
        "ORDER BY updated_at DESC LIMIT 1",
        (episode_id, kind),
    ).fetchone()
    return _row_to_revision(row)


def screenplay_scene_shard_expected_hashes(
    revision: ProductionRevision | None,
) -> tuple[str, str]:
    checkpoint = dict(revision.checkpoint_json or {}) if revision else {}
    return (
        str(checkpoint.get("blueprint_hash") or ""),
        str(checkpoint.get("identity_registry_hash") or ""),
    )


def _artifact_json(row: dict[str, Any]) -> dict[str, Any] | None:
    content = row.get("content")
    if isinstance(content, dict):
        return content
    try:
        value = json.loads(row.get("content_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _artifact_hash_is_valid(row: dict[str, Any], content: dict[str, Any]) -> bool:
    from app.evidence import repository as evidence_repository

    recorded = str(row.get("content_hash") or "")
    return bool(recorded and recorded == evidence_repository.content_hash(content))


def _structured_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _artifact_parent_ids(row: dict[str, Any]) -> set[str] | None:
    try:
        values = json.loads(row.get("parent_artifact_ids_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(values, list):
        return None
    return {str(value) for value in values if str(value)}


def _screenplay_checkpoint_compatibility(
    episode_id: str,
    revision: ProductionRevision,
    *,
    conn,
) -> tuple[dict[str, Any], dict[str, int], bool]:
    from app.narrative_blueprint import BLUEPRINT_VERSION, NarrativeBlueprint
    from app.screenplay_ir import IR_VERSION, ScreenplayGenerationIR
    from app.screenplay_scene_shards import (
        SCREENPLAY_ENVELOPE_VERSION,
        SCREENPLAY_MERGED_IR_VERSION,
        blueprint_content_hash,
        screenplay_envelope_artifact_compatibility,
        screenplay_scene_shard_artifact_compatibility,
    )

    checkpoint = dict(revision.checkpoint_json or {})
    shard_rows = [
        item for item in checkpoint.get("shards") or []
        if isinstance(item, dict)
    ]
    artifact_ids = {
        str(value)
        for value in [
            checkpoint.get("blueprint_artifact_id"),
            checkpoint.get("identity_artifact_id"),
            checkpoint.get("envelope_artifact_id"),
            checkpoint.get("merged_ir_artifact_id"),
            *(item.get("normalized_artifact_id") for item in shard_rows),
        ]
        if str(value or "").strip()
    }
    artifacts = {
        str(row["id"]): dict(row)
        for row in conn.execute(
            "SELECT id,type,scope_type,scope_id,status,contract_version,"
            "content_json,content_hash,parent_artifact_ids_json,model_snapshot_json "
            "FROM artifacts WHERE id IN ("
            + ",".join("?" for _ in artifact_ids)
            + ")",
            sorted(artifact_ids),
        ).fetchall()
    } if artifact_ids else {}
    recovered_shards = [
        dict(row)
        for row in conn.execute(
            "SELECT id,type,scope_type,scope_id,status,contract_version,"
            "content_json,content_hash,parent_artifact_ids_json,model_snapshot_json "
            "FROM artifacts WHERE scope_type='episode' AND scope_id=? "
            "AND type='screenplay_scene_shard' AND status='validated'",
            (episode_id,),
        ).fetchall()
    ]
    raw_parent_ids = {
        parent_id
        for row in [*artifacts.values(), *recovered_shards]
        for parent_id in (_artifact_parent_ids(row) or set())
    }
    if raw_parent_ids:
        artifacts.update({
            str(row["id"]): dict(row)
            for row in conn.execute(
                "SELECT id,type,scope_type,scope_id,status,contract_version,"
                "content_json,content_hash,parent_artifact_ids_json,model_snapshot_json "
                "FROM artifacts WHERE id IN ("
                + ",".join("?" for _ in raw_parent_ids)
                + ")",
                sorted(raw_parent_ids),
            ).fetchall()
        })

    def base_artifact(
        artifact_id: str,
        *,
        artifact_type: str,
        contract_version: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        row = artifacts.get(artifact_id)
        if (
            row is None
            or row.get("type") != artifact_type
            or row.get("scope_type") != "episode"
            or row.get("scope_id") != episode_id
            or row.get("status") != "validated"
            or str(row.get("contract_version") or "") != contract_version
        ):
            return None
        content = _artifact_json(row)
        if content is None or not _artifact_hash_is_valid(row, content):
            return None
        return row, content

    reusable: dict[str, Any] = {}
    blueprint_hash = str(checkpoint.get("blueprint_hash") or "")
    identity_hash = str(checkpoint.get("identity_registry_hash") or "")

    blueprint_id = str(checkpoint.get("blueprint_artifact_id") or "")
    blueprint_pair = base_artifact(
        blueprint_id,
        artifact_type="screenplay_narrative_blueprint",
        contract_version=BLUEPRINT_VERSION,
    )
    if blueprint_pair and blueprint_hash:
        try:
            blueprint = NarrativeBlueprint.model_validate(blueprint_pair[1])
            blueprint_valid = blueprint_content_hash(blueprint) == blueprint_hash
        except (TypeError, ValueError):
            blueprint_valid = False
        if blueprint_valid:
            reusable["blueprint_artifact_id"] = blueprint_id
            reusable["blueprint_hash"] = blueprint_hash

    identity_id = str(checkpoint.get("identity_artifact_id") or "")
    identity_pair = base_artifact(
        identity_id,
        artifact_type="screenplay_identity_registry",
        contract_version="screenplay-identity-registry.v1",
    )
    if identity_pair:
        identity_content = identity_pair[1]
        if (
            identity_content.get("contract_version")
            == "screenplay-identity-registry.v1"
            and identity_hash
            and identity_content.get("identity_registry_hash") == identity_hash
            and isinstance(identity_content.get("identities"), list)
            and _structured_hash(identity_content["identities"]) == identity_hash
        ):
            reusable["identity_artifact_id"] = identity_id
            reusable["identity_registry_hash"] = identity_hash

    authority_compatible = bool(
        reusable.get("blueprint_artifact_id")
        and reusable.get("identity_artifact_id")
    )
    envelope_id = str(checkpoint.get("envelope_artifact_id") or "")
    envelope_pair = base_artifact(
        envelope_id,
        artifact_type="screenplay_envelope",
        contract_version=SCREENPLAY_ENVELOPE_VERSION,
    )
    if authority_compatible and envelope_pair:
        envelope_row = envelope_pair[0]
        normalized_parents = _artifact_parent_ids(envelope_row) or set()
        raw_envelope = (
            artifacts.get(next(iter(normalized_parents)))
            if len(normalized_parents) == 1
            else None
        )
        envelope_valid, _reason = screenplay_envelope_artifact_compatibility(
            envelope_row,
            expected_blueprint_hash=blueprint_hash,
            expected_identity_registry_hash=identity_hash,
            raw_artifact=raw_envelope,
            expected_authority_artifact_ids={blueprint_id, identity_id},
        )
        if envelope_valid:
            reusable["envelope_artifact_id"] = envelope_id

    validated_shards: list[dict[str, Any]] = []
    for item in shard_rows:
        artifact_id = str(item.get("normalized_artifact_id") or "")
        candidates = []
        if artifacts.get(artifact_id) is not None:
            candidates.append(artifacts[artifact_id])
        candidates.extend(
            row for row in recovered_shards
            if str(row.get("id") or "") != artifact_id
        )
        for row in candidates:
            compatible = False
            content = _artifact_json(row)
            if authority_compatible and content is not None:
                normalized_parents = _artifact_parent_ids(row) or set()
                raw_shard = (
                    artifacts.get(next(iter(normalized_parents)))
                    if len(normalized_parents) == 1
                    else None
                )
                compatible, _reason = screenplay_scene_shard_artifact_compatibility(
                    row,
                    expected_blueprint_hash=blueprint_hash,
                    expected_identity_registry_hash=identity_hash,
                    expected_generation_scaffold_hash=str(
                        item.get("generation_scaffold_hash") or ""
                    ),
                    raw_artifact=raw_shard,
                    expected_authority_artifact_ids={blueprint_id, identity_id},
                )
                compatible = bool(
                    compatible
                    and all(
                        not str(item.get(key) or "")
                        or str(item.get(key) or "") == str(content.get(key) or "")
                        for key in (
                            "shard_id", "source_hash", "boundary_hash",
                            "generation_scaffold_hash",
                        )
                    )
                    and (
                        artifact_id == str(row.get("id") or "")
                        or (
                            all(str(item.get(key) or "") for key in (
                                "shard_id", "source_hash", "boundary_hash",
                                "generation_scaffold_hash",
                            ))
                            and all(
                                str(item.get(key) or "") == str(content.get(key) or "")
                                for key in (
                                    "shard_id", "source_hash", "boundary_hash",
                                    "generation_scaffold_hash",
                                )
                            )
                        )
                    )
                )
            if compatible:
                recovered = dict(item)
                recovered["normalized_artifact_id"] = str(row["id"])
                recovered["status"] = "validated"
                validated_shards.append(recovered)
                break
    if validated_shards:
        reusable["shards"] = validated_shards

    merged_id = str(checkpoint.get("merged_ir_artifact_id") or "")
    merged_pair = base_artifact(
        merged_id,
        artifact_type="screenplay_generation_ir_merged",
        contract_version=SCREENPLAY_MERGED_IR_VERSION,
    )
    if (
        authority_compatible
        and reusable.get("envelope_artifact_id")
        and merged_pair
    ):
        row, content = merged_pair
        try:
            parent_ids = set(json.loads(row.get("parent_artifact_ids_json") or "[]"))
            snapshot = json.loads(row.get("model_snapshot_json") or "{}")
            ScreenplayGenerationIR.model_validate(content)
            validated_shard_ids = {
                str(item.get("normalized_artifact_id") or "")
                for item in validated_shards
                if str(item.get("normalized_artifact_id") or "")
            }
            expected_parent_ids = {
                blueprint_id,
                identity_id,
                envelope_id,
                *validated_shard_ids,
            }
            merged_valid = (
                content.get("format_version") == IR_VERSION
                and snapshot.get("blueprint_hash") == blueprint_hash
                and snapshot.get("identity_registry_hash") == identity_hash
                and bool(shard_rows)
                and len(validated_shards) == len(shard_rows)
                and len(validated_shard_ids) == len(shard_rows)
                and parent_ids == expected_parent_ids
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            merged_valid = False
        if merged_valid:
            reusable["merged_ir_artifact_id"] = merged_id

    def shard_is_validated(item: dict[str, Any]) -> bool:
        artifact_id = str(item.get("normalized_artifact_id") or "")
        return any(
            (
                artifact_id
                and str(candidate.get("normalized_artifact_id") or "")
                == artifact_id
            )
            or all(
                str(item.get(key) or "") == str(candidate.get(key) or "")
                for key in (
                    "shard_id", "source_hash", "boundary_hash",
                    "generation_scaffold_hash",
                )
            )
            for candidate in validated_shards
        )

    progress = {
        "total": len(shard_rows),
        "validated": len(validated_shards),
        "running": sum(
            item.get("status") == "running" and not shard_is_validated(item)
            for item in shard_rows
        ),
        "failed": sum(
            item.get("status") == "failed" and not shard_is_validated(item)
            for item in shard_rows
        ),
    }
    incompatible_checkpoint = any((
        bool(blueprint_id and not reusable.get("blueprint_artifact_id")),
        bool(identity_id and not reusable.get("identity_artifact_id")),
        bool(envelope_id and not reusable.get("envelope_artifact_id")),
        bool(merged_id and not reusable.get("merged_ir_artifact_id")),
        any(
            item.get("normalized_artifact_id") and not shard_is_validated(item)
            for item in shard_rows
        ),
    ))
    return reusable, progress, incompatible_checkpoint


def resolve_screenplay_resume_eligibility(
    episode_id: str,
    *,
    revision: ProductionRevision | None = None,
    conn=None,
) -> ScreenplayResumeEligibility:
    """Resolve one executable recovery mode from persisted structured evidence."""
    from app.errors import ArtifactNeedsRebuildError
    from app.harness.contracts import get_contract
    from app.production.patch import screenplay_from_artifact_record

    db = conn or get_conn()
    rev = revision or _row_to_revision(db.execute(
        "SELECT * FROM production_revisions "
        "WHERE episode_id=? AND kind='screenplay' AND status='active' "
        "ORDER BY updated_at DESC LIMIT 1",
        (episode_id,),
    ).fetchone())
    if rev is None:
        episode = db.execute(
            "SELECT screenplay_status,active_screenplay_run_id "
            "FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        interrupted_baseline = bool(
            episode
            and episode["active_screenplay_run_id"]
            and episode["screenplay_status"] in {
                "queued", "running", "repairing",
            }
        )
        return ScreenplayResumeEligibility(
            mode="baseline" if interrupted_baseline else "none",
            label=(
                "恢复首版生成"
                if interrupted_baseline
                else "无可恢复剧本"
            ),
            revision_id=None,
            revision_action="none",
            working_artifact_id=None,
            working_compatible=False,
            reusable_checkpoint={},
            reason_code=(
                "INTERRUPTED_BASELINE"
                if interrupted_baseline
                else "NO_ACTIVE_REVISION"
            ),
            reason=(
                "持久化运行在 revision 建立前中断"
                if interrupted_baseline
                else "没有 active screenplay revision"
            ),
        )
    if rev.published_artifact_id:
        return ScreenplayResumeEligibility(
            mode="complete",
            label="已完成",
            revision_id=rev.id,
            revision_action="none",
            working_artifact_id=rev.working_artifact_id,
            working_compatible=True,
            reusable_checkpoint={},
            reason_code="PUBLISHED",
            reason="revision 已发布",
        )

    reusable, progress, incompatible_checkpoint = _screenplay_checkpoint_compatibility(
        episode_id,
        rev,
        conn=db,
    )
    reusable["shard_progress"] = progress
    current_baseline_checkpoint = bool(
        reusable.get("blueprint_artifact_id")
        or reusable.get("envelope_artifact_id")
        or reusable.get("shards")
        or reusable.get("merged_ir_artifact_id")
    )
    rebuild_transition = str(
        (rev.checkpoint_json or {}).get("resume_mode") or ""
    ) == "baseline_rebuild"
    working_id = str(rev.working_artifact_id or "")
    if rev.baseline_done and working_id:
        row = db.execute(
            "SELECT id,type,scope_type,scope_id,status,contract_version,"
            "content_json,content_hash,parent_artifact_ids_json "
            "FROM artifacts WHERE id=?",
            (working_id,),
        ).fetchone()
        artifact = dict(row) if row else None
        current_contract = get_contract("screenplay").version
        known_incompatibility = (
            artifact is None
            or artifact.get("type") != "screenplay_document"
            or artifact.get("scope_type") != "episode"
            or artifact.get("scope_id") != episode_id
            or artifact.get("status") not in {"candidate", "validated", "approved"}
            or str(artifact.get("contract_version") or "") != current_contract
        )
        if artifact is not None and not known_incompatibility:
            content = _artifact_json(artifact)
            known_incompatibility = bool(
                content is None or not _artifact_hash_is_valid(artifact, content)
            )
        if not known_incompatibility:
            try:
                screenplay_from_artifact_record({
                    **artifact,
                    "content": content,
                    "parent_artifact_ids": list(
                        _artifact_parent_ids(artifact) or set()
                    ),
                })
            except ArtifactNeedsRebuildError as exc:
                known_incompatibility = True
                incompatibility_reason = str(exc)
            except Exception as exc:  # fail closed on unclassified validation errors
                return ScreenplayResumeEligibility(
                    mode="none",
                    label="恢复资格校验失败",
                    revision_id=rev.id,
                    revision_action="none",
                    working_artifact_id=working_id,
                    working_compatible=False,
                    reusable_checkpoint=reusable,
                    reason_code="ELIGIBILITY_CHECK_FAILED",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            else:
                incompatibility_reason = ""
        else:
            incompatibility_reason = "working Artifact 不符合当前类型、状态、合同或内容哈希"
        if not known_incompatibility:
            from app.production.screenplay_authority import (
                SCREENPLAY_QA_PROFILE_VERSION,
            )

            profile_requires_revalidation = (
                str(rev.qa_profile_version or "")
                != SCREENPLAY_QA_PROFILE_VERSION
            )
            if profile_requires_revalidation:
                return ScreenplayResumeEligibility(
                    mode="finalize",
                    label="按新门禁重验并发布",
                    revision_id=rev.id,
                    revision_action="reuse",
                    working_artifact_id=working_id,
                    working_compatible=False,
                    reusable_checkpoint=reusable,
                    reason_code="WORKING_REVALIDATION_REQUIRED",
                    reason=(
                        "working Artifact 的 QA/validator 语义版本已过期；"
                        "worker 必须先按当前确定性门禁只读重验，失败时只能从"
                        "可信不可变上游重建"
                    ),
                )
            return ScreenplayResumeEligibility(
                mode="finalize",
                label="继续校验并发布",
                revision_id=rev.id,
                revision_action="reuse",
                working_artifact_id=working_id,
                working_compatible=True,
                reusable_checkpoint=reusable,
                reason_code="WORKING_COMPATIBLE",
                reason="working Artifact 通过当前合同与 lineage 校验",
            )
        return ScreenplayResumeEligibility(
            mode="baseline_rebuild",
            label="按新合同重建剧本",
            revision_id=rev.id,
            revision_action="rebase",
            working_artifact_id=working_id,
            working_compatible=False,
            reusable_checkpoint=reusable,
            reason_code="WORKING_INCOMPATIBLE",
            reason=incompatibility_reason,
        )

    if incompatible_checkpoint:
        return ScreenplayResumeEligibility(
            mode="baseline_rebuild",
            label="按新合同重建剧本",
            revision_id=rev.id,
            revision_action="rebase",
            working_artifact_id=None,
            working_compatible=False,
            reusable_checkpoint=reusable,
            reason_code="MIXED_CHECKPOINT_REQUIRES_REBUILD",
            reason="pre-Document checkpoint 混合了当前与不兼容合同产物",
        )
    if rebuild_transition:
        return ScreenplayResumeEligibility(
            mode="baseline_rebuild",
            label="按新合同重建剧本",
            revision_id=rev.id,
            revision_action="reuse",
            working_artifact_id=None,
            working_compatible=False,
            reusable_checkpoint=reusable,
            reason_code="BASELINE_REBUILD_TRANSITION",
            reason="新 revision 已进入当前合同重建流程",
        )
    if current_baseline_checkpoint:
        return ScreenplayResumeEligibility(
            mode="baseline",
            label="继续当前首版生成",
            revision_id=rev.id,
            revision_action="reuse",
            working_artifact_id=None,
            working_compatible=False,
            reusable_checkpoint=reusable,
            reason_code="CURRENT_CHECKPOINT",
            reason="存在当前合同兼容的 pre-Document checkpoint",
        )
    current_contract = get_contract("screenplay").version
    if not rev.baseline_done:
        episode = db.execute(
            "SELECT screenplay_status,active_screenplay_run_id "
            "FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        interrupted_baseline = bool(
            episode
            and episode["active_screenplay_run_id"]
            and episode["screenplay_status"] in {
                "queued", "running", "repairing",
            }
        )
        metadata_incompatible = bool(
            rev.contract_version
            and rev.contract_version != current_contract
        )
        if not interrupted_baseline:
            return ScreenplayResumeEligibility(
                mode="none",
                label="无可恢复剧本",
                revision_id=rev.id,
                revision_action="none",
                working_artifact_id=None,
                working_compatible=False,
                reusable_checkpoint=reusable,
                reason_code="BASELINE_NOT_STARTED",
                reason="active revision 尚未生成可复用 checkpoint",
            )
        return ScreenplayResumeEligibility(
            mode="baseline_rebuild" if metadata_incompatible else "baseline",
            label=("按新合同重建剧本" if metadata_incompatible else "继续首版生成"),
            revision_id=rev.id,
            revision_action="rebase" if metadata_incompatible else "reuse",
            working_artifact_id=None,
            working_compatible=False,
            reusable_checkpoint=reusable,
            reason_code=(
                "REVISION_CONTRACT_INCOMPATIBLE"
                if metadata_incompatible
                else "BASELINE_NOT_STARTED"
            ),
            reason=(
                "active revision 元数据不兼容当前剧本合同"
                if metadata_incompatible
                else "active revision 尚未生成完整 Document"
            ),
        )
    return ScreenplayResumeEligibility(
        mode="none",
        label="无可恢复剧本",
        revision_id=rev.id,
        revision_action="none",
        working_artifact_id=working_id or None,
        working_compatible=False,
        reusable_checkpoint=reusable,
        reason_code="NO_COMPATIBLE_RECOVERY_POINT",
        reason="没有当前合同兼容的 working Artifact 或 pre-Document checkpoint",
    )


def rebase_screenplay_revision_for_resume(
    eligibility: ScreenplayResumeEligibility,
    *,
    conn,
) -> ProductionRevision:
    """CAS-supersede an incompatible working revision and create a clean baseline."""
    if (
        eligibility.mode != "baseline_rebuild"
        or eligibility.revision_action != "rebase"
        or not eligibility.revision_id
    ):
        raise ValueError("screenplay resume rebase 缺少结构化资格")
    old = conn.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (eligibility.revision_id,),
    ).fetchone()
    if (
        old is None
        or old["status"] != "active"
        or old["episode_id"] is None
        or str(old["working_artifact_id"] or "")
        != str(eligibility.working_artifact_id or "")
    ):
        raise ValueError("screenplay resume rebase 发生 CAS 冲突")
    stamp = now()
    cursor = conn.execute(
        "UPDATE production_revisions SET status='superseded',updated_at=? "
        "WHERE id=? AND status='active' "
        "AND COALESCE(working_artifact_id,'')=COALESCE(?, '')",
        (stamp, eligibility.revision_id, eligibility.working_artifact_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("screenplay resume rebase 发生 CAS 冲突")
    if eligibility.working_artifact_id:
        conn.execute(
            "UPDATE artifacts SET status='stale',stale_reason=? "
            "WHERE id=? AND status NOT IN ('rejected','stale','superseded')",
            (
                f"[{eligibility.reason_code}] {eligibility.reason}",
                eligibility.working_artifact_id,
            ),
        )
    reused_inputs: dict[str, Any] = {}
    identity_artifact_id = eligibility.reusable_checkpoint.get(
        "identity_artifact_id"
    )
    identity_registry_hash = eligibility.reusable_checkpoint.get(
        "identity_registry_hash"
    )
    if identity_artifact_id and identity_registry_hash:
        reused_inputs["identity_registry"] = {
            "artifact_id": identity_artifact_id,
            "identity_registry_hash": identity_registry_hash,
        }
    checkpoint = {
        "phase": "BLUEPRINT_GENERATION",
        "resume_mode": "baseline_rebuild",
        "source_revision_id": eligibility.revision_id,
        "reused_inputs": reused_inputs,
        "yield_reason": "baseline_rebuild_transition",
    }
    revision_id = new_id("rev")
    conn.execute(
        """INSERT INTO production_revisions(
            id,episode_id,kind,status,baseline_generation_count,
            input_fingerprint,contract_version,qa_profile_version,grant_id,
            checkpoint_json,created_at,updated_at
        ) VALUES(?,?,?,'active',0,'','','',NULL,?,?,?)""",
        (
            revision_id,
            old["episode_id"],
            old["kind"],
            json.dumps(checkpoint, ensure_ascii=False),
            stamp,
            stamp,
        ),
    )
    conn.execute(
        "UPDATE episodes SET working_screenplay_artifact_id=NULL "
        "WHERE id=? AND COALESCE(working_screenplay_artifact_id,'')=COALESCE(?, '')",
        (old["episode_id"], eligibility.working_artifact_id),
    )
    revision = _row_to_revision(conn.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone())
    if revision is None:
        raise RuntimeError("screenplay resume rebase 未创建新 revision")
    return revision


def rebind_input_fingerprint(
    revision_id: str,
    *,
    input_fingerprint: str,
    expected_working_artifact_id: str,
    conn=None,
    commit: bool = True,
) -> ProductionRevision:
    """CAS-bind an active, QA-verified working revision to current authority."""
    if not input_fingerprint or not expected_working_artifact_id:
        raise ValueError("revision 指纹重绑缺少权威指纹或 working artifact")
    db = conn or get_conn()
    owner_row = db.execute(
        "SELECT episode_id,kind FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()
    if owner_row is None:
        raise ValueError("revision 指纹重绑的记录不存在")
    _assert_screenplay_write_owner(
        db,
        episode_id=owner_row["episode_id"],
        kind=owner_row["kind"],
        revision_id=revision_id,
    )
    cursor = db.execute(
        """UPDATE production_revisions
              SET input_fingerprint=?, updated_at=?
            WHERE id=? AND status='active'
              AND working_artifact_id=?
              AND published_artifact_id IS NULL""",
        (
            input_fingerprint,
            now(),
            revision_id,
            expected_working_artifact_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("revision 指纹重绑发生 CAS 冲突")
    if commit:
        db.commit()
    row = db.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()
    revision = _row_to_revision(row)
    if revision is None:
        raise ValueError("revision 指纹重绑后记录不存在")
    return revision


def bind_unpublished_revision_metadata(
    revision_id: str,
    *,
    input_fingerprint: str,
    contract_version: str,
    qa_profile_version: str,
    conn=None,
    commit: bool = True,
) -> ProductionRevision:
    """Bind metadata omitted when a resumable revision was first initialized.

    Storyboard revisions are created before a final board exists, so their
    content fingerprint is not available at initialization. This CAS only
    fills blank values (or accepts exact matches) on an unpublished active
    revision; conflicting non-empty metadata still fails closed.
    """
    if not input_fingerprint or not contract_version or not qa_profile_version:
        raise ValueError("revision 元数据绑定缺少指纹、契约或评分版本")
    db = conn or get_conn()
    row = db.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError("revision 元数据绑定的记录不存在")
    _assert_screenplay_write_owner(
        db,
        episode_id=row["episode_id"],
        kind=row["kind"],
        revision_id=revision_id,
    )
    if row["status"] != "active" or row["published_artifact_id"]:
        raise ValueError("只能绑定尚未发布的 active revision")
    requested = {
        "input_fingerprint": input_fingerprint,
        "contract_version": contract_version,
        "qa_profile_version": qa_profile_version,
    }
    for field, value in requested.items():
        current = str(row[field] or "")
        if current and current != value:
            raise ValueError(f"revision {field} 已绑定其他版本")
    cursor = db.execute(
        """UPDATE production_revisions
              SET input_fingerprint=?,contract_version=?,qa_profile_version=?,
                  updated_at=?
            WHERE id=? AND status='active' AND published_artifact_id IS NULL
              AND input_fingerprint IN ('',?)
              AND contract_version IN ('',?)
              AND qa_profile_version IN ('',?)""",
        (
            input_fingerprint,
            contract_version,
            qa_profile_version,
            now(),
            revision_id,
            input_fingerprint,
            contract_version,
            qa_profile_version,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("revision 元数据绑定发生 CAS 冲突")
    if commit:
        db.commit()
    revision = _row_to_revision(db.execute(
        "SELECT * FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone())
    if revision is None:
        raise ValueError("revision 元数据绑定后记录不存在")
    return revision


def screenplay_production_state(episode_id: str) -> dict[str, Any]:
    """Return the persisted screenplay stage and UI-safe recovery action."""
    from app import task_registry

    stage_order = [
        ("CHARACTER_DISCOVERY", "人物识别"),
        ("BLUEPRINT_GENERATION", "叙事蓝图"),
        ("IDENTITY_FREEZE", "身份冻结"),
        ("ENVELOPE_GENERATION", "全局包络"),
        ("SCENE_SHARD_GENERATION", "场次写作"),
        ("IR_MERGE", "全局编译"),
        ("STRUCTURE_VALIDATION", "结构校验"),
        ("QUALITY_SCORING", "质量评分"),
        ("PUBLISHING", "原子发布"),
        ("SUCCEEDED", "已完成"),
    ]
    rev = get_active_production_revision(episode_id, "screenplay")
    if rev is None:
        rev = _row_to_revision(get_conn().execute(
            """SELECT revision.*
                 FROM production_revisions AS revision
                 JOIN episodes AS episode
                   ON episode.screenplay_production_revision_id=revision.id
                WHERE episode.id=?
                  AND revision.episode_id=episode.id
                  AND revision.kind='screenplay'
                  AND revision.status='published'
                  AND episode.screenplay_artifact_id IS NOT NULL
                  AND episode.screenplay_artifact_id=revision.published_artifact_id
                LIMIT 1""",
            (episode_id,),
        ).fetchone())
    conn = get_conn()
    episode = conn.execute(
        "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    from app.evidence import repository as evidence_repository

    current_run = (
        conn.execute(
            "SELECT status,failure_code,failure_message FROM workflow_runs "
            "WHERE id=?",
            (episode["active_screenplay_run_id"],),
        ).fetchone()
        if episode and episode["active_screenplay_run_id"]
        else None
    )
    active = bool(
        task_registry.active("screenplay", episode_id)
        or (
            episode
            and evidence_repository.get_active_scoped_run(
                episode["active_screenplay_run_id"],
                workflow_type="screenplay",
                scope_type="episode",
                scope_id=episode_id,
                conn=conn,
            )
        )
    )
    if rev is None:
        return {
            "operation": "baseline",
            "phase": "CHARACTER_DISCOVERY",
            "phase_label": "人物识别",
            "stage_index": 0,
            "stage_count": len(stage_order),
            "stages": [
                {"key": key, "label": label, "status": "pending"}
                for key, label in stage_order
            ],
            "baseline_done": False,
            "first_evaluation_done": False,
            "task_active": active,
            "can_resume_repair": False,
            "can_resume_baseline": False,
            "has_working_baseline": False,
            "has_resumable_baseline": False,
            "shard_progress": {
                "total": 0, "validated": 0, "running": 0, "failed": 0,
            },
            "activation_count": 0,
            "patch_count": 0,
            "open_issue_count": 0,
            "yield_reason": "",
            "stage_stop_reason": "",
        }
    checkpoint = dict(rev.checkpoint_json or {})
    eligibility = resolve_screenplay_resume_eligibility(
        episode_id,
        revision=rev,
        conn=conn,
    )
    has_working_baseline = eligibility.working_compatible
    has_resumable_baseline = any(
        eligibility.reusable_checkpoint.get(key)
        for key in (
            "blueprint_artifact_id",
            "envelope_artifact_id",
            "shards",
            "merged_ir_artifact_id",
        )
    )
    published = bool(rev.published_artifact_id)
    phase = str(
        checkpoint.get("phase")
        or ("SUCCEEDED" if published else "STRUCTURE_VALIDATION" if has_working_baseline
            else "BLUEPRINT_GENERATION")
    )
    if eligibility.mode == "baseline_rebuild":
        phase = "BLUEPRINT_GENERATION"
    phase_aliases = {
        "BASELINE": "BLUEPRINT_GENERATION",
        "GENERATING_BASELINE": "BLUEPRINT_GENERATION",
        "QA": "QUALITY_SCORING",
        "WAITING_HUMAN": "STRUCTURE_VALIDATION",
        "FAILED": "STRUCTURE_VALIDATION",
    }
    phase = phase_aliases.get(phase, phase)
    if (
        has_working_baseline
        and not published
        and phase in {
            "BLUEPRINT_GENERATION", "IDENTITY_FREEZE", "ENVELOPE_GENERATION",
            "SCENE_SHARD_GENERATION", "IR_MERGE", "IDENTITY_AUDIT",
        }
    ):
        # Baseline persistence precedes the next checkpoint write. A crash in
        # that narrow window must resume from the durable artifact instead of
        # presenting or charging for another full baseline generation.
        phase = "STRUCTURE_VALIDATION"
    stage_keys = [key for key, _label in stage_order]
    stage_index = (
        stage_keys.index(phase)
        if phase in stage_keys
        else (len(stage_order) - 1 if published else 0)
    )
    yield_reason = str(checkpoint.get("yield_reason") or "")
    gate_stop_reasons = {
        "character_identity_hard_gate",
        "narrative_gate_needs_review",
        "quality_gate_needs_review",
    }
    if active or published:
        stage_stop_reason = ""
    elif yield_reason in gate_stop_reasons or checkpoint.get("open_issue_ids"):
        stage_stop_reason = "blocked"
    elif current_run and current_run["status"] == "FAILED":
        stage_stop_reason = "failed"
    else:
        stage_stop_reason = "paused"
    stages = []
    for index, (key, label) in enumerate(stage_order):
        if published or index < stage_index:
            status = "completed"
        elif index == stage_index:
            status = "in_progress" if active else stage_stop_reason
        else:
            status = "pending"
        stages.append({"key": key, "label": label, "status": status})
    projected_shard_progress = dict(
        eligibility.reusable_checkpoint.get("shard_progress") or {
            "total": 0, "validated": 0, "running": 0, "failed": 0,
        }
    )
    return {
        "revision_id": rev.id,
        "operation": eligibility.mode,
        "mode": eligibility.mode,
        "mode_label": eligibility.label,
        "eligibility": eligibility.to_dict(),
        "phase": phase,
        "phase_label": dict(stage_order).get(phase, phase),
        "stage_index": stage_index,
        "stage_count": len(stage_order),
        "stages": stages,
        "baseline_done": rev.baseline_done,
        "first_evaluation_done": rev.first_evaluation_done,
        "task_active": active,
        "can_resume_repair": bool(
            eligibility.mode == "finalize" and not active
        ),
        "can_resume_baseline": bool(
            eligibility.mode in {"baseline", "baseline_rebuild"} and not active
        ),
        "has_working_baseline": has_working_baseline,
        "has_resumable_baseline": has_resumable_baseline,
        "shard_progress": projected_shard_progress,
        "activation_count": int(checkpoint.get("activation_no") or 0),
        "patch_count": len(checkpoint.get("patch_artifact_ids") or []),
        "open_issue_count": len(checkpoint.get("open_issue_ids") or []),
        "quality_score": checkpoint.get("quality_score"),
        "quality_issue_count": int(checkpoint.get("quality_issue_count") or 0),
        "gate_retry_exhausted": bool(checkpoint.get("gate_retry_exhausted")),
        "yield_reason": yield_reason,
        "stage_stop_reason": stage_stop_reason,
    }


def ensure_production_revision(
    *,
    episode_id: str,
    kind: Kind,
    input_fingerprint: str = "",
    contract_version: str = "",
    qa_profile_version: str = "",
    grant_id: str | None = None,
    resume: bool = True,
) -> ProductionRevision:
    """获取或创建候选 revision，不改变 episode 的正式发布指针。"""
    ensure_production_revisions_table()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_screenplay_write_owner(
            conn,
            episode_id=episode_id,
            kind=kind,
        )
        if resume:
            existing = _row_to_revision(conn.execute(
                "SELECT * FROM production_revisions "
                "WHERE episode_id=? AND kind=? AND status='active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (episode_id, kind),
            ).fetchone())
            if existing:
                requested = {
                    "input_fingerprint": input_fingerprint,
                    "contract_version": contract_version,
                    "qa_profile_version": qa_profile_version,
                }
                conflicts = [
                    field
                    for field, value in requested.items()
                    if value
                    and str(getattr(existing, field) or "")
                    and str(getattr(existing, field) or "") != str(value)
                ]
                if not conflicts:
                    conn.commit()
                    return existing

        stamp = now()
        conn.execute(
            "UPDATE production_revisions SET status='superseded', updated_at=? "
            "WHERE episode_id=? AND kind=? AND status='active'",
            (stamp, episode_id, kind),
        )
        revision_id = new_id("rev")
        conn.execute(
            """INSERT INTO production_revisions(
                id, episode_id, kind, status, baseline_generation_count,
                input_fingerprint, contract_version, qa_profile_version, grant_id,
                checkpoint_json, created_at, updated_at
            ) VALUES(?,?,?,'active',0,?,?,?,?, '{}',?,?)""",
            (
                revision_id, episode_id, kind, input_fingerprint,
                contract_version, qa_profile_version, grant_id, stamp, stamp,
            ),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return get_production_revision(revision_id)  # type: ignore[return-value]


def mark_baseline_generated(
    revision_id: str,
    *,
    baseline_artifact_id: str | None = None,
    working_artifact_id: str | None = None,
) -> ProductionRevision:
    ensure_production_revisions_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    _assert_screenplay_write_owner(
        conn,
        episode_id=row["episode_id"],
        kind=row["kind"],
        revision_id=revision_id,
    )
    if int(row["baseline_generation_count"] or 0) != 0 or row["status"] != "active":
        raise ValueError("production revision Baseline 已生成或 revision 不再 active")
    stamp = now()
    cursor = conn.execute(
        """UPDATE production_revisions SET
            baseline_generation_count=1,
            baseline_artifact_id=COALESCE(?, baseline_artifact_id),
            working_artifact_id=COALESCE(?, working_artifact_id),
            updated_at=?
        WHERE id=? AND status='active' AND baseline_generation_count=0""",
        (baseline_artifact_id, working_artifact_id or baseline_artifact_id, stamp, revision_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise ValueError("production revision Baseline 发生 CAS 冲突")
    # episode working pointer
    kind = row["kind"]
    episode_id = row["episode_id"]
    art = working_artifact_id or baseline_artifact_id
    if art:
        col = (
            "working_screenplay_artifact_id"
            if kind == "screenplay"
            else "working_storyboard_artifact_id"
        )
        try:
            conn.execute(f"UPDATE episodes SET {col}=? WHERE id=?", (art, episode_id))
        except Exception:  # noqa: BLE001
            pass
    conn.commit()
    return get_production_revision(revision_id)  # type: ignore[return-value]


def mark_first_evaluation(revision_id: str, evaluation_id: str) -> ProductionRevision:
    ensure_production_revisions_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    _assert_screenplay_write_owner(
        conn,
        episode_id=row["episode_id"],
        kind=row["kind"],
        revision_id=revision_id,
    )
    if row["first_evaluation_id"]:
        return get_production_revision(revision_id)  # type: ignore[return-value]
    stamp = now()
    cursor = conn.execute(
        "UPDATE production_revisions SET first_evaluation_id=?, updated_at=? "
        "WHERE id=? AND status='active' AND first_evaluation_id IS NULL",
        (evaluation_id, stamp, revision_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise ValueError("production revision 首次评估发生 CAS 冲突")
    conn.commit()
    return get_production_revision(revision_id)  # type: ignore[return-value]


def update_working_artifact(revision_id: str, artifact_id: str, *, expected_hash: str | None = None) -> None:
    """CAS 更新 working_artifact_id。"""
    ensure_production_revisions_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    _assert_screenplay_write_owner(
        conn,
        episode_id=row["episode_id"],
        kind=row["kind"],
        revision_id=revision_id,
    )
    if row["status"] != "active":
        raise RuntimeError("production revision 不再 active")
    if expected_hash:
        current_id = row["working_artifact_id"]
        if current_id:
            art = conn.execute(
                "SELECT content_hash FROM artifacts WHERE id=?", (current_id,)
            ).fetchone()
            if art and art["content_hash"] and art["content_hash"] != expected_hash:
                raise RuntimeError("working artifact hash conflict")
    stamp = now()
    cursor = conn.execute(
        "UPDATE production_revisions SET working_artifact_id=?, updated_at=? "
        "WHERE id=? AND status='active' "
        "AND COALESCE(working_artifact_id, '')=COALESCE(?, '')",
        (
            artifact_id,
            stamp,
            revision_id,
            row["working_artifact_id"],
        ),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise RuntimeError("production revision working artifact 发生 CAS 冲突")
    kind = row["kind"]
    episode_id = row["episode_id"]
    col = (
        "working_screenplay_artifact_id"
        if kind == "screenplay"
        else "working_storyboard_artifact_id"
    )
    try:
        conn.execute(f"UPDATE episodes SET {col}=? WHERE id=?", (artifact_id, episode_id))
    except Exception:  # noqa: BLE001
        pass
    conn.commit()


def recover_screenplay_working_authority(
    revision_id: str,
    artifact_id: str,
    *,
    expected_working_artifact_id: str,
    expected_working_hash: str,
    expected_replacement_hash: str,
    trusted_merged_ir_artifact_id: str,
    revalidation_evaluation_id: str,
    input_fingerprint: str,
    contract_version: str,
    qa_profile_version: str,
    checkpoint: dict[str, Any],
) -> ProductionRevision:
    """Create a new revision bound to a revalidated working authority.

    The old revision and its QA/repair metadata stay immutable audit history.
    Only a current, typed screenplay document may become the new authority,
    and the old revision/episode pointers are fenced in one transaction so a
    late worker cannot restore a poisoned candidate.
    """
    ensure_production_revisions_table()
    from app.harness.contracts import get_contract
    from app.production.screenplay_authority import SCREENPLAY_QA_PROFILE_VERSION

    if qa_profile_version != SCREENPLAY_QA_PROFILE_VERSION:
        raise ValueError("screenplay recovery 只能绑定当前 QA profile")
    if contract_version != get_contract("screenplay").version:
        raise ValueError("screenplay recovery 只能绑定当前 Document contract")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM production_revisions WHERE id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ValueError("screenplay recovery revision 不存在")
        _assert_screenplay_write_owner(
            conn,
            episode_id=row["episode_id"],
            kind=row["kind"],
            revision_id=revision_id,
        )
        current_eligibility = resolve_screenplay_resume_eligibility(
            str(row["episode_id"]),
            revision=_row_to_revision(row),
            conn=conn,
        )
        if (
            row["status"] != "active"
            or str(row["working_artifact_id"] or "")
            != expected_working_artifact_id
            or current_eligibility.reason_code
            != "WORKING_REVALIDATION_REQUIRED"
            or current_eligibility.revision_id != revision_id
            or current_eligibility.working_artifact_id
            != expected_working_artifact_id
        ):
            raise RuntimeError("screenplay recovery eligibility/working CAS 冲突")
        current_artifact = conn.execute(
            "SELECT content_hash FROM artifacts WHERE id=?",
            (expected_working_artifact_id,),
        ).fetchone()
        if (
            current_artifact is None
            or str(current_artifact["content_hash"] or "")
            != expected_working_hash
        ):
            raise RuntimeError("screenplay recovery working hash CAS 冲突")
        replacement = conn.execute(
            "SELECT id,type,scope_type,scope_id,status,contract_version,"
            "content_json,content_hash,parent_artifact_ids_json "
            "FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()
        if (
            replacement is None
            or replacement["type"] != "screenplay_document"
            or replacement["scope_type"] != "episode"
            or replacement["scope_id"] != row["episode_id"]
            or replacement["status"] not in {"candidate", "validated", "approved"}
            or str(replacement["contract_version"] or "") != contract_version
            or str(replacement["content_hash"] or "")
            != expected_replacement_hash
        ):
            raise ValueError("screenplay recovery replacement Artifact 合同不兼容")
        replacement_content = _artifact_json(dict(replacement))
        if (
            replacement_content is None
            or not _artifact_hash_is_valid(
                dict(replacement), replacement_content
            )
        ):
            raise ValueError("screenplay recovery replacement 内容哈希失效")
        try:
            replacement_parents = {
                str(value)
                for value in json.loads(
                    replacement["parent_artifact_ids_json"] or "[]"
                )
                if str(value)
            }
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("screenplay recovery replacement lineage 无效") from exc
        trusted_merged_id = str(trusted_merged_ir_artifact_id or "")
        eligible_merged_id = str(
            current_eligibility.reusable_checkpoint.get(
                "merged_ir_artifact_id"
            ) or ""
        )
        if artifact_id != expected_working_artifact_id:
            if (
                not trusted_merged_id
                or trusted_merged_id != eligible_merged_id
                or replacement_parents != {trusted_merged_id}
            ):
                raise ValueError("screenplay recovery replacement 不来自可信上游")
        evaluation = conn.execute(
            "SELECT artifact_id,evaluator_name,evaluator_version,status,"
            "hard_gate_passed,runtime_blocking,issues_json,evidence_json "
            "FROM evaluations WHERE id=?",
            (revalidation_evaluation_id,),
        ).fetchone()
        if evaluation is None:
            raise ValueError("screenplay recovery 缺少持久化 gate-3 复验证据")
        try:
            evaluation_evidence = json.loads(
                evaluation["evidence_json"] or "{}"
            )
            evaluation_issues = json.loads(
                evaluation["issues_json"] or "[]"
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("screenplay recovery 复验证据不可解析") from exc
        blocking_evaluation_issue = any(
            isinstance(issue, dict)
            and (
                bool(issue.get("must_fix"))
                or bool((issue.get("evidence") or {}).get("must_fix"))
                or bool((issue.get("evidence") or {}).get("runtime_blocking"))
            )
            for issue in evaluation_issues
        )
        if (
            evaluation["artifact_id"] != artifact_id
            or evaluation["evaluator_name"] != "screenplay_production_qa"
            or evaluation["evaluator_version"] != qa_profile_version
            or evaluation["status"] in {"failed", "error"}
            or not bool(evaluation["hard_gate_passed"])
            or bool(evaluation["runtime_blocking"])
            or blocking_evaluation_issue
            or not isinstance(evaluation_evidence, dict)
            or str(evaluation_evidence.get("artifact_hash") or "")
            != expected_replacement_hash
            or str(
                evaluation_evidence.get("authority_input_fingerprint") or ""
            ) != input_fingerprint
        ):
            raise ValueError("screenplay recovery gate-3 复验证据不成立")
        old_checkpoint_json = str(row["checkpoint_json"] or "{}")
        stamp = now()
        new_revision_id = new_id("rev")
        cursor = conn.execute(
            "UPDATE production_revisions SET status='superseded',updated_at=? "
            "WHERE id=? AND status='active' "
            "AND COALESCE(working_artifact_id,'')=? AND checkpoint_json=?",
            (
                stamp,
                revision_id,
                expected_working_artifact_id,
                old_checkpoint_json,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("screenplay recovery revision CAS 冲突")
        conn.execute(
            """INSERT INTO production_revisions(
                id,episode_id,kind,status,baseline_generation_count,
                first_evaluation_id,baseline_artifact_id,working_artifact_id,
                published_artifact_id,grant_id,input_fingerprint,
                contract_version,qa_profile_version,checkpoint_json,
                created_at,updated_at
            ) VALUES(?,?,?,'active',1,NULL,?,?,NULL,NULL,?,?,?,?,?,?)""",
            (
                new_revision_id,
                row["episode_id"],
                row["kind"],
                artifact_id,
                artifact_id,
                input_fingerprint,
                contract_version,
                qa_profile_version,
                json.dumps(checkpoint, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        episode_cursor = conn.execute(
            "UPDATE episodes SET working_screenplay_artifact_id=? WHERE id=? "
            "AND COALESCE(working_screenplay_artifact_id,'')=?",
            (
                artifact_id,
                row["episode_id"],
                expected_working_artifact_id,
            ),
        )
        if episode_cursor.rowcount != 1:
            raise RuntimeError("screenplay recovery episode pointer CAS 冲突")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    revision = get_production_revision(new_revision_id)
    if revision is None:
        raise RuntimeError("screenplay recovery 更新后 revision 丢失")
    return revision


def save_checkpoint(revision_id: str, checkpoint: dict[str, Any]) -> None:
    ensure_production_revisions_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT episode_id,kind,status FROM production_revisions WHERE id=?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"production revision not found: {revision_id}")
    _assert_screenplay_write_owner(
        conn,
        episode_id=row["episode_id"],
        kind=row["kind"],
        revision_id=revision_id,
        allow_current_published=True,
    )
    cursor = conn.execute(
        "UPDATE production_revisions SET checkpoint_json=?, updated_at=? "
        "WHERE id=? AND status IN ('active','published')",
        (json.dumps(checkpoint, ensure_ascii=False), now(), revision_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise ValueError("production revision checkpoint 已失效")
    conn.commit()


def set_published_artifact(
    revision_id: str,
    artifact_id: str,
    *,
    certificate_id: str | None = None,
    conn=None,
    commit: bool = True,
) -> None:
    if conn is None:
        ensure_production_revisions_table()
    db = conn or get_conn()
    row = db.execute(
        "SELECT * FROM production_revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"production revision not found: {revision_id}")
    _assert_screenplay_write_owner(
        db,
        episode_id=row["episode_id"],
        kind=row["kind"],
        revision_id=revision_id,
    )
    if row["status"] != "active":
        raise ValueError("只能发布 active revision")
    stamp = now()
    cursor = db.execute(
        "UPDATE production_revisions SET published_artifact_id=?, working_artifact_id=?, "
        "status='published', updated_at=? WHERE id=? AND status='active'",
        (artifact_id, artifact_id, stamp, revision_id),
    )
    if cursor.rowcount != 1:
        if commit:
            db.rollback()
        raise ValueError("production revision 发布发生 CAS 冲突")
    kind = row["kind"]
    episode_id = row["episode_id"]
    if kind == "screenplay":
        db.execute(
            "UPDATE episodes SET published_screenplay_artifact_id=?, "
            "working_screenplay_artifact_id=?, screenplay_artifact_id=?, "
            "screenplay_completion_certificate_id=?, "
            "screenplay_production_revision_id=? WHERE id=?",
            (
                artifact_id, artifact_id, artifact_id, certificate_id,
                revision_id, episode_id,
            ),
        )
    else:
        db.execute(
            "UPDATE episodes SET published_storyboard_artifact_id=?, "
            "working_storyboard_artifact_id=?, storyboard_artifact_id=?, "
            "storyboard_completion_certificate_id=?, "
            "storyboard_production_revision_id=? WHERE id=?",
            (
                artifact_id, artifact_id, artifact_id, certificate_id,
                revision_id, episode_id,
            ),
        )
    if commit:
        db.commit()
