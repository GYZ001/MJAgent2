"""叙事蓝图——权威检查点提交与工作流步骤驱动。"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable


from app import config
from app.db import get_conn, log_provider_call
from app.narrative_blueprint import (
    BLUEPRINT_VERSION,
    BlueprintSourceOccurrenceError,
    BlueprintSourceOwnershipError,
    NarrativeBlueprint,
    derive_blueprint_scene_plans,
    validate_blueprint_scene_partition,
    validate_narrative_blueprint,
)
from app.schemas import (EpisodeScreenplay)
from app.validators import (ending_hook_grounding_report)

from .blueprint_budget import _BlueprintGenerationBudget
from .blueprint_budget_trace import blueprint_retry_receipts_hash
from .blueprint_generate_entry import _save_screenplay_generation_checkpoint
from .common import StageError
from .constants import SCREENPLAY_BLUEPRINT_PROMPT_VERSION
from .ir_snapshot import (
    _artifact_json_content_is_sealed,
    _blueprint_authority_snapshot_is_current,
    _narrative_blueprint_content_hash,
)


def _commit_blueprint_authority_checkpoint(
    *,
    episode_id: str,
    blueprint_artifact_id: str,
    blueprint_hash: str,
    source_text: str,
) -> None:
    """Atomically checkpoint current Blueprint authority and resolve its receipts."""
    from app.observability.tracing import current_trace

    trace = current_trace()
    run_id = str(trace.run_id or "")
    if not run_id:
        _save_screenplay_generation_checkpoint(
            episode_id,
            "IDENTITY_FREEZE",
            blueprint_artifact_id=blueprint_artifact_id,
            blueprint_hash=blueprint_hash,
            yield_reason=None,
        )
        return
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT active_screenplay_run_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if owner is None or str(owner["active_screenplay_run_id"] or "") != run_id:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_OWNER_DRIFT] 当前运行已失去剧集写权"],
            )
        revision = conn.execute(
            """SELECT id,checkpoint_json,grant_id FROM production_revisions
                 WHERE episode_id=? AND kind='screenplay' AND status='active'
                 ORDER BY updated_at DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
        if revision is None:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_REVISION_MISSING] 缺少active revision"],
            )
        artifact = conn.execute(
            """SELECT content_json,content_hash,model_snapshot_json FROM artifacts
                 WHERE id=? AND type='screenplay_narrative_blueprint'
                   AND scope_type='episode' AND scope_id=?
                   AND status='validated' AND contract_version=?
                   AND prompt_version=?""",
            (
                blueprint_artifact_id,
                episode_id,
                BLUEPRINT_VERSION,
                SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            ),
        ).fetchone()
        if artifact is None:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_ARTIFACT_INVALID] current Blueprint artifact失效"],
            )
        snapshot = json.loads(artifact["model_snapshot_json"] or "{}")
        artifact_content = json.loads(artifact["content_json"] or "{}")
        if not _artifact_json_content_is_sealed(artifact, artifact_content):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_ARTIFACT_HASH] current Blueprint 内容指纹漂移"],
            )
        artifact_blueprint = NarrativeBlueprint.model_validate(artifact_content)
        artifact_blueprint_hash = _narrative_blueprint_content_hash(
            artifact_blueprint
        )
        artifact_errors = validate_narrative_blueprint(
            artifact_blueprint,
            source_text,
        )
        try:
            artifact_plans = derive_blueprint_scene_plans(artifact_blueprint)
            artifact_errors.extend(
                validate_blueprint_scene_partition(
                    artifact_blueprint,
                    artifact_plans,
                )
            )
        except (
            BlueprintSourceOccurrenceError,
            BlueprintSourceOwnershipError,
            ValueError,
        ) as exc:
            artifact_errors.extend(
                getattr(exc, "errors", None) or [str(exc)]
            )
        if (
            artifact_blueprint_hash != blueprint_hash
            or not _blueprint_authority_snapshot_is_current(
                snapshot,
                source_text,
            )
            or artifact_errors
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                [
                    "[BLUEPRINT_AUTHORITY_SNAPSHOT_DRIFT] "
                    "Blueprint authority版本或语义漂移"
                ] + artifact_errors[:10],
            )
        checkpoint = json.loads(revision["checkpoint_json"] or "{}")
        checkpoint.update({
            "phase": "IDENTITY_FREEZE",
            "blueprint_artifact_id": blueprint_artifact_id,
            "blueprint_hash": blueprint_hash,
            "yield_reason": None,
        })
        changed = conn.execute(
            "UPDATE production_revisions SET checkpoint_json=?,updated_at=? "
            "WHERE id=? AND status='active' AND grant_id IS ?",
            (
                json.dumps(checkpoint, ensure_ascii=False),
                time.time(),
                str(revision["id"]),
                revision["grant_id"],
            ),
        )
        if changed.rowcount != 1:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_AUTHORITY_CHECKPOINT_CAS] revision authority漂移"],
            )

        run = conn.execute(
            "SELECT input_fingerprint,config_snapshot_json FROM workflow_runs "
            "WHERE id=? AND scope_type='episode' AND scope_id=?",
            (run_id, episode_id),
        ).fetchone()
        config_snapshot = json.loads(run["config_snapshot_json"] or "{}")
        receipts = config_snapshot.get("blueprint_retry_receipts") or []
        pinned_hash = str(
            config_snapshot.get("blueprint_retry_receipts_hash") or ""
        )
        grant_id = str(config_snapshot.get("blueprint_retry_grant_id") or "")
        if not receipts and not pinned_hash and not grant_id:
            conn.commit()
            return
        if (
            not isinstance(receipts, list)
            or not receipts
            or blueprint_retry_receipts_hash(receipts) != pinned_hash
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_RECEIPTS_DRIFT] retry receipts snapshot漂移"],
            )
        if str(revision["grant_id"] or "") != grant_id:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_GRANT_DRIFT] retry grant authority漂移"],
            )
        grant = conn.execute(
            """SELECT 1 FROM production_grants
                WHERE id=? AND episode_id=? AND kind='screenplay'
                  AND production_revision_id=?
                  AND issued_by='user_retry_approval'
                  AND input_artifact_hash=? AND consumed_at IS NOT NULL
                  AND revoked_at IS NULL AND expires_at>?""",
            (
                grant_id,
                episode_id,
                str(revision["id"]),
                pinned_hash,
                time.time(),
            ),
        ).fetchone()
        if grant is None:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_GRANT_INVALID] pinned retry grant失效"],
            )
        exact_ids = [int(item.get("call_id") or 0) for item in receipts]
        if any(call_id <= 0 for call_id in exact_ids) or len(exact_ids) != len(set(exact_ids)):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_RECEIPTS_INVALID] retry call IDs无效"],
            )
        artifact_hash = str(artifact["content_hash"] or "")
        operation_id = "blueprint-resolution:" + hashlib.sha256(
            f"{blueprint_artifact_id}:{artifact_hash}:{run_id}:{grant_id}:{pinned_hash}".encode()
        ).hexdigest()
        resolution = conn.execute(
            "SELECT id FROM provider_calls WHERE operation_id=? "
            "AND kind='blueprint_authority_resolution'",
            (operation_id,),
        ).fetchone()
        resolution_id = int(resolution["id"]) if resolution is not None else 0
        # Reconstruct the authority receipts with the same durable resolver
        # used by preflight/runtime.  Old provider rows may have lossy meta,
        # NULL request hashes, and no durable grant column; the resolver's
        # narrow BASELINE-event bridge is the only authority allowed to infer
        # those legacy fields.  On crash replay, include receipts already
        # terminalized by this exact deterministic resolution.
        durable_budget = _BlueprintGenerationBudget.from_durable_calls(
            run_id=run_id,
            episode_id=episode_id,
            input_fingerprint=str(run["input_fingerprint"] or ""),
            retry_grant_id=grant_id,
            include_resolved_by_call_id=(resolution_id or None),
        )
        canonical_receipts = durable_budget.unknown_receipts
        if (
            canonical_receipts != receipts
            or blueprint_retry_receipts_hash(canonical_receipts) != pinned_hash
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_RESOLUTION_RECEIPTS_DRIFT] durable retry receipts漂移"],
            )
        placeholders = ",".join("?" for _ in exact_ids)
        rows = conn.execute(
            f"""SELECT pc.id,pc.status,pc.superseded_by_call_id,
                       pc.recovery_disposition,pc.operation_id,
                       pc.request_hash,pc.production_grant_id,pc.meta,
                       wr.input_fingerprint
                  FROM provider_calls pc
                  JOIN workflow_runs wr ON wr.id=pc.run_id
                 WHERE pc.id IN ({placeholders})
                   AND wr.scope_type='episode' AND wr.scope_id=?""",
            (*exact_ids, episode_id),
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        for receipt in receipts:
            call_id = int(receipt["call_id"])
            row = by_id.get(call_id)
            meta = json.loads(row["meta"] or "{}") if row is not None else {}
            already_exact = bool(
                resolution_id
                and row is not None
                and int(row["superseded_by_call_id"] or 0) == resolution_id
                and str(row["recovery_disposition"] or "")
                == "SUPERSEDED_BY_VALIDATED_BLUEPRINT_REBUILD"
            )
            if (
                row is None
                or str(row["status"] or "") not in {"INTERRUPTED", "RUNNING"}
                or (
                    row["superseded_by_call_id"] is not None
                    and not already_exact
                )
                or str(row["input_fingerprint"] or "")
                != str(run["input_fingerprint"] or "")
                or str(meta.get("stage_key") or "")
                != str(receipt.get("stage_key") or "")
                or str(row["operation_id"] or "")
                != str(receipt.get("operation_id") or "")
                or str(row["request_hash"] or "")
                != str(receipt.get("request_hash") or "")
            ):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    [f"[BLUEPRINT_RESOLUTION_RECEIPT_CAS] call {call_id} authority漂移"],
                )
        if resolution is None:
            cursor = conn.execute(
                """INSERT INTO provider_calls(
                       ts,kind,model,status,latency_ms,contract_version,
                       production_grant_id,response_json,meta,run_id,step_run_id,
                       operation_id,attempt_no,recovery_disposition
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), "blueprint_authority_resolution",
                    "deterministic", "OK", 0, BLUEPRINT_VERSION, grant_id,
                    json.dumps({
                        "artifact_id": blueprint_artifact_id,
                        "artifact_hash": artifact_hash,
                        "receipts_hash": pinned_hash,
                    }, sort_keys=True, separators=(",", ":")),
                    json.dumps({
                        "stage_key": "screenplay_blueprint_resolution",
                        "episode_id": episode_id,
                    }, sort_keys=True, separators=(",", ":")),
                    run_id, trace.step_run_id, operation_id, 1,
                    "VALIDATED_BLUEPRINT_AUTHORITY",
                ),
            )
            resolution_id = int(cursor.lastrowid)
        else:
            resolution_row = conn.execute(
                """SELECT status,run_id,production_grant_id,response_json,
                          contract_version,recovery_disposition
                     FROM provider_calls WHERE id=?""",
                (resolution_id,),
            ).fetchone()
            resolution_response: dict[str, Any] = {}
            if resolution_row is not None:
                try:
                    resolution_response = json.loads(
                        resolution_row["response_json"] or "{}"
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            if (
                resolution_row is None
                or str(resolution_row["status"] or "") != "OK"
                or str(resolution_row["run_id"] or "") != run_id
                or str(resolution_row["production_grant_id"] or "")
                != grant_id
                or str(resolution_row["contract_version"] or "")
                != BLUEPRINT_VERSION
                or str(resolution_row["recovery_disposition"] or "")
                != "VALIDATED_BLUEPRINT_AUTHORITY"
                or str(resolution_response.get("artifact_id") or "")
                != blueprint_artifact_id
                or str(resolution_response.get("artifact_hash") or "")
                != artifact_hash
                or str(resolution_response.get("receipts_hash") or "")
                != pinned_hash
            ):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_RESOLUTION_RECEIPT_INVALID] resolution receipt漂移"],
                )
        for call_id in exact_ids:
            cursor = conn.execute(
                "UPDATE provider_calls SET superseded_by_call_id=?,"
                "recovery_disposition='SUPERSEDED_BY_VALIDATED_BLUEPRINT_REBUILD' "
                "WHERE id=? AND status IN ('INTERRUPTED','RUNNING') "
                "AND superseded_by_call_id IS NULL",
                (resolution_id, call_id),
            )
            if cursor.rowcount == 0:
                exact = conn.execute(
                    "SELECT 1 FROM provider_calls WHERE id=? "
                    "AND superseded_by_call_id=? "
                    "AND recovery_disposition="
                    "'SUPERSEDED_BY_VALIDATED_BLUEPRINT_REBUILD'",
                    (call_id, resolution_id),
                ).fetchone()
                if exact is None:
                    raise StageError(
                        "剧本时空因果蓝图分片",
                        ["[BLUEPRINT_RESOLUTION_PARTIAL] retry receipts未精确终结"],
                    )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


async def _run_screenplay_workflow_step(
    step_key: str,
    operation: Callable[[], Any],
    *,
    agent_name: str,
    context_manifest: dict[str, Any] | None = None,
) -> Any:
    """Expose each pre-document generation phase as a persisted workflow step."""
    from app.observability.tracing import current_trace

    trace = current_trace()
    if not trace.run_id:
        return await operation()
    from app.orchestration.engine import WorkflowRecorder

    _step_id, result = await WorkflowRecorder(trace.run_id).step(
        step_key,
        operation,
        agent_name=agent_name,
        context_manifest=context_manifest,
    )
    return result


def _clear_ungrounded_ending_hook(
    script: EpisodeScreenplay,
    *,
    episode_id: str,
    source: str,
) -> None:
    """ending_hook 溯源判定失败时清空，并把判定证据写成可查的观测事件。

    背景：这条清空动作以前是完全静默的（app/stages.py 两处、
    app/production/publish.py 一处直接 `script.ending_hook = ""`，不留任何
    痕迹）——数据上无法区分"原文真的没钩子（合法留空）"和"被误杀"。EP4 的
    269 条原子事件把结尾拆得极细，导致旧的单事件判据误杀了一条模型正确、
    忠实原文的钩子；这次要不是人工追问 EP4 为什么慢，根本发现不了。QA
    校验（validate_screenplay_narrative 里的 ending_hook 分支）只在
    "非空但过短"时报错，对"被清空为空"完全无感，指望不上它兜底。

    这里复用 ending_hook_grounding_report 而不是 ending_hook_is_grounded：
    两层判据的实测覆盖率数值、最佳匹配 event id/窗口，都要落进 provider_calls
    观测记录，供事后查证具体清空原因，而不只是一个 bool。
    """
    hook_text = (script.ending_hook or "").strip()
    if not hook_text:
        return
    report = ending_hook_grounding_report(
        script.ending_hook, script.full_script_text, events=script.events,
    )
    if report["grounded"]:
        return
    script.ending_hook = ""
    log_provider_call(
        "ending_hook_grounding_rejected",
        config.MODEL_TEXT,
        "REJECTED",
        None,
        0,
        meta={
            "episode_id": episode_id,
            "source": source,
            "hook_text": report["hook_text"],
            "tier": report["tier"],
            "layer1_coverage": report["layer1_coverage"],
            "best_event_id": report["best_event_id"],
            "best_event_coverage": report["best_event_coverage"],
            "window": report["window"],
        },
    )
