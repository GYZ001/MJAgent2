"""app.models_registry.crypto 的正确性证明。

自写密码学没有 known-answer test 等于没写——即使这里只是薄封装
``cryptography`` 的 ``AESGCM``，"封装参数用对了没有"仍然需要独立验证：nonce
长度是不是 96-bit、AAD 是不是真的绑定了 model_id、tag 是不是真的挂在密文尾部。

KAT 向量：GCM 规范原始论文（McGrew & Viega,"The Galois/Counter Mode of
Operation"，Appendix B）的官方 Test Case 16——AES-256、带 AAD、5 块，NIST 同源，
与 ``tests/test_payments_crypto_aesgcm.py`` 引用的是同一组公开向量（那边验证
纯手写解密实现，这边验证 ``cryptography`` 封装的加密+解密）。
"""
from __future__ import annotations

import pytest

from app.models_registry.crypto import (
    AeadError,
    KEY_LEN,
    NONCE_LEN,
    credential_aad,
    decrypt_secret,
    encrypt_secret,
    fingerprint,
    gcm_decrypt,
    gcm_encrypt,
    mask,
)

# ---------------------------------------------------------------------------
# NIST/GCM 规范 Test Case 16（AES-256，见模块文档）
# ---------------------------------------------------------------------------
_KEY16 = bytes.fromhex("feffe9928665731c6d6a8f9467308308" "feffe9928665731c6d6a8f9467308308")
_NONCE16 = bytes.fromhex("cafebabefacedbaddecaf888")
_AAD16 = bytes.fromhex("feedfacedeadbeeffeedfacedeadbeef" "abaddad2")
_PT16 = bytes.fromhex(
    "d9313225f88406e5a55909c5aff5269a" "86a7a9531534f7da2e4c303d8a318a72"
    "1c3c0c95956809532fcf0e2449a6b525" "b16aedf5aa0de657ba637b39"
)
_CT16 = bytes.fromhex(
    "522dc1f099567d07f47f37a32a84427d" "643a8cdcbfe5c0c97598a2bd2555d1aa"
    "8cb08e48590dbb3da7b08b1056828838" "c5f61e6393ba7a0abcc9f662"
)
_TAG16 = bytes.fromhex("76fc6ece0f4e1768cddf8853bb2d551b")


def test_kat_test_case_16_encrypt_matches_official_vector() -> None:
    assert len(_KEY16) == KEY_LEN
    assert len(_NONCE16) == NONCE_LEN
    got = gcm_encrypt(_KEY16, _NONCE16, _AAD16, _PT16)
    assert got == _CT16 + _TAG16


def test_kat_test_case_16_decrypt_recovers_plaintext() -> None:
    got = gcm_decrypt(_KEY16, _NONCE16, _AAD16, _CT16 + _TAG16)
    assert got == _PT16


def test_kat_tampered_ciphertext_rejected() -> None:
    tampered = bytes([_CT16[0] ^ 1]) + _CT16[1:]
    with pytest.raises(AeadError):
        gcm_decrypt(_KEY16, _NONCE16, _AAD16, tampered + _TAG16)


def test_kat_tampered_tag_rejected() -> None:
    tampered_tag = bytes([_TAG16[0] ^ 1]) + _TAG16[1:]
    with pytest.raises(AeadError):
        gcm_decrypt(_KEY16, _NONCE16, _AAD16, _CT16 + tampered_tag)


def test_kat_tampered_aad_rejected() -> None:
    tampered_aad = bytes([_AAD16[0] ^ 1]) + _AAD16[1:]
    with pytest.raises(AeadError):
        gcm_decrypt(_KEY16, _NONCE16, tampered_aad, _CT16 + _TAG16)


def test_rejects_non_256_bit_key() -> None:
    with pytest.raises(ValueError):
        gcm_encrypt(b"short", _NONCE16, _AAD16, _PT16)


def test_rejects_non_96_bit_nonce() -> None:
    with pytest.raises(ValueError):
        gcm_encrypt(_KEY16, b"short", _AAD16, _PT16)


# ---------------------------------------------------------------------------
# 凭据便捷封装：AAD 绑定 model_id
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_secret_roundtrip() -> None:
    key = b"\x11" * KEY_LEN
    nonce, ciphertext = encrypt_secret(key, "mdl_abc123", "sk-super-secret")
    assert len(nonce) == NONCE_LEN
    assert decrypt_secret(key, "mdl_abc123", nonce, ciphertext) == "sk-super-secret"


def test_aad_binds_to_model_id_moving_ciphertext_fails_to_decrypt() -> None:
    """一条密文被挪到另一个 model_id 的行下面必须解不开——这是"model 与凭据
    必须一起传递、不可分割"这条硬约束在存储层的体现（CLAUDE.md 记录过分开传
    会把请求打到错误端点且不报错的真实事故）。"""
    key = b"\x22" * KEY_LEN
    nonce, ciphertext = encrypt_secret(key, "mdl_owner", "sk-owner-key")
    with pytest.raises(AeadError):
        decrypt_secret(key, "mdl_someone_else", nonce, ciphertext)


def test_credential_aad_rejects_empty_model_id() -> None:
    with pytest.raises(ValueError):
        credential_aad("")


def test_credential_aad_distinct_per_model() -> None:
    assert credential_aad("a") != credential_aad("b")


# ---------------------------------------------------------------------------
# fingerprint / mask：展示层，不可逆推明文
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_and_12_chars() -> None:
    fp = fingerprint("sk-abcdef1234567890")
    assert len(fp) == 12
    assert fp == fingerprint("sk-abcdef1234567890")


def test_fingerprint_differs_for_different_secrets() -> None:
    assert fingerprint("sk-one") != fingerprint("sk-two")


def test_mask_keeps_only_prefix_and_suffix() -> None:
    masked = mask("sk-abcdef1234567890abcd")
    assert masked.startswith("sk-")
    assert masked.endswith("abcd")
    assert "1234567890" not in masked


def test_mask_short_secret_fully_starred() -> None:
    assert mask("short") == "*****"
    assert mask("") == ""
