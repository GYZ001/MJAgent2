"""观测数据只对租户管理员开放（2026-09-03 用户拍板）。

普通会员账号在界面上连观测台按钮都看不到（frontend/src/appSections.ts 的
``adminOnly`` + App.tsx 的兜底跳转），但**前端隐藏只是体验层面**——真正的边界在
这里：``app/main.py`` 给 ``observability_router`` 挂了 ``require_system_admin``，
``app/system_api.py`` 的 ``/system/(jobs|calls|errors)`` 逐条挂了同一个依赖。

两档断言各自独立，缺一不可：

1. **结构档**：遍历真实 ``app.main:app`` 的路由表，任何路径里带 observability
   的路由都必须挂着 ``require_system_admin``。新加一条观测路由却忘了挂闸门，
   在这里立刻红——不依赖有没有人记得为它补一条 HTTP 用例。
2. **行为档**：用真实 ``TestClient(app)`` 打 HTTP，非管理员（哪怕是项目所有者
   本人）拿 403、管理员拿 200。只验依赖函数本身不算数——那正是 CLAUDE 记录里
   ContextVar fail-open 的教训。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth.deps import require_system_admin
from app.auth.sessions import create_session
from app import system_api
from app.db import get_conn, new_id, now
from app.main import app
from app.observability import api as observability_api


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


@pytest.fixture()
def seed():
    conn = get_conn()
    member = _mk_user(conn, "obs-member")
    admin = _mk_user(conn, "obs-admin", is_system_admin=True)
    conn.execute(
        "INSERT INTO projects(id, name, status, owner_user_id, created_at) VALUES(?,?,?,?,?)",
        ("proj_obs", "观测项目", "created", member, now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, title, created_at) VALUES(?,?,?,?,?)",
        ("ep_obs", "proj_obs", 1, "第一集", now()),
    )
    conn.execute(
        "INSERT INTO jobs(id, kind, project_id, episode_id, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("job_obs", "video", "proj_obs", "ep_obs", "queued", now(), now()),
    )
    conn.execute(
        """INSERT INTO workflow_runs(
               id, workflow_type, scope_type, scope_id, status, input_fingerprint, updated_at
           ) VALUES(?,?,?,?,?,?,?)""",
        ("run_obs", "screenplay", "episode", "ep_obs", "FAILED", "fp-obs", now()),
    )
    conn.execute(
        "INSERT INTO provider_calls(ts, kind, status, project_id) VALUES(?,?,?,?)",
        (now(), "chat", "ok", "proj_obs"),
    )
    conn.commit()
    return SimpleNamespace(
        headers_member=_headers(member),
        headers_admin=_headers(admin),
    )


@pytest.fixture()
def client():
    with TestClient(app) as raw:
        yield raw


def _route_requires_admin(route: APIRoute) -> bool:
    return any(
        getattr(dependency, "dependency", None) is require_system_admin
        for dependency in route.dependencies
    )


def _sample_path(route: APIRoute) -> str:
    """把路由模板里的占位符填成本 fixture 造好的对象（其余填一个存在也无妨的
    占位值）——闸门排在处理器之前，对象存不存在都不改变判定。"""
    filled = route.path
    for name, value in (
        ("{project_id}", "proj_obs"), ("{run_id}", "run_obs"), ("{job_id}", "job_obs"),
        ("{call_id}", "1"), ("{action}", "cancel"), ("{object_type}", "runs"),
        ("{object_id}", "run_obs"), ("{node_id}", "run%3Arun_obs"),
        ("{artifact_id}", "art_obs"), ("{step_run_id}", "sr_obs"), ("{index}", "0"),
    ):
        filled = filled.replace(name, value)
    return filled


def test_every_observability_route_rejects_a_member(seed, client) -> None:
    """遍历 observability_router 的**每一条**路由实打实发一次请求。

    闸门挂在 app/main.py 的 include_router 上（这个 FastAPI 版本把它折叠进
    ``_IncludedRouter``，路由表上翻不出依赖列表），所以这里只能用行为断言——
    这也更硬：新加一条观测路由却漏了闸门，会在这里立刻红，不必有人记得补用例。
    """
    routes = [route for route in observability_api.router.routes if isinstance(route, APIRoute)]
    assert routes, "观测路由一条都没枚举到——router 结构变了就把这里一起改"
    leaked = []
    for route in routes:
        path = _sample_path(route)
        if "{" in path:  # 出现没登记的占位符：宁可红，也不要静默跳过
            leaked.append(f"未知占位符 {route.path}")
            continue
        method = "GET" if "GET" in route.methods else sorted(route.methods)[0]
        resp = client.request(method, path, headers=seed.headers_member, json={})
        if resp.status_code != 403:
            leaked.append(f"{method} {path} -> {resp.status_code}")
    assert leaked == [], f"这些观测路由没把普通会员挡在 403：{leaked}"


@pytest.mark.parametrize(
    "path",
    [
        "/api/system/jobs",
        "/api/system/jobs/query",
        "/api/system/calls",
        "/api/system/calls/query",
        "/api/system/errors",
    ],
)
def test_legacy_system_observability_lists_carry_the_admin_gate(path: str) -> None:
    route = next(
        route for route in system_api.router.routes
        if isinstance(route, APIRoute)
        and route.path in (path, path.removeprefix("/api"))
    )
    assert _route_requires_admin(route), f"{path} 没挂 require_system_admin"


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/proj_obs/observability/jobs",
        "/api/projects/proj_obs/observability/runs",
        "/api/projects/proj_obs/observability/calls",
        "/api/projects/proj_obs/observability/jobs/job_obs",
        "/api/projects/proj_obs/observability/runs/run_obs",
    ],
)
def test_project_owner_without_admin_gets_403(seed, client, path: str) -> None:
    """项目所有者本人也看不到自己项目的观测数据——这正是本次要的语义。"""
    denied = client.get(path, headers=seed.headers_member)
    assert denied.status_code == 403, f"{path} -> {denied.status_code} {denied.text}"


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/proj_obs/observability/jobs",
        "/api/projects/proj_obs/observability/runs",
        "/api/projects/proj_obs/observability/calls",
        "/api/system/jobs",
        "/api/system/calls",
        "/api/system/errors",
    ],
)
def test_tenant_admin_still_reads_everything(seed, client, path: str) -> None:
    allowed = client.get(path, headers=seed.headers_admin)
    assert allowed.status_code == 200, f"{path} -> {allowed.status_code} {allowed.text}"


def test_legacy_system_lists_are_closed_to_members(seed, client) -> None:
    for path in ("/api/system/jobs", "/api/system/calls", "/api/system/errors"):
        denied = client.get(path, headers=seed.headers_member)
        assert denied.status_code == 403, f"{path} -> {denied.status_code} {denied.text}"


def test_legacy_resolve_entry_is_closed_to_members(seed, client) -> None:
    """旧观测链接的解析入口同样要关：它会回答「这个 run 属于哪个项目」。"""
    denied = client.get(
        "/api/observability/resolve?run_id=run_obs", headers=seed.headers_member
    )
    assert denied.status_code == 403, denied.text
