"""app.domain.storyboard_ops.resource_labels：段落资源清单挂人类可读名字。

真实事故（proj_f8cf2eeb2e66 EP1，2026-09-01）：生成台"本段涉及素材"里群演显示成
``entity:ee1fb41c79e4e33d`` 一串哈希——资源清单只存内部键，可读名字在映射包的
``functional_extras[].label`` 里，展示侧从来没去查。
"""
from __future__ import annotations

import json

import app.domain.storyboard_ops.resource_labels as resource_labels
from app.domain.storyboard_ops.resource_labels import attach_resource_display_names

_MANIFEST = {
    "prep_pack_version": "2.0.4",
    "asset_manifest": {
        "characters": [
            {"identity_id": "bible:孟浩", "display_name": "孟浩",
             "visual_entity_id": "bible:孟浩"},
            {"identity_id": "bible:银袍女子", "display_name": "李慕婉",
             "display_appellation": "银袍女子", "visual_entity_id": "bible:银袍女子"},
        ],
        "functional_extras": [
            {"label": "虎头虎脑的少年", "visual_entity_id": "entity:ee1fb41c79e4e33d"},
        ],
        "scenes": [
            {"scene_id": "scene:赵国大青山山顶", "display_name": "赵国大青山山顶"},
        ],
    },
}


def _stub_episode_pack(monkeypatch, payload: dict | None) -> list[str]:
    """把"按 episode_id 读映射包"换成纯查表桩，记录被查的分集 id。"""
    asked: list[str] = []

    class _Conn:
        def execute(self, _sql, params):
            asked.append(params[0])

            class _Cursor:
                def fetchone(self):
                    return {"screenplay_json": json.dumps(payload) if payload else None}
            return _Cursor()

    monkeypatch.setattr(resource_labels, "get_conn", lambda: _Conn())
    return asked


def _detail_with(entries: dict) -> dict:
    return {"id": "ep_1", "shots": [{"storyboard_pack_segment": {"resources": entries}}]}


def test_functional_extra_shows_its_label_instead_of_the_entity_hash(monkeypatch) -> None:
    _stub_episode_pack(monkeypatch, _MANIFEST)
    detail = _detail_with({"characters": [{"identity_id": "entity:ee1fb41c79e4e33d"}], "scenes": []})
    attach_resource_display_names(detail, "wall")
    character = detail["shots"][0]["storyboard_pack_segment"]["resources"]["characters"][0]
    assert character["display_name"] == "虎头虎脑的少年"
    # 原始 id 必须原样保留：它是溯源键，界面把它放进 title。
    assert character["identity_id"] == "entity:ee1fb41c79e4e33d"


def test_named_character_uses_this_episode_appellation_first(monkeypatch) -> None:
    """本集称谓优先于全局正名——展示不得提前剧透还没揭晓的真名。"""
    _stub_episode_pack(monkeypatch, _MANIFEST)
    detail = _detail_with({"characters": [{"identity_id": "bible:银袍女子"}], "scenes": []})
    attach_resource_display_names(detail, "board")
    assert detail["shots"][0]["storyboard_pack_segment"]["resources"]["characters"][0][
        "display_name"] == "银袍女子"


def test_scene_gets_its_display_name(monkeypatch) -> None:
    _stub_episode_pack(monkeypatch, _MANIFEST)
    detail = _detail_with({"characters": [], "scenes": [{"scene_id": "scene:赵国大青山山顶"}]})
    attach_resource_display_names(detail, "board")
    assert detail["shots"][0]["storyboard_pack_segment"]["resources"]["scenes"][0][
        "display_name"] == "赵国大青山山顶"


def test_unknown_id_gets_no_invented_name(monkeypatch) -> None:
    """映射包里查不到就不挂——不得按哈希编一个名字（"不得兜底填充"）。"""
    _stub_episode_pack(monkeypatch, _MANIFEST)
    detail = _detail_with({"characters": [{"identity_id": "entity:deadbeefdeadbeef"}], "scenes": []})
    attach_resource_display_names(detail, "wall")
    assert "display_name" not in detail["shots"][0]["storyboard_pack_segment"]["resources"]["characters"][0]


def test_no_shots_means_no_lookup_at_all(monkeypatch) -> None:
    asked = _stub_episode_pack(monkeypatch, _MANIFEST)
    detail = {"id": "ep_1", "shots": []}
    attach_resource_display_names(detail, "script")
    assert asked == []


def test_episode_without_a_prep_pack_is_a_noop(monkeypatch) -> None:
    _stub_episode_pack(monkeypatch, None)
    detail = _detail_with({"characters": [{"identity_id": "bible:孟浩"}], "scenes": []})
    attach_resource_display_names(detail, "board")
    assert "display_name" not in detail["shots"][0]["storyboard_pack_segment"]["resources"]["characters"][0]
