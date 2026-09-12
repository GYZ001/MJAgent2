"""EP-02 第一阶段：OIDC Authorization Code + PKCE 全流程行为断言。

一律"做一次真实操作，看它是否真被挡住/真被放行"（CLAUDE.md）。网络边界
（token 端点交换、JWKS 拉取）用 ``patch_sso_everywhere`` 打桩——这是唯一
被打桩的部分；id_token 的六项校验清单（``app.sso.oidc_verify``）与
``app.sso.provision`` 的开户/映射逻辑全部走真实代码路径，用测试自己生成的
RSA 密钥签发/篡改 id_token 来证伪。

安全类断言全部是证伪式的：篡改签名/改 aud/改 nonce/过期 exp/重放 state/
重放交换码/过期交换码各一条，断言全部被拒（401/400）；另有一条回归闸门
断言 302 的 Location 里不含真会话令牌（2026-09-12 修复：会话交接曾经把真
令牌塞进查询串，会明文落进 nginx access log/浏览器历史/Referer）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import patch_sso_everywhere
from tests.sso_test_helpers import (
    HEADERS,
    create_oidc_idp,
    create_wecom_idp,
    default_id_token_payload,
    generate_rsa_keypair,
    read_auth_request,
    sign_id_token,
    start_login,
)

ISSUER = "https://idp.example.com/"
CLIENT_ID = "test-client-id"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _patch_token_exchange(monkeypatch, *, id_token: str | None = None, access_token: str = "fake-access-token") -> None:
    async def fake_exchange_code(endpoints, **kwargs):
        resp = {"access_token": access_token}
        if id_token is not None:
            resp["id_token"] = id_token
        return resp

    patch_sso_everywhere(monkeypatch, "exchange_code", fake_exchange_code)


def _patch_jwks(monkeypatch, jwk: dict) -> None:
    async def fake_fetch_jwks(jwks_url, *, force: bool = False):
        return [jwk]

    patch_sso_everywhere(monkeypatch, "_fetch_jwks", fake_fetch_jwks)


def _begin_and_sign(conn, client, idp_id, private_key, *, subject="ext-sub-1", payload_overrides=None):
    state, _location, query = start_login(client, idp_id)
    auth_req = read_auth_request(conn, state)
    payload = default_id_token_payload(
        issuer=ISSUER, client_id=CLIENT_ID, subject=subject, nonce=auth_req["nonce"],
        **(payload_overrides or {}),
    )
    id_token = sign_id_token(private_key, {"alg": "RS256", "kid": "test-key-1", "typ": "JWT"}, payload)
    return state, id_token


def _callback(client, idp_id, state, code="fake-code"):
    return client.get(
        f"/api/auth/sso/{idp_id}/callback", params={"code": code, "state": state},
        headers=HEADERS, follow_redirects=False,
    )


def _sso_code_from_location(location: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(location).query)["sso_code"][0]


def _exchange(client, code: str):
    return client.post("/api/auth/sso/exchange", json={"code": code}, headers=HEADERS)


# ---------------------------------------------------------------------------
# 正常流：start -> callback -> 会话签发 -> /api/auth/me；user_identities 落一行
# ---------------------------------------------------------------------------


def test_full_login_flow_creates_session_and_identity(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-full-flow")
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith("/dashboard?")
    assert "session_token" not in location  # 会话交接不走查询串，见 app/sso/api.py 模块文档

    sso_code = _sso_code_from_location(location)
    exchanged = _exchange(client, sso_code)
    assert exchanged.status_code == 200, exchanged.text
    token = exchanged.json()["session_token"]

    me = client.get("/api/auth/me", headers={**HEADERS, "X-Manju-Session": token})
    assert me.status_code == 200, me.text

    row = conn.execute(
        "SELECT user_id FROM user_identities WHERE idp_id=? AND external_subject=?",
        (idp_id, "ext-sub-full-flow"),
    ).fetchone()
    assert row is not None
    assert row["user_id"] == me.json()["user"]["id"]

    audit_events = {
        r["event"] for r in conn.execute(
            "SELECT event FROM operation_audit WHERE target=?", ("ext-sub-full-flow",)
        ).fetchall()
    }
    assert "sso.login_success" in audit_events
    assert "sso.provision_created" in audit_events


def test_second_login_reuses_same_account(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    _patch_jwks(monkeypatch, jwk)

    state1, id_token1 = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-repeat")
    _patch_token_exchange(monkeypatch, id_token=id_token1)
    resp1 = _callback(client, idp_id, state1)
    user_count_after_first = conn.execute(
        "SELECT COUNT(*) c FROM user_identities WHERE idp_id=? AND external_subject=?",
        (idp_id, "ext-sub-repeat"),
    ).fetchone()["c"]
    assert user_count_after_first == 1

    state2, id_token2 = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-repeat")
    _patch_token_exchange(monkeypatch, id_token=id_token2)
    resp2 = _callback(client, idp_id, state2)
    assert resp1.status_code == resp2.status_code == 302

    total_identities = conn.execute(
        "SELECT COUNT(*) c FROM user_identities WHERE idp_id=? AND external_subject=?",
        (idp_id, "ext-sub-repeat"),
    ).fetchone()["c"]
    assert total_identities == 1  # 没有重复开户


# ---------------------------------------------------------------------------
# 证伪式安全断言：state 重放 / 签名篡改 / aud 篡改 / nonce 篡改 / exp 过期
# ---------------------------------------------------------------------------


def test_replayed_state_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-replay")
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    first = _callback(client, idp_id, state)
    assert first.status_code == 302

    second = _callback(client, idp_id, state)
    assert second.status_code == 400, second.text


def test_tampered_signature_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    wrong_key, _wrong_jwk = generate_rsa_keypair(kid="test-key-1")  # 同 kid，但公钥不匹配
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, _valid_token = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-sig")
    auth_req = read_auth_request(conn, state)
    tampered_payload = default_id_token_payload(
        issuer=ISSUER, client_id=CLIENT_ID, subject="ext-sub-sig", nonce=auth_req["nonce"],
    )
    tampered_token = sign_id_token(wrong_key, {"alg": "RS256", "kid": "test-key-1", "typ": "JWT"}, tampered_payload)
    _patch_token_exchange(monkeypatch, id_token=tampered_token)
    _patch_jwks(monkeypatch, jwk)  # JWKS 端点仍发布"正版"公钥

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 401, resp.text
    assert "签名" in resp.json()["detail"]


def test_tampered_aud_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(
        conn, client, idp_id, private_key, subject="ext-sub-aud",
        payload_overrides={"aud": "someone-elses-client-id"},
    )
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 401, resp.text
    assert "aud" in resp.json()["detail"]


def test_tampered_nonce_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, _location, _query = start_login(client, idp_id)
    payload = default_id_token_payload(
        issuer=ISSUER, client_id=CLIENT_ID, subject="ext-sub-nonce", nonce="attacker-supplied-nonce",
    )
    id_token = sign_id_token(private_key, {"alg": "RS256", "kid": "test-key-1", "typ": "JWT"}, payload)
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 401, resp.text
    assert "nonce" in resp.json()["detail"]


def test_expired_exp_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(
        conn, client, idp_id, private_key, subject="ext-sub-expired",
        payload_overrides={"exp": time.time() - 1000, "iat": time.time() - 1300},
    )
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 401, resp.text
    assert "过期" in resp.json()["detail"]


def test_missing_sub_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, _location, _query = start_login(client, idp_id)
    auth_req = read_auth_request(conn, state)
    payload = default_id_token_payload(
        issuer=ISSUER, client_id=CLIENT_ID, subject="ignored", nonce=auth_req["nonce"],
    )
    del payload["sub"]
    id_token = sign_id_token(private_key, {"alg": "RS256", "kid": "test-key-1", "typ": "JWT"}, payload)
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 401, resp.text
    assert "sub" in resp.json()["detail"]


def test_wrong_issuer_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(
        conn, client, idp_id, private_key, subject="ext-sub-iss",
        payload_overrides={"iss": "https://attacker.example.com/"},
    )
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 401, resp.text
    assert "iss" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 会话交接一次性交换码（2026-09-12 修复：不能把真会话令牌塞进 302 查询串，
# 否则会明文落进 nginx access log/浏览器历史/Referer）：交换码重放/过期
# 均被拒；回归闸门断言 Location 里不含任何看起来像真会话令牌的值。
# ---------------------------------------------------------------------------


def test_exchange_code_replay_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-exchange-replay")
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    sso_code = _sso_code_from_location(resp.headers["location"])

    first = _exchange(client, sso_code)
    assert first.status_code == 200, first.text
    second = _exchange(client, sso_code)
    assert second.status_code == 400, second.text


def test_exchange_code_expired_is_rejected(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-exchange-expired")
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    sso_code = _sso_code_from_location(resp.headers["location"])

    import hashlib

    code_hash = hashlib.sha256(sso_code.encode("utf-8")).hexdigest()
    conn.execute("UPDATE sso_login_exchanges SET expires_at = expires_at - 3600 WHERE code_hash=?", (code_hash,))
    conn.commit()

    expired = _exchange(client, sso_code)
    assert expired.status_code == 400, expired.text


def test_unknown_exchange_code_is_rejected(client: TestClient) -> None:
    resp = _exchange(client, "this-code-was-never-issued")
    assert resp.status_code == 400, resp.text


def test_callback_redirect_never_contains_real_session_token(client: TestClient, monkeypatch) -> None:
    """回归闸门：防止将来有人图省事又把真会话令牌塞回 302 查询串。断言
    Location 里除了一次性交换码之外，取不出任何能直接当 X-Manju-Session
    用的凭证——用交换码本身去 /api/auth/me 必须 401（证明它不是会话令牌，
    必须先经 /api/auth/sso/exchange 才能换成一个）。
    """
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-no-leak")
    _patch_token_exchange(monkeypatch, id_token=id_token)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    location = resp.headers["location"]
    assert "session_token" not in location
    assert "X-Manju-Session" not in location

    sso_code = _sso_code_from_location(location)
    not_a_session = client.get("/api/auth/me", headers={**HEADERS, "X-Manju-Session": sso_code})
    assert not_a_session.status_code == 401, not_a_session.text


# ---------------------------------------------------------------------------
# PKCE：结构性验证——生成侧 verifier/challenge 的 S256 关系、传输侧
# code_verifier 与 authorize 阶段一致地被带进 token 交换请求。
# （真正的"code_verifier 不匹配 -> 拒绝"由 IdP 侧执行，本地没有真实 IdP 无法
# 做拒绝式证伪，见交付报告"未完成项"。）
# ---------------------------------------------------------------------------


def test_pkce_challenge_matches_verifier_with_s256(client: TestClient) -> None:
    import base64
    import hashlib

    conn = get_conn()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, _location, query = start_login(client, idp_id)
    auth_req = read_auth_request(conn, state)

    assert query["code_challenge_method"][0] == "S256"
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(auth_req["code_verifier"].encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert query["code_challenge"][0] == expected


def test_token_exchange_receives_the_same_code_verifier(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    idp_id = create_oidc_idp(conn, issuer=ISSUER, client_id=CLIENT_ID)
    state, id_token = _begin_and_sign(conn, client, idp_id, private_key, subject="ext-sub-pkce")
    auth_req = read_auth_request(conn, state)

    captured: dict = {}

    async def capturing_exchange_code(endpoints, **kwargs):
        captured.update(kwargs)
        return {"id_token": id_token, "access_token": "fake-access-token"}

    patch_sso_everywhere(monkeypatch, "exchange_code", capturing_exchange_code)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 302, resp.text
    assert captured["code_verifier"] == auth_req["code_verifier"]


# ---------------------------------------------------------------------------
# 国内 IdP profile：企业微信（纯 OAuth2 + userinfo，无 id_token）走同一条
# callback 代码路径；新增一家走既有协议族的 IdP 不需要新分支——见
# app/sso/profiles.py 与 app/sso/oidc.py 的分支闭集只按 kind 取 profile 数据，
# 没有针对具体租户/客户的 if。
# ---------------------------------------------------------------------------


def test_wecom_profile_uses_userinfo_not_id_token(client: TestClient, monkeypatch) -> None:
    conn = get_conn()
    idp_id = create_wecom_idp(conn)
    state, _location, query = start_login(client, idp_id)
    assert "nonce" not in query  # wecom 无 id_token，不需要 nonce 参与 authorize URL

    async def fake_exchange_code(endpoints, **kwargs):
        return {"access_token": "wecom-access-token"}

    async def fake_fetch_userinfo(idp_row, endpoints, access_token):
        assert access_token == "wecom-access-token"
        return {"userid": "wecom-user-1", "name": "王小明"}

    patch_sso_everywhere(monkeypatch, "exchange_code", fake_exchange_code)
    patch_sso_everywhere(monkeypatch, "fetch_userinfo", fake_fetch_userinfo)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 302, resp.text
    row = conn.execute(
        "SELECT user_id FROM user_identities WHERE idp_id=? AND external_subject=?",
        (idp_id, "wecom-user-1"),
    ).fetchone()
    assert row is not None


def test_new_idp_of_existing_kind_needs_no_new_branch(client: TestClient, monkeypatch) -> None:
    """新增一家 IdP（哪怕是完全不同的租户/发行方）只插入一行配置，走的是与
    ``test_full_login_flow_creates_session_and_identity`` 完全相同的
    ``app.sso.api``/``app.sso.oidc``/``app.sso.provision`` 代码路径——本测试
    与之共用同一套 helper 函数，唯一不同的只有 issuer/client_id 两个配置值。
    """
    conn = get_conn()
    private_key, jwk = generate_rsa_keypair()
    other_issuer = "https://another-tenant.okta.com/"
    other_client_id = "another-client-id"
    idp_id = create_oidc_idp(conn, issuer=other_issuer, client_id=other_client_id)
    state, _location, query = start_login(client, idp_id)
    auth_req = read_auth_request(conn, state)
    payload = default_id_token_payload(
        issuer=other_issuer, client_id=other_client_id, subject="ext-sub-other-tenant", nonce=auth_req["nonce"],
    )
    id_token = sign_id_token(private_key, {"alg": "RS256", "kid": "test-key-1", "typ": "JWT"}, payload)

    async def fake_exchange_code(endpoints, **kwargs):
        return {"id_token": id_token, "access_token": "fake"}

    patch_sso_everywhere(monkeypatch, "exchange_code", fake_exchange_code)
    _patch_jwks(monkeypatch, jwk)

    resp = _callback(client, idp_id, state)
    assert resp.status_code == 302, resp.text
