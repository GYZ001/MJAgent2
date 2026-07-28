import pytest

from app.ingest import clean_text, ingest_novel, split_chapters


def test_ingest_drops_adjacent_title_only_duplicate() -> None:
    text = """
第一千六百二十二章 双帝之战！（上）
正文 第一千六百二十二章 双帝之战！（上）

第一千六百二十二章双帝之战
魂天帝踏着血云现身，萧炎迎空而起。两人气息碰撞，中州众人抬头仰望。
魂天帝冷声宣战，萧炎催动异火回应，双帝之战由此爆发。
血云与异火反复撞击，古元和烛坤率领联盟众人后撤，守住最后一道防线。
萧炎踏火逼近魂天帝，两位斗帝连续交锋，余波掀翻山岭，也让所有人看清决战已经无法回避。
魂天帝再次催动血海，萧炎则召回漫天异火，在众人的注视下凝成足以决定天地命运的火焰之阵。

第一千六百二十三章 双帝之战！（下）
异火在天空汇聚成阵，战斗继续向最后的决胜推进。
"""

    result = ingest_novel(text.encode("utf-8"))

    assert result["deduplicated_stub_chapters"] == 1
    assert result["chapter_count"] == 2
    assert "魂天帝踏着血云现身" in result["chapters"][0]["content"]
    assert [chapter["idx"] for chapter in result["chapters"]] == [1, 2]


def test_split_does_not_merge_different_titles_with_reused_ordinal() -> None:
    text = """
第一千六百二十三章 双帝之战！（下）
异火在天空汇聚成阵，萧炎与魂天帝继续交锋，天地不断崩裂。

第一千六百二十三章 结束，也是开始
大战落幕，中州开始重建，幸存者们重新踏上各自的道路。
"""

    chapters = split_chapters(text)

    assert len(chapters) == 2
    assert chapters[0]["title"] != chapters[1]["title"]


def test_ingest_preserves_single_chapter_heading_with_utf8_bom() -> None:
    raw = "\ufeff第一章 初遇\n雨夜里，林舟在旧车站第一次见到沈青。".encode("utf-8")

    result = ingest_novel(raw)

    assert result["chapter_count"] == 1
    assert result["auto_split"] is False
    assert result["chapters"][0]["title"] == "第一章 初遇"


def test_ingest_supports_utf16_txt_with_bom() -> None:
    raw = "第一章 醒来\n他睁开眼，看见窗外的雪。".encode("utf-16")

    result = ingest_novel(raw)

    assert result["chapter_count"] == 1
    assert result["chapters"][0]["title"] == "第一章 醒来"


def test_ingest_rejects_binary_payload() -> None:
    with pytest.raises(ValueError, match="二进制内容"):
        ingest_novel(b"\x00\x01\x02\x03not-a-novel")


def test_ingest_recognizes_special_and_bracketed_chapter_headings() -> None:
    text = """
序章 雨夜
林舟第一次来到旧车站。

【第一章 初遇】
他在站台遇见沈青。

番外一 重逢
多年后，两人再次回到这里。
"""

    result = ingest_novel(text.encode())

    assert result["auto_split"] is False
    assert [chapter["title"] for chapter in result["chapters"]] == [
        "序章 雨夜",
        "第一章 初遇",
        "番外一 重逢",
    ]


def test_clean_text_keeps_story_wechat_but_removes_social_promotion() -> None:
    cleaned, removed = clean_text(
        "他打开微信，看见母亲发来的消息。\n"
        "关注微信公众号领取最新章节福利\n"
        "她放下手机，推门走进雨里。"
    )

    assert removed == 1
    assert "他打开微信" in cleaned
    assert "她放下手机" in cleaned
    assert "微信公众号" not in cleaned
