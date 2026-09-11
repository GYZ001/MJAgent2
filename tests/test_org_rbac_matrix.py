"""EP-01 §12 验收矩阵：组织/团队/角色/项目授权的行为断言。

一律是"做一次真实操作,看它是否真被挡住/真被放行"——不写"检查某字段等于某
值"（tests/test_rbac_enforcement_evidence.py 文件头定的规矩，本文件照用）。

两条通道分工明确，与生产代码的判定分工一致：
- Command Bus 层（``principal.can()``）：经 ``CommandBus.execute_async()``
  真实发起一次命令，看 ``CommandResult.error_code`` 是不是
  ``forbidden_role_permission``/``forbidden_admin_only``——这是"这类动作你
  一般能不能做"。审校/制作等角色的正向案例（能做的动作）只验证"没有被角色
  闸门挡住"，不追到域逻辑真正成功的 200：域逻辑成功需要完整的剧本/分镜/
  素材链路（生成一集需要走 LLM），不是本单元的职责，也不是这条判定链路本身
  要证明的东西；真正被角色闸门挡住的负向案例（如 producer 调
  video.clear_episode）反而不需要真实数据就能决定性地证明——鉴权发生在
  幂等缓存查询之前，甚至排在域对象存在性检查之前。
- HTTP 边界（``app/authz/resolve.py::_accessible``）：经真实 ``TestClient``
  发起 GET 请求，看状态码——这是"这条具体数据你碰不碰得到"。项目授权/
  跨组织隔离案例全部走这条通道，因为它们测的正是 ``_accessible()`` 新增的
  project_grants/org_admin 分支。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.principal import Principal, set_current_principal
from app.auth.sessions import create_session
from app.capabilities.bus import get_command_bus
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.schemas import CommandStatus
from app.db import get_conn, new_id, now
from app.main import app
from app.orgs import service as orgs_service
from app.orgs import store as orgs_store
from tests.rbac_isolation_helpers import _mk_episode, _mk_project, _mk_shot, _mk_user

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


# ---------------------------------------------------------------------------
# 造数据 helper
# ---------------------------------------------------------------------------


def _set_user_org(conn, user_id: str, org_id: str) -> None:
    conn.execute("UPDATE users SET org_id=? WHERE id=?", (org_id, user_id))


def _set_project_org(conn, project_id: str, org_id: str) -> None:
    conn.execute("UPDATE projects SET org_id=? WHERE id=?", (project_id, org_id))


def _builtin_role_id(conn, key: str) -> str:
    role = orgs_store.get_role_by_key(conn, None, key)
    assert role is not None, f"builtin role {key!r} missing -- app.orgs.schema.ensure_schema() 没跑过？"
    return role["id"]


def _governed_principal(user_id: str, username: str, permission_keys: frozenset[str]) -> Principal:
    return Principal(
        user_id=user_id, username=username, is_system_admin=False,
        role_governed=True, permission_keys=permission_keys,
    )


async def _run(name: str, args: dict):
    return await get_command_bus().execute_async(name, args)


def _rejected_for_permission(result) -> bool:
    return result.status == CommandStatus.REJECTED and result.error_code in {
        "forbidden_role_permission", "forbidden_admin_only",
    }


# ---------------------------------------------------------------------------
# Command Bus 层：审校 / 制作 / 只读 / 无角色
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_role_blocks_generate_but_allows_decide_commands() -> None:
    """EP-01 §6 的正确性试金石：video.generate_shot 必须 403，
    storyboard.confirm / video.adopt_version 必须不被角色闸门挡住。"""
    ensure_catalog_loaded()
    conn = get_conn()
    reviewer_role_id = _builtin_role_id(conn, "reviewer")
    permission_keys = orgs_store.list_role_permission_keys(conn, reviewer_role_id)
    assert "video.generate_shot" not in permission_keys
    assert "storyboard.confirm" in permission_keys
    assert "video.adopt_version" in permission_keys

    principal = _governed_principal("usr_reviewer", "reviewer-user", permission_keys)
    set_current_principal(principal)
    try:
        denied = await _run("video.generate_shot", {"shot_id": "shot_does_not_exist"})
        assert denied.status == CommandStatus.REJECTED
        assert denied.error_code == "forbidden_role_permission"

        confirm = await _run("storyboard.confirm", {"episode_id": "ep_does_not_exist"})
        assert not _rejected_for_permission(confirm), confirm

        adopt = await _run(
            "video.adopt_version", {"shot_id": "shot_x", "version_id": "ver_x"}
        )
        assert not _rejected_for_permission(adopt), adopt

        stop = await _run("video.stop_shot", {"shot_id": "shot_x"})
        assert not _rejected_for_permission(stop), stop
    finally:
        set_current_principal(None)


@pytest.mark.asyncio
async def test_producer_role_can_generate_but_not_review_delivery() -> None:
    ensure_catalog_loaded()
    conn = get_conn()
    producer_role_id = _builtin_role_id(conn, "producer")
    permission_keys = orgs_store.list_role_permission_keys(conn, producer_role_id)
    assert "video.generate_shot" in permission_keys
    assert "delivery.review" not in permission_keys

    principal = _governed_principal("usr_producer", "producer-user", permission_keys)
    set_current_principal(principal)
    try:
        generate = await _run("video.generate_shot", {"shot_id": "shot_does_not_exist"})
        assert not _rejected_for_permission(generate), generate

        review = await _run(
            "delivery.review", {"episode_id": "ep_x", "decision": "approve"}
        )
        assert review.status == CommandStatus.REJECTED
        assert review.error_code == "forbidden_role_permission"
    finally:
        set_current_principal(None)


@pytest.mark.asyncio
async def test_viewer_role_blocks_every_write_command() -> None:
    ensure_catalog_loaded()
    conn = get_conn()
    viewer_role_id = _builtin_role_id(conn, "viewer")
    permission_keys = orgs_store.list_role_permission_keys(conn, viewer_role_id)

    principal = _governed_principal("usr_viewer", "viewer-user", permission_keys)
    set_current_principal(principal)
    try:
        for name, args in (
            ("video.generate_shot", {"shot_id": "shot_x"}),
            ("storyboard.confirm", {"episode_id": "ep_x"}),
            ("delivery.review", {"episode_id": "ep_x", "decision": "approve"}),
        ):
            result = await _run(name, args)
            assert result.status == CommandStatus.REJECTED, (name, result)
            assert result.error_code == "forbidden_role_permission", (name, result)
    finally:
        set_current_principal(None)


@pytest.mark.asyncio
async def test_governed_user_with_empty_role_permissions_is_denied_everything() -> None:
    """空集合不等于放行：角色存在、但没配任何权限点的团队成员，写操作全 403。"""
    conn = get_conn()
    org_id = orgs_service.create_org(name="空权限测试组织", created_by="test")
    team_id = orgs_service.create_team(org_id=org_id, name="空权限团队", description=None, created_by="test")
    empty_role_id = orgs_service.create_custom_role(
        org_id=org_id, key="empty", name="空权限角色", description=None,
        permission_keys=frozenset(), created_by="test",
    )
    orgs_service.add_team_members(team_id=team_id, members=[("usr_empty", empty_role_id)], created_by="test")

    principal = _governed_principal(
        "usr_empty", "empty-user", orgs_store.user_permission_keys(conn, "usr_empty"),
    )
    assert principal.permission_keys == frozenset()
    set_current_principal(principal)
    try:
        result = await _run("video.generate_shot", {"shot_id": "shot_x"})
        assert result.status == CommandStatus.REJECTED
        assert result.error_code == "forbidden_role_permission"
    finally:
        set_current_principal(None)


@pytest.mark.asyncio
async def test_ungoverned_plain_user_keeps_full_pre_ep01_access() -> None:
    """迁移零变化：一个从未被拉进任何团队/授权的账号（本阶段唯一的开户产出
    状态），行为必须与 EP-01 之前完全一致——只受 admin_only 把关。"""
    principal = Principal(user_id="usr_plain", username="plain-user", is_system_admin=False)
    assert principal.role_governed is False
    set_current_principal(principal)
    try:
        result = await _run("video.generate_shot", {"shot_id": "shot_does_not_exist"})
        assert not _rejected_for_permission(result), result
    finally:
        set_current_principal(None)


@pytest.mark.asyncio
async def test_custom_role_created_granted_and_effective() -> None:
    """自定义角色：新建 -> 授予 -> 生效——直接用 Bus 验证被授予的那条命令真的
    放行，而没被授予的命令仍然拒绝。"""
    org_id = orgs_service.create_org(name="自定义角色测试组织", created_by="test")
    team_id = orgs_service.create_team(org_id=org_id, name="自定义团队", description=None, created_by="test")
    role_id = orgs_service.create_custom_role(
        org_id=org_id, key="stopper", name="只能停任务", description=None,
        permission_keys=frozenset({"video.stop_shot"}), created_by="test",
    )
    orgs_service.add_team_members(team_id=team_id, members=[("usr_custom", role_id)], created_by="test")

    conn = get_conn()
    principal = _governed_principal(
        "usr_custom", "custom-user", orgs_store.user_permission_keys(conn, "usr_custom"),
    )
    set_current_principal(principal)
    try:
        allowed = await _run("video.stop_shot", {"shot_id": "shot_x"})
        assert not _rejected_for_permission(allowed), allowed
        denied = await _run("video.generate_shot", {"shot_id": "shot_x"})
        assert denied.status == CommandStatus.REJECTED
        assert denied.error_code == "forbidden_role_permission"
    finally:
        set_current_principal(None)


def test_custom_role_delete_conflicts_while_referenced_then_succeeds() -> None:
    org_id = orgs_service.create_org(name="角色删除测试组织", created_by="test")
    team_id = orgs_service.create_team(org_id=org_id, name="临时团队", description=None, created_by="test")
    role_id = orgs_service.create_custom_role(
        org_id=org_id, key="temp", name="临时角色", description=None,
        permission_keys=frozenset({"video.stop_shot"}), created_by="test",
    )
    orgs_service.add_team_members(team_id=team_id, members=[("usr_temp", role_id)], created_by="test")

    with pytest.raises(HTTPException) as exc_info:
        orgs_service.delete_role(role_id=role_id)
    assert exc_info.value.status_code == 409

    orgs_service.remove_team_member(team_id=team_id, user_id="usr_temp")
    orgs_service.delete_role(role_id=role_id)  # 不再被引用，应当成功、不抛异常
    conn = get_conn()
    assert orgs_store.get_role(conn, role_id) is None


def test_builtin_role_cannot_be_deleted_or_edited() -> None:
    conn = get_conn()
    reviewer_role_id = _builtin_role_id(conn, "reviewer")
    with pytest.raises(HTTPException) as exc_info:
        orgs_service.delete_role(role_id=reviewer_role_id)
    assert exc_info.value.status_code == 422
    with pytest.raises(HTTPException) as exc_info:
        orgs_service.update_role_permissions(role_id=reviewer_role_id, permission_keys=frozenset())
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# HTTP 边界：跨组织隔离 / project_grants / org_admin
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _login(user_id: str) -> dict[str, str]:
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


def test_cross_org_project_and_episode_access_is_404(client: TestClient) -> None:
    conn = get_conn()
    org_b = orgs_service.create_org(name="B组织", created_by="test")
    user_a = _mk_user(conn, "org-a-user")
    user_b = _mk_user(conn, "org-b-user")
    _set_user_org(conn, user_a, orgs_store.ORG_DEFAULT_ID)
    _set_user_org(conn, user_b, org_b)
    _mk_project(conn, "proj_org_a", user_a)
    _set_project_org(conn, "proj_org_a", orgs_store.ORG_DEFAULT_ID)
    _mk_episode(conn, "ep_org_a", "proj_org_a")
    _mk_shot(conn, "shot_org_a", "ep_org_a")
    conn.commit()

    headers_b = _login(user_b)
    assert client.get("/api/projects/proj_org_a", headers=headers_b).status_code == 404
    assert client.get("/api/episodes/ep_org_a", headers=headers_b).status_code == 404

    headers_a = _login(user_a)
    assert client.get("/api/projects/proj_org_a", headers=headers_a).status_code == 200


def test_org_admin_can_access_any_project_within_same_org(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, "owner-of-proj-x")
    admin_user = _mk_user(conn, "org-admin-user")
    _set_user_org(conn, owner, orgs_store.ORG_DEFAULT_ID)
    _set_user_org(conn, admin_user, orgs_store.ORG_DEFAULT_ID)
    _mk_project(conn, "proj_org_admin_target", owner)
    _set_project_org(conn, "proj_org_admin_target", orgs_store.ORG_DEFAULT_ID)
    conn.commit()

    org_admin_role_id = _builtin_role_id(conn, "org_admin")
    team_id = orgs_service.create_team(
        org_id=orgs_store.ORG_DEFAULT_ID, name="组织管理团队", description=None, created_by="test",
    )
    orgs_service.add_team_members(team_id=team_id, members=[(admin_user, org_admin_role_id)], created_by="test")

    headers_admin = _login(admin_user)
    resp = client.get("/api/projects/proj_org_admin_target", headers=headers_admin)
    assert resp.status_code == 200, resp.text


def test_individual_project_grant_bypasses_owner_check(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, "owner-of-proj-y")
    outsider = _mk_user(conn, "grantee-user")
    _mk_project(conn, "proj_grant_target", owner)
    conn.commit()

    viewer_role_id = _builtin_role_id(conn, "viewer")
    orgs_service.grant_project_access(
        project_id="proj_grant_target", subject_type="user", subject_id=outsider,
        role_id=viewer_role_id, created_by="test",
    )

    headers = _login(outsider)
    assert client.get("/api/projects/proj_grant_target", headers=headers).status_code == 200


def test_team_project_grant_covers_all_members(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, "owner-of-proj-z")
    member = _mk_user(conn, "team-grant-member")
    outsider = _mk_user(conn, "not-in-team")
    _mk_project(conn, "proj_team_grant_target", owner)
    conn.commit()

    viewer_role_id = _builtin_role_id(conn, "viewer")
    team_id = orgs_service.create_team(
        org_id=orgs_store.ORG_DEFAULT_ID, name="被授权团队", description=None, created_by="test",
    )
    orgs_service.add_team_members(team_id=team_id, members=[(member, viewer_role_id)], created_by="test")
    orgs_service.grant_project_access(
        project_id="proj_team_grant_target", subject_type="team", subject_id=team_id,
        role_id=viewer_role_id, created_by="test",
    )

    assert client.get("/api/projects/proj_team_grant_target", headers=_login(member)).status_code == 200
    assert client.get("/api/projects/proj_team_grant_target", headers=_login(outsider)).status_code == 404


def test_project_grant_revocation_removes_access(client: TestClient) -> None:
    conn = get_conn()
    owner = _mk_user(conn, "owner-of-proj-revoke")
    grantee = _mk_user(conn, "revoked-grantee")
    _mk_project(conn, "proj_revoke_target", owner)
    conn.commit()

    viewer_role_id = _builtin_role_id(conn, "viewer")
    orgs_service.grant_project_access(
        project_id="proj_revoke_target", subject_type="user", subject_id=grantee,
        role_id=viewer_role_id, created_by="test",
    )
    headers = _login(grantee)
    assert client.get("/api/projects/proj_revoke_target", headers=headers).status_code == 200

    revoked = orgs_service.revoke_project_access(
        project_id="proj_revoke_target", subject_type="user", subject_id=grantee,
    )
    assert revoked is True
    assert client.get("/api/projects/proj_revoke_target", headers=headers).status_code == 404
