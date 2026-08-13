"""Live, read-only replay for run_9063aa816692's exact merged IR."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.domain.common import _episode_source_text
from app.narrative import validate_screenplay_narrative
from app.production.screenplay_authority import (
    screenplay_authorized_source_chapters,
)
from app.schemas import Bible
from app.screenplay_ir import ScreenplayGenerationIR, compile_screenplay_ir


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = ROOT / "data" / "manju.db"
EPISODE_ID = "ep_711b29204aa9"
MERGED_IR_ID = "art_d341438579e3"


@pytest.mark.live_integration
def test_run_9063_exact_merged_ir_recompiles_environment_subjects() -> None:
    if not PRODUCTION_DB.exists():
        pytest.skip("production database is not present")
    conn = sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    episode_row = conn.execute(
        "SELECT * FROM episodes WHERE id=?",
        (EPISODE_ID,),
    ).fetchone()
    artifact_row = conn.execute(
        "SELECT content_json FROM artifacts WHERE id=?",
        (MERGED_IR_ID,),
    ).fetchone()
    if episode_row is None or artifact_row is None:
        pytest.skip("run_9063 production fixture is not present")
    project_row = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?",
        (episode_row["project_id"],),
    ).fetchone()

    episode = dict(episode_row)
    episode["character_resolutions"] = json.loads(
        episode_row["screenplay_character_resolutions"]
    )
    source_text = _episode_source_text(conn, episode_row)
    episode["authorized_source_chapters"] = screenplay_authorized_source_chapters(
        EPISODE_ID,
        conn=conn,
    )
    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(json.loads(artifact_row[0])),
        episode=episode,
        source_text=source_text,
        bible=Bible.model_validate(json.loads(project_row[0])),
    )

    plan = screenplay.narrative_plan
    environment_id = f"environment:{EPISODE_ID}"
    environment_facts = [
        fact for fact in plan.state_facts if fact.subject_id == environment_id
    ]
    propositions = {
        proposition.proposition_id: proposition
        for proposition in plan.propositions
    }
    assert len(plan.events) == 83
    assert len(environment_facts) == 15
    assert all(
        environment_id in propositions[fact.proposition_id].entity_ids
        for fact in environment_facts
    )
    assert environment_id not in {
        identity.identity_id for identity in plan.identity_contracts
    }
    assert environment_id not in {
        voice.speaker_id for voice in screenplay.voice_bible
    }
    assert all(
        environment_id not in scene.characters
        for scene in screenplay.scene_outline
    )
    assert all(
        environment_id not in event.onscreen_entity_ids
        for event in plan.events
    )
    assert all(
        environment_id not in [*action.actor_ids, *action.target_ids]
        for action in plan.atomic_actions
    )
    assert all(
        scene.point_of_view_character_id != environment_id
        for scene in plan.scene_contracts
    )
    assert validate_screenplay_narrative(
        screenplay,
        require=True,
        expected_scope_id=EPISODE_ID,
        authorized_source_chapters=episode["authorized_source_chapters"],
    ) == []
