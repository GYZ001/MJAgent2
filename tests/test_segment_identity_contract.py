"""身份合同回归：完整第五章、真实消费者和不可变提交边界。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.production.storyboard_dialogue_attribution import attribute_prose_speaker, dialogue_speaker_errors
from app.production.storyboard_dialogue_extract import extract_dialogue_targets
from app.production.storyboard_dialogue_ledger import _AiKeptLine, required_dialogue_for_segments
from app.production.storyboard_identity_contract import canonical_segment_identities, identity_contract_errors, identity_contract_fingerprint
from app.production.storyboard_identity_scope import bind_quote_identities, scoped_name_map
from app.production.storyboard_identity_submission import segment_submission_errors
from app.production.storyboard_pack import _manifest_speaker_names
from app.production.storyboard_pack_identity import resolve_persisted_character_ids
from app.production.storyboard_speech_render import render_segment_speech, speech_template_errors
from app.source_excerpt import SourceSegment
from app.video_modes.seedance_reference_notes import _replace_at_mentions_with_picture_numbers


def _line(speaker="bible:孟浩", kind="inner_monologue"):
    return dict(utterance_id="U01", speaker_identity_id=speaker, line="我一定会回来。", source_segment_index=1,
                delivery="spoken_dialogue" if kind == "spoken_dialogue" else "offscreen_voice", delivery_kind=kind)


def _segment():
    return dict(prompt_text="镜头1：人群等待。{{speech:U01}}", dialogue=[_line()],
                resources={"characters":[{"identity_id":"bible:孟浩","display_name":"孟浩","visibility":"voice_only","subject_kind":"character"}],"scenes":[],"props":[]},
                required_dialogue=[{"quote_id":"Q01","speaker":"孟浩","speaker_identity_id":"bible:孟浩","text":"我一定会回来。","source_segment_index":1}],
                source_segment_indexes=[1], degraded_capabilities=[])


def test_complete_ep5_carries_listener_across_source_splits_and_quote_capacity():
    text = (Path(__file__).parent / "fixtures/yyft_ep5_identity_source.txt").read_text()
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    source = [SourceSegment(str(i), p, 0, len(p)) for i, p in enumerate(paragraphs)]
    payload = {"asset_manifest":{"characters":[{"identity_id":"bible:孟浩","display_name":"孟浩","aliases":[]}],"functional_extras":[]}}
    quotes = extract_dialogue_targets(source, set(), speaker_names=["孟浩"])
    bind_quote_identities(quotes, payload)
    quote = next(q for q in quotes if q.text.startswith("以王腾飞师兄的资质"))
    assert quote.excluded_speaker_identity_ids == ["bible:孟浩"]
    required = required_dialogue_for_segments([_AiKeptLine(quote_id=quote.quote_id,segment_no=1)],quotes)[1]
    draft = NS(dialogue=[NS(speaker_identity_id="bible:孟浩",line=quote.text,delivery="spoken_dialogue")],prompt_text=quote.text)
    # 阶段二已裁掉后面的听者句，来源合同仍须阻止错绑。
    errors = dialogue_speaker_errors(draft,required,{"孟浩":"bible:孟浩"},quote.text)
    assert any("听者" in e for e in errors)


def test_reaction_does_not_override_explicit_speaker():
    source = '张三说：“走吧。”李四点了点头。'
    start = source.index('走吧')
    assert attribute_prose_speaker(source,start,start+3,["张三","李四"]) == "张三"


def test_narrator_cannot_replace_a_named_source_speaker():
    line = NS(**_line("旁白","narration"))
    errors = dialogue_speaker_errors(NS(dialogue=[line],prompt_text=""),_segment()["required_dialogue"],{"孟浩":"bible:孟浩"},'孟浩说：“我一定会回来。”')
    assert errors


def test_unquoted_os_keeps_speaker_and_note_in_ledger():
    text = '孟浩（OS）：我一定会回来。\n同门甲：等等我。\n旁白：广场安静下来。'
    payload = {"asset_manifest":{"characters":[{"identity_id":"bible:孟浩","display_name":"孟浩"}],"functional_extras":[{"visual_entity_id":"entity:extra","label":"同门甲","segment_indexes":[1]}]}}
    quotes = extract_dialogue_targets([SourceSegment("s",text,0,len(text))],set(),speaker_names=_manifest_speaker_names(payload))
    bind_quote_identities(quotes,payload)
    assert [q.speaker for q in quotes] == ["孟浩","同门甲","旁白"]
    required = required_dialogue_for_segments([_AiKeptLine(quote_id=q.quote_id,segment_no=1) for q in quotes],quotes)[1]
    assert required[0]["delivery_kind"] == "inner_monologue"
    assert required[0]["source_start"] == text.index("我一定")
    draft = NS(dialogue=[NS(**_line())],prompt_text="画外音（孟浩）：“我一定会回来。”")
    assert dialogue_speaker_errors(draft,required,{"孟浩":"bible:孟浩"},text) == []
    assert draft.dialogue[0].speaker_identity_id == "bible:孟浩"


def test_aliases_are_scoped_and_do_not_override_a_registered_extra():
    payload = {"asset_manifest":{"characters":[{"identity_id":"bible:甲老","display_name":"甲老","aliases":["老者"],"segment_indexes":[1]}, {"identity_id":"bible:乙老","display_name":"乙老","aliases":["老者"],"segment_indexes":[2]}],"functional_extras":[{"label":"老者","visual_entity_id":"entity:extra","segment_indexes":[3]}]},"appellation_map":[{"raw_mention":"老者","identity_id":"bible:甲老","segment_index":1},{"raw_mention":"老者","identity_id":"bible:乙老","segment_index":2}]}
    assert "老者" not in scoped_name_map(payload)
    for index, expected in [(1,"bible:甲老"),(2,"bible:乙老"),(3,"entity:extra")]:
        assert resolve_persisted_character_ids(payload,["老者"],segment_source_indexes=[index]) == ([expected],[])
    result, notes = resolve_persisted_character_ids(payload,["老者"],segment_source_indexes=[1,2])
    assert result == ["老者"] and notes


def test_canonical_identity_is_shared_by_resources_and_dialogue():
    source = _segment()
    source["dialogue"][0]["speaker_identity_id"] = "少年"
    source["resources"]["characters"][0]["identity_id"] = "少年"
    payload = {"asset_manifest":{"characters":[{"identity_id":"bible:孟浩","display_name":"孟浩"}]},"appellation_map":[{"raw_mention":"少年","identity_id":"bible:孟浩","segment_index":1}]}
    result = canonical_segment_identities(source,payload)
    assert result["dialogue"][0]["speaker_identity_id"] == result["resources"]["characters"][0]["identity_id"] == "bible:孟浩"
    assert source["dialogue"][0]["speaker_identity_id"] == "少年"


@pytest.mark.parametrize("dialect",["seedance", "minimax_h3_native_fields"])
def test_speech_label_is_rendered_once_and_contract_changes_are_detected(dialect):
    segment = _segment()
    assert speech_template_errors(segment,require_tokens=True) == []
    render_segment_speech(segment,dialect=dialect)
    assert segment["prompt_text"].count("我一定会回来。") == 1
    assert segment_submission_errors(segment,source_text="孟浩（OS）：我一定会回来。") == []
    segment["dialogue"][0]["speaker_identity_id"] = "旁白"
    assert segment_submission_errors(segment,source_text="孟浩（OS）：我一定会回来。")


def test_legacy_mismatched_prompt_is_rejected_without_mutating_data():
    segment = _segment()
    segment["dialogue"][0] = _line("旁白","narration")
    segment["prompt_text"] = "画外音（孟浩）：“我一定会回来。”"
    before = deepcopy(segment)
    assert segment_submission_errors(segment,source_text="我一定会回来。")
    assert before == segment


def test_no_borrowed_face_for_explicit_extras_or_voice_only_subjects(monkeypatch):
    import app.multiview as multiview
    calls = []
    monkeypatch.setattr(multiview,"current_portrait_ref",lambda *args,**kwargs: calls.append(args) or {"portrait_id":"p","image_path":"/synthetic/face.jpg"})
    segment = _segment()
    segment["resources"]["characters"].append({"identity_id":"entity:extra","display_name":"老者","subject_kind":"extra","visibility":"visible"})
    bible = NS(characters=[NS(name="孟浩",aliases=[]),NS(name="老者",aliases=[])],scenes=[])
    manifest = multiview._storyboard_pack_asset_dependencies(project_id="p",episode_no=5,shot_id="s",segment=segment,conn=None,bible=bible)
    assert calls == []
    assert len(manifest["characters"]) == 1
    assert manifest["characters"][0]["selected_view_ids"] == []


def test_known_character_can_be_visible_during_inner_monologue(monkeypatch):
    import app.multiview as multiview
    monkeypatch.setattr(multiview,"current_portrait_ref",lambda *a,**kw:{"portrait_id":"p","image_path":"/synthetic/meng.jpg"})
    segment = _segment()
    segment["resources"]["characters"][0]["visibility"] = "visible"
    bible = NS(characters=[NS(name="孟浩",aliases=[])],scenes=[])
    manifest = multiview._storyboard_pack_asset_dependencies(project_id="p",episode_no=5,shot_id="s",segment=segment,conn=None,bible=bible)
    assert manifest["characters"][0]["selected_view_ids"] == ["p"]


def test_exact_image_subject_token_does_not_match_a_name_prefix():
    assert _replace_at_mentions_with_picture_numbers("@孟浩同门 看向 @孟浩。",{"孟浩":1}) == "@孟浩同门 看向 @图片1。"


def test_identity_revision_changes_when_only_visibility_changes():
    segment = _segment()
    before = identity_contract_fingerprint(segment)
    segment["resources"]["characters"][0]["visibility"] = "visible"
    assert identity_contract_fingerprint(segment) != before


def test_confirmed_extra_cannot_claim_formal_card():
    segment = _segment()
    segment["resources"]["characters"][0]["subject_kind"] = "extra"
    assert any("独立群演" in e for e in identity_contract_errors(segment))
