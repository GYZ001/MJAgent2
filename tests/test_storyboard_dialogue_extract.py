"""app.production.storyboard_dialogue_extract 的抽取判据测试。

真实故障背景（2026-09-03，项目「橘座在上」第 1 集）：原文是剧本格式，
「说话人（备注）：台词」这种没有引号的行，旧的纯引号正则一句都抽不到，反而
把动作行里的拟声词/引用词当成了台词。fixture 直接取自该集 chapters.content
片段，覆盖真实故障样例。
"""
from __future__ import annotations

from app.production.storyboard_dialogue_extract import extract_dialogue_targets
from app.production.storyboard_dialogue_ledger import (
    extract_dialogue_targets as legacy_extract_dialogue_targets,
)
from app.source_excerpt import SourceSegment

# 真实原文片段（场 1-2），逐字取自故障集 chapters.content。
_SCRIPT_SCENE = (
    "### 场1-2\n"
    "夜 内 李麦麦出租屋\n"
    "出场人物：李麦麦、橘座\n"
    "△ 逼仄的单间里，橘座正埋头猛炫着泡面碗底的火腿肠碎末。\n"
    "李麦麦（心疼又无奈）：我这破房子连纱窗都没有，明天我去上班，留你一个猫在家，不出半天你就得摔死或者饿死。\n"
    "李麦麦（叹气）：算了，跟我去公司当社畜吧，记住了，到了公司你就是个“没有感情的摆件”，绝对不能出声，知道吗？\n"
    "△ 话音未落，橘座猛地顶开猫包拉链，“嗖”地一下窜上长桌\n"
    "黄总（暴怒）：这就是你们运营部做的方案？一点记忆点都没有！没有流量，我们拿什么忽悠资方？拿什么上市？！\n"
    "李麦麦（os）：完了，我好像把它推进火坑了。"
)

_SPEAKER_NAMES = ["李麦麦", "黄总", "橘座"]


def _seg(text: str) -> SourceSegment:
    return SourceSegment(segment_id="s", text=text, start_offset=0, end_offset=len(text))


def _strip_new_fields(quotes) -> list[dict]:
    """只保留旧函数也有的字段，用于新旧结果对照（新函数额外填了 speaker/note/偏移）。"""
    return [
        q.model_dump(exclude={"speaker", "note", "start_offset", "end_offset"})
        for q in quotes
    ]


# ---------------------------------------------------------------------------
# 剧本格式段：说话人行抽取
# ---------------------------------------------------------------------------

def test_script_segment_extracts_every_speaker_line_and_keeps_embedded_quotes_inline():
    segments = [_seg(_SCRIPT_SCENE)]
    quotes = extract_dialogue_targets(segments, set(), speaker_names=_SPEAKER_NAMES)

    assert [q.speaker for q in quotes] == ["李麦麦", "李麦麦", "黄总", "李麦麦"]
    assert [q.note for q in quotes] == ["心疼又无奈", "叹气", "暴怒", "os"]
    texts = [q.text for q in quotes]
    assert texts == [
        "我这破房子连纱窗都没有，明天我去上班，留你一个猫在家，不出半天你就得摔死或者饿死。",
        "算了，跟我去公司当社畜吧，记住了，到了公司你就是个“没有感情的摆件”，绝对不能出声，知道吗？",
        "这就是你们运营部做的方案？一点记忆点都没有！没有流量，我们拿什么忽悠资方？拿什么上市？！",
        "完了，我好像把它推进火坑了。",
    ]
    # 台词里嵌着的引号短语（“没有感情的摆件”）是这句话的一部分，不再单独成条。
    assert "没有感情的摆件" not in texts
    assert [q.quote_id for q in quotes] == ["Q01", "Q02", "Q03", "Q04"]
    assert all(q.source_segment_index == 1 for q in quotes)


def test_script_segment_action_line_quotes_are_not_counted_as_dialogue():
    """动作行「△ 话音未落……"嗖"地一下窜上长桌」里的引号短语不算台词。"""
    segments = [_seg(_SCRIPT_SCENE)]
    quotes = extract_dialogue_targets(segments, set(), speaker_names=_SPEAKER_NAMES)
    texts = [q.text for q in quotes]
    assert "嗖" not in texts
    assert not any("嗖" in t and len(t) < 5 for t in texts)


def test_speaker_not_in_speaker_names_is_not_counted():
    """行首前缀不在 speaker_names 里（如「出场人物」「路人」）不算说话人行。"""
    segments = [_seg(
        "出场人物：李麦麦、橘座\n"
        "路人（惊讶）：这是什么猫？\n"
        "李麦麦（无奈）：这是我家猫。"
    )]
    quotes = extract_dialogue_targets(segments, set(), speaker_names=["李麦麦"])
    assert [q.text for q in quotes] == ["这是我家猫。"]
    assert [q.speaker for q in quotes] == ["李麦麦"]


def test_alias_in_speaker_names_is_recognized():
    """speaker_names 已由调用方拍平 display_name/aliases；命中别名同样算说话人行。"""
    segments = [_seg("麦麦（笑）：我们走吧。")]
    quotes = extract_dialogue_targets(segments, set(), speaker_names=["李麦麦", "麦麦"])
    assert [q.text for q in quotes] == ["我们走吧。"]
    assert quotes[0].speaker == "麦麦"


# ---------------------------------------------------------------------------
# 段内一条说话人行都没有：沿用旧引号抽取逻辑，逐字一致
# ---------------------------------------------------------------------------

def test_prose_segment_without_speaker_line_falls_back_to_quote_extraction():
    segments = [_seg("甲说：“先走”，乙答：“再等等”。")]
    quotes = extract_dialogue_targets(segments, set(), speaker_names=_SPEAKER_NAMES)
    assert [(q.quote_id, q.text) for q in quotes] == [("Q01", "先走"), ("Q02", "再等等")]
    assert all(q.speaker == "" and q.note == "" for q in quotes)


def test_new_function_with_empty_speaker_names_matches_legacy_function_exactly():
    """混合文档（剧本段 + 小说段）：speaker_names=[] 时新函数处处走引号路径，
    与旧函数在共有字段（quote_id/source_segment_index/text/content_chars）上
    逐字一致。"""
    segments = [
        _seg(_SCRIPT_SCENE),
        _seg("少年站在山顶，喃喃道：“风好大。”远处传来一声“轰”。"),
    ]
    legacy = legacy_extract_dialogue_targets(segments, set())
    new = extract_dialogue_targets(segments, set(), speaker_names=[])
    assert _strip_new_fields(new) == _strip_new_fields(legacy)
    # speaker_names 为空时新函数不识别任何说话人行，四个新字段留默认值。
    assert all(q.speaker == "" and q.note == "" for q in new)
    assert all(q.start_offset == -1 or q.start_offset >= 0 for q in new)


def test_new_function_matches_legacy_on_pure_prose_document():
    segments = [
        _seg("张三说：“你好啊”。"),
        _seg("李四说：「我们走吧」。"),
        _seg("他喊：『小心！』"),
        _seg('她答："好的。"'),
    ]
    legacy = legacy_extract_dialogue_targets(segments, set())
    new = extract_dialogue_targets(segments, set(), speaker_names=["李麦麦"])
    assert _strip_new_fields(new) == _strip_new_fields(legacy)


# ---------------------------------------------------------------------------
# 偏移：segment.text[start:end] == quote 原文
# ---------------------------------------------------------------------------

def test_offsets_recover_the_original_substring_for_speaker_lines():
    segments = [_seg(_SCRIPT_SCENE)]
    quotes = extract_dialogue_targets(segments, set(), speaker_names=_SPEAKER_NAMES)
    for quote in quotes:
        assert quote.start_offset >= 0 and quote.end_offset > quote.start_offset
        assert segments[0].text[quote.start_offset:quote.end_offset] == quote.text


def test_offsets_recover_the_original_substring_for_quote_extraction_path():
    segments = [_seg("甲说：“先走”，乙答：“再等等”。")]
    quotes = extract_dialogue_targets(segments, set(), speaker_names=[])
    for quote in quotes:
        assert segments[0].text[quote.start_offset:quote.end_offset] == quote.text


# ---------------------------------------------------------------------------
# paratext 排除
# ---------------------------------------------------------------------------

def test_paratext_segments_are_excluded_in_script_mode_too():
    segments = [_seg(_SCRIPT_SCENE), _seg("作者的话：“求票求收藏”。")]
    quotes = extract_dialogue_targets(segments, {2}, speaker_names=_SPEAKER_NAMES)
    assert all(q.source_segment_index == 1 for q in quotes)


# ---------------------------------------------------------------------------
# 超长台词仍预拆
# ---------------------------------------------------------------------------

def test_overlong_speaker_line_dialogue_is_pre_split():
    long_clause = "我真的没有骗你，" * 12  # 远超 15 秒口播容量
    segments = [_seg(f"李麦麦（急）：{long_clause}")]
    quotes = extract_dialogue_targets(segments, set(), speaker_names=_SPEAKER_NAMES)
    assert len(quotes) > 1
    assert [q.quote_id for q in quotes] == [f"Q{i:02d}" for i in range(1, len(quotes) + 1)]
    assert all(q.speaker == "李麦麦" and q.note == "急" for q in quotes)
    for quote in quotes:
        assert segments[0].text[quote.start_offset:quote.end_offset] == quote.text
    # 拼回去应还原原始台词（预拆只切分，不改写用词）。
    assert "".join(q.text for q in quotes) == long_clause.strip()


def test_overlong_quote_extraction_path_is_still_pre_split_like_legacy():
    long_clause = "他一路奔跑，" * 12
    segments = [_seg(f"他喊：“{long_clause}”。")]
    legacy = legacy_extract_dialogue_targets(segments, set())
    new = extract_dialogue_targets(segments, set(), speaker_names=[])
    assert len(new) == len(legacy) > 1
    assert [q.text for q in new] == [q.text for q in legacy]
