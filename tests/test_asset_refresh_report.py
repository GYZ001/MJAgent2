"""「参考资产已更新」面板的后端：按实体成组的影响分析、成组重生成与整组
原子采纳。分组/状态判据用真实 ``resolve_shot_asset_dependencies`` 路径 +
构造的冻结清单（不手工伪造比较结果，CLAUDE.md「判据要用真实 resolve 路径」）；
``_prop_reference_lookup`` 按既有先例 monkeypatch（见
``tests/test_prop_reference_assets.py``），不依赖 ``app.props`` 真实表结构。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import HTTPException

from app.domain.video_ops import asset_drift as drift
from app.domain.video_ops import asset_refresh
from app.domain.video_ops.asset_refresh_report import episode_asset_refresh_groups
import app.video_modes.prop_references as prop_references
from tests.conftest import patch_api_everywhere, patch_worker_everywhere
from tests.test_review_wall_prd import _conn


# ---------------------------------------------------------------------------
# entity_diff：纯字典判据（人物/场景的新增/更新/移除），不手工伪造比较结果，
# 直接用真实 ``entity_diff``——只测它接收的输入形状。
# ---------------------------------------------------------------------------

def test_entity_diff_character_newly_available_is_added() -> None:
    frozen = {"characters": [{"name": "温念", "look_revision_id": None}]}
    current = {"characters": [{"name": "温念", "look_revision_id": "portrait-1"}]}
    diffs = drift.entity_diff(frozen, current)
    assert diffs == [{
        "entity_key": "character:温念", "entity_type": "character", "entity_name": "温念",
        "category": "added", "category_label": "新增参考图",
    }]


def test_entity_diff_scene_revision_change_is_updated() -> None:
    frozen = {"scene": {"name": "咖啡馆", "scene_revision_id": "rev1"}}
    current = {"scene": {"name": "咖啡馆", "scene_revision_id": "rev2"}}
    diffs = drift.entity_diff(frozen, current)
    assert diffs == [{
        "entity_key": "scene:咖啡馆", "entity_type": "scene", "entity_name": "咖啡馆",
        "category": "updated", "category_label": "参考图已更新",
    }]


def test_entity_diff_character_no_longer_resolved_is_removed() -> None:
    frozen = {"characters": [{"name": "顾屿", "look_revision_id": "portrait-9"}]}
    current = {"characters": []}
    diffs = drift.entity_diff(frozen, current)
    assert diffs == [{
        "entity_key": "character:顾屿", "entity_type": "character", "entity_name": "顾屿",
        "category": "removed", "category_label": "参考被移除",
    }]


def test_entity_diff_ignores_view_fingerprint_only_noise() -> None:
    """2026-10-03 B 库实测：新增视角导致选中视角切换，revision id 不变——
    不应被判成任何差异（见模块 docstring 的假阳性说明）。"""
    frozen = {"characters": [{
        "name": "温念", "look_revision_id": "portrait-1",
        "selected_views": [{"view_role": "front_full", "input_fingerprint": "fp-a"}],
    }]}
    current = {"characters": [{
        "name": "温念", "look_revision_id": "portrait-1",
        "selected_views": [{"view_role": "face_closeup", "input_fingerprint": "fp-b"}],
    }]}
    assert drift.entity_diff(frozen, current) == []


def test_entity_diff_no_change_is_empty() -> None:
    manifest = {
        "characters": [{"name": "温念", "look_revision_id": "portrait-1"}],
        "scene": {"name": "咖啡馆", "scene_revision_id": "rev1"},
        "props": [{"label": "马克杯", "ready": True, "prop_revision_id": "pr1"}],
    }
    assert drift.entity_diff(manifest, manifest) == []

_SEGMENT = {"resources": {"characters": [], "scenes": [], "props": [{"label": "马克杯", "description": ""}]}}


def _prop_manifest(*, ready: bool, revision_id: str | None, order: int = 0) -> dict:
    return {
        "episode_no": 1, "shot_id": "ignored", "characters": [], "scene": None, "additional_scenes": [],
        "props": [{
            "label": "马克杯", "description": "", "ready": ready, "image_path": "",
            "resources_order": order, "prop_revision_id": revision_id,
        }],
    }


def _insert_pack_shot(
    conn, *, shot_id: str, shot_no: int, adopted_version_id: str | None, duration_s: float = 15,
    segment: dict | None = None,
) -> None:
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,action_desc,characters,dialogues,storyboard_artifact_id,
               shot_contract_json,adopted_version_id
           ) VALUES(?,'e',?,?,'中景','固定','日，测试室内场景','action','[]','[]','board-1',?,?)""",
        (shot_id, shot_no, duration_s, json.dumps({"storyboard_pack_segment": segment or _SEGMENT}), adopted_version_id),
    )


def _insert_version(
    conn, *, version_id: str, shot_id: str, version_no: int, frozen_manifest: dict,
    status: str = "succeeded", video_path: str = "/tmp/x.mp4", technical: dict | None = None,
    segment: dict | None = None,
) -> None:
    image_inputs = {"reference_manifest": frozen_manifest, "shot_contract_json": {"storyboard_pack_segment": segment or _SEGMENT}}
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,
               technical_validation_json,image_inputs,created_at
           ) VALUES(?,?,?,'p','idem',?,?,?,?,?)""",
        (version_id, shot_id, version_no, status, video_path, json.dumps(technical or {}), json.dumps(image_inputs), version_no),
    )


def _lookup_stub(image_path: str, revision_id: str):
    return lambda c, project_id, name, episode_no: (
        {"id": revision_id, "status": "ready", "image_path": image_path} if name == "马克杯" else None
    )


def _multi_lookup_stub(table: dict[str, tuple[str, str]]):
    """同 ``_lookup_stub``，支持多个道具标签各自独立的 (image_path, revision_id)。"""
    def _lookup(c, project_id, name, episode_no):
        hit = table.get(name)
        return {"id": hit[1], "status": "ready", "image_path": hit[0]} if hit else None
    return _lookup


def _episode_row(conn):
    return conn.execute("SELECT * FROM episodes WHERE id='e'").fetchone()


def _fresh_conn() -> sqlite3.Connection:
    """``_conn()`` 自带一个占位 ``s1``（分镜确认资格测试用），本文件自己构造
    分镜包段落，先清空避免 shot_no 唯一约束冲突；``bible_json`` 补成最小合法
    值——``_conn()`` 本身留空，``episode_bible_and_screenplay`` 要求能
    ``Bible.model_validate``。"""
    from app.schemas import Bible, World

    conn = _conn()
    conn.execute("DELETE FROM shots")
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='p'",
        (Bible(characters=[], world=World(visual_style_canonical="写实")).model_dump_json(),),
    )
    return conn


# ---------------------------------------------------------------------------
# 分组与状态：新增道具卡 / revision 变化 / 无变化
# ---------------------------------------------------------------------------

def test_newly_ready_prop_card_groups_as_added_and_needs_regen(monkeypatch, tmp_path) -> None:
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev1"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=False, revision_id=None))
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    assert [g["entity_key"] for g in report["groups"]] == ["prop:马克杯"]
    group = report["groups"][0]
    assert group["category"] == "added"
    assert len(group["members"]) == 1
    member = group["members"][0]
    assert member["shot_id"] == "s1" and member["status"] == "needs_regen"
    assert report["needs_regen_shot_count"] == 1
    assert report["needs_regen_seconds"] == 15


def test_prop_card_re_registration_groups_as_updated(monkeypatch, tmp_path) -> None:
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug2.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev2"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    assert len(report["groups"]) == 1
    assert report["groups"][0]["category"] == "updated"
    assert report["groups"][0]["members"][0]["status"] == "needs_regen"


def test_unchanged_prop_card_produces_no_groups(monkeypatch, tmp_path) -> None:
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug3.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev1"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    assert report["groups"] == []
    assert report["needs_regen_shot_count"] == 0
    assert report["needs_regen_seconds"] == 0


def test_existing_matching_candidate_marks_has_candidate_not_needs_regen(monkeypatch, tmp_path) -> None:
    """一段已有按最新参考生成的成功候选时不再计入需要重生成。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug4.png"
    image.write_bytes(b"x")
    video = tmp_path / "candidate.mp4"
    video.write_bytes(b"v")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev2"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    _insert_version(
        conn, version_id="v2", shot_id="s1", version_no=2,
        frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev2"), video_path=str(video),
    )
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    member = report["groups"][0]["members"][0]
    assert member["status"] == "has_candidate"
    # video_path 落在 pytest tmp_path 下，不在 config.PROJECTS_DIR 之下，
    # build_media_url 据其文档约定在这种情况下返回 None（见下方
    # test_candidate_includes_version_no_and_video_url 验证非 None 的真实情形）。
    assert member["candidates"] == [{"version_id": "v2", "version_no": 2, "created_at": 2, "video_url": None}]
    assert member["adopted_version_no"] == 1
    assert report["needs_regen_shot_count"] == 0


def test_candidate_includes_version_no_and_video_url(monkeypatch, tmp_path) -> None:
    """2026-10-03 用户发现候选下拉框只显示裸的 version_id（如 ver_xxx），既
    分不清版本也无法预览。候选必须带 version_no 与可播放的 video_url。"""
    from app import config as app_config

    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(app_config, "PROJECTS_DIR", projects_dir)
    image = tmp_path / "mug6.png"
    image.write_bytes(b"x")
    video_dir = projects_dir / "p" / "e"
    video_dir.mkdir(parents=True)
    video = video_dir / "candidate.mp4"
    video.write_bytes(b"v")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev2"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    _insert_version(
        conn, version_id="v2", shot_id="s1", version_no=2,
        frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev2"), video_path=str(video),
    )
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    candidate = report["groups"][0]["members"][0]["candidates"][0]
    assert candidate["version_no"] == 2
    assert candidate["video_url"] is not None
    assert candidate["video_url"].startswith("/media/p/e/candidate.mp4")


def test_multiple_candidates_ordered_newest_first_for_default_preselection(monkeypatch, tmp_path) -> None:
    """前端默认预选取 ``candidates[0]``，这里必须保证顺序是"最新候选排第一"，
    否则默认预选会悄悄选中最旧的候选而不是最新的。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug7.png"
    image.write_bytes(b"x")
    video_a = tmp_path / "a.mp4"
    video_a.write_bytes(b"v")
    video_b = tmp_path / "b.mp4"
    video_b.write_bytes(b"v")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev2"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    _insert_version(
        conn, version_id="v2", shot_id="s1", version_no=2,
        frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev2"), video_path=str(video_a),
    )
    _insert_version(
        conn, version_id="v3", shot_id="s1", version_no=3,
        frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev2"), video_path=str(video_b),
    )
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    candidates = report["groups"][0]["members"][0]["candidates"]
    assert [c["version_id"] for c in candidates] == ["v3", "v2"]


def test_not_adopted_member_has_null_adopted_version_no(monkeypatch, tmp_path) -> None:
    """未采用任何版本的段不该假造一个采用版本号；已采用的段要如实带上
    version_no，供前端展示"现采用 v{n}"与候选对比。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug9.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev2"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    _insert_pack_shot(conn, shot_id="s2", shot_no=2, adopted_version_id=None)
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    members = {m["shot_id"]: m for m in report["groups"][0]["members"]}
    assert members["s2"]["status"] == "not_adopted"
    assert members["s2"]["adopted_version_no"] is None
    assert members["s1"]["adopted_version_no"] == 1


def test_quota_sums_distinct_shots_without_double_counting(monkeypatch, tmp_path) -> None:
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug5.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev2"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1", duration_s=15)
    _insert_pack_shot(conn, shot_id="s2", shot_no=2, adopted_version_id="v2", duration_s=10)
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    _insert_version(conn, version_id="v2", shot_id="s2", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    assert report["needs_regen_shot_count"] == 2
    assert report["needs_regen_seconds"] == 25


# ---------------------------------------------------------------------------
# 成组重生成：只对需要的段发命令、闸门 409 透传
# ---------------------------------------------------------------------------

def _canned_report(shot_ids: list[str]) -> dict:
    return {"episode_id": "e", "groups": [{
        "entity_key": "prop:马克杯", "entity_type": "prop", "entity_name": "马克杯",
        "category": "added", "category_label": "新增参考图", "members": [],
        "needs_regen_shot_ids": shot_ids, "needs_regen_seconds": 15 * len(shot_ids),
    }], "needs_regen_shot_count": len(shot_ids), "needs_regen_seconds": 15 * len(shot_ids)}


async def test_regenerate_core_requires_idempotency_key(monkeypatch) -> None:
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    with pytest.raises(HTTPException) as exc_info:
        await asset_refresh._asset_refresh_regenerate_core("e", {})
    assert exc_info.value.status_code == 422


async def test_regenerate_core_dispatches_each_shot_and_collects_gate_409(monkeypatch) -> None:
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(asset_refresh, "episode_asset_refresh_groups", lambda c, ep: _canned_report(["s1", "s2"]))

    calls: list[dict] = []

    async def fake_dispatch(name, args, initiator="ui"):
        calls.append(args)
        if args["shot_id"] == "s2":
            raise HTTPException(409, "场景状态图未就绪")
        return object()

    import app.capabilities.dispatch as dispatch_module
    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    monkeypatch.setattr(dispatch_module, "respond_ui", lambda result: {"ok": True})

    out = await asset_refresh._asset_refresh_regenerate_core("e", {"idempotency_key": "batch-1"})
    assert out["queued"] == ["s1"]
    assert out["errors"] == [{"shot_id": "s2", "status_code": 409, "detail": "场景状态图未就绪"}]
    assert [c["idempotency_key"] for c in calls] == ["batch-1:s1", "batch-1:s2"]


# ---------------------------------------------------------------------------
# 整组原子采纳：任何一段不合格整组拒绝，全部合格才一次性提交
# ---------------------------------------------------------------------------

def _patch_evidence_get_conn(monkeypatch, conn) -> None:
    """``_adopt_version_apply`` 内部经由 ``app.evidence.media``/``app.evidence.
    repository`` 各自独立的 ``get_conn`` 绑定落地候选证据——不在
    ``patch_api_everywhere`` 的 ``app.domain`` 递归范围内（它们是
    ``app.evidence`` 包，不是 ``app.domain`` 子模块），需要单独点名打桩。"""
    import app.evidence.media as media_evidence
    import app.evidence.repository as evidence_repository

    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(evidence_repository, "get_conn", lambda: conn)


def _insert_adoptable_version(conn, *, version_id: str, shot_id: str, version_no: int, video_path: str) -> None:
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,
               technical_validation_json,qa_json,image_inputs,created_at
           ) VALUES(?,?,?,'p','idem','succeeded',?,'{}','{}','{}',0)""",
        (version_id, shot_id, version_no, video_path),
    )


async def test_adopt_core_rejects_whole_group_when_one_shot_fails_validation(monkeypatch, tmp_path) -> None:
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_worker_everywhere(monkeypatch, "invalidate_episode_final", lambda episode_id: None)
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id=None)
    _insert_pack_shot(conn, shot_id="s2", shot_no=2, adopted_version_id=None)
    video = tmp_path / "vgood.mp4"
    video.write_bytes(b"v")
    _insert_adoptable_version(conn, version_id="vgood", shot_id="s1", version_no=1, video_path=str(video))
    conn.commit()

    monkeypatch.setattr(
        asset_refresh, "candidate_adopt_check",
        lambda conn, shot_row, version_id, **kw: (True, "") if shot_row["id"] == "s1" else (False, "参考资产不是当前最新"),
    )

    with pytest.raises(HTTPException) as exc_info:
        asset_refresh._asset_refresh_adopt_core("e", {
            "entity_key": "prop:马克杯", "reason": "按新道具卡统一采用",
            "versions": {"s1": "vgood", "s2": "vmissing"},
        })
    assert exc_info.value.status_code == 409
    row = conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()
    assert row["adopted_version_id"] is None  # 一段不合格，整组不动——s1 也没被采用


async def test_adopt_core_adopts_all_atomically_and_invalidates_once(monkeypatch, tmp_path) -> None:
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    _patch_evidence_get_conn(monkeypatch, conn)
    invalidated: list[str] = []
    patch_worker_everywhere(monkeypatch, "invalidate_episode_final", lambda episode_id: invalidated.append(episode_id))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id=None)
    _insert_pack_shot(conn, shot_id="s2", shot_no=2, adopted_version_id=None)
    video1 = tmp_path / "v1.mp4"; video1.write_bytes(b"v")
    video2 = tmp_path / "v2.mp4"; video2.write_bytes(b"v")
    _insert_adoptable_version(conn, version_id="v1", shot_id="s1", version_no=1, video_path=str(video1))
    _insert_adoptable_version(conn, version_id="v2", shot_id="s2", version_no=1, video_path=str(video2))
    conn.commit()
    monkeypatch.setattr(asset_refresh, "candidate_adopt_check", lambda conn, shot_row, version_id, **kw: (True, ""))

    out = asset_refresh._asset_refresh_adopt_core("e", {
        "entity_key": "prop:马克杯", "reason": "按新道具卡统一采用",
        "versions": {"s1": "v1", "s2": "v2"},
    })
    assert {a["shot_id"]: a["version_id"] for a in out["adopted"]} == {"s1": "v1", "s2": "v2"}
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()["adopted_version_id"] == "v1"
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s2'").fetchone()["adopted_version_id"] == "v2"
    assert invalidated == ["e"]
