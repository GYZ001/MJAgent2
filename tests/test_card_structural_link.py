"""同姓氏键的结构候选让「许师姐」连回「许姓女子」的卡（2026-09-06 第 6 轮同一人两张卡）。

正例：第 5 章「可想到那许师姐……」与下一段「许姓女子迟疑了一下」相距在共现窗口内，模型选中
候选、钉证段在候选一侧，登记别名不建卡。反例：两个同姓的不同人，模型选了候选但全书没有窗口
共现证据，机械核验拒绝，照常建卡。
"""
from __future__ import annotations

import asyncio
import json

from app import portraits
from app.harness import model_gateway
from app.portraits import card_structural_link as link
from app.schemas import Bible, Character, CharacterAlias, World
from tests.conftest import patch_portraits_everywhere
from tests.test_card_merge import _characters, _fake_assess_important_person, _make_conn, _patch_everything, _seed_project


def test_surname_key_recognises_title_and_marker_forms_only() -> None:
    assert link.surname_key("许师姐") == "许"
    assert link.surname_key("许姓女子") == "许"
    assert link.surname_key("王腾飞师兄") is None  # 完整人名归 strip_relational_title
    assert link.surname_key("小胖子") is None and link.surname_key("孟浩") is None


def test_structural_candidates_match_same_surname_key_and_leading_char() -> None:
    bible = Bible(world=World(visual_style_canonical="国漫"), characters=[
        Character(name="许姓女子", role="重要配角", appearance_canonical="银袍女修"),
        Character(name="许青", role="重要配角", appearance_canonical="青衣"),
        Character(name="孟浩", role="主角", appearance_canonical="少年", aliases=[
            CharacterAlias(text="许家小子", name_kind="referential", evidence_chapter_index=1, evidence_quote="许家小子", is_exclusive=False),
        ]),
        Character(name="上官修", role="重要配角", appearance_canonical="金袍"),
    ])
    assert link.structural_candidates(bible, "许师姐") == ["许姓女子", "许青", "孟浩"]
    assert link.structural_candidates(bible, "上官师叔") == []  # 复姓不进这条结构规则
    assert link.structural_candidates(bible, "孟浩") == []


_CH5 = (
    "众人心中对孟浩迁怒，可想到那许师姐，一个个顿时犹豫中打消了念头。\n\n"
    "许姓女子迟疑了一下，觉得以自己内门弟子的身份，白拿一个刚入门的外宗弟子的好处，有些过意不去。\n\n"
    "远处的山峰上，两个老者盘膝而坐。"
)


def test_title_form_label_merges_into_surname_marker_card(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, Bible(
        world=World(visual_style_canonical="国漫"),
        characters=[Character(name="许姓女子", role="重要配角", appearance_canonical="穿着银袍的年轻女修，黑发高束")],
    ), _CH5)
    _patch_everything(monkeypatch, conn)
    patch_portraits_everywhere(monkeypatch, "assess_new_character", _fake_assess_important_person)
    seen: dict = {}

    async def fake_chat_structured(messages, *, model_type, **kwargs):
        seen["candidates"] = kwargs["call_meta"]["candidates"]
        seen["prompt"] = messages[0]["content"]
        return model_type(selected_candidate="许姓女子", supporting_entry_index=2, supporting_quote="")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    result = asyncio.run(portraits.ensure_character_card("p1", "许师姐", 21, generate_portrait=False))
    assert result == {"status": "exists", "name": "许姓女子"}
    assert seen["candidates"] == ["许姓女子"] and "许姓女子迟疑了一下" in seen["prompt"]  # 候选一侧的段进了卷宗
    characters = _characters(conn)
    assert len(characters) == 1
    alias = {a["text"]: a for a in characters[0]["aliases"]}["许师姐"]
    assert "许师姐" in alias["evidence_quote"] and "许姓女子" in alias["evidence_quote"]


def test_same_surname_without_cooccurrence_is_not_merged(monkeypatch) -> None:
    conn = _make_conn()
    far_apart = "许姓女子在内门闭关多年。\n\n" + "山外的风声。\n\n" * 40 + "外宗弟子提起那许师姐，语气敬畏。"
    _seed_project(conn, Bible(
        world=World(visual_style_canonical="国漫"),
        characters=[Character(name="许姓女子", role="重要配角", appearance_canonical="穿着银袍的年轻女修，黑发高束")],
    ), far_apart)
    _patch_everything(monkeypatch, conn)
    patch_portraits_everywhere(monkeypatch, "assess_new_character", _fake_assess_important_person)

    async def fake_chat_structured(messages, *, model_type, **kwargs):
        return model_type(selected_candidate="许姓女子", supporting_entry_index=1, supporting_quote="")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    result = asyncio.run(portraits.ensure_character_card("p1", "许师姐", 21, generate_portrait=False))
    assert result["status"] == "added" and result["name"] == "许师姐"
    assert [c["name"] for c in _characters(conn)] == ["许姓女子", "许师姐"]
    assert json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"][0]["aliases"] == []
