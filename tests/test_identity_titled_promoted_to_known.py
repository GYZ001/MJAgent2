"""n/f 里的「登记名＋关系称谓」确定性改选那条 K 决议（2026-09-05 第 5 集「韩宗师兄」）。"""
from __future__ import annotations

from app.portraits.identity_promotions import promote_titled_known
from app.portraits.identity_schemas import CurrentIdentityCandidateResponse


def test_new_name_with_title_of_registered_person_becomes_known_decision() -> None:
    known = {
        "K:E002:han": {"evidence_ref": "E002", "source_label": "韩宗", "canonical_name": "韩宗",
                       "allowed_kinds": ["onscreen", "mentioned"]},
        "K:E001:meng": {"evidence_ref": "E001", "source_label": "孟浩", "canonical_name": "孟浩"},
    }
    response = CurrentIdentityCandidateResponse.model_validate({
        "k": [{"decision_id": "K:E001:meng", "kind": "onscreen"}],
        "n": [{"evidence_ref": "E002", "identity_label": "韩宗师兄", "name_kind": "honorific", "kind": "onscreen"}],
        "f": [{"evidence_ref": "E002", "source_label": "王腾飞师兄", "functional_identity_key": "王腾飞师兄",
               "kind": "mentioned"}],
    })
    promoted = promote_titled_known(response, known)
    assert [item.decision_id for item in promoted.k] == ["K:E001:meng", "K:E002:han"]
    assert promoted.n == []
    # 「王腾飞」没有登记：f 原样保留交给校验，不猜。
    assert [item.source_label for item in promoted.f] == ["王腾飞师兄"]


def test_allowed_kinds_are_respected_and_unrelated_labels_untouched() -> None:
    known = {"K:E003:han": {"evidence_ref": "E003", "source_label": "韩宗", "canonical_name": "韩宗",
                            "allowed_kinds": ["mentioned"]}}
    response = CurrentIdentityCandidateResponse.model_validate({
        "k": [], "n": [],
        "f": [{"evidence_ref": "E003", "source_label": "韩宗长老", "functional_identity_key": "韩宗长老", "kind": "onscreen"},
              {"evidence_ref": "E003", "source_label": "门卫", "functional_identity_key": "门卫", "kind": "onscreen"}],
    })
    promoted = promote_titled_known(response, known)
    assert [(item.decision_id, item.kind) for item in promoted.k] == [("K:E003:han", "mentioned")]
    assert [item.source_label for item in promoted.f] == ["门卫"]


def test_stem_matching_two_registered_names_is_left_alone() -> None:
    known = {
        "K:E001:a": {"evidence_ref": "E001", "source_label": "大汉", "canonical_name": "大汉"},
        "K:E001:b": {"evidence_ref": "E001", "source_label": "大汉", "canonical_name": "大汉"},
    }
    response = CurrentIdentityCandidateResponse.model_validate({
        "k": [], "n": [],
        "f": [{"evidence_ref": "E001", "source_label": "大汉兄", "functional_identity_key": "大汉兄", "kind": "onscreen"}],
    })
    # 两条 K 同名（同一个人两条证据）→ 唯一名，可以归；不同名才不归。这里同名，归到第一条。
    promoted = promote_titled_known(response, known)
    assert [item.decision_id for item in promoted.k] == ["K:E001:a"]
    response2 = CurrentIdentityCandidateResponse.model_validate({
        "k": [], "n": [], "f": [{"evidence_ref": "E001", "source_label": "门卫兄", "functional_identity_key": "x", "kind": "onscreen"}],
    })
    assert promote_titled_known(response2, known) is response2
