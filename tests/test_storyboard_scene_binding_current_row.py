"""场景绑定当前生效行（app.production.storyboard_scene_binding）。

真实回归：proj_ca86b15ab7d7 EP1「温念的出租屋」被整包重生后，旧图挪进历史槽
（``ep_start=-2, ep_end=0``——负数是已作废的历史槽位标记，不是区间；历史行
``ep_start<=episode_no`` 恒成立，但 ``ep_end`` 已封顶，天然被「对本集生效」
的区间查询排除），新图另起一行（``ep_start=1, ep_end=NULL``）。映射台建包
那一刻把 ``scene_reference_id`` 冻结成快照，分镜台如果直接读它，会把已经
作废的历史行的 ``scene_canonical`` 喂给模型、烧进 ``prompt_text``——生成
阶段的参考图装配（``app.multiview._resolve_scene_entry``）本就按场景名+
集号重新查表，用的是新图，文字与画面因此互相矛盾。

覆盖：
1. 历史行 + 当前行同时存在：manifest 快照指向历史行，enrich 后必须改写成
   当前行的 id 与 canonical。
2. 当前行不存在（这一集也好、全项目也好都查不到）：必须显式写 None，不
   回退旧快照值（CLAUDE.md「不得兜底填充」）；canonical 回退世界书。
3. 映射包快照本就没绑定（``scene_reference_id=None``），但当前行此刻已经
   存在（例如「老城区窄巷」建包之后才建卡）：必须被正确绑定上。
4. 生成后回填（``canonical_segment_identities``）：段草稿的
   ``resources.scenes[].scene_reference_id`` 必须统一改写为 manifest 当前值，
   不沿用模型草稿里可能过滤缺口导致的旧值/空值。
"""
from __future__ import annotations

import pytest

from app import db
from app.production.storyboard_identity_contract import canonical_segment_identities
from app.production.storyboard_pack import _enrich_asset_manifest_canonical_visuals
from app.schemas import Bible

PROJECT_ID = "proj-scene-rebind"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-binding.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed_project(conn, project_id: str = PROJECT_ID) -> None:
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        (project_id, "scene rebind fixture", "{}", db.now()),
    )


def _seed_scene_row(
    conn, *, row_id: str, scene_name: str, ep_start: int, ep_end: int | None,
    scene_canonical: str, project_id: str = PROJECT_ID,
) -> None:
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "scene_canonical, created_at) VALUES(?,?,?,?,?,?,?)",
        (row_id, project_id, scene_name, ep_start, ep_end, scene_canonical, db.now()),
    )


def _manifest_payload(*, scene_reference_id: str | None, scene_name: str = "温念的出租屋") -> dict:
    return {
        "episode_no": 1,
        "asset_manifest": {
            "characters": [], "functional_extras": [], "props": [],
            "scenes": [{
                "scene_id": f"scene:{scene_name}", "display_name": scene_name,
                "scene_reference_id": scene_reference_id, "segment_indexes": [1],
            }],
        },
    }


def test_stale_history_reference_is_rebound_to_current_row_and_canonical():
    conn = db.get_conn()
    _seed_project(conn)
    _seed_scene_row(
        conn, row_id="scene_history", scene_name="温念的出租屋",
        ep_start=-2, ep_end=0, scene_canonical="旧：地面积半掌深的积水",
    )
    _seed_scene_row(
        conn, row_id="scene_current", scene_name="温念的出租屋",
        ep_start=1, ep_end=None, scene_canonical="新：干净整洁的出租屋",
    )
    conn.commit()
    payload = _manifest_payload(scene_reference_id="scene_history")
    _enrich_asset_manifest_canonical_visuals(conn, payload, bible=None, project_id=PROJECT_ID)
    scene = payload["asset_manifest"]["scenes"][0]
    assert scene["scene_reference_id"] == "scene_current"
    assert scene["scene_canonical"] == "新：干净整洁的出租屋"


def test_no_current_row_clears_stale_reference_and_falls_back_to_bible_canonical():
    """当前生效行查不到时必须显式写 None，不许回退快照里的旧值——旧值已经
    被证明不是当前生效行，兜底填充只是把同一个错误换个理由继续发生。"""
    conn = db.get_conn()
    _seed_project(conn)
    conn.commit()
    payload = _manifest_payload(scene_reference_id="scene_stale_and_gone", scene_name="山谷小屋")
    bible = Bible.model_validate({
        "characters": [],
        "scenes": [{"name": "山谷小屋", "scene_canonical": "世界书标准：山谷里的破旧小屋。"}],
        "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
    })
    _enrich_asset_manifest_canonical_visuals(conn, payload, bible=bible, project_id=PROJECT_ID)
    scene = payload["asset_manifest"]["scenes"][0]
    assert scene["scene_reference_id"] is None
    assert scene["scene_canonical"] == "世界书标准：山谷里的破旧小屋。"


def test_unbound_manifest_scene_gets_bound_when_current_row_now_exists():
    """对应「老城区窄巷」：映射包建包时这个场景还没有卡（快照里
    scene_reference_id=None），后来卡建好了——分镜台必须能重新绑上，不能
    因为快照当初是 None 就永远 None。"""
    conn = db.get_conn()
    _seed_project(conn)
    _seed_scene_row(
        conn, row_id="scene_new", scene_name="老城区窄巷",
        ep_start=1, ep_end=None, scene_canonical="青石板窄巷，两侧砖墙斑驳。",
    )
    conn.commit()
    payload = _manifest_payload(scene_reference_id=None, scene_name="老城区窄巷")
    _enrich_asset_manifest_canonical_visuals(conn, payload, bible=None, project_id=PROJECT_ID)
    scene = payload["asset_manifest"]["scenes"][0]
    assert scene["scene_reference_id"] == "scene_new"
    assert scene["scene_canonical"] == "青石板窄巷，两侧砖墙斑驳。"


def test_missing_project_context_leaves_snapshot_untouched():
    """project_id/episode_no 缺失是「结构上没法查」，不是「查了没找到」——
    保持快照原值，不能跟真正查过之后的 None 混为一谈。既有调用方（部分单测
    不传 project_id）依赖这个语义不被破坏。"""
    conn = db.get_conn()
    _seed_project(conn)
    conn.commit()
    payload = _manifest_payload(scene_reference_id="scene_ref_kept")
    _enrich_asset_manifest_canonical_visuals(conn, payload, bible=None, project_id=None)
    scene = payload["asset_manifest"]["scenes"][0]
    assert scene["scene_reference_id"] == "scene_ref_kept"


def test_post_generation_backfill_rewrites_stale_segment_scene_reference():
    """步骤二：生成后回填。段草稿这一条场景的 scene_reference_id 可能因为
    ``_segment_relevant_assets`` 的过滤缺口而落空/带旧值；manifest 已经是
    fix 1 重新解析过的当前值，回填必须让两边一致。"""
    payload = {
        "asset_manifest": {
            "characters": [], "functional_extras": [],
            "scenes": [{
                "scene_id": "scene:温念的出租屋", "display_name": "温念的出租屋",
                "scene_reference_id": "scene_current",
            }],
        },
    }
    segment = {
        "source_segment_indexes": [1],
        "resources": {
            "characters": [],
            "scenes": [{"scene_id": "scene:温念的出租屋", "scene_reference_id": "scene_history_stale"}],
        },
        "dialogue": [],
    }
    result = canonical_segment_identities(segment, payload)
    scene = result["resources"]["scenes"][0]
    assert scene["scene_reference_id"] == "scene_current"


def test_post_generation_backfill_is_idempotent():
    payload = {
        "asset_manifest": {
            "characters": [], "functional_extras": [],
            "scenes": [{"scene_id": "scene:山顶", "display_name": "山顶", "scene_reference_id": "scene_x"}],
        },
    }
    segment = {
        "source_segment_indexes": [1],
        "resources": {"characters": [], "scenes": [{"scene_id": "scene:山顶", "scene_reference_id": "scene_x"}]},
        "dialogue": [],
    }
    once = canonical_segment_identities(segment, payload)
    twice = canonical_segment_identities(once, payload)
    assert twice["resources"]["scenes"][0]["scene_reference_id"] == "scene_x"
