"""单镜首轮预留金额计算——仅供内部记账台账，不参与任何拦截/展示。

金额不再构成生成拦截（会员分档时长制，非按金额计费）：本文件原有的历史成功
率成本预测（``historical_attempt_stats``/``predict_shot_completion_cost``/
``predict_episode_completion_cost``，服务于已删除的 UI 成本展示与
``attempts_for`` 金额换算）在消费者清零后已删除——见 CLAUDE.md「Retiring
Features」与本次「成本预算拦截体系退场」。``initial_shot_generation_cost``
本应随之整文件删除，但它仍是 ``budget_reservations``/
``provider_video_budget_claims`` 记账写入（这两张审计台账本轮明确不动，见
同一份退场说明）唯一的金额来源，`app/media_exec/enqueue.py`、
`app/system_api.py` 的记账调用点仍依赖它，因此本文件降级为只保留这一个
纯计算函数，不再整删。它的返回值只写进内部台账表，不出现在任何拦截判据或
用户可见的展示界面里。
"""
from __future__ import annotations

from app.compiler import shot_cost_cny
from app.config import IMAGE_PRICE_PER_UNIT
from app import video_modes


def initial_shot_generation_cost(duration_s: float) -> float:
    """Match the exact first-pass reservation shown at UI approval."""
    return round(
        shot_cost_cny(int(duration_s or 5))
        + IMAGE_PRICE_PER_UNIT
        * video_modes.estimated_keyframe_generation_count(),
        6,
    )
