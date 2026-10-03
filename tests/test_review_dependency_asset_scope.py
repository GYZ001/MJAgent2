"""资产依赖围栏的最小作用域回归。

第一次根因证伪（2026-10-01《顾念长安》EP1，见派单报告）：生产 DB 取证显示两次
事故里 ``_review_upstream_snapshot`` 的"稳定摘要"（``published_screenplay_
artifact_id``/``confirmed_storyboard_artifact_id``/``screenplay_revision``/
``storyboard_revision``）在全部 14 条失败记录里逐字不变；真正变化的是资产
摘要——当时的 ``_review_asset_contract`` 把"整集里任何其它镜头当前的素材
选择"当成每个任务的上游依赖，兄弟镜自己换一次道具勾选、丢一张本镜用不到的
场景参考图，都会把毫不相关的在途任务判成 ``REVIEW_DEPENDENCY_STALE``。当时的
修法 ``_review_shared_asset_entities`` 把跨镜比较收窄到"本镜自己声明依赖、且
带着真实共享库版本号（``asset_version``）"的实体——但仍然是"拿兄弟镜头画廊
做子集比较"这一个思路的变体。

第二次事故（同集段 15/16/17，派单报告）证明这个思路本身有缺口：兄弟镜把画廊
从"全身定妆照"换成"头像照"（``asset_version`` 的取值来源从
``library_revision_id`` 换成 ``library_view_id``），entity 身份没变、
narrowing 判据仍然判定"共享"，照样把毫不相关的在途任务判成过期。彻底修法
（见 ``app.media_exec.authority._review_shot_manifest_equal``）不再看任何
兄弟镜头的画廊，只认本镜自己捕获/冻结的 ``reference_manifest``——
``_review_shared_asset_entities``/``_review_asset_contract`` 已随之删除。

本文件覆盖资产围栏的最小作用域与 fail-closed 安全网：
- ``test_sibling_gallery_view_switch_does_not_fence``：复现第二次事故，证明
  兄弟镜画廊变化不再牵连本镜（修复前手写判据副本验证会被误判过期）。
- ``test_own_portrait_replaced_still_fences`` / ``test_own_scene_reference_
  replaced_still_fences``：真过期（本镜自己依赖的库资产被替换）必须继续拦。
- ``test_episode_level_storyboard_revision_change_still_fences``：稳定事实
  漂移（分镜重新发布）不受本次改动影响，仍须拦住。

``本镜自己的分镜被修订时仍须 fail closed`` 由另一条独立机制
（``app.media_exec.identity_fence.assert_identity_revision``）负责，未被这次
改动触及，见 ``test_own_shot_identity_revision_still_fails_closed``。
"""
from copy import deepcopy
import json

import pytest

from app import db, worker
from app.domain.storyboard_ops import identity_workspace as workspace
from app.media_exec.identity_fence import assert_identity_revision
from app.media_exec.enqueue import _load_shot_model
from app.multiview import resolve_shot_asset_dependencies
from app.production.storyboard_identity_contract import identity_contract_fingerprint, stamp_identity_contract
from app.production.storyboard_speech_render import render_segment_speech
from app.schemas import Bible, Character, World
from tests.conftest import patch_api_everywhere, patch_portraits_everywhere, patch_worker_everywhere
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


def _seed_bible_character(conn, name: str) -> None:
    bible = Bible(
        characters=[Character(name=name, role="lead", appearance_canonical="黑发")],
        world=World(visual_style_canonical="写实"),
    )
    conn.execute("UPDATE projects SET bible_json=? WHERE id='p'", (bible.model_dump_json(),))


def _seed_character_portrait(conn, tmp_path, *, name: str, episode_no: int, suffix: str) -> str:
    """插入一张状态 ``ready``、文件真实存在（``resolve_views_for_roles`` 按
    ``Path.exists()`` 选图）的定妆照+视角，返回 portrait_id。"""
    image_path = tmp_path / f"portrait-{suffix}.png"
    image_path.write_bytes(b"x")
    portrait_id = f"portrait-{suffix}"
    conn.execute(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,ep_end,appearance,image_path,bible_version,created_at
           ) VALUES(?,'p',?,?,NULL,'黑发',?,1,0)""",
        (portrait_id, name, episode_no, str(image_path)),
    )
    conn.execute(
        """INSERT INTO character_portrait_views(
               id,portrait_id,view_role,status,selected,input_fingerprint,image_path,created_at
           ) VALUES(?,?,'front_full','ready',1,?,?,0)""",
        (f"{portrait_id}_view", portrait_id, f"fp-{suffix}", str(image_path)),
    )
    return portrait_id


def _seed_scene_reference(conn, tmp_path, *, name: str, episode_no: int, suffix: str) -> str:
    """同 ``_seed_character_portrait``，场景版："ready" + 文件真实存在。"""
    image_path = tmp_path / f"scene-{suffix}.png"
    image_path.write_bytes(b"x")
    scene_id = f"scene-{suffix}"
    conn.execute(
        """INSERT INTO scene_references(
               id,project_id,scene_name,ep_start,ep_end,scene_canonical,image_path,bible_version,created_at
           ) VALUES(?,'p',?,?,NULL,'s',?,1,0)""",
        (scene_id, name, episode_no, str(image_path)),
    )
    conn.execute(
        """INSERT INTO scene_reference_views(
               id,scene_reference_id,view_role,status,selected,input_fingerprint,image_path,created_at
           ) VALUES(?,?,'establishing','ready',1,?,?,0)""",
        (f"{scene_id}_view", scene_id, f"fp-{suffix}", str(image_path)),
    )
    return scene_id


def _resolve_s1_manifest(conn) -> dict:
    """按 s1 当前的 shots 行 + 项目人物谱，现场解析一份 reference_manifest——
    与 ``_review_shot_manifest_equal`` 冻结/复核用的是同一个函数。"""
    shot_row = conn.execute("SELECT * FROM shots WHERE id='s1'").fetchone()
    project = conn.execute("SELECT bible_json FROM projects WHERE id='p'").fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    return resolve_shot_asset_dependencies(
        project_id="p", episode_no=1, shot_id="s1", shot=_load_shot_model(shot_row),
        scene_name=None, conn=conn, bible=bible, screenplay=None,
    )


def _patch_get_conn_everywhere(monkeypatch, conn) -> None:
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)


def _pre_fix_shared_entities(items: list[dict], target_shot_id: str) -> set:
    """已删除的 ``_review_shared_asset_entities`` 逐字副本（独立观察点，不回退
    线上代码）：本镜自己捕获快照里、带真实 ``asset_version`` 的共享实体集合。"""
    return {
        (item.get("entity_type"), item.get("entity_name"))
        for item in items
        if item.get("shot_id") == target_shot_id and item.get("asset_version")
    }


def _pre_fix_assets_equal(expected: list[dict], current: list[dict], target_shot_id: str) -> bool:
    """已删除的 ``_review_asset_contract``+子集判定逐字副本：用于证明第二次
    事故场景在修复前确实会被误判过期（红），修复后不会（绿）。"""
    shared = _pre_fix_shared_entities(expected, target_shot_id)

    def contract(items: list[dict]) -> set:
        return {
            json.dumps(
                {k: v for k, v in item.items() if k not in {"version_id", "ref_id"}},
                ensure_ascii=False, sort_keys=True,
            )
            for item in items
            if item.get("shot_id") != target_shot_id
            and item.get("asset_version")
            and (item.get("entity_type"), item.get("entity_name")) in shared
        }

    return bool(not expected or contract(expected) <= contract(current))


def test_sibling_gallery_view_switch_does_not_fence(monkeypatch, tmp_path) -> None:
    """复现第二次事故（同集段 15/16/17）：共享人物的兄弟镜把画廊从"全身定妆照"
    换成"头像照"——entity 身份不变，但 ``asset_version`` 的取值来源从
    ``library_revision_id`` 换成 ``library_view_id``，旧判据（即便已经收窄到
    共享实体）仍会把这判成"共享资产换版本"而拦住毫不相关的本镜任务。

    新判据只认本镜自己捕获/冻结的 ``reference_manifest``，与兄弟镜画廊完全
    解耦：s1 的库状态（人物甲的定妆照）全程未变，worker_start 必须放行。
    """
    conn = _conn()
    _seed_bible_character(conn, "角色甲")
    conn.execute("UPDATE shots SET characters=? WHERE id='s1'", (json.dumps(["角色甲"]),))
    _add_sibling_shot(conn, "s2", 2)
    portrait_id = _seed_character_portrait(conn, tmp_path, name="角色甲", episode_no=1, suffix="v1")
    conn.commit()
    _patch_get_conn_everywhere(monkeypatch, conn)

    frozen = _resolve_s1_manifest(conn)
    own_reference = {
        "id": "own-ref", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "character", "entity_name": "角色甲", "library_revision_id": portrait_id,
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-own','s1',1,'p','own-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [own_reference]}),),
    )
    conn.execute("UPDATE shots SET adopted_version_id='v-own' WHERE id='s1'")
    full_body_reference = {
        "id": "shared-ref", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "character", "entity_name": "角色甲", "library_revision_id": portrait_id,
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-other','s2',1,'p','other-key','succeeded',?,0)""",
        (json.dumps({"reference_images": [full_body_reference]}),),
    )
    conn.commit()
    from app import api
    before_assets = _capture(api._review_upstream_snapshot("e"))["asset_inputs"]
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',2,'p','current-key','running',?,1)""",
        (json.dumps({
            "reference_manifest": frozen,
            "review_dependency_snapshot": _capture(api._review_upstream_snapshot("e")),
        }),),
    )
    # s2 重新生成，把同一人物换成头像照：entity 没变，asset_version 换了来源。
    headshot_reference = {
        "id": "shared-ref-2", "selectedForSeedance": True, "gate_status": "library",
        "entity_type": "character", "entity_name": "角色甲", "library_view_id": f"{portrait_id}_headshot",
    }
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v-other'",
        (json.dumps({"reference_images": [headshot_reference]}),),
    )
    conn.commit()
    after_assets = _capture(api._review_upstream_snapshot("e"))["asset_inputs"]

    # 独立观察点：修复前的判据（逐字副本）在这组数据上确实会误判过期（红）。
    assert _pre_fix_assets_equal(before_assets, after_assets, "s1") is False

    # 修复后：新判据与兄弟镜画廊无关，必须放行（绿）。
    worker._assert_review_dependency_fence(
        {"episode_id": "e", "shot_id": "s1", "project_id": "p"}, "v-current", "worker_start",
    )


def test_own_portrait_replaced_still_fences(monkeypatch, tmp_path) -> None:
    """真过期必须继续拦：本镜自己依赖的定妆照被替换成新 portrait（新库版本
    号），与任何兄弟镜无关——fail-closed 语义不因本次改动削弱。"""
    conn = _conn()
    _seed_bible_character(conn, "角色甲")
    conn.execute("UPDATE shots SET characters=? WHERE id='s1'", (json.dumps(["角色甲"]),))
    _seed_character_portrait(conn, tmp_path, name="角色甲", episode_no=1, suffix="v1")
    conn.commit()
    _patch_get_conn_everywhere(monkeypatch, conn)

    frozen = _resolve_s1_manifest(conn)
    from app import api
    captured = _capture(api._review_upstream_snapshot("e"))
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',1,'p','current-key','running',?,1)""",
        (json.dumps({"reference_manifest": frozen, "review_dependency_snapshot": captured}),),
    )
    conn.commit()
    # 定妆照被替换成新 portrait：删除旧行（级联删视角），同一集区间插入新行。
    conn.execute("DELETE FROM character_portraits WHERE character_name='角色甲'")
    conn.commit()
    _seed_character_portrait(conn, tmp_path, name="角色甲", episode_no=1, suffix="v2")
    conn.commit()

    with pytest.raises(worker.ReviewDependencyFence) as fenced:
        worker._assert_review_dependency_fence(
            {"episode_id": "e", "shot_id": "s1", "project_id": "p"}, "v-current", "worker_start",
        )
    assert "REVIEW_DEPENDENCY_STALE" in str(fenced.value)


def test_own_scene_reference_replaced_still_fences(monkeypatch, tmp_path) -> None:
    """真过期必须继续拦：本镜自己依赖的场景参考图被替换，与任何兄弟镜无关。"""
    conn = _conn()
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p'",
        (Bible(characters=[], world=World(visual_style_canonical="写实")).model_dump_json(),),
    )
    conn.execute("UPDATE shots SET scene_name='出租屋' WHERE id='s1'")
    _seed_scene_reference(conn, tmp_path, name="出租屋", episode_no=1, suffix="v1")
    conn.commit()
    _patch_get_conn_everywhere(monkeypatch, conn)

    frozen = _resolve_s1_manifest(conn)
    from app import api
    captured = _capture(api._review_upstream_snapshot("e"))
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',1,'p','current-key','running',?,1)""",
        (json.dumps({"reference_manifest": frozen, "review_dependency_snapshot": captured}),),
    )
    conn.commit()
    conn.execute("DELETE FROM scene_references WHERE scene_name='出租屋'")
    conn.commit()
    _seed_scene_reference(conn, tmp_path, name="出租屋", episode_no=1, suffix="v2")
    conn.commit()

    with pytest.raises(worker.ReviewDependencyFence) as fenced:
        worker._assert_review_dependency_fence(
            {"episode_id": "e", "shot_id": "s1", "project_id": "p"}, "v-current", "worker_start",
        )
    assert "REVIEW_DEPENDENCY_STALE" in str(fenced.value)


def test_own_shot_contract_edit_without_library_change_does_not_fence(monkeypatch, tmp_path) -> None:
    """单镜合同在入队后被编辑（不碰库资产、不提升 storyboard_revision）：
    worker_start 仍须放行，不能被自己"后来"的合同编辑误判成 asset_drift。

    2026-10-02 代码评审复现：视角选择（``select_character_view_roles``）依赖
    ``shot.risk_tags`` 的 ``contact_phase`` 标签；``_review_shot_manifest_equal``
    若不像 ``app.media_exec.input_reference._prepare_reference_mode_inputs_impl``
    那样用本版本冻结的 ``shot_contract_json`` 覆盖 shots 行当前值，单镜编辑
    接口（``storyboard_ops.edit_shot`` 只改 ``shot_contract_json``，不联动
    提升 ``storyboard_production_revision_id``）就会让这里重算出不同的视角
    选择，把一次与库资产无关的编辑误判成 ``REVIEW_DEPENDENCY_STALE``。
    """
    conn = _conn()
    _seed_bible_character(conn, "角色甲")
    frozen_contract = json.dumps({"risk_tags": ["contact_phase:approach"]})
    conn.execute(
        "UPDATE shots SET characters=?, shot_contract_json=? WHERE id='s1'",
        (json.dumps(["角色甲"]), frozen_contract),
    )
    portrait_id = _seed_character_portrait(conn, tmp_path, name="角色甲", episode_no=1, suffix="v1")
    profile_path = tmp_path / "portrait-v1-profile.png"
    profile_path.write_bytes(b"x")
    conn.execute(
        """INSERT INTO character_portrait_views(
               id,portrait_id,view_role,status,selected,input_fingerprint,image_path,created_at
           ) VALUES(?,?,'profile','ready',1,?,?,0)""",
        (f"{portrait_id}_profile", portrait_id, "fp-v1-profile", str(profile_path)),
    )
    conn.commit()
    _patch_get_conn_everywhere(monkeypatch, conn)

    # 入队时冻结：risk_tags 仍是 contact_phase:approach，视角选中 profile。
    frozen = _resolve_s1_manifest(conn)
    from app.multiview import manifest_asset_view_fingerprints
    assert ("character", "角色甲", "profile") in manifest_asset_view_fingerprints(frozen)
    from app import api
    captured = _capture(api._review_upstream_snapshot("e"))
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v-current','s1',1,'p','current-key','running',?,1)""",
        (json.dumps({
            "reference_manifest": frozen,
            "review_dependency_snapshot": captured,
            "shot_contract_json": frozen_contract,
        }),),
    )
    conn.commit()
    # 单镜编辑：去掉接触标签——不碰库资产、不提升 storyboard_revision。
    conn.execute(
        "UPDATE shots SET shot_contract_json=? WHERE id='s1'",
        (json.dumps({"risk_tags": []}),),
    )
    conn.commit()

    worker._assert_review_dependency_fence(
        {"episode_id": "e", "shot_id": "s1", "project_id": "p"}, "v-current", "worker_start",
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
