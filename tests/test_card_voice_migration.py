"""卡改名（``card_rebind``）/合并（``card_merge``）随 ``character_portraits``
同一事务迁移 ``character_voices``（角色固定音色 U1，CLAUDE.md「Retiring
Features」「卡改名/合并联动」同一纪律：上游事实变了，下游状态必须跟着走，
不能留一张挂在旧名字下的孤儿声音）。

用真实 sqlite3 内存库 + ``app.voice.store.ensure_tables_on_connection`` 建表
（不手抄 schema 副本），与 ``tests/test_card_rebind.py``/``test_card_merge.py``
同一套 ``_make_conn`` 风格。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.portraits.card_merge import apply_card_merge_alias
from app.portraits.card_rebind import rebind_character_card
from app.schemas import Bible, Character, CharacterAlias, World
from app.voice import store as voice_store
from tests.conftest import patch_portraits_everywhere


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, "
        "base_portrait_id TEXT, bible_version INTEGER, created_at REAL)"
    )
    voice_store.ensure_tables_on_connection(conn)
    return conn


def _seed_bible(conn: sqlite3.Connection, characters: list[Character]) -> None:
    bible = Bible(world=World(visual_style_canonical="国风"), characters=characters)
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 1)",
        (json.dumps(bible.model_dump(mode="json"), ensure_ascii=False),),
    )
    conn.commit()


def _seed_current_voice(conn: sqlite3.Connection, character_name: str) -> str:
    voice_id = voice_store.insert_generating(
        conn, project_id="p1", character_name=character_name, model_id="m1",
        voice_prompt="prompt", preview_text="preview", created_by="tester",
    )
    voice_store.mark_finished(conn, "p1", voice_id, status=voice_store.STATUS_CANDIDATE)
    voice_store.set_current(conn, "p1", character_name, voice_id, adopted_by="tester")
    conn.commit()
    return voice_id


def test_rebind_character_card_migrates_voice_rows_to_new_name(monkeypatch) -> None:
    conn = _make_conn()
    _seed_bible(conn, [Character(name="许师姐", role="重要配角", appearance_canonical="青衣女子")])
    voice_id = _seed_current_voice(conn, "许师姐")
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)

    ok = asyncio.run(rebind_character_card("p1", "许师姐", "许清"))

    assert ok is True
    assert voice_store.current_for(conn, "p1", "许师姐") is None
    moved = voice_store.current_for(conn, "p1", "许清")
    assert moved is not None and moved["id"] == voice_id


def test_apply_card_merge_alias_promotes_source_current_when_target_has_none() -> None:
    conn = _make_conn()
    _seed_bible(conn, [Character(name="井田", role="主角", appearance_canonical="少女")])
    voice_id = _seed_current_voice(conn, "妈妈")  # "妈妈" 尚未建卡，只是恰好留过一条声音行
    alias = CharacterAlias(
        text="妈妈", name_kind="referential", evidence_chapter_index=1,
        evidence_quote="以后别叫我井田了，叫我妈妈吧", is_exclusive=True,
    ).model_dump(mode="json")

    ok = apply_card_merge_alias(conn, "p1", "井田", alias)

    assert ok is True
    assert voice_store.current_for(conn, "p1", "妈妈") is None
    moved = voice_store.current_for(conn, "p1", "井田")
    assert moved is not None and moved["id"] == voice_id


def test_apply_card_merge_alias_retires_source_current_when_target_already_has_one() -> None:
    conn = _make_conn()
    _seed_bible(conn, [Character(name="井田", role="主角", appearance_canonical="少女")])
    target_voice_id = _seed_current_voice(conn, "井田")
    source_voice_id = _seed_current_voice(conn, "妈妈")
    alias = CharacterAlias(
        text="妈妈", name_kind="referential", evidence_chapter_index=1,
        evidence_quote="以后别叫我井田了，叫我妈妈吧", is_exclusive=True,
    ).model_dump(mode="json")

    ok = apply_card_merge_alias(conn, "p1", "井田", alias)

    assert ok is True
    current = voice_store.current_for(conn, "p1", "井田")
    assert current is not None and current["id"] == target_voice_id  # 目标的 current 不受影响
    retired = voice_store.get(conn, "p1", source_voice_id)
    assert retired["character_name"] == "井田"
    assert retired["status"] == voice_store.STATUS_RETIRED  # 来源卡的行退场，不是删除，文件仍在
