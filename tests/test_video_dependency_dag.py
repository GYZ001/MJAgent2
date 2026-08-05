import json
import sqlite3

import pytest

from app import db, hiagent
import app.video_plan as video_plan
from app.video_plan import (
    AssetSource,
    EpisodeVideoGenerationPlan,
    PlanAssetRequirement,
    ProviderVideoCapabilitySnapshot,
    ShotVideoGenerationPlan,
    VideoGenerationMode,
    VideoInputIntent,
    VideoPlanValidationError,
    bind_plan_release_identity,
    current_storyboard_release_manifest,
    validate_episode_plan,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,storyboard_artifact_id,created_at
           ) VALUES('e','p',1,'storyboard_rev_1',0)"""
    )
    for shot_no in range(1, 4):
        contract = {
            "shot_id": f"SH-{shot_no}",
            "narrative_boundary_from_previous": (
                None if shot_no == 1 else {
                    "boundary_id": f"NB-SH-{shot_no - 1}-SH-{shot_no}",
                    "previous_shot_id": f"SH-{shot_no - 1}",
                    "next_shot_id": f"SH-{shot_no}",
                    "narrative_relation": "ordered_handoff",
                    "cut_motivation": "The previous beat has completed.",
                }
            ),
        }
        conn.execute(
            """INSERT INTO shots(
                   id,shot_uid,episode_id,shot_no,duration_s,shot_size,camera_move,
                   scene_setting,characters,action_desc,dialogues,transition,
                   shot_contract_json
               ) VALUES(?,?,?,?,5,'中景','固定','同一空间','[]',?,'[]','硬切',?)""",
            (
                f"s{shot_no}", f"uid-{shot_no}", "e", shot_no,
                f"关系驱动动作 {shot_no}", json.dumps(contract, ensure_ascii=False),
            ),
        )
    conn.commit()
    return conn


def _snapshot(*, continuation: bool = False) -> ProviderVideoCapabilitySnapshot:
    return ProviderVideoCapabilitySnapshot(
        id="cap-1",
        provider="provider",
        model="model",
        supports_reference_image=True,
        supports_first_frame=True,
        supports_last_frame=True,
        supports_first_last_pair=True,
        supports_reference_video=True,
        supports_true_video_continuation=continuation,
        semantic_continuation_success=continuation,
        probe_time=1,
        technical_success=True,
    )


def _shot(
    shot_no: int,
    mode: VideoGenerationMode,
    *,
    depends_on: str | None = None,
    intent: VideoInputIntent | None = None,
    required_assets: list[PlanAssetRequirement] | None = None,
) -> ShotVideoGenerationPlan:
    return ShotVideoGenerationPlan(
        shot_plan_id=f"svp-{shot_no}",
        episode_video_plan_id="evp-1",
        source_storyboard_revision_id="storyboard_rev_1",
        shot_id=f"SH-{shot_no}",
        published_shot_id=f"SH-{shot_no}",
        shot_no=shot_no,
        mode=mode,
        video_input_intent=intent,
        depends_on_shot_id=depends_on,
        required_assets=required_assets or [],
        reason_codes=["RELATION_DRIVEN"],
        confidence=0.9,
        estimated_latency_ms=100,
        estimated_cost=1,
        capability_snapshot_id="cap-1",
    )


def _validate_plan(
    plan: EpisodeVideoGenerationPlan,
    conn: sqlite3.Connection,
    snapshot: ProviderVideoCapabilitySnapshot,
) -> EpisodeVideoGenerationPlan:
    rows = conn.execute("SELECT * FROM shots ORDER BY shot_no").fetchall()
    manifest = current_storyboard_release_manifest("e", conn=conn)
    bind_plan_release_identity(plan, list(rows), manifest)
    return validate_episode_plan(
        plan, list(rows), snapshot, release_manifest=manifest,
    )


def test_dependency_dag_keeps_independent_shots_parallel() -> None:
    conn = _conn()
    plan = EpisodeVideoGenerationPlan(
        episode_video_plan_id="evp-1",
        episode_id="e",
        plan_revision=1,
        source_storyboard_revision_id="storyboard_rev_1",
        capability_snapshot_id="cap-1",
        shots=[
            _shot(1, VideoGenerationMode.REFERENCE_IMAGE_MODE),
            _shot(
                2,
                VideoGenerationMode.FIRST_LAST_FRAME_MODE,
                depends_on="SH-1",
                required_assets=[
                    PlanAssetRequirement(
                        role="first_frame",
                        source=AssetSource.PREVIOUS_ADOPTED_TAIL,
                        source_shot_id="SH-1",
                    ),
                    PlanAssetRequirement(
                        role="last_frame",
                        source=AssetSource.STATIC_BOUNDARY_ASSET,
                    ),
                ],
            ),
            _shot(
                3,
                VideoGenerationMode.FIRST_LAST_FRAME_MODE,
                required_assets=[
                    PlanAssetRequirement(
                        role="first_frame",
                        source=AssetSource.PREVIOUS_STATIC_TAIL,
                        source_shot_id="SH-2",
                    ),
                    PlanAssetRequirement(
                        role="last_frame",
                        source=AssetSource.STATIC_BOUNDARY_ASSET,
                    ),
                ],
            ),
        ],
    )

    result = _validate_plan(plan, conn, _snapshot())

    assert result.shots[0].mode == VideoGenerationMode.REFERENCE_IMAGE_MODE
    assert result.shots[1].depends_on_shot_id == "s1"
    assert result.shots[2].depends_on_shot_id is None
    assert result.shots[2].required_assets[0].source == AssetSource.PREVIOUS_STATIC_TAIL
    assert result.shots[2].required_assets[0].source_shot_id == "s2"
    assert result.safe_parallelism_ratio == pytest.approx(2 / 3, abs=0.001)
    assert result.critical_path_latency_ms == 200


def test_first_shot_and_cycle_are_rejected_before_queue() -> None:
    conn = _conn()
    plan = EpisodeVideoGenerationPlan(
        episode_video_plan_id="evp-1",
        episode_id="e",
        plan_revision=1,
        source_storyboard_revision_id="storyboard_rev_1",
        capability_snapshot_id="cap-1",
        shots=[
            _shot(1, VideoGenerationMode.FIRST_LAST_FRAME_MODE),
            _shot(
                2,
                VideoGenerationMode.VIDEO_INPUT_MODE,
                depends_on="SH-3",
                intent=VideoInputIntent.MOTION_REFERENCE,
                required_assets=[
                    PlanAssetRequirement(
                        role="previous_adopted_video",
                        source=AssetSource.PREVIOUS_ADOPTED_VIDEO,
                    )
                ],
            ),
            _shot(
                3,
                VideoGenerationMode.VIDEO_INPUT_MODE,
                depends_on="SH-2",
                intent=VideoInputIntent.MOTION_REFERENCE,
                required_assets=[
                    PlanAssetRequirement(
                        role="previous_adopted_video",
                        source=AssetSource.PREVIOUS_ADOPTED_VIDEO,
                    )
                ],
            ),
        ],
    )

    with pytest.raises(VideoPlanValidationError) as exc:
        _validate_plan(plan, conn, _snapshot())

    codes = {item["code"] for item in exc.value.issues}
    assert "FIRST_SHOT_NO_PREDECESSOR" in codes
    assert "DEPENDENCY_NOT_UPSTREAM" in codes
    assert "DEPENDENCY_CYCLE" in codes


def test_unverified_true_continuation_is_rejected() -> None:
    conn = _conn()
    plan = EpisodeVideoGenerationPlan(
        episode_video_plan_id="evp-1",
        episode_id="e",
        plan_revision=1,
        source_storyboard_revision_id="storyboard_rev_1",
        capability_snapshot_id="cap-1",
        shots=[
            _shot(1, VideoGenerationMode.REFERENCE_IMAGE_MODE),
            _shot(
                2,
                VideoGenerationMode.VIDEO_INPUT_MODE,
                depends_on="SH-1",
                intent=VideoInputIntent.CONTINUE_PREVIOUS_TAKE,
                required_assets=[
                    PlanAssetRequirement(
                        role="previous_adopted_video",
                        source=AssetSource.PREVIOUS_ADOPTED_VIDEO,
                    )
                ],
            ),
            _shot(3, VideoGenerationMode.REFERENCE_IMAGE_MODE),
        ],
    )

    with pytest.raises(VideoPlanValidationError) as exc:
        _validate_plan(plan, conn, _snapshot(continuation=False))
    assert "PROVIDER_CAPABILITY_UNVERIFIED" in {
        item["code"] for item in exc.value.issues
    }


@pytest.mark.asyncio
async def test_ai_episode_plan_is_single_call_versioned_and_first_shot_is_fixed(
    monkeypatch,
) -> None:
    conn = _conn()
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return json.dumps({
            "shots": [
                {
                    "shot_id": "SH-1",
                    "mode": "VIDEO_INPUT_MODE",
                    "video_input_intent": "MOTION_REFERENCE",
                    "depends_on_shot_id": "SH-1",
                    "required_assets": [{
                        "role": "previous_adopted_video",
                        "source": "PREVIOUS_ADOPTED_VIDEO",
                    }],
                    "reason_codes": ["MODEL_PROPOSED_INVALID_FIRST_MODE"],
                    "confidence": 0.9,
                    "estimated_latency_ms": 100,
                    "estimated_cost": 1,
                },
                {
                    "shot_id": "SH-2",
                    "mode": "FIRST_LAST_FRAME_MODE",
                    "required_assets": [
                        {"role": "first_frame", "source": "STATIC_BOUNDARY_ASSET"},
                        {"role": "last_frame", "source": "STATIC_BOUNDARY_ASSET"},
                    ],
                    "reason_codes": ["EXACT_START_END_STATE_REQUIRED"],
                    "confidence": 0.9,
                    "estimated_latency_ms": 100,
                    "estimated_cost": 1,
                },
                {
                    "shot_id": "SH-3",
                    "mode": "REFERENCE_IMAGE_MODE",
                    "required_assets": [],
                    "reason_codes": ["INTENTIONAL_RECOMPOSITION"],
                    "confidence": 0.9,
                    "estimated_latency_ms": 100,
                    "estimated_cost": 1,
                },
            ],
        })

    monkeypatch.setattr(video_plan, "get_conn", lambda: conn)
    monkeypatch.setattr(hiagent, "chat", fake_chat)
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(hiagent, "active_model", lambda *_args, **_kwargs: "model")
    monkeypatch.setattr(
        "app.multiview.resolve_shot_asset_dependencies",
        lambda **kwargs: {
            "characters": [],
            "scene": None,
            "input_fingerprint": f"assets-{kwargs['shot_id']}",
            "status": "ready",
        },
    )

    plan = await video_plan.generate_episode_plan("e", force=True, conn=conn)

    assert len(calls) == 1
    assert plan.plan_revision == 1
    assert plan.shots[0].mode == VideoGenerationMode.REFERENCE_IMAGE_MODE
    assert plan.shots[0].depends_on_shot_id is None
    assert "FIRST_SHOT_NO_PREDECESSOR" in plan.shots[0].reason_codes
    assert conn.execute(
        "SELECT COUNT(*) FROM shot_video_generation_plans"
    ).fetchone()[0] == 3
