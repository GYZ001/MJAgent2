"""``app.domain.storyboard_ops.staleness._shot_adopted_assets_stale`` 单测。

背景（2026-09-30 只读复核证实，见派单）：分镜包分支（``app.multiview.
_storyboard_pack_asset_dependencies``）没有走真正的多视角生成，把当前定妆照/
场景图的 ``portrait_id``/``scene_reference_id`` 本身当"视角指纹"塞进冻结的
``reference_manifest.characters[].selected_views[].input_fingerprint``。旧版
``_shot_adopted_assets_stale`` 拿它去跟 ``character_portrait_views``/
``scene_reference_views`` 表里的真实内容哈希列比较，两者形态天然不可能相等，
于是资产其实没变也恒判 stale——生产库按同一逻辑复算，proj_ca86b15ab7d7 第 1
集已采用的 23 段修复前全部被 ``_shot_adopted_assets_stale`` 误报「人物或场景
资产版本已变更」。

本文件覆盖四类判据：未变→不报、定妆照/场景图真换了→报、视角指纹就是
portrait_id 的分镜包段不误报（即便 character_portrait_views 表里恰好也有一条
内容哈希完全不同的行，也不能被拿去比较）、以及一个已知残留（见
``test_legacy_reverse_angle_view_id_as_fingerprint_stays_stale``）：本次修复
只改变以后新生成时写入的指纹，不回填已落库的历史 manifest，
proj_ca86b15ab7d7 第 1 集这 23 段里仍有 8 段（shot 12/14/17/19/20/21/22/23，
反打视角历史指纹=视角行 id）在修复后继续被判 stale，需要重新采用该镜头或走
一次显式授权的数据回填迁移才能清零，两者都不在本次任务范围内——只读复核以
``_shot_adopted_assets_stale`` 直接复算：23/23 → 8/23。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import db as db_mod
from app.domain.storyboard_ops.staleness import _shot_adopted_assets_stale


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db_mod.SCHEMA)
    for statement in db_mod.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


@pytest.fixture
def conn(monkeypatch):
    """``character_multiview_enabled``/``current_portrait_ref`` 默认参数都会
    回退 ``app.db.get_conn()``（settings 读取、以及本文件里不显式传 conn 的
    调用点一概没有），必须打桩成同一个内存连接，否则会去连本机磁盘上的真实
    开发库。"""
    c = _memory_conn()
    monkeypatch.setattr(db_mod, "get_conn", lambda: c)
    yield c
    c.close()


def _seed_episode(conn, *, project_id="proj_1", episode_id="ep_1", episode_no=2):
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?, 'created', 1)",
        (project_id, "测试项目"),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, status, created_at) "
        "VALUES(?,?,?, 'ep', 'confirmed', 1)",
        (episode_id, project_id, episode_no),
    )
    return project_id, episode_id


def _seed_shot_and_version(conn, *, shot_id, episode_id, version_id, manifest: dict) -> None:
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, adopted_version_id) "
        "VALUES(?,?,1,5,?)",
        (shot_id, episode_id, version_id),
    )
    image_inputs = json.dumps({"reference_manifest": manifest}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, "
        "created_at, image_inputs) VALUES(?,?,1,'p',?, 'succeeded', 1, ?)",
        (version_id, shot_id, f"idem-{version_id}", image_inputs),
    )


def _seed_portrait(
    conn, *, portrait_id, project_id, name, identity_id, image_path, ep_start=1, ep_end=None,
) -> None:
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, visual_entity_id, "
        "ep_start, ep_end, appearance, image_path, bible_version, created_at) "
        "VALUES(?,?,?,?,?,?, 'a', ?, 1, 1)",
        (portrait_id, project_id, name, identity_id, ep_start, ep_end, image_path),
    )


def _seed_portrait_view(conn, *, portrait_id, view_role, input_fingerprint) -> None:
    conn.execute(
        "INSERT INTO character_portrait_views(id, portrait_id, view_role, status, selected, "
        "input_fingerprint, created_at) VALUES(?,?,?, 'ready', 1, ?, 1)",
        (f"{portrait_id}_{view_role}", portrait_id, view_role, input_fingerprint),
    )


def _seed_scene(conn, *, scene_id, project_id, name, ep_start=1, ep_end=None) -> None:
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "scene_canonical, image_path, bible_version, created_at) "
        "VALUES(?,?,?,?,?, 's', 'y.png', 1, 1)",
        (scene_id, project_id, name, ep_start, ep_end),
    )


def _seed_scene_view(conn, *, scene_reference_id, view_role, input_fingerprint) -> None:
    conn.execute(
        "INSERT INTO scene_reference_views(id, scene_reference_id, view_role, status, selected, "
        "input_fingerprint, created_at) VALUES(?,?,?, 'ready', 1, ?, 1)",
        (f"{scene_reference_id}_{view_role}", scene_reference_id, view_role, input_fingerprint),
    )


def _character_entry(*, name, identity_id, portrait_id):
    """分镜包分支冻结形状：``selected_views[0].input_fingerprint`` 就是
    ``look_revision_id``（portrait_id）本身，不是真实多视角内容哈希——见
    ``app.multiview._storyboard_pack_asset_dependencies``。"""
    return {
        "name": name, "identity_id": identity_id, "asset_name": name,
        "role_kind": "storyboard_pack", "asset_required": True,
        "look_revision_id": portrait_id,
        "selected_view_ids": [portrait_id],
        "selected_views": [{
            "id": portrait_id, "view_role": "front_full", "image_path": "x.png",
            "input_fingerprint": portrait_id, "purposes": ["keyframe_seed"],
        }],
        "available_view_roles": ["front_full"], "missing_required": [],
    }


def _scene_entry(*, name, scene_reference_id):
    return {
        "name": name, "asset_required": True, "scene_revision_id": scene_reference_id,
        "asset_usable": True, "primary_usable": True,
        "selected_view_ids": [scene_reference_id],
        "selected_views": [{
            "id": scene_reference_id, "view_role": "establishing", "image_path": "y.png",
            "input_fingerprint": scene_reference_id, "purposes": ["keyframe_seed"],
        }],
        "available_view_roles": ["establishing"], "missing_required": [],
    }


def test_unchanged_character_and_scene_not_stale(conn, tmp_path):
    """未变→不报：分镜包冻结的 portrait/scene 仍是本集当前生效版本。"""
    image = tmp_path / "portrait_a.png"
    image.write_bytes(b"x")
    project_id, episode_id = _seed_episode(conn)
    _seed_portrait(
        conn, portrait_id="portrait_a", project_id=project_id, name="甲",
        identity_id="bible:甲", image_path=str(image),
    )
    _seed_scene(conn, scene_id="scene_a", project_id=project_id, name="客厅")
    manifest = {
        "characters": [_character_entry(name="甲", identity_id="bible:甲", portrait_id="portrait_a")],
        "scene": _scene_entry(name="客厅", scene_reference_id="scene_a"),
        "additional_scenes": [],
    }
    _seed_shot_and_version(conn, shot_id="shot_1", episode_id=episode_id, version_id="ver_1", manifest=manifest)
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is False


def test_portrait_view_fingerprint_equal_to_portrait_id_not_misreported(conn, tmp_path):
    """视角指纹=portrait_id 的分镜包段不误报：即便 character_portrait_views 表
    里恰好有一条内容哈希完全不同的 ready 行（真实生产数据形态），也不能被拿
    去跟冻结的伪视角指纹比较——那条比较本身就是错的，不是「指纹恰好没变」。"""
    image = tmp_path / "portrait_a.png"
    image.write_bytes(b"x")
    project_id, episode_id = _seed_episode(conn)
    _seed_portrait(
        conn, portrait_id="portrait_a", project_id=project_id, name="甲",
        identity_id="bible:甲", image_path=str(image),
    )
    _seed_portrait_view(
        conn, portrait_id="portrait_a", view_role="front_full",
        input_fingerprint="674f8f8f6aa30c1e9434625baf6db4d1e25fbd8e",
    )
    manifest = {
        "characters": [_character_entry(name="甲", identity_id="bible:甲", portrait_id="portrait_a")],
        "scene": None, "additional_scenes": [],
    }
    _seed_shot_and_version(conn, shot_id="shot_1", episode_id=episode_id, version_id="ver_1", manifest=manifest)
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is False


def test_portrait_actually_replaced_is_stale(conn, tmp_path):
    """定妆照真换了→报：冻结的 portrait_a 已被覆盖本集（episode_no=2）的新
    portrait_b 取代。"""
    image_a = tmp_path / "portrait_a.png"
    image_a.write_bytes(b"x")
    image_b = tmp_path / "portrait_b.png"
    image_b.write_bytes(b"y")
    project_id, episode_id = _seed_episode(conn, episode_no=2)
    _seed_portrait(
        conn, portrait_id="portrait_a", project_id=project_id, name="甲", identity_id="bible:甲",
        image_path=str(image_a), ep_start=1, ep_end=1,
    )
    _seed_portrait(
        conn, portrait_id="portrait_b", project_id=project_id, name="甲", identity_id="bible:甲",
        image_path=str(image_b), ep_start=2, ep_end=None,
    )
    manifest = {
        "characters": [_character_entry(name="甲", identity_id="bible:甲", portrait_id="portrait_a")],
        "scene": None, "additional_scenes": [],
    }
    _seed_shot_and_version(conn, shot_id="shot_1", episode_id=episode_id, version_id="ver_1", manifest=manifest)
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is True


def test_scene_actually_replaced_is_stale(conn):
    """场景同理：冻结的 scene_a 已被覆盖本集（episode_no=2）的新 scene_b 取代。"""
    project_id, episode_id = _seed_episode(conn, episode_no=2)
    _seed_scene(conn, scene_id="scene_a", project_id=project_id, name="客厅", ep_start=1, ep_end=1)
    _seed_scene(conn, scene_id="scene_b", project_id=project_id, name="客厅", ep_start=2, ep_end=None)
    manifest = {
        "characters": [], "scene": _scene_entry(name="客厅", scene_reference_id="scene_a"),
        "additional_scenes": [],
    }
    _seed_shot_and_version(conn, shot_id="shot_1", episode_id=episode_id, version_id="ver_1", manifest=manifest)
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is True


def test_no_reference_manifest_not_stale(conn):
    """旧采用版没有冻结 reference_manifest 时不判 stale（没有可比较的数据，不
    是「恒不报」——判据仍然是数据里有没有东西可比）。"""
    project_id, episode_id = _seed_episode(conn)
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, adopted_version_id) "
        "VALUES('shot_1', ?, 1, 5, 'ver_1')",
        (episode_id,),
    )
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, created_at) "
        "VALUES('ver_1', 'shot_1', 1, 'p', 'idem-1', 'succeeded', 1)",
    )
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is False


def test_legacy_reverse_angle_view_id_as_fingerprint_stays_stale(conn):
    """已知残留、非本次可修（2026-09-30 只读复核证实）：``app.scene_reverse.
    segment_views`` 曾把反打视角的 ``input_fingerprint`` 错写成视角行自己的
    ``id``（真实内容哈希另见该模块修复）。这个错误在修复前已经把值冻结进一批
    历史采用版的 ``image_inputs.reference_manifest``，修复只改变以后新生成时
    写入的值，不会、也不能回填已经落库的历史记录。

    场景本身（``scene_revision_id``）没有换，走到 revision 比较这一关不会报；
    但反打视角条目里 ``input_fingerprint`` 是视角行 id（形如
    ``sview_xxx``），既不等于 ``scene_revision_id``（所以不会被
    ``_selected_views_stale`` 的「伪视角」跳过规则命中），也不可能等于
    ``scene_reference_views.input_fingerprint`` 现状里的真实内容哈希——于是
    这类镜头会被永久判 stale，直到该镜头被重新采用（自然刷新 manifest）或对
    这批历史数据做一次显式授权的回填迁移，两者都不在本次任务范围内。

    本测试把这个残留锁定为当前代码的预期行为：断言仍是 ``True``，防止未来有
    人把这里也塞进 ``_selected_views_stale`` 的跳过条件——那会把「真的换了反
    打图」也一起吞掉。生产复核样本：proj_ca86b15ab7d7 第 1 集 shot
    12/14/17/19/20/21/22/23。"""
    project_id, episode_id = _seed_episode(conn)
    _seed_scene(conn, scene_id="scene_a", project_id=project_id, name="客厅")
    _seed_scene_view(
        conn, scene_reference_id="scene_a", view_role="reverse_angle",
        input_fingerprint="28518bc9673c0d199fa245285770157b7d983a2d",
    )
    manifest = {
        "characters": [],
        "scene": {
            "name": "客厅", "asset_required": True, "scene_revision_id": "scene_a",
            "selected_view_ids": ["legacy_view_id_x"],
            "selected_views": [{
                "id": "legacy_view_id_x", "view_role": "reverse_angle", "image_path": "z.png",
                "input_fingerprint": "legacy_view_id_x", "purposes": ["qa_anchor"],
            }],
            "available_view_roles": ["reverse_angle"], "missing_required": [],
        },
        "additional_scenes": [],
    }
    _seed_shot_and_version(conn, shot_id="shot_1", episode_id=episode_id, version_id="ver_1", manifest=manifest)
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is True


def _seed_prop_reference(conn, *, project_id, episode_no, created_at) -> None:
    """按真实 ``prop_references`` 表结构插入一行，驱动 ``_prop_asset_updated_
    after`` 的真实查询路径（``prop_reference_for_episode``），不手工伪造比较
    结果——与 ``tests/test_asset_refresh_prop_revision_fallback.py`` 同一手法。"""
    from app.props.store import ensure_tables_on_connection

    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_references(id, project_id, prop_name, ep_start, ep_end, "
        "appearance, image_path, prompt, status, qa_json, created_at) "
        "VALUES('propref-1', ?, '马克杯', 1, NULL, 'a', '/tmp/x.png', 'p', 'ready', '{}', ?)",
        (project_id, created_at),
    )


def test_props_stale_missing_revision_key_regenerated_after_video_is_stale(conn, monkeypatch):
    """``_props_stale`` 对缺 ``prop_revision_id`` 键的冻结道具条目要走与
    ``app.domain.video_ops.asset_drift`` 同一份时间判据回退，不是另起一套：
    道具卡最近一次登记/重出图的时间晚于这个采用版本的生成时间，必须判
    stale——否则生成台「过期」提示会跟参考资产面板的说法不一致（CLAUDE.md
    「同一件事界面只有一种说法」；2026-10-04 生产实测：《顾念长安》"浅灰色
    卫衣"卡重出图后，缺这个字段的旧采用版本曾被两边一起漏报）。"""
    import app.multiview as multiview_mod
    import app.video_modes.prop_references as prop_references_mod

    monkeypatch.setattr(multiview_mod, "character_multiview_enabled", lambda: False)
    monkeypatch.setattr(multiview_mod, "scene_multiview_enabled", lambda: False)
    monkeypatch.setattr(
        prop_references_mod, "resolve_segment_prop_manifest_entries",
        lambda entries, *, conn, project_id, episode_no: [
            {"label": entries[0]["label"], "ready": True, "prop_revision_id": "rev-current"},
        ],
    )
    project_id, episode_id = _seed_episode(conn)
    manifest = {
        "characters": [], "scene": None, "additional_scenes": [],
        "props": [{"label": "马克杯", "ready": True}],
    }
    _seed_shot_and_version(conn, shot_id="shot_1", episode_id=episode_id, version_id="ver_1", manifest=manifest)
    _seed_prop_reference(conn, project_id=project_id, episode_no=2, created_at=100)
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is True


def test_props_stale_missing_revision_key_untouched_before_video_stays_fresh(conn, monkeypatch):
    """同样缺 ``prop_revision_id`` 键，但道具卡最近一次生效时间早于视频生成
    时间（从未重新登记过的旧卡）——必须维持「不变」，不能仅因为代码升级补了
    新字段就把一直没变过的道具判成过期（与
    ``test_asset_refresh_prop_revision_fallback`` 同口径的对称用例）。"""
    import app.multiview as multiview_mod
    import app.video_modes.prop_references as prop_references_mod

    monkeypatch.setattr(multiview_mod, "character_multiview_enabled", lambda: False)
    monkeypatch.setattr(multiview_mod, "scene_multiview_enabled", lambda: False)
    monkeypatch.setattr(
        prop_references_mod, "resolve_segment_prop_manifest_entries",
        lambda entries, *, conn, project_id, episode_no: [
            {"label": entries[0]["label"], "ready": True, "prop_revision_id": "rev-current"},
        ],
    )
    project_id, episode_id = _seed_episode(conn)
    manifest = {
        "characters": [], "scene": None, "additional_scenes": [],
        "props": [{"label": "马克杯", "ready": True}],
    }
    _seed_shot_and_version(conn, shot_id="shot_1", episode_id=episode_id, version_id="ver_1", manifest=manifest)
    _seed_prop_reference(conn, project_id=project_id, episode_no=2, created_at=0.5)
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is False


def test_prop_drift_detected_even_when_character_and_scene_multiview_disabled(conn, monkeypatch):
    """两个多视角开关（``character_multiview_enabled``/``scene_multiview_
    enabled``）都被关掉时，道具漂移仍必须被判 stale——``_props_stale`` 判的
    是道具库状态，与"人物/场景要不要走多视角"这件事无关，不能被这两个开关
    连带挂起（2026-10-03 用仓库内真实函数复现：构造 ready False→True 的真实
    道具漂移，关闭两个开关后旧实现返回 False，应为 True）。"""
    import app.multiview as multiview_mod
    import app.video_modes.prop_references as prop_references_mod

    monkeypatch.setattr(multiview_mod, "character_multiview_enabled", lambda: False)
    monkeypatch.setattr(multiview_mod, "scene_multiview_enabled", lambda: False)
    monkeypatch.setattr(
        prop_references_mod, "resolve_segment_prop_manifest_entries",
        lambda entries, *, conn, project_id, episode_no: [
            {"label": entries[0]["label"], "ready": True, "prop_revision_id": "rev-new"},
        ],
    )
    _, episode_id = _seed_episode(conn)
    manifest = {
        "characters": [], "scene": None, "additional_scenes": [],
        "props": [{"label": "马克杯", "ready": False, "prop_revision_id": None}],
    }
    _seed_shot_and_version(conn, shot_id="shot_1", episode_id=episode_id, version_id="ver_1", manifest=manifest)
    conn.commit()

    shot_row = conn.execute("SELECT * FROM shots WHERE id='shot_1'").fetchone()
    ver_row = conn.execute("SELECT * FROM shot_versions WHERE id='ver_1'").fetchone()
    assert _shot_adopted_assets_stale(conn, shot_row, ver_row) is True
