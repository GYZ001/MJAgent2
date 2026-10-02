"""映射台 2.0.16 缺陷②第四轮续修：同名不同物要有能区分的 label（见
``chunk_extraction.py`` ``_KNOWN_PROP_NAME_FIELD_RULE`` 上方 PREP_PACK_
VERSION 2.0.16 changelog）。

真实案例（顾念长安第2集，B 沙箱用 2.0.15 真实重跑）：第6段顾屿「自己的
外套」正确判断归属不符（``known_prop_name`` 留空），``card_match.py`` 也
不再被字面包含判据推翻这个拒绝——但这条提及的 ``label`` 仍是裸词"外套"，
与已登记卡同名，``app.props.service._prop_mention_skip_reason`` 的"label
已登记"判据直接把它当成"这就是那张卡的重复申报"而跳过：既不绑错，也不
建卡，分镜台拿不到任何参考图。这条 skip 判据本身没错——它拦的是真正同一
件实物的重复申报；错的是这条提及自己的 label 没有把"不是同一件"体现出来。

修法：``known_prop_name`` 留空时，label 要写成能与同名/相近名卡区分开的
称呼（带归属或可见特征，例如"顾屿外套"）。**不能用"谁的＋东西"这种带
"的"字的写法**（例如"顾屿的外套"）——本文件独立验证过：``app.props.
labels.normalize_prop_label`` 会把"谁的"这个前缀剥掉，剥完又变回裸词，
重新撞上同一张卡，``ensure_props_for_labels`` 会把这个"区分过"的 label
当成别名悄悄合并回错的那张卡，区分就白做了（见下方
``test_ensure_props_for_labels_with_de_phrasing_would_silently_merge_
as_alias`` 这条反例测试，钉死这个真实验证过的陷阱，防止提示词以后改回
"谁的"这种更顺口但会踩坑的写法）。

派单要求先确认的另一件事：label 换成非原文字面的描述性称呼，不会让这条
提及被锚定核验判成"未解析"——``prop_literal_or_trailing_anchor`` 对
label 命中失败时会退回检查 ``source_wording``（2.0.7 起的既有行为，本文件
未改动，只是补测），只要 source_wording 仍然逐字摘自原文，锚定照样通过。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_conn
from app.production.prep_pack import chunk_extraction as ce
from app.production.prep_pack import prop_recheck
from app.production.prep_pack.prop_manifest import (
    _prep_pack_build_prop_manifest,
    _prep_pack_prop_mention_binding,
)
from app.production.prep_pack.trailing_anchor import prop_literal_or_trailing_anchor
from app.props import judge, service
from app.props.labels import normalize_prop_label
from app.schemas import Prop
from app.source_excerpt import SourceSegment

SEG6_TEXT = (
    "夜里，顾屿家的阳台上凉风习习……顾屿听完温念转述陆一舟的话，端着两杯热姜茶出来"
    "的手顿了一下，递给她一杯，又顺手把自己的外套搭在她肩上，声音放得很稳。"
)


def _card(name: str, appearance: str = "米白色灯芯绒") -> Prop:
    return Prop(name=name, appearance_canonical=appearance, aliases=[])


def _seed_project(project_id: str, props_list: list[dict]) -> None:
    bible = {
        "characters": [], "scenes": [], "props": props_list,
        "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), 0),
    )
    conn.commit()


class _Captured(Exception):
    """只用来把控制权从被测协程里拿回来，不代表失败。"""


# ---------------------------------------------------------------------------
# 提示词：单源常量要求 label 区分、禁止"谁的"写法、source_wording 不受影响
# ---------------------------------------------------------------------------


def test_known_prop_name_rule_tells_model_to_disambiguate_the_label() -> None:
    rule = ce._KNOWN_PROP_NAME_FIELD_RULE
    assert "label 不要再写成与那张卡一样的裸词" in rule
    assert "带上这段里能确定的归属或可见特征" in rule
    assert "顾屿外套" in rule and "游客手机" in rule


def test_known_prop_name_rule_forbids_de_possessive_phrasing() -> None:
    rule = ce._KNOWN_PROP_NAME_FIELD_RULE
    assert "不要用「谁的＋东西」这种带" in rule
    assert "不要写「顾屿的外套」" in rule
    assert "标签归一逻辑剥掉「谁的」这个前缀" in rule


def test_known_prop_name_rule_keeps_source_wording_literal_unchanged() -> None:
    rule = ce._KNOWN_PROP_NAME_FIELD_RULE
    assert "source_wording 仍然只填原文逐字写法，不用跟着改" in rule
    assert "source_wording 负责「原文怎么写」，label 负责「申报哪一件」" in rule


def test_recheck_prompt_includes_the_same_disambiguation_rule_via_single_source() -> None:
    """复核提示词通过同一个 ``_KNOWN_PROP_NAME_FIELD_RULE`` 常量带上这段规则
    ——字符串级相等，不是两处各写一份措辞。"""
    prompt = prop_recheck._prompt("（原文）", [], [])
    assert ce._KNOWN_PROP_NAME_FIELD_RULE in prompt


def test_extraction_prompt_includes_the_rule_verbatim() -> None:
    seen: dict[str, str] = {}

    async def fake_call(**kwargs):
        seen["prompt"] = kwargs.get("prompt") or ""
        raise _Captured

    original = ce._call_structured
    ce._call_structured = fake_call
    try:
        segment = SourceSegment(segment_id="s1", start_offset=0, end_offset=20, text=SEG6_TEXT)
        with pytest.raises(_Captured):
            asyncio.run(ce._extract_chunk(
                chunk=[(6, segment)], known_characters=[], known_scenes=[],
                known_props=["名称：外套｜外观：米白色灯芯绒"], attempt_hint="",
                run_id=None, episode_id="ep2", episode_no=2, chunk_index=0,
            ))
    finally:
        ce._call_structured = original
    assert "顾屿外套" in seen["prompt"]


# ---------------------------------------------------------------------------
# 先确认：描述性 label + 逐字 source_wording 仍能通过锚定核验（2.0.7 既有
# 行为，未改动，补测钉住）
# ---------------------------------------------------------------------------


def test_descriptive_label_still_anchors_via_literal_source_wording() -> None:
    segments = [SourceSegment(segment_id="s6", text=SEG6_TEXT, start_offset=0, end_offset=len(SEG6_TEXT))]
    literal_indexes, phrase, trailing = prop_literal_or_trailing_anchor(
        "顾屿外套", "外套", [1], segments, SEG6_TEXT, card_bound=False,
    )
    assert literal_indexes == [1], "label 非原文字面时应退回 source_wording 逐字命中锚定"
    assert phrase == "外套"
    assert trailing is False, "是 source_wording 直接命中，不是尾部退让"


def test_mention_binding_builds_distinct_unbound_entry_for_disambiguated_label() -> None:
    """卡「外套」在 cards_with_prior_evidence 里、未提名：常规判据会先字面
    命中又被收紧拒绝，card=None；但描述性 label 让锚定走 source_wording
    直接命中，提及不会被当成"未解析"丢弃。"""
    card = _card("外套")
    segments = [SourceSegment(segment_id="s6", text=SEG6_TEXT, start_offset=0, end_offset=len(SEG6_TEXT))]

    binding = _prep_pack_prop_mention_binding(
        "顾屿外套", "外套", "", [1], segments, [card], frozenset({"外套"}),
    )

    assert binding is not None, "不应该被判成未解析丢弃"
    segment_indexes, bound_card, method, anchor_segments, anchor_phrase, trailing = binding
    assert bound_card is None, "常规判据应该被此前出场证据收紧拒绝"
    assert segment_indexes == [1]
    assert method == "direct"
    assert anchor_phrase == "外套"


def test_build_prop_manifest_keys_disambiguated_label_separately_from_the_card() -> None:
    """清单条目的 key 取 canonical_name or label；card=None 时 key 就是
    label 本身——"顾屿外套"与卡名"外套"是两个不同的 key，不会被同一个
    setdefault 合并掉彼此。"""
    card = _card("外套")
    segments = [SourceSegment(segment_id="s6", text=SEG6_TEXT, start_offset=0, end_offset=len(SEG6_TEXT))]
    mentions = [{
        "label": "顾屿外套", "source_wording": "外套", "known_prop_name": "",
        "segment_indexes": [1], "description": "顾屿自己的外套，搭在温念肩上",
        "plot_significant": True, "plot_significant_quote": "又顺手把自己的外套搭在她肩上",
    }]

    props = _prep_pack_build_prop_manifest(
        mentions, segments, cards=[card], cards_with_prior_evidence=frozenset({"外套"}),
    )

    assert len(props) == 1
    assert props[0]["label"] == "顾屿外套"
    assert props[0]["canonical_name"] is None


# ---------------------------------------------------------------------------
# 陷阱验证：normalize_prop_label 会剥掉"谁的"前缀——这是提示词禁止"的"字
# 写法的真实依据，不是凭空猜测
# ---------------------------------------------------------------------------


def test_normalize_prop_label_strips_de_possessive_prefix_back_to_the_collision() -> None:
    """独立验证提示词里"不要用谁的＋东西"这条禁令的真实依据：把"顾屿的
    外套"喂给 normalize_prop_label，物主前缀被剥掉，只剩"外套"——与已登记
    卡重新撞名。"""
    assert normalize_prop_label("顾屿的外套") == ["外套"]
    assert normalize_prop_label("游客的手机") == ["手机"]
    # 不带"的"的写法不会被这条规则误伤
    assert normalize_prop_label("顾屿外套") == ["顾屿外套"]
    assert normalize_prop_label("游客手机") == ["游客手机"]


async def test_ensure_props_for_labels_builds_independent_card_without_de_phrasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端正例：label="顾屿外套"（不带"的"）——不撞已登记名字，
    ``match_existing_prop_card`` 常规判据虽然命中又被此前出场证据拒绝，
    最终走新建卡流程，建出一张独立的「顾屿外套」卡，原「外套」卡不受
    任何影响（不多一个别名、不被覆盖）。"""
    _seed_project("p-coat-ok", [{"name": "外套", "appearance_canonical": "米白色灯芯绒", "aliases": []}])

    async def fake_chat_structured(_messages, **_kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(appearance_canonical="深灰色羊毛呢外套，翻领双排扣", aliases=[])

    monkeypatch.setattr(judge.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", lambda *a, **k: asyncio.sleep(0, result=None))

    mentions = [{
        "label": "顾屿外套", "description": "顾屿自己的外套，搭在温念肩上保暖",
        "segment_indexes": [6], "plot_significant": True,
        "plot_significant_quote": "又顺手把自己的外套搭在她肩上",
        "source_wording": "外套", "known_prop_name": "",
    }]

    result = await service.ensure_props_for_labels(
        "p-coat-ok", 2, mentions, source_text=SEG6_TEXT,
        cards_with_prior_evidence=frozenset({"外套"}),
    )

    assert [item["name"] for item in result["added"]] == ["顾屿外套"]
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-coat-ok'").fetchone()["bible_json"])
    names = {p["name"] for p in bible["props"]}
    assert names == {"外套", "顾屿外套"}
    original_card = next(p for p in bible["props"] if p["name"] == "外套")
    assert original_card["aliases"] == [], "原卡不应该被污染新别名"


async def test_ensure_props_for_labels_with_de_phrasing_would_silently_merge_as_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反例（钉住陷阱，不是期望行为）：如果 label 仍然写成"顾屿的外套"这种
    带"的"的写法，``normalize_prop_label`` 会把它剥回"外套"、与已登记名字
    相同，``ensure_props_for_labels`` 会判定"已经归到既有卡"，把"顾屿的
    外套"登记成那张**错误**卡的别名——这正是提示词新规则要避免的真实后果。
    这条测试钉住这个陷阱的存在，防止以后有人把提示词"简化"回"谁的"写法。
    """
    _seed_project("p-coat-trap", [{"name": "外套", "appearance_canonical": "米白色灯芯绒", "aliases": []}])

    async def explode_if_called(*_a, **_k):
        raise AssertionError("陷阱场景下不该发起任何模型调用——应该在 known 命中就短路")

    monkeypatch.setattr(judge.model_gateway, "chat_structured", explode_if_called)

    mentions = [{
        "label": "顾屿的外套", "description": "顾屿自己的外套，搭在温念肩上保暖",
        "segment_indexes": [6], "plot_significant": True,
        "plot_significant_quote": "又顺手把自己的外套搭在她肩上",
        "source_wording": "外套", "known_prop_name": "",
    }]

    result = await service.ensure_props_for_labels(
        "p-coat-trap", 2, mentions, source_text=SEG6_TEXT,
        cards_with_prior_evidence=frozenset({"外套"}),
    )

    assert result == {"added": [], "errors": []}, "陷阱：没有新建卡，模型调用都没发起"
    conn = get_conn()
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p-coat-trap'").fetchone()["bible_json"])
    assert [p["name"] for p in bible["props"]] == ["外套"], "陷阱：没有建出独立卡"
    assert "顾屿的外套" in bible["props"][0]["aliases"], "陷阱：被错误登记成原卡的别名"
