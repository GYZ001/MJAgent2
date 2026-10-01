"""映射台 defect② 第三轮续修：常规（字面包含）判据只在「模型无从判断归属」
时才放行（见 ``app.props.card_match`` 模块 docstring 2026-10-01 第三轮修复
一节完整案情）。

真实案例（顾念长安第2集，B 沙箱用 2.0.13 真实重跑）：上一轮给模型喂了每张卡
在更早集数里的「此前出场」归属证据（``chunking._prep_pack_known_prop_
names``），模型也确实用上了——第6段「顾屿……又顺手把自己的外套搭在她肩上」
这条提及，抽取调用真实 reasoning 原文是"已登记的外套：……此前出场：第1集
温念穿的外套，顾屿曾帮她扣好扣子……这里的自己是顾屿……那和已登记的温念的
外套不是同一件，所以known_prop_name填空"——模型正确判断归属不符，
``nominated_card`` 留空。但 ``match_existing_prop_card`` 的常规判据只看字面
包含：这条提及的 ``label`` 恰好是裸词"外套"，与卡名"外套"字面相等，常规
判据照样把它判给了同一张卡，模型的拒绝被架空。同一次真实运行里第4段"两名
游客正举着手机在城门下拍照"也是同一形状——``label``="手机" 字面撞上了项目
里"温念的手机"那张卡，被同一条常规判据静默绑定。

本文件钉住修法：``cards_with_prior_evidence`` 必传集合里的卡，未提名时常规
判据不再把它判给这条提及；没有此前出场证据的卡（第一次登记、或旧卡没有
manifest 记录）不受影响，常规判据照常生效保留召回兜底——真实案例「木簪」
「双人自行车」「热姜茶」「旧铜盘」「小木星星」「行李箱」「字条」这些道具
在它们各自第一次/仅有一次出现的集里都没有「此前出场」证据，必须仍然正常
绑定/建卡，不能被这条收紧误伤。
"""
from __future__ import annotations

import pytest

from app.props.card_match import match_existing_prop_card
from app.schemas import Prop


def _card(name: str, appearance: str = "", aliases: list[str] | None = None) -> Prop:
    return Prop(name=name, appearance_canonical=appearance or f"{name}的标准外观", aliases=aliases or [])


# ---------------------------------------------------------------------------
# 核心场景：真实"外套"案例——未提名 + 有此前出场证据 → 不绑定，有信号
# ---------------------------------------------------------------------------


def test_literal_match_declined_when_card_has_prior_evidence_and_not_nominated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真实案例原样复现：label="外套" 字面等于卡名"外套"，未提名
    （``nominated_card=""``），但这张卡在 ``cards_with_prior_evidence`` 里——
    不绑定，打 ``[PREP_PACK_PROP_LITERAL_MATCH_DECLINED][未拦截]``。"""
    card = _card("外套", "米白色灯芯绒材质，翻领单排扣款式，藏青色罗纹收边")
    evidence_text = "顾屿……又顺手把自己的外套搭在她肩上"

    with caplog.at_level("WARNING"):
        resolved = match_existing_prop_card(
            "外套", evidence_text, [card], source_wording="外套", nominated_card="",
            cards_with_prior_evidence=frozenset({"外套"}),
        )

    assert resolved is None
    assert "[PREP_PACK_PROP_LITERAL_MATCH_DECLINED][未拦截]" in caplog.text
    assert "外套" in caplog.text


def test_same_inputs_without_prior_evidence_still_bind_existing_behavior() -> None:
    """同样的输入，唯一区别是这张卡不在 ``cards_with_prior_evidence`` 里
    （它是第一次登记、或旧卡从没有过 manifest 记录）——常规判据照常生效，
    这是刻意保留的召回兜底，不能被这条新规则一并收紧。"""
    card = _card("外套", "米白色灯芯绒材质，翻领单排扣款式，藏青色罗纹收边")
    evidence_text = "顾屿……又顺手把自己的外套搭在她肩上"

    resolved = match_existing_prop_card(
        "外套", evidence_text, [card], source_wording="外套", nominated_card="",
        cards_with_prior_evidence=frozenset(),
    )

    assert resolved is card


def test_real_shape_phone_mention_also_declined_with_prior_evidence() -> None:
    """同一次真实运行的第二个真实案例："两名游客正举着手机在城门下拍照"，
    label="手机" 字面撞上项目里"温念的手机"卡——同一条新规则同样生效。"""
    card = _card("手机", "银灰色直板手机，背部有磨损划痕")
    evidence_text = "两名游客正举着手机在城门下拍照，互相帮对方摆姿势"

    resolved = match_existing_prop_card(
        "手机", evidence_text, [card], source_wording="手机", nominated_card="",
        cards_with_prior_evidence=frozenset({"手机"}),
    )

    assert resolved is None


# ---------------------------------------------------------------------------
# 提名非空：完全不受这条新规则影响（仍走 _resolve_nominated_card 那条路）
# ---------------------------------------------------------------------------


def test_nomination_bypasses_the_decline_gate_entirely() -> None:
    """模型明确提名（``nominated_card`` 非空）时走提名核验那条路，根本不会
    碰到常规判据、更不会被这条新规则拦——提名核验通过就直接绑定。"""
    card = _card("外套", "米白色灯芯绒材质，翻领单排扣款式，藏青色罗纹收边")
    evidence_text = "温念穿着那件外套，顾屿替她扣好扣子"

    resolved = match_existing_prop_card(
        "外套", evidence_text, [card], source_wording="外套", nominated_card="外套",
        cards_with_prior_evidence=frozenset({"外套"}),
    )

    assert resolved is card


# ---------------------------------------------------------------------------
# 多候选交互：剔除有证据的候选后，剩余候选的歧义判定要重新算
# ---------------------------------------------------------------------------


def test_removing_the_declined_card_resolves_what_was_an_ambiguous_tie() -> None:
    """两张卡同时字面命中本来是结构性歧义，但其中一张被「此前出场」证据
    排除后，只剩一张候选——不再是歧义，正常绑定那一张。"""
    evidenced_card = _card("灵石", "半透明灰蓝色矿石")
    other_card = _card("凝灵丹")
    evidence_text = "桌上摆着凝灵丹与半块灵石，两件宝物一同发亮"

    resolved = match_existing_prop_card(
        "凝灵丹与半块灵石", evidence_text, [evidenced_card, other_card],
        cards_with_prior_evidence=frozenset({"灵石"}),
    )

    assert resolved is other_card


def test_all_candidates_declined_returns_none_without_ambiguous_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """两张卡都在 ``cards_with_prior_evidence`` 里——都被剔除，剩零候选，
    不绑定；不应该再打 PROP_CARD_MATCH_AMBIGUOUS（候选已经不存在，不是
    "多个候选难以取舍"的情形，是"两个都被模型基于证据拒绝了"）。"""
    card_a = _card("灵石")
    card_b = _card("凝灵丹")
    evidence_text = "桌上摆着凝灵丹与半块灵石，两件宝物一同发亮"

    with caplog.at_level("WARNING"):
        resolved = match_existing_prop_card(
            "凝灵丹与半块灵石", evidence_text, [card_a, card_b],
            cards_with_prior_evidence=frozenset({"灵石", "凝灵丹"}),
        )

    assert resolved is None
    assert "PROP_CARD_MATCH_AMBIGUOUS" not in caplog.text
    assert caplog.text.count("[PREP_PACK_PROP_LITERAL_MATCH_DECLINED][未拦截]") == 2


# ---------------------------------------------------------------------------
# 真实道具召回不能被误伤：没有「此前出场」证据的卡照常绑定/建卡
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label_and_card", [
    ("木簪", "木簪"), ("双人自行车", "双人自行车"), ("热姜茶", "热姜茶"),
    ("旧铜盘", "旧铜盘"), ("小木星星", "小木星星"), ("行李箱", "行李箱"), ("字条", "字条"),
])
def test_real_recurring_props_without_prior_evidence_are_not_blocked(
    label_and_card: tuple[str, str],
) -> None:
    """顾念长安第2集这几件真实道具在它们各自第一次出现的集里都没有「此前
    出场」证据——``cards_with_prior_evidence`` 传空集合时必须正常绑定，
    不能被这条收紧误伤真实召回。"""
    label, card_name = label_and_card
    card = _card(card_name)
    evidence_text = f"她伸手碰了碰那件{label}。"

    resolved = match_existing_prop_card(
        label, evidence_text, [card], cards_with_prior_evidence=frozenset(),
    )

    assert resolved is card
