"""道具冻结记录缺 ``prop_revision_id`` 字段时的时间判据回退——2026-10-04
生产实测漏报修复：《顾念长安》第 1 集「浅灰色卫衣」卡被重新出图后，21/23/
29/31/32/34 段的采用版本因为冻结记录是 2026-10-01 前写的、缺这个字段，被
``asset_drift`` 当「未知=不变」直接放行，成片里来回跳变。

拆成独立文件（而不是加进 ``test_asset_refresh_report.py``）：避免把那个已经
456 行、此前一直合规的文件推过测试文件 500 行基线——CLAUDE.md「新增文件与
新增函数严格达标，一条都不许进 ``[baseline.*]``」。复用
``test_asset_refresh_report`` 现成的 fixture helper（同一先例见
``test_review_wall_prd.py`` 对 ``test_screenplay_edit_save._valid_script``
的跨文件 import），不重复实现一份分镜/版本构造逻辑。
"""
from __future__ import annotations

import app.video_modes.prop_references as prop_references
from app.domain.video_ops.asset_refresh_report import episode_asset_refresh_groups
from tests.conftest import patch_api_everywhere
from tests.test_asset_refresh_report import (
    _episode_row,
    _fresh_conn,
    _insert_pack_shot,
    _insert_version,
    _lookup_stub,
)


def _prop_manifest_no_revision_key(*, ready: bool, order: int = 0) -> dict:
    """模拟 2026-10-01 之前冻结的记录——没有 ``prop_revision_id`` 键（不是显式
    None），供时间判据回退路径（``_prop_asset_updated_after``）的测试用。"""
    return {
        "episode_no": 1, "shot_id": "ignored", "characters": [], "scene": None, "additional_scenes": [],
        "props": [{"label": "马克杯", "description": "", "ready": ready, "image_path": "", "resources_order": order}],
    }


def _insert_prop_reference(conn, *, project_id: str, prop_name: str, episode_no: int, created_at: float) -> None:
    """按 ``app.props.store`` 真实表结构直接插入一行，供
    ``_prop_asset_updated_after`` 的真实查询路径（``prop_reference_for_
    episode``）使用——这是伪造输入数据，不是伪造比较结果，与
    ``test_asset_refresh_report._insert_version``/``_insert_pack_shot`` 同一
    测试手法。"""
    from app.props.store import ensure_tables_on_connection

    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_references(id, project_id, prop_name, ep_start, ep_end, "
        "appearance, image_path, prompt, status, qa_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("propref-1", project_id, prop_name, episode_no, None, "appearance", "/tmp/x.png", "prompt", "ready", "{}", created_at),
    )


def test_prop_card_regenerated_after_video_without_revision_key_needs_regen(monkeypatch, tmp_path) -> None:
    """冻结记录缺 ``prop_revision_id`` 键，但道具卡最近一次登记/重出图的时间
    晚于视频生成时间——必须判「参考图已更新」，不能因为缺字段就放行。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug_legacy.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev9"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    # _insert_version 把 created_at 固定写成 version_no（这里是 1）。
    _insert_version(
        conn, version_id="v1", shot_id="s1", version_no=1,
        frozen_manifest=_prop_manifest_no_revision_key(ready=True),
    )
    _insert_prop_reference(conn, project_id="p", prop_name="马克杯", episode_no=1, created_at=100.0)
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    assert [g["entity_key"] for g in report["groups"]] == ["prop:马克杯"]
    assert report["groups"][0]["category"] == "updated"
    assert report["groups"][0]["members"][0]["status"] == "needs_regen"


def test_prop_card_untouched_before_video_without_revision_key_stays_latest(monkeypatch, tmp_path) -> None:
    """同样缺 ``prop_revision_id`` 键，但道具卡最近一次生效时间早于视频生成
    时间（10-01 前从未改过的卡）——必须维持「不变」，不能仅因为缺字段就把
    一直没变过的道具制造成噪声。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug_legacy2.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev9"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(
        conn, version_id="v1", shot_id="s1", version_no=200,
        frozen_manifest=_prop_manifest_no_revision_key(ready=True),
    )
    _insert_prop_reference(conn, project_id="p", prop_name="马克杯", episode_no=1, created_at=1.0)
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    assert report["groups"] == []
    assert report["needs_regen_shot_count"] == 0


def test_prop_card_timestamp_exactly_equal_to_video_stays_latest(monkeypatch, tmp_path) -> None:
    """道具卡最近生效时间与视频生成时间恰好相等（边界情况）——判据用严格
    大于，相等按「未变化」处理，与「查不到时间就不算变化」同一保守方向（见
    ``asset_drift._prop_asset_updated_after`` docstring），不是漏判。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug_legacy3.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev9"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(
        conn, version_id="v1", shot_id="s1", version_no=50,
        frozen_manifest=_prop_manifest_no_revision_key(ready=True),
    )
    _insert_prop_reference(conn, project_id="p", prop_name="马克杯", episode_no=1, created_at=50.0)
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    assert report["groups"] == []
    assert report["needs_regen_shot_count"] == 0
