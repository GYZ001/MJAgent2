"""needs_crop 自动裁切：ffmpeg 后处理（不计费）。

对片头片尾无效段做轻量裁剪；成功则写出新本地版本供采用，不触发 Seedance。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.db import get_conn, new_id, now
from app.evidence.media import grade_shot_video, record_video_candidate, validate_video_file


def _ffprobe_duration(path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        raw = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, check=True, timeout=20,
        ).stdout
        data = json.loads(raw or "{}")
        return float((data.get("format") or {}).get("duration") or 0) or None
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def auto_crop_video(
    src_path: str,
    dest_path: str,
    *,
    expected_duration_s: float = 5.0,
    head_trim_s: float = 0.35,
    tail_trim_s: float = 0.35,
) -> dict[str, Any]:
    """裁掉头尾无效段；保持目标时长合同附近。

    返回 ``{ok, path, error?, duration_s?}``。
    """
    if not shutil.which("ffmpeg"):
        return {"ok": False, "error": "ffmpeg_unavailable"}
    src = Path(src_path)
    if not src.is_file():
        return {"ok": False, "error": "source_missing"}
    duration = _ffprobe_duration(src)
    if duration is None or duration <= 0:
        return {"ok": False, "error": "probe_failed"}

    # 可裁空间不足则只做极短修剪
    max_trim = max(0.0, (duration - max(3.0, expected_duration_s * 0.7)) / 2)
    head = min(head_trim_s, max_trim)
    tail = min(tail_trim_s, max_trim)
    if head + tail <= 0.05:
        return {"ok": False, "error": "nothing_to_trim"}

    out_dur = max(0.5, duration - head - tail)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 用 -ss/-t 重封装；失败则尝试重编码
    cmd_copy = [
        "ffmpeg", "-y", "-ss", f"{head:.3f}", "-i", str(src),
        "-t", f"{out_dur:.3f}", "-c", "copy", "-movflags", "+faststart", str(dest),
    ]
    try:
        subprocess.run(cmd_copy, capture_output=True, text=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        cmd_re = [
            "ffmpeg", "-y", "-ss", f"{head:.3f}", "-i", str(src),
            "-t", f"{out_dur:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-movflags", "+faststart", str(dest),
        ]
        try:
            subprocess.run(cmd_re, capture_output=True, text=True, check=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"ffmpeg_failed:{exc}"}

    if not dest.is_file() or dest.stat().st_size <= 0:
        return {"ok": False, "error": "empty_output"}
    technical = validate_video_file(str(dest), expected_duration_s=expected_duration_s)
    return {
        "ok": bool(technical.get("passed")),
        "path": str(dest),
        "duration_s": _ffprobe_duration(dest),
        "technical": technical,
        "head_trim_s": head,
        "tail_trim_s": tail,
        "error": None if technical.get("passed") else "technical_failed",
    }


def try_auto_crop_shot_version(version_id: str) -> dict[str, Any] | None:
    """对成功版做自动裁切，写入新 succeeded 版本（cost_cny=0）。成功返回新 version 摘要。"""
    from app.config import PROJECTS_DIR

    conn = get_conn()
    row = conn.execute(
        """SELECT v.*, s.episode_id, s.duration_s, s.shot_no, e.project_id, e.episode_no
           FROM shot_versions v
           JOIN shots s ON s.id=v.shot_id
           JOIN episodes e ON e.id=s.episode_id
           WHERE v.id=?""",
        (version_id,),
    ).fetchone()
    if not row or row["status"] != "succeeded" or not row["video_path"]:
        return None
    src = Path(row["video_path"])
    if not src.is_file():
        return None

    dest_dir = PROJECTS_DIR / row["project_id"] / f"ep{int(row['episode_no']):02d}" / "videos"
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_no = (conn.execute(
        "SELECT COALESCE(MAX(version_no),0) AS m FROM shot_versions WHERE shot_id=?",
        (row["shot_id"],),
    ).fetchone()["m"]) + 1
    dest = dest_dir / f"shot{int(row['shot_no']):02d}_v{new_no}_crop.mp4"
    result = auto_crop_video(
        str(src), str(dest), expected_duration_s=float(row["duration_s"] or 5),
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "source_version_id": version_id}

    new_id_ver = new_id("ver")
    meta = {
        "auto_crop": True,
        "crop_from_version_id": version_id,
        "head_trim_s": result.get("head_trim_s"),
        "tail_trim_s": result.get("tail_trim_s"),
        "supervisor_strategy": "auto_crop",
    }
    # 复制 QA（裁切后构图问题可能缓解，保留原 QA 供对比，标记 crop_applied）
    qa = json.loads(row["qa_json"] or "{}")
    if isinstance(qa, dict):
        qa = dict(qa)
        qa["crop_applied"] = True
        fts = [x for x in (qa.get("failure_types") or []) if x != "needs_crop"]
        qa["failure_types"] = fts
    conn.execute(
        """INSERT INTO shot_versions(
            id, shot_id, version_no, prompt_text, idem_key, status, created_at,
            image_inputs, video_path, qa_json, cost_cny, technical_validation_json
        ) VALUES(?,?,?,?,?, 'succeeded', ?, ?, ?, ?, 0, ?)""",
        (
            new_id_ver, row["shot_id"], new_no, row["prompt_text"],
            f"crop:{version_id}:{new_no}", now(),
            json.dumps(meta, ensure_ascii=False),
            str(dest),
            json.dumps(qa, ensure_ascii=False),
            json.dumps({
                **(result.get("technical") or {}),
                "issues": [
                    i.model_dump(mode="json") if hasattr(i, "model_dump") else i
                    for i in ((result.get("technical") or {}).get("issues") or [])
                ],
            }, ensure_ascii=False),
        ),
    )
    conn.commit()
    try:
        record_video_candidate(new_id_ver)
    except Exception:  # noqa: BLE001
        pass
    graded = grade_shot_video(row["shot_id"], version_row=dict(
        conn.execute("SELECT * FROM shot_versions WHERE id=?", (new_id_ver,)).fetchone()
    ))
    return {
        "ok": True,
        "version_id": new_id_ver,
        "source_version_id": version_id,
        "grade": graded.get("grade"),
        "path": str(dest),
    }
