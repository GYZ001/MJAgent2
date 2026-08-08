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


def test_minimax_h3_maps_all_three_generation_modes(monkeypatch) -> None:
    requests: list[dict] = []
    upload_index = 0
    monkeypatch.setattr(config, "MINIMAX_H3_ACCELERATION", "turbo")
    monkeypatch.setattr(config, "MINIMAX_H3_STEPS", 8)
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
            return _Response(202, {"id": f"task-{len(requests)}", "status": "queued"})

    async def materialize(value, *, media_kind, index):
        suffix = ".jpg" if media_kind == "image" else ".mp4"
        mime = "image/jpeg" if media_kind == "image" else "video/mp4"
        return f"{media_kind}_{index}{suffix}", value.encode(), mime

    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(minimax_h3, "_materialize_input", materialize)
    monkeypatch.setattr(minimax_h3, "start_provider_call", lambda *_args, **_kwargs: 1)
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
    assert requests[0]["steps"] == 8
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
        "queue_position": None,
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
    assert queued["meta_changed"] is False

    generating = worker._provider_wait_policy(
        "minimax_h3:task-1",
        {"status": "running", "stage": "generating", "queue_position": 0},
        meta,
        duration_s=15,
        provider_submitted_at=100,
        stamp=1000,
    )
    assert generating["elapsed_s"] == 0
    assert generating["timeout_s"] > 2 * 60 * 60
    assert generating["poll_delay_s"] == 5.0
    assert generating["meta_changed"] is True

    resumed = worker._provider_wait_policy(
        "minimax_h3:task-1",
        {"status": "running", "stage": "generating", "queue_position": 0},
        meta,
        duration_s=15,
        provider_submitted_at=100,
        stamp=1060,
    )
    assert resumed["elapsed_s"] == 60
    assert resumed["meta_changed"] is False


def test_minimax_h3_probe_requires_selected_acceleration(monkeypatch) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, **_kwargs):
            return _Response(200, {
                "status": "ok",
                "modes": {
                    name: {"ready": True, "missing": []}
                    for name in ("keyframes", "reference_images", "reference_video")
                },
                "accelerations": {
                    "standard": {"ready": True, "missing": []},
                    "turbo": {"ready": True, "missing": []},
                },
                "te_speed_available": True,
            })

    monkeypatch.setattr(config, "MINIMAX_H3_ACCELERATION", "turbo")
    monkeypatch.setattr(config, "MINIMAX_H3_STEPS", 8)
    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = asyncio.run(minimax_h3.probe_connection())

    assert result["acceleration"] == "turbo"
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

    snapshot = video_plan.current_capability_snapshot(
        provider="minimax_h3",
        model="minimax-h3",
        conn=conn,
    )

    assert snapshot.technical_success is True
    assert snapshot.supports_reference_image is True
    assert snapshot.supports_first_last_pair is True
    assert snapshot.supports_reference_video is True
    assert snapshot.api_version == "1.2.0"
    assert snapshot.format_limits["default_acceleration"] == "turbo"
    assert snapshot.probe_result == "verified_v1.2.0_three_modes_turbo_2026_08_08"
