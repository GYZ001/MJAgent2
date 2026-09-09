"""复核证据以提交顺序为准；素材候选顺序与供应商编号可能不同。"""
from app.domain.storyboard_ops.identity_review import _review_reference_images


def test_review_references_follow_submitted_order_and_exclude_unused_candidates():
    refs = [
        {"slot_key": "character:meng", "entity_name": "孟浩", "type": "character", "path": "https://example.com/meng.png"},
        {"slot_key": "scene:mountain", "entity_name": "山顶", "type": "scene", "path": "https://example.com/mountain.png"},
        {"slot_key": "character:unused", "entity_name": "未使用角色", "type": "character", "path": "https://example.com/unused.png"},
    ]
    result = _review_reference_images({"reference_images": refs, "_seedance_image_input_labels": [
        {"slot_key": "scene:mountain", "entity_name": "山顶"}, {"slot_key": "character:meng", "entity_name": "孟浩"},
    ]})
    assert [r["label"] for r in result] == ["参考图 1 · 山顶", "参考图 2 · 孟浩"]
    assert [r["url"] for r in result] == [refs[1]["path"], refs[0]["path"]]


def test_ambiguous_history_does_not_guess_a_reference_image():
    refs = [{"entity_name": "孟浩", "path": "https://example.com/a.png"},
            {"entity_name": "孟浩", "path": "https://example.com/b.png"}]
    result = _review_reference_images({"reference_images": refs, "_seedance_image_input_labels": [{"entity_name": "孟浩"}]})
    assert result[0]["url"] is None
    assert "无法唯一定位" in result[0]["label"]


def test_old_history_without_submitted_labels_is_explicitly_marked():
    refs = [{"entity_name": "孟浩", "path": "https://example.com/a.png"},
            {"entity_name": "未选", "selectedForSeedance": False}, {"entity_name": "已删", "deleted": True}]
    result = _review_reference_images({"reference_images": refs})
    assert len(result) == 1 and "提交顺序未记录" in result[0]["label"]
    assert _review_reference_images({"reference_images": refs, "_seedance_image_input_labels": []}) == []
