"""准备包资产清单去重：与有参考图角色重名的 functional_extras 并回角色本体。

2026-09-16（龙猫出爪连播第 4、5 集）实测的数据缺陷：同一个实体在一份
asset_manifest 里被登记了两次——``bible:龙猫`` 带 portrait_id 躺在 characters，
同时又以 label「龙猫」躺在 functional_extras（另发了一个 entity: 的
visual_entity_id）。下游因此拿到两套互相矛盾的身份：``storyboard_reference_repair``
把它当群演剥掉 ``@龙猫``，而 ``reference_mention_errors`` 按角色卡要求必须
``@点名``，模型怎么写都过不了；分镜模型自己也被绕晕，把 ``bible:龙猫`` 的
display_name 填成了别名「小龙」。

合并而不是直接删：群演那一份常带着角色条目没有的段号（第 4 集 extras「龙猫」
segment_indexes=[9,12]，角色条目是 [2,4,5,6,7,8,10,11,14]），直接删会把第 9、12
段「这个角色在场」的事实一起删掉。

**判据只取正名与 display_name，不取别名。** 别名池里混着代词——实测第 3、5 集
的 functional_extras 有 label「我」「你」，而多个角色的 aliases 里都登记了它们
（见 memory: 准备包里的代词别名）。按别名合并会把一整串段号灌给一个只是恰好
共用代词的角色，属于「兜底填充制造看似正确实则编造的归属」。别名撞车那一侧的
危害（@别名 被当群演剥掉）已经由 ``storyboard_reference_repair._protected_names``
兜住，不需要在这里冒身份判错的风险。同一个名字对应多个有卡角色时同样不合并——
不确定不绑。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _card_backed_owners(characters: list[Any]) -> dict[str, dict[str, Any]]:
    """正名/display_name → 唯一的有参考图角色条目；一名多主的名字直接排除。"""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for character in characters:
        if not isinstance(character, dict) or not character.get("portrait_id"):
            continue
        names = {
            str(character.get("identity_id") or "").split(":", 1)[-1],
            str(character.get("display_name") or ""),
        }
        for name in {value.strip() for value in names if value and value.strip()}:
            by_name.setdefault(name, []).append(character)
    return {name: owners[0] for name, owners in by_name.items() if len(owners) == 1}


def merge_card_backed_extras(asset_manifest: dict[str, Any]) -> dict[str, Any]:
    """把与有参考图角色同名的群演条目并进该角色并移出清单；原地改并返回同一份清单。

    返回 manifest 本身（而不是合并记录）是为了能包在 ``asset_manifest = ...({...})``
    外面调用：``generate_once._generate_prep_pack_once`` 的函数长度正卡在
    FILE_CONVENTIONS 的 function_lines 基线上，而那条基线是只降不升的棘轮，
    不许为了插一行调用把它调大。合并明细走 log。
    """
    extras = asset_manifest.get("functional_extras") or []
    owners = _card_backed_owners(asset_manifest.get("characters") or [])
    kept: list[Any] = []
    notes: list[str] = []
    for extra in extras:
        label = str((extra or {}).get("label") or "").strip()
        owner = owners.get(label) if label else None
        if owner is None:
            kept.append(extra)
            continue
        before = list(owner.get("segment_indexes") or [])
        added = sorted(set(extra.get("segment_indexes") or []) - set(before))
        owner["segment_indexes"] = sorted(set(before) | set(added))
        notes.append(
            f"群演标签「{label}」与有参考图的角色 {owner.get('identity_id')} 是同一个实体，"
            f"已并入该角色（补入段号 {added or '无'}），不再单独登记为群演"
        )
    if notes:
        asset_manifest["functional_extras"] = kept
        for note in notes:
            log.info("[PREP_PACK_EXTRAS_DEDUP] %s", note)
    return asset_manifest


__all__ = ["merge_card_backed_extras"]
