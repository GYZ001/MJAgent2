"""真名揭示后卡名要跟上：「陈师兄」→「陈凡」、「许师姐」→「许清」（2026-09-06 第 15 轮人物谱核查）。"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.portraits import card_rebind
from app.schemas import Bible, Character, CharacterAlias, World

_APPEARANCE = "二十余岁青年，束发，身着靠山宗内门弟子制式灰白衣袍，身形挺拔"


def _conn(*characters: Character) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER, bible_artifact_id TEXT)")
    conn.execute("CREATE TABLE character_portraits(project_id TEXT, character_name TEXT, ep_start INTEGER)")
    conn.execute("INSERT INTO character_portraits VALUES('p1','陈师兄',1)")
    bible = Bible(world=World(visual_style_canonical="国漫"), characters=list(characters))
    conn.execute(
        "INSERT INTO projects VALUES('p1', ?, 1, NULL)",
        (json.dumps(bible.model_dump(), ensure_ascii=False),),
    )
    conn.commit()
    return conn


def _patch(monkeypatch, conn) -> None:
    monkeypatch.setattr(card_rebind, "get_conn", lambda: conn)

    async def fake_lock(_project_id):
        class _Lock:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        return _Lock()

    monkeypatch.setattr(card_rebind, "_bible_lock", fake_lock)


def _names(conn) -> list[str]:
    data = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    return [c["name"] for c in data["characters"]]


def _call(**over) -> bool:
    kwargs = dict(authority_id="bible:陈凡", canonical_name="陈凡", source_labels=["陈师兄"])
    kwargs.update(over)
    return asyncio.run(card_rebind.rebind_for_revealed_true_name("p1", **kwargs))


def test_label_card_is_renamed_to_the_revealed_true_name(monkeypatch) -> None:
    conn = _conn(Character(name="陈师兄", role="重要配角", appearance_canonical=_APPEARANCE))
    _patch(monkeypatch, conn)
    assert _call() is True
    assert _names(conn) == ["陈凡"]


def test_true_name_already_owned_is_left_to_the_merge_channel(monkeypatch) -> None:
    conn = _conn(
        Character(name="陈师兄", role="重要配角", appearance_canonical=_APPEARANCE),
        Character(name="陈凡", role="重要配角", appearance_canonical=_APPEARANCE),
    )
    _patch(monkeypatch, conn)
    assert _call() is False  # 两张卡是不是同一个人由归并通道判，这里不猜
    assert _names(conn) == ["陈师兄", "陈凡"]


def test_label_that_is_only_an_alias_is_not_renamed(monkeypatch) -> None:
    conn = _conn(Character(
        name="孟浩", role="主角", appearance_canonical=_APPEARANCE,
        aliases=[CharacterAlias(text="陈师兄", name_kind="referential", evidence_chapter_index=1, evidence_quote="陈师兄", is_exclusive=False)],
    ))
    _patch(monkeypatch, conn)
    assert _call() is False  # 只是别名不是卡名：改名会把别人的卡改掉
    assert _names(conn) == ["孟浩"]


def test_legacy_shape_authority_carries_the_old_label(monkeypatch) -> None:
    conn = _conn(Character(name="曹某", role="反派", appearance_canonical=_APPEARANCE))
    _patch(monkeypatch, conn)
    assert _call(authority_id="bible:曹某", canonical_name="曹阳", source_labels=[]) is True
    assert _names(conn) == ["曹阳"]


def test_no_revealed_name_or_no_matching_card_does_nothing(monkeypatch) -> None:
    conn = _conn(Character(name="陈师兄", role="重要配角", appearance_canonical=_APPEARANCE))
    _patch(monkeypatch, conn)
    assert _call(canonical_name="") is False
    assert _call(source_labels=["某个没建卡的称谓"]) is False
    assert _names(conn) == ["陈师兄"]
