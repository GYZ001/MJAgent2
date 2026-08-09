from __future__ import annotations

import asyncio
import base64
import sqlite3

import pytest
from fastapi import HTTPException

from app import config, db, hiagent, minimax_h3, monitoring, system_api, video_plan, worker


class _Response:
    def __init__(self, status_code: int, payload: dict, *, content: bytes = b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = str(payload)

    def json(self):
        return self._payload


def _image_data_url() -> str:
    return "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode("ascii")


def _probe_root_payload() -> dict:
    return {
        "name": "MiniMax H3 ComfyUI API",
        "version": "1.3.0",
        "modes": ["keyframes", "reference_images", "reference_video"],
        "accelerations": ["standard", "turbo"],
        "turbo_profiles": {"preview": 4, "balanced": 6, "quality": 8},
        "default_turbo_profile": "quality",
        "video_vae_profiles": ["fp16", "int8_convrot"],
    }


def _probe_health_payload() -> dict:
    return {
        "status": "ok",
        "modes": {
            name: {"ready": True, "missing": []}
            for name in ("keyframes", "reference_images", "reference_video")
        },
        "accelerations": {
            "standard": {"ready": True, "missing": []},
            "turbo": {"ready": True, "missing": []},
        },
        "video_vae_profiles": {
            "fp16": {"ready": True, "missing": []},
            "int8_convrot": {"ready": True, "missing": []},
        },
        "te_speed_available": True,
    }


def test_minimax_h3_maps_all_three_generation_modes(monkeypatch) -> None:
    requests: list[dict] = []
    request_headers: list[dict] = []
    upload_index = 0
    monkeypatch.setattr(config, "MINIMAX_H3_ACCELERATION", "turbo")
    monkeypatch.setattr(config, "MINIMAX_H3_STEPS", 8)
    monkeypatch.setattr(config, "MINIMAX_H3_TURBO_PROFILE", "quality")
    monkeypatch.setattr(config, "MINIMAX_H3_VIDEO_VAE", "fp16")
    monkeypatch.setattr(config, "MINIMAX_H3_USE_TE_SPEED", False)
    monkeypatch.setattr(config, "MINIMAX_H3_TURBO_STRENGTH", 1.0)
    monkeypatch.setattr(config, "MINIMAX_H3_TURBO_LOW_VRAM", False)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            nonlocal upload_index
            if url.endswith("/v1/files"):
                upload_index += 1
                return _Response(201, {"filename": f"uploaded_{upload_index}"})
            requests.append(kwargs["json"])
            request_headers.append(kwargs["headers"])
            return _Response(202, {"id": f"task-{len(requests)}", "status": "queued"})

    async def materialize(value, *, media_kind, index):
        suffix = ".jpg" if media_kind == "image" else ".mp4"
        mime = "image/jpeg" if media_kind == "image" else "video/mp4"
        return f"{media_kind}_{index}{suffix}", value.encode(), mime

    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(minimax_h3, "_materialize_input", materialize)
    monkeypatch.setattr(minimax_h3, "latest_provider_request_json", lambda *_args: None)
    monkeypatch.setattr(minimax_h3, "start_provider_call", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        minimax_h3,
        "update_provider_call_request",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(minimax_h3, "finish_provider_call", lambda *_args, **_kwargs: None)

    keyframes = asyncio.run(minimax_h3.create_video_task(
        "镜头推进 --ratio 9:16 --dur 5",
        image_urls=[
            (_image_data_url(), "first_frame"),
            (_image_data_url(), "last_frame"),
        ],
        call_meta={"operation_id": "keyframes", "duration_s": 5},
    ))
    references = asyncio.run(minimax_h3.create_video_task(
        "Reference image 1 is the character. Reference image 2 is the scene.",
        image_urls=[
            (_image_data_url(), "reference_image"),
            (_image_data_url(), "reference_image"),
        ],
        call_meta={"operation_id": "references", "duration_s": 6},
    ))
    video = asyncio.run(minimax_h3.create_video_task(
        "沿用参考视频的声音节奏",
        video_urls=[("https://media.example.test/source.mp4", "reference_video")],
        call_meta={
            "operation_id": "video",
            "duration_s": 7,
            "video_input_intent": "AUDIO_REFERENCE",
        },
    ))

    assert keyframes == "minimax_h3:task-1"
    assert references == "minimax_h3:task-2"
    assert video == "minimax_h3:task-3"
    assert requests[0]["mode"] == "keyframes"
    assert requests[0]["first_frame"] and requests[0]["last_frame"]
    assert requests[0]["duration"] == 5
    assert requests[0]["width"] == 576
    assert requests[0]["height"] == 1024
    assert requests[0]["acceleration"] == "turbo"
    assert "steps" not in requests[0]
    assert requests[0]["turbo_profile"] == "quality"
    assert requests[0]["video_vae"] == "fp16"
    assert requests[0]["scheduler"] == "simple"
    assert requests[0]["use_te_speed"] is False
    assert requests[0]["turbo_strength"] == 1.0
    assert requests[0]["turbo_low_vram"] is False
    assert "--ratio" not in requests[0]["prompt"]
    assert "--dur" not in requests[0]["prompt"]
    assert requests[1]["mode"] == "reference_images"
    assert requests[1]["reference_images"] == ["uploaded_3", "uploaded_4"]
    assert "<Picture 1>" in requests[1]["prompt"]
    assert "<Picture 2>" in requests[1]["prompt"]
    assert requests[2]["mode"] == "reference_video"
    assert requests[2]["reference_videos"] == ["uploaded_5"]
    assert requests[2]["use_source_audio"] is True
    assert "<Video 1>" in requests[2]["prompt"]
    assert "<Audio 1>" in requests[2]["prompt"]
    assert [headers["Idempotency-Key"] for headers in request_headers] == [
        "keyframes", "references", "video",
    ]


def test_minimax_h3_retry_reuses_exact_checkpoint_without_reupload(monkeypatch) -> None:
    checkpoint: dict = {}
    uploads = 0
    generation_requests: list[dict] = []
    generation_headers: list[dict] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            nonlocal uploads
            if url.endswith("/v1/files"):
                uploads += 1
                return _Response(201, {"filename": "durable_input.jpg"})
            generation_requests.append(kwargs["json"])
            generation_headers.append(kwargs["headers"])
            return _Response(202, {
                "id": "same-task",
                "status": "queued",
                "replayed": len(generation_requests) > 1,
            })

    async def materialize(_value, *, media_kind, index):
        return f"{media_kind}_{index}.jpg", b"jpeg", "image/jpeg"

    def latest(*_args):
        return checkpoint.get("value")

    def persist(_call_id, value, **_kwargs):
        checkpoint["value"] = value

    monkeypatch.setattr(config, "MINIMAX_H3_ACCELERATION", "turbo")
    monkeypatch.setattr(config, "MINIMAX_H3_TURBO_PROFILE", "quality")
    monkeypatch.setattr(config, "MINIMAX_H3_VIDEO_VAE", "fp16")
    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(minimax_h3, "_materialize_input", materialize)
    monkeypatch.setattr(minimax_h3, "latest_provider_request_json", latest)
    monkeypatch.setattr(minimax_h3, "start_provider_call", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(minimax_h3, "update_provider_call_request", persist)
    monkeypatch.setattr(minimax_h3, "finish_provider_call", lambda *_args, **_kwargs: None)

    kwargs = {
        "image_urls": [(_image_data_url(), "first_frame")],
        "call_meta": {"operation_id": "video-create-ver_1", "duration_s": 5},
    }
    first = asyncio.run(minimax_h3.create_video_task("人物转身", **kwargs))
    second = asyncio.run(minimax_h3.create_video_task("人物转身", **kwargs))

    assert first == second == "minimax_h3:same-task"
    assert uploads == 1
    assert generation_requests[0] == generation_requests[1]
    assert {
        headers["Idempotency-Key"] for headers in generation_headers
    } == {"video-create-ver_1"}


def test_minimax_h3_poll_maps_result_and_provider_prefix(monkeypatch) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            assert url.endswith("/v1/videos/generations/provider-task")
            return _Response(200, {
                "status": "succeeded",
                "files": [{
                    "filename": "result.mp4",
                    "url": "http://192.168.31.232:8181/v1/outputs?filename=result.mp4",
                }],
                "errors": [],
            })

    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = asyncio.run(minimax_h3.poll_video_task("minimax_h3:provider-task"))

    assert result == {
        "status": "succeeded",
        "stage": "",
        "stage_label": "H3 处理中",
        "provider": "minimax_h3",
        "provider_label": "MiniMax H3",
        "sequence": None,
        "queue_position": None,
        "estimated_seconds": None,
        "timings": {},
        "metadata": {},
        "video_url": "http://192.168.31.232:8181/v1/outputs?filename=result.mp4",
        "last_frame_url": "",
        "error": "",
    }


def test_minimax_h3_v12_latency_profiles_match_readme_benchmarks(monkeypatch) -> None:
    monkeypatch.setattr(config, "MINIMAX_H3_ACCELERATION", "turbo")
    monkeypatch.setattr(config, "MINIMAX_H3_POLL_INTERVAL", 5.0)

    assert minimax_h3.estimated_generation_seconds(
        "FIRST_LAST_FRAME_MODE", 5,
    ) == 207
    assert minimax_h3.estimated_generation_seconds(
        "REFERENCE_IMAGE_MODE", 10,
    ) == 379
    assert minimax_h3.estimated_generation_seconds(
        "VIDEO_INPUT_MODE", 15,
    ) == 3918
    assert minimax_h3.generation_timeout_seconds(
        "VIDEO_INPUT_MODE", 15,
    ) > 2 * 60 * 60
    assert minimax_h3.poll_interval_seconds() == 5.0


def test_minimax_h3_wait_policy_starts_mode_timeout_only_when_generating(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "MINIMAX_H3_ACCELERATION", "turbo")
    monkeypatch.setattr(config, "MINIMAX_H3_POLL_INTERVAL", 5.0)
    monkeypatch.setattr(config, "VIDEO_PROVIDER_MAX_WAIT", 6 * 60 * 60)
    meta = {"actual_mode": "VIDEO_INPUT_MODE"}

    queued = worker._provider_wait_policy(
        "minimax_h3:task-1",
        {"status": "queued", "stage": "queued", "queue_position": 3},
        meta,
        duration_s=15,
        provider_submitted_at=100,
        stamp=1000,
    )
    assert queued["elapsed_s"] == 900
    assert queued["timeout_s"] == 6 * 60 * 60
    assert queued["meta_changed"] is True
    assert queued["stage_progress"]["provider_phase"] == "queued"
    assert queued["stage_progress"]["provider_queue_position"] == 3

    generating = worker._provider_wait_policy(
        "minimax_h3:task-1",
        {
            "status": "running",
            "stage": "sampling",
            "queue_position": 0,
            "estimated_seconds": 616,
            "timings": {"text_encode_ms": 2500, "sampling_ms": None},
        },
        meta,
        duration_s=15,
        provider_submitted_at=100,
        stamp=1000,
    )
    assert generating["elapsed_s"] == 0
    assert generating["timeout_s"] == 1678
    assert generating["poll_delay_s"] == 5.0
    assert generating["meta_changed"] is True
    assert generating["stage_progress"]["provider_stage"] == "sampling"
    assert meta["minimax_h3_estimated_generation_s"] == 616

    resumed = worker._provider_wait_policy(
        "minimax_h3:task-1",
        {
            "status": "running",
            "stage": "video_vae",
            "queue_position": 0,
            "estimated_seconds": 616,
            "timings": {"sampling_ms": 540000, "video_vae_ms": None},
        },
        meta,
        duration_s=15,
        provider_submitted_at=100,
        stamp=1060,
    )
    assert resumed["elapsed_s"] == 60
    assert resumed["meta_changed"] is True
    assert resumed["stage_progress"]["provider_stage"] == "video_vae"


@pytest.mark.parametrize(
    ("stage", "phase"),
    [
        ("queued", "queued"),
        ("checking_comfyui", "waiting"),
        ("waiting_for_comfyui", "waiting"),
        ("submitting", "waiting"),
        ("queued_in_comfyui", "waiting"),
        ("conditioning", "generating"),
        ("sampling", "generating"),
        ("video_vae", "generating"),
        ("audio_vae", "generating"),
        ("video_encoding", "generating"),
        ("locating_comfyui_job", "generating"),
    ],
)
def test_minimax_h3_classifies_v13_provider_stages(stage, phase) -> None:
    assert minimax_h3.provider_task_phase({
        "status": "queued" if stage == "queued" else "running",
        "stage": stage,
        "queue_position": 1 if stage == "queued" else 0,
    }) == phase


def test_minimax_h3_probe_requires_selected_acceleration(monkeypatch) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            return _Response(
                200,
                _probe_health_payload()
                if url.endswith("/health")
                else _probe_root_payload(),
            )

    monkeypatch.setattr(config, "MINIMAX_H3_ACCELERATION", "turbo")
    monkeypatch.setattr(config, "MINIMAX_H3_STEPS", 8)
    monkeypatch.setattr(config, "MINIMAX_H3_TURBO_PROFILE", "quality")
    monkeypatch.setattr(config, "MINIMAX_H3_VIDEO_VAE", "fp16")
    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = asyncio.run(minimax_h3.probe_connection())

    assert result["api_version"] == "1.3.0"
    assert result["acceleration"] == "turbo"
    assert result["turbo_profile"] == "quality"
    assert result["video_vae"] == "fp16"
    assert result["steps"] == 8
    assert "turbo" in result["preview"]


def test_hiagent_dispatches_minimax_tasks_without_api_key(monkeypatch) -> None:
    seen: dict = {}

    async def create(prompt_text, **kwargs):
        seen["prompt"] = prompt_text
        seen.update(kwargs)
        return "minimax_h3:task"

    async def poll(task_id, **kwargs):
        seen["task_id"] = task_id
        return {"status": "running", "video_url": "", "last_frame_url": "", "error": ""}

    monkeypatch.setattr(hiagent, "active_provider", lambda kind: "minimax_h3")
    monkeypatch.setattr(minimax_h3, "create_video_task", create)
    monkeypatch.setattr(minimax_h3, "poll_video_task", poll)

    task_id = asyncio.run(hiagent.create_video_task(
        "prompt",
        image_urls=[(_image_data_url(), "reference_image")],
    ))
    result = asyncio.run(hiagent.poll_video_task(task_id))

    assert task_id == "minimax_h3:task"
    assert seen["image_urls"][0][1] == "reference_image"
    assert seen["task_id"] == task_id
    assert result["status"] == "running"


def test_minimax_h3_is_ready_in_model_catalog_without_key(monkeypatch) -> None:
    monkeypatch.setattr(system_api, "get_setting", lambda _key: "")

    item = next(
        model for model in system_api.get_models()["items"]
        if model["provider"] == "minimax_h3"
    )

    assert item["model"] == "minimax-h3"
    assert item["label"] == "MiniMaxH3"
    assert item["kinds"] == ["video"]
    assert item["requires_api_key"] is False
    assert item["key_configured"] is True
    assert monitoring.normalize_setting("model_video_provider", "minimax_h3") == "minimax_h3"
    assert monitoring.normalize_setting(
        "minimax_h3_base_url",
        "http://192.168.31.232:8181",
    ) == "http://192.168.31.232:8181"
    with pytest.raises(HTTPException):
        monitoring.normalize_setting(
            "minimax_h3_base_url",
            "http://192.168.31.232:8181/unexpected-path",
        )


def test_minimax_h3_capabilities_are_verified_before_model_selection(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "hiagent")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, provider=None: (
            "minimax-h3" if provider == "minimax_h3" else "seedance"
        ),
    )
    monkeypatch.setattr(
        minimax_h3,
        "probe_connection_sync",
        lambda: {
            "ok": True,
            "base_url": "http://192.168.31.232:8181",
            "api_version": "1.3.0",
            "modes": {
                "keyframes": True,
                "reference_images": True,
                "reference_video": True,
            },
            "accelerations": {"standard": True, "turbo": True},
            "acceleration": "turbo",
            "turbo_profiles": {"preview": 4, "balanced": 6, "quality": 8},
            "turbo_profile": "quality",
            "steps": 8,
            "video_vae_profiles": {"fp16": True, "int8_convrot": True},
            "video_vae": "fp16",
            "te_speed_available": True,
        },
    )

    snapshot = video_plan.current_capability_snapshot(
        provider="minimax_h3",
        model="minimax-h3",
        conn=conn,
    )

    assert snapshot.technical_success is True
    assert snapshot.supports_reference_image is True
    assert snapshot.supports_first_last_pair is True
    assert snapshot.supports_reference_video is True
    assert snapshot.api_version == "1.3.0"
    assert snapshot.format_limits["capability_source"] == "live_health"
    assert snapshot.format_limits["default_acceleration"] == "turbo"
    assert snapshot.format_limits["default_turbo_profile"] == "quality"
    assert snapshot.format_limits["default_video_vae"] == "fp16"
    assert snapshot.probe_result == "live_health:1.3.0:turbo:quality:fp16"


def test_minimax_h3_failed_discovery_never_registers_static_success(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    monkeypatch.setattr(
        minimax_h3,
        "probe_connection_sync",
        lambda: (_ for _ in ()).throw(hiagent.ProviderError("offline")),
    )

    snapshot = video_plan.current_capability_snapshot(
        provider="minimax_h3",
        model="minimax-h3",
        conn=conn,
    )

    assert snapshot.technical_success is False
    assert snapshot.supports_reference_image is False
    assert snapshot.supports_first_last_pair is False
    assert snapshot.supports_reference_video is False
    assert snapshot.format_limits["capability_source"] == "live_health_error"
    assert snapshot.probe_result.startswith("live_health_failed:ProviderError:")


def test_minimax_h3_unchanged_live_contract_reuses_snapshot_id() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    probe = {
        "ok": True,
        "base_url": "http://192.168.31.232:8181",
        "api_version": "1.3.0",
        "modes": {
            "keyframes": True,
            "reference_images": True,
            "reference_video": True,
        },
        "accelerations": {"standard": True, "turbo": True},
        "acceleration": "turbo",
        "turbo_profiles": {"preview": 4, "balanced": 6, "quality": 8},
        "turbo_profile": "quality",
        "steps": 8,
        "video_vae_profiles": {"fp16": True, "int8_convrot": True},
        "video_vae": "fp16",
        "te_speed_available": True,
    }

    first = video_plan.record_minimax_h3_probe_snapshot(
        probe,
        provider="minimax_h3",
        model="minimax-h3",
        conn=conn,
    )
    second = video_plan.record_minimax_h3_probe_snapshot(
        probe,
        provider="minimax_h3",
        model="minimax-h3",
        conn=conn,
    )

    assert second.id == first.id
    assert conn.execute(
        "SELECT COUNT(*) FROM provider_video_capability_snapshots"
    ).fetchone()[0] == 1
