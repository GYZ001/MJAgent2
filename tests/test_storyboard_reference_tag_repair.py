"""@ 引用与紧随镜头描述连写时的确定性修补。

真实故障见 app.production.storyboard_reference_tag_repair 模块 docstring：
2026-09-24《我欲封天》EP3 分镜第二步，模型把 @孟浩 与后面的镜头描述连写成
「@孟浩肩后看向对面的」，final_identity_prompt_errors 把整串当成不存在的名字
拒绝，语义重试耗尽、整集失败（StructuredSemanticError）。
"""
import pytest

from app.production.storyboard_identity_generation import generated_identity_errors
from app.production.storyboard_identity_validation import final_identity_prompt_errors, visible_reference_names
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_reference_tag_repair import repair_reference_tags, repair_segment_reference_tags
from app.production.storyboard_speech_render import render_segment_speech


def _character(name: str, *, visibility: str = "visible", subject_kind: str = "character") -> dict:
    return {"identity_id": f"bible:{name}", "display_name": name, "visibility": visibility, "subject_kind": subject_kind}


def _scene(name: str) -> dict:
    return {"scene_id": f"scene:{name}", "display_name": name}


def _segment(prompt_text: str, *, characters: tuple = (), scenes: tuple = (), dialogue: list | None = None) -> dict:
    return {
        "prompt_text": prompt_text,
        "dialogue": dialogue or [],
        "resources": {"characters": list(characters), "scenes": list(scenes)},
    }


def test_glued_character_reference_is_split_and_then_passes_validation():
    """红：修补前 final_identity_prompt_errors 直接拒绝原文；绿：修补后通过。"""
    segment = _segment("镜头3：@孟浩肩后看向对面的，说话。", characters=[_character("孟浩")])
    before = final_identity_prompt_errors(segment)
    assert any("图片引用 @孟浩肩后看向对面的" in e for e in before)

    fixed = repair_segment_reference_tags(segment)

    assert fixed == ["孟浩肩后看向对面的"]
    assert segment["prompt_text"] == "镜头3：@孟浩 肩后看向对面的，说话。"
    assert final_identity_prompt_errors(segment) == []


def test_longest_legal_name_prefix_wins_over_a_shorter_one():
    """「孟浩」「孟浩然」同在名单时，@孟浩然师弟... 必须按「孟浩然」拆，不能被短名字截断。"""
    segment = _segment("镜头1：@孟浩然师弟入场。", characters=[_character("孟浩"), _character("孟浩然")])

    fixed = repair_segment_reference_tags(segment)

    assert fixed == ["孟浩然师弟入场"]
    assert segment["prompt_text"] == "镜头1：@孟浩然 师弟入场。"
    assert final_identity_prompt_errors(segment) == []


def test_scene_name_prefix_is_also_repaired():
    segment = _segment("镜头1：@孟府书房内烛火摇曳。", scenes=[_scene("孟府书房")])

    fixed = repair_segment_reference_tags(segment)

    assert fixed == ["孟府书房内烛火摇曳"]
    assert segment["prompt_text"] == "镜头1：@孟府书房 内烛火摇曳。"
    assert final_identity_prompt_errors(segment) == []


def test_unknown_reference_without_any_legal_prefix_is_left_untouched_and_still_errors():
    segment = _segment("镜头1：@未登记人物走入画面。", characters=[_character("孟浩")])
    original = segment["prompt_text"]

    fixed = repair_segment_reference_tags(segment)

    assert fixed == []
    assert segment["prompt_text"] == original
    assert any("图片引用 @未登记人物走入画面" in e for e in final_identity_prompt_errors(segment))


def test_already_separated_reference_is_unchanged():
    segment = _segment("镜头1：@孟浩 转身离去。", characters=[_character("孟浩")])
    original = segment["prompt_text"]

    fixed = repair_segment_reference_tags(segment)

    assert fixed == []
    assert segment["prompt_text"] == original


def test_repair_reference_tags_does_not_mutate_its_string_input():
    prompt = "@孟浩肩后看向对面的。"
    names = {"孟浩"}

    repaired, fixed = repair_reference_tags(prompt, names)

    assert prompt == "@孟浩肩后看向对面的。"
    assert repaired == "@孟浩 肩后看向对面的。"
    assert fixed == ["孟浩肩后看向对面的"]


def test_template_and_rendered_prompt_text_are_repaired_identically():
    """speech_template 是渲染前的模板源头；render_segment_speech 只替换
    {{speech:Uxx}}，@ 引用原样透传到 prompt_text——两个字段修补后必须保持一致。"""
    template = "镜头1：@孟浩肩后看向对面的，说{{speech:U01}}。"
    segment = _segment(
        template,
        characters=[_character("孟浩")],
        dialogue=[{"utterance_id": "U01", "speaker_identity_id": "bible:孟浩", "line": "回来吧。", "delivery_kind": "spoken_dialogue"}],
    )
    render_segment_speech(segment, dialect="seedance")
    assert "@孟浩肩后看向对面的" in segment["speech_template"]
    assert "@孟浩肩后看向对面的" in segment["prompt_text"]

    fixed = repair_segment_reference_tags(segment)

    assert fixed == ["孟浩肩后看向对面的"]
    assert "@孟浩 肩后看向对面的" in segment["speech_template"]
    assert "@孟浩 肩后看向对面的" in segment["prompt_text"]
    assert final_identity_prompt_errors(segment) == []


def _generation_draft_and_payload(dialect: str) -> tuple[_AiStoryboardSegmentDraft, dict]:
    prompt = (
        "镜头1：@孟浩肩后看向对面的。{{speech:U01}} 镜头2：山路。"
        "integrated_multimodal_description: x overall_soundscape: y non_diegetic_music: z"
    )
    value = dict(
        prompt_text=prompt, shot_count=2,
        dialogue=[dict(utterance_id="U01", speaker_identity_id="bible:孟浩", line="回来吧。",
                       source_segment_index=1, delivery_kind="spoken_dialogue")],
        resources={"characters": [_character("孟浩")]},
    )
    payload = {"asset_manifest": {"characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩"}]}}
    return _AiStoryboardSegmentDraft.model_validate(value), payload


@pytest.mark.parametrize("dialect", ["seedance", "minimax_h3_native_fields", ""])
def test_generation_path_repair_is_independent_of_render_dialect(dialect):
    """忠实/短剧两档最终都经 storyboard_pack 里唯一的逐段生成调用点，二者共用
    generated_identity_errors；这里用它们共用的 dialect 形参证明修补不分方言/档位分支。"""
    draft, payload = _generation_draft_and_payload(dialect)

    errors = generated_identity_errors(draft, payload=payload, source_indexes=[1], required_dialogue=[], dialect=dialect)

    assert errors == []
    assert "@孟浩 肩后看向对面的" in draft.prompt_text


def test_visible_reference_names_excludes_voice_only_and_non_character_subjects():
    segment = _segment(
        "",
        characters=[
            _character("孟浩"),
            _character("旁白角色", visibility="voice_only"),
            _character("群演甲", subject_kind="extra"),
        ],
    )
    assert visible_reference_names(segment) == {"孟浩"}
