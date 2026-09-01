"""稳定称谓建卡：真名揭晓之前就把视觉身份钉住。

用户诉求（2026-08-31）：有的角色戏份很重，但第一集只给称呼、不给真名，于是
拿不到定妆照，分镜台每个镜头各自想象她长什么样，样貌逐镜漂移；而且这不是第一
集独有——真名揭晓之前的每一集都会重演一次。

为什么"去后文查真名"救不了这一类：实测 ``proj_f8cf2eeb2e66`` 的「许师姐」在
第 1、5、6、8、10、12… 共 67 章逐字出现，而她的真名「许清」在**前 30 章里
一次都没出现**——远在 ``IDENTITY_DISCOVERY_FORWARD_CHAPTERS`` 的前瞻窗口之外，
把窗口放大还会撞上"前瞻只能消歧、不得把后文剧情拉进本集"这条红线。

症结也不在别名机制：``functional_candidate_verdict`` 只能把一个未解析标签绑到
**已经存在**的角色卡上，而第一集人物谱是空的，候选集里根本没有正确答案，必然
落 ``functional_extras`` 当无图群演。缺的是第一张多米诺——先建卡。

判据是纯字符串包含的跨章计数（零语义、零模型调用，与
``true_name.py::_prep_pack_true_name_dossier`` 同一个原语）：标签在全书逐字命中
的章数 > ``PERSISTENT_APPELLATION_MIN_CHAPTERS``。实测这一集的分离度没有灰带——
孟浩 1609 章、许师姐 67 章、王有材 30 章（后两者都是真身份），而一次性描述是
「虎头虎脑的少年」2 章、「绿袍男子」1 章、「白白净净较胖的少年」0 章。

一致性由既有机制接力，本模块不自己实现：
- 建卡时 ``ensure_character_card`` 先走 ``resolve_card_build_or_merge``——如果这
  个称谓其实是人物谱里某人的另一种叫法，登记别名、复用那张卡，不建第二张；
  返回的 ``name`` 是**归属者的规范名**，于是后来的各种代称都收敛到最初那张卡。
- 真名揭晓时走 ``card_rebind.rebind_character_card``，它是
  ``UPDATE character_portraits SET character_name=?`` 原地改名，定妆照那一行不动
  ——从第一集到真名揭晓，用的一直是同一张图。
"""

from __future__ import annotations

from typing import Any

# 用户定的产品口径（2026-08-31）：全书出现超过 2 章就算一个角色，不再当群演。
PERSISTENT_APPELLATION_MIN_CHAPTERS = 2


def label_chapter_span(conn, project_id: str, label: str) -> int:
    """标签在本项目全书逐字命中的章数。零语义：只做字符串包含。"""
    label = str(label or "").strip()
    if not label:
        return 0
    rows = conn.execute(
        "SELECT content FROM chapters WHERE project_id=?", (project_id,),
    ).fetchall()
    return sum(1 for row in rows if label in str(row["content"] or ""))


def label_episode_anchor(segments: Any, label: str) -> dict[str, Any] | None:
    """标签在本集原文里的第一个字面锚点段（1-based 段号 + 该段原文）。

    与候选判别的钉证同形：调用方拿它写 provenance，要求锚点是代码检索出的
    真实原文而不是模型转录。本集里连一次字面出现都没有（标签是模型转述的
    描述短语）就返回 ``None``——钉不住就不建卡，与"不确定不绑"同一套纪律。
    """
    for index, segment in enumerate(segments or [], start=1):
        text = str(getattr(segment, "text", "") or "")
        if label and label in text:
            return {"segment_index": index, "text": text}
    return None


async def resolve_persistent_appellation(
    conn, *, project_id: str, episode_no: int, label: str, segments: Any,
) -> dict[str, Any] | None:
    """跨章稳定的称谓 → 建卡出图，返回可直接并入候选判别结果的 payload。

    ``None`` 表示不适用（跨章次数不够、本集钉不住锚点、建卡没成、或建完仍然
    没有可绑定的定妆照），调用方维持原行为让标签落 functional_extras——不确定
    不绑，与候选判别同一套纪律。
    """
    from app.portraits import ensure_character_card

    from .asset_lookup import _resolve_portrait_id

    if label_chapter_span(conn, project_id, label) <= PERSISTENT_APPELLATION_MIN_CHAPTERS:
        return None
    anchor = label_episode_anchor(segments, label)
    if anchor is None:
        return None
    # require_identity_card：跨章复现已经是"这是个稳定身份"的结构证据，不能
    # 再让模型以"本集戏份少"把它降回路人——那正是漂移的来源。
    result = await ensure_character_card(
        project_id, label, episode_no,
        generate_portrait=True, require_identity_card=True,
    )
    status = str((result or {}).get("status") or "")
    # "exists" 是这个称谓命中了人物谱里已有角色的别名，返回的 name 是归属者的
    # 规范名——正是"后来的代称绑回最初那张卡"。"conflict" 一律不接（同一称呼
    # 命中多个角色是真实存在的合法数据，猜一个就会制造错误归属）。
    if status not in {"added", "exists"}:
        return None
    canonical_name = str((result or {}).get("name") or "").strip() or label
    if not _resolve_portrait_id(conn, project_id, canonical_name, episode_no):
        return None
    return {
        "resolved": True,
        "canonical_name": canonical_name,
        "persistent_appellation": True,
        "segment_index": anchor["segment_index"],
        "text": anchor["text"],
    }
