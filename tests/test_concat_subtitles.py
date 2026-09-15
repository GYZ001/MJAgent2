"""``app.media_exec.concat`` 的字幕嵌入接线：draft_concat 路径真烧字幕、缓存
命中、关闭时与改动前逐项相等的 ``_run_concat_demuxer`` 参数。

真实 ffmpeg 编码 + 解码，缺 ffmpeg/ffprobe 则 skip；不依赖真实 ASR 模型——
``engine.transcribe_media``/``engine.engine_status`` 用 monkeypatch 注入与
台词一致的固定 token，隔离出「字幕接线是否正确」这一件事，不牵扯语音识别
本身的准确率（那是 U1/U2 的验收范围）。

DB 用 ``app.db.get_conn()`` 直接读写（同 ``tests/test_subtitles_episode.py``/
``tests/test_subtitles_store.py``）：``tests/conftest.py`` 的 autouse fixture
每个测试克隆一份独立数据库，``store.put_alignment`` 的独立连接与本文件的
``get_conn()`` 都落在同一份 ``db.DB_PATH``，缓存写入/读出天然一致——不需要
像 ``tests/test_episode_partial_concat.py`` 那样手搭 ``:memory:`` 连接
（那条路径下 ``store.put_alignment`` 的独立连接会连到真实 ``db.DB_PATH``，
与测试自建的 ``:memory:`` 连接是两个互不相通的数据库，缓存断言必假）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app import config, worker
from app.db import get_conn
from app.subtitles import engine

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")

_DURATION_S = 3.0
_TOKENS = {
    "v1": [("你", 0.5), ("好", 0.7), ("世", 0.9), ("界", 1.1)],
    "v2": [("再", 0.5), ("见", 0.7), ("世", 0.9), ("界", 1.1)],
}


def _make_clip(path: Path, *, color: str, audio: bool = True) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c={color}:s=1080x1920:r=24:d={_DURATION_S}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={_DURATION_S}"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-ar", "44100"] if audio else ["-an"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)


def _segment(text: str) -> dict:
    return {"storyboard_pack_segment": {
        "dialogue": [{"utterance_id": "U01", "line": text, "speaker_identity_id": "旁白", "delivery_kind": "narration"}],
        "resources": {"characters": []},
    }}


def _seed(conn, project_root: Path) -> tuple[Path, Path]:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) VALUES('e','p',1,'E','confirmed',0)"
    )
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    path1, path2 = shot_dir / "shot-1.mp4", shot_dir / "shot-2.mp4"
    _make_clip(path1, color="blue")
    _make_clip(path2, color="green")
    for no, path, text in ((1, path1, "你好世界"), (2, path2, "再见世界")):
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_contract_json) VALUES(?,?,?,?,?)",
            (f"s{no}", "e", no, _DURATION_S, json.dumps(_segment(text), ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at) "
            "VALUES(?,?,1,'p','k','succeeded',?,0)",
            (f"v{no}", f"s{no}", str(path)),
        )
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (f"v{no}", f"s{no}"))
    conn.commit()
    return path1, path2


def _seed3(conn, project_root: Path) -> tuple[Path, Path, Path]:
    """3 镜，中间一镜 ``audio=False``（``-an``，真实无音轨容器）——用于验收实测的
    「无音轨镜头拖垮整集合成」回归：见模块末尾 ``test_no_audio_middle_shot_...``。
    """
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) VALUES('e','p',1,'E','confirmed',0)"
    )
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    path1, path2, path3 = shot_dir / "shot-1.mp4", shot_dir / "shot-2.mp4", shot_dir / "shot-3.mp4"
    _make_clip(path1, color="blue")
    _make_clip(path2, color="green", audio=False)
    _make_clip(path3, color="red")
    for no, path, text in ((1, path1, "你好世界"), (2, path2, "无声镜头"), (3, path3, "再见世界")):
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_contract_json) VALUES(?,?,?,?,?)",
            (f"s{no}", "e", no, _DURATION_S, json.dumps(_segment(text), ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at) "
            "VALUES(?,?,1,'p','k','succeeded',?,0)",
            (f"v{no}", f"s{no}", str(path)),
        )
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (f"v{no}", f"s{no}"))
    conn.commit()
    return path1, path2, path3


def _fake_transcribe(jobs):
    return {vid: engine.AsrResult(text="", tokens=tuple(_TOKENS[vid])) for vid in jobs}


def _ok_status():
    return engine.EngineStatus(ok=True, model_dir=Path("/fake"), model_id="test-model", problems=(), engine_id="sherpa-onnx/test")


def _manifest(episode_id, conn=None):
    rows = (conn or get_conn()).execute(
        """SELECT s.shot_no,s.adopted_version_id,v.playback_rate,v.video_path
             FROM shots s LEFT JOIN shot_versions v ON v.id=s.adopted_version_id
            WHERE s.episode_id=? ORDER BY s.shot_no""", (episode_id,),
    ).fetchall()
    items = [
        {"shot_id": f"s{row['shot_no']}", "shot_no": row["shot_no"], "adopted_version_id": row["adopted_version_id"],
         "playback_rate": float(row["playback_rate"] or 1), "video_path": row["video_path"],
         "file_sha256": f"sha{row['shot_no']}"}
        for row in rows
    ]
    return {"manifest_hash": json.dumps(items, sort_keys=True), "items": items}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from app import downstream_authority

    conn = get_conn()
    project_root = tmp_path / "projects"
    path1, path2 = _seed(conn, project_root)
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(
        downstream_authority, "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: {"published_storyboard_artifact_id": f"sb:{episode_id}", "release_qualification_hash": "cur"},
    )
    monkeypatch.setattr(downstream_authority, "current_partial_adopted_video_delivery_manifest", _manifest)
    monkeypatch.setattr(engine, "engine_status", lambda: _ok_status())
    monkeypatch.setattr(engine, "transcribe_media", _fake_transcribe)
    return path1, path2


def _final_path(project_root: Path) -> Path:
    return project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"


def _probe_video_duration(path: Path) -> float:
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    return float(json.loads(raw)["streams"][0]["duration"])


def _extract_frame(video_path: Path, at_s: float, out_png: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(at_s), "-i", str(video_path), "-frames:v", "1", str(out_png)],
        check=True, capture_output=True, timeout=30,
    )


def _max_brightness(image: Image.Image, *, x_range: range, y_range: range) -> int:
    return max(image.getpixel((x, y)) for y in y_range for x in x_range)


def test_draft_concat_burns_subtitles_real_frame_and_writes_sidecars(tmp_path, rig, monkeypatch):
    path1, _path2 = rig
    from app.db import set_setting

    set_setting("subtitle_burn_in_enabled", "true")
    project_root = config.PROJECTS_DIR

    result = worker.concatenate_episode("e")

    final_path = _final_path(project_root)
    assert final_path.is_file()
    ass_path = final_path.with_name("episode.ass")
    srt_path = final_path.with_name("episode.srt")
    assert ass_path.is_file()
    assert srt_path.is_file()

    report = result["final_edit"]["subtitles"]
    assert report["enabled"] is True
    assert report["lines_total"] == 2
    assert report["lines_aligned"] == 2
    assert report["cache_hits"] == 0
    assert report["asr_shots"] == 2

    cues = report["cues_timeline"]
    shot1_cue = next(c for c in cues if c["shot_no"] == 1)
    shot2_cue = next(c for c in cues if c["shot_no"] == 2)
    shot1_measured = _probe_video_duration(path1)
    # 第二镜首条 cue 时间 = 第一镜实测时长 + 镜内本地时间（draft_concat 全部 xfade=0）。
    assert abs(shot2_cue["start_s"] - (shot1_measured + shot1_cue["start_s"])) < 0.3

    png_path = tmp_path / "frame.png"
    _extract_frame(final_path, 1.0, png_path)
    image = Image.open(png_path).convert("L")
    width, height = image.size
    band_y = range(max(0, height - 400 - 64 - 80), min(height, height - 400 - 64 + 80), 4)
    band_bright = _max_brightness(image, x_range=range(0, width, 4), y_range=band_y)
    quadrant_bright = _max_brightness(image, x_range=range(0, width // 4, 4), y_range=range(0, height // 4, 4))
    assert band_bright > 200, "字幕安全区应能抽到高亮像素（白字描边）"
    assert quadrant_bright <= 200, "画面左上四分之一不应叠加字幕亮像素"

    status = worker.episode_mix_status("e")
    assert status["subtitle_srt_url"]
    assert "ass_text" not in status["final_edit_report"]["subtitles"]
    assert "cues_timeline" not in status["final_edit_report"]["subtitles"]


def test_second_concat_is_pure_cache_hit(rig):
    from app.db import set_setting

    set_setting("subtitle_burn_in_enabled", "true")
    worker.concatenate_episode("e")

    result = worker.concatenate_episode("e")

    report = result["final_edit"]["subtitles"]
    assert report["cache_hits"] == 2
    assert report["asr_shots"] == 0


def test_disabled_matches_pre_change_demuxer_args_and_writes_no_sidecars(rig, monkeypatch):
    import app.media_exec.concat as concat_mod

    captured: dict = {}
    real_demuxer = concat_mod._run_concat_demuxer

    def spy(*args, **kwargs):
        captured["args"], captured["kwargs"] = args, kwargs
        return real_demuxer(*args, **kwargs)

    monkeypatch.setattr(concat_mod, "_run_concat_demuxer", spy)

    result = worker.concatenate_episode("e")

    assert captured["kwargs"] == {"ass_filter": None}
    assert len(captured["args"]) == 5
    assert captured["args"][3] is True  # uniform_ok：两段同为 1080x1920
    assert captured["args"][4] == 48000  # FINAL_AUDIO_RATE

    final_path = _final_path(config.PROJECTS_DIR)
    assert not final_path.with_name("episode.ass").exists()
    assert not final_path.with_name("episode.srt").exists()
    assert result["final_edit"]["subtitles"] == {"enabled": False}


def test_no_audio_middle_shot_does_not_fail_concat_and_others_get_cues(tmp_path, monkeypatch):
    """2026-09-15 验收实测：无音轨镜头此前会被塞进 ASR jobs，真实 ffmpeg 抽音轨对
    无音轨容器报 ``Output file does not contain any stream``，一镜无音轨即拖垮
    整集合成。修复后该镜完全不提交给引擎，直接判 missing/no_audio；其余镜头
    正常出 cue，成片仍然产出。
    """
    from app import downstream_authority
    from app.db import set_setting

    conn = get_conn()
    project_root = tmp_path / "projects"
    _path1, _path2, _path3 = _seed3(conn, project_root)
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(
        downstream_authority, "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: {"published_storyboard_artifact_id": f"sb:{episode_id}", "release_qualification_hash": "cur"},
    )
    monkeypatch.setattr(downstream_authority, "current_partial_adopted_video_delivery_manifest", _manifest)
    monkeypatch.setattr(engine, "engine_status", lambda: _ok_status())
    tokens = {
        "v1": [("你", 0.5), ("好", 0.7), ("世", 0.9), ("界", 1.1)],
        "v3": [("再", 0.5), ("见", 0.7), ("世", 0.9), ("界", 1.1)],
    }
    received_jobs: dict = {}

    def fake_transcribe(jobs):
        received_jobs.update(jobs)
        return {vid: engine.AsrResult(text="", tokens=tuple(tokens[vid])) for vid in jobs}

    monkeypatch.setattr(engine, "transcribe_media", fake_transcribe)
    set_setting("subtitle_burn_in_enabled", "true")

    result = worker.concatenate_episode("e")

    assert set(received_jobs.keys()) == {"v1", "v3"}, "无音轨镜（v2）不应提交给引擎"
    report = result["final_edit"]["subtitles"]
    assert report["lines_missing"] == 1
    no_audio_missing = [m for m in report["missing"] if m["shot_no"] == 2]
    assert len(no_audio_missing) == 1
    assert no_audio_missing[0]["reason"] == "no_audio"
    assert {c["shot_no"] for c in report["cues_timeline"]} == {1, 3}
    final_path = _final_path(project_root)
    assert final_path.is_file()
    assert final_path.with_name("episode.srt").is_file()
