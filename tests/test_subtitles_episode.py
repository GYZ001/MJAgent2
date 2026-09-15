"""``app.subtitles.episode``：整集字幕编排（缓存命中/未命中、旧镜头台词回退、
无音轨镜头、引擎不就绪快速失败、边车文件幂等与哈希核验）。

DB 隔离沿用 ``tests/conftest.py`` 的自动重置 fixture（同 ``tests/test_subtitles_
store.py``）：``get_conn()`` 每个测试拿到独立克隆的 SQLite，不需要手搭 sqlite3
连接（对照 ``tests/test_episode_partial_concat.py`` 建 episodes/shots/shot_versions
的形状，字段等价，只是省掉了那份手写 schema 的样板）。
"""
from __future__ import annotations

import json

import pytest

from app.db import get_conn
from app.subtitles import episode, engine


def _segment(text: str) -> dict:
    return {
        "storyboard_pack_segment": {
            "dialogue": [
                {"utterance_id": "U01", "line": text, "speaker_identity_id": "char:zhang", "delivery_kind": "spoken_dialogue"},
            ],
            "resources": {"characters": [{"identity_id": "char:zhang", "display_name": "张三"}]},
        }
    }


def _seed(conn, *, shot1_video: str = "/tmp/v1.mp4", shot2_video: str = "/tmp/v2.mp4") -> None:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) VALUES('e','p',1,'E','confirmed',0)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_contract_json) VALUES('s1','e',1,5,?)",
        (json.dumps(_segment("你好世界"), ensure_ascii=False),),
    )
    legacy = [{"speaker": "李四", "line": "再见世界", "delivery": "spoken_dialogue"}]
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,dialogues) VALUES('s2','e',2,5,?)",
        (json.dumps(legacy, ensure_ascii=False),),
    )
    for shot_id, version_id, path in (("s1", "v1", shot1_video), ("s2", "v2", shot2_video)):
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at) "
            "VALUES(?,?,1,'p','k','succeeded',?,0)",
            (version_id, shot_id, path),
        )
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.commit()


_MANIFEST = [
    {"shot_no": 1, "adopted_version_id": "v1", "file_sha256": "sha1"},
    {"shot_no": 2, "adopted_version_id": "v2", "file_sha256": "sha2"},
]
_PIECE_SPECS = [(1, "/tmp/v1.mp4", 1.0), (2, "/tmp/v2.mp4", 1.0)]
_PROBE = {1: {"video_duration_s": 5.0, "has_audio": True}, 2: {"video_duration_s": 5.0, "has_audio": True}}


def _ok_status() -> engine.EngineStatus:
    from pathlib import Path

    return engine.EngineStatus(ok=True, model_dir=Path("/fake"), model_id="test-model", problems=(), engine_id="sherpa-onnx/test")


def _fake_transcribe(calls: list):
    tokens_by_version = {
        "v1": [("你", 0.1), ("好", 0.3), ("世", 0.5), ("界", 0.7)],
        "v2": [("再", 0.1), ("见", 0.3), ("世", 0.5), ("界", 0.7)],
    }

    def fake(jobs):
        calls.append(dict(jobs))
        return {vid: engine.AsrResult(text="", tokens=tuple(tokens_by_version[vid])) for vid in jobs}

    return fake


def test_first_call_hits_engine_and_caches_second_call_is_pure_cache(monkeypatch):
    conn = get_conn()
    _seed(conn)
    calls: list = []
    monkeypatch.setattr(engine, "engine_status", _ok_status)
    monkeypatch.setattr(engine, "transcribe_media", _fake_transcribe(calls))

    plan1 = episode.prepare_episode_subtitles(
        conn, episode_id="e", piece_specs=_PIECE_SPECS, probe_by_shot=_PROBE, manifest_items=_MANIFEST,
    )
    assert len(calls) == 1
    assert plan1.asr_shots == 2
    assert plan1.cache_hits == 0
    assert plan1.shots[1].alignment.lines[0].status == "aligned"
    assert plan1.shots[2].alignment.lines[0].status == "aligned"
    assert plan1.shots[1].cues, "对齐命中的台词应当出 cue"

    monkeypatch.setattr(
        engine, "transcribe_media",
        lambda jobs: (_ for _ in ()).throw(AssertionError("缓存命中不应再调用引擎")),
    )
    plan2 = episode.prepare_episode_subtitles(
        conn, episode_id="e", piece_specs=_PIECE_SPECS, probe_by_shot=_PROBE, manifest_items=_MANIFEST,
    )
    assert plan2.asr_shots == 0
    assert plan2.cache_hits == 2


def test_no_audio_shot_is_all_missing_with_no_audio_reason(monkeypatch):
    """无音轨镜头必须整个不进 ASR jobs——真实 ffmpeg 对无音轨视频抽音轨会报
    ``Output file does not contain any stream``（2026-09-15 验收实测），不是靠
    mock 才侥幸走通；这里用 jobs 快照证明它压根没被提交给引擎，不是「提交了但
    引擎不 care」。"""
    conn = get_conn()
    _seed(conn)
    calls: list = []
    monkeypatch.setattr(engine, "engine_status", _ok_status)
    monkeypatch.setattr(engine, "transcribe_media", _fake_transcribe(calls))
    probe = {1: {"video_duration_s": 5.0, "has_audio": False}, 2: {"video_duration_s": 5.0, "has_audio": True}}

    plan = episode.prepare_episode_subtitles(
        conn, episode_id="e", piece_specs=_PIECE_SPECS, probe_by_shot=probe, manifest_items=_MANIFEST,
    )

    assert len(calls) == 1
    assert set(calls[0].keys()) == {"v2"}, "无音轨镜（v1）不应出现在提交给引擎的 jobs 里"
    line = plan.shots[1].alignment.lines[0]
    assert line.status == "missing"
    assert line.reason == "no_audio"
    assert plan.shots[1].cues == ()
    assert plan.shots[2].alignment.lines[0].status == "aligned"


def test_engine_not_ok_raises_before_any_ffmpeg_work(monkeypatch):
    conn = get_conn()
    _seed(conn)
    monkeypatch.setattr(
        engine, "engine_status",
        lambda: engine.EngineStatus(ok=False, model_dir=__import__("pathlib").Path("/x"), model_id="m", problems=("缺模型文件",)),
    )
    monkeypatch.setattr(
        engine, "transcribe_media",
        lambda jobs: (_ for _ in ()).throw(AssertionError("引擎不就绪不应该走到 ASR 调用")),
    )

    with pytest.raises(engine.AsrEngineError, match="缺模型文件"):
        episode.prepare_episode_subtitles(
            conn, episode_id="e", piece_specs=_PIECE_SPECS, probe_by_shot=_PROBE, manifest_items=_MANIFEST,
        )


def test_legacy_dialogues_column_yields_line_specs():
    conn = get_conn()
    _seed(conn)
    row = conn.execute("SELECT * FROM shots WHERE id='s2'").fetchone()

    specs = episode.shot_line_specs(row)

    assert len(specs) == 1
    assert specs[0].utterance_id == "L01"
    assert specs[0].text == "再见世界"
    assert specs[0].speaker == "李四"


def test_segment_dialogue_resolves_display_name_over_identity_id():
    conn = get_conn()
    _seed(conn)
    row = conn.execute("SELECT * FROM shots WHERE id='s1'").fetchone()

    specs = episode.shot_line_specs(row)

    assert specs[0].utterance_id == "U01"
    assert specs[0].speaker == "张三"


def test_materialize_sidecars_is_idempotent_by_content_hash(tmp_path, monkeypatch):
    final_path = tmp_path / "episode.mp4"
    report = {
        "subtitles": {
            "enabled": True, "ass_text": "dummy-ass",
            "cues_timeline": [{"shot_no": 1, "utterance_id": "U01", "text": "你好", "start_s": 0.0, "end_s": 1.0}],
        }
    }
    write_calls: list = []
    real_atomic_write_text = episode.atomic_write_text

    def counting_write(path, text):
        write_calls.append(path)
        real_atomic_write_text(path, text)

    monkeypatch.setattr(episode, "atomic_write_text", counting_write)

    episode.materialize_sidecars(final_path, report)
    assert len(write_calls) == 2  # srt + ass
    assert (tmp_path / "episode.srt").is_file()
    assert (tmp_path / "episode.ass").is_file()

    episode.materialize_sidecars(final_path, report)
    assert len(write_calls) == 2, "内容 sha256 相同不应重写"


def test_materialize_sidecars_removes_stale_sidecars_when_disabled(tmp_path):
    final_path = tmp_path / "episode.mp4"
    episode.materialize_sidecars(final_path, {"subtitles": {"enabled": True, "ass_text": "x", "cues_timeline": []}})
    episode.materialize_sidecars(final_path, {"subtitles": {"enabled": False}})
    assert not (tmp_path / "episode.srt").is_file()
    assert not (tmp_path / "episode.ass").is_file()


def test_srt_sidecar_url_none_when_sha_mismatch(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    final_path = tmp_path / "episode.mp4"
    (tmp_path / "episode.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n\n", encoding="utf-8")

    assert episode.srt_sidecar_url(final_path, {"subtitles": {"enabled": True, "srt_sha256": "not-the-real-hash"}}) is None
    assert episode.srt_sidecar_url(final_path, {"subtitles": {"enabled": False}}) is None
    assert episode.srt_sidecar_url(final_path, None) is None
