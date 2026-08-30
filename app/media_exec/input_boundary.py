"""首末帧模式共用的边界资产与执行计划原语（拆分自 ``run_job.py``）。

``_ContinuityWait`` 是首帧/首末帧/参考图/通用视频输入准备四条路径共用的哨兵异
常，标记「本镜需要等待上一镜的连续性锚点，不是失败」。``_image_dimensions``/
``_normalize_boundary_pair``/``_load_boundary_asset``/``_persist_boundary_asset``
是边界帧（首帧、末帧）的探测、归一化（分辨率/宽高比对齐）与持久化；
``_resolve_current_execution_plan`` 读取 job 当前生效的执行计划快照。本文件不
含任何模式专属的准备逻辑（那些在 ``.input_reference``/``.input_first_frame_last``/
``.input_video_mode``），只是它们共同依赖的底层工具。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.db import new_id, now

from .fences import VideoInputRepairRequired


class _ContinuityWait(Exception):
    """Local inputs are progressing but one declared boundary is not ready."""

    def __init__(self, reason: str, *, reason_code: str = "WAITING_VIDEO_PLAN_DEPENDENCY"):
        super().__init__(reason)
        self.reason = reason
        self.reason_code = reason_code


def _image_dimensions(path: str) -> tuple[int, int]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=20, check=True,
        )
        stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
        return int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
        return 0, 0


def _normalize_boundary_pair(
    first_path: str,
    last_path: str,
) -> tuple[str, str, tuple[int, int]]:
    """Normalize an aspect-compatible pair to one deterministic resolution."""
    sizes = {
        first_path: _image_dimensions(first_path),
        last_path: _image_dimensions(last_path),
    }
    if any(not all(size) for size in sizes.values()):
        raise VideoInputRepairRequired(
            "首尾帧尺寸不可识别："
            f"first={sizes[first_path]}, last={sizes[last_path]}"
        )
    first_size = sizes[first_path]
    last_size = sizes[last_path]
    cross_error = abs(
        first_size[0] * last_size[1] - last_size[0] * first_size[1]
    )
    cross_scale = max(
        first_size[0] * last_size[1],
        last_size[0] * first_size[1],
        1,
    )
    if cross_error / cross_scale > 0.005:
        raise VideoInputRepairRequired(
            "首尾帧宽高比不一致，禁止裁切后伪造边界合同："
            f"first={first_size}, last={last_size}"
        )
    target = min((first_size, last_size), key=lambda size: size[0] * size[1])
    for path, size in sizes.items():
        if size == target:
            continue
        source = Path(path)
        if not source.is_file():
            raise VideoInputRepairRequired(f"首尾帧文件不存在：{path}")
        with tempfile.NamedTemporaryFile(
            prefix=f".{source.stem}.normalized-",
            suffix=source.suffix or ".jpg",
            dir=source.parent,
            delete=False,
        ) as handle:
            normalized = Path(handle.name)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(source),
                    "-vf", f"scale={target[0]}:{target[1]}:flags=lanczos",
                    "-frames:v", "1", str(normalized),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            normalized.replace(source)
        except (OSError, subprocess.SubprocessError) as exc:
            normalized.unlink(missing_ok=True)
            raise VideoInputRepairRequired(
                f"首尾帧统一尺寸失败：{type(exc).__name__}: {exc}"
            ) from exc
    return first_path, last_path, target


def _load_boundary_asset(conn, shot_plan_id: str, role: str, fingerprint: str):
    row = conn.execute(
        """SELECT * FROM video_boundary_assets
           WHERE shot_plan_id=? AND role=? AND fingerprint=? AND qa_status='passed'
           ORDER BY created_at DESC LIMIT 1""",
        (shot_plan_id, role, fingerprint),
    ).fetchone()
    if row and row["path"] and Path(row["path"]).is_file():
        return row
    return None


def _persist_boundary_asset(
    conn,
    *,
    shot_plan,
    role: str,
    source: str,
    source_revision_id: str,
    source_shot_id: str | None,
    source_adopted_version_id: str | None,
    path: str,
    fingerprint: str,
    qa: dict[str, Any],
) -> None:
    raw = Path(path).read_bytes()
    width, height = _image_dimensions(path)
    conn.execute(
        """INSERT OR REPLACE INTO video_boundary_assets(
               id,episode_video_plan_id,shot_plan_id,shot_id,role,source,
               source_revision_id,source_shot_id,source_adopted_version_id,
               path,sha256,mime,width,height,qa_status,qa_json,fingerprint,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("vba"), shot_plan.episode_video_plan_id, shot_plan.shot_plan_id,
            shot_plan.shot_id, role, source, source_revision_id, source_shot_id,
            source_adopted_version_id, path, hashlib.sha256(raw).hexdigest(),
            "image/jpeg", width, height, "passed", json.dumps(qa, ensure_ascii=False),
            fingerprint, now(),
        ),
    )


def _resolve_current_execution_plan(
    conn,
    shot_id: str,
    meta: dict,
):
    """Rebind an equivalent sibling-replanned contract to the current plan."""
    from app.video_plan import active_plan_is_current, get_shot_plan

    current = get_shot_plan(shot_id, conn=conn)
    submitted_id = str(meta.get("shot_plan_id") or "")
    if current is None or not submitted_id:
        return None
    if current.shot_plan_id == submitted_id:
        return current
    if not active_plan_is_current(submitted_id, conn=conn):
        return None
    meta.setdefault("submitted_shot_plan_id", submitted_id)
    meta.setdefault(
        "submitted_episode_video_plan_id",
        meta.get("episode_video_plan_id"),
    )
    meta.update({
        "shot_plan_id": current.shot_plan_id,
        "episode_video_plan_id": current.episode_video_plan_id,
        "plan_revision": current.plan_revision,
        "source_storyboard_revision_id": current.source_storyboard_revision_id,
        "capability_snapshot_id": current.capability_snapshot_id,
        "input_revision_fingerprints": dict(current.input_revision_fingerprints),
        "planned_mode": current.mode.value,
        "equivalent_plan_rebound": True,
        "equivalent_plan_rebound_at": now(),
    })
    return current

__all__ = [name for name in globals() if not name.startswith("__")]
