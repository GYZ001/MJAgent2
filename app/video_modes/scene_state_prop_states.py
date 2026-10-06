"""场景状态图：把上一段备忘里"这件道具此刻在哪、是什么状态"画进状态图本身。

背景（2026-10-05，《顾念长安》proj_ca86b15ab7d7 第1集第2段真实故障）：温念拔下
插头，第1段 ``continuity_memo.props`` 记着「插座与插头｜位置：插座在床尾墙根
贴近地板处；插头已拔出，在温念右手附近｜状态：已拔下，插座墙根留一缕淡淡焦黑
痕迹」，第2段分镜正文也写明插头躺在地板上，但同时发给视频模型的场景状态图
（见 ``app.video_modes.scene_state_views``）只画了"深夜断电、墙角渗水、插座
留痕"这类场景级状态，没有画插头本身此刻在哪——本会话已实测视频模型对状态图
的服从度远高于文字，四轮重抽都把插头画回插座里。这里补的是"物件此刻的位置与
状态"这一句正面陈述，和 ``scene_state_views.prop_appearance_notes_for_
description``（道具卡外观）是同一张状态图提示词上的两句互补陈述，不是两套
机制：外观陈述管"长什么样"，这里管"此刻在哪、什么状态"。

取数来源是**上一段**（``segment_no - 1``，即 ``shots.shot_no - 1``——分镜台
每个 ``shots`` 行就是一个 ``segment_no``，见 ``app.production.storyboard_
pack`` 写库那段）的 ``continuity_memo.props``，不是本段自己的：状态图画的是
"本段开始前这个场景此刻是什么样"，这正是上一段结束时的世界状态。只有上一段
与本状态串同属一个场景（``scene_reference_id`` 相同，判据与 ``group_scene_
state_runs`` 用于分组的字段完全一致——换场景的上一段状态对本场景没有参照
意义）才取；上一段不存在（本状态串从本集第一段开始）、没有备忘、或备忘没有
``props`` 时诚实地给空字符串，不编造。

过滤掉"在人手里/穿在身上"的条目（CLAUDE.md「禁止黑白名单与枚举穷举」：判据
从数据本身推导，不维护名单）——但"文字里出现了角色显示名"不能直接当判据：
续接备忘写道具位置时常用角色身体部位做相对空间锚点来定位物件本身（上面那条
真实故障数据原文就是"插头已拔出，在温念右手附近"，物件已经不在角色身上，
角色名只是用来说"大概在屋里哪个位置"），这种写法里同一条目往往还有别的、完全
不提角色的独立空间信息（"插座在床尾墙根贴近地板处"）。真正该剔除的是整条
``location``/``state`` 拆开逐句看、每一句都点到了角色显示名（人物谱 ``name``
与 ``aliases[].text``，覆盖全集而不是只看本段 ``resources.characters``，因为
"谁手里拿着它"未必是本段登记的在场人物）、没有任何一句带独立空间信息的条目——
这种条目整条都在讲"此刻在谁手上/身上"，空房间状态图里画出来会在视频里变成
第二份（道具卡的参考图/续接正面陈述里已经交代了"在某人手上"，状态图再画一遍
是编造）；只要有一句不提角色名，就说明这件物件本身有独立于人物的位置/状态，
整条原文保留，不删改（2026-10-05 返工：旧判据按"整条文字里出现过角色名就
剔除整条"实现，在上面这条真实故障数据上会把"插座与插头"整条滤掉，核心故障
反而没被修到，见 ``filter_prop_states_for_empty_room``）。
"""
from __future__ import annotations

import re
from typing import Any

from app.props.text_match import matched_props_in_text
from app.video_modes.scene_state_views import (
    scene_entries_for_shot,
    segment_props_for_shot_row,
)

#: 中文常见分句符：按它们把 location/state 单个字段拆成独立分句，纯结构切分，
#: 不认任何具体词汇——判据落在"这一句本身提没提角色名"上，不靠维护一张身体
#: 部位/相对方位的词表去分辨"拿着"和"附近"。
_CLAUSE_SPLIT_PATTERN = re.compile(r"[，；。！？,;]")


def _nonempty_clauses(text: str) -> list[str]:
    return [clause.strip() for clause in _CLAUSE_SPLIT_PATTERN.split(text) if clause.strip()]


def character_display_names_from_bible(bible: Any) -> set[str]:
    """本集人物谱里全部角色的显示名：规范名 + 全部别名文本。空名/空别名跳过。"""
    names: set[str] = set()
    for character in bible.characters:
        name = str(getattr(character, "name", "") or "").strip()
        if name:
            names.add(name)
        for alias in getattr(character, "aliases", None) or []:
            text = str(getattr(alias, "text", "") or "").strip()
            if text:
                names.add(text)
    return names


def _shot_row_by_no(shot_rows: list[Any], shot_no: int) -> Any | None:
    for row in shot_rows:
        if int(row["shot_no"]) == shot_no:
            return row
    return None


def _previous_segment_same_scene_props(
    shot_rows: list[Any], start_shot_no: int, scene_reference_id: str,
) -> list[dict[str, Any]]:
    """上一段（``start_shot_no - 1``）若与本状态串同属 ``scene_reference_id``
    这个场景，返回它 ``continuity_memo.props``；否则（没有上一段/换了场景）
    返回空列表——宁缺不错。"""
    prev_row = _shot_row_by_no(shot_rows, start_shot_no - 1)
    if prev_row is None:
        return []
    prev_scene_ids = {str(e.get("scene_reference_id") or "") for e in scene_entries_for_shot(prev_row)}
    if scene_reference_id not in prev_scene_ids:
        return []
    return segment_props_for_shot_row(prev_row)


def filter_prop_states_for_empty_room(
    props: list[dict[str, Any]], character_display_names: set[str],
) -> list[dict[str, Any]]:
    """只留 ``location``/``state`` 至少一项非空的条目，且剔除"整条都在讲
    被人带着"的条目：把 location 拆成分句（没有 location 时才看 state），只有
    当**全部**位置分句都点到了本集某个角色显示名（没有任何一句带独立空间
    信息）才算被人拿着/穿着而剔除；只要有一句不提角色名，说明这件物件本身另有独立于人物的
    位置/状态（常见写法是用角色身体部位做相对空间锚点定位物件本身，
    "插头已拔出，在温念右手附近"里"插头已拔出"这一句就是独立信息），
    整条原文保留，不删改、不只删掉提了角色名的那一句（见模块文档
    2026-10-05 返工段落）。"""
    kept: list[dict[str, Any]] = []
    for prop in props:
        location = str(prop.get("location") or "").strip()
        state = str(prop.get("state") or "").strip()
        if not location and not state:
            continue
        # 「是否被人拿着/穿着」只看位置分句：状态分句描述的是物件自身（屏幕亮着、
        # 泡胀了），不提人物名不代表它独立于人物存在——「手机在温念左手；屏幕
        # 亮着」若把状态分句也算进来就会被保留，空房间状态图里多画一部手机。
        # 没有位置文字时才退回看状态分句。
        clauses = _nonempty_clauses(location) if location else _nonempty_clauses(state)
        all_clauses_name_only = clauses and all(
            any(name and name in clause for name in character_display_names) for clause in clauses
        )
        if all_clauses_name_only:
            continue
        kept.append(prop)
    return kept


def _prop_state_fragment(prop: dict[str, Any], card: Any | None) -> str:
    name = str(prop.get("name") or "").strip()
    location = str(prop.get("location") or "").strip()
    state = str(prop.get("state") or "").strip()
    clauses: list[str] = []
    if card is not None:
        clauses.append(f"外观（颜色、材质、款式）按道具卡画：{card.appearance_canonical}")
    if location:
        clauses.append(f"位置：{location}")
    if state:
        clauses.append(f"状态：{state}")
    return f"「{name}」" + "；".join(clauses)


def _prop_card_for_name(name: str, props: list[Any], exclude_ids: set[int]) -> Any | None:
    """按 name 字段去已有道具卡里找同名/别名命中的卡，跳过已经在场景描述那句
    陈述里讲过外观的卡（``exclude_ids``）——同一张卡的外观不重复写两遍。"""
    for card in matched_props_in_text(name, props):
        if id(card) not in exclude_ids:
            return card
    return None


def prop_state_notes_for_description(
    description: str, filtered_props: list[dict[str, Any]], props: list[Any],
) -> str:
    """把过滤后的"上一段道具状态"拼成一句状态图正面陈述；命中道具卡且该卡
    没有被 ``description`` 自己的外观陈述覆盖过时，带上卡面外观，不重复。
    没有可取条目时返回空字符串（指纹/提示词据此保持改动前逐字不变）。"""
    if not filtered_props:
        return ""
    already_covered = {id(card) for card in matched_props_in_text(description, props)}
    fragments = []
    for prop in filtered_props:
        name = str(prop.get("name") or "").strip()
        if not name:
            continue
        card = _prop_card_for_name(name, props, already_covered)
        fragments.append(_prop_state_fragment(prop, card))
    if not fragments:
        return ""
    return "画面里这些物件此刻的位置与状态：" + "；".join(fragments) + "。"


def prop_state_notes_for_run(
    *, shot_rows: list[Any], description: str, start_shot_no: int, scene_reference_id: str,
    props: list[Any], character_display_names: set[str],
) -> str:
    """状态图提示词与指纹共用的唯一入口：扫描侧（``scene_state_views.scan_
    episode_scene_state_needs``）与装配侧（``scene_state_views.resolve_scene_
    state_view_for_shot``）必须都调用这一份函数，不能各自实现一遍——否则两侧
    算出的文字可能不同，指纹也就对不上（见 ``scene_state_views.scene_state_
    input_fingerprint`` docstring）。"""
    raw_props = _previous_segment_same_scene_props(shot_rows, start_shot_no, scene_reference_id)
    filtered = filter_prop_states_for_empty_room(raw_props, character_display_names)
    return prop_state_notes_for_description(description, filtered, props)
