"""泛称不登记成全局别名（2026-09-06 第 11 轮人物谱：许师姐 别名['女子']、王腾飞 别名['这青年']）。"""
from __future__ import annotations

from app.portraits.card_aliases import alias_is_specific, new_card_aliases
from app.portraits import card_merge


def test_generic_descriptors_and_demonstratives_are_not_specific() -> None:
    for label in ("女子", "这青年", "那女子", "一个少年", "老者", "此人", "青年男子"):
        assert not alias_is_specific(label), label
    for label in ("小胖子", "赵师兄", "许师姐", "虎爷", "妈妈", "李富贵", "自称虎爷的大汉"):
        assert alias_is_specific(label), label


def test_new_card_aliases_skip_generic_labels_even_with_cooccurrence() -> None:
    chapters = {1: "许师姐已经到了凝气第七层。那女子神情冷漠。外宗弟子私下都叫许师姐冷面仙子。"}
    aliases = new_card_aliases("许师姐", ["女子", "冷面仙子"], chapters)
    assert [a["text"] for a in aliases] == ["冷面仙子"]  # 「女子」同章共现也不登记


def test_apply_card_merge_alias_treats_generic_label_as_done_without_writing(monkeypatch) -> None:
    import json, sqlite3
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)")
    bible = {"world": {"visual_style_canonical": "国漫"}, "characters": [{"name": "许师姐", "role": "重要配角", "appearance_canonical": "银袍女修", "aliases": []}]}
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 1)", (json.dumps(bible, ensure_ascii=False),))
    conn.commit()
    monkeypatch.setattr(card_merge, "_has_column", lambda *a: False)
    monkeypatch.setattr(card_merge, "_has_table", lambda *a: False)
    ok = card_merge.apply_card_merge_alias(conn, "p1", "许师姐", {"text": "女子", "name_kind": "referential", "evidence_chapter_index": 1, "evidence_quote": "那女子…许师姐", "is_exclusive": False})
    assert ok is True
    assert json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"][0]["aliases"] == []
