"""网关 4xx + 结构化 error 信封 = 明确拒绝、任务未创建；create 不再被当成「结果不确定」。

2026-09-03 ep_eaa907515df8 第 1 镜：Seedance/HiAgent 对视频 create 回 HTTP 400
``{"error":{"message":"抱歉，该问题不符合安全合规要求…","type":"bad_request","code":"bad_request"}}``，
被判成 VIDEO_PROVIDER_CREATE_UNRESOLVED（「请核对供应商任务」——其实没有任务可核对）。
判定只看结构（状态码 + error 信封），不看正文写了什么词。
"""
from __future__ import annotations

import json

from app.hiagent import _classify_http_error
from app.media_exec.authority import _provider_create_outcome_unknown

REFUSAL = json.dumps({"error": {"message": "抱歉，该问题不符合安全合规要求，暂时无法回答",
                                "type": "bad_request", "code": "bad_request", "request_id": ""}}, ensure_ascii=False)


def test_structured_400_refusal_means_create_not_accepted():
    err = _classify_http_error(400, REFUSAL)
    assert err.create_not_accepted is True
    assert err.delivery_state == "responded"
    assert _provider_create_outcome_unknown(err) is False
    assert "不符合安全合规要求" in str(err)  # 供应商原话逐字透出


def test_timeout_and_conflict_statuses_stay_unknown():
    for status in (408, 409):
        err = _classify_http_error(status, REFUSAL)
        assert err.create_not_accepted is False
        assert _provider_create_outcome_unknown(err) is True


def test_unstructured_body_and_server_errors_fail_closed():
    assert _provider_create_outcome_unknown(_classify_http_error(400, "<html>bad gateway</html>")) is True
    assert _provider_create_outcome_unknown(_classify_http_error(400, json.dumps({"detail": "x"}))) is True
    assert _provider_create_outcome_unknown(_classify_http_error(502, REFUSAL)) is True
