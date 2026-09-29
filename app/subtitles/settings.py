"""字幕嵌入的监制房设置读取（PRD §8/§10）。L2，只依赖 ``app.db``。

照抄 ``app/media_exec/subtitle_gate.py::enabled()`` 的写法：非法值一律
``RuntimeError``，不静默回退默认——设置项一旦写坏（比如字号写成负数），必须
让用户在监制房看到明确报错去改，而不是悄悄套用一个跟用户预期不符的默认值。
"""
from __future__ import annotations

from app.db import get_setting
from app.subtitles.ass import SubtitleStyle

SETTING_ENABLED = "subtitle_burn_in_enabled"
SETTING_FONT_SIZE = "subtitle_font_size"
SETTING_MARGIN_BOTTOM = "subtitle_margin_bottom"
SETTING_MAX_CHARS_PER_LINE = "subtitle_max_chars_per_line"
SETTING_SHOW_SPEAKER = "subtitle_show_speaker"

_FONT_SIZE_RANGE = (40, 120)
_MARGIN_BOTTOM_RANGE = (0, 800)
_MAX_CHARS_RANGE = (8, 24)

# ``subtitle_margin_bottom`` 是按竖屏标定的像素值（默认 400，对应约 1920 高的
# 21%）：横屏画布高度只有约 1080，同一个像素值会把字幕抬到离底边约 37% 处
# （2026-09-28 用户反馈「字幕位置太高」的根因）。横屏改按画面高度的固定比例
# 换算，不复用竖屏那个像素设置——两种画幅的安全边距语义本就不同（竖屏要避开
# 短视频平台底部 UI，横屏只是常规字幕安全边距），同一个像素刻度套两种画幅
# 会让其中一种失真；这里选择「按画幅换算」而不是新增一个横屏专属设置键，因为
# 换算值是纯几何安全边距、没有需要用户按项目调节的理由，多一个键只会增加
# 认知负担又不提供真实可调价值。
_LANDSCAPE_MARGIN_BOTTOM_RATIO = 0.07


def _effective_margin_bottom(configured: int, play_res: tuple[int, int]) -> int:
    width, height = play_res
    if width > height:
        return round(height * _LANDSCAPE_MARGIN_BOTTOM_RATIO)
    return configured


def _boolean_setting(key: str) -> bool:
    value = (get_setting(key) or "false").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError(f"非法运行时设置 {key}；请在监制房修正")
    return value == "true"


def burn_in_enabled() -> bool:
    return _boolean_setting(SETTING_ENABLED)


def _ranged_int_setting(key: str, low: int, high: int) -> int:
    raw = (get_setting(key) or "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"非法运行时设置 {key}={raw!r}；请在监制房修正") from exc
    if not low <= value <= high:
        raise RuntimeError(f"运行时设置 {key}={value} 超出合法范围 [{low}, {high}]；请在监制房修正")
    return value


def style_from_settings(*, font_family: str, play_res: tuple[int, int]) -> SubtitleStyle:
    """四个样式键读出来做范围校验；越界抛 ``RuntimeError`` 并提示在监制房修正。
    ``play_res`` 必传：底边距按画幅换算（见 ``_effective_margin_bottom``），
    没有默认值可用——沿用哪种画幅的语义是所有权问题，不能悄悄兜底。"""
    font_size = _ranged_int_setting(SETTING_FONT_SIZE, *_FONT_SIZE_RANGE)
    configured_margin = _ranged_int_setting(SETTING_MARGIN_BOTTOM, *_MARGIN_BOTTOM_RANGE)
    margin_bottom = _effective_margin_bottom(configured_margin, play_res)
    max_chars_per_line = _ranged_int_setting(SETTING_MAX_CHARS_PER_LINE, *_MAX_CHARS_RANGE)
    show_speaker = _boolean_setting(SETTING_SHOW_SPEAKER)
    return SubtitleStyle(
        font_family=font_family, font_size=font_size, margin_bottom=margin_bottom,
        max_chars_per_line=max_chars_per_line, show_speaker=show_speaker,
    )
