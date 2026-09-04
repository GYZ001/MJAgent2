"""app.production.storyboard_prop_assets -- 道具外观/参考图接入素材清单（P2）。

覆盖：manifest["props"][].appearance 按世界书 Prop.name/aliases 逐字命中
（未命中写占位说明，同角色/场景先例）；ref_image_path 只在 app.props 返回
ready 行且文件真实存在时才带；_segment_relevant_assets（storyboard_pack.py）
过滤后仍带着这两个字段（因为它只是筛选已 enrich 过的 manifest，不重新查询）。

app.props 尚未落地（WS-P1 并行开发），全部通过 monkeypatch
storyboard_prop_assets._prop_reference_lookup 打桩，不依赖真实模块存在。
"""
from __future__ import annotations

from app.production import storyboard_prop_assets as prop_assets
from app.production.storyboard_pack import (
    _enrich_asset_manifest_canonical_visuals,
    _segment_relevant_assets,
)
from app.schemas import Bible


def _manifest_with_prop(label: str = "旧猫包", segment_indexes: list[int] | None = None) -> dict:
    return {
        "asset_manifest": {
            "characters": [], "scenes": [], "functional_extras": [],
            "props": [
                {"label": label, "description": "一只破旧的猫包", "segment_indexes": segment_indexes or [1]},
            ],
        },
    }


class _FakeProp:
    def __init__(self, name: str, appearance_canonical: str, aliases: list[str] | None = None) -> None:
        self.name = name
        self.appearance_canonical = appearance_canonical
        self.aliases = aliases or []


class _FakeBible:
    def __init__(self, props: list[_FakeProp]) -> None:
        self.props = props
        # storyboard_pack._enrich_asset_manifest_canonical_visuals 无条件读
        # bible.characters/scenes（bible 非空时），伪造对象需要一并满足。
        self.characters = []
        self.scenes = []


def test_enrich_prop_manifest_entries_matches_bible_prop_name_verbatim() -> None:
    manifest = _manifest_with_prop()["asset_manifest"]
    bible = _FakeBible([_FakeProp("旧猫包", "灰色网状帆布猫包，边角磨损露出内衬。")])
    prop_assets.enrich_prop_manifest_entries(None, manifest, bible=bible)
    assert manifest["props"][0]["appearance"] == "灰色网状帆布猫包，边角磨损露出内衬。"


def test_enrich_prop_manifest_entries_matches_bible_prop_alias_verbatim() -> None:
    manifest = _manifest_with_prop(label="猫包")["asset_manifest"]
    bible = _FakeBible([_FakeProp("旧猫包", "灰色网状帆布猫包。", aliases=["猫包", "破猫包"])])
    prop_assets.enrich_prop_manifest_entries(None, manifest, bible=bible)
    assert manifest["props"][0]["appearance"] == "灰色网状帆布猫包。"


def test_enrich_prop_manifest_entries_notes_no_canonical_appearance_when_unmatched() -> None:
    manifest = _manifest_with_prop(label="不存在的道具")["asset_manifest"]
    bible = _FakeBible([_FakeProp("旧猫包", "灰色网状帆布猫包。")])
    prop_assets.enrich_prop_manifest_entries(None, manifest, bible=bible)
    assert "没有为这个道具建立标准外观" in manifest["props"][0]["appearance"]


def test_enrich_prop_manifest_entries_no_bible_falls_back_to_note() -> None:
    manifest = _manifest_with_prop()["asset_manifest"]
    prop_assets.enrich_prop_manifest_entries(None, manifest, bible=None)
    assert "没有为这个道具建立标准外观" in manifest["props"][0]["appearance"]


def test_enrich_prop_manifest_entries_real_bible_without_props_input_does_not_crash() -> None:
    # 输入 dict 不带 props 键时 Bible.props 靠 default_factory 落到空列表；
    # 本函数用 getattr 兜底同一结果，双保险确保字段缺失不会阻断分镜生成
    # （即便未来 Bible.props 又变回可选/缺省，这里也不会因 AttributeError 炸）。
    bible = Bible.model_validate({
        "characters": [], "world": {"era": "", "genre": "", "visual_style_canonical": "水墨"},
    })
    assert getattr(bible, "props", None) == []
    manifest = _manifest_with_prop()["asset_manifest"]
    prop_assets.enrich_prop_manifest_entries(None, manifest, bible=bible)
    assert "没有为这个道具建立标准外观" in manifest["props"][0]["appearance"]


def test_enrich_prop_manifest_entries_adds_ref_image_path_when_ready(monkeypatch, tmp_path) -> None:
    image = tmp_path / "prop.jpg"
    image.write_bytes(b"fake-jpeg")
    monkeypatch.setattr(
        prop_assets, "_prop_reference_lookup",
        lambda conn, project_id, name, episode_no: {"status": "ready", "image_path": str(image), "appearance": "x"},
    )
    manifest = _manifest_with_prop()["asset_manifest"]
    prop_assets.enrich_prop_manifest_entries(
        object(), manifest, bible=None, project_id="proj-1", episode_no=3,
    )
    assert manifest["props"][0]["ref_image_path"] == str(image)


def test_enrich_prop_manifest_entries_skips_ref_image_path_when_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        prop_assets, "_prop_reference_lookup",
        lambda conn, project_id, name, episode_no: {"status": "pending", "image_path": "", "appearance": ""},
    )
    manifest = _manifest_with_prop()["asset_manifest"]
    prop_assets.enrich_prop_manifest_entries(
        object(), manifest, bible=None, project_id="proj-1", episode_no=3,
    )
    assert "ref_image_path" not in manifest["props"][0]


def test_enrich_prop_manifest_entries_skips_ref_image_path_when_lookup_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(prop_assets, "_prop_reference_lookup", lambda *a, **k: None)
    manifest = _manifest_with_prop()["asset_manifest"]
    prop_assets.enrich_prop_manifest_entries(
        object(), manifest, bible=None, project_id="proj-1", episode_no=3,
    )
    assert "ref_image_path" not in manifest["props"][0]


def test_enrich_prop_manifest_entries_skips_lookup_when_project_id_missing(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("不该在缺 project_id 时发起查询")

    monkeypatch.setattr(prop_assets, "_prop_reference_lookup", _boom)
    manifest = _manifest_with_prop()["asset_manifest"]
    prop_assets.enrich_prop_manifest_entries(object(), manifest, bible=None, project_id=None, episode_no=3)
    assert "ref_image_path" not in manifest["props"][0]


def test_enrich_prop_manifest_entries_skips_ref_image_path_when_file_missing_on_disk(monkeypatch) -> None:
    monkeypatch.setattr(
        prop_assets, "_prop_reference_lookup",
        lambda conn, project_id, name, episode_no: {
            "status": "ready", "image_path": "/nonexistent/prop.jpg", "appearance": "",
        },
    )
    manifest = _manifest_with_prop()["asset_manifest"]
    prop_assets.enrich_prop_manifest_entries(
        object(), manifest, bible=None, project_id="proj-1", episode_no=3,
    )
    assert "ref_image_path" not in manifest["props"][0]


def test_enrich_asset_manifest_canonical_visuals_wires_prop_enrichment(monkeypatch, tmp_path) -> None:
    """端到端确认 storyboard_pack._enrich_asset_manifest_canonical_visuals 真的
    调用了 enrich_prop_manifest_entries（不是各写各的、没接上）。"""
    image = tmp_path / "prop.jpg"
    image.write_bytes(b"fake-jpeg")
    monkeypatch.setattr(
        prop_assets, "_prop_reference_lookup",
        lambda conn, project_id, name, episode_no: {"status": "ready", "image_path": str(image), "appearance": "x"},
    )
    payload = {
        "episode_no": 7,
        "asset_manifest": {
            "characters": [], "scenes": [], "functional_extras": [],
            "props": [{"label": "旧猫包", "description": "破猫包", "segment_indexes": [1]}],
        },
    }
    bible = _FakeBible([_FakeProp("旧猫包", "灰色网状帆布猫包。")])
    _enrich_asset_manifest_canonical_visuals(object(), payload, bible=bible, project_id="proj-9")
    prop = payload["asset_manifest"]["props"][0]
    assert prop["appearance"] == "灰色网状帆布猫包。"
    assert prop["ref_image_path"] == str(image)


def test_segment_relevant_assets_carries_prop_appearance_and_ref_image_path() -> None:
    payload = {
        "asset_manifest": {
            "characters": [], "scenes": [], "functional_extras": [],
            "props": [
                {
                    "label": "旧猫包", "description": "破猫包", "segment_indexes": [1, 2],
                    "appearance": "灰色网状帆布猫包。", "ref_image_path": "/tmp/prop.jpg",
                },
                {"label": "不相关道具", "description": "x", "segment_indexes": [9]},
            ],
        },
        "appellation_map": [],
    }
    relevant = _segment_relevant_assets(payload, [1])
    assert len(relevant["props"]) == 1
    prop = relevant["props"][0]
    assert prop["appearance"] == "灰色网状帆布猫包。"
    assert prop["ref_image_path"] == "/tmp/prop.jpg"
