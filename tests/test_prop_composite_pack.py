"""道具拼图（超出参考图张数上限时的合成装箱）：`app.video_modes.prop_composite_*`。

覆盖（2026-10-03 派单任务 B，《顾念长安》proj_ca86b15ab7d7 第 1 集真实故障：
第 15/20/22/24 段已有卡且 ready 的道具被参考图张数上限挤掉）：
1. 超限时溢出道具与"最后一个放得下的道具"合成一张拼图，成员全送达且总数 ≤9；
2. 未超限时一张拼图都不生成，字节级结果不变；
3. 含人脸道具不入拼图，保持单张参与正常优先级竞争；
4. 人脸判定失败 fail closed（按有人脸处理，不入拼图）；
5. 参考图说明文本写清拼图成员顺序；
6. 冻结清单（``ReferenceImageAsset.composite_member_fingerprints``）含成员指纹，
   成员变化能被既有 ``manifest_revisions_match`` 围栏识别；
7. 拼图图片本身不调用任何文字绘制（``PIL.ImageDraw.Draw`` 从不被调用）；
8. 拼图条目不污染 ``relatedCharacterIds``（代码评审修复，2026-10-03）；
9. 候选截断用与真正装箱完全同一个 ``ref_pack_priority``（同上）；
10. 尾帧到达后的连续性重装配不会丢失拼图成员信息（同上）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from app import config
from app.multiview import manifest_revisions_match, ref_pack_priority
from app.video_modes import continuity_tail as continuity_tail_mod
from app.video_modes import prop_composite_face_check as face_check_mod
from app.video_modes import prop_composite_pack as pack_mod
from app.video_modes.mode_selection import ReferenceImageAsset
from app.video_modes.prop_composite_image import (
    MAX_PROP_COMPOSITE_MEMBERS,
    build_prop_composite_image,
    composite_grid_dims,
)
from app.video_modes.prop_references import manifest_props_signature
from app.video_modes.seedance_pack import build_seedance_image_inputs
from app.video_modes.seedance_reference_notes import build_seedance_reference_prompt_notes


def _write_png(path, *, size=(1440, 2560), color=(220, 220, 225)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return str(path)


def _prop_asset(label: str, path: str, *, resources_order: int) -> ReferenceImageAsset:
    return ReferenceImageAsset(
        id=f"ref_prop_{label}", url="", path=path, type="prop", source="asset_library",
        entity_type="prop", entity_name=label, relatedCharacterIds=[label],
        selectedForSeedance=True, required=False, resources_order=resources_order,
        purposes=["qa_anchor"],
    )


def _character_asset(name: str) -> ReferenceImageAsset:
    return ReferenceImageAsset(
        id=f"ref_char_{name}", url="", path=f"/fake/{name}.png", type="character",
        source="asset_library", entity_type="character", entity_name=name,
        relatedCharacterIds=[name], selectedForSeedance=True, required=True,
    )


@pytest.fixture(autouse=True)
def _no_real_face_calls(monkeypatch):
    """默认所有候选图都判"无人脸"，避免测试误触发真实模型调用；需要真实
    人脸路径的用例自行覆盖。"""
    async def _fake(_path, *, call_meta=None):
        return False

    monkeypatch.setattr(pack_mod, "prop_image_has_face", _fake)
    yield


# ---------------------------------------------------------------------------
# 1) 超限：溢出道具合成拼图，全部送达且 ≤9
# ---------------------------------------------------------------------------

async def test_overflow_props_merge_into_composite_and_total_stays_within_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    project_id = "proj_overflow"
    # 2 个必需人物 + 7 件道具 = 9 件，超过上限（上限设为 6，便于触发溢出）。
    characters = [_character_asset("温念"), _character_asset("顾屿")]
    prop_labels = ["手机", "浅蓝色碎花长裙", "浅灰色卫衣", "米白色针织开衫", "深灰色围巾", "外套", "大衣"]
    props = []
    for i, label in enumerate(prop_labels):
        path = _write_png(tmp_path / project_id / "prop_refs" / f"{label}.png")
        props.append(_prop_asset(label, path, resources_order=i))
    assets = [*characters, *props]

    merged = await pack_mod.merge_prop_composite_overflow(
        assets, project_id=project_id, max_images=6,
        required_identity_names=["温念", "顾屿"],
    )

    selected = [a for a in merged if a.selectedForSeedance and not a.deleted]
    assert len(selected) <= 6
    composite = next(a for a in selected if a.view_role == "prop_composite")
    # 全部 7 件道具都必须能在"composite 成员 + 仍单独送达的道具"里找到。
    covered = set(composite.composite_member_labels)
    for a in selected:
        if a.type == "prop" and a.view_role != "prop_composite":
            covered.add(a.entity_name)
    assert covered == set(prop_labels)
    assert len(composite.composite_member_fingerprints) == len(composite.composite_member_labels)


# ---------------------------------------------------------------------------
# 2) 未超限：零行为变化
# ---------------------------------------------------------------------------

async def test_no_overflow_returns_assets_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    project_id = "proj_fit"
    path = _write_png(tmp_path / project_id / "prop_refs" / "手机.png")
    assets = [_character_asset("温念"), _prop_asset("手机", path, resources_order=0)]

    merged = await pack_mod.merge_prop_composite_overflow(assets, project_id=project_id, max_images=9)

    assert merged is assets
    assert not any(a.view_role == "prop_composite" for a in merged)


# ---------------------------------------------------------------------------
# 3) 含人脸道具不入拼图，保持单张
# ---------------------------------------------------------------------------

async def test_prop_with_face_stays_standalone_not_in_composite(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    project_id = "proj_face"
    labels = ["手机", "浅蓝色碎花长裙", "浅灰色卫衣", "旧照片"]
    props = []
    for i, label in enumerate(labels):
        path = _write_png(tmp_path / project_id / "prop_refs" / f"{label}.png")
        props.append(_prop_asset(label, path, resources_order=i))
    assets = [_character_asset("温念"), *props]

    async def _face_only_for_old_photo(path, *, call_meta=None):
        return "旧照片" in path

    monkeypatch.setattr(pack_mod, "prop_image_has_face", _face_only_for_old_photo)

    merged = await pack_mod.merge_prop_composite_overflow(
        assets, project_id=project_id, max_images=3,
    )
    composite = next((a for a in merged if a.view_role == "prop_composite"), None)
    assert composite is not None
    assert "旧照片" not in composite.composite_member_labels
    # 有脸的道具原条目必须仍然存在（保持单张，不被拼图悄悄吞掉）。
    old_photo_asset = next(a for a in merged if a.entity_name == "旧照片")
    assert old_photo_asset.view_role != "prop_composite"


# ---------------------------------------------------------------------------
# 4) 人脸判定失败 fail closed
# ---------------------------------------------------------------------------

async def test_face_check_failure_is_fail_closed(monkeypatch, tmp_path):
    async def _boom(*_a, **_k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(face_check_mod, "get_cached_has_face", lambda _h: None)
    monkeypatch.setattr(face_check_mod, "_judge_has_face", _boom)

    path = _write_png(tmp_path / "x.png")
    result = await face_check_mod.prop_image_has_face(path)
    assert result is True


async def test_face_check_cache_hit_skips_model_call(monkeypatch, tmp_path):
    monkeypatch.setattr(face_check_mod, "get_cached_has_face", lambda _h: False)

    async def _boom(*_a, **_k):
        raise AssertionError("命中缓存后不该再发模型调用")

    monkeypatch.setattr(face_check_mod, "_judge_has_face", _boom)
    path = _write_png(tmp_path / "y.png")
    assert await face_check_mod.prop_image_has_face(path) is False


# ---------------------------------------------------------------------------
# 5) 参考图说明文本写清拼图成员顺序
# ---------------------------------------------------------------------------

def test_prompt_note_lists_composite_members_in_order():
    ref = {
        "type": "prop", "view_role": "prop_composite", "path": "/fake/composite.png",
        "composite_member_labels": ["手机", "浅蓝色碎花长裙", "深灰色围巾"],
    }
    text = build_seedance_reference_prompt_notes("正文占位", [ref], aspect_ratio="9:16")
    assert "拼图" in text
    assert "手机、浅蓝色碎花长裙、深灰色围巾" in text
    assert "分格" in text or "白边" in text


# ---------------------------------------------------------------------------
# 6) 冻结清单含成员指纹；成员变化被既有 manifest_revisions_match 围栏识别
# ---------------------------------------------------------------------------

def test_manifest_signature_change_on_any_composite_member_is_detected():
    """拼图本身不需要独立的过期判定：成员的 prop_revision_id 仍按 label 逐一
    走既有 manifest_props_signature/manifest_revisions_match（见
    app.video_modes.prop_composite_pack 模块 docstring"与围栏的关系"一段）。
    这里用拼图实际会包含的两个 label 构造冻结/当前两份 manifest，证明其中
    一个成员的外观卡被重新登记后，整体判定为过期。"""
    frozen = {"props": [
        {"label": "手机", "resources_order": 0, "ready": True, "prop_revision_id": "rev_a"},
        {"label": "浅灰色卫衣", "resources_order": 1, "ready": True, "prop_revision_id": "rev_b"},
    ]}
    current_same = {"props": [
        {"label": "手机", "resources_order": 0, "ready": True, "prop_revision_id": "rev_a"},
        {"label": "浅灰色卫衣", "resources_order": 1, "ready": True, "prop_revision_id": "rev_b"},
    ]}
    current_changed = {"props": [
        {"label": "手机", "resources_order": 0, "ready": True, "prop_revision_id": "rev_a"},
        # 浅灰色卫衣重新登记过外观卡 -> prop_revision_id 换了新值。
        {"label": "浅灰色卫衣", "resources_order": 1, "ready": True, "prop_revision_id": "rev_c"},
    ]}
    assert manifest_props_signature(frozen) == manifest_props_signature(current_same)
    assert manifest_props_signature(frozen) != manifest_props_signature(current_changed)
    assert manifest_revisions_match(
        {"characters": [], "scenes": [], **frozen}, {"characters": [], "scenes": [], **current_same},
    )
    assert not manifest_revisions_match(
        {"characters": [], "scenes": [], **frozen}, {"characters": [], "scenes": [], **current_changed},
    )


# ---------------------------------------------------------------------------
# 7) 拼图图片：布局/封顶常量、内容寻址、不画文字
# ---------------------------------------------------------------------------

def test_composite_grid_dims_three_tiers():
    assert composite_grid_dims(2) == (2, 2)
    assert composite_grid_dims(4) == (2, 2)
    assert composite_grid_dims(5) == (2, 3)
    assert composite_grid_dims(6) == (2, 3)
    assert composite_grid_dims(7) == (3, 3)
    assert composite_grid_dims(MAX_PROP_COMPOSITE_MEMBERS) == (3, 3)


def test_build_prop_composite_image_never_draws_text(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    import PIL.ImageDraw

    def _boom(*_a, **_k):
        raise AssertionError("拼图不得调用任何文字/图形绘制")

    monkeypatch.setattr(PIL.ImageDraw, "Draw", _boom)
    p1 = _write_png(tmp_path / "proj_draw" / "prop_refs" / "a.png", color=(10, 20, 30))
    p2 = _write_png(tmp_path / "proj_draw" / "prop_refs" / "b.png", color=(200, 150, 90))
    out_path, fingerprints = build_prop_composite_image("proj_draw", [("道具A", p1), ("道具B", p2)])
    assert len(fingerprints) == 2
    with open(out_path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"


async def test_excess_over_composite_cap_still_dropped_and_recorded_visibly(tmp_path, monkeypatch):
    """超过 MAX_PROP_COMPOSITE_MEMBERS 的部分不能硬塞进同一张拼图（会挤爆布局），
    必须交回按声明顺序丢弃；但"丢弃"不能是静默的——端到端走
    ``build_seedance_image_inputs`` 验证 ``_seedance_prop_reference_degraded``
    会把它点名。"""
    from app.video_modes.mode_selection import REFERENCE_IMAGE_MODE, REFERENCE_INPUT_POLICY_VERSION

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    project_id = "proj_cap"
    # 默认上限 9：留 9 个在 kept 区占满槽位，再加 10 个溢出道具——锚点 + 10 个
    # 溢出 = 11 个候选，超过 MAX_PROP_COMPOSITE_MEMBERS(9)，必须有 2 个连拼图
    # 都进不去，交回按声明顺序丢弃。
    label_count = 9 + 10
    labels = [f"道具{i:02d}" for i in range(label_count)]
    props = []
    for i, label in enumerate(labels):
        path = _write_png(tmp_path / project_id / "prop_refs" / f"{label}.png")
        props.append(_prop_asset(label, path, resources_order=i))
    assets = list(props)

    merged = await pack_mod.merge_prop_composite_overflow(assets, project_id=project_id)
    composite = next(a for a in merged if a.view_role == "prop_composite")
    assert len(composite.composite_member_labels) == MAX_PROP_COMPOSITE_MEMBERS

    meta = {
        "mode": REFERENCE_IMAGE_MODE,
        "reference_input_policy_version": REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": [a.public_dict() for a in merged],
    }
    out = build_seedance_image_inputs(meta)
    assert len(out) <= 9
    degraded = set(meta.get("_seedance_prop_reference_degraded") or [])
    # 没挤进拼图、也没单独占到槽位的那几件道具必须出现在可见信号里。
    assert degraded, "超过拼图封顶的道具必须被记成可见的降级信号，不能静默消失"
    assert degraded <= set(labels)


def test_build_prop_composite_image_is_content_addressed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    p1 = _write_png(tmp_path / "proj_cache" / "prop_refs" / "a.png")
    p2 = _write_png(tmp_path / "proj_cache" / "prop_refs" / "b.png")
    out1, fp1 = build_prop_composite_image("proj_cache", [("道具A", p1), ("道具B", p2)])
    calls = {"n": 0}
    real_new = Image.new

    def _counting_new(*a, **k):
        calls["n"] += 1
        return real_new(*a, **k)

    import PIL.Image as PILImage
    monkeypatch.setattr(PILImage, "new", _counting_new)
    out2, fp2 = build_prop_composite_image("proj_cache", [("道具A", p1), ("道具B", p2)])
    assert out1 == out2
    assert fp1 == fp2
    assert calls["n"] == 0  # 第二次命中内容寻址缓存，没有重新合成


# ---------------------------------------------------------------------------
# 8) 拼图条目不污染 relatedCharacterIds（代码评审修复）
# ---------------------------------------------------------------------------

async def test_composite_entry_does_not_pollute_related_character_ids(tmp_path, monkeypatch):
    """relatedCharacterIds 在 app.video_modes.seedance_pack._reference_identity_
    names / seedance_reference_notes._related_names 里被无条件当"角色身份名"
    读取；拼图成员是道具标签不是角色身份，混进去会污染必需人物身份覆盖判定
    与 @名字替换表。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    project_id = "proj_no_pollution"
    characters = [_character_asset("温念"), _character_asset("顾屿")]
    prop_labels = ["手机", "浅蓝色碎花长裙", "浅灰色卫衣", "米白色针织开衫", "深灰色围巾", "外套", "大衣"]
    props = []
    for i, label in enumerate(prop_labels):
        path = _write_png(tmp_path / project_id / "prop_refs" / f"{label}.png")
        props.append(_prop_asset(label, path, resources_order=i))

    merged = await pack_mod.merge_prop_composite_overflow(
        [*characters, *props], project_id=project_id, max_images=6,
        required_identity_names=["温念", "顾屿"],
    )
    composite = next(a for a in merged if a.view_role == "prop_composite")
    assert composite.relatedCharacterIds == []
    assert set(composite.composite_member_labels) & set(prop_labels)


# ---------------------------------------------------------------------------
# 9) 候选截断用与真正装箱完全同一个 ref_pack_priority（代码评审修复）
# ---------------------------------------------------------------------------

async def test_collect_composite_members_sorts_by_ref_pack_priority(tmp_path, monkeypatch):
    """resources_order 相同/缺失时，``_collect_composite_members`` 的裁剪顺序
    必须与 ``app.multiview.ref_pack_priority`` 的 tie-break（质量分、id）一致，
    不能只看 resources_order 一级字段。依赖 autouse ``_no_real_face_calls``
    把所有候选都判"无人脸"。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    project_id = "proj_tie_break"
    paths = {}
    for label in ("道具A", "道具B", "道具C"):
        paths[label] = _write_png(tmp_path / project_id / "prop_refs" / f"{label}.png")
    # 三件道具都没有 resources_order（旧数据），质量分不同——真正装箱时
    # ref_pack_priority 按 -质量分 排序，高分应该排前面、更可能被保留为成员。
    refs = [
        {"id": "r_a", "type": "prop", "entity_name": "道具A", "path": paths["道具A"], "qualityScore": 0.2},
        {"id": "r_b", "type": "prop", "entity_name": "道具B", "path": paths["道具B"], "qualityScore": 0.9},
        {"id": "r_c", "type": "prop", "entity_name": "道具C", "path": paths["道具C"], "qualityScore": 0.5},
    ]
    by_id = {
        r["id"]: ReferenceImageAsset(
            id=r["id"], url="", path=r["path"], type="prop", source="asset_library",
            entity_type="prop", entity_name=r["entity_name"], qualityScore=r["qualityScore"],
            selectedForSeedance=True, required=False,
        )
        for r in refs
    }
    members = await pack_mod._collect_composite_members(refs, by_id, call_meta={})
    expected_order = [r["entity_name"] for r in sorted(refs, key=ref_pack_priority)]
    assert [label for _, label, _ in members] == expected_order
    assert expected_order == ["道具B", "道具C", "道具A"]  # -质量分：0.9 > 0.5 > 0.2


# ---------------------------------------------------------------------------
# 10) 尾帧到达后的连续性重装配不丢失拼图成员信息（代码评审修复）
# ---------------------------------------------------------------------------

async def test_continuity_tail_preserves_composite_member_fields_with_path(tmp_path, monkeypatch):
    """``assemble_continuity_tail`` 重建 ``ReferenceImageAsset`` 时（``path``
    分支，走 ``_asset_from_path``）必须像 ``resources_order`` 一样把
    ``composite_member_labels``/``composite_member_fingerprints`` 从原始 ref
    读回来，否则尾帧到达后拼图条目的成员信息会被静默重置成空列表。"""
    async def _passthrough(*, selected, **_kwargs):
        return selected

    def _identity_dedupe(assets):
        return assets

    monkeypatch.setattr(continuity_tail_mod, "_enforce_reference_consistency", _passthrough)
    monkeypatch.setattr(continuity_tail_mod, "_dedupe_assets", _identity_dedupe)

    composite_path = tmp_path / "composite.png"
    composite_path.write_bytes(b"fake-png-bytes")
    ref = {
        "id": "prop_composite_abc", "type": "prop", "view_role": "prop_composite",
        "entity_type": "prop", "entity_name": "道具拼图", "path": str(composite_path),
        "source": "asset_library", "selectedForSeedance": True,
        "composite_member_labels": ["手机", "浅蓝色碎花长裙"],
        "composite_member_fingerprints": ["fp_a", "fp_b"],
    }
    meta = {"reference_images": [ref]}
    shot = SimpleNamespace(shot_no=1)

    selected = await continuity_tail_mod.assemble_continuity_tail(
        conn=None, project_id="proj_tail", episode_no=1, episode_id="ep1", shot_id="shot1",
        shot=shot, bible=None, meta=meta, prev_shot=None,
    )

    composite = next(a for a in selected if a.view_role == "prop_composite")
    assert composite.composite_member_labels == ["手机", "浅蓝色碎花长裙"]
    assert composite.composite_member_fingerprints == ["fp_a", "fp_b"]


async def test_continuity_tail_preserves_composite_member_fields_without_path(tmp_path, monkeypatch):
    """同上，但覆盖没有 ``path``、只有 data URL 的构造分支（``else`` 分支）。"""
    async def _passthrough(*, selected, **_kwargs):
        return selected

    def _identity_dedupe(assets):
        return assets

    monkeypatch.setattr(continuity_tail_mod, "_enforce_reference_consistency", _passthrough)
    monkeypatch.setattr(continuity_tail_mod, "_dedupe_assets", _identity_dedupe)

    ref = {
        "id": "prop_composite_xyz", "type": "prop", "view_role": "prop_composite",
        "entity_type": "prop", "entity_name": "道具拼图",
        "url": "data:image/png;base64,AAAA", "source": "asset_library",
        "selectedForSeedance": True,
        "composite_member_labels": ["卫衣", "围巾"],
        "composite_member_fingerprints": ["fp_c", "fp_d"],
    }
    meta = {"reference_images": [ref]}
    shot = SimpleNamespace(shot_no=1)

    selected = await continuity_tail_mod.assemble_continuity_tail(
        conn=None, project_id="proj_tail", episode_no=1, episode_id="ep1", shot_id="shot1",
        shot=shot, bible=None, meta=meta, prev_shot=None,
    )

    composite = next(a for a in selected if a.view_role == "prop_composite")
    assert composite.composite_member_labels == ["卫衣", "围巾"]
    assert composite.composite_member_fingerprints == ["fp_c", "fp_d"]
