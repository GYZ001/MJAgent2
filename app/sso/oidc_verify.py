"""OIDC ``id_token`` 的 JWT 解析 + 签名验证 + 五项声明校验（L1：纯计算，零
``app`` 内部依赖，零网络——JWKS 拉取在 ``app.sso.oidc``，本模块只消费已经
拿到手的 JWK dict）。

id_token 六项校验清单（PRD EP-02 §4，缺一条即视为未实现）：``iss`` 精确
匹配、``aud`` 含 client_id、``exp``/``iat`` 带 ≤120s 时钟偏移、``nonce`` 与
``sso_auth_requests`` 一致、签名验证（RS256/ES256）、``sub`` 非空。前五项在
``verify_claims()``，签名验证在 ``verify_signature()``——两者必须都调用，
且顺序上先验签名、后信任 payload 的其它字段（``app.sso.oidc.verify_id_token``
的编排顺序），不能先读 claims 再"顺便"验签名，那样任何一条 claims 判断在
验签之前短路返回都会把未验证的 payload 当真。

签名验证用 ``cryptography``（RS256/ES256 都支持，回退方案 B 只能做 RS256），
不使用任何 JWT 第三方库——EP-02 §7 冻结依赖只多一个 ``cryptography``。
"""
from __future__ import annotations

import base64
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

CLOCK_SKEW_S = 120
_SUPPORTED_ALGS = ("RS256", "ES256")
_P256_CURVE = ec.SECP256R1()


class IdTokenError(ValueError):
    """id_token 六项校验清单中任意一条不通过；调用方一律映射成 401。"""


def _b64url_decode(segment: str) -> bytes:
    pad = (-len(segment)) % 4
    return base64.urlsafe_b64decode(segment + ("=" * pad))


def _b64url_uint(segment: str) -> int:
    return int.from_bytes(_b64url_decode(segment), "big")


def decode_jwt_parts(token: str) -> tuple[dict, dict, bytes, bytes]:
    """拆 JWT 为 ``(header, payload, signing_input, signature)``，不校验签名。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise IdTokenError("id_token 格式不正确：不是标准的三段式 JWT")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except (ValueError, UnicodeDecodeError) as exc:
        raise IdTokenError(f"id_token 解码失败：{exc}") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise IdTokenError("id_token 的 header/payload 必须是 JSON 对象")
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    return header, payload, signing_input, signature


def _rsa_public_key(jwk: dict) -> rsa.RSAPublicKey:
    n = _b64url_uint(jwk["n"])
    e = _b64url_uint(jwk["e"])
    return rsa.RSAPublicNumbers(e, n).public_key()


def _ec_public_key(jwk: dict) -> ec.EllipticCurvePublicKey:
    if jwk.get("crv") != "P-256":
        raise IdTokenError(f"ES256 仅支持 P-256 曲线，JWK 声明的是 {jwk.get('crv')!r}")
    x = _b64url_uint(jwk["x"])
    y = _b64url_uint(jwk["y"])
    return ec.EllipticCurvePublicNumbers(x, y, _P256_CURVE).public_key()


def verify_signature(alg: str, jwk: dict, signing_input: bytes, signature: bytes) -> None:
    """签名校验失败一律抛 ``IdTokenError``，不吞异常、不当作"跳过校验"处理。"""
    if alg not in _SUPPORTED_ALGS:
        raise IdTokenError(f"不支持的签名算法：{alg!r}，仅支持 {'/'.join(_SUPPORTED_ALGS)}")
    try:
        if alg == "RS256":
            _rsa_public_key(jwk).verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        else:  # ES256：JWT 签名是原始 r||s（各 32 字节），需转 DER 才能喂给 cryptography
            if len(signature) != 64:
                raise IdTokenError(f"ES256 签名长度异常：期望 64 字节，实际 {len(signature)}")
            r = int.from_bytes(signature[:32], "big")
            s = int.from_bytes(signature[32:], "big")
            der_sig = utils.encode_dss_signature(r, s)
            _ec_public_key(jwk).verify(der_sig, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise IdTokenError("id_token 签名校验失败：可能被篡改，或使用了错误的密钥") from exc
    except KeyError as exc:
        raise IdTokenError(f"JWK 缺少必要字段：{exc}") from exc


def verify_claims(payload: dict, *, issuer: str, client_id: str, nonce: str) -> dict:
    """六项校验清单里除签名外的其余五项：``iss``/``aud``/``exp``+``iat``/
    ``nonce``/``sub``。只在签名已验证通过之后调用（见模块文档的顺序要求）。
    """
    iss = payload.get("iss")
    if iss != issuer:
        raise IdTokenError(f"iss 不匹配：期望 {issuer!r}，实际收到 {iss!r}")

    aud = payload.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if client_id not in aud_list:
        raise IdTokenError(f"aud 不包含 client_id：期望包含 {client_id!r}，实际 {aud!r}")

    now_ts = time.time()
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise IdTokenError("id_token 缺少 exp 声明")
    if now_ts > exp + CLOCK_SKEW_S:
        raise IdTokenError(
            f"id_token 已过期：服务器时间 {now_ts:.0f}，exp {exp:.0f}，"
            f"允许 ≤{CLOCK_SKEW_S}s 时钟偏移，实际已超出 {now_ts - exp:.0f}s——"
            "若两端只差几秒到几分钟，先检查服务器与 IdP 的系统时钟同步"
        )
    iat = payload.get("iat")
    if isinstance(iat, (int, float)) and iat > now_ts + CLOCK_SKEW_S:
        raise IdTokenError(
            f"id_token 的 iat（{iat:.0f}）晚于服务器时间（{now_ts:.0f}）超出 "
            f"允许的 ≤{CLOCK_SKEW_S}s 时钟偏移，请检查两端系统时钟"
        )

    if payload.get("nonce") != nonce:
        raise IdTokenError("nonce 不匹配：可能是重放攻击，或 sso_auth_requests 记录已损坏")

    sub = payload.get("sub")
    if not sub or not str(sub).strip():
        raise IdTokenError("id_token 缺少非空的 sub 声明")

    return payload
