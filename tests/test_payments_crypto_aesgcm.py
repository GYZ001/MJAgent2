"""AES-256-GCM 纯标准库实现的正确性证明。

不是自造的"看起来对"的向量：全部来自 GCM 规范原始论文（McGrew & Viega,
"The Galois/Counter Mode of Operation"，Appendix B）的官方 Test Case
13/14/16——这三条同时也是 NIST 同源、AES-256 族最常被引用的公开测试向量。
取值过程见任务记录：用 ``pdftotext -layout`` 逐字段抽取原文 PDF 并用
Python 校验每个字段的字节长度与已知代数关系（``Tag = GHASH(H,A,C) XOR
E(K,Y0)``、CTR 分块异或）全部吻合，不是手抄大表容易出錯的那种引用方式。
"""
from __future__ import annotations

import pytest

from app.payments.crypto_aesgcm import AeadError, aes256_gcm_decrypt

_K13 = bytes.fromhex("00000000000000000000000000000000" + "00000000000000000000000000000000")
_NONCE13 = bytes.fromhex("000000000000000000000000")
_TAG13 = bytes.fromhex("530f8afbc74536b9a963b4f1c4cb738b")

_Y0_14_PLAINTEXT = bytes.fromhex("00000000000000000000000000000000")
_CT14 = bytes.fromhex("cea7403d4d606b6e074ec5d3baf39d18")
_TAG14 = bytes.fromhex("d0d1c8a799996bf0265b98b5d48ab919")

_K16 = bytes.fromhex("feffe9928665731c6d6a8f9467308308" + "feffe9928665731c6d6a8f9467308308")
_NONCE16 = bytes.fromhex("cafebabefacedbaddecaf888")
_AAD16 = bytes.fromhex("feedfacedeadbeeffeedfacedeadbeef" + "abaddad2")
_CT16 = bytes.fromhex(
    "522dc1f099567d07f47f37a32a84427d" "643a8cdcbfe5c0c97598a2bd2555d1aa"
    "8cb08e48590dbb3da7b08b1056828838" "c5f61e6393ba7a0abcc9f662"
)
_TAG16 = bytes.fromhex("76fc6ece0f4e1768cddf8853bb2d551b")
_PT16 = bytes.fromhex(
    "d9313225f88406e5a55909c5aff5269a" "86a7a9531534f7da2e4c303d8a318a72"
    "1c3c0c95956809532fcf0e2449a6b525" "b16aedf5aa0de657ba637b39"
)


def test_empty_plaintext_and_aad_test_case_13():
    assert aes256_gcm_decrypt(_K13, _NONCE13, _TAG13, b"") == b""


def test_single_block_test_case_14():
    got = aes256_gcm_decrypt(_K13, _NONCE13, _CT14 + _TAG14, b"")
    assert got == _Y0_14_PLAINTEXT


def test_multi_block_with_aad_test_case_16():
    got = aes256_gcm_decrypt(_K16, _NONCE16, _CT16 + _TAG16, _AAD16)
    assert got == _PT16


def test_tampered_ciphertext_rejected():
    bad = bytes([_CT16[0] ^ 1]) + _CT16[1:]
    with pytest.raises(AeadError):
        aes256_gcm_decrypt(_K16, _NONCE16, bad + _TAG16, _AAD16)


def test_tampered_tag_rejected():
    bad_tag = bytes([_TAG16[0] ^ 1]) + _TAG16[1:]
    with pytest.raises(AeadError):
        aes256_gcm_decrypt(_K16, _NONCE16, _CT16 + bad_tag, _AAD16)


def test_tampered_aad_rejected():
    bad_aad = bytes([_AAD16[0] ^ 1]) + _AAD16[1:]
    with pytest.raises(AeadError):
        aes256_gcm_decrypt(_K16, _NONCE16, _CT16 + _TAG16, bad_aad)


def test_wrong_key_rejected():
    wrong_key = bytes([_K16[0] ^ 1]) + _K16[1:]
    with pytest.raises(AeadError):
        aes256_gcm_decrypt(wrong_key, _NONCE16, _CT16 + _TAG16, _AAD16)


def test_rejects_non_256_bit_key():
    with pytest.raises(ValueError):
        aes256_gcm_decrypt(b"\x00" * 16, _NONCE13, _TAG13, b"")


def test_rejects_non_96_bit_nonce():
    with pytest.raises(ValueError):
        aes256_gcm_decrypt(_K13, b"\x00" * 16, _TAG13, b"")
