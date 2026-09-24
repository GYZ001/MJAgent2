"""``app.voice.store`` 的懒建表、current 唯一约束、保留上限删文件、改名/合并
迁移与「生成中断」投影。用真实 ``app.db.get_conn()``（autouse 隔离夹具给的
per-test 干净库），不手搭 schema 副本——``ensure_schema()`` 是懒建表真源。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app import db
from app.voice import store as voice_store

PROJECT_ID = "p1"


def _conn() -> sqlite3.Connection:
    return db.get_conn()


def _insert(character_name: str = "张三", **overrides) -> str:
    conn = _conn()
    kwargs = dict(
        project_id=PROJECT_ID, character_name=character_name, model_id="m1",
        voice_prompt="prompt", preview_text="preview", created_by="tester",
    )
    kwargs.update(overrides)
    voice_id = voice_store.insert_generating(conn, **kwargs)
    conn.commit()
    return voice_id


def test_ensure_tables_on_connection_is_idempotent() -> None:
    conn = _conn()
    voice_store.ensure_tables_on_connection(conn)
    voice_store.ensure_tables_on_connection(conn)  # 第二次不应报错
    conn.execute("SELECT 1 FROM character_voices").fetchall()


def test_insert_generating_and_get_roundtrip() -> None:
    voice_id = _insert()
    row = voice_store.get(_conn(), PROJECT_ID, voice_id)
    assert row["status"] == voice_store.STATUS_GENERATING
    assert row["character_name"] == "张三"
    assert row["anchor_key"] == ""
    assert row["created_by"] == "tester"


def test_get_returns_none_for_missing_id() -> None:
    assert voice_store.get(_conn(), PROJECT_ID, "voice_missing") is None


def test_mark_finished_updates_fields_without_touching_status_elsewhere() -> None:
    voice_id = _insert()
    conn = _conn()
    voice_store.mark_finished(
        conn, PROJECT_ID, voice_id, status=voice_store.STATUS_CANDIDATE,
        provider_voice_id="prov1", audio_path="/tmp/a.wav", clip_path="/tmp/a_clip.wav",
        clip_duration_s=4.5, clip_sha256="abc", check_status="passed", asr_text="你好",
        asr_match=1.0,
    )
    conn.commit()
    row = voice_store.get(conn, PROJECT_ID, voice_id)
    assert row["status"] == voice_store.STATUS_CANDIDATE
    assert row["provider_voice_id"] == "prov1"
    assert row["clip_duration_s"] == 4.5
    assert row["check_status"] == "passed"
    assert row["asr_match"] == 1.0


def test_set_current_demotes_previous_current_to_candidate() -> None:
    conn = _conn()
    first = _insert()
    voice_store.mark_finished(conn, PROJECT_ID, first, status=voice_store.STATUS_CANDIDATE)
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, "张三", first, adopted_by="u1")
    conn.commit()
    assert voice_store.current_for(conn, PROJECT_ID, "张三")["id"] == first

    second = _insert()
    voice_store.mark_finished(conn, PROJECT_ID, second, status=voice_store.STATUS_CANDIDATE)
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, "张三", second, adopted_by="u2")
    conn.commit()

    current = voice_store.current_for(conn, PROJECT_ID, "张三")
    assert current["id"] == second
    assert current["adopted_by"] == "u2"
    demoted = voice_store.get(conn, PROJECT_ID, first)
    assert demoted["status"] == voice_store.STATUS_CANDIDATE  # 降级不是删除


def test_current_unique_index_rejects_two_current_rows_for_same_character() -> None:
    """直接绕过 ``set_current`` 手工 INSERT 两条 current，验证的是索引本身
    （部分唯一索引），不是 ``set_current`` 的应用层逻辑。"""
    conn = _conn()
    voice_store.ensure_schema()
    stamp = db.now()
    conn.execute(
        "INSERT INTO character_voices(id, project_id, character_name, anchor_key, status, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        ("v1", PROJECT_ID, "李四", "", voice_store.STATUS_CURRENT, stamp, stamp),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO character_voices(id, project_id, character_name, anchor_key, status, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            ("v2", PROJECT_ID, "李四", "", voice_store.STATUS_CURRENT, stamp, stamp),
        )
    conn.rollback()


def test_prune_noncurrent_deletes_oldest_beyond_cap_and_their_files(tmp_path) -> None:
    conn = _conn()
    ids: list[str] = []
    for i in range(7):
        voice_id = _insert(character_name="王五")
        audio = tmp_path / f"{voice_id}_full.wav"
        clip = tmp_path / f"{voice_id}_clip.wav"
        audio.write_bytes(b"full")
        clip.write_bytes(b"clip")
        voice_store.mark_finished(
            conn, PROJECT_ID, voice_id, status=voice_store.STATUS_CANDIDATE,
            audio_path=str(audio), clip_path=str(clip),
        )
        conn.commit()
        ids.append(voice_id)
        time.sleep(0.001)  # created_at 用 time.time()，保证严格递增排序

    voice_store.prune_noncurrent(conn, PROJECT_ID, "王五", keep=5)
    conn.commit()

    remaining = {r["id"] for r in voice_store.list_for_character(conn, PROJECT_ID, "王五")}
    assert remaining == set(ids[2:])  # 最旧的两条被删
    for voice_id in ids[:2]:
        assert voice_store.get(conn, PROJECT_ID, voice_id) is None
        assert not (tmp_path / f"{voice_id}_full.wav").exists()
        assert not (tmp_path / f"{voice_id}_clip.wav").exists()
    for voice_id in ids[2:]:
        assert (tmp_path / f"{voice_id}_full.wav").exists()


def test_prune_noncurrent_never_touches_current_row(tmp_path) -> None:
    conn = _conn()
    current_id = _insert(character_name="赵六")
    voice_store.mark_finished(conn, PROJECT_ID, current_id, status=voice_store.STATUS_CANDIDATE)
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, "赵六", current_id, adopted_by="u1")
    conn.commit()
    for _ in range(6):
        _insert(character_name="赵六")

    voice_store.prune_noncurrent(conn, PROJECT_ID, "赵六", keep=5)
    conn.commit()

    assert voice_store.get(conn, PROJECT_ID, current_id) is not None
    assert voice_store.current_for(conn, PROJECT_ID, "赵六")["id"] == current_id


def test_effective_status_projects_stale_generating_row_as_failed() -> None:
    conn = _conn()
    voice_id = _insert(character_name="孙七")
    row = voice_store.get(conn, PROJECT_ID, voice_id)
    fresh_status, fresh_error = voice_store.effective_status(row, now_ts=row["created_at"] + 10)
    assert fresh_status == voice_store.STATUS_GENERATING
    assert fresh_error == ""

    stale_now = row["created_at"] + voice_store.GENERATING_STALE_AFTER_S + 1
    stale_status, stale_error = voice_store.effective_status(row, now_ts=stale_now)
    assert stale_status == voice_store.STATUS_FAILED
    assert "生成中断" in stale_error


def test_migrate_character_voices_renames_when_target_has_no_current() -> None:
    conn = _conn()
    voice_id = _insert(character_name="旧名")
    voice_store.mark_finished(conn, PROJECT_ID, voice_id, status=voice_store.STATUS_CANDIDATE)
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, "旧名", voice_id, adopted_by="u1")
    conn.commit()

    voice_store.migrate_character_voices(conn, PROJECT_ID, "旧名", "新名")
    conn.commit()

    assert voice_store.current_for(conn, PROJECT_ID, "旧名") is None
    moved = voice_store.current_for(conn, PROJECT_ID, "新名")
    assert moved is not None
    assert moved["id"] == voice_id


def test_migrate_character_voices_retires_source_current_when_target_already_has_one() -> None:
    conn = _conn()
    target_current = _insert(character_name="目标")
    voice_store.mark_finished(conn, PROJECT_ID, target_current, status=voice_store.STATUS_CANDIDATE)
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, "目标", target_current, adopted_by="u1")
    conn.commit()

    source_current = _insert(character_name="来源")
    voice_store.mark_finished(conn, PROJECT_ID, source_current, status=voice_store.STATUS_CANDIDATE)
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, "来源", source_current, adopted_by="u2")
    conn.commit()

    voice_store.migrate_character_voices(conn, PROJECT_ID, "来源", "目标")
    conn.commit()

    # 目标的 current 保持不变
    assert voice_store.current_for(conn, PROJECT_ID, "目标")["id"] == target_current
    moved = voice_store.get(conn, PROJECT_ID, source_current)
    assert moved["character_name"] == "目标"
    assert moved["status"] == voice_store.STATUS_RETIRED  # 来源卡的行退场，不是删除


def test_migrate_character_voices_noop_when_names_equal() -> None:
    conn = _conn()
    voice_id = _insert(character_name="不变")
    voice_store.migrate_character_voices(conn, PROJECT_ID, "不变", "不变")
    conn.commit()
    row = voice_store.get(conn, PROJECT_ID, voice_id)
    assert row["character_name"] == "不变"


def test_voice_dir_creates_directory(tmp_path, monkeypatch) -> None:
    from app import config

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    d = voice_store.voice_dir("proj_x")
    assert d.is_dir()
    assert d == tmp_path / "proj_x" / "voices"
