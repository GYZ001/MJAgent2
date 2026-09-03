"""WS2-D/E 接线验收。

D：``_segment_content_advisories`` 的资源判据统一改走 ``app.validators.
resource_advisories_for_segment``（与生成台 ``manifest_production_blockers``
同判据）。``manifest`` 是调用方必传的显式关键字（无默认值，CLAUDE.md
「Ownership Must Be Explicit」：可选参数是缺陷温床）——``manifest=None``
（bible 缺失/解析不到）时该判据本身按最严处理（``manifest_production_
blockers`` 对非 dict manifest 返回"依赖 manifest 缺失"，verdict=WILL_BLOCK），
不是本函数另开一条回退文案；manifest 是"存在但没有任何条目"的空 dict 时才
走 TEXT_ONLY_FALLBACK（未拦截）。见 app/production/storyboard_pack.py
``_segment_content_advisories`` 与 ``_generate_all_segment_prompts`` 的
WS8 接线注释。

E：分镜台 2.0.0 段落 schema（``_AiStoryboardSegmentDraft``/
``StoryboardPackSegment``）新增 ``form``/``beats``，``persist_storyboard_pack``
写进 ``shots.shot_contract_json.storyboard_pack_segment``，
``app.continuity.apply_shot_contract`` 把它们提升成 ``Shot.form``/
``Shot.beats`` 顶层字段——校验器（``app.validators.storyboard_montage``）与
``app.domain.video_ops.confirmation_eval`` 读的都是 Shot 顶层字段，不是嵌套
在 storyboard_pack_segment 里的原始 dict。
"""
from __future__ import annotations

import pytest

from app.continuity import apply_shot_contract
from app.production.storyboard_pack import (
    _AiContinuityMemo,
    _AiDialogueLine,
    _AiSegmentResources,
    _AiStoryboardSegmentDraft,
    _segment_content_advisories,
)
from app.schemas import MontageBeat, Shot


def _draft(**overrides) -> _AiStoryboardSegmentDraft:
    base = dict(
        prompt_text="占位提示词。",
        shot_count=3,
        dialogue=[_AiDialogueLine(speaker_identity_id="bible:孟浩", line="走吧", source_segment_index=1)],
        resources=_AiSegmentResources(
            characters=[{"identity_id": "bible:孟浩", "description": "占位"}],
        ),
        continuity_memo=_AiContinuityMemo(time_of_day="白天"),
    )
    base.update(overrides)
    return _AiStoryboardSegmentDraft(**base)


def test_manifest_required_keyword_has_no_default():
    """manifest 必须是显式关键字——漏传即 TypeError，调用点不能靠默认值蒙混
    过去（CLAUDE.md「Ownership Must Be Explicit」，真实教训 conn=None）。"""
    with pytest.raises(TypeError):
        _segment_content_advisories(_draft(), source_segment_indexes=[1])


def test_manifest_none_is_treated_as_missing_and_blocks():
    """manifest=None 是"bible 缺失/解析不到"这一显式状态，判据本身按最严
    处理——不是本函数另开一条回退文案，而是与生成台
    app.media_exec.reference_pool_gate._reference_pool_blockers 同一处理
    （非 dict manifest 视为"依赖 manifest 缺失"）。"""
    advisories = _segment_content_advisories(
        _draft(), source_segment_indexes=[1], manifest=None,
    )
    blocked = [a for a in advisories if "STORYBOARD_PACK_RESOURCE_CHARACTER_BLOCKED" in a]
    assert blocked, "manifest 缺失必须按 [拦截] 处理，不能静默放行"
    assert "[拦截]" in blocked[0]
    assert "manifest 缺失" in blocked[0] or "依赖 manifest 缺失" in blocked[0]


def test_manifest_present_but_empty_falls_back_to_text_only_unknown():
    """manifest 存在但没有任何条目（本集资产解析真的一无所获，区别于"没
    传"）：走 TEXT_ONLY_FALLBACK，未拦截，退化成 CHARACTER_UNKNOWN 文案——
    真实 EP7 回归的现代等价物（见 test_storyboard_pack.py 同名历史用例）。"""
    manifest = {"characters": [], "scene": None, "additional_scenes": []}
    advisories = _segment_content_advisories(
        _draft(), source_segment_indexes=[1], manifest=manifest,
    )
    unknown = [a for a in advisories if "STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN" in a]
    assert unknown
    assert "[未拦截]" in unknown[0]


def test_manifest_present_routes_through_shared_resource_advisories():
    """manifest 里孟浩已注册但没有 selected_view_ids，其它人物本该有资产
    （asset_required=True）——verdict=OK，孟浩自己的缺口走 UNKNOWN（未拦截）。"""
    manifest = {
        "characters": [{"name": "孟浩", "asset_required": True, "selected_view_ids": []}],
        "scene": None, "additional_scenes": [],
    }
    advisories = _segment_content_advisories(
        _draft(), source_segment_indexes=[1], manifest=manifest,
    )
    character_advisories = [a for a in advisories if "STORYBOARD_PACK_RESOURCE_CHARACTER" in a]
    assert character_advisories, "manifest 判定孟浩缺资产时必须留下可见信号"
    assert not any("不是映射台已知的人物身份" in a for a in advisories), (
        "改走共享判据后不应再出现旧硬编码文案"
    )


def test_manifest_present_with_asset_available_stays_silent():
    """manifest 里孟浩已有 selected_view_ids，说明真实有参考图可用，不该报告。"""
    manifest = {
        "characters": [{"name": "孟浩", "asset_required": True, "selected_view_ids": ["v1"]}],
        "scene": None, "additional_scenes": [],
    }
    advisories = _segment_content_advisories(
        _draft(), source_segment_indexes=[1], manifest=manifest,
    )
    assert not any("STORYBOARD_PACK_RESOURCE_CHARACTER" in a for a in advisories)


# ---------------------------------------------------------------------------
# WS7：form/beats schema + 持久化提升
# ---------------------------------------------------------------------------

def test_segment_draft_defaults_to_scene_form_with_no_beats():
    draft = _draft()
    assert draft.form == "scene"
    assert draft.beats == []


def test_segment_draft_accepts_montage_form_with_beats():
    draft = _draft(form="montage", beats=[
        MontageBeat(time_anchor="我八岁", visual="八岁的自己打针", source_span="我八岁的时候被诊断出长不高"),
        MontageBeat(time_anchor="我三十五岁", visual="把奖杯抱在怀里", source_span="把它抱在怀里"),
    ])
    assert draft.form == "montage"
    assert len(draft.beats) == 2


def test_apply_shot_contract_promotes_form_and_beats_to_shot_top_level():
    """真实持久化形状：shot_contract_json.storyboard_pack_segment.{form,beats}
    必须被提升成 shot.form/shot.beats——校验器/生成台读的是顶层字段。"""
    shot = Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="",
        action_desc="占位", prompt_contract_version="storyboard_pack.v1",
    )
    contract = {
        "storyboard_pack_segment": {
            "form": "montage",
            "beats": [
                {"time_anchor": "我八岁", "scene_name": "", "visual": "占位", "source_span": "我八岁"},
            ],
        },
    }
    apply_shot_contract(shot, contract)
    assert shot.form == "montage"
    assert len(shot.beats) == 1
    assert shot.beats[0].time_anchor == "我八岁"


def test_apply_shot_contract_without_segment_leaves_form_at_default():
    shot = Shot(shot_no=1, duration_s=15, shot_size="近景", camera_move="固定", action_desc="占位")
    apply_shot_contract(shot, {"purpose": "占位"})
    assert shot.form == "scene"
    assert shot.beats == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
