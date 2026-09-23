"""分镜台 2.x 段提示词方言守卫的回归证据。

背景（主会话核实的真实故障形状）：分镜台 2.x 段的 ``prompt_text`` 按「本集绑定
视频模型」的方言写成（Seedance 自由中文散文 / MiniMax H3 结构化英文字段，见
``app.production.storyboard_dialects``），两种语法互不兼容。旧的
``[VIDEO_MODEL_BINDING_MISMATCH]`` 守卫（``app.media_exec.enqueue_context``）
只比较 ``episodes.target_video_model`` 与当前生效供应商是否**同族**，从不看
段落本身实际是哪种方言写的——本集切换过供应商但分镜台没有重新生成过分镜时，
两个状态字段可以完全一致，段落里仍是旧方言的 ``prompt_text`` 会原样发给新
供应商，新供应商不报错、静默按自由文本理解，镜头/字段/口型全部失效。

本文件验证：
1. ``app.production.storyboard_dialects.dialect_literal_for_target_video_model``
   ——两个守卫共用的方言字面值解析，不各自抄一份映射表。
2. ``app.media_exec.prompt_dialect_guard.dialect_mismatch_error`` 的判据：非
   2.x 镜头放行、方言一致放行、方言不一致拦下、方言字段缺失 fail closed（不
   默认按 Seedance 处理——见该模块 docstring 引用的 commit 18a0416f 依据）。
3. ``app.media_exec.enqueue_context.load_video_binding_context`` 把新守卫接进
   入队路径：家族一致但方言不一致的场景（正是「先切生效模型再重试」这条旧建议
   会踩中的坑）必须被新守卫拦下；``[VIDEO_MODEL_BINDING_MISMATCH]`` 本身的文案
   改成同时讲清两个条件。
4. ``app.domain.storyboard_ops.video_model.set_episode_video_model`` 切换后如实
   报告分镜方言是否仍停留在旧供应商，不自动重新生成分镜。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app
from app.media_exec import enqueue_context
from app.media_exec.prompt_dialect_guard import dialect_mismatch_error
from app.production.storyboard_dialects import dialect_literal_for_target_video_model

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _add_user(username: str) -> str:
    from app.auth.passwords import hash_password

    conn = get_conn()
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, created_at) "
        "VALUES(?,?,?,'active',0,?)",
        (user_id, username, hash_password("pw-" + username), now()),
    )
    conn.commit()
    return user_id


def _seed_episode(project_id: str, episode_id: str, owner_user_id: str, *, target_video_model: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,owner_user_id,created_at) VALUES(?,?,?,?)",
        (project_id, project_id, owner_user_id, now()),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,target_video_model,created_at) "
        "VALUES(?,?,1,'confirmed',?,?)",
        (episode_id, project_id, target_video_model, now()),
    )
    conn.commit()


def _seed_shot(episode_id: str, shot_id: str, *, segment: dict | None) -> None:
    """插入一个镜头行；``segment`` 非 None 时写成分镜台 2.x 段（shot_contract_json.
    storyboard_pack_segment），None 时是旧版镜头（无 shot_contract_json）。"""
    conn = get_conn()
    contract = json.dumps({"storyboard_pack_segment": segment}, ensure_ascii=False) if segment is not None else None
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_contract_json) VALUES(?,?,1,15,?)",
        (shot_id, episode_id, contract),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. 共用方言字面值解析
# ---------------------------------------------------------------------------

def test_dialect_literal_resolves_seedance_family_providers() -> None:
    assert dialect_literal_for_target_video_model("hiagent") == "seedance_2"


def test_dialect_literal_resolves_minimax_h3() -> None:
    assert dialect_literal_for_target_video_model("minimax_h3") == "minimax_h3"


# ---------------------------------------------------------------------------
# 2. dialect_mismatch_error 判据
# ---------------------------------------------------------------------------

def test_dialect_mismatch_error_none_for_legacy_shot_without_segment() -> None:
    row = {"shot_contract_json": None}
    assert dialect_mismatch_error(row, "hiagent") is None


def test_dialect_mismatch_error_none_when_dialect_matches() -> None:
    row = {"shot_contract_json": json.dumps(
        {"storyboard_pack_segment": {"target_model": "seedance_2"}}
    )}
    assert dialect_mismatch_error(row, "hiagent") is None


def test_dialect_mismatch_error_blocks_when_dialect_differs() -> None:
    row = {"shot_contract_json": json.dumps(
        {"storyboard_pack_segment": {"target_model": "seedance_2"}}
    )}
    error = dialect_mismatch_error(row, "minimax_h3")
    assert error is not None
    assert error.startswith("[VIDEO_PROMPT_DIALECT_MISMATCH]")
    assert "seedance_2" in error and "minimax_h3" in error
    assert "重新生成本集分镜" in error


def test_dialect_mismatch_error_fail_closed_when_target_model_missing() -> None:
    """老数据没有 target_model 字段：不许默认按 Seedance 放行，必须 fail closed。"""
    row = {"shot_contract_json": json.dumps(
        {"storyboard_pack_segment": {"prompt_text": "一些提示词"}}
    )}
    error = dialect_mismatch_error(row, "hiagent")
    assert error is not None
    assert error.startswith("[VIDEO_PROMPT_DIALECT_MISMATCH]")
    assert "重新生成本集分镜" in error


# ---------------------------------------------------------------------------
# 3. load_video_binding_context 接线
# ---------------------------------------------------------------------------

def test_load_video_binding_context_blocks_stale_dialect_even_when_family_matches() -> None:
    """真实故障场景：本集绑定 minimax_h3，生效模型也切到了 minimax_h3（家族检查
    通过——这正是旧版 [VIDEO_MODEL_BINDING_MISMATCH] 建议用户做的事），但段落
    prompt_text 仍是 Seedance 散文。新守卫必须在这里拦下，不能让方言不兼容的
    提示词发给供应商。"""
    project_id, episode_id, shot_id = "proj_dlg_1", "ep_dlg_1", "shot_dlg_1"
    user_id = _add_user("dlg_owner_1")
    _seed_episode(project_id, episode_id, user_id, target_video_model="minimax_h3")
    _seed_shot(episode_id, shot_id, segment={"target_model": "seedance_2", "prompt_text": "镜头1："})

    conn = get_conn()
    with pytest.raises(ValueError) as excinfo:
        enqueue_context.load_video_binding_context(conn, shot_id, "minimax_h3")
    assert "[VIDEO_PROMPT_DIALECT_MISMATCH]" in str(excinfo.value)


def test_load_video_binding_context_passes_when_dialect_matches() -> None:
    project_id, episode_id, shot_id = "proj_dlg_2", "ep_dlg_2", "shot_dlg_2"
    user_id = _add_user("dlg_owner_2")
    _seed_episode(project_id, episode_id, user_id, target_video_model="hiagent")
    _seed_shot(episode_id, shot_id, segment={"target_model": "seedance_2", "prompt_text": "镜头1："})

    conn = get_conn()
    shot_row, ep, project = enqueue_context.load_video_binding_context(conn, shot_id, "hiagent")
    assert shot_row["id"] == shot_id
    assert ep["id"] == episode_id
    assert project["id"] == project_id


def test_binding_mismatch_message_states_both_conditions() -> None:
    """[VIDEO_MODEL_BINDING_MISMATCH] 不能再单独建议"切生效模型"了事——两个
    条件（供应商族一致 + 分镜提示词方言一致）都要在文案里讲清楚。"""
    project_id, episode_id, shot_id = "proj_dlg_3", "ep_dlg_3", "shot_dlg_3"
    user_id = _add_user("dlg_owner_3")
    _seed_episode(project_id, episode_id, user_id, target_video_model="minimax_h3")
    _seed_shot(episode_id, shot_id, segment=None)

    conn = get_conn()
    with pytest.raises(ValueError) as excinfo:
        enqueue_context.load_video_binding_context(conn, shot_id, "hiagent")
    message = str(excinfo.value)
    assert message.startswith("[VIDEO_MODEL_BINDING_MISMATCH]")
    # 旧文案只单独建议"切生效模型"，只字不提分镜方言也必须一致——那条建议照做后
    # 会正中真实故障（本报告 CLAUDE.md 引用的场景）。新文案必须正面列出两个条件
    # 都要满足，而不是暗示切一个字段就够。
    assert "方言" in message
    assert "同时满足" in message or "两个条件" in message
    assert "分镜台" in message and "模型中心" in message


# ---------------------------------------------------------------------------
# 4. set_episode_video_model 切换后如实报告
# ---------------------------------------------------------------------------

def test_switch_reports_storyboard_dialect_stale(client: TestClient) -> None:
    project_id, episode_id, shot_id = "proj_dlg_4", "ep_dlg_4", "shot_dlg_4"
    user_id = _add_user("dlg_owner_4")
    _seed_episode(project_id, episode_id, user_id, target_video_model="hiagent")
    _seed_shot(episode_id, shot_id, segment={"target_model": "seedance_2", "prompt_text": "镜头1："})

    resp = client.post(
        f"/api/episodes/{episode_id}/video-model",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={"target_video_model": "minimax_h3"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["storyboard_dialect_stale"] is True
    assert body["stale_segment_count"] == 1
    assert "分镜提示词仍是" in body["message"]
    assert "重新生成本集分镜" in body["message"]


def test_switch_reports_not_stale_when_no_2x_segments(client: TestClient) -> None:
    project_id, episode_id, shot_id = "proj_dlg_5", "ep_dlg_5", "shot_dlg_5"
    user_id = _add_user("dlg_owner_5")
    _seed_episode(project_id, episode_id, user_id, target_video_model="hiagent")
    _seed_shot(episode_id, shot_id, segment=None)

    resp = client.post(
        f"/api/episodes/{episode_id}/video-model",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={"target_video_model": "minimax_h3"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["storyboard_dialect_stale"] is False
    assert body["stale_segment_count"] == 0
    assert "message" not in body or not body["message"]
