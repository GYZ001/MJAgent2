"""2026-09-09 本地连播 1—10 集失败的真实响应回放，不调用供应商。"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.production.storyboard_dialogue_attribution import dialogue_speaker_errors
from app.production.storyboard_dialogue_repeat_repair import repair_preempted_dialogue
from app.production.storyboard_identity_contract import canonical_segment_identities, identity_contract_errors, registered_subject_errors
from app.production.storyboard_identity_generation import generated_identity_errors
from app.production.storyboard_identity_validation import quote_provenance_errors
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_repair_context import known_character_identities, storyboard_repair_context
from app.production.storyboard_speech_render import speech_template_errors

CASES = {item['episode_no']: item for item in json.loads(
    (Path(__file__).parent / 'fixtures/series_identity_failures_20260909.json').read_text()
)}


@pytest.mark.parametrize('episode_no', [2, 4, 6])
def test_optional_voice_removal_keeps_template_in_sync(episode_no):
    case = CASES[episode_no]
    context = case['context']
    draft = _AiStoryboardSegmentDraft.model_validate(case['draft'])
    before_ids = {line.utterance_id for line in draft.dialogue}
    assert speech_template_errors(draft.model_dump(), require_tokens=True) == []
    dialogue_speaker_errors(draft, context['required_dialogue'], {}, context['source_text_by_segment'])
    removed = before_ids - {line.utterance_id for line in draft.dialogue}
    assert removed
    assert speech_template_errors(draft.model_dump(), require_tokens=True) == []
    for identity in removed:
        assert '{{speech:' + identity + '}}' not in draft.prompt_text
    assert '镜头1' in draft.prompt_text


def test_reserved_dialogue_removal_also_removes_its_speech_token():
    draft = _AiStoryboardSegmentDraft.model_validate(CASES[1]['draft'])
    original = draft.prompt_text
    text = draft.dialogue[0].line
    assert repair_preempted_dialogue(draft, [(3, text)], current_segment_no=2)
    assert not draft.dialogue
    assert draft.prompt_text == original.replace('{{speech:U01}}', '')
    assert speech_template_errors(draft.model_dump(), require_tokens=True) == []


@pytest.mark.parametrize('episode_no', [1, 7, 8])
def test_quote_repair_receives_and_preserves_exact_source(episode_no):
    case = CASES[episode_no]
    context = case['context']
    draft = deepcopy(case['draft'])
    segment = dict(draft, required_dialogue=context['required_dialogue'])
    errors = quote_provenance_errors(segment)
    quote = context['required_dialogue'][0]
    assert errors and any(quote['text'] in error for error in errors)
    repair = json.loads(storyboard_repair_context(context))
    assert repair['required_dialogue'] == context['required_dialogue']
    assert repair['source_text_by_segment'] == context['source_text_by_segment']
    line = segment['dialogue'][0]
    line.update(line=quote['text'], source_segment_index=quote['source_segment_index'], source_quote_id=quote['quote_id'])
    assert quote_provenance_errors(segment) == []
    typed = _AiStoryboardSegmentDraft.model_validate(segment)
    names = {c['display_name']: c['identity_id'] for c in context['relevant_assets']['characters']}
    assert dialogue_speaker_errors(typed, context['required_dialogue'], names, context['source_text_by_segment']) == []
    # 去掉来源 ID 也不能借旧模糊匹配放行改写过的必保原话。
    line.update(source_quote_id='', line=case['draft']['dialogue'][0]['line'])
    assert quote_provenance_errors(segment)


def test_hidden_registered_characters_are_available_to_generation_and_repair():
    case = CASES[5]
    payload = case['identity_payload']
    catalog = known_character_identities(payload)
    visible_assets = {c['identity_id'] for c in case['context']['relevant_assets']['characters']}
    assert 'bible:许师姐' not in visible_assets
    assert {'bible:许师姐', 'bible:陈师兄'} <= {c['identity_id'] for c in catalog}
    context = dict(case['context'], known_character_identities=catalog)
    assert json.loads(storyboard_repair_context(context))['known_character_identities'] == catalog
    raw = dict(deepcopy(case['draft']), source_segment_indexes=context['source_segment_indexes'])
    normalized = canonical_segment_identities(raw, payload)
    assert registered_subject_errors(normalized, payload)
    for character in raw['resources']['characters']:
        if character['display_name'] in {'许师姐', '陈师兄'}:
            character['identity_id'] = 'bible:' + character['display_name']
            character['subject_kind'] = 'character'
    draft = _AiStoryboardSegmentDraft.model_validate(raw)
    errors = generated_identity_errors(draft, payload=payload, source_indexes=context['source_segment_indexes'],
                                       required_dialogue=context['required_dialogue'], dialect='seedance')
    assert errors and all('图片引用' in error for error in errors)
    draft.prompt_text = draft.prompt_text.replace('@上官修身着', '@上官修 身着')
    assert generated_identity_errors(draft, payload=payload, source_indexes=context['source_segment_indexes'],
                                     required_dialogue=context['required_dialogue'], dialect='seedance') == []
    # 真正的独立群演依然不能借同名正式角色卡。
    raw['resources']['characters'][1].update(identity_id='bible:许师姐', portrait_id='portrait_x')
    assert identity_contract_errors(raw, require_explicit=True)
