"""服装状态延续的确定性回填（``ensure_wardrobe_continuity_in_prompt``），与
``ensure_travel_direction_in_prompt`` 同一形状。

2026-09-28 真实回归（《顾念长安》第 1 集）：围巾第 14 段由顾屿给温念围上，第 16、18 段整段
消失；开衫扣子第 11 段扣好、第 12 段敞开——wardrobe 字段此前只落库、只在 log 级别提示，从不
回填进提示词。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_continuity_memo import (
    _AiCharacterState,
    _AiContinuityMemo,
    ensure_wardrobe_continuity_in_prompt,
)


class _FakeProp:
    def __init__(self, label: str, description: str = "") -> None:
        self.label = label
        self.description = description


def _draft(prompt_text: str, *, characters, resource_characters, props=None) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_text=prompt_text,
        continuity_memo=_AiContinuityMemo(time_of_day="白日", characters=characters),
        resources=SimpleNamespace(characters=resource_characters, props=list(props or [])),
    )


def _visible(identity_id: str, display_name: str) -> SimpleNamespace:
    return SimpleNamespace(identity_id=identity_id, display_name=display_name, visibility="visible")


def test_appends_wardrobe_sentence_and_registers_prop_when_missing():
    """红：修复前的旧行为是只记日志、什么都不回填——用手写的旧版本函数证明视频模型
    读不到围巾状态；绿：新函数把 wardrobe 写进提示词并登记进 resources.props。"""
    draft = _draft(
        "镜头1：温念低头看信。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="围着顾屿给的红色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    assert ensure_wardrobe_continuity_in_prompt(draft, prop_factory=_FakeProp) == []
    assert draft.prompt_text.endswith("续接服装：@温念 围着顾屿给的红色围巾。")
    assert len(draft.resources.props) == 1
    assert draft.resources.props[0].label == "温念的服装"
    assert draft.resources.props[0].description == "围着顾屿给的红色围巾"

    def _old_log_only_wardrobe(_draft) -> None:
        """修复前的旧行为：wardrobe 只在 log 级别提示，从不回填提示词或 resources.props。"""
        return None

    red_draft = _draft(
        "镜头1：温念低头看信。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="围着顾屿给的红色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    _old_log_only_wardrobe(red_draft)
    assert "围巾" not in red_draft.prompt_text and red_draft.resources.props == [], "旧行为确实不回填——红态验证成立"


def test_does_not_duplicate_when_prompt_already_mentions_wardrobe():
    draft = _draft(
        "镜头1：@温念 围着顾屿给的红色围巾走进屋。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="围着顾屿给的红色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    before = draft.prompt_text
    assert ensure_wardrobe_continuity_in_prompt(draft, prop_factory=_FakeProp) == []
    assert draft.prompt_text == before
    assert len(draft.resources.props) == 1, "提示词已提到但 resources.props 里仍要补登记"


def test_does_not_duplicate_prop_entry_when_already_registered():
    draft = _draft(
        "镜头1：温念低头看信。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="围着顾屿给的红色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
        props=[_FakeProp("温念的红围巾", "围着顾屿给的红色围巾")],
    )
    ensure_wardrobe_continuity_in_prompt(draft, prop_factory=_FakeProp)
    assert len(draft.resources.props) == 1


def test_empty_wardrobe_or_no_display_name_is_skipped():
    draft = _draft(
        "镜头1：温念低头看信。",
        characters=[
            _AiCharacterState(identity_id="bible:温念", wardrobe=""),
            _AiCharacterState(identity_id="bible:未知", wardrobe="灰色外套"),
        ],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    before = draft.prompt_text
    ensure_wardrobe_continuity_in_prompt(draft, prop_factory=_FakeProp)
    assert draft.prompt_text == before
    assert draft.resources.props == []


def test_empty_prompt_or_no_characters_is_left_untouched():
    empty_prompt = _draft(
        "", characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="红围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    ensure_wardrobe_continuity_in_prompt(empty_prompt, prop_factory=_FakeProp)
    assert empty_prompt.prompt_text == ""

    no_characters = _draft("镜头1：空镜。", characters=[], resource_characters=[])
    ensure_wardrobe_continuity_in_prompt(no_characters, prop_factory=_FakeProp)
    assert no_characters.prompt_text == "镜头1：空镜。"
