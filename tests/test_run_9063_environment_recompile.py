"""Live, read-only replay for run_9063aa816692's exact merged IR."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.domain.common import _episode_source_text
from app.production.screenplay_authority import (
    screenplay_authorized_source_chapters,
)
from app.schemas import Bible
from app.screenplay_ir import (
    ScreenplayGenerationIR,
    ScreenplayIRFidelityError,
    compile_screenplay_ir,
)


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = ROOT / "data" / "manju.db"
EPISODE_ID = "ep_711b29204aa9"
MERGED_IR_ID = "art_5283a3ccb12b"


@pytest.mark.live_integration
def test_run_21a_old_merged_ir_requires_state_subject_rebuild() -> None:
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
    payload = json.loads(artifact_row[0])
    invalid_unattributed_units = {
        unit["unit_key"]
        for scene in payload["scenes"]
        for unit in scene["units"]
        if (
            unit.get("kind") == "action"
            and not unit.get("actor_keys")
            and not unit.get("state_subject_key")
            and "environment_only" not in unit
        )
    }
    assert len(invalid_unattributed_units) >= 15

    with pytest.raises(
        ScreenplayIRFidelityError,
        match="state_subject_key/environment_only.*旧 IR 必须重建",
    ):
        compile_screenplay_ir(
            ScreenplayGenerationIR.model_validate(payload),
            episode=episode,
            source_text=source_text,
            bible=Bible.model_validate(json.loads(project_row[0])),
        )
