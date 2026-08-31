"""最小 DER/PEM 解析：只为了从 X.509 证书 / SubjectPublicKeyInfo / PKCS8 私钥里
取出 RSA 的 (n, e) 或 (n, d)，供 ``crypto_rsa.py`` 做验签/签名用。

不是通用 ASN.1 库——只实现本包用得到的四种结构（SEQUENCE/INTEGER/BIT STRING/
OCTET STRING 的 TLV 读取 + 三种具体文档结构的字段定位），且只解码不编码（生产
代码从不需要"造一个证书"，只需要解析拿到的证书/公钥/私钥）。纯标准库
（``base64``），零第三方依赖。
"""
from __future__ import annotations

import base64

TAG_INTEGER = 0x02
TAG_BIT_STRING = 0x03
TAG_OCTET_STRING = 0x04
TAG_SEQUENCE = 0x30


class DerError(ValueError):
    """DER 结构不符合预期（截断、长度不符、标签不对）。"""


def pem_decode(pem_text: str) -> bytes:
    """剥掉 ``-----BEGIN X-----``/``-----END X-----`` 外壳，base64 解码。

    不校验 label 内容（CERTIFICATE / PUBLIC KEY / PRIVATE KEY / RSA PRIVATE
    KEY 都走这一条路径），标签语义由调用方根据自己传入的是哪种文件决定。
    """
    lines = [ln.strip() for ln in pem_text.strip().splitlines()]
    body = [ln for ln in lines if ln and not ln.startswith("-----")]
    if not body:
        raise DerError("PEM 内容为空或缺少 BEGIN/END 边界")
    try:
        return base64.b64decode("".join(body))
    except Exception as exc:  # noqa: BLE001 — 统一转成 DerError，调用方只认一种异常
        raise DerError(f"PEM base64 解码失败: {exc}") from exc


def _read_tlv(data: bytes, offset: int) -> tuple[int, int, int, int]:
    """读取一个 TLV，返回 ``(tag, value_start, value_end, next_offset)``。"""
    if offset >= len(data):
        raise DerError("DER 数据在标签位置截断")
    tag = data[offset]
    if offset + 1 >= len(data):
        raise DerError("DER 数据在长度位置截断")
    first_len = data[offset + 1]
    if first_len & 0x80 == 0:
        length = first_len
        value_start = offset + 2
    else:
        n_bytes = first_len & 0x7F
        if n_bytes == 0:
            raise DerError("不支持 DER 不定长编码（BER indefinite length）")
        len_start = offset + 2
        len_end = len_start + n_bytes
        if len_end > len(data):
            raise DerError("DER 长度字段截断")
        length = int.from_bytes(data[len_start:len_end], "big")
        value_start = len_end
    value_end = value_start + length
    if value_end > len(data):
        raise DerError("DER 值字段超出数据边界")
    return tag, value_start, value_end, value_end


def read_children(value: bytes) -> list[tuple[int, bytes]]:
    """把一段 SEQUENCE 的 value 字节按顺序拆成直接子节点 ``[(tag, value), ...]``。"""
    children: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(value):
        tag, v_start, v_end, next_offset = _read_tlv(value, offset)
        children.append((tag, value[v_start:v_end]))
        offset = next_offset
    return children


def _top_level_value(der: bytes, expect_tag: int = TAG_SEQUENCE) -> bytes:
    tag, v_start, v_end, _ = _read_tlv(der, 0)
    if tag != expect_tag:
        raise DerError(f"顶层标签 0x{tag:02x} 不是期望的 0x{expect_tag:02x}")
    return der[v_start:v_end]


def _der_integer(value: bytes) -> int:
    return int.from_bytes(value, "big")


def rsa_public_key_from_spki(der: bytes) -> tuple[int, int]:
    """SubjectPublicKeyInfo（``-----BEGIN PUBLIC KEY-----``）里取 (n, e)。

    结构：SEQUENCE { AlgorithmIdentifier, BIT STRING subjectPublicKey }；
    BIT STRING 首字节是"未用位数"（RSA 公钥字节对齐，恒为 0x00），其余字节是
    内层 RSAPublicKey ::= SEQUENCE { INTEGER n, INTEGER e } 的 DER。
    """
    children = read_children(_top_level_value(der))
    if len(children) != 2 or children[1][0] != TAG_BIT_STRING:
        raise DerError("SubjectPublicKeyInfo 结构不符合预期")
    bitstring = children[1][1]
    if not bitstring or bitstring[0] != 0x00:
        raise DerError("RSA 公钥 BIT STRING 未按字节对齐")
    rsa_pub = read_children(_top_level_value(bitstring[1:]))
    if len(rsa_pub) != 2 or rsa_pub[0][0] != TAG_INTEGER or rsa_pub[1][0] != TAG_INTEGER:
        raise DerError("RSAPublicKey 结构不符合预期")
    return _der_integer(rsa_pub[0][1]), _der_integer(rsa_pub[1][1])


def _looks_like_spki(tag: int, value: bytes) -> bool:
    if tag != TAG_SEQUENCE:
        return False
    try:
        inner = read_children(value)
    except DerError:
        return False
    return len(inner) == 2 and inner[0][0] == TAG_SEQUENCE and inner[1][0] == TAG_BIT_STRING


def rsa_public_key_from_certificate(der: bytes) -> tuple[int, int]:
    """X.509 证书（``-----BEGIN CERTIFICATE-----``，微信支付平台证书）里取 (n, e)。

    Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm, signatureValue }；
    tbsCertificate 内 SubjectPublicKeyInfo 前面有几个可选字段（version 用
    [0] EXPLICIT 包一层，序列号/签名算法/颁发者/有效期/主体名称），不同证书里
    它在第几个子节点会变——不按位置定位，按结构定位：tbsCertificate 的直接子
    节点里，"自身是 SEQUENCE 且其两个子节点分别是 SEQUENCE+BIT STRING" 的那个
    才是 SubjectPublicKeyInfo（其余 SEQUENCE 型字段如 validity 两个子节点是
    Time 不是 SEQUENCE，issuer/subject 两个子节点的首个子节点是 SET 不是
    SEQUENCE，均不匹配这个形状）。
    """
    cert_children = read_children(_top_level_value(der))
    if not cert_children or cert_children[0][0] != TAG_SEQUENCE:
        raise DerError("Certificate 结构不符合预期：缺少 tbsCertificate")
    tbs_children = read_children(cert_children[0][1])
    for tag, value in tbs_children:
        if _looks_like_spki(tag, value):
            return rsa_public_key_from_spki(bytes([TAG_SEQUENCE]) + _der_len_prefix(value) + value)
    raise DerError("tbsCertificate 中未找到 SubjectPublicKeyInfo")


def _der_len_prefix(value: bytes) -> bytes:
    """重建长度字段（把已拆开的子节点 value 重新拼回带 tag+length 的完整 TLV，
    好复用 ``rsa_public_key_from_spki`` 接受"完整 DER"的入参约定）。"""
    n = len(value)
    if n < 0x80:
        return bytes([n])
    n_bytes = (n.bit_length() + 7) // 8
    return bytes([0x80 | n_bytes]) + n.to_bytes(n_bytes, "big")


def rsa_private_key_from_pem(pem_text: str) -> tuple[int, int]:
    """商户私钥（PKCS8 ``PRIVATE KEY`` 或 PKCS1 ``RSA PRIVATE KEY``）里取 (n, d)。

    RSAPrivateKey ::= SEQUENCE { version, n, e, d, p, q, dP, dQ, qInv }
    （0 基索引：1=n，3=d）。PKCS8 是 RSAPrivateKey 外面再包一层
    ``SEQUENCE { version, AlgorithmIdentifier, OCTET STRING }``，OCTET STRING
    的 value 直接就是内层 RSAPrivateKey 的 DER（不像 BIT STRING 多一个未用位
    数字节）。按 PEM label 区分两种外壳，微信支付商户 API 私钥通常是 PKCS8
    ``BEGIN PRIVATE KEY``。
    """
    label_is_pkcs1 = "RSA PRIVATE KEY" in pem_text
    der = pem_decode(pem_text)
    if label_is_pkcs1:
        rsa_priv_der = der
    else:
        children = read_children(_top_level_value(der))
        octet = next((v for t, v in children if t == TAG_OCTET_STRING), None)
        if octet is None:
            raise DerError("PKCS8 PrivateKeyInfo 缺少 OCTET STRING privateKey 字段")
        rsa_priv_der = octet
    fields = read_children(_top_level_value(rsa_priv_der))
    if len(fields) < 4:
        raise DerError("RSAPrivateKey 字段数量不足")
    return _der_integer(fields[1][1]), _der_integer(fields[3][1])
