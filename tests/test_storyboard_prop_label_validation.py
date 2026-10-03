"""``resources.props[].label`` 逐字核验（``app.production.storyboard_prop_label_
validation``，2026-10-02，《顾念长安》EP1 第 14/15 段真实故障：经「修订本段」重生成
后，label 被写成「浅蓝色碎花长裙（外套下摆露出的一截）」这类"卡名+括号说明"，参考图
解析按 label 逐字查卡查不到，第 15 段温念的浅蓝碎花长裙被画成了另一条裤子）。

覆盖三件事：① ``storyboard_prop_visibility`` 两份方言规则文本都新增了"label 逐字取
卡名/别名、可见范围写 description"的正面陈述；② 新校验函数本身的判据（报错/放行各
自的分支）；③ 校验真的接进了逐段生成（``storyboard_identity_generation.
generated_identity_errors``）与「修订本段」（``identity_workspace.
prepare_identity_candidate``）两条生产路径，不是写了没接线。
"""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app import db
from app.domain.storyboard_ops import identity_workspace as workspace
from app.production.storyboard_identity_contract import stamp_identity_contract
from app.production.storyboard_identity_generation import generated_identity_errors
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_prop_label_validation import prop_label_bracket_note_errors
from app.production.storyboard_prop_visibility import MINIMAX_H3_PROP_VISIBILITY_RULE, SEEDANCE_PROP_VISIBILITY_RULE
from app.production.storyboard_speech_render import render_segment_speech


# ---------------------------------------------------------------------------
# ① 规则文本新增正面陈述
# ---------------------------------------------------------------------------

def test_seedance_rule_requires_label_verbatim_from_known_card():
    assert "逐字使用" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "名称或其登记的别名" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "不得在 label 后面用括号补充" in SEEDANCE_PROP_VISIBILITY_RULE
    assert "写进 description" in SEEDANCE_PROP_VISIBILITY_RULE


def test_h3_rule_requires_label_verbatim_from_known_card():
    assert "registered aliases" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "never appended to label in parentheses" in MINIMAX_H3_PROP_VISIBILITY_RULE
    assert "belongs in description" in MINIMAX_H3_PROP_VISIBILITY_RULE


# ---------------------------------------------------------------------------
# ② 校验函数本身的判据
# ---------------------------------------------------------------------------

KNOWN_PROPS = [
    {"label": "浅蓝色碎花长裙", "canonical_name": "浅蓝色碎花长裙", "segment_indexes": [14, 15]},
    {"label": "旧行李箱", "canonical_name": "行李箱", "segment_indexes": [14]},
]
PAYLOAD = {"asset_manifest": {"props": KNOWN_PROPS}}


def errors_for(props, *, source_segment_indexes=(14, 15)):
    return prop_label_bracket_note_errors(props, payload=PAYLOAD, source_segment_indexes=list(source_segment_indexes))


def test_card_name_plus_bracket_note_is_rejected():
    props = [{"label": "浅蓝色碎花长裙（外套下摆露出的一截）", "description": ""}]
    errors = errors_for(props)
    assert len(errors) == 1
    assert "props[0].label" in errors[0]
    assert "浅蓝色碎花长裙（外套下摆露出的一截）" in errors[0]
    assert "应逐字写道具卡名「浅蓝色碎花长裙」" in errors[0]
    assert "写进 description" in errors[0]


def test_half_width_parenthesis_note_is_also_rejected():
    assert errors_for([{"label": "浅蓝色碎花长裙(外套下摆露出的一截)"}])


def test_exact_card_name_is_allowed():
    assert errors_for([{"label": "浅蓝色碎花长裙"}]) == []


def test_exact_registered_alias_is_allowed():
    """``label`` 字段本身就是这件道具这次提及时的原文写法（"旧行李箱"），功能上
    等同于一个已登记别名——与绑定的卡规范名「行李箱」不同字，仍然合法。"""
    assert errors_for([{"label": "旧行李箱"}]) == []
    assert errors_for([{"label": "行李箱"}]) == []


def test_generic_name_without_any_card_is_allowed():
    assert errors_for([{"label": "保温杯"}]) == []


def test_label_that_happens_to_equal_another_full_card_name_is_not_a_false_positive():
    """已知卡名本身恰好带括号（「深色毛衫（旧）」是它自己登记的完整名称，不是
    "深色毛衫"后面被模型加了说明），精确匹配优先于形状判据，不得误报。"""
    props_with_bracket_card = {"asset_manifest": {"props": [
        {"label": "深色毛衫", "canonical_name": "深色毛衫", "segment_indexes": [14]},
        {"label": "深色毛衫（旧）", "canonical_name": "深色毛衫（旧）", "segment_indexes": [14]},
    ]}}
    errors = prop_label_bracket_note_errors(
        [{"label": "深色毛衫（旧）"}], payload=props_with_bracket_card, source_segment_indexes=[14],
    )
    assert errors == []


def test_multiple_props_report_their_own_index():
    props = [
        {"label": "浅蓝色碎花长裙"},
        {"label": "浅蓝色碎花长裙（外套下摆露出的一截）"},
    ]
    errors = errors_for(props)
    assert len(errors) == 1
    assert "props[1]" in errors[0]


def test_empty_source_segment_indexes_falls_back_to_full_known_set():
    """旧数据/调用方未传 source_segment_indexes 时不收窄，仍能识别出已知卡名。"""
    assert errors_for([{"label": "浅蓝色碎花长裙（露出的一截）"}], source_segment_indexes=[]) != []


# ---------------------------------------------------------------------------
# ③ 两条生产路径真的接了这道校验
# ---------------------------------------------------------------------------

def _draft_payload_with_prop(label: str):
    value = dict(
        prompt_text="镜头1：@孟浩 望向远方。{{speech:U01}} 镜头2：山路空寂。",
        shot_count=2,
        dialogue=[dict(utterance_id="U01", speaker_identity_id="bible:孟浩", line="回来吧。",
                        source_segment_index=1, delivery_kind="spoken_dialogue")],
        resources={
            "characters": [dict(identity_id="bible:孟浩", display_name="孟浩", visibility="visible", subject_kind="character")],
            "props": [{"label": label, "description": ""}],
        },
    )
    payload = {
        "asset_manifest": {
            "characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩"}],
            "props": [{"label": "浅蓝色碎花长裙", "canonical_name": "浅蓝色碎花长裙", "segment_indexes": [1]}],
        },
    }
    return value, payload


def test_generation_path_rejects_bracketed_label():
    value, payload = _draft_payload_with_prop("浅蓝色碎花长裙（外套下摆露出的一截）")
    errors = generated_identity_errors(
        _AiStoryboardSegmentDraft.model_validate(value), payload=payload, source_indexes=[1], required_dialogue=[],
    )
    assert any("应逐字写道具卡名「浅蓝色碎花长裙」" in e for e in errors)


def test_generation_path_allows_exact_card_name():
    value, payload = _draft_payload_with_prop("浅蓝色碎花长裙")
    errors = generated_identity_errors(
        _AiStoryboardSegmentDraft.model_validate(value), payload=payload, source_indexes=[1], required_dialogue=[],
    )
    assert not any("道具卡" in e for e in errors)


@pytest.fixture
def workspace_fixture():
    conn = db.get_conn()
    payload = {
        "prep_pack_version": "2.0.0",
        "asset_manifest": {
            "characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩", "segment_indexes": [1]}],
            "props": [{"label": "浅蓝色碎花长裙", "canonical_name": "浅蓝色碎花长裙", "segment_indexes": [1]}],
            "scenes": [], "functional_extras": [],
        },
    }
    conn.execute("INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','本地回归','{}',?)", (db.now(),))
    conn.execute("INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,'第五章',?)", ('孟浩（OS）：我一定会回来。\n\n山风吹过。',))
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,screenplay_json,created_at) VALUES('ep','p',5,'第五集','[1]','confirmed',?,?)", (json.dumps(payload), db.now()))
    segment = dict(
        segment_no=1, synopsis="孟浩自述", source_segment_indexes=[1], beat_ids=["B1"],
        beats=[{"beat_id": "B1", "summary": "孟浩自述", "segment_indexes": [1]}], shot_count=2, duration_s=15,
        target_model="seedance_2", degraded_capabilities=[],
        prompt_text="镜头1：远处山风。{{speech:U01}} 镜头2：山路空寂。",
        dialogue=[dict(utterance_id="U01", speaker_identity_id="bible:孟浩", line="我一定会回来。",
                        source_segment_index=1, delivery="offscreen_voice", delivery_kind="inner_monologue")],
        resources={"characters": [dict(identity_id="bible:孟浩", display_name="孟浩", subject_kind="character", visibility="voice_only")], "scenes": [], "props": []},
    )
    render_segment_speech(segment, dialect="seedance")
    stamp_identity_contract(segment)
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) "
        "VALUES('s1','ep',1,15,'','','','','','[]','[]',?,?,NULL)",
        ('孟浩（OS）：我一定会回来。', json.dumps({"storyboard_pack_segment": segment})),
    )
    conn.commit()
    return conn, segment


def test_identity_workspace_revision_path_rejects_bracketed_label(workspace_fixture):
    conn, segment = workspace_fixture
    candidate = deepcopy(segment)
    candidate["resources"]["props"] = [{"label": "浅蓝色碎花长裙（外套下摆露出的一截）", "description": ""}]
    with pytest.raises(ValueError, match="应逐字写道具卡名「浅蓝色碎花长裙」"):
        workspace.prepare_identity_candidate(conn, shot_id="s1", candidate=candidate)


def test_identity_workspace_revision_path_allows_exact_card_name(workspace_fixture):
    conn, segment = workspace_fixture
    candidate = deepcopy(segment)
    candidate["resources"]["props"] = [{"label": "浅蓝色碎花长裙", "description": "完全可见"}]
    prepared = workspace.prepare_identity_candidate(conn, shot_id="s1", candidate=candidate)
    assert prepared["resources"]["props"] == candidate["resources"]["props"]
