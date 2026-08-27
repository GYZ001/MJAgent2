import pytest

from app import errors
from app.schemas import extract_json, normalize_screenplay_json_shape
from app.stages import StageError


def test_extract_json_skips_non_json_braces_before_payload() -> None:
    text = """说明：下面这个 {不是 JSON，只是普通说明}

```json
{
  "characters": [],
  "world": {"visual_style_canonical": "竖屏漫画，清晰线稿，柔和光影"}
}
```
"""

    obj = extract_json(text)

    assert obj["characters"] == []
    assert obj["world"]["visual_style_canonical"] == "竖屏漫画，清晰线稿，柔和光影"


def test_extract_json_reports_missing_object() -> None:
    with pytest.raises(ValueError, match="找不到 JSON"):
        extract_json("没有对象")


def test_extract_json_does_not_accept_nested_object_from_broken_root() -> None:
    text = '''{
  "episode_no": 1,
  "shot": {
    "source_excerpt": "少女轻声唤他"甲一哥哥。"随后弯腰",
    "dialogues": [{"speaker": "甲二儿", "line": "甲一哥哥。"}]
  }
}'''

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)


def test_extract_json_rejects_object_inside_unclosed_top_level_string() -> None:
    text = '"unfinished {"issues":[]}'

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text, repair_unescaped_inner_quotes=True)


def test_extract_json_can_repair_unescaped_quotes_for_screenplay_only() -> None:
    text = '''{
  "episode_no": 1,
  "turn": {"speaker": "围观者", "line": "这个"天才"仍在原地", "source_text": "这个“天才”仍在原地"}
}'''

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)

    obj = extract_json(text, repair_unescaped_inner_quotes=True)
    assert obj["turn"]["line"] == '这个"天才"仍在原地'
    assert obj["turn"]["source_text"] == "这个“天才”仍在原地"


def test_extract_json_repairs_bare_string_array_inside_string() -> None:
    # Production 29805 shape, with run-specific content and identifiers redacted.
    text = '''{"issues":[
    {
        "code":"state_subject_assignment_conflict",
        "node_keys":["S005-E01-S05-N001"],
        "message":"joint主体与原文冲突",
        "required_resolution":"修正为["孟浩","王有材"]并保持其余字段"
    },
    {
        "code":"timeline_conflict",
        "node_keys":["S004-N001"],
        "message":"时间顺序冲突",
        "required_resolution":"恢复原文顺序"
    }
]}'''

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)

    obj = extract_json(text, repair_unescaped_inner_quotes=True)

    assert len(obj["issues"]) == 2
    assert obj["issues"][0]["required_resolution"] == (
        '修正为["孟浩","王有材"]并保持其余字段'
    )
    assert obj["issues"][1]["code"] == "timeline_conflict"


def test_extract_json_preserves_escapes_in_embedded_string_array() -> None:
    embedded_fragment = r'["第一行\n第二行","他说\"好\"","C:\\tmp"]'
    text = (
        '{"required_resolution":"修正为'
        + embedded_fragment
        + '并保持其余字段"}'
    )

    obj = extract_json(text, repair_unescaped_inner_quotes=True)

    assert obj["required_resolution"] == (
        "修正为" + embedded_fragment + "并保持其余字段"
    )


def test_embedded_array_repair_does_not_hide_missing_outer_comma() -> None:
    text = (
        '{"required_resolution":"修正为["甲","乙"]并保持其余字段" '
        '"next":"不得改写为合并字段"}'
    )

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text, repair_unescaped_inner_quotes=True)


def test_inner_quote_repair_does_not_hide_json_structure_errors() -> None:
    text = '{"episode_no": 1, "title": "第一集" "logline": "缺少逗号"}'
    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text, repair_unescaped_inner_quotes=True)


def test_extract_json_repairs_declared_singleton_string_object_field_only() -> None:
    text = (
        '{"audience_prior_id":"AP-1",'
        '"attention_memory_assumptions":{"关注主角情绪变化"}}'
    )

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)

    assert extract_json(
        text,
        repair_singleton_string_object_fields=("attention_memory_assumptions",),
    )["attention_memory_assumptions"] == {
        "description": "关注主角情绪变化",
    }


def test_extract_json_closes_only_missing_root_brace_at_eof() -> None:
    text = '{"episode_no": 9, "forbidden_additions": ["禁止发明新角色"]'

    assert extract_json(text) == {
        "episode_no": 9,
        "forbidden_additions": ["禁止发明新角色"],
    }


def test_extract_json_combines_inner_quote_and_missing_root_repairs() -> None:
    text = '{"episode_no": 9, "line": "这个"天才"仍在原地"'

    assert extract_json(text, repair_unescaped_inner_quotes=True) == {
        "episode_no": 9,
        "line": '这个"天才"仍在原地',
    }


def test_extract_json_closes_array_before_next_object_field() -> None:
    text = (
        '{"approved_adaptations":["保留核心冲突",'
        '"简化露骨描写",'
        '"forbidden_additions":["禁止原创人物"]}'
    )

    assert extract_json(text) == {
        "approved_adaptations": ["保留核心冲突", "简化露骨描写"],
        "forbidden_additions": ["禁止原创人物"],
    }


def test_extract_json_escapes_raw_newline_inside_string() -> None:
    text = '{"characters":[{"evidence":"第一行\n第二行"}]}'

    assert extract_json(text) == {
        "characters": [{"evidence": "第一行\n第二行"}],
    }


def test_extract_json_uses_formal_payload_after_think_marker() -> None:
    text = (
        '{"characters":[{"broken":"模型思考\n仍在继续"}]}\n'
        '</think_never_used_deadbeef>'
        '{"characters":[{"source_label":"老头"}]}'
    )

    assert extract_json(text) == {
        "characters": [{"source_label": "老头"}],
    }


def test_extract_json_closes_multiple_missing_trailing_containers() -> None:
    # ERR-20260826-93c8e3 (run_f8a23b28d098, provider_calls id 12985/12986): the
    # model stopped (finish_reason=stop, not a token-budget truncation) right
    # after writing a complete value two containers deep, with neither the
    # array's nor the root object's closing bracket ever emitted. This used to
    # be refused outright (`_close_missing_root_object` only handled exactly
    # one missing `}`); it is now a supported case of
    # `_close_missing_trailing_containers`, which is deliberately updated here
    # -- see logs/structured_output_resilience_plan.md P0-B for why closing an
    # unbounded (not just single) stack of trailing containers is still a
    # syntax-only, fail-closed repair, not a semantic guess.
    text = '{"episode_no": 9, "events": [{"event_id": "E1"'

    assert extract_json(text) == {
        "episode_no": 9,
        "events": [{"event_id": "E1"}],
    }


def test_extract_json_refuses_multi_closer_repair_inside_unterminated_string() -> None:
    # Same shape as above, but the final string was itself cut off mid-write
    # (no closing quote on "E1). This is the ambiguous case the syntax-only
    # repair must still refuse: appending closers over a dangling string would
    # paper over missing content, not complete a finished value.
    text = '{"episode_no": 9, "events": [{"event_id": "E1'

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)


def test_extract_json_repairs_unique_wrong_eof_closer_sequence() -> None:
    text = (
        '{"episode_no":1,"narrative_plan":{"arc_contracts":'
        '[{"arc_id":"ARC-1"}]}}'
    )
    broken = text[:-3] + "}"

    assert extract_json(broken) == {
        "episode_no": 1,
        "narrative_plan": {
            "arc_contracts": [{"arc_id": "ARC-1"}],
        },
    }


def test_extract_json_does_not_repair_non_eof_closer_mismatch() -> None:
    text = '{"episode_no":1,"events":}[]}'

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)


def test_extract_json_repairs_array_missing_object_brace() -> None:
    # Production ERR-20260819-ebf9b6: the model emitted a doubled comma and
    # omitted the opening brace of the next object inside a characters array.
    text = '''{"characters": [
    {"source_label": "甲", "canonical_name": "", "identity_kind": "functional", "future_evidence": ""},
    {"source_label": "乙", "canonical_name": "乙真", "identity_kind": "named", "future_evidence": "乙真"},
    ,"canonical_name": "", "identity_kind": "functional", "future_evidence": ""}'''

    obj = extract_json(text, repair_unescaped_inner_quotes=True)

    assert len(obj["characters"]) == 3
    assert obj["characters"][2]["canonical_name"] == ""
    assert obj["characters"][2]["identity_kind"] == "functional"


def test_extract_json_repairs_fullwidth_closing_quote() -> None:
    # The model used a full-width closing quote instead of the required ASCII
    # quote, leaving the JSON string open across a newline until the next
    # object.  This is the malformed shape seen in character-discovery runs.
    text = '''{
  "characters": [
    {"source_label": "甲", "canonical_name": "", "identity_kind": "functional", "future_evidence": ""},
    {"source_label": "乙", "canonical_name": "乙真", "identity_kind": "named", "future_evidence": "他说：“乙真。”
    },
    {"source_label": "丙", "canonical_name": "", "identity_kind": "functional", "future_evidence": ""}
  ]
}'''

    obj = extract_json(text, repair_unescaped_inner_quotes=True)

    assert [item["source_label"] for item in obj["characters"]] == ["甲", "乙", "丙"]
    assert obj["characters"][1]["future_evidence"] == "他说：“乙真。"


def test_screenplay_shape_hoists_fields_misnested_under_plot_spine() -> None:
    # One brace closes plot_spine; the missing final brace closes the root.
    # This is the shape observed in ERR-20260803-df0cee.
    text = '''{"episode_no": 9, "plot_spine": {
        "episode_premise": "处理赵武刚",
        "spine_beats": [],
        "must_keep_ending": "孟浩突破",
        "drop_list": [],
        "scene_outline": [{"scene_no": 1}],
        "full_script_text": "【场1】孟浩举起铜镜。",
        "events": [{"event_id": "E1"}],
        "forbidden_additions": ["禁止发明新角色"]}'''

    parsed = extract_json(text)
    normalized, moved = normalize_screenplay_json_shape(parsed)

    assert normalized["plot_spine"] == {
        "episode_premise": "处理赵武刚",
        "spine_beats": [],
        "must_keep_ending": "孟浩突破",
        "drop_list": [],
    }
    assert normalized["scene_outline"] == [{"scene_no": 1}]
    assert normalized["events"] == [{"event_id": "E1"}]
    assert set(moved) == {
        "scene_outline", "full_script_text", "events", "forbidden_additions",
    }


def test_screenplay_shape_preserves_string_familiarity_assumptions_as_objects() -> None:
    payload = {
        "episode_no": 1,
        "narrative_plan": {
            "audience_priors": [{
                "audience_prior_id": "AP-1",
                "familiarity_assumptions": ["知道科举，但不了解修仙宗门"],
            }],
        },
    }

    normalized, changes = normalize_screenplay_json_shape(payload)

    assumptions = normalized["narrative_plan"]["audience_priors"][0][
        "familiarity_assumptions"
    ]
    assert assumptions == [{"description": "知道科举，但不了解修仙宗门"}]
    assert changes == [
        "narrative_plan.audience_priors[0].familiarity_assumptions[0]",
    ]
    assert payload["narrative_plan"]["audience_priors"][0][
        "familiarity_assumptions"
    ] == ["知道科举，但不了解修仙宗门"]


def test_screenplay_shape_normalizes_action_relation_audit_aliases() -> None:
    payload = {
        "episode_no": 1,
        "narrative_plan": {
            "action_relation_audits": [{
                "audit_id": "ARA-1",
                "action_ids": ["A-1", "A-2"],
                "relation": "sequential_distinct",
                "rationale": "动作目标不同，因果递进",
            }],
        },
    }

    normalized, changes = normalize_screenplay_json_shape(payload)
    audit = normalized["narrative_plan"]["action_relation_audits"][0]

    assert audit["action_relation_audit_id"] == "ARA-1"
    assert audit["semantically_equivalent"] is False
    assert audit["reason"] == "动作目标不同，因果递进"
    assert len(changes) == 3


def test_json_stage_failure_has_specific_error_code_and_non_blind_hint() -> None:
    exc = StageError("剧本首次整版 Baseline", ["JSON 解析失败（line 39）"])
    assert errors.classify(exc) == ("generation", "JSON")
    hint = errors.CATEGORIES["generation"]["hint"]
    assert "先按错误码检查具体原因" in hint


def test_structured_format_error_no_longer_falls_back_to_system_category() -> None:
    # ERR-20260826-93c8e3: app.harness.model_gateway.StructuredFormatError had no
    # classify() branch at all, so it fell to the "system"/"SYS" fallback and the
    # user saw "系统内部错误，请把错误码反馈给技术人员" for what was actually a
    # retryable structured-output failure. It must land in the same
    # generation/JSON bucket as the StageError + "JSON 解析失败" case above, with
    # the same "可点击重试" hint.
    from app.harness.model_gateway import StructuredFormatError

    exc = StructuredFormatError("op 结构化输出失败：Expecting ',' delimiter")
    assert errors.classify(exc) == ("generation", "JSON")
    hint = errors.CATEGORIES["generation"]["hint"]
    assert "可点击重试" in hint


def test_structured_provider_rejection_classifies_as_provider_failure() -> None:
    # Sibling of StructuredFormatError/StructuredSemanticError, raised by the
    # same chat_structured() for an explicit provider refusal envelope. Only
    # one call site (app/media_exec/run_job.py) converted it to ProviderError
    # before logging; every other caller (e.g. the identity-resample path in
    # app/portraits.py) let it bubble up with its raw type, which classify()
    # had no branch for.
    from app.harness.model_gateway import StructuredProviderRejection

    exc = StructuredProviderRejection("content policy rejection")
    assert errors.classify(exc) == ("provider", "LLM")


def test_classify_dispatch_follows_inheritance_not_just_exact_class_name() -> None:
    # SceneAssetQualityError(ContentGenerationError) is a live example of the
    # structural gap: classify() used to match only `type(exc).__name__`
    # exactly, so a subclass of an already-classified exception silently
    # bypassed its parent's classification and fell through to the system
    # fallback. classify() now matches against the full MRO name set instead.
    from app.scenes import SceneAssetQualityError

    exc = SceneAssetQualityError("场景状态变化版本未能创建：卧室")
    assert errors.classify(exc) == ("quality_gate", "QA")
