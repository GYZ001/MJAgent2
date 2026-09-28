"""app.multiview.ensure_scene_multiview_pack 的反打指纹幂等边界。

风险点（见 app.scene_reverse.produce 模块文档与派单）：起草文本/最终提示词只能
体现在返回值的 ``prompt``/``qa`` 里，绝不能混进落库的 ``input_fingerprint``——
否则每次触碰这个场景都会判定指纹不一致，无限重新付费生成。这是一个只有跑得
够久才会暴露的缺陷，不写专门的回归测试就不会在小规模测试里现形。
"""
from __future__ import annotations

import asyncio
import base64
import threading

import pytest

from app import config, db, multiview, scenes
from app.schemas import Bible, Character, Scene, World


@pytest.fixture
def asset_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "assets.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn(), tmp_path
    db.get_conn().close()


def _seed_bible_project(conn) -> None:
    bible = Bible(
        world=World(visual_style_canonical="cinematic animation"),
        characters=[Character(name="Hero", role="lead", appearance_canonical="young hero")],
        scenes=[Scene(name="Courtyard", scene_canonical="stone courtyard at dawn with a red gate")],
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES('proj_bootstrap', 'Bootstrap', 'bible_ready', ?, 1, 1)", (bible.model_dump_json(),),
    )
    conn.commit()


def test_reverse_angle_regeneration_is_idempotent_when_inputs_unchanged(asset_db, monkeypatch) -> None:
    """establishing 直接以 ready+匹配指纹的方式落库，隔离掉「父图缺失走重建分支」
    这条与本测试无关的路径，只考察 reverse_angle 自己的幂等边界。"""
    conn, tmp_path = asset_db
    _seed_bible_project(conn)
    primary = tmp_path / "primary.jpg"
    primary.write_bytes(b"primary")
    scene_canonical = "stone courtyard at dawn with a red gate"
    visual_style = "cinematic animation"
    parent_prompt = "stable-parent-prompt"
    qa = {"overall": 0.95, "status": "ready", "hard_gate_passed": True, "hard_failures": []}
    scene_id = scenes.register_initial_scene_ref(
        conn, "proj_bootstrap", "Courtyard", str(primary), scene_canonical, parent_prompt, qa, 1,
    )
    est_fp = multiview.view_input_fingerprint(
        view_role="establishing", prompt=parent_prompt, anchor_text=scene_canonical,
        parent_revision_id=scene_id, base_view_id=None, seed_hint=None,
    )
    conn.execute(
        "INSERT INTO scene_reference_views("
        "id, scene_reference_id, view_role, image_path, status, input_fingerprint, created_at) "
        "VALUES(?,?,?,?,?,?,1)",
        ("view-establishing", scene_id, "establishing", str(primary), "ready", est_fp),
    )
    conn.commit()

    generate_calls: list[dict] = []
    draft_calls: list[int] = []
    judge_calls: list[int] = []
    encoded = base64.b64encode(b"reverse-image").decode("ascii")

    async def fake_generate(*_args, **kwargs):
        generate_calls.append(kwargs["call_meta"])
        return {"b64_json": encoded}

    def fake_draft(**_kwargs):
        draft_calls.append(1)
        return asyncio.sleep(0, result={"reverse_view": "机位背后是斑驳的砖墙与半开的木门"})

    def fake_judge(**_kwargs):
        judge_calls.append(1)
        verdict = {"checked": True, "passed": True, "reason": "反打方向确实相反", "error": None}
        return asyncio.sleep(0, result=verdict)

    monkeypatch.setattr(multiview, "_generate_image", fake_generate)
    monkeypatch.setattr(multiview, "draft_behind_camera_note", fake_draft)
    monkeypatch.setattr(multiview, "judge_reverse_angle", fake_judge)

    call_kwargs = dict(
        project_id="proj_bootstrap", scene_reference_id=scene_id, scene_name="Courtyard",
        scene_canonical=scene_canonical, visual_style=visual_style, ep_start=1,
    )
    first = asyncio.run(multiview.ensure_scene_multiview_pack(**call_kwargs))
    assert first["status"] == "ready"
    assert len(generate_calls) == 1  # 只有 reverse_angle 需要生成
    assert len(draft_calls) == 1
    assert len(judge_calls) == 1

    second = asyncio.run(multiview.ensure_scene_multiview_pack(**call_kwargs))
    assert second["status"] == "ready"
    assert len(generate_calls) == 1  # 指纹匹配，第二次不得重新生成/起草/判定
    assert len(draft_calls) == 1
    assert len(judge_calls) == 1
