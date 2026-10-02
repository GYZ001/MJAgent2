"""道具复核（``app.production.prep_pack.prop_recheck``）的契约与容错。

同 ``app.production.prep_pack.scene_recheck`` 同一形状的「单次长 chunk 调用
会漏掉整个类别」补漏调用，只不过这次补的是道具——见该模块 docstring 完整
案情：我欲封天系列 EP1 用 2.0.9 重跑映射，模型报的 8 件道具里 7 件是物件库
已有卡，原文里被角色操作、本集多段出现的脸盆/电闸/勺子/大衣/黑鸟一个都
没报。这里锁住四件事：合并规则（抽取已报不重复、新发现追加）、判重按
label/known_prop_name/source_wording 结构判据、判据原文单源、失败不阻断。

否决权（2.0.18，正穿着的衣物被误判成道具）相关测试拆到
``test_prep_pack_prop_recheck_veto.py``（本文件已在 500 行基线顶格，见该
文件 docstring 完整案情）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import prop_recheck, prop_recheck_addition_confirm
from app.production.prep_pack.chunk_extraction import _PROP_SEGMENT_CRITERIA
from app.production.prep_pack.schemas import _ModelPropMention
from tests.conftest import patch_prep_pack_everywhere

# ---------------------------------------------------------------------------
# 提示词契约
# ---------------------------------------------------------------------------


def test_prompt_uses_the_same_criteria_sentence_as_extraction() -> None:
    """判据原文单源——不是另外复制一份措辞，字符串级相等。"""
    prompt = prop_recheck._prompt("（原文）", [], [])
    assert _PROP_SEGMENT_CRITERIA in prompt


def test_prompt_tells_model_this_is_independent_not_a_diff() -> None:
    prompt = prop_recheck._prompt("（原文）", ["旧行李箱"], [])
    assert "这是一次独立标注" in prompt
    assert "不用回避重复" in prompt


def test_prompt_includes_the_dont_replace_with_registered_name_sentence() -> None:
    """抽取提示词新增的正面陈述（source_wording 不用登记名替代）同样适用于
    复核调用，不是只改了一处。"""
    prompt = prop_recheck._prompt("（原文）", [], [])
    assert "不用物件库里的登记名替代，登记名只填进 known_prop_name" in prompt


# ---------------------------------------------------------------------------
# 合并规则：补漏追加 / 已报不重复
# ---------------------------------------------------------------------------


def test_recheck_appends_a_genuinely_missed_prop(monkeypatch: pytest.MonkeyPatch) -> None:
    """抽取漏报的脸盆被复核补上——新发现的道具追加进 response.props。"""
    async def fake(**kwargs):
        return [{
            "label": "脸盆", "description": "一只搪瓷脸盆", "segment_indexes": [3, 7],
            "plot_significant": False, "plot_significant_quote": "",
            "source_wording": "脸盆", "known_prop_name": "",
        }], []

    monkeypatch.setattr(prop_recheck, "recheck_chunk_props", fake)

    response = type("R", (), {})()
    response.props = []
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=[], chunk_index=1, episode_id="ep1",
        known_props=[], run_id=None,
    ))
    assert [m.label for m in result.props] == ["脸盆"]
    assert result.props[0].segment_indexes == [3, 7]


def test_recheck_does_not_duplicate_already_declared_prop(monkeypatch: pytest.MonkeyPatch) -> None:
    """抽取已经报过的道具（同 label）不重复追加——判重不做并集，只跳过。"""
    async def fake(**kwargs):
        return [{
            "label": "脸盆", "description": "重复申报", "segment_indexes": [9],
            "plot_significant": False, "plot_significant_quote": "",
            "source_wording": "", "known_prop_name": "",
        }], []

    monkeypatch.setattr(prop_recheck, "recheck_chunk_props", fake)

    response = type("R", (), {})()
    response.props = [_ModelPropMention(
        label="脸盆", description="已申报", segment_indexes=[3],
        plot_significant=False, plot_significant_quote="",
        source_wording="", known_prop_name="",
    )]
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=[], chunk_index=1, episode_id="ep1",
        known_props=[], run_id=None,
    ))
    assert len(result.props) == 1, "同 label 的复核候选不得重复追加"
    assert result.props[0].segment_indexes == [3]


def test_recheck_dedup_matches_by_known_prop_name_or_source_wording_too() -> None:
    """判重不只看 label——known_prop_name/source_wording 任一相同也算重复
    （结构判据，比较字段值，不枚举具体道具名）。"""
    existing = _ModelPropMention(
        label="旧行李箱", description="", segment_indexes=[1],
        plot_significant=False, plot_significant_quote="",
        source_wording="", known_prop_name="行李箱",
    )
    candidate = {"label": "行李箱三件套", "known_prop_name": "行李箱", "source_wording": ""}
    assert prop_recheck._prop_mention_signature(existing) & prop_recheck._prop_mention_signature(candidate)


def test_recheck_result_never_removes_already_declared_props(monkeypatch: pytest.MonkeyPatch) -> None:
    """复核只回答「还漏了哪些」，从不否定抽取已申报的条目——同
    scene_recheck 的并集纪律。"""
    async def fake(**kwargs):
        return [{
            "label": "脸盆", "description": "新发现", "segment_indexes": [7],
            "plot_significant": False, "plot_significant_quote": "",
            "source_wording": "", "known_prop_name": "",
        }], []

    monkeypatch.setattr(prop_recheck, "recheck_chunk_props", fake)

    response = type("R", (), {})()
    response.props = [_ModelPropMention(
        label="手机", description="抽取已报", segment_indexes=[1],
        plot_significant=False, plot_significant_quote="",
        source_wording="", known_prop_name="",
    )]
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=[], chunk_index=1, episode_id="ep1",
        known_props=[], run_id=None,
    ))
    labels = {m.label for m in result.props}
    assert labels == {"手机", "脸盆"}


# ---------------------------------------------------------------------------
# 容错：复核失败/空结果不阻断主流程
# ---------------------------------------------------------------------------


def test_recheck_failure_never_breaks_the_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """复核是补漏增强，不是门禁：它挂了不能把整个映射包拖垮，只记告警、
    原样交回抽取结果。"""
    class _Boom(Exception):
        pass

    async def explode(**kwargs):
        raise _Boom("provider down")

    monkeypatch.setattr(prop_recheck, "recheck_chunk_props", explode)

    response = type("R", (), {})()
    response.props = []
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=[], chunk_index=1, episode_id="ep1",
        known_props=[], run_id=None,
    ))
    assert result is response, "补漏失败必须原样交回抽取结果，不能丢数据"


def test_recheck_failure_logs_a_visible_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def explode(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(prop_recheck, "recheck_chunk_props", explode)

    response = type("R", (), {})()
    response.props = []
    with caplog.at_level("WARNING"):
        asyncio.run(prop_recheck.attach_prop_recheck(
            response, chunk=[], chunk_index=1, episode_id="ep1",
            known_props=[], run_id=None,
        ))
    assert "道具复核失败" in caplog.text


def test_empty_recheck_result_leaves_response_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**kwargs):
        return [], []

    monkeypatch.setattr(prop_recheck, "recheck_chunk_props", fake)

    response = type("R", (), {})()
    response.props = []
    result = asyncio.run(prop_recheck.attach_prop_recheck(
        response, chunk=[], chunk_index=1, episode_id="ep1",
        known_props=[], run_id=None,
    ))
    assert result is response
    assert result.props == []


# ---------------------------------------------------------------------------
# recheck_chunk_props：段号结构闸
# ---------------------------------------------------------------------------


def test_recheck_chunk_props_gates_segment_indexes_to_the_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复核申报的段号同样要过结构闸——不在本 chunk 范围内的段号被丢弃。"""
    async def fake_call(**kwargs):
        if kwargs.get("model_type") is prop_recheck_addition_confirm._PropAdditionConfirmResponse:
            return prop_recheck_addition_confirm._PropAdditionConfirmResponse(vetoes=[])
        return prop_recheck._PropRecheckResponse(props=[{
            "label": "脸盆", "description": "一只搪瓷脸盆", "segment_indexes": [3, 99],
            "plot_significant": False, "plot_significant_quote": "",
            "source_wording": "", "known_prop_name": "",
        }])

    patch_prep_pack_everywhere(monkeypatch, "_call_structured", fake_call)
    segment = type("Seg", (), {"text": "脸盆扣在水槽边上。"})()
    added, vetoes = asyncio.run(prop_recheck.recheck_chunk_props(
        episode_id="ep1", chunk_index=1, chunk=[(3, segment)],
        known_props=[], run_id=None, declared_props=[],
    ))
    assert len(added) == 1
    assert added[0]["segment_indexes"] == [3], "超出本 chunk 范围的段号 99 必须被丢弃"
    assert vetoes == []


def test_recheck_chunk_props_drops_mentions_with_blank_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_call(**kwargs):
        return prop_recheck._PropRecheckResponse(props=[{
            "label": "", "description": "", "segment_indexes": [1],
            "plot_significant": False, "plot_significant_quote": "",
            "source_wording": "", "known_prop_name": "",
        }])

    monkeypatch.setattr(prop_recheck, "_call_structured", fake_call)
    segment = type("Seg", (), {"text": "占位原文。"})()
    added, vetoes = asyncio.run(prop_recheck.recheck_chunk_props(
        episode_id="ep1", chunk_index=1, chunk=[(1, segment)],
        known_props=[], run_id=None, declared_props=[],
    ))
    assert added == []
    assert vetoes == []
