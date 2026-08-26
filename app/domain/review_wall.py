"""生成台的稳定对象、上游资格与版本操作契约。

这个模块由 ``app.api`` 兼容门面在其命名空间内执行，因此路由和
原有领域函数共用同一个 ``router``。安全校验函数也供视频写路径调用，
保证 UI、Agent 和直接 REST 调用的口径一致。
"""
from __future__ import annotations
from app.auth.principal import current_actor_name

import hashlib
import json
import math
from typing import Any

try:
    router
except NameError:  # pragma: no cover - used when importing this module directly
    from app.domain.common import *


_REVIEW_TERMINAL_RUN_STATES = {
    "SUCCEEDED", "FAILED", "CANCELLED", "COMPLETED", "PARTIAL",
    "succeeded", "failed", "cancelled", "completed", "partial",
}
_REVIEW_ACTIVE_RUN_STATES = {
    "CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN",
    "WAITING_AUTHORIZATION", "PAUSED_BUDGET", "PAUSED_EXTERNAL",
}


def _review_sha(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _review_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default


_REVIEW_INVALID_ARTIFACT_STATUSES = {
    "stale", "rejected", "superseded", "needs_revision",
}


def _review_artifact_binding(
    conn,
    *,
    artifact_id: str | None,
    episode_id: str,
    expected_types: set[str],
) -> dict[str, Any]:
    """Return a content-addressed artifact binding without trusting its pointer."""
    binding: dict[str, Any] = {
        "artifact_id": artifact_id,
        "content_hash": None,
        "status": None,
        "verified": False,
        "content": None,
        "parent_artifact_ids": [],
    }
    if not artifact_id:
        return binding
    try:
        row = conn.execute(
            """SELECT id,type,scope_type,scope_id,status,content_json,content_hash,
                      parent_artifact_ids_json,contract_version,prompt_version
                 FROM artifacts WHERE id=?""",
            (artifact_id,),
        ).fetchone()
    except Exception:  # legacy databases do not own authoritative artifacts
        return binding
    if row is None:
        return binding
    content = _review_json(row["content_json"], None)
    parents = _review_json(row["parent_artifact_ids_json"], [])
    if not isinstance(parents, list):
        parents = []
    try:
        from app.evidence.repository import content_hash

        computed_hash = content_hash(content)
    except Exception:  # a hash service failure is an invalid binding
        computed_hash = None
    binding.update({
        "artifact_id": row["id"],
        "artifact_type": row["type"],
        "content_hash": row["content_hash"],
        "computed_hash": computed_hash,
        "status": row["status"],
        "contract_version": row["contract_version"],
        "prompt_version": row["prompt_version"],
        "content": content,
        "parent_artifact_ids": [str(item) for item in parents if item],
        "verified": bool(
            row["type"] in expected_types
            and row["scope_type"] == "episode"
            and row["scope_id"] == episode_id
            and row["status"] not in _REVIEW_INVALID_ARTIFACT_STATUSES
            and content is not None
            and computed_hash
            and row["content_hash"] == computed_hash
        ),
    })
    return binding


def _review_certificate_binding(
    conn,
    *,
    certificate_id: str | None,
    kind: str,
    episode_id: str,
    artifact_id: str | None,
    artifact_hash: str | None,
    production_revision_id: str | None,
    artifact_contract_version: str | None,
    review_comparator_version: str | None = None,
) -> dict[str, Any]:
    """Verify the consumed release certificate and its exact gate evidence."""
    binding: dict[str, Any] = {
        "certificate_id": certificate_id,
        "verified": False,
        "evaluation_ids": [],
        "evaluation_fingerprint": None,
    }
    if not certificate_id or not artifact_id or not artifact_hash or not production_revision_id:
        return binding
    try:
        row = conn.execute(
            """SELECT id,kind,scope_id,artifact_id,artifact_hash,input_fingerprint,
                      contract_version,qa_profile_version,
                      evaluation_ids_json,blockers,must_fix_issues,
                      production_revision_id,consumed_at
                 FROM completion_certificates WHERE id=?""",
            (certificate_id,),
        ).fetchone()
    except Exception:
        return binding
    if row is None:
        return binding
    evaluation_ids = _review_json(row["evaluation_ids_json"], [])
    if not isinstance(evaluation_ids, list):
        evaluation_ids = []
    evaluation_ids = list(dict.fromkeys(str(item) for item in evaluation_ids if item))
    evaluation_rows: list[Any] = []
    if evaluation_ids:
        marks = ",".join("?" for _ in evaluation_ids)
        evaluation_rows = conn.execute(
            f"""SELECT id,artifact_id,evaluator_name,evaluator_version,status,
                       hard_gate_passed,evaluation_role,runtime_blocking,issues_json
                  FROM evaluations WHERE id IN ({marks})""",
            evaluation_ids,
        ).fetchall()
    evaluation_projection = sorted(
        ({key: item[key] for key in item.keys()} for item in evaluation_rows),
        key=lambda item: item["id"],
    )
    exact_evaluations = bool(
        len(evaluation_projection) == len(evaluation_ids)
        and all(item["artifact_id"] == artifact_id for item in evaluation_projection)
    )

    def _qualified(name: str, *, evaluator_version: str | None) -> bool:
        matches = [item for item in evaluation_projection if item["evaluator_name"] == name]
        if len(matches) != 1 or not evaluator_version:
            return False
        item = matches[0]
        issues = _review_json(item.get("issues_json"), [])
        blocking_issue = bool(
            not isinstance(issues, list)
            or any(
                isinstance(issue, dict)
                and (
                    str(issue.get("severity") or "").lower() == "blocker"
                    or bool(issue.get("must_fix"))
                )
                for issue in issues
            )
        )
        score_only = bool(
            item["evaluation_role"] == "score_only"
            and not bool(item["runtime_blocking"])
        )
        legacy_runtime_gate = bool(
            item["status"] == "passed"
            and bool(item["hard_gate_passed"])
            and item["evaluation_role"] == "runtime_gate"
            and bool(item["runtime_blocking"])
            and not blocking_issue
        )
        return bool(
            item["evaluator_version"] == evaluator_version
            and (score_only or legacy_runtime_gate)
        )

    required_gates_passed = (
        _qualified(
            "screenplay_production_qa",
            evaluator_version=row["qa_profile_version"],
        )
        if kind == "screenplay"
        else _qualified(
            "storyboard_full_gate",
            evaluator_version=row["contract_version"],
        )
    )
    try:
        revision = conn.execute(
            "SELECT * FROM production_revisions WHERE id=?",
            (production_revision_id,),
        ).fetchone()
    except Exception:  # legacy schemas cannot prove a narrative revision
        revision = None
    revision_verified = bool(
        revision
        and revision["kind"] == kind
        and revision["episode_id"] == episode_id
        and revision["status"] == "published"
        and revision["working_artifact_id"] == artifact_id
        and revision["published_artifact_id"] == artifact_id
        and str(revision["input_fingerprint"] or "") == str(row["input_fingerprint"] or "")
        and str(revision["contract_version"] or "") == str(row["contract_version"] or "")
        and str(revision["qa_profile_version"] or "") == str(row["qa_profile_version"] or "")
    )
    binding.update({
        "certificate_id": row["id"],
        "artifact_id": row["artifact_id"],
        "artifact_hash": row["artifact_hash"],
        "production_revision_id": row["production_revision_id"],
        "contract_version": row["contract_version"],
        "qa_profile_version": row["qa_profile_version"],
        "consumed": row["consumed_at"] is not None,
        "evaluation_ids": evaluation_ids,
        "evaluation_fingerprint": _review_sha(evaluation_projection),
        "verified": bool(
            row["kind"] == kind
            and row["scope_id"] == episode_id
            and row["artifact_id"] == artifact_id
            and row["artifact_hash"] == artifact_hash
            and row["production_revision_id"] == production_revision_id
            and bool(row["contract_version"])
            and row["contract_version"] == artifact_contract_version
            and bool(row["qa_profile_version"])
            and int(row["blockers"] or 0) == 0
            and int(row["must_fix_issues"] or 0) == 0
            and row["consumed_at"] is not None
            and exact_evaluations
            and required_gates_passed
            and revision_verified
        ),
    })
    return binding


def _review_narrative_authority_snapshot(conn, ep: dict[str, Any]) -> dict[str, Any]:
    """Bind every release fact required by a narrative-authority episode.

    This contract is intentionally content-addressed.  It does not infer story
    meaning from names or genres; the presence of the typed narrative plan is
    the sole authority switch.
    """
    raw_screenplay = _review_json(ep.get("screenplay_json"), None)
    raw_requires_authority = bool(
        isinstance(raw_screenplay, dict)
        and raw_screenplay.get("narrative_plan") is not None
    )
    from app.production.screenplay_authority import (
        episode_requires_immutable_screenplay_authority,
        resolve_downstream_screenplay,
    )

    immutable_required = episode_requires_immutable_screenplay_authority(
        ep,
        conn=conn,
    )
    try:
        screenplay_context = resolve_downstream_screenplay(
            str(ep.get("id") or ""),
            conn=conn,
        )
        screenplay = screenplay_context.screenplay
    except Exception as exc:
        required = bool(immutable_required or raw_requires_authority)
        invalid_version = (
            _review_sha({
                "episode_id": ep.get("id"),
                "screenplay_projection": raw_screenplay,
                "error": "NARRATIVE_SCREENPLAY_PROJECTION_INVALID",
                "detail": str(exc),
            })[:32]
            if required
            else None
        )
        return {
            "required": required,
            "verified": False,
            "authority_version": invalid_version,
            "errors": ["NARRATIVE_SCREENPLAY_PROJECTION_INVALID"],
        }
    if not screenplay_context.narrative_authority_required:
        return {
            "required": False,
            "verified": True,
            "authority_version": None,
            "errors": [],
        }

    episode_id = str(ep.get("id") or "")
    errors: list[str] = []
    screenplay_id = ep.get("published_screenplay_artifact_id")
    storyboard_id = ep.get("published_storyboard_artifact_id")
    if screenplay_id != ep.get("screenplay_artifact_id"):
        errors.append("NARRATIVE_SCREENPLAY_NOT_CURRENT_PUBLISHED_ARTIFACT")
    if storyboard_id != ep.get("storyboard_artifact_id"):
        errors.append("NARRATIVE_STORYBOARD_NOT_CURRENT_PUBLISHED_ARTIFACT")

    screenplay_artifact = _review_artifact_binding(
        conn,
        artifact_id=screenplay_id,
        episode_id=episode_id,
        expected_types={"screenplay_document"},
    )
    storyboard_artifact = _review_artifact_binding(
        conn,
        artifact_id=storyboard_id,
        episode_id=episode_id,
        expected_types={"storyboard", "storyboard_document"},
    )
    if not screenplay_artifact["verified"]:
        errors.append("NARRATIVE_SCREENPLAY_ARTIFACT_UNVERIFIED")
    if not storyboard_artifact["verified"]:
        errors.append("NARRATIVE_STORYBOARD_ARTIFACT_UNVERIFIED")

    screenplay_projection_verified = False
    if screenplay_artifact["verified"]:
        try:
            content = screenplay_artifact["content"]
            if isinstance(content, dict) and "screenplay_metadata" in content:
                from app.production.screenplay_document import (
                    ScreenplayDocument,
                    document_to_screenplay,
                )

                artifact_screenplay = document_to_screenplay(
                    ScreenplayDocument.model_validate(content)
                )
            elif isinstance(content, dict) and "_projection" in content:
                artifact_screenplay = EpisodeScreenplay.model_validate(content["_projection"])
            else:
                artifact_screenplay = EpisodeScreenplay.model_validate(content)
            screenplay_projection_verified = (
                artifact_screenplay.model_dump(mode="json")
                == screenplay.model_dump(mode="json")
            )
        except Exception:
            screenplay_projection_verified = False
    if not screenplay_projection_verified:
        errors.append("NARRATIVE_SCREENPLAY_PROJECTION_DRIFT")

    shots_projection_hash: str | None = None
    shots_projection_verified = False
    board = None
    board_payload: dict[str, Any] | None = None
    try:
        from app.evidence.repository import content_hash

        try:
            board_builder = _board_from_shot_rows
        except NameError:  # pragma: no cover - direct module import compatibility
            from app.domain.storyboard_ops import _board_from_shot_rows as board_builder

        shot_rows = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        board = board_builder(shot_rows, int(ep.get("episode_no") or 1))
        board_payload = board.model_dump(mode="json")
        from app.narrative import storyboard_authority_projection

        current_authority_projection = storyboard_authority_projection(board)
        artifact_authority_projection = storyboard_authority_projection(
            storyboard_artifact["content"]
        )
        shots_projection_hash = content_hash(current_authority_projection)
        shots_projection_verified = bool(
            storyboard_artifact["verified"]
            and artifact_authority_projection == current_authority_projection
        )
    except Exception:
        shots_projection_verified = False
    if not shots_projection_verified:
        errors.append("NARRATIVE_SHOTS_PROJECTION_DRIFT")

    screenplay_certificate = _review_certificate_binding(
        conn,
        certificate_id=ep.get("screenplay_completion_certificate_id"),
        kind="screenplay",
        episode_id=episode_id,
        artifact_id=screenplay_id,
        artifact_hash=screenplay_artifact.get("content_hash"),
        production_revision_id=ep.get("screenplay_production_revision_id"),
        artifact_contract_version=screenplay_artifact.get("contract_version"),
    )
    storyboard_certificate = _review_certificate_binding(
        conn,
        certificate_id=ep.get("storyboard_completion_certificate_id"),
        kind="storyboard",
        episode_id=episode_id,
        artifact_id=storyboard_id,
        artifact_hash=storyboard_artifact.get("content_hash"),
        production_revision_id=ep.get("storyboard_production_revision_id"),
        artifact_contract_version=storyboard_artifact.get("contract_version"),
    )
    if not screenplay_certificate["verified"]:
        errors.append("NARRATIVE_SCREENPLAY_CERTIFICATE_UNVERIFIED")
    if not storyboard_certificate["verified"]:
        errors.append("NARRATIVE_STORYBOARD_CERTIFICATE_UNVERIFIED")

    completion_authority_verified = False
    if board_payload is not None:
        try:
            from app.production.certificate import (
                verify_current_storyboard_completion_authority,
            )

            verify_current_storyboard_completion_authority(
                episode=ep,
                current_storyboard_content=board_payload,
            )
            completion_authority_verified = True
        except Exception:
            completion_authority_verified = False
    if not completion_authority_verified:
        errors.append("NARRATIVE_STORYBOARD_COMPLETION_AUTHORITY_UNVERIFIED")

    material = {
        "episode_id": episode_id,
        "published_screenplay": {
            key: screenplay_artifact.get(key)
            for key in ("artifact_id", "content_hash", "status", "verified")
        },
        "published_storyboard": {
            key: storyboard_artifact.get(key)
            for key in ("artifact_id", "content_hash", "status", "verified")
        },
        "screenplay_certificate": {
            key: screenplay_certificate.get(key)
            for key in (
                "certificate_id", "artifact_id", "artifact_hash",
                "production_revision_id", "contract_version",
                "qa_profile_version", "consumed", "verified",
            )
        },
        "storyboard_certificate": {
            key: storyboard_certificate.get(key)
            for key in (
                "certificate_id", "artifact_id", "artifact_hash",
                "production_revision_id", "contract_version",
                "qa_profile_version", "consumed", "verified",
            )
        },
        "storyboard_completion_authority_verified": completion_authority_verified,
        "shots_projection_hash": shots_projection_hash,
        "shots_projection_verified": shots_projection_verified,
        "screenplay_projection_verified": screenplay_projection_verified,
        "errors": sorted(set(errors)),
    }
    authority_version = _review_sha(material)[:32]
    return {
        "required": True,
        "verified": not errors,
        "authority_version": authority_version,
        "published_screenplay_artifact_id": screenplay_artifact.get("artifact_id"),
        "published_screenplay_artifact_hash": screenplay_artifact.get("content_hash"),
        "published_storyboard_artifact_id": storyboard_artifact.get("artifact_id"),
        "published_storyboard_artifact_hash": storyboard_artifact.get("content_hash"),
        "screenplay_completion_certificate_id": screenplay_certificate.get("certificate_id"),
        "screenplay_certificate_verified": screenplay_certificate.get("verified", False),
        "storyboard_completion_certificate_id": storyboard_certificate.get("certificate_id"),
        "storyboard_certificate_verified": storyboard_certificate.get("verified", False),
        "storyboard_completion_authority_verified": completion_authority_verified,
        "shots_projection_hash": shots_projection_hash,
        "shots_projection_verified": shots_projection_verified,
        "errors": sorted(set(errors)),
    }


def _ensure_review_wall_tables(conn=None) -> None:
    """存量数据库的进程内兼容迁移。"""
    db = conn or get_conn()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS video_version_archives (
            version_id TEXT PRIMARY KEY, archived_by TEXT NOT NULL DEFAULT 'user',
            reason TEXT, archived_at REAL NOT NULL,
            FOREIGN KEY(version_id) REFERENCES shot_versions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS review_action_audit (
            id TEXT PRIMARY KEY, action TEXT NOT NULL, scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL, target_version TEXT, idempotency_key TEXT,
            old_state_json TEXT NOT NULL DEFAULT '{}', new_state_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT, decided_by TEXT NOT NULL DEFAULT 'user', request_id TEXT, created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_review_action_idempotency
            ON review_action_audit(action, idempotency_key)
            WHERE idempotency_key IS NOT NULL AND idempotency_key != '';
        """
    )
    db.commit()


def _review_asset_qualification(conn, episode_id: str) -> dict[str, Any]:
    """Inspect the exact shot gallery source used by ``enqueue_shot``.

    The adopted version wins when it owns a gallery; otherwise the newest
    version with a gallery wins.  Selected legacy references without an
    explicit gate verdict are unverified and therefore fail closed for new
    production, while their historical videos remain readable.
    """
    rows = conn.execute(
        """SELECT v.id AS version_id, v.shot_id, v.version_no, v.image_inputs,
                  s.adopted_version_id
             FROM shot_versions v JOIN shots s ON s.id=v.shot_id
            WHERE s.episode_id=?
              AND v.status!='cleared'
            ORDER BY v.shot_id, v.version_no DESC""",
        (episode_id,),
    ).fetchall()
    by_shot: dict[str, list[Any]] = {}
    for row in rows:
        by_shot.setdefault(row["shot_id"], []).append(row)
    selected_rows: list[Any] = []
    for versions in by_shot.values():
        adopted_id = versions[0]["adopted_version_id"]
        adopted = next((row for row in versions if row["version_id"] == adopted_id), None)
        adopted_inputs = _review_json(adopted["image_inputs"], {}) if adopted else {}
        if adopted and adopted_inputs.get("reference_images"):
            selected_rows.append(adopted)
            continue
        fallback = next(
            (row for row in versions if _review_json(row["image_inputs"], {}).get("reference_images")),
            None,
        )
        if fallback:
            selected_rows.append(fallback)
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    qualified_inputs: list[dict[str, Any]] = []
    checked = 0
    for row in selected_rows:
        inputs = _review_json(row["image_inputs"], {})
        for ref in inputs.get("reference_images") or []:
            if ref.get("deleted") or not ref.get("selectedForSeedance"):
                continue
            checked += 1
            qa = ref.get("qa") or {}
            hard = list(qa.get("hard_failures") or ref.get("hard_failures") or [])
            gate = str(ref.get("gate_status") or ref.get("downstream_eligibility") or qa.get("status") or "").lower()
            if qa.get("qa_recovered"):
                gate = "unverified"
            payload = {
                "shot_id": row["shot_id"], "version_id": row["version_id"], "ref_id": ref.get("id"),
                "entity_type": ref.get("entity_type"), "entity_name": ref.get("entity_name"),
                "asset_version": ref.get("library_revision_id") or ref.get("library_view_id"),
                "rule_version": ref.get("rule_version") or qa.get("rule_version"),
                "hard_failures": hard,
            }
            if not gate:
                gate = "scored"
                payload["gate_status"] = gate
            payload["gate_status"] = gate
            payload["soft_warnings"] = [
                str(item) for item in (ref.get("soft_warnings") or qa.get("issues") or [])
            ]
            # Score-only：QA hard gate / unverified 只进 soft_warnings，不进 blockers（PRD QA-SO #32）。
            for msg in hard:
                warnings.append({**payload, "warning": f"qa_hard_failure:{msg}"})
            if qa.get("hard_gate_passed") is False:
                warnings.append({**payload, "warning": "hard_gate_not_passed_score_only"})
            if gate in {"failed", "hard_failed", "unverified", "unknown", "ineligible", "pending"}:
                warnings.append({**payload, "warning": f"gate_status:{gate}"})
            # 文件缺失会在媒体执行器中有界重建；耗尽后降级为其余锚点/纯文本。
            missing_file = bool(ref.get("file_missing") or ref.get("missing"))
            if missing_file:
                warnings.append({**payload, "warning": "asset_file_missing_retry_then_fallback"})
            else:
                qualified_inputs.append(payload)
            for warning in ref.get("soft_warnings") or qa.get("issues") or []:
                warnings.append({**payload, "warning": str(warning)})
    return {
        "eligible": not blockers,
        "status": "blocked" if blockers else ("passed" if checked else "no_selected_inputs"),
        "checked_inputs": checked,
        "inputs": qualified_inputs,
        "blockers": blockers,
        "soft_warnings": warnings,
    }


def _review_upstream_snapshot(episode_id: str) -> dict[str, Any]:
    conn = get_conn()
    ep_row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep_row:
        raise HTTPException(404, "分集不存在")
    ep = dict(ep_row)
    published_screenplay = ep.get("published_screenplay_artifact_id") or ep.get("screenplay_artifact_id")
    published_storyboard = ep.get("published_storyboard_artifact_id") or ep.get("storyboard_artifact_id")
    screenplay_qualified = _screenplay_ready(ep)
    active: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    for kind, run_id in (
        ("screenplay", ep.get("active_screenplay_run_id")),
        ("storyboard", ep.get("active_storyboard_run_id")),
    ):
        if not run_id:
            continue
        run = conn.execute("SELECT id, status, current_step_key, updated_at FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] not in _REVIEW_TERMINAL_RUN_STATES:
            seen_run_ids.add(run_id)
            active.append({
                "kind": kind, "run_id": run_id,
                "status": run["status"] if run else "unknown",
                "stage": run["current_step_key"] if run else None,
                "updated_at": run["updated_at"] if run else None,
                "source": "episode_pointer",
            })
    # 服务重启或启动阶段异常可能使 active_* 指针与 durable run 短暂失配。
    # 资格门禁同时反查持久化事实，避免把仍可恢复的上游任务误判为已结束。
    marks = ",".join("?" for _ in _REVIEW_ACTIVE_RUN_STATES)
    durable_runs = conn.execute(
        f"""SELECT id, workflow_type, status, current_step_key, updated_at
              FROM workflow_runs
             WHERE scope_type='episode' AND scope_id=?
               AND workflow_type IN ('screenplay', 'storyboard')
               AND status IN ({marks})
               AND recovered_by_run_id IS NULL
             ORDER BY updated_at DESC""",
        (episode_id, *sorted(_REVIEW_ACTIVE_RUN_STATES)),
    ).fetchall()
    for run in durable_runs:
        if run["id"] in seen_run_ids:
            continue
        # 历史兼容：旧版重启恢复竞态会留下 PAUSED_EXTERNAL 孤儿。
        # 若它已有更新的同类成功运行，当前剧集也已发布确认，
        # 这条旧运行已被可证明地取代，不应永久锁死生成资格。
        superseded_restart_orphan = False
        if (
            run["status"] == "PAUSED_EXTERNAL"
            and ep.get("status") in {"confirmed", "generating", "done", "mixed"}
            and published_storyboard
        ):
            successor = conn.execute(
                """SELECT 1 FROM workflow_runs
                     WHERE scope_type='episode' AND scope_id=? AND workflow_type=?
                       AND status='SUCCEEDED' AND updated_at>=?
                     LIMIT 1""",
                (episode_id, run["workflow_type"], run["updated_at"] or 0),
            ).fetchone()
            superseded_restart_orphan = successor is not None
        if superseded_restart_orphan:
            continue
        seen_run_ids.add(run["id"])
        active.append({
            "kind": run["workflow_type"],
            "run_id": run["id"],
            "status": run["status"],
            "stage": run["current_step_key"],
            "updated_at": run["updated_at"],
            "source": "workflow_run",
        })
    for kind in ("screenplay", "storyboard"):
        if task_registry.active(kind, episode_id) and not any(
            item["kind"] == kind for item in active
        ):
            active.append({
                "kind": kind,
                "run_id": None,
                "status": "RUNNING",
                "stage": None,
                "updated_at": None,
                "source": "task_registry",
            })
    # 旧任务没有 workflow_run 时，剧集状态仍要 fail-closed。
    if ep.get("status") in {"scripting", "storyboarding", "planned"} and not active:
        active.append({
            "kind": "upstream", "run_id": None, "status": ep.get("status"),
            "stage": None, "updated_at": None, "source": "episode_status",
        })
    confirmed = ep.get("status") in {"confirmed", "generating", "done", "mixed"}
    has_artifacts = bool(screenplay_qualified and published_screenplay and published_storyboard)
    assets = _review_asset_qualification(conn, episode_id)
    narrative_authority = _review_narrative_authority_snapshot(conn, ep)
    active_storyboard_shot_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
    ]
    blockers: list[str] = []
    if not screenplay_qualified:
        blockers.append("剧本尚未取得与当前版本一致的完成凭证")
    if not published_storyboard or not confirmed:
        blockers.append("分镜尚未完整确认")
    if active:
        blockers.append("编剧或分镜任务仍在运行")
    if narrative_authority["required"] and not narrative_authority["verified"]:
        blockers.append("叙事发布依赖已缺失或与当前正式投影不一致")
    if not assets["eligible"]:
        # 兼容历史快照；当前执行路径会重试资产并在耗尽后降级，不再阻断正向生产。
        assets["soft_warnings"].append({"warning": "asset_retry_then_fallback"})
    raw = {
        "episode_id": episode_id,
        "episode_status": ep.get("status"),
        "published_screenplay_artifact_id": published_screenplay,
        "confirmed_storyboard_artifact_id": published_storyboard,
        "screenplay_revision": ep.get("screenplay_production_revision_id"),
        "storyboard_revision": ep.get("storyboard_production_revision_id"),
        "active_upstream_runs": active,
        "asset_status": assets["status"],
        "asset_inputs": assets["inputs"],
        "asset_blockers": assets["blockers"],
        "asset_soft_warnings": assets["soft_warnings"],
        "active_storyboard_shot_ids": active_storyboard_shot_ids,
        "narrative_authority_required": bool(narrative_authority["required"]),
        "narrative_authority_verified": bool(narrative_authority["verified"]),
        "narrative_authority_version": narrative_authority.get("authority_version"),
        "narrative_authority": narrative_authority,
    }
    qualification_material = {
        **raw,
        "narrative_authority": {
            key: value
            for key, value in narrative_authority.items()
            if not key.startswith("narrative_review")
            and not key.startswith("narrative_calibration")
        },
        # The gallery may be copied into a newly queued version. The version
        # row is lineage, not an upstream dependency; hashing it would make a
        # run invalidate itself even when every asset/rule verdict is equal.
        "asset_inputs": [
            {key: value for key, value in item.items() if key != "version_id"}
            for item in assets["inputs"]
        ],
    }
    qualification_digest = _review_sha(qualification_material)[:32]
    qualification_version = (
        f"{narrative_authority['authority_version']}:{qualification_digest}"
        if narrative_authority["required"]
        else qualification_digest
    )
    return {
        **raw,
        "qualification_version": qualification_version,
        "eligible_for_production": bool(
            confirmed
            and has_artifacts
            and not active
            and assets["eligible"]
            and (not narrative_authority["required"] or narrative_authority["verified"])
        ),
        "blockers": blockers,
        "assets": assets,
        "server_time": now(),
    }


def _review_assert_positive_action(episode_id: str, expected_qualification_version: str | None = None) -> dict[str, Any]:
    snapshot = _review_upstream_snapshot(episode_id)
    if expected_qualification_version and expected_qualification_version != snapshot["qualification_version"]:
        raise HTTPException(409, {
            "code": "REVIEW_QUALIFICATION_CHANGED",
            "message": "上游或资产资格已变化，请重新预演",
            "qualification": snapshot,
        })
    if not snapshot["eligible_for_production"]:
        raise HTTPException(409, {
            "code": "REVIEW_PRODUCTION_BLOCKED",
            "message": "；".join(snapshot["blockers"]) or "当前不可执行正向媒体生产",
            "qualification": snapshot,
        })
    return snapshot


def _review_assert_shot_positive(shot_id: str, expected_qualification_version: str | None = None) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT episode_id FROM shots WHERE id=?", (shot_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "镜头不存在")
    return _review_assert_positive_action(row["episode_id"], expected_qualification_version)


def _review_assert_reference_restore(version_id: str, ref_id: str) -> dict[str, Any]:
    conn = get_conn()
    row = conn.execute(
        """SELECT s.episode_id, v.image_inputs FROM shot_versions v
             JOIN shots s ON s.id=v.shot_id WHERE v.id=?""",
        (version_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "视频版本不存在")
    snapshot = _review_assert_positive_action(row["episode_id"])
    refs = _review_json(row["image_inputs"], {}).get("reference_images") or []
    ref = next((item for item in refs if item.get("id") == ref_id), None)
    if ref is None:
        raise HTTPException(404, "参考图不存在")
    path = str(ref.get("path") or ref.get("image_path") or "").strip()
    url = str(ref.get("url") or "").strip()
    if not ((path and Path(path).is_file()) or url.startswith("data:image")):
        raise HTTPException(409, {
            "code": "REFERENCE_FILE_UNAVAILABLE",
            "message": "该参考图文件不可用，无法恢复为生产输入",
            "ref_id": ref_id,
        })
    return snapshot


def _review_write_audit(
    action: str, scope_type: str, scope_id: str, *, target_version: str | None = None,
    idempotency_key: str | None = None, old_state: Any = None, new_state: Any = None,
    reason: str | None = None, decided_by: str = "user", request_id: str | None = None,
    conn=None, commit: bool = True,
) -> dict[str, Any]:
    if conn is None:
        _ensure_review_wall_tables()
        conn = get_conn()
    if idempotency_key:
        existing = conn.execute(
            "SELECT * FROM review_action_audit WHERE action=? AND idempotency_key=?",
            (action, idempotency_key),
        ).fetchone()
        if existing:
            return dict(existing)
    audit_id = new_id("review_audit")
    conn.execute(
        """INSERT INTO review_action_audit(
               id, action, scope_type, scope_id, target_version, idempotency_key,
               old_state_json, new_state_json, reason, decided_by, request_id, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            audit_id, action, scope_type, scope_id, target_version, idempotency_key,
            json.dumps(old_state or {}, ensure_ascii=False),
            json.dumps(new_state or {}, ensure_ascii=False),
            reason, decided_by, request_id, now(),
        ),
    )
    if commit:
        conn.commit()
    return {"id": audit_id, "action": action, "scope_type": scope_type, "scope_id": scope_id}


@router.get("/episodes/{episode_id}/review-context")
def review_wall_context(episode_id: str):
    _ensure_review_wall_tables()
    conn = get_conn()
    snapshot = _review_upstream_snapshot(episode_id)
    archived = {
        row["version_id"]: dict(row)
        for row in conn.execute(
            """SELECT a.* FROM video_version_archives a JOIN shot_versions v ON v.id=a.version_id
                 JOIN shots s ON s.id=v.shot_id WHERE s.episode_id=?""", (episode_id,),
        ).fetchall()
    }
    return {
        "episode_id": episode_id,
        "object_version": _review_sha({
            "qualification": snapshot["qualification_version"],
            "archived_versions": sorted(archived),
        })[:32],
        "upstream": snapshot,
        "archived_versions": archived,
        "authorization_constraints": {
            "budget_cap_cny": {"type": "number", "unit": "CNY", "default": 150, "min": 1, "max": 100000, "step": 1, "finite": True},
            "wall_clock_cap_s": {"type": "number", "unit": "seconds", "default": 14400, "min": 60, "max": 604800, "step": 60, "finite": True},
            "add_budget_cny": {"type": "number", "unit": "CNY", "default": 50, "min": 1, "max": 100000, "step": 1, "finite": True},
            "add_wall_clock_s": {"type": "number", "unit": "seconds", "default": 3600, "min": 60, "max": 604800, "step": 60, "finite": True},
        },
        "server_time": now(),
    }


@router.post("/versions/{version_id}/archive")
def archive_video_version(version_id: str, body: dict | None = Body(None)):
    _ensure_review_wall_tables()
    body = body or {}
    conn = get_conn()
    row = conn.execute(
        """SELECT v.*, s.adopted_version_id FROM shot_versions v
             JOIN shots s ON s.id=v.shot_id WHERE v.id=?""", (version_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "视频版本不存在")
    if row["adopted_version_id"] == version_id:
        raise HTTPException(409, "当前采用版不能归档")
    conn.execute("BEGIN IMMEDIATE")
    try:
        inserted = conn.execute(
            """INSERT INTO video_version_archives(version_id, archived_by, reason, archived_at)
               VALUES(?,?,?,?) ON CONFLICT(version_id) DO NOTHING""",
            (
                version_id,
                current_actor_name(),
                str(body.get("reason") or "").strip() or None,
                now(),
            ),
        )
        if inserted.rowcount == 1:
            _review_write_audit(
                "video_version.archive", "version", version_id,
                reason=body.get("reason"), conn=conn, commit=False,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "version_id": version_id,
        "archived": True,
        "idempotent": inserted.rowcount == 0,
    }


@router.delete("/versions/{version_id}/archive")
def unarchive_video_version(version_id: str):
    _ensure_review_wall_tables()
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        deleted = conn.execute(
            "DELETE FROM video_version_archives WHERE version_id=?", (version_id,)
        )
        if deleted.rowcount == 1:
            _review_write_audit(
                "video_version.unarchive", "version", version_id,
                conn=conn, commit=False,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "version_id": version_id,
        "archived": False,
        "idempotent": deleted.rowcount == 0,
    }


@router.get("/review-wall/events")
def review_wall_events(episode_id: str, limit: int = 100):
    """脱敏埋点/审计投影：只返回稳定对象与状态，不返回批注正文。"""
    _ensure_review_wall_tables()
    limit = max(1, min(int(limit), 500))
    rows = get_conn().execute(
        """SELECT a.id, a.action, a.scope_type, a.scope_id, a.target_version,
                  a.decided_by, a.request_id, a.created_at
             FROM review_action_audit a
            WHERE (a.scope_type='episode' AND a.scope_id=?)
               OR (a.scope_type='shot' AND a.scope_id IN (SELECT id FROM shots WHERE episode_id=?))
            ORDER BY a.created_at DESC LIMIT ?""",
        (episode_id, episode_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _review_validate_authorization_number(
    value: Any, *, field: str, minimum: float, maximum: float, allow_none: bool = True,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise HTTPException(422, f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"{field} 必须是数字") from exc
    if not math.isfinite(number):
        raise HTTPException(422, f"{field} 必须是有限数")
    if number < minimum or number > maximum:
        raise HTTPException(422, f"{field} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return number
