"""模型凭据加密：封装 ``cryptography`` 的 AES-256-GCM（AEAD），零 db 依赖（L1）。

依赖决策（2026-09-10 用户拍板，覆盖了本文件最初"复用 app/payments/crypto_aesgcm.py
手写原语"的方案）：改用标准库级别的 ``cryptography`` 包（已加入
``requirements.txt``），不再自己实现 GCTR/GHASH。``app/payments/crypto_aesgcm.py``
是支付链路在用的手写实现，保持不动，本模块不从它借用代码，两者互相独立。

自写密码学没有 known-answer test 等于没写——即使这里只是薄封装，"封装参数用对了
没有"（nonce 长度、AAD 是否真的绑定了 model_id、tag 是否真的挂在密文尾部）仍然
需要独立验证，不能只信"库本身是对的"。``tests/test_model_credentials_crypto.py``
用 GCM 规范原始论文（McGrew & Viega,Appendix B）的官方 Test Case 16（AES-256、
带 AAD、5 块，NIST 同源，与 ``tests/test_payments_crypto_aesgcm.py`` 引用的是
同一组公开向量）逐字节验证本模块的 ``gcm_encrypt``/``gcm_decrypt``。

AAD 绑定 model_id：见 ``credential_aad()``。一条密文如果被挪到另一个 model_id
的行下面，AAD 不匹配，解密直接抛 ``AeadError``，不会解出"看似正常"的错误
明文——这是"model 与凭据必须一起传递、不可分割"这条硬约束在存储层的体现。
"""
from __future__ import annotations

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LEN = 32  # AES-256
NONCE_LEN = 12  # GCM 标准 96-bit nonce，与 app/payments/crypto_aesgcm.py 同口径


class AeadError(ValueError):
    """AEAD 认证失败：tag 不匹配，或 AAD 与目标 model_id 对不上。一律整体拒绝，
    不做任何"格式不对就跳过"的分支——凭据解密失败必须让调用方能感知到，不能
    悄悄返回空字符串把"解不出来"和"本来就没配"混为一谈。"""


def credential_aad(model_id: str) -> bytes:
    """凭据密文的 AAD：绑定 model_id，防止密文被挪到另一行还能解开。"""
    model_id = str(model_id or "").strip()
    if not model_id:
        raise ValueError("model_id 不能为空——凭据必须绑定到具体模型")
    return f"model_credentials:{model_id}".encode("utf-8")


def gcm_encrypt(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM 加密，返回 ciphertext||tag（tag 固定挂在末尾 16 字节，与
    ``app/payments/crypto_aesgcm.py::aes256_gcm_decrypt`` 的输入格式一致）。"""
    _check_key(key)
    _check_nonce(nonce)
    return AESGCM(key).encrypt(nonce, plaintext, aad)


def gcm_decrypt(key: bytes, nonce: bytes, aad: bytes, ciphertext_and_tag: bytes) -> bytes:
    """AES-256-GCM 解密并验证 tag；失败（tag 不对/AAD 不对/key 不对）一律抛
    ``AeadError``，不吞异常。"""
    _check_key(key)
    _check_nonce(nonce)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext_and_tag, aad)
    except InvalidTag as exc:
        raise AeadError(
            "AES-256-GCM 认证标签校验失败：密文/AAD 已被篡改，或 key/nonce/"
            "model_id 与写入时不一致"
        ) from exc


def encrypt_secret(key: bytes, model_id: str, plaintext: str) -> tuple[bytes, bytes]:
    """凭据落库用的便捷封装：随机生成 nonce，AAD 绑定 model_id。

    返回 ``(nonce, ciphertext_with_tag)``，两者都要落库（``model_credentials``
    表的 ``key_nonce``/``key_ciphertext`` 两列）。
    """
    nonce = os.urandom(NONCE_LEN)
    ciphertext = gcm_encrypt(key, nonce, credential_aad(model_id), plaintext.encode("utf-8"))
    return nonce, ciphertext


def decrypt_secret(key: bytes, model_id: str, nonce: bytes, ciphertext_and_tag: bytes) -> str:
    """``encrypt_secret`` 的逆操作。"""
    plaintext = gcm_decrypt(key, nonce, credential_aad(model_id), ciphertext_and_tag)
    return plaintext.decode("utf-8")


def fingerprint(secret: str) -> str:
    """sha256(secret) 前 12 位——用于"是不是同一个 Key"比对，不可逆推明文。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def mask(secret: str) -> str:
    """展示用掩码，如 ``sk-****abcd``；接口任何路径都只回这个，不回明文。"""
    value = secret.strip()
    if len(value) <= 8:
        return "*" * len(value) if value else ""
    return f"{value[:3]}****{value[-4:]}"


def _check_key(key: bytes) -> None:
    if len(key) != KEY_LEN:
        raise ValueError(f"AES-256-GCM 需要 {KEY_LEN} 字节 key，实际 {len(key)} 字节")


def _check_nonce(nonce: bytes) -> None:
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"本模块只支持 {NONCE_LEN} 字节（96-bit）nonce，实际 {len(nonce)} 字节")
