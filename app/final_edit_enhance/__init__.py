"""成片合成三项增强（统一配乐混音 / 片头高能预告 / 主角内心独白），2026-09-28
《顾念长安（第二版）》第 1 集试点。

唯一对外入口是 :func:`apply_enhancements`（``app.media_exec.concat`` 的两处
合成路径各调用一次，开关全关时零 IO、逐字节不变）。子模块划分与设计取舍见
``apply.py``/``plan_schema.py``/``music_runs.py``/``teaser.py``/
``subtitle_shift.py`` 各自的模块 docstring，不在这里重复。
"""
from __future__ import annotations

from app.final_edit_enhance.apply import (
    EnhancementResult as EnhancementResult,
    apply_enhancements as apply_enhancements,
    apply_enhancements_sync as apply_enhancements_sync,
)

__all__ = ["apply_enhancements", "apply_enhancements_sync", "EnhancementResult"]
