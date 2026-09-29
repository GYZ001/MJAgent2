"""``app.final_edit_enhance.music_library``：曲库清单加载——目录/清单/单曲
缺失都要有可见信号（丢弃原因），不是静默跳过；整体不可用只在"一首可用曲目
都没有"时才发生。
"""
from __future__ import annotations

import json

from app.final_edit_enhance.music_library import load_music_library, resolve_library_dir


def _write_manifest(tmp_path, tracks: list[dict]) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"tracks": tracks}), encoding="utf-8")


def test_load_music_library_missing_directory_returns_none(tmp_path) -> None:
    assert load_music_library(tmp_path / "does-not-exist") is None


def test_load_music_library_missing_manifest_returns_none(tmp_path) -> None:
    assert load_music_library(tmp_path) is None


def test_load_music_library_malformed_json_returns_none(tmp_path) -> None:
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert load_music_library(tmp_path) is None


def test_load_music_library_drops_tracks_with_missing_audio_file(tmp_path) -> None:
    _write_manifest(tmp_path, [
        {"track_id": "t1", "title": "曲一", "duration_s": 120.0, "mood_tags": ["甜"], "converted_file": "converted/t1.m4a"},
    ])
    library = load_music_library(tmp_path)
    assert library is None  # 唯一一条也丢了，整体不可用
    # 换一条真实存在的音频文件，加上一条缺文件的：应各自独立判定
    (tmp_path / "converted").mkdir()
    (tmp_path / "converted" / "t2.m4a").write_bytes(b"fake-audio")
    _write_manifest(tmp_path, [
        {"track_id": "t1", "title": "曲一", "duration_s": 120.0, "mood_tags": ["甜"], "converted_file": "converted/t1.m4a"},
        {"track_id": "t2", "title": "曲二", "duration_s": 90.0, "mood_tags": ["失落"], "converted_file": "converted/t2.m4a"},
    ])
    library = load_music_library(tmp_path)
    assert library is not None
    assert [t.track_id for t in library.tracks] == ["t2"]
    assert len(library.dropped) == 1
    assert "t1" in library.dropped[0]


def test_load_music_library_by_id_and_mood_tags(tmp_path) -> None:
    (tmp_path / "converted").mkdir()
    (tmp_path / "converted" / "t1.m4a").write_bytes(b"fake-audio")
    _write_manifest(tmp_path, [
        {"track_id": "t1", "title": "曲一", "duration_s": 120.5, "mood_tags": ["温柔心动"], "converted_file": "converted/t1.m4a"},
    ])
    library = load_music_library(tmp_path)
    assert library is not None
    track = library.by_id("t1")
    assert track is not None
    assert track.duration_s == 120.5
    assert track.mood_tags == ("温柔心动",)
    assert library.by_id("does-not-exist") is None


def test_resolve_library_dir_relative_path_anchored_at_runtime_root() -> None:
    from app import config

    resolved = resolve_library_dir("data/music_library")
    assert resolved == config.RUNTIME_ROOT / "data" / "music_library"


def test_resolve_library_dir_absolute_path_used_as_is(tmp_path) -> None:
    assert resolve_library_dir(str(tmp_path)) == tmp_path


def test_resolve_library_dir_blank_defaults_to_data_music_library() -> None:
    from app import config

    assert resolve_library_dir("") == config.RUNTIME_ROOT / "data" / "music_library"
