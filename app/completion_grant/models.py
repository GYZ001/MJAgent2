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
DEFAULT_VIDEO_WALL_CLOCK_CAP_S = 4 * 3600
DEFAULT_FALLBACK_QUOTA_FRACTION = 0.2

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
    """A payable provider video call could not be recorded against the ledger.

    金额不再构成生成拦截（会员分档时长制）：``reserve_provider_video_budget``
    正常路径恒返回 True，这个异常现在只在预算台账表缺失（部署/迁移异常）时
    触发，不代表"超支"——见 CLAUDE.md「Retiring Features」与本次「成本预算
    拦截体系退场」。类名与异常类型保留，避免改动全部调用/捕获点签名。
    """
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
