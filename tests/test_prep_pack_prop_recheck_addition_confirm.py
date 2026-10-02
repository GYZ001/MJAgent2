"""道具复核新发现的独立自查（2.0.18 第二/四/五处，从
``test_prep_pack_prop_recheck_veto.py`` 拆出——该文件在补第五处"任何角色
不分主次"测试后超过 500 行基线）。

见 ``app.production.prep_pack.prop_recheck_addition_confirm`` 模块
docstring 完整案情：第二处是复核自己新发现的候选同样可能误判穿着中的
衣物（第一处否决审不到"复核自己报的"），第四处是同一件实物在
declaration_vetoes 与新发现自查之间给出两个不一致裁决时的传播修复，
第五处是"任何角色"不分主次的提示词澄清（B 沙箱真实案例"两名游客举着
手机拍照"被误判成"无核心角色互动"）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import prop_recheck, prop_recheck_addition_confirm
from app.production.prep_pack.schemas import _ModelPropMention
from tests.conftest import patch_prep_pack_everywhere

SWEATSHIRT_QUOTE = "顾屿换下了昨晚那身深灰大衣，套了一件浅灰色卫衣"

# ---------------------------------------------------------------------------
# 否决权第二处：新发现独立自查（B 沙箱两轮真实验证第1轮当场复现）——抽取
# 这次没报任何道具（declared_props 为空），复核自己独立扫描原文却把正穿着
# 的衣物当新发现报了出来，第一处否决审不到"复核自己报的"，必须有独立的
# 第二次自查。
# ---------------------------------------------------------------------------


def test_addition_confirm_prompt_clarifies_any_character_includes_background_extras() -> None:
    """2.0.18 第五处真实案例（"游客举手机拍照"被误判成"无核心角色互动"）：
    新发现自查提示词也要带上"任何角色不分主次"的完整正面陈述——两条否决
    通路共用同一份语义交代，不是只改了第一处；这是语义判断，mock 测试
    只能测提示词有没有带上这段陈述。"""
    candidate = _ModelPropMention(
        label="游客手机", description="手机", segment_indexes=[4],
        plot_significant=False, plot_significant_quote="",
        source_wording="手机", known_prop_name="",
    )
    prompt = prop_recheck_addition_confirm._addition_confirm_prompt("（原文）", [candidate])
    assert "群演、路人、功能性人物同样算角色" in prompt
    assert "核心角色" in prompt and "判据原文没有的限定词" in prompt


def _self_reported_sweatshirt() -> dict:
    """B 沙箱真实复现（本次重跑该集，extraction 的 props 为空，recheck 自己
    的 props 字段里新冒出这一条）：label/known_prop_name 都提名了既有的
    「浅灰色卫衣」卡，plot_significant_quote 自己写的就是穿着句。"""
    return {
        "label": "浅灰色卫衣", "description": "浅灰色卫衣",
        "segment_indexes": [2], "plot_significant": False,
        "plot_significant_quote": SWEATSHIRT_QUOTE,
        "source_wording": "浅灰色卫衣", "known_prop_name": "浅灰色卫衣",
    }


def test_confirm_additions_returns_unchanged_when_no_candidates() -> None:
    """没有新发现时直接原样返回空列表，不发起自查调用（省一次调用）。"""
    kept, vetoed = asyncio.run(prop_recheck_addition_confirm._confirm_additions(
        episode_id="ep2", chunk_index=1, chunk=[], run_id=None,
        candidates=[], chunk_by_index={},
    ))
    assert kept == []
    assert vetoed == []


def test_confirm_additions_vetoes_a_self_reported_worn_clothing_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实案例端到端复现：复核自己新报出的"浅灰色卫衣"带着自我拆台的
    穿着句证据，独立自查必须否决它——不能因为是复核自己说的就天然可信。"""
    candidate = _self_reported_sweatshirt()
    seg2 = type("Seg", (), {"text": f"{SWEATSHIRT_QUOTE}，出门去逛。"})()
    chunk_by_index = {2: seg2}

    async def fake_call(**kwargs):
        assert kwargs.get("model_type") is prop_recheck_addition_confirm._PropAdditionConfirmResponse
        return prop_recheck_addition_confirm._PropAdditionConfirmResponse(vetoes=[
            {"declared_index": 1, "criterion": "仍穿在身上未脱下", "evidence_quote": SWEATSHIRT_QUOTE},
        ])

    monkeypatch.setattr(prop_recheck_addition_confirm, "_call_structured", fake_call)
    kept, vetoed = asyncio.run(prop_recheck_addition_confirm._confirm_additions(
        episode_id="ep2", chunk_index=1, chunk=[(2, seg2)], run_id=None,
        candidates=[candidate], chunk_by_index=chunk_by_index,
    ))
    assert kept == [], "带自我拆台穿着句证据的新发现必须被自查否决摘除"
    assert len(vetoed) == 1 and vetoed[0]["mention"].label == "浅灰色卫衣"


def test_confirm_additions_keeps_candidate_when_veto_evidence_ungrounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自查给出否决，但证据句是转述（非逐字）——保留候选，不许静默删。"""
    candidate = _self_reported_sweatshirt()
    seg2 = type("Seg", (), {"text": f"{SWEATSHIRT_QUOTE}。"})()
    chunk_by_index = {2: seg2}

    async def fake_call(**kwargs):
        return prop_recheck_addition_confirm._PropAdditionConfirmResponse(vetoes=[
            {"declared_index": 1, "criterion": "仍穿着", "evidence_quote": "顾屿穿着浅灰色卫衣"},
        ])

    monkeypatch.setattr(prop_recheck_addition_confirm, "_call_structured", fake_call)
    kept, vetoed = asyncio.run(prop_recheck_addition_confirm._confirm_additions(
        episode_id="ep2", chunk_index=1, chunk=[(2, seg2)], run_id=None,
        candidates=[candidate], chunk_by_index=chunk_by_index,
    ))
    assert kept == [candidate], "证据不是逐字时必须保留，不许静默删"
    assert vetoed == []


def test_confirm_additions_keeps_all_candidates_when_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自查调用本身失败——候选原样保留，不阻断主流程（同既有补漏/否决的
    失败处置）。"""
    candidate = _self_reported_sweatshirt()

    async def explode(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(prop_recheck_addition_confirm, "_call_structured", explode)
    kept, vetoed = asyncio.run(prop_recheck_addition_confirm._confirm_additions(
        episode_id="ep2", chunk_index=1, chunk=[(2, type("Seg", (), {"text": "占位"})())],
        run_id=None, candidates=[candidate], chunk_by_index={},
    ))
    assert kept == [candidate]
    assert vetoed == []


def test_real_sandbox_shape_extraction_empty_recheck_self_report_vetoed_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B 沙箱两轮验证第1轮真实复现的端到端形状：extraction 这次 props 为空
    （declared_props=[]，不问否决环节），recheck 自己在 props 字段里新冒出
    "浅灰色卫衣"（带穿着句证据）——经过 recheck_chunk_props 的新发现自查，
    最终 added 必须不含这条，response.props 最终不出现这件衣物。"""
    seg2_text = f"{SWEATSHIRT_QUOTE}，出门去逛。"
    seg2 = type("Seg", (), {"text": seg2_text})()
    chunk = [(2, seg2)]
    calls: list[type] = []

    async def fake_call(**kwargs):
        model_type = kwargs.get("model_type")
        calls.append(model_type)
        if model_type is prop_recheck_addition_confirm._PropAdditionConfirmResponse:
            return prop_recheck_addition_confirm._PropAdditionConfirmResponse(vetoes=[
                {"declared_index": 1, "criterion": "仍穿在身上未脱下", "evidence_quote": SWEATSHIRT_QUOTE},
            ])
        return prop_recheck._PropRecheckResponse(props=[_self_reported_sweatshirt()], declaration_vetoes=[])

    patch_prep_pack_everywhere(monkeypatch, "_call_structured", fake_call)
    response = type("R", (), {})()
    response.props = []
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=chunk, chunk_index=1, episode_id="ep2",
        known_props=[], run_id=None,
    ))
    assert result.props == [], "复核自己新发现的穿着类道具必须被自查否决，不进入最终 props"
    assert prop_recheck_addition_confirm._PropAdditionConfirmResponse in calls, "必须真的发起了新发现自查调用"


# ---------------------------------------------------------------------------
# 同一件实物两条不一致裁决（第二轮真实验证发现）：抽取已经申报过的同一件
# 实物，复核自己重新扫描又报了一遍，declaration_vetoes 放过了原始申报、
# 新发现自查却正确否决了重复的那份——真实案例"毛线围巾"/"手机"。
# ---------------------------------------------------------------------------


def test_propagate_self_check_veto_to_matching_declared_entry() -> None:
    """纯函数核验：自查否决的候选和 declared_props 里某条原始申报结构匹配
    （同 label）时，同一个否决结论要同步套用到那条原始申报。"""
    declared_item = _ModelPropMention(
        label="毛线围巾", description="毛线围巾", segment_indexes=[7],
        plot_significant=False, plot_significant_quote="",
        source_wording="毛线围巾", known_prop_name="",
    )
    duplicate_mention = _ModelPropMention(
        label="毛线围巾", description="毛线围巾（复核重复发现）", segment_indexes=[7],
        plot_significant=False, plot_significant_quote="",
        source_wording="毛线围巾", known_prop_name="",
    )
    quote = "准备整理妈妈寄来的换季衣物——厚外套、毛线围巾"
    segment = type("Seg", (), {"text": quote})()
    chunk_by_index = {7: segment}
    vetoed = [{"mention": duplicate_mention, "criterion": "背景陈设一笔带过+无人互动+全文只出现一次", "evidence_quote": quote}]

    results = prop_recheck_addition_confirm._propagate_self_check_vetoes_to_declared(
        vetoed, [declared_item], chunk_by_index,
    )
    assert len(results) == 1
    assert results[0]["item"] is declared_item
    assert results[0]["grounded"] is True


def test_propagate_does_not_match_unrelated_declared_entries() -> None:
    """签名不匹配的原始申报不受牵连——不是随便拿一个否决去牵连其它条目。"""
    unrelated_item = _ModelPropMention(
        label="木簪", description="木簪", segment_indexes=[2],
        plot_significant=True, plot_significant_quote="顾屿买下来别进她鬓边",
        source_wording="木簪", known_prop_name="",
    )
    duplicate_mention = _ModelPropMention(
        label="毛线围巾", description="毛线围巾", segment_indexes=[7],
        plot_significant=False, plot_significant_quote="",
        source_wording="毛线围巾", known_prop_name="",
    )
    vetoed = [{"mention": duplicate_mention, "criterion": "背景陈设", "evidence_quote": "随便一句话"}]
    results = prop_recheck_addition_confirm._propagate_self_check_vetoes_to_declared(
        vetoed, [unrelated_item], {},
    )
    assert results == []


def test_real_second_round_shape_duplicate_declaration_both_vetoed_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实案例端到端复现（B 沙箱第二轮验证）：抽取已经申报了"毛线围巾"，
    复核自己独立重新扫描原文时又把同一件实物报了一遍（作为"新发现"）；
    declaration_vetoes 没有否决原始申报（模拟模型在这条判断上的疏漏），
    但新发现自查正确否决了重复的那份——两条记录代表同一件实物，原始申报
    也必须被同步摘除，不能让它靠"没被 declaration_vetoes 点名"原样存活。"""
    quote = "准备整理妈妈寄来的换季衣物——厚外套、毛线围巾"
    seg7 = type("Seg", (), {"text": quote})()
    chunk = [(7, seg7)]

    declared_scarf = _ModelPropMention(
        label="毛线围巾", description="毛线围巾", segment_indexes=[7],
        plot_significant=False, plot_significant_quote="",
        source_wording="毛线围巾", known_prop_name="",
    )

    async def fake_call(**kwargs):
        model_type = kwargs.get("model_type")
        if model_type is prop_recheck_addition_confirm._PropAdditionConfirmResponse:
            return prop_recheck_addition_confirm._PropAdditionConfirmResponse(vetoes=[
                {"declared_index": 1, "criterion": "背景陈设一笔带过+无人互动+全文只出现一次",
                 "evidence_quote": quote},
            ])
        # 复核自己独立重新扫描，把抽取已经申报的同一件实物又报了一遍，
        # declaration_vetoes 这次没有否决它（模拟模型在这条判断上的疏漏）。
        return prop_recheck._PropRecheckResponse(
            props=[{
                "label": "毛线围巾", "description": "毛线围巾", "segment_indexes": [7],
                "plot_significant": False, "plot_significant_quote": "",
                "source_wording": "毛线围巾", "known_prop_name": "",
            }],
            declaration_vetoes=[],
        )

    patch_prep_pack_everywhere(monkeypatch, "_call_structured", fake_call)
    response = type("R", (), {})()
    response.props = [declared_scarf]
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=chunk, chunk_index=1, episode_id="ep2",
        known_props=[], run_id=None,
    ))
    assert result.props == [], (
        "复核自己重复发现的同一件实物被自查否决时，抽取的原始申报也必须被同步摘除，"
        "不能因为 declaration_vetoes 没点名它就原样存活"
    )
