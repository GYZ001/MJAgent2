"""build_delivery_package 的「命中既有产物」两条分支：重放同一 package_id 的
既有交付包，以及崩溃恢复遗留在磁盘/Artifact 表里但尚未落 delivery_packages
行的孤儿产物。两条分支都不写新文件，只核验既有产物与当前权威一致后原样交付。

`_sha256`/`_archive_matches_directory`/`_assert_delivery_operation_owner`/
`_episode_release_status_cas_clause` 定义在 app.delivery 里且被该模块其余函数
共用，本文件在函数体内惰性导入它们，避免与 app.delivery 顶层互相导入成环。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evidence import repository


def _delivery_package_file_diffs(
    package_path: Path, manifest: dict, quality_report: dict,
) -> tuple[list[str], list[str]]:
    """对照 manifest 逐文件核验存在性与 sha256/size；再核验 manifest/quality-report 快照未被改写。"""
    from app.delivery import _sha256

    missing_files: list[str] = []
    damaged_files: list[str] = []
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
    return missing_files, damaged_files


def _delivery_package_integrity_check(
    package_id: str, existing: Any, delivery_root: Path, release_authority: dict,
) -> tuple[Path, dict, dict]:
    """核验既有交付包目录路径与文件校验和均未漂移；返回 (package_path, manifest, quality_report)。"""
    package_path = Path(existing["package_path"]).resolve()
    if not package_path.is_relative_to(delivery_root):
        raise ValueError("交付包路径超出当前剧集目录，已拒绝读取")
    manifest = json.loads(existing["manifest_json"])
    if manifest.get("storyboard_release_authority") != release_authority:
        raise ValueError("交付包绑定的分镜发布权威已漂移，禁止重放旧包")
    quality_report = json.loads(existing["quality_report_json"])
    missing_files, damaged_files = _delivery_package_file_diffs(
        package_path, manifest, quality_report
    )
    if not package_path.is_dir() or missing_files or damaged_files:
        details = [
            *(f"缺少 {path}" for path in missing_files[:3]),
            *(f"损坏 {path}" for path in damaged_files[:3]),
        ]
        raise ValueError(
            "交付包文件不完整，数据库记录已保留供审计；请重新创建一份交付包"
            + (f"（{'；'.join(details)}）" if details else "")
        )
    return package_path, manifest, quality_report


def _refresh_delivery_package_archive_if_needed(
    conn, package_id: str, existing: Any, package_path: Path,
    *, operation_lease_owner: str | None, operation_request_fingerprint: str | None,
) -> tuple[Path, bool]:
    """既有交付包压缩包若与目录内容不一致：仅 waiting_human 状态且持有租约时允许重建。"""
    from app.delivery import _archive_matches_directory, _assert_delivery_operation_owner
    from app.atomic_io import atomic_zip_directory

    archive_path = Path(str(package_path) + ".zip")
    if _archive_matches_directory(archive_path, package_path):
        return archive_path, False
    if existing["status"] != "waiting_human":
        raise ValueError("已审核或历史交付包字节不可变，压缩包损坏后禁止自动重写")
    if not operation_lease_owner:
        raise ValueError("等待审核交付包缺少原 operation owner，禁止自动重写压缩包")
    _assert_delivery_operation_owner(
        conn,
        package_id=package_id,
        request_fingerprint=str(operation_request_fingerprint),
        lease_owner=operation_lease_owner,
    )
    archive_path = atomic_zip_directory(package_path, archive_path)
    return archive_path, True


def _replay_existing_delivery_package(
    conn, package_id: str, existing: Any, delivery_root: Path, release_authority: dict,
    *, operation_lease_owner: str | None, operation_request_fingerprint: str | None,
) -> dict[str, Any]:
    """既有交付包命中同一 package_id 时的重放路径：核验完整性后原样（或补建压缩包）返回。"""
    package_path, manifest, quality_report = _delivery_package_integrity_check(
        package_id, existing, delivery_root, release_authority,
    )
    archive_path, archive_recovered = _refresh_delivery_package_archive_if_needed(
        conn, package_id, existing, package_path,
        operation_lease_owner=operation_lease_owner,
        operation_request_fingerprint=operation_request_fingerprint,
    )
    return {
        "package_id": existing["id"],
        "artifact_id": existing["artifact_id"],
        "trust_level": (repository.get_artifact(existing["artifact_id"]) or {}).get(
            "trust_level", "T3"
        ),
        "status": existing["status"],
        "package_path": str(package_path),
        "archive_path": str(archive_path),
        "manifest": manifest,
        "quality_report": quality_report,
        "archive_recovered": archive_recovered,
    }


def _orphan_delivery_files_valid(orphan_package_dir: Path, orphan_manifest: dict) -> bool:
    """崩溃恢复候选目录里 manifest 列出的每个文件必须实际存在且哈希/大小吻合。"""
    from app.delivery import _sha256

    for item in orphan_manifest.get("files") or []:
        candidate = (orphan_package_dir / str(item.get("path") or "")).resolve()
        if (
            orphan_package_dir not in candidate.parents
            or not candidate.is_file()
            or candidate.stat().st_size != int(item.get("size_bytes") or -1)
            or _sha256(candidate) != str(item.get("sha256") or "")
        ):
            return False
    return True


def _load_and_validate_orphan_snapshot(
    orphan_package_dir: Path, orphan_archive: Path,
    orphan_artifact: dict, release_authority: dict, video_delivery_manifest: dict,
) -> tuple[dict, dict]:
    """加载崩溃恢复候选的 manifest/quality-report 快照，核验与当前权威、Artifact 内容一致。"""
    from app.delivery import _archive_matches_directory

    try:
        orphan_manifest = json.loads(
            (orphan_package_dir / "manifest.json").read_text(encoding="utf-8")
        )
        orphan_quality = json.loads(
            (orphan_package_dir / "quality-report.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("交付崩溃恢复目录已损坏") from exc
    if (
        orphan_manifest.get("storyboard_release_authority") != release_authority
        or orphan_manifest.get("video_delivery_manifest") != video_delivery_manifest
        or not _orphan_delivery_files_valid(orphan_package_dir, orphan_manifest)
        or not _archive_matches_directory(orphan_archive, orphan_package_dir)
        or repository.content_hash(
            orphan_artifact.get("content"), orphan_artifact.get("file_path"),
        ) != orphan_artifact.get("content_hash")
    ):
        raise ValueError("交付崩溃恢复证据与当前权威不一致")
    return orphan_manifest, orphan_quality


def _orphan_recovery_expected_parents(readiness: dict) -> set[str]:
    """崩溃恢复候选 Artifact 应有的父血缘集合：来源 Artifact + 已采纳镜头视频 Artifact。"""
    expected_parents = {str(item["id"]) for item in readiness["source_artifacts"]}
    expected_parents.update(
        str(item["artifact_id"])
        for item in readiness["videos"]
        if item.get("artifact_id")
    )
    return expected_parents


def _validate_orphan_recovery_artifact_lineage(
    conn, *, episode_id: str, package_id: str, orphan_artifact_id: str,
    orphan_package_dir: Path, orphan_manifest: dict, orphan_quality: dict, readiness: dict,
) -> None:
    """崩溃恢复分支专用：核验当前 Artifact 行的合同版本、范围、内容与父血缘均未漂移。

    必须在调用方已开启的 BEGIN IMMEDIATE 事务里调用——校验对象是本次事务视角下的
    当前行，不是快照。
    """
    current_orphan_artifact = conn.execute(
        """SELECT type,scope_type,scope_id,status,contract_version,
                  content_json,file_path,content_hash,parent_artifact_ids_json
             FROM artifacts WHERE id=?""",
        (orphan_artifact_id,),
    ).fetchone()
    if current_orphan_artifact is None:
        raise ValueError("交付崩溃恢复 Artifact 已被撤销")
    try:
        current_orphan_content = json.loads(
            current_orphan_artifact["content_json"] or "null"
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("交付崩溃恢复 Artifact 内容损坏") from exc
    expected_orphan_content = {
        "package_id": package_id,
        "manifest": orphan_manifest,
        "quality_report": orphan_quality,
    }
    expected_parents = _orphan_recovery_expected_parents(readiness)
    try:
        actual_parents = {
            str(item) for item in json.loads(
                current_orphan_artifact["parent_artifact_ids_json"] or "[]"
            )
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("交付崩溃恢复 Artifact 父血缘损坏") from exc
    expected_manifest_path = (orphan_package_dir / "manifest.json").resolve()
    actual_manifest_path = Path(
        str(current_orphan_artifact["file_path"] or "")
    ).resolve()
    if (
        current_orphan_artifact["type"] != "delivery_package"
        or current_orphan_artifact["scope_type"] != "episode"
        or current_orphan_artifact["scope_id"] != episode_id
        or current_orphan_artifact["status"] != "validated"
        or current_orphan_artifact["contract_version"] != "delivery-1.0.0"
        or current_orphan_content != expected_orphan_content
        or actual_manifest_path != expected_manifest_path
        or actual_parents != expected_parents
    ):
        raise ValueError("交付崩溃恢复 Artifact 合同、范围或血缘不兼容")
    if repository.content_hash(
        current_orphan_content, current_orphan_artifact["file_path"],
    ) != current_orphan_artifact["content_hash"]:
        raise ValueError("交付崩溃恢复 Artifact 实际内容已漂移")


def _raise_if_orphan_recovery_authority_drifted(
    conn, episode_id: str, release_authority: dict, video_delivery_manifest: dict,
) -> None:
    """崩溃恢复事务内核验分镜发布权威与已采纳视频未漂移。

    用 module-attribute 访问（`downstream_authority.verify_...`）而不是 bare-name
    import，与原实现一致——天然免疫跨模块 monkeypatch 绑定分裂，因为每次调用都
    重新从模块对象取当前值。
    """
    from app import downstream_authority

    if downstream_authority.verify_current_storyboard_release_authority(
        episode_id, conn=conn,
    ) != release_authority:
        raise ValueError("交付崩溃恢复时分镜发布权威发生漂移")
    if downstream_authority.current_adopted_video_delivery_manifest(
        episode_id, conn=conn,
    ) != video_delivery_manifest:
        raise ValueError("交付崩溃恢复时已采纳视频发生漂移")


def _insert_orphan_delivery_package_row_and_cas_episode(
    conn, *, package_id: str, episode_id: str, orphan_artifact_id: str,
    orphan_package_dir: Path, orphan_manifest: dict, orphan_quality: dict,
    operation_started_at: float, release_authority: dict,
) -> None:
    """插入崩溃恢复的 delivery_packages 行，并 CAS 把 episodes 的交付指针切到该 Artifact。"""
    from app.delivery import _episode_release_status_cas_clause

    conn.execute(
        """INSERT INTO delivery_packages(
               id,episode_id,artifact_id,status,package_path,manifest_json,
               quality_report_json,known_issues,created_at
           ) VALUES(?,?,?,'waiting_human',?,?,?,'',?)""",
        (
            package_id,
            episode_id,
            orphan_artifact_id,
            str(orphan_package_dir),
            json.dumps(orphan_manifest, ensure_ascii=False),
            json.dumps(orphan_quality, ensure_ascii=False),
            operation_started_at,
        ),
    )
    status_clause, status_params = _episode_release_status_cas_clause(
        conn, episode_id,
    )
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
            orphan_artifact_id,
            episode_id,
            release_authority["published_storyboard_artifact_id"],
            release_authority["published_storyboard_artifact_id"],
            release_authority["storyboard_production_revision_id"],
            release_authority["storyboard_completion_certificate_id"],
            *status_params,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("交付崩溃恢复发生分镜权威 CAS 冲突")


def _commit_orphan_delivery_recovery(
    conn, *, episode_id: str, package_id: str, release_authority: dict,
    video_delivery_manifest: dict, orphan_artifact_id: str, orphan_package_dir: Path,
    orphan_manifest: dict, orphan_quality: dict, operation_started_at: float,
    operation_lease_owner: str | None, operation_request_fingerprint: str | None,
    readiness: dict,
) -> None:
    """崩溃恢复分支的落盘事务：校验权威未漂移后插入 delivery_packages 行并 CAS 更新 episodes。

    调用前调用方必须已提交任何既有事务；本函数自行 BEGIN IMMEDIATE，失败时在
    except 的第一条语句里 rollback，成功路径以 conn.commit() 收尾。
    """
    from app.delivery import _assert_delivery_operation_owner

    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _raise_if_orphan_recovery_authority_drifted(
            conn, episode_id, release_authority, video_delivery_manifest,
        )
        if operation_lease_owner:
            _assert_delivery_operation_owner(
                conn,
                package_id=package_id,
                request_fingerprint=str(operation_request_fingerprint),
                lease_owner=operation_lease_owner,
            )
        _validate_orphan_recovery_artifact_lineage(
            conn,
            episode_id=episode_id,
            package_id=package_id,
            orphan_artifact_id=orphan_artifact_id,
            orphan_package_dir=orphan_package_dir,
            orphan_manifest=orphan_manifest,
            orphan_quality=orphan_quality,
            readiness=readiness,
        )
        _insert_orphan_delivery_package_row_and_cas_episode(
            conn,
            package_id=package_id,
            episode_id=episode_id,
            orphan_artifact_id=orphan_artifact_id,
            orphan_package_dir=orphan_package_dir,
            orphan_manifest=orphan_manifest,
            orphan_quality=orphan_quality,
            operation_started_at=operation_started_at,
            release_authority=release_authority,
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _recover_orphan_delivery_package(
    conn, *, episode_id: str, package_id: str, delivery_root: Path,
    release_authority: dict, video_delivery_manifest: dict, readiness: dict,
    operation_started_at: float, operation_lease_owner: str | None,
    operation_request_fingerprint: str | None,
) -> dict[str, Any] | None:
    """若存在同名 package_id 的崩溃恢复候选目录+Artifact，校验后落盘恢复；否则返回 None。"""
    orphan_package_dir = (delivery_root / package_id).resolve()
    orphan_archive = Path(str(orphan_package_dir) + ".zip")
    orphan_artifact_id = f"art_delivery_{package_id.removeprefix('delivery_')}"
    orphan_artifact = repository.get_artifact(orphan_artifact_id)
    if not (orphan_package_dir.is_dir() and orphan_artifact is not None):
        return None
    orphan_manifest, orphan_quality = _load_and_validate_orphan_snapshot(
        orphan_package_dir, orphan_archive, orphan_artifact,
        release_authority, video_delivery_manifest,
    )
    # This branch recovers production artifacts left by an older process
    # that committed the immutable files/artifact before the package row.
    # Serialize the final authority checks with adoption and operation
    # takeover.  Otherwise a new adopted video could commit after the
    # checks above and the orphan would be resurrected as current.
    _commit_orphan_delivery_recovery(
        conn,
        episode_id=episode_id,
        package_id=package_id,
        release_authority=release_authority,
        video_delivery_manifest=video_delivery_manifest,
        orphan_artifact_id=orphan_artifact_id,
        orphan_package_dir=orphan_package_dir,
        orphan_manifest=orphan_manifest,
        orphan_quality=orphan_quality,
        operation_started_at=operation_started_at,
        operation_lease_owner=operation_lease_owner,
        operation_request_fingerprint=operation_request_fingerprint,
        readiness=readiness,
    )
    return {
        "package_id": package_id,
        "artifact_id": orphan_artifact_id,
        "trust_level": orphan_artifact.get("trust_level", "T3"),
        "status": "waiting_human",
        "package_path": str(orphan_package_dir),
        "archive_path": str(orphan_archive),
        "manifest": orphan_manifest,
        "quality_report": orphan_quality,
        "archive_recovered": True,
    }
