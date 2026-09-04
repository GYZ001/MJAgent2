"""分镜台 2.2.0 方言规则层（app.production.storyboard_dialects）。

专家审阅真实 EP1 十八段产出后拍板的六项方言规则：画外音口型写法统一、
台词锚定到发生它的那个镜头、关键道具建立镜头+构图锁、同框人数上限+受力
描写、跨空间对话的建立镜、镜头时长按信息密度伸缩（不设硬性秒数上限）。

Seedance（中文自由散文）与 H3（英文固定字段语法）两个方言块必须逐条对称
落地——本文件里同一条规则的两个测试函数总是成对出现，一个覆盖 Seedance
一个覆盖 H3，防止「一边改了一边漏」。不覆盖真实供应商往返，只测方言指令
文本本身。

两段指令都是排版成 ~78 列的手工换行散文（发给模型时换行本身不影响语义，
纯粹是给人看的排版），断言时不应该依赖某个多字/多词短语恰好没有跨行——
那样的测试会在下次纯排版调整时误报红。用 ``_nospace``（去掉全部空白，用于
中文短语，中文本身不靠空格分词）和 ``_flat``（把连续空白折成一个空格，用于
英文多词短语）两个规整函数做断言的输入，只有 ``_dialect_for_target_video_model``
返回值这类真正的字面量常量才直接比较原文。
"""
from __future__ import annotations

import re

from app.production.storyboard_dialects import (
    MINIMAX_H3_DIALECT_INSTRUCTIONS,
    SEEDANCE_DIALECT_INSTRUCTIONS,
)


def _nospace(text: str) -> str:
    """去掉全部空白字符——中文短语不含空格，跨行换行不该让断言误判缺失。"""
    return re.sub(r"\s+", "", text)


def _flat(text: str) -> str:
    """把连续空白折成一个空格——英文多词短语跨行换行不该让断言误判缺失。"""
    return re.sub(r"\s+", " ", text)


SEEDANCE_FLAT = _nospace(SEEDANCE_DIALECT_INSTRUCTIONS)
H3_FLAT = _flat(MINIMAX_H3_DIALECT_INSTRUCTIONS)

# ---------------------------------------------------------------------------
# 规则 1：画外音口型统一（修自相矛盾）
# ---------------------------------------------------------------------------

def test_seedance_offscreen_lip_wording_is_locked_and_forbids_slight_movement():
    """实测段 1/5/7 出现「画外音+嘴唇微动」，观众会以为角色在自言自语；
    同一集里还混着「嘴唇没有张合动作」两种写法。文案必须钉死唯一措辞，并
    正面禁止会被理解成开口的替代写法。"""
    assert "嘴唇闭合无张合动作" in SEEDANCE_FLAT, "没有把画外音口型写法钉成唯一措辞"
    assert "嘴唇微动" in SEEDANCE_FLAT and "禁止" in SEEDANCE_FLAT, (
        "没有正面禁止「嘴唇微动」这类会被理解成开口的措辞"
    )


def test_h3_offscreen_lip_wording_forbids_slight_movement():
    """H3 已有 lips-closed 规则，本次只是补上「禁止哪些替代写法」，口径要
    和 Seedance 一致（同一处规则，两种语言各写一份）。"""
    assert "lips remain fully closed" in H3_FLAT
    assert "lips move slightly" in H3_FLAT, "没有正面禁止「lips move slightly」这类会被理解成开口的措辞"


def test_offscreen_lip_rule_still_wired_to_delivery_field_in_both_dialects():
    """口型措辞变了，但不能把它和既有 delivery 字段（offscreen_voice/
    spoken_dialogue）的挂钩规则一起改掉。"""
    for text in (SEEDANCE_DIALECT_INSTRUCTIONS, MINIMAX_H3_DIALECT_INSTRUCTIONS):
        assert "offscreen_voice" in text
        assert "spoken_dialogue" in text


# ---------------------------------------------------------------------------
# 规则 2：台词锚到镜头
# ---------------------------------------------------------------------------

def test_seedance_dialogue_line_must_be_embedded_in_its_own_shot():
    """实测段 11 三人三句、段 17 三句台词全部堆在「全片贯穿」，模型自行
    分配台词到镜头，口型和内容错位概率高。文案必须要求台词写进它发生的
    那个「镜头N」动作链里，而不是先攒着最后再分配。"""
    assert "「镜头N」的动作链" in SEEDANCE_FLAT
    assert "不要把本段所有台词都堆到结尾" in SEEDANCE_FLAT, "没有正面禁止把台词全堆在全片贯穿段"
    assert "段11" in SEEDANCE_FLAT and "段17" in SEEDANCE_FLAT, "没有引用真实故障案例"


def test_h3_dialogue_line_must_be_embedded_in_its_own_shot():
    assert "the specific [Shot N] where" in H3_FLAT
    assert "never bundled into one shot's description" in H3_FLAT


def test_seedance_recap_section_no_longer_repeats_dialogue_verbatim():
    """2026-09-03 改版：台词只写在它发生的那个「镜头N」里一次；结尾「全片
    贯穿」段保留，但只汇总环境音、配乐、风格与约束，不再逐句重申台词——
    旧版「逐镜文本/结尾汇总/dialogue[] 三处逐字一致」收窄成「逐镜动作链
    与 dialogue[] 两处逐字一致」，全片贯穿段不构成第三处。"""
    assert "不再重复这些台词" in SEEDANCE_FLAT
    assert "两处必须逐字一致" in SEEDANCE_FLAT
    assert "不构成第三处" in SEEDANCE_FLAT


def test_dialogue_ledger_cross_check_language_untouched_in_both_dialects():
    """这条规则是在既有『dialogue[] 与音频/prompt_text 互覆盖』规则之上
    再加一层，不是替换——两条规则的关键词必须同时存在。"""
    for text in (SEEDANCE_DIALECT_INSTRUCTIONS, MINIMAX_H3_DIALECT_INSTRUCTIONS):
        assert "required_dialogue" in text
        assert "dialogue[]" in text


def test_seedance_shot_labels_no_longer_carry_a_second_range():
    """2026-09-03 改版：秒数区间会被模型当字面时间码执行，镜头标签改成纯
    序号「镜头1：」「镜头2：」，不再写「（约0-3秒）」这类区间；段时长仍
    固定 15 秒、2-4 镜（本次未改，只去掉标签里的秒数）。"""
    assert "镜头1：" in SEEDANCE_DIALECT_INSTRUCTIONS
    assert "（约0-" not in SEEDANCE_FLAT
    assert "本段固定15秒" in SEEDANCE_FLAT or "本段固定 15" in SEEDANCE_DIALECT_INSTRUCTIONS
    assert "2-4" in SEEDANCE_DIALECT_INSTRUCTIONS


def test_seedance_quote_holds_exactly_one_sentence():
    """一个引号里只放一句话；原文一句台词若含多个独立句子，按标点拆成多个
    连续引号，仍归同一说话人、同一条 dialogue[]。"""
    assert "一个引号里只放一句话" in SEEDANCE_FLAT
    assert "按句号/问号/感叹号拆成多个引号" in SEEDANCE_FLAT
    assert "同一条dialogue[]" in SEEDANCE_FLAT


# ---------------------------------------------------------------------------
# 规则 3：关键道具建立+构图锁
# ---------------------------------------------------------------------------

def test_seedance_prop_requires_establishing_shot_on_first_appearance():
    """实测：葫芦在段 1 突然出现在手里，被反复特写，段 6 扔掉，观众全程
    不知道它是什么——补一条建立镜头，交代来历/身份。"""
    assert "交代它来历/身份的" in SEEDANCE_FLAT and "建立镜头" in SEEDANCE_FLAT
    assert "凭空出现在角色手里" in SEEDANCE_FLAT
    assert "葫芦在段1" in SEEDANCE_FLAT, "没有把真实故障案例写进规则"


def test_seedance_prop_composition_lock_cites_both_jade_pendant_and_gourd_incidents():
    """既有「一镜锁一件道具」规则之外，补构图锁写法，并把「玉佩砸入水面」
    的旧教训和「葫芦一镜四事两空间」的新案例都写进去。"""
    assert "构图约束" in SEEDANCE_FLAT
    assert "玉佩砸入水面" in SEEDANCE_FLAT
    assert "一镜四件事" in SEEDANCE_FLAT or "一镜四事" in SEEDANCE_FLAT
    assert "只剩几个像素" in SEEDANCE_FLAT


def test_h3_prop_requires_establishing_shot_and_composition_lock():
    assert "an establishing shot the first time it appears" in H3_FLAT
    assert "framing constraint" in H3_FLAT
    assert "jade pendant smashes into the water" in H3_FLAT
    assert "a few pixels" in H3_FLAT


# ---------------------------------------------------------------------------
# 规则 4：同框人数与受力
# ---------------------------------------------------------------------------

def test_seedance_group_headcount_cap_is_four_with_split_instruction():
    """实测段 14 镜 1 同框 5 人+高速飞行，新增人脸必崩——超过 4 人必须拆
    镜头或分批入画。"""
    assert "超过4人" in SEEDANCE_FLAT
    assert "拆成" in SEEDANCE_FLAT and ("多个镜头" in SEEDANCE_FLAT or "分批入画" in SEEDANCE_FLAT)
    assert "段14" in SEEDANCE_FLAT, "没有引用真实故障案例"


def test_h3_group_headcount_cap_is_four_with_split_instruction():
    assert "Cap any single frame at 4 people" in H3_FLAT
    assert "split it into multiple shots" in H3_FLAT or "stagger the characters" in H3_FLAT


def test_seedance_high_speed_shot_requires_force_effects():
    """只写「谁在哪」会生成没有速度感的漂浮合影，必须写受力特征。"""
    assert "受力后的具体特征" in SEEDANCE_FLAT
    assert "向后绷直" in SEEDANCE_FLAT and "高频抖动" in SEEDANCE_FLAT
    assert "吹向" in SEEDANCE_FLAT and ("脑后" in SEEDANCE_FLAT or "后方" in SEEDANCE_FLAT)


def test_h3_high_speed_shot_requires_force_effects():
    assert "force effects explicitly" in H3_FLAT
    assert "vibrating rapidly" in H3_FLAT
    assert "blown completely backward" in H3_FLAT


# ---------------------------------------------------------------------------
# 规则 5：跨空间对话的建立镜
# ---------------------------------------------------------------------------

def test_seedance_cross_space_dialogue_requires_establishing_shot():
    """实测段 8-12 全程不同框靠画外音串联，观众不知道两人相隔多远——两个
    说话人分处不同空间时，段内必须有一个交代空间关系的镜头。"""
    assert "分处不同空间" in SEEDANCE_FLAT
    assert "过肩俯视" in SEEDANCE_FLAT
    assert "空间关系" in SEEDANCE_FLAT


def test_h3_cross_space_dialogue_requires_establishing_shot():
    assert "in different spaces" in H3_FLAT
    assert "over-the-shoulder high angle" in H3_FLAT
    assert "spatial relationship" in H3_FLAT


# ---------------------------------------------------------------------------
# 规则 6：镜头时长随情节伸缩（用户否决硬性秒数上限）
# ---------------------------------------------------------------------------

def test_seedance_shot_duration_scales_with_information_density():
    """正面陈述：镜头数/时长由信息密度决定，长镜必须写满可看内容；反例是
    段 16 镜 2 的 9 秒静止袖口特写，导致模型自行插切点、镜头编号对不上。"""
    assert "信息密度决定" in SEEDANCE_FLAT
    assert "写满能看的表演内容" in SEEDANCE_FLAT or "写满可看的表演内容" in SEEDANCE_FLAT
    assert "9秒的袖口特写" in SEEDANCE_FLAT
    assert "无法定点重跑" in SEEDANCE_FLAT


def test_h3_shot_duration_scales_with_information_density():
    assert "information density decide the shot count" in H3_FLAT
    assert "watchable performance content" in H3_FLAT
    assert "9 seconds" in H3_FLAT and "cuff close-up" in H3_FLAT
    assert "could not be singled out for a retry" in H3_FLAT


def test_no_hard_per_shot_second_cap_is_introduced_in_either_dialect():
    """用户明确否决了硬性秒数上限；这条测试是防回归闸门，防止将来有人
    图省事又写回「单镜不得超过 N 秒」这类一刀切措辞。"""
    forbidden_cn = ("不得超过", "不能超过", "最长不得", "上限为")
    forbidden_en = ("must not exceed", "no longer than", "shall not exceed")
    for phrase in forbidden_cn:
        assert phrase not in SEEDANCE_FLAT, f"引入了硬性秒数上限措辞：{phrase!r}"
    for phrase in forbidden_en:
        assert phrase not in H3_FLAT, f"引入了硬性秒数上限措辞：{phrase!r}"


def test_shot_count_range_two_to_four_still_intact_after_duration_rewrite():
    """时长规则重写不能连带把既有「2-4 镜」放宽范围一起改掉。"""
    assert "2-4" in SEEDANCE_DIALECT_INSTRUCTIONS
    assert "2-4" in MINIMAX_H3_DIALECT_INSTRUCTIONS
    assert "本段固定 3-4 镜" not in SEEDANCE_DIALECT_INSTRUCTIONS
    assert "write 3-4 Shots total." not in MINIMAX_H3_DIALECT_INSTRUCTIONS


# ---------------------------------------------------------------------------
# 对称性元测试：六项规则必须两个方言块同时命中，不许只改一边
# ---------------------------------------------------------------------------

def test_all_six_rules_present_symmetrically_in_both_dialects():
    """docstring 里写着两块同步的纪律：这里用可执行断言守住它，而不是靠
    人肉审阅——每条规则给一对中英文标志短语，必须同时出现在各自方言块里。"""
    rule_markers = [
        ("画外音口型统一", "嘴唇闭合无张合动作", "lips remain fully closed"),
        ("台词锚到镜头", "「镜头N」的动作链", "the specific [Shot N] where"),
        ("道具建立镜头", "建立镜头", "an establishing shot the first time it appears"),
        ("道具构图锁", "构图约束", "framing constraint"),
        ("同框人数上限", "超过4人", "Cap any single frame at 4 people"),
        ("高速受力描写", "受力后的具体特征", "force effects explicitly"),
        ("跨空间建立镜", "过肩俯视", "over-the-shoulder high angle"),
        ("时长按信息密度伸缩", "信息密度决定", "information density decide the shot count"),
    ]
    missing = []
    for name, cn_marker, en_marker in rule_markers:
        if cn_marker not in SEEDANCE_FLAT:
            missing.append(f"Seedance 缺「{name}」标志短语：{cn_marker!r}")
        if en_marker not in H3_FLAT:
            missing.append(f"H3 缺「{name}」标志短语：{en_marker!r}")
    assert not missing, "；".join(missing)


def test_seedance_requires_footing_and_display_name_mentions():
    from app.production.storyboard_dialects import (
        SEEDANCE_DIALECT_INSTRUCTIONS,
        prompt_reference_prefix_errors,
    )

    assert "脚下与依托" in SEEDANCE_DIALECT_INSTRUCTIONS
    assert "人不上桌" in SEEDANCE_DIALECT_INSTRUCTIONS
    assert "人物与家具不穿插" in SEEDANCE_DIALECT_INSTRUCTIONS
    assert "@ 后面\n  直接跟 relevant_assets.characters" in SEEDANCE_DIALECT_INSTRUCTIONS or "display_name（例如 @黄总）" in SEEDANCE_DIALECT_INSTRUCTIONS
    errors = prompt_reference_prefix_errors("镜头1：@bible:黄总 拍桌，@张姐 缩着脖子，@entity:a029ddf7 围观")
    assert len(errors) == 1 and "@bible:黄总" in errors[0] and "@entity:a029ddf7" in errors[0]
    assert prompt_reference_prefix_errors("镜头1：@黄总 拍桌") == []
