"""测试专用：现场生成一把小型 RSA 密钥对 + 手写最小 DER 编码器，产出能喂给
``app.payments.crypto_asn1`` 解析的 PEM（PKCS8 私钥 / SPKI 公钥 / 一张结构
合法但不是任何 CA 签发的"自证书"）。

生产代码（``app/payments/crypto_asn1.py``）只做解码，不做编码——真实商户私钥/
证书从来是"读别人给的文件"，从不需要"造一份"。测试需要一份可控的密钥材料来
验证解码 + RSA 签名/验签的完整链路，所以编码器只活在这里。

1024 位仅用于测试提速（Miller-Rabin 生成大素数是纯 Python，位数越大越慢）；
生产环境要求至少 2048 位 RSA，这里不代表真实商户密钥的安全强度。
"""
from __future__ import annotations

import base64
import random
import textwrap

_RSA_ALG_ID_OID = bytes.fromhex("06092a864886f70d010101")  # 1.2.840.113549.1.1.1


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    n_bytes = (n.bit_length() + 7) // 8
    return bytes([0x80 | n_bytes]) + n.to_bytes(n_bytes, "big")


def _der_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(value)) + value


def _der_integer(x: int) -> bytes:
    n_bytes = max(1, (x.bit_length() + 7) // 8)
    raw = x.to_bytes(n_bytes, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der_tlv(0x02, raw)


def _der_sequence(children: list[bytes]) -> bytes:
    return _der_tlv(0x30, b"".join(children))


def _der_bitstring(inner: bytes) -> bytes:
    return _der_tlv(0x03, b"\x00" + inner)


def _der_octetstring(inner: bytes) -> bytes:
    return _der_tlv(0x04, inner)


_RSA_ALG_ID = _der_sequence([_RSA_ALG_ID_OID, bytes.fromhex("0500")])


def pem_encode(label: str, der: bytes) -> str:
    body = "\n".join(textwrap.wrap(base64.b64encode(der).decode("ascii"), 64))
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def encode_spki_pem(n: int, e: int) -> str:
    rsa_pub = _der_sequence([_der_integer(n), _der_integer(e)])
    der = _der_sequence([_RSA_ALG_ID, _der_bitstring(rsa_pub)])
    return pem_encode("PUBLIC KEY", der)


def encode_pkcs8_private_key_pem(n: int, e: int, d: int, p: int, q: int) -> str:
    d_p, d_q, q_inv = d % (p - 1), d % (q - 1), pow(q, -1, p)
    rsa_priv = _der_sequence([
        _der_integer(0), _der_integer(n), _der_integer(e), _der_integer(d),
        _der_integer(p), _der_integer(q), _der_integer(d_p), _der_integer(d_q), _der_integer(q_inv),
    ])
    der = _der_sequence([_der_integer(0), _RSA_ALG_ID, _der_octetstring(rsa_priv)])
    return pem_encode("PRIVATE KEY", der)


def encode_fake_certificate_pem(n: int, e: int) -> str:
    """结构合法但没有任何 CA 签名的"证书"——只用来测
    ``rsa_public_key_from_certificate`` 的字段定位逻辑，不测证书链信任。"""
    spki = _der_sequence([_RSA_ALG_ID, _der_bitstring(_der_sequence([_der_integer(n), _der_integer(e)]))])
    version = _der_tlv(0xA0, _der_integer(2))
    empty_name = _der_sequence([])
    validity_time = _der_tlv(0x17, b"260101000000Z")
    tbs = _der_sequence([
        version, _der_integer(1), _RSA_ALG_ID, empty_name,
        _der_sequence([validity_time, validity_time]), empty_name, spki,
    ])
    der = _der_sequence([tbs, _RSA_ALG_ID, _der_bitstring(b"\x00" * 16)])
    return pem_encode("CERTIFICATE", der)


def _is_probable_prime(n: int, rounds: int = 12) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = random.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        candidate = random.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


def generate_rsa_keypair(bits: int = 1024) -> tuple[int, int, int, int, int]:
    """返回 ``(n, e, d, p, q)``。纯 Python Miller-Rabin 生成大素数，测试专用
    （见模块文档：位数远低于生产要求的 2048）。"""
    e = 65537
    while True:
        p = _gen_prime(bits // 2)
        q = _gen_prime(bits // 2)
        if p == q:
            continue
        n = p * q
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        d = pow(e, -1, phi)
        return n, e, d, p, q


def aes256_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """测试专用：借生产代码 ``app.payments.crypto_aesgcm`` 同一套已验证过的
    AES-256 底层 primitives（``_key_expansion``/``_encrypt_block``/``_gctr``/
    ``_ghash``）反向拼一份 AEAD 密文+tag，喂给微信回调测试用——生产代码本身
    只实现解密方向（见该模块文档）。CTR 是异或对称操作，GCM 加密与解密对
    密文块的处理完全一致，只是"密文"的来源不同（这里是我们自己造的，解密时
    是网络收到的），tag 的算法两边完全相同（都是 GHASH(密文) XOR E(K,J0)）。
    """
    from app.payments import crypto_aesgcm as g

    round_keys = g._key_expansion(key)
    h = g._encrypt_block(round_keys, b"\x00" * 16)
    j0 = int.from_bytes(nonce + b"\x00\x00\x00\x01", "big")
    ciphertext = g._gctr(round_keys, g._inc32(j0), plaintext)
    tag_int = int.from_bytes(g._ghash(h, aad, ciphertext), "big") ^ int.from_bytes(
        g._encrypt_block(round_keys, j0.to_bytes(16, "big")), "big"
    )
    return ciphertext + tag_int.to_bytes(16, "big")
