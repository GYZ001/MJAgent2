"""写锁内提交前的并发复核：快照后人物谱多了称谓就先归并（2026-09-06 并发映射台建重卡）。"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.portraits import card_commit
from app.schemas import Bible, Character, World


def _conn_with_bible(characters: list[Character]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)")
    bible = Bible(world=World(visual_style_canonical="国漫"), characters=characters)
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 3)",
                 (json.dumps(bible.model_dump(), ensure_ascii=False),))
    conn.commit()
    return conn


_CARD = {"name": "许师姐", "role": "重要配角", "appearance_canonical": "二十许岁女修士，高束乌黑长发，身着银纹镶边长袍",
         "personality": "冷静", "speech_style": "简洁", "relationships": [], "source_evidence": [], "aliases": []}


def _names(conn) -> list[str]:
    return [c["name"] for c in json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"]]


def test_no_new_labels_since_snapshot_appends_without_model_call(monkeypatch) -> None:
    conn = _conn_with_bible([Character(name="孟浩", role="主角", appearance_canonical="少年")])

    async def boom(*_a, **_k):
        raise AssertionError("快照后没有新称谓，不该发起归并裁决")

    monkeypatch.setattr(card_commit, "resolve_card_merge_target", boom)
    ok = asyncio.run(card_commit.append_character_or_merge(conn, "p1", dict(_CARD), snapshot_labels={"孟浩"}))
    assert ok is True and _names(conn) == ["孟浩", "许师姐"]


def test_new_card_since_snapshot_that_is_the_same_person_becomes_an_alias(monkeypatch) -> None:
    conn = _conn_with_bible([
        Character(name="孟浩", role="主角", appearance_canonical="少年"),
        Character(name="许姓女子", role="重要配角", appearance_canonical="银袍女修"),  # 别的并发映射台刚建的
    ])
    probes: list[str] = []

    async def fake_merge(conn_, project_id, label, bible):
        probes.append(label)
        return "许姓女子", {"text": label, "name_kind": "referential", "evidence_chapter_index": 5,
                        "evidence_quote": "可想到那许师姐……许姓女子迟疑了一下", "is_exclusive": False}

    monkeypatch.setattr(card_commit, "resolve_card_merge_target", fake_merge)
    ok = asyncio.run(card_commit.append_character_or_merge(conn, "p1", dict(_CARD), snapshot_labels={"孟浩"}))
    assert ok is False and probes == ["许师姐"]
    data = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert [c["name"] for c in data["characters"]] == ["孟浩", "许姓女子"]
    assert [a["text"] for a in data["characters"][1]["aliases"]] == ["许师姐"]


def test_new_card_since_snapshot_that_is_someone_else_still_appends(monkeypatch) -> None:
    conn = _conn_with_bible([
        Character(name="孟浩", role="主角", appearance_canonical="少年"),
        Character(name="许青", role="重要配角", appearance_canonical="青衣少年"),
    ])

    async def no_merge(*_a, **_k):
        return None

    monkeypatch.setattr(card_commit, "resolve_card_merge_target", no_merge)
    ok = asyncio.run(card_commit.append_character_or_merge(conn, "p1", dict(_CARD), snapshot_labels={"孟浩"}))
    assert ok is True and _names(conn) == ["孟浩", "许青", "许师姐"]
