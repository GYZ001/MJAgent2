"""分镜台持久化侧的称谓正名替换与旁白过滤（WS2-B），以及群演描述性措辞归并
（WS12，见 ``app.production.storyboard_extras_reconcile`` 模块 docstring）。

背景：``persist_storyboard_pack`` 把段落自报的 ``resources.characters[].
identity_id`` 直接投影成 ``shots.characters``（经 ``_resource_identity_
display_names`` 反查 asset_manifest 拿展示名，查不到就原样回退成 identity_id
本身）。当分镜台模型自己没能把"少年/球员/八岁男孩"这类叙述向称谓映到正确
identity_id 时（即使映射台已经通过 ``app.production.prep_pack.
appellation_resolve`` 把它们解析进了 asset_manifest），这条投影会直接把裸
称谓当成 identity_id 写进 ``shots.characters``——真实案例：跑不快的孩子 ep2
shot1，``characters`` 里出现的是原文称谓（"少年"/"球员"/"旁白"），不是
"里奥"。

本模块是这条投影链路上的一道正名闸：候选 identity_id 依次尝试
asset_manifest.characters（已注册）、appellation_map（映射台已判定"这个
称谓就是谁"）、functional_extras（WS12 结构性归并，见下），不猜测——三道都
落空就原样保留原值（诚实，不吞、不编）；"旁白"是画外叙述声音，结构性排除，
永远不进 characters（CLAUDE.md「User-Facing Behavior」：界面承诺必须与实际
行为一致，不得把画外音当画面里的人）。

层号：随 ``app.production`` 包前缀归 L4（app/LAYERS.toml），只依赖同层/更低层
的 ``app.schemas``。
"""
from __future__ import annotations

from typing import Any

from app.production.storyboard_extras_reconcile import reconcile_persisted_extra_ids
from app.schemas import is_narrator_label


def resolve_persisted_character_ids(
    payload: dict[str, Any],
    identity_ids: list[str],
    *,
    segment_source_indexes: list[int],
) -> tuple[list[str], list[str]]:
    """段落 ``resources.characters[].identity_id`` 的持久化前正名替换。

    - 已经是 ``asset_manifest.characters`` 里注册过的 identity_id：原样保留；
    - 不是，但字面命中 ``appellation_map`` 某条 ``raw_mention``（映射台已经
      判定这个称谓归属谁）：替换成该行记录的 ``identity_id``；
    - 是旁白（``is_narrator_label``）：整条丢弃，旁白从不是画面里的人；
    - 前两道都落空：尝试对 ``asset_manifest.functional_extras`` 做 WS12
      结构性归并（段落范围重叠 + label 逐字互为子串，两个条件同时满足才
      归并，多候选记一条可见 note、不猜）；
    - 归并也落空：原样保留——查不清就是查不清，不得猜一个值出来。

    ``segment_source_indexes`` 是必传参数（不给默认值）：这一镜对应的原文
    段号范围是 WS12 归并判据的一半，漏传会让归并静默退化成"永远不命中"，
    比显式要求调用方传值更危险（CLAUDE.md「Ownership Must Be Explicit」）。

    返回 ``(resolved_ids, notes)``——``notes`` 只在 WS12 归并遇到歧义时非空，
    调用方负责把它们接进 ``degraded_capabilities`` 之类的可见位置。
    """
    manifest_ids = {
        str(c.get("identity_id") or "")
        for c in (payload.get("asset_manifest") or {}).get("characters") or []
    }
    appellation_by_raw = {
        str(row.get("raw_mention") or ""): str(row.get("identity_id") or "")
        for row in payload.get("appellation_map") or []
        if row.get("raw_mention") and row.get("identity_id")
    }
    functional_extras = (payload.get("asset_manifest") or {}).get("functional_extras") or []

    # resolved[i] is None until settled; None never survives to the return
    # value (every non-narrator branch below assigns a str, including the
    # WS12 fallback which — worst case — echoes the raw id back unchanged).
    resolved: list[str | None] = []
    pending: list[tuple[int, str]] = []
    for raw_id in identity_ids:
        if is_narrator_label(raw_id):
            resolved.append(None)
            continue
        if raw_id in manifest_ids:
            resolved.append(raw_id)
        elif raw_id in appellation_by_raw:
            resolved.append(appellation_by_raw[raw_id])
        else:
            pending.append((len(resolved), raw_id))
            resolved.append(None)

    notes: list[str] = []
    if pending:
        merged_ids, merge_notes = reconcile_persisted_extra_ids(
            [raw_id for _, raw_id in pending], segment_source_indexes, functional_extras,
        )
        notes.extend(merge_notes)
        for (position, _raw_id), merged_id in zip(pending, merged_ids):
            resolved[position] = merged_id

    return [value for value in resolved if value is not None], notes


__all__ = ["resolve_persisted_character_ids"]
