"""P0-E 体貌锚点与着装/表情分离（``app.production.storyboard_physical_anchor``，
2026-09-30，真实生产回归 proj_ca86b15ab7d7 EP1 逐帧核对驱动）。

覆盖：
1. 子序列核验（``_is_char_subsequence``/``physical_anchor_verified``）——合法删减
   通过，改写/新增/打乱顺序不通过。
2. ``build_physical_anchor_overrides``/``apply_physical_anchor_overrides``——未知
   人物、核验失败、没有可核验锚点三种情形都被丢弃并记告警，原样保留完整锚点
   （不兜底改写，Required Outcome D）；核验通过的条目原地覆盖 appearance。
3. 规则文案：``physical_anchor_beat_sheet_rules``/``wardrobe_plan_beat_sheet_rules``/
   ``_WARDROBE_FIELD_RULE`` 都包含正面陈述，断言的是新增文案确实存在，不是断言
   否定词缺席——规则文本本身会引用「不再」「没有」这类词作为「不要这样写」的例子，
   断言缺席会是伪阳性。
4. 端到端接线（骨架照抄 ``tests/test_storyboard_wardrobe_plan.py`` 的
   ``test_generate_all_segment_prompts_injects_wardrobe_rule_text``）：segment
   task payload 里 ``relevant_assets.characters[].appearance`` 是体貌专用锚点、
   ``rules[]`` 里的当前着装来自全集服装表，服装不再随体貌锚点重复出现。
"""
from __future__ import annotations

import json

import pytest

from app.production.storyboard_beat_sheet_schemas import (
    _AiBeat, _AiBeatSheetDraft, _AiPhysicalAnchor, _AiSegmentPlan, _AiWardrobeState,
)
from app.production.storyboard_continuity_memo import _WARDROBE_FIELD_RULE
from app.production.storyboard_physical_anchor import (
    _is_char_subsequence,
    apply_physical_anchor_overrides,
    build_physical_anchor_overrides,
    physical_anchor_beat_sheet_rules,
    physical_anchor_verified,
)
from app.production.storyboard_wardrobe_plan import wardrobe_plan_beat_sheet_rules
from app.source_excerpt import SourceSegment

_FULL_ANCHOR = (
    "二十四岁的年轻女性，中等身高，鹅蛋脸，杏眼，乌黑顺直的长发垂到胸前，"
    "穿米白色宽松针织开衫，内搭浅蓝色细碎花棉质长裙，米白色平底单鞋，"
    "神情温柔，嘴角带着浅浅的笑。"
)
_PHYSICAL_ONLY = "二十四岁的年轻女性，中等身高，鹅蛋脸，杏眼，乌黑顺直的长发垂到胸前。"


def _payload(c1_appearance: str = _FULL_ANCHOR) -> dict:
    return {
        "asset_manifest": {
            "characters": [
                {
                    "identity_id": "bible:c1", "display_name": "温念", "aliases": [],
                    "segment_indexes": [1], "appearance": c1_appearance,
                },
                {
                    "identity_id": "bible:c2", "display_name": "顾屿", "aliases": [],
                    "segment_indexes": [1], "appearance": "",
                },
            ],
        },
    }


def _draft(*anchors: _AiPhysicalAnchor, wardrobe_plan: list[_AiWardrobeState] | None = None) -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="咖啡馆见面", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"])],
        physical_anchors=list(anchors),
        wardrobe_plan=wardrobe_plan or [],
    )


# ---------------------------------------------------------------------------
# 字符子序列核验
# ---------------------------------------------------------------------------

def test_subsequence_true_for_deletion_only_excerpt():
    assert _is_char_subsequence("ABD", "ABCD")


def test_subsequence_false_when_reordered():
    assert not _is_char_subsequence("BAD", "ABCD")


def test_subsequence_false_when_new_content_inserted():
    assert not _is_char_subsequence("ABZD", "ABCD")


def test_physical_anchor_verified_accepts_valid_excerpt():
    assert physical_anchor_verified(_PHYSICAL_ONLY, _FULL_ANCHOR)


def test_physical_anchor_verified_rejects_fabricated_text():
    fabricated = _PHYSICAL_ONLY.replace("鹅蛋脸", "圆脸")  # 「圆」不在原锚点里
    assert not physical_anchor_verified(fabricated, _FULL_ANCHOR)


def test_physical_anchor_verified_rejects_empty():
    assert not physical_anchor_verified("   ", _FULL_ANCHOR)


def test_physical_anchor_verified_ignores_whitespace_only_difference():
    spaced = " ".join(_PHYSICAL_ONLY)
    assert physical_anchor_verified(spaced, _FULL_ANCHOR)


# ---------------------------------------------------------------------------
# build/apply overrides：核验通过才覆盖，核验失败/无锚点回退完整锚点（不兜底改写）
# ---------------------------------------------------------------------------

def test_build_overrides_keeps_valid_nomination():
    draft = _draft(_AiPhysicalAnchor(identity_id="bible:c1", physical_description=_PHYSICAL_ONLY))
    overrides = build_physical_anchor_overrides(draft, _payload())
    assert overrides == {"bible:c1": _PHYSICAL_ONLY}


def test_build_overrides_drops_unknown_identity_and_logs(caplog):
    draft = _draft(_AiPhysicalAnchor(identity_id="bible:ghost", physical_description="随便写点什么"))
    with caplog.at_level("WARNING"):
        overrides = build_physical_anchor_overrides(draft, _payload())
    assert overrides == {}
    assert "bible:ghost" in caplog.text
    assert "STORYBOARD_PHYSICAL_ANCHOR_UNKNOWN" in caplog.text


def test_build_overrides_drops_fabricated_description_and_logs(caplog):
    fabricated = _PHYSICAL_ONLY.replace("鹅蛋脸", "圆脸")
    draft = _draft(_AiPhysicalAnchor(identity_id="bible:c1", physical_description=fabricated))
    with caplog.at_level("WARNING"):
        overrides = build_physical_anchor_overrides(draft, _payload())
    assert overrides == {}
    assert "STORYBOARD_PHYSICAL_ANCHOR_UNVERIFIED" in caplog.text


def test_build_overrides_drops_when_character_has_no_appearance_anchor():
    """已知人物但没有可核验的完整锚点（appearance 为空——群演/未出图角色）：
    结构上没法核验，不覆盖，不是「查了没找到」。"""
    draft = _draft(_AiPhysicalAnchor(identity_id="bible:c2", physical_description="随便写点什么"))
    overrides = build_physical_anchor_overrides(draft, _payload())
    assert overrides == {}


def test_build_overrides_empty_when_nothing_nominated():
    draft = _draft()
    assert build_physical_anchor_overrides(draft, _payload()) == {}


def test_apply_overrides_mutates_only_matched_identity():
    payload = _payload()
    apply_physical_anchor_overrides(payload, {"bible:c1": _PHYSICAL_ONLY})
    characters = {c["identity_id"]: c["appearance"] for c in payload["asset_manifest"]["characters"]}
    assert characters["bible:c1"] == _PHYSICAL_ONLY
    assert characters["bible:c2"] == ""


def test_apply_overrides_no_op_keeps_full_anchor():
    """核验失败/没有申报时的降级路径：appearance 原样保留完整锚点，行为与改造前
    完全一致（Required Outcome D）——不是拿一个编造/裁剪值顶替。"""
    payload = _payload()
    apply_physical_anchor_overrides(payload, {})
    assert payload["asset_manifest"]["characters"][0]["appearance"] == _FULL_ANCHOR


# ---------------------------------------------------------------------------
# 规则文案：正面陈述真的写了（不是断言否定词缺席——规则本身会引用它们作反例）
# ---------------------------------------------------------------------------

def test_physical_anchor_rules_instruct_deletion_only_derivation():
    rules = "".join(physical_anchor_beat_sheet_rules())
    assert "只删不改、不新增一个字" in rules
    assert "服装" in rules and "默认表情" in rules


def test_physical_anchor_rules_cover_concealed_items_positively():
    rules = "".join(physical_anchor_beat_sheet_rules())
    assert "若隐若现的轻微凸起" in rules


def test_wardrobe_plan_rule_states_change_positively():
    rules = "".join(wardrobe_plan_beat_sheet_rules())
    assert "只正面描述换装后穿着/佩戴的东西" in rules
    assert "不必专门声明它的缺席" in rules
    assert "衣服下看不见" in rules  # 首次出场那条的隐藏随身物正面陈述


def test_continuity_memo_wardrobe_rule_states_positively():
    assert "只正面描述此刻确实穿着" in _WARDROBE_FIELD_RULE
    assert "不必专门声明它的缺席" in _WARDROBE_FIELD_RULE
    assert "衣服下看不见" in _WARDROBE_FIELD_RULE


# ---------------------------------------------------------------------------
# 端到端接线：segment task payload 拿到体貌专用锚点 + 当前着装，互不覆盖
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_all_segment_prompts_gets_physical_anchor_and_current_wardrobe(monkeypatch):
    """骨架照抄 tests/test_storyboard_wardrobe_plan.py 的
    ``test_generate_all_segment_prompts_injects_wardrobe_rule_text``（fake
    chat_structured 捕获 messages[1]["content"]）。真实生产链路里
    ``apply_physical_anchor_overrides`` 在 ``generate_beat_sheet_with_drop_review``
    返回前完成（见 storyboard_physical_anchor 模块 docstring「覆盖点选在……」），
    这里手动做同一步模拟真实调用顺序，再验证 ``_generate_all_segment_prompts``
    发给模型的 relevant_assets.characters[].appearance 是体貌专用锚点、rules[]
    里的着装来自全集服装表——服装不再随体貌锚点重复出现（Required Outcome A）。
    """
    import app.production.storyboard_pack as storyboard_pack_module
    from app.production.storyboard_pack import _AiStoryboardSegmentDraft

    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        return _AiStoryboardSegmentDraft(prompt_text="提示词", shot_count=3)

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _draft(
        _AiPhysicalAnchor(identity_id="bible:c1", physical_description=_PHYSICAL_ONLY),
        wardrobe_plan=[_AiWardrobeState(
            identity_id="bible:c1", beat_id="B1",
            wardrobe="浅蓝色碎花长裙，外系鹅黄色棉布围裙", change_reason="首次出场",
        )],
    )
    payload = _payload()
    # 模拟 generate_beat_sheet_with_drop_review 返回前的那一步覆盖（真实调用顺序）。
    apply_physical_anchor_overrides(payload, build_physical_anchor_overrides(beat_draft, payload))

    source = [SourceSegment(segment_id="s1", text="温念在咖啡馆见面。", start_offset=0, end_offset=10)]

    await storyboard_pack_module._generate_all_segment_prompts(
        episode_id="ep-physical-anchor", episode_no=1, beat_draft=beat_draft, segments=source,
        payload=payload, target_video_model="hiagent", bible=None, conn=None, project_id="", aspect_ratio="9:16",
        enhance_music_bed=False, required_dialogue_by_segment_no={},
    )

    appearance_by_id = {c["identity_id"]: c["appearance"] for c in calls[0]["relevant_assets"]["characters"]}
    assert appearance_by_id["bible:c1"] == _PHYSICAL_ONLY
    assert "开衫" not in appearance_by_id["bible:c1"]  # 默认服装不再随体貌锚点出现

    rules = "".join(calls[0]["rules"])
    assert "本段着装（全集服装表）" in rules and "浅蓝色碎花长裙，外系鹅黄色棉布围裙" in rules
