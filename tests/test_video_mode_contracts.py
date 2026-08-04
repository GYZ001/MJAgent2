import pytest
import sqlite3

from app import db
from app.hiagent import ProviderError
from app.stages import evaluate_video_mode_qa
from app.video_plan import ProviderMediaPublicationService
from app.video_modes import (
    FIRST_LAST_FRAME_MODE,
    REFERENCE_IMAGE_MODE,
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
