"""Shared JWT/IdP builders for EP-02 SSO tests (``test_sso_oidc_flow.py`` /
``test_sso_provisioning.py``). Not a ``test_`` file itself -- pure
fixture/builder helpers, same convention as ``tests/rbac_isolation_helpers.py``
(kept out of both test files purely to stay under the 500-line new-file cap
without duplicating ~100 lines of RSA/JWT plumbing twice).
"""
from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.sso import store as sso_store

HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}

DEFAULT_CLAIM_MAP = {
    "subject": "sub", "username": "preferred_username", "display_name": "name",
    "email": "email", "dept": "department", "groups": "groups",
}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_uint(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return b64url(value.to_bytes(length, "big"))


def generate_rsa_keypair(kid: str = "test-key-1"):
    """返回 ``(private_key, jwk_dict)``：私钥用于测试自己签发 id_token，
    ``jwk_dict`` 模拟 IdP 会发布到 JWKS 端点的公钥条目。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig",
        "n": b64url_uint(numbers.n), "e": b64url_uint(numbers.e),
    }
    return private_key, jwk


def sign_id_token(private_key, header: dict, payload: dict) -> str:
    header_b64 = b64url(json.dumps(header).encode("utf-8"))
    payload_b64 = b64url(json.dumps(payload).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{b64url(signature)}"


def default_id_token_payload(*, issuer: str, client_id: str, subject: str, nonce: str, **overrides) -> dict:
    ts = time.time()
    payload = {"iss": issuer, "aud": client_id, "sub": subject, "nonce": nonce, "iat": ts, "exp": ts + 300}
    payload.update(overrides)
    return payload


def create_oidc_idp(conn, *, issuer: str, client_id: str, provision: dict | None = None, org_id=None) -> str:
    idp_id = sso_store.create_idp(
        conn, org_id=org_id, kind="oidc", name=f"test-oidc-{issuer}",
        issuer=issuer, client_id=client_id, client_secret_plain="s3cr3t",
        discovery_url=None, authorize_url="https://idp.example.com/authorize",
        token_url="https://idp.example.com/token", userinfo_url="https://idp.example.com/userinfo",
        jwks_url="https://idp.example.com/jwks", scopes="openid profile email",
        claim_map_json=json.dumps(DEFAULT_CLAIM_MAP),
        provision_json=json.dumps(provision or {"auto_create": True, "on_no_match": "default"}),
        allowed_domains=None, enabled=True, created_by="test",
    )
    conn.commit()
    return idp_id


def create_wecom_idp(conn, *, provision: dict | None = None) -> str:
    claim_map = {**DEFAULT_CLAIM_MAP, "subject": "userid", "username": "userid"}
    idp_id = sso_store.create_idp(
        conn, org_id=None, kind="wecom", name="test-wecom", issuer=None,
        client_id="wecom-corp-id", client_secret_plain="wecom-secret",
        discovery_url=None, authorize_url="https://open.weixin.qq.com/connect/oauth2/authorize",
        token_url="https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        userinfo_url="https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo",
        jwks_url=None, scopes="snsapi_base", claim_map_json=json.dumps(claim_map),
        provision_json=json.dumps(provision or {"auto_create": True, "on_no_match": "default"}),
        allowed_domains=None, enabled=True, created_by="test",
    )
    conn.commit()
    return idp_id


def start_login(client, idp_id: str, *, redirect_to: str = "/dashboard", headers: dict | None = None):
    resp = client.get(
        f"/api/auth/sso/{idp_id}/start", params={"redirect_to": redirect_to},
        headers=headers or HEADERS, follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    query = parse_qs(urlparse(location).query)
    return query["state"][0], location, query


def read_auth_request(conn, state: str) -> dict:
    """不消费地读一行 ``sso_auth_requests``（测试专用：真实流程里这一步的
    nonce/code_verifier 是身份提供方原样回填的，这里直接读库是因为我们在
    测试里同时扮演"发起方"与"假 IdP"两个角色）。"""
    row = conn.execute("SELECT * FROM sso_auth_requests WHERE state=?", (state,)).fetchone()
    assert row is not None, f"sso_auth_requests 里找不到 state={state!r}"
    return dict(row)
