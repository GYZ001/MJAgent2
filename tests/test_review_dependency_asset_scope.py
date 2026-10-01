"""资产依赖围栏的最小作用域回归（2026-10-01《顾念长安》EP1 两次生产事故）。

根因证伪（见派单报告）：生产 DB 取证显示两次事故里 ``_review_upstream_snapshot``
的"稳定摘要"（``published_screenplay_artifact_id``/``confirmed_storyboard_
artifact_id``/``screenplay_revision``/``storyboard_revision``）在全部 14 条
失败记录里逐字不变；真正变化的是资产摘要——``_review_asset_contract`` 把"整集
里任何其它镜头当前的素材选择"当成每个任务的上游依赖，而兄弟镜自己换一次道具
勾选、丢一张本镜用不到的场景参考图，都会把毫不相关的在途任务判成
``REVIEW_DEPENDENCY_STALE``。修法：``app.media_exec.authority.
_review_shared_asset_entities`` 把跨镜比较收窄到"本镜自己声明依赖、且带着真实
共享库版本号（``asset_version``）"的实体，详见该函数文档。

本文件只覆盖资产围栏本身；``本镜自己的分镜被修订时仍须 fail closed`` 由另一条
独立机制（``app.media_exec.identity_fence.assert_identity_revision``）负责，
未被这次改动触及，见 ``test_own_shot_identity_revision_still_fails_closed``。
"""
from copy import deepcopy
import json

import pytest

from app import db, worker
from app.domain.storyboard_ops import identity_workspace as workspace
from app.media_exec.identity_fence import assert_identity_revision
from app.production.storyboard_identity_contract import identity_contract_fingerprint, stamp_identity_contract
from app.production.storyboard_speech_render import render_segment_speech
from tests.conftest import patch_api_everywhere, patch_worker_everywhere
from tests.test_review_wall_prd import _conn


def _capture(snapshot: dict) -> dict:
    return {
        key: snapshot.get(key) for key in (
            "qualification_version", "published_screenplay_artifact_id",
            "confirmed_storyboard_artifact_id", "screenplay_revision",
            "storyboard_revision", "asset_inputs", "asset_soft_warnings",
        )
    }


def _add_sibling_shot(conn, shot_id: str, shot_no: int) -> None:
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,action_desc,characters,dialogues,storyboard_artifact_id
           ) VALUES(?,'e',?,5,'中景','固定','日，测试室内场景','other','[]','[]','board-1')""",
        (shot_id, shot_no),
    )


def test_sibling_prop_selection_churn_does_not_fence(monkeypatch) -> None:
    """复现段 20/段 18 实测：兄弟镜自己改选道具（没有库版本号），不得牵连别的在途镜。"""
    conn = _conn()
    _add_sibling_shot(conn, "s2", 2)
    own_reference = {
        "id": "own-ref", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "character", "entity_name": "角色甲", "library_revision_id": "portrait-v1",
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-own','s1',1,'p','own-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [own_reference]}),),
    )
    conn.execute("UPDATE shots SET adopted_version_id='v-own' WHERE id='s1'")
    prop_reference = {
        "id": "prop-ref", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "prop", "entity_name": "外套",
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-other','s2',1,'p','other-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [prop_reference]}),),
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    from app import api
    captured = _capture(api._review_upstream_snapshot("e"))
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',2,'p','current-key','running',?,1)""",
        (json.dumps({"review_dependency_snapshot": captured}),),
    )
    # 段 20 的真实情形：下一次生成重新勾选道具，旧条目消失、新条目出现。
    new_prop_reference = {
        "id": "prop-ref-2", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "prop", "entity_name": "水泡坏的行李箱",
    }
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v-other'",
        (json.dumps({"reference_images": [new_prop_reference]}),),
    )
    conn.commit()

    worker._assert_review_dependency_fence(
        {"episode_id": "e", "shot_id": "s1"}, "v-current", "worker_start",
    )


def test_sibling_scene_variant_outside_target_dependency_does_not_fence(monkeypatch) -> None:
    """复现段 19/段 5 实测：兄弟镜丢了一张本镜根本不依赖的"反打"场景图，不得牵连。"""
    conn = _conn()
    _add_sibling_shot(conn, "s2", 2)
    base_scene = {
        "id": "base-ref", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "scene", "entity_name": "出租屋", "library_revision_id": "scene-v1",
    }
    reverse_scene = {
        "id": "reverse-ref", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "scene", "entity_name": "出租屋·反打", "library_revision_id": "scene-v1-rev",
    }
    # s1 自己只依赖基础场景图，不用"反打"视角。
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-own','s1',1,'p','own-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [base_scene]}),),
    )
    conn.execute("UPDATE shots SET adopted_version_id='v-own' WHERE id='s1'")
    # s2 两个都用过。
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-other','s2',1,'p','other-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [base_scene, reverse_scene]}),),
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    from app import api
    captured = _capture(api._review_upstream_snapshot("e"))
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',2,'p','current-key','running',?,1)""",
        (json.dumps({"review_dependency_snapshot": captured}),),
    )
    # s2 下一次生成不再需要反打视角，基础场景图原样保留。
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v-other'",
        (json.dumps({"reference_images": [base_scene]}),),
    )
    conn.commit()

    worker._assert_review_dependency_fence(
        {"episode_id": "e", "shot_id": "s1"}, "v-current", "worker_start",
    )


def test_sibling_shared_library_revision_change_still_fences(monkeypatch) -> None:
    """安全网：兄弟镜真正共享的库资产（本镜自己也依赖）换了版本，仍须拦住。"""
    conn = _conn()
    _add_sibling_shot(conn, "s2", 2)
    shared_v1 = {
        "id": "shared-ref", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "character", "entity_name": "角色甲", "library_revision_id": "portrait-v1",
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-own','s1',1,'p','own-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [shared_v1]}),),
    )
    conn.execute("UPDATE shots SET adopted_version_id='v-own' WHERE id='s1'")
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-other','s2',1,'p','other-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [shared_v1]}),),
    )
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    from app import api
    captured = _capture(api._review_upstream_snapshot("e"))
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',2,'p','current-key','running',?,1)""",
        (json.dumps({"review_dependency_snapshot": captured}),),
    )
    shared_v2 = {**shared_v1, "library_revision_id": "portrait-v2"}
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v-other'",
        (json.dumps({"reference_images": [shared_v2]}),),
    )
    conn.commit()

    with pytest.raises(worker.ReviewDependencyFence):
        worker._assert_review_dependency_fence(
            {"episode_id": "e", "shot_id": "s1"}, "v-current", "worker_start",
        )


def test_episode_level_storyboard_revision_change_still_fences(monkeypatch) -> None:
    """安全网：本次改动没有碰 upstream_keys——分镜整体重做仍要拦住在途任务。"""
    conn = _conn()
    conn.execute("UPDATE episodes SET storyboard_production_revision_id='rev-1' WHERE id='e'")
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    from app import api
    captured = _capture(api._review_upstream_snapshot("e"))
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',1,'p','current-key','running',?,0)""",
        (json.dumps({"review_dependency_snapshot": captured}),),
    )
    conn.execute("UPDATE episodes SET storyboard_production_revision_id='rev-2' WHERE id='e'")
    conn.commit()

    with pytest.raises(worker.ReviewDependencyFence):
        worker._assert_review_dependency_fence(
            {"episode_id": "e", "shot_id": "s1"}, "v-current", "worker_start",
        )


@pytest.fixture
def _apply_fixture(tmp_path):
    """两镜共用同一角色，均带画廊；用于验证真实 ``save_identity_candidate``
    对兄弟镜资产快照的实际影响（见下方测试文档）。"""
    conn = db.get_conn()
    payload = {"prep_pack_version": "2.0.0", "asset_manifest": {
        "characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩", "segment_indexes": [1, 2]}],
        "scenes": [], "functional_extras": [],
    }}
    conn.execute("INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','本地回归','{}',?)", (db.now(),))
    conn.execute("INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,'第五章',?)", ("孟浩（OS）：我一定会回来。\n\n山风吹过。",))
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,screenplay_json,created_at) VALUES('ep','p',5,'第五集','[1]','confirmed',?,?)", (json.dumps(payload), db.now()))
    segment = dict(
        synopsis="孟浩自述", source_segment_indexes=[1], beat_ids=["B1"],
        beats=[{"beat_id": "B1", "summary": "孟浩自述", "segment_indexes": [1]}], shot_count=1, duration_s=15,
        target_model="seedance_2", degraded_capabilities=[], prompt_text="镜头1：远处山风。{{speech:U01}}",
        dialogue=[dict(utterance_id="U01", speaker_identity_id="bible:孟浩", line="我一定会回来。", source_segment_index=1, delivery="offscreen_voice", delivery_kind="inner_monologue")],
        resources={"characters": [dict(identity_id="bible:孟浩", display_name="孟浩", subject_kind="character", visibility="voice_only")], "scenes": [], "props": []},
    )
    gallery = json.dumps({"reference_images": [{
        "id": "ref", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "character", "entity_name": "孟浩", "library_revision_id": "portrait-v1",
    }]})
    stored = {}
    for i in (1, 2):
        current = deepcopy(segment)
        current["segment_no"] = i
        render_segment_speech(current, dialect="seedance")
        stamp_identity_contract(current)
        stored[i] = current
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) VALUES(?, 'ep',?,15,'','','','','','[]','[]',?,?,?)",
            (f"s{i}", i, "孟浩（OS）：我一定会回来。", json.dumps({"storyboard_pack_segment": current}), f"v{i}"),
        )
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at) VALUES(?,?,1,?,?,'succeeded',?,?)",
            (f"v{i}", f"s{i}", current["prompt_text"], f"old-{i}", gallery, db.now()),
        )
    conn.commit()
    return conn, stored


def test_own_shot_identity_revision_still_fails_closed(_apply_fixture) -> None:
    """独立机制（identity_fence）兜底：本镜自己被 apply 修订，仍必须拦住已入队任务——这次改动没有碰这条路径。"""
    conn, stored = _apply_fixture
    fingerprint_before = identity_contract_fingerprint(stored[1])
    meta = {"segment_identity_fingerprint": fingerprint_before}
    candidate = deepcopy(stored[1])
    candidate["resources"]["characters"][0]["visibility"] = "visible"
    candidate["speech_template"] = "镜头1：@孟浩 望向山路。{{speech:U01}}"
    workspace.save_identity_candidate(conn, shot_id="s1", baseline=fingerprint_before, candidate=candidate)

    with pytest.raises(Exception, match="STORYBOARD_IDENTITY_STALE"):
        assert_identity_revision(conn, shot_id="s1", meta=meta, write_point="worker_start")


def test_apply_on_sibling_shot_does_not_change_asset_snapshot_for_unrelated_shot(_apply_fixture) -> None:
    """用真实 ``save_identity_candidate`` 核实派单描述的事故序列第一步的实际影响：
    B 段（s2）被 apply 修订，不应该改变整集资产快照（``asset_inputs``）。

    证据（详见派单报告）：apply 本身不改写 ``shot_versions.image_inputs``，只清空
    s2 的 ``adopted_version_id`` 并把旧画廊行标记 stale——该行仍是唯一候选，回退
    选中结果与改前完全一致。真正让整集资产摘要漂移、把毫不相关的 A 段任务判成
    过期的是后续对 B 段调用生成（重新解析画廊，见
    ``test_sibling_prop_selection_churn_does_not_fence``/
    ``test_sibling_scene_variant_outside_target_dependency_does_not_fence``）——
    这条测试不走完整的 ``_assert_review_dependency_fence``（本仓库这个最小夹具
    没有搭完整的剧本发布凭证链，``eligible_for_production`` 会因为与本次改动
    无关的原因恒为 False，不是这里要验证的东西），直接比对快照本身更精确。
    """
    conn, stored = _apply_fixture
    from app import api
    before_assets = _capture(api._review_upstream_snapshot("ep"))["asset_inputs"]

    fingerprint_before = identity_contract_fingerprint(stored[2])
    candidate = deepcopy(stored[2])
    candidate["resources"]["characters"][0]["visibility"] = "visible"
    candidate["speech_template"] = "镜头2：@孟浩 回望。{{speech:U01}}"
    workspace.save_identity_candidate(conn, shot_id="s2", baseline=fingerprint_before, candidate=candidate)

    after_assets = _capture(api._review_upstream_snapshot("ep"))["asset_inputs"]
    assert after_assets == before_assets
