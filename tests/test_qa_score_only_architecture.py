from __future__ import annotations

from pathlib import Path

from app.media_pipeline import retry_policy
from app.media_pipeline.retry_policy import decide_retry_by_error_class


def test_retry_by_error_class_rejects_qa_quality_score_classes() -> None:
    for error_class in (
        "QA_LOW_SCORE",
        "QUALITY_GATE_FAILED",
        "SCORE_BELOW_THRESHOLD",
        "VIDEO_QA_CHARACTER_DUPLICATE",
        "scene_hard_failure",
        "consistency_drift",
        "score_below_policy",
    ):
        decision = decide_retry_by_error_class(error_class, attempt=0)
        assert decision.allow is False


def test_qa_retake_policy_no_longer_exists() -> None:
    assert not hasattr(retry_policy, "decide_qa_retake")
    assert "QA_RETAKE" not in retry_policy.RetryKind.__members__


def test_frontend_scene_usability_does_not_use_hard_gate_for_availability() -> None:
    source = Path("frontend/src/lib/sceneUsability.ts").read_text(encoding="utf-8")
    assert "hard_gate_passed" not in source
