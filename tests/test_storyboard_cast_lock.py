"""同框人物清单锁定的确定性回填（app.production.storyboard_cast_lock）。

2026-09-28 真实回归：《顾念长安》第 1 集第 9 段背景自动出现撞脸路人与陌生女性、真顾屿却不在
温念桌边，第 8 段也冒出未铺垫的顾客——全片贯穿约束模板里的「人数锁定」29/30 段逐字相同、不
含实际人数，没有约束力。判据从数据推导：直接读本段 resources.characters 的 visibility 字段。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_cast_lock import ensure_cast_lock_in_prompt


def _character(identity_id: str, display_name: str, *, visible: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        identity_id=identity_id, display_name=display_name,
        visibility="visible" if visible else "voice_only",
    )


def _draft(prompt_text: str, characters: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(prompt_text=prompt_text, resources=SimpleNamespace(characters=characters))


def test_appends_lock_sentence_with_actual_names_and_count():
    """红：修复前的旧行为是全片贯穿约束里逐字相同的固定模板，不含实际人数——用手写的
    旧版本函数（永远返回空模板）证明它测不出「路人闯入」这类问题；绿：新函数按数据算出
    人数与正名。"""
    draft = _draft(
        "镜头1：温念坐在桌边，顾屿走近。",
        [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text.endswith("画面中只有@温念、@顾屿共2人，不出现其他人物或路人。")

    def _old_fixed_template_lock(_draft) -> None:
        """修复前的旧行为：全片贯穿约束模板里的固定一句，不读任何实际数据。"""
        _draft.prompt_text = _draft.prompt_text.rstrip() + "\n人数锁定：画面中只有我方人物，不出现其他人物。"

    red_draft = _draft(
        "镜头1：温念坐在桌边，顾屿走近。",
        [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")],
    )
    _old_fixed_template_lock(red_draft)
    assert "@温念" not in red_draft.prompt_text and "@顾屿" not in red_draft.prompt_text
    assert "只有我方人物" in red_draft.prompt_text, "旧模板确实不含实际人数——红态验证成立"


def test_does_not_duplicate_when_already_present():
    draft = _draft(
        "镜头1：@温念 坐在桌边。画面中只有@温念共1人，不出现其他人物或路人。",
        [_character("bible:温念", "温念")],
    )
    before = draft.prompt_text
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == before


def test_segment_without_visible_characters_is_left_untouched():
    """纯画外音/旁白段：没有可见角色，不写这句话——"无可见角色"本身是诚实的事实。"""
    draft = _draft("镜头1：空镜，风声。", [_character("bible:温念", "温念", visible=False)])
    assert ensure_cast_lock_in_prompt(draft) == []
    assert "画面中只有" not in draft.prompt_text


def test_empty_prompt_text_is_left_untouched():
    draft = _draft("", [_character("bible:温念", "温念")])
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == ""


def test_three_visible_characters_dedupes_and_preserves_order():
    draft = _draft(
        "镜头1：三人对峙。",
        [
            _character("bible:温念", "温念"),
            _character("bible:顾屿", "顾屿"),
            _character("bible:温念", "温念"),  # 重复条目
            _character("bible:林姐", "林姐"),
        ],
    )
    ensure_cast_lock_in_prompt(draft)
    assert draft.prompt_text.endswith("画面中只有@温念、@顾屿、@林姐共3人，不出现其他人物或路人。")
