"""「登记名＋关系称谓」归到同一张卡（2026-09-05 我欲封天第 5 集「韩宗师兄」）。

人物谱登记的是「韩宗」，原文写「韩宗师兄」：旧判据只做精确相等，查无此人就当群演建了
第二张没有定妆照的卡，分镜台显示「无定妆照」、视频拿不到脸。新判据仍是精确相等——去掉
闭集里的一个称谓后缀后，剩余部分逐字等于登记名/别名；不做子串，不猜姓氏。
"""
from __future__ import annotations

from app.portraits.card_owner import resolve_card_owner, strip_relational_title
from app.schemas import Bible, Character, CharacterAlias, World


def _bible(*names: str, aliases: dict[str, list[str]] | None = None) -> Bible:
    return Bible(
        world=World(visual_style_canonical="国风"),
        characters=[
            Character(
                name=name, role="配角", appearance_canonical="外观",
                aliases=[
                    CharacterAlias(text=a, name_kind="referential", is_exclusive=False,
                                   evidence_chapter_index=1, evidence_quote=f"有人唤他{a}")
                    for a in (aliases or {}).get(name, [])
                ],
            )
            for name in names
        ],
    )


def test_strip_relational_title_only_when_a_real_stem_remains() -> None:
    assert strip_relational_title("韩宗师兄") == "韩宗"
    assert strip_relational_title("欧阳大长老") == "欧阳大"  # 剩余部分再由登记名精确核验
    assert strip_relational_title("王兄") is None  # 单字剩余不算
    assert strip_relational_title("孟浩") is None
    assert strip_relational_title("") is None


def test_registered_name_plus_title_is_owned_by_that_character() -> None:
    bible = _bible("韩宗", "孟浩")
    assert resolve_card_owner(bible, "韩宗师兄") == ("owner", "韩宗")
    assert resolve_card_owner(bible, "韩宗") == ("owner", "韩宗")


def test_exact_match_takes_precedence_over_title_stripping() -> None:
    # 人物谱本来就把「许师姐」登记成正名：精确命中优先，不会被拆成「许」。
    bible = _bible("许师姐", "许清")
    assert resolve_card_owner(bible, "许师姐") == ("owner", "许师姐")


def test_unregistered_stem_stays_none_and_alias_stem_counts() -> None:
    bible = _bible("上官修", "李富贵", aliases={"李富贵": ["小胖子"]})
    assert resolve_card_owner(bible, "上官师叔") == ("none", "")  # 「上官」查无此人，不猜
    assert resolve_card_owner(bible, "小胖子哥") == ("owner", "李富贵")  # 别名＋称谓同样归卡


def test_title_stem_hitting_two_characters_fails_closed() -> None:
    bible = _bible("大汉", "曹阳", aliases={"曹阳": ["大汉"]})
    assert resolve_card_owner(bible, "大汉兄")[0] == "conflict"
