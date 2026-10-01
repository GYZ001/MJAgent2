"""闪回人物（``resources.flashback_figures``）——分镜台 2.x 段落契约（2026-09-29）。

真实缺陷（proj_ca86b15ab7d7 EP1 段16）：温念回忆童年，闪回镜头是「小时候的顾屿，
一个六岁左右的男孩……趴在床沿数数」，模型却把这个六岁男孩登记进
``resources.characters`` 并绑定成年顾屿的定妆照（``portrait_id=portrait_9c434ec58162``）。
后果：① 生成视频时会把成年顾屿的参考图发给这一镜（见 app.multiview.
``_storyboard_pack_asset_dependencies`` 只读 ``resources.characters``）；②
``storyboard_cast_lock`` 追加的人数锁定句写「画面中只有@温念、@顾屿共2人」，与正文
自己写的「不出现成年的顾屿」互相矛盾。

四层覆盖，对应本次改造的四个模块：
1. schema 默认值/旧行兼容（``_AiSegmentResources``/``FlashbackFigure``）。
2. ``storyboard_cast_lock``：人数锁定句在有/无闪回人物时的文本，及可见角色
   「除锁定句外正文从未点名」的 advisory（含「@顾屿家客房」这类场景提及不得
   被误算成对「顾屿」的点名）。
3. ``app.domain.storyboard_ops.identity_workspace``：身份工作台预览路径接受
   带 flashback_figures 的段落，不因新键而报错。
4. 接线守卫：新规则文本真的进了 ``_generate_all_segment_prompts`` 发给模型的
   task_payload（不是写了函数却没接线）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import db
from app.domain.storyboard_ops import identity_workspace as workspace
from app.production import storyboard_cast_lock as cast_lock
from app.production.storyboard_identity_generation import IDENTITY_GENERATION_RULES
from app.production.storyboard_identity_validation import identity_schema_errors
from app.production.storyboard_pack import (
    FlashbackFigure,
    _AiBeat,
    _AiBeatSheetDraft,
    _AiCameraDigest,
    _AiSegmentPlan,
    _AiSegmentResources,
    _AiStoryboardSegmentDraft,
    _generate_all_segment_prompts,
)
from app.production.storyboard_segment_output import segment_output_contract
from app.production.storyboard_speech_render import render_segment_speech
from app.source_excerpt import SourceSegment

# ---------------------------------------------------------------------------
# 1. schema 默认值 / 旧行兼容
# ---------------------------------------------------------------------------


def test_flashback_figures_defaults_to_empty_list():
    assert _AiSegmentResources().flashback_figures == []


def test_flashback_figures_parses_from_payload():
    resources = _AiSegmentResources.model_validate({
        "characters": [], "scenes": [], "props": [],
        "flashback_figures": [{"label": "六岁的顾屿", "description": "圆脸，白色短袖睡衣"}],
    })
    assert resources.flashback_figures[0].label == "六岁的顾屿"
    assert resources.flashback_figures[0].description == "圆脸，白色短袖睡衣"


def test_flashback_figures_missing_key_still_validates():
    """旧持久化行没有这个键，必须仍能解析——不是新故障点。"""
    old_dict = {"characters": [{"identity_id": "bible:孟浩", "description": "x"}], "scenes": [], "props": []}
    resources = _AiSegmentResources.model_validate(old_dict)
    assert resources.flashback_figures == []


def test_flashback_figure_requires_label_description_optional():
    figure = FlashbackFigure(label="六岁的顾屿")
    assert figure.description == ""
    with pytest.raises(ValidationError):
        FlashbackFigure()


def test_identity_schema_errors_accepts_flashback_figures_and_rejects_malformed():
    segment = {"resources": {"characters": [], "flashback_figures": [{"label": "六岁的顾屿"}]}, "dialogue": []}
    assert identity_schema_errors(segment) == []
    broken = {"resources": {"characters": [], "flashback_figures": [{"description": "没有称呼"}]}, "dialogue": []}
    assert identity_schema_errors(broken) != []


# ---------------------------------------------------------------------------
# 2. storyboard_cast_lock：人数锁定句 + 未点名 advisory
# ---------------------------------------------------------------------------


def _character(identity_id: str, display_name: str, *, visible: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        identity_id=identity_id, display_name=display_name,
        visibility="visible" if visible else "voice_only",
    )


def _figure(label: str) -> SimpleNamespace:
    return SimpleNamespace(label=label)


def _draft(prompt_text: str, characters: list[SimpleNamespace], flashback_figures: list[SimpleNamespace] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_text=prompt_text,
        resources=SimpleNamespace(characters=characters, flashback_figures=list(flashback_figures)),
    )


def test_no_flashback_figures_still_names_the_full_segment_cast():
    """2026-10-01 名单语义改写：旧句「画面中只有……共N人」被视频模型逐帧误读成
    「每个镜头都要有N人」（见 app.production.storyboard_cast_lock 模块 docstring
    2026-10-01 条）；无闪回人物时改写成「本段画面出场人物共N人：……」，「共N人」
    明确是全段名单人数，不是单镜人数。"""
    draft = _draft("镜头1：温念坐在桌边，顾屿走近。", [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")])
    assert cast_lock.ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text.endswith(
        "本段画面出场人物共2人：@温念、@顾屿；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )


def test_flashback_figures_split_lock_sentence_into_real_and_flashback_groups():
    draft = _draft(
        "镜头1：温念望着窗外出神。镜头2：闪回，六岁的顾屿趴在床沿数数。",
        [_character("bible:温念", "温念")],
        [_figure("六岁的顾屿")],
    )
    assert cast_lock.ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text.endswith(
        "本段现实画面出场人物共1人：@温念；闪回画面出场人物：六岁的顾屿；"
        "每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )
    assert "@顾屿" not in draft.prompt_text, "闪回人物没有当前定妆照，不能绑 @"


def test_flashback_only_segment_omits_empty_real_cast_clause():
    """整段都是闪回、没有现实同框角色时，不写「现实画面出场人物共0人」这种空话。"""
    draft = _draft("镜头1：闪回，六岁的顾屿趴在床沿数数。", [], [_figure("六岁的顾屿")])
    assert cast_lock.ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text.endswith(
        "本段闪回画面出场人物：六岁的顾屿；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )
    assert "现实画面出场人物" not in draft.prompt_text


def test_switching_from_plain_to_flashback_format_replaces_old_line_not_duplicates():
    """幂等剥离必须认识旧格式（无「现实/闪回」前缀、旧收尾「画面中只有」）与新复合
    格式两种写法。"""
    draft = _draft(
        "镜头1：温念望着窗外出神。\n画面中只有@温念共1人，不出现其他人物或路人。",
        [_character("bible:温念", "温念")],
        [_figure("六岁的顾屿")],
    )
    cast_lock.ensure_cast_lock_in_prompt(draft)
    # 复合句本身含两个「出场人物」（现实/闪回各一个）是预期形状，不是重复追加；
    # 真正要守住的是旧格式的那句没有被原样保留（未被剥离）。
    assert draft.prompt_text.count("现实画面出场人物") == 1
    assert draft.prompt_text.count("闪回画面出场人物") == 1
    assert "画面中只有" not in draft.prompt_text
    assert draft.prompt_text.endswith(
        "本段现实画面出场人物共1人：@温念；闪回画面出场人物：六岁的顾屿；"
        "每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )


def _advisory_draft(prompt_text: str, characters: list[SimpleNamespace]) -> SimpleNamespace:
    draft = _draft(prompt_text, characters)
    cast_lock.ensure_cast_lock_in_prompt(draft)  # 追加真实的人数锁定句，advisory 要能看穿它
    return draft


def test_unmentioned_visible_character_gets_advisory_even_with_cast_lock_line():
    """真实缺陷复现：正文只写了「六岁男孩」，@顾屿 只出现在人数锁定句里。"""
    draft = _advisory_draft(
        "镜头1：闪回，一个六岁左右的男孩趴在床沿数数。",
        [_character("bible:顾屿", "顾屿")],
    )
    assert "@顾屿" in draft.prompt_text, "锁定句必须真的写了 @顾屿（否则这条测试没有意义）"
    advisories = cast_lock.unmentioned_visible_character_advisories(draft)
    assert len(advisories) == 1
    assert "STORYBOARD_PACK_RESOURCE_CHARACTER_UNMENTIONED" in advisories[0]
    assert "[未拦截]" in advisories[0]
    assert "顾屿" in advisories[0]


def test_scene_mention_sharing_a_name_prefix_does_not_count_as_character_mention():
    """「@顾屿家客房」是场景提及，最长匹配整体取「顾屿家客房」一个词，不等于「顾屿」，
    不能被误判成已经点过名——advisory 必须照常触发。"""
    draft = _advisory_draft(
        "镜头1：@顾屿家客房 的窗帘被风吹动，一个六岁左右的男孩趴在床沿数数。",
        [_character("bible:顾屿", "顾屿")],
    )
    advisories = cast_lock.unmentioned_visible_character_advisories(draft)
    assert any("顾屿" in a for a in advisories)


def test_actual_body_mention_suppresses_advisory():
    draft = _advisory_draft(
        "镜头1：@顾屿 快步走进书房，神色凝重。",
        [_character("bible:顾屿", "顾屿")],
    )
    assert cast_lock.unmentioned_visible_character_advisories(draft) == []


def test_offscreen_voice_style_mention_also_suppresses_advisory():
    """「画外音（姓名）」是既有合法写法（见 storyboard_dialects.reference_mention_errors），
    与 @ 点名同等有效，不重复报告。"""
    draft = _advisory_draft(
        "镜头1：空镜，窗帘被风吹动。画外音（顾屿）：还没到时候。",
        [_character("bible:顾屿", "顾屿")],
    )
    assert cast_lock.unmentioned_visible_character_advisories(draft) == []


def test_voice_only_character_never_triggers_advisory():
    draft = _draft("镜头1：空镜，风声。", [_character("bible:顾屿", "顾屿", visible=False)])
    assert cast_lock.unmentioned_visible_character_advisories(draft) == []


# ---------------------------------------------------------------------------
# 3. 身份工作台预览路径接受带 flashback_figures 的段落
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_fixture():
    conn = db.get_conn()
    payload = {"prep_pack_version": "2.0.0", "asset_manifest": {"characters": [{"identity_id": "bible:顾屿", "display_name": "顾屿", "segment_indexes": [1]}], "scenes": [], "functional_extras": []}}
    conn.execute("INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','本地回归','{}',?)", (db.now(),))
    conn.execute("INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,'第一章',?)", ('温念想起小时候的顾屿。\n\n窗帘被风吹动。',))
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,screenplay_json,created_at) VALUES('ep','p',1,'第一集','[1]','confirmed',?,?)", (json.dumps(payload), db.now()))
    segment = dict(
        segment_no=1, synopsis="温念回忆童年的顾屿", source_segment_indexes=[1], beat_ids=["B1"],
        beats=[{"beat_id": "B1", "summary": "闪回", "segment_indexes": [1]}], shot_count=2, duration_s=15,
        target_model="seedance_2", degraded_capabilities=[],
        prompt_text="镜头1：闪回，六岁的顾屿趴在床沿数数。\n闪回画面中只有六岁的顾屿，不出现其他人物或路人。",
        dialogue=[],
        resources={
            "characters": [], "scenes": [], "props": [],
            "flashback_figures": [{"label": "六岁的顾屿", "description": "圆脸，白色短袖睡衣"}],
        },
    )
    render_segment_speech(segment, dialect="seedance")
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) "
        "VALUES('s1','ep',1,15,'','','','','','[]','[]',?,?,NULL)",
        ('温念想起小时候的顾屿。', json.dumps({"storyboard_pack_segment": segment})),
    )
    conn.commit()
    return conn, segment


def test_review_identity_workspace_round_trips_flashback_figures(workspace_fixture):
    conn, _segment = workspace_fixture
    review = workspace.review_identity_workspace(conn, "s1")
    assert review["segment"]["resources"]["flashback_figures"] == [
        {"label": "六岁的顾屿", "description": "圆脸，白色短袖睡衣"},
    ]


def test_prepare_identity_candidate_accepts_segment_with_flashback_figures(workspace_fixture):
    """身份工作台的候选校验/重渲染全流程（schema 校验 -> 身份合同 -> 台词展开 ->
    提交前复核）不因 resources.flashback_figures 这个新键报错，且原样带过。"""
    conn, segment = workspace_fixture
    from copy import deepcopy
    candidate = deepcopy(segment)
    prepared = workspace.prepare_identity_candidate(conn, shot_id="s1", candidate=candidate)
    assert prepared["resources"]["flashback_figures"] == [
        {"label": "六岁的顾屿", "description": "圆脸，白色短袖睡衣"},
    ]


# ---------------------------------------------------------------------------
# 4. 接线守卫：规则文本真的进了 task_payload
# ---------------------------------------------------------------------------


def test_output_contract_mentions_flashback_figures():
    contract = segment_output_contract([1], min_shots=2, max_shots=4)
    assert "flashback_figures" in contract["resources"]


def test_identity_generation_rules_include_flashback_positive_statement():
    joined = "\n".join(IDENTITY_GENERATION_RULES)
    assert "flashback_figures" in joined
    assert "resources.characters" in joined
    assert "@" in joined


def _single_segment_beat_draft() -> _AiBeatSheetDraft:
    return _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="温念回忆童年的顾屿", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="段1", source_segment_indexes=[1], beat_ids=["B1"])],
    )


def _single_source_segment() -> list[SourceSegment]:
    return [SourceSegment(segment_id="s1", text="温念想起小时候的顾屿。", start_offset=0, end_offset=11)]


@pytest.mark.asyncio
async def test_flashback_rule_and_output_contract_reach_segment_task_payload(monkeypatch):
    """真的跑一遍 ``_generate_all_segment_prompts``（stub ``chat_structured``），
    核对发给模型的 task_payload 里 rules[] 与 output_contract.resources 都带上了
    闪回人物的规则文本——不是定义了规则却没接进阶段二的调用。"""
    import app.production.storyboard_pack as storyboard_pack_module

    captured: dict = {}

    async def fake_chat_structured(messages, **kwargs):
        captured["payload"] = json.loads(messages[1]["content"])
        return _AiStoryboardSegmentDraft(
            prompt_text="镜头1：闪回，六岁的顾屿趴在床沿数数。", shot_count=2, camera_digest=_AiCameraDigest(),
        )

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    await _generate_all_segment_prompts(
        episode_id="ep-flashback", episode_no=1, beat_draft=_single_segment_beat_draft(),
        segments=_single_source_segment(), payload={}, target_video_model="hiagent",
        bible=None, conn=None, project_id="", aspect_ratio="9:16", enhance_music_bed=False,
        required_dialogue_by_segment_no={},
    )

    payload = captured["payload"]
    assert any("flashback_figures" in rule for rule in payload["rules"])
    assert "flashback_figures" in payload["output_contract"]["resources"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
