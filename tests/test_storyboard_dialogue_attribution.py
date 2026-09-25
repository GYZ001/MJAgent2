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


_SPEAKER_OBJECT_NAMES = ["林姐", "小满", "小姑娘", "你", "我", "她", "老周"]


def test_same_clause_multiple_candidates_defers_instead_of_guessing():
    """2026-09-24 复盘：生产实测 proj_112c2467fcc9/ep_49a5d01e5dd6 第 8 段
    `林姐冲小满挤挤眼："……"` 被判给小满；第一版修法（按「最靠左」当主语）在这句上
    碰巧判对，但结构判据分不清同一分句里谁是主语、谁是介词宾语——反例是神墓「辰南
    想起了他父亲对他说的话：」，「辰南」（主句主语）最靠左但真正说话人是嵌在从句里
    的「他父亲」（见 test_embedded_clause_speaker_is_not_confused_with_matrix_
    subject）。按 CLAUDE.md「确定不了时不猜，空着至少是诚实的」：离冒号最近、且含
    候选名字的分句里出现 ≥2 个不同候选就留空，交给读了完整原文的第二阶段模型判断；
    这意味着原本「判对」的「林姐冲小满挤挤眼：」现在也留空——不为了保住一个只在
    部分结构上凑巧对的位置启发式而放松「不确定就不猜」。不逐一列举「冲/对/朝/向/
    跟/看着」这些介词/动词，覆盖该结构任意写法；反过来小满是主语时同样留空，不是
    偏向固定某个名字。"""
    cases = [
        ("林姐冲小满挤挤眼：“小姑娘，你运气好，全城只有老周修得了这个。”", "冲"),
        ("林姐对小满说：“小姑娘，你运气好。”", "对"),
        ("林姐朝小满喊：“小姑娘，你运气好。”", "朝"),
        ("林姐看着小满道：“小姑娘，你运气好。”", "看着…道"),
        ("林姐向小满招手：“小姑娘，你运气好。”", "向"),
        ("林姐跟小满说：“小姑娘，你运气好。”", "跟"),
        ("小满冲林姐挤挤眼：“阿姨，你手真巧。”", "宾语/主语对调"),
        ("林姐冲小满挤挤眼，笑着说：“小姑娘，你运气好。”", "回退到上一分句后仍有 2 个候选"),
        ("小满被林姐拉着说：“你别急。”", "被字句：同一分句两个候选"),
    ]
    for text, label in cases:
        start = text.index("“") + 1
        end = text.index("”")
        assert attribute_prose_speaker(text, start, end, _SPEAKER_OBJECT_NAMES) == "", label


def test_clause_retreat_finds_the_sole_candidate_in_an_earlier_clause():
    """「看着小满，林姐说道：」离冒号最近的分句「林姐说道：」只有一个候选「林姐」——
    不用回退也不构成歧义，仍应判出（与上面「同一分句 2 个候选」的留空场景对照）。"""
    text = "看着小满，林姐说道：“小姑娘，你运气好。”"
    start = text.index("“") + 1
    end = text.index("”")
    assert attribute_prose_speaker(text, start, end, _SPEAKER_OBJECT_NAMES) == "林姐"


def test_repeated_name_occurrence_is_attributed_by_its_nearest_clause():
    """2026-09-24 B 上历史台词只读排查：三国演义 EP1 第 19 段「刘备惊问张飞，张飞
    道：「如此害民贼……」」被判给刘备——「张飞」在窗口里出现两次（先做「惊问」的
    宾语，后做「道」的主语），旧实现用 re.search 按 name 整体找一次匹配起点，扫到
    的是「张飞」的第一次（宾语）出现就已经能拼出合法匹配（拿「，张飞道」当填充），
    于是把「张飞」错误定位到更早的分句，与「刘备」的分句序打平后按位置选中了刘备。
    改成直接按分句做子串包含检查后，离冒号最近的分句「张飞道：」只含「张飞」一个
    候选（「刘备」「督邮」都只在更早的分句里），不再受重复出现次数影响。"""
    text = "刘备听见门前喧闹，急忙出去观看，见被捆打的正是督邮。刘备惊问张飞，张飞道：“如此害民贼，不打死留着干什么！”"
    names = ["张飞", "刘备", "督邮"]
    start = text.index("“") + 1
    end = text.index("”")
    assert attribute_prose_speaker(text, start, end, names) == "张飞"


def test_embedded_clause_speaker_is_not_confused_with_matrix_subject():
    """2026-09-24 B 上历史台词只读排查：神墓 EP1 第 24 段「辰南想起了他父亲对他说的
    话：「辰南你要记住……」」真正说话人是嵌在同位语从句里的「他父亲」，而离冒号最近
    的分句「辰南想起了他父亲对他说的话：」同时含「辰南」（主句主语）与「他父亲」
    （从句主语，真正的说话人）两个候选——结构启发式分不清主句主语与从句主语，按
    「≥2 个不同候选就留空」不会再把这类嵌套句式误判给主句主语。"""
    text = "辰南想起了他父亲对他说的话：“辰南你要记住，能够看透我们家传玄功内息流转的人都不简单。”"
    names = ["辰南", "他父亲"]
    start = text.index("“") + 1
    end = text.index("”")
    assert attribute_prose_speaker(text, start, end, names) == ""


def test_speaker_tie_break_does_not_depend_on_unrelated_alias_count():
    """同一对名字的判定结果不该因人物谱里多挂了几个不相关别名就翻转——旧实现遇到
    第一个正则命中的名字就返回，长度相同时先命中谁取决于 set 的遍历顺序，只因
    names 集合大小变化就可能整个翻转（与这句话本身的语义毫无关系）。改成分句内按
    子串包含判定候选集合后，集合运算与遍历顺序无关，两种 names 集合结果一致——
    同一分句里仍是 2 个不同候选，按当前判据都应留空。"""
    text = "林姐冲小满挤挤眼：“小姑娘，你运气好，全城只有老周修得了这个。”"
    start = text.index("“") + 1
    end = text.index("”")
    minimal = ["林姐", "小满"]
    with_unrelated_aliases = ["林姐", "小满", "小姑娘", "你", "我", "她"]
    assert attribute_prose_speaker(text, start, end, minimal) == ""
    assert attribute_prose_speaker(text, start, end, with_unrelated_aliases) == ""


def test_prose_extraction_leaves_ambiguous_quote_speaker_empty_and_required_dialogue_omits_it():
    """全链路：同一分句 2 个候选时，_extract_prose_segment → extract_dialogue_
    targets 产出 speaker=""，required_dialogue_for_segments 因此不写 speaker/
    speaker_identity_id 键（storyboard_dialogue_ledger.required_dialogue_for_
    segments 用 `**({"speaker": quote.speaker} if quote.speaker else {})`，空串
    不写键，不是写一个空字符串）——与「原文本就没点名说话人」的既有形态完全一致，
    第二阶段模型据此自行判断，不会因为多出一个「已归属但是错的」字段把模型锁死。"""
    text = "林姐冲小满挤挤眼：“小姑娘，你运气好，全城只有老周修得了这个。”"
    quotes = extract_dialogue_targets([_seg(text)], set(), speaker_names=_SPEAKER_OBJECT_NAMES)
    assert quotes and quotes[0].speaker == "" and quotes[0].speaker_identity_id == ""
    kept = [SimpleNamespace(quote_id=quotes[0].quote_id, segment_no=1)]
    required = required_dialogue_for_segments(kept, quotes)
    assert "speaker" not in required[1][0] and "speaker_identity_id" not in required[1][0]


def test_prose_extraction_attributes_unambiguous_repeated_name_to_the_subject():
    """全链路对照：张飞重复出现但离冒号最近的分句唯一候选是张飞（同上一条 attribute_
    prose_speaker 单测），extract_dialogue_targets 接上后仍正确产出非空 speaker。"""
    text = "刘备听见门前喧闹，急忙出去观看，见被捆打的正是督邮。刘备惊问张飞，张飞道：“如此害民贼，不打死留着干什么！”"
    quotes = extract_dialogue_targets([_seg(text)], set(), speaker_names=["张飞", "刘备", "督邮"])
    assert quotes and quotes[0].speaker == "张飞"


def test_screenplay_format_coverb_note_does_not_confuse_speaker_line_match():
    """剧本体（「说话人（备注）：台词」）夹具：介词宾语出现在备注里不会被误当说话人。
    这条走的是完全独立的抽取路径（_extract_script_segment 按行首字面命中
    speaker_names），不经过 attribute_prose_speaker，确认两条路径分工清楚、
    互不影响、剧本体这条本就不受本次改动触及。"""
    text = "林姐（冲小满挤挤眼）：小姑娘，你运气好，全城只有老周修得了这个。"
    names = ["林姐", "小满", "小姑娘", "老周"]
    quotes = extract_dialogue_targets([_seg(text)], set(), speaker_names=names)
    assert [q.speaker for q in quotes] == ["林姐"]
    assert quotes[0].text == "小姑娘，你运气好，全城只有老周修得了这个。"


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


# ---------------------------------------------------------------- 引语跨段窗口
def test_ledger_quote_split_across_segment_windows_still_rejects_narrator() -> None:
    """2026-09-12 我欲封天第 4 集镜 24/25：引语「越强则越强……此宗被称之为赵国魔宗。」跨在
    单元 7/8 的边界上，阶段二给每段的窗口一边只剩左引号、一边只剩右引号，_QUOTE_RE 配不
    上对，in_quotes 恒 False，「引语不能改成旁白」不触发；预检拿整段原文当场拦下，出路
    只剩人工修订。台账按引号抽出这句时就已经判定它是引语，阶段二要认台账，不认窗口。"""
    # 本段窗口：只有左引号，右引号在下一段
    window = "[段4·S06]除非是获得丹药或者灵石，可以大大缩短时间。[段4·S07]“越强则越强，越弱则越弱，难道这靠山宗是要以这种方法，来培养出内门弟子……"
    required = [{"quote_id": "Q24", "text": "越强则越强，越弱则越弱，难道这靠山宗是要以这种方法，来培养出内门弟子……",
                 "speaker": "", "source_segment_index": 4}]
    line = _Line(NARRATOR, "越强则越强，越弱则越弱，难道这靠山宗是要以这种方法，来培养出内门弟子……", "offscreen_voice")
    errors = dialogue_speaker_errors(_draft([line]), required, manifest_name_to_identity(PAYLOAD), window)
    assert any("不能因说话人未知就改为旁白" in e for e in errors), errors


def test_ledger_quote_explicitly_narrated_is_still_allowed() -> None:
    """台账自己把这句记成旁白（剧本格式 `旁白：…` 行）时，阶段二不得反过来打回。"""
    window = "[段1·S01]旁白：多年前，此宗被称之为赵国魔宗。"
    required = [{"quote_id": "Q01", "text": "多年前，此宗被称之为赵国魔宗。", "speaker": "旁白", "source_segment_index": 1}]
    line = _Line(NARRATOR, "多年前，此宗被称之为赵国魔宗。", "offscreen_voice")
    assert dialogue_speaker_errors(_draft([line]), required, manifest_name_to_identity(PAYLOAD), window) == []


def test_narration_outside_ledger_is_untouched_by_the_quote_rule() -> None:
    """不在台账、也不在引号里的旁白概括，本来就允许——这条规则只管台账认定的引语。"""
    window = "[段1·S01]时间一晃，过去了七天，这七天里孟浩在外宗又看到了几次抢夺之事。"
    line = _Line(NARRATOR, "时间一晃，过去了七天", "offscreen_voice")
    assert dialogue_speaker_errors(_draft([line]), [], manifest_name_to_identity(PAYLOAD), window) == []
