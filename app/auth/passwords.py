"""口令哈希：仅用标准库 hashlib.scrypt，不引入任何第三方依赖。

存储格式：``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>``。参数固定为
n=16384, r=8, p=1, dklen=32；salt 每次随机生成，因此同一口令两次哈希结果不同。
后续 RBAC 阶段（登录、改密）都复用这里的两个函数，不再各自实现哈希逻辑。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGO = "scrypt"
_N = 16384
_R = 8
_P = 1
_DKLEN = 32


def hash_password(plain: str) -> str:
    """对明文口令生成带随机 salt 的 scrypt 哈希，返回可直接入库的字符串。"""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        plain.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"{_ALGO}${_N}${_R}${_P}${salt.hex()}${derived.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """用 stored 中记录的参数重新推导，再用常数时间比较，避免时序侧信道。"""
    try:
        algo, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    derived = hashlib.scrypt(
        plain.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
    )
    return hmac.compare_digest(derived, expected)
