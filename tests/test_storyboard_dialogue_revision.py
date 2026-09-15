"""2.x 镜头台词人工修订：原句留档溯源、修订句进提示词，提交断言按原句核验来源、按修订句核验展开。"""
from copy import deepcopy

import pytest

from app.production import storyboard_identity_submission as submission
from app.production.storyboard_dialogue_revision import (
    has_revisions, revise_segment_dialogue, revision_errors, revisions_from_dialogues, source_faithful_copy,
)

SEGMENT = {
    "speech_dialect": "seedance_compact_director_brief",
    "speech_template": "镜头1：@听听 蹲在台边，开口发声{{speech:U01}}\n镜头2：@龙猫 答{{speech:U02}}",
    "prompt_text": "",
    "resources": {"characters": [
        {"identity_id": "bible:听听", "display_name": "听听"}, {"identity_id": "bible:龙猫", "display_name": "龙猫"},
    ]},
    "dialogue": [
        {"utterance_id": "U01", "speaker_identity_id": "bible:听听", "line": "那根越来越粗了。", "delivery": "spoken_dialogue", "delivery_kind": "spoken_dialogue"},
        {"utterance_id": "U02", "speaker_identity_id": "bible:龙猫", "line": "还不是时候。", "delivery": "spoken_dialogue", "delivery_kind": "spoken_dialogue"},
    ],
}


def _rendered(segment):
    from app.production.storyboard_speech_render import render_segment_speech
    return render_segment_speech(deepcopy(segment), dialect=segment["speech_dialect"])


def test_revision_keeps_original_for_provenance_and_rerenders_prompt() -> None:
    base = _rendered(SEGMENT)
    revised = revise_segment_dialogue(base, {"U01": "那根线越来越粗了。"}, reason="供应商合规拒收")
    line = revised["dialogue"][0]
    assert line["line"] == "那根线越来越粗了。" and line["revised_from"] == "那根越来越粗了。" and line["revision_reason"] == "供应商合规拒收"
    assert "那根线越来越粗了" in revised["prompt_text"] and "那根越来越粗了" not in revised["prompt_text"]
    assert revised["speech_template"] == base["speech_template"]  # 模板不动
    assert has_revisions(revised) and not has_revisions(base)
    # 再改一次仍指向最初的原句
    again = revise_segment_dialogue(revised, {"U01": "那根红线越来越粗了。"}, reason="再改")
    assert again["dialogue"][0]["revised_from"] == "那根越来越粗了。"
    faithful = source_faithful_copy(again)
    assert faithful["dialogue"][0]["line"] == "那根越来越粗了。" and "那根越来越粗了" in faithful["prompt_text"]
    assert revision_errors(again) == []


def test_revision_errors_catch_hand_edited_prompt_and_missing_template() -> None:
    revised = revise_segment_dialogue(_rendered(SEGMENT), {"U01": "那根线越来越粗了。"}, reason="r")
    tampered = dict(revised, prompt_text=revised["prompt_text"] + " 手改")
    assert revision_errors(tampered) and "模板" in revision_errors(tampered)[0]
    legacy = dict(revised, speech_template="")
    assert revision_errors(legacy) and "不支持台词修订" in revision_errors(legacy)[0]


def test_submission_checks_source_against_original_lines_then_revision(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(submission, "_source_checked_errors", lambda segment, *, source_text: seen.append(segment) or [])
    revised = revise_segment_dialogue(_rendered(SEGMENT), {"U01": "那根线越来越粗了。"}, reason="r")
    assert submission.segment_submission_errors(revised, source_text="听听：那根越来越粗了。") == []
    assert seen[0]["dialogue"][0]["line"] == "那根越来越粗了。"  # 溯源检查看到的是原句
    tampered = dict(revised, prompt_text="手改正文")
    assert submission.segment_submission_errors(tampered, source_text="x")


def test_revisions_from_dialogues_only_allows_line_changes() -> None:
    base = _rendered(SEGMENT)
    edited = [{"speaker": "听听", "line": "那根线越来越粗了。"}, {"speaker": "龙猫", "line": "还不是时候。"}]
    assert revisions_from_dialogues(base, edited) == {"U01": "那根线越来越粗了。"}
    with pytest.raises(ValueError, match="条数"):
        revisions_from_dialogues(base, edited[:1])
    with pytest.raises(ValueError, match="发声者"):
        revisions_from_dialogues(base, [{"speaker": "龙猫", "line": "x"}, edited[1]])
    with pytest.raises(ValueError, match="utterance_id"):
        revise_segment_dialogue(base, {"U99": "x"}, reason="r")
