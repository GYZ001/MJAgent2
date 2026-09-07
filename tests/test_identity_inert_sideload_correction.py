"""非 NEW 决议多填侧载：给一次指名纠正的重问；其余语义违规照旧第一时间 fail closed。

2026-09-07 第 53 集：模型给 G001 选了非 N: 决议（判断本身没问题），却顺手填了下游一个字都不读的
侧载字段，整集映射台失败。侧载只有 new_named 才被消费，所以纠正它不是「判断错了再摇一次」。
"""
from __future__ import annotations

import asyncio

import pytest

from app.harness import model_gateway
from app.portraits import discovery_resample as dr


def _semantic(detail: str) -> model_gateway.StructuredSemanticError:
    return model_gateway.StructuredSemanticError(detail)


def test_only_the_inert_sideload_class_earns_a_correction() -> None:
    assert dr._semantic_correction_note(_semantic("future identity 非 NEW 决议侧载必须为空：G001"))
    assert dr._semantic_correction_note(_semantic(
        "future identity 非 NEW 决议侧载必须为空：G001；future identity 非 NEW 决议侧载必须为空：G002"
    ))
    # 判断本身有问题的每一类都不给第二次机会
    assert dr._semantic_correction_note(_semantic("future identity decision_id 越界：G001")) == ""
    assert dr._semantic_correction_note(_semantic(
        "future identity 非 NEW 决议侧载必须为空：G001；future identity decision_id 越界：G002"
    )) == ""
    assert dr._semantic_correction_note(_semantic("")) == ""


def _run(errors: list[Exception | None], monkeypatch) -> tuple[object, list[dict]]:
    seen: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        seen.append({"content": messages[-1]["content"], "operation_id": kwargs["operation_id"],
                     "temperature": kwargs.get("temperature")})
        outcome = errors[len(seen) - 1]
        if outcome is not None:
            raise outcome
        return "ok"

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    result = asyncio.run(dr._identity_structured_with_resample(
        [{"role": "user", "content": "原始提示词"}],
        model_type=object, validate=lambda _v: [], max_tokens=128, temperature=0.1,
        operation_id_for_attempt=lambda attempt: f"op:{attempt}",
        call_meta={"stage": "x"},
    ))
    return result, seen


def test_inert_sideload_is_corrected_once_with_the_violation_quoted(monkeypatch) -> None:
    result, seen = _run([_semantic("future identity 非 NEW 决议侧载必须为空：G001"), None], monkeypatch)
    assert result == "ok" and len(seen) == 2
    assert "非 NEW 决议侧载必须为空：G001" in seen[1]["content"]  # 指名违规
    assert seen[1]["operation_id"] != seen[0]["operation_id"]  # 幂等账目仍然精确
    assert seen[1]["temperature"] == seen[0]["temperature"]  # 纠正不抬温度：不是再摇一次骰子


def test_a_second_identical_violation_fails_closed(monkeypatch) -> None:
    err = _semantic("future identity 非 NEW 决议侧载必须为空：G001")
    with pytest.raises(model_gateway.StructuredSemanticError):
        _run([err, err, None], monkeypatch)


def test_other_semantic_violations_are_still_one_call(monkeypatch) -> None:
    with pytest.raises(model_gateway.StructuredSemanticError):
        _run([_semantic("future identity decision_id 越界：G001"), None], monkeypatch)
