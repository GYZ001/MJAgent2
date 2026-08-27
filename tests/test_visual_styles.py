from app.visual_styles import (
    VISUAL_STYLE_PRESETS,
    is_photographic_style_prompt,
    visual_style_prompt,
)


def test_visual_style_presets_define_complete_positive_render_contracts() -> None:
    for preset in VISUAL_STYLE_PRESETS:
        assert preset.name.strip()
        assert preset.description.strip()
        assert preset.prompt.strip()
        assert preset.sample_image.strip()
        assert "非真人照片" in preset.prompt

    assert "东方仙侠" not in visual_style_prompt("国漫电影风")
    assert "虚构数字角色" in visual_style_prompt("国漫电影风")


def test_visual_style_presets_reduced_to_four_with_two_photographic() -> None:
    """负责人拍板缩减到 4 条：真人摄影风/精修真人风/国漫电影风（默认）/古典水墨风。"""
    names = [preset.name for preset in VISUAL_STYLE_PRESETS]
    assert names == ["真人摄影风", "精修真人风", "国漫电影风", "古典水墨风"]

    photographic_names = {p.name for p in VISUAL_STYLE_PRESETS if p.photographic}
    assert photographic_names == {"真人摄影风", "精修真人风"}


def test_is_photographic_style_prompt_matches_by_resolved_prompt_text() -> None:
    """按解析后的 prompt 串逐字匹配，而不是按名称——世界书落库后只保留 prompt。"""
    assert is_photographic_style_prompt(visual_style_prompt("真人摄影风")) is True
    assert is_photographic_style_prompt(visual_style_prompt("精修真人风")) is True
    assert is_photographic_style_prompt(visual_style_prompt("国漫电影风")) is False
    assert is_photographic_style_prompt(visual_style_prompt("古典水墨风")) is False
    # 自由文本/历史遗留画风/已下线预设的旧值一律按非摄影处理（保守默认，不误判）。
    assert is_photographic_style_prompt("超写实风") is False
    assert is_photographic_style_prompt("") is False
    assert is_photographic_style_prompt(None) is False
