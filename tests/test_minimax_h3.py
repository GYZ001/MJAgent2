from __future__ import annotations

import asyncio
import base64
import json
import sqlite3

import pytest
from fastapi import HTTPException

from app import (
    config,
    db,
    hiagent,
    minimax_h3,
    monitoring,
    system_api,
    video_modes,
    video_plan,
    worker,
)


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
    assert "Reference image 1 is the character" not in requests[1]["prompt"]
    assert requests[2]["mode"] == "reference_video"
    assert requests[2]["reference_videos"] == ["uploaded_5"]
    assert requests[2]["use_source_audio"] is True
    assert "<Video 1>" in requests[2]["prompt"]
    assert "<Audio 1>" in requests[2]["prompt"]
    assert "only the carrier of <Audio 1>" in requests[2]["prompt"]
    assert "supplies body motion" not in requests[2]["prompt"]
    assert "supplies camera movement" not in requests[2]["prompt"]
    assert [headers["Idempotency-Key"] for headers in request_headers] == [
        "keyframes", "references", "video",
    ]


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("MOTION_REFERENCE", "supplies body motion and physical trajectory only"),
        ("CAMERA_REFERENCE", "supplies camera movement, framing change, and lens rhythm only"),
        ("RHYTHM_REFERENCE", "supplies temporal pacing and beat timing only"),
        ("AUDIO_REFERENCE", "is only the carrier of <Audio 1>"),
        ("CONTINUE_PREVIOUS_TAKE", "preceding take for visual and audio continuity"),
    ],
)
def test_minimax_h3_video_reference_mapping_respects_declared_intent(
    intent,
    expected,
) -> None:
    prompt = minimax_h3._tagged_prompt(
        "当前镜正文 --ratio 9:16 --dur 5",
        video_count=1,
        use_source_audio=intent in {"AUDIO_REFERENCE", "CONTINUE_PREVIOUS_TAKE"},
        video_input_intent=intent,
    )

    assert expected in prompt
    assert "当前镜正文" in prompt


def test_minimax_h3_only_consumes_trailing_technical_args() -> None:
    prompt = minimax_h3._tagged_prompt(
        "角色对白中逐字说出“--dur 5”作为口令。\n"
        "本镜继续动作 --ratio 9:16 --dur 5",
    )

    assert "逐字说出“--dur 5”作为口令" in prompt
    assert prompt.endswith("本镜继续动作")
    assert "--ratio 9:16 --dur 5" not in prompt


def test_seedance_binding_contract_round_trips_into_h3_picture_tags() -> None:
    common_prompt = "[FORMAT]\n电影化单镜头。 --ratio 9:16 --dur 5"
    refs = [{
        "id": "character",
        "url": "data:image/jpeg;base64,YQ==",
        "type": "character",
        "source": "asset_library",
        "selectedForSeedance": True,
        "entity_name": "A",
        "relatedCharacterIds": ["A"],
    }]
    seedance_prompt = video_modes.append_reference_prompt_notes_from_dicts(
        common_prompt,
        refs,
    )

    h3_prompt = minimax_h3._tagged_prompt(
        seedance_prompt,
        image_count=1,
    )

    assert seedance_prompt.endswith("--ratio 9:16 --dur 5")
    assert "<Picture 1>: use as character" in h3_prompt
    assert "<Picture 1>: use as character「A」" in h3_prompt
    assert "identity/appearance only" in h3_prompt
    assert "--ratio 9:16" not in h3_prompt
    assert "--dur 5" not in h3_prompt


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


def test_minimax_h3_checkpoint_rejects_changed_request_for_same_operation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        minimax_h3,
        "latest_provider_request_json",
        lambda *_args: {
            "checkpoint_version": 1,
            "logical_fingerprint": "different",
            "provider_request": {"mode": "keyframes"},
        },
    )

    with pytest.raises(hiagent.ProviderError) as exc:
        minimax_h3._load_request_checkpoint("video-create-ver_1", "expected")

    assert exc.value.retryable is False
    assert "请求内容发生变化" in str(exc.value)


def test_provider_request_checkpoint_preserves_exact_long_payload(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    cursor = conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,model,status,latency_ms,attempt_no,received_chars
           ) VALUES(?,?,?,?,?,?,?)""",
        (1, "video_create", "minimax-h3", "RUNNING", 0, 1, 0),
    )
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    prompt = "x" * 130_000

    db.update_provider_call_request(
        int(cursor.lastrowid),
        {"provider_request": {"prompt": prompt}},
        preserve_exact=True,
    )

    saved = json.loads(conn.execute(
        "SELECT request_json FROM provider_calls WHERE id=?",
        (int(cursor.lastrowid),),
    ).fetchone()["request_json"])
    assert saved["provider_request"]["prompt"] == prompt


def test_minimax_h3_poll_maps_result_and_provider_prefix(monkeypatch) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            assert minimax_h3.base_url()
            assert url == f"{minimax_h3.base_url()}/v1/videos/generations/provider-task"
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
        "failure": None,
    }


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_failure"),
    [
        pytest.param(
            422,
            {
                "error": {
                    "message": "request rejected",
                    "failure": {
                        "category": "model_rejection",
                        "kind": "provider_rejected",
                        "retryable": True,
                    },
                },
            },
            {
                "category": "model_rejection",
                "kind": "provider_rejected",
                "disposition": "external_terminal",
                "retryable": False,
            },
            id="explicit-model-rejection",
        ),
        pytest.param(
            409,
            {
                "error": {
                    "message": "provider execution unavailable",
                    "failure": {
                        "category": "technical",
                        "kind": "provider_execution_failed",
                        "retryable": True,
                    },
                },
            },
            {
                "category": "technical",
                "kind": "provider_execution_failed",
                "disposition": "automatic_retry",
                "retryable": True,
            },
            id="technical-failure",
        ),
    ],
)
def test_minimax_h3_poll_non_200_preserves_typed_failure(
    monkeypatch,
    status_code: int,
    payload: dict,
    expected_failure: dict,
) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, **_kwargs):
            return _Response(status_code, payload)

    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(minimax_h3.poll_video_task("minimax_h3:provider-task"))

    assert caught.value.failure.to_payload() == expected_failure


@pytest.mark.parametrize(
    "json_result",
    [
        pytest.param(ValueError("invalid JSON"), id="invalid-json"),
        pytest.param([], id="non-object-json"),
    ],
)
def test_minimax_h3_poll_malformed_response_is_typed_technical_failure(
    monkeypatch,
    json_result,
) -> None:
    class Response:
        status_code = 200
        text = "malformed response"

        def json(self):
            if isinstance(json_result, Exception):
                raise json_result
            return json_result

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, **_kwargs):
            return Response()

    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(minimax_h3.poll_video_task("minimax_h3:provider-task"))

    assert caught.value.failure.to_payload() == {
        "category": "technical",
        "kind": "malformed_response",
        "disposition": "automatic_retry",
        "retryable": True,
    }

@pytest.mark.parametrize(
    ("payload", "expected_kind", "expected_error"),
    [
        pytest.param(
            {"status": "not_found", "files": [], "errors": []},
            "provider_task_not_found",
            "MiniMaxH3 队列和历史中均找不到该任务",
            id="provider-task-not-found",
        ),
        pytest.param(
            {"status": "succeeded", "files": [], "errors": []},
            "provider_output_missing",
            "MiniMaxH3 任务成功但未返回 MP4 文件",
            id="succeeded-without-mp4",
        ),
    ],
)
def test_minimax_h3_poll_preserves_technical_failure_contract(
    monkeypatch,
    payload: dict,
    expected_kind: str,
    expected_error: str,
) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, **_kwargs):
            return _Response(200, payload)

    monkeypatch.setattr(minimax_h3.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = asyncio.run(minimax_h3.poll_video_task("minimax_h3:provider-task"))

    assert result["status"] == "failed"
    assert result["error"] == expected_error
    assert result["failure"] == {
        "category": "technical",
        "kind": expected_kind,
        "disposition": "manual_review",
        "retryable": False,
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
