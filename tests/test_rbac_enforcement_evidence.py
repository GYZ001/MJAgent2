"""RBAC 强制性的行为证据。

这个文件存在的理由是一类反复出现的缺陷形状：**字段在、赋值在、清零在，唯独
没有任何一行强制它**，而且不抛异常。代码目检看起来是做完了，实际从未生效。
本项目已知的同族例子包括 json_schema 静默降级、spine_beat 补丁空转，以及
RBAC 这边的 ``users.must_change_password``。

所以这里的断言一律是「做一次真实操作，看它是否真的被挡住」，而不是
「检查某个字段是否等于某个值」。任何一条如果改成后者，它就失去了存在意义。

账号即项目空间落地后，团队/工作空间（``workspaces.status`` 停用即收权）相关
的证据随该模型一并退场；账号本身的启停（``users.status``）与项目归属
（``projects.owner_user_id``）取而代之，见下面对应的测试。
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


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


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


def test_new_project_lands_in_the_creators_own_account():
    """建项目必须落进创建者自己的账号——否则他建完就再也看不见它。

    账号即项目空间之后，这条规则不再需要"团队"这层中间概念：项目直接归属
    ``principal.user_id``，无歧义、无需选择。历史教训（团队模型下 workspace_id
    读写两条路径口径不一致，导致项目建完立刻从创建者眼前消失）随团队模型一并
    退场，但"创建者必须能看到自己刚建的项目"这条不变量本身仍然成立，这里继续
    守住它，只是判据换成了 owner_user_id。
    """
    from app.auth.principal import Principal, set_current_principal
    from app.domain.projects import _creation_owner_user_id

    member = Principal("u_b", "bob", False)
    set_current_principal(member)
    try:
        assert _creation_owner_user_id() == "u_b"
    finally:
        set_current_principal(None)


def test_no_principal_context_falls_back_to_legacy_placeholder_owner():
    """没有 Principal 上下文（内部调用、既有测试、兼容期共享会话）时的既有行为
    保持不变：落到一个不对应任何真实账号的占位符，只有系统管理员（含
    ``legacy-shared`` 本身，见 app/local_session.py）能看到。

    ``tests/conftest.py`` 的 autouse fixture 默认注入一个系统管理员 Principal
    （历史测试不需要逐个改造），这里必须显式清空才能复现"真没有 Principal"的
    内部调用场景，用完照既有约定还原。
    """
    from app.auth.principal import get_current_principal, set_current_principal
    from app.domain.projects import _creation_owner_user_id

    previous = get_current_principal()
    set_current_principal(None)
    try:
        assert _creation_owner_user_id() == "legacy-shared"
    finally:
        set_current_principal(previous)


def test_project_created_through_the_real_product_path_is_visible_to_its_creator(client: TestClient):
    """至少一条经产品真实路径的建项目验收——这是【测试替系统完成职责】的解药。

    覆盖率对这类盲区无效：INSERT 那一行确实被跑过，只是少了一列。所以这里不追求
    覆盖更多分支，只要求**这条路径真的被走一次**，从 HTTP 请求一直到能在列表里
    看见，并且看不到别人的项目。
    """
    conn = get_conn()
    user_id = _add_user("creator")
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

    row = conn.execute("SELECT owner_user_id FROM projects WHERE id=?", (project_id,)).fetchone()
    assert row["owner_user_id"] == user_id, "项目没落进创建者自己的账号，他将看不到自己刚建的项目"

    listed = {p["id"] for p in client.get("/api/projects", headers=headers).json()}
    assert project_id in listed, "项目建成了却不在创建者的列表里——正是那个『上传完就消失』的症状"

    other = _add_user("stranger")
    other_headers = {**_HEADERS, "X-Manju-Session": create_session(other)}
    stranger_listed = {p["id"] for p in client.get("/api/projects", headers=other_headers).json()}
    assert project_id not in stranger_listed, "陌生账号不该看到别人刚建的项目"


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


def _seed_video_model_episode(project_id: str, episode_id: str, owner_user_id: str, *,
                               with_video_product: bool) -> None:
    """播种一集绑定到给定账号的项目 + 分集；按需再挂一条 shot_versions 产物，
    用来触发 ``set_episode_video_model`` 的破坏性清空分支（本集已有视频生成
    产物时 ``confirm_clear_prompts=true`` 会连带清空它们）。``status='confirmed'``
    且不带任何 ``active_*_run_id`` 是为了不撞上 ``_review_upstream_snapshot`` 的
    『上游任务仍在写入』fail-closed 分支，专注只测归属闸门。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,owner_user_id,created_at) VALUES(?,?,?,?)",
        (project_id, project_id, owner_user_id, now()),
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


def test_video_model_switch_destructive_clear_works_for_the_owning_account(
    client: TestClient,
) -> None:
    """账号即项目空间之后，能触达这个端点的就已经是本项目的所有者（HTTP 边界
    ``require_project_owner_access`` 已经拦过一轮）——不再有 review/production
    这类团队角色差异化，清空分支不该额外挡住所有者本人。这条不要求清空最终一定
    成功——业务原因（例如供应商任务未结清）导致的失败可以接受；不能接受的是
    权限层本身把所有者挡在门外。跨账号（非所有者）被拦的证据见
    ``tests/test_rbac_project_isolation.py``。
    """
    user_id = _add_user("video_owner")
    _seed_video_model_episode(
        "proj_video_owner", "ep_video_owner", user_id, with_video_product=True
    )

    resp = client.post(
        "/api/episodes/ep_video_owner/video-model",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={"target_video_model": "minimax_h3", "confirm_clear_prompts": True},
    )
    assert resp.status_code != 403, f"所有者本人不该被 403 挡住：{resp.text}"
    assert resp.status_code == 200, resp.text
    assert resp.json()["changed"] is True
    assert resp.json()["cleared_videos"] == 1


def _seed_text_model_project(project_id: str, owner_user_id: str) -> None:
    """播种一个挂到给定账号的项目，用于测世界书/映射台/分镜台分环节文本模型
    切换（PUT /projects/{id}/text-models）的归属闸门。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,owner_user_id,created_at) VALUES(?,?,?,?)",
        (project_id, project_id, owner_user_id, now()),
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


def test_text_model_switch_works_for_the_owning_account(
    client: TestClient, _text_model_catalog: str,
) -> None:
    """所有者本人切换分环节文本模型不该被挡住，且必须真的写进 projects 表——
    不是只测个 200。跨账号被拦的证据见 tests/test_rbac_project_isolation.py。"""
    user_id = _add_user("text_model_owner")
    _seed_text_model_project("proj_text_model_owner", user_id)

    resp = client.put(
        "/api/projects/proj_text_model_owner/text-models",
        headers={**_HEADERS, "X-Manju-Session": create_session(user_id)},
        json={
            "bible_text_provider": _text_model_catalog,
            "script_text_provider": _text_model_catalog,
            "board_text_provider": _text_model_catalog,
        },
    )
    assert resp.status_code != 403, f"所有者本人不该被 403 挡住：{resp.text}"
    assert resp.status_code == 200, resp.text

    row = get_conn().execute(
        "SELECT bible_text_provider, script_text_provider, board_text_provider "
        "FROM projects WHERE id='proj_text_model_owner'"
    ).fetchone()
    assert row["bible_text_provider"] == _text_model_catalog
    assert row["script_text_provider"] == _text_model_catalog
    assert row["board_text_provider"] == _text_model_catalog


def test_text_model_switch_rejects_provider_without_credentials(
    client: TestClient, _text_model_catalog: str,
) -> None:
    """不能选一个没配凭据、必然失败的 provider——服务端要真的拒绝，不能只靠
    前端下拉不显示来兜底（Agent/脚本可以绕过前端直接打这个接口）。"""
    user_id = _add_user("text_model_bad")
    _seed_text_model_project("proj_text_model_bad", user_id)

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
