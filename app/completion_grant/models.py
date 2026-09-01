"""视频补齐授权的常量、数据模型与错误类型。

从 app/completion_grant.py 拆出（2026-08-31，#75）：原文件 2467 行，是 500 行
上限的 5 倍，基线已收到零余量——任何新增行都会让闸门变红。

这一层不依赖包内任何其它模块，是拆分的底座。``_row_to_video_grant``（数据库行
转数据模型）也放在这里：它只依赖 VideoCompletionGrant，放在别处会让签发与读取
两个模块互相 import 成环。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
)

GRANT_TTL_S = 6 * 3600  # 6 小时
VIDEO_PERMISSION = "video.complete_episode"
DEFAULT_VIDEO_BUDGET_CAP_CNY = 150.0
DEFAULT_VIDEO_WALL_CLOCK_CAP_S = 4 * 3600
DEFAULT_FALLBACK_QUOTA_FRACTION = 0.2
# 快速生成（video.generate_episode / video.generate_shot）曾经把 cap 精确设成
# 首轮预估，零余量——任何一镜只要真需要第二次付费尝试就必然打穿上限，只能
# 人工加额。2026-08-25 用 data/manju.db 里 shot_versions 的版本数分布反推：
# 已产出真实版本的 24 个镜头中 66.7% 只需 1 个版本、29.2% 需要 2 个、4.2%
# （EP1 第 1 镜）需要 3 个——单镜最多观测到 2 次重投，EP1 全集口径达到过
# 15 版/8 镜=1.88 倍。按“每镜最多可承受 2 次重投”取整数倍数，对首轮预估
# 整体乘 3，覆盖已观测到的最坏单镜情形，即使全集所有镜头都撞到这个上限也
# 仍有余量。
VIDEO_BUDGET_RETRY_MARGIN_MULTIPLIER = 3.0
# 用户拍板的单集硬上限（2026-08-25，「留余量吧，一集 500 块钱以内」）。现有
# 单价 ¥12/镜、库内最大分集 15 镜（ep_a0e90058f83c）时，3 倍余量后 ¥540 会
# 被这道保险丝截到 ¥500——截断后的有效倍数（¥500/¥180≈2.78）仍高于历史
# 最坏重投比例（EP1 1.88 倍），只是把无限风险换成有限风险，正常分集规模
# 下不会触发。
EPISODE_VIDEO_BUDGET_HARD_CAP_CNY = 500.0

_PROVIDER_CLAIM_LEDGER_COLUMNS = {
    "operation_id",
    "project_id",
    "episode_id",
    "shot_id",
    "job_id",
    "version_id",
    "origin_episode_id",
    "origin_shot_id",
    "origin_job_id",
    "origin_version_id",
    "amount_cny",
    "status",
    "liability_source",
    "created_at",
    "updated_at",
    "accepted_at",
    "settled_at",
    "released_at",
    "liability_closed_at",
    "closure_reason",
}

class VideoCompletionGrant(BaseModel):
    grant_id: str
    episode_id: str
    project_id: str
    storyboard_artifact_id: str
    release_qualification_hash: str = ""
    release_qualification: dict[str, Any] = Field(default_factory=dict)
    episode_video_plan_id: str | None = None
    episode_video_plan_revision: int | None = None
    video_plan_release_hash: str | None = None
    capability_snapshot_id: str | None = None
    permission: Literal["video.complete_episode"] = VIDEO_PERMISSION
    kind: Literal["video"] = "video"
    budget_cap_cny: float = DEFAULT_VIDEO_BUDGET_CAP_CNY
    wall_clock_cap_s: float = DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    deadline_at: float
    allow_fallback_adopt: bool = True
    max_fallback_shots: int = 0
    allow_storyboard_edit: bool = False
    issued_by: str = "user"
    issued_at: float
    expires_at: float
    consumed_at: float | None = None
    revoked_at: float | None = None


class VideoBudgetAuthorizationError(RuntimeError):
    """A payable provider video call would exceed the user-approved cap."""
class GrantValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
class VideoPlanGenerationError(RuntimeError):
    """整集视频计划 JSON 编译/校验失败——不是授权问题。

    无论用户追加多少预算或时长都解决不了模型输出畸形。刻意不继承
    ``GrantValidationError``：那样会被 video_supervisor 里每一处
    ``except GrantValidationError`` 一并吞掉，翻译成 WAITING_AUTHORIZATION，
    把用户导向一个死循环的 ``authorize_continue`` 入口（ERR-20260831-dd05c7，
    run_45be44ddd467）。保留 ``.code`` 字段只是为了复用既有的
    ``_record_grant_validation_failure`` 明细落库逻辑，不代表它是同一类错误。
    """
    def __init__(self, message: str, *, code: str = "VIDEO_PLAN_INVALID"):
        self.code = code
        super().__init__(message)
def _row_to_video_grant(row) -> VideoCompletionGrant:
    def _col(name, default=None):
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return default

    try:
        release_qualification = json.loads(
            _col("release_qualification_json", "{}") or "{}"
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        release_qualification = {}
    return VideoCompletionGrant(
        grant_id=row["id"],
        episode_id=row["episode_id"],
        project_id=row["project_id"],
        storyboard_artifact_id=_col("storyboard_artifact_id") or "",
        release_qualification_hash=_col("release_qualification_hash") or "",
        release_qualification=release_qualification,
        episode_video_plan_id=_col("episode_video_plan_id") or None,
        episode_video_plan_revision=(
            int(_col("episode_video_plan_revision"))
            if _col("episode_video_plan_revision") is not None
            else None
        ),
        video_plan_release_hash=_col("video_plan_release_hash") or None,
        capability_snapshot_id=_col("capability_snapshot_id") or None,
        budget_cap_cny=float(_col("budget_cap_cny") or DEFAULT_VIDEO_BUDGET_CAP_CNY),
        wall_clock_cap_s=float(_col("wall_clock_cap_s") or DEFAULT_VIDEO_WALL_CLOCK_CAP_S),
        deadline_at=float(
            _col("deadline_at")
            or (float(row["issued_at"]) + float(_col("wall_clock_cap_s") or DEFAULT_VIDEO_WALL_CLOCK_CAP_S))
        ),
        allow_fallback_adopt=bool(int(_col("allow_fallback_adopt", 1) or 0)),
        max_fallback_shots=int(_col("max_fallback_shots") or 0),
        allow_storyboard_edit=bool(int(_col("allow_storyboard_edit", 0) or 0)),
        issued_by=row["issued_by"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        revoked_at=row["revoked_at"],
    )
