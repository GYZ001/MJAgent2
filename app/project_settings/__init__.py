"""项目级设置存取：改编强度档位（``adaptation_mode``）/ 画幅（``aspect_ratio``）/
AI 标识开关（``ai_label_enabled``）。只读写 ``projects`` 表的这三列，不做业务判定
——「改编强度具体怎么影响生成」是后续单元的事，本包只负责存取契约本身。

真源在 ``app.project_settings.store``；这里只做显式再导出（CLAUDE.md「再导出写
``from x import y as y``，不借道某个碰巧 import 了它的子模块转手」）。
"""
from __future__ import annotations

from app.project_settings.store import (
    ADAPTATION_MODES as ADAPTATION_MODES,
    ASPECT_RATIOS as ASPECT_RATIOS,
    ai_label_enabled as ai_label_enabled,
    canvas_size as canvas_size,
    resolve_adaptation_mode as resolve_adaptation_mode,
    resolve_aspect_ratio as resolve_aspect_ratio,
    update_project_settings as update_project_settings,
)
