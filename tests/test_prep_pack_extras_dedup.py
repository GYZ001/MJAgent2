"""准备包里同一实体被登记两次（有卡角色 + 群演）时并回角色本体。

真实数据取自 2026-09-16 龙猫出爪第 4 集的 asset_manifest：``bible:龙猫`` 带
portrait_id 在 characters，同时以 label「龙猫」「小龙」在 functional_extras，
下游因此拿到两套互相矛盾的身份（完整案情见 app.production.prep_pack.extras_dedup）。
"""
from __future__ import annotations

from app.production.prep_pack.extras_dedup import merge_card_backed_extras

_EP4 = {
    "characters": [
        {"identity_id": "bible:阿凯", "display_name": "阿凯", "portrait_id": "portrait_06c0",
         "aliases": ["小陈", "他"], "segment_indexes": [3, 4, 5]},
        {"identity_id": "bible:龙猫", "display_name": "龙猫", "portrait_id": "portrait_70ce",
         "aliases": ["小龙", "一只猫"], "segment_indexes": [2, 4, 5, 6, 7, 8, 10, 11, 14]},
        {"identity_id": "bible:虫虫", "display_name": "虫虫", "portrait_id": None,
         "aliases": [], "segment_indexes": [6]},
    ],
    "functional_extras": [
        {"label": "龙猫", "segment_indexes": [9, 12], "visual_entity_id": "entity:d193"},
        {"label": "招聘经理", "segment_indexes": [13], "visual_entity_id": "entity:a524"},
        {"label": "小龙", "segment_indexes": [4, 5], "visual_entity_id": "entity:810e"},
    ],
}


def _manifest() -> dict:
    import copy
    return copy.deepcopy(_EP4)


def test_same_name_extra_is_merged_into_the_card_backed_character() -> None:
    manifest = merge_card_backed_extras(_manifest())
    assert [extra["label"] for extra in manifest["functional_extras"]] == ["招聘经理", "小龙"]


def test_merge_keeps_segment_indexes_the_character_entry_was_missing() -> None:
    """群演那一份带着角色条目没有的段号（第 9、12 段），直接删会丢掉在场事实。"""
    manifest = merge_card_backed_extras(_manifest())
    longmao = next(c for c in manifest["characters"] if c["identity_id"] == "bible:龙猫")
    assert longmao["segment_indexes"] == [2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14]


def test_alias_only_match_is_left_registered() -> None:
    """判据只取正名/display_name：别名池混着代词，按别名合并会编造归属。"""
    manifest = merge_card_backed_extras(_manifest())
    assert any(extra["label"] == "小龙" for extra in manifest["functional_extras"])


def test_pronoun_label_shared_by_several_characters_is_never_merged() -> None:
    """第 3、5 集实测形态：label「你」「我」同时是多个角色的别名，一条都不能并。"""
    manifest = merge_card_backed_extras({
        "characters": [
            {"identity_id": "bible:周晚", "display_name": "周晚", "portrait_id": "p1",
             "aliases": ["我", "你"], "segment_indexes": [1]},
            {"identity_id": "bible:小李", "display_name": "小李", "portrait_id": "p2",
             "aliases": ["你"], "segment_indexes": [2]},
        ],
        "functional_extras": [{"label": "你", "segment_indexes": [7, 8]}],
    })
    assert [extra["label"] for extra in manifest["functional_extras"]] == ["你"]
    assert [c["segment_indexes"] for c in manifest["characters"]] == [[1], [2]]


def test_extra_matching_a_character_without_portrait_is_left_alone() -> None:
    """没参考图的角色不构成「两套身份」的冲突——它本来就要靠文字描述长相。"""
    manifest = merge_card_backed_extras({
        "characters": [{"identity_id": "bible:虫虫", "display_name": "虫虫",
                        "portrait_id": None, "segment_indexes": [6]}],
        "functional_extras": [{"label": "虫虫", "segment_indexes": [9]}],
    })
    assert [extra["label"] for extra in manifest["functional_extras"]] == ["虫虫"]


def test_manifest_without_extras_is_returned_unchanged() -> None:
    manifest = merge_card_backed_extras({"characters": [], "functional_extras": []})
    assert manifest == {"characters": [], "functional_extras": []}
