import pytest
from fastapi import HTTPException

from app import api
from app.main import app
from app.video_plan import ProviderVideoCapabilitySnapshot
from tests.conftest import patch_api_everywhere, patch_video_plan_everywhere


def test_video_mode_api_surface_is_registered() -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    }
    expected = {
        ("POST", "/api/episodes/{episode_id}/video-generation-plan"),
        ("GET", "/api/episodes/{episode_id}/video-generation-plan"),
        ("POST", "/api/episodes/{episode_id}/video-generation-plan/validate"),
        ("POST", "/api/episodes/{episode_id}/video-generation-plan/reconcile"),
        ("POST", "/api/episodes/{episode_id}/video-generation-plan/{plan_id}/execute"),
        ("GET", "/api/video-capabilities/{provider}/{model}"),
        ("POST", "/api/video-capabilities/{provider}/{model}/probe"),
        ("POST", "/api/provider-media-publications"),
        ("GET", "/api/jobs/{job_id}/video-mode-audit"),
    }
    assert expected <= routes


def test_missing_video_plan_is_a_normal_empty_state(monkeypatch) -> None:
    patch_api_everywhere(monkeypatch, "_episode_or_404", lambda _episode_id: {"id": "e"})
    patch_video_plan_everywhere(monkeypatch, "load_latest_plan", lambda _episode_id: None)

    assert api.get_episode_video_generation_plan("e") is None


@pytest.mark.asyncio
async def test_paid_capability_probe_requires_explicit_confirmation() -> None:
    with pytest.raises(HTTPException) as exc:
        await api.probe_video_capability(
            "provider",
            "model",
            {"capability": "reference_video"},
        )
    assert exc.value.status_code == 409
    assert "confirm=true" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_async_probe_failure_does_not_register_capability(
    monkeypatch,
) -> None:
    base = ProviderVideoCapabilitySnapshot(
        id="cap-old",
        provider="provider",
        model="model",
        supports_reference_image=True,
        supports_reference_video=True,
        probe_time=1,
        technical_success=True,
    )
    saved = []

    async def create_task(*_args, **_kwargs):
        return "provider-task-1"

    async def poll_task(*_args, **_kwargs):
        return {
            "status": "failed",
            "video_url": "",
            "last_frame_url": "",
            "error": "InvalidParameter",
        }

    monkeypatch.setattr("app.hiagent.create_video_task", create_task)
    monkeypatch.setattr("app.hiagent.poll_video_task", poll_task)
    patch_video_plan_everywhere(monkeypatch, "current_capability_snapshot", lambda **_kwargs: base)
    patch_video_plan_everywhere(
        monkeypatch, "save_capability_snapshot", lambda snapshot: saved.append(snapshot)
    )

    result = await api.probe_video_capability(
        "provider",
        "model",
        {
            "confirm": True,
            "capability": "reference_video",
            "reference_video_url": "https://media.example.test/source.mp4",
        },
    )

    assert result["technical_success"] is False
    assert result["supports_reference_video"] is False
    assert saved and saved[0].probe_task_id == "provider-task-1"
