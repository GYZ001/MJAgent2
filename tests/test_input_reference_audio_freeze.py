"""``app.media_exec.input_reference_audio.freeze_segment_reference_audios``：
只在本次调用真正完成一次全新的参考图冻结时才计算并冻结音频清单，已经冻结
过的版本原样跳过（U3 派单第 4 条的核心判据）。

用真实 ``app.db.get_conn()``（autouse 隔离夹具）落库 projects/episodes/
shots/shot_versions/character_voices，不打桩数据层；``current_capability_
snapshot`` 走真实 Seedance 静态快照（无网络）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import json
import sqlite3

import pytest

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


def _real_clip(name: str) -> str:
    """参考片段必须真实存在才会被传入（文件缺失的声音按「声音文件缺失」跳过）。"""
    path = Path(tempfile.gettempdir()) / "mj_voice_test_clips" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    return str(path)


def _adopt_voice(character_name: str) -> None:
    conn = _conn()
    voice_id = voice_store.insert_generating(
        conn, project_id=PROJECT_ID, character_name=character_name, model_id="m1",
        voice_prompt="p", preview_text="t", created_by="tester",
    )
    conn.commit()
    voice_store.mark_finished(
        conn, PROJECT_ID, voice_id, status=voice_store.STATUS_CANDIDATE,
        clip_path=_real_clip(f"{voice_id}_clip.wav"), clip_sha256="sha-1", clip_duration_s=4.0,
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


# 真实流水线的形态（2026-09-24 线上测试集实测）：分镜台 2.x 的参考图在入队时已从素材库
# 拼好，运行时走快路径，meta 里只有 reference_manifest_frozen、没有
# video_input_manifest_frozen。旧判据挂在后者上，线上 9 段一段都没带上声音。
_REAL_META = {"reference_manifest_frozen": True, "reference_images": [{"id": "img1"}], "aspect_ratio": "9:16"}
_OP = "video-create-ver_test"


def test_disabled_setting_is_a_pure_noop(monkeypatch) -> None:
    # 默认开启；只有显式关闭才是纯 no-op
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "false" if key == segment_refs.SETTING_KEY_ENABLED else "")
    job, version, shot_row = _seed_chain("shot_1", segment=_segment_with_speaker("张三"))
    meta = dict(_REAL_META)

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "原文 --ratio 9:16 --dur 15", operation_id=_OP,
    )

    assert result == "原文 --ratio 9:16 --dur 15"
    assert "reference_audios" not in meta
    assert "reference_audio_skips" not in meta


def test_already_decided_version_is_reused_verbatim(monkeypatch) -> None:
    """重试的核心场景：本版本已决定过声音清单（哪怕决定为空），一律原样复用，不重新解析。"""
    _enable(monkeypatch)
    _adopt_voice("张三")
    job, version, shot_row = _seed_chain("shot_2", segment=_segment_with_speaker("张三"))
    meta = {**_REAL_META, "reference_audios": [], "reference_audio_skips": [{"character_name": "张三", "reason": "未配置声音"}]}

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "原文 --ratio 9:16 --dur 15", operation_id=_OP,
    )

    assert result == "原文 --ratio 9:16 --dur 15"
    assert meta["reference_audios"] == []


def test_version_already_submitted_to_provider_is_not_changed(monkeypatch) -> None:
    """上线前就发过创建请求的在途版本：不再补上声音，避免请求形状变化被幂等核对拦截。"""
    from app import hiagent

    _enable(monkeypatch)
    _adopt_voice("张三")
    monkeypatch.setattr(hiagent, "_latest_provider_operation_request", lambda kind, op: {"content": []})
    job, version, shot_row = _seed_chain("shot_3", segment=_segment_with_speaker("张三"))
    meta = dict(_REAL_META)

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "原文 --ratio 9:16 --dur 15", operation_id=_OP,
    )

    assert result == "原文 --ratio 9:16 --dur 15"
    assert "reference_audios" not in meta


def test_legacy_shot_without_storyboard_pack_segment_is_a_noop(monkeypatch) -> None:
    _enable(monkeypatch)
    job, version, shot_row = _seed_chain("shot_4", segment=None)
    meta = dict(_REAL_META)

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "原文 --ratio 9:16 --dur 15", operation_id=_OP,
    )

    assert result == "原文 --ratio 9:16 --dur 15"
    assert "reference_audios" not in meta


def test_fresh_freeze_writes_meta_appends_note_and_persists(monkeypatch) -> None:
    _enable(monkeypatch)
    _adopt_voice("张三")
    job, version, shot_row = _seed_chain("shot_5", segment=_segment_with_speaker("张三"))
    meta = dict(_REAL_META)  # 真实 2.x 形态：没有 video_input_manifest_frozen 也必须决定声音
    prompt_text = "镜头1：@张三 说话。 --ratio 9:16 --dur 15"

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, prompt_text, operation_id=_OP,
    )

    assert meta["reference_audios"] == [{
        "index": 1, "character_name": "张三", "anchor_key": "", "voice_id": meta["reference_audios"][0]["voice_id"],
        "clip_path": _real_clip(f"{meta['reference_audios'][0]['voice_id']}_clip.wav"), "clip_sha256": "sha-1",
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


def test_fresh_freeze_with_stale_persisted_snapshot(monkeypatch) -> None:
    """复现线上（2026-09-24 测试集）：库里存着上线前的旧能力快照（没有参考音频字段，读出来
    默认「不支持」），不能再挡住声音参考。"""
    from app import hiagent
    from app.video_plan.capability_snapshot import save_capability_snapshot
    from app.video_plan.models import ProviderVideoCapabilitySnapshot
    from app.video_providers import resolve as resolve_adapter

    _enable(monkeypatch)
    _adopt_voice("张三")
    provider = hiagent.active_provider("video")
    model = hiagent.active_model("video", provider)
    fresh = resolve_adapter(provider).capability_snapshot(provider=provider, model=model)
    old = ProviderVideoCapabilitySnapshot.model_validate({**fresh.model_dump(), "id": "cap_old_without_audio"})
    conn = _conn()
    save_capability_snapshot(old, conn=conn)
    # 模拟上线前的库行：capabilities_json 里根本没有参考音频这三个键
    row = conn.execute(
        "SELECT capabilities_json FROM provider_video_capability_snapshots WHERE id=?", ("cap_old_without_audio",),
    ).fetchone()
    legacy_json = {k: v for k, v in json.loads(row["capabilities_json"]).items()
                   if k not in ("supports_reference_audio", "max_reference_audios", "max_reference_audio_total_s")}
    conn.execute(
        "UPDATE provider_video_capability_snapshots SET capabilities_json=? WHERE id=?",
        (json.dumps(legacy_json), "cap_old_without_audio"),
    )
    conn.commit()
    job, version, shot_row = _seed_chain("shot_6", segment=_segment_with_speaker("张三"))
    meta = dict(_REAL_META)

    result = freeze_segment_reference_audios(
        _conn(), job, version, shot_row, meta, "镜头1：@张三 说话。 --ratio 9:16 --dur 15", operation_id=_OP,
    )

    assert [a["character_name"] for a in meta["reference_audios"]] == ["张三"]
    assert meta["reference_audio_skips"] == []
    assert "声音参考：" in result


def test_missing_conn_fails_loudly_instead_of_deep_attribute_error() -> None:
    """线上 run_job 曾把给续租心跳子任务用、生产上恒为 None 的 operation_conn 传进来，
    在函数深处炸成 AttributeError；现在连接必须显式给出，缺了在入口就报 TypeError。"""
    job, version, shot_row = _seed_chain("shot_7", segment=_segment_with_speaker("张三"))
    with pytest.raises(TypeError):
        freeze_segment_reference_audios(
            None, job, version, shot_row, dict(_REAL_META), "原文 --ratio 9:16 --dur 15", operation_id=_OP,
        )

