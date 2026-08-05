import json
import sqlite3

import pytest

from app import db, hiagent, worker
from app.video_plan import (
    AssetSource,
    EpisodeVideoGenerationPlan,
    PlanAssetRequirement,
    ProviderVideoCapabilitySnapshot,
    ShotVideoGenerationPlan,
    VideoGenerationMode,
    VideoInputIntent,
    VideoPlanValidationError,
    active_plan_is_current,
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
                mode=VideoGenerationMode.VIDEO_INPUT_MODE,
                video_input_intent=VideoInputIntent.MOTION_REFERENCE,
                depends_on_shot_id="SH-1",
                required_assets=[
                    PlanAssetRequirement(
                        role="previous_adopted_video",
                        source=AssetSource.PREVIOUS_ADOPTED_VIDEO,
                    )
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
            "source": "STATIC_BOUNDARY_ASSET",
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
