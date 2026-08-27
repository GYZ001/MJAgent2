from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
import subprocess

import pytest
from pydantic import BaseModel

from app.evidence import repository
from app.harness import model_gateway
from app.production import prep_pack


class _StrictResponse(BaseModel):
    ok: bool


# ---------------------------------------------------------------------------
# ERR-20260824-7ab7cb regression fixture: EP7 mapping-stage (episode_prep_pack)
# chunk-1 attempt 2, provider_calls.id=12417 (data/manju.db), verbatim
# choices[0].message.content -- a REAL stream-corrupted response, not a
# synthesized one. A duplicated/malformed "scenes" key mid-stream closes the
# intended root object early (leaving a complete, valid "characters" array
# with 孟浩's 33-segment mention stranded inside it) and what follows reads as
# a second, independent, bare trailing "scenes" array that merely happens to
# close its own brackets cleanly. The old `_latest_json_authority_root`
# picked "whichever non-nested root appears last in the text", which is that
# trailing bare array -- discarding the only object that actually carried
# `characters`. provider_calls.id=12418 is the real format-repair call that
# was actually issued against that stripped fragment; it faithfully returned
# `"characters": []`, and that empty list is what shipped as EP7's asset
# manifest (ERR-20260824-7ab7cb).
_EP7_CHUNK1_ATTEMPT2_RAW = json.loads(
    r'''"{\"characters\":[{\"display_name\":\"孟浩\",\"segment_indexes\":[2,4,7,9,10,11,14,15,17,21,22,23,24,25,26,27,28,29,30,31,32,33,35,36,37,38,39,40,41,43,44],\"suspected_true_name\":\"孟浩\"}],\"scenes\": [\n           \n            ],\"suspected_true_name\":\"靠山宗荒山丛林\"},{\"display_name\":\"洞府\",\"segment_indexes\":[24,25,26,27,28,29,30,31,33,34,35,36,37,38,39,43],\"suspected_true_name\":\"南峰山脚洞府\"}],\"paratext_segments\":[45],\"props\":[{\"description\":\"外观带有锈迹，拿在手中使用时可爆掉毛发茂盛的兽类屁股，吸收灵石后可复制妖丹\",\"label\":\"铜镜\",\"segment_indexes\":[7,9,17,19,21,22,25,26,33,34,35,37,38,39,43]},{\"description\":\"指甲盖大小，晶莹剔透，泌出阵阵清香，蕴含天地灵气，修士可直接服下\",\"label\":\"妖丹\",\"segment_indexes\":[23,24,25,26,28,34,35,36,37]},{\"description\":\"可辅助修士吸收灵气，是修士修行的必备物品\",\"label\":\"半块灵石\",\"segment_indexes\":[26,34,35]},{\"description\":\"宗门发放的丹药，入体即化，可化作丝丝灵气帮助修士修行\",\"label\":\"凝灵丹\",\"segment_indexes\":[26,27]}]\n        , \"scenes\": [\n            {\n                \"display_name\": \"荒山\",\n                \"quote\": \"好在这里荒山众多，否则的话若是都集中在一座山上，必定会让此山血气冲天。\",\n                \"segment_indexes\": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24],\n                \"suspected_true_name\": \"靠山宗荒山丛林\"\n            },\n            {\n                \"display_name\": \"洞府\",\n                \"quote\": \"回到洞府时已是深夜，孟浩盘膝坐在地上，看着手中的妖丹与铜镜，双眼光芒闪动。\",\n                \"segment_indexes\": [24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 43],\n                \"suspected_true_name\": \"南峰山脚洞府\"\n            }\n        ]}"'''
)


def test_format_repair_context_is_optional_keyword_only() -> None:
    parameter = inspect.signature(
        model_gateway.chat_structured
    ).parameters["format_repair_context"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == ""


def test_direct_chat_structured_calls_pass_required_keyword_only_arguments() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    required = {
        name
        for name, parameter in inspect.signature(
            model_gateway.chat_structured
        ).parameters.items()
        if (
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            and parameter.default is inspect.Parameter.empty
        )
    }
    violations: list[str] = []

    for relative_path in tracked:
        path = root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "chat_structured"
                and isinstance(function.value, ast.Name)
                and function.value.id == "model_gateway"
            ):
                continue
            explicit_keywords = {
                keyword.arg
                for keyword in node.keywords
                if keyword.arg is not None
            }
            missing = sorted(required - explicit_keywords)
            if missing:
                violations.append(
                    f"{relative_path}:{node.lineno} missing {', '.join(missing)}"
                )

    assert not violations, "\n".join(violations)


def test_chat_structured_keeps_required_response_format_across_format_retry(
    monkeypatch,
) -> None:
    schema = _StrictResponse.model_json_schema()
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "strict_response",
            "strict": True,
            "schema": schema,
        },
    }
    calls: list[tuple[list[dict[str, str]], dict]] = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return "not json" if len(calls) == 1 else '{"ok":true}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "Return JSON."}],
        model_type=_StrictResponse,
        validate=None,
        operation_id="strict-response-format",
        max_tokens=256,
        format_retry_limit=1,
        semantic_retry_limit=0,
        output_schema=schema,
        response_format=response_format,
        require_response_format=True,
    ))

    assert result == _StrictResponse(ok=True)
    assert len(calls) == 2
    assert all(
        kwargs["response_format"] is response_format
        and kwargs["call_meta"]["response_format_required"] is True
        for _messages, kwargs in calls
    )
    second_messages, second_kwargs = calls[1]
    expected_identity = repository.content_hash({
        "base_operation_id": "strict-response-format",
        "format_attempt": 1,
        "semantic_attempt": 0,
        "messages": second_messages,
        "max_tokens": 256,
        "temperature": 0.1,
        "structured_schema": schema,
        "response_format": response_format,
        "require_response_format": True,
    })
    assert second_kwargs["call_meta"]["operation_id"] == (
        "strict-response-format:structured-attempt:"
        + expected_identity
    )


def test_semantic_repair_stage_rejects_non_strict_response_format(
    monkeypatch,
) -> None:
    called = False

    async def forbidden_chat(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid repair contract reached provider")

    monkeypatch.setattr(model_gateway, "chat", forbidden_chat)
    with pytest.raises(
        ValueError,
        match="semantic_repair requires strict json_schema",
    ):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "repair"}],
            model_type=_StrictResponse,
            validate=None,
            operation_id="semantic-repair-json-object-forbidden",
            max_tokens=256,
            call_meta={
                "stage_key": "screenplay_scene_shard_semantic_repair",
            },
            response_format={"type": "json_object"},
            require_response_format=True,
        ))

    assert called is False


def test_structural_identity_coverage_rejects_non_strict_response_format(
    monkeypatch,
) -> None:
    called = False

    async def forbidden_chat(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid coverage contract reached provider")

    monkeypatch.setattr(model_gateway, "chat", forbidden_chat)
    with pytest.raises(
        ValueError,
        match="structural_coverage requires strict json_schema",
    ):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "coverage"}],
            model_type=_StrictResponse,
            validate=None,
            operation_id="coverage-json-object-forbidden",
            max_tokens=256,
            call_meta={
                "stage_key": "screenplay_character_discovery",
                "substage": "structural_coverage",
            },
            response_format={"type": "json_object"},
            require_response_format=True,
        ))

    assert called is False


def test_future_identity_rejects_attempt12_response_format_name(
    monkeypatch,
) -> None:
    called = False
    schema = _StrictResponse.model_json_schema()

    async def forbidden_chat(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("stale future contract reached provider")

    monkeypatch.setattr(model_gateway, "chat", forbidden_chat)
    with pytest.raises(
        ValueError,
        match="name=screenplay_future_identity_resolution_v10",
    ):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "future identity"}],
            model_type=_StrictResponse,
            validate=None,
            operation_id="attempt12-future-contract-forbidden",
            max_tokens=256,
            format_retry_limit=0,
            semantic_retry_limit=0,
            call_meta={
                "stage_key": "screenplay_character_discovery",
                "substage": "future_identity",
                "reuse_successful_operation": False,
                "disable_provider_retries": True,
                "disable_provider_candidate_fallback": True,
                "disable_reasoning_fallback": True,
            },
            output_schema=schema,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "screenplay_future_identity_resolution_v8",
                    "strict": True,
                    "schema": schema,
                },
            },
            require_response_format=True,
        ))

    assert called is False


def test_current_identity_rejects_attempt15_response_format_name(
    monkeypatch,
) -> None:
    called = False
    schema = _StrictResponse.model_json_schema()

    async def forbidden_chat(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("stale current identity contract reached provider")

    monkeypatch.setattr(model_gateway, "chat", forbidden_chat)
    with pytest.raises(
        ValueError,
        match="name=screenplay_current_identity_discovery_v11",
    ):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "current identity"}],
            model_type=_StrictResponse,
            validate=None,
            operation_id="attempt14-current-contract-forbidden",
            max_tokens=256,
            format_retry_limit=0,
            semantic_retry_limit=0,
            call_meta={
                "stage_key": "screenplay_character_discovery",
                "substage": "current_identity",
                "reuse_successful_operation": False,
                "disable_provider_retries": True,
                "disable_provider_candidate_fallback": True,
                "disable_reasoning_fallback": True,
            },
            output_schema=schema,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "screenplay_current_identity_discovery_v10",
                    "strict": True,
                    "schema": schema,
                },
            },
            require_response_format=True,
        ))

    assert called is False


def test_non_contract_character_discovery_keeps_default_structured_behavior(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["response_format"] is None
        assert kwargs["call_meta"]["expected_json"] is True
        return '{"ok":true}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "current identity"}],
        model_type=_StrictResponse,
        validate=None,
        operation_id="non-target-character-discovery",
        max_tokens=256,
        call_meta={
            "stage_key": "screenplay_character_discovery",
            "substage": "legacy_identity",
        },
    ))

    assert result == _StrictResponse(ok=True)
    assert calls == 1


def test_ep7_chunk1_authority_root_keeps_characters_ahead_of_trailing_scene_fragment() -> None:
    """Unit-level pin on the selection judgement itself (ERR-20260824-7ab7cb).

    ``_latest_json_authority_root`` still honors "the latest candidate wins"
    (a deliberate, separately-tested design --
    ``tests/test_screenplay_structured_runner.py`` pins that a corrupted or
    wrong-type *latest* root must win over an older, valid one; falling back
    silently would resurrect abandoned draft data). The narrower fix here
    only excludes a candidate proven to be a *dangling container child*: text
    immediately preceding it (a bare "," or a "key": prefix) proves, by JSON
    syntax alone, that it was authored as a value inside another
    list/object -- it only looks "top-level" because that real parent was
    closed early by corruption, exactly what happened to every array
    candidate in this real EP7 response (each one sits right after a
    ``"characters":``/``"scenes":``/``"props":``/``"paratext_segments":``-style
    prefix). With every array candidate excluded on that basis, the sole
    remaining eligible candidate is the leading object that actually carries
    孟浩's ``characters`` entry.
    """
    result = model_gateway._latest_json_authority_root(_EP7_CHUNK1_ATTEMPT2_RAW)

    assert result is not None
    root_type, root_text, candidate_count = result
    assert root_type == "object"
    assert candidate_count > 1  # multiple independent roots existed
    assert "孟浩" in root_text
    decoded, _ = json.JSONDecoder().raw_decode(root_text)
    assert decoded["characters"]
    assert decoded["characters"][0]["display_name"] == "孟浩"

    # Pin the actual distinguishing mechanism directly: every array candidate
    # in this response (including the trailing "scenes" fragment the old
    # position-only rule picked) is a dangling container child; the winning
    # object is not.
    candidates = model_gateway._json_authority_candidates(
        _EP7_CHUNK1_ATTEMPT2_RAW.strip()
    )
    array_candidates = [c for c in candidates if c[1] == "array"]
    assert array_candidates, "fixture must still contain the corrupted arrays"
    assert all(
        model_gateway._is_dangling_container_child(
            _EP7_CHUNK1_ATTEMPT2_RAW.strip(), index
        )
        for index, _root_type, _root_text in array_candidates
    )
    winning_index = next(
        index for index, _rt, rt in candidates if rt == root_text
    )
    assert not model_gateway._is_dangling_container_child(
        _EP7_CHUNK1_ATTEMPT2_RAW.strip(), winning_index
    )


_EP7_CHUNK1_REPAIR_SECOND_RESPONSE = json.dumps(
    {
        "characters": [
            {
                "display_name": "孟浩",
                "suspected_true_name": "孟浩",
                "segment_indexes": [2],
            }
        ],
        "scenes": [],
        "props": [],
        "paratext_segments": [],
    },
    ensure_ascii=False,
)


def test_chat_structured_repair_prompt_preserves_valid_characters_from_corrupted_ep7_chunk(
    monkeypatch,
) -> None:
    """Integration-level red/green proof for ERR-20260824-7ab7cb.

    Feeds the real corrupted provider response (provider_calls.id=12417) as
    the first attempt. It fails full schema validation regardless of which
    JSON authority root is chosen (extra ``suspected_true_name`` key at the
    root; missing required ``props``/``paratext_segments``), so a
    format-repair round always triggers -- what this test actually pins is
    *what gets sent to that repair round*. Before the fix, the picked
    recovery root was the bare trailing ``scenes`` array with no
    ``characters`` key at all; the real repair call
    (provider_calls.id=12418) was issued against exactly that fragment and
    faithfully answered ``"characters": []``, which is how EP7 shipped with
    zero characters. After the fix, the repair round must instead receive
    the valid ``characters`` array (with 孟浩's mention) that was present
    all along, earlier in the same response.
    """
    calls: list[tuple[list[dict[str, str]], dict]] = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return (
            _EP7_CHUNK1_ATTEMPT2_RAW
            if len(calls) == 1
            else _EP7_CHUNK1_REPAIR_SECOND_RESPONSE
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "extract chunk 1"}],
        model_type=prep_pack._ChunkResponse,
        validate=None,
        operation_id="episode_prep_pack:ep_621d93ac1231:chunk:1",
        max_tokens=8000,
        format_retry_limit=1,
        semantic_retry_limit=0,
        call_meta={
            "stage_key": "episode_prep_pack_event_chain",
            "episode_id": "ep_621d93ac1231",
            "chunk_index": 1,
        },
        response_format=prep_pack._response_format(
            prep_pack._ChunkResponse, "episode_prep_pack_chunk_v4"
        ),
        require_response_format=True,
    ))

    assert len(calls) == 2
    repair_prompt = calls[1][0][0]["content"]
    # The repair prompt always contains the *schema* (which always names
    # "characters" as a property) and the corrupted response also mentions
    # 孟浩's name incidentally inside a scene's `quote` field -- neither is a
    # valid discriminator on its own. The one signal that can only appear if
    # the actual characters *data* reached the repair round is 孟浩's
    # mention's own segment_indexes list, copied verbatim from the real
    # corrupted response's characters[0].segment_indexes.
    candidate_section = repair_prompt.split("完整候选：\n", 1)[1]
    meng_hao_segment_indexes = (
        "[2,4,7,9,10,11,14,15,17,21,22,23,24,25,26,27,28,29,30,31,32,33,"
        "35,36,37,38,39,40,41,43,44]"
    )
    assert meng_hao_segment_indexes in candidate_section, candidate_section
    assert '"characters":[{' in candidate_section, candidate_section
    # Sanity: the overall call still completes successfully end to end.
    assert result.characters
    assert result.characters[0].display_name == "孟浩"


def test_chat_structured_records_discarded_json_candidate_event(monkeypatch) -> None:
    """The discard must stay visible even though the final result validates.

    A silently-empty ``characters: []`` and a silently-discarded populated
    one pass schema validation identically -- the only way to catch this
    failure shape is an explicit, always-fired signal at the moment a
    candidate was passed over, not a downstream validation error. This pins
    that the ``STRUCTURED_JSON_RECOVERY_CANDIDATE_DISCARDED`` run event
    fires on the corrupted first attempt even though the run overall ends up
    with a valid result after the second.
    """
    from app.orchestration.engine import WorkflowRecorder

    calls: list[list[dict[str, str]]] = []

    async def fake_chat(messages, **_kwargs):
        calls.append(messages)
        return (
            _EP7_CHUNK1_ATTEMPT2_RAW
            if len(calls) == 1
            else _EP7_CHUNK1_REPAIR_SECOND_RESPONSE
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="ep_621d93ac1231",
        input_fingerprint="ep7-chunk1-corrupted-fixture",
    )
    recorder.start()

    async def operation():
        return await model_gateway.chat_structured(
            [{"role": "user", "content": "extract chunk 1"}],
            model_type=prep_pack._ChunkResponse,
            validate=None,
            operation_id="episode_prep_pack:ep_621d93ac1231:chunk:1",
            max_tokens=8000,
            format_retry_limit=1,
            semantic_retry_limit=0,
            call_meta={
                "stage_key": "episode_prep_pack_event_chain",
                "episode_id": "ep_621d93ac1231",
                "chunk_index": 1,
            },
            response_format=prep_pack._response_format(
                prep_pack._ChunkResponse, "episode_prep_pack_chunk_v4"
            ),
            require_response_format=True,
        )

    _, result = asyncio.run(
        recorder.step(
            "storyboard", operation, contract_key="storyboard", agent_name="storyboard"
        )
    )
    recorder.succeed(conn=None)

    assert result.characters
    events = repository.get_events(recorder.run_id, limit=100)
    discard_events = [
        event for event in events
        if event["event_type"] == "STRUCTURED_JSON_RECOVERY_CANDIDATE_DISCARDED"
    ]
    assert len(discard_events) == 1
    payload = discard_events[0]["payload"]
    assert payload["chosen_root_type"] == "object"
    assert payload["candidate_count"] > 1
    assert payload["stage_key"] == "episode_prep_pack_event_chain"
