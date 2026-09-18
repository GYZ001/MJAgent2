"""场景复核（``app.production.prep_pack.scene_recheck``）的契约与容错。

这一轮是实测驱动出来的，完整轨迹见 scene_recheck 模块文档。要点：判据必须是
「摄影机置身在哪个空间」，不是「画面里看得见什么」——龙猫出爪 EP6 段 6/9/10 机位
都在前台室内，正文写「门口的玻璃上多了一张海报」「看见门口那张海报」，按「看得见」
判会把这三段收进门口场景，让室内戏挂上室外全景参考图。改用机位判据后，同一份原文
连跑 8 次：段 14 的格局镜命中 6 次，室内段误收 0 次。

误收比漏报危险得多——漏一个只是少一张参考图，误收是把错误的归属当成正确结果交付。
所以这里锁的三件事全部围绕「不许错」，不围绕「尽量多报」。
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError


from app.production.prep_pack import scene_recheck


def test_enum_reaches_the_response_format_actually_sent() -> None:
    """enum 必须出现在**真正下发的 response_format** 里，不是某个辅助函数的返回值。

    2026-09-18 实测的真实缺陷：第一版把 enum 放进 ``_call_structured`` 的
    ``output_schema`` 形参，而 model_gateway 只在格式/语义修复重试时把它塞进提示词
    文本，首次调用的请求体里压根没有——供应商侧毫无约束，而当时那条测试只验了辅助
    函数的返回值，照样全绿。判据必须挂在「这件事成没成」上：断言走完
    ``response_model -> _response_format`` 这条真实路径之后 enum 还在。
    """
    import json

    from app.production.prep_pack.schemas import _response_format

    def field_schema(names: list[str]) -> dict:
        sent = _response_format(
            scene_recheck.response_model(names), "episode_prep_pack_scene_recheck_v1")
        assert sent["json_schema"]["strict"] is True
        defs = sent["json_schema"]["schema"].get("$defs") or {}
        assert defs, "动态模型必须把 mention 定义放进 $defs"
        return next(iter(defs.values()))["properties"]["display_name"]

    # 多值渲染成 enum，单值渲染成 const——断言「值域被钉死」而不是「出现了 enum 这个词」。
    # 只认字面 enum 的话，场景库恰好只有一个条目时会误判成「约束丢了」（2026-09-18 实测）。
    multi = field_schema(["晚安宠物医院门口", "宠物医院前台"])
    assert multi.get("enum") == ["晚安宠物医院门口", "宠物医院前台"]
    single = field_schema(["晚安宠物医院门口"])
    assert single.get("const") == "晚安宠物医院门口" or single.get("enum") == ["晚安宠物医院门口"]
    assert json.dumps(multi) and "string" == multi.get("type")


def test_response_model_rejects_names_outside_the_library() -> None:
    """动态模型本身就拒绝清单外的名字——这是 schema 层的第一道，不是唯一一道。"""
    model = scene_recheck.response_model(["晚安宠物医院门口"])
    model(scenes=[{"display_name": "晚安宠物医院门口", "segment_indexes": [14], "quote": "x"}])
    with pytest.raises(ValidationError):
        model(scenes=[{"display_name": "我自己编的地方", "segment_indexes": [14], "quote": "x"}])


def test_prompt_separates_camera_position_from_what_is_visible() -> None:
    """提示词必须把「机位在哪」与「画面里看得见什么」分开。

    删掉这个区分，EP6 段 6/9/10 那类「室内望见门口海报」的段落会重新被收进门口
    场景——实测按「拍到」判时 6 次里误收 4 次。
    """
    prompt = scene_recheck._prompt("（原文）", ["晚安宠物医院门口"])
    assert "摄影机置身在哪个空间" in prompt
    # 不算的情形要逐条写出来，只说「要准确」没有可执行性
    assert "窗外的招牌、门上的海报" in prompt
    assert "镜头如果就架在那个地点里，算；镜头只是从别处望见它，不算" in prompt
    # 没有角色的编号同样有场景——段 14 是「人物：无」的格局镜
    assert "「人物：无」不等于「没有场景」" in prompt


def test_empty_scene_library_skips_the_call_entirely() -> None:
    """没有已登记场景时直接跳过：空 enum 是非法 schema，而且那种情况无从复核。

    不跳过的话会发出一个 enum 为空的 schema，供应商侧报错，整条抽取链路跟着失败——
    而本该发生的只是「这一集还没有场景库，没什么可补」。
    """
    added = asyncio.run(scene_recheck.recheck_chunk_scenes(
        episode_id="ep1", chunk_index=1, chunk=[], known_scenes=[], run_id=None,
    ))
    assert added == []


def test_recheck_failure_never_breaks_the_extraction(monkeypatch) -> None:
    """补漏失败只记日志，原样交回抽取结果。

    复核是增强不是门禁：它挂了不能把整个映射包拖垮。反过来，抽取自身的失败仍要
    照常抛出——这里吞的是「补漏没补成」，不是「抽取失败」。
    """
    class _Boom(Exception):
        pass

    async def explode(**kwargs):
        raise _Boom("provider down")

    monkeypatch.setattr(scene_recheck, "recheck_chunk_scenes", explode)

    class _Mention:
        display_name = "宠物医院前台"
        segment_indexes = [4]

    class _Response:
        scenes = [_Mention()]

    original = _Response()
    result = asyncio.run(scene_recheck.attach_scene_recheck(
        original, chunk=[], chunk_index=1, episode_id="ep1",
        known_scenes=["宠物医院前台"], run_id=None,
    ))
    assert result is original, "复核失败必须原样交回抽取结果，不能丢数据"
    assert [m.segment_indexes for m in result.scenes] == [[4]]


def test_recheck_result_is_union_never_removes_declared(monkeypatch) -> None:
    """复核只回答「还漏了哪些」，从不否定抽取已申报的条目。"""
    async def fake(**kwargs):
        return [{
            "display_name": "晚安宠物医院门口", "suspected_true_name": None,
            "segment_indexes": [14], "quote": "从门口升到街道",
        }]

    monkeypatch.setattr(scene_recheck, "recheck_chunk_scenes", fake)

    from app.production.prep_pack.schemas import _ModelSceneMention

    response = type("R", (), {})()
    response.scenes = [_ModelSceneMention(
        display_name="人间老街区", suspected_true_name=None,
        segment_indexes=[14], quote="老街清晨",
    )]
    result = asyncio.run(scene_recheck.attach_scene_recheck(
        response, chunk=[], chunk_index=1, episode_id="ep1",
        known_scenes=["晚安宠物医院门口"], run_id=None,
    ))
    by_name = {m.display_name: sorted(m.segment_indexes) for m in result.scenes}
    assert by_name["人间老街区"] == [14], "抽取报的条目必须原样保留"
    assert by_name["晚安宠物医院门口"] == [14], "复核补的条目要并进来"


def test_recheck_drops_names_outside_the_registered_list(monkeypatch) -> None:
    """enum 之外的名字一律丢弃，不做近似匹配。

    近似匹配会把模型的自造名硬塞给某个登记场景——那正是「兜底填充」，比留空危险。
    """
    async def fake_call(**kwargs):
        class _M:
            def __init__(self, name, idx):
                self.display_name = name
                self.segment_indexes = idx
                self.quote = "x"

        class _R:
            scenes = [_M("医院门口旁边那条街", [14])]

        return _R()

    monkeypatch.setattr(scene_recheck, "_call_structured", fake_call)
    segment = type("Seg", (), {"text": "（格局镜：从门口升到街道。）"})()
    added = asyncio.run(scene_recheck.recheck_chunk_scenes(
        episode_id="ep1", chunk_index=1, chunk=[(14, segment)],
        known_scenes=["晚安宠物医院门口"], run_id=None,
    ))
    assert added == []


def test_prompt_pushes_past_the_first_match() -> None:
    """提示词必须给出「先假定多场景、再逐个排除」的思考顺序。

    2026-09-18 实测：去掉这句之后连跑 4 次全部漏报，而且失败形状完全一致——模型每次
    都正确认出段 13 是门口，然后就收工了。它不是判错，是找到一个就停。判据说清了
    「一个编号可以置身多个空间」还不够，得连搜索顺序一起给。
    """
    prompt = scene_recheck._prompt("（原文）", ["晚安宠物医院门口"])
    assert "先假定每个编号可能有不止一个场景，再逐个排除" in prompt
    assert "格局镜" in prompt and "转场镜" in prompt


def test_prompt_does_not_feed_back_already_declared_names() -> None:
    """提示词不再塞「已申报清单」——那是这轮真实漏报的根源。

    抽取报原文写法「人间·医院门口」而候选清单是登记名「晚安宠物医院门口」，两套名字
    并排摆着，模型合理地认为已经报过了，交回 {"scenes": []}。改成独立标注后没有这个
    歧义，重复由代码做并集消化。
    """
    prompt = scene_recheck._prompt("（原文）", ["晚安宠物医院门口"])
    assert "上一轮已经申报" not in prompt
    assert "这是一次独立标注" in prompt
