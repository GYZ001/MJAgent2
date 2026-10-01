"""映射台 2.0.17：带数量的群体称谓不能归到候选判别选中的某一位具名角色
（见 ``functional_candidates.py`` ``_prep_pack_label_is_counted_group`` 上方
PREP_PACK_VERSION 2.0.17 changelog）。

真实案例（顾念长安第2集，生产 B 用 2.0.16 重跑映射，run_c64b5d58c431）：第4段
原文「两名游客正举着手机在城门下拍照，互相帮对方摆姿势，说笑着走远」——抽取
调用把这两个路人报成角色提及 ``display_name="游客"``（模型自己转述时把"两名"
这个数量词丢了），discovery 判定不是可建卡的新角色、落候选判别
（``_prep_pack_functional_candidate_verdict_only``）。B 上只读查真实
provider_calls 证实：候选判别调用的候选判别卷宗（按既有设计跨段拼接、覆盖
本集其它段落）里，模型拿第2段温念吃小吃"连连夸赞比什么都新奇"这种毫不相关
的"像游客"语气当依据，把"游客"判给了候选温念——``supporting_segment_index``
钉证只核验"这是卷宗里真实的段号"，不核验"这段真的在说这个标签"，于是温念
的 ``aliases`` 里多出一条"游客"，分镜台按别名表会把第4段两个路人的镜头画成
温念的样子。同一次运行里，独立的叙述向称谓归属（``appellation_resolve.py``）
对同一处原文的判定是 ``functional``（候选之外的另几个具体的人）——两条通路
互相印证"游客"不该被候选判别绑定给任何候选。

修法：不检查"游客"这个具体词（no-blacklist-fixes 纪律），检查这个标签自己
申报的段落原文里，紧邻标签之前是否写着"数词/不定量词＋人物类量词"（两名/
几位/三个/数名……）——汉语数量短语的语法结构，闭集语法单位。命中就说明
候选名单之外至少两个人，候选判别这一步直接短路返回未尝试，连模型调用都不
发起（省一次调用，也不给模型"自由联想"的空间）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.production.prep_pack.functional_candidates import _prep_pack_label_is_counted_group
from app.production.prep_pack import functional_candidate_verdict as fcv
from app.schemas import Bible
from app.source_excerpt import SourceSegment

SEG4_TEXT = (
    "正午过后，顾屿借了一辆双人自行车，带温念绕着明城墙骑了一圈……骑到一处城门下……"
    "两名游客正举着手机在城门下拍照，互相帮对方摆姿势，说笑着走远。"
)


def _segments(overrides: dict[int, str], *, last_index: int) -> list[SourceSegment]:
    out: list[SourceSegment] = []
    for index in range(1, last_index + 1):
        text = overrides.get(index, f"第{index}段：占位原文。")
        out.append(SourceSegment(segment_id=f"s{index}", text=text, start_offset=0, end_offset=len(text)))
    return out


def _bible() -> Bible:
    return Bible.model_validate({
        "characters": [
            {"name": "温念", "role": "主角", "appearance_canonical": "占位外观"},
            {"name": "顾屿", "role": "主角", "appearance_canonical": "占位外观"},
        ],
        "world": {"era": "", "genre": "", "visual_style_canonical": "占位画风"},
    })


def _conn_with_episodes_table() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE episodes(id TEXT, project_id TEXT, screenplay_json TEXT)")
    return conn


# ---------------------------------------------------------------------------
# _prep_pack_label_is_counted_group：纯结构判据本身
# ---------------------------------------------------------------------------


def test_real_case_two_tourists_is_counted_group() -> None:
    """真实案例原样复现：标签「游客」自己申报的段落（第4段）原文里，紧邻
    它之前写着「两名」。"""
    segments = _segments({4: SEG4_TEXT}, last_index=4)
    assert _prep_pack_label_is_counted_group("游客", segments, {4}) is True


@pytest.mark.parametrize("quantifier_text", [
    "几位客人正在大堂等候。",
    "三个路人匆匆走过。",
    "数名士兵把守城门。",
    "十几位乘客陆续下车。",
    "2名工作人员在核对名单。",
])
def test_various_counted_group_quantifiers_are_detected(quantifier_text: str) -> None:
    """不针对"游客"这个具体词——任意标签只要自己申报的段落里紧邻着数词/
    不定量词＋人物类量词，都要能识别，这是语法结构判据不是词表。"""
    import re
    label = re.search(r"(?:客人|路人|士兵|乘客|工作人员)", quantifier_text).group()
    segments = _segments({1: quantifier_text}, last_index=1)
    assert _prep_pack_label_is_counted_group(label, segments, {1}) is True


def test_quantifier_kept_inside_the_label_itself_is_also_detected() -> None:
    """B 沙箱第二次真实重跑同一案例实测的另一种模型措辞：这次抽取调用没有
    把"两名"转述丢掉，display_name 本身就是"两名游客"——数量词变成了
    label 的前缀而不是"匹配之前的原文"，必须单独核验 label 自身的开头，
    不能只看"匹配前的原文"这一种形态。"""
    segments = _segments({4: SEG4_TEXT}, last_index=4)
    assert _prep_pack_label_is_counted_group("两名游客", segments, {4}) is True


def test_single_named_character_is_not_a_counted_group() -> None:
    """具名角色没有数量词前缀，不该被这条新规则误伤。"""
    segments = _segments({1: "顾屿说：“姜茶，趁热喝，晚上凉。”"}, last_index=1)
    assert _prep_pack_label_is_counted_group("顾屿", segments, {1}) is False


def test_descriptive_label_without_quantifier_is_not_a_counted_group() -> None:
    """没有数量词的描述性标签（"银色长袍女子"类真实先例）照常放行，不被
    误伤——这条新规则只拦"数量词+量词"这个具体语法形态。"""
    segments = _segments({1: "银色长袍女子缓步走出大殿，无人认得她的身份。"}, last_index=1)
    assert _prep_pack_label_is_counted_group("银色长袍女子", segments, {1}) is False


def test_singular_yi_quantifier_does_not_count_as_group() -> None:
    """"一位游客"是单数（"一"不计入群体数量词），不该被误判成群体——真正
    会被识别成群体的是"两/三/几/数/多"等≥2或不定多数的量词。"""
    segments = _segments({1: "一位游客站在桥头拍照。"}, last_index=1)
    assert _prep_pack_label_is_counted_group("游客", segments, {1}) is False


def test_quantifier_not_immediately_before_label_does_not_count() -> None:
    """数量词必须紧邻标签本身，不是"段落里随便哪里出现过数量词"就算——
    "两名士兵"跟后面单独出现的"游客"没有语法关系。"""
    segments = _segments({1: "两名士兵把守城门，一个游客远远地张望着。"}, last_index=1)
    assert _prep_pack_label_is_counted_group("游客", segments, {1}) is False


def test_only_checks_the_labels_own_declared_segments() -> None:
    """只看标签自己申报的段落（event_span_segments），不做全集扫描——即使
    第2段里写着"两名游客"，标签自己只申报了第4段（且第4段没有数量词前缀），
    就不该命中；不越权替这条提及去别处找证据。"""
    segments = _segments({
        2: "两名游客路过。",
        4: "游客正举着手机拍照。",
    }, last_index=4)
    assert _prep_pack_label_is_counted_group("游客", segments, {4}) is False


# ---------------------------------------------------------------------------
# 端到端：_prep_pack_functional_candidate_verdict_only 短路，不发起模型调用
# ---------------------------------------------------------------------------


async def test_counted_group_label_never_reaches_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实案例端到端复现：label="游客"、自己申报段落[4]含"两名"前缀——
    候选判别必须在调用模型之前就短路返回未尝试，温念不会因此多出"游客"
    这个别名。"""
    async def explode(**kwargs):
        raise AssertionError("不该发起候选判别模型调用——应该在数量词检查那里短路")

    monkeypatch.setattr(fcv, "_prep_pack_functional_candidate_call", explode)
    conn = _conn_with_episodes_table()
    segments = _segments({4: SEG4_TEXT}, last_index=4)

    result = await fcv._prep_pack_functional_candidate_verdict_only(
        conn=conn, project_id="p1", episode_id="ep2", episode_no=2,
        label="游客", source_text=SEG4_TEXT, segments=segments, bible=_bible(),
        character_mentions=[{"display_name": "游客", "segment_indexes": [4]}],
    )

    assert result == {"resolved": False, "attempted": False}


async def test_single_person_label_still_resolves_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """反例：没有数量词前缀的单人标签不受影响，候选判别照常发起模型调用
    并能正常绑定——不能为了堵"游客"这个真实案例，连正常场景也一起堵死。"""
    called = {"count": 0}

    async def fake_call(**kwargs):
        called["count"] += 1
        return fcv._PrepPackFunctionalCandidateVerdict(
            selected_candidate="温念", supporting_segment_index=1, supporting_quote="",
        )

    monkeypatch.setattr(fcv, "_prep_pack_functional_candidate_call", fake_call)
    conn = _conn_with_episodes_table()
    text = "穿着米白针织开衫的女孩笑着回头，温念、顾屿都在场。"
    segments = _segments({1: text}, last_index=1)

    result = await fcv._prep_pack_functional_candidate_verdict_only(
        conn=conn, project_id="p1", episode_id="ep2", episode_no=2,
        label="女孩", source_text=text, segments=segments, bible=_bible(),
        character_mentions=[{"display_name": "女孩", "segment_indexes": [1]}],
    )

    assert called["count"] == 1, "单人称谓必须正常发起候选判别模型调用"
    assert result == {
        "resolved": True, "attempted": True, "canonical_name": "温念",
        "segment_index": 1, "text": text,
    }


# ---------------------------------------------------------------------------
# 钉证二次核验：_prep_pack_candidate_pin_is_grounded（2.0.17 第二轮，协调方
# 复核指出数量群体短路只挡住一种形态——真正缺口是钉证只核验段号真实存在，
# 不核验这段真的在说这个标签、并且把它和候选联系在一起）
# ---------------------------------------------------------------------------


def test_pin_rejected_when_segment_is_not_the_labels_own_and_lacks_the_label_text() -> None:
    """真实案例形状复现：候选判别钉在候选自己出场的第2段（"像游客"语气），
    但这段既不是标签"游客"自己申报的段落（申报的是第4段），原文里也没有
    "游客"这个字面——两层都不满足，必须判未核验通过。"""
    text = "温念买了一串糖葫芦，吃得连连夸赞，像极了第一次见世面的人。"
    assert fcv._prep_pack_candidate_pin_is_grounded(
        2, {4}, text, "游客", ["温念", "她"],
    ) is False


def test_pin_accepted_when_segment_is_the_labels_own_declared_segment() -> None:
    """合法形状：候选锚点段落恰好就是标签自己申报的出场段落之一（即使
    标签本身是合成描述短语、原文从不逐字出现——真实 EP1"银色长袍女子"
    案例），且该段原文里确有候选的名字/已登记别名——两层都满足，通过。"""
    text = "绿袍男子对着她躬身行礼，口称许师姐，随后请四人随他回宗门。"
    assert fcv._prep_pack_candidate_pin_is_grounded(
        14, {13, 14}, text, "银色长袍女子", ["许清", "许师姐"],
    ) is True


def test_pin_accepted_via_literal_label_fallback_when_not_self_declared() -> None:
    """标签字面结构性回退：即使这段不在标签自己申报的段落集合里，只要
    标签本身逐字出现在这段原文、候选名字也在，同样算结构性证据站得住——
    跟 dossier 自己的 A 侧主锚点同一来源（自报段落∪字面命中段落）。"""
    text = "温老师笑着摆摆手，温念连忙道谢。"
    assert fcv._prep_pack_candidate_pin_is_grounded(
        9, {3}, text, "温老师", ["温念", "她"],
    ) is True


def test_pin_rejected_when_label_side_grounded_but_candidate_name_absent() -> None:
    """标签侧过关（就是自己申报的段落），候选侧没过——这段原文压根没提
    候选的名字/任何已登记别名，同样不能判未核验通过，不能只核验一侧。"""
    text = "两名游客正举着手机在城门下拍照，互相帮对方摆姿势，说笑着走远。"
    assert fcv._prep_pack_candidate_pin_is_grounded(
        4, {4}, text, "游客", ["温念", "她", "温老师"],
    ) is False


# ---------------------------------------------------------------------------
# 端到端：_prep_pack_functional_candidate_verdict_only 真的发起模型调用，
# 但钉证核验失败/通过
# ---------------------------------------------------------------------------


async def test_ungrounded_pin_does_not_bind_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：label="路人"没有数量词前缀，不会被 2.0.17 第一轮短路，候选
    判别模型调用真的发起了；但模型钉的段落（第2段，候选自己出场但跟标签
    无关）既不是标签自己申报的段落（第4段），原文也没有标签字面——不能
    绑定，返回"发起过但没绑定"。"""
    async def fake_call(**kwargs):
        return fcv._PrepPackFunctionalCandidateVerdict(
            selected_candidate="温念", supporting_segment_index=2, supporting_quote="",
        )

    monkeypatch.setattr(fcv, "_prep_pack_functional_candidate_call", fake_call)
    conn = _conn_with_episodes_table()
    seg2_text = "温念买了一串糖葫芦，吃得连连夸赞，像极了第一次见世面的人。"
    seg4_text = "一个路人举着手机在城门下拍照，笑着走远。"
    segments = _segments({2: seg2_text, 4: seg4_text}, last_index=4)

    result = await fcv._prep_pack_functional_candidate_verdict_only(
        conn=conn, project_id="p1", episode_id="ep2", episode_no=2,
        label="路人", source_text=seg2_text + seg4_text, segments=segments, bible=_bible(),
        character_mentions=[{"display_name": "路人", "segment_indexes": [4]}],
    )

    assert result == {"resolved": False, "attempted": True}


async def test_grounded_pin_on_same_segment_binds_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """反例：正常的称谓归属不被新核验误伤——候选判别钉的段落正是"温老师"
    这个称呼自己出现的段落，且同段里温念本人也在场对话。"""
    async def fake_call(**kwargs):
        return fcv._PrepPackFunctionalCandidateVerdict(
            selected_candidate="温念", supporting_segment_index=1, supporting_quote="",
        )

    monkeypatch.setattr(fcv, "_prep_pack_functional_candidate_call", fake_call)
    conn = _conn_with_episodes_table()
    text = "温老师笑着摆摆手，温念连忙道谢。"
    segments = _segments({1: text}, last_index=1)

    result = await fcv._prep_pack_functional_candidate_verdict_only(
        conn=conn, project_id="p1", episode_id="ep2", episode_no=2,
        label="温老师", source_text=text, segments=segments, bible=_bible(),
        character_mentions=[{"display_name": "温老师", "segment_indexes": [1]}],
    )

    assert result == {
        "resolved": True, "attempted": True, "canonical_name": "温念",
        "segment_index": 1, "text": text,
    }
