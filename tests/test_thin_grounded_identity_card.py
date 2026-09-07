"""身份已确认的真名、外观经原文核验后变薄：薄卡照建，不因 20 字下限整集失败（第 14 轮 上官修/何洛华）。"""
from __future__ import annotations

from app.portraits.card_verdict import unimportant_verdict_result


def _verdict(**over) -> dict:
    base = {
        "important": False, "model_important": True, "subject_kind": "person", "role": "反派",
        "appearance_canonical": "九十余岁的男性", "dropped_appearance": ["面容阴鸷如鹰"],
        "incomplete_reason": "appearance_canonical 长度 7 字，要求 20~80 字", "reason": "反复出场",
    }
    base.update(over)
    return base


def _call(name, verdict, **over):
    kwargs = dict(require_identity_card=True, card_complete=False, project_id="p1", fragment_signature="sig")
    kwargs.update(over)
    return unimportant_verdict_result(name, verdict, **kwargs)


def test_mapping_stage_accepts_thin_grounded_card() -> None:
    verdict = _verdict()
    assert _call("上官修", verdict, accept_thin_grounded_card=True) is None
    assert verdict["appearance_thin"] is True


def test_nomination_path_still_reports_the_real_reason() -> None:
    """用户提名时人就站在界面前：越界数值必须如实报出来，不得静默建一张薄卡。"""
    result = _call("上官修", _verdict())  # accept_thin_grounded_card 默认关
    assert result["status"] == "error" and "20~80" in result["reason"]


def test_non_identity_path_still_reports_incomplete() -> None:
    result = _call("上官修", _verdict(), require_identity_card=False)
    assert result["status"] == "card_incomplete"


def test_empty_appearance_or_unimportant_is_never_exempted() -> None:
    empty = _call("妖蟒", _verdict(appearance_canonical=""), accept_thin_grounded_card=True)
    assert empty["status"] == "error"
    minor = _call("路人", _verdict(model_important=False), accept_thin_grounded_card=True)
    assert minor["status"] == "error"


def test_presence_override_needs_an_appearance_to_stand() -> None:
    """空外观时「非人→人」的改判不成立：拿它建的卡定不了妆（第 13/15 轮 妖蟒 空外观仍出图）。"""
    from app.portraits.card_verdict import reconsider_verdict_with_presence_evidence

    evidence = {"onscreen_mentions": [{"chapter_index": 23, "quote": "围杀妖蟒，轰鸣阵阵"}]}
    blank = {"subject_kind": "creature", "model_important": True, "appearance_canonical": "", "reason": "视觉锚点"}
    assert reconsider_verdict_with_presence_evidence("妖蟒", blank, evidence) is blank  # 原样返回，不改判

    grounded = {**blank, "appearance_canonical": "十六七岁少年，黑发束起"}
    fixed = reconsider_verdict_with_presence_evidence("孟浩", grounded, evidence)
    assert fixed["subject_kind"] == "person" and fixed["important"] is True
