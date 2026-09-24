"""``app.voice.service`` 的生成编排：描述自动补全、供应商调用、真实 ffmpeg
裁片、（打桩）语音识别核验、自动采用规则、采用/批量补齐/定妆钩子。

打桩纪律（派单明确要求）：``dispatch.design_voice`` 用
``monkeypatch.setattr(dispatch, "design_voice", fake)``——service.py 里是
``from app.voice.providers import dispatch`` 再 ``dispatch.design_voice(...)``
的模块限定调用，打在模块属性上对所有调用方生效，不是 ``from ... import
design_voice`` 的名字拷贝陷阱。裁片用真实 ffmpeg 处理这里合成的正弦波 wav；
语音识别（``check_preview_match``）打桩——service.py 是它唯一消费方，属于
``from x import y`` 安全的单一绑定点，打在 ``voice_service`` 命名空间上。
"""
from __future__ import annotations

import asyncio
import json
import math
import struct
import wave
from io import BytesIO

import pytest

from app import db, task_registry
from app.models_registry.routing import ResolvedModel
from app.schemas import Bible, Character, World
from app.voice import service as voice_service
from app.voice import store as voice_store
from app.voice.asr_check import VoiceCheckResult
from app.voice.providers import dispatch
from app.voice.providers.base import VoiceDesignResult, VoiceProviderError

PROJECT_ID = "p1"


def _wav_bytes(duration_s: float = 6.0, *, freq: float = 220.0, pause_at: float = 4.6,
               sample_rate: int = 24000) -> bytes:
    """合成一段带停顿的正弦波 wav——真实 ffmpeg 裁片测试用，不是打桩。"""
    n = int(duration_s * sample_rate)
    frames = bytearray()
    for i in range(n):
        t = i / sample_rate
        in_pause = pause_at <= t < pause_at + 0.3
        val = 0.0 if in_pause else 0.3 * math.sin(2 * math.pi * freq * t)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, val)) * 32767))
    buf = BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return buf.getvalue()


def _resolved_model() -> ResolvedModel:
    return ResolvedModel(
        model_id="model_test", purpose="voice:default", priority=0, provider="custom",
        protocol="qwen_voice_design", model_ref="qwen3-tts-vd-test",
        base_url="https://example.invalid", api_key="sk-test", params={},
    )


def _seed_project(characters: list[Character], *, project_id: str = PROJECT_ID, era: str = "") -> None:
    bible = Bible(world=World(visual_style_canonical="国风", era=era), characters=characters)
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json, bible_version) "
        "VALUES(?,?,?,?,?,?)",
        (project_id, "测试项目", "created", db.now(),
         json.dumps(bible.model_dump(mode="json"), ensure_ascii=False), 1),
    )
    conn.commit()


def _character(name: str) -> Character:
    return Character(
        name=name, role="主角", appearance_canonical="少年，黑发",
        personality="沉稳", speech_style="简洁",
    )


def _design_voice_ok(monkeypatch, *, wav: bytes | None = None) -> None:
    async def fake_design_voice(req, *, purpose, call_meta=None):
        return VoiceDesignResult(
            provider_voice_id="prov_1", audio=wav or _wav_bytes(),
            audio_format="wav", sample_rate=24000, request_id="req_1", latency_ms=120,
        )

    monkeypatch.setattr(dispatch, "design_voice", fake_design_voice)


def _check_passed(monkeypatch) -> None:
    monkeypatch.setattr(
        voice_service, "check_preview_match",
        lambda audio_path, preview_text: VoiceCheckResult("passed", "", preview_text, 1.0),
    )


def _check_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        voice_service, "check_preview_match",
        lambda audio_path, preview_text: VoiceCheckResult("failed", "匹配率过低", "别的话", 0.1),
    )


def _fake_description(monkeypatch) -> None:
    """批量补齐/定妆钩子路径总是传空 voice_prompt/preview_text，会触发自动
    写描述；不打桩会打到真实 model_gateway，在测试环境里失败并被
    ``_generate_missing_task`` 的按角色容错悄悄吞掉（现象：sweep 声称
    accepted，但该角色永远没有 current，且没有任何测试失败信号）。"""
    async def fake(character, *, era, existing_prompts):
        return f"{character.name}的音色描述", f"{character.name}的试听台词内容"

    monkeypatch.setattr(voice_service, "generate_voice_description", fake)


# ---------------------------------------------------------------------------
# generate_voice_for_character
# ---------------------------------------------------------------------------


def test_generate_voice_for_character_happy_path_auto_adopts(monkeypatch) -> None:
    _seed_project([_character("张三")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)

    result = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "张三", voice_prompt="青年男声，清亮", preview_text="今天天气不错",
        created_by="tester",
    ))

    assert result["status"] == "current"  # 该角色此前没有 current，首个通过校验的候选自动采用
    assert result["check_status"] == "passed"
    assert result["audio_url"]
    assert result["clip_url"]
    assert 2.0 <= result["clip_duration_s"] <= 5.0
    current = voice_store.current_for(db.get_conn(), PROJECT_ID, "张三")
    assert current is not None and current["id"] == result["id"]


def test_generate_voice_for_character_second_candidate_not_auto_adopted(monkeypatch) -> None:
    _seed_project([_character("李四")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    first = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "李四", voice_prompt="a", preview_text="第一句试听台词",
        created_by="tester",
    ))
    assert first["status"] == "current"

    second = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "李四", voice_prompt="b", preview_text="第二句试听台词",
        created_by="tester",
    ))

    assert second["status"] == "candidate"  # 已有 current，新候选只进候选列表
    current = voice_store.current_for(db.get_conn(), PROJECT_ID, "李四")
    assert current["id"] == first["id"]  # 原 current 不变


def test_generate_voice_for_character_check_failed_still_candidate_not_auto_adopted(monkeypatch) -> None:
    """校验未通过（check_status=failed）不算「校验通过」，不自动采用，但生成
    本身没有失败——status 仍是 candidate，等人工判断是否采用。"""
    _seed_project([_character("周五")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_failed(monkeypatch)

    result = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "周五", voice_prompt="a", preview_text="试听台词",
        created_by="tester",
    ))

    assert result["status"] == "candidate"
    assert result["check_status"] == "failed"
    assert voice_store.current_for(db.get_conn(), PROJECT_ID, "周五") is None


def test_generate_voice_for_character_raises_not_configured(monkeypatch) -> None:
    _seed_project([_character("赵六")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: None)

    with pytest.raises(VoiceProviderError) as exc:
        asyncio.run(voice_service.generate_voice_for_character(
            PROJECT_ID, "赵六", voice_prompt="a", preview_text="b", created_by="tester",
        ))
    assert exc.value.failure_kind == "not_configured"


def test_generate_voice_for_character_marks_row_failed_on_provider_error(monkeypatch) -> None:
    _seed_project([_character("孙七")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    async def fake_fail(req, *, purpose, call_meta=None):
        raise VoiceProviderError("供应商额度不足", failure_kind="insufficient_balance")

    monkeypatch.setattr(dispatch, "design_voice", fake_fail)

    with pytest.raises(VoiceProviderError):
        asyncio.run(voice_service.generate_voice_for_character(
            PROJECT_ID, "孙七", voice_prompt="a", preview_text="b", created_by="tester",
        ))

    rows = voice_store.list_for_character(db.get_conn(), PROJECT_ID, "孙七")
    assert len(rows) == 1
    assert rows[0]["status"] == voice_store.STATUS_FAILED
    assert "额度不足" in rows[0]["error"]


def test_generate_voice_for_character_raises_lookup_error_for_missing_character(monkeypatch) -> None:
    _seed_project([_character("已存在")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    with pytest.raises(voice_service.VoiceLookupError):
        asyncio.run(voice_service.generate_voice_for_character(
            PROJECT_ID, "不存在的角色", voice_prompt="a", preview_text="b", created_by="tester",
        ))


def test_generate_voice_for_character_auto_writes_description_when_blank(monkeypatch) -> None:
    _seed_project([_character("自动描述")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)

    async def fake_description(character, *, era, existing_prompts):
        assert character.name == "自动描述"
        return "自动生成的音色描述", "这是自动生成的试听台词内容"

    monkeypatch.setattr(voice_service, "generate_voice_description", fake_description)

    result = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "自动描述", voice_prompt="", preview_text="", created_by="tester",
    ))
    assert result["voice_prompt"] == "自动生成的音色描述"
    assert result["preview_text"] == "这是自动生成的试听台词内容"


def test_generate_voice_for_character_clip_failure_marks_failed_not_candidate(monkeypatch) -> None:
    """全量音频太短（<2 秒），裁片必然失败——整行应标记 failed，不产出半成品候选。"""
    _seed_project([_character("太短")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch, wav=_wav_bytes(duration_s=1.0, pause_at=0.5))
    _check_passed(monkeypatch)

    result = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "太短", voice_prompt="a", preview_text="b", created_by="tester",
    ))
    assert result["status"] == "failed"
    assert "裁片失败" in result["error"]


# ---------------------------------------------------------------------------
# adopt_voice
# ---------------------------------------------------------------------------


def test_adopt_voice_switches_current_and_demotes_previous(monkeypatch) -> None:
    _seed_project([_character("阿凯")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    first = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "阿凯", voice_prompt="a", preview_text="第一句试听台词", created_by="tester",
    ))
    second = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "阿凯", voice_prompt="b", preview_text="第二句试听台词", created_by="tester",
    ))
    assert first["status"] == "current" and second["status"] == "candidate"

    adopted = asyncio.run(voice_service.adopt_voice(PROJECT_ID, "阿凯", second["id"], adopted_by="user1"))

    assert adopted["status"] == "current"
    current = voice_store.current_for(db.get_conn(), PROJECT_ID, "阿凯")
    assert current["id"] == second["id"]


def test_adopt_voice_rejects_wrong_character(monkeypatch) -> None:
    _seed_project([_character("小明"), _character("小红")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    row = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "小明", voice_prompt="a", preview_text="试听台词", created_by="tester",
    ))

    with pytest.raises(voice_service.VoiceLookupError):
        asyncio.run(voice_service.adopt_voice(PROJECT_ID, "小红", row["id"], adopted_by="u"))


def test_adopt_voice_rejects_failed_row(monkeypatch) -> None:
    _seed_project([_character("小刚")])
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch, wav=_wav_bytes(duration_s=1.0, pause_at=0.5))
    _check_passed(monkeypatch)
    failed_row = asyncio.run(voice_service.generate_voice_for_character(
        PROJECT_ID, "小刚", voice_prompt="a", preview_text="b", created_by="tester",
    ))
    assert failed_row["status"] == "failed"

    with pytest.raises(ValueError):
        asyncio.run(voice_service.adopt_voice(PROJECT_ID, "小刚", failed_row["id"], adopted_by="u"))


# ---------------------------------------------------------------------------
# suggest_description
# ---------------------------------------------------------------------------


def test_suggest_description_does_not_persist_anything(monkeypatch) -> None:
    _seed_project([_character("建议角色")])

    async def fake_description(character, *, era, existing_prompts):
        return "建议的音色描述", "建议的试听台词"

    monkeypatch.setattr(voice_service, "generate_voice_description", fake_description)

    result = asyncio.run(voice_service.suggest_description(PROJECT_ID, "建议角色"))

    assert result == {"voice_prompt": "建议的音色描述", "preview_text": "建议的试听台词"}
    assert voice_store.list_for_character(db.get_conn(), PROJECT_ID, "建议角色") == []


def test_suggest_description_raises_lookup_error_for_missing_character() -> None:
    _seed_project([_character("存在的")])
    with pytest.raises(voice_service.VoiceLookupError):
        asyncio.run(voice_service.suggest_description(PROJECT_ID, "不存在"))


# ---------------------------------------------------------------------------
# generate_missing_for_project / trigger_auto_generate_after_portrait
# ---------------------------------------------------------------------------


async def _run_and_drain(coro, project_id: str):
    result = await coro
    task = task_registry.get(voice_service._VOICE_BULK_TASK_KIND, project_id)
    if task is not None:
        await task
    return result


def test_generate_missing_for_project_generates_all_eligible_characters(monkeypatch) -> None:
    _seed_project([_character("甲"), _character("乙")], project_id="p_missing")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    _fake_description(monkeypatch)

    accepted, names = asyncio.run(_run_and_drain(
        voice_service.generate_missing_for_project("p_missing", triggered_by="tester"), "p_missing",
    ))

    assert accepted == 2
    assert set(names) == {"甲", "乙"}
    for name in ("甲", "乙"):
        assert voice_store.current_for(db.get_conn(), "p_missing", name) is not None


def test_generate_missing_for_project_raises_not_configured(monkeypatch) -> None:
    """未配置模型时明确报错（REST 层转 409 并提示去模型中心），不静默返回 0。"""
    _seed_project([_character("丙")], project_id="p_missing_unconf")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: None)

    with pytest.raises(VoiceProviderError) as excinfo:
        asyncio.run(voice_service.generate_missing_for_project("p_missing_unconf", triggered_by="tester"))
    assert excinfo.value.failure_kind == "not_configured"


def test_generate_missing_for_project_reports_running_batch(monkeypatch) -> None:
    """同项目已有批量任务在跑：明确报「进行中」，不静默返回受理 0 个。"""
    _seed_project([_character("己")], project_id="p_missing_busy")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())

    def busy_spawn(kind, key, coro, **kwargs):
        coro.close()
        raise RuntimeError("already running")

    monkeypatch.setattr(voice_service.task_registry, "spawn", busy_spawn)
    with pytest.raises(ValueError, match="进行中"):
        asyncio.run(voice_service.generate_missing_for_project("p_missing_busy", triggered_by="tester"))


def test_generate_voice_for_character_unexpected_finalize_error_marks_failed(monkeypatch) -> None:
    """落盘/裁片阶段的意外异常要把该行标成 failed，不能停在 generating 等超时。"""
    _seed_project([_character("庚")], project_id="p_finalize_err")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)

    def broken_finalize(*args, **kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr(voice_service, "_finalize_voice_files", broken_finalize)
    with pytest.raises(OSError):
        asyncio.run(voice_service.generate_voice_for_character(
            "p_finalize_err", "庚", voice_prompt="a", preview_text="b", created_by="tester",
        ))
    rows = voice_store.list_for_character(db.get_conn(), "p_finalize_err", "庚")
    assert [r["status"] for r in rows] == ["failed"]
    assert "磁盘已满" in rows[0]["error"]
    assert rows[0]["model_id"] == "model_test"  # 记的是模型库条目 id，不是模型标识


def test_generate_missing_for_project_skips_characters_that_already_have_current(monkeypatch) -> None:
    _seed_project([_character("丁"), _character("戊")], project_id="p_missing_partial")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    _fake_description(monkeypatch)
    asyncio.run(voice_service.generate_voice_for_character(
        "p_missing_partial", "丁", voice_prompt="a", preview_text="试听台词", created_by="tester",
    ))
    ding_before = voice_store.current_for(db.get_conn(), "p_missing_partial", "丁")

    accepted, names = asyncio.run(_run_and_drain(
        voice_service.generate_missing_for_project("p_missing_partial", triggered_by="tester"),
        "p_missing_partial",
    ))
    assert accepted == 1
    assert names == ["戊"]
    # 背景任务真的跑完了：戊 现在有 current，丁 的 current 没被重复生成打扰。
    assert voice_store.current_for(db.get_conn(), "p_missing_partial", "戊") is not None
    assert voice_store.current_for(db.get_conn(), "p_missing_partial", "丁")["id"] == ding_before["id"]


def test_trigger_auto_generate_after_portrait_noop_when_disabled(monkeypatch) -> None:
    _seed_project([_character("己")], project_id="p_hook_off")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    monkeypatch.setattr(voice_service, "voice_auto_generate_enabled", lambda: False)

    voice_service.trigger_auto_generate_after_portrait("p_hook_off", ["己"])

    assert task_registry.get(voice_service._VOICE_BULK_TASK_KIND, "p_hook_off") is None


def test_trigger_auto_generate_after_portrait_noop_when_not_configured(monkeypatch) -> None:
    _seed_project([_character("庚")], project_id="p_hook_unconf")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: None)
    monkeypatch.setattr(voice_service, "voice_auto_generate_enabled", lambda: True)

    voice_service.trigger_auto_generate_after_portrait("p_hook_unconf", ["庚"])

    assert task_registry.get(voice_service._VOICE_BULK_TASK_KIND, "p_hook_unconf") is None


def test_trigger_auto_generate_after_portrait_generates_missing_characters(monkeypatch) -> None:
    _seed_project([_character("辛"), _character("壬")], project_id="p_hook_on")
    monkeypatch.setattr(dispatch.routing, "resolve", lambda purpose: _resolved_model())
    monkeypatch.setattr(voice_service, "voice_auto_generate_enabled", lambda: True)
    _design_voice_ok(monkeypatch)
    _check_passed(monkeypatch)
    _fake_description(monkeypatch)

    async def _drive():
        voice_service.trigger_auto_generate_after_portrait("p_hook_on", ["辛", "壬"])
        task = task_registry.get(voice_service._VOICE_BULK_TASK_KIND, "p_hook_on")
        assert task is not None
        await task

    asyncio.run(_drive())

    for name in ("辛", "壬"):
        assert voice_store.current_for(db.get_conn(), "p_hook_on", name) is not None
