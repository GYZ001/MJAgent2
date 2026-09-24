"""角色名 → 声音供应商可接受的 ``preferred_name``。

探针实测：千问声音设计的 ``preferred_name`` 只接受字母数字，带下划线会被 400
拒绝（官方页未写，见
docs/角色固定音色_声音生成接口调研与实施方案_2026-09-23.md §10）。取角色名
拼音的字母数字部分 + 短哈希，保证同项目不同角色不会重名、且不依赖角色名本身
是否恰好是纯字母数字。
"""
from __future__ import annotations

import hashlib

from pypinyin import Style, lazy_pinyin

MAX_LEN = 16
_HASH_LEN = 6
_NAME_LEN = MAX_LEN - _HASH_LEN


def preferred_name_for(character_name: str) -> str:
    pinyin = "".join(lazy_pinyin(character_name, style=Style.NORMAL))
    letters = "".join(ch for ch in pinyin.lower() if ch.isalnum())
    digest = hashlib.sha256(character_name.encode("utf-8")).hexdigest()[:_HASH_LEN]
    base = (letters[:_NAME_LEN] or "voice")[:_NAME_LEN]
    return (base + digest)[:MAX_LEN]
