"""``tests/test_rbac_project_isolation.py`` 与
``tests/test_rbac_admin_cross_account_audit.py`` 共用的造数据与 fixture。

拆出来的原因是行数棘轮：隔离测试原本 777 行，超出 ``app/FILE_CONVENTIONS.toml``
里 761 行的基线，而基线只降不升。审计那一节自成一体（判据是「管理员且 owner 不是
本人」，与解析表/HTTP 边界无关），整节搬到独立文件，两边共用这一份造数据。

不是测试文件（没有 ``test_`` 前缀），只放 fixture 与 builder。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth.principal import set_current_principal
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app


# ---------------------------------------------------------------------------
# 造数据：两个账号，各自一整条 project -> episode -> shot -> version 链路。
# ---------------------------------------------------------------------------


def _mk_user(conn, username: str, *, is_system_admin: bool = False) -> str:
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users(id, username, display_name, auth_provider, status, "
        "is_system_admin, created_at) VALUES(?,?,?,'local','active',?,?)",
        (user_id, username, username, int(is_system_admin), now()),
    )
    # 提交：否则线程局部连接会一直持有未提交的写事务，阻塞任何需要独立连接
    # BEGIN IMMEDIATE 的懒加载建表（EP-03 第二阶段起 create_session() 会触发
    # app.provisioning.schema.ensure_schema() 去补 user_sessions.kind 列，
    # 2 秒超时后失败会被吞掉，看起来"成功"，随后的 INSERT 才报 "no such
    # column"——同一个坑已经在 tests/test_metrics_endpoint.py 踩过一次）。
    conn.commit()
    return user_id


def _headers(user_id: str) -> dict[str, str]:
    return {"X-Manju-Session": create_session(user_id)}


def _mk_project(conn, project_id: str, owner_user_id: str) -> None:
    conn.execute(
        "INSERT INTO projects(id, name, status, owner_user_id, created_at) VALUES(?,?,?,?,?)",
        (project_id, project_id, "created", owner_user_id, now()),
    )


def _mk_episode(conn, episode_id: str, project_id: str, episode_no: int = 1) -> None:
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, created_at) VALUES(?,?,?,?,?)",
        (episode_id, project_id, episode_no, episode_id, now()),
    )


def _mk_shot(conn, shot_id: str, episode_id: str, shot_no: int = 1) -> None:
    # shot_size/camera_move/scene_setting/action_desc 在 Shot pydantic 模型里是
    # 必填字符串字段（剧集详情接口会拿分镜行反序列化成 Shot），留 NULL 会在完全
    # 无关的业务序列化步骤炸掉，而不是我们要测的鉴权路径。
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_size, camera_move, "
        "scene_setting, action_desc) VALUES(?,?,?,5,'','','','')",
        (shot_id, episode_id, shot_no),
    )


def _mk_version(conn, version_id: str, shot_id: str, version_no: int = 1) -> None:
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (version_id, shot_id, version_no, "prompt", f"idem-{version_id}", now()),
    )


def _mk_job(conn, job_id: str, project_id: str, episode_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO jobs(id, kind, project_id, episode_id, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (job_id, "video", project_id, episode_id, "queued", now(), now()),
    )


def _mk_artifact(conn, artifact_id: str, scope_type: str, scope_id: str) -> None:
    conn.execute(
        """INSERT INTO artifacts(
               id, type, scope_type, scope_id, version, status, trust_level,
               content_hash, created_at
           ) VALUES(?,?,?,?,1,'validated','T3',?,?)""",
        (artifact_id, "character_bible", scope_type, scope_id, f"hash-{artifact_id}", now()),
    )


def _mk_package(conn, package_id: str, episode_id: str, artifact_id: str) -> None:
    conn.execute(
        """INSERT INTO delivery_packages(
               id, episode_id, artifact_id, status, package_path, manifest_json,
               quality_report_json, known_issues, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (package_id, episode_id, artifact_id, "ready", "/tmp/x", "{}", "{}", "[]", now()),
    )


def _mk_run(conn, run_id: str, scope_type: str, scope_id: str) -> None:
    conn.execute(
        """INSERT INTO workflow_runs(
               id, workflow_type, scope_type, scope_id, status, input_fingerprint, updated_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (run_id, "screenplay", scope_type, scope_id, "FAILED", f"fp-{run_id}", now()),
    )


def _mk_call(conn, project_id: str | None) -> int:
    cursor = conn.execute(
        "INSERT INTO provider_calls(ts, kind, status, project_id) VALUES(?,?,?,?)",
        (now(), "chat", "ok", project_id),
    )
    return cursor.lastrowid


def _mk_conversation(conn, conversation_id: str, *, project_id: str | None, created_by: str) -> None:
    conn.execute(
        "INSERT INTO agent_conversations(id, title, project_id, created_by, status, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (conversation_id, "t", project_id, created_by, "active", now(), now()),
    )


def _mk_turn(conn, turn_id: str, conversation_id: str) -> None:
    conn.execute(
        "INSERT INTO agent_turns(id, conversation_id, status, started_at) VALUES(?,?,?,?)",
        (turn_id, conversation_id, "finished", now()),
    )


def _mk_tool_call(conn, tool_call_id: str, turn_id: str) -> None:
    conn.execute(
        "INSERT INTO agent_tool_calls(id, turn_id, command_name, arguments_json, status) "
        "VALUES(?,?,?,?,?)",
        (tool_call_id, turn_id, "noop", "{}", "succeeded"),
    )


@pytest.fixture()
def seed():
    """两个账号 A/B，各自一整条 project -> episode -> shot -> version 链路。"""
    conn = get_conn()
    user_a = _mk_user(conn, "user-a")
    user_b = _mk_user(conn, "user-b")
    admin = _mk_user(conn, "sys-admin", is_system_admin=True)

    _mk_project(conn, "proj_a", user_a)
    _mk_project(conn, "proj_b", user_b)
    _mk_episode(conn, "ep_a", "proj_a")
    _mk_episode(conn, "ep_b", "proj_b")
    _mk_shot(conn, "shot_a", "ep_a")
    _mk_shot(conn, "shot_b", "ep_b")
    _mk_version(conn, "ver_a", "shot_a")
    _mk_version(conn, "ver_b", "shot_b")
    _mk_job(conn, "job_a", "proj_a", "ep_a")
    _mk_job(conn, "job_b", "proj_b", "ep_b")
    _mk_artifact(conn, "art_a", "project", "proj_a")
    _mk_artifact(conn, "art_b", "project", "proj_b")
    _mk_package(conn, "pkg_a", "ep_a", "art_a")
    _mk_package(conn, "pkg_b", "ep_b", "art_b")
    _mk_run(conn, "run_a_proj", "project", "proj_a")
    _mk_run(conn, "run_b_proj", "project", "proj_b")
    _mk_run(conn, "run_a_ep", "episode", "ep_a")
    _mk_run(conn, "run_a_shot_ckpt", "storyboard_checkpoint", "ep_a:1")
    call_a = _mk_call(conn, "proj_a")
    call_b = _mk_call(conn, "proj_b")
    call_null = _mk_call(conn, None)
    _mk_conversation(conn, "conv_a", project_id="proj_a", created_by=user_a)
    _mk_conversation(conn, "conv_null", project_id=None, created_by=user_a)
    _mk_turn(conn, "turn_null", "conv_null")
    _mk_tool_call(conn, "tool_null", "turn_null")
    conn.commit()

    return SimpleNamespace(
        user_a=user_a, user_b=user_b, admin=admin,
        headers_a=_headers(user_a), headers_b=_headers(user_b), headers_admin=_headers(admin),
        call_a=call_a, call_b=call_b, call_null=call_null,
    )


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client




@pytest.fixture()
def _clear_principal_after():
    yield
    set_current_principal(None)

