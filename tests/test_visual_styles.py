from app.visual_styles import VISUAL_STYLE_PRESETS, visual_style_prompt


def test_visual_style_presets_define_complete_positive_render_contracts() -> None:
    for preset in VISUAL_STYLE_PRESETS:
        assert preset.name.strip()
        assert preset.description.strip()
        assert preset.prompt.strip()
        assert "非真人照片" in preset.prompt

    assert "东方仙侠" not in visual_style_prompt("超写实风")
    assert "虚构数字角色" in visual_style_prompt("超写实风")
