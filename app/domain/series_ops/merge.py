"""连播成片：五步全部完成后，把各集 ``final/episode.mp4`` 用 ffmpeg 拼接。

各集编码参数可能不一致，用 ``filter_complex`` 的 ``concat`` 滤镜逐路先统一
``scale/crop/fps``、音频重采样，再拼接、重编码（交付编码参数照
``app/media_pipeline/delivery_encode.py::DELIVERY_VIDEO_ARGS``，与成片台/
``app/final_edit.py`` 同一份）。先写 ``film.tmp.mp4`` 再原子改名，时长用
``app/media_exec/concat.py::_probe_concat_media`` 校验（容差沿用它的
``_CONCAT_DURATION_TOLERANCE_*`` 常量），失败时旧长片原封不动。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from app import config
from app.db import get_conn
from app.final_edit import FINAL_AUDIO_RATE, FINAL_FPS, _run_ffmpeg
from app.media_exec.concat import (
    _CONCAT_DURATION_TOLERANCE_MIN_S,
    _CONCAT_DURATION_TOLERANCE_RATIO,
    _final_video_path,
    _probe_concat_media,
)
from app.media_pipeline.delivery_encode import (
    DELIVERY_VIDEO_ARGS, canvas_filter, encode_timeout_s, probe_resolution,
)
from app.media_urls import build_media_url


def series_film_dir(project_id: str, episode_from: int, episode_to: int) -> Path:
    return config.PROJECTS_DIR / project_id / "series" / f"ep{episode_from}-ep{episode_to}"


def _probe_durations(paths: list[Path]) -> list[float]:
    durations = []
    for path in paths:
        try:
            probe = _probe_concat_media(str(path))
        except ValueError as exc:
            raise RuntimeError(f"{path.name} 时长探测失败：{exc}") from exc
        durations.append(probe["video_duration_s"])
    return durations


def _build_chapters(episode_nos: list[int], durations: list[float]) -> list[dict]:
    chapters = []
    cursor = 0.0
    for no, dur in zip(episode_nos, durations):
        chapters.append({
            "episode_no": no,
            "start_s": round(cursor, 3),
            "duration_s": round(dur, 3),
        })
        cursor += dur
    return chapters


def _input_fingerprints(paths: list[Path]) -> list[dict]:
    fingerprints = []
    for path in paths:
        stat = path.stat()
        fingerprints.append({
            "path": str(path),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        })
    return fingerprints


def _build_filter_complex(count: int) -> str:
    parts: list[str] = []
    labels: list[str] = []
    for i in range(count):
        parts.append(f"[{i}:v]{canvas_filter()},fps={FINAL_FPS},setsar=1[v{i}]")
        parts.append(f"[{i}:a]aresample={FINAL_AUDIO_RATE}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    concat = f"{''.join(labels)}concat=n={count}:v=1:a=1[outv][outa]"
    return ";".join(parts) + ";" + concat


def _run_concat_ffmpeg(paths: list[Path], out_path: Path, timeout_s: float) -> None:
    cmd = ["ffmpeg", "-y"]
    for path in paths:
        cmd += ["-i", str(path)]
    cmd += [
        "-filter_complex", _build_filter_complex(len(paths)),
        "-map", "[outv]", "-map", "[outa]",
        *DELIVERY_VIDEO_ARGS,
        "-c:a", "aac", "-b:a", "160k", "-ar", str(FINAL_AUDIO_RATE),
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run_ffmpeg(cmd, timeout=timeout_s, context="连播成片合并")


def _validate_merged_duration(tmp_path: Path, expected_total: float) -> dict:
    try:
        probe = _probe_concat_media(str(tmp_path))
    except ValueError as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"连播成片校验失败：{exc}") from exc
    tolerance = max(
        _CONCAT_DURATION_TOLERANCE_MIN_S,
        expected_total * _CONCAT_DURATION_TOLERANCE_RATIO,
    )
    if abs(probe["video_duration_s"] - expected_total) > tolerance:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            "连播成片时长校验失败：期望约 "
            f"{expected_total:.1f}s，实际 {probe['video_duration_s']:.1f}s"
        )
    return probe


def build_series_film(
    project_id: str,
    episode_from: int,
    episode_to: int,
    episode_nos: list[int],
) -> dict:
    """真实跑一次 ffmpeg 合并；任何一步失败都不改动已有 ``film.mp4``。"""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("服务端未找到 ffmpeg/ffprobe，无法生成连播成片")
    final_paths = [_final_video_path(project_id, no) for no in episode_nos]
    missing = [str(no) for no, path in zip(episode_nos, final_paths) if not path.is_file()]
    if missing:
        raise RuntimeError(f"以下集数尚无成片，无法合并：{'、'.join(missing)}")
    durations = _probe_durations(final_paths)
    expected_total = sum(durations)
    out_dir = series_film_dir(project_id, episode_from, episode_to)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = out_dir / "film.tmp.mp4"
    timeout_s = encode_timeout_s(expected_total)
    _run_concat_ffmpeg(final_paths, tmp_path, timeout_s)
    probe = _validate_merged_duration(tmp_path, expected_total)
    film_path = out_dir / "film.mp4"
    os.replace(tmp_path, film_path)
    width, height = probe_resolution(film_path)
    report = {
        "episode_from": episode_from,
        "episode_to": episode_to,
        "chapters": _build_chapters(episode_nos, durations),
        "duration_s": probe["video_duration_s"],
        "size_bytes": film_path.stat().st_size,
        "width": width,
        "height": height,
        "created_at": time.time(),
        "input_fingerprints": _input_fingerprints(final_paths),
        "storyboard_artifact_ids": _storyboard_artifact_ids(project_id, episode_nos),
        "ffmpeg_command_summary": (
            "filter_complex concat(scale/crop/fps/aresample 归一化，lanczos) -> "
            "DELIVERY_VIDEO_ARGS(h264 medium crf20) + aac + faststart"
        ),
    }
    (out_dir / "film.report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return report


def _storyboard_artifact_ids(project_id: str, episode_nos: list[int]) -> dict[str, str | None]:
    """各集当前的分镜产物 id（分镜清空/重做后会变），JSON 键用字符串以便与报告逐字比较。"""
    marks = ",".join("?" for _ in episode_nos)
    rows = get_conn().execute(
        f"SELECT episode_no, storyboard_artifact_id FROM episodes WHERE project_id=? AND episode_no IN ({marks})",
        (project_id, *episode_nos),
    ).fetchall()
    found = {str(row["episode_no"]): row["storyboard_artifact_id"] for row in rows}
    return {str(no): found.get(str(no)) for no in episode_nos}


def merge_is_current(
    project_id: str,
    episode_from: int,
    episode_to: int,
    episode_nos: list[int],
) -> bool:
    """成片存在且 report 记录的各集输入指纹（mtime+size）与当前一致才算未过期。"""
    out_dir = series_film_dir(project_id, episode_from, episode_to)
    report_path = out_dir / "film.report.json"
    if not (out_dir / "film.mp4").is_file() or not report_path.is_file():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    # 2026-09-03 实测：清空分镜后各集 final/episode.mp4 原封不动，只看文件指纹会把
    # 一部分镜已经不存在的成片判成「未过期」，用户重新入队被跳过。分镜产物 id 是
    # 第二个输入指纹；旧报告没有这个键时只按文件指纹判（不把历史成片全判成过期）。
    recorded_ids = report.get("storyboard_artifact_ids")
    if recorded_ids is not None and recorded_ids != _storyboard_artifact_ids(project_id, episode_nos):
        return False
    recorded = {item["path"]: item for item in report.get("input_fingerprints") or []}
    for no in episode_nos:
        path = _final_video_path(project_id, no)
        item = recorded.get(str(path))
        if item is None or not path.is_file():
            return False
        stat = path.stat()
        if item.get("mtime_ns") != stat.st_mtime_ns or item.get("size") != stat.st_size:
            return False
    return True


def _film_projection(out_dir: Path) -> dict | None:
    film_path = out_dir / "film.mp4"
    if not film_path.is_file():
        return None
    try:
        report = json.loads((out_dir / "film.report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    stat = film_path.stat()
    return {
        "url": build_media_url(str(film_path), version=f"{stat.st_mtime_ns}-{stat.st_size}"),
        "path": str(film_path.relative_to(config.PROJECTS_DIR)),
        "duration_s": float(report.get("duration_s") or 0.0),
        "size_bytes": int(stat.st_size),
        "created_at": float(report.get("created_at") or stat.st_mtime),
        "episode_from": int(report.get("episode_from") or 0),
        "episode_to": int(report.get("episode_to") or 0),
        "chapters": report.get("chapters") or [],
        "width": int(report.get("width") or 0),
        "height": int(report.get("height") or 0),
    }


def film_for_range(project_id: str, episode_from: int, episode_to: int) -> dict | None:
    return _film_projection(series_film_dir(project_id, episode_from, episode_to))


def latest_film(project_id: str) -> dict | None:
    """项目下最近一次成功合成的连播成片，可能来自比当前 run 更早的一次运行。"""
    base = config.PROJECTS_DIR / project_id / "series"
    if not base.is_dir():
        return None
    candidates = [child for child in base.iterdir() if (child / "film.mp4").is_file()]
    if not candidates:
        return None
    newest = max(candidates, key=lambda child: (child / "film.mp4").stat().st_mtime)
    return _film_projection(newest)
