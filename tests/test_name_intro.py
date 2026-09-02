"""古典自我介绍形态的确定性解析（app.portraits.name_intro）。"""
from app.portraits.name_intro import find_name_introductions, intro_owner_of


def test_full_form_with_courtesy_and_later_rename() -> None:
    text = "其人曰：“吾姓关，名羽，字长生，后改云长，河东解良人也。”"
    intros = find_name_introductions(text)
    assert len(intros) == 1
    intro = intros[0]
    assert intro.full_name == "关羽"
    assert intro.alt_names == ("长生", "云长")
    assert intro.quote == "姓关，名羽，字长生，后改云长"
    assert intro_owner_of("长生", intros).full_name == "关羽"
    assert intro_owner_of("云长", intros).full_name == "关羽"
    assert intro_owner_of("关羽", intros) is None  # 全名本身不是别名


def test_courtesy_only_form_and_compound_surname() -> None:
    intros = find_name_introductions("那人道：刘备，字玄德。军师姓诸葛，名亮，字孔明。")
    by_full = {i.full_name: i for i in intros}
    assert by_full["刘备"].alt_names == ("玄德",)
    assert by_full["诸葛亮"].alt_names == ("孔明",)


def test_grammar_words_before_full_name_are_not_swallowed() -> None:
    # 「叫徐庶，字元直」：全名前的「叫」不是姓名的一部分——不得解析出「叫徐庶」。
    intros = find_name_introductions("此人叫徐庶，字元直。")
    assert [i.full_name for i in intros] == ["徐庶"]
    assert intro_owner_of("元直", intros).full_name == "徐庶"


def test_no_introduction_returns_nothing() -> None:
    assert find_name_introductions("老人拄着一条拐杖颤颤巍巍向他走来。") == []
    assert intro_owner_of("老人", []) is None


def test_conflicting_owners_fail_closed() -> None:
    intros = find_name_introductions("甲，字子明。乙，字子明。")
    assert intro_owner_of("子明", intros) is None
