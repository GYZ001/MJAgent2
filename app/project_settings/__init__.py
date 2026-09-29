"""项目级设置存取：改编强度档位（``adaptation_mode``）/ 画幅（``aspect_ratio``）/
AI 标识开关（``ai_label_enabled``）/ 统一配乐（``enhance_music_bed``）/ 片头预告
（``enhance_teaser``）/ 主角内心独白（``enhance_monologue``）/ 旁白固定音色角色
（``narrator_voice_character``）。只读写 ``projects`` 表的这七列，不做业务判定——
「改编强度具体怎么影响生成」「统一配乐具体怎么改写提示词」都是各自消费方的事，
本包只负责存取契约本身。

真源在 ``app.project_settings.store``；这里只做显式再导出（CLAUDE.md「再导出写
``from x import y as y``，不借道某个碰巧 import 了它的子模块转手」）。
"""
from __future__ import annotations

from app.project_settings.store import (
    ADAPTATION_MODES as ADAPTATION_MODES,
    ASPECT_RATIOS as ASPECT_RATIOS,
    ai_label_enabled as ai_label_enabled,
    canvas_phrase as canvas_phrase,
    canvas_size as canvas_size,
    enhance_monologue_enabled as enhance_monologue_enabled,
    enhance_music_bed_enabled as enhance_music_bed_enabled,
    enhance_teaser_enabled as enhance_teaser_enabled,
    resolve_adaptation_mode as resolve_adaptation_mode,
    resolve_aspect_ratio as resolve_aspect_ratio,
    resolve_narrator_voice_character as resolve_narrator_voice_character,
    update_project_settings as update_project_settings,
)
