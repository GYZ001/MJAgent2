"""dict 型 HTTPException.detail 必须优先取 "message" 键，不能把 Python dict
的 repr 糊给用户看。

回归背景：额度用尽时 ``app.quota.QuotaExceeded`` 构造
``detail={"code": ..., "message": "中文提示", ...}``（见 app/quota.py 94-100
行），经 ``app.errors._extract_message`` 处理后，此前对 dict 一律
``str(detail)``，用户在生成台批量生成失败 toast（``${failed[0].error}``，见
frontend/src/pages/WallPage.tsx）里看到的是
``"{'code': 'QUOTA_EXCEEDED_VIDEO_SECONDS', 'message': '...', ...}"`` 这样的
Python 对象打印，不是中文提示本身。仓库里手写 dict detail 的 HTTPException
（app/monitoring.py、app/orchestration/api.py、app/domain/bible_ops/
primitives.py 等 6 处）也统一约定带 "message" 字段。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.errors import _extract_message, classify, log_error

_QUOTA_DETAIL = {
    "code": "QUOTA_EXCEEDED_VIDEO_SECONDS",
    "gate": "video_seconds", "message": "30 天视频时长额度已用尽", "tier": "free",
    "limit": 900, "used": 900, "remaining": 0, "reset_at": None,
    "upgrade_path": "/pricing",
}


def test_extract_message_prefers_message_key_over_dict_repr():
    exc = HTTPException(status_code=429, detail=_QUOTA_DETAIL)
    assert _extract_message(exc) == "30 天视频时长额度已用尽"


def test_extract_message_falls_back_to_str_when_no_message_key():
    detail = {"code": "REVIEW_DEPENDENCY_STALE", "write_point": "y"}
    exc = HTTPException(status_code=409, detail=detail)
    assert _extract_message(exc) == str(detail)


def test_extract_message_falls_back_when_message_is_not_a_usable_string():
    detail = {"code": "X", "message": ""}
    exc = HTTPException(status_code=409, detail=detail)
    assert _extract_message(exc) == str(detail)


def test_extract_message_still_handles_plain_string_detail():
    exc = HTTPException(status_code=404, detail="未找到")
    assert _extract_message(exc) == "未找到"


def test_extract_message_handles_no_exception():
    assert _extract_message(None) == ""


def test_log_error_public_text_shows_chinese_message_not_dict_repr():
    exc = HTTPException(status_code=429, detail=_QUOTA_DETAIL)
    record = log_error(exc, persist=False)

    assert "30 天视频时长额度已用尽" in record.public
    assert "QUOTA_EXCEEDED_VIDEO_SECONDS" not in record.public
    assert "{'code'" not in record.public
    category, _code = classify(exc)
    assert category == "validation"  # 429 落 400<=status<500 分支，非技术类不脱敏
