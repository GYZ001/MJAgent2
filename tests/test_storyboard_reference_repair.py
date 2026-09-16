"""群演上的 @ 机械去掉（2026-09-14 第 12 集第 11 段：模型三次写「@那修士的对手」，校验拒绝、整集失败）。
只处理本段已知群演；真正写错的人物引用仍留给 final_identity_prompt_errors 拦截。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_dialects import reference_mention_errors
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


# 2026-09-16 龙猫出爪连播第 4、5、6 集整集失败的真实数据：这里剥掉的 @ 正是
# reference_mention_errors 下一步要求必须在的那一个，模型重试时照写 @龙猫 也
# 每次都被改掉，三次预算烧完整集失败。两种形态各一条，都以「修补后校验不再
# 报错」收尾——死锁是否解除由那一步断言，不只看字符串。
def _carded_draft(prompt: str, name: str) -> SimpleNamespace:
    characters = [SimpleNamespace(identity_id=f"bible:{name}", portrait_id="pt_x", display_name=name,
                                  visibility="visible", subject_kind="character")]
    return SimpleNamespace(prompt_text=prompt, resources=SimpleNamespace(characters=characters, scenes=[]))


def test_card_backed_character_keeps_its_marker_when_also_registered_as_extra() -> None:
    """第 4/5 集：bible:龙猫 有参考图，却又被登记成 functional_extras 的「龙猫」。"""
    prompt = "镜头2：明黄色短毛家猫@龙猫 从屏幕里跳出来，端正坐下。"
    draft = _carded_draft(prompt, "龙猫")
    payload = {"asset_manifest": {"functional_extras": [{"label": "龙猫"}, {"label": "小龙"}]}}
    strip_extra_reference_markers(draft, payload)
    assert draft.prompt_text == prompt
    assert reference_mention_errors(draft.prompt_text, draft.resources) == []


def test_extra_label_that_is_a_prefix_of_a_character_name_does_not_break_it() -> None:
    """第 6 集：群演 label「王婶」是角色名「王婶的老狗」的前缀，裸 replace 会拦腰斩断。"""
    prompt = "镜头1：@王婶的老狗 趴在门口，尾巴扫过地面。"
    draft = _carded_draft(prompt, "王婶的老狗")
    payload = {"asset_manifest": {"functional_extras": [{"label": "王婶"}]}}
    strip_extra_reference_markers(draft, payload)
    assert draft.prompt_text == prompt
    assert reference_mention_errors(draft.prompt_text, draft.resources) == []


def test_alias_of_a_card_backed_character_is_protected_via_manifest() -> None:
    """角色卡的别名也受保护：模型按原文称谓写 @小龙，不该被同名群演标签剥掉。"""
    draft = _carded_draft("镜头3：@小龙 抬爪轻点键盘边缘。", "龙猫")
    payload = {"asset_manifest": {
        "characters": [{"identity_id": "bible:龙猫", "portrait_id": "pt_x", "aliases": ["小龙"]}],
        "functional_extras": [{"label": "小龙"}],
    }}
    strip_extra_reference_markers(draft, payload)
    assert draft.prompt_text == "镜头3：@小龙 抬爪轻点键盘边缘。"


def test_extra_label_followed_by_more_characters_is_left_alone() -> None:
    """词边界：@他们的领队 以群演标签开头但后面还连着字，不剥离，交给校验如实报错。"""
    draft = SimpleNamespace(prompt_text="@他们的领队 举起手。",
                            resources=SimpleNamespace(characters=[], scenes=[]))
    strip_extra_reference_markers(draft, {"asset_manifest": {"functional_extras": [{"label": "他们"}]}})
    assert draft.prompt_text == "@他们的领队 举起手。"
