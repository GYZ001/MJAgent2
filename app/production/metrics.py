"""生产交付关键指标（写入 provider_calls / val422_metric）。"""
from __future__ import annotations

from typing import Any

from app.observability.metrics import inc


def record_baseline_generation(*, kind: str, episode_id: str, revision_id: str, **extra: Any) -> None:
    inc(
        "baseline_generation_calls_total",
        kind=kind,
        episode_id=episode_id,
        revision_id=revision_id,
        **extra,
    )


def record_full_regen_denied(*, kind: str, episode_id: str, revision_id: str, reason: str = "", **extra: Any) -> None:
    inc(
        "full_regeneration_after_first_qa_total",
        kind=kind,
        episode_id=episode_id,
        revision_id=revision_id,
        reason=reason or "FULL_REGEN_AFTER_QA_DENIED",
        **extra,
    )


def record_patch(*, kind: str, episode_id: str, revision_id: str, touched: int = 0, **extra: Any) -> None:
    inc(
        "production_repair_patch_total",
        kind=kind,
        episode_id=episode_id,
        revision_id=revision_id,
        touched=touched,
        **extra,
    )


def record_noop_rejected(*, kind: str, episode_id: str, **extra: Any) -> None:
    inc("repair_noop_rejected_total", kind=kind, episode_id=episode_id, **extra)


def record_issue_reopened(*, kind: str, episode_id: str, fingerprint: str = "", **extra: Any) -> None:
    inc(
        "repair_issue_reopened_total",
        kind=kind,
        episode_id=episode_id,
        fingerprint=fingerprint,
        **extra,
    )


def record_activation(*, kind: str, episode_id: str, activation_no: int = 0, **extra: Any) -> None:
    inc(
        "repair_activation_total",
        kind=kind,
        episode_id=episode_id,
        activation_no=activation_no,
        **extra,
    )


def record_certificate_issued(*, kind: str, episode_id: str, **extra: Any) -> None:
    inc("time_to_completion_certificate_seconds", kind=kind, episode_id=episode_id, **extra)
    inc(f"certified_{kind}_delivery_rate", kind=kind, episode_id=episode_id, value=1, **extra)


def record_publish_without_certificate(*, kind: str, episode_id: str, **extra: Any) -> None:
    inc("published_without_certificate_total", kind=kind, episode_id=episode_id, **extra)
