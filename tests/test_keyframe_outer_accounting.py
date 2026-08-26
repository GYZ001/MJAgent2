import json

from app import video_cost_model, video_modes
from app.media_exec import run_job
from app.media_pipeline import retry_policy


def test_default_generation_budget_has_no_generated_images(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "keyframe_candidate_count", lambda: 3)
    monkeypatch.setattr(video_modes, "supporting_keyframe_candidate_count", lambda: 3)

    assert video_modes.estimated_keyframe_generation_count() == 0


def test_timeline_keyframe_progress_aggregates_master_and_supporting_slots(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "keyframe_candidate_count", lambda: 3)
    monkeypatch.setattr(video_modes, "supporting_keyframe_candidate_count", lambda: 1)
    monkeypatch.setattr(video_modes, "estimated_keyframe_generation_count", lambda: 9)
    meta = {
        "reference_slots": {
            "narrative_keyframe": {
                "status": "qa_pending",
                "candidate_target": 3,
                "candidates": [
                    {"candidate_no": 1, "status": "qa_pending"},
                    {"candidate_no": 2, "status": "generation_failed"},
                ],
            },
            "narrative_keyframe_01": {
                "status": "qa_pending",
                "candidate_target": 1,
                "candidates": [{"candidate_no": 1, "status": "qa_pending"}],
            },
            "narrative_keyframe_03": {
                "status": "passed",
                "candidate_target": 1,
                # Final/legacy checkpoints may retain only the winner path.
                "path": "/tmp/winner.jpg",
            },
            "unrelated_slot": {
                "status": "qa_pending",
                "candidate_target": 50,
                "candidates": [{"candidate_no": 1}],
            },
        },
    }

    assert run_job._narrative_keyframe_candidate_progress(meta) == (4, 5)


def test_timeline_progress_uses_frozen_sequence_before_slots_exist(monkeypatch) -> None:
    monkeypatch.setattr(video_modes, "keyframe_candidate_count", lambda: 3)
    monkeypatch.setattr(video_modes, "supporting_keyframe_candidate_count", lambda: 3)
    meta = {
        "keyframe_sequence": {
            "beats": [
                {"slot_key": "narrative_keyframe_01"},
                {"slot_key": "narrative_keyframe"},
            ],
        },
        "reference_slots": {},
    }

    assert run_job._narrative_keyframe_candidate_progress(meta) == (0, 6)


def test_completed_reference_slots_includes_all_timeline_keyframe_slots() -> None:
    raw = json.dumps({
        "reference_slots": {
            "narrative_keyframe": {"status": "passed"},
            "narrative_keyframe_01": {"status": "unverified"},
            "narrative_keyframe_02": {"status": "scored_warning"},
            "narrative_keyframe_03": {"status": "qa_pending"},
        },
    })

    assert run_job._completed_reference_slots(raw) == 3


def test_video_cost_model_uses_full_timeline_generation_estimate(monkeypatch) -> None:
    monkeypatch.setattr(video_cost_model, "shot_cost_cny", lambda _duration: 5.0)
    monkeypatch.setattr(video_cost_model, "IMAGE_PRICE_PER_UNIT", 0.2)
    monkeypatch.setattr(
        video_cost_model.video_modes,
        "estimated_keyframe_generation_count",
        lambda: 9,
    )
    monkeypatch.setattr(video_cost_model, "historical_attempt_stats", lambda **_kwargs: {
        "samples": 0.0,
        "avg_cost_per_paid_version": 0.0,
        "avg_paid_attempts_per_shot": 1.0,
        "success_rate": 1.0,
    })

    estimate = video_cost_model.predict_shot_completion_cost(5, retry_factor=1.0)

    assert estimate["unit_cny"] == 6.8
    assert estimate["expected_cny"] == 6.8


def test_reference_cohort_limit_uses_full_timeline_generation_estimate(monkeypatch) -> None:
    from app.media_pipeline import concurrency, stages

    monkeypatch.setattr(retry_policy, "get_setting", lambda _key: None)
    monkeypatch.setattr(
        concurrency,
        "channel_limit",
        lambda resource: 20 if resource == stages.RESOURCE_IMAGE else 1,
    )
    monkeypatch.setattr(video_modes, "estimated_keyframe_generation_count", lambda: 9)

    assert retry_policy.reference_shot_cohort_limit() == 2
