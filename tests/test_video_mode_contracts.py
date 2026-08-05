import asyncio
import json
import pytest
import sqlite3
from types import SimpleNamespace
from PIL import Image

from app import db, worker
from app.hiagent import ProviderError
from app.schemas import Bible, Shot, World
from app.stages import evaluate_video_mode_qa
from app.video_plan import (
    AssetSource,
    PlanAssetRequirement,
    ProviderMediaPublicationService,
)
from app.video_modes import (
    FIRST_LAST_FRAME_MODE,
    REFERENCE_IMAGE_MODE,
    ReferenceImageAsset,
    VIDEO_INPUT_MODE,
    build_seedance_image_inputs,
    build_seedance_video_inputs,
)


def test_three_provider_payload_contracts_are_mutually_exclusive(tmp_path) -> None:
    first = tmp_path / "first.jpg"
    last = tmp_path / "last.jpg"
    first.write_bytes(b"first")
    last.write_bytes(b"last")

    reference = build_seedance_image_inputs({
        "mode": REFERENCE_IMAGE_MODE,
        "reference_images": [{
            "url": "data:image/jpeg;base64,YQ==",
            "selectedForSeedance": True,
            "type": "character",
        }],
    })
    boundary = build_seedance_image_inputs({
        "mode": FIRST_LAST_FRAME_MODE,
        "first_frame_path": str(first),
        "last_frame_path": str(last),
    })
    video = build_seedance_video_inputs({
        "mode": VIDEO_INPUT_MODE,
        "video_input_url": "https://media.example.test/source.mp4",
        "video_input_intent": "MOTION_REFERENCE",
    })

    assert [role for _, role in reference] == ["reference_image"]
    assert [role for _, role in boundary] == ["first_frame", "last_frame"]
    assert video == [("https://media.example.test/source.mp4", "reference_video")]

    with pytest.raises(ProviderError):
        build_seedance_image_inputs({
            "mode": FIRST_LAST_FRAME_MODE,
            "first_frame_path": str(first),
            "last_frame_path": str(last),
            "reference_images": [{"url": "data:image/jpeg;base64,YQ=="}],
        })
    with pytest.raises(ProviderError):
        build_seedance_video_inputs({
            "mode": VIDEO_INPUT_MODE,
            "video_input_url": "data:video/mp4;base64,YQ==",
            "video_input_intent": "MOTION_REFERENCE",
        })
    with pytest.raises(ProviderError, match="禁止纯文本提交"):
        build_seedance_image_inputs({
            "mode": REFERENCE_IMAGE_MODE,
            "reference_images": [],
        })


def test_reference_video_technical_success_is_not_true_continuation_success() -> None:
    result = evaluate_video_mode_qa(
        meta={
            "mode": VIDEO_INPUT_MODE,
            "actual_mode": VIDEO_INPUT_MODE,
            "planned_mode": VIDEO_INPUT_MODE,
            "video_input_intent": "CONTINUE_PREVIOUS_TAKE",
            "reference_video_used": True,
            "reference_image_used": False,
            "first_frame_used": False,
            "last_frame_used": False,
        },
        qa={"status": "scored", "overall": 0.95},
        technical={"passed": True},
    )

    assert result["provider_read_video"] is True
    assert result["semantic_success"] is None
    assert "独立多样本" in result["issues"][0]


def test_first_last_mode_qa_requires_both_boundary_matches() -> None:
    result = evaluate_video_mode_qa(
        meta={
            "mode": FIRST_LAST_FRAME_MODE,
            "first_frame_used": True,
            "last_frame_used": True,
            "reference_image_used": False,
            "reference_video_used": False,
        },
        qa={
            "status": "scored",
            "start_state_match": 0.9,
            "end_state_match": 0.4,
        },
        technical={"passed": True},
    )

    assert result["input_roles_valid"] is True
    assert result["semantic_success"] is False


def test_boundary_pair_normalizes_aspect_compatible_resolutions(tmp_path) -> None:
    first = tmp_path / "first.jpg"
    last = tmp_path / "last.jpg"
    Image.new("RGB", (720, 1280), "black").save(first)
    Image.new("RGB", (1440, 2560), "white").save(last)

    _, _, size = worker._normalize_boundary_pair(str(first), str(last))

    assert size == (720, 1280)
    with Image.open(first) as image:
        assert image.size == size
    with Image.open(last) as image:
        assert image.size == size


def _first_last_runtime_fixture(
    tmp_path,
    monkeypatch,
    *,
    first_source: AssetSource,
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    bible = Bible(characters=[], world=World(visual_style_canonical="film"))
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','P',?,0)",
        (bible.model_dump_json(),),
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e','p',1,'confirmed',0)"""
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s,characters,dialogues)
           VALUES('s1','e',1,5,'[]','[]')"""
    )
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s,characters,dialogues)
           VALUES('s2','e',2,5,'[]','[]')"""
    )
    conn.commit()
    shot_model = Shot(
        shot_no=2,
        duration_s=5,
        shot_size="中景",
        camera_move="固定",
        scene_setting="室内",
        characters=[],
        action_desc="人物完成动作。",
        first_frame_desc="动作开始。",
        last_frame_desc="动作结束。",
        source_excerpt="人物完成动作。",
        dialogues=[],
        transition="硬切",
    )
    plan = SimpleNamespace(
        episode_video_plan_id="evp",
        shot_plan_id="svp-current",
        shot_id="s2",
        source_storyboard_revision_id="storyboard",
        depends_on_shot_id=(
            "s1" if first_source == AssetSource.PREVIOUS_ADOPTED_TAIL else None
        ),
        max_attempts=2,
        required_assets=[
            PlanAssetRequirement(
                role="first_frame",
                source=first_source,
                source_shot_id="s1",
            ),
            PlanAssetRequirement(
                role="last_frame",
                source=AssetSource.STATIC_BOUNDARY_ASSET,
            ),
        ],
    )
    persisted_roles: list[str] = []
    generated_seed_inputs: list[list[str]] = []
    tail = tmp_path / "tail.jpg"

    async def generate_tail(**kwargs):
        generated_seed_inputs.append(list(kwargs.get("seed_inputs") or []))
        Image.new("RGB", (720, 1280), "white").save(tail)
        return ReferenceImageAsset(
            id="tail",
            url="",
            type="plot_key_frame",
            source="generated",
            path=str(tail),
            selectedForSeedance=True,
            qa={"overall": 1.0},
        )

    monkeypatch.setattr(
        worker, "_resolve_current_execution_plan",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(worker, "_load_shot_model", lambda _row: shot_model)
    monkeypatch.setattr(worker, "_assert_job_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_load_boundary_asset", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "_persist_boundary_asset",
        lambda _conn, **kwargs: persisted_roles.append(kwargs["role"]),
    )
    monkeypatch.setattr(worker, "_set_version", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.continuity.apply_shot_contract",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.multiview.resolve_shot_asset_dependencies",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr("app.multiview.keyframe_seed_paths", lambda _manifest: [])
    monkeypatch.setattr(
        "app.media_pipeline.stage_state.set_pipeline_stage",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        worker.video_modes, "_portrait_seed_inputs", lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        worker.video_modes, "_generate_one_reference", generate_tail,
    )
    monkeypatch.setattr(
        worker.video_modes,
        "review_reference_consistency",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("shared boundary contract must not run pair semantic QA")
        ),
    )
    return conn, plan, persisted_roles, generated_seed_inputs


def test_scene_second_shot_waits_before_generating_unconditioned_tail(
    tmp_path,
    monkeypatch,
) -> None:
    conn, _plan, persisted_roles, generated_seed_inputs = _first_last_runtime_fixture(
        tmp_path,
        monkeypatch,
        first_source=AssetSource.PREVIOUS_ADOPTED_TAIL,
    )

    with pytest.raises(worker._ContinuityWait):
        asyncio.run(worker._prepare_first_last_mode_inputs(
            conn,
            {"id": "j", "project_id": "p", "episode_id": "e", "shot_id": "s2"},
            {"id": "v"},
            {"shot_no": 2},
            {"episode_no": 1},
            {},
            "prompt",
            lease_owner="lease",
        ))

    assert persisted_roles == []
    assert generated_seed_inputs == []


def test_previous_static_tail_is_reused_without_pair_semantic_qa(
    tmp_path,
    monkeypatch,
) -> None:
    conn, plan, persisted_roles, generated_seed_inputs = _first_last_runtime_fixture(
        tmp_path,
        monkeypatch,
        first_source=AssetSource.PREVIOUS_STATIC_TAIL,
    )
    upstream = tmp_path / "upstream-tail.jpg"
    Image.new("RGB", (720, 1280), "black").save(upstream)
    conn.execute(
        """INSERT INTO shot_video_generation_plans(
               id,episode_video_plan_id,shot_id,shot_no,planned_mode,
               capability_snapshot_id,created_at,updated_at
           ) VALUES('svp-upstream','evp','s1',1,'FIRST_LAST_FRAME_MODE','cap',0,0)"""
    )
    conn.execute(
        """INSERT INTO video_boundary_assets(
               id,episode_video_plan_id,shot_plan_id,shot_id,role,source,path,
               sha256,mime,width,height,qa_status,fingerprint,created_at
           ) VALUES('boundary','evp','svp-upstream','s1','last_frame',
                    'STATIC_BOUNDARY_ASSET',?,'abc','image/jpeg',720,1280,
                    'passed','tail-fingerprint',0)""",
        (str(upstream),),
    )
    conn.commit()

    meta, _ = asyncio.run(worker._prepare_first_last_mode_inputs(
        conn,
        {"id": "j", "project_id": "p", "episode_id": "e", "shot_id": "s2"},
        {"id": "v"},
        {"shot_no": 2},
        {"episode_no": 1},
        {},
        "prompt",
        lease_owner="lease",
    ))

    assert persisted_roles == ["first_frame", "last_frame"]
    assert generated_seed_inputs
    assert generated_seed_inputs[0][0].startswith("data:image/")
    assert meta["boundary_pair_qa"]["semantic_pair_review_performed"] is False
    assert meta["boundary_pair_qa"]["tail_conditioned_on_first_frame"] is True
    assert meta["boundary_pair_qa"]["shared_boundary_contract"] == (
        "shared_static_tail_v3"
    )
    assert meta["boundary_pair_qa"]["first_frame_source"] == (
        AssetSource.PREVIOUS_STATIC_TAIL.value
    )


@pytest.mark.parametrize(
    "url",
    [
        "data:video/mp4;base64,YQ==",
        "http://127.0.0.1/video.mp4",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_provider_media_publication_rejects_non_public_urls(url: str) -> None:
    with pytest.raises(ValueError):
        ProviderMediaPublicationService._assert_web_url(url)


@pytest.mark.asyncio
async def test_provider_media_publication_persists_content_hash(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)

    async def accessible(_url: str) -> None:
        return None

    async def metadata(_url: str) -> dict:
        return {
            "sha256": "a" * 64,
            "size_bytes": 123,
            "mime": "video/mp4",
        }

    monkeypatch.setattr(
        ProviderMediaPublicationService,
        "_check_accessible",
        staticmethod(accessible),
    )
    monkeypatch.setattr(
        ProviderMediaPublicationService,
        "_remote_metadata",
        staticmethod(metadata),
    )
    result = await ProviderMediaPublicationService().publish(
        source_revision_id="video-rev-1",
        source_url="https://media.example.test/source.mp4",
        expires_at=10**12,
        conn=conn,
    )

    assert result["sha256"] == "a" * 64
    row = conn.execute(
        "SELECT sha256,mime,status FROM provider_media_publications"
    ).fetchone()
    assert tuple(row) == ("a" * 64, "video/mp4", "ready")
