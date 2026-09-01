"""app.domain.storyboard_ops.current_scene_refs：映射台/分镜台/生成台给场景
资产条目挂上"当前实际会用的那张"场景图。

真实事故（proj_f8cf2eeb2e66 EP1，2026-09-01）：场景库三张图都出好了，映射台
的"出场场景"仍一律显示"场景图待生成"，硬刷新也不变——前端只能拿产物里固化的
scene_reference_id 快照去场景库查图，而出图解耦到后台后映射跑完那一刻快照恒为
null，于是这条展示路径永远查不到东西。修法与角色侧对称：后端按场景名现查。
"""
from __future__ import annotations

from app import config
import app.domain.storyboard_ops.current_scene_refs as current_scene_refs_module
from app.domain.storyboard_ops.current_scene_refs import (
    attach_current_scene_references,
)


def _stub_scene_row(monkeypatch, table: dict[tuple[str, int], dict]) -> list[tuple[str, int]]:
    """把 scene_row_for_episode 换成纯查表桩：key=(scene_name, episode_no)。
    返回记录调用的列表，供去重断言使用。"""
    calls: list[tuple[str, int]] = []

    def fake(project_id, name, episode_no, *, conn=None):
        del project_id, conn
        calls.append((name, episode_no))
        return table.get((name, episode_no))

    monkeypatch.setattr(current_scene_refs_module, "scene_row_for_episode", fake)
    return calls


def _sandbox_image(monkeypatch, tmp_path, filename: str) -> str:
    """build_media_url 只认 config.PROJECTS_DIR 之下确实存在的文件，其它一律
    判 None（"这个资源还没生成"的既有语义）——沙盒到 tmp_path 才能出真链接。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    image = tmp_path / "proj_1" / "scene_refs" / filename
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"fake")
    return str(image)


def test_attach_resolves_when_snapshot_scene_reference_id_is_null(monkeypatch, tmp_path) -> None:
    """出图解耦到后台后，映射那一刻快照 scene_reference_id 必然是 null；挂当前
    场景图不得以快照非空为门槛，否则图出好了界面也永远停在"场景图待生成"。"""
    image_path = _sandbox_image(monkeypatch, tmp_path, "peak.jpg")
    _stub_scene_row(monkeypatch, {
        ("赵国大青山山顶", 1): {"id": "scene_late", "image_path": image_path},
    })
    detail = {
        "project_id": "proj_1",
        "episode_no": 1,
        "prep_pack": {
            "asset_manifest": {
                "scenes": [{
                    "scene_id": "scene:赵国大青山山顶",
                    "display_name": "赵国大青山山顶",
                    "scene_reference_id": None,
                }],
            },
        },
        "shots": [],
    }
    attach_current_scene_references(detail, "script")
    scene = detail["prep_pack"]["asset_manifest"]["scenes"][0]
    # 快照是溯源信息，必须原样保留——不许被"当前"覆盖掉。
    assert scene["scene_reference_id"] is None
    assert scene["current_scene_reference_id"] == "scene_late"
    assert scene["current_scene_image_url"] is not None


def test_attach_updates_per_shot_resources_and_dedupes_lookups(monkeypatch, tmp_path) -> None:
    image_path = _sandbox_image(monkeypatch, tmp_path, "crack.jpg")
    calls = _stub_scene_row(monkeypatch, {
        ("大青山半山裂缝", 3): {"id": "scene_crack", "image_path": image_path},
    })
    detail = {
        "project_id": "proj_1",
        "episode_no": 3,
        "prep_pack": None,
        "shots": [
            {"storyboard_pack_segment": {"resources": {"scenes": [
                {"scene_id": "scene:大青山半山裂缝", "scene_reference_id": "scene_old"},
            ]}}},
            {"storyboard_pack_segment": {"resources": {"scenes": [
                {"scene_id": "scene:大青山半山裂缝", "scene_reference_id": "scene_old"},
            ]}}},
        ],
    }
    attach_current_scene_references(detail, "board")
    for shot in detail["shots"]:
        scene = shot["storyboard_pack_segment"]["resources"]["scenes"][0]
        assert scene["scene_reference_id"] == "scene_old"
        assert scene["current_scene_reference_id"] == "scene_crack"
    # 同一场景在多个段里重复出现，只应该真正查询一次（缓存命中，不是每段都打 DB）。
    assert calls == [("大青山半山裂缝", 3)]


def test_attach_leaves_none_when_resolution_misses(monkeypatch) -> None:
    """解析不到时两个字段都必须是 None（前端据此显示"场景图待生成"），不得回退
    填成 scene_reference_id 快照。"""
    _stub_scene_row(monkeypatch, {})
    detail = {
        "project_id": "proj_1",
        "episode_no": 1,
        "prep_pack": {"asset_manifest": {"scenes": [
            {"scene_id": "scene:失踪场景", "scene_reference_id": "scene_old"},
        ]}},
        "shots": [],
    }
    attach_current_scene_references(detail, "script")
    scene = detail["prep_pack"]["asset_manifest"]["scenes"][0]
    assert scene["scene_reference_id"] == "scene_old"
    assert scene["current_scene_reference_id"] is None
    assert scene["current_scene_image_url"] is None


def test_attach_drops_the_id_too_when_the_image_file_is_gone(monkeypatch, tmp_path) -> None:
    """登记了行、图却已不在盘上：不得只给 id 让前端以为有图——两个字段一起判空。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    _stub_scene_row(monkeypatch, {
        ("靠山宗半山青石坪", 1): {
            "id": "scene_gone", "image_path": str(tmp_path / "proj_1" / "scene_refs" / "gone.jpg"),
        },
    })
    detail = {
        "project_id": "proj_1",
        "episode_no": 1,
        "prep_pack": {"asset_manifest": {"scenes": [{"scene_id": "scene:靠山宗半山青石坪"}]}},
        "shots": [],
    }
    attach_current_scene_references(detail, "script")
    scene = detail["prep_pack"]["asset_manifest"]["scenes"][0]
    assert scene["current_scene_reference_id"] is None
    assert scene["current_scene_image_url"] is None


def test_attach_ignores_scene_ids_without_the_prefix(monkeypatch) -> None:
    """scene_id 不带 scene: 前缀就不猜场景名，也不查库——只写空值，不编造。"""
    calls = _stub_scene_row(monkeypatch, {})
    detail = {
        "project_id": "proj_1",
        "episode_no": 1,
        "prep_pack": {"asset_manifest": {"scenes": [{"scene_id": "", "display_name": "无 id 场景"}]}},
        "shots": [],
    }
    attach_current_scene_references(detail, "script")
    assert calls == []
    assert detail["prep_pack"]["asset_manifest"]["scenes"][0]["current_scene_image_url"] is None


def test_attach_is_a_noop_without_project_id(monkeypatch) -> None:
    calls = _stub_scene_row(monkeypatch, {})
    detail = {"episode_no": 1, "prep_pack": None, "shots": []}
    attach_current_scene_references(detail, "cinema")
    assert calls == []
