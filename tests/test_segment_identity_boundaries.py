"""生成边界的负例：不能通过规范化、空台词或悬空引用掩盖冲突。"""
from copy import deepcopy

import pytest

from app import config
from app.production.storyboard_identity_generation import generated_identity_errors
from app.production.storyboard_identity_submission import segment_submission_errors
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_speech_render import render_segment_speech
from app.production.storyboard_dialogue_attribution import attribute_prose_speaker


def draft_and_payload():
    value = dict(prompt_text="镜头1：@孟浩 等待。{{speech:U01}} 镜头2：山路。",shot_count=2,
                 dialogue=[dict(utterance_id="U01",speaker_identity_id="bible:孟浩",line="回来吧。",source_segment_index=1,delivery_kind="spoken_dialogue")],
                 resources={"characters":[dict(identity_id="bible:孟浩",display_name="孟浩",visibility="visible",subject_kind="character")]})
    return value, {"asset_manifest":{"characters":[{"identity_id":"bible:孟浩","display_name":"孟浩"}]},"appellation_map":[{"raw_mention":"同门","identity_id":"bible:孟浩","segment_index":1}]}


def generation_errors(value,payload):
    return generated_identity_errors(_AiStoryboardSegmentDraft.model_validate(value),payload=payload,source_indexes=[1],required_dialogue=[])


def test_normalization_must_not_hide_an_extra_borrowing_a_known_identity():
    value,payload = draft_and_payload()
    value["resources"]["characters"][0].update(identity_id="同门",subject_kind="extra")
    value["dialogue"][0]["speaker_identity_id"] = "同门"
    assert any("独立群演" in e for e in generation_errors(value,payload))


@pytest.mark.parametrize("mention",["@孟浩同门", "@未登记人物", "@bible:孟浩"])
def test_unknown_and_prefix_image_references_are_rejected(mention):
    value,payload = draft_and_payload()
    value["prompt_text"] = value["prompt_text"].replace("@孟浩",mention)
    assert any("图片引用" in e for e in generation_errors(value,payload))


def test_prompt_capacity_is_checked_after_utterance_expansion(monkeypatch):
    value,payload = draft_and_payload()
    monkeypatch.setattr(config,"PROMPT_CHAR_LIMIT",len(value["prompt_text"])+1)
    assert any("展开后提示词长度" in e for e in generation_errors(value,payload))


def test_deleting_all_dialogue_does_not_bypass_required_source_lines():
    value,_ = draft_and_payload()
    value["required_dialogue"] = [{"quote_id":"Q01","text":"回来吧。","source_segment_index":1}]
    value["dialogue"] = []
    value["prompt_text"] = "山路空寂。"
    assert segment_submission_errors(value,source_text='孟浩说：“回来吧。”')


def test_corrupted_template_is_an_actionable_error_not_a_key_error():
    value,_ = draft_and_payload()
    render_segment_speech(value,dialect="seedance")
    value["speech_template"] = "{{speech:missing}}"
    before = deepcopy(value)
    assert segment_submission_errors(value,source_text='孟浩说：“回来吧。”')
    assert value == before


def test_h3_keeps_native_audio_blocks_and_stable_speaker_number():
    value,_ = draft_and_payload()
    render_segment_speech(value,dialect="minimax_h3_native_fields")
    assert "(S1, 孟浩) says: <d>[Chinese] 回来吧。</d>" in value["prompt_text"]


def test_old_os_source_label_does_not_require_a_new_ledger_to_stay_correct():
    value,_ = draft_and_payload()
    value["dialogue"][0].update(delivery="offscreen_voice",delivery_kind="inner_monologue")
    render_segment_speech(value,dialect="seedance")
    assert segment_submission_errors(value,source_text="孟浩（OS）：回来吧。") == []


@pytest.mark.parametrize("following",['李四笑了。','李四说：“等等。”'])
def test_explicit_speaker_before_quote_precedes_following_action(following):
    text = '张三说：“走吧。”' + following
    start = text.index('走吧')
    assert attribute_prose_speaker(text,start,start+3,["张三","李四"]) == "张三"
