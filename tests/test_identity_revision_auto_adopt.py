"""``app.evidence.media.select_best_video_candidate`` 对「修订本段」保留版本
的 sticky 豁免（2026-10-03 修复）。

背景：``tests/test_identity_revision_retention.py`` 已覆盖标记读写/交付清单/
人工采纳/字幕快照等场景；这里单独补两条系统代采的边界场景，避免该文件超过
500 行上限：

1. 保留版本在技术合格池里、且池里没有别的版本——必须继续沿用它顶着（不是
   『不享受 sticky』就意味着无条件换人，没有替代品时它仍是唯一合法选择）；
2. 普通（无保留标记）镜头——行为必须与修复前字节级一致，继续 sticky 在已
   采用版本上，不因为新增的保留豁免分支而被误伤。

与 ``test_identity_revision_retention.py::
test_auto_adopt_replaces_retained_version_when_new_version_is_available`` 和
``test_auto_adopt_replaces_and_releases_marker_when_retained_version_falls_out_of_pool``
一起，四条测试覆盖 ``select_best_video_candidate`` 里『保留版本是否享受
sticky』判断的全部分支组合。
"""
from __future__ import annotations

import json

from app.evidence.identity_revision_retention import (
    is_retained_after_revision,
    mark_retained_after_revision,
)
from tests.test_episode_partial_concat import _database, _version


_SNAPSHOT = {
    "dialogue": [{"utterance_id": "U01", "speaker_identity_id": "bible:孟浩",
                  "line": "修订前的旧台词。", "delivery_kind": "inner_monologue"}],
    "resources": {"characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩"}]},
}


def test_auto_adopt_stays_on_retained_version_when_no_alternative_exists(tmp_path) -> None:
    """保留版本仍在技术合格池里、且池里没有别的版本（还没有修订后的新版本
    生成成功）：必须继续沿用它顶着，不能因为『不享受 sticky』就被换掉——
    它本就是池里唯一的技术有效候选。"""
    import app.evidence.media as media_module

    conn = _database((1,))
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=True)
    mark_retained_after_revision(conn, "v1", dialogue_snapshot=_SNAPSHOT)
    conn.execute("UPDATE shot_versions SET technical_validation_json=? WHERE id='v1'", (json.dumps({"passed": True}),))
    conn.commit()

    orig_get_conn = media_module.get_conn
    media_module.get_conn = lambda: conn
    try:
        result = media_module.select_best_video_candidate("s1")
    finally:
        media_module.get_conn = orig_get_conn

    assert result["version_id"] == "v1", "池里没有别的候选时，保留版本仍是唯一选择，必须继续顶着"
    row = conn.execute("SELECT status,adoption_reason FROM shot_versions WHERE id='v1'").fetchone()
    assert row["status"] == "succeeded"
    assert is_retained_after_revision(row["adoption_reason"]), "没有被替换，标记不应被清除"


def test_auto_adopt_is_still_sticky_for_plain_shots_without_retention_marker(tmp_path) -> None:
    """普通镜头（无保留标记）：已采用 v1，新 v2 技术合格也不换——修复只豁免
    带标记的保留版本，不能动到既有的 sticky 行为（字节级不变）。"""
    import app.evidence.media as media_module

    conn = _database((1,))
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=True)
    conn.execute("UPDATE shot_versions SET technical_validation_json=? WHERE id='v1'", (json.dumps({"passed": True}),))
    new_path = tmp_path / "new.mp4"
    new_path.write_bytes(b"new-video")
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
                                     video_path,technical_validation_json,created_at)
           VALUES('v2','s1',2,'prompt','key-2','succeeded',?,?,0)""",
        (str(new_path), json.dumps({"passed": True})),
    )
    conn.commit()

    orig_get_conn = media_module.get_conn
    media_module.get_conn = lambda: conn
    try:
        result = media_module.select_best_video_candidate("s1")
    finally:
        media_module.get_conn = orig_get_conn

    assert result["version_id"] == "v1", "没有保留标记时必须维持既有 sticky 行为"
    row = conn.execute("SELECT status,adoption_reason FROM shot_versions WHERE id='v1'").fetchone()
    assert row["status"] == "succeeded"
    assert not is_retained_after_revision(row["adoption_reason"])
