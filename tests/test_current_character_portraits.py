"""app.domain.storyboard_ops.current_portraits：映射台/分镜台/生成台给
人物谱身份的角色资产条目挂上"当前实际会用的那张"定妆照，供前端渲染缩略图、
并在快照与当前不同时提示"已更新"。真实事故：proj_1fce17f77010「景田」，
映射台/分镜台读已发布产物里固化的 portrait_id 快照，与生成时实际选中的
那张不同（19:55 那张旧的，而不是 20:25 用户重做后的新的）。

第二起真实事故（proj_f8cf2eeb2e66 EP1，2026-09-01）：出图解耦到后台之后，
映射跑完那一刻快照 portrait_id 恒为 null，而这里旧版拿快照当挂字段的门槛，
于是人物谱三个角色的定妆照早就出好了，映射台仍一律显示"定妆照待生成"，
硬刷新也不变——见下面 test_attach_resolves_when_snapshot_portrait_id_is_null。
"""
from __future__ import annotations

from app import config
import app.domain.storyboard_ops.current_portraits as current_portraits_module
from app.domain.storyboard_ops.current_portraits import (
    attach_current_character_portraits,
)


def _stub_current_portrait_ref(monkeypatch, table: dict[tuple[str, str], dict]) -> None:
    """把 current_portrait_ref 换成一个纯查表桩：key=(character_name, episode_no)。"""

    def fake(project_id, name, episode_no, *, visual_entity_id=None):
        del project_id, visual_entity_id
        return table.get((name, episode_no))

    monkeypatch.setattr(current_portraits_module, "current_portrait_ref", fake)


def test_attach_updates_prep_pack_characters_without_touching_frozen_portrait_id(monkeypatch, tmp_path) -> None:
    # build_media_url 只认 config.PROJECTS_DIR 之下的路径，其它一律判 None
    # （"这个资源还没生成"的既有语义）——沙盒到 tmp_path 才能让 _media_url 出真链接。
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    new_image = tmp_path / "proj_1" / "refs" / "new.jpg"
    new_image.parent.mkdir(parents=True)
    new_image.write_bytes(b"fake")
    _stub_current_portrait_ref(monkeypatch, {
        ("景田", 1): {"portrait_id": "portrait_new", "image_path": str(new_image)},
    })
    detail = {
        "project_id": "proj_1",
        "episode_no": 1,
        "prep_pack": {
            "asset_manifest": {
                "characters": [
                    {"identity_id": "bible:景田", "display_name": "景田", "portrait_id": "portrait_old"},
                ],
            },
        },
        "shots": [],
    }
    attach_current_character_portraits(detail, "script")
    character = detail["prep_pack"]["asset_manifest"]["characters"][0]
    # 快照 id 是溯源信息，必须原样保留——不许被"当前"覆盖掉。
    assert character["portrait_id"] == "portrait_old"
    assert character["current_portrait_id"] == "portrait_new"
    assert character["current_portrait_image_url"] is not None


def test_attach_updates_per_shot_storyboard_resources_and_dedupes_lookups(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake(project_id, name, episode_no, *, visual_entity_id=None):
        del project_id, visual_entity_id
        calls.append((name, episode_no))
        return {"portrait_id": "portrait_new", "image_path": "/data/new.jpg"}

    monkeypatch.setattr(current_portraits_module, "current_portrait_ref", fake)
    detail = {
        "project_id": "proj_1",
        "episode_no": 3,
        "prep_pack": None,
        "shots": [
            {"storyboard_pack_segment": {"resources": {"characters": [
                {"identity_id": "bible:景田", "portrait_id": "portrait_old"},
            ]}}},
            {"storyboard_pack_segment": {"resources": {"characters": [
                {"identity_id": "bible:景田", "portrait_id": "portrait_old"},
            ]}}},
        ],
    }
    attach_current_character_portraits(detail, "wall")
    for shot in detail["shots"]:
        character = shot["storyboard_pack_segment"]["resources"]["characters"][0]
        assert character["portrait_id"] == "portrait_old"
        assert character["current_portrait_id"] == "portrait_new"
    # 同一身份在多个段里重复出现，只应该真正查询一次（缓存命中，不是每段都打 DB）。
    assert calls == [("景田", 3)]


def test_attach_leaves_none_when_current_resolution_misses_not_a_fallback_to_snapshot(monkeypatch) -> None:
    """解析不到时必须原样是 None（前端据此显示"无定妆照"），不得回退填成
    portrait_id 快照，也不得编造任何图。"""
    _stub_current_portrait_ref(monkeypatch, {})
    detail = {
        "project_id": "proj_1",
        "episode_no": 1,
        "prep_pack": {
            "asset_manifest": {
                "characters": [
                    {"identity_id": "bible:失踪角色", "display_name": "失踪角色", "portrait_id": "portrait_old"},
                ],
            },
        },
        "shots": [],
    }
    attach_current_character_portraits(detail, "script")
    character = detail["prep_pack"]["asset_manifest"]["characters"][0]
    assert character["portrait_id"] == "portrait_old"
    assert character["current_portrait_id"] is None
    assert character["current_portrait_image_url"] is None


def test_attach_resolves_when_snapshot_portrait_id_is_null(monkeypatch, tmp_path) -> None:
    """出图解耦到后台后，映射那一刻快照 portrait_id 必然是 null（卡刚建、图还
    没出）。挂当前定妆照的门槛只能是"这是不是人物谱身份"，不能是快照非空——
    否则图出好了界面也永远停在"定妆照待生成"。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    image = tmp_path / "proj_1" / "refs" / "late.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fake")
    _stub_current_portrait_ref(monkeypatch, {
        ("孟浩", 1): {"portrait_id": "portrait_late", "image_path": str(image)},
    })
    detail = {
        "project_id": "proj_1",
        "episode_no": 1,
        "prep_pack": {
            "asset_manifest": {
                "characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩", "portrait_id": None}],
            },
        },
        "shots": [],
    }
    attach_current_character_portraits(detail, "script")
    character = detail["prep_pack"]["asset_manifest"]["characters"][0]
    assert character["portrait_id"] is None
    assert character["current_portrait_id"] == "portrait_late"
    assert character["current_portrait_image_url"] is not None


def test_attach_skips_identities_outside_the_bible(monkeypatch) -> None:
    """群演/未收录称谓（identity_id 不带 bible: 前缀）没有定妆照是设计使然：
    不触发任何解析调用，也不写这两个字段（前端据前缀显示"无定妆照"）。"""
    calls: list[tuple[str, int]] = []

    def fake(project_id, name, episode_no, *, visual_entity_id=None):
        del project_id, visual_entity_id
        calls.append((name, episode_no))
        return None

    monkeypatch.setattr(current_portraits_module, "current_portrait_ref", fake)
    detail = {
        "project_id": "proj_1",
        "episode_no": 1,
        "prep_pack": {
            "asset_manifest": {
                "characters": [{"identity_id": "entity:绿袍男子", "display_name": "绿袍男子", "portrait_id": None}],
            },
        },
        "shots": [],
    }
    attach_current_character_portraits(detail, "script")
    character = detail["prep_pack"]["asset_manifest"]["characters"][0]
    assert "current_portrait_id" not in character
    assert calls == []


def test_attach_is_a_noop_without_project_id(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        current_portraits_module, "current_portrait_ref",
        lambda *a, **kw: calls.append(a) or None,
    )
    detail = {"episode_no": 1, "prep_pack": None, "shots": []}
    attach_current_character_portraits(detail, "cinema")
    assert calls == []
