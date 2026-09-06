"""结构化输出驼峰键归一（2026-09-05 验收轮：称谓归属 20 条里 1 条 rawLabel 让整集映射台失败）。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.harness.model_gateway import _coerce_structured
from app.harness.structured_key_case import snake_case_keys_for_model, to_snake_case


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_label: str
    identity: str
    segment_indexes: list[int] = []


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appellations: list[_Verdict] = []
    note_text: str = ""


def test_camel_keys_are_renamed_only_when_the_snake_field_exists() -> None:
    payload = {
        "appellations": [
            {"raw_label": "少年", "identity": "K:1", "segmentIndexes": [1]},
            {"rawLabel": "四个拥有资质的小娃", "identity": "F:1"},
        ],
        "noteText": "x",
    }
    fixed = snake_case_keys_for_model(_Response, payload)
    assert fixed == {
        "appellations": [
            {"raw_label": "少年", "identity": "K:1", "segment_indexes": [1]},
            {"raw_label": "四个拥有资质的小娃", "identity": "F:1"},
        ],
        "note_text": "x",
    }
    assert _coerce_structured(_Response, payload).appellations[1].raw_label == "四个拥有资质的小娃"


def test_genuinely_extra_keys_still_fail_closed() -> None:
    payload = {"appellations": [{"raw_label": "少年", "identity": "K:1", "confidence": 0.9}]}
    assert snake_case_keys_for_model(_Response, payload) == payload
    with pytest.raises(ValidationError):
        _coerce_structured(_Response, payload)


def test_snake_key_present_wins_over_camel_duplicate_and_non_models_pass_through() -> None:
    payload = {"appellations": [{"raw_label": "甲", "rawLabel": "乙", "identity": "K:1"}]}
    fixed = snake_case_keys_for_model(_Response, payload)
    assert fixed["appellations"][0]["raw_label"] == "甲" and "rawLabel" in fixed["appellations"][0]
    assert snake_case_keys_for_model(dict, payload) is payload
    assert snake_case_keys_for_model(_Response, "not a dict") == "not a dict"
    assert to_snake_case("rawLabel") == "raw_label" and to_snake_case("segmentIndexes") == "segment_indexes"
    assert to_snake_case("raw_label") == "raw_label"


def test_duplicate_camel_and_snake_with_equal_values_drops_the_camel_copy() -> None:
    """第 11 轮第 1 集：同一条里 raw_label 与 rawLabel 都写了、值相同，extra=forbid 仍拒——多余的那份丢掉；值不同则保留让校验拒。"""
    from pydantic import BaseModel, ConfigDict

    class Item(BaseModel):
        model_config = ConfigDict(extra="forbid")
        raw_label: str

    class Resp(BaseModel):
        model_config = ConfigDict(extra="forbid")
        appellations: list[Item]

    same = snake_case_keys_for_model(Resp, {"appellations": [{"raw_label": "他", "rawLabel": "他"}]})
    assert same == {"appellations": [{"raw_label": "他"}]}
    Resp.model_validate(same)
    different = snake_case_keys_for_model(Resp, {"appellations": [{"raw_label": "他", "rawLabel": "她"}]})
    assert "rawLabel" in different["appellations"][0]
