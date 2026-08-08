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
    "source_excerpt": "少女轻声唤他"萧炎哥哥。"随后弯腰",
    "dialogues": [{"speaker": "萧薰儿", "line": "萧炎哥哥。"}]
  }
}'''

    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(text)


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


def test_extract_json_does_not_guess_multiple_missing_closers() -> None:
    text = '{"episode_no": 9, "events": [{"event_id": "E1"'

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
