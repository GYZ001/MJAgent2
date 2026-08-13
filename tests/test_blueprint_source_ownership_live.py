"""Read-only production replay for run_be31's first Blueprint shard."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.narrative_blueprint import (
    NarrativeBlueprintShard,
    validate_narrative_blueprint_shard,
)


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = ROOT / "data" / "manju.db"
ARTIFACT_ID = "art_3a64327feeca"


@pytest.mark.live_integration
def test_run_be31_same_scene_duplicate_src0014_is_rejected() -> None:
    if not PRODUCTION_DB.exists():
        pytest.skip("production database is not present")
    with sqlite3.connect(
        f"file:{PRODUCTION_DB}?mode=ro",
        uri=True,
    ) as conn:
        row = conn.execute(
            "SELECT content_json FROM artifacts WHERE id=?",
            (ARTIFACT_ID,),
        ).fetchone()
    if row is None:
        pytest.skip("run_be31 Blueprint shard fixture is not present")

    payload = json.loads(row[0])
    canonical = NarrativeBlueprintShard.model_validate(payload)
    canonical_errors = validate_narrative_blueprint_shard(
        canonical,
        expected_episode_no=canonical.episode_no,
        expected_shard_index=canonical.shard_index,
        expected_source_segment_ids=canonical.source_segment_ids,
    )
    assert not any("SOURCE_DUPLICATE" in error for error in canonical_errors)

    mutated = canonical.model_copy(deep=True)
    mutated.nodes[2].dramatic_load = 1
    mutated.nodes[3].dramatic_load = 1
    mutated.nodes[3].source_segment_ids.insert(0, "SRC0014")

    errors = validate_narrative_blueprint_shard(
        mutated,
        expected_episode_no=mutated.episode_no,
        expected_shard_index=mutated.shard_index,
        expected_source_segment_ids=mutated.source_segment_ids,
    )

    assert any(
        "[BLUEPRINT_SHARD_PICTURE_SOURCE_DUPLICATE]" in error
        and "SRC0014" in error
        and "S001-node_003" in error
        and "S001-node_004" in error
        for error in errors
    )
