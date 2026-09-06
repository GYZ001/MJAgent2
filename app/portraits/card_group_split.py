"""多人合称拆成成员卡（2026-09-06 用户拍板：「两个老者」不建合称卡也不丢群像，按原文能否区分成员拆）。

第 6 轮人物谱里出现了一张画着两位老者的「两个老者」卡：合称被当成一个人建卡定妆，
视频参考图上一张图两个人，谁也锚不住。用户否决了「合称只进群像不建卡」——他们有台词
有正面戏份，需要各自的视觉锚点。落地规则：

* 判定阶段模型给 ``subject_kind=group`` 并按原文列出 ``members``，每个成员的 ``source_label``
  必须逐字出现在给模型看过的原文片段里（``verbatim_member_labels`` 机械核验，编造的称呼
  一律丢弃）——「穿着灰色长袍的高大老者」里的「高大老者」是合法成员称呼。
* 合称本身不建卡；每个成员走一遍正常的 ``ensure_character_card``（自己的原文片段、自己的
  判定、自己的定妆），并把合称作为 ``identity_source_labels`` 传入——既有的
  ``card_aliases.new_card_aliases`` 只在合称与成员称呼同章共现时才登记为非独占别名，
  不确定不登记。
* 原文不区分成员（members 为空）的合称仍走 ``skipped_not_person`` 那条路，留在群像/群演里。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

CHARACTER_SUBJECT_GROUP = "group"


def verbatim_member_labels(raw_members: Any, fragments: str) -> list[str]:
    """模型申报的成员称呼里，只保留非空、去重、且逐字出现在原文片段里的；顺序照申报。"""
    text = str(fragments or "")
    labels: list[str] = []
    for item in raw_members if isinstance(raw_members, list) else []:
        label = str((item.get("source_label") if isinstance(item, dict) else item) or "").strip()
        if label and label in text and label not in labels:
            labels.append(label)
    return labels


async def split_group_members(
    gate_result: dict, project_id: str, from_episode_no: int, *,
    ensure: Callable[..., Awaitable[dict]], generate_portrait: bool,
    write_guard: Callable[[], None] | None,
) -> dict:
    """把 ``skipped_group`` 判定展开成逐个成员建卡；返回带成员结果的汇总（status=split_group）。

    ``ensure`` 由调用方传入（就是 ``cards.ensure_character_card`` 自己），避免与 cards.py 成环。
    成员的 ``identity_source_labels`` 带上合称，让合称按既有共现闸登记为成员卡的共享别名。"""
    group_label = str(gate_result.get("name") or "").strip()
    member_results: list[dict] = []
    for label in gate_result.get("members") or []:
        if label == group_label:
            continue
        result = await ensure(
            project_id, label, from_episode_no, generate_portrait=generate_portrait,
            write_guard=write_guard, identity_source_labels=[group_label],
        )
        member_results.append({"label": label, **{k: result.get(k) for k in ("status", "name", "reason")}})
    return {
        "status": "split_group", "name": group_label, "reason": gate_result.get("reason"),
        "members": member_results,
        "added_members": [m["name"] for m in member_results if m.get("status") == "added"],
    }
