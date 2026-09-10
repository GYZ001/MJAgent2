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


def test_narration_derived_offscreen_line_requires_atomic_rewrite():
    src = "[段1·S01]他们不知道，有些难过是没有表情的。[段1·S02]2014 年，巴西。"
    prompt = "镜头1：里奥站在场边。画外音（里奥）：“有些难过是没有表情的。”\n全片贯穿：环境音；配乐；风格；约束。"
    payload = {"asset_manifest": {"characters": [{"identity_id": "bible:里奥", "display_name": "里奥", "aliases": []}]}}
    line = _Line("bible:里奥", "有些难过是没有表情的。", "offscreen_voice")
    draft = _draft([line], prompt)
    assert dialogue_speaker_errors(draft, [], manifest_name_to_identity(payload), src)
    assert line.speaker_identity_id == "bible:里奥"
    assert draft.prompt_text == prompt


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


# ---------------------------------------------------------------- 原文未点名说话人的台词
CROWD_SRC = (
    "“应该是这样，你们看外宗的韩宗师兄出现了。”"
    "“以王腾飞师兄的资质，根本就不会在意这些丹药，他当年加入靠山宗，可是引起了掌门长老等人不小的轰动。”"
    "孟浩听着身边同门的议论，尽管是第一次参与这种事，但也知晓每一次发丹将是争夺的关键。"
)
CROWD_LINE = "以王腾飞师兄的资质，根本就不会在意这些丹药，他当年加入靠山宗，可是引起了掌门长老等人不小的轰动。"
CROWD_PAYLOAD = {"asset_manifest": {"characters": [
    {"identity_id": "bible:孟浩", "display_name": "孟浩", "aliases": []},
    {"identity_id": "entity:f747bf96e5b61fb0", "display_name": "议论的同门", "aliases": []},
]}}


def _crowd_required():
    return [{"quote_id": "Q02", "text": CROWD_LINE, "speaker": "", "source_segment_index": 1}]


def test_unattributed_crowd_line_given_to_the_listener_is_rejected():
    """第 5 集实测：原文写「孟浩听着身边同门的议论」，模型却让孟浩张嘴说无名同门的话。"""
    draft = _draft([_Line("bible:孟浩", CROWD_LINE)])
    errors = dialogue_speaker_errors(draft, _crowd_required(), manifest_name_to_identity(CROWD_PAYLOAD), CROWD_SRC)
    assert len(errors) == 1 and "听者" in errors[0] and "无名人物" in errors[0]


def test_unattributed_line_given_to_a_name_absent_from_the_window_is_not_rejected():
    """缺席不是证据：对话轮替、自称（第 4 集「为兄」）都能合法推出说话人；只有「X听/闻」这种听者证据才打回。"""
    src = "“应该是这样，你们看外宗的韩宗师兄出现了。”" + "“" + CROWD_LINE + "”" + "广场上一片嘈杂。" * 5 + "孟浩走上前。"
    draft = _draft([_Line("bible:孟浩", CROWD_LINE)])
    assert dialogue_speaker_errors(draft, _crowd_required(), manifest_name_to_identity(CROWD_PAYLOAD), src) == []


def test_unattributed_line_given_to_an_unnamed_entity_or_narrator_passes():
    n2i = manifest_name_to_identity(CROWD_PAYLOAD)
    assert dialogue_speaker_errors(_draft([_Line("entity:f747bf96e5b61fb0", CROWD_LINE)]), _crowd_required(), n2i, CROWD_SRC) == []
    assert dialogue_speaker_errors(_draft([_Line(NARRATOR, CROWD_LINE)]), _crowd_required(), n2i, CROWD_SRC) == []


def test_line_with_source_attribution_is_not_touched_by_the_unattributed_rule():
    src = "孟浩沉声道：“" + CROWD_LINE + "”众人闻言一惊。"
    required = [{"quote_id": "Q03", "text": CROWD_LINE, "speaker": "孟浩", "source_segment_index": 1}]
    assert dialogue_speaker_errors(_draft([_Line("bible:孟浩", CROWD_LINE)]), required, manifest_name_to_identity(CROWD_PAYLOAD), src) == []


def test_substring_lines_are_paired_exactly_not_crosswise():
    """第 29 集：『认输……』是『上去就立刻认输。』的子串，包含匹配把两句说话人交叉判错。"""
    payload = {"asset_manifest": {"characters": [
        {"identity_id": "bible:孟浩", "display_name": "孟浩", "aliases": []},
        {"identity_id": "bible:小胖子", "display_name": "小胖子", "aliases": []},
    ]}}
    n2i = manifest_name_to_identity(payload)
    required = [
        {"quote_id": "Q03", "text": "上去就立刻认输。", "speaker": "孟浩", "source_segment_index": 1},
        {"quote_id": "Q04", "text": "认输……", "speaker": "小胖子", "source_segment_index": 1},
    ]
    draft = _draft([_Line("bible:孟浩", "上去就立刻认输。"), _Line("bible:小胖子", "认输……")])
    assert dialogue_speaker_errors(draft, required, n2i, "") == []
    wrong = _draft([_Line("bible:小胖子", "上去就立刻认输。"), _Line("bible:孟浩", "认输……")])
    errors = dialogue_speaker_errors(wrong, required, n2i, "")
    assert len(errors) == 2 and "应为「孟浩」" in errors[0] and "应为「小胖子」" in errors[1]
    # 草稿只有长句且被拆写（包含匹配仍要工作）：短账本项没有精确命中时不去抢长句
    partial = _draft([_Line("bible:孟浩", "上去就立刻认输。")])
    assert dialogue_speaker_errors(partial, required, n2i, "") == []



# ---------------------------------------------------------------- 开口台词取值域
# 2026-09-10 实测「我欲封天」前 10 集分镜产物：259 条 spoken_dialogue 里 15 条（5.8%）
# 在全书原文里逐字找不到，且原样进了视频提示词。此前「追溯不到就删」只管画外音，
# spoken_dialogue 没有取值域约束，提示词还明说「可以补充少量衔接性台词」。
_MASK_SRC = (
    "[段1·S01]“此去外宗，我要向你交代外宗的规矩，我等所在靠山宗，多年前被称之为赵国魔宗，"
    "此名可见凶残之处。”[段1·S02]马脸青年神色平静的说道。"
)


def test_compressed_rewrite_of_a_source_line_is_dropped():
    """第 3 集：原文长台词被压缩改写成一句，而逐字版在同集另外两个镜头照说不误。"""
    prompt = "镜头1：马脸青年开口。“此去外宗，外宗规矩凶险，有杀人区，好自为之。”\n全片贯穿：环境音。"
    line = _Line("bible:马脸青年", "此去外宗，外宗规矩凶险，有杀人区，好自为之。")
    draft = _draft([line], prompt)
    payload = {"asset_manifest": {"characters": [
        {"identity_id": "bible:马脸青年", "display_name": "马脸青年", "aliases": []},
    ]}}
    assert dialogue_speaker_errors(draft, [], manifest_name_to_identity(payload), _MASK_SRC) == []
    assert draft.dialogue == []
    assert "有杀人区" not in draft.prompt_text
    assert "镜头1：马脸青年开口。" in draft.prompt_text


def test_narration_rewritten_into_first_person_speech_is_dropped():
    """第 4 集：第三人称叙述句「正是三个月前将他……」被改成角色第一人称开口。"""
    src = "[段1·S01]孟浩看向台上的女子，这女子，正是三个月前将他从大青山抓来之人。"
    line = _Line("bible:孟浩", "这女子，正是三个月前将我从大青山抓来之人。")
    draft = _draft([line], "镜头1：孟浩抬头。“这女子，正是三个月前将我从大青山抓来之人。”\n全片贯穿：环境音。")
    assert dialogue_speaker_errors(draft, [], manifest_name_to_identity(PAYLOAD), src) == []
    assert draft.dialogue == []


def test_verbatim_source_spoken_line_survives():
    src = "[段1·S01]“早晚有一日，我定要手刃此人！”孟浩咬牙道。"
    line = _Line("bible:孟浩", "早晚有一日，我定要手刃此人！")
    draft = _draft([line])
    assert dialogue_speaker_errors(draft, [], manifest_name_to_identity(PAYLOAD), src) == []
    assert draft.dialogue == [line]


def test_partial_take_of_a_source_line_survives():
    """容量拆分把一句原文台词拆到相邻两个镜头：各取连续的一截，两截都合法。"""
    src = "[段1·S01]“考了三年，这三年来整日看那些贤者书籍，已看的几欲作呕。”孟浩自嘲。"
    head = _Line("bible:孟浩", "考了三年，这三年来整日看那些贤者书籍，")
    tail = _Line("bible:孟浩", "已看的几欲作呕。")
    draft = _draft([head, tail])
    assert dialogue_speaker_errors(draft, [], manifest_name_to_identity(PAYLOAD), src) == []
    assert draft.dialogue == [head, tail]


def test_screenplay_format_line_without_quotes_survives():
    """剧本格式原文（说话人（备注）：台词）没有引号，判据是逐字子串而不是引号内。"""
    src = "[段1·S01]孟浩（叹气）：我自己都快养不活了……\n[段1·S02]（孟浩起身走向门口）"
    line = _Line("bible:孟浩", "我自己都快养不活了……")
    draft = _draft([line])
    assert dialogue_speaker_errors(draft, [], manifest_name_to_identity(PAYLOAD), src) == []
    assert draft.dialogue == [line]


def test_required_line_with_connective_edit_survives_even_if_not_verbatim():
    """必保台词的衔接性微调由 required_dialogue_missing_errors 那条口径管，这里不重复拦。"""
    src = "[段1·S01]马脸青年交代了外宗的规矩。"
    required = [{"quote_id": "Q01", "text": "此去外宗，我要向你交代外宗的规矩。", "source_segment_index": 1}]
    line = _Line("bible:孟浩", "此去外宗，我要向你交代外宗的规矩")
    draft = _draft([line])
    assert dialogue_speaker_errors(draft, required, manifest_name_to_identity(PAYLOAD), src) == []
    assert draft.dialogue == [line]


def test_offscreen_voice_is_not_held_to_the_verbatim_spoken_rule():
    """旁白画外音可以复述叙述句（自己那条可追溯规则管），不受「逐字来自原文」约束。

    同一句话写成 spoken_dialogue 就会被删——判据的差别正是「角色当场开口」与
    「叙述者讲述」的差别，而不是这句话本身合不合法。
    """
    src = "[段1·S01]孟浩心中想着，这铜镜实在诡异。"
    line = _Line(NARRATOR, "这铜镜实在诡异", "offscreen_voice")
    draft = _draft([line])
    assert dialogue_speaker_errors(draft, [], manifest_name_to_identity(PAYLOAD), src) == []
    assert draft.dialogue == [line]
    spoken = _Line(NARRATOR, "这铜镜实在诡异")
    spoken_draft = _draft([spoken])
    assert dialogue_speaker_errors(spoken_draft, [], manifest_name_to_identity(PAYLOAD), src) == []
    assert spoken_draft.dialogue == []
