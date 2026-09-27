"""必需资产缺图不能悄悄放行出片（P0 修复回归）。

取证：生产项目 proj_c89e1d2fa4be《顾念长安》第 1 集，女主温念的定妆照在并行
映射时丢失（``character_portraits`` 只剩 ``ep_start=-1`` 的已作废历史槽位
行），但第 1 集 12 段视频照样 ``succeeded``、温念全程没有带参考图；其中两段
还被供应商以版权理由连续拒收 3 次。

根因链（详见 ``app.video_modes.reference_assemble._build_library_reference_
assets`` 内的修复注释）：``app.multiview.assert_manifest_allows_production``
只逐条目扫描 manifest 并把结果写进 ``meta["asset_manifest_warnings"]``，从不
影响函数返回值；只要 ``select_library_references`` 挑出的候选非空（同段里
另一个角色/场景有图即成立），函数就把"部分装配好的资产"当成功返回——缺图的
必需角色被 ``library_anchor_assets_from_manifest`` 的 continue 与
``character_reference_assets`` 回退分支的"已有一个 character 资产就不再兜底"
双重短路悄悄吞掉，出片时该角色没有任何参考图，且没有任何可见信号。

本文件按 manifest 条目自带的 ``asset_required``/``missing_required``/
``selected_views`` 数据推导（不写角色名单），覆盖三态：

1. 两个 ``asset_required`` 条目一个有图一个没图（人物、场景各验一次）→
   整体判空，交回既有的候选池判空 → 自愈 → 待人工拦截路径
   （``app.media_exec.reference_pool_gate``；该路径的自愈与拦截行为本身已在
   ``tests/test_reference_pool_gate_self_heal.py`` 覆盖，这里只验证
   ``reference_assemble`` 正确把"候选池本该有却没有"这个信号交出去，并且
   guidance 文案确实点名缺口在哪、去哪补）。
2. ``asset_required=False``（群演/一次性人物）缺图 → 不拦，正常出片。
3. 全部必需资产齐全 → 输出与修复前逐字节一致（非空、包含全部条目）。
"""
from __future__ import annotations

import asyncio

from app import multiview as mv
from app.media_exec import reference_pool_gate as rpg
from app.schemas import Bible, Character, Shot, World
from app.video_modes.reference_assemble import _build_library_reference_assets


def _bible(*names: str) -> Bible:
    return Bible(
        characters=[
            Character(name=name, role="lead", appearance_canonical="黑发红裙")
            for name in names
        ],
        world=World(visual_style_canonical="古装言情剧"),
    )


def _shot(*characters: str) -> Shot:
    return Shot(
        shot_no=1, duration_s=5, shot_size="中景", camera_move="固定", scene_setting="室内",
        characters=list(characters), action_desc="人物对话。", first_frame_desc="起幅。",
        last_frame_desc="落幅。", source_excerpt="人物对话。", dialogues=[], transition="硬切",
        continuity_from_prev=False,
    )


def _character_entry(name: str, *, asset_required: bool, image_path: str | None) -> dict:
    """镜像 ``app.multiview._storyboard_pack_asset_dependencies``（生产实际
    走的分镜包分支）的产出形状，不重新发明字段。"""
    usable = bool(image_path)
    selected_view = {
        "id": f"portrait-{name}", "view_role": "front_full", "image_path": image_path or "",
        "input_fingerprint": f"portrait-{name}",
        "purposes": ["keyframe_seed", "qa_anchor", "video_input"],
    } if usable else None
    return {
        "name": name, "identity_id": name, "asset_name": name, "role_kind": "storyboard_pack",
        "asset_required": asset_required,
        "look_revision_id": f"portrait-{name}" if usable else None,
        "pack_status": "ready" if usable else None,
        "selected_view_ids": [f"portrait-{name}"] if usable else [],
        "selected_views": [selected_view] if selected_view else [],
        "available_view_roles": ["front_full"] if usable else [],
        "missing_required": [] if (usable or not asset_required) else ["front_full"],
    }


def _scene_entry(name: str, *, asset_required: bool, image_path: str | None) -> dict:
    """镜像 ``_storyboard_pack_asset_dependencies._resolve_scene_entry``。"""
    usable = bool(image_path)
    selected_view = {
        "id": f"scene-{name}", "view_role": "establishing", "image_path": image_path or "",
        "input_fingerprint": f"scene-{name}",
        "purposes": ["keyframe_seed", "qa_anchor", "video_input"],
    } if usable else None
    return {
        "name": name, "asset_required": asset_required,
        "scene_revision_id": f"scene-{name}" if usable else None,
        "pack_status": "ready" if usable else None,
        "asset_usable": usable, "pack_usable": usable, "primary_usable": usable,
        "selected_view_ids": [f"scene-{name}"] if usable else [],
        "selected_views": [selected_view] if selected_view else [],
        "available_view_roles": ["establishing"] if usable else [],
        "missing_required": [] if (usable or not asset_required) else ["establishing"],
    }


def _manifest(*characters: dict, scene: dict | None = None) -> dict:
    return {
        "episode_no": 1, "shot_id": "shot-1", "characters": list(characters), "scene": scene,
        "additional_scenes": [], "keyframe_slot": "narrative_keyframe", "props": [],
        "input_fingerprint": "fp-test",
    }


def _build(monkeypatch, manifest: dict, *, characters: list[str], meta: dict) -> list:
    monkeypatch.setattr(mv, "resolve_shot_asset_dependencies", lambda **_k: manifest)
    result = asyncio.run(_build_library_reference_assets(
        conn=object(), project_id="proj-c89e1d2fa4be", episode_no=1, episode_id="ep-1",
        shot_id="shot-1", shot=_shot(*characters), bible=_bible(*characters), existing_meta=meta,
    ))
    return result


def test_one_required_character_missing_alongside_one_present_blocks_pool(
    monkeypatch, tmp_path,
) -> None:
    present_image = tmp_path / "luren.jpg"
    present_image.write_bytes(b"jpeg-bytes")
    manifest = _manifest(
        _character_entry("温念", asset_required=True, image_path=None),
        _character_entry("路人", asset_required=True, image_path=str(present_image)),
    )
    meta: dict = {}

    result = _build(monkeypatch, manifest, characters=["温念", "路人"], meta=meta)

    # 核心断言：不能因为"路人"有图就把"温念"缺图悄悄吞掉、以部分成功收尾——
    # 必须整体判空，交回候选池判空/自愈/待人工拦截路径。
    assert result == []
    assert meta["reference_manifest"] is manifest

    blockers = rpg._reference_pool_blockers(meta["reference_manifest"])
    assert any("温念" in b for b in blockers)
    guidance = rpg._reference_repair_guidance(blockers)
    assert "温念" in guidance
    assert "人物谱" in guidance or "场景库" in guidance


def test_one_required_scene_missing_alongside_present_character_blocks_pool(
    monkeypatch, tmp_path,
) -> None:
    present_image = tmp_path / "luren.jpg"
    present_image.write_bytes(b"jpeg-bytes")
    manifest = _manifest(
        _character_entry("路人", asset_required=True, image_path=str(present_image)),
        scene=_scene_entry("长安街市", asset_required=True, image_path=None),
    )
    meta: dict = {}

    result = _build(monkeypatch, manifest, characters=["路人"], meta=meta)

    assert result == []
    blockers = rpg._reference_pool_blockers(meta["reference_manifest"])
    assert any("长安街市" in b for b in blockers)
    guidance = rpg._reference_repair_guidance(blockers)
    assert "长安街市" in guidance


def test_missing_optional_character_does_not_block(monkeypatch, tmp_path) -> None:
    present_image = tmp_path / "luren.jpg"
    present_image.write_bytes(b"jpeg-bytes")
    manifest = _manifest(
        _character_entry("路人甲", asset_required=False, image_path=None),
        _character_entry("路人", asset_required=True, image_path=str(present_image)),
    )
    meta: dict = {}

    result = _build(monkeypatch, manifest, characters=["路人甲", "路人"], meta=meta)

    assert result != []
    assert {a.entity_name for a in result} == {"路人"}
    assert "asset_manifest_warnings" not in meta


def test_all_required_assets_present_output_unchanged(monkeypatch, tmp_path) -> None:
    image_a = tmp_path / "wennian.jpg"
    image_a.write_bytes(b"jpeg-bytes-a")
    image_b = tmp_path / "luren.jpg"
    image_b.write_bytes(b"jpeg-bytes-b")
    manifest = _manifest(
        _character_entry("温念", asset_required=True, image_path=str(image_a)),
        _character_entry("路人", asset_required=True, image_path=str(image_b)),
    )
    meta: dict = {}

    result = _build(monkeypatch, manifest, characters=["温念", "路人"], meta=meta)

    assert {a.entity_name for a in result} == {"温念", "路人"}
    assert "asset_manifest_warnings" not in meta
