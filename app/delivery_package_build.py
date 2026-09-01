"""build_delivery_package 的「从零落盘」阶段：准备工作目录、写只读快照、拷贝
媒体文件、组装 manifest/quality-report 与人类可读报告。不含事务与最终发布，
纯粹是把已经确定要写的东西写到 package_dir 底下。

`_write_json`/`_copy_if_present`/`_sanitize_delivery_value`/`_sha256` 定义在
app.delivery 里且被该模块其余函数共用，本文件在函数体内惰性导入它们，避免
与 app.delivery 顶层互相导入成环。
"""

from __future__ import annotations

import html
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from app import config
from app.db import rows_to_dicts
from app.orchestration.engine import fingerprint


def _resolve_delivery_final_package_dir(package_id: str, delivery_root: Path) -> Path:
    """交付包发布后的最终目录路径；越权路径直接拒绝。"""
    final_package_dir = (delivery_root / package_id).resolve()
    if not final_package_dir.is_relative_to(delivery_root):
        raise ValueError("非法的 package_id")
    return final_package_dir


def _resolve_delivery_workspace_dir(
    conn, *, package_id: str, delivery_root: Path,
    operation_lease_owner: str | None, operation_request_fingerprint: str | None,
) -> tuple[Path, Any]:
    """确定本次构建使用的临时工作目录；持有 lease 时必须绑定到该 lease 的 receipt 行。

    首次进入 building 阶段时把 workspace_path 写回 receipt（CAS 更新，rowcount 校验
    被接管）；返回 (package_dir, receipt_row)，receipt_row 供调用方判断崩溃恢复遗留。
    """
    owner_suffix = operation_lease_owner or uuid.uuid4().hex
    package_dir = (delivery_root / f".{package_id}.{owner_suffix}.tmp").resolve()
    if not operation_lease_owner:
        return package_dir, None
    receipt_row = conn.execute(
        """SELECT workspace_path,promotion_phase,abandoned_workspace_path,
                  abandoned_promotion_phase,recovery_fenced_owner
             FROM delivery_operation_receipts
           WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
             AND status='running'""",
        (package_id, operation_request_fingerprint, operation_lease_owner),
    ).fetchone()
    if receipt_row is None:
        raise ValueError("交付 operation workspace owner 已失效")
    bound_workspace = str(receipt_row["workspace_path"] or "")
    if bound_workspace:
        return Path(bound_workspace).resolve(), receipt_row
    updated = conn.execute(
        """UPDATE delivery_operation_receipts
              SET workspace_path=?,promotion_phase='building',updated_at=?
            WHERE package_id=? AND request_fingerprint=? AND lease_owner=?
              AND status='running'""",
        (
            str(package_dir), time.time(), package_id,
            operation_request_fingerprint, operation_lease_owner,
        ),
    )
    if updated.rowcount != 1:
        raise ValueError("交付 operation workspace owner 已被接管")
    conn.commit()
    return package_dir, receipt_row


def _purge_startup_fenced_delivery_remnants(
    receipt_row: Any, final_package_dir: Path,
    *, delivery_root: Path, operation_lease_owner: str | None,
) -> None:
    """新 owner 启动期被 fenced 接管旧 owner 时，若旧 owner 已推进到 ready 及以后阶段，
    清除它在 final_package_dir 与其自身遗留工作目录下的产物（只有这一证据允许删除，
    单纯租约到期不足以断定旧线程已经真正停止）。
    """
    if receipt_row is None or not operation_lease_owner:
        return
    abandoned_workspace = str(receipt_row["abandoned_workspace_path"] or "")
    abandoned_phase = str(receipt_row["abandoned_promotion_phase"] or "")
    startup_fenced = (
        str(receipt_row["recovery_fenced_owner"] or "") == str(operation_lease_owner or "")
    )
    if not (startup_fenced and abandoned_phase in {"ready", "directory_promoted", "promoted"}):
        return
    abandoned_path = Path(abandoned_workspace).resolve() if abandoned_workspace else None
    if abandoned_path is not None and not abandoned_path.is_relative_to(delivery_root):
        raise ValueError("交付恢复遗留目录超出当前剧集目录")
    if final_package_dir.exists():
        shutil.rmtree(final_package_dir)
    Path(str(final_package_dir) + ".zip").unlink(missing_ok=True)
    if abandoned_path is not None and abandoned_path.exists():
        shutil.rmtree(abandoned_path)
    if abandoned_path is not None:
        Path(str(abandoned_path) + ".zip").unlink(missing_ok=True)


def _create_delivery_workspace_dir(package_dir: Path) -> tuple[Path, Path]:
    """清空旧工作目录残留后重新创建，返回 (archive_candidate, snapshots_dir)。"""
    if package_dir.exists():
        shutil.rmtree(package_dir)
    archive_candidate = Path(str(package_dir) + ".zip")
    if archive_candidate.exists():
        archive_candidate.unlink()
    package_dir.mkdir(parents=True, exist_ok=False)
    snapshots = package_dir / "snapshots"
    snapshots.mkdir()
    return archive_candidate, snapshots


def _write_delivery_snapshots(
    conn, snapshots: Path, *, project: Any, screenplay_context: Any,
    episode_id: str, ep: Any, readiness: dict,
) -> None:
    """把人物设定/剧本/来源章节/分镜表四份只读快照落盘到 package_dir/snapshots。"""
    from app.delivery import _sanitize_delivery_value, _write_json

    _write_json(
        snapshots / "character-bible.json",
        _sanitize_delivery_value(json.loads(project["bible_json"] or "{}")),
    )
    _write_json(
        snapshots / "screenplay.json",
        _sanitize_delivery_value(screenplay_context.screenplay.model_dump(mode="json")),
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


def _collect_delivery_media_files(
    package_dir: Path, snapshots: Path, ep: Any, readiness: dict,
) -> list[dict[str, Any]]:
    """拷贝终剪成片/终剪报告/逐镜成片到工作目录，连同快照汇总成 manifest 的 files 列表
    （已含每个文件的 sha256/size_bytes）。
    """
    from app.delivery import _copy_if_present, _sha256

    files: list[dict[str, Any]] = []
    final_source = (
        config.PROJECTS_DIR / ep["project_id"] / "episodes"
        / str(ep["episode_no"]) / "final" / "episode.mp4"
    )
    final_copy = _copy_if_present(str(final_source), package_dir / "media" / "episode.mp4")
    if final_copy:
        files.append({
            "role": "final_video", "path": final_copy.relative_to(package_dir).as_posix(),
        })
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
        files.append({
            "role": "snapshot", "path": snapshot.relative_to(package_dir).as_posix(),
        })
    for item in files:
        path = package_dir / item["path"]
        item.update({"sha256": _sha256(path), "size_bytes": path.stat().st_size})
    return files


def _build_delivery_manifest(
    package_id: str, episode_id: str, ep: Any, operation_started_at: float,
    release_authority: dict, video_delivery_manifest: dict, readiness: dict,
    files: list[dict],
) -> dict[str, Any]:
    """组装交付 manifest.json 的完整结构（含可复现性指纹）。"""
    return {
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
            "shot_duration_range_s": [
                config.VIDEO_DURATION_MIN_S, config.VIDEO_DURATION_MAX_S,
            ],
            "shot_duration_decided_by": "model",
        },
    }


def _build_delivery_quality_report(
    gate_findings: list[dict], readiness: dict,
    *, decision: str | None, decided_by: str | None, reason: str, accepted_risk: str | None,
) -> dict[str, Any]:
    """组装交付 quality-report.json（评分不阻断，人工决定字段先占位待审批落盘时填）。"""
    from app.delivery import _sanitize_delivery_value

    return {
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


def _render_delivery_quality_report_html(
    ep: Any, readiness: dict, *, decision: str | None, reason: str,
) -> str:
    """渲染人类可读的质量报告 HTML（quality-report.html）。"""
    return (
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


def _build_delivery_known_issues_lines(
    readiness: dict, accepted_risk: str | None,
) -> list[str]:
    """组装 known-issues.md 的行列表（未落盘，调用方负责 join 后写文件）。"""
    known_lines = ["# Known Issues", ""]
    if readiness["warnings"]:
        known_lines.extend(
            f"- {item.get('message', item.get('code', '未知风险'))}"
            for item in readiness["warnings"]
        )
    else:
        known_lines.append("- 无已知残余问题。")
    if accepted_risk:
        known_lines.extend(["", "## Accepted Risk", "", f"- {accepted_risk}"])
    return known_lines


def _append_delivery_report_files(files: list[dict], package_dir: Path) -> None:
    """把 quality-report.json/html 与 known-issues.md 三份报告追加进 files 清单（原地修改）。"""
    from app.delivery import _sha256

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
