from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
import subprocess

import pytest
from pydantic import BaseModel

from app.evidence import repository
from app.harness import model_gateway


class _StrictResponse(BaseModel):
    ok: bool


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


def test_current_identity_rejects_attempt14_response_format_name(
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
        match="name=screenplay_current_identity_discovery_v9",
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
                    "name": "screenplay_current_identity_discovery_v8",
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
