from __future__ import annotations

import hashlib
import json
import re
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
from app.harness.types import Evaluation, Issue, IssueSeverity
from app.orchestration.engine import fingerprint

from app.delivery_package_build import (
    _append_delivery_report_files,
    _build_delivery_known_issues_lines,
    _build_delivery_manifest,
    _build_delivery_quality_report,
    _collect_delivery_media_files,
    _create_delivery_workspace_dir,
    _purge_startup_fenced_delivery_remnants,
    _render_delivery_quality_report_html,
    _resolve_delivery_final_package_dir,
    _resolve_delivery_workspace_dir,
    _write_delivery_snapshots,
)
from app.delivery_package_publish import (
    _advance_delivery_operation_phase,
    _assert_delivery_operation_lease_consistent,
    _commit_delivery_package_publication,
    _prepare_delivery_artifact_content,
    _raise_if_delivery_authority_drifted,
    _raise_if_delivery_gate_blocked,
    _resolve_delivery_package_id,
)
from app.delivery_package_replay import (
    _recover_orphan_delivery_package,
    _replay_existing_delivery_package,
)

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
               workspace_path TEXT NOT NULL DEFAULT '',
               promotion_phase TEXT NOT NULL DEFAULT 'claimed',
               abandoned_workspace_path TEXT NOT NULL DEFAULT '',
               abandoned_promotion_phase TEXT NOT NULL DEFAULT '',
               interrupted_at REAL,
               recovery_fenced_owner TEXT NOT NULL DEFAULT '',
               updated_at REAL NOT NULL
           )"""
    )
    columns = {str(row[1]) for row in conn.execute(
        "PRAGMA table_info(delivery_operation_receipts)"
    )}
    if "workspace_path" not in columns:
        conn.execute(
            "ALTER TABLE delivery_operation_receipts ADD COLUMN workspace_path TEXT NOT NULL DEFAULT ''"
        )
    if "promotion_phase" not in columns:
        conn.execute(
            "ALTER TABLE delivery_operation_receipts ADD COLUMN promotion_phase TEXT NOT NULL DEFAULT 'claimed'"
        )
    if "abandoned_workspace_path" not in columns:
        conn.execute(
            "ALTER TABLE delivery_operation_receipts ADD COLUMN abandoned_workspace_path TEXT NOT NULL DEFAULT ''"
        )
    if "abandoned_promotion_phase" not in columns:
        conn.execute(
            "ALTER TABLE delivery_operation_receipts ADD COLUMN abandoned_promotion_phase TEXT NOT NULL DEFAULT ''"
        )
    if "interrupted_at" not in columns:
        conn.execute(
            "ALTER TABLE delivery_operation_receipts ADD COLUMN interrupted_at REAL"
        )
    if "recovery_fenced_owner" not in columns:
        conn.execute(
            "ALTER TABLE delivery_operation_receipts ADD COLUMN recovery_fenced_owner TEXT NOT NULL DEFAULT ''"
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
            ):
                db.commit()
                raise ValueError("相同交付操作正在执行中")
            updated = db.execute(
                """UPDATE delivery_operation_receipts
                      SET lease_owner=?,lease_expires_at=?,status='running',
                          abandoned_workspace_path=workspace_path,
                          abandoned_promotion_phase=promotion_phase,
                          workspace_path='',promotion_phase='claimed',
                          recovery_fenced_owner=CASE
                              WHEN interrupted_at IS NOT NULL OR ? THEN ? ELSE '' END,
                          interrupted_at=NULL,updated_at=?
                    WHERE package_id=? AND request_fingerprint=?
                      AND (status!='running' OR lease_expires_at<=?)""",
                (
                    owner,
                    stamp + _DELIVERY_OPERATION_LEASE_S,
                    int(str(row["status"] or "") == "failed"),
                    owner,
                    stamp,
                    package_id,
                    request_fingerprint,
                    stamp,
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


def _renew_delivery_operation_owner(
    conn,
    *,
    package_id: str,
    request_fingerprint: str,
    lease_owner: str,
) -> None:
    stamp = time.time()
    updated = conn.execute(
        """UPDATE delivery_operation_receipts
              SET lease_expires_at=?,updated_at=?
            WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
              AND status='running'""",
        (
            stamp + _DELIVERY_OPERATION_LEASE_S,
            stamp,
            package_id,
            request_fingerprint,
            lease_owner,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise ValueError("交付 operation owner 已被接管")
    conn.commit()


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


def verify_delivery_package_artifact_binding(
    conn,
    row,
    *,
    episode_id: str,
    allowed_statuses: set[str],
) -> dict[str, Any]:
    """Verify the immutable package row, Artifact, manifest, and lineage."""
    artifact = repository.get_artifact(str(row["artifact_id"]), conn=conn)
    if (
        artifact is None
        or artifact.get("type") != "delivery_package"
        or artifact.get("scope_type") != "episode"
        or artifact.get("scope_id") != episode_id
        or artifact.get("status") not in allowed_statuses
        or artifact.get("contract_version") != "delivery-1.0.0"
    ):
        raise ValueError("交付包 Artifact 合同、状态或作用域不匹配")
    try:
        manifest = json.loads(str(row["manifest_json"] or "{}"))
        quality_report = json.loads(str(row["quality_report_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("交付包数据库快照损坏") from exc
    package_dir = Path(str(row["package_path"])).resolve()
    manifest_path = (package_dir / "manifest.json").resolve()
    expected_content = {
        "package_id": str(row["id"]),
        "manifest": manifest,
        "quality_report": quality_report,
    }
    expected_parents = {
        str(item.get("id"))
        for item in manifest.get("source_artifacts") or []
        if item.get("id")
    }
    expected_parents.update(
        str(item.get("artifact_id"))
        for item in (manifest.get("video_delivery_manifest") or {}).get("items") or []
        if item.get("artifact_id")
    )
    if (
        artifact.get("content") != expected_content
        or Path(str(artifact.get("file_path") or "")).resolve() != manifest_path
        or {str(item) for item in artifact.get("parent_artifact_ids") or []}
        != expected_parents
    ):
        raise ValueError("交付包 Artifact 内容、文件或父血缘绑定已漂移")
    try:
        actual_hash = repository.content_hash(
            artifact.get("content"), artifact.get("file_path"),
        )
    except OSError as exc:
        raise ValueError("交付包 Artifact 文件证据缺失") from exc
    if actual_hash != str(artifact.get("content_hash") or ""):
        raise ValueError("交付包 Artifact 实际内容哈希已漂移")
    return {
        "artifact": artifact,
        "manifest": manifest,
        "quality_report": quality_report,
        "package_dir": package_dir,
        "manifest_path": manifest_path,
    }


def _delivery_approval_snapshot(conn, row, episode_id: str) -> dict[str, Any]:
    binding = verify_delivery_package_artifact_binding(
        conn, row, episode_id=episode_id, allowed_statuses={"validated", "approved"},
    )
    artifact = binding["artifact"]
    package_dir = binding["package_dir"]
    manifest_path = binding["manifest_path"]
    archive_path = Path(str(package_dir) + ".zip")
    if not package_dir.is_dir() or not manifest_path.is_file():
        raise ValueError("交付草稿文件已丢失")
    try:
        manifest = binding["manifest"]
        disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("交付草稿 manifest 损坏") from exc
    if not isinstance(manifest, dict) or manifest != disk_manifest:
        raise ValueError("交付草稿 manifest 与审核快照不一致")
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
    # P0-1：owned_episode_row 折叠 existence+ownership 判据（同一份判据用在
    # app.domain.common._episode_or_404）。这个函数除了走
    # GET /episodes/{episode_id}/delivery/readiness（episode_id 是路径参数，
    # 已被 require_project_owner_access 拦过一轮）之外，还被
    # app/capabilities/handlers/delivery.py::check()（Command Bus 的
    # delivery.check handler，episode_id 来自命令参数体）与
    # app/mcp/resources.py 的 delivery 只读 Resource（MCP 完全不挂本机会话
    # 闸门）直接调用——两条路径都没有任何上游校验过 episode_id 属于谁，裸 SQL
    # 只验证"存在"会把交付就绪细节泄露给任何登录账号。保持抛 KeyError（不是
    # HTTPException）：三个调用方都已经把 KeyError 当"不存在"处理，existence
    # 与 ownership 折叠进同一个异常分支，不额外泄露"存在但无权"。
    from app.domain.common import owned_episode_row

    conn = get_conn()
    ep = owned_episode_row(episode_id)
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


def _episode_release_status_cas_clause(conn, episode_id: str) -> tuple[str, tuple[str, ...]]:
    """交付包写入 CAS 用的 status 判据：分镜台 2.0.0 产物齐全时额外放行 'scripted'。

    老版逐镜叙事契约要人工点「确认」才把 episodes.status 推到
    confirmed/generating/done/mixed。分镜台 2.0.0（app.production.
    storyboard_pack）生成完成后只落 'scripted'，从不推进到这个白名单——
    用户已拆掉两台之间的人工确认仪式，新分集永远到不了 confirmed。
    上面 build_delivery_package 已经用 app.downstream_authority.
    verify_current_storyboard_release_authority（storyboard_artifact_id/
    published_storyboard_artifact_id/revision/certificate 四件套 + 未偏离 +
    release qualification）证明了发布权威链未漂移；这里只是把同一个产物
    信号（storyboard_pack_prompts_complete）补进 CAS 的 status 判据，
    不放宽、不跳过上面那些实质校验。
    """
    from app.domain.common import storyboard_pack_prompts_complete

    statuses: tuple[str, ...] = ("confirmed", "generating", "done", "mixed")
    if storyboard_pack_prompts_complete(conn, episode_id):
        statuses = (*statuses, "scripted")
    placeholders = ",".join("?" for _ in statuses)
    return f"status IN ({placeholders})", statuses


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
    gate_findings = _raise_if_delivery_gate_blocked(readiness)
    release_authority = dict(readiness["storyboard_release_authority"] or {})
    video_delivery_manifest = dict(readiness["video_delivery_manifest"] or {})
    if decision is not None:
        raise ValueError("交付批准必须对已落盘的精确草稿执行，禁止构建时直达 T5")
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    package_id = _resolve_delivery_package_id(package_id)
    _assert_delivery_operation_lease_consistent(
        conn,
        package_id=package_id,
        operation_request_fingerprint=operation_request_fingerprint,
        operation_lease_owner=operation_lease_owner,
    )
    operation_started_at = operation_started_at or now()
    delivery_root = (
        config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"]) / "delivery"
    ).resolve()

    existing = conn.execute(
        "SELECT * FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()
    if existing:
        return _replay_existing_delivery_package(
            conn, package_id, existing, delivery_root, release_authority,
            operation_lease_owner=operation_lease_owner,
            operation_request_fingerprint=operation_request_fingerprint,
        )

    recovered = _recover_orphan_delivery_package(
        conn,
        episode_id=episode_id,
        package_id=package_id,
        delivery_root=delivery_root,
        release_authority=release_authority,
        video_delivery_manifest=video_delivery_manifest,
        readiness=readiness,
        operation_started_at=operation_started_at,
        operation_lease_owner=operation_lease_owner,
        operation_request_fingerprint=operation_request_fingerprint,
    )
    if recovered is not None:
        return recovered

    # A delivery snapshot is a production output, not an editor preview.  The
    # mutable episode projection is only accepted for genuinely legacy,
    # plan-null episodes; modern episodes must be re-resolved through their
    # consumed immutable screenplay authority before bytes are written.
    from app.production.screenplay_authority import resolve_downstream_screenplay

    screenplay_context = resolve_downstream_screenplay(episode_id, conn=conn)
    final_package_dir = _resolve_delivery_final_package_dir(package_id, delivery_root)
    package_dir, receipt_row = _resolve_delivery_workspace_dir(
        conn,
        package_id=package_id,
        delivery_root=delivery_root,
        operation_lease_owner=operation_lease_owner,
        operation_request_fingerprint=operation_request_fingerprint,
    )
    if not package_dir.is_relative_to(delivery_root):
        raise ValueError("非法的交付操作目录")
    # A startup-fenced takeover retains the previous owner's workspace/phase
    # as evidence.  Only that proof permits removal of exact crash remnants;
    # natural lease expiry alone is insufficient because the old thread may
    # still be alive.  The new owner always builds in its own directory.
    _purge_startup_fenced_delivery_remnants(
        receipt_row, final_package_dir,
        delivery_root=delivery_root, operation_lease_owner=operation_lease_owner,
    )
    archive_candidate, snapshots = _create_delivery_workspace_dir(package_dir)
    _write_delivery_snapshots(
        conn, snapshots,
        project=project, screenplay_context=screenplay_context,
        episode_id=episode_id, ep=ep, readiness=readiness,
    )

    files = _collect_delivery_media_files(package_dir, snapshots, ep, readiness)

    manifest = _build_delivery_manifest(
        package_id, episode_id, ep, operation_started_at,
        release_authority, video_delivery_manifest, readiness, files,
    )
    quality_report = _build_delivery_quality_report(
        gate_findings, readiness,
        decision=decision, decided_by=decided_by, reason=reason, accepted_risk=accepted_risk,
    )
    _write_json(package_dir / "manifest.json", manifest)
    _write_json(package_dir / "quality-report.json", quality_report)
    report_html = _render_delivery_quality_report_html(
        ep, readiness, decision=decision, reason=reason,
    )
    atomic_write_text(package_dir / "quality-report.html", report_html)
    known_lines = _build_delivery_known_issues_lines(readiness, accepted_risk)
    atomic_write_text(package_dir / "known-issues.md", "\n".join(known_lines))
    _assert_no_delivery_secrets(package_dir)
    _append_delivery_report_files(files, package_dir)
    manifest["files"] = files
    manifest["reproducibility"]["input_fingerprint"] = fingerprint(
        readiness["source_artifacts"], readiness["source_chapters"], files
    )
    _write_json(package_dir / "manifest.json", manifest)
    _assert_no_delivery_secrets(package_dir)
    if operation_lease_owner:
        _renew_delivery_operation_owner(
            conn,
            package_id=package_id,
            request_fingerprint=str(operation_request_fingerprint),
            lease_owner=operation_lease_owner,
        )
    _raise_if_delivery_authority_drifted(
        conn, episode_id, release_authority, video_delivery_manifest,
        storyboard_drift_message="交付构建期间分镜发布权威发生漂移，已拒绝发布交付包",
        video_drift_message="交付构建期间已采纳视频发生漂移，已拒绝发布交付包",
    )
    # 先完成客户可下载的文件，再提交数据库指针；ZIP 失败不能留下“已批准但不可下载”的记录。
    archive_path = Path(atomic_zip_directory(package_dir, archive_candidate))

    if operation_lease_owner:
        _advance_delivery_operation_phase(
            conn, package_id=package_id,
            operation_request_fingerprint=operation_request_fingerprint,
            operation_lease_owner=operation_lease_owner,
            phase="ready", conflict_message="交付 operation owner 在 ready 前已被接管",
        )
    if final_package_dir.exists() or Path(str(final_package_dir) + ".zip").exists():
        raise ValueError("交付最终目录已存在，拒绝删除或覆盖另一 owner 产物")
    package_dir.rename(final_package_dir)
    if operation_lease_owner:
        _advance_delivery_operation_phase(
            conn, package_id=package_id,
            operation_request_fingerprint=operation_request_fingerprint,
            operation_lease_owner=operation_lease_owner,
            phase="directory_promoted",
            conflict_message="交付 operation owner 在目录发布后已被接管",
        )
    final_archive_path = Path(str(final_package_dir) + ".zip")
    archive_path.rename(final_archive_path)
    if operation_lease_owner:
        _advance_delivery_operation_phase(
            conn, package_id=package_id,
            operation_request_fingerprint=operation_request_fingerprint,
            operation_lease_owner=operation_lease_owner,
            phase="promoted", conflict_message="交付 operation owner 在压缩包发布后已被接管",
        )
    package_dir = final_package_dir
    archive_path = final_archive_path

    artifact_id, parent_ids, artifact_content, expected_artifact_hash = (
        _prepare_delivery_artifact_content(
            package_id, package_dir, manifest, quality_report, readiness,
        )
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
    _commit_delivery_package_publication(
        conn,
        episode_id=episode_id,
        package_id=package_id,
        artifact_id=artifact_id,
        package_dir=package_dir,
        release_authority=release_authority,
        video_delivery_manifest=video_delivery_manifest,
        artifact_content=artifact_content,
        expected_artifact_hash=expected_artifact_hash,
        parent_ids=parent_ids,
        file_eval=file_eval,
        manifest=manifest,
        quality_report=quality_report,
        known_lines=known_lines,
        operation_started_at=operation_started_at,
        operation_lease_owner=operation_lease_owner,
        operation_request_fingerprint=operation_request_fingerprint,
    )
    return {
        "package_id": package_id,
        "artifact_id": artifact_id,
        "trust_level": "T3",
        "status": "waiting_human",
        "package_path": str(package_dir),
        "archive_path": str(archive_path),
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
        if row["status"] not in {"waiting_human", "approving", "approved", "rejected", "superseded"}:
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
        rejection_fingerprint = fingerprint(
            episode_id,
            {
                "source_package_id": row["id"],
                "source_artifact_id": row["artifact_id"],
                "decision": decision,
                "decided_by": decided_by,
                "reason": reason,
            },
        )
        receipt_id = "delivery_approval_" + hashlib.sha256(
            str(row["id"]).encode("utf-8")
        ).hexdigest()[:24]
        owner, recovered = claim_delivery_package_operation(
            package_id=receipt_id,
            episode_id=episode_id,
            request_fingerprint=rejection_fingerprint,
            allow_interrupted_takeover=allow_interrupted_takeover,
            conn=conn,
        )
        if recovered is not None:
            return recovered
        assert owner is not None
        try:
            conn.execute("BEGIN IMMEDIATE")
            _assert_delivery_operation_owner(
                conn,
                package_id=receipt_id,
                request_fingerprint=rejection_fingerprint,
                lease_owner=owner,
            )
            current = conn.execute(
                "SELECT * FROM delivery_packages WHERE id=? AND episode_id=?",
                (row["id"], episode_id),
            ).fetchone()
            artifact = repository.get_artifact(str(row["artifact_id"]), conn=conn)
            episode = conn.execute(
                "SELECT delivery_artifact_id,delivery_status FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            if (
                current is None
                or current["status"] != "waiting_human"
                or artifact is None
                or artifact["status"] != "validated"
                or episode is None
                or episode["delivery_artifact_id"] != row["artifact_id"]
                or episode["delivery_status"] != "waiting_human"
            ):
                raise ValueError("交付草稿已不是当前可拒绝候选")
            stamp = now()
            conn.execute(
                "UPDATE delivery_packages SET status='rejected' WHERE id=? AND status='waiting_human'",
                (row["id"],),
            )
            conn.execute(
                "UPDATE artifacts SET status='rejected' WHERE id=? AND status='validated'",
                (row["artifact_id"],),
            )
            conn.execute(
                """UPDATE episodes SET delivery_status='rejected'
                     WHERE id=? AND delivery_artifact_id=? AND delivery_status='waiting_human'""",
                (episode_id, row["artifact_id"]),
            )
            conn.execute(
                """INSERT INTO gate_decisions(
                       id,artifact_id,gate_key,decision,decided_by,reason,accepted_risk,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    new_id("gate"), row["artifact_id"], "delivery", decision,
                    decided_by, reason, accepted_risk, stamp,
                ),
            )
            result = {
                "artifact_id": row["artifact_id"],
                "decision": decision,
                "trust_level": "T3",
                "package_id": row["id"],
            }
            receipt = conn.execute(
                """UPDATE delivery_operation_receipts
                      SET status='succeeded',result_json=?,lease_expires_at=0,updated_at=?
                    WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
                      AND status='running'""",
                (
                    json.dumps(result, ensure_ascii=False, sort_keys=True), stamp,
                    receipt_id, rejection_fingerprint, owner,
                ),
            )
            if receipt.rowcount != 1:
                raise ValueError("交付拒绝 receipt CAS 冲突")
            conn.commit()
            return result
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            conn.execute(
                """UPDATE delivery_operation_receipts
                      SET status='failed',lease_expires_at=0,updated_at=?
                    WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
                      AND status='running'""",
                (time.time(), receipt_id, rejection_fingerprint, owner),
            )
            conn.commit()
            raise

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
    # A first approval rejects tampered/stale input before creating a durable
    # claim.  Recovery/replay must consult its exact receipt first: a later
    # supersession cannot erase the historical result of the same operation.
    if row["status"] == "waiting_human":
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
        try:
            _delivery_approval_snapshot(conn, row, episode_id)
            recovered["authority_current"] = row["status"] == "approved"
        except ValueError:
            recovered["authority_current"] = False
        return recovered
    assert owner is not None

    _delivery_approval_snapshot(conn, row, episode_id)

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
        old_packages = conn.execute(
            """SELECT id,artifact_id FROM delivery_packages
               WHERE episode_id=? AND id!=? AND status='approved'""",
            (episode_id, row["id"]),
        ).fetchall()
        conn.executemany(
            "UPDATE delivery_packages SET status='superseded' WHERE id=?",
            [(item["id"],) for item in old_packages],
        )
        conn.executemany(
            """UPDATE artifacts SET status='superseded',superseded_by_artifact_id=?
               WHERE id=? AND status='approved'""",
            [(row["artifact_id"], item["artifact_id"]) for item in old_packages],
        )
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
        conn.execute(
            """UPDATE delivery_operation_receipts
                  SET status='failed',lease_expires_at=0,updated_at=?
                WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
                  AND status='running'""",
            (time.time(), receipt_id, approval_fingerprint, owner),
        )
        conn.commit()
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


# app.db.init_db() no longer imports this module directly (P0-3 dependency
# inversion, docs/coupling_review_2026-08-29.md 第2步) — it looks this up by
# name through app.db_schema instead.
from app.db_schema import register_table as _register_table  # noqa: E402

_register_table("delivery_operation_receipts_table", _ensure_delivery_operation_receipts)
