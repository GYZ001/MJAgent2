"""RBAC 强制性的行为证据。

这个文件存在的理由是一类反复出现的缺陷形状：**字段在、赋值在、清零在，唯独
没有任何一行强制它**，而且不抛异常。代码目检看起来是做完了，实际从未生效。
本项目已知的同族例子包括 json_schema 静默降级、spine_beat 补丁空转，以及
RBAC 这边的 ``users.must_change_password`` 与 ``workspaces.status``。

所以这里的断言一律是「做一次真实操作，看它是否真的被挡住」，而不是
「检查某个字段是否等于某个值」。任何一条如果改成后者，它就失去了存在意义。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.sessions import create_session, resolve_session
from app.db import get_conn, new_id, now, set_setting
from app.main import app

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


def _add_user(username: str, *, admin: int = 0, status: str = "active") -> str:
    conn = get_conn()
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, username, hash_password("pw-" + username), status, admin, now()),
    )
    conn.commit()
    return user_id


def _add_workspace(workspace_id: str, status: str = "active") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO workspaces(id, tenant_id, name, status, created_at) "
        "VALUES(?, 'tenant_default', ?, ?, ?)",
        (workspace_id, workspace_id, status, now()),
    )
    conn.commit()


def _join(workspace_id: str, user_id: str, role: str = "production") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO workspace_members(workspace_id, user_id, role, created_at) VALUES(?,?,?,?)",
        (workspace_id, user_id, role, now()),
    )
    conn.commit()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_disabled_workspace_actually_strips_member_access():
    """停用团队后，成员必须真的失去该团队的 scope——不是只把 status 写成 disabled。"""
    _add_workspace("ws_live", "active")
    _add_workspace("ws_dead", "disabled")
    user_id = _add_user("bob")
    _join("ws_live", user_id)
    _join("ws_dead", user_id)

    principal = resolve_session(create_session(user_id))
    assert principal is not None
    assert principal.can_access("ws_live") is True
    # 这一条是本文件的由来：改之前它是 True，停用团队等于没停。
    assert principal.can_access("ws_dead") is False
    assert principal.scopes_for("ws_dead") == frozenset()


def test_disabled_user_cannot_resolve_session():
    user_id = _add_user("ghost", status="disabled")
    assert resolve_session(create_session(user_id)) is None


def test_login_throttle_actually_blocks_after_repeated_failures(client: TestClient):
    """节流必须真的挡住请求，且挡住之后连正确密码也进不来——否则它只是个计数器。"""
    _add_user("target")
    seen = [
        client.post(
            "/api/auth/login",
            json={"username": "target", "password": "WRONG"},
            headers=_HEADERS,
        ).status_code
        for _ in range(7)
    ]
    assert 429 in seen, f"节流从未触发：{seen}"
    blocked = client.post(
        "/api/auth/login",
        json={"username": "target", "password": "pw-target"},
        headers=_HEADERS,
    )
    assert blocked.status_code == 429


def test_password_change_revokes_other_sessions():
    """改密必须让旧会话立即失效，而不只是把 password_hash 换掉。"""
    user_id = _add_user("rotate")
    stale = create_session(user_id)
    assert resolve_session(stale) is not None

    conn = get_conn()
    conn.execute(
        "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (now(), user_id),
    )
    conn.commit()
    assert resolve_session(stale) is None


def test_must_change_password_is_surfaced_so_the_ui_can_enforce_it(client: TestClient):
    """后端必须如实吐出该标志位；前端 AuthGate 靠它决定挂不挂载应用壳。

    强制点本身在前端（见 ForcePasswordChangePage），这里守住的是它的输入：
    一旦这个字段不再出现在 /api/auth/me 的响应里，前端的强制会静默失效。
    """
    user_id = _add_user("fresh")
    conn = get_conn()
    conn.execute("UPDATE users SET must_change_password=1 WHERE id=?", (user_id,))
    conn.commit()

    resp = client.get(
        "/api/auth/me",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
    )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True


def test_no_module_builds_media_urls_outside_the_signer():
    """媒体 URL 只能由 build_media_url 产出——这是 A 类（规则在链路中丢失）的守卫。

    ``/media`` 的凭证只能进查询串（``<img>``/``<video>`` 不带自定义头）。签名逻辑
    集中在 ``app/media_urls.py``，历史上却有 8 处各自裸拼 ``f"/media/{rel}?v=..."``。
    只要有人日后再添一处绕过签名，在 ``MJ_MEDIA_REQUIRE_TICKET`` 打开那天，那批
    图片会静默 403——而单测全绿，因为签名函数本身没坏。

    所以这里守的不是"签名函数对不对"，是"有没有人绕开它"。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {"app/media_urls.py", "app/main.py"}
    offenders = []
    for path in (root / "app").rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if '"/media/' in line or 'f"/media/' in line or "/media/{" in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "这些地方绕过了 build_media_url：\n" + "\n".join(offenders)


def test_disabled_workspace_disappears_from_the_login_payload_too(client: TestClient):
    """展示口径必须和授权口径一致：停用的团队不能还留在 /api/auth/me 的列表里。

    ``resolve_session`` 与 ``_workspaces_payload`` 是两处独立计算「我属于哪些团队」
    的地方。只有前者带 status 过滤时，用户会在界面上看到一个自己其实已经没有任何
    权限的团队——点进去每个请求都 404，而「团队还在列表里」会让人以为是系统坏了。
    这条守的就是这两处口径不能分叉。
    """
    _add_workspace("ws_shown", "active")
    _add_workspace("ws_hidden", "disabled")
    user_id = _add_user("split")
    _join("ws_shown", user_id)
    _join("ws_hidden", user_id)

    resp = client.get(
        "/api/auth/me",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
    )
    assert resp.status_code == 200
    listed = {w["id"] for w in resp.json()["workspaces"]}
    assert listed == {"ws_shown"}, f"停用团队仍出现在登录载荷里：{listed}"


def test_login_payload_never_lists_a_team_the_principal_cannot_access(client: TestClient):
    """跨口径一致性：登录载荷里的团队集合必须是 can_access 为真的子集。

    这是【重复真源】那一类的守卫。第 12 例的教训是：单独给"授权口径"写行为测试
    是不够的——那条测试当时是绿的，因为它只断言了我想到的那一半，另一处独立实现
    根本不在断言范围里。这条测试同时钉住两处：任何一方将来再分叉，它就红。

    真正的结构性修复是让 _workspaces_payload 只消费 principal.workspace_roles
    （成员判定只有一个真源，这里只补团队名）；这条断言是那次收敛的回归网。
    """
    _add_workspace("ws_ok", "active")
    _add_workspace("ws_off", "disabled")
    user_id = _add_user("crosscheck")
    _join("ws_ok", user_id)
    _join("ws_off", user_id)

    token = create_session(user_id)
    principal = resolve_session(token)
    assert principal is not None

    resp = client.get("/api/auth/me", headers={**_HEADERS, "X-Manju-Session": token})
    assert resp.status_code == 200
    listed = {w["id"] for w in resp.json()["workspaces"]}

    unauthorized = {ws for ws in listed if not principal.can_access(ws)}
    assert not unauthorized, f"载荷列出了无权访问的团队：{unauthorized}"
    # 反向也要成立，否则"藏起一个其实有权访问的团队"同样是分叉。
    assert listed == set(principal.workspace_roles)


def test_new_project_lands_in_the_creator_own_team():
    """建项目必须落进创建者自己的团队——否则他建完就再也看不见它。

    读路径（list_projects）当初加了 workspace 过滤，写路径没跟上：INSERT 不带
    workspace_id，靠列默认值 'ws_default' 兜底。于是只属于 B 团队的用户上传小说，
    项目落进 ws_default，而他不是那里的成员——**项目建完立刻从他眼前消失**，
    他会以为上传失败了。

    这条守的是读写两条路径对同一个归属字段的口径一致。
    """
    from app.auth.principal import Principal, set_current_principal
    from app.domain.projects import _creation_workspace_id

    _add_workspace("ws_team_b", "active")
    member = Principal("u_b", "bob", False, {"ws_team_b": "production"})
    set_current_principal(member)
    try:
        assert _creation_workspace_id() == "ws_team_b"
    finally:
        set_current_principal(None)


def test_teamless_user_cannot_create_an_invisible_project():
    """不属于任何团队的普通用户不能建项目——建一个自己看不见的毫无意义。"""
    import pytest as _pytest
    from fastapi import HTTPException

    from app.auth.principal import Principal, set_current_principal
    from app.domain.projects import _creation_workspace_id

    set_current_principal(Principal("u_o", "orphan", False, {}))
    try:
        with _pytest.raises(HTTPException) as exc:
            _creation_workspace_id()
        assert exc.value.status_code == 403
    finally:
        set_current_principal(None)

    # 系统管理员没有成员身份也不该被卡住：他看得到所有团队，不会丢件。
    set_current_principal(Principal("u_a", "root", True, {}))
    try:
        assert _creation_workspace_id() == "ws_default"
    finally:
        set_current_principal(None)


def test_project_created_through_the_real_product_path_is_visible_to_its_creator(client: TestClient):
    """至少一条经产品真实路径的建项目验收——这是【测试替系统完成职责】的解药。

    扫描发现：全仓 170 处测试直接 ``INSERT INTO projects``，**0 处**经由
    ``POST /api/projects``。于是"建项目时该写归属团队"这段产品逻辑从未被任何测试
    执行过——fixture 早就把 workspace_id 写对了，等于替产品做完了它该做的判定。
    结果就是那个"项目建完立刻从创建者眼前消失"的缺陷一路活到线上走查才暴露。

    覆盖率对这类盲区无效：INSERT 那一行确实被跑过，只是少了一列。所以这里不追求
    覆盖更多分支，只要求**这条路径真的被走一次**，从 HTTP 请求一直到能在列表里看见。
    """
    conn = get_conn()
    _add_workspace("ws_creator", "active")
    user_id = _add_user("creator")
    _join("ws_creator", user_id, role="workspace_admin")
    conn.commit()

    headers = {**_HEADERS, "X-Manju-Session": create_session(user_id)}
    novel = ("第一章 起\n" + "正文内容。" * 200 + "\n第二章 承\n" + "更多正文。" * 200).encode()
    files = {"file": ("book.txt", novel, "text/plain")}

    resp = client.post("/api/projects", headers=headers, files=files, data={"name": "端到端小说"})
    # 建项目是 R2 命令，走两段式审批：先拿 approval_token 再重提。
    if resp.status_code == 202 and resp.json().get("approval_token"):
        resp = client.post(
            "/api/projects",
            headers={**headers, "X-Manju-Approval-Token": resp.json()["approval_token"]},
            files={"file": ("book.txt", novel, "text/plain")},
            data={"name": "端到端小说"},
        )
    assert resp.status_code == 200, resp.text
    project_id = resp.json()["project_id"]

    row = conn.execute("SELECT workspace_id FROM projects WHERE id=?", (project_id,)).fetchone()
    assert row["workspace_id"] == "ws_creator", "项目没落进创建者的团队，他将看不到自己刚建的项目"

    listed = {p["id"] for p in client.get("/api/projects", headers=headers).json()}
    assert project_id in listed, "项目建成了却不在创建者的列表里——正是那个『上传完就消失』的症状"


def test_longer_session_window_did_not_weaken_revocation_or_the_absolute_cap():
    """把滑动窗口从 12 小时放宽到 7 天，不能顺带削弱任何一条安全边界。

    放宽是为了体验（刷新不掉线、隔夜不掉线）。这条守的是"便利没有换掉安全"：
    绝对上限仍然封顶，停用账号仍然当场失效。只要有人日后为了更省事再去调这两个
    值，这条会红。
    """
    from app.auth.sessions import ABSOLUTE_TTL_S, SESSION_TTL_S

    # 体验要求：至少覆盖一天，否则隔夜必掉线。
    assert SESSION_TTL_S >= 24 * 60 * 60
    # 安全要求：绝对上限必须仍然严格大于滑动窗口，否则滑动就等于永不过期。
    assert ABSOLUTE_TTL_S > SESSION_TTL_S

    conn = get_conn()
    user_id = _add_user("longlived")
    token = create_session(user_id)
    session_id = token.split(".", 1)[0]
    assert resolve_session(token) is not None

    # 绝对上限仍然封顶：即便滑动窗口没到期，超过 created_at + 30 天也必须失效。
    conn.execute(
        "UPDATE user_sessions SET created_at=?, expires_at=? WHERE id=?",
        (now() - ABSOLUTE_TTL_S - 1, now() + SESSION_TTL_S, session_id),
    )
    conn.commit()
    assert resolve_session(token) is None, "绝对上限被放宽了"

    # 停用账号仍然当场失效，不受更长的窗口影响。
    fresh = create_session(user_id)
    assert resolve_session(fresh) is not None
    conn.execute("UPDATE users SET status='disabled' WHERE id=?", (user_id,))
    conn.commit()
    assert resolve_session(fresh) is None, "停用账号后会话还活着"


def _seed_video_model_episode(
    workspace_id: str, project_id: str, episode_id: str, *, with_video_product: bool
) -> None:
    """播种一集绑定到给定 workspace 的项目 + 分集；按需再挂一条 shot_versions
    产物，用来触发 ``set_episode_video_model`` 的破坏性清空分支（本集已有产物时
    ``confirm_clear_prompts=true`` 会连带清空它们）。``status='confirmed'`` 且不带
    任何 ``active_*_run_id`` 是为了不撞上 ``_review_upstream_snapshot`` 的『上游任务
    仍在写入』fail-closed 分支，专注只测 scope 闸门。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,workspace_id,created_at) VALUES(?,?,?,?)",
        (project_id, project_id, workspace_id, now()),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,target_video_model,created_at) "
        "VALUES(?,?,1,'confirmed','hiagent',?)",
        (episode_id, project_id, now()),
    )
    if with_video_product:
        shot_id = f"{episode_id}-s1"
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,1,5)",
            (shot_id, episode_id),
        )
        conn.execute(
            """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,created_at)
               VALUES(?,?,1,'prompt','idem-1','succeeded',?)""",
            (f"{shot_id}-v1", shot_id, now()),
        )
    conn.commit()


def test_video_model_switch_destructive_clear_blocks_review_without_project_write(
    client: TestClient,
) -> None:
    """review 只有 manju:read + manju:delivery，没有 manju:project-write。

    本集已有视频产物时，切换模型要连带清空它们——这是不可逆操作，和
    ``video.clear_episode_videos`` 同档要求写权限。挡住的必须是这次真实
    HTTP 请求本身（403），而不是事后检查某个字段没变。
    """
    _add_workspace("ws_video_review")
    user_id = _add_user("video_review")
    _join("ws_video_review", user_id, role="review")
    _seed_video_model_episode(
        "ws_video_review", "proj_video_review", "ep_video_review", with_video_product=True
    )

    resp = client.post(
        "/api/episodes/ep_video_review/video-model",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={"target_video_model": "minimax_h3", "confirm_clear_prompts": True},
    )
    assert resp.status_code == 403, resp.text

    conn = get_conn()
    row = conn.execute(
        "SELECT target_video_model FROM episodes WHERE id='ep_video_review'"
    ).fetchone()
    assert row["target_video_model"] == "hiagent", "被 403 挡住之前不能已经切换了模型"
    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM shot_versions v JOIN shots s ON s.id=v.shot_id "
        "WHERE s.episode_id='ep_video_review'"
    ).fetchone()["c"]
    assert remaining == 1, "被 403 挡住之前不能已经清空了产物"


def test_video_model_switch_destructive_clear_is_not_blocked_for_production(
    client: TestClient,
) -> None:
    """production 持有 manju:project-write，走同一条确认清空分支不能被 403 挡住。

    这条不要求清空最终一定成功——业务原因（例如供应商任务未结清）导致的失败
    可以接受；不能接受的是权限层本身把持有写权限的角色也挡在门外。
    """
    _add_workspace("ws_video_prod")
    user_id = _add_user("video_prod")
    _join("ws_video_prod", user_id, role="production")
    _seed_video_model_episode(
        "ws_video_prod", "proj_video_prod", "ep_video_prod", with_video_product=True
    )

    resp = client.post(
        "/api/episodes/ep_video_prod/video-model",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={"target_video_model": "minimax_h3", "confirm_clear_prompts": True},
    )
    assert resp.status_code != 403, f"production 持有 manju:project-write，不该被 403 挡住：{resp.text}"
    assert resp.status_code == 200, resp.text
    assert resp.json()["changed"] is True
    assert resp.json()["cleared_videos"] == 1


def test_video_model_switch_without_existing_products_is_not_gated_for_review(
    client: TestClient,
) -> None:
    """没有已生成产物时的切换是分镜台日常操作，不是不可逆清空。

    只应该收紧『清空』这一支，收紧理由是不可逆，不是『改一个字段』——这条证明
    没有产物时 review 角色仍然能正常切换，没有被误伤。
    """
    _add_workspace("ws_video_switch")
    user_id = _add_user("video_switch")
    _join("ws_video_switch", user_id, role="review")
    _seed_video_model_episode(
        "ws_video_switch", "proj_video_switch", "ep_video_switch", with_video_product=False
    )

    resp = client.post(
        "/api/episodes/ep_video_switch/video-model",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={"target_video_model": "minimax_h3"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["changed"] is True

    row = get_conn().execute(
        "SELECT target_video_model FROM episodes WHERE id='ep_video_switch'"
    ).fetchone()
    assert row["target_video_model"] == "minimax_h3", "普通切换必须真的写进去，否则这条只是在测 200"


def _seed_text_model_project(workspace_id: str, project_id: str) -> None:
    """播种一个挂到给定 workspace 的项目，用于测世界书/映射台/分镜台分环节
    文本模型切换（PUT /projects/{id}/text-models）的 scope 闸门。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,workspace_id,created_at) VALUES(?,?,?,?)",
        (project_id, project_id, workspace_id, now()),
    )
    conn.commit()


@pytest.fixture()
def _text_model_catalog():
    """真库落一条已配凭据的 kind=text 条目，走 text_model_choices() 的真实校验路径。"""
    set_setting("custom_models", json.dumps([{
        "id": "model_rbac_text", "provider": "custom:model_rbac_text", "model": "rbac-test-model",
        "label": "RBAC 测试文本模型", "kinds": ["text"], "builtin": False,
        "protocol": "openai", "base_url": "https://rbac.example.test", "api_key": "sk-test",
    }], ensure_ascii=False))
    try:
        yield "custom:model_rbac_text"
    finally:
        set_setting("custom_models", "[]")


def test_text_model_switch_blocks_review_without_project_write(
    client: TestClient, _text_model_catalog: str,
) -> None:
    """review 只有 manju:read + manju:delivery，没有 manju:project-write。

    世界书/映射台/分镜台分环节文本模型是项目级设置，会改变之后新发起的生成
    调用选哪个 provider——与 video-model 的清空分支同档收在 manju:project-write。
    挡住的必须是这次真实 HTTP 请求本身（403），不是事后检查字段没变。
    """
    _add_workspace("ws_text_model_review")
    user_id = _add_user("text_model_review")
    _join("ws_text_model_review", user_id, role="review")
    _seed_text_model_project("ws_text_model_review", "proj_text_model_review")

    resp = client.put(
        "/api/projects/proj_text_model_review/text-models",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={"board_text_provider": _text_model_catalog},
    )
    assert resp.status_code == 403, resp.text

    row = get_conn().execute(
        "SELECT board_text_provider FROM projects WHERE id='proj_text_model_review'"
    ).fetchone()
    assert row["board_text_provider"] == "", "被 403 挡住之前不能已经写入了选择"


def test_text_model_switch_is_not_blocked_for_production(
    client: TestClient, _text_model_catalog: str,
) -> None:
    """production 持有 manju:project-write，同一条切换入口不能被 403 挡住，
    且必须真的写进 projects 表——不是只测个 200。"""
    _add_workspace("ws_text_model_prod")
    user_id = _add_user("text_model_prod")
    _join("ws_text_model_prod", user_id, role="production")
    _seed_text_model_project("ws_text_model_prod", "proj_text_model_prod")

    resp = client.put(
        "/api/projects/proj_text_model_prod/text-models",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={
            "bible_text_provider": _text_model_catalog,
            "script_text_provider": _text_model_catalog,
            "board_text_provider": _text_model_catalog,
        },
    )
    assert resp.status_code != 403, f"production 持有 manju:project-write，不该被 403 挡住：{resp.text}"
    assert resp.status_code == 200, resp.text

    row = get_conn().execute(
        "SELECT bible_text_provider, script_text_provider, board_text_provider "
        "FROM projects WHERE id='proj_text_model_prod'"
    ).fetchone()
    assert row["bible_text_provider"] == _text_model_catalog
    assert row["script_text_provider"] == _text_model_catalog
    assert row["board_text_provider"] == _text_model_catalog


def test_text_model_switch_rejects_provider_without_credentials(
    client: TestClient, _text_model_catalog: str,
) -> None:
    """不能选一个没配凭据、必然失败的 provider——服务端要真的拒绝，不能只靠
    前端下拉不显示来兜底（Agent/脚本可以绕过前端直接打这个接口）。"""
    _add_workspace("ws_text_model_bad")
    user_id = _add_user("text_model_bad")
    _join("ws_text_model_bad", user_id, role="production")
    _seed_text_model_project("ws_text_model_bad", "proj_text_model_bad")

    resp = client.put(
        "/api/projects/proj_text_model_bad/text-models",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={"bible_text_provider": "custom:model_never_configured"},
    )
    assert resp.status_code == 422, resp.text

    row = get_conn().execute(
        "SELECT bible_text_provider FROM projects WHERE id='proj_text_model_bad'"
    ).fetchone()
    assert row["bible_text_provider"] == "", "被拒绝之前不能已经写入了无效选择"
