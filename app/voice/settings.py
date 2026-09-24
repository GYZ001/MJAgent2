"""``voice_auto_generate`` 设置读取（schema 声明见 ``app.monitoring.
SETTINGS_SCHEMA``，label「定妆后自动生成角色声音」，默认开启）。

默认开启与 ``app.video_plan.prev_frame_reference`` 默认关闭的读法刻意不同：
空值在这里代表"未显式关闭"，不是"未显式打开"。不复用 ``app.db.get_setting``
的 ``config.DEFAULT_SETTINGS`` 兜底（那是另一份独立的默认值表，本设置未在
其中登记，直接查会拿到空串），改由本模块自己在空值时按 schema 默认值兜底,
避免两份默认值表不同步。
"""
from __future__ import annotations

from app.db import get_setting

SETTING_KEY = "voice_auto_generate"


def voice_auto_generate_enabled() -> bool:
    raw = str(get_setting(SETTING_KEY) or "").strip().lower()
    if raw == "":
        return True
    return raw in {"1", "true", "on", "yes"}  # 设置台存 true/false，旧行存 0/1
