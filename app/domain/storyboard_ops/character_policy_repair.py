"""分镜生成过程中角色策略修复项的落库。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 mutation_primitives。
"""
from __future__ import annotations

import json

from app import (
    config,
    worker,
)
from app.db import log_provider_call
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import (
    Evaluation,
    EvidenceArtifact,
)
from app.schemas import Storyboard

from .mutation_primitives import _shot_contract_json


def _persist_storyboard_character_policy_repairs(
    conn, episode_id: str, board: Storyboard, changes: list[dict]
) -> list[str]:
    """Persist deterministic repairs as derived T1 candidates, preserving lineage.

    The character-policy evaluation only proves this normalization, not every storyboard
    gate, so the derived artifact must not be committed as T2 on its own.
    """
    material = [change for change in changes if change.get("mutated")]
    if not material:
        return []
    contract_version = get_contract("storyboard").version
    artifact_ids: list[str] = []
    by_shot = {shot.shot_no: shot for shot in board.shots}
    for shot_no in dict.fromkeys(int(change["shot_no"]) for change in material):
        row = conn.execute(
            "SELECT id, storyboard_artifact_id FROM shots WHERE episode_id=? AND shot_no=?",
            (episode_id, shot_no),
        ).fetchone()
        shot = by_shot.get(shot_no)
        if row is None or shot is None:
            continue
        shot_changes = [change for change in material if int(change["shot_no"]) == shot_no]
        previous_artifact_id = row["storyboard_artifact_id"]
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="storyboard_shot",
            scope_type="storyboard_checkpoint",
            scope_id=f"{episode_id}:{shot_no}",
            status="candidate",
            trust_level="T1",
            content=shot.model_dump(mode="json"),
            parent_artifact_ids=[previous_artifact_id] if previous_artifact_id else [],
            contract_version=contract_version,
        ))
        evidence_repository.create_evaluation(
            artifact["id"],
            Evaluation(
                evaluator_type="deterministic",
                evaluator_name="storyboard_character_policy",
                evaluator_version=contract_version,
                status="passed",
                hard_gate_passed=True,
                score=100,
                evidence={
                    "policy": "functional_extra_v1",
                    "scope": "character_policy_only",
                    "changes": shot_changes,
                },
            ),
        )
        if previous_artifact_id:
            evidence_repository.invalidate_descendants(
                previous_artifact_id,
                f"镜头角色合同已由 {artifact['id']} 修订",
                exclude_ids={str(artifact["id"])},
            )
        has_runtime_derivatives = conn.execute(
            """SELECT EXISTS(SELECT 1 FROM shot_versions WHERE shot_id=?)
                      OR EXISTS(SELECT 1 FROM shot_scenes WHERE shot_id=?) AS present""",
            (row["id"], row["id"]),
        ).fetchone()["present"]
        if has_runtime_derivatives:
            worker.clear_shot_artifacts(row["id"])
        conn.execute(
            """UPDATE shots SET characters=?, action_desc=?, first_frame_desc=?,
               last_frame_desc=?, narration=?, dialogues=?, shot_contract_json=?,
               continuity_mode=?, observed_state_out=?, storyboard_artifact_id=? WHERE id=?""",
            (
                json.dumps(shot.characters, ensure_ascii=False),
                shot.action_desc,
                shot.first_frame_desc,
                shot.last_frame_desc,
                shot.narration,
                json.dumps([dialogue.model_dump() for dialogue in shot.dialogues], ensure_ascii=False),
                _shot_contract_json(shot),
                shot.continuity_mode,
                shot.observed_state_out,
                artifact["id"],
                row["id"],
            ),
        )
        artifact_ids.append(str(artifact["id"]))
        log_provider_call(
            "storyboard_character_policy",
            config.MODEL_TEXT,
            "CHARACTER_POLICY_REPAIRED",
            None,
            0,
            meta={
                "episode_id": episode_id,
                "shot_no": shot_no,
                "contract_version": contract_version,
                "artifact_id": artifact["id"],
                "changes": shot_changes,
            },
        )
    conn.commit()
    return artifact_ids
