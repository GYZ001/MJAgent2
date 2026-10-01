"""服装状态延续的确定性回填（``ensure_wardrobe_continuity_in_prompt``），与
``ensure_travel_direction_in_prompt`` 同一形状。

2026-09-28 真实回归（《顾念长安》第 1 集）：围巾第 14 段由顾屿给温念围上，第 16、18 段整段
消失；开衫扣子第 11 段扣好、第 12 段敞开——wardrobe 字段此前只落库、只在 log 级别提示，从不
回填进提示词。
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from app.production.storyboard_continuity_memo import (
    _AiCharacterState,
    _AiContinuityMemo,
    _wardrobe_line_pattern,
    ensure_wardrobe_continuity_in_prompt,
)


class _FakeProp:
    def __init__(self, label: str, description: str = "") -> None:
        self.label = label
        self.description = description


def _draft(prompt_text: str, *, characters, resource_characters, props=None) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_text=prompt_text,
        continuity_memo=_AiContinuityMemo(time_of_day="白日", characters=characters),
        resources=SimpleNamespace(characters=resource_characters, props=list(props or [])),
    )


def _visible(identity_id: str, display_name: str) -> SimpleNamespace:
    return SimpleNamespace(identity_id=identity_id, display_name=display_name, visibility="visible")


def test_appends_wardrobe_sentence_but_does_not_register_a_prop():
    """红：修复前的旧行为是只记日志、什么都不回填——用手写的旧版本函数证明视频模型
    读不到围巾状态；绿：新函数把 wardrobe 写进提示词的「续接服装：」行。2026-10-01
    起不再登记进 resources.props——那只是记账标签，没有任何下游消费，却天然无图，
    纯属分镜台界面「道具」分组里的噪音（见 ensure_wardrobe_continuity_in_prompt
    docstring）。"""
    draft = _draft(
        "镜头1：温念低头看信。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="围着顾屿给的红色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    assert ensure_wardrobe_continuity_in_prompt(draft) == []
    assert draft.prompt_text.endswith("续接服装：@温念 围着顾屿给的红色围巾。")
    assert draft.resources.props == [], "服装续接不再铸成 props 条目"

    def _old_log_only_wardrobe(_draft) -> None:
        """修复前的旧行为：wardrobe 只在 log 级别提示，从不回填提示词。"""
        return None

    red_draft = _draft(
        "镜头1：温念低头看信。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="围着顾屿给的红色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    _old_log_only_wardrobe(red_draft)
    assert "围巾" not in red_draft.prompt_text, "旧行为确实不回填提示词——红态验证成立"


def test_does_not_duplicate_when_prompt_already_mentions_wardrobe():
    draft = _draft(
        "镜头1：@温念 围着顾屿给的红色围巾走进屋。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="围着顾屿给的红色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    before = draft.prompt_text
    assert ensure_wardrobe_continuity_in_prompt(draft) == []
    assert draft.prompt_text == before
    assert draft.resources.props == [], "不再登记进 resources.props，props 应保持为空"


def test_leaves_pre_existing_props_untouched():
    """``resources.props`` 若已有调用方自己登记的、跟服装无关的其它道具条目，
    2026-10-01 起本函数完全不碰这个列表——既不新增也不去重，原样保留。"""
    existing = _FakeProp("温念的红围巾", "围着顾屿给的红色围巾")
    draft = _draft(
        "镜头1：温念低头看信。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="围着顾屿给的红色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
        props=[existing],
    )
    ensure_wardrobe_continuity_in_prompt(draft)
    assert draft.resources.props == [existing]


def test_empty_wardrobe_or_no_display_name_is_skipped():
    draft = _draft(
        "镜头1：温念低头看信。",
        characters=[
            _AiCharacterState(identity_id="bible:温念", wardrobe=""),
            _AiCharacterState(identity_id="bible:未知", wardrobe="灰色外套"),
        ],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    before = draft.prompt_text
    ensure_wardrobe_continuity_in_prompt(draft)
    assert draft.prompt_text == before
    assert draft.resources.props == []


def test_replaces_stale_line_instead_of_duplicating_when_wardrobe_text_drifts():
    """2026-09-28 真实回归（《顾念长安》第 1 集第 13 段）：同一角色的服装描述在多次
    调用间轻微改写（「扣子已扣好，领口被她拢紧」→「扣子已被顾屿扣好」），旧版
    ``wardrobe not in prompt`` 逐字比对对第二次改写视而不见，两条「续接服装：@温念」
    整行都留在了提示词里。红：手写旧逻辑复现两行同存；绿：新函数收敛成一行。"""
    stale_prompt = (
        "镜头1：温念低头看信。\n续接服装：@温念 米白色开衫（扣子已扣好，领口被她拢紧）。"
    )
    draft = _draft(
        stale_prompt,
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="米白色开衫（扣子已被顾屿扣好）")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    assert ensure_wardrobe_continuity_in_prompt(draft) == []
    assert draft.prompt_text.count("续接服装：@温念") == 1
    assert draft.prompt_text.endswith("续接服装：@温念 米白色开衫（扣子已被顾屿扣好）。")

    def _old_substring_check_append(_draft, wardrobe: str) -> None:
        """修复前的旧行为：只按 wardrobe 原文是否已是 prompt 子串判断，改写后的新
        文本不是旧行的子串，直接在旧行之后再追加一行。"""
        prompt = _draft.prompt_text
        if wardrobe not in prompt:
            _draft.prompt_text = prompt.rstrip() + f"\n续接服装：@温念 {wardrobe}。"

    red_draft = _draft(
        stale_prompt,
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="米白色开衫（扣子已被顾屿扣好）")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    _old_substring_check_append(red_draft, "米白色开衫（扣子已被顾屿扣好）")
    assert red_draft.prompt_text.count("续接服装：@温念") == 2, "旧逐字比对确实会重复追加——红态验证成立"


def test_replacing_stale_line_does_not_swallow_trailing_sentence_on_same_physical_line():
    """评审复现（2026-09-28）：模型有时把「续接服装：……。」与别的句子挤在同一物理行；
    ``_wardrobe_line_pattern`` 的量词必须非贪婪，只吃到第一个句号，不能一路吃到该行
    最后一个句号把无关后续句子一并删掉。红：手写贪婪版本证明会吞掉「这句话很重要。」；
    绿：当前实现（含整段 ``ensure_wardrobe_continuity_in_prompt`` 端到端）都不吞。"""
    stale_prompt = (
        "镜头1：温念低头看信。\n续接服装：@温念 米白色开衫（扣子已扣好）。这句话很重要。"
    )
    draft = _draft(
        stale_prompt,
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="米白色开衫（扣子已被顾屿扣好）")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    assert ensure_wardrobe_continuity_in_prompt(draft) == []
    assert "这句话很重要。" in draft.prompt_text
    assert draft.prompt_text.count("续接服装：@温念") == 1

    def _old_greedy_pattern(name: str) -> re.Pattern[str]:
        return re.compile(rf"\n?续接服装：@{re.escape(name)} [^\n]*。")

    red_result = _old_greedy_pattern("温念").sub("", stale_prompt)
    assert "这句话很重要。" not in red_result, "旧贪婪正则确实会把同一行后续句子一并吞掉——红态验证成立"

    assert _wardrobe_line_pattern("温念").search(stale_prompt).group(0) == (
        "\n续接服装：@温念 米白色开衫（扣子已扣好）。"
    )


def test_wardrobe_overlapping_appearance_anchor_logs_advisory_not_blank(caplog):
    """模型仍把体貌特征/说明性文字写进 wardrobe（真实样本「外观锚点未写服装（二十
    余岁青年男性，身形高挑，留乌黑短发）」）：不静默吞、不兜底改写，只记一条可搜索
    的告警；wardrobe 依然按原样回填（诚实呈现问题，不擅自替模型编造/清空）。"""
    anchor = "二十余岁青年男性，身形高挑，留乌黑短发"
    draft = _draft(
        f"镜头1：@顾屿 的外观：{anchor}。他站在窗边。",
        characters=[_AiCharacterState(identity_id="bible:顾屿", wardrobe=f"外观锚点未写服装（{anchor}）")],
        resource_characters=[_visible("bible:顾屿", "顾屿")],
    )
    with caplog.at_level("WARNING"):
        ensure_wardrobe_continuity_in_prompt(draft)
    assert "[STORYBOARD_WARDROBE_NOT_CLOTHING][未拦截]" in caplog.text
    assert "续接服装：@顾屿" in draft.prompt_text  # 不静默吞，也不兜底清空


def test_wardrobe_unrelated_to_anchor_does_not_log_advisory(caplog):
    """真实服装描述与外观锚点没有重合：不误报。"""
    draft = _draft(
        "镜头1：@温念 的外观：二十四岁的年轻女性，身形纤细。她走进屋。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="米白色针织开衫，内搭浅蓝色碎花长裙")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    with caplog.at_level("WARNING"):
        ensure_wardrobe_continuity_in_prompt(draft)
    assert "STORYBOARD_WARDROBE_NOT_CLOTHING" not in caplog.text


def test_empty_prompt_or_no_characters_is_left_untouched():
    empty_prompt = _draft(
        "", characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="红围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    ensure_wardrobe_continuity_in_prompt(empty_prompt)
    assert empty_prompt.prompt_text == ""

    no_characters = _draft("镜头1：空镜。", characters=[], resource_characters=[])
    ensure_wardrobe_continuity_in_prompt(no_characters)
    assert no_characters.prompt_text == "镜头1：空镜。"


def test_planned_change_at_segment_start_is_not_overwritten_by_previous_look():
    """P0-D（2026-09-29，app.production.storyboard_wardrobe_plan）：全集服装表
    在本段开场就安排换装时，模型按注入的规则把 continuity_memo.characters[].
    wardrobe 写成新造型——本函数不接收 previous_memo，只读本段 continuity_memo
    自己上报的值，没有任何路径能把它拉回上一段的旧造型（例如上一段还是「米白色
    针织开衫」，本段计划要求换成围巾）。"""
    draft = _draft(
        "镜头1：温念走进屋内，顾屿跟在她身后。",
        characters=[_AiCharacterState(identity_id="bible:温念", wardrobe="颈间绕着深灰色围巾")],
        resource_characters=[_visible("bible:温念", "温念")],
    )
    assert ensure_wardrobe_continuity_in_prompt(draft) == []
    assert draft.prompt_text.endswith("续接服装：@温念 颈间绕着深灰色围巾。")
    assert "米白色针织开衫" not in draft.prompt_text
