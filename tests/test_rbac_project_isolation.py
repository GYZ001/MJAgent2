"""账号即项目空间：账号级项目隔离的硬证明。

覆盖三条线：
1. ``app.authz.resolve.resolve_request_scope`` 这张解析表本身——直接单元调用，
   逐个核对 run_id/artifact_id 的 scope_type+scope_id 解释、call_id/
   conversation_id 的 NULL 归属兜底、error_id/token_id 的系统管理员专属。
2. ``app.authz.require_project_owner_access`` 真的挂在了 ``app.main:app`` 上、
   真的读到了中间件注入的 Principal——通过 ``TestClient(app)`` 打真实 HTTP
   请求验证，而不是只调用依赖函数本身（这正是 CLAUDE 记录里那次 ContextVar
   fail-open 的教训：写测试也不能只信任「看起来接上了」）。
3. **绕过 HTTP、直接调 domain 函数**：``app.domain.common._project_or_404``/
   ``_episode_or_404`` 是 domain 层第二道独立闸门（见该文件的 ``_assert_
   principal_owns``），只要 ContextVar 里有 Principal，即便完全不经过
   ``TestClient``/ASGI 路由也一样按归属拦截——证明隔离不是「只在 HTTP 边界
   记得检查」，而是"新端点忘了在路由上挂鉴权，只要它调用了这两个几乎所有
   业务函数都会先调用的入口，归属校验依然生效"。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.principal import Principal, set_current_principal
from app.auth.sessions import create_session
from app.authz.resolve import resolve_request_scope
from app.db import get_conn, new_id, now
from app.main import app
from tests.conftest import SessionTestClient


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


# ---------------------------------------------------------------------------
# 解析表本身：直接单元调用 resolve_request_scope，覆盖 CLAUDE 任务书里列的每
# 一种归属规则。
# ---------------------------------------------------------------------------


def test_resolver_project_id_direct(seed):
    resolution = resolve_request_scope({"project_id": "proj_a"}, {})
    assert (resolution.kind, resolution.value) == ("owner", seed.user_a)


def test_resolver_episode_and_shot_and_version_chain(seed):
    assert resolve_request_scope({"episode_id": "ep_b"}, {}) == resolve_request_scope(
        {"project_id": "proj_b"}, {}
    )
    assert resolve_request_scope({"shot_id": "shot_a"}, {}).value == seed.user_a
    assert resolve_request_scope({"version_id": "ver_b"}, {}).value == seed.user_b


def test_resolver_job_and_package(seed):
    assert resolve_request_scope({"job_id": "job_a"}, {}).value == seed.user_a
    assert resolve_request_scope({"package_id": "pkg_b"}, {}).value == seed.user_b


def test_resolver_run_scope_project_episode_and_derived_prefix(seed):
    """run_id 的 scope_type 只是提示，真正归属看 scope_id 冒号前缀（与
    ``_delete_scoped_evidence`` 的解释口径一致）：storyboard_checkpoint 的前缀
    其实是 episode_id，照样能解析到项目。"""
    assert resolve_request_scope({"run_id": "run_a_proj"}, {}).value == seed.user_a
    assert resolve_request_scope({"run_id": "run_a_ep"}, {}).value == seed.user_a
    assert resolve_request_scope({"run_id": "run_a_shot_ckpt"}, {}).value == seed.user_a


def test_resolver_artifact_scope(seed):
    assert resolve_request_scope({"artifact_id": "art_a"}, {}).value == seed.user_a
    assert resolve_request_scope({"artifact_id": "art_b"}, {}).value == seed.user_b


def test_resolver_call_id_null_project_is_admin_only(seed):
    resolved = resolve_request_scope({"call_id": str(seed.call_a)}, {})
    assert (resolved.kind, resolved.value) == ("owner", seed.user_a)
    null_resolved = resolve_request_scope({"call_id": str(seed.call_null)}, {})
    assert null_resolved.kind == "admin_only"


def test_resolver_conversation_turn_tool_call_chain(seed):
    assert resolve_request_scope({"conversation_id": "conv_a"}, {}).value == seed.user_a
    null_conv = resolve_request_scope({"conversation_id": "conv_null"}, {})
    assert (null_conv.kind, null_conv.value) == ("creator", seed.user_a)
    # turn_id / tool_call_id 沿着 turn -> conversation 传递同一个 creator 兜底。
    turn_resolved = resolve_request_scope({"turn_id": "turn_null"}, {})
    assert (turn_resolved.kind, turn_resolved.value) == ("creator", seed.user_a)
    tool_call_resolved = resolve_request_scope({"tool_call_id": "tool_null"}, {})
    assert (tool_call_resolved.kind, tool_call_resolved.value) == ("creator", seed.user_a)


def test_resolver_error_and_token_always_admin_only(seed):
    assert resolve_request_scope({"error_id": "err-anything"}, {}).kind == "admin_only"
    assert resolve_request_scope({"token_id": "tok-anything"}, {}).kind == "admin_only"


def test_resolver_unrecognized_or_missing_object_is_unresolved(seed):
    assert resolve_request_scope({"scene_name": "s1"}, {}).kind == "none"
    assert resolve_request_scope({"project_id": "no-such-project"}, {}).kind == "none"
    assert resolve_request_scope({}, {}).kind == "none"


def test_resolver_project_id_wins_over_other_params_in_same_path(seed):
    """嵌套路由里 project_id 与其他参数同时出现时，project_id 命中优先，不用
    为了别的参数多查一次库（即便那个参数属于另一个账号也不影响这里的结论，
    业务路由自己的细粒度校验负责拦这种越权）。"""
    resolution = resolve_request_scope({"project_id": "proj_a", "artifact_id": "art_b"}, {})
    assert resolution.value == seed.user_a


# ---------------------------------------------------------------------------
# 真·HTTP：经 app.main:app 的中间件 + require_project_owner_access 依赖，证明
# Principal 真的在依赖里可读（而不是同步依赖里写 ContextVar 那种看似接上、
# 实则 fail-open 的坑）。
# ---------------------------------------------------------------------------


def test_project_detail_cross_account_is_404(seed, client):
    own = client.get("/api/projects/proj_a", headers=seed.headers_a)
    assert own.status_code == 200, own.text
    foreign = client.get("/api/projects/proj_b", headers=seed.headers_a)
    assert foreign.status_code == 404
    assert "proj_b" not in foreign.text


def test_episode_detail_cross_account_is_404(seed, client):
    assert client.get("/api/episodes/ep_a", headers=seed.headers_a).status_code == 200
    assert client.get("/api/episodes/ep_b", headers=seed.headers_a).status_code == 404


def test_artifact_detail_cross_account_is_404(seed, client):
    assert client.get("/api/artifacts/art_a", headers=seed.headers_a).status_code == 200
    assert client.get("/api/artifacts/art_b", headers=seed.headers_a).status_code == 404


def test_job_detail_cross_account_is_404(seed, client):
    own = client.get("/api/system/jobs/job_a", headers=seed.headers_a)
    assert own.status_code == 200, own.text
    assert client.get("/api/system/jobs/job_b", headers=seed.headers_a).status_code == 404


def test_conversation_cross_account_is_404(seed, client):
    own = client.get("/api/agent/conversations/conv_a", headers=seed.headers_a)
    assert own.status_code == 200, own.text
    assert client.get("/api/agent/conversations/conv_a", headers=seed.headers_b).status_code == 404


def test_conversation_null_project_visible_only_to_creator_or_admin(seed, client):
    creator = client.get("/api/agent/conversations/conv_null", headers=seed.headers_a)
    assert creator.status_code == 200, creator.text
    other = client.get("/api/agent/conversations/conv_null", headers=seed.headers_b)
    assert other.status_code == 404
    admin = client.get("/api/agent/conversations/conv_null", headers=seed.headers_admin)
    assert admin.status_code == 200


def test_system_admin_reaches_both_accounts(seed, client):
    assert client.get("/api/projects/proj_a", headers=seed.headers_admin).status_code == 200
    assert client.get("/api/projects/proj_b", headers=seed.headers_admin).status_code == 200
    assert client.get("/api/episodes/ep_b", headers=seed.headers_admin).status_code == 200
    assert client.get("/api/system/jobs/job_b", headers=seed.headers_admin).status_code == 200


def test_projects_list_scoped_to_caller_account(seed, client):
    listing_a = client.get("/api/projects", headers=seed.headers_a)
    assert listing_a.status_code == 200
    ids_a = {row["id"] for row in listing_a.json()}
    assert ids_a == {"proj_a"}

    listing_b = client.get("/api/projects", headers=seed.headers_b)
    ids_b = {row["id"] for row in listing_b.json()}
    assert ids_b == {"proj_b"}

    listing_admin = client.get("/api/projects", headers=seed.headers_admin)
    ids_admin = {row["id"] for row in listing_admin.json()}
    assert {"proj_a", "proj_b"} <= ids_admin


def test_project_write_endpoint_cross_account_is_404(seed, client):
    """至少一个写操作端点：账号 A 不能改账号 B 项目的分环节文本模型设置。"""
    denied = client.put(
        "/api/projects/proj_b/text-models", headers=seed.headers_a,
        json={"bible_text_provider": ""},
    )
    assert denied.status_code == 404
    own = client.put(
        "/api/projects/proj_a/text-models", headers=seed.headers_a,
        json={"bible_text_provider": ""},
    )
    assert own.status_code in (200, 400, 422), own.text  # 只要不是 404 就说明鉴权放行了


@pytest.mark.parametrize("path", ["/api/runs", "/api/runs/query", "/api/gates"])
def test_global_observability_collections_are_admin_only(seed, client, path):
    denied = client.get(path, headers=seed.headers_a)
    assert denied.status_code == 403
    allowed = client.get(path, headers=seed.headers_admin)
    assert allowed.status_code == 200


def test_global_run_detail_steps_events_are_admin_only(seed, client):
    for suffix in ("", "/steps", "/events"):
        denied = client.get(f"/api/runs/run_a_proj{suffix}", headers=seed.headers_a)
        assert denied.status_code == 403, (suffix, denied.text)
        allowed = client.get(f"/api/runs/run_a_proj{suffix}", headers=seed.headers_admin)
        assert allowed.status_code == 200, (suffix, allowed.text)


def test_system_overview_is_admin_only(seed, client):
    """账号即项目空间之后，「全部项目」本身就是跨账号信息（见
    app/observability/api.py::system_overview 加固时的说明）。"""
    denied = client.get("/api/system/overview", headers=seed.headers_a)
    assert denied.status_code == 403
    allowed = client.get("/api/system/overview", headers=seed.headers_admin)
    assert allowed.status_code == 200


def test_project_scoped_observability_still_works_for_members(seed, client):
    """项目内的观测入口继续对项目所有者开放（_assert_scope 的既有细粒度校验不受
    影响），只是全局跨项目入口收紧到系统管理员。"""
    own = client.get(
        "/api/projects/proj_a/observability/runs/run_a_proj", headers=seed.headers_a
    )
    assert own.status_code == 200, own.text
    foreign = client.get(
        "/api/projects/proj_a/observability/runs/run_a_proj", headers=seed.headers_b
    )
    assert foreign.status_code == 404


# ---------------------------------------------------------------------------
# 回归闸门：既有的系统管理员 / 旧调用方在本阶段前后行为必须完全一致。
# ---------------------------------------------------------------------------


def test_legacy_system_admin_caller_sees_no_behaviour_change(seed):
    """``SessionTestClient`` 默认签发系统管理员会话（tests/conftest.py 既有约定），
    模拟另一个会话反复重启后端后跑的回归脚本：本阶段新增的拦截对它必须完全
    透明——所有历史上能访问的入口，现在依然一样能访问。"""
    with TestClient(app) as raw_client:
        legacy = SessionTestClient(raw_client)
        assert legacy.get("/api/projects").status_code == 200
        assert legacy.get("/api/projects/proj_a").status_code == 200
        assert legacy.get("/api/projects/proj_b").status_code == 200
        assert legacy.get("/api/episodes/ep_a").status_code == 200
        assert legacy.get("/api/episodes/ep_b").status_code == 200
        assert legacy.get("/api/system/jobs/job_a").status_code == 200
        assert legacy.get("/api/system/jobs/job_b").status_code == 200
        assert legacy.get("/api/artifacts/art_a").status_code == 200
        assert legacy.get("/api/artifacts/art_b").status_code == 200
        assert legacy.get("/api/runs").status_code == 200
        assert legacy.get("/api/gates").status_code == 200


# ---------------------------------------------------------------------------
# 绕过 HTTP：直接调 domain 函数，证明隔离不是只挂在 ASGI 路由这一层。
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clear_principal_after():
    yield
    set_current_principal(None)


def test_project_or_404_enforces_ownership_without_any_http_request(seed, _clear_principal_after):
    """``app.domain.common._project_or_404`` 是几乎所有 bible/screenplay/
    storyboard/video 端点的项目存在性入口。这里完全不经过 TestClient/ASGI，
    直接调用它——只要 ContextVar 里有 Principal，归属校验照样生效，证明就算
    有人新写一个端点、忘了在路由上挂 require_project_owner_access，只要它调用
    了这个（几乎必然会调用的）入口，依然拿不到别人的项目。"""
    from app.domain.common import _project_or_404

    set_current_principal(Principal(user_id=seed.user_a, username="user-a", is_system_admin=False))
    own = _project_or_404("proj_a")
    assert own["id"] == "proj_a"

    with pytest.raises(HTTPException) as exc:
        _project_or_404("proj_b")
    assert exc.value.status_code == 404


def test_deleted_project_or_404_enforces_ownership_without_any_http_request(
    seed, _clear_principal_after
):
    """回收站的存在性入口也必须自带归属校验。

    ``_deleted_project_or_404`` 是「恢复」与「彻底清理」两个端点共用的入口，
    而彻底清理是**不可逆**的（删库行 + rmtree 产物目录）。它一度只查
    ``WHERE id=? AND deleted_at IS NOT NULL``、不校归属：HTTP 边界的
    ``require_project_owner_access`` 能挡住走路由的请求，但 Agent/MCP 工具
    调用、内部脚本、测试直接进 domain 函数会完全绕过它——那条路径上任何账号
    都能按 id 恢复或永久销毁**别人**回收站里的项目。

    静态 SQL 守卫看不见这个洞：它对 ``WHERE id=?`` 按「主键锚定」放行，前提是
    「这个 id 进系统时已过归属闸门」，而这条路径上没有。所以判据只能落在这里。
    """
    from app.domain import projects as projects_mod

    conn = projects_mod.get_conn()
    conn.execute("UPDATE projects SET deleted_at=? WHERE id=?", (1.0, "proj_b"))
    conn.commit()
    try:
        set_current_principal(
            Principal(user_id=seed.user_b, username="user-b", is_system_admin=False)
        )
        own = projects_mod._deleted_project_or_404("proj_b")
        assert own["id"] == "proj_b"

        set_current_principal(
            Principal(user_id=seed.user_a, username="user-a", is_system_admin=False)
        )
        with pytest.raises(HTTPException) as exc:
            projects_mod._deleted_project_or_404("proj_b")
        assert exc.value.status_code == 404, "跨账号必须 404，不得泄漏存在性"
    finally:
        conn.execute("UPDATE projects SET deleted_at=NULL WHERE id=?", ("proj_b",))
        conn.commit()


def test_episode_or_404_enforces_ownership_without_any_http_request(seed, _clear_principal_after):
    from app.domain.common import _episode_or_404

    set_current_principal(Principal(user_id=seed.user_b, username="user-b", is_system_admin=False))
    own = _episode_or_404("ep_b")
    assert own["id"] == "ep_b"

    with pytest.raises(HTTPException) as exc:
        _episode_or_404("ep_a")
    assert exc.value.status_code == 404


def test_domain_layer_admin_principal_bypasses_ownership_without_http(seed, _clear_principal_after):
    """系统管理员的跨账号可见性同样在 domain 层生效，不只是 HTTP 边界的特例。"""
    from app.domain.common import _episode_or_404, _project_or_404

    set_current_principal(Principal(user_id=seed.admin, username="sys-admin", is_system_admin=True))
    assert _project_or_404("proj_a")["id"] == "proj_a"
    assert _project_or_404("proj_b")["id"] == "proj_b"
    assert _episode_or_404("ep_a")["id"] == "ep_a"
    assert _episode_or_404("ep_b")["id"] == "ep_b"


def test_domain_layer_no_principal_context_keeps_legacy_internal_call_behaviour(
    seed, _clear_principal_after
):
    """``principal is None``（后台任务/CLI/未注入身份的内部调用）不受这道闸门
    限制——与全仓既有约定一致（HTTP 边界、Command Bus 都是同一条口径），不是
    这里新引入的例外。"""
    from app.domain.common import _project_or_404

    set_current_principal(None)
    assert _project_or_404("proj_a")["id"] == "proj_a"
    assert _project_or_404("proj_b")["id"] == "proj_b"


def test_list_projects_direct_call_still_scoped_without_http(seed, _clear_principal_after):
    """列表函数直接调用（不经 HTTP）一样按调用者账号过滤。"""
    from app.domain.projects import list_projects

    set_current_principal(Principal(user_id=seed.user_a, username="user-a", is_system_admin=False))
    ids = {row["id"] for row in list_projects()}
    assert ids == {"proj_a"}
