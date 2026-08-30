"""Readability-window shot-id projection and final StoryboardOutline
assembly (phases J/K/L) for normalize_narrative_storyboard_outline.

Split out of narrative_outline.py -- see that function's docstring.
"""
from __future__ import annotations

from typing import Any

from app.schemas import StoryboardOutline


def _finalize_outline_windows_and_shots(
    outline: StoryboardOutline,
    plan: Any,
    normalized_shots: list[Any],
    positions_by_event: dict[str, list[int]],
    delta_owner_position: dict[str, int],
) -> None:
    """Project each readability_window's shot_ids/planned_available_s, link shots back to windows, and replace outline.shots/readability_windows/cognitive_bridge_plans."""
    windows = []
    for source_window in plan.readability_windows:
        window = source_window.model_copy(deep=True)
        shot_ids: list[str] = []
        for event_id in window.event_ids:
            shot_ids.extend(
                normalized_shots[position].shot_id
                for position in positions_by_event.get(event_id, [])
            )
        for delta_id in window.target_delta_ids:
            owner_position = delta_owner_position.get(delta_id)
            if owner_position is not None:
                shot_ids.append(normalized_shots[owner_position].shot_id)
        window.shot_ids = list(dict.fromkeys(shot_ids))
        linked_duration = sum(
            float(shot.duration_s or 0)
            for shot in normalized_shots
            if shot.shot_id in window.shot_ids
        )
        window.planned_available_s = min(
            linked_duration,
            max(
                float(window.scheduled_processing_s or 0),
                min(linked_duration, float(window.planned_available_s or 0)),
            ),
        )
        windows.append(window)

    for shot in normalized_shots:
        shot.readability_window_ids = [
            window.readability_window_id
            for window in windows
            if shot.shot_id in window.shot_ids
        ]

    normalized = StoryboardOutline.model_validate({
        "episode_no": outline.episode_no,
        "shots": [
            shot.model_dump(mode="json")
            for shot in normalized_shots
        ],
        "readability_windows": [
            window.model_dump(mode="json")
            for window in windows
        ],
        "cognitive_bridge_plans": [],
    })
    outline.shots = normalized.shots
    outline.readability_windows = normalized.readability_windows
    outline.cognitive_bridge_plans = []

