"""场景库「整包重生」（已有场景改描述后请求重出场景图）的原子切换回归测试。

真实故障：2026-09-28 生产 proj_ca86b15ab7d7，run_d185a4e13006——PUT 改了两张
场景卡的 scene_canonical（返回 pending_redraw），随后 POST /scene-refs 整包重出。
provider_calls（image_generate/image_edit/chat）全部 OK，artifacts 也确实生成了
approved 记录，但 scene_references 当前行从未真的换成新图、change_json 仍卡在
pending_redraw:true，而 workflow_runs/step_runs 却报 SUCCEEDED，界面没有任何
可见信号。此前另一次同样的请求（run_954ce9506caa）结果完全一样，稳定复现。

根因：``app.scenes._generate_one_scene_reference`` 把候选图插入负数候选槽位
（``candidate_start``）、QA/多视角包通过后，把旧当前版本挪去历史槽位时重新
查询 ``MIN(ep_start<=0 AND id<>候选自己)``——候选是本场景【唯一】一条负值行
时（任何从未整包重生过的已有场景，第一次整包重生必然是这个状态），排除掉
候选自己后 MIN 为空，退回默认值 ``0-1=-1``，与候选此刻仍占着的 ``-1`` 撞上
``UNIQUE(project_id,scene_name,ep_start)`` 约束，UPDATE 语句本身直接
``IntegrityError``。这个异常被外层 ``except Exception`` 当作可重试的生成失败
处理：候选被 ``purge_scene_reference`` 物理删除（文件也被删），重试到第二次
仍然是同一个必然结果，两次耗尽后作为「本场景重试耗尽」记进
``generate_scene_refs`` 返回值的 ``warnings``；但 ``asyncio.gather(...,
return_exceptions=True)`` 让整批调用本身不再向上抛异常，
``app.domain.bible_ops.scene_bible_prep._scene_refs_task`` 此前又完全不读
``recorder.step(...)`` 的返回值，于是 run/step 全部落地 SUCCEEDED。

两处修复：
1. ``app/scenes.py``：``history_start`` 直接用 ``candidate_start - 1``，不再
   重新查询会与候选自身撞车的 MIN。
2. ``app/domain/bible_ops/scene_bible_prep.py``：``_scene_refs_task`` 读取
   ``generate_scene_refs`` 的返回值，任何场景进了 ``warnings`` 就把整个
   run/项目状态判失败，不能只看 operation() 有没有抛异常。
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading

import pytest

from app import config, db, multiview, scenes
from app.domain.bible_ops import scene_bible_prep
from app.schemas import Bible, Scene, World

_OLD_SCENE_CANONICAL = "改造前的老出租屋，昏暗灯光，堆满杂物纸箱"
_NEW_SCENE_CANONICAL = "改造后的出租屋，暖色灯光，干净整洁陈设"


@pytest.fixture
def scene_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "assets.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn(), tmp_path
    db.get_conn().close()


def _seed_existing_scene(conn, tmp_path):
    """种一个「已经整包出过图、现在改了描述、待重绘」的场景（旧当前版本，
    ep_start=1, ep_end=NULL，change_json.pending_redraw=true），复刻 PUT
    /projects/{id}/scenes/{name} 之后 POST /scene-refs 之前的真实生产状态。"""
    bible = Bible(
        world=World(visual_style_canonical="cinematic animation"),
        characters=[],
        scenes=[Scene(name="温念的出租屋", scene_canonical=_OLD_SCENE_CANONICAL)],
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at, aspect_ratio) "
        "VALUES('proj_regen', 'Regen', 'bible_ready', ?, 1, 1, '16:9')",
        (bible.model_dump_json(),),
    )
    conn.commit()
    old_image = tmp_path / "old-main.jpg"
    old_image.write_bytes(b"old-main-image")
    scene_id = scenes.register_initial_scene_ref(
        conn, "proj_regen", "温念的出租屋", str(old_image),
        _OLD_SCENE_CANONICAL, "old prompt", {}, 1,
    )
    conn.execute(
        "UPDATE scene_references SET change_json=? WHERE id=?",
        (json.dumps({"description_changed": True, "pending_redraw": True}, ensure_ascii=False), scene_id),
    )
    # PUT 已经把新描述写进 bible_json（本测试只关心场景图这一侧的重生，不重跑
    # edit_scene_anchor 路由本身）。
    row = conn.execute("SELECT bible_json FROM projects WHERE id='proj_regen'").fetchone()
    bible_data = json.loads(row["bible_json"])
    bible_data["scenes"][0]["scene_canonical"] = _NEW_SCENE_CANONICAL
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id='proj_regen'",
        (json.dumps(bible_data, ensure_ascii=False),),
    )
    conn.commit()
    return scene_id


def _patch_successful_generation(monkeypatch) -> None:
    """跳过真实供应商调用与图片格式技术校验，只把「候选生成成功、多视角包
    成功」这个既定事实喂给被测代码——与
    tests/test_initial_multiview_bootstrap.py::_patch_successful_character_generation
    同一惯例。"""
    encoded = base64.b64encode(b"brand-new-candidate-image").decode("ascii")
    approvals = {"n": 0}

    def fake_record_reference_asset(**kwargs):
        approvals["n"] += 1
        return {"id": f"art_{approvals['n']}", "status": "approved", "file_path": kwargs["file_path"]}

    async def fake_generate_image(*_args, **_kwargs):
        return {"b64_json": encoded}

    async def fake_draft(**_kwargs):
        return {}

    async def fake_judge(**_kwargs):
        return {"checked": True, "passed": True, "reason": "确实是反打", "error": None}

    monkeypatch.setattr(scenes, "record_reference_asset", fake_record_reference_asset)
    monkeypatch.setattr(scenes.hiagent, "generate_image", fake_generate_image)
    monkeypatch.setattr(multiview, "_generate_image", fake_generate_image)
    monkeypatch.setattr(multiview, "scene_multiview_enabled", lambda: True)
    monkeypatch.setattr(multiview, "draft_behind_camera_note", fake_draft)
    monkeypatch.setattr(multiview, "judge_reverse_angle", fake_judge)


def test_existing_scene_pack_regeneration_replaces_current_image(scene_db, monkeypatch) -> None:
    """已有场景改描述后请求整包重生：候选图与多视角包都成功产出时，当前版本
    必须真的换成新图、change_json 不再带 pending_redraw——这正是生产事故里
    「provider_calls 全部 OK、却没有任何一行 scene_references 更新」的那条路径。
    """
    conn, tmp_path = scene_db
    old_scene_id = _seed_existing_scene(conn, tmp_path)
    _patch_successful_generation(monkeypatch)

    asyncio.run(scenes.generate_scene_refs("proj_regen", only_scene=["温念的出租屋"]))

    current = conn.execute(
        "SELECT * FROM scene_references WHERE project_id='proj_regen' AND scene_name='温念的出租屋' "
        "AND ep_end IS NULL",
    ).fetchone()
    assert current is not None, "整包重生完成后必须仍有一行当前版本（ep_end IS NULL）"
    assert current["id"] != old_scene_id, "当前版本必须切换成新候选，不能还是原来那一行"
    assert current["scene_canonical"] == _NEW_SCENE_CANONICAL
    change = json.loads(current["change_json"] or "{}")
    assert not change.get("pending_redraw"), "替换成功后必须清除 pending_redraw"

    old_row = conn.execute(
        "SELECT ep_end FROM scene_references WHERE id=?", (old_scene_id,),
    ).fetchone()
    assert old_row["ep_end"] == 0, "旧版本应该被移入历史槽，而不是原地保留为 current"

    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_regen'",
    ).fetchone()["bible_json"])
    assert bible["scenes"][0]["ref_image_path"] == current["image_path"]


def test_scene_refs_task_surfaces_failure_when_every_scene_exhausts_retries(scene_db, monkeypatch) -> None:
    """_scene_refs_task 不能只看 generate_scene_refs 这一步有没有抛异常：它对
    单场景失败是「记进 warnings、批次继续」的有界重试语义，本身不抛异常，
    调用方必须读这份返回值，否则 run/项目状态会在场景实际全部失败时仍然报
    ready/SUCCEEDED（本次事故的第二层根因，独立于上面那条 SQL 修复）。"""
    conn, _tmp_path = scene_db
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('proj_gate', 'Gate', 'bible_ready', 1)"
    )
    conn.commit()

    async def fake_generate_scene_refs(*_args, **_kwargs):
        return {"generated": [], "gate_retry_exhausted": True, "warnings": ["温念的出租屋：场景图技术校验未通过"]}

    monkeypatch.setattr(scenes, "generate_scene_refs", fake_generate_scene_refs)

    asyncio.run(scene_bible_prep._scene_refs_task("proj_gate", ["温念的出租屋"]))

    row = conn.execute(
        "SELECT scene_refs_status, scene_refs_error FROM projects WHERE id='proj_gate'",
    ).fetchone()
    assert row["scene_refs_status"] == "failed", (
        "generate_scene_refs 报告场景失败时，任务不能仍然落地成 ready"
    )
    assert "温念的出租屋" in (row["scene_refs_error"] or "")


def test_pack_not_ready_still_promotes_primary_image_to_current(scene_db, monkeypatch) -> None:
    """反打视角没通过（ensure_scene_multiview_pack 返回非 ready）时，已经落盘
    的主视角图必须照常晋升为当前版本——不能因为侧视角不达标把已付费的主图
    也一起作废（用户拍板 2026-09-01，见 app.scenes._generate_one_scene_reference
    对 pack_result_ok 的处理）。这不是本次事故的根因（该分支本来就不会作废
    主图），补上是为了锁住这条相邻不变量，防止本次改动误伤它。"""
    conn, tmp_path = scene_db
    old_scene_id = _seed_existing_scene(conn, tmp_path)

    encoded = base64.b64encode(b"brand-new-main-only").decode("ascii")

    def fake_record_reference_asset(**kwargs):
        return {"id": "art_only", "status": "approved", "file_path": kwargs["file_path"]}

    async def fake_generate_image(*_args, **_kwargs):
        return {"b64_json": encoded}

    async def fake_pack_not_ready(**kwargs):
        return {
            "status": "failed",
            "scene_reference_id": kwargs.get("scene_reference_id"),
            "failed_view": "reverse_angle",
        }

    monkeypatch.setattr(scenes, "record_reference_asset", fake_record_reference_asset)
    monkeypatch.setattr(scenes.hiagent, "generate_image", fake_generate_image)
    monkeypatch.setattr(multiview, "scene_multiview_enabled", lambda: True)
    monkeypatch.setattr(multiview, "ensure_scene_multiview_pack", fake_pack_not_ready)

    asyncio.run(scenes.generate_scene_refs("proj_regen", only_scene=["温念的出租屋"]))

    current = conn.execute(
        "SELECT * FROM scene_references WHERE project_id='proj_regen' AND scene_name='温念的出租屋' "
        "AND ep_end IS NULL",
    ).fetchone()
    assert current is not None
    assert current["id"] != old_scene_id
    assert current["scene_canonical"] == _NEW_SCENE_CANONICAL
