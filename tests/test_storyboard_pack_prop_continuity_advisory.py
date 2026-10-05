"""``storyboard_pack._segment_content_advisories`` 接线：continuity_memo.props[].name
的代码核验（``storyboard_prop_continuity.prop_name_advisories``）确实被调用到——
单独开一个文件而不是加进 ``tests/test_storyboard_pack.py``，因为该文件已逼近
500 行测试文件基线（``app/FILE_CONVENTIONS.toml`` 棘轮，零余量），新测试不应该
把自己的欠账记到那份基线上。
"""
from __future__ import annotations

from app.production.storyboard_continuity_memo import _AiCharacterState, _AiContinuityMemo, _AiPropState
from app.production.storyboard_pack import _AiResourceProp, _AiSegmentResources, _AiStoryboardSegmentDraft, _segment_content_advisories


def _draft(*, props, resource_props, wardrobes=()) -> _AiStoryboardSegmentDraft:
    characters = [_AiCharacterState(identity_id=f"id_{i}", wardrobe=w) for i, w in enumerate(wardrobes)]
    return _AiStoryboardSegmentDraft(
        prompt_text="镜头1：画面。", shot_count=3,
        continuity_memo=_AiContinuityMemo(time_of_day="白天", props=props, characters=characters),
        resources=_AiSegmentResources(props=resource_props),
    )


def test_segment_content_advisories_flags_prop_name_not_in_resources_or_wardrobe():
    draft = _draft(props=[_AiPropState(name="插座与插头")], resource_props=[_AiResourceProp(label="深卡其色行李箱")])
    advisories = _segment_content_advisories(
        draft, source_segment_indexes=[1], manifest=None,
        emotional_turns_here=(), foreshadowing_here=(), prop_entrances_here=(), prop_locks_here=(),
    )
    assert any("[STORYBOARD_PROP_CONTINUITY_NAME_UNKNOWN][未拦截]" in a and "插座与插头" in a for a in advisories)


def test_segment_content_advisories_silent_when_prop_name_matches_resource_label():
    draft = _draft(props=[_AiPropState(name="深卡其色行李箱")], resource_props=[_AiResourceProp(label="深卡其色行李箱")])
    advisories = _segment_content_advisories(
        draft, source_segment_indexes=[1], manifest=None,
        emotional_turns_here=(), foreshadowing_here=(), prop_entrances_here=(), prop_locks_here=(),
    )
    assert not any("STORYBOARD_PROP_CONTINUITY_NAME_UNKNOWN" in a for a in advisories)


def test_segment_content_advisories_silent_when_prop_name_matches_wardrobe_text():
    draft = _draft(props=[_AiPropState(name="围巾")], resource_props=[], wardrobes=["颈间绕着深灰色围巾"])
    advisories = _segment_content_advisories(
        draft, source_segment_indexes=[1], manifest=None,
        emotional_turns_here=(), foreshadowing_here=(), prop_entrances_here=(), prop_locks_here=(),
    )
    assert not any("STORYBOARD_PROP_CONTINUITY_NAME_UNKNOWN" in a for a in advisories)
