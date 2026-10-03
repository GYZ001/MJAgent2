"""场景状态图生成编排单测（``app.video_modes.scene_state_ensure``）。

覆盖：``claim_or_get`` 的库内 CAS（首次插入/ready 复用/running 未超时不重复
触发/running 超时或 failed 可重新抢占，均经真实的 ``db.run_write_transaction``
独立事务）、``ensure_scene_state_views`` 端到端（真实 sqlite + 打桩 hiagent）、
缺场景卡主图/状态描述为空直接 failed 不调用供应商、operation_id 随指纹变化、
生成入口闸门三态（missing/running 拦截且启动 ensure，failed 不拦）、
``video.generate_episode`` 闸门范围随 ``only_incomplete`` 收窄（2026-10-02
代码评审 #0）。
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app import db as db_mod
from app import hiagent
from app.capabilities import inputs as I
from app.capabilities.handlers import video as video_handlers
from app.db import get_conn
from app.video_modes import scene_state_ensure as ensure_mod
from app.video_modes.scene_state_views import scene_state_input_fingerprint, state_key_for
from app.video_modes.scene_state_views_store import ensure_tables_on_connection


def _seed_state_row(**fields) -> None:
    conn = get_conn()
    ensure_tables_on_connection(conn)
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO scene_state_views({columns}) VALUES({placeholders})", list(fields.values()))
    conn.commit()


# ---------- claim_or_get CAS（真实 db.run_write_transaction） ----------

def test_claim_or_get_first_time_inserts_running_row():
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", fingerprint="fp1",
    ))
    assert should_generate is True
    assert row["status"] == "running"
    stored = get_conn().execute("SELECT * FROM scene_state_views WHERE id=?", (row["id"],)).fetchone()
    assert stored["status"] == "running"


def test_claim_or_get_reuses_ready_row_when_fingerprint_matches(tmp_path):
    path = tmp_path / "state.jpg"
    path.write_bytes(b"fake")
    _seed_state_row(
        id="scstate_1", project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", image_path=str(path), prompt="p", status="ready",
        input_fingerprint="fp1", created_at=1.0, updated_at=1.0,
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", fingerprint="fp1",
    ))
    assert should_generate is False
    assert row["status"] == "ready"


def test_claim_or_get_regenerates_when_fingerprint_changed(tmp_path):
    path = tmp_path / "state.jpg"
    path.write_bytes(b"fake")
    _seed_state_row(
        id="scstate_1", project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", image_path=str(path), prompt="p", status="ready",
        input_fingerprint="fp_old", created_at=1.0, updated_at=1.0,
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", fingerprint="fp_new",
    ))
    assert should_generate is True
    assert row["status"] == "running"


def test_claim_or_get_running_within_stale_window_is_not_retriggered():
    _seed_state_row(
        id="scstate_1", project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", image_path="", prompt="", status="running",
        input_fingerprint="fp1", created_at=db_mod.now(), updated_at=db_mod.now(),
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", fingerprint="fp1",
    ))
    assert should_generate is False
    assert row["status"] == "running"


def test_claim_or_get_stale_running_is_reclaimed():
    """进程被杀、running 行从未回写终态：超过 ``_RUNNING_STALE_S`` 视为僵死，
    允许重新抢占，否则这张状态图会永久卡在"生成中"。"""
    stale_ts = db_mod.now() - ensure_mod._RUNNING_STALE_S - 1
    _seed_state_row(
        id="scstate_1", project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", image_path="", prompt="", status="running",
        input_fingerprint="fp1", created_at=stale_ts, updated_at=stale_ts,
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", fingerprint="fp1",
    ))
    assert should_generate is True


def test_claim_or_get_failed_row_is_reclaimed():
    _seed_state_row(
        id="scstate_1", project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", image_path="", prompt="", status="failed", error="网络超时",
        input_fingerprint="fp1", created_at=1.0, updated_at=1.0,
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", episode_id="ep_1", scene_reference_id="scene_1",
        state_key="key1", description="满地积水", fingerprint="fp1",
    ))
    assert should_generate is True
    assert row["status"] == "running"


# ---------- ensure_scene_state_views 端到端 ----------

def _seed_episode_with_state_need(tmp_path) -> tuple[str, str]:
    conn = get_conn()
    est_path = tmp_path / "est.jpg"
    est_path.write_bytes(b"fake-est")
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?, 'created', ?, 1)",
        ("proj_ensure_1", "测试项目", json.dumps(
            {"characters": [], "scenes": [], "props": [], "world": {"visual_style_canonical": "写实"}},
            ensure_ascii=False,
        )),
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, image_path, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("scene_ensure_1", "proj_ensure_1", "温念的出租屋", 1, None, str(est_path), db_mod.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        ("ep_ensure_1", "proj_ensure_1", 1, "created", 1.0),
    )
    segment = {
        "resources": {"characters": [], "scenes": [{
            "scene_id": "scene:温念的出租屋", "scene_reference_id": "scene_ensure_1",
            "scene_state_matches_card": "no", "description": "满地积水，鞋柜歪倒",
        }]},
    }
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
        ("shot_ensure_1", "ep_ensure_1", 15, 15, json.dumps({"storyboard_pack_segment": segment}, ensure_ascii=False)),
    )
    conn.commit()
    return "proj_ensure_1", "ep_ensure_1"


def test_ensure_scene_state_views_generates_missing_and_marks_ready(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_state_need(tmp_path)

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        assert "满地积水" in prompt
        return {"b64_json": base64.b64encode(b"generated-state-bytes").decode("ascii")}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    result = asyncio.run(ensure_mod.ensure_scene_state_views(project_id=project_id, episode_id=episode_id))

    assert result["summary"]["ready"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["status"] == "ready"
    row = get_conn().execute(
        "SELECT * FROM scene_state_views WHERE scene_reference_id='scene_ensure_1'",
    ).fetchone()
    assert row["status"] == "ready"
    assert row["image_path"]


def test_ensure_scene_state_views_marks_failed_on_provider_error(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_state_need(tmp_path)

    async def failing_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        raise hiagent.ProviderError("供应商超时")

    monkeypatch.setattr(hiagent, "generate_image", failing_generate_image)

    result = asyncio.run(ensure_mod.ensure_scene_state_views(project_id=project_id, episode_id=episode_id))

    assert result["summary"]["failed"] == 1
    assert result["items"][0]["status"] == "failed"
    assert "供应商超时" in result["items"][0]["error"]


def test_ensure_scene_state_views_is_idempotent_on_second_call(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_state_need(tmp_path)
    calls = {"count": 0}

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        calls["count"] += 1
        return {"b64_json": base64.b64encode(b"generated-state-bytes").decode("ascii")}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    asyncio.run(ensure_mod.ensure_scene_state_views(project_id=project_id, episode_id=episode_id))
    asyncio.run(ensure_mod.ensure_scene_state_views(project_id=project_id, episode_id=episode_id))

    assert calls["count"] == 1


# ---------- 缺场景卡主图：直接 failed，不调用供应商 ----------

def test_generate_one_state_fails_without_seed_image_and_skips_provider_call(monkeypatch):
    calls = {"n": 0}

    async def fake_generate_image(*args, **kwargs):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)
    spec = {
        "scene_reference_id": "scene_missing_seed", "state_key": "keyX",
        "establishing_image_path": "/tmp/does-not-exist-scene-state-xyz.jpg",
        "description": "满地积水", "scene_name": "温念的出租屋", "fingerprint": "fpX",
    }
    asyncio.run(ensure_mod._generate_one_state(
        project_id="proj_x", episode_id="ep_x", visual_style="写实", aspect_ratio="9:16", spec=spec,
    ))

    assert calls["n"] == 0
    row = get_conn().execute(
        "SELECT * FROM scene_state_views WHERE scene_reference_id='scene_missing_seed' AND state_key='keyX'",
    ).fetchone()
    assert row["status"] == "failed"
    assert "场景卡主图缺失" in row["error"]


def test_generate_one_state_fails_on_empty_description_and_skips_provider_call(tmp_path, monkeypatch):
    """2026-10-02 代码评审 #0：分镜模型只被要求填 scene_state_matches_card，
    description 没有对应的"必须填"提示词规则——串内全部段落都没写这段状态具体是
    什么时，没有依据知道该往参考图上改什么，不能盲目拿默认状态图原样出一遍图再
    当作"已确认新状态"发给视频模型（CLAUDE.md「不得兜底填充」）。"""
    est_path = tmp_path / "est.jpg"
    est_path.write_bytes(b"fake-est")
    calls = {"n": 0}

    async def fake_generate_image(*args, **kwargs):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)
    spec = {
        "scene_reference_id": "scene_empty_desc", "state_key": "keyY",
        "establishing_image_path": str(est_path),
        "description": "   ", "scene_name": "温念的出租屋", "fingerprint": "fpY",
    }
    asyncio.run(ensure_mod._generate_one_state(
        project_id="proj_x", episode_id="ep_x", visual_style="写实", aspect_ratio="9:16", spec=spec,
    ))

    assert calls["n"] == 0
    row = get_conn().execute(
        "SELECT * FROM scene_state_views WHERE scene_reference_id='scene_empty_desc' AND state_key='keyY'",
    ).fetchone()
    assert row["status"] == "failed"
    assert "场景状态描述为空" in row["error"]


# ---------- operation_id 随指纹变化 ----------

def test_scene_state_operation_id_changes_with_fingerprint():
    a = ensure_mod._scene_state_operation_id("scene_1", "key1", "fp_a")
    b = ensure_mod._scene_state_operation_id("scene_1", "key1", "fp_b")
    assert a != b


def test_scene_state_input_fingerprint_changes_with_establishing_image_path():
    """场景卡重新生成拿到新种子图文件路径后，指纹必须跟着变，否则旧状态图会
    被当成仍然正确而永远不重出。"""
    fp_a = scene_state_input_fingerprint(
        scene_reference_id="scene_1", establishing_image_path="/a.jpg", description="满地积水", visual_style="写实",
    )
    fp_b = scene_state_input_fingerprint(
        scene_reference_id="scene_1", establishing_image_path="/b.jpg", description="满地积水", visual_style="写实",
    )
    assert fp_a != fp_b


# ---------- 生成入口闸门三态 ----------

def test_pending_scene_state_gate_missing_returns_message_and_launches_once(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_state_need(tmp_path)
    launched = []
    monkeypatch.setattr(ensure_mod, "launch_background_ensure", lambda **kw: launched.append(kw))

    message = asyncio.run(ensure_mod.pending_scene_state_gate(project_id, episode_id, None))

    assert message is not None
    assert "场景状态图" in message and "本集" in message
    assert len(launched) == 1


def test_pending_scene_state_gate_shot_scope_uses_segment_wording(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_state_need(tmp_path)
    monkeypatch.setattr(ensure_mod, "launch_background_ensure", lambda **kw: None)

    message = asyncio.run(ensure_mod.pending_scene_state_gate(project_id, episode_id, ["shot_ensure_1"]))

    assert message is not None
    assert "本段" in message


def test_pending_scene_state_gate_running_blocks_and_still_launches_ensure(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_state_need(tmp_path)
    key = state_key_for("scene_ensure_1", 15, "满地积水，鞋柜歪倒")
    _seed_state_row(
        id="scstate_running", project_id=project_id, episode_id=episode_id, scene_reference_id="scene_ensure_1",
        state_key=key, description="满地积水，鞋柜歪倒", image_path="", prompt="", status="running",
        input_fingerprint="fp", created_at=db_mod.now(), updated_at=db_mod.now(),
    )
    launched = []
    monkeypatch.setattr(ensure_mod, "launch_background_ensure", lambda **kw: launched.append(kw))

    message = asyncio.run(ensure_mod.pending_scene_state_gate(project_id, episode_id, None))

    assert message is not None
    assert len(launched) == 1


def test_pending_scene_state_gate_failed_does_not_block(tmp_path, monkeypatch):
    """failed 不拦——照常生成，装配期退回现有省略行为并留 advisory。"""
    project_id, episode_id = _seed_episode_with_state_need(tmp_path)
    key = state_key_for("scene_ensure_1", 15, "满地积水，鞋柜歪倒")
    fp = scene_state_input_fingerprint(
        scene_reference_id="scene_ensure_1",
        establishing_image_path=str((tmp_path / "est.jpg")), description="满地积水，鞋柜歪倒", visual_style="写实",
    )
    _seed_state_row(
        id="scstate_failed", project_id=project_id, episode_id=episode_id, scene_reference_id="scene_ensure_1",
        state_key=key, description="满地积水，鞋柜歪倒", image_path="", prompt="", status="failed", error="e",
        input_fingerprint=fp, created_at=db_mod.now(), updated_at=db_mod.now(),
    )
    monkeypatch.setattr(ensure_mod, "launch_background_ensure", lambda **kw: (_ for _ in ()).throw(AssertionError("不应启动")))

    message = asyncio.run(ensure_mod.pending_scene_state_gate(project_id, episode_id, None))

    assert message is None


# ---------- video.generate_episode 闸门范围随 only_incomplete 收窄 ----------

def _seed_episode_for_only_incomplete(conn) -> str:
    """3 镜：s_done_version 靠 shot_versions 成功版判完成，s_done_adopted 靠
    adopted_version_id 判完成，s_incomplete 两者都没有——only_incomplete 续跑只
    会重新派发 s_incomplete。"""
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?, 'created', ?, 1)",
        ("proj_oi_1", "测试项目", "{}"),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        ("ep_oi_1", "proj_oi_1", 1, "created", 1.0),
    )
    for shot_id, shot_no in [("s_done_version", 1), ("s_done_adopted", 2), ("s_incomplete", 3)]:
        conn.execute(
            "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,15)",
            (shot_id, "ep_oi_1", shot_no),
        )
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, video_path, created_at) "
        "VALUES('v1','s_done_version',1,'p','idem-v1','succeeded','/tmp/x.mp4',1.0)",
    )
    conn.execute("UPDATE shots SET adopted_version_id='v2' WHERE id='s_done_adopted'")
    conn.commit()
    return "proj_oi_1"


@pytest.mark.asyncio
async def test_generate_episode_only_incomplete_narrows_gate_to_unfinished_shots(monkeypatch) -> None:
    conn = get_conn()
    _seed_episode_for_only_incomplete(conn)
    captured: dict = {}

    async def fake_gate(project_id_arg, episode_id_arg, shot_ids_arg):
        captured.update(project_id=project_id_arg, episode_id=episode_id_arg, shot_ids=shot_ids_arg)
        return "占位：场景状态图正在生成"

    monkeypatch.setattr(ensure_mod, "pending_scene_state_gate", fake_gate)

    args = I.VideoGenerateEpisodeInput(
        episode_id="ep_oi_1", only_incomplete=True, idempotency_key="idem-scope-1",
    )
    result = await video_handlers.generate_episode(args)

    assert result.status == "failed"
    assert result.error_code == "scene_state_pending"
    assert captured["project_id"] == "proj_oi_1"
    assert captured["episode_id"] == "ep_oi_1"
    assert captured["shot_ids"] == ["s_incomplete"]


@pytest.mark.asyncio
async def test_generate_episode_only_incomplete_skips_gate_when_nothing_left(monkeypatch) -> None:
    """全部镜头已完成时续跑范围为空——不调用闸门（传空列表会被 `_target_shot_nos`
    的 `not shot_ids` 短路成"不过滤=整集"，错误地放大回整集范围，见代码评审 #0）。"""
    conn = get_conn()
    _seed_episode_for_only_incomplete(conn)
    conn.execute("UPDATE shots SET adopted_version_id='v3' WHERE id='s_incomplete'")
    conn.commit()

    def _fail_if_called(*_a, **_k):
        raise AssertionError("续跑范围为空时不应调用闸门")

    monkeypatch.setattr(ensure_mod, "pending_scene_state_gate", _fail_if_called)

    def _assert_reached_claim(**_k):
        raise AssertionError("本测试只验证闸门范围跳过，不应继续往下走")

    monkeypatch.setattr(
        "app.video_command_operations.claim_video_command_operation", _assert_reached_claim,
    )

    args = I.VideoGenerateEpisodeInput(
        episode_id="ep_oi_1", only_incomplete=True, idempotency_key="idem-scope-2",
    )
    with pytest.raises(AssertionError, match="不应继续往下走"):
        await video_handlers.generate_episode(args)
