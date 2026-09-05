"""2026-09-05 两条成片根因的守卫：说话人确定性归属、结尾段台词剥除、画外音可追溯与旁白归属。"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_dialogue_attribution import (
    NARRATOR,
    attribute_prose_speaker,
    dialogue_speaker_errors,
    manifest_name_to_identity,
    strip_tail_dialogue,
)
from app.production.storyboard_dialogue_extract import extract_dialogue_targets
from app.production.storyboard_dialogue_ledger import required_dialogue_for_segments
from app.source_excerpt import SourceSegment

NAMES = ["孟浩", "王腾飞", "小胖子", "许师姐"]


def _seg(text: str, no: int = 1) -> SourceSegment:
    return SourceSegment(segment_id=f"seg{no}", text=text, start_offset=0, end_offset=len(text))


# ---------------------------------------------------------------- 归属
def test_speaker_after_closing_quote_wins_over_earlier_name():
    text = "王腾飞站在高台上。“早晚有一日，我定要手刃此人！”孟浩想起对方与自己之间的仇恨。"
    start = text.index("早晚"); end = text.index("！”") + 1
    assert attribute_prose_speaker(text, start, end, NAMES) == "孟浩"


def test_speaker_before_quote_used_when_nothing_follows():
    text = "孟浩皱着眉头沉吟起来：“若真是瓶颈，就需要丹药。”"
    start = text.index("若真"); end = text.index("药。”") + 2
    assert attribute_prose_speaker(text, start, end, NAMES) == "孟浩"


def test_nearest_name_without_utterance_evidence_is_not_a_speaker():
    """第 3 集真实形态：『……觉得虎爷声音大？』孟浩翻了个白眼——孟浩只是听者。没有发声证据就留空。"""
    text = "“小个屁声，觉得虎爷声音大？”孟浩翻了个白眼，没有理会。"
    names = NAMES + ["虎爷"]
    start = text.index("小个"); end = text.index("大？”") + 2
    assert attribute_prose_speaker(text, start, end, names) == ""
    text2 = "“小个屁声，觉得虎爷声音大？”虎爷哼了一声。"
    assert attribute_prose_speaker(text2, start, end, names) == "虎爷"


def test_post_window_stops_at_next_quote_so_opponent_is_not_borrowed():
    text = "“你来做什么？”“我不是说过别再来了。”王腾飞冷冷道。"
    start = text.index("你来"); end = text.index("么？”") + 2
    # 引号后紧跟另一句引号，窗口截断，退回引号前——前面没有名字 → 空，不猜
    assert attribute_prose_speaker(text, start, end, NAMES) == ""


def test_prose_extraction_carries_speaker_into_required_dialogue():
    text = "“按照许师姐的说法，为何我的瓶颈提前了？”孟浩盘膝坐在洞府内，皱着眉头沉吟起来。"
    quotes = extract_dialogue_targets([_seg(text)], set(), speaker_names=NAMES)
    assert quotes and quotes[0].speaker == "孟浩"
    kept = [SimpleNamespace(quote_id=quotes[0].quote_id, segment_no=1)]
    required = required_dialogue_for_segments(kept, quotes)
    assert required[1][0]["speaker"] == "孟浩"


# ---------------------------------------------------------------- 结尾段剥台词
def test_tail_dialogue_is_stripped_in_all_real_shapes():
    body = "镜头2（约4-9秒）：解说员嘴唇开合喊出：\"他三十五岁了——他不该还能这样跑！\"，场景为赛场。\n"
    tails = [
        "全片贯穿：环境音是赛场的欢呼声；配乐是激昂的管弦乐；台词：\"他三十五岁了——他不该还能这样跑！\"；风格为国漫。",
        "全片贯穿：音频包含奔跑声，对话清晰可闻：“救命！”“曹某面前，谁也救不了你。”；风格为国漫。",
        "全片贯穿：音频为刘备说出的\"你如此害民，本该取你性命！\"、轻微的衣料摩擦声；风格为国漫。",
        "全片贯穿：音频：大雨声、雷声，画外音（辰南）：“天地虽大，何处是我家？” “雨馨……”；风格为国漫。",
    ]
    for tail in tails:
        cleaned, removed = strip_tail_dialogue(body + tail)
        assert removed, tail
        assert "全片贯穿" in cleaned
        assert cleaned.startswith(body), "镜头段落里的台词必须原样保留"
        assert not any(q in cleaned.split("全片贯穿")[-1] for q in ("「", "“", "\"", "『")), cleaned


def test_prompt_without_tail_quotes_is_untouched():
    text = "镜头1：@孟浩 嘴唇开合说出：「走吧。」\n全片贯穿：环境音是风声；配乐是古琴；风格为国漫；约束：面部一致。"
    assert strip_tail_dialogue(text) == (text, [])


# ---------------------------------------------------------------- 第二阶段校验
class _Line:
    def __init__(self, speaker, line, delivery="spoken_dialogue"):
        self.speaker_identity_id, self.line, self.delivery = speaker, line, delivery


def _draft(lines, prompt="镜头1：……\n全片贯穿：环境音；配乐；风格；约束。"):
    return SimpleNamespace(dialogue=lines, prompt_text=prompt)


PAYLOAD = {"asset_manifest": {"characters": [
    {"identity_id": "bible:孟浩", "display_name": "孟浩", "aliases": ["孟师弟"]},
    {"identity_id": "bible:王腾飞", "display_name": "王腾飞", "aliases": []},
]}}


def test_required_line_speaker_mismatch_is_a_precise_error():
    n2i = manifest_name_to_identity(PAYLOAD)
    draft = _draft([_Line("bible:王腾飞", "早晚有一日，我定要手刃此人！")])
    required = [{"quote_id": "Q01", "text": "早晚有一日，我定要手刃此人！", "speaker": "孟浩", "source_segment_index": 1}]
    errors = dialogue_speaker_errors(draft, required, n2i, "")
    assert len(errors) == 1 and "孟浩" in errors[0] and "bible:孟浩" in errors[0]
    assert dialogue_speaker_errors(_draft([_Line("bible:孟浩", "早晚有一日，我定要手刃此人！")]), required, n2i, "") == []


def test_untraceable_offscreen_line_is_dropped_from_dialogue_and_prompt():
    """2026-09-05 第 2 集：模型把原文转述成画外音，打回三次仍如此。追溯不到就删，不再拦整段。"""
    src = "[段1·S01]孟浩面色阴沉，他不时取出妖丹吞下。[段1·S02]“早晚有一日，我定要手刃此人！”孟浩想起对方的贪婪。"
    prompt = "镜头1：孟浩盘膝而坐。画外音（孟浩）：“昔日屈辱，今日必讨。”；夕阳暖金色光影\n全片贯穿：环境音。"
    draft = _draft([_Line("bible:孟浩", "昔日屈辱，今日必讨。", "offscreen_voice")], prompt)
    errors = dialogue_speaker_errors(draft, [], manifest_name_to_identity(PAYLOAD), src)
    assert errors == []
    assert draft.dialogue == []
    assert "昔日屈辱" not in draft.prompt_text and "画外音（孟浩）" not in draft.prompt_text
    assert "镜头1：孟浩盘膝而坐。" in draft.prompt_text and "夕阳暖金色光影" in draft.prompt_text


def test_narration_derived_offscreen_line_is_reassigned_to_narrator_and_label_rewritten():
    src = "[段1·S01]他们不知道，有些难过是没有表情的。[段1·S02]2014 年，巴西。"
    prompt = "镜头1：里奥站在场边。画外音（里奥）：“有些难过是没有表情的。”\n全片贯穿：环境音；配乐；风格；约束。"
    payload = {"asset_manifest": {"characters": [{"identity_id": "bible:里奥", "display_name": "里奥", "aliases": []}]}}
    line = _Line("bible:里奥", "有些难过是没有表情的。", "offscreen_voice")
    draft = _draft([line], prompt)
    assert dialogue_speaker_errors(draft, [], manifest_name_to_identity(payload), src) == []
    assert line.speaker_identity_id == NARRATOR
    assert "画外音（旁白）：“有些难过是没有表情的。”" in draft.prompt_text


def test_quoted_source_offscreen_line_keeps_character_speaker():
    src = "[段1·S01]“早晚有一日，我定要手刃此人！”孟浩想起对方的贪婪。"
    line = _Line("bible:孟浩", "早晚有一日，我定要手刃此人！", "offscreen_voice")
    assert dialogue_speaker_errors(_draft([line]), [], manifest_name_to_identity(PAYLOAD), src) == []
    assert line.speaker_identity_id == "bible:孟浩"


def test_explicit_naming_in_same_sentence_overrides_appellation_match():
    """「少年叹了口气，他叫孟浩」：称谓匹配到「少年」（映射台曾把它登记成王有材别名），
    但同一句原文点了名——以点名为准，孟浩落榜独白不能判给王有材。"""
    names = ["孟浩", "王有材", "少年"]
    text = "神色中多了一抹茫然。\n“又落榜了……”少年叹了口气，他叫孟浩，是这大青山下云杰县一个普通书生"
    start = text.index("又落榜"); end = text.index("……”") + 2
    assert attribute_prose_speaker(text, start, end, names) == "孟浩"
    plain = "夜里。\n“走吧。”少年道，转身离开，孟浩跟在后面"
    start = plain.index("走吧"); end = plain.index("。”") + 1
    assert attribute_prose_speaker(plain, start, end, names) == "少年", "没有点名句时仍按称谓本身"


def test_self_mocking_is_utterance_evidence():
    text = "已贫贫如洗。\n“莫非科举真的不是我孟浩未来的路？”孟浩自嘲，低头看了一眼手中的葫芦"
    start = text.index("莫非"); end = text.index("？”") + 1
    assert attribute_prose_speaker(text, start, end, ["孟浩", "王有材"]) == "孟浩"
