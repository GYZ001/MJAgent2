"""角色级保存只校验被改的角色（2026-09-15《龙猫出爪》：改「散散」被「阿凯外观 10 字」拦住）。"""
from __future__ import annotations

from app.domain.bible_ops.edit import _errors_for_character


def test_other_characters_errors_do_not_block_this_save() -> None:
    errors = [
        "characters[3](阿凯).appearance_canonical 长度 10 字，要求 20~80 字",
        "characters[5](散散).appearance_canonical 长度 4 字，要求 20~80 字",
        "world.visual_style_canonical 不能为空",
    ]
    assert _errors_for_character(errors, 5) == [
        "characters[5](散散).appearance_canonical 长度 4 字，要求 20~80 字",
        "world.visual_style_canonical 不能为空",
    ]
    assert _errors_for_character(errors, 1) == ["world.visual_style_canonical 不能为空"]
    assert _errors_for_character([], 0) == []
