"""整集目标时长的取整规则。

``_compact_episode_target`` 从 ``app/domain/common.py`` 按原样搬移到这里
（2026-08-30，见 ``docs/layer_violations_plan_2026-08-30.md`` 组 7a）：只依赖
``app.config`` 的三个 ``EPISODE_TARGET_*`` 常量（L1），但原来待在 L5 的
``app.domain.common`` 里，逼着 ``app.domain.video_ops.confirmation_eval``（其余
依赖都 <=L4）为了这一个纯取整函数越级 import 整个 domain 包。``app.domain.common``
继续从本模块重新导入并保持这个名字可从 ``app.domain.common``/``app.domain``/
``app.domain.video_ops`` 原样导入，不影响任何既有调用点。
"""
from __future__ import annotations

from app import config


def _compact_episode_target(target_duration_s: int | None) -> int:
    if target_duration_s is None:
        return config.EPISODE_TARGET_DEFAULT_S
    target = max(int(target_duration_s), config.EPISODE_TARGET_MIN_S)
    step = config.EPISODE_TARGET_STEP_S
    rounded = ((target + step // 2) // step) * step
    return max(config.EPISODE_TARGET_MIN_S, rounded)
