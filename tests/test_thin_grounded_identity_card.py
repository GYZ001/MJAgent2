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


def test_identity_path_accepts_thin_grounded_card(monkeypatch) -> None:
    verdict = _verdict()
    assert unimportant_verdict_result("上官修", verdict, require_identity_card=True, card_complete=False, project_id="p1", fragment_signature="sig") is None
    assert verdict["appearance_thin"] is True


def test_non_identity_path_still_reports_incomplete(monkeypatch) -> None:
    result = unimportant_verdict_result("上官修", _verdict(), require_identity_card=False, card_complete=False, project_id="p1", fragment_signature="sig")
    assert result["status"] == "card_incomplete"


def test_identity_path_with_empty_appearance_or_unimportant_still_errors(monkeypatch) -> None:
    empty = unimportant_verdict_result("妖蟒", _verdict(appearance_canonical=""), require_identity_card=True, card_complete=False, project_id="p1", fragment_signature="sig")
    assert empty["status"] == "error"
    minor = unimportant_verdict_result("路人", _verdict(model_important=False), require_identity_card=True, card_complete=False, project_id="p1", fragment_signature="sig")
    assert minor["status"] == "error"
