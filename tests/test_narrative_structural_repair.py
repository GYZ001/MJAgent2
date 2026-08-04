from __future__ import annotations

import pytest

from app.narrative_repair import (
    SemanticOutlineOperation,
    apply_semantic_outline_operations,
)
from app.repair_router import route_issues
from app.schemas import Shot, Storyboard, StoryboardOutline, StoryboardOutlineShot
from app.storyboard_supervisor import (
    SupervisorCheckpoint,
    _apply_repair,
    _merge_repair_candidate,
    _outline_changed_window,
)
from tests.test_narrative_continuity import (
    _screenplay as _narrative_screenplay,
    _settled_followup_shot,
    _shot as _narrative_shot,
)


def _outline(*shot_ids: str) -> StoryboardOutline:
    return StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=position,
                shot_id=shot_id,
                beat=f"beat:{shot_id}",
            )
            for position, shot_id in enumerate(shot_ids, start=1)
        ],
    )


def _outline_shot(shot_id: str, *, beat: str | None = None) -> StoryboardOutlineShot:
    return StoryboardOutlineShot(
        shot_no=999,
        shot_id=shot_id,
        beat=beat or f"beat:{shot_id}",
    )


def _board(*shot_ids: str) -> Storyboard:
    return Storyboard(
        episode_no=1,
        shots=[
            Shot(
                shot_no=position,
                shot_id=shot_id,
                duration_s=5,
                shot_size="中景",
                camera_move="固定",
                characters=["entity"],
                action_desc=f"action:{shot_id}",
            )
            for position, shot_id in enumerate(shot_ids, start=1)
        ],
    )


def _ids(value: Storyboard | StoryboardOutline) -> list[str]:
    return [shot.shot_id for shot in value.shots]


def test_semantic_outline_operations_compose_without_story_classification() -> None:
    official = _outline("SH-A", "SH-B", "SH-C")
    operations = [
        SemanticOutlineOperation(
            op="replace_outline_shot",
            target={"shot_id": "SH-B"},
            value=_outline_shot("SH-B", beat="replacement-by-stable-id"),
        ),
        SemanticOutlineOperation(
            op="insert_outline_shot",
            target={"after_shot_id": "SH-A"},
            value=_outline_shot("SH-X"),
        ),
        SemanticOutlineOperation(
            op="delete_outline_shot",
            target={"shot_id": "SH-C"},
        ),
        SemanticOutlineOperation(
            op="move_outline_shot",
            target={"shot_id": "SH-B", "to_index": 0},
        ),
    ]

    candidate, events = apply_semantic_outline_operations(official, operations)

    assert _ids(candidate) == ["SH-B", "SH-A", "SH-X"]
    assert [shot.shot_no for shot in candidate.shots] == [1, 2, 3]
    assert candidate.shots[0].beat == "replacement-by-stable-id"
    assert [event["op"] for event in events] == [
        "replace_outline_shot",
        "insert_outline_shot",
        "delete_outline_shot",
        "move_outline_shot",
    ]
    assert events[0]["before_shot_id"] == events[0]["after_shot_id"] == "SH-B"
    assert events[2]["before_shot_id"] == "SH-C"
    assert events[3]["shot_id"] == "SH-B"
    assert _ids(official) == ["SH-A", "SH-B", "SH-C"]
    assert [shot.shot_no for shot in official.shots] == [1, 2, 3]


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            SemanticOutlineOperation(
                op="insert_outline_shot",
                target={"after_shot_id": "SH-A"},
                value=_outline_shot(""),
            ),
            "empty shot_id",
        ),
        (
            SemanticOutlineOperation(
                op="insert_outline_shot",
                target={"after_shot_id": "SH-A"},
                value=_outline_shot("SH-B"),
            ),
            "duplicate shot_id",
        ),
    ],
)
def test_semantic_outline_operations_reject_invalid_stable_ids(
    operation: SemanticOutlineOperation,
    message: str,
) -> None:
    official = _outline("SH-A", "SH-B")

    with pytest.raises(ValueError, match=message):
        apply_semantic_outline_operations(official, [operation])

    assert _ids(official) == ["SH-A", "SH-B"]


def test_semantic_outline_operation_rejects_unknown_stable_target() -> None:
    official = _outline("SH-A", "SH-B")
    operation = SemanticOutlineOperation(
        op="delete_outline_shot",
        target={"shot_id": "SH-UNKNOWN"},
    )

    with pytest.raises(KeyError, match="shot_id not found: SH-UNKNOWN"):
        apply_semantic_outline_operations(official, [operation])


def test_semantic_outline_operation_rejects_ambiguous_typed_coordinates() -> None:
    official = _outline("SH-A", "SH-B")
    operation = SemanticOutlineOperation(
        op="stage_new_relation",
        executor="insert_outline_shot",
        target={"after_shot_id": "SH-A", "to_index": 0},
        value=_outline_shot("SH-X"),
    )

    with pytest.raises(ValueError, match="at most one insertion coordinate"):
        apply_semantic_outline_operations(official, [operation])

    assert _ids(official) == ["SH-A", "SH-B"]


@pytest.mark.parametrize(
    ("before_ids", "after_ids", "expected"),
    [
        (("SH-A", "SH-B", "SH-C"), ("SH-A", "SH-B", "SH-C"), None),
        (("SH-A", "SH-B", "SH-C"), ("SH-A", "SH-X", "SH-C"), (2, 2, 2)),
        (("SH-A", "SH-B", "SH-C"), ("SH-A", "SH-X", "SH-B", "SH-C"), (2, 1, 2)),
        (("SH-A", "SH-B", "SH-C"), ("SH-A", "SH-C"), (2, 2, 1)),
        (("SH-A", "SH-B", "SH-C"), ("SH-C", "SH-A", "SH-B"), (1, 3, 3)),
    ],
)
def test_outline_changed_window_is_the_minimal_identity_span(
    before_ids: tuple[str, ...],
    after_ids: tuple[str, ...],
    expected: tuple[int, int, int] | None,
) -> None:
    assert _outline_changed_window(
        _outline(*before_ids),
        _outline(*after_ids),
    ) == expected


def test_outline_changed_window_detects_authority_change_under_same_stable_id() -> None:
    before = _outline("SH-A", "SH-B", "SH-C")
    after = before.model_copy(deep=True)
    after.shots[1].beat = "changed-authoritative-task"

    assert _outline_changed_window(before, after) == (2, 2, 2)


def test_split_shot_route_alias_preserves_selected_semantic_assessment() -> None:
    selected_assessment = {
        "strategy": "split_shot",
        "expected_narrative_gain": 0.8,
        "destructive_cost": 0.2,
        "satisfies_gap_test": True,
        "passes_marginal_gain_test": True,
        "preserves_invariants": True,
        "rationale": "Measured capacity requires one adjacent structural node.",
        "outline_operations": [{
            "op": "insert_outline_shot",
            "target": {"after_shot_id": "SH-A"},
            "value": _outline_shot("SH-X").model_dump(mode="json"),
        }],
    }
    diagnosis = {
        "diagnosis_id": "NRD-alias",
        "semantic_gap": "A measured relation exceeds the current window.",
        "affected_shot_nos": [1],
        "candidate_assessments": [
            {
                "strategy": "repair_current",
                "expected_narrative_gain": 0.1,
                "destructive_cost": 0.1,
            },
            selected_assessment,
        ],
        "selected_strategy": "split_shot",
        "selection_reason": "The selected operation has positive marginal gain.",
    }

    plan = route_issues(
        ["[SEMANTIC_GAP_OTHER] shot_no=1 relation capacity is insufficient"],
        validated_prefix_end=1,
        next_shot_no=2,
        semantic_diagnosis=diagnosis,
    )

    assert plan.strategy == "split_adjacent_shot"
    assert plan.selected_candidate_id == "candidate-split"
    assert plan.semantic_diagnosis["selected_strategy"] == "split_shot"
    assert next(
        item
        for item in plan.semantic_diagnosis["candidate_assessments"]
        if item["strategy"] == "split_shot"
    ) == selected_assessment


def test_open_strategy_applies_its_typed_semantic_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screenplay = _narrative_screenplay()
    current = Storyboard(episode_no=1, shots=[_narrative_shot()])
    official_outline = StoryboardOutline(
        episode_no=1,
        shots=[StoryboardOutlineShot.model_validate(
            current.shots[0].model_dump(mode="json")
        )],
        readability_windows=[
            window.model_copy(deep=True)
            for window in screenplay.narrative_plan.readability_windows
        ],
    )
    inserted = StoryboardOutlineShot.model_validate(
        _settled_followup_shot().model_dump(mode="json")
    )
    open_strategy = "stage_relation_specific_assimilation"
    selected_assessment = {
        "strategy": open_strategy,
        "expected_narrative_gain": 0.8,
        "destructive_cost": 0.2,
        "satisfies_gap_test": True,
        "passes_marginal_gain_test": True,
        "preserves_invariants": True,
        "rationale": "The adjacent node is a validated structural candidate.",
        "outline_operations": [{
            "op": "make_the_relation_readable",
            "executor": "insert_outline_shot",
            "target": {"after_shot_id": "SH-1"},
            "value": inserted.model_dump(mode="json"),
        }],
    }
    plan = route_issues(
        ["[SEMANTIC_GAP_OTHER] shot_no=1 measured relation exceeds its window"],
        validated_prefix_end=1,
        next_shot_no=2,
        semantic_diagnosis={
            "diagnosis_id": "NRD-alias-apply",
            "semantic_gap": "The measured relation needs one adjacent node.",
            "affected_shot_nos": [1],
            "candidate_assessments": [
                {"strategy": "repair_current", "expected_narrative_gain": 0.1},
                selected_assessment,
            ],
            "selected_strategy": open_strategy,
            "selection_reason": "The selected operation has positive marginal gain.",
            "scope": "structure",
            "execution_verified": True,
        },
    )

    class QueryResult:
        def __init__(self, *, one=None, many=None):
            self.one = one
            self.many = many or []

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.many

    class CandidateOnlyConnection:
        commits = 0

        def execute(self, sql, _params=()):
            if "SELECT * FROM shots" in sql:
                return QueryResult(many=[{"shot_no": 1}])
            if "SELECT episode_no,target_duration_s,storyboard_outline_json" in sql:
                return QueryResult(one={
                    "episode_no": 1,
                    "target_duration_s": 10,
                    "storyboard_outline_json": official_outline.model_dump_json(),
                })
            if "SELECT * FROM episodes" in sql:
                return QueryResult(one={"id": "episode-generic"})
            raise AssertionError(f"unexpected SQL in candidate-only planning: {sql}")

        def commit(self):
            self.commits += 1

    conn = CandidateOnlyConnection()
    monkeypatch.setattr(
        "app.storyboard_supervisor._board_from_rows",
        lambda _rows, _episode_no: current.model_copy(deep=True),
    )
    monkeypatch.setattr(
        "app.domain.common._load_screenplay",
        lambda _row: screenplay,
    )
    from app.production.screenplay_authority import DownstreamScreenplayContext

    monkeypatch.setattr(
        "app.production.screenplay_authority.resolve_downstream_screenplay",
        lambda _episode_id, **_kwargs: DownstreamScreenplayContext(
            screenplay=screenplay,
            narrative_authority_required=True,
            immutable_authority_required=True,
        ),
    )
    monkeypatch.setattr(
        "app.storyboard_supervisor.save_checkpoint",
        lambda _checkpoint, **_kwargs: None,
    )

    planned = _apply_repair(
        SupervisorCheckpoint(episode_id="episode-generic", validated_prefix_end=1),
        plan,
        conn,
        "episode-generic",
        list(current.shots),
        official_outline,
    )

    assert "candidate_outline" in planned.last_repair, planned.last_repair.get("reason")
    candidate_outline = StoryboardOutline.model_validate(
        planned.last_repair["candidate_outline"]
    )
    assert planned.last_repair["mode"] == "structure"
    assert planned.last_repair["structure_old_end"] == 1
    assert planned.last_repair["structure_new_end"] == 2
    assert planned.last_repair["semantic_outline_events"] == [{
        "op": "insert_outline_shot",
        "intent_op": "make_the_relation_readable",
        "index": 1,
        "after_shot_id": "SH-2",
    }]
    assert _ids(candidate_outline) == ["SH-1", "SH-2"]
    assert _ids(official_outline) == ["SH-1"]


@pytest.mark.parametrize(
    ("window_start", "old_end", "candidate_ids", "expected_ids"),
    [
        (2, 1, ("SH-X",), ("SH-A", "SH-X", "SH-B", "SH-C", "SH-D")),
        (2, 2, (), ("SH-A", "SH-C", "SH-D")),
        (2, 3, ("SH-C", "SH-B"), ("SH-A", "SH-C", "SH-B", "SH-D")),
    ],
    ids=("insert", "delete", "move"),
)
def test_structure_mode_merges_candidate_without_mutating_official_board(
    window_start: int,
    old_end: int,
    candidate_ids: tuple[str, ...],
    expected_ids: tuple[str, ...],
) -> None:
    official = _board("SH-A", "SH-B", "SH-C", "SH-D")
    official_dump = official.model_dump(mode="json")
    checkpoint = SupervisorCheckpoint(
        episode_id="episode-generic",
        last_repair={
            "mode": "structure",
            "window_start": window_start,
            "structure_old_end": old_end,
        },
        repair_candidate_shots=[
            shot.model_dump(mode="json")
            for shot in _board(*candidate_ids).shots
        ],
    )

    merged = _merge_repair_candidate(official, checkpoint)

    assert _ids(merged) == list(expected_ids)
    assert [shot.shot_no for shot in merged.shots] == list(
        range(1, len(expected_ids) + 1)
    )
    assert official.model_dump(mode="json") == official_dump
    assert _ids(official) == ["SH-A", "SH-B", "SH-C", "SH-D"]
