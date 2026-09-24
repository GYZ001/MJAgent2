"""``app.voice.segment_refs``：说话人 -> 参考音频清单解析规则逐条验证。

用真实 ``app.db.get_conn()``（autouse 隔离夹具给的 per-test 干净库）与真实
``app.voice.store``/``character_portraits`` 落库，不打桩数据层——规则本身
就是对这两张表的查询结果做判断，打桩会验证不到真实查询路径。
"""
from __future__ import annotations

import pytest

from app import db
from app.voice import segment_refs
from app.voice import store as voice_store
from app.voice.segment_refs import (
    DEFAULT_MAX_SPEAKERS,
    SETTING_KEY_ENABLED,
    SETTING_KEY_MAX_SPEAKERS,
    configured_max_speakers,
    fingerprint_reference_audios,
    reference_audio_enabled,
    resolve_segment_reference_audios,
)

PROJECT_ID = "p1"


def _conn():
    return db.get_conn()


def _seed_project(project_id: str = PROJECT_ID) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, bible_json, bible_version) "
        "VALUES(?,?,?,?,?,?)",
        (project_id, "测试项目", "created", db.now(), "{}", 1),
    )
    conn.commit()


def _insert_portrait(portrait_id: str, character_name: str, *, anchor_key: str = "") -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        (portrait_id, PROJECT_ID, character_name, 1, None, f"/tmp/{portrait_id}.jpg", db.now()),
    )
    conn.commit()
    if anchor_key:
        from app.portraits.portrait_lookup import _ensure_anchor_key_column

        _ensure_anchor_key_column(conn)
        conn.execute("UPDATE character_portraits SET anchor_key=? WHERE id=?", (anchor_key, portrait_id))
        conn.commit()


def _adopt_voice(character_name: str, *, anchor_key: str = "", clip_sha256: str = "sha-x",
                  clip_duration_s: float = 4.0) -> str:
    conn = _conn()
    voice_id = voice_store.insert_generating(
        conn, project_id=PROJECT_ID, character_name=character_name, anchor_key=anchor_key,
        model_id="m1", voice_prompt="p", preview_text="t", created_by="tester",
    )
    conn.commit()
    voice_store.mark_finished(
        conn, PROJECT_ID, voice_id, status=voice_store.STATUS_CANDIDATE,
        clip_path=f"/tmp/{voice_id}_clip.wav", clip_sha256=clip_sha256, clip_duration_s=clip_duration_s,
    )
    conn.commit()
    voice_store.set_current(conn, PROJECT_ID, character_name, voice_id, anchor_key, adopted_by="tester")
    conn.commit()
    return voice_id


def _dialogue(*speakers: str) -> list[dict]:
    return [{"speaker_identity_id": speaker, "line": f"line {i}"} for i, speaker in enumerate(speakers)]


def _segment(dialogue: list[dict], characters: list[dict] | None = None) -> dict:
    return {"dialogue": dialogue, "resources": {"characters": characters or []}}


_CAP = {
    "has_visual_reference": True, "max_speakers": 3,
    "supports_reference_audio": True, "max_reference_audios": 3,
    "max_reference_audio_total_s": 15.0,
}


def _resolve(segment: dict, **overrides) -> tuple[list[dict], list[dict]]:
    kwargs = {**_CAP, **overrides}
    return resolve_segment_reference_audios(conn=_conn(), project_id=PROJECT_ID, segment=segment, **kwargs)


@pytest.fixture(autouse=True)
def _project() -> None:
    _seed_project()


def test_empty_dialogue_returns_empty_lists() -> None:
    refs, skips = _resolve(_segment([]))
    assert refs == []
    assert skips == []


def test_narration_and_entity_speakers_are_excluded() -> None:
    segment = _segment(_dialogue("旁白", "entity:abc123"))
    refs, skips = _resolve(segment)
    assert refs == []
    assert skips == []


def test_speaker_without_voice_is_skipped_with_reason() -> None:
    segment = _segment(_dialogue("bible:张三"))
    refs, skips = _resolve(segment)
    assert refs == []
    assert skips == [{"character_name": "张三", "reason": "未配置声音"}]


def test_speaker_with_current_voice_is_included() -> None:
    _adopt_voice("张三")
    segment = _segment(_dialogue("bible:张三", "bible:张三"))
    refs, skips = _resolve(segment)
    assert skips == []
    assert len(refs) == 1
    assert refs[0]["index"] == 1
    assert refs[0]["character_name"] == "张三"
    assert refs[0]["anchor_key"] == ""
    assert refs[0]["clip_sha256"] == "sha-x"


def test_offscreen_and_inner_monologue_delivery_still_counted() -> None:
    """含画外音/内心独白：speaker_identity_id 仍是 bible: 前缀，判据只看前缀。"""
    _adopt_voice("张三")
    segment = _segment([
        {"speaker_identity_id": "bible:张三", "line": "画外音", "delivery_kind": "offscreen_dialogue"},
        {"speaker_identity_id": "bible:张三", "line": "心里想", "delivery_kind": "inner_monologue"},
    ])
    refs, _skips = _resolve(segment)
    assert len(refs) == 1 and refs[0]["character_name"] == "张三"


def test_ranking_prefers_more_lines_then_first_appearance() -> None:
    _adopt_voice("甲")
    _adopt_voice("乙")
    _adopt_voice("丙")
    # 乙 2 句最多排第一；甲、丙同为 1 句时按首次出场，甲先出场排在丙之前。
    segment = _segment(_dialogue("bible:甲", "bible:丙", "bible:乙", "bible:乙"))
    refs, skips = _resolve(segment, max_speakers=3)
    assert skips == []
    assert [r["character_name"] for r in refs] == ["乙", "甲", "丙"]
    assert [r["index"] for r in refs] == [1, 2, 3]


def test_exceeds_max_speakers_cap_is_skipped() -> None:
    for name in ("甲", "乙", "丙", "丁"):
        _adopt_voice(name)
    segment = _segment(_dialogue("bible:甲", "bible:乙", "bible:丙", "bible:丁"))
    refs, skips = _resolve(segment, max_speakers=2)
    assert [r["character_name"] for r in refs] == ["甲", "乙"]
    assert {"character_name": "丙", "reason": "超出每段 2 个上限"} in skips
    assert {"character_name": "丁", "reason": "超出每段 2 个上限"} in skips


def test_effective_cap_is_smaller_of_setting_and_capability() -> None:
    for name in ("甲", "乙", "丙"):
        _adopt_voice(name)
    segment = _segment(_dialogue("bible:甲", "bible:乙", "bible:丙"))
    refs, skips = _resolve(segment, max_speakers=3, max_reference_audios=1)
    assert [r["character_name"] for r in refs] == ["甲"]
    assert len(skips) == 2
    assert all(s["reason"] == "超出每段 1 个上限" for s in skips)


def test_non_default_anchor_without_matching_voice_is_skipped() -> None:
    _insert_portrait("port_1", "张三", anchor_key="age:8")
    _adopt_voice("张三")  # 只有默认年龄段的声音
    segment = _segment(
        _dialogue("bible:张三"),
        characters=[{"identity_id": "bible:张三", "portrait_id": "port_1"}],
    )
    refs, skips = _resolve(segment)
    assert refs == []
    assert skips == [{"character_name": "张三", "reason": "该年龄段未绑定声音"}]


def test_non_default_anchor_with_matching_voice_is_included() -> None:
    _insert_portrait("port_1", "张三", anchor_key="age:8")
    _adopt_voice("张三", anchor_key="age:8", clip_sha256="sha-child")
    segment = _segment(
        _dialogue("bible:张三"),
        characters=[{"identity_id": "bible:张三", "portrait_id": "port_1"}],
    )
    refs, skips = _resolve(segment)
    assert skips == []
    assert refs[0]["anchor_key"] == "age:8"
    assert refs[0]["clip_sha256"] == "sha-child"


def test_no_visual_reference_skips_everyone() -> None:
    _adopt_voice("张三")
    segment = _segment(_dialogue("bible:张三"))
    refs, skips = _resolve(segment, has_visual_reference=False)
    assert refs == []
    assert skips == [{"character_name": "张三", "reason": "本段没有参考图，视频模型不接受只传声音"}]


def test_model_without_audio_support_skips_everyone() -> None:
    _adopt_voice("张三")
    segment = _segment(_dialogue("bible:张三"))
    refs, skips = _resolve(segment, supports_reference_audio=False)
    assert refs == []
    assert skips == [{"character_name": "张三", "reason": "当前视频模型未接入参考音频"}]


def test_total_duration_over_cap_truncates_from_the_tail() -> None:
    _adopt_voice("甲", clip_duration_s=5.0)
    _adopt_voice("乙", clip_duration_s=5.0)
    _adopt_voice("丙", clip_duration_s=5.0)
    segment = _segment(_dialogue("bible:甲", "bible:乙", "bible:丙"))
    refs, skips = _resolve(segment, max_reference_audio_total_s=12.0)
    assert [r["character_name"] for r in refs] == ["甲", "乙"]
    assert skips == [{"character_name": "丙", "reason": "超出总时长上限"}]


def test_total_duration_exactly_at_cap_keeps_all() -> None:
    _adopt_voice("甲", clip_duration_s=5.0)
    _adopt_voice("乙", clip_duration_s=5.0)
    _adopt_voice("丙", clip_duration_s=5.0)
    segment = _segment(_dialogue("bible:甲", "bible:乙", "bible:丙"))
    refs, skips = _resolve(segment, max_reference_audio_total_s=15.0)
    assert len(refs) == 3
    assert skips == []


def test_fingerprint_is_stable_and_sensitive_to_voice_change() -> None:
    refs_a = [{"character_name": "甲", "anchor_key": "", "voice_id": "v1", "clip_sha256": "s1"}]
    refs_b = [{"character_name": "甲", "anchor_key": "", "voice_id": "v1", "clip_sha256": "s1"}]
    refs_c = [{"character_name": "甲", "anchor_key": "", "voice_id": "v2", "clip_sha256": "s2"}]
    assert fingerprint_reference_audios(refs_a) == fingerprint_reference_audios(refs_b)
    assert fingerprint_reference_audios(refs_a) != fingerprint_reference_audios(refs_c)
    assert fingerprint_reference_audios([]) == ""


def test_settings_default_off_and_default_cap_three(monkeypatch) -> None:
    # segment_refs.py 是 get_setting 唯一消费方（from app.db import get_setting 的名字
    # 拷贝），打在 db 模块本体上对它无效，必须打在它自己的命名空间——同
    # tests/test_voice_service.py 模块 docstring 说明的「安全单一绑定点」。
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "")
    assert reference_audio_enabled() is False
    assert configured_max_speakers() == DEFAULT_MAX_SPEAKERS == 3


def test_settings_honor_explicit_values(monkeypatch) -> None:
    values = {SETTING_KEY_ENABLED: "true", SETTING_KEY_MAX_SPEAKERS: "1"}
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: values.get(key, ""))
    assert reference_audio_enabled() is True
    assert configured_max_speakers() == 1


def test_max_speakers_setting_clamped_to_1_3(monkeypatch) -> None:
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "99")
    assert configured_max_speakers() == 3
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "0")
    assert configured_max_speakers() == 1
    monkeypatch.setattr(segment_refs, "get_setting", lambda key: "not-a-number")
    assert configured_max_speakers() == DEFAULT_MAX_SPEAKERS
