from app.ingest import ingest_novel, split_chapters


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
