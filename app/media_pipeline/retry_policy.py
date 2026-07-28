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


RETRYABLE_ERROR_CLASSES = frozenset({
    "PROVIDER_TRANSIENT",
    "ACCIDENTAL_JSON_INVALID",
    "ACCIDENTAL_SCHEMA_MISMATCH",
    "PROVIDER_RESPONSE_INCOMPLETE",
    "MEDIA_TECHNICALLY_UNUSABLE",
    "CONTEXT_MISSING",
})

_QA_SCORE_ONLY_PREFIXES = ("QA_", "QUALITY_", "SCORE_")
_QA_SCORE_ONLY_MARKERS = ("hard_failure", "consistency_drift", "score_below")


def decide_retry_by_error_class(
    error_class: str,
    *,
    attempt: int = 0,
    max_attempts: int | None = None,
    context_missing_retryable: bool = False,
) -> RetryDecision:
    """Only accidental/transient infrastructure failures may schedule retries.

    QA, quality and score-derived findings are score-only signals on this branch;
    they must never create an automatic retry or retake path.
    """
    normalized = str(error_class or "").strip().upper()
    lowered = normalized.lower()
    limit = max_attempts if max_attempts is not None else technical_resubmit_limit()
    if (
        normalized.startswith(_QA_SCORE_ONLY_PREFIXES)
        or "_QA_" in normalized
        or any(marker in lowered for marker in _QA_SCORE_ONLY_MARKERS)
    ):
        return RetryDecision(
            False,
            RetryKind.TECHNICAL,
            False,
            "QA/quality/score findings are score-only and cannot trigger retry",
            limit,
            attempt,
        )
    if normalized not in RETRYABLE_ERROR_CLASSES:
        return RetryDecision(
            False,
            RetryKind.TECHNICAL,
            False,
            f"error_class {normalized or '<empty>'} is not retry allowlisted",
            limit,
            attempt,
        )
    if normalized == "CONTEXT_MISSING" and not context_missing_retryable:
        return RetryDecision(
            False,
            RetryKind.TECHNICAL,
            False,
            "CONTEXT_MISSING requires explicit retryable context",
            limit,
            attempt,
        )
    if normalized == "PROVIDER_TRANSIENT":
        limit = max_attempts if max_attempts is not None else job_transient_max_retries()
        kind = RetryKind.PROVIDER_TRANSIENT
        create_new_version = False
    else:
        kind = RetryKind.TECHNICAL
        create_new_version = normalized == "MEDIA_TECHNICALLY_UNUSABLE"
    return RetryDecision(
        attempt < limit,
        kind,
        create_new_version,
        f"error_class {normalized} is retry allowlisted",
        limit,
        attempt,
    )


def auto_retake_limit() -> int:
    """明确结构性 QA 失败最多自动重抽一次，随后转人工。"""
    try:
        return max(1, int(get_setting("video_auto_retake_limit") or 1))
    except (TypeError, ValueError):
        return 1


def technical_resubmit_limit() -> int:
    return 2


def job_transient_max_retries() -> int:
    return int(config.VIDEO_JOB_MAX_RETRIES)


def decide_qa_retake(*, auto_retake_count: int, qa_overall: float, threshold: float | None = None,
                     hard_failures: list[str] | None = None) -> RetryDecision:
    """QA 只评分：永远禁止由 QA 分数/hard_failures 触发自动重抽（PRD QA-SO-002）。"""
    del qa_overall, threshold, hard_failures  # 保留签名兼容旧调用方
    limit = auto_retake_limit()
    return RetryDecision(
        False,
        RetryKind.QA_RETAKE,
        False,
        "QA 只评分，禁止自动重抽",
        limit,
        auto_retake_count,
    )


def first_pass_retake_slot_fraction() -> float:
    """首轮未覆盖完成前，自动重抽最多占视频槽位的比例。"""
    return 0.25


def episode_inflight_cap() -> int:
    return int(get_setting("episode_video_inflight_limit") or 15)


def project_inflight_cap() -> int:
    return int(get_setting("project_video_inflight_limit") or 15)


def prepared_reference_backlog() -> int:
    """参考图可领先视频槽位的镜数（legacy 兼容；stage_aware 用高低水位）。"""
    return int(get_setting("reference_prepared_backlog") or 8)


def video_ready_low_watermark() -> int:
    return max(0, int(get_setting("video_ready_low_watermark") or 2))


def video_ready_high_watermark() -> int:
    high = int(get_setting("video_ready_high_watermark") or 6)
    return max(video_ready_low_watermark(), high)


def reference_shot_cohort_limit() -> int:
    """同时占用图片通道的镜头批次数。"""
    raw = get_setting("reference_shot_cohort_limit")
    if raw not in (None, ""):
        try:
            return max(1, int(raw))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("非法运行时设置 reference_shot_cohort_limit；请在监制房修正") from exc
    # 默认：floor(image_concurrency / target_refs)。这里按一镜完整
    # 时序关键帧计划估算，避免多节拍上线后单镜瞬时占满图片通道。
    from app.media_pipeline.concurrency import channel_limit
    from app.media_pipeline import stages as S
    from app import video_modes
    image_n = channel_limit(S.RESOURCE_IMAGE)
    # PRD：cohort_limit = max(1, floor(image / target_generated_refs))
    planned = max(1, video_modes.estimated_keyframe_generation_count())
    return max(1, image_n // planned)


def scheduler_policy() -> str:
    value = (get_setting("media_scheduler_policy") or "stage_aware").strip().lower()
    if value not in {"legacy", "stage_aware"}:
        raise RuntimeError("非法运行时设置 media_scheduler_policy；请在监制房修正")
    return value


def batch_prompt_enabled() -> bool:
    value = (get_setting("video_reference_batch_prompt") or "true").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError("非法运行时设置 video_reference_batch_prompt；请在监制房修正")
    return value == "true"


def role_adaptive_enabled() -> bool:
    value = (get_setting("video_reference_role_adaptive") or "false").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError("非法运行时设置 video_reference_role_adaptive；请在监制房修正")
    return value == "true"
