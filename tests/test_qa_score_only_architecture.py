from __future__ import annotations

from pathlib import Path

from app.media_pipeline.retry_policy import decide_qa_retake, decide_retry_by_error_class


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


def test_decide_qa_retake_is_bounded_and_never_blocks_closeout() -> None:
    decision = decide_qa_retake(
        auto_retake_count=0,
        qa_overall=0.1,
        threshold=0.8,
        hard_failures=["score_below", "consistency_drift"],
    )
    assert decision.allow is True
    assert decision.create_new_version is True
    exhausted = decide_qa_retake(
        auto_retake_count=decision.max_attempts,
        qa_overall=0.1,
        threshold=0.8,
        hard_failures=["score_below"],
    )
    assert exhausted.allow is False
    assert exhausted.create_new_version is False
    assert "自动择优" in exhausted.reason


def test_frontend_scene_usability_does_not_use_hard_gate_for_availability() -> None:
    source = Path("frontend/src/lib/sceneUsability.ts").read_text(encoding="utf-8")
    assert "hard_gate_passed" not in source
