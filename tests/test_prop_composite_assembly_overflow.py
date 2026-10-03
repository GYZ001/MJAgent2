"""装配阶段（``_build_library_reference_assets``）截断前的道具拼图整合测试。

覆盖 2026-10-03《顾念长安》第 1 集第 10 段线上实测故障：冻结的
``image_inputs.reference_images`` 里 10 条参考（2 人物 + 1 场景 + 7 道具）超过
9 张参考图上限，``select_library_references`` 截断掉最后一件道具（白色陶瓷杯，
``selectedForSeedance=False``）却没有触发拼图——
``app.video_modes.prop_composite_pack.merge_prop_composite_overflow`` 只在
``reference_images`` 冻结之后才被调用，那时收到的候选池已经是
``selectedForSeedance=False`` 的残局，``reference_packing_preview`` 的
``_dedupe_usable`` 只看 ``selectedForSeedance=True``，永远探测不到超限。

与 ``tests/test_prop_composite_pack.py`` 的关键区别：这里走真实装配函数
``_build_library_reference_assets``（+ 真实图片文件 + 真实
``build_seedance_image_inputs``），不是手工构造一份已经全员
``selectedForSeedance=True`` 的候选池。那种手工构造方式天然绕开了"装配阶段
先截断一次"这一步，测试全绿但真实链路不生效——这正是线上故障在
``test_prop_composite_pack.py`` 全部通过的情况下仍然发生的原因。
"""
from __future__ import annotations

import asyncio

from PIL import Image

from app import config
from app import multiview as mv
from app.schemas import Bible, Character, Shot, World
from app.video_modes import prop_composite_pack as pack_mod
from app.video_modes.mode_selection import REFERENCE_IMAGE_MODE, REFERENCE_INPUT_POLICY_VERSION
from app.video_modes.reference_assemble import _build_library_reference_assets
from app.video_modes.seedance_pack import build_seedance_image_inputs

CHARACTERS = ["温念", "顾屿"]
SCENE_NAME = "老街角咖啡馆"
# 顺序与真实第 10 段 resources.props 声明顺序一致：白色陶瓷杯排最后、
# selectedForSeedance=False 最先被截断。
PROP_LABELS = ["手机", "外套", "米白色针织开衫", "浅蓝色碎花长裙", "大衣", "深灰色围巾", "白色陶瓷杯"]


async def _no_face(_path, *, call_meta=None):
    return False


async def _always_face(_path, *, call_meta=None):
    return True


def _write_png(path, *, color=(200, 200, 200)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color).save(path, format="PNG")
    return str(path)


def _bible() -> Bible:
    return Bible(
        characters=[Character(name=name, role="lead", appearance_canonical="黑发") for name in CHARACTERS],
        world=World(visual_style_canonical="都市言情剧"),
    )


def _shot() -> Shot:
    return Shot(
        shot_no=10, duration_s=5, shot_size="中景", camera_move="固定", scene_setting=SCENE_NAME,
        characters=list(CHARACTERS), action_desc="两人对坐喝咖啡。", first_frame_desc="起幅。",
        last_frame_desc="落幅。", source_excerpt="两人对坐喝咖啡。", dialogues=[], transition="硬切",
        continuity_from_prev=False,
    )


def _character_entry(name: str, image_path: str) -> dict:
    return {
        "name": name, "identity_id": name, "asset_name": name, "role_kind": "storyboard_pack",
        "asset_required": True, "look_revision_id": f"portrait-{name}", "pack_status": "ready",
        "selected_view_ids": [f"portrait-{name}"],
        "selected_views": [{
            "id": f"portrait-{name}", "view_role": "front_full", "image_path": image_path,
            "input_fingerprint": f"portrait-{name}", "purposes": ["keyframe_seed", "qa_anchor", "video_input"],
        }],
        "available_view_roles": ["front_full"], "missing_required": [],
    }


def _scene_entry(name: str, image_path: str) -> dict:
    return {
        "name": name, "asset_required": True, "scene_revision_id": f"scene-{name}",
        "pack_status": "ready", "asset_usable": True, "pack_usable": True, "primary_usable": True,
        "selected_view_ids": [f"scene-{name}"],
        "selected_views": [{
            "id": f"scene-{name}", "view_role": "establishing", "image_path": image_path,
            "input_fingerprint": f"scene-{name}", "purposes": ["keyframe_seed", "qa_anchor", "video_input"],
        }],
        "available_view_roles": ["establishing"], "missing_required": [],
    }


def _prop_entry(label: str, order: int, image_path: str) -> dict:
    return {
        "label": label, "ready": True, "image_path": image_path,
        "resources_order": order, "prop_revision_id": f"prop-{label}",
    }


def _manifest(tmp_path, *, prop_labels: list[str] = PROP_LABELS) -> dict:
    characters = [_character_entry(name, _write_png(tmp_path / f"{name}.png")) for name in CHARACTERS]
    scene = _scene_entry(SCENE_NAME, _write_png(tmp_path / "scene.png"))
    props = [
        _prop_entry(label, i, _write_png(tmp_path / f"prop_{i}.png"))
        for i, label in enumerate(prop_labels)
    ]
    return {
        "episode_no": 1, "shot_id": "shot-10", "characters": characters, "scene": scene,
        "additional_scenes": [], "keyframe_slot": "narrative_keyframe", "props": props,
        "input_fingerprint": "fp-ep1-seg10",
    }


def _assemble(monkeypatch, manifest: dict, meta: dict) -> list:
    monkeypatch.setattr(mv, "resolve_shot_asset_dependencies", lambda **_k: manifest)
    return asyncio.run(_build_library_reference_assets(
        conn=object(), project_id="proj-ca86b15ab7d7", episode_no=1, episode_id="ep-1",
        shot_id="shot-10", shot=_shot(), bible=_bible(), existing_meta=meta,
    ))


def _as_submission_meta(meta: dict, assets: list) -> dict:
    meta["mode"] = REFERENCE_IMAGE_MODE
    meta["reference_input_policy_version"] = REFERENCE_INPUT_POLICY_VERSION
    meta["reference_images"] = [a.public_dict() for a in assets]
    return meta


# ---------------------------------------------------------------------------
# 1) 超限：装配阶段截断前先拼图，全部声明道具最终都送达，≤9 张
# ---------------------------------------------------------------------------

def test_assembly_overflow_composites_dropped_prop_and_delivers_all(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(pack_mod, "prop_image_has_face", _no_face)
    meta: dict = {}
    manifest = _manifest(tmp_path)

    assets = _assemble(monkeypatch, manifest, meta)
    _as_submission_meta(meta, assets)

    out = build_seedance_image_inputs(meta)
    assert len(out) <= 9

    refs = meta["reference_images"]
    composite = next((r for r in refs if r.get("view_role") == "prop_composite"), None)
    assert composite is not None, "超限必须在装配阶段触发拼图，不能静默丢弃道具"
    assert composite["selectedForSeedance"] is True
    assert "白色陶瓷杯" in composite["composite_member_labels"]

    standalone = {
        r.get("entity_name") for r in refs
        if r.get("type") == "prop" and r.get("selectedForSeedance") and r.get("view_role") != "prop_composite"
    }
    covered = standalone | set(composite["composite_member_labels"])
    assert covered == set(PROP_LABELS), "本段声明的全部道具都必须以单张或拼图成员身份送达"
    assert not meta.get("_seedance_prop_reference_degraded")


# ---------------------------------------------------------------------------
# 2) 人脸判定 fail closed：找不到可用锚点，不拼图；被丢的道具必须可见
# ---------------------------------------------------------------------------

def test_face_present_everywhere_skips_composite_and_records_degradation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(pack_mod, "prop_image_has_face", _always_face)
    meta: dict = {}
    manifest = _manifest(tmp_path)

    assets = _assemble(monkeypatch, manifest, meta)
    _as_submission_meta(meta, assets)
    build_seedance_image_inputs(meta)

    refs = meta["reference_images"]
    assert not any(r.get("view_role") == "prop_composite" for r in refs)
    degraded = set(meta.get("_seedance_prop_reference_degraded") or [])
    assert "白色陶瓷杯" in degraded, "被装配阶段截掉又拼不成图的道具必须留下可见的降级信号"


# ---------------------------------------------------------------------------
# 3) 未超限：零行为变化
# ---------------------------------------------------------------------------

def test_no_overflow_leaves_assembly_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(pack_mod, "prop_image_has_face", _no_face)
    meta: dict = {}
    # 2 人物 + 1 场景 + 3 道具 = 6 <= 9，装配阶段不触发任何拼图逻辑。
    manifest = _manifest(tmp_path, prop_labels=PROP_LABELS[:3])

    assets = _assemble(monkeypatch, manifest, meta)

    assert not any(a.view_role == "prop_composite" for a in assets)
    prop_names = {a.entity_name for a in assets if a.entity_type == "prop"}
    assert prop_names == set(PROP_LABELS[:3])
    assert all(a.selectedForSeedance for a in assets)
