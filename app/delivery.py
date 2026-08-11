from __future__ import annotations

import html
import hashlib
import json
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from app import config
from app.atomic_io import atomic_copy, atomic_write_text, atomic_zip_directory
from app.db import get_conn, new_id, now, rows_to_dicts
from app.evidence import repository
from app.evidence.media import grade_shot_video, validate_video_file
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity
from app.orchestration.engine import fingerprint

# 仅允许 new_id("delivery") / 测试稳定 id 形态，禁止路径分隔与穿越。
_PACKAGE_ID_RE = re.compile(r"^delivery_[A-Za-z0-9_-]+$")
_DELIVERY_OPERATION_LEASE_S = 4 * 60 * 60


def _ensure_delivery_operation_receipts(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_operation_receipts(
               package_id TEXT PRIMARY KEY,
               episode_id TEXT NOT NULL,
               request_fingerprint TEXT NOT NULL,
               lease_owner TEXT NOT NULL,
               lease_expires_at REAL NOT NULL,
               status TEXT NOT NULL,
               result_json TEXT NOT NULL DEFAULT '{}',
               updated_at REAL NOT NULL
           )"""
    )


def claim_delivery_package_operation(
    *,
    package_id: str,
    episode_id: str,
    request_fingerprint: str,
    allow_interrupted_takeover: bool = False,
    conn=None,
) -> tuple[str | None, dict[str, Any] | None]:
    db = conn or get_conn()
    owner = uuid.uuid4().hex
    stamp = time.time()
    try:
        db.execute("BEGIN IMMEDIATE")
        _ensure_delivery_operation_receipts(db)
        row = db.execute(
            "SELECT * FROM delivery_operation_receipts WHERE package_id=?",
            (package_id,),
        ).fetchone()
        if row:
            if (
                str(row["episode_id"]) != episode_id
                or str(row["request_fingerprint"]) != request_fingerprint
            ):
                raise ValueError("交付 operation id 已绑定不同请求")
            if row["status"] == "succeeded":
                result = json.loads(row["result_json"] or "{}")
                db.commit()
                return None, result
            if (
                row["status"] == "running"
                and float(row["lease_expires_at"] or 0) > stamp
                and not allow_interrupted_takeover
            ):
                db.commit()
                raise ValueError("相同交付操作正在执行中")
            updated = db.execute(
                """UPDATE delivery_operation_receipts
                      SET lease_owner=?,lease_expires_at=?,status='running',updated_at=?
                    WHERE package_id=? AND request_fingerprint=?
                      AND (status!='running' OR lease_expires_at<=? OR ?)""",
                (
                    owner,
                    stamp + _DELIVERY_OPERATION_LEASE_S,
                    stamp,
                    package_id,
                    request_fingerprint,
                    stamp,
                    int(allow_interrupted_takeover),
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("交付 operation lease CAS 冲突")
        else:
            db.execute(
                """INSERT INTO delivery_operation_receipts(
                       package_id,episode_id,request_fingerprint,lease_owner,
                       lease_expires_at,status,result_json,updated_at
                   ) VALUES(?,?,?,?,?,'running','{}',?)""",
                (
                    package_id,
                    episode_id,
                    request_fingerprint,
                    owner,
                    stamp + _DELIVERY_OPERATION_LEASE_S,
                    stamp,
                ),
            )
        db.commit()
        return owner, None
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def _assert_delivery_operation_owner(
    conn,
    *,
    package_id: str,
    request_fingerprint: str,
    lease_owner: str,
) -> None:
    row = conn.execute(
        """SELECT 1 FROM delivery_operation_receipts
            WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
              AND status='running' AND lease_expires_at>?""",
        (package_id, request_fingerprint, lease_owner, time.time()),
    ).fetchone()
    if row is None:
        raise ValueError("交付 operation lease 已失效")


def finish_delivery_package_operation(
    *,
    package_id: str,
    request_fingerprint: str,
    lease_owner: str,
    result: dict[str, Any],
    succeeded: bool,
    conn=None,
) -> None:
    db = conn or get_conn()
    cursor = db.execute(
        """UPDATE delivery_operation_receipts
              SET status=?,result_json=?,lease_expires_at=0,updated_at=?
            WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
              AND status='running'""",
        (
            "succeeded" if succeeded else "failed",
            json.dumps(result, ensure_ascii=False, default=str),
            time.time(),
            package_id,
            request_fingerprint,
            lease_owner,
        ),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise ValueError("交付 operation 完成 CAS 冲突")
    db.commit()


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


def _archive_matches_directory(archive_path: Path, package_path: Path) -> bool:
    if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
        return False
    expected = {
        path.relative_to(package_path).as_posix()
        for path in package_path.rglob("*")
        if path.is_file()
    }
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != expected:
                return False
            for relative in expected:
                source = package_path / relative
                info = archive.getinfo(relative)
                if int(info.file_size) != int(source.stat().st_size):
                    return False
                digest = hashlib.sha256()
                with archive.open(relative) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != _sha256(source):
                    return False
            return True
    except (OSError, zipfile.BadZipFile):
        return False


def _delivery_approval_snapshot(conn, row, episode_id: str) -> dict[str, Any]:
    artifact = repository.get_artifact(str(row["artifact_id"]), conn=conn)
    if artifact is None or artifact["type"] != "delivery_package":
        raise ValueError("交付草稿证据不存在")
    if artifact["scope_type"] != "episode" or artifact["scope_id"] != episode_id:
        raise ValueError("交付草稿证据作用域不匹配")
    package_dir = Path(str(row["package_path"])).resolve()
    manifest_path = package_dir / "manifest.json"
    archive_path = Path(str(package_dir) + ".zip")
    if not package_dir.is_dir() or not manifest_path.is_file():
        raise ValueError("交付草稿文件已丢失")
    try:
        manifest = json.loads(str(row["manifest_json"] or "{}"))
        disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("交付草稿 manifest 损坏") from exc
    if not isinstance(manifest, dict) or manifest != disk_manifest:
        raise ValueError("交付草稿 manifest 与审核快照不一致")
    if repository.content_hash(
        artifact.get("content"), artifact.get("file_path"),
    ) != str(artifact.get("content_hash") or ""):
        raise ValueError("交付草稿证据内容已被篡改")
    for item in manifest.get("files") or []:
        candidate = (package_dir / str(item.get("path") or "")).resolve()
        if package_dir not in candidate.parents or not candidate.is_file():
            raise ValueError("交付草稿包含非法或缺失文件")
        if (
            _sha256(candidate) != str(item.get("sha256") or "")
            or candidate.stat().st_size != int(item.get("size_bytes") or -1)
        ):
            raise ValueError("交付草稿文件已被篡改")
    if not _archive_matches_directory(archive_path, package_dir):
        raise ValueError("交付草稿压缩包与审核快照不一致")
    from app.downstream_authority import (
        current_adopted_video_delivery_manifest,
        verify_current_storyboard_release_authority,
    )

    if verify_current_storyboard_release_authority(
        episode_id, conn=conn,
    ) != manifest.get("storyboard_release_authority"):
        raise ValueError("交付草稿的分镜发布权威已漂移，请刷新后重建")
    if current_adopted_video_delivery_manifest(
        episode_id, conn=conn,
    ) != manifest.get("video_delivery_manifest"):
        raise ValueError("交付草稿的已采纳视频已漂移，请刷新后重建")
    return {
        "artifact": artifact,
        "manifest": manifest,
        "package_dir": package_dir,
        "manifest_sha256": _sha256(manifest_path),
        "archive_sha256": _sha256(archive_path),
    }


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


def _authorized_source_chapters(conn: Any, episode: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = episode["source_chapters"]
    parse_error = ""
    try:
        decoded = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = []
        parse_error = "episodes.source_chapters 不是有效 JSON"
    if not isinstance(decoded, list):
        decoded = []
        parse_error = "episodes.source_chapters 必须是章节索引列表"

    invalid_references: list[str] = []
    indexes: list[int] = []
    for value in decoded:
        if isinstance(value, bool):
            invalid_references.append(str(value))
            continue
        if isinstance(value, int):
            index = value
        elif isinstance(value, str):
            try:
                index = int(value.strip())
            except ValueError:
                invalid_references.append(value)
                continue
        else:
            invalid_references.append(str(value))
            continue
        indexes.append(index)
    indexes = list(dict.fromkeys(indexes))

    rows: list[Any] = []
    if indexes:
        marks = ",".join("?" for _ in indexes)
        rows = conn.execute(
            f"SELECT id,project_id,idx,title,content FROM chapters "
            f"WHERE project_id=? AND idx IN ({marks}) ORDER BY idx",
            (episode["project_id"], *indexes),
        ).fetchall()
    records = [
        {
            "chapter_id": int(row["id"]),
            "project_id": str(row["project_id"]),
            "chapter_idx": int(row["idx"]),
            "title": str(row["title"] or ""),
            "content_sha256": hashlib.sha256(
                str(row["content"] or "").encode("utf-8")
            ).hexdigest(),
        }
        for row in rows
    ]
    resolved_indexes = {record["chapter_idx"] for record in records}
    missing_indexes = [index for index in indexes if index not in resolved_indexes]
    foreign_matches: list[dict[str, Any]] = []
    if missing_indexes:
        marks = ",".join("?" for _ in missing_indexes)
        foreign_rows = conn.execute(
            f"SELECT idx,project_id FROM chapters "
            f"WHERE project_id<>? AND idx IN ({marks}) ORDER BY idx,project_id",
            (episode["project_id"], *missing_indexes),
        ).fetchall()
        foreign_matches = [
            {"chapter_idx": int(row["idx"]), "project_id": str(row["project_id"])}
            for row in foreign_rows
        ]
    evidence = {
        "authorized_indices": indexes,
        "resolved_chapters": records,
        "missing_indices": missing_indexes,
        "foreign_project_matches": foreign_matches,
        "invalid_references": invalid_references,
        "parse_error": parse_error or None,
    }
    return records, evidence


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

    storyboard_release_authority: dict[str, Any] | None = None
    storyboard_release_error: str | None = None
    try:
        from app.downstream_authority import verify_current_storyboard_release_authority

        storyboard_release_authority = verify_current_storyboard_release_authority(
            episode_id,
            conn=conn,
        )
    except (KeyError, TypeError, ValueError) as exc:
        storyboard_release_error = str(exc)
    check(
        "storyboard_release_authority",
        storyboard_release_authority is not None,
        "分镜已确认，并绑定当前已发布 revision、已消费凭证与 release qualification",
        storyboard_release_authority or {"error": storyboard_release_error},
    )
    video_delivery_manifest: dict[str, Any] | None = None
    video_manifest_error: str | None = None
    try:
        from app.downstream_authority import current_adopted_video_delivery_manifest

        video_delivery_manifest = current_adopted_video_delivery_manifest(
            episode_id,
            conn=conn,
        )
    except (OSError, TypeError, ValueError) as exc:
        video_manifest_error = str(exc)
    check(
        "adopted_video_manifest",
        video_delivery_manifest is not None,
        "每镜采用关系、视频 Artifact、文件哈希和倍速已形成内容寻址快照",
        video_delivery_manifest or {"error": video_manifest_error},
    )

    source_chapters, source_chapter_evidence = _authorized_source_chapters(conn, ep)
    source_chapters_valid = bool(source_chapter_evidence["authorized_indices"]) and not any((
        source_chapter_evidence["parse_error"],
        source_chapter_evidence["invalid_references"],
        source_chapter_evidence["missing_indices"],
        source_chapter_evidence["foreign_project_matches"],
    ))
    check(
        "source_chapters",
        source_chapters_valid,
        "授权章节列表非空，且每章存在并属于当前项目",
        source_chapter_evidence,
    )
    check("shots_present", bool(shots), "至少存在一个分镜", {"count": len(shots)})
    numbers = [shot["shot_no"] for shot in shots]
    check("shot_order", numbers == sorted(set(numbers)), "已采纳镜头按镜号递增且不重复", numbers)
    invalid_durations = [shot["shot_no"] for shot in shots if shot["duration_s"] not in config.ALLOWED_DURATIONS]
    check(
        "shot_duration_range",
        not invalid_durations,
        f"每镜时长为模型选择的 {config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S} 秒整数",
        {"invalid_shots": invalid_durations},
    )
    playback_by_shot: dict[str, float] = {}
    for shot in shots:
        rate = 1.0
        if shot["adopted_version_id"]:
            rate_row = conn.execute(
                "SELECT playback_rate FROM shot_versions WHERE id=?",
                (shot["adopted_version_id"],),
            ).fetchone()
            if rate_row:
                rate = float(rate_row["playback_rate"] or 1.0)
        playback_by_shot[str(shot["id"])] = rate
    expected_total_duration = sum(
        float(shot["duration_s"] or 0) / playback_by_shot[str(shot["id"])]
        for shot in shots
    )
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
    final_outdated = final_path.with_suffix(".stale").is_file()
    final_technical["outdated"] = final_outdated
    final_edit_report: dict[str, Any] | None = None
    final_edit_report_path = final_path.with_name("episode.edit-report.json")
    if final_edit_report_path.is_file():
        try:
            loaded_report = json.loads(final_edit_report_path.read_text(encoding="utf-8"))
            if isinstance(loaded_report, dict):
                final_edit_report = loaded_report
        except (OSError, ValueError, TypeError):
            final_edit_report = None
    final_manifest_hash = (
        str(final_edit_report.get("video_delivery_manifest_hash") or "")
        if isinstance(final_edit_report, dict)
        else ""
    )
    check(
        "final_video_manifest_binding",
        bool(
            video_delivery_manifest
            and final_manifest_hash == video_delivery_manifest["manifest_hash"]
        ),
        "整集成片精确绑定当前已采纳视频 manifest",
        {
            "expected": (
                video_delivery_manifest.get("manifest_hash")
                if video_delivery_manifest else None
            ),
            "actual": final_manifest_hash or None,
        },
    )
    final_expected_sha256 = (
        str(final_edit_report.get("final_video_sha256") or "")
        if isinstance(final_edit_report, dict)
        else ""
    )
    final_actual_sha256 = _sha256(final_path) if final_path.is_file() else ""
    check(
        "final_video_content_binding",
        bool(final_expected_sha256 and final_expected_sha256 == final_actual_sha256),
        "整集成片实际文件哈希与合片发布证明一致",
        {
            "expected": final_expected_sha256 or None,
            "actual": final_actual_sha256 or None,
        },
    )
    check(
        "final_video",
        final_valid and not final_outdated,
        f"整集成片可解码、与当前采纳镜头一致且时长约为 {expected_total_duration} 秒",
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
                "playback_rate": playback_by_shot[str(shot["id"])],
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
    if final_edit_report:
        if final_edit_report.get("ok") is not True:
            warnings.append({
                "code": "FINAL_EDIT_FALLBACK",
                "message": "终剪增强失败，已使用完整时间线基础合成交付",
                "detail": final_edit_report,
            })
        for failure in final_edit_report.get("text_failures") or []:
            warnings.append({
                "shot_no": failure.get("shot_no"),
                "code": "DETERMINISTIC_TEXT_INSERT_FAILED",
                "message": "确定性文字插入未完成，已保留无字可播片段",
                "detail": failure,
            })
        boundary = final_edit_report.get("boundary_report") or {}
        if int(boundary.get("issue_count") or 0) > 0:
            warnings.append({
                "code": "BOUNDARY_CONTINUITY_RISK",
                "message": f"终剪发现 {int(boundary['issue_count'])} 项跨镜结构化连续性风险，未阻断交付",
                "detail": boundary,
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
        "source_chapters": source_chapters,
        "videos": video_items,
        "final_edit_report": final_edit_report,
        "storyboard_release_authority": storyboard_release_authority,
        "video_delivery_manifest": video_delivery_manifest,
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
    operation_request_fingerprint: str | None = None,
    operation_lease_owner: str | None = None,
) -> dict[str, Any]:
    readiness = delivery_readiness(episode_id)
    gate_findings = list(readiness["blockers"])
    if gate_findings:
        summary = "；".join(
            str(item.get("message") or item.get("key") or "未知阻塞")
            for item in gate_findings[:5]
        )
        raise ValueError(f"交付硬门禁未通过：{summary}")
    release_authority = dict(readiness["storyboard_release_authority"] or {})
    video_delivery_manifest = dict(readiness["video_delivery_manifest"] or {})
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
    if bool(operation_request_fingerprint) != bool(operation_lease_owner):
        raise ValueError("交付构建 operation lease 参数不完整")
    if operation_lease_owner:
        _assert_delivery_operation_owner(
            conn,
            package_id=package_id,
            request_fingerprint=str(operation_request_fingerprint),
            lease_owner=operation_lease_owner,
        )
    operation_started_at = operation_started_at or now()
    delivery_root = (
        config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"]) / "delivery"
    ).resolve()
    existing = conn.execute(
        "SELECT * FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()
    if existing:
        package_path = Path(existing["package_path"]).resolve()
        if not package_path.is_relative_to(delivery_root):
            raise ValueError("交付包路径超出当前剧集目录，已拒绝读取")
        manifest = json.loads(existing["manifest_json"])
        if manifest.get("storyboard_release_authority") != release_authority:
            raise ValueError("交付包绑定的分镜发布权威已漂移，禁止重放旧包")
        quality_report = json.loads(existing["quality_report_json"])
        missing_files = []
        damaged_files = []
        for item in manifest.get("files") or []:
            relative = str(item.get("path") or "").strip()
            if not relative:
                continue
            candidate = (package_path / relative).resolve()
            if not candidate.is_relative_to(package_path):
                damaged_files.append(relative)
                continue
            if not candidate.is_file():
                missing_files.append(relative)
                continue
            expected_size = item.get("size_bytes")
            expected_hash = str(item.get("sha256") or "")
            if (
                expected_size is not None and candidate.stat().st_size != int(expected_size)
            ) or (
                expected_hash and _sha256(candidate) != expected_hash
            ):
                damaged_files.append(relative)
        for filename, expected_payload in (
            ("manifest.json", manifest),
            ("quality-report.json", quality_report),
        ):
            path = package_path / filename
            try:
                actual_payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                damaged_files.append(filename)
                continue
            if actual_payload != expected_payload:
                damaged_files.append(filename)
        if not package_path.is_dir() or missing_files or damaged_files:
            details = [
                *(f"缺少 {path}" for path in missing_files[:3]),
                *(f"损坏 {path}" for path in damaged_files[:3]),
            ]
            raise ValueError(
                "交付包文件不完整，数据库记录已保留供审计；请重新创建一份交付包"
                + (f"（{'；'.join(details)}）" if details else "")
            )
        archive_path = Path(str(package_path) + ".zip")
        archive_recovered = False
        if not _archive_matches_directory(archive_path, package_path):
            archive_path = atomic_zip_directory(package_path, archive_path)
            archive_recovered = True
        return {
            "package_id": existing["id"],
            "artifact_id": existing["artifact_id"],
            "trust_level": (repository.get_artifact(existing["artifact_id"]) or {}).get("trust_level", "T3"),
            "status": existing["status"],
            "package_path": str(package_path),
            "archive_path": str(archive_path),
            "manifest": manifest,
            "quality_report": quality_report,
            "archive_recovered": archive_recovered,
        }
    # A delivery snapshot is a production output, not an editor preview.  The
    # mutable episode projection is only accepted for genuinely legacy,
    # plan-null episodes; modern episodes must be re-resolved through their
    # consumed immutable screenplay authority before bytes are written.
    from app.production.screenplay_authority import resolve_downstream_screenplay

    screenplay_context = resolve_downstream_screenplay(episode_id, conn=conn)
    package_dir = (delivery_root / package_id).resolve()
    if not package_dir.is_relative_to(delivery_root):
        raise ValueError("非法的 package_id")
    # A directory without its database pointer is an uncommitted crash remnant.
    # Rebuilding the same operation id is safe and avoids exposing a half package.
    if package_dir.exists():
        if operation_lease_owner:
            _assert_delivery_operation_owner(
                conn,
                package_id=package_id,
                request_fingerprint=str(operation_request_fingerprint),
                lease_owner=operation_lease_owner,
            )
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
        _sanitize_delivery_value(
            screenplay_context.screenplay.model_dump(mode="json")
        ),
    )
    _write_json(
        snapshots / "source-chapters.json",
        {
            "schema_version": "1.0.0",
            "episode_id": episode_id,
            "project_id": ep["project_id"],
            "chapters": readiness["source_chapters"],
        },
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
    final_edit_source = final_source.with_name("episode.edit-report.json")
    final_edit_copy = _copy_if_present(
        str(final_edit_source), package_dir / "reports" / "final-edit.json",
    )
    if final_edit_copy:
        files.append({
            "role": "final_edit_report",
            "path": final_edit_copy.relative_to(package_dir).as_posix(),
        })
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
        "storyboard_release_authority": release_authority,
        "video_delivery_manifest": video_delivery_manifest,
        "source_chapters": readiness["source_chapters"],
        "files": files,
        "reproducibility": {
            "input_fingerprint": fingerprint(
                readiness["source_artifacts"], readiness["source_chapters"], files
            ),
            "shot_duration_range_s": [config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S],
            "shot_duration_decided_by": "model",
        },
    }
    quality_report = {
        "schema_version": "1.0.0",
        "hard_gate_passed": True,
        "runtime_blocking": False,
        "gate_retry_exhausted": bool(gate_findings),
        "gate_findings": _sanitize_delivery_value(gate_findings),
        "checks": _sanitize_delivery_value(readiness["checks"]),
        "evidence_coverage": readiness["evidence_coverage"],
        "warnings": _sanitize_delivery_value(readiness["warnings"]),
        "final_edit": _sanitize_delivery_value(readiness.get("final_edit_report")),
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
        f"<p>质量检查：仅评分、不阻断；证据覆盖率：{readiness['evidence_coverage']:.1%}</p>"
        f"<p>终剪状态：{'已执行确定性终剪' if (readiness.get('final_edit_report') or {}).get('ok') else '基础合成降级或无终剪报告'}</p>"
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
        readiness["source_artifacts"], readiness["source_chapters"], files
    )
    _write_json(package_dir / "manifest.json", manifest)
    _assert_no_delivery_secrets(package_dir)
    from app.downstream_authority import verify_current_storyboard_release_authority

    if verify_current_storyboard_release_authority(
        episode_id,
        conn=conn,
    ) != release_authority:
        raise ValueError("交付构建期间分镜发布权威发生漂移，已拒绝发布交付包")
    from app.downstream_authority import current_adopted_video_delivery_manifest

    if current_adopted_video_delivery_manifest(
        episode_id,
        conn=conn,
    ) != video_delivery_manifest:
        raise ValueError("交付构建期间已采纳视频发生漂移，已拒绝发布交付包")
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
    if verify_current_storyboard_release_authority(
        episode_id,
        conn=conn,
    ) != release_authority:
        raise ValueError("交付发布前分镜发布权威发生漂移，已拒绝写入交付指针")
    if current_adopted_video_delivery_manifest(
        episode_id,
        conn=conn,
    ) != video_delivery_manifest:
        raise ValueError("交付发布前已采纳视频发生漂移，已拒绝写入交付指针")
    if operation_lease_owner:
        _assert_delivery_operation_owner(
            conn,
            package_id=package_id,
            request_fingerprint=str(operation_request_fingerprint),
            lease_owner=operation_lease_owner,
        )
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
    cursor = conn.execute(
        """UPDATE episodes
              SET delivery_artifact_id=?, delivery_status=?
            WHERE id=?
              AND storyboard_artifact_id=?
              AND published_storyboard_artifact_id=?
              AND storyboard_production_revision_id=?
              AND storyboard_completion_certificate_id=?
              AND status IN ('confirmed','generating','done','mixed')""",
        (
            artifact["id"],
            status,
            episode_id,
            release_authority["published_storyboard_artifact_id"],
            release_authority["published_storyboard_artifact_id"],
            release_authority["storyboard_production_revision_id"],
            release_authority["storyboard_completion_certificate_id"],
        ),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise ValueError("交付发布发生分镜权威 CAS 冲突，未更新交付指针")
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
    allow_interrupted_takeover: bool = False,
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
        if row["status"] not in {"waiting_human", "approving", "approved"}:
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
    if approved_package_id:
        # Retained for API compatibility. Approval never rebuilds live inputs
        # into this id: the human decision is bound to the exact draft bytes.
        validate_package_id(approved_package_id)
    if decision == "reject":
        claimed = conn.execute(
            "UPDATE delivery_packages SET status='rejected' WHERE id=? AND status='waiting_human'",
            (row["id"],),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            raise ValueError("交付草稿已由另一审批任务处理")
        conn.execute("UPDATE artifacts SET status='rejected' WHERE id=?", (row["artifact_id"],))
        conn.execute("UPDATE episodes SET delivery_status='rejected' WHERE id=?", (episode_id,))
        conn.execute(
            """INSERT INTO gate_decisions(
                   id, artifact_id, gate_key, decision, decided_by, reason, accepted_risk, created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (new_id("gate"), row["artifact_id"], "delivery", decision, decided_by, reason, accepted_risk, now()),
        )
        conn.commit()
        return {"artifact_id": row["artifact_id"], "decision": decision, "trust_level": "T3", "package_id": row["id"]}

    if row["status"] == "waiting_human":
        artifact = repository.get_artifact(str(row["artifact_id"]), conn=conn)
        ep = conn.execute(
            "SELECT delivery_artifact_id,delivery_status FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if (
            artifact is None
            or artifact["status"] != "validated"
            or ep is None
            or ep["delivery_artifact_id"] != row["artifact_id"]
            or ep["delivery_status"] != "waiting_human"
        ):
            raise ValueError("交付草稿已不是当前可审批候选")
    # Reject tampered/stale inputs before creating the durable approval claim.
    _delivery_approval_snapshot(conn, row, episode_id)
    approval_fingerprint = fingerprint(
        episode_id,
        {
            "source_package_id": row["id"],
            "source_artifact_id": row["artifact_id"],
            "decision": decision,
            "decided_by": decided_by,
            "reason": reason,
            "accepted_risk": accepted_risk,
        },
    )
    receipt_id = "delivery_approval_" + hashlib.sha256(
        str(row["id"]).encode("utf-8")
    ).hexdigest()[:24]
    owner, recovered = claim_delivery_package_operation(
        package_id=receipt_id,
        episode_id=episode_id,
        request_fingerprint=approval_fingerprint,
        allow_interrupted_takeover=allow_interrupted_takeover,
        conn=conn,
    )
    if recovered is not None:
        return recovered
    assert owner is not None

    # Validate before claiming any domain state, then repeat every check inside
    # the single publish transaction immediately before the T5 write.
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_delivery_operation_owner(
            conn,
            package_id=receipt_id,
            request_fingerprint=approval_fingerprint,
            lease_owner=owner,
        )
        current = conn.execute(
            "SELECT * FROM delivery_packages WHERE id=? AND episode_id=?",
            (row["id"], episode_id),
        ).fetchone()
        if current is None or current["status"] not in {"waiting_human", "approving"}:
            raise ValueError("交付草稿审批状态已漂移")
        snapshot = _delivery_approval_snapshot(conn, current, episode_id)
        artifact = snapshot["artifact"]
        if artifact["status"] != "validated":
            raise ValueError("交付草稿 Artifact 已不是 validated 候选")
        claimed = conn.execute(
            """UPDATE delivery_packages SET status='approving'
                 WHERE id=? AND status IN ('waiting_human','approving')""",
            (row["id"],),
        )
        if claimed.rowcount != 1:
            raise ValueError("交付草稿审批 CAS 冲突")
        stamp = now()
        issues = ([{
            "code": "RISK_ACCEPTED",
            "severity": "warning",
            "subject": str(row["artifact_id"]),
            "message": accepted_risk or reason,
            "path": None,
            "retryable": False,
            "suggested_fix": None,
        }] if decision == "approve_with_risk" else [])
        evidence = {
            "decision": decision,
            "reason": reason,
            "accepted_risk": accepted_risk,
            "approved_manifest_hash": snapshot["manifest_sha256"],
            "approved_archive_sha256": snapshot["archive_sha256"],
            "approved_package_id": str(row["id"]),
        }
        conn.execute(
            """INSERT INTO evaluations(
                   id,artifact_id,evaluator_type,evaluator_name,evaluator_version,
                   status,hard_gate_passed,evaluation_role,score_status,runtime_blocking,
                   retry_eligible,score,dimension_scores_json,issues_json,evidence_json,
                   confidence,recovered,created_at
               ) VALUES(?,?,?,?,?,?,1,'runtime_gate','scored',1,0,100,'{}',?,?,1,0,?)""",
            (
                new_id("eval"), row["artifact_id"], "human", decided_by, "1.0.0",
                "warning" if decision == "approve_with_risk" else "passed",
                json.dumps(issues, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                stamp,
            ),
        )
        updated_artifact = conn.execute(
            """UPDATE artifacts SET status='approved',trust_level='T5',approved_at=?
                 WHERE id=? AND status='validated' AND content_hash=?""",
            (stamp, row["artifact_id"], artifact["content_hash"]),
        )
        if updated_artifact.rowcount != 1:
            raise ValueError("交付草稿 Artifact 批准 CAS 冲突")
        conn.execute(
            """INSERT INTO gate_decisions(
                   id,artifact_id,gate_key,decision,decided_by,reason,accepted_risk,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                new_id("gate"), row["artifact_id"], "delivery", decision,
                decided_by, reason, accepted_risk, stamp,
            ),
        )
        conn.execute(
            "UPDATE delivery_packages SET status='approved',approved_at=? WHERE id=? AND status='approving'",
            (stamp, row["id"]),
        )
        episode_update = conn.execute(
            """UPDATE episodes SET delivery_artifact_id=?,delivery_status='approved'
                 WHERE id=? AND delivery_artifact_id=? AND delivery_status='waiting_human'""",
            (row["artifact_id"], episode_id, row["artifact_id"]),
        )
        if episode_update.rowcount != 1:
            raise ValueError("交付草稿审批时当前指针已漂移")
        result = {
            "package_id": row["id"],
            "artifact_id": row["artifact_id"],
            "trust_level": "T5",
            "status": "approved",
            "package_path": str(snapshot["package_dir"]),
            "archive_path": str(snapshot["package_dir"]) + ".zip",
            "manifest": snapshot["manifest"],
            "archive_sha256": snapshot["archive_sha256"],
            "decision": decision,
            "approved_snapshot_preserved": True,
        }
        receipt = conn.execute(
            """UPDATE delivery_operation_receipts
                  SET status='succeeded',result_json=?,lease_expires_at=0,updated_at=?
                WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
                  AND status='running'""",
            (
                json.dumps(result, ensure_ascii=False, sort_keys=True), stamp,
                receipt_id, approval_fingerprint, owner,
            ),
        )
        if receipt.rowcount != 1:
            raise ValueError("交付审批 receipt CAS 冲突")
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


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
