"""Build the AI-planner payload and the canonical typed ``Shot`` from one DB
shot row.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale). Used by
both ``.release_manifest`` (fingerprinting) and ``.validate``/``.generate``
(planning), kept in its own file to avoid an artificial import direction
between those two.
"""
from __future__ import annotations

import json
from typing import Any

from .primitives import _row_value


def _shot_planner_payload(row: Any) -> dict[str, Any]:
    try:
        contract = json.loads(_row_value(row, "shot_contract_json", "") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        contract = {}
    return {
        "shot_id": str(contract.get("shot_id") or _row_value(row, "shot_uid", "") or row["id"]),
        "database_shot_id": row["id"],
        "shot_no": row["shot_no"],
        "duration_s": row["duration_s"],
        "scene_time": _row_value(row, "scene_time", ""),
        "scene_name": _row_value(row, "scene_name", "") or row["scene_setting"],
        "shot_size": row["shot_size"],
        "camera_move": row["camera_move"],
        "action_desc": row["action_desc"],
        "first_frame_desc": _row_value(row, "first_frame_desc", ""),
        "last_frame_desc": _row_value(row, "last_frame_desc", ""),
        "dialogues": json.loads(row["dialogues"] or "[]"),
        "transition": row["transition"],
        "continuity_mode": _row_value(row, "continuity_mode", ""),
        "state_in": contract.get("state_in"),
        "state_out": contract.get("state_out"),
        "planned_state_in_fact_ids": contract.get("planned_state_in_fact_ids") or [],
        "planned_state_out_fact_ids": contract.get("planned_state_out_fact_ids") or [],
        "primary_action_id": contract.get("primary_action_id"),
        "supporting_action_ids": contract.get("supporting_action_ids") or [],
        "action_phase_ids": contract.get("action_phase_ids") or [],
        "completed_before_action_ids": contract.get("completed_before_action_ids") or [],
        "completed_before_action_phase_ids": (
            contract.get("completed_before_action_phase_ids") or []
        ),
        "capacity_budget": contract.get("capacity_budget"),
        "visible_entity_ids": contract.get("visible_entity_ids") or [],
        "offscreen_action_actor_ids": contract.get("offscreen_action_actor_ids") or [],
        "offscreen_action_target_ids": contract.get("offscreen_action_target_ids") or [],
        "action_participant_deliveries": (
            contract.get("action_participant_deliveries") or []
        ),
        "event_ids": contract.get("event_ids") or [],
        "boundary_from_previous": contract.get("narrative_boundary_from_previous"),
    }


def _shot_model_from_row(row: Any):
    from app.continuity import apply_shot_contract
    from app.schemas import Shot

    shot = Shot(
        shot_no=row["shot_no"],
        shot_uid=_row_value(row, "shot_uid", "") or "",
        duration_s=row["duration_s"],
        shot_size=row["shot_size"],
        camera_move=row["camera_move"],
        scene_time=_row_value(row, "scene_time", "") or "",
        scene_setting=row["scene_setting"],
        scene_name=_row_value(row, "scene_name", "") or "",
        characters=json.loads(row["characters"] or "[]"),
        action_desc=row["action_desc"],
        first_frame_desc=_row_value(row, "first_frame_desc", "") or "",
        last_frame_desc=_row_value(row, "last_frame_desc", "") or "",
        source_excerpt=_row_value(row, "source_excerpt", "") or "",
        narration=row["narration"],
        dialogues=json.loads(row["dialogues"] or "[]"),
        transition=row["transition"] or "硬切",
        continuity_from_prev=bool(row["continuity_from_prev"]),
        continuity_mode=_row_value(row, "continuity_mode", "") or "",
        observed_state_out=_row_value(row, "observed_state_out", "") or "",
    )
    apply_shot_contract(shot, _row_value(row, "shot_contract_json", ""))
    return shot
