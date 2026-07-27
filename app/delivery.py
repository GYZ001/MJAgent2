from __future__ import annotations

import html
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from app import config
from app.atomic_io import atomic_copy, atomic_write_text, atomic_zip_directory
from app.db import get_conn, new_id, now, rows_to_dicts
from app.evidence import repository
from app.evidence.media import grade_shot_video, record_video_candidate, validate_video_file
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.orchestration.engine import fingerprint

# 仅允许 new_id("delivery") / 测试稳定 id 形态，禁止路径分隔与穿越。
_PACKAGE_ID_RE = re.compile(r"^delivery_[A-Za-z0-9_-]+$")


def validate_package_id(package_id: str) -> str:
    """校验交付包 id，防止被拼进文件系统路径时发生穿越。"""
    value = (package_id or "").strip()
    if not value or not _PACKAGE_ID_RE.fullmatch(value):
        raise ValueError("非法的 package_id")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_summary(artifact_id: str | None) -> dict[str, Any] | None:
    if not artifact_id:
        return None
    artifact = repository.get_artifact(artifact_id)
    if not artifact:
        return None
    return {
        "id": artifact["id"],
        "type": artifact["type"],
        "version": artifact["version"],
        "status": artifact["status"],
        "trust_level": artifact["trust_level"],
        "content_hash": artifact["content_hash"],
        "contract_version": artifact["contract_version"],
        "prompt_version": artifact["prompt_version"],
        "model_snapshot": artifact.get("model_snapshot") or {},
    }


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?[^\s,'\"]{8,}"),
)


def _sanitize_delivery_value(value: Any, *, key: str = "") -> Any:
    """Remove workstation paths and credential-shaped fields from exported snapshots."""
    lowered = key.lower()
    if any(marker in lowered for marker in ("api_key", "access_token", "client_secret")):
        return "<redacted>"
    if isinstance(value, dict):
        return {item_key: _sanitize_delivery_value(item, key=item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_delivery_value(item, key=key) for item in value]
    if isinstance(value, str):
        if "path" in lowered and Path(value).is_absolute():
            return Path(value).name
        project_root = str(config.PROJECTS_DIR.resolve())
        return value.replace(project_root, "<PROJECTS_DIR>")
    return value


def _assert_no_delivery_secrets(package_dir: Path) -> None:
    for path in package_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".html", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                raise ValueError(f"交付安全扫描失败：{path.name} 包含疑似凭证")


def delivery_readiness(episode_id: str) -> dict[str, Any]:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise KeyError(episode_id)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall())
    checks: list[dict[str, Any]] = []

    def check(key: str, passed: bool, message: str, evidence: Any = None) -> None:
        checks.append({"key": key, "passed": bool(passed), "message": message, "evidence": evidence})

    check("shots_present", bool(shots), "至少存在一个分镜", {"count": len(shots)})
    numbers = [shot["shot_no"] for shot in shots]
    check("shot_order", numbers == list(range(1, len(shots) + 1)), "镜号从 1 连续递增", numbers)
    invalid_durations = [shot["shot_no"] for shot in shots if shot["duration_s"] not in config.ALLOWED_DURATIONS]
    check(
        "shot_duration_range",
        not invalid_durations,
        f"每镜时长为模型选择的 {config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S} 秒整数",
        {"invalid_shots": invalid_durations},
    )
    expected_total_duration = sum(shot["duration_s"] for shot in shots)
    final_path = (
        config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"])
        / "final" / "episode.mp4"
    )
    final_technical = validate_video_file(
        str(final_path), expected_duration_s=expected_total_duration
    )
    final_technical = {
        **final_technical,
        "issues": [
            issue.model_dump(mode="json") if hasattr(issue, "model_dump") else issue
            for issue in (final_technical.get("issues") or [])
        ],
    }
    final_valid = bool(final_technical.get("passed"))
    check(
        "final_video",
        final_valid,
        f"整集成片可解码且时长约为 {expected_total_duration} 秒",
        final_technical,
    )
    video_items: list[dict[str, Any]] = []
    for shot in shots:
        version = None
        if shot["adopted_version_id"]:
            version = conn.execute(
                "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
                (shot["adopted_version_id"],),
            ).fetchone()
        item: dict[str, Any] = {
            "shot_id": shot["id"],
            "shot_no": shot["shot_no"],
            "version_id": shot["adopted_version_id"],
            "ready": False,
        }
        if version and version["video_path"]:
            technical = json.loads(version["technical_validation_json"] or "{}")
            if not technical:
                try:
                    record_video_candidate(version["id"])
                    version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version["id"],)).fetchone()
                    technical = json.loads(version["technical_validation_json"] or "{}")
                except (OSError, ValueError):
                    technical = validate_video_file(
                        version["video_path"], expected_duration_s=shot["duration_s"]
                    )
            qa = json.loads(version["qa_json"] or "{}")
            graded = grade_shot_video(
                technical=technical,
                qa=qa,
                version_row=dict(version),
            )
            item.update({
                "ready": bool(technical.get("passed")),
                "path": version["video_path"],
                "artifact_id": version["artifact_id"],
                "technical": technical,
                "adoption_reason": version["adoption_reason"],
                "qa": qa,
                "grade": graded,
            })
        video_items.append(item)
    check(
        "adopted_videos",
        bool(video_items) and all(item["ready"] for item in video_items),
        "每镜都有已采用且通过技术校验的视频",
        {"missing_or_invalid": [item["shot_no"] for item in video_items if not item["ready"]]},
    )
    fatal_quality = [
        {
            "shot_no": item["shot_no"],
            "version_id": item.get("version_id"),
            "fatal_failures": (item.get("grade") or {}).get("fatal_failures") or [],
        }
        for item in video_items
        if (item.get("grade") or {}).get("fatal_failures")
    ]
    source_artifacts = [
        _artifact_summary(project["bible_artifact_id"]),
        _artifact_summary(ep["screenplay_artifact_id"]),
        _artifact_summary(ep["storyboard_artifact_id"]),
    ]
    source_artifacts = [artifact for artifact in source_artifacts if artifact]
    stale = [artifact["id"] for artifact in source_artifacts if artifact["status"] == "stale"]
    check("source_lineage", len(source_artifacts) >= 3 and not stale, "人物谱、剧本、分镜来源完整且未失效", {
        "artifacts": source_artifacts, "stale": stale,
    })
    expected_evidence = 3 + len(video_items)
    actual_evidence = len(source_artifacts) + sum(bool(item.get("artifact_id")) for item in video_items)
    coverage = actual_evidence / expected_evidence if expected_evidence else 0.0
    check("evidence_coverage", coverage >= 0.9, "证据覆盖率不低于 90%", {"coverage": coverage})
    blockers = [item for item in checks if not item["passed"]]
    warnings: list[dict[str, Any]] = []
    # Score-only：致命内容问题进入 warnings，不再阻断交付（PRD QA-SO #33）。
    if fatal_quality:
        warnings.append({
            "check": "fatal_video_quality",
            "code": "FATAL_VIDEO_QUALITY_SCORE_ONLY",
            "message": "已采用视频存在分身、错误文字等质量风险（仅评分，不阻断交付）",
            "detail": {"fatal_shots": fatal_quality},
        })
    for item in video_items:
        for issue in (item.get("technical") or {}).get("issues") or []:
            severity = issue.get("severity") if isinstance(issue, dict) else None
            if severity == "warning":
                warnings.append({"shot_no": item["shot_no"], **issue})
        qa = item.get("qa") or {}
        for message in qa.get("issues") or []:
            warnings.append({"shot_no": item["shot_no"], "code": "MEDIA_QUALITY", "message": message})
    return {
        "episode_id": episode_id,
        "project_id": ep["project_id"],
        "episode_no": ep["episode_no"],
        "title": ep["title"],
        "ready": not blockers,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "evidence_coverage": round(coverage, 4),
        "source_artifacts": source_artifacts,
        "videos": video_items,
    }


def _write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _copy_if_present(source: str | None, destination: Path) -> Path | None:
    if not source or not Path(source).is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    return atomic_copy(source, destination)


def build_delivery_package(
    episode_id: str,
    *,
    package_id: str | None = None,
    decided_by: str | None = None,
    decision: str | None = None,
    reason: str = "",
    accepted_risk: str | None = None,
    operation_started_at: float | None = None,
) -> dict[str, Any]:
    readiness = delivery_readiness(episode_id)
    if readiness["blockers"]:
        raise ValueError("交付硬门禁未通过：" + "；".join(item["message"] for item in readiness["blockers"]))
    if decision not in {None, "approve", "approve_with_risk"}:
        raise ValueError("decision 必须为 approve 或 approve_with_risk")
    if decision == "approve_with_risk" and not (accepted_risk or "").strip():
        raise ValueError("带风险批准必须填写 accepted_risk")
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if package_id is not None:
        package_id = validate_package_id(package_id)
    else:
        package_id = validate_package_id(new_id("delivery"))
    operation_started_at = operation_started_at or now()
    existing = conn.execute(
        "SELECT * FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()
    if existing:
        return {
            "package_id": existing["id"],
            "artifact_id": existing["artifact_id"],
            "trust_level": (repository.get_artifact(existing["artifact_id"]) or {}).get("trust_level", "T3"),
            "status": existing["status"],
            "package_path": existing["package_path"],
            "archive_path": str(existing["package_path"]) + ".zip",
            "manifest": json.loads(existing["manifest_json"]),
            "quality_report": json.loads(existing["quality_report_json"]),
        }
    delivery_root = (
        config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"]) / "delivery"
    ).resolve()
    package_dir = (delivery_root / package_id).resolve()
    if not package_dir.is_relative_to(delivery_root):
        raise ValueError("非法的 package_id")
    # A directory without its database pointer is an uncommitted crash remnant.
    # Rebuilding the same operation id is safe and avoids exposing a half package.
    if package_dir.exists():
        shutil.rmtree(package_dir)
    archive_candidate = Path(str(package_dir) + ".zip")
    if archive_candidate.exists():
        archive_candidate.unlink()
    package_dir.mkdir(parents=True, exist_ok=False)
    snapshots = package_dir / "snapshots"
    snapshots.mkdir()
    _write_json(
        snapshots / "character-bible.json",
        _sanitize_delivery_value(json.loads(project["bible_json"] or "{}")),
    )
    _write_json(
        snapshots / "screenplay.json",
        _sanitize_delivery_value(json.loads(ep["screenplay_json"] or "{}")),
    )
    shot_rows = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)
    ).fetchall())
    for shot in shot_rows:
        for field in ("characters", "dialogues"):
            shot[field] = json.loads(shot[field] or "[]")
    _write_json(
        snapshots / "storyboard.json",
        _sanitize_delivery_value({"episode_no": ep["episode_no"], "shots": shot_rows}),
    )

    files: list[dict[str, Any]] = []
    final_source = config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"]) / "final" / "episode.mp4"
    final_copy = _copy_if_present(str(final_source), package_dir / "media" / "episode.mp4")
    if final_copy:
        files.append({"role": "final_video", "path": final_copy.relative_to(package_dir).as_posix()})
    for item in readiness["videos"]:
        dest = package_dir / "media" / "shots" / f"shot-{item['shot_no']:03d}.mp4"
        copied = _copy_if_present(item.get("path"), dest)
        if copied:
            files.append({
                "role": "shot_video", "shot_no": item["shot_no"],
                "artifact_id": item.get("artifact_id"),
                "path": copied.relative_to(package_dir).as_posix(),
            })
    for snapshot in sorted(snapshots.iterdir()):
        files.append({"role": "snapshot", "path": snapshot.relative_to(package_dir).as_posix()})
    for item in files:
        path = package_dir / item["path"]
        item.update({"sha256": _sha256(path), "size_bytes": path.stat().st_size})

    manifest = {
        "schema_version": "1.0.0",
        "package_id": package_id,
        "episode": {"id": episode_id, "number": ep["episode_no"], "title": ep["title"]},
        "created_at": operation_started_at,
        "source_artifacts": readiness["source_artifacts"],
        "files": files,
        "reproducibility": {
            "input_fingerprint": fingerprint(readiness["source_artifacts"], files),
            "shot_duration_range_s": [config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S],
            "shot_duration_decided_by": "model",
        },
    }
    quality_report = {
        "schema_version": "1.0.0",
        "hard_gate_passed": True,
        "checks": _sanitize_delivery_value(readiness["checks"]),
        "evidence_coverage": readiness["evidence_coverage"],
        "warnings": _sanitize_delivery_value(readiness["warnings"]),
        "human_decision": {
            "decision": decision,
            "decided_by": decided_by,
            "reason": reason,
            "accepted_risk": accepted_risk,
        },
    }
    _write_json(package_dir / "manifest.json", manifest)
    _write_json(package_dir / "quality-report.json", quality_report)
    report_html = (
        "<!doctype html><meta charset='utf-8'><title>Delivery Quality Report</title>"
        f"<h1>第 {ep['episode_no']} 集交付质量报告</h1>"
        f"<p>硬门禁：通过；证据覆盖率：{readiness['evidence_coverage']:.1%}</p>"
        "<h2>检查项</h2><ul>"
        + "".join(
            f"<li>{'通过' if item['passed'] else '失败'} · {html.escape(item['message'])}</li>"
            for item in readiness["checks"]
        )
        + "</ul><h2>人工决定</h2>"
        + f"<p>{html.escape(decision or '等待人工门禁')} · {html.escape(reason or '')}</p>"
    )
    atomic_write_text(package_dir / "quality-report.html", report_html)
    known_lines = ["# Known Issues", ""]
    if readiness["warnings"]:
        known_lines.extend(f"- {item.get('message', item.get('code', '未知风险'))}" for item in readiness["warnings"])
    else:
        known_lines.append("- 无已知残余问题。")
    if accepted_risk:
        known_lines.extend(["", "## Accepted Risk", "", f"- {accepted_risk}"])
    atomic_write_text(package_dir / "known-issues.md", "\n".join(known_lines))
    _assert_no_delivery_secrets(package_dir)
    for role, filename in (
        ("quality_report_json", "quality-report.json"),
        ("quality_report_html", "quality-report.html"),
        ("known_issues", "known-issues.md"),
    ):
        report_path = package_dir / filename
        files.append({
            "role": role,
            "path": filename,
            "sha256": _sha256(report_path),
            "size_bytes": report_path.stat().st_size,
        })
    manifest["files"] = files
    manifest["reproducibility"]["input_fingerprint"] = fingerprint(
        readiness["source_artifacts"], files
    )
    _write_json(package_dir / "manifest.json", manifest)
    _assert_no_delivery_secrets(package_dir)
    # 先完成客户可下载的文件，再提交数据库指针；ZIP 失败不能留下“已批准但不可下载”的记录。
    archive_path = atomic_zip_directory(package_dir, archive_candidate)

    parent_ids = [artifact["id"] for artifact in readiness["source_artifacts"]]
    parent_ids.extend(item["artifact_id"] for item in readiness["videos"] if item.get("artifact_id"))
    artifact_id = f"art_delivery_{package_id.removeprefix('delivery_')}"
    artifact_content = {
        "package_id": package_id, "manifest": manifest, "quality_report": quality_report,
    }
    artifact = repository.get_artifact(artifact_id)
    if artifact and artifact["content_hash"] != repository.content_hash(
        artifact_content, str(package_dir / "manifest.json")
    ):
        raise ValueError("同一交付操作的恢复输入已变化，已停止覆盖原证据")
    if not artifact:
        artifact = repository.create_artifact(EvidenceArtifact(
            id=artifact_id,
            type="delivery_package",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T3",
            content=artifact_content,
            file_path=str(package_dir / "manifest.json"),
            parent_artifact_ids=parent_ids,
            contract_version="delivery-1.0.0",
        ))
    file_eval = Evaluation(
        evaluator_type="file",
        evaluator_name="delivery_manifest_validator",
        evaluator_version="1.0.0",
        status="passed",
        hard_gate_passed=True,
        score=100,
        evidence={"file_count": len(files), "checks": readiness["checks"]},
    )
    status = "waiting_human"
    approved_at = None
    if decision:
        human_eval = Evaluation(
            evaluator_type="human",
            evaluator_name=decided_by or "delivery_reviewer",
            evaluator_version="1.0.0",
            status="warning" if decision == "approve_with_risk" else "passed",
            hard_gate_passed=True,
            score=100,
            issues=[Issue(
                code="RISK_ACCEPTED",
                severity=IssueSeverity.WARNING,
                subject=artifact["id"],
                message=accepted_risk or reason or "人工带风险批准",
            )] if decision == "approve_with_risk" else [],
            evidence={"decision": decision, "reason": reason, "accepted_risk": accepted_risk},
        )
        if artifact["status"] != "approved":
            artifact = repository.commit_artifact(None, artifact["id"], [file_eval, human_eval])
        conn.execute(
            """INSERT INTO gate_decisions(
                   id, artifact_id, gate_key, decision, decided_by, reason, accepted_risk, created_at
               ) SELECT ?,?,?,?,?,?,?,?
                 WHERE NOT EXISTS (
                   SELECT 1 FROM gate_decisions WHERE artifact_id=? AND gate_key='delivery'
                 )""",
            (
                new_id("gate"), artifact["id"], "delivery", decision, decided_by or "user",
                reason, accepted_risk, now(), artifact["id"],
            ),
        )
        status = "approved"
        approved_at = now()
    else:
        existing_file_eval = conn.execute(
            "SELECT 1 FROM evaluations WHERE artifact_id=? AND evaluator_name='delivery_manifest_validator'",
            (artifact["id"],),
        ).fetchone()
        if not existing_file_eval:
            repository.create_evaluation(artifact["id"], file_eval)
    conn.execute(
        """INSERT INTO delivery_packages(
               id, episode_id, artifact_id, status, package_path, manifest_json,
               quality_report_json, known_issues, created_at, approved_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            package_id, episode_id, artifact["id"], status, str(package_dir),
            json.dumps(manifest, ensure_ascii=False), json.dumps(quality_report, ensure_ascii=False),
            "\n".join(known_lines), operation_started_at, approved_at,
        ),
    )
    conn.execute(
        "UPDATE episodes SET delivery_artifact_id=?, delivery_status=? WHERE id=?",
        (artifact["id"], status, episode_id),
    )
    conn.commit()
    return {
        "package_id": package_id,
        "artifact_id": artifact["id"],
        "trust_level": artifact["trust_level"],
        "status": status,
        "package_path": str(package_dir),
        "archive_path": archive_path,
        "manifest": manifest,
        "quality_report": quality_report,
    }


def approve_delivery(
    episode_id: str,
    *,
    decided_by: str,
    decision: str,
    reason: str,
    accepted_risk: str | None = None,
    approved_package_id: str | None = None,
    operation_started_at: float | None = None,
    package_id: str | None = None,
) -> dict[str, Any]:
    conn = get_conn()
    if package_id:
        package_id = validate_package_id(package_id)
        row = conn.execute(
            "SELECT * FROM delivery_packages WHERE id=? AND episode_id=?",
            (package_id, episode_id),
        ).fetchone()
        if not row:
            raise ValueError("指定的交付包不存在")
        if row["status"] != "waiting_human":
            raise ValueError(f"交付包当前状态为 {row['status']}，不可审核")
    else:
        row = conn.execute(
            "SELECT * FROM delivery_packages WHERE episode_id=? AND status='waiting_human' ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    if not row:
        raise ValueError("没有等待人工门禁的交付包")
    if decision not in {"approve", "approve_with_risk", "reject"}:
        raise ValueError("未知门禁决定")
    if not (decided_by or "").strip():
        raise ValueError("必须填写审核人")
    if not (reason or "").strip():
        raise ValueError("必须填写审核意见")
    if decision == "approve_with_risk" and not (accepted_risk or "").strip():
        raise ValueError("带风险批准必须填写 accepted_risk")
    approved_package_id = validate_package_id(approved_package_id or new_id("delivery"))
    if decision == "reject":
        conn.execute("UPDATE artifacts SET status='rejected' WHERE id=?", (row["artifact_id"],))
        conn.execute("UPDATE delivery_packages SET status='rejected' WHERE id=?", (row["id"],))
        conn.execute("UPDATE episodes SET delivery_status='rejected' WHERE id=?", (episode_id,))
        conn.execute(
            """INSERT INTO gate_decisions(
                   id, artifact_id, gate_key, decision, decided_by, reason, accepted_risk, created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (new_id("gate"), row["artifact_id"], "delivery", decision, decided_by, reason, accepted_risk, now()),
        )
        conn.commit()
        return {"artifact_id": row["artifact_id"], "decision": decision, "trust_level": "T3", "package_id": row["id"]}

    # CAS 领取草稿，避免用户重复点击并发生成两份 T5；失败时恢复为 waiting_human。
    claimed = conn.execute(
        "UPDATE delivery_packages SET status='approving' WHERE id=? AND status='waiting_human'",
        (row["id"],),
    )
    if claimed.rowcount != 1:
        conn.rollback()
        raise ValueError("交付草稿已由另一审批任务处理")
    conn.commit()
    try:
        approved = build_delivery_package(
            episode_id,
            package_id=approved_package_id,
            decided_by=decided_by,
            decision=decision,
            reason=reason,
            accepted_risk=accepted_risk,
            operation_started_at=operation_started_at,
        )
    except Exception:
        conn.execute(
            "UPDATE delivery_packages SET status='waiting_human' WHERE id=? AND status='approving'",
            (row["id"],),
        )
        conn.commit()
        raise
    # 交付草稿是已经落盘并计算哈希的不可变证据。批准产生新快照后，旧草稿只改数据库生命周期状态。
    conn.execute("UPDATE delivery_packages SET status='superseded' WHERE id=?", (row["id"],))
    conn.execute("UPDATE artifacts SET status='superseded' WHERE id=?", (row["artifact_id"],))
    conn.commit()
    return {
        **approved,
        "decision": decision,
        "superseded_package_id": row["id"],
        "superseded_artifact_id": row["artifact_id"],
    }


def add_customer_feedback(
    episode_id: str,
    *,
    message: str,
    created_by: str,
    issue_code: str | None = None,
    rating: int | None = None,
    request_revision: bool = False,
) -> dict[str, Any]:
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep or not ep["delivery_artifact_id"]:
        raise ValueError("本集尚无可关联的交付 Artifact")
    if rating is not None and not 1 <= int(rating) <= 5:
        raise ValueError("rating 必须在 1~5")
    revision_run_id = None
    if request_revision:
        revision_run_id = repository.create_run(
            workflow_type="delivery_revision",
            scope_type="episode",
            scope_id=episode_id,
            input_fingerprint=fingerprint(ep["delivery_artifact_id"], message, issue_code),
            requested_by=created_by,
            trigger_type="customer_feedback",
            policy_snapshot={"source_delivery_artifact_id": ep["delivery_artifact_id"]},
        )
    feedback_id = new_id("feedback")
    conn.execute(
        """INSERT INTO customer_feedback(
               id, episode_id, artifact_id, issue_code, rating, message, created_by,
               revision_run_id, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            feedback_id, episode_id, ep["delivery_artifact_id"], issue_code, rating,
            message.strip(), created_by, revision_run_id, now(),
        ),
    )
    repository.create_evaluation(
        ep["delivery_artifact_id"],
        Evaluation(
            evaluator_type="human", evaluator_name="customer_feedback",
            evaluator_version="1.0.0", status="warning" if rating and rating < 4 else "passed",
            hard_gate_passed=True,
            score=float(rating * 20) if rating else None,
            issues=[Issue(
                code=issue_code or "CUSTOMER_FEEDBACK",
                severity=IssueSeverity.WARNING,
                subject=ep["delivery_artifact_id"],
                message=message.strip(),
            )],
            evidence={"feedback_id": feedback_id, "delivery_snapshot_immutable": True},
        ),
    )
    conn.commit()
    return {"feedback_id": feedback_id, "revision_run_id": revision_run_id}
