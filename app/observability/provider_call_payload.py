"""供应商调用落库前的载荷压缩（2026-09-06 第 13 轮写锁风暴的根因）。

B 库实测：``provider_calls`` 46,277 行占 6.67 GB，其中 ``video_create`` 3,853 行平均 1.6 MB——
参考图以 base64 data URL 原样进了 ``request_json``（``preserve_exact`` 路径绕过了裁剪），
30 集同时派发时每次 INSERT/UPDATE 都要在写锁下写几 MB，WAL 涨到 2 GB，心跳写不进、
watchdog 误收口。这里把 base64 换成「长度 + sha256 前 16 位」占位：磁盘不再背图片字节，
而重启后的幂等漂移核对（seedance 比较存档请求与当前载荷）仍能区分「换了一张图」。

同一个值压缩两次结果不变（占位串不再二次压缩），所以历史上按原样存的行与新行可以用
同一函数归一后比较。
"""
from __future__ import annotations

import hashlib
from typing import Any

MAX_STRING_CHARS = 120_000
_OMITTED_MARK = "[omitted "


def compact_provider_payload(value: Any, *, max_string: int = MAX_STRING_CHARS) -> Any:
    """递归压缩：base64 data URL → 占位；超过 ``max_string`` 的字符串截断并标注长度。"""
    if isinstance(value, dict):
        return {k: compact_provider_payload(v, max_string=max_string) for k, v in value.items()}
    if isinstance(value, list):
        return [compact_provider_payload(v, max_string=max_string) for v in value]
    if isinstance(value, str):
        if ";base64," in value[:80]:
            prefix, encoded = value.split(";base64,", 1)
            if encoded.startswith(_OMITTED_MARK):
                return value
            digest = hashlib.sha256(encoded.encode("ascii", "replace")).hexdigest()[:16]
            return f"{prefix};base64,{_OMITTED_MARK}{len(value)} chars sha256:{digest}]"
        if len(value) > max_string:
            return f"{value[:max_string]}\n...[truncated {len(value) - max_string} chars]"
    return value


def compact_exact_request(value: Any) -> Any:
    """幂等核对用的形态：只换掉 base64，不截断任何文本。"""
    return compact_provider_payload(value, max_string=10**9)
