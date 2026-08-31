"""纯标准库 RSA-SHA256 / PKCS#1 v1.5 验签与签名。

环境未装 ``cryptography``（``pip show cryptography`` 确认过，见
``app/payments/__init__.py`` 模块文档），按派单"没有就用标准库能做的方案"实现：
RSA 验签/签名的核心运算就是模幂，Python 内置三参数 ``pow(base, exp, mod)`` 本身
就是任意精度整数的高效模幂实现，不需要额外的大数库；真正需要手写的只有
PKCS#1 v1.5 的 EMSA 编码/解码（本文件）与 DER 取公私钥（``crypto_asn1.py``）。

**验签用公钥做模幂，无密钥相关的秘密输入，不存在时序旁道问题**；**签名用私钥
做模幂，理论上存在时序侧信道**（攻击者若能大量、精确计时地提交"请对这段内容
签名"的请求，可能反推私钥）——本包的签名只用于我们自己对外发起的下单请求
（内容是我方订单数据，不接受攻击者任意输入触发签名），实际可利用性很低，但
仍在验收报告里如实标注：这两个原语都是手写实现，不是审计过的库，正式上线前
应换成 ``cryptography``。
"""
from __future__ import annotations

import hashlib

from app.payments.crypto_asn1 import DerError

#: RFC 8017 §9.2 表 2：SHA-256 的 DigestInfo DER 前缀（AlgorithmIdentifier +
#: OCTET STRING 头，不含摘要本身），几乎所有 PKCS#1 v1.5 实现都硬编码这同一
#: 串常量（OpenSSL/BoringSSL/Java Signature 等）。
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class RsaSignatureError(ValueError):
    """签名格式不符合 PKCS#1 v1.5，或签名值超出模长——调用方应按验签失败处理。"""


def _digest_info(message: bytes) -> bytes:
    return _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()


def _key_size_bytes(n: int) -> int:
    return (n.bit_length() + 7) // 8


def _emsa_pkcs1v15_encode(message: bytes, em_len: int) -> bytes:
    t = _digest_info(message)
    ps_len = em_len - len(t) - 3
    if ps_len < 8:
        raise RsaSignatureError("RSA 模长相对 SHA-256 DigestInfo 太短，无法编码")
    return b"\x00\x01" + b"\xff" * ps_len + b"\x00" + t


def verify_pkcs1v15_sha256(message: bytes, signature: bytes, n: int, e: int) -> bool:
    """RSASSA-PKCS1-v1_5 验签。任何格式错误、签名越界都返回 ``False``（不抛异常），
    调用方一律按"验签失败"处理，不需要区分失败原因——这也是不留旁路开关的一部分：
    没有"格式错误就放行"这种分支。
    """
    em_len = _key_size_bytes(n)
    if len(signature) != em_len:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    recovered_int = pow(sig_int, e, n)
    try:
        recovered = recovered_int.to_bytes(em_len, "big")
        expected = _emsa_pkcs1v15_encode(message, em_len)
    except (RsaSignatureError, OverflowError):
        return False
    # 定长字节串比较；两串长度已知相等（都固定为 em_len），标准库常量时间比较。
    import hmac as _hmac
    return _hmac.compare_digest(recovered, expected)


def sign_pkcs1v15_sha256(message: bytes, n: int, d: int) -> bytes:
    """RSASSA-PKCS1-v1_5 签名，供本包对外发起的下单请求做 Authorization 签名用。"""
    em_len = _key_size_bytes(n)
    em = _emsa_pkcs1v15_encode(message, em_len)
    m_int = int.from_bytes(em, "big")
    if m_int >= n:
        raise RsaSignatureError("编码后的消息长度超出 RSA 模长")
    sig_int = pow(m_int, d, n)
    return sig_int.to_bytes(em_len, "big")


def load_public_key(pem_text: str) -> tuple[int, int]:
    """按 PEM label 自动选择证书或裸公钥解析路径，统一入口给调用方用。"""
    from app.payments.crypto_asn1 import pem_decode, rsa_public_key_from_certificate, rsa_public_key_from_spki

    der = pem_decode(pem_text)
    is_certificate = "CERTIFICATE" in pem_text and "PUBLIC KEY" not in pem_text
    try:
        if is_certificate:
            return rsa_public_key_from_certificate(der)
        return rsa_public_key_from_spki(der)
    except DerError:
        raise
