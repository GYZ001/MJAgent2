"""``app.subtitles.settings``：运行时设置读取 + 字幕底边距按画幅换算
（2026-09-28 用户反馈「字幕位置太高」）。"""
from __future__ import annotations

import pytest

from app.subtitles import settings

_PORTRAIT = (1080, 1920)
_LANDSCAPE = (1920, 1080)


def test_portrait_keeps_configured_pixel_margin_unchanged():
    """竖屏行为不变：直接用监制房配置的像素值，默认 400。"""
    style = settings.style_from_settings(font_family="X", play_res=_PORTRAIT)
    assert style.margin_bottom == 400


def test_landscape_ignores_pixel_setting_and_uses_height_ratio():
    """2026-09-28 修复：横屏不再直接套用按竖屏标定的 400px（在 1080 高画布上
    是约 37% 离底），改按画面高度的 7% 换算，1080p 约 75px。"""
    style = settings.style_from_settings(font_family="X", play_res=_LANDSCAPE)
    assert style.margin_bottom == round(1080 * 0.07) == 76


def test_square_canvas_is_not_landscape_keeps_configured_pixel_margin():
    """宽==高时不算横屏（判据是 width > height），沿用配置值，不误伤方形画布。"""
    style = settings.style_from_settings(font_family="X", play_res=(1080, 1080))
    assert style.margin_bottom == 400


@pytest.mark.parametrize("play_res, expected", [((1280, 720), 50), ((3840, 2160), 151)])
def test_landscape_ratio_scales_with_actual_height_not_hardcoded_1080(play_res, expected):
    style = settings.style_from_settings(font_family="X", play_res=play_res)
    assert style.margin_bottom == expected


def test_pre_fix_hardcoded_400_would_put_subtitle_too_high_on_landscape():
    """红绿验证：修复前的行为是横竖屏共用同一个像素配置（本仓库改之前的真实
    代码，见 git 历史 app/subtitles/ass.py::SubtitleStyle.margin_bottom 默认值
    400）。在 1080 高的横屏画布上，400px 离底边距占比高达 37%，明显偏高；
    修复后的换算值只占 7%，验证确实"往下"移动了。"""
    old_hardcoded_margin = 400  # 修复前：不区分画幅，一律用配置的像素值
    old_ratio = old_hardcoded_margin / _LANDSCAPE[1]
    assert old_ratio == pytest.approx(0.37, abs=0.01)

    style = settings.style_from_settings(font_family="X", play_res=_LANDSCAPE)
    new_ratio = style.margin_bottom / _LANDSCAPE[1]
    assert new_ratio == pytest.approx(0.07, abs=0.001)
    assert style.margin_bottom < old_hardcoded_margin
