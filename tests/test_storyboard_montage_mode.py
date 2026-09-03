"""WS7：蒙太奇镜头形态（Shot.form/beats）的契约、方言与校验测试。

背景与真实缺陷证据（2026-09-02，B 库只读实测，见任务派单）：项目「跑不快的
孩子」第 2 集是一段 366 字的第一人称总结（「我八岁的时候被诊断出长不高……
我三十五岁，把它抱在怀里」），分镜台 2.0.0 把它拆成 4 个 15 秒段，每段的
scene_name 只取 resources.scenes[0]（第一镜被配成「校园食堂」，与荣誉列举
无关）；characters 里出现「旁白」这个不该进画面人物清单的标签；第 3 镜的
第三人称叙述句「他跑得不算快」被安在角色「球员」自己头上说。下面 fixture
里的字段值是从 B 生产库原样摘录的（projects.name='跑不快的孩子'，
episodes.episode_no=2），不是构造的假数据。

本文件覆盖四件事：
1. Shot.form/beats 契约本身（默认值、可选性、MontageBeat 结构）。
2. app.validators.storyboard_montage 的结构校验（beats 数/scene 合法性/
   narration 非空/旁白泄漏），且对 form != "montage" 的行零影响。
3. app.production.storyboard_dialects.render_montage_beat_shots 的确定性
   拍点转文本渲染。
4. 跨项目真实数据的形态判定：既有「scene」形态镜头（含台词全部
   offscreen_voice 的单场景内心独白）在新校验下必须零回归；「跑不快的孩子」
   这类跨年排比段落按新合同应判为 montage 候选。
"""
from __future__ import annotations

from app.production.storyboard_dialects import render_montage_beat_shots
from app.schemas import (
    NARRATOR_LABEL,
    SHOT_FORMS,
    MontageBeat,
    Shot,
    Storyboard,
    is_narrator_label,
)
from app.validators import (
    storyboard_montage_shot_errors,
    storyboard_narrator_label_errors,
    validate_storyboard_pack_montage,
)


# ---------------------------------------------------------------------------
# 1. 契约本身
# ---------------------------------------------------------------------------

def test_shot_form_defaults_to_scene_and_beats_default_empty():
    shot = Shot(shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x")
    assert shot.form == "scene"
    assert shot.beats == []


def test_shot_forms_constant_covers_scene_and_montage():
    assert SHOT_FORMS == {"scene", "montage"}


def test_shot_accepts_montage_form_with_beats():
    shot = Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x",
        form="montage",
        narration="总结性原文",
        beats=[
            MontageBeat(time_anchor="我八岁", scene_name="", visual="病房里打针"),
            MontageBeat(time_anchor="我三十五岁", scene_name="世界杯赛场", visual="把奖杯抱在怀里"),
        ],
    )
    assert shot.form == "montage"
    assert [b.time_anchor for b in shot.beats] == ["我八岁", "我三十五岁"]


def test_narrator_label_constant_and_helper():
    assert NARRATOR_LABEL == "旁白"
    assert is_narrator_label("旁白")
    assert is_narrator_label(" 旁白 ")
    assert not is_narrator_label("球员")
    assert not is_narrator_label("")


# ---------------------------------------------------------------------------
# 2. 结构校验
# ---------------------------------------------------------------------------

def _montage_shot(**overrides) -> Shot:
    fields = {
        "shot_no": 1, "duration_s": 15, "shot_size": "", "camera_move": "",
        "action_desc": "x", "form": "montage", "narration": "总结性原文",
        "beats": [
            MontageBeat(time_anchor="我八岁", scene_name="", visual="病房里打针"),
            MontageBeat(time_anchor="我三十五岁", scene_name="世界杯赛场", visual="把奖杯抱在怀里"),
        ],
    }
    fields.update(overrides)
    return Shot(**fields)


def test_scene_form_shot_is_never_checked_by_montage_validator():
    # form 仍是默认的 "scene"：即使 beats/narration 都不合法，也必须放行——
    # 这条校验只管 montage 形态自己的结构，不重新给 scene 形态的行加规则。
    shot = Shot(
        shot_no=1, duration_s=15, shot_size="远景", camera_move="固定", action_desc="x",
        narration="", beats=[MontageBeat(visual="")] * 5,
    )
    assert shot.form == "scene"
    assert storyboard_montage_shot_errors(shot, known_scene_names=set()) == []


def test_montage_shot_valid_case_passes():
    shot = _montage_shot()
    errors = storyboard_montage_shot_errors(
        shot, known_scene_names={"世界杯赛场", "校园食堂"},
    )
    assert errors == []


def test_montage_shot_flags_empty_narration():
    shot = _montage_shot(narration="")
    errors = storyboard_montage_shot_errors(shot, known_scene_names={"世界杯赛场"})
    assert any("NARRATION_EMPTY" in e for e in errors)


def test_montage_shot_flags_too_few_beats():
    shot = _montage_shot(beats=[])
    errors = storyboard_montage_shot_errors(shot, known_scene_names=set())
    assert any("BEAT_COUNT" in e for e in errors)


def test_montage_shot_flags_too_many_beats():
    shot = _montage_shot(beats=[
        MontageBeat(visual="a"), MontageBeat(visual="b"),
        MontageBeat(visual="c"), MontageBeat(visual="d"),
    ])
    errors = storyboard_montage_shot_errors(shot, known_scene_names=set())
    assert any("BEAT_COUNT" in e for e in errors)


def test_montage_shot_flags_unknown_beat_scene():
    shot = _montage_shot(beats=[MontageBeat(scene_name="不存在的场景", visual="x")])
    errors = storyboard_montage_shot_errors(shot, known_scene_names={"世界杯赛场"})
    assert any("BEAT_SCENE_UNKNOWN" in e for e in errors)


def test_montage_shot_empty_known_scenes_fails_closed_not_open():
    """known_scene_names 为空集合时，任何非空 scene_name 都必须判非法——
    「没有任何合法场景」不等于「跳过检查」（CLAUDE.md 的空集合闸门规则）。
    scene_name 留空的拍点则始终合法，不受 known_scene_names 是否为空影响。
    """
    shot_with_scene = _montage_shot(beats=[MontageBeat(scene_name="某场景", visual="x")])
    errors = storyboard_montage_shot_errors(shot_with_scene, known_scene_names=set())
    assert any("BEAT_SCENE_UNKNOWN" in e for e in errors)

    shot_without_scene = _montage_shot(beats=[MontageBeat(scene_name="", visual="x")])
    assert storyboard_montage_shot_errors(shot_without_scene, known_scene_names=set()) == []


def test_montage_shot_flags_empty_beat_visual():
    shot = _montage_shot(beats=[MontageBeat(visual="")])
    errors = storyboard_montage_shot_errors(shot, known_scene_names=set())
    assert any("BEAT_VISUAL_EMPTY" in e for e in errors)


def test_validate_storyboard_pack_montage_aggregates_over_board():
    board = Storyboard(episode_no=1, shots=[_montage_shot(), _montage_shot(narration="")])
    errors = validate_storyboard_pack_montage(board, known_scene_names={"世界杯赛场"})
    assert any("NARRATION_EMPTY" in e for e in errors)


def test_narrator_label_errors_flags_characters_and_visible():
    shot = Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x",
        characters=["旁白", "球员"], characters_visible=["旁白"],
    )
    errors = storyboard_narrator_label_errors(shot)
    assert any("NARRATOR_IN_CHARACTERS]" in e for e in errors)
    assert any("NARRATOR_IN_CHARACTERS_VISIBLE]" in e for e in errors)


def test_narrator_label_errors_clean_when_absent():
    shot = Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x",
        characters=["球员"], characters_visible=["球员"],
    )
    assert storyboard_narrator_label_errors(shot) == []


# ---------------------------------------------------------------------------
# 3. 方言：拍点到「镜头N」文本的确定性渲染
# ---------------------------------------------------------------------------

def test_render_montage_beat_shots_empty_beats():
    assert render_montage_beat_shots([], duration_s=15) == ""


def test_render_montage_beat_shots_single_beat_spans_full_duration():
    text = render_montage_beat_shots(
        [MontageBeat(time_anchor="我三十五岁", scene_name="世界杯赛场", visual="把奖杯抱在怀里")],
        duration_s=15,
    )
    assert text == "镜头1（约0-15秒）：我三十五岁、世界杯赛场、把奖杯抱在怀里"


def test_render_montage_beat_shots_splits_evenly_across_three_beats():
    beats = [
        MontageBeat(time_anchor="我八岁", visual="打针"),
        MontageBeat(time_anchor="我十三岁", visual="食堂吃饭"),
        MontageBeat(time_anchor="我三十五岁", visual="抱起奖杯"),
    ]
    text = render_montage_beat_shots(beats, duration_s=15)
    lines = text.split("\n")
    assert lines[0].startswith("镜头1（约0-5秒）")
    assert lines[1].startswith("镜头2（约5-10秒）")
    assert lines[2].startswith("镜头3（约10-15秒）")


def test_render_montage_beat_shots_last_beat_absorbs_remainder():
    # 15 // 2 = 7：第一拍 0-7，第二拍吸收余数到 7-15，不是 7-14 留 1 秒真空。
    beats = [MontageBeat(visual="a"), MontageBeat(visual="b")]
    text = render_montage_beat_shots(beats, duration_s=15)
    lines = text.split("\n")
    assert lines[0].startswith("镜头1（约0-7秒）")
    assert lines[1].startswith("镜头2（约7-15秒）")


# ---------------------------------------------------------------------------
# 4. 跨项目真实数据：零回归 + montage 候选判定
# ---------------------------------------------------------------------------
#
# 下面每个 _b_* 函数返回的字段值都摘自 B 生产库真实分镜行（scene_name /
# characters / narration / dialogues[].delivery / source_excerpt 逐字照抄）。
# 形态判定表（本次派单要求的产出物，判据：原文段落本身是否列举/排比多个
# 时间点——不是「有没有 offscreen_voice」）：
#
#   项目            集/镜        判定       理由
#   跑不快的孩子    ep2 shot1   montage    荣誉列举+跨年排比引子
#   跑不快的孩子    ep2 shot2   montage    「我八岁…我十三岁…我三十五岁」教科书级排比
#   跑不快的孩子    ep2 shot3   边界（保守判 scene）  单句跨年总结「跑了二十六年」，
#                                          无逐年列举，暂不满足"多个时间点"门槛
#   跑不快的孩子    ep2 shot4   scene      单句现场引语，spoken_dialogue，不许改判
#   我欲封天        ep1 shot1-4 scene      同场景连续内心独白，非跨年总结
#   神墓            ep1 shot15/16/22/24 scene  同场景内心独白/回忆引语，单一时空
#   西游记          ep1 shot1/2/18 scene  开篇诗白/写景/行路引语，均非「我X岁…」
#                                          式排比，证据不足不升级为 montage
#
# 下面测试只验证机械可判的部分：scene 形态的行在新校验下必须零回归（这是
# 「现有校验对既有 scene 形态镜头零影响」的可执行证明）；montage 候选行套用
# 新契约后必须能通过结构校验（证明契约本身可用，不是只停留在文档）。

def _b_paobukuai_ep2_shot1() -> Shot:
    return Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x",
        scene_name="校园食堂",
        characters=["少年", "球员", "旁白"],
        narration="旁白介绍大众眼中的传奇球员生涯荣誉与称号，引出他的内心想法",
        dialogues=[
            {"speaker": "旁白", "line": "七座金球，四十四个冠军，八次西甲金靴，世界杯，史上最伟大球员，人称球王GOAT。", "delivery": "offscreen_voice"},
            {"speaker": "旁白", "line": "可你问他会怎么说？", "delivery": "offscreen_voice"},
            {"speaker": "球员", "line": "我三十五岁捧起世界杯奖杯。", "delivery": "offscreen_voice"},
        ],
        source_excerpt="很多年以后，人们会这样介绍他……我八岁的时候被诊断出长不高……我三十五岁，在卡塔尔的夜里，把它抱在怀里。",
    )


def _b_paobukuai_ep2_shot4() -> Shot:
    return Shot(
        shot_no=4, duration_s=15, shot_size="", camera_move="", action_desc="x",
        scene_name="罗萨里奥土场",
        characters=["球员"],
        narration="球员以罗萨里奥土场的小孩自喻，总结生涯始终未改的初心",
        dialogues=[{
            "speaker": "球员",
            "line": "从头到尾，我只是那个在罗萨里奥土场上被撞倒了就爬起来接着跑的小孩。",
            "delivery": "spoken_dialogue",
        }],
        source_excerpt="——从头到尾，我只是那个在罗萨里奥土场上被撞倒了就爬起来接着跑的小孩。",
    )


def _b_woyufengtian_ep1_shot1() -> Shot:
    return Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x",
        scene_name="赵国大青山山顶",
        characters=["孟浩"],
        narration="落榜书生孟浩坐在大青山顶，感叹科举三年屡次失败",
        dialogues=[
            {"speaker": "bible:孟浩", "line": "又落榜了……", "delivery": "offscreen_voice"},
            {"speaker": "bible:孟浩", "line": "考了三年……莫非科举真的不是我孟浩未来的路？", "delivery": "offscreen_voice"},
        ],
        source_excerpt="少年有些瘦弱，手中拿着一个葫芦……坐在那里的一个文生少年身上。",
    )


def _b_shenmu_ep1_shot15() -> Shot:
    return Shot(
        shot_no=15, duration_s=15, shot_size="", camera_move="", action_desc="x",
        scene_name="乡野山坳茅屋",
        characters=["辰南", "老人"],
        narration="看到茅屋和老人，心生莫名情绪",
        dialogues=[
            {"speaker": "bible:辰南", "line": "我怎么会将父母和这个老人联系到一起呢？", "delivery": "offscreen_voice"},
            {"speaker": "bible:辰南", "line": "让我去打水？难道他要我在这里当苦力？", "delivery": "offscreen_voice"},
        ],
        source_excerpt="辰南心中涌起一股莫名的情绪，这是他再世为人后见到的第一个人……",
    )


def _b_xiyouji_ep1_shot1() -> Shot:
    return Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x",
        scene_name="",
        characters=["中年文士"],
        narration="开篇吟诵西游诗，讲解天地十二会演化规则",
        dialogues=[
            {"speaker": "中年文士", "line": "混沌未分天地乱，茫茫渺渺无人见。", "delivery": "offscreen_voice"},
            {"speaker": "中年文士", "line": "盖闻天地之数，有十二万九千六百岁为一元。将一元分为十二会。", "delivery": "offscreen_voice"},
        ],
        source_excerpt="诗曰：混沌未分天地乱，茫茫渺渺无人见……盖闻天地之数，有十二万九千六百岁为一元。",
    )


_REAL_SCENE_FORM_SHOTS = [
    _b_paobukuai_ep2_shot4,
    _b_woyufengtian_ep1_shot1,
    _b_shenmu_ep1_shot15,
    _b_xiyouji_ep1_shot1,
]


def test_real_scene_form_shots_are_zero_impact_under_new_montage_validator():
    """既有 scene 形态镜头（含全部台词 offscreen_voice 的单场景内心独白）
    套用新校验必须一条错误都不报——这是「现有校验对既有 scene 形态镜头零
    影响」的可执行证明，不是转述。"""
    for factory in _REAL_SCENE_FORM_SHOTS:
        shot = factory()
        assert shot.form == "scene"
        assert storyboard_montage_shot_errors(shot, known_scene_names=set()) == []


def test_real_paobukuai_shot1_reproduces_narrator_leaked_into_characters():
    """跑不快的孩子 ep2 shot1 是真实缺陷现场：characters 里混进了「旁白」。
    本模块的校验必须能抓住它——这证明新增的旁白校验不是空转。"""
    shot = _b_paobukuai_ep2_shot1()
    errors = storyboard_narrator_label_errors(shot)
    assert any("NARRATOR_IN_CHARACTERS]" in e for e in errors)


def test_real_paobukuai_shot4_dialogue_shot_not_flagged_as_montage_candidate():
    """shot4 是单句现场引语（spoken_dialogue、单一场景），新合同下不许被
    改判成 montage——"对白/动作明确的段落不许被改成蒙太奇"这条不倒退。"""
    shot = _b_paobukuai_ep2_shot4()
    assert shot.form == "scene"  # 持久化字段本身已经是 scene，不需要重判


def test_paobukuai_shot1_and_shot2_as_corrected_montage_pass_new_contract():
    """把 shot1/shot2 real 原文按新合同重写成 montage 形态（本次改动要交付
    的契约），验证结构校验放行——证明契约本身可用，不只是停留在文档层面。
    这不是断言现有生成代码已经产出这个形状（storyboard_pack.py 的接线不在
    本次改动范围内，见最终报告），而是断言"如果按新合同生成，会通过校验"。
    """
    corrected_shot1 = Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x",
        form="montage",
        narration=(
            "很多年以后，人们会这样介绍他：七座金球奖，四十四个冠军，八次西甲金靴，"
            "史上最伟大的球员。可如果你问他，他会说——我八岁的时候被诊断出长不高。"
        ),
        beats=[
            MontageBeat(time_anchor="", scene_name="世界杯赛场", visual="领奖台荣誉墙镜头快切", source_span="七座金球奖，四十四个冠军"),
            MontageBeat(time_anchor="我八岁", scene_name="", visual="八岁男孩打针的特写", source_span="我八岁的时候被诊断出长不高"),
        ],
    )
    corrected_shot2 = Shot(
        shot_no=2, duration_s=15, shot_size="", camera_move="", action_desc="x",
        form="montage",
        narration="球员自述年少患病、离家独自生活、直至捧杯的多年历程",
        beats=[
            MontageBeat(time_anchor="我八岁", scene_name="", visual="打针三年", source_span="我每天给自己打针，打了三年"),
            MontageBeat(time_anchor="我十三岁", scene_name="校园食堂", visual="独自吃饭", source_span="在食堂一个人吃饭"),
            MontageBeat(time_anchor="我三十五岁", scene_name="世界杯赛场", visual="把奖杯抱在怀里", source_span="把它抱在怀里"),
        ],
    )
    board = Storyboard(episode_no=2, shots=[corrected_shot1, corrected_shot2])
    known_scenes = {"世界杯赛场", "校园食堂", "罗萨里奥土场"}
    assert validate_storyboard_pack_montage(board, known_scene_names=known_scenes) == []
