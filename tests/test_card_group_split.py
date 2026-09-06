"""多人合称拆成成员卡：合称不建卡、成员各建各的、合称成为共享别名（2026-09-06「两个老者」）。"""
from __future__ import annotations

import asyncio

from app import portraits
from app.portraits import card_group_split as split
from app.portraits.card_verdict import non_character_or_unimportant_result
from app.schemas import Bible, Character, World
from tests.conftest import patch_portraits_everywhere
from tests.test_card_merge import _characters, _make_conn, _patch_everything, _seed_project

_CH5 = (
    "在这靠山宗四周的山峰上，有两个老者盘膝坐在山顶，正皱着眉头的看着山下外宗广场的一幕幕。\n\n"
    "那之前叹息的两个老者中穿着灰色长袍的高大老者，双眼猛地明亮，带着强烈的赞赏，哈哈大笑起来。\n\n"
    "旁边的清瘦老者衣着素净古朴，只是微微一笑，并未说话。"
)


def test_verbatim_member_labels_keep_only_labels_present_in_fragments() -> None:
    members = [{"source_label": "高大老者"}, {"source_label": "清瘦老者"}, {"source_label": "灰袍老者"}, {"source_label": ""}, "高大老者"]
    assert split.verbatim_member_labels(members, _CH5) == ["高大老者", "清瘦老者"]
    assert split.verbatim_member_labels(None, _CH5) == []


def test_group_verdict_with_members_is_not_cached_as_non_person(monkeypatch) -> None:
    written: dict[str, str] = {}
    from app.portraits import card_verdict
    monkeypatch.setattr(card_verdict, "set_setting", lambda k, v: written.__setitem__(k, v))
    verdict = {"subject_kind": "group", "members": ["高大老者", "清瘦老者"], "reason": "两位老者各有戏份"}
    result = non_character_or_unimportant_result(
        "两个老者", verdict, require_identity_card=False, card_complete=False, project_id="p1", cache_signature="sig",
    )
    assert result == {"status": "skipped_group", "name": "两个老者", "members": ["高大老者", "清瘦老者"], "reason": "两位老者各有戏份"}
    assert written == {}  # 没进负缓存：成员卡还要建
    verdict_no_members = {"subject_kind": "group", "members": [], "reason": "原文不区分成员"}
    assert non_character_or_unimportant_result(
        "众弟子", verdict_no_members, require_identity_card=False, card_complete=False, project_id="p1", cache_signature="sig",
    )["status"] == "skipped_not_person"


def test_group_label_builds_member_cards_and_registers_group_alias(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, Bible(
        world=World(visual_style_canonical="国漫"),
        characters=[Character(name="孟浩", role="主角", appearance_canonical="十六七岁少年，黑发高束")],
    ), _CH5)
    _patch_everything(monkeypatch, conn)
    assessed: list[str] = []

    async def fake_assess(name, fragments, *, style, known_names, ep_label, **_kwargs):
        assessed.append(name)
        if name == "两个老者":
            return {"subject_kind": "group", "important": True, "reason": "两位老者各有戏份", "role": "重要配角",
                    "appearance_canonical": "", "personality": "", "speech_style": "", "relationships": [],
                    "members": ["高大老者", "清瘦老者"]}
        looks = {"高大老者": "须发皆白的高大老者，身着灰色宽袖长袍，身形挺拔如松，面容威严",
                 "清瘦老者": "须发皆白的清瘦老者，衣着素净古朴的灰白长衫，神态沉稳温和"}
        return {"subject_kind": "person", "important": True, "reason": "山顶观战有台词", "role": "重要配角",
                "appearance_canonical": looks[name], "personality": "沉稳", "speech_style": "简洁", "relationships": []}

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)
    result = asyncio.run(portraits.ensure_character_card("p1", "两个老者", 21, generate_portrait=False))
    assert result["status"] == "split_group" and result["added_members"] == ["高大老者", "清瘦老者"]
    assert assessed == ["两个老者", "高大老者", "清瘦老者"]
    names = [c["name"] for c in _characters(conn)]
    assert names == ["孟浩", "高大老者", "清瘦老者"]  # 合称本身没有建卡
    aliases = {c["name"]: {a["text"] for a in c["aliases"]} for c in _characters(conn)}
    assert "两个老者" not in aliases["高大老者"]  # 合称是泛称：不登记成全局别名（card_aliases.alias_is_specific）


def test_presence_forced_person_without_appearance_is_card_incomplete_not_a_card() -> None:
    """第 13 轮：妖蟒/两色雾魂被画面在场证据改判为人、important 恢复，但外观为空，仍落了卡还出了图。"""
    from app.portraits.card_verdict import unimportant_verdict_result
    verdict = {"important": True, "model_important": True, "reason": "有戏份", "incomplete_reason": "appearance_canonical 长度 0 字"}
    result = unimportant_verdict_result("妖蟒", verdict, require_identity_card=False, card_complete=False,
                                        project_id="p1", fragment_signature="sig")
    assert result["status"] == "card_incomplete"
