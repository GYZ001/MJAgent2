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


def style_from_settings(*, font_family: str) -> SubtitleStyle:
    """四个样式键读出来做范围校验；越界抛 ``RuntimeError`` 并提示在监制房修正。"""
    font_size = _ranged_int_setting(SETTING_FONT_SIZE, *_FONT_SIZE_RANGE)
    margin_bottom = _ranged_int_setting(SETTING_MARGIN_BOTTOM, *_MARGIN_BOTTOM_RANGE)
    max_chars_per_line = _ranged_int_setting(SETTING_MAX_CHARS_PER_LINE, *_MAX_CHARS_RANGE)
    show_speaker = _boolean_setting(SETTING_SHOW_SPEAKER)
    return SubtitleStyle(
        font_family=font_family, font_size=font_size, margin_bottom=margin_bottom,
        max_chars_per_line=max_chars_per_line, show_speaker=show_speaker,
    )
