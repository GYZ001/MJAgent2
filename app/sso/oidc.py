"""EP-02 OIDC/OAuth2 出网适配：discovery、PKCE、token 交换、userinfo、JWKS
缓存、连通性自检（L3，见 app/LAYERS.toml::app.sso.oidc）。

出网一律走 ``httpx``（PRD 明确禁止裸 ``requests``），超时按"典型 JSON 端点
往返基线 1-2s 的 5-8 倍"设置——本环境没有真实企业 IdP 可实测基线，取行业内
常见的保守值（连接 5s / 读 10s），比模型调用（10s 起）更紧是因为这些端点
只是简单的 JSON 交换，不涉及长时间生成；生产环境如果实测某家 IdP 显著更慢，
应调整这里的常量，不应绕过超时本身。

**绝对禁止**（PRD EP-02 §7）：跳过签名校验、只信 userinfo 端点判定 OIDC 身份、
``verify=False``。本模块所有 ``httpx`` 调用都使用 httpx 默认的 TLS 校验
（从不传 ``verify=False``），签名校验委托给 ``app.sso.oidc_verify``。
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

from app.sso import oidc_verify
from app.sso.profiles import profile_for

_ENDPOINT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
JWKS_CACHE_TTL_S = 600.0
DISCOVERY_CACHE_TTL_S = 3600.0

_jwks_cache: dict[str, tuple[float, list[dict]]] = {}
_discovery_cache: dict[str, tuple[float, dict]] = {}


class OidcTransportError(RuntimeError):
    """discovery/token/userinfo/JWKS 网络调用失败（超时、连接失败、非 2xx）。"""


def generate_pkce() -> tuple[str, str]:
    """S256 PKCE：返回 ``(code_verifier, code_challenge)``。"""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def _fetch_discovery(discovery_url: str) -> dict:
    cached = _discovery_cache.get(discovery_url)
    if cached and time.time() - cached[0] < DISCOVERY_CACHE_TTL_S:
        return cached[1]
    async with httpx.AsyncClient(timeout=_ENDPOINT_TIMEOUT) as client:
        try:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            doc = resp.json()
        except httpx.HTTPError as exc:
            raise OidcTransportError(f"OIDC discovery 拉取失败（{discovery_url}）：{exc}") from exc
    _discovery_cache[discovery_url] = (time.time(), doc)
    return doc


async def resolve_endpoints(idp_row: dict) -> dict:
    """discovery_url 补全缺失的 authorize/token/userinfo/jwks 端点；显式配置
    的字段始终优先，discovery 只填缺口。"""
    endpoints = {
        "authorize_url": idp_row.get("authorize_url") or "",
        "token_url": idp_row.get("token_url") or "",
        "userinfo_url": idp_row.get("userinfo_url") or "",
        "jwks_url": idp_row.get("jwks_url") or "",
    }
    discovery_url = (idp_row.get("discovery_url") or "").strip()
    if not discovery_url or all(endpoints.values()):
        return endpoints
    doc = await _fetch_discovery(discovery_url)
    doc_keys = {
        "authorize_url": "authorization_endpoint", "token_url": "token_endpoint",
        "userinfo_url": "userinfo_endpoint", "jwks_url": "jwks_uri",
    }
    for key, doc_key in doc_keys.items():
        if not endpoints[key]:
            endpoints[key] = str(doc.get(doc_key) or "")
    return endpoints


def build_authorize_url(
    idp_row: dict, endpoints: dict, *, redirect_uri: str, state: str, nonce: str, code_challenge: str,
) -> str:
    profile = profile_for(idp_row["kind"])
    params = {
        "client_id": idp_row["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": idp_row.get("scopes") or profile.default_scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if profile.requires_id_token:
        params["nonce"] = nonce
    return f"{endpoints['authorize_url']}?{urlencode(params)}"


async def exchange_code(
    endpoints: dict, *, client_id: str, client_secret: str | None, code: str,
    redirect_uri: str, code_verifier: str,
) -> dict:
    payload = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=_ENDPOINT_TIMEOUT) as client:
        try:
            resp = await client.post(endpoints["token_url"], data=payload, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise OidcTransportError(f"token 端点交换失败（{endpoints['token_url']}）：{exc}") from exc


async def fetch_userinfo(idp_row: dict, endpoints: dict, access_token: str) -> dict:
    profile = profile_for(idp_row["kind"])
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if profile.userinfo_token_location == "header":
        header_value = f"Bearer {access_token}" if profile.userinfo_token_param == "Authorization" else access_token
        headers[profile.userinfo_token_param] = header_value
    else:
        params[profile.userinfo_token_param] = access_token
    async with httpx.AsyncClient(timeout=_ENDPOINT_TIMEOUT) as client:
        try:
            resp = await client.get(endpoints["userinfo_url"], params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise OidcTransportError(f"userinfo 端点调用失败（{endpoints['userinfo_url']}）：{exc}") from exc


async def _fetch_jwks(jwks_url: str, *, force: bool = False) -> list[dict]:
    cached = _jwks_cache.get(jwks_url)
    if not force and cached and time.time() - cached[0] < JWKS_CACHE_TTL_S:
        return cached[1]
    async with httpx.AsyncClient(timeout=_ENDPOINT_TIMEOUT) as client:
        try:
            resp = await client.get(jwks_url)
            resp.raise_for_status()
            keys = resp.json().get("keys") or []
        except httpx.HTTPError as exc:
            raise OidcTransportError(f"JWKS 拉取失败（{jwks_url}）：{exc}") from exc
    _jwks_cache[jwks_url] = (time.time(), keys)
    return keys


async def _jwk_for_kid(jwks_url: str, kid: str | None) -> dict:
    keys = await _fetch_jwks(jwks_url)
    for key in keys:
        if kid is None or key.get("kid") == kid:
            return key
    # 命中不了缓存：可能是密钥刚轮换，强制刷新一次再试，避免长期缓存卡死轮转。
    keys = await _fetch_jwks(jwks_url, force=True)
    for key in keys:
        if kid is None or key.get("kid") == kid:
            return key
    raise oidc_verify.IdTokenError(f"JWKS（{jwks_url}）中找不到 kid={kid!r} 对应的公钥")


async def verify_id_token(id_token: str, *, idp_row: dict, endpoints: dict, nonce: str) -> dict:
    """id_token 六项校验清单的完整编排：先验签名，再验其余五项声明。"""
    header, payload, signing_input, signature = oidc_verify.decode_jwt_parts(id_token)
    alg = header.get("alg")
    jwk = await _jwk_for_kid(endpoints["jwks_url"], header.get("kid"))
    oidc_verify.verify_signature(alg, jwk, signing_input, signature)
    return oidc_verify.verify_claims(
        payload, issuer=idp_row["issuer"] or "", client_id=idp_row["client_id"], nonce=nonce,
    )


async def check_connectivity(idp_row: dict) -> tuple[bool, str]:
    """SSO 可用性自检：真实走一遍 discovery（如配置）+ token 端点连通，用于
    切换 ``local_login_policy=disabled`` 前的强制闸门（PRD EP-02 §6）。

    不做真实的授权码交换（没有真实用户 code 可用）——"连通"的判据是"能建立
    TCP/TLS 连接并收到一个 HTTP 响应"，哪怕响应是 4xx（说明请求到达了服务端、
    只是参数不对）；只有连接类异常（DNS 失败/超时/TLS 失败）才判定为不通。
    """
    try:
        endpoints = await resolve_endpoints(idp_row)
    except OidcTransportError as exc:
        return False, str(exc)
    if not endpoints["authorize_url"] or not endpoints["token_url"]:
        return False, "IdP 配置不完整：authorize_url/token_url 缺失（且未配置 discovery_url 可补全）"
    async with httpx.AsyncClient(timeout=_ENDPOINT_TIMEOUT) as client:
        try:
            await client.post(
                endpoints["token_url"],
                data={"grant_type": "authorization_code", "code": "connectivity-check", "client_id": idp_row["client_id"]},
            )
        except httpx.HTTPError as exc:
            return False, f"token 端点（{endpoints['token_url']}）连通性检查失败：{exc}"
    return True, "discovery + token 端点连通性检查通过"
