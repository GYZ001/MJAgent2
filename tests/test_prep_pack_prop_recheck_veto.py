"""道具复核的否决权第一处（2.0.18，真实案例 proj_ca86b15ab7d7 顾念长安第2集）：
对抽取已申报条目的逐件否决。

从 ``test_prep_pack_prop_recheck.py`` 拆出（该文件已在 500 行基线顶格，否决
权是独立于原有"补漏/合并/容错"职责的新机制）：chunk 抽取把温念正穿着的衣物
（浅灰色卫衣/米白针织开衫/浅蓝碎花长裙）申报成道具，复核此前只能并集补充、
删不掉误报，生产 run_21445d406ecb 实测复现；修法见 ``app.production.
prep_pack.prop_recheck`` 模块 docstring"否决权"一节完整案情。本文件锁住
对已申报条目的逐件否决（``_prop_veto_is_grounded``/``_prop_recheck_
vetoes``/``_apply_prop_vetoes``）与第五处"任何角色不分主次"的提示词
澄清。否决权第二/四/五处（复核自己新发现的独立自查、同一实物两条不一致
裁决的传播修复）的测试拆到 ``test_prep_pack_prop_recheck_addition_
confirm.py``（本文件补第五处测试后也顶到了 500 行基线，见该文件 docstring
完整案情）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import prop_recheck
from app.production.prep_pack.prop_veto import _PropDeclarationVeto, _prop_veto_is_grounded
from app.production.prep_pack.schemas import _ModelPropMention

# ---------------------------------------------------------------------------
# 提示词契约：否决环节的出现/跳过
# ---------------------------------------------------------------------------


def test_prompt_omits_veto_section_when_nothing_declared_yet() -> None:
    """本 chunk 还没有任何已申报道具时（declared_props 为空），不问一个
    空名单——整段否决环节跳过，不给模型制造"随便挑一条否决"的空间。"""
    prompt = prop_recheck._prompt("（原文）", [], [])
    assert "declaration_vetoes" not in prompt


def test_prompt_lists_declared_props_with_index_for_veto() -> None:
    """已申报道具清单带编号出现在提示词里，供模型用编号引用——不要求模型
    复述 label 字符串。"""
    declared = [_ModelPropMention(
        label="浅灰色卫衣", description="灰色卫衣", segment_indexes=[2, 4, 6],
        plot_significant=False, plot_significant_quote="套了一件浅灰色卫衣",
        source_wording="浅灰色卫衣", known_prop_name="",
    )]
    prompt = prop_recheck._prompt("（原文）", [], declared)
    assert "declaration_vetoes" in prompt
    assert "1. label=「浅灰色卫衣」" in prompt
    assert "套了一件浅灰色卫衣" in prompt


def test_prompt_clarifies_any_character_includes_background_extras() -> None:
    """2.0.18 第五处真实案例（"游客举手机拍照"被误判成"无核心角色互动"）：
    提示词必须包含"任何角色不分主次"的完整正面陈述，并明确点名群演/路人/
    功能性人物同样算角色，禁止模型自行加"核心角色"类限定词——这是语义
    判断，mock 测试只能测提示词有没有带上这段陈述，模型是否真的听懂要靠
    B 沙箱真实验证。"""
    declared = [_ModelPropMention(
        label="游客手机", description="手机", segment_indexes=[4],
        plot_significant=False, plot_significant_quote="",
        source_wording="手机", known_prop_name="",
    )]
    prompt = prop_recheck._prompt("（原文）", [], declared)
    assert "群演、路人、功能性人物同样算角色" in prompt
    assert "核心角色" in prompt and "判据原文没有的限定词" in prompt


# ---------------------------------------------------------------------------
# 否决权第一处：对抽取已申报条目的逐件裁决
# ---------------------------------------------------------------------------

SWEATSHIRT_QUOTE = "顾屿换下了昨晚那身深灰大衣，套了一件浅灰色卫衣"
CARDIGAN_QUOTE = "温念仍是那件米白针织开衫和浅蓝碎花长裙"


def _worn_clothing_declared() -> list[_ModelPropMention]:
    """真实生产形状复现：chunk 抽取（id 82339）把温念/顾屿正穿着的衣物报成
    了道具，known_prop_name 提名了旧衣服卡，plot_significant_quote 自己写的
    就是明确的穿着句——这正是应该被否决的三条。"""
    return [
        _ModelPropMention(
            label="浅灰色卫衣", description="浅灰色卫衣", segment_indexes=[2, 4, 6],
            plot_significant=False, plot_significant_quote=SWEATSHIRT_QUOTE,
            source_wording="浅灰色卫衣", known_prop_name="卫衣",
        ),
        _ModelPropMention(
            label="米白针织开衫", description="米白针织开衫", segment_indexes=[2, 3, 4, 5, 6],
            plot_significant=False, plot_significant_quote=CARDIGAN_QUOTE,
            source_wording="米白针织开衫", known_prop_name="针织开衫",
        ),
        _ModelPropMention(
            label="浅蓝碎花长裙", description="浅蓝碎花长裙", segment_indexes=[2, 3, 4, 5, 6],
            plot_significant=False, plot_significant_quote=CARDIGAN_QUOTE,
            source_wording="浅蓝碎花长裙", known_prop_name="",
        ),
    ]


def test_veto_grounded_when_quote_is_verbatim_and_names_the_item() -> None:
    """二次核验通过的形状：证据句逐字出自该道具自己申报的段落、且包含
    label。"""
    item = _worn_clothing_declared()[0]
    segment = type("Seg", (), {"text": f"正午过后，{SWEATSHIRT_QUOTE}，出门去逛。"})()
    chunk_by_index = {2: segment}
    assert _prop_veto_is_grounded(item, SWEATSHIRT_QUOTE, chunk_by_index) is True


def test_veto_rejected_when_quote_is_not_verbatim() -> None:
    """证据句被转述/改写过（不是逐字子串）——核验不通过，不许删。"""
    item = _worn_clothing_declared()[0]
    segment = type("Seg", (), {"text": f"正午过后，{SWEATSHIRT_QUOTE}，出门去逛。"})()
    chunk_by_index = {2: segment}
    paraphrased = "顾屿穿着浅灰色卫衣"
    assert _prop_veto_is_grounded(item, paraphrased, chunk_by_index) is False


def test_veto_rejected_when_quote_does_not_name_the_item() -> None:
    """证据句确实逐字出自原文，但压根没提这件道具的 label/source_wording——
    核验不通过，防止拿一句无关的话否决这条申报。"""
    item = _worn_clothing_declared()[0]
    unrelated = "顾屿寸步不离地跟在旁边"
    segment = type("Seg", (), {"text": f"{unrelated}，{SWEATSHIRT_QUOTE}。"})()
    chunk_by_index = {2: segment}
    assert _prop_veto_is_grounded(item, unrelated, chunk_by_index) is False


def test_veto_rejected_when_quote_is_from_a_different_segment() -> None:
    """证据句不在这条申报自己 segment_indexes 覆盖的任何一段里——即使文字
    本身逐字存在于别的段落，也不算数（不越权替这条申报去别处找证据）。"""
    item = _worn_clothing_declared()[0]  # segment_indexes=[2, 4, 6]
    chunk_by_index = {9: type("Seg", (), {"text": SWEATSHIRT_QUOTE})()}
    assert _prop_veto_is_grounded(item, SWEATSHIRT_QUOTE, chunk_by_index) is False


def test_veto_rejected_when_evidence_quote_is_empty() -> None:
    item = _worn_clothing_declared()[0]
    chunk_by_index = {2: type("Seg", (), {"text": SWEATSHIRT_QUOTE})()}
    assert _prop_veto_is_grounded(item, "", chunk_by_index) is False


def test_declared_index_out_of_range_is_dropped_not_matched() -> None:
    """模型给出的 declared_index 超出本次喂给它的清单范围（凭空编造/协议
    之外）——这条裁决直接丢弃，不当成任何一条申报的否决。"""
    declared = _worn_clothing_declared()
    verdicts = [_PropDeclarationVeto(
        declared_index=99, criterion="越界", evidence_quote=SWEATSHIRT_QUOTE,
    )]
    results = prop_recheck._prop_recheck_vetoes(verdicts, declared, {})
    assert results == []


def test_real_production_shape_three_worn_clothes_are_vetoed_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实案例端到端复现（run_21445d406ecb）：三件正穿着的衣物被 chunk
    抽取误报成道具，复核模型正确否决、代码核验通过——必须被摘除，不再
    进入 asset_manifest.props。同时验证一件真实道具（木簪）不受牵连。"""
    declared = _worn_clothing_declared() + [_ModelPropMention(
        label="木簪", description="缠着细银丝的木簪", segment_indexes=[2],
        plot_significant=True, plot_significant_quote="顾屿听着，不动声色地买下来",
        source_wording="木簪", known_prop_name="木簪",
    )]
    seg2 = type("Seg", (), {"text": f"{SWEATSHIRT_QUOTE}。{CARDIGAN_QUOTE}。顾屿听着，不动声色地买下来。"})()
    chunk = [(2, seg2)]

    async def fake_call(**kwargs):
        return prop_recheck._PropRecheckResponse(props=[], declaration_vetoes=[
            {"declared_index": 1, "criterion": "仍穿在身上未脱下", "evidence_quote": SWEATSHIRT_QUOTE},
            {"declared_index": 2, "criterion": "仍穿在身上未脱下", "evidence_quote": CARDIGAN_QUOTE},
            {"declared_index": 3, "criterion": "仍穿在身上未脱下", "evidence_quote": CARDIGAN_QUOTE},
        ])

    monkeypatch.setattr(prop_recheck, "_call_structured", fake_call)
    response = type("R", (), {})()
    response.props = list(declared)
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=chunk, chunk_index=1, episode_id="ep2",
        known_props=[], run_id=None,
    ))
    labels = {m.label for m in result.props}
    assert labels == {"木簪"}, "三件正穿着的衣物必须被否决摘除，木簪这类真实道具不受牵连"


def test_veto_with_ungrounded_evidence_does_not_delete_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反例：模型给出否决，但证据句是转述（非逐字）——必须保留原申报，打
    [PREP_PACK_PROP_VETO_UNGROUNDED] 而不是静默删除。"""
    declared = [_worn_clothing_declared()[0]]
    seg2 = type("Seg", (), {"text": f"{SWEATSHIRT_QUOTE}。"})()
    chunk = [(2, seg2)]

    async def fake_call(**kwargs):
        return prop_recheck._PropRecheckResponse(props=[], declaration_vetoes=[
            {"declared_index": 1, "criterion": "仍穿着", "evidence_quote": "顾屿穿着浅灰色卫衣"},
        ])

    monkeypatch.setattr(prop_recheck, "_call_structured", fake_call)
    response = type("R", (), {})()
    response.props = list(declared)
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=chunk, chunk_index=1, episode_id="ep2",
        known_props=[], run_id=None,
    ))
    assert [m.label for m in result.props] == ["浅灰色卫衣"], "证据不是逐字时必须保留，不许静默删"


def test_veto_ungrounded_logs_visible_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    declared = [_worn_clothing_declared()[0]]
    seg2 = type("Seg", (), {"text": f"{SWEATSHIRT_QUOTE}。"})()
    chunk = [(2, seg2)]

    async def fake_call(**kwargs):
        return prop_recheck._PropRecheckResponse(props=[], declaration_vetoes=[
            {"declared_index": 1, "criterion": "仍穿着", "evidence_quote": "顾屿穿着浅灰色卫衣"},
        ])

    monkeypatch.setattr(prop_recheck, "_call_structured", fake_call)
    response = type("R", (), {})()
    response.props = list(declared)
    with caplog.at_level("WARNING"):
        asyncio.run(prop_recheck.attach_prop_recheck(
            response, chunk=chunk, chunk_index=1, episode_id="ep2",
            known_props=[], run_id=None,
        ))
    assert "PREP_PACK_PROP_VETO_UNGROUNDED" in caplog.text


def test_veto_grounded_logs_visible_warning_with_evidence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    declared = [_worn_clothing_declared()[0]]
    seg2 = type("Seg", (), {"text": f"{SWEATSHIRT_QUOTE}。"})()
    chunk = [(2, seg2)]

    async def fake_call(**kwargs):
        return prop_recheck._PropRecheckResponse(props=[], declaration_vetoes=[
            {"declared_index": 1, "criterion": "仍穿在身上未脱下", "evidence_quote": SWEATSHIRT_QUOTE},
        ])

    monkeypatch.setattr(prop_recheck, "_call_structured", fake_call)
    response = type("R", (), {})()
    response.props = list(declared)
    with caplog.at_level("WARNING"):
        asyncio.run(prop_recheck.attach_prop_recheck(
            response, chunk=chunk, chunk_index=1, episode_id="ep2",
            known_props=[], run_id=None,
        ))
    assert "PREP_PACK_PROP_DECLARATION_VETOED" in caplog.text
    assert SWEATSHIRT_QUOTE in caplog.text


def test_recheck_failure_preserves_all_declared_props_including_worn_clothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复核调用失败时一律保留——即使这个 chunk 里有看起来像误报的穿着类
    道具，也不能因为复核这道增强链路本身出问题就被连累删除（宁可多一次
    误报留给人工核查，也不许静默删）。"""
    declared = _worn_clothing_declared()

    async def explode(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(prop_recheck, "_call_structured", explode)
    response = type("R", (), {})()
    response.props = list(declared)
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=[(2, type("Seg", (), {"text": "占位"})())], chunk_index=1,
        episode_id="ep2", known_props=[], run_id=None,
    ))
    assert len(result.props) == 3, "复核失败必须原样保留全部已申报道具，一条都不许静默删"
