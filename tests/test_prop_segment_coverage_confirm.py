"""映射台 2.0.13 缺陷③：道具段号补全按字面包含把不同的东西并成一件（见
``prop_segment_coverage.py`` 模块 docstring 与 PREP_PACK_VERSION 2.0.13
changelog）。

真实案例（顾念长安第2集，用准备包 2.0.12 重跑映射台）：``fill_prop_segment_
coverage`` 对道具卡「外套」做全文检索，第6段「顾屿……又顺手把自己的外套搭
在她肩上」是唯一真正核验过的段落（provenance.anchor_segments=[6]），但
「外套」两个字作为子串又命中了第5段「陆一舟……穿着格子衬衫配牛仔外套」
（陆一舟的）与第7段「从一件厚外套的夹层里……」（温念妈妈寄来的）——三件
完全不同的东西，就因为写法里都含"外套"两个字，被合并成同一张卡的
``segment_indexes=[5, 6, 7]``。

本文件用实测代码证伪两个假设（先证伪，不要顺着假设改代码）：
- ``test_coverage_reproduces_the_real_merge_bug_before_confirmation``：单条
  "外套"提及只声明过第6段，``fill_prop_segment_coverage`` 在没有确认步骤
  （confirm-all 短路）时确实会把 5/6/7 全部并入，证明 bug 出在这个函数本身
  的字面检索，不是别处；
- 真实 asset_manifest 里该条目 provenance.anchor_segments 确实只有 [6]，说明
  5/7 是补全步骤加的，不是抽取阶段按 label/known_prop_name 合并进来的——这
  一点在任务说明里已用生产数据核实，这里只钉住"补全步骤能被确认调用正确
  拦住"这一半。

修复钉住：命中段落只是候选，新增一次批量模型确认调用，只有确认为"同一件
实物"的段号才并入；未确认/调用失败/返回不完整，都不静默并入，且打一条
``[PREP_PACK_PROP_COVERAGE_UNCONFIRMED][未拦截]`` 可见信号；没有新候选时
不发起模型调用；真实召回场景（同一件实物反复出现在多段）确认后仍然正确
并入，不因为新增确认步骤丢失召回能力。
"""
from __future__ import annotations

import asyncio

import pytest

from app.production.prep_pack import prop_segment_coverage as psc
from app.production.prep_pack.prop_segment_coverage import fill_prop_segment_coverage
from app.schemas import Prop
from app.source_excerpt import SourceSegment


def _segment(text: str) -> SourceSegment:
    return SourceSegment(segment_id="s", text=text, start_offset=0, end_offset=len(text))


def _waican_segments() -> list[SourceSegment]:
    """真实案例的三段原文（顾念长安第2集第5/6/7段，节选到足够判别归属的
    片段）。"""
    return [
        _segment("占位第1段"),
        _segment("占位第2段"),
        _segment("占位第3段"),
        _segment("占位第4段"),
        _segment(
            "傍晚，温念到社区幼儿园交接……陆一舟中等身材，略微发胖，戴一副黑框眼镜，"
            "穿着格子衬衫配牛仔外套，肩上背着一个鼓鼓囊囊的双肩包。",
        ),
        _segment(
            "夜里，顾屿家的阳台上……顾屿听完温念转述陆一舟的话，端着两杯热姜茶出来的手"
            "顿了一下，递给她一杯，又顺手把自己的外套搭在她肩上，声音放得很稳。",
        ),
        _segment(
            "第二天下午，温念翻出衣柜深处那只旧行李箱，准备整理妈妈寄来的换季衣物——"
            "厚外套、毛线围巾。从一件厚外套的夹层里，她的指尖忽然触到一张字条。",
        ),
    ]


def _waican_entry() -> dict:
    """真实 asset_manifest 里这条「外套」提及的形状：只在第6段核验过
    （provenance.anchor_segments=[6]），canonical_name/known_prop_name 绑定到
    项目物件库里那张泛名卡。"""
    return {
        "label": "外套", "canonical_name": "外套",
        "description": "顾屿的外套，被他搭在温念肩上保暖",
        "segment_indexes": [6], "source_wording": "外套",
        "provenance": {"method": "direct", "anchor_segments": [6], "anchor_phrase": "外套"},
        "plot_significant": True, "plot_significant_quote": "又顺手把自己的外套搭在她肩上",
        "known_prop_name": "外套",
    }


def _card() -> Prop:
    return Prop(name="外套", appearance_canonical="米白色灯芯绒，翻领单排扣，藏青色罗纹袖口")


# ---------------------------------------------------------------------------
# 先证伪：字面检索本身（没有确认步骤时）确实会复现真实事故
# ---------------------------------------------------------------------------


def test_coverage_reproduces_the_real_merge_bug_before_confirmation() -> None:
    """证伪环节：confirm-all 短路（等价于 2.0.13 之前"命中即合并"的行为）
    时，单条只声明过第6段的「外套」提及确实会被第5/7段的「牛仔外套」「厚
    外套」字面命中拖进来，证明 bug 出在这个函数的字面检索本身。"""
    async def _confirm_all(ids, requests, _segments, *, run_id, episode_id):
        del run_id, episode_id, _segments
        return {req_id: set(r["candidate_indexes"]) for req_id, r in zip(ids, requests)}

    segments = _waican_segments()
    payload = [_waican_entry()]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_confirm_prop_coverage_candidates", _confirm_all)
        result = asyncio.run(fill_prop_segment_coverage(payload, segments, cards=[_card()]))

    assert result[0]["segment_indexes"] == [5, 6, 7], (
        "字面检索把陆一舟的牛仔外套、顾屿的外套、妈妈的厚外套全部并入同一条，"
        "这就是修复前的真实缺陷"
    )


# ---------------------------------------------------------------------------
# 修复后：候选只有经确认才并入
# ---------------------------------------------------------------------------


def test_candidates_not_confirmed_are_not_merged() -> None:
    """模型正确判断第5/7段不是同一件实物（same_object=false）：segment_
    indexes 保持只有已核验的第6段，不把陆一舟/妈妈的东西并进来。"""
    async def fake_call(**kwargs):
        return psc._PropCoverageConfirmResponse(verdicts=[
            {"prop_id": "p0", "segment_index": 5, "same_object": False},
            {"prop_id": "p0", "segment_index": 7, "same_object": False},
        ])

    segments = _waican_segments()
    payload = [_waican_entry()]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_call_structured", fake_call)
        result = asyncio.run(fill_prop_segment_coverage(payload, segments, cards=[_card()]))

    assert result[0]["segment_indexes"] == [6], "两个候选都被判定不是同一件实物，不应并入"


def test_selectively_confirmed_candidate_is_merged_the_other_is_not() -> None:
    """同一批候选里允许有的确认、有的不确认——不是整条道具要么全并要么全不并。"""
    async def fake_call(**kwargs):
        return psc._PropCoverageConfirmResponse(verdicts=[
            {"prop_id": "p0", "segment_index": 5, "same_object": False},
            {"prop_id": "p0", "segment_index": 7, "same_object": True},
        ])

    segments = _waican_segments()
    payload = [_waican_entry()]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_call_structured", fake_call)
        result = asyncio.run(fill_prop_segment_coverage(payload, segments, cards=[_card()]))

    assert result[0]["segment_indexes"] == [6, 7]


def test_no_new_candidates_never_calls_the_model() -> None:
    """没有新候选时不发起模型调用——结构上就是同步的纯字面检索。"""
    async def explode(**kwargs):
        raise AssertionError("不该有新候选却仍然发起了模型调用")

    segments = [_segment("温念拿起手机看了一眼。")]
    payload = [{
        "label": "手机", "canonical_name": None, "description": "一部手机",
        "segment_indexes": [1], "source_wording": "手机",
        "provenance": {"method": "direct", "anchor_segments": [1], "anchor_phrase": "手机"},
        "plot_significant": False, "plot_significant_quote": "", "known_prop_name": "",
    }]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_call_structured", explode)
        result = asyncio.run(fill_prop_segment_coverage(payload, segments, cards=()))

    assert result[0]["segment_indexes"] == [1]


# ---------------------------------------------------------------------------
# 失败/不完整响应：不静默并入，产出可见信号
# ---------------------------------------------------------------------------


def test_call_failure_does_not_merge_and_logs_visible_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def explode(**kwargs):
        raise RuntimeError("provider down")

    segments = _waican_segments()
    payload = [_waican_entry()]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_call_structured", explode)
        with caplog.at_level("WARNING"):
            result = asyncio.run(fill_prop_segment_coverage(payload, segments, cards=[_card()]))

    assert result[0]["segment_indexes"] == [6], "调用失败时未确认候选一律不并入"
    assert "[PREP_PACK_PROP_COVERAGE_UNCONFIRMED][未拦截]" in caplog.text


def test_incomplete_response_does_not_merge_the_missing_pair_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """模型只答了候选里的一部分——缺席的那组不能被当成"确认通过"悄悄放过，
    也不能让已经答过的那组被连带拖累（结构性核验，不是全有全无）。"""
    async def fake_call(**kwargs):
        return psc._PropCoverageConfirmResponse(verdicts=[
            {"prop_id": "p0", "segment_index": 7, "same_object": True},
            # 第5段这组候选完全缺席响应。
        ])

    segments = _waican_segments()
    payload = [_waican_entry()]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_call_structured", fake_call)
        with caplog.at_level("WARNING"):
            result = asyncio.run(fill_prop_segment_coverage(payload, segments, cards=[_card()]))

    assert result[0]["segment_indexes"] == [6, 7], "已确认的第7段仍然并入"
    assert "[PREP_PACK_PROP_COVERAGE_UNCONFIRMED][未拦截]" in caplog.text


def test_verdict_outside_the_offered_candidates_is_ignored() -> None:
    """模型编造了候选之外的 (prop_id, segment_index) 组合——结构性丢弃，
    不信任，也不因此报错崩溃。"""
    async def fake_call(**kwargs):
        return psc._PropCoverageConfirmResponse(verdicts=[
            {"prop_id": "p0", "segment_index": 99, "same_object": True},
            {"prop_id": "does-not-exist", "segment_index": 5, "same_object": True},
        ])

    segments = _waican_segments()
    payload = [_waican_entry()]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_call_structured", fake_call)
        result = asyncio.run(fill_prop_segment_coverage(payload, segments, cards=[_card()]))

    assert result[0]["segment_indexes"] == [6]


# ---------------------------------------------------------------------------
# 召回不能丢：真正同一件实物反复出现时，确认后仍然正确并入
# ---------------------------------------------------------------------------


def test_genuine_recurring_prop_is_still_recalled_after_confirmation() -> None:
    """真实案例（第1集手机在第 7/16/20/24/54 段反复出现）：候选确认机制
    不能把这类真实召回也一并拦掉——模型确认候选确实是同一件实物时，照常
    并入。"""
    segments = [
        _segment(f"第{i}段：天色渐暗，走廊很安静。") for i in range(1, 55)
    ]
    segments[6] = _segment("温念从包里摸出手机，看了一眼又放回去。")  # 第7段
    segments[15] = _segment("手机在桌上震动了一下，她没有去碰。")  # 第16段
    segments[19] = _segment("他低头编辑着手机里那条没发出去的消息。")  # 第20段
    payload = [{
        "label": "手机", "canonical_name": None, "description": "一部手机",
        "segment_indexes": [7], "source_wording": "手机",
        "provenance": {"method": "direct", "anchor_segments": [7], "anchor_phrase": "手机"},
        "plot_significant": False, "plot_significant_quote": "", "known_prop_name": "",
    }]

    async def fake_call(**kwargs):
        return psc._PropCoverageConfirmResponse(verdicts=[
            {"prop_id": "p0", "segment_index": 16, "same_object": True},
            {"prop_id": "p0", "segment_index": 20, "same_object": True},
        ])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_call_structured", fake_call)
        result = asyncio.run(fill_prop_segment_coverage(payload, segments, cards=()))

    assert result[0]["segment_indexes"] == [7, 16, 20]


# ---------------------------------------------------------------------------
# stage_key / 调用形状：照抄 prop_recheck.py 的写法
# ---------------------------------------------------------------------------


def test_confirm_call_uses_the_prep_pack_stage_key_prefix() -> None:
    seen: dict[str, str] = {}

    async def fake_call(**kwargs):
        seen["step_key"] = kwargs.get("step_key") or ""
        seen["stage_key"] = (kwargs.get("call_meta") or {}).get("stage_key") or ""
        return psc._PropCoverageConfirmResponse(verdicts=[])

    segments = _waican_segments()
    payload = [_waican_entry()]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psc, "_call_structured", fake_call)
        asyncio.run(fill_prop_segment_coverage(payload, segments, cards=[_card()]))

    assert seen["step_key"] == "episode_prep_pack_prop_coverage_confirm"
    assert seen["stage_key"] == "episode_prep_pack_prop_coverage_confirm"
