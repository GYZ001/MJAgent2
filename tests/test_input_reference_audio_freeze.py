"""``app.media_exec.input_reference_audio.freeze_segment_reference_audios``：
只在本次调用真正完成一次全新的参考图冻结时才计算并冻结音频清单，已经冻结
过的版本原样跳过（U3 派单第 4 条的核心判据）。

用真实 ``app.db.get_conn()``（autouse 隔离夹具）落库 projects/episodes/
shots/shot_versions/character_voices，不打桩数据层；``current_capability_
snapshot`` 走真实 Seedance 静态快照（无网络）。
"""
from __future__ import annotations

import json
import sqlite3

from app import db
from app.media_exec.input_reference_audio import freeze_segment_reference_audios
from app.voice import segment_refs, store as voice_store

PROJECT_ID = "proj_1"
EPISODE_ID = "ep_1"


def _conn():
    return db.get_conn()


def _seed_chain(shot_id: str, *, segment: dict | None) -> tuple[dict, dict, object]:
    """落一条 project/episode/shot/shot_version 全链路，返回 (job, version, shot_row)。"""
    conn = _conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json, bible_version) "
        "VALUES(?,?,?,?,?,?)",
        (PROJECT_ID, "测试项目", "created", db.now(), "{}", 1),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,1,'confirmed',?)",
        (EPISODE_ID, PROJECT_ID, db.now()),
    )
    contract = json.dumps({"storyboard_pack_segment": segment}, ensure_ascii=False) if segment is not None else None
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, "
        "scene_setting, action_desc, shot_contract_json) VALUES(?,?,1,15,'中景','固定','客厅','说话',?)",
        (shot_id, EPISODE_ID, contract),
    )
    version_id = "ver_1"
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, created_at) "
        "VALUES(?,?,1,'原始提示词 --ratio 9:16 --dur 15','key1',?)",
        (version_id, shot_id, db.now()),
    )
    conn.commit()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    return {"project_id": PROJECT_ID}, {"id": version_id}, shot_row


def _adopt_voice(character_name: str) -> None:
    conn = _conn()
    voice_id = voice_store.insert_generating(
        conn, project_id=PROJECT_ID, character_name=character_name, model_id="m1",
        voice_prompt="p", preview_text="t", created_by="tester",
    )
    conn.commit()
    voice_store.mark_finished(
        conn, PROJECT_ID, voice_id, status=voice_store.STATUS_CANDIDATE,
        clip_path=f"/tmp/{voice_id}_clip.wav", clip_sha256="sha-1", clip_duration_s=4.0,
    )
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, character_name, voice_id, adopted_by="tester")
    conn.commit()


def _segment_with_speaker(character_name: str) -> dict:
    return {
        "dialogue": [{"speaker_identity_id": f"bible:{character_name}", "line": "你好"}],
        "resources": {"characters": [{"identity_id": f"bible:{character_name}"}]},
    }


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "true" if key == segment_refs.SETTING_KEY_ENABLED else "")


def test_disabled_setting_is_a_pure_noop(monkeypatch) -> None:
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "")
    job, version, shot_row = _seed_chain("shot_1", segment=_segment_with_speaker("张三"))
    meta = {"video_input_manifest_frozen": True, "reference_images": [{"id": "img1"}]}

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "原文 --ratio 9:16 --dur 15", already_frozen=False,
    )

    assert result == "原文 --ratio 9:16 --dur 15"
    assert "reference_audios" not in meta
    assert "reference_audio_skips" not in meta


def test_already_frozen_is_a_pure_noop_even_when_enabled(monkeypatch) -> None:
    """老版本重试的核心场景：即便这次 meta 显示已冻结，只要 already_frozen=True
    （调用前就已冻结），一律不重新计算——防止请求形状漂移触发供应商幂等冲突。"""
    _enable(monkeypatch)
    _adopt_voice("张三")
    job, version, shot_row = _seed_chain("shot_2", segment=_segment_with_speaker("张三"))
    meta = {"video_input_manifest_frozen": True, "reference_images": [{"id": "img1"}]}

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "原文 --ratio 9:16 --dur 15", already_frozen=True,
    )

    assert result == "原文 --ratio 9:16 --dur 15"
    assert "reference_audios" not in meta


def test_not_yet_frozen_this_call_is_a_noop(monkeypatch) -> None:
    """本次调用还没有真正完成参考图冻结（video_input_manifest_frozen 仍为假），
    即使开关打开也不计算——避免对等待中的半成品 meta 抢先写入音频键。"""
    _enable(monkeypatch)
    _adopt_voice("张三")
    job, version, shot_row = _seed_chain("shot_3", segment=_segment_with_speaker("张三"))
    meta = {"video_input_manifest_frozen": False, "reference_images": []}

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "原文 --ratio 9:16 --dur 15", already_frozen=False,
    )

    assert result == "原文 --ratio 9:16 --dur 15"
    assert "reference_audios" not in meta


def test_legacy_shot_without_storyboard_pack_segment_is_a_noop(monkeypatch) -> None:
    _enable(monkeypatch)
    job, version, shot_row = _seed_chain("shot_4", segment=None)
    meta = {"video_input_manifest_frozen": True, "reference_images": [{"id": "img1"}]}

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "原文 --ratio 9:16 --dur 15", already_frozen=False,
    )

    assert result == "原文 --ratio 9:16 --dur 15"
    assert "reference_audios" not in meta


def test_fresh_freeze_writes_meta_appends_note_and_persists(monkeypatch) -> None:
    _enable(monkeypatch)
    _adopt_voice("张三")
    job, version, shot_row = _seed_chain("shot_5", segment=_segment_with_speaker("张三"))
    meta = {
        "video_input_manifest_frozen": True, "reference_images": [{"id": "img1"}],
        "aspect_ratio": "9:16",
    }
    prompt_text = "镜头1：@张三 说话。 --ratio 9:16 --dur 15"

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, prompt_text, already_frozen=False,
    )

    assert meta["reference_audios"] == [{
        "index": 1, "character_name": "张三", "anchor_key": "", "voice_id": meta["reference_audios"][0]["voice_id"],
        "clip_path": f"/tmp/{meta['reference_audios'][0]['voice_id']}_clip.wav", "clip_sha256": "sha-1",
        "clip_duration_s": 4.0,
    }]
    assert meta["reference_audio_skips"] == []
    assert "声音参考：" in result
    assert "@音频1 是张三的声音" in result
    assert result.endswith("--ratio 9:16 --dur 15")

    # 第二条独立连接重读盘上数据，不借同一个 get_conn() 缓存连接读自己刚写
    # 的东西——证明的是真提交，不是内存里的假象（CLAUDE.md「验证要有独立
    # 观察点」）。
    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    try:
        persisted = verify_conn.execute(
            "SELECT image_inputs, prompt_text FROM shot_versions WHERE id=?", (version["id"],),
        ).fetchone()
    finally:
        verify_conn.close()
    persisted_meta = json.loads(persisted["image_inputs"])
    assert persisted_meta["reference_audios"][0]["character_name"] == "张三"
    assert persisted["prompt_text"] == result
