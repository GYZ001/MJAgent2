"""纯标准库 AES-256 + GCM 解密。

只做解密方向：微信支付 V3 回调通知的 ``resource`` 字段用 AEAD_AES_256_GCM 加密
（订单号/金额/交易状态等实际内容都在密文里，不解密读不到），GCM 的
CTR（计数器加密）与 GHASH（认证标签）两部分都只需要 AES **正向**（加密）
运算，不需要实现 AES 解密方向（逆 S 盒/逆 MixColumns），因此比"完整 AES"
省了一半代码。

风险披露（见 ``app/payments/__init__.py`` 模块文档，不在这里重复）：环境未装
``cryptography``，这是纯手写实现，不是审计过的库。已用 GCM 规范原始论文
（McGrew & Viega, Appendix B）里的官方 Test Case 13/14/16（NIST 同源，AES-256
族）逐字段验证：Test Case 14 覆盖单块 AES 正确性、Test Case 16 覆盖 5 块
CTR+GHASH+AAD 的完整链路（含篡改密文/AAD/tag 三种情况正确拒绝），Test Case 13
覆盖空密文/空 AAD 边界。测试见 ``tests/test_payments_crypto_aesgcm.py``。

S 盒不是硬编码 256 字节表（手抄大表出错难查），而是按 AES 标准定义现场算出来
（GF(2^8) 乘法逆元 + 仿射变换），用 sbox[0]=0x63、sbox[1]=0x7c、sbox[0x53]=0xed
三个公开已知值自检——算法錯了这三个值不可能同时凑对。
"""
from __future__ import annotations

import hmac

# ---------------------------------------------------------------------------
# GF(2^8) 运算与 AES S 盒（现场推导，不是抄表）
# ---------------------------------------------------------------------------

def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法（AES 模多项式 x^8+x^4+x^3+x+1，即 0x11B），俄式乘法算法。"""
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        carry = a & 0x80
        a = (a << 1) & 0xFF
        if carry:
            a ^= 0x1B
        b >>= 1
    return product


def _gf_inverse(a: int) -> int:
    if a == 0:
        return 0
    for x in range(1, 256):
        if _gmul(a, x) == 1:
            return x
    raise AssertionError(f"GF(2^8) 元素 {a} 未找到乘法逆元，实现有 bug")  # pragma: no cover


def _rotl8(b: int, n: int) -> int:
    return ((b << n) | (b >> (8 - n))) & 0xFF


def _build_sbox() -> tuple[int, ...]:
    sbox = []
    for a in range(256):
        inv = _gf_inverse(a)
        s = inv
        for n in (1, 2, 3, 4):
            s ^= _rotl8(inv, n)
        sbox.append(s ^ 0x63)
    return tuple(sbox)


_SBOX = _build_sbox()
assert _SBOX[0] == 0x63 and _SBOX[1] == 0x7C and _SBOX[0x53] == 0xED  # noqa: S101 — 自检，见模块文档

_RCON = [1]
for _ in range(9):
    _RCON.append(_gmul(_RCON[-1], 2))


# ---------------------------------------------------------------------------
# AES-256 密钥扩展与单块加密（仅正向）
# ---------------------------------------------------------------------------

def _sub_word(w: bytes) -> bytes:
    return bytes(_SBOX[b] for b in w)


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _key_expansion(key: bytes) -> list[bytes]:
    """AES-256（Nk=8, Nr=14）密钥扩展，返回 15 个 16 字节轮密钥。"""
    nk, nr = 8, 14
    words = [key[4 * i:4 * i + 4] for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = words[i - 1]
        if i % nk == 0:
            rotated = temp[1:] + temp[:1]
            temp = _xor_bytes(_sub_word(rotated), bytes([_RCON[i // nk - 1], 0, 0, 0]))
        elif i % nk == 4:
            temp = _sub_word(temp)
        words.append(_xor_bytes(words[i - nk], temp))
    return [b"".join(words[4 * r:4 * r + 4]) for r in range(nr + 1)]


def _shift_rows(state: bytearray) -> None:
    src = bytes(state)
    for r in range(4):
        for c in range(4):
            state[r + 4 * c] = src[r + 4 * ((c + r) % 4)]


def _mix_columns(state: bytearray) -> None:
    for c in range(4):
        i = 4 * c
        s0, s1, s2, s3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _gmul(s0, 2) ^ _gmul(s1, 3) ^ s2 ^ s3
        state[i + 1] = s0 ^ _gmul(s1, 2) ^ _gmul(s2, 3) ^ s3
        state[i + 2] = s0 ^ s1 ^ _gmul(s2, 2) ^ _gmul(s3, 3)
        state[i + 3] = _gmul(s0, 3) ^ s1 ^ s2 ^ _gmul(s3, 2)


def _encrypt_block(round_keys: list[bytes], block: bytes) -> bytes:
    """AES-256 单块（16 字节）正向加密，14 轮。"""
    state = bytearray(x ^ y for x, y in zip(block, round_keys[0]))
    for rnd in range(1, 14):
        state = bytearray(_SBOX[b] for b in state)
        _shift_rows(state)
        _mix_columns(state)
        state = bytearray(x ^ y for x, y in zip(state, round_keys[rnd]))
    state = bytearray(_SBOX[b] for b in state)
    _shift_rows(state)
    return bytes(x ^ y for x, y in zip(state, round_keys[14]))


# ---------------------------------------------------------------------------
# GCM：GHASH（GF(2^128)）+ CTR + 解密入口
# ---------------------------------------------------------------------------

_GCM_R = 0xE1 << 120  # NIST SP 800-38D 的既定约减常量


def _gf128_mul(x: int, y: int) -> int:
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        v = (v >> 1) ^ _GCM_R if v & 1 else v >> 1
    return z


def _pad16(data: bytes) -> bytes:
    remainder = len(data) % 16
    return data if remainder == 0 else data + b"\x00" * (16 - remainder)


def _ghash(h: bytes, aad: bytes, ciphertext: bytes) -> bytes:
    h_int = int.from_bytes(h, "big")
    y = 0
    for source in (_pad16(aad), _pad16(ciphertext)):
        for offset in range(0, len(source), 16):
            y = _gf128_mul(y ^ int.from_bytes(source[offset:offset + 16], "big"), h_int)
    length_block = (len(aad) * 8).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")
    y = _gf128_mul(y ^ int.from_bytes(length_block, "big"), h_int)
    return y.to_bytes(16, "big")


def _inc32(counter: int) -> int:
    """只自增低 32 位（模 2**32），高 96 位（nonce 部分）不动——GCM 规范的
    ``inc32``，不是普通大数加一。"""
    return (counter & ~0xFFFFFFFF) | ((counter + 1) & 0xFFFFFFFF)


def _gctr(round_keys: list[bytes], initial_counter: int, data: bytes) -> bytes:
    out = bytearray()
    counter = initial_counter
    for offset in range(0, len(data), 16):
        keystream = _encrypt_block(round_keys, counter.to_bytes(16, "big"))
        chunk = data[offset:offset + 16]
        out += bytes(a ^ b for a, b in zip(chunk, keystream))
        counter = _inc32(counter)
    return bytes(out)


class AeadError(ValueError):
    """AEAD 认证失败：tag 不匹配（密文/AAD 被篡改，或 key/nonce 不对），一律
    按验签失败处理，不做任何"格式不对就跳过"的分支。"""


def aes256_gcm_decrypt(key: bytes, nonce: bytes, ciphertext_and_tag: bytes, aad: bytes) -> bytes:
    """AES-256-GCM 解密并验证认证标签。``ciphertext_and_tag`` 末尾 16 字节是
    tag（微信支付 V3 通知的 ``resource.ciphertext`` 字段就是这种"密文+tag
    拼接后再 base64"的格式）。tag 校验失败抛 ``AeadError``，调用方必须整体
    拒绝这条通知，不能只警告不阻断。
    """
    if len(key) != 32:
        raise ValueError("AES-256-GCM 需要 32 字节 key")
    if len(nonce) != 12:
        raise ValueError("本实现只支持 96-bit nonce（微信支付 V3 通知固定用这个长度）")
    if len(ciphertext_and_tag) < 16:
        raise AeadError("密文长度小于 tag 长度，数据被截断或不是合法 AEAD 密文")
    ciphertext, tag = ciphertext_and_tag[:-16], ciphertext_and_tag[-16:]
    round_keys = _key_expansion(key)
    h = _encrypt_block(round_keys, b"\x00" * 16)
    j0 = int.from_bytes(nonce + b"\x00\x00\x00\x01", "big")
    tag_int = int.from_bytes(_ghash(h, aad, ciphertext), "big") ^ int.from_bytes(
        _encrypt_block(round_keys, j0.to_bytes(16, "big")), "big"
    )
    expected_tag = tag_int.to_bytes(16, "big")
    if not hmac.compare_digest(expected_tag, tag):
        raise AeadError("AES-256-GCM 认证标签校验失败：密文/AAD 已被篡改或 key/nonce 不匹配")
    return _gctr(round_keys, _inc32(j0), ciphertext)
