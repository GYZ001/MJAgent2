"""超出参考图张数上限时，把本应丢弃的道具拼成一张图顶替一个槽位的装箱编排。

背景：参考图总数硬顶 ``max_reference_images()``；超限时道具永远排最后被丢
（``app.multiview.ref_pack_priority``，道具档是 tier 4，只排在风格/未知类型
之前）。与其让已经有图的道具静默消失（真实故障：第 1 集第 15/20/22/24 段道
具被挤掉），把"本应被丢弃的道具"与"最后一个放得下的单张道具"合成一张拼图
顶替后者原来占的那一个槽位，让本段列出的 ready 道具全部送达——见
``app.video_modes.prop_composite_image``（拼图本身怎么拼）与
``app.video_modes.prop_composite_face_check``（为什么要先判人脸）。

本模块只做编排：用 ``reference_packing_preview``（与真正装箱同一套排序，见
该模块 docstring）探测超限、挑出可合成的成员、调用图像合成、把结果重新接回
``ReferenceImageAsset`` 列表。调用时机是"冻结参考池"那一刻（``app.media_exec.
input_reference``/``reference_pool_gate`` 里 ``meta["reference_images"]`` 从
``assets`` 定稿的那几处，均已是 async 上下文），只跑一次；之后无论是立即生
成 prompt 说明还是在真正派发时重新装箱，都在读这份已经包含拼图条目的冻结
池，天然幂等——不需要每次装箱都重新判人脸、重新合成。

与"围栏"（``app.multiview.manifest_revisions_match``）的关系：拼图发生在
``reference_manifest`` 解析完成之后，manifest 里每个道具条目的
``prop_revision_id``（见 ``app.video_modes.prop_references``）已经按 label 逐一
跟踪过期；任一成员的源图被重新登记都会让该 label 的 revision 变化，继而让
manifest 判定为过期并触发整段重新装配（含重新拼图）——不需要为拼图再加一套
独立的过期判定。``composite_member_fingerprints`` 仍然写进冻结的
``reference_images``（``ReferenceImageAsset.composite_member_fingerprints``），
只作为人工核查"这张拼图当时到底拼了哪几张图"的可见记录。

两个互不重叠的入口（2026-10-03 修复线上实测故障后新增第二个）：
``merge_prop_composite_overflow`` 是旧入口，操作 dict 化的候选池、靠
``reference_packing_preview``/``ref_pack_priority`` 探测超限，只服务"冻结之后
还会再截断一次"的路径（尾帧到达后的连续性重装配，``app.video_modes.
continuity_tail.assemble_continuity_tail`` 会把全部技术有效资产重新标回
``selectedForSeedance=True``，真正的截断推迟到供应商提交前）。
``composite_overflow_props`` 是新入口，直接操作 ``ReferenceImageAsset``
对象、按 ``app.video_modes.reference_assemble.select_library_references``
同一规则探测超限，服务装配阶段本身（``_build_library_reference_assets`` 在
真正调用 ``select_library_references`` 截断之前调用）——那层截断发生在
``merge_prop_composite_overflow`` 能看到候选之前，旧入口对它天生不可见
（截断完的道具已经是 ``selectedForSeedance=False``，``reference_packing_
preview`` 的 ``_dedupe_usable`` 直接把它们过滤掉，永远探测不到超限）。
两个入口不能合并：各自的"超限判据"函数本身不同（``select_library_
references`` vs ``ref_pack_priority``），削峰时机也不同。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .mode_selection import ReferenceImageAsset
from .prop_composite_face_check import prop_image_has_face
from .prop_composite_image import MAX_PROP_COMPOSITE_MEMBERS, build_prop_composite_image, composite_fingerprint
from .reference_packing_preview import reference_packing_preview

_Member = tuple[dict[str, Any], str, str]  # (ref_dict, label, path)


async def _resolve_composite_slot(
    kept: list[dict[str, Any]], by_id: dict[str, ReferenceImageAsset], *, call_meta: dict[str, Any],
) -> dict[str, Any] | None:
    """从已装箱、未超限的那部分里由后向前找一个可以让出槽位的道具——必须
    没有人脸（有脸的道具不能进拼图，只能继续占它自己的单张槽位）。找不到
    返回 ``None``，表示这一轮没有可用槽位，溢出道具维持原样被丢弃。"""
    for ref in reversed(kept):
        if str(ref.get("type") or "") != "prop":
            continue
        asset = by_id.get(str(ref.get("id") or ""))
        path = asset.path if asset else None
        if not path or not Path(path).is_file():
            continue
        if not await prop_image_has_face(path, call_meta=call_meta):
            return ref
    return None


async def _collect_composite_members(
    candidates: list[dict[str, Any]], by_id: dict[str, ReferenceImageAsset], *, call_meta: dict[str, Any],
) -> list[_Member]:
    """过滤掉含人脸/文件缺失的候选，按声明顺序（``resources_order``）排好，
    封顶 ``MAX_PROP_COMPOSITE_MEMBERS``——仍放不下的交回调用方按既有优先级
    丢弃（``app.video_modes.seedance_pack._dropped_prop_labels`` 记可见信号）。
    """
    members: list[_Member] = []
    for ref in candidates:
        asset = by_id.get(str(ref.get("id") or ""))
        path = asset.path if asset else None
        if not path or not Path(path).is_file():
            continue
        if await prop_image_has_face(path, call_meta=call_meta):
            continue
        label = str(ref.get("entity_name") or "").strip() or "道具"
        members.append((ref, label, path))
    from app.multiview import ref_pack_priority  # 函数内导入：与真正装箱用完全同一个优先级函数，避免两套排序对不上（见 _collect_composite_members 调用处 docstring）

    members.sort(key=lambda item: ref_pack_priority(item[0]))
    return members[:MAX_PROP_COMPOSITE_MEMBERS]


def _apply_composite(
    assets: list[ReferenceImageAsset], project_id: str, members: list[_Member],
) -> list[ReferenceImageAsset]:
    composite_path, fingerprints = build_prop_composite_image(
        project_id, [(label, path) for _, label, path in members],
    )
    merged_ids = {str(ref.get("id") or "") for ref, _, _ in members}
    for asset in assets:
        if asset.id in merged_ids:
            # 原条目让位给拼图，但保留在列表里供审计（与既有「QA 淘汰图保留
            # video_input 用途供审计」同一取舍），只是不再参与装箱竞争。
            asset.selectedForSeedance = False
    labels = [label for _, label, _ in members]
    composite = ReferenceImageAsset(
        id="prop_composite_" + composite_fingerprint(fingerprints)[:20],
        url="", path=composite_path, type="prop", source="asset_library",
        entity_type="prop", entity_name="道具拼图",
        # 不写 relatedCharacterIds：该字段在 app.video_modes.seedance_pack.
        # _reference_identity_names / seedance_reference_notes._related_names
        # 里被当作"角色身份名"集合无条件读取（不按 entity_type 过滤），拼图
        # 成员是道具标签不是角色身份，混进去会污染必需人物身份覆盖判定与
        # @名字替换表；成员名只走 composite_member_labels。
        qa={"status": "asset_library_composite", "overall": None, "issues": []},
        purposes=["qa_anchor", "keyframe_seed"],
        selectedForSeedance=True, required=False,
        view_role="prop_composite",
        resources_order=members[0][0].get("resources_order"),
        composite_member_labels=labels,
        composite_member_fingerprints=fingerprints,
    )
    return [*assets, composite]


async def merge_prop_composite_overflow(
    assets: list[ReferenceImageAsset], *, project_id: str,
    max_images: int | None = None, required_identity_names: list[str] | None = None,
) -> list[ReferenceImageAsset]:
    """超限时把本应丢弃的道具与最后一个放得下的道具合成一张拼图；未超限或
    超限但不涉及道具时原样返回（零行为变化，见模块/派单对「不改变未超限
    段落行为」的要求）。调用方须在 ``meta["reference_images"]`` 定稿前调用
    一次（见模块 docstring 的"冻结时机"一段）。
    """
    if not assets:
        return assets
    refs = [a.public_dict() for a in assets]
    eligible, limit = reference_packing_preview(
        refs, max_images=max_images, required_identity_names=required_identity_names,
    )
    if len(eligible) <= limit:
        return assets
    overflow_props = [r for r in eligible[limit:] if str(r.get("type") or "") == "prop"]
    if not overflow_props:
        return assets
    by_id = {a.id: a for a in assets}
    call_meta = {"project_id": project_id}
    anchor = await _resolve_composite_slot(eligible[:limit], by_id, call_meta=call_meta)
    if anchor is None:
        return assets
    members = await _collect_composite_members([anchor, *overflow_props], by_id, call_meta=call_meta)
    if len(members) < 2:
        return assets
    return _apply_composite(assets, project_id, members)


# ---------------------------------------------------------------------------
# 装配阶段入口：直接操作 ReferenceImageAsset 对象，不经过 dict/by_id 间接层
# （那层是给上面 merge_prop_composite_overflow 的 dict 候选池用的；装配阶段
# 的候选池本来就是对象，调用方 app.video_modes.reference_assemble._apply_
# prop_composite_overflow 已经用 select_library_references 算好了"谁会被
# 丢"，这里只负责"能不能拼、拼成什么样"，见模块 docstring 两个入口一段）。
# ---------------------------------------------------------------------------

_AssemblyMember = tuple[ReferenceImageAsset, str, str]  # (asset, label, path)


async def _resolve_assembly_anchor(
    candidates: list[ReferenceImageAsset], *, call_meta: dict[str, Any],
) -> ReferenceImageAsset | None:
    """从已入选候选池里由后向前找一个可以让出槽位的道具——必须没有人脸。
    找不到返回 ``None``，交回调用方按原有规则丢弃。"""
    for asset in reversed(candidates):
        if (asset.entity_type or asset.type) != "prop":
            continue
        if not asset.path or not Path(asset.path).is_file():
            continue
        if not await prop_image_has_face(asset.path, call_meta=call_meta):
            return asset
    return None


async def _collect_assembly_members(
    candidates: list[ReferenceImageAsset], *, call_meta: dict[str, Any],
) -> list[_AssemblyMember]:
    """过滤含人脸/文件缺失的候选，按 ``resources_order``（与
    ``select_library_references`` 的道具排序同一把尺子，不是
    ``ref_pack_priority``——这里的"谁会被丢"已经由调用方用那个函数算好，
    拼图内部顺序不能另起一套）排好，封顶 ``MAX_PROP_COMPOSITE_MEMBERS``。"""
    members: list[_AssemblyMember] = []
    for asset in candidates:
        if not asset.path or not Path(asset.path).is_file():
            continue
        if await prop_image_has_face(asset.path, call_meta=call_meta):
            continue
        label = str(asset.entity_name or "").strip() or "道具"
        members.append((asset, label, asset.path))
    members.sort(key=lambda item: item[0].resources_order if item[0].resources_order is not None else float("inf"))
    return members[:MAX_PROP_COMPOSITE_MEMBERS]


def _build_assembly_composite(
    anchor: ReferenceImageAsset, project_id: str, members: list[_AssemblyMember],
) -> ReferenceImageAsset:
    composite_path, fingerprints = build_prop_composite_image(
        project_id, [(label, path) for _, label, path in members],
    )
    labels = [label for _, label, _ in members]
    return ReferenceImageAsset(
        id="prop_composite_" + composite_fingerprint(fingerprints)[:20],
        url="", path=composite_path, type="prop", source="asset_library",
        entity_type="prop", entity_name="道具拼图",
        # 不写 relatedCharacterIds：理由与 _apply_composite 同一处注释一致。
        qa={"status": "asset_library_composite", "overall": None, "issues": []},
        purposes=["qa_anchor", "keyframe_seed"],
        selectedForSeedance=True, required=False,
        view_role="prop_composite",
        resources_order=anchor.resources_order,
        composite_member_labels=labels,
        composite_member_fingerprints=fingerprints,
    )


async def composite_overflow_props(
    *, project_id: str, dropped_props: list[ReferenceImageAsset],
    anchor_candidates: list[ReferenceImageAsset],
) -> tuple[ReferenceImageAsset, set[int]] | None:
    """装配阶段截断前的拼图入口，唯一调用方见
    ``app.video_modes.reference_assemble._apply_prop_composite_overflow``。

    ``dropped_props``：按 ``select_library_references`` 同一规则算出的"会被
    丢弃"的道具；``anchor_candidates``：那次选取实际入选的候选池（从后往前
    找没有人脸的单张道具让出槽位）。返回 ``(拼图资产, 被并入的成员 object id
    集合)``；找不到可用锚点、或成员不足两张时返回 ``None``，交回调用方按原有
    规则丢弃。不修改任何输入列表。
    """
    call_meta = {"project_id": project_id}
    anchor = await _resolve_assembly_anchor(anchor_candidates, call_meta=call_meta)
    if anchor is None:
        return None
    members = await _collect_assembly_members([anchor, *dropped_props], call_meta=call_meta)
    if len(members) < 2:
        return None
    composite = _build_assembly_composite(anchor, project_id, members)
    merged_ids = {id(member_asset) for member_asset, _, _ in members}
    return composite, merged_ids
