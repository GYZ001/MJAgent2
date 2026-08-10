"""Read-only offline replay for an approved screenplay Artifact."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.narrative import (  # noqa: E402
    validate_screenplay_narrative,
    validate_storyboard_narrative,
)
from app.narrative_outline import (  # noqa: E402
    narrative_outline_action_delivery_errors,
)
from app.narrative_priority import (  # noqa: E402
    authoritative_outline_duration_s,
    compile_authoritative_delivery_outline,
)
from app.production.screenplay_document import (  # noqa: E402
    ScreenplayDocument,
    document_to_screenplay,
)
from app.schemas import Bible  # noqa: E402
from app.validators import (  # noqa: E402
    narrative_outline_action_capacity_errors,
    outline_key_line_capacity_errors,
    outline_key_line_speaker_errors,
    outline_scene_coverage_errors,
)
from app.video_cost_model import initial_shot_generation_cost  # noqa: E402


def _readonly_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def replay(
    *,
    db_path: Path,
    artifact_id: str,
    project_id: str | None,
) -> dict:
    conn = _readonly_connection(db_path)
    artifact = conn.execute(
        "SELECT scope_id,content_json FROM artifacts WHERE id=?",
        (artifact_id,),
    ).fetchone()
    if artifact is None:
        raise ValueError(f"Artifact 不存在：{artifact_id}")
    episode = conn.execute(
        "SELECT id,project_id,target_duration_s FROM episodes WHERE id=?",
        (artifact["scope_id"],),
    ).fetchone()
    resolved_project_id = (
        project_id
        or (str(episode["project_id"]) if episode is not None else "")
    )
    project = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?",
        (resolved_project_id,),
    ).fetchone()
    bible = (
        Bible.model_validate_json(project["bible_json"])
        if project is not None and project["bible_json"]
        else None
    )
    document = ScreenplayDocument.model_validate_json(
        artifact["content_json"]
    )
    screenplay = document_to_screenplay(document)
    projected, outline, audit = compile_authoritative_delivery_outline(
        screenplay,
        bible=bible,
    )
    screenplay_errors = validate_screenplay_narrative(
        projected,
        expected_scope_id=projected.id,
    )
    outline_errors = [
        *narrative_outline_action_delivery_errors(outline, projected),
        *outline_key_line_capacity_errors(outline, projected),
        *outline_key_line_speaker_errors(outline, projected),
        *outline_scene_coverage_errors(outline, projected, bible),
        *narrative_outline_action_capacity_errors(
            outline,
            projected.narrative_plan,
        ),
        *validate_storyboard_narrative(
            None,
            projected,
            outline=outline,
            complete=True,
            expected_scope_id=projected.id,
        ),
    ]
    source_coverage = {
        decision.source_segment_id: decision.disposition
        for decision in projected.source_coverage
    }
    duration_s = authoritative_outline_duration_s(outline)
    first_pass_cost = round(sum(
        initial_shot_generation_cost(float(shot.duration_s or 0))
        for shot in outline.shots
    ), 6)
    event_ids = {
        event_id
        for shot in outline.shots
        for event_id in shot.event_ids
    }
    information_ids = {
        info_id
        for shot in outline.shots
        for info_id in shot.information_ids
    }
    return {
        "artifact_id": artifact_id,
        "episode_id": artifact["scope_id"],
        "stored_target_duration_s": (
            int(episode["target_duration_s"])
            if episode is not None else None
        ),
        "authoritative_target_duration_s": duration_s,
        "shot_count": len(outline.shots),
        "duration_s": duration_s,
        "duration_histogram": {
            str(duration): sum(
                int(shot.duration_s or 0) == duration
                for shot in outline.shots
            )
            for duration in sorted({
                int(shot.duration_s or 0)
                for shot in outline.shots
            })
        },
        "story_event_coverage": {
            "covered": len(event_ids),
            "total": len(projected.narrative_plan.events),
        },
        "information_coverage": {
            "covered": len(information_ids),
            "total": len(projected.information_ledger),
        },
        "source_coverage": {
            "covered": len(source_coverage),
            "total": len(projected.source_coverage),
            "by_disposition": {
                disposition: sum(
                    value == disposition
                    for value in source_coverage.values()
                )
                for disposition in sorted(set(source_coverage.values()))
            },
        },
        "picture_projection": audit["picture_projection"],
        "merged_delivery_beat_count": len(
            audit["beat_merge_changes"]
        ),
        "authoritative_first_pass_budget_cny": first_pass_cost,
        "screenplay_errors": screenplay_errors,
        "outline_errors": list(dict.fromkeys(outline_errors)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "manju.db")
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--project-id")
    args = parser.parse_args()
    print(json.dumps(
        replay(
            db_path=args.db,
            artifact_id=args.artifact_id,
            project_id=args.project_id,
        ),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
