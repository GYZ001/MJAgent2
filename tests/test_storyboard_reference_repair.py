"""群演上的 @ 机械去掉（2026-09-14 第 12 集第 11 段：模型三次写「@那修士的对手」，校验拒绝、整集失败）。
只处理本段已知群演；真正写错的人物引用仍留给 final_identity_prompt_errors 拦截。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_identity_validation import final_identity_prompt_errors
from app.production.storyboard_reference_repair import strip_extra_reference_markers

PROMPT = (
    "镜头1：@孟浩 站在平顶山公开区，脚步移动绕到@那修士的对手 身旁。\n"
    "镜头2：@孟浩 歪头看向身前的@那修士的对手（青年男性修士），说出台词{{speech:U01}}。"
)


def _draft(prompt: str = PROMPT, **character_kwargs) -> SimpleNamespace:
    characters = [
        SimpleNamespace(identity_id="bible:孟浩", portrait_id="pt_mh", display_name="孟浩",
                        visibility="visible", subject_kind="character"),
        SimpleNamespace(identity_id="entity:abc", portrait_id=None, display_name="那修士的对手",
                        visibility="visible", subject_kind=character_kwargs.get("subject_kind", "extra")),
    ]
    return SimpleNamespace(prompt_text=prompt, resources=SimpleNamespace(characters=characters, scenes=[]))


def test_extra_marker_is_stripped_everywhere_and_text_otherwise_untouched() -> None:
    draft = _draft()
    assert strip_extra_reference_markers(draft) == []
    assert draft.prompt_text == PROMPT.replace("@那修士的对手", "那修士的对手")
    assert draft.prompt_text.count("@孟浩") == 2


def test_manifest_extra_label_counts_even_when_draft_resources_omit_it() -> None:
    draft = SimpleNamespace(prompt_text="@打斗修士 与 @孟浩 对视。", resources=SimpleNamespace(characters=[], scenes=[]))
    payload = {"asset_manifest": {"functional_extras": [{"label": "打斗修士"}]}}
    strip_extra_reference_markers(draft, payload)
    assert draft.prompt_text == "打斗修士 与 @孟浩 对视。"


def test_unknown_reference_is_left_for_validation() -> None:
    draft = _draft("@孟浩 看向 @许清。")
    strip_extra_reference_markers(draft, {"asset_manifest": {"functional_extras": []}})
    assert draft.prompt_text == "@孟浩 看向 @许清。"


def test_repaired_prompt_passes_the_reference_validation() -> None:
    segment = {
        "prompt_text": PROMPT,
        "resources": {"characters": [
            {"identity_id": "bible:孟浩", "display_name": "孟浩", "visibility": "visible", "subject_kind": "character"},
            {"identity_id": "entity:abc", "display_name": "那修士的对手", "visibility": "visible", "subject_kind": "extra"},
        ], "scenes": []},
    }
    before = final_identity_prompt_errors(segment)
    assert any("@那修士的对手" in error for error in before)
    draft = _draft()
    strip_extra_reference_markers(draft)
    segment["prompt_text"] = draft.prompt_text
    assert not any("图片引用" in error for error in final_identity_prompt_errors(segment))
