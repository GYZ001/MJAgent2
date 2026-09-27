"""app.scene_reverse.segment_views：反打视角图判定与装配辅助的纯函数单测。

覆盖生成期（scene_reverse_angle_available/reverse_mention_errors）与装配期
（mentioned_reverse_scene_names/augment_scene_entry_with_reverse_angle/
scene_anchor_entity_name）两侧共用的具体逻辑。不依赖真实项目/世界书数据，
只建一张最小的 ``scene_reference_views`` 表（真实列名，无外键）。
"""
from __future__ import annotations

import json
import sqlite3

from app.scene_reverse import evidence as reverse_evidence
from app.scene_reverse import segment_views


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE scene_reference_views(id TEXT, scene_reference_id TEXT, view_role TEXT, "
        "image_path TEXT, qa_json TEXT, status TEXT)"
    )
    return conn


def _insert_view(conn, *, scene_reference_id="sr1", image_path, passed=True) -> None:
    qa = {"reverse_check": {"checked": True, "passed": passed, "reason": "朝向相反"}}
    conn.execute(
        "INSERT INTO scene_reference_views(id, scene_reference_id, view_role, image_path, qa_json, status) "
        "VALUES('rv1', ?, 'reverse_angle', ?, ?, 'ready')",
        (scene_reference_id, image_path, json.dumps(qa, ensure_ascii=False)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# scene_reverse_angle_available（生成期：模型能不能看到这个选项）
# ---------------------------------------------------------------------------

def test_scene_reverse_angle_available_true_with_passed_evidence_and_real_file(monkeypatch, tmp_path):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "true")
    conn = _conn()
    image = tmp_path / "reverse.jpg"
    image.write_bytes(b"jpg")
    _insert_view(conn, image_path=str(image))
    assert segment_views.scene_reverse_angle_available(conn, "sr1") is True


def test_scene_reverse_angle_available_false_when_switch_off(monkeypatch, tmp_path):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "false")
    conn = _conn()
    image = tmp_path / "reverse.jpg"
    image.write_bytes(b"jpg")
    _insert_view(conn, image_path=str(image))
    assert segment_views.scene_reverse_angle_available(conn, "sr1") is False


def test_scene_reverse_angle_available_false_when_no_row(monkeypatch):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "true")
    conn = _conn()
    assert segment_views.scene_reverse_angle_available(conn, "sr1") is False


def test_scene_reverse_angle_available_false_when_check_not_passed(monkeypatch, tmp_path):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "true")
    conn = _conn()
    image = tmp_path / "reverse.jpg"
    image.write_bytes(b"jpg")
    _insert_view(conn, image_path=str(image), passed=False)
    assert segment_views.scene_reverse_angle_available(conn, "sr1") is False


def test_scene_reverse_angle_available_false_when_file_missing(monkeypatch):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "true")
    conn = _conn()
    _insert_view(conn, image_path="/nonexistent/reverse.jpg")
    assert segment_views.scene_reverse_angle_available(conn, "sr1") is False


def test_scene_reverse_angle_available_false_when_no_scene_reference_id(monkeypatch):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "true")
    conn = _conn()
    assert segment_views.scene_reverse_angle_available(conn, None) is False


# ---------------------------------------------------------------------------
# reverse_mention_errors（生成期阻断核验）
# ---------------------------------------------------------------------------

def test_reverse_mention_errors_blocks_unmatched_name_and_explains_legal_source():
    relevant_scenes = [{"display_name": "修表铺", "reverse_angle_available": True}]
    errors = segment_views.reverse_mention_errors("镜头1：@后院·反打 门口。", relevant_scenes)
    assert len(errors) == 1
    assert "@后院·反打" in errors[0]
    assert "reverse_angle_available=true" in errors[0]
    assert "display_name" in errors[0]


def test_reverse_mention_errors_allows_matched_ready_scene():
    relevant_scenes = [{"display_name": "修表铺", "reverse_angle_available": True}]
    errors = segment_views.reverse_mention_errors("镜头1：@修表铺·反打 门口回望。", relevant_scenes)
    assert errors == []


def test_reverse_mention_errors_blocks_mention_of_scene_not_marked_available():
    relevant_scenes = [{"display_name": "修表铺", "reverse_angle_available": False}]
    errors = segment_views.reverse_mention_errors("镜头1：@修表铺·反打 门口回望。", relevant_scenes)
    assert len(errors) == 1 and "@修表铺·反打" in errors[0]


def test_reverse_mention_errors_noop_without_any_mention():
    relevant_scenes = [{"display_name": "修表铺", "reverse_angle_available": True}]
    assert segment_views.reverse_mention_errors("镜头1：固定远景，修表铺全景。", relevant_scenes) == []


# ---------------------------------------------------------------------------
# mentioned_reverse_scene_names（装配期：从持久化正文里找被点名的场景）
# ---------------------------------------------------------------------------

def test_mentioned_reverse_scene_names_uses_display_name_callback():
    entries = [{"scene_id": "bible:修表铺"}, {"scene_id": "bible:老宅"}]
    names = segment_views.mentioned_reverse_scene_names(
        "镜头1：@修表铺·反打 门口。", entries, display_name=lambda sid: sid.split(":", 1)[-1],
    )
    assert names == {"修表铺"}


# ---------------------------------------------------------------------------
# augment_scene_entry_with_reverse_angle（装配期：真的把反打视角塞进 entry）
# ---------------------------------------------------------------------------

_BASE_ENTRY = {
    "name": "修表铺", "selected_view_ids": ["sr1"],
    "selected_views": [{"id": "sr1", "view_role": "establishing"}],
    "available_view_roles": ["establishing"],
}


def test_augment_adds_reverse_view_when_mentioned_and_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "true")
    conn = _conn()
    image = tmp_path / "reverse.jpg"
    image.write_bytes(b"jpg")
    _insert_view(conn, image_path=str(image))

    out = segment_views.augment_scene_entry_with_reverse_angle(
        _BASE_ENTRY, conn=conn, scene_reference_id="sr1", scene_name="修表铺",
        mentioned_scene_names={"修表铺"}, purposes=["qa_anchor"],
    )

    assert out["available_view_roles"] == ["establishing", "reverse_angle"]
    assert out["selected_view_ids"] == ["sr1", "rv1"]
    assert out["selected_views"][-1]["image_path"] == str(image)
    # 不修改传入对象本身
    assert _BASE_ENTRY["available_view_roles"] == ["establishing"]


def test_augment_leaves_entry_unchanged_when_not_mentioned(monkeypatch, tmp_path):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "true")
    conn = _conn()
    image = tmp_path / "reverse.jpg"
    image.write_bytes(b"jpg")
    _insert_view(conn, image_path=str(image))

    out = segment_views.augment_scene_entry_with_reverse_angle(
        _BASE_ENTRY, conn=conn, scene_reference_id="sr1", scene_name="修表铺",
        mentioned_scene_names=set(), purposes=["qa_anchor"],
    )
    assert out is _BASE_ENTRY


def test_augment_leaves_entry_unchanged_when_switch_off(monkeypatch, tmp_path):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "false")
    conn = _conn()
    image = tmp_path / "reverse.jpg"
    image.write_bytes(b"jpg")
    _insert_view(conn, image_path=str(image))

    out = segment_views.augment_scene_entry_with_reverse_angle(
        _BASE_ENTRY, conn=conn, scene_reference_id="sr1", scene_name="修表铺",
        mentioned_scene_names={"修表铺"}, purposes=["qa_anchor"],
    )
    assert out is _BASE_ENTRY


def test_augment_leaves_entry_unchanged_when_no_ready_view(monkeypatch):
    monkeypatch.setattr(reverse_evidence, "get_setting", lambda key: "true")
    conn = _conn()  # 没有任何反打视角行

    out = segment_views.augment_scene_entry_with_reverse_angle(
        _BASE_ENTRY, conn=conn, scene_reference_id="sr1", scene_name="修表铺",
        mentioned_scene_names={"修表铺"}, purposes=["qa_anchor"],
    )
    assert out is _BASE_ENTRY


# ---------------------------------------------------------------------------
# scene_anchor_entity_name
# ---------------------------------------------------------------------------

def test_scene_anchor_entity_name_adds_suffix_only_for_reverse_angle():
    assert segment_views.scene_anchor_entity_name("修表铺", "reverse_angle") == "修表铺·反打"
    assert segment_views.scene_anchor_entity_name("修表铺", "establishing") == "修表铺"
    assert segment_views.scene_anchor_entity_name(None, "reverse_angle") == "·反打"
