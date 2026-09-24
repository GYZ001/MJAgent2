"""``app.domain.bible_ops.voice_routes`` 的 REST 契约：字段形状与状态码。

直接调用路由函数（同 ``tests/test_bible_style_endpoint.py`` 对
``bible_ops.set_style`` 的写法）——这些函数内部先过 ``ui_route``，在普通测试
里不在「命令总线 handler 内部」（``in_handler()`` 为假），所以会真的经过一次
完整的 Command Bus 派发：成功响应因此是 ``{**领域数据, "ok", "status",
"summary", "command_id", ...}`` 的超集形状（``app.capabilities.dispatch.
result_http_payload`` 用 ``setdefault`` 叠加总线元信息，不覆盖领域字段），
不是精确相等——断言按「包含约定字段且取值正确」，不是整个字典相等
（CLAUDE.md「比较集合用包含，不用相等」同一原则）。异常路径（404/409/422/502）
经总线一次往返后状态码与 message 原样还原，已用直接调用实测确认。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app import db
from app.domain.bible_ops import voice_routes
from app.schemas import Bible, Character, World
from app.voice import service as voice_service
from app.voice.asr_check import VoiceCheckResult
from app.voice.providers import dispatch
from app.voice.providers.base import VoiceDesignResult, VoiceProviderError
from tests.test_voice_service import _resolved_model, _wav_bytes


def _seed_project(characters: list[Character], *, project_id: str) -> None:
    bible = Bible(world=World(visual_style_canonical="国风"), characters=characters)
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json, bible_version) VALUES(?,?,?,?,?,?)",
        (project_id, "测试项目", "created", db.now(),
         json.dumps(bible.model_dump(mode="json"), ensure_ascii=False), 1),
    )
    conn.commit()


def _character(name: str) -> Character:
    return Character(name=name, role="主角", appearance_canonical="少年，黑发")


def _patch_generation_ok(monkeypatch) -> None:
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    async def fake_design_voice(req, *, purpose, call_meta=None):
        return VoiceDesignResult(
            provider_voice_id="prov_1", audio=_wav_bytes(), audio_format="wav",
            sample_rate=24000, request_id="req_1", latency_ms=100,
        )

    monkeypatch.setattr(dispatch, "design_voice", fake_design_voice)
    monkeypatch.setattr(
        voice_service, "check_preview_match",
        lambda audio_path, preview_text: VoiceCheckResult("passed", "", preview_text, 1.0),
    )


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/voices
# ---------------------------------------------------------------------------


def test_list_character_voices_lists_every_named_character_with_empty_slot(monkeypatch) -> None:
    _seed_project([_character("甲"), _character("乙")], project_id="p_list")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: None)

    result = asyncio.run(voice_routes.list_character_voices("p_list"))

    assert result["project_id"] == "p_list"
    assert result["voice_model_configured"] is False
    assert isinstance(result["auto_generate"], bool)
    names = {item["character_name"] for item in result["items"]}
    assert names == {"甲", "乙"}
    for item in result["items"]:
        assert item["anchor_key"] == ""
        assert item["current"] is None
        assert item["candidates"] == []
        assert item["generating"] is False


def test_list_character_voices_reflects_current_and_candidates(monkeypatch) -> None:
    _seed_project([_character("丙")], project_id="p_list2")
    _patch_generation_ok(monkeypatch)
    first = asyncio.run(voice_service.generate_voice_for_character(
        "p_list2", "丙", voice_prompt="a", preview_text="第一句试听台词", created_by="tester",
    ))
    second = asyncio.run(voice_service.generate_voice_for_character(
        "p_list2", "丙", voice_prompt="b", preview_text="第二句试听台词", created_by="tester",
    ))

    result = asyncio.run(voice_routes.list_character_voices("p_list2"))

    item = next(i for i in result["items"] if i["character_name"] == "丙")
    assert item["current"]["id"] == first["id"]
    assert [c["id"] for c in item["candidates"]] == [second["id"]]


def test_list_character_voices_project_not_found_404() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.list_character_voices("no-such-project"))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# POST .../voice-description
# ---------------------------------------------------------------------------


def test_voice_description_suggest_returns_suggestion_without_persisting(monkeypatch) -> None:
    _seed_project([_character("丁")], project_id="p_desc")

    async def fake_description(character, *, era, existing_prompts):
        return "音色描述内容", "试听台词内容"

    monkeypatch.setattr(voice_service, "generate_voice_description", fake_description)

    result = asyncio.run(voice_routes.voice_description_suggest("p_desc", "丁"))

    assert result["voice_prompt"] == "音色描述内容"
    assert result["preview_text"] == "试听台词内容"
    listed = asyncio.run(voice_routes.list_character_voices("p_desc"))
    assert listed["items"][0]["current"] is None
    assert listed["items"][0]["candidates"] == []


def test_voice_description_suggest_character_not_found_404() -> None:
    _seed_project([_character("戊")], project_id="p_desc2")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.voice_description_suggest("p_desc2", "不存在的角色"))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# POST .../voices
# ---------------------------------------------------------------------------


def test_generate_character_voice_happy_path_returns_voice_envelope(monkeypatch) -> None:
    _seed_project([_character("己")], project_id="p_gen")
    _patch_generation_ok(monkeypatch)

    result = asyncio.run(voice_routes.generate_character_voice(
        "p_gen", "己", {"voice_prompt": "青年男声", "preview_text": "今天天气不错", "idempotency_key": "k1"},
    ))

    voice = result["voice"]
    assert voice["status"] == "current"
    assert voice["voice_prompt"] == "青年男声"
    assert voice["preview_text"] == "今天天气不错"
    assert voice["audio_url"]
    assert voice["clip_url"]


def test_generate_character_voice_missing_idempotency_key_422() -> None:
    _seed_project([_character("庚")], project_id="p_gen2")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.generate_character_voice(
            "p_gen2", "庚", {"voice_prompt": "a", "preview_text": "b"},
        ))
    assert exc.value.status_code == 422


def test_generate_character_voice_character_not_found_404(monkeypatch) -> None:
    _seed_project([_character("辛")], project_id="p_gen3")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.generate_character_voice(
            "p_gen3", "不存在", {"voice_prompt": "a", "preview_text": "b", "idempotency_key": "k1"},
        ))
    assert exc.value.status_code == 404


def test_generate_character_voice_not_configured_409(monkeypatch) -> None:
    _seed_project([_character("壬")], project_id="p_gen4")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.generate_character_voice(
            "p_gen4", "壬", {"voice_prompt": "a", "preview_text": "b", "idempotency_key": "k1"},
        ))
    assert exc.value.status_code == 409
    assert "未配置声音生成模型" in str(exc.value.detail)


def test_generate_character_voice_provider_failure_502(monkeypatch) -> None:
    _seed_project([_character("癸")], project_id="p_gen5")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    async def fake_fail(req, *, purpose, call_meta=None):
        raise VoiceProviderError("供应商拒绝了本次请求", failure_kind="content_rejected")

    monkeypatch.setattr(dispatch, "design_voice", fake_fail)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.generate_character_voice(
            "p_gen5", "癸", {"voice_prompt": "a", "preview_text": "b", "idempotency_key": "k1"},
        ))
    assert exc.value.status_code == 502
    assert "供应商拒绝了本次请求" in str(exc.value.detail)


# ---------------------------------------------------------------------------
# POST .../voices/{voice_id}/adopt
# ---------------------------------------------------------------------------


def test_adopt_character_voice_happy_path(monkeypatch) -> None:
    _seed_project([_character("阿凯")], project_id="p_adopt")
    _patch_generation_ok(monkeypatch)
    first = asyncio.run(voice_service.generate_voice_for_character(
        "p_adopt", "阿凯", voice_prompt="a", preview_text="第一句试听台词", created_by="tester",
    ))
    second = asyncio.run(voice_service.generate_voice_for_character(
        "p_adopt", "阿凯", voice_prompt="b", preview_text="第二句试听台词", created_by="tester",
    ))
    assert first["status"] == "current" and second["status"] == "candidate"

    result = asyncio.run(voice_routes.adopt_character_voice("p_adopt", "阿凯", second["id"]))

    assert result["voice"]["status"] == "current"
    assert result["voice"]["id"] == second["id"]


def test_adopt_character_voice_wrong_character_404(monkeypatch) -> None:
    _seed_project([_character("周晚"), _character("小李")], project_id="p_adopt2")
    _patch_generation_ok(monkeypatch)
    row = asyncio.run(voice_service.generate_voice_for_character(
        "p_adopt2", "周晚", voice_prompt="a", preview_text="试听台词", created_by="tester",
    ))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.adopt_character_voice("p_adopt2", "小李", row["id"]))
    assert exc.value.status_code == 404


def test_adopt_character_voice_failed_row_409(monkeypatch) -> None:
    _seed_project([_character("小虎")], project_id="p_adopt3")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    async def fake_short(req, *, purpose, call_meta=None):
        return VoiceDesignResult(
            provider_voice_id="prov_short", audio=_wav_bytes(duration_s=1.0, pause_at=0.5),
            audio_format="wav", sample_rate=24000, request_id="req_short", latency_ms=50,
        )

    monkeypatch.setattr(dispatch, "design_voice", fake_short)
    failed_row = asyncio.run(voice_service.generate_voice_for_character(
        "p_adopt3", "小虎", voice_prompt="a", preview_text="b", created_by="tester",
    ))
    assert failed_row["status"] == "failed"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.adopt_character_voice("p_adopt3", "小虎", failed_row["id"]))
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# POST .../voices/generate-missing
# ---------------------------------------------------------------------------


def test_generate_missing_voices_returns_accepted_and_characters(monkeypatch) -> None:
    _seed_project([_character("小美"), _character("小强")], project_id="p_missing_route")
    _patch_generation_ok(monkeypatch)

    async def fake_description(character, *, era, existing_prompts):
        return f"{character.name}的描述", f"{character.name}的试听台词内容"

    monkeypatch.setattr(voice_service, "generate_voice_description", fake_description)

    result = asyncio.run(voice_routes.generate_missing_voices("p_missing_route"))

    assert result["accepted"] == 2
    assert set(result["characters"]) == {"小美", "小强"}


def test_generate_missing_voices_project_not_found_404() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_routes.generate_missing_voices("no-such-project-2"))
    assert exc.value.status_code == 404
