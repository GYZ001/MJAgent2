"""画外音归属人物的「原文发声依据」：同一句里「人物 + 心道/说道… + 台词」。

2026-09-14 我欲封天第 4 集第 16 段：「精明男子……内心暗笑，心道这些刚入外宗的弟子最好糊弄」
无引号，模型归给精明男子（忠实），旧闸门要求「提供原文发声依据」却没有字段能满足，三次修复失败。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_dialogue_attribution import dialogue_speaker_errors
from app.production.storyboard_voicing_evidence import voicing_evidence

SOURCE = (
    "[段2·S07] 孟浩拿着铜镜迟疑不定。\n"
    "[段2·S08] 精明男子大有深意的看着孟浩，眼见孟浩越加迟疑，内心暗笑，心道这些刚入外宗的弟子最好糊弄，"
    "不过以这镜子为诱，为他赚了不少的灵石。\n"
    "[段2·S09] 靠山宗曾是赵国四大宗门之首，风光一时无两。"
)
NAMES = {"精明男子": "bible:精明男子", "孟浩": "bible:孟浩", "他": "bible:孟浩", "旁白": "旁白"}
LINE = "这些刚入外宗的弟子最好糊弄，靠这铜镜又能赚不少灵石。"


def test_thought_verb_in_same_sentence_names_the_sentence_subject() -> None:
    assert voicing_evidence(LINE, "bible:精明男子", NAMES, SOURCE) is True
    # 「眼见孟浩迟疑」里的孟浩不是无主语小句「心道……」的主语
    assert voicing_evidence(LINE, "bible:孟浩", NAMES, SOURCE) is False


def test_label_right_before_verb_wins_over_sentence_subject() -> None:
    source = "精明男子看着他，孟浩心道这些刚入外宗的弟子最好糊弄。"
    assert voicing_evidence(LINE, "bible:孟浩", NAMES, source) is True
    assert voicing_evidence(LINE, "bible:精明男子", NAMES, source) is False


def test_plain_narration_has_no_voicing_evidence() -> None:
    assert voicing_evidence("靠山宗曾是赵国四大宗门之首", "bible:精明男子", NAMES, SOURCE) is False


def test_single_char_pronoun_alias_is_not_evidence() -> None:
    assert voicing_evidence(LINE, "bible:孟浩", NAMES, "他心道这些刚入外宗的弟子最好糊弄。") is False


def _draft(identity: str):
    line = SimpleNamespace(line=LINE, delivery="offscreen_voice", delivery_kind="inner_monologue",
                           speaker_identity_id=identity, speaker=identity.split(":")[-1])
    return SimpleNamespace(dialogue=[line], prompt_text="镜头1：精明男子看着孟浩，画外音（精明男子）：“" + LINE + "”")


def test_gate_accepts_inner_thought_attributed_to_its_thinker() -> None:
    assert dialogue_speaker_errors(_draft("bible:精明男子"), [], NAMES, SOURCE) == []


def test_gate_still_rejects_wrong_thinker_with_actionable_message() -> None:
    errors = dialogue_speaker_errors(_draft("bible:孟浩"), [], NAMES, SOURCE)
    assert len(errors) == 1 and "缺少人物发声证据" in errors[0] and "改为旁白" in errors[0]


def test_first_person_rendering_of_a_thought_traces_to_its_thinker() -> None:
    """第 5 集第 8 段：原文「孟浩沉默，暗道自己只有凝气一层…」，台词改成第一人称「我只有凝气一层…」。"""
    source = "[段1·S09] 孟浩沉默，暗道自己只有凝气一层，这种所谓的单独丹药，应该不会落在自己身上。"
    line = "我只有凝气一层，这种所谓的单独丹药，应该不会落在我身上。"
    assert voicing_evidence(line, "bible:孟浩", NAMES, source) is True
    assert voicing_evidence(line, "bible:精明男子", NAMES, source) is False


EP13_SEG2 = (
    "“怎么他还在，这坑人的家伙，他实在是太损了！”这修士又恨又怕，叹气正要离开，忽然他双眼一亮，看到山下有一个大汉，正迈步临近公开区。\n"
    "“是曹阳……此人凝气二层巅峰，半只脚迈入三层，他表兄陆烘更是低阶公开区内第一人，使得曹阳在这里横行霸道，"
    "就算是一向喜欢卑鄙的趁别人双双打斗受伤时出手，可却让人敢怒不敢言，若是换了其他人如此，早就被围攻了。"
    "昨日他没来，其他人还好些，今日或许有好戏看。”这修士连忙靠近一些，暗道这曹阳最好去招惹养丹坊分店的家伙。\n"
    "曹阳冷哼一声，他身子高大，虎背熊腰。"
)
EP13_NAMES = {"这修士": "entity:d58", "曹阳": "bible:曹阳", "大汉": "entity:400c", "孟浩": "bible:孟浩"}


def test_multi_sentence_quote_attributed_after_the_closing_mark() -> None:
    """2026-09-14 第 13 集第 8 段：引文内含多个句号，发声者「这修士」与动词「暗道」在引号收尾之后。"""
    line = "是曹阳……此人凝气二层巅峰，半只脚迈入三层，他表兄陆烘更是低阶公开区内第一人，使得曹阳在这里横行霸道，"
    assert voicing_evidence(line, "entity:d58", EP13_NAMES, EP13_SEG2) is True
    # 「“……”这修士又恨又怕，叹气正要离开」：引号后只有称谓没有发声动词——紧邻的称谓也可能是听者
    # （「“你走吧。”孟浩闻言一愣」），保守起见不算证据。
    assert voicing_evidence("怎么他还在，这坑人的家伙，他实在是太损了！", "entity:d58", EP13_NAMES, EP13_SEG2) is False


def test_quote_attributed_to_someone_else_after_closing_mark_is_rejected() -> None:
    line = "是曹阳……此人凝气二层巅峰，半只脚迈入三层，他表兄陆烘更是低阶公开区内第一人，使得曹阳在这里横行霸道，"
    assert voicing_evidence(line, "bible:曹阳", EP13_NAMES, EP13_SEG2) is False
    assert voicing_evidence(line, "bible:孟浩", EP13_NAMES, EP13_SEG2) is False

