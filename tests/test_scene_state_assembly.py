"""装配期接入场景状态图：端到端 sqlite，覆盖
``app.video_modes.scene_state_assembly.resolve_scene_entry_with_state``
——``app.multiview._storyboard_pack_asset_dependencies`` 实际调用的那一个
函数（接线守卫见 ``tests/test_scene_state_selection_wiring.py``）。

覆盖：有 ready 且指纹匹配的状态图 → 发送它、省略原因清空；没有状态图 → 保持
既有省略行为；状态图指纹不匹配（场景卡换了新种子图/描述变了）→ 当成没有、
继续省略。另外钉住 ``manifest_asset_view_fingerprints`` 能感知状态图被换成
新指纹的那一行——重出之后旧的冻结清单必须被判过期。
"""
from __future__ import annotations

import json

import pytest

from app import db
from app.multiview import _storyboard_pack_asset_dependencies, manifest_asset_view_fingerprints
from app.schemas import Bible, Scene, World
from app.video_modes.scene_state_views_store import ensure_tables_on_connection

_SCENE_NAME = "温念的出租屋"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-state-assembly.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed(conn, tmp_path) -> tuple[str, str]:
    est = tmp_path / "est.png"
    est.write_bytes(b"dry-room")
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        ("proj-1", "出租屋 fixture", "{}", db.now()),
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, image_path, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_a", "proj-1", _SCENE_NAME, 1, None, str(est), db.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        ("ep-1", "proj-1", 1, "scripted", db.now()),
    )
    conn.commit()
    return "scene_ref_a", str(est)


def _insert_shot(conn, shot_id: str, shot_no: int, *, matches_card: str, description: str = "") -> None:
    segment = {
        "segment_no": shot_no, "prompt_text": f"段{shot_no}正文。",
        "resources": {
            "characters": [],
            "scenes": [{
                "scene_id": f"scene:{_SCENE_NAME}", "scene_reference_id": "scene_ref_a",
                "scene_state_matches_card": matches_card, "description": description,
            }],
        },
    }
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
        (shot_id, "ep-1", shot_no, 15, json.dumps({"storyboard_pack_segment": segment}, ensure_ascii=False)),
    )
    conn.commit()


def _insert_state_view(conn, *, state_key: str, fingerprint: str, image_path: str, status: str = "ready") -> str:
    ensure_tables_on_connection(conn)
    row_id = "scstate-1"
    conn.execute(
        """INSERT INTO scene_state_views(
               id, project_id, episode_id, scene_reference_id, state_key, description, image_path, prompt,
               status, error, input_fingerprint, created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row_id, "proj-1", "ep-1", "scene_ref_a", state_key, "满地积水", image_path, "prompt",
         status, None, fingerprint, db.now(), db.now()),
    )
    conn.commit()
    return row_id


def _bible() -> Bible:
    return Bible(characters=[], world=World(visual_style_canonical="写实"), scenes=[Scene(name=_SCENE_NAME, scene_canonical=f"{_SCENE_NAME}，干燥整洁")])


def _manifest_for_shot(conn, shot_id: str, shot_no: int, *, matches_card: str) -> dict:
    segment = {
        "segment_no": shot_no, "prompt_text": f"段{shot_no}正文。",
        "resources": {
            "characters": [],
            "scenes": [{
                "scene_id": f"scene:{_SCENE_NAME}", "scene_reference_id": "scene_ref_a",
                "scene_state_matches_card": matches_card, "description": "满地积水",
            }],
        },
    }
    return _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id=shot_id, segment=segment, conn=conn, bible=_bible(),
    )


def _expected_fingerprint(establishing_path: str) -> str:
    from app.video_modes.scene_state_views import scene_state_input_fingerprint

    return scene_state_input_fingerprint(
        scene_reference_id="scene_ref_a", establishing_image_path=establishing_path,
        description="满地积水", visual_style="写实",
    )


def test_ready_matching_state_view_is_sent_instead_of_omitted(tmp_path):
    conn = db.get_conn()
    _, est_path = _seed(conn, tmp_path)
    _insert_shot(conn, "shot-15", 15, matches_card="no", description="满地积水")
    state_image = tmp_path / "state.png"
    state_image.write_bytes(b"flooded")
    from app.video_modes.scene_state_views import state_key_for

    key = state_key_for("scene_ref_a", 15, "满地积水")
    _insert_state_view(
        conn, state_key=key, fingerprint=_expected_fingerprint(est_path), image_path=str(state_image),
    )

    manifest = _manifest_for_shot(conn, "shot-15", 15, matches_card="no")

    assert manifest["scene"]["scene_state_omitted_reason"] is None
    assert manifest["scene"]["primary_usable"] is True
    roles = [v["view_role"] for v in manifest["scene"]["selected_views"]]
    assert roles == ["scene_state"]
    assert manifest["scene"]["selected_views"][0]["image_path"] == str(state_image)
    assert manifest["scene"]["scene_revision_id"] == "scene_ref_a"


def test_no_state_view_keeps_existing_omission_behavior(tmp_path):
    conn = db.get_conn()
    _seed(conn, tmp_path)
    _insert_shot(conn, "shot-15", 15, matches_card="no", description="满地积水")

    manifest = _manifest_for_shot(conn, "shot-15", 15, matches_card="no")

    assert manifest["scene"]["scene_state_omitted_reason"] is not None
    assert manifest["scene"]["selected_views"] == []


def test_fingerprint_mismatch_is_treated_as_no_state_view(tmp_path):
    """场景卡换了新的种子图（establishing_image_path 变化）或描述变了之后，
    旧状态图的指纹不再匹配——必须当成没有状态图，不能把过期图发出去。"""
    conn = db.get_conn()
    _, est_path = _seed(conn, tmp_path)
    _insert_shot(conn, "shot-15", 15, matches_card="no", description="满地积水")
    state_image = tmp_path / "state.png"
    state_image.write_bytes(b"flooded")
    from app.video_modes.scene_state_views import state_key_for

    key = state_key_for("scene_ref_a", 15, "满地积水")
    _insert_state_view(conn, state_key=key, fingerprint="stale-fingerprint", image_path=str(state_image))

    manifest = _manifest_for_shot(conn, "shot-15", 15, matches_card="no")

    assert manifest["scene"]["scene_state_omitted_reason"] is not None
    assert manifest["scene"]["selected_views"] == []


def test_manifest_fingerprint_sensing_detects_new_state_view(tmp_path):
    """``manifest_asset_view_fingerprints`` 已经按 (type, name, role) 收了
    ``selected_views`` 里每一张图的指纹（不需要再改 multiview.py）：状态图
    重出拿到新指纹后，冻结清单里记的旧指纹必须与"当前实际装配出来的指纹"
    不相等，``manifest_revisions_match`` 才会判它过期、触发重新装配。"""
    conn = db.get_conn()
    _, est_path = _seed(conn, tmp_path)
    _insert_shot(conn, "shot-15", 15, matches_card="no", description="满地积水")
    from app.video_modes.scene_state_views import state_key_for

    key = state_key_for("scene_ref_a", 15, "满地积水")
    state_image = tmp_path / "state.png"
    state_image.write_bytes(b"flooded")
    _insert_state_view(conn, state_key=key, fingerprint=_expected_fingerprint(est_path), image_path=str(state_image))
    frozen = _manifest_for_shot(conn, "shot-15", 15, matches_card="no")
    frozen_fp = manifest_asset_view_fingerprints({"scene": frozen["scene"]})
    assert frozen_fp[("scene", _SCENE_NAME, "scene_state")] == _expected_fingerprint(est_path)

    # 状态图重出：新种子图路径（establishing 换了新文件）意味着新指纹，旧行
    # 因指纹不符被当成不存在（见 test_fingerprint_mismatch_is_treated_as_no_
    # state_view），装配因此退回省略——当前实际状态与冻结快照不再一致。
    conn.execute("UPDATE scene_state_views SET input_fingerprint='new-fp' WHERE scene_reference_id='scene_ref_a'")
    conn.commit()
    current = _manifest_for_shot(conn, "shot-15", 15, matches_card="no")
    current_fp = manifest_asset_view_fingerprints({"scene": current["scene"]})
    assert frozen_fp != current_fp
