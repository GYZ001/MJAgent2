"""RSA-SHA256/PKCS1v1.5 签名验证 + DER/PEM 解析的正确性证明。

用 ``tests/payments_pki_helpers.py`` 现场生成一把测试密钥对（纯 Python
Miller-Rabin，1024 位仅为测试提速，不代表生产强度要求），走完整链路：
编码成 PKCS8 私钥 / SPKI 公钥 / 自制证书 PEM -> 用生产代码
``crypto_asn1``/``crypto_rsa`` 解析 -> 签名 -> 验签，并证明篡改/错误密钥
都被正确拒绝。
"""
from __future__ import annotations

import pytest

from app.payments.crypto_asn1 import (
    DerError,
    pem_decode,
    rsa_private_key_from_pem,
    rsa_public_key_from_certificate,
    rsa_public_key_from_spki,
)
from app.payments.crypto_rsa import load_public_key, sign_pkcs1v15_sha256, verify_pkcs1v15_sha256
from tests.payments_pki_helpers import (
    encode_fake_certificate_pem,
    encode_pkcs8_private_key_pem,
    encode_spki_pem,
    generate_rsa_keypair,
)


@pytest.fixture(scope="module")
def keypair():
    return generate_rsa_keypair(1024)


def test_spki_round_trip(keypair):
    n, e, _, _, _ = keypair
    n2, e2 = rsa_public_key_from_spki(pem_decode(encode_spki_pem(n, e)))
    assert (n2, e2) == (n, e)


def test_certificate_round_trip(keypair):
    n, e, _, _, _ = keypair
    n2, e2 = rsa_public_key_from_certificate(pem_decode(encode_fake_certificate_pem(n, e)))
    assert (n2, e2) == (n, e)


def test_pkcs8_private_key_round_trip(keypair):
    n, e, d, p, q = keypair
    n2, d2 = rsa_private_key_from_pem(encode_pkcs8_private_key_pem(n, e, d, p, q))
    assert (n2, d2) == (n, d)


def test_load_public_key_autodetects_spki_and_certificate(keypair):
    n, e, _, _, _ = keypair
    assert load_public_key(encode_spki_pem(n, e)) == (n, e)
    assert load_public_key(encode_fake_certificate_pem(n, e)) == (n, e)


def test_sign_verify_round_trip(keypair):
    n, e, d, _, _ = keypair
    message = b"order:pay-abc123 amount=19900"
    signature = sign_pkcs1v15_sha256(message, n, d)
    assert verify_pkcs1v15_sha256(message, signature, n, e) is True


def test_verify_rejects_tampered_message(keypair):
    n, e, d, _, _ = keypair
    signature = sign_pkcs1v15_sha256(b"original message", n, d)
    assert verify_pkcs1v15_sha256(b"tampered message", signature, n, e) is False


def test_verify_rejects_tampered_signature(keypair):
    n, e, d, _, _ = keypair
    signature = sign_pkcs1v15_sha256(b"original message", n, d)
    corrupted = bytes([signature[0] ^ 0xFF]) + signature[1:]
    assert verify_pkcs1v15_sha256(b"original message", corrupted, n, e) is False


def test_verify_rejects_signature_from_different_key(keypair):
    n, e, _, _, _ = keypair
    other_n, other_e, other_d, _, _ = generate_rsa_keypair(1024)
    message = b"same message, different signer"
    signature = sign_pkcs1v15_sha256(message, other_n, other_d)
    assert verify_pkcs1v15_sha256(message, signature, n, e) is False


def test_pem_decode_rejects_garbage():
    with pytest.raises(DerError):
        pem_decode("-----BEGIN CERTIFICATE-----\nnot-valid-base64!!!\n-----END CERTIFICATE-----\n")
