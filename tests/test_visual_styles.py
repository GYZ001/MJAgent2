from app.visual_styles import VISUAL_STYLE_PRESETS, visual_style_prompt


def test_visual_style_presets_are_non_human_and_genre_neutral_by_default() -> None:
    forbidden = ("真人照片", "真人实拍", "真实人物建模")
    for preset in VISUAL_STYLE_PRESETS:
        assert "非真人照片" in preset.prompt
        assert not any(
            token in preset.prompt.replace("非真人照片", "")
            for token in forbidden
        )

    assert "东方仙侠" not in visual_style_prompt("超写实风")
    assert "虚构数字角色" in visual_style_prompt("超写实风")
