from __future__ import annotations

from pathlib import Path

from app.media_pipeline import retry_policy


def test_error_class_retry_router_no_longer_exists() -> None:
    assert not hasattr(retry_policy, "decide_retry_by_error_class")
    assert not hasattr(retry_policy, "decide_qa_retake")


def test_frontend_scene_usability_does_not_use_hard_gate_for_availability() -> None:
    source = Path("frontend/src/lib/sceneUsability.ts").read_text(encoding="utf-8")
    assert "hard_gate_passed" not in source
