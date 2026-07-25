"""统一重试 / 自动重抽策略中心。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app import config
from app.db import get_setting


class RetryKind(str, Enum):
    PROVIDER_TRANSIENT = "provider_transient"  # 同 job 延迟重排，不新建版本
    SAFETY = "safety"                          # 同版本清 task_id 再提交
    COPYRIGHT = "copyright"
    TECHNICAL = "technical"                    # 技术校验失败 → 可新建版本重提
    QA_RETAKE = "qa_retake"                    # 质量低分 → 同参考图集重抽


@dataclass(frozen=True)
class RetryDecision:
    allow: bool
    kind: RetryKind
    create_new_version: bool
    reason: str
    max_attempts: int
    attempt: int


def auto_retake_limit() -> int:
    """质量 QA 自动重抽：连续失败 2 次后停止烧钱转人工（PRD §14.3）。"""
    try:
        return max(1, int(get_setting("video_auto_retake_limit") or 2))
    except (TypeError, ValueError):
        return 2


def technical_resubmit_limit() -> int:
    return 2


def job_transient_max_retries() -> int:
    return int(config.VIDEO_JOB_MAX_RETRIES)


def decide_qa_retake(*, auto_retake_count: int, qa_overall: float, threshold: float | None = None,
                     hard_failures: list[str] | None = None) -> RetryDecision:
    thr = threshold if threshold is not None else float(get_setting("auto_retake_threshold") or 0.6)
    limit = auto_retake_limit()
    failures = list(hard_failures or [])
    if qa_overall < 0 and not failures:
        return RetryDecision(False, RetryKind.QA_RETAKE, False, "质检未完成", limit, auto_retake_count)
    if not failures and qa_overall >= thr:
        return RetryDecision(False, RetryKind.QA_RETAKE, False, "已达标", limit, auto_retake_count)
    if auto_retake_count >= limit:
        return RetryDecision(
            False, RetryKind.QA_RETAKE, False,
            "自动重抽已达上限，停止烧钱并转人工处理队列", limit, auto_retake_count,
        )
    reason = "质检未达阈值，按失败类型定向重抽" if failures else "质检未达阈值，自动重抽"
    return RetryDecision(
        True, RetryKind.QA_RETAKE, True,
        reason, limit, auto_retake_count + 1,
    )


def first_pass_retake_slot_fraction() -> float:
    """首轮未覆盖完成前，自动重抽最多占视频槽位的比例。"""
    return 0.25


def episode_inflight_cap() -> int:
    return int(get_setting("episode_video_inflight_limit") or 8)


def project_inflight_cap() -> int:
    return int(get_setting("project_video_inflight_limit") or 12)


def prepared_reference_backlog() -> int:
    """参考图可领先视频槽位的镜数。"""
    return int(get_setting("reference_prepared_backlog") or 8)
