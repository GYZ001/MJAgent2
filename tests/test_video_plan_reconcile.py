import json
import sqlite3

import pytest

from app import db, hiagent, worker
from app.video_plan import (
    AssetSource,
    EpisodeVideoGenerationPlan,
    PlanAssetRequirement,
    ProviderVideoCapabilitySnapshot,
    SHOT_RELATION_ENUM_CONTRACT,
    ShotVideoGenerationPlan,
    VideoGenerationMode,
    VideoPlanValidationError,
    active_plan_is_current,
    apply_scene_boundary_strategy,
    assert_video_provider_submission_authority,
    bind_plan_release_identity,
    create_local_replan_revision,
    current_storyboard_release_manifest,
    normalize_ai_shot_plan_candidate,
    publish_plan,
    reconcile_adopted_revision,
    save_capability_snapshot,
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
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,storyboard_artifact_id,created_at
           ) VALUES('e','p',1,'storyboard_rev_1',0)"""
    )
    for number in (1, 2):
        conn.execute(
            """INSERT INTO shots(
                   id,shot_uid,episode_id,shot_no,duration_s,shot_size,camera_move,
                   scene_setting,characters,action_desc,dialogues,transition,
                   shot_contract_json
               ) VALUES(?,?,?,?,5,'中景','固定','空间','[]','动作','[]','硬切',?)""",
            (
                f"s{number}", f"uid-{number}", "e", number,
                json.dumps({"shot_id": f"SH-{number}"}, ensure_ascii=False),
            ),
        )
    snapshot = ProviderVideoCapabilitySnapshot(
        id="cap-1",
        provider="provider",
        model="model",
        supports_reference_image=True,
        supports_first_frame=True,
        supports_last_frame=True,
        supports_first_last_pair=True,
        supports_reference_video=True,
        probe_time=1,
        technical_success=True,
    )
    save_capability_snapshot(snapshot, conn=conn)
    plan = EpisodeVideoGenerationPlan(
        episode_video_plan_id="evp-1",
        episode_id="e",
        plan_revision=1,
        source_storyboard_revision_id="storyboard_rev_1",
        capability_snapshot_id="cap-1",
        shots=[
            ShotVideoGenerationPlan(
                shot_plan_id="svp-1",
                episode_video_plan_id="evp-1",
                source_storyboard_revision_id="storyboard_rev_1",
                shot_id="SH-1",
                published_shot_id="SH-1",
                shot_no=1,
                mode=VideoGenerationMode.REFERENCE_IMAGE_MODE,
                confidence=0.9,
                capability_snapshot_id="cap-1",
            ),
            ShotVideoGenerationPlan(
                shot_plan_id="svp-2",
                episode_video_plan_id="evp-1",
                source_storyboard_revision_id="storyboard_rev_1",
                shot_id="SH-2",
                published_shot_id="SH-2",
                shot_no=2,
                mode=VideoGenerationMode.FIRST_LAST_FRAME_MODE,
                depends_on_shot_id="SH-1",
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
                confidence=0.9,
                capability_snapshot_id="cap-1",
            ),
        ],
    )
    rows = conn.execute("SELECT * FROM shots ORDER BY shot_no").fetchall()
    manifest = current_storyboard_release_manifest("e", conn=conn)
    bind_plan_release_identity(plan, list(rows), manifest)
    validate_episode_plan(
        plan, list(rows), snapshot, release_manifest=manifest,
    )
    publish_plan(plan, conn=conn)
    for version_id, number in (("v1", 1), ("v2", 2)):
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
               ) VALUES(?,?,?,?,?,'succeeded',?,0)""",
            (version_id, "s1", number, "prompt", version_id, f"/tmp/{version_id}.mp4"),
        )
    conn.commit()
    return conn


def test_ai_plan_candidate_normalizes_known_relation_and_asset_aliases() -> None:
    normalized, changes = normalize_ai_shot_plan_candidate({
        "mode": "REFERENCE_IMAGE_MODE",
        "relations": {
            "temporal": "episode_start",
            "spatial": "establishing",
            "edit": "none",
            "action": "origin",
        },
        "required_assets": [{
            "role": "reference_image",
            "source": "ASSET_REPO",
            "source_shot_id": None,
        }],
    })

    assert normalized["relations"] == {
        "temporal": "new_domain",
        "spatial": "new_space",
        "edit": "unknown",
        "action": "starts_new_action",
    }
    assert normalized["required_assets"] == []
    assert len(changes) == 5


def test_ai_plan_prompt_contract_lists_every_strict_relation_enum() -> None:
    assert SHOT_RELATION_ENUM_CONTRACT == {
        "temporal": ["same_moment", "elapsed", "jump", "new_domain", "unknown"],
        "spatial": ["same_space", "adjacent_space", "new_space", "unknown"],
        "edit": [
            "continuous_take",
            "match_cut",
            "angle_cut",
            "reaction_cut",
            "reverse_angle",
            "insert_cut",
            "montage",
            "scene_cut",
            "unknown",
        ],
        "action": [
            "continues_same_action",
            "starts_new_action",
            "shows_result",
            "observes_result",
            "no_action",
            "unknown",
        ],
    }


@pytest.mark.parametrize(
    ("first_source", "source_shot_id", "supplied_dependency", "expected_dependency"),
    [
        ("STATIC_BOUNDARY_ASSET", None, "previous-shot", None),
        ("PREVIOUS_STATIC_TAIL", "previous-shot", "previous-shot", None),
        ("PREVIOUS_ADOPTED_TAIL", "previous-shot", None, "previous-shot"),
    ],
)
def test_first_last_dependency_is_derived_from_first_frame_source(
    first_source: str,
    source_shot_id: str | None,
    supplied_dependency: str | None,
    expected_dependency: str | None,
) -> None:
    normalized, changes = normalize_ai_shot_plan_candidate({
        "mode": "FIRST_LAST_FRAME_MODE",
        "depends_on_shot_id": supplied_dependency,
        "required_assets": [
            {
                "role": "first_frame",
                "source": first_source,
                "source_shot_id": source_shot_id,
            },
            {
                "role": "last_frame",
                "source": "STATIC_BOUNDARY_ASSET",
                "source_shot_id": None,
            },
        ],
    })

    assert normalized["depends_on_shot_id"] == expected_dependency
    assert changes[-1]["reason"] == "derived_from_first_frame_source"


def test_ai_plan_candidate_removes_automatic_mode_fallback() -> None:
    normalized, changes = normalize_ai_shot_plan_candidate({
        "mode": "FIRST_LAST_FRAME_MODE",
        "fallback_order": ["REFERENCE_IMAGE_MODE"],
    })

    assert normalized["fallback_order"] == []
    assert changes[-1]["reason"] == "automatic_mode_fallback_disabled"


@pytest.mark.parametrize(
    "relations",
    [
        {"temporal": "new_domain", "spatial": "same_space", "edit": "angle_cut"},
        {"temporal": "jump", "spatial": "new_space", "edit": "scene_cut"},
        {"temporal": "jump", "spatial": "same_space", "edit": "scene_cut"},
    ],
)
def test_scene_domain_change_requires_reference_mode(relations: dict) -> None:
    normalized, changes = normalize_ai_shot_plan_candidate({
        "mode": "FIRST_LAST_FRAME_MODE",
        "depends_on_shot_id": "previous-shot",
        "relations": relations,
        "state_dependency": "start_and_end",
        "motion_dependency": "pose",
        "required_assets": [
            {"role": "first_frame", "source": "PREVIOUS_ADOPTED_TAIL"},
            {"role": "last_frame", "source": "STATIC_BOUNDARY_ASSET"},
        ],
        "reason_codes": ["MODEL_SELECTED_BOUNDARY_MODE"],
    })

    assert normalized["mode"] == "REFERENCE_IMAGE_MODE"
    assert normalized["depends_on_shot_id"] is None
    assert normalized["required_assets"] == []
    assert normalized["state_dependency"] == "none"
    assert normalized["motion_dependency"] == "none"
    assert "SCENE_DOMAIN_CHANGED" in normalized["reason_codes"]
    assert any(
        change.get("reason") == "scene_domain_requires_recomposition"
        for change in changes
    )


def test_same_space_relation_does_not_force_a_mode() -> None:
    normalized, _changes = normalize_ai_shot_plan_candidate({
        "mode": "FIRST_LAST_FRAME_MODE",
        "relations": {
            "temporal": "elapsed",
            "spatial": "same_space",
            "edit": "angle_cut",
        },
    })

    assert normalized["mode"] == "FIRST_LAST_FRAME_MODE"


def test_scene_boundary_strategy_only_waits_for_each_scene_second_shot() -> None:
    shots = [
        ShotVideoGenerationPlan(
            source_storyboard_revision_id="rev",
            shot_id=f"shot-{number}",
            published_shot_id=f"shot-{number}",
            shot_no=number,
            mode=VideoGenerationMode.VIDEO_INPUT_MODE,
            video_input_intent="MOTION_REFERENCE",
            relations=relations,
            confidence=1,
            capability_snapshot_id="cap",
        )
        for number, relations in (
            (1, {}),
            (2, {"spatial": "same_space", "edit": "angle_cut"}),
            (3, {"spatial": "same_space", "edit": "continuous_take"}),
            (4, {"spatial": "new_space", "edit": "scene_cut"}),
            (5, {"spatial": "same_space", "edit": "angle_cut"}),
            (6, {"spatial": "same_space", "edit": "continuous_take"}),
        )
    ]

    apply_scene_boundary_strategy(shots)

    assert [item.mode for item in shots] == [
        VideoGenerationMode.REFERENCE_IMAGE_MODE,
        VideoGenerationMode.FIRST_LAST_FRAME_MODE,
        VideoGenerationMode.FIRST_LAST_FRAME_MODE,
        VideoGenerationMode.REFERENCE_IMAGE_MODE,
        VideoGenerationMode.FIRST_LAST_FRAME_MODE,
        VideoGenerationMode.FIRST_LAST_FRAME_MODE,
    ]
    assert [item.depends_on_shot_id for item in shots] == [
        None, "shot-1", None, None, "shot-4", None,
    ]
    assert [
        next(
            (
                asset.source
                for asset in item.required_assets
                if asset.role == "first_frame"
            ),
            None,
        )
        for item in shots
    ] == [
        None,
        AssetSource.PREVIOUS_ADOPTED_TAIL,
        AssetSource.PREVIOUS_STATIC_TAIL,
        None,
        AssetSource.PREVIOUS_ADOPTED_TAIL,
        AssetSource.PREVIOUS_STATIC_TAIL,
    ]


def test_published_scene_identity_overrides_ai_boundary_drift() -> None:
    shots = [
        ShotVideoGenerationPlan(
            source_storyboard_revision_id="rev",
            shot_id=f"shot-{number}",
            published_shot_id=f"shot-{number}",
            shot_no=number,
            mode=VideoGenerationMode.REFERENCE_IMAGE_MODE,
            relations=relations,
            confidence=1,
            capability_snapshot_id="cap",
        )
        for number, relations in (
            (1, {}),
            (2, {"spatial": "new_space", "edit": "scene_cut"}),
            (3, {"spatial": "same_space", "edit": "angle_cut"}),
            (4, {"spatial": "same_space", "edit": "angle_cut"}),
        )
    ]

    apply_scene_boundary_strategy(
        shots,
        scene_identity_by_shot_id={
            "shot-1": "set-a",
            "shot-2": "set-a",
            "shot-3": "set-b",
            "shot-4": "set-b",
        },
    )

    assert [item.mode for item in shots] == [
        VideoGenerationMode.REFERENCE_IMAGE_MODE,
        VideoGenerationMode.FIRST_LAST_FRAME_MODE,
        VideoGenerationMode.REFERENCE_IMAGE_MODE,
        VideoGenerationMode.FIRST_LAST_FRAME_MODE,
    ]
    assert [item.depends_on_shot_id for item in shots] == [
        None, "shot-1", None, "shot-3",
    ]


def test_first_adoption_binds_dependency_and_changed_adoption_stales_descendant() -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at,image_inputs
           ) VALUES('downstream','s2',1,'prompt','downstream','queued',0,?)""",
        (json.dumps({"shot_plan_id": "svp-2"}),),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               created_at,updated_at,provider_non_cancellable
           ) VALUES('job','video','s2','downstream','e','p','queued',0,0,0)"""
    )
    conn.commit()

    conn.execute("UPDATE shots SET adopted_version_id='v1' WHERE id='s1'")
    first = reconcile_adopted_revision("s1", "v1", conn=conn)
    assert first == {"bound": 1, "stale_shot_ids": []}
    dependency = conn.execute(
        "SELECT upstream_adopted_version_id FROM video_plan_dependencies"
    ).fetchone()
    assert dependency["upstream_adopted_version_id"] == "v1"
    prepared = conn.execute(
        "SELECT idem_key,image_inputs FROM shot_versions WHERE id='downstream'"
    ).fetchone()
    prepared_meta = json.loads(prepared["image_inputs"])
    assert prepared["idem_key"] != "downstream"
    assert prepared_meta["upstream_adopted_video_revision"] == "v1"
    assert prepared_meta["input_revision_fingerprints"][
        "upstream_adopted_video_revision"
    ] == "v1"
    assert conn.execute(
        "SELECT after_version_id FROM jobs WHERE id='job'"
    ).fetchone()["after_version_id"] == "v1"

    conn.execute("UPDATE shots SET adopted_version_id='v2' WHERE id='s1'")
    changed = reconcile_adopted_revision("s1", "v2", conn=conn)

    assert changed["stale_shot_ids"] == ["s2"]
    assert conn.execute(
        "SELECT status FROM shot_video_generation_plans WHERE id='svp-2'"
    ).fetchone()["status"] == "stale"
    job = conn.execute(
        "SELECT status,cancellation_requested,abandoned FROM jobs WHERE id='job'"
    ).fetchone()
    assert tuple(job) == ("stale", 1, 1)
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='downstream'"
    ).fetchone()["status"] == "stale"


def test_non_cancellable_stale_provider_result_remains_pollable_but_cannot_auto_adopt() -> None:
    conn = _conn()
    conn.execute("UPDATE shots SET adopted_version_id='v1' WHERE id='s1'")
    reconcile_adopted_revision("s1", "v1", conn=conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at,image_inputs
           ) VALUES('running-v','s2',1,'prompt','running-v','waiting_provider',0,?)""",
        (json.dumps({"shot_plan_id": "svp-2"}),),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               created_at,updated_at,provider_non_cancellable
           ) VALUES('running-job','video','s2','running-v','e','p',
                    'waiting_provider',0,0,1)"""
    )
    conn.commit()

    conn.execute("UPDATE shots SET adopted_version_id='v2' WHERE id='s1'")
    reconcile_adopted_revision("s1", "v2", conn=conn)

    job = conn.execute(
        "SELECT status,cancellation_requested,abandoned FROM jobs WHERE id='running-job'"
    ).fetchone()
    assert tuple(job) == ("waiting_provider", 0, 0)
    meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id='running-v'"
    ).fetchone()["image_inputs"])
    assert meta["stale"] is True
    assert meta["stale_reason"] == "upstream_adopted_revision_changed"


@pytest.mark.parametrize(
    ("case", "pointer", "supplied", "message"),
    [
        ("foreign", "foreign", "foreign", "不属于"),
        ("failed", "failed", "failed", "已成功"),
        ("pointer_mismatch", "v1", "v2", "指针不一致"),
    ],
)
def test_reconcile_rejects_forged_adoption_revision(
    case: str,
    pointer: str,
    supplied: str,
    message: str,
) -> None:
    conn = _conn()
    if case == "foreign":
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,created_at
               ) VALUES('foreign','s2',9,'prompt','foreign','succeeded',0)"""
        )
    elif case == "failed":
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,created_at
               ) VALUES('failed','s1',9,'prompt','failed','failed',0)"""
        )
    conn.execute(
        "UPDATE shots SET adopted_version_id=? WHERE id='s1'",
        (pointer,),
    )
    conn.commit()

    with pytest.raises(ValueError, match=message):
        reconcile_adopted_revision("s1", supplied, conn=conn)

    dependency = conn.execute(
        "SELECT upstream_adopted_version_id FROM video_plan_dependencies"
    ).fetchone()
    assert dependency["upstream_adopted_version_id"] is None


def test_reconcile_rejects_adoption_when_latest_plan_is_stale() -> None:
    conn = _conn()
    conn.execute("UPDATE shots SET adopted_version_id='v1' WHERE id='s1'")
    conn.execute("UPDATE shots SET action_desc='plan drift' WHERE id='s2'")
    conn.commit()

    with pytest.raises(ValueError, match="计划已过期"):
        reconcile_adopted_revision("s1", "v1", conn=conn)

    assert conn.execute(
        "SELECT status FROM episode_video_generation_plans WHERE id='evp-1'",
    ).fetchone()["status"] == "stale"
    assert conn.execute(
        "SELECT upstream_adopted_version_id FROM video_plan_dependencies"
    ).fetchone()["upstream_adopted_version_id"] is None


def test_missing_projection_with_durable_screenplay_pointer_cannot_use_legacy_manifest() -> None:
    conn = _conn()
    conn.execute(
        """UPDATE episodes
              SET screenplay_json=NULL,
                  published_screenplay_artifact_id='modern-screenplay'
            WHERE id='e'"""
    )
    conn.commit()

    with pytest.raises(ValueError, match="投影"):
        current_storyboard_release_manifest("e", conn=conn)


def test_local_replan_changes_only_target_contract_identity(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model",
    )

    replacement = create_local_replan_revision(
        "s2",
        reason="single_shot_reroll",
        conn=conn,
    )

    assert replacement.plan_revision == 2
    assert active_plan_is_current("svp-1", conn=conn) is True
    assert active_plan_is_current("svp-2", conn=conn) is False
    target = next(item for item in replacement.shots if item.shot_id == "s2")
    unchanged = next(item for item in replacement.shots if item.shot_id == "s1")
    assert "LOCAL_REPLAN_FOR_REDO" in target.reason_codes
    assert "local_replan_revision" in target.input_revision_fingerprints
    assert "local_replan_revision" not in unchanged.input_revision_fingerprints
    selected, _snapshot = assert_video_provider_submission_authority(
        shot_id="s1",
        shot_plan_id="svp-1",
        actual_mode=VideoGenerationMode.REFERENCE_IMAGE_MODE,
        expected_capability_snapshot_id="cap-1",
        conn=conn,
    )
    assert selected.shot_id == "s1"
    meta = {
        "shot_plan_id": "svp-1",
        "episode_video_plan_id": "evp-1",
    }
    rebound = worker._resolve_current_execution_plan(conn, "s1", meta)
    assert rebound is not None
    assert rebound.shot_plan_id == unchanged.shot_plan_id
    assert meta["shot_plan_id"] == unchanged.shot_plan_id
    assert meta["submitted_shot_plan_id"] == "svp-1"
    assert meta["equivalent_plan_rebound"] is True


def test_recover_equivalent_stale_provider_job_without_new_create(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model",
    )
    replacement = create_local_replan_revision(
        "s1",
        reason="target_only_replan",
        conn=conn,
    )
    current = next(item for item in replacement.shots if item.shot_id == "s2")
    assert active_plan_is_current("svp-2", conn=conn) is True

    meta = {
        "mode": "FIRST_LAST_FRAME_MODE",
        "planned_mode": "FIRST_LAST_FRAME_MODE",
        "actual_mode": "FIRST_LAST_FRAME_MODE",
        "episode_video_plan_id": "evp-1",
        "shot_plan_id": "svp-2",
        "plan_revision": 1,
        "source_storyboard_revision_id": "storyboard_rev_1",
        "capability_snapshot_id": "cap-1",
        "input_revision_fingerprints": dict(
            next(item for item in replacement.shots if item.shot_id == "s2").input_revision_fingerprints
        ),
    }
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,provider_task_id,
               status,error,image_inputs,created_at
           ) VALUES('stale-v','s2',1,'prompt','stale-key','provider-task',
                    'stale','plan stale',?,1)""",
        (json.dumps(meta),),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,error,
               created_at,updated_at,provider_operation_id,provider_create_state,
               provider_non_cancellable
           ) VALUES('stale-job','video','s2','stale-v','e','p','stale',
                    'plan stale',1,1,'video-create-stale-v','accepted',1)"""
    )
    conn.execute(
        """INSERT INTO budget_reservations(
               id,job_id,scope_type,scope_id,amount_cny,status,created_at,
               settled_at,actual_cost_cny
           ) VALUES('budget-stale','stale-job','episode','e',5,'released',1,2,0)"""
    )
    conn.execute(
        """INSERT INTO video_generation_attempts(
               id,shot_plan_id,version_id,attempt_no,planned_mode,actual_mode,
               status,provider_task_id,created_at,updated_at
           ) VALUES('attempt-stale','svp-2','stale-v',1,'FIRST_LAST_FRAME_MODE',
                    'FIRST_LAST_FRAME_MODE','provider_running','provider-task',1,1)"""
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    creates_before = conn.execute(
        "SELECT COUNT(*) FROM provider_calls WHERE kind='video_create'"
    ).fetchone()[0]

    result = worker.recover_equivalent_stale_provider_jobs("e")

    assert result["recovered_jobs"] == 1
    assert result["provider_task_ids"] == ["provider-task"]
    assert result["provider_create_calls"] == 0
    assert conn.execute(
        "SELECT status FROM jobs WHERE id='stale-job'"
    ).fetchone()[0] == "waiting_provider"
    version = conn.execute(
        "SELECT status,image_inputs FROM shot_versions WHERE id='stale-v'"
    ).fetchone()
    recovered_meta = json.loads(version["image_inputs"])
    assert version["status"] == "running"
    assert recovered_meta["submitted_shot_plan_id"] == "svp-2"
    assert recovered_meta["shot_plan_id"] == current.shot_plan_id
    assert conn.execute(
        "SELECT shot_plan_id FROM video_generation_attempts WHERE id='attempt-stale'"
    ).fetchone()[0] == current.shot_plan_id
    creates_after = conn.execute(
        "SELECT COUNT(*) FROM provider_calls WHERE kind='video_create'"
    ).fetchone()[0]
    assert creates_after == creates_before


def test_recovery_keeps_older_task_stale_when_shot_has_usable_candidate(
    monkeypatch,
) -> None:
    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model",
    )
    create_local_replan_revision("s2", reason="target_only_replan", conn=conn)
    assert active_plan_is_current("svp-1", conn=conn) is True
    meta = json.dumps({
        "mode": "REFERENCE_IMAGE_MODE",
        "planned_mode": "REFERENCE_IMAGE_MODE",
        "actual_mode": "REFERENCE_IMAGE_MODE",
        "episode_video_plan_id": "evp-1",
        "shot_plan_id": "svp-1",
    })
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,provider_task_id,
               status,error,image_inputs,created_at
           ) VALUES('older-stale','s1',3,'prompt','older-key','older-task',
                    'stale','plan stale',?,1)""",
        (meta,),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,error,
               created_at,updated_at,provider_operation_id,provider_create_state,
               provider_non_cancellable
           ) VALUES('older-job','video','s1','older-stale','e','p','stale',
                    'plan stale',1,1,'video-create-older-stale','accepted',1)"""
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    result = worker.recover_equivalent_stale_provider_jobs("e")

    assert result["recovered_jobs"] == 0
    assert conn.execute(
        "SELECT status FROM jobs WHERE id='older-job'"
    ).fetchone()[0] == "stale"
    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='older-stale'"
    ).fetchone()[0] == "stale"


def test_reference_mode_paid_submission_rejects_current_shot_contract_drift(
    monkeypatch,
) -> None:
    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model",
    )
    conn.execute("UPDATE shots SET action_desc='changed after plan' WHERE id='s1'")
    conn.commit()

    with pytest.raises(VideoPlanValidationError) as rejected:
        assert_video_provider_submission_authority(
            shot_id="s1",
            shot_plan_id="svp-1",
            actual_mode=VideoGenerationMode.REFERENCE_IMAGE_MODE,
            expected_capability_snapshot_id="cap-1",
            conn=conn,
        )

    assert {
        issue["code"] for issue in rejected.value.issues
    } >= {"VIDEO_SUBMISSION_PLAN_STALE"}
    assert conn.execute(
        "SELECT status FROM episode_video_generation_plans WHERE id='evp-1'",
    ).fetchone()["status"] == "stale"
    assert conn.execute(
        "SELECT status FROM shot_video_generation_plans WHERE id='svp-1'",
    ).fetchone()["status"] == "stale"


@pytest.mark.asyncio
async def test_stale_reference_plan_is_fenced_before_reference_asset_builder(
    monkeypatch,
) -> None:
    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model",
    )
    conn.execute("UPDATE shots SET action_desc='drift before asset build' WHERE id='s1'")
    conn.commit()
    builder_calls: list[dict] = []

    async def forbidden_builder(**kwargs):
        builder_calls.append(kwargs)
        raise AssertionError("stale plan must not purchase reference assets")

    monkeypatch.setattr(
        worker.video_modes,
        "build_reference_assets",
        forbidden_builder,
    )

    with pytest.raises(worker.VideoPlanStaleFence):
        await worker._prepare_planned_mode_inputs(
            conn,
            {"shot_id": "s1"},
            {},
            None,
            None,
            {
                "mode": "REFERENCE_IMAGE_MODE",
                "shot_plan_id": "svp-1",
                "capability_snapshot_id": "cap-1",
            },
            "prompt",
            lease_owner="worker-test",
        )

    assert builder_calls == []
    assert conn.execute(
        "SELECT status FROM episode_video_generation_plans WHERE id='evp-1'",
    ).fetchone()["status"] == "stale"


def test_latest_active_capability_withdrawal_stales_reference_plan(
    monkeypatch,
) -> None:
    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model",
    )
    save_capability_snapshot(
        ProviderVideoCapabilitySnapshot(
            id="cap-withdrawn",
            provider="provider",
            model="model",
            supports_reference_image=False,
            probe_time=2,
            probe_result="reference_image_capability_withdrawn",
            technical_success=True,
        ),
        conn=conn,
    )
    conn.commit()

    with pytest.raises(VideoPlanValidationError) as rejected:
        assert_video_provider_submission_authority(
            shot_id="s1",
            shot_plan_id="svp-1",
            actual_mode=VideoGenerationMode.REFERENCE_IMAGE_MODE,
            expected_capability_snapshot_id="cap-1",
            conn=conn,
        )

    assert "VIDEO_SUBMISSION_CAPABILITY_WITHDRAWN" in {
        issue["code"] for issue in rejected.value.issues
    }
    assert conn.execute(
        "SELECT status FROM episode_video_generation_plans WHERE id='evp-1'",
    ).fetchone()["status"] == "stale"
