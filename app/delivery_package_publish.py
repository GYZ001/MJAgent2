"""build_delivery_package 的门禁/租约推进/最终发布事务：硬门禁检查、
package_id 解析、operation lease 一致性、分镜发布权威/已采纳视频漂移检查、
lease 阶段推进，以及把交付包正式落 delivery_packages/artifacts/evaluations
三张表的提交事务。

`validate_package_id`/`_assert_delivery_operation_owner`/
`_episode_release_status_cas_clause` 定义在 app.delivery 里且被该模块其余
函数共用，本文件在函数体内惰性导入它们，避免与 app.delivery 顶层互相导入成环。

`new_id` 同样惰性从 `app.delivery` 取（而不是直接 `from app.db import new_id`）：
测试用 `monkeypatch.setattr(delivery, "new_id", ...)` 精确注入交付发布事务中段的
崩溃点（见 tests/test_delivery_promotion_recovery.py），只打 app.delivery 自己
的绑定；若这里直接从 app.db 导入会持有独立绑定，patch 会静默失效。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.db import now
from app.evidence import repository
from app.harness.types import Evaluation


def _raise_if_delivery_gate_blocked(readiness: dict) -> list[dict]:
    """交付硬门禁：readiness['blockers'] 非空即拒绝构建，返回原始 blockers 供质检报告复用。"""
    gate_findings = list(readiness["blockers"])
    if gate_findings:
        summary = "；".join(
            str(item.get("message") or item.get("key") or "未知阻塞")
            for item in gate_findings[:5]
        )
        raise ValueError(f"交付硬门禁未通过：{summary}")
    return gate_findings


def _resolve_delivery_package_id(package_id: str | None) -> str:
    """未显式传入 package_id 时生成新 id；两条路径都要过 validate_package_id 校验。"""
    from app.delivery import new_id, validate_package_id

    if package_id is not None:
        return validate_package_id(package_id)
    return validate_package_id(new_id("delivery"))


def _assert_delivery_operation_lease_consistent(
    conn,
    *,
    package_id: str,
    operation_request_fingerprint: str | None,
    operation_lease_owner: str | None,
) -> None:
    """operation lease 的 fingerprint/owner 必须成对出现；owner 存在时校验其仍持有租约。"""
    from app.delivery import _assert_delivery_operation_owner

    if bool(operation_request_fingerprint) != bool(operation_lease_owner):
        raise ValueError("交付构建 operation lease 参数不完整")
    if operation_lease_owner:
        _assert_delivery_operation_owner(
            conn,
            package_id=package_id,
            request_fingerprint=str(operation_request_fingerprint),
            lease_owner=operation_lease_owner,
        )


def _raise_if_delivery_authority_drifted(
    conn, episode_id: str, release_authority: dict, video_delivery_manifest: dict,
    *, storyboard_drift_message: str, video_drift_message: str,
) -> None:
    """核验分镜发布权威与已采纳视频未漂移（构建期与发布前提交事务里各调用一次）。"""
    from app.downstream_authority import (
        current_adopted_video_delivery_manifest,
        verify_current_storyboard_release_authority,
    )

    if verify_current_storyboard_release_authority(
        episode_id, conn=conn,
    ) != release_authority:
        raise ValueError(storyboard_drift_message)
    if current_adopted_video_delivery_manifest(
        episode_id, conn=conn,
    ) != video_delivery_manifest:
        raise ValueError(video_drift_message)


def _advance_delivery_operation_phase(
    conn, *, package_id: str, operation_request_fingerprint: str | None,
    operation_lease_owner: str | None, phase: str, conflict_message: str,
) -> None:
    """把 operation receipt 的 promotion_phase 推进到 phase（CAS：仍持有 lease 才允许）。"""
    updated = conn.execute(
        f"""UPDATE delivery_operation_receipts
              SET promotion_phase='{phase}',updated_at=?
            WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
              AND status='running'""",
        (time.time(), package_id, operation_request_fingerprint, operation_lease_owner),
    )
    if updated.rowcount != 1:
        raise ValueError(conflict_message)
    conn.commit()


def _prepare_delivery_artifact_content(
    package_id: str, package_dir: Path, manifest: dict, quality_report: dict, readiness: dict,
) -> tuple[str, list[str], dict[str, Any], str]:
    """算出交付 Artifact 的 id/父血缘/内容/内容哈希；若已存在同 id 但内容哈希不同则拒绝覆盖。"""
    parent_ids = [artifact["id"] for artifact in readiness["source_artifacts"]]
    parent_ids.extend(
        item["artifact_id"] for item in readiness["videos"] if item.get("artifact_id")
    )
    artifact_id = f"art_delivery_{package_id.removeprefix('delivery_')}"
    artifact_content = {
        "package_id": package_id, "manifest": manifest, "quality_report": quality_report,
    }
    artifact = repository.get_artifact(artifact_id)
    expected_artifact_hash = repository.content_hash(
        artifact_content, str(package_dir / "manifest.json")
    )
    if artifact and artifact["content_hash"] != expected_artifact_hash:
        raise ValueError("同一交付操作的恢复输入已变化，已停止覆盖原证据")
    return artifact_id, parent_ids, artifact_content, expected_artifact_hash


def _upsert_delivery_artifact_row(
    conn, *, artifact_id: str, episode_id: str, package_dir: Path,
    artifact_content: dict, expected_artifact_hash: str, parent_ids: list[str],
) -> None:
    """交付 Artifact 行不存在则插入（版本号自增）；已存在则要求内容哈希与本次一致（CAS）。"""
    artifact_row = conn.execute(
        "SELECT * FROM artifacts WHERE id=?", (artifact_id,),
    ).fetchone()
    if artifact_row is None:
        version = conn.execute(
            """SELECT COALESCE(MAX(version),0)+1 AS n FROM artifacts
               WHERE type='delivery_package' AND scope_type='episode' AND scope_id=?""",
            (episode_id,),
        ).fetchone()["n"]
        conn.execute(
            """INSERT INTO artifacts(
                   id,type,scope_type,scope_id,version,status,trust_level,
                   content_json,file_path,content_hash,parent_artifact_ids_json,
                   contract_version,created_at
               ) VALUES(?,'delivery_package','episode',?,?,'validated','T3',?,?,?,?,?,?)""",
            (
                artifact_id,
                episode_id,
                version,
                json.dumps(artifact_content, ensure_ascii=False),
                str(package_dir / "manifest.json"),
                expected_artifact_hash,
                json.dumps(parent_ids, ensure_ascii=False),
                "delivery-1.0.0",
                now(),
            ),
        )
    elif str(artifact_row["content_hash"] or "") != expected_artifact_hash:
        raise ValueError("交付 Artifact CAS 内容冲突")


def _insert_delivery_file_evaluation_if_absent(
    conn, artifact_id: str, file_eval: Evaluation,
) -> None:
    """delivery_manifest_validator 评估行按 artifact_id 去重插入（同一 Artifact 不重复评估）。"""
    from app.delivery import new_id

    existing_file_eval = conn.execute(
        """SELECT 1 FROM evaluations
           WHERE artifact_id=? AND evaluator_name='delivery_manifest_validator'""",
        (artifact_id,),
    ).fetchone()
    if existing_file_eval:
        return
    conn.execute(
        """INSERT INTO evaluations(
               id,artifact_id,evaluator_type,evaluator_name,evaluator_version,
               status,hard_gate_passed,evaluation_role,score_status,runtime_blocking,
               retry_eligible,score,dimension_scores_json,issues_json,evidence_json,
               confidence,recovered,created_at
           ) VALUES(?,?, 'file',?,?,'passed',1,'runtime_gate','scored',1,0,100,
                    '{}','[]',?,1,0,?)""",
        (
            new_id("eval"), artifact_id, file_eval.evaluator_name,
            file_eval.evaluator_version,
            json.dumps(file_eval.evidence, ensure_ascii=False, sort_keys=True),
            now(),
        ),
    )


def _insert_delivery_package_row_and_cas_episode(
    conn, *, package_id: str, episode_id: str, artifact_id: str, package_dir: Path,
    manifest: dict, quality_report: dict, known_lines: list[str],
    operation_started_at: float, release_authority: dict,
) -> None:
    """插入最终 delivery_packages 行，并 CAS 把 episodes 的交付指针切到本次 Artifact。"""
    from app.delivery import _episode_release_status_cas_clause

    conn.execute(
        """INSERT INTO delivery_packages(
               id,episode_id,artifact_id,status,package_path,manifest_json,
               quality_report_json,known_issues,created_at,approved_at
           ) VALUES(?,?,?,'waiting_human',?,?,?, ?,?,NULL)""",
        (
            package_id, episode_id, artifact_id, str(package_dir),
            json.dumps(manifest, ensure_ascii=False),
            json.dumps(quality_report, ensure_ascii=False),
            "\n".join(known_lines), operation_started_at,
        ),
    )
    status_clause, status_params = _episode_release_status_cas_clause(conn, episode_id)
    cursor = conn.execute(
        f"""UPDATE episodes
              SET delivery_artifact_id=?,delivery_status='waiting_human'
            WHERE id=?
              AND storyboard_artifact_id=?
              AND published_storyboard_artifact_id=?
              AND storyboard_production_revision_id=?
              AND storyboard_completion_certificate_id=?
              AND {status_clause}""",
        (
            artifact_id,
            episode_id,
            release_authority["published_storyboard_artifact_id"],
            release_authority["published_storyboard_artifact_id"],
            release_authority["storyboard_production_revision_id"],
            release_authority["storyboard_completion_certificate_id"],
            *status_params,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("交付发布发生分镜权威 CAS 冲突")


def _commit_delivery_package_publication(
    conn, *, episode_id: str, package_id: str, artifact_id: str, package_dir: Path,
    release_authority: dict, video_delivery_manifest: dict, artifact_content: dict,
    expected_artifact_hash: str, parent_ids: list[str], file_eval: Evaluation,
    manifest: dict, quality_report: dict, known_lines: list[str],
    operation_started_at: float, operation_lease_owner: str | None,
    operation_request_fingerprint: str | None,
) -> None:
    """交付发布的落盘事务：核验权威未漂移 -> upsert Artifact/评估 -> 插入交付包行并 CAS。

    调用前调用方必须已提交任何既有事务；本函数自行 BEGIN IMMEDIATE，失败时在
    except 的第一条语句里 rollback，成功路径以 conn.commit() 收尾。
    """
    from app.delivery import _assert_delivery_operation_owner

    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _raise_if_delivery_authority_drifted(
            conn, episode_id, release_authority, video_delivery_manifest,
            storyboard_drift_message="交付发布前分镜发布权威发生漂移",
            video_drift_message="交付发布前已采纳视频发生漂移",
        )
        if operation_lease_owner:
            _assert_delivery_operation_owner(
                conn,
                package_id=package_id,
                request_fingerprint=str(operation_request_fingerprint),
                lease_owner=operation_lease_owner,
            )
        _upsert_delivery_artifact_row(
            conn, artifact_id=artifact_id, episode_id=episode_id, package_dir=package_dir,
            artifact_content=artifact_content, expected_artifact_hash=expected_artifact_hash,
            parent_ids=parent_ids,
        )
        _insert_delivery_file_evaluation_if_absent(conn, artifact_id, file_eval)
        _insert_delivery_package_row_and_cas_episode(
            conn, package_id=package_id, episode_id=episode_id, artifact_id=artifact_id,
            package_dir=package_dir, manifest=manifest, quality_report=quality_report,
            known_lines=known_lines, operation_started_at=operation_started_at,
            release_authority=release_authority,
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
