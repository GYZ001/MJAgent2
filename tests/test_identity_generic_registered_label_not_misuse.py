"""已登记称谓在本批被模型当作通称的头名词使用时，functional 声明不算「冒用」（ERR-20260902-ba850c）。

《神墓》第二章：人物谱里有一张第一集建的、名字就叫「老人」的卡（守墓老人）。本章「老人」既指
老妇人（E001/E002）、又指「镇上一位老人」、还指「守墓老人」；模型如实给出三条 functional
（镇上一位老人 F4 / 老人 F5 / 守墓老人 F6），却因「老人」是已登记称谓被判冒用、整集硬失败。
判据仍取自本批模型自己的产出：一个已登记称谓若在本批别的 functional 称谓里作为组成部分出现
（「守墓老人」含「老人」），它在这批证据里就是通称，不指向唯一身份；真名（刘备）不会这样出现。
"""
from __future__ import annotations

from app import portraits

_EVIDENCE = {
    "E001": {"text": "走进来一个半百老妇人，老人一脸和蔼之色。"},
    "E002": {"text": "最后镇上一位老人对他道：孩子，这个问题很多人都想知道。"},
    "E003": {"text": "辰南想起了守墓老人，老人拄着拐杖的样子历历在目。"},
}


def _project(f_items: list[dict], reserved: set[str]):
    response = portraits.CurrentIdentityCandidateResponse.model_validate({"k": [], "n": [], "f": f_items})
    return portraits._project_current_identity_response(
        response,
        evidence_by_ref=_EVIDENCE,
        known_decisions={},
        reserved_authority_labels=reserved,
        group_scope="current-1",
        existing_functional_routes=set(),
    )


def test_registered_generic_label_used_as_head_noun_is_not_misuse() -> None:
    f_items = [
        {"evidence_ref": "E002", "source_label": "镇上一位老人", "functional_identity_key": "F4", "kind": "mentioned"},
        {"evidence_ref": "E003", "source_label": "老人", "functional_identity_key": "F5", "kind": "mentioned"},
        {"evidence_ref": "E003", "source_label": "守墓老人", "functional_identity_key": "F6", "kind": "mentioned"},
    ]
    projected, errors = _project(f_items, reserved={"老人"})
    assert not any("冒用" in message for message in errors), errors
    assert {item["source_label"] for item in projected} == {"镇上一位老人", "老人", "守墓老人"}


def test_registered_real_name_declared_functional_is_still_misuse() -> None:
    evidence = dict(_EVIDENCE)
    evidence["E001"] = {"text": "刘备引军前来助战。"}
    response = portraits.CurrentIdentityCandidateResponse.model_validate({"k": [], "n": [], "f": [
        {"evidence_ref": "E001", "source_label": "刘备", "functional_identity_key": "F1", "kind": "onscreen"},
    ]})
    _projected, errors = portraits._project_current_identity_response(
        response, evidence_by_ref=evidence, known_decisions={}, reserved_authority_labels={"刘备"},
        group_scope="current-1", existing_functional_routes=set(),
    )
    assert any("current functional 不得冒用已登记身份称谓：刘备" in message for message in errors), errors
