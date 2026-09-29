"""本地曲库清单读取（统一配乐用）——只读 manifest.json + 校验音频文件是否
真的落盘，不下载、不转码（版权来源核验与格式转换是曲库准备阶段的事，见
``/tmp/music_lib/manifest.json`` 的 ``license_policy``/``note_on_verification_method``
字段，本模块只信任已经准备好的 manifest）。

目录或清单缺失/无效时不抛异常——调用方（``app.final_edit_enhance.apply``）
按"曲库缺失"跳过配乐，写进成片报告，不阻断合成（CLAUDE.md「拦住用户时必须
给出路」不适用于配乐这种非门禁增强项，但仍要求可见信号）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import config

_MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class MusicTrack:
    track_id: str
    title: str
    mood_tags: tuple[str, ...]
    duration_s: float
    audio_path: Path


@dataclass(frozen=True)
class MusicLibrary:
    tracks: tuple[MusicTrack, ...]
    dropped: tuple[str, ...]  # 人类可读的丢弃原因（文件缺失/字段畸形等）

    def by_id(self, track_id: str) -> MusicTrack | None:
        return next((t for t in self.tracks if t.track_id == track_id), None)


def resolve_library_dir(raw: str) -> Path:
    """空/相对路径按 ``app.config.RUNTIME_ROOT`` 解析（默认值 ``data/music_library``
    正是相对路径写法，与 ``app.config.DATA_DIR = RUNTIME_ROOT/"data"`` 同一基准）。"""
    trimmed = (raw or "").strip()
    if not trimmed:
        trimmed = "data/music_library"
    path = Path(trimmed)
    return path if path.is_absolute() else (config.RUNTIME_ROOT / path)


def _track_from_entry(entry: dict[str, Any], library_dir: Path) -> tuple[MusicTrack | None, str | None]:
    track_id = str(entry.get("track_id") or "").strip()
    converted = str(entry.get("converted_file") or "").strip()
    if not track_id or not converted:
        return None, f"曲库条目缺少 track_id 或 converted_file：{entry!r}"[:200]
    audio_path = library_dir / converted
    if not audio_path.is_file():
        return None, f"曲目 {track_id} 的音频文件未找到：{audio_path}"
    try:
        duration_s = float(entry.get("duration_s"))
    except (TypeError, ValueError):
        return None, f"曲目 {track_id} 的 duration_s 不是有效数字"
    if duration_s <= 0:
        return None, f"曲目 {track_id} 的 duration_s 非正"
    mood_tags = tuple(str(tag) for tag in (entry.get("mood_tags") or []) if str(tag).strip())
    return MusicTrack(
        track_id=track_id, title=str(entry.get("title") or track_id),
        mood_tags=mood_tags, duration_s=duration_s, audio_path=audio_path,
    ), None


def load_music_library(library_dir: Path) -> MusicLibrary | None:
    """``None`` = 曲库整体不可用（目录/清单缺失或损坏、或一首可用曲目都没有）；
    非 None 时 ``dropped`` 记录被剔除条目的原因，不代表整体失败。"""
    manifest_path = library_dir / _MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw_tracks = payload.get("tracks") if isinstance(payload, dict) else None
    if not isinstance(raw_tracks, list):
        return None
    tracks: list[MusicTrack] = []
    dropped: list[str] = []
    for entry in raw_tracks:
        if not isinstance(entry, dict):
            dropped.append(f"曲库条目不是对象：{entry!r}"[:200])
            continue
        track, reason = _track_from_entry(entry, library_dir)
        if track is None:
            dropped.append(reason or "未知原因")
        else:
            tracks.append(track)
    if not tracks:
        return None
    return MusicLibrary(tracks=tuple(tracks), dropped=tuple(dropped))
