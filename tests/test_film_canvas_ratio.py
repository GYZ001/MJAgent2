"""画幅随项目切换（P0 回归）：draft_concat 与 final_edit 两条合成路径都必须
产出调用方显式传入 ``play_res`` 对应分辨率的成片，不再默认写死 1080x1920。

lavfi 生成的小尺寸短时长源，一次只跑一个 ffmpeg，用 ``nice -n 19`` 降低对本机
（2 核）其它任务的影响；两段刻意用不同分辨率（64x64 / 96x96）强制触发
``_piece_video_args`` 的 needs_scale 重编码分支，否则同分辨率会走 ``-c:v copy``
直通、看不出 canvas_filter 是否真的生效。
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import db
from app.final_edit import render_episode_final_edit
from app.media_exec.concat import _draft_concat_pieces, _probe_concat_media
from app.media_pipeline.delivery_encode import probe_resolution

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")

_RATIOS = [(1080, 1920), (1920, 1080)]


def _make_clip(path: Path, *, size: str, duration_s: float = 1.0) -> None:
    subprocess.run(
        ["nice", "-n", "19", "ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=red:s={size}:r=24:d={duration_s}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration_s}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", str(path)],
        check=True, capture_output=True, timeout=30,
    )


@pytest.mark.parametrize("play_res", _RATIOS, ids=["9x16", "16x9"])
def test_draft_concat_final_resolution_matches_play_res(tmp_path: Path, play_res: tuple[int, int]) -> None:
    clip1, clip2 = tmp_path / "shot1.mp4", tmp_path / "shot2.mp4"
    _make_clip(clip1, size="64x64")
    _make_clip(clip2, size="96x96")
    piece_specs = [(1, str(clip1), 1.0), (2, str(clip2), 1.0)]
    probe_by_shot = {no: _probe_concat_media(path) for no, path, _rate in piece_specs}
    final_path = tmp_path / "episode.mp4"

    _total_dur, publish_candidate, _artifacts, _clip_loudness = _draft_concat_pieces(
        piece_specs, probe_by_shot, final_path, concat_timeout_s=60.0, subtitle_plan=None,
        play_res=play_res,
    )

    assert probe_resolution(publish_candidate) == play_res


def _seed_two_shot_episode(conn: sqlite3.Connection) -> None:
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('e','p',1,'E','confirmed',0)"
    )
    for shot_no in (1, 2):
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
                   scene_name,characters,action_desc,source_excerpt,dialogues,transition,
                   continuity_from_prev,continuity_mode,shot_contract_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"s{shot_no}", "e", shot_no, 1, "中景", "固定", "测试", "测试场", "[]", "",
             "", "[]", "硬切", 0, "", "{}"),
        )
    conn.commit()


@pytest.mark.parametrize("play_res", _RATIOS, ids=["9x16", "16x9"])
def test_final_edit_final_resolution_matches_play_res(tmp_path: Path, play_res: tuple[int, int]) -> None:
    from app.final_edit import _font_path
    try:
        _font_path()
    except RuntimeError:
        pytest.skip("test host has no configured CJK font")

    clip1, clip2 = tmp_path / "shot1.mp4", tmp_path / "shot2.mp4"
    _make_clip(clip1, size="64x64")
    _make_clip(clip2, size="96x96")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_two_shot_episode(conn)
    destination = tmp_path / "final.mp4"

    render_episode_final_edit(
        conn, "e", [(1, str(clip1), 1.0), (2, str(clip2), 1.0)], destination, tmp_path / "work",
        play_res=play_res,
    )

    assert probe_resolution(destination) == play_res


# ---------------------------------------------------------------------------
# 「是否最新」判据（派单 B 项）：纯逻辑，不需要真实 ffmpeg，独立于上面的 skipif。
# ---------------------------------------------------------------------------


def _stale_check_rig(tmp_path: Path, monkeypatch):
    from app import config
    from app.media_exec.concat import _final_video_is_stale, _final_video_path

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.commit()
    final_path = _final_video_path("p", 1)
    final_path.write_bytes(b"fake-final-video")
    return conn, {"project_id": "p", "episode_no": 1}, _final_video_is_stale


def test_final_video_is_stale_matches_current_settings_is_fresh(tmp_path, monkeypatch):
    conn, ep_row, is_stale = _stale_check_rig(tmp_path, monkeypatch)
    report = {"canvas": {"width": 1080, "height": 1920}, "ai_label_enabled": False}
    assert is_stale(conn, ep_row, report) is False


def test_final_video_is_stale_flags_canvas_drift(tmp_path, monkeypatch):
    conn, ep_row, is_stale = _stale_check_rig(tmp_path, monkeypatch)
    conn.execute("UPDATE projects SET aspect_ratio='16:9' WHERE id='p'")
    report = {"canvas": {"width": 1080, "height": 1920}, "ai_label_enabled": False}
    assert is_stale(conn, ep_row, report) is True


def test_final_video_is_stale_flags_ai_label_drift(tmp_path, monkeypatch):
    conn, ep_row, is_stale = _stale_check_rig(tmp_path, monkeypatch)
    conn.execute("UPDATE projects SET ai_label_enabled=1 WHERE id='p'")
    report = {"canvas": {"width": 1080, "height": 1920}, "ai_label_enabled": False}
    assert is_stale(conn, ep_row, report) is True


def test_final_video_is_stale_legacy_report_without_canvas_key_assumes_9_16(tmp_path, monkeypatch):
    """2026-09-23 前产出的旧报告没有 canvas/ai_label_enabled 键；项目仍是默认 9:16
    时不能被当成「无法判断」一律判脏，否则全仓存量成片会在升级后集体变成过期。"""
    conn, ep_row, is_stale = _stale_check_rig(tmp_path, monkeypatch)
    assert is_stale(conn, ep_row, {}) is False
    assert is_stale(conn, ep_row, None) is False

    conn.execute("UPDATE projects SET aspect_ratio='16:9' WHERE id='p'")
    assert is_stale(conn, ep_row, {}) is True
