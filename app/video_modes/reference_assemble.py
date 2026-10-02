"""参考资产一致性校验与基于图库素材的整体装配、对外统一入口 build_reference_assets。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.schemas import Bible, EpisodeScreenplay, Shot

from .asset_lookup import _asset_from_path, character_reference_assets, scene_reference_assets
from .mode_selection import (
    REFERENCE_INPUT_POLICY_VERSION,
    ReferenceImageAsset,
    ShotVideoModeDecision,
    _dedupe_str,
    max_reference_images,
)
from .seedance_pack import _dedupe_assets



async def _enforce_reference_consistency(*, selected: list[ReferenceImageAsset], shot: Shot, bible: Bible,
                                         project_id: str, episode_no: int,
                                         rejection_details: list[dict[str, Any]] | None = None,
                                         rejected_out: list[ReferenceImageAsset] | None = None,
                                         screenplay: EpisodeScreenplay | None = None,
                                         ) -> list[ReferenceImageAsset]:
    """VLM 参考图一致性质检已下线：不再跨候选比对锚点、不再触发漂移重生。

    技术产物存在即可用——已生成的候选原样放行，去留交给人工在成片里判断。
    保留全部形参与返回类型不变，使调用方无需改动；``rejection_details``/
    ``rejected_out`` 不再被写入（没有一致性检查就没有可报告的一致性拒绝理由）。
    """
    del project_id, episode_no, rejection_details, rejected_out, shot, bible, screenplay
    return selected


def select_library_references(
    assets: list[ReferenceImageAsset], identity_names: list[str], max_images: int,
) -> list[ReferenceImageAsset]:
    """人物 > 场景 > 道具挑参考图，同类内部按 ``role_priority``/画质排序；
    ``max_images`` 硬顶——超出时道具最先被舍弃（P2「道具外观一致性」拍板：
    道具是新加的第三类，永远排在人物与场景之后选取；人物/场景的既有排序与
    去重行为原样保留，只是从 ``_build_library_reference_assets`` 内联搬出来，
    让那个函数腾出预算接住道具这第三类分支，见该函数调用处）。

    场景一栏按 ``entity_name`` 去重、逐个收纳，不是只挑一张——多场景转场段
    的 ``additional_scenes`` 各自建立镜与本段被点名的反打视角（entity_name
    带「·反打」后缀，见 ``app.scene_reverse.segment_views.scene_anchor_
    entity_name``）互为不同名字，天然都能进来；单场景且没有反打点名时
    ``ordered`` 里只有一个 scene 资产，行为与改动前逐条相同。
    """
    role_priority = {
        "front_full": 0,
        "action_zone": 0, "establishing": 1, "reverse_angle": 2,
    }
    kind_rank = {"character": 0, "scene": 1, "prop": 2}

    def _rank(asset: ReferenceImageAsset) -> tuple[int, float | int, str]:
        # 2026-10-01：道具档第一键改用 resources_order（本段 resources.props 的
        # 声明顺序），不靠 path 字典序——同一道闸的真实故障（第 20 段 2 人物 +
        # 场景 + 8 道具已超 9 张上限，这一道比 app.multiview.ref_pack_priority
        # 更早截断）；role_priority 对道具恒为占位默认值 9（道具没有 view_role），
        # 换成 order_key 不丢既有信息。没有 resources_order 的旧数据排最后，
        # 组内仍按原有 path or id 排序，行为逐字不变，见
        # app.video_modes.prop_references 模块 docstring。
        kind = str(asset.entity_type or asset.type)
        if kind == "prop":
            order = asset.resources_order
            order_key = order if order is not None else float("inf")
            return (kind_rank.get(kind, 9), order_key, asset.path or asset.id)
        return (kind_rank.get(kind, 9), role_priority.get(str(asset.view_role or ""), 9), asset.path or asset.id)

    ordered = sorted(assets, key=_rank)
    selected: list[ReferenceImageAsset] = []
    selected_names: set[str] = set()
    for asset in ordered:
        if len(selected) >= max_images:
            break
        if (asset.entity_type or asset.type) != "character":
            continue
        name = str(asset.entity_name or "").strip()
        if identity_names and name not in identity_names:
            continue
        key = name or "|".join(asset.relatedCharacterIds)
        if key in selected_names:
            continue
        selected_names.add(key)
        selected.append(asset)
    scene_keys_seen: set[str] = set()
    for asset in ordered:
        if len(selected) >= max_images or (asset.entity_type or asset.type) != "scene":
            continue
        key = str(asset.entity_name or "").strip() or asset.path or asset.id
        if key in scene_keys_seen:
            continue
        scene_keys_seen.add(key)
        selected.append(asset)
    prop_names: set[str] = set()
    for asset in ordered:
        if len(selected) >= max_images or (asset.entity_type or asset.type) != "prop":
            continue
        name = str(asset.entity_name or "").strip()
        if name and name not in prop_names:
            prop_names.add(name)
            selected.append(asset)
    return selected


async def _build_library_reference_assets(
    *,
    conn: Any,
    project_id: str,
    episode_no: int,
    episode_id: str,
    shot_id: str,
    shot: Shot,
    bible: Bible,
    on_progress: Callable[
        [list[ReferenceImageAsset], list[ReferenceImageAsset]], None
    ] | None = None,
    existing_meta: dict[str, Any] | None = None,
    screenplay: EpisodeScreenplay | None = None,
) -> list[ReferenceImageAsset]:
    """Resolve existing character/scene-library images without generating media."""
    from app.continuity import effective_characters_visible
    from app.multiview import (
        PURPOSE_QA_ANCHOR,
        PURPOSE_VIDEO_INPUT,
        assert_manifest_allows_production,
        library_anchor_assets_from_manifest,
        resolve_shot_asset_dependencies,
    )

    meta = existing_meta if existing_meta is not None else {}
    scene_name = str(getattr(shot, "scene_name", "") or "").strip()
    visible_names = effective_characters_visible(shot)
    bible_names = {character.name for character in bible.characters}
    if screenplay is not None and screenplay.narrative_plan is not None:
        from app.identity_contracts import narrative_identity_resolver

        resolver = narrative_identity_resolver(bible, screenplay)
        identity_names = list(dict.fromkeys(
            identity.asset_name
            for identity in (
                resolver.resolve(name, usage="visual") for name in visible_names
            )
            if identity.allows_asset
        ))
    else:
        identity_names = [name for name in visible_names if name in bible_names]

    manifest = resolve_shot_asset_dependencies(
        project_id=project_id,
        episode_no=episode_no,
        shot_id=shot_id,
        shot=shot,
        scene_name=scene_name or None,
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    warnings = assert_manifest_allows_production(manifest)
    if warnings:
        meta["asset_manifest_gate_retry_exhausted"] = True
        meta["asset_manifest_warnings"] = list(warnings)

    assets: list[ReferenceImageAsset] = []
    for anchor in library_anchor_assets_from_manifest(manifest):
        entity_type = str(anchor.get("entity_type") or anchor.get("type") or "")
        # "prop"（P2 新增）与既有 character/scene 同走 asset_library 血缘；
        # related_character_ids 字段名沿用既有命名，但对 prop 同样承载
        # entity_name——这是 seedance_reference_notes._related_names 识别
        # "@道具名" 并替换成 "@图片N" 的唯一入口，缺了它道具的 @ 引用会
        # 原样漏进正文。
        if entity_type not in {"character", "scene", "prop"}:
            continue
        path = str(anchor.get("image_path") or "").strip()
        if not path or not Path(path).is_file():
            continue
        try:
            assets.append(_asset_from_path(
                path=path,
                ref_type=entity_type,
                source="asset_library",
                related_character_ids=(
                    [str(anchor.get("entity_name"))]
                    if entity_type in {"character", "prop"} and anchor.get("entity_name")
                    else None
                ),
                qa={"status": "library", "overall": None, "issues": []},
                entity_type=entity_type,
                entity_name=anchor.get("entity_name"),
                library_revision_id=anchor.get("library_revision_id"),
                library_view_id=anchor.get("library_view_id"),
                view_role=anchor.get("view_role"),
                purposes=[PURPOSE_QA_ANCHOR],
                resources_order=anchor.get("resources_order"),
                # costume_mode 此前只接到了单图回退分支（asset_lookup.character_
                # reference_assets），没接进这条 2.x 分镜包主通路——不传会让按段
                # 选图（character_look_selection）的「只锁长相」文案形同虚设。
                costume_mode=anchor.get("costume_mode"),
            ))
        except OSError:
            continue

    if not any(asset.entity_type == "character" for asset in assets):
        assets.extend(character_reference_assets(
            bible, identity_names, limit=max(1, len(identity_names)),
            project_id=project_id, episode_no=episode_no, shot=shot,
        ))
    # manifest 场景段按状态不一致主动省略了参考图（scene_state_omitted_reason，
    # 见 app.video_modes.scene_state_selection）时不能走这条回退——回退按场景名
    # 直接查图库，不认那个省略决定，会把本该不发的旧状态图原样塞回来。
    scene_state_omitted = bool((manifest.get("scene") or {}).get("scene_state_omitted_reason"))
    if not any(asset.entity_type == "scene" for asset in assets) and not scene_state_omitted:
        assets.extend(scene_reference_assets(
            bible,
            scene_name,
            project_id=project_id,
            episode_no=episode_no,
        ))
    assets = [
        asset for asset in _dedupe_assets(assets)
        if (asset.entity_type or asset.type) in {"character", "scene", "prop"} and asset.source == "asset_library"
    ]

    selected = select_library_references(assets, identity_names, max_reference_images())
    selected_ids = {id(asset) for asset in selected}
    for asset in assets:
        asset.shotId = shot_id
        asset.episodeId = episode_id
        asset.required = id(asset) in selected_ids
        asset.selectedForSeedance = id(asset) in selected_ids
        asset.purposes = _dedupe_str([
            *(asset.purposes or []),
            PURPOSE_QA_ANCHOR,
            *([PURPOSE_VIDEO_INPUT] if id(asset) in selected_ids else []),
        ])

    meta.update({
        "reference_input_policy_version": REFERENCE_INPUT_POLICY_VERSION,
        "reference_manifest": manifest,
        "reference_manifest_frozen": True,
        "reference_slots": {},
        "keyframe_sequence": {"beats": [], "beat_count": 0},
        "narrative_keyframe_missing": False,
    })
    for key in (
        "keyframe_fallback_mode",
        "keyframe_structural_fallback_slots",
        "keyframe_contract_fingerprint",
    ):
        meta.pop(key, None)
    if on_progress is not None:
        on_progress(list(assets), [])
    # P0 修复：``warnings``（= assert_manifest_allows_production 对本镜 manifest
    # 的判定）逐条目标出"asset_required=True 却缺必需视角"的人物/场景——之前只
    # 写进 meta 供事后排查，从不影响返回值，于是同段里只要另一个角色的图在，
    # selected 就非空，缺图角色被这里的 continue（library_anchor_assets_from_
    # manifest 里跳过它、character_reference_assets 回退分支又因为"已有一个
    # character 资产"被短路）静默漏掉，出片时该角色直接没有参考图。数据推导、
    # 不写名单：manifest 每个条目自带 asset_required/missing_required/
    # selected_views，warnings 就是逐条目扫描的结果，不是新发明的规则。warnings
    # 非空时整体判空，交回调用方既有的候选池判空 → 自愈 → 待人工拦截路径
    # （app.media_exec.reference_pool_gate.finish_reference_mode_without_assets，
    # 该路径已经会先按 blockers 指向的人物/场景各自动补生成一次，仍缺才拦），
    # 而不是以"缺一个必需角色"悄悄收尾成功。全齐（warnings 为空）时行为不变。
    return [] if warnings else (assets if selected else [])


async def build_reference_assets(*, conn: Any, project_id: str, episode_no: int, episode_id: str,
                                 shot_id: str, shot: Shot, bible: Bible,
                                 decision: ShotVideoModeDecision, prev_shot: Any | None = None,
                                 rejection_details: list[dict[str, Any]] | None = None,
                                 rejected_out: list[ReferenceImageAsset] | None = None,
                                 on_progress: Callable[
                                     [list[ReferenceImageAsset], list[ReferenceImageAsset]], None
                                 ] | None = None,
                                 allow_missing_continuity_tail: bool = False,
                                 job_id: str | None = None,
                                 existing_meta: dict[str, Any] | None = None,
                                 screenplay: EpisodeScreenplay | None = None) -> list[ReferenceImageAsset]:
    return await _build_library_reference_assets(
        conn=conn,
        project_id=project_id,
        episode_no=episode_no,
        episode_id=episode_id,
        shot_id=shot_id,
        shot=shot,
        bible=bible,
        on_progress=on_progress,
        existing_meta=existing_meta,
        screenplay=screenplay,
    )
