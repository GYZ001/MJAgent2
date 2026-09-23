"""``app/main.py`` 全局 ``Exception`` 处理器（``_on_unhandled``）：未经命令总线、
路由自己也没捕获的 ``ValueError`` 必须转成 409 并保留原中文 message。

2026-09-23 用户拍板：全局统一把 ValueError 转成 409，口径对齐命令总线
``app.capabilities.handlers.common.call_guarded`` 的 ``except ValueError`` ->
``invalid_state``(409)。修复前 ``_on_unhandled`` 把 ``http_status`` 写死 500，
``errors.classify()`` 落到 ``_FALLBACK="system"``，公开文案被替换成「系统内部
…服务器内部错误」，领域层写好的中文原因只进日志——用户看到的是一句和真实原因
无关的话。

本文件覆盖的是 *全局兜底* 这一档，不是某条路由自己的 try/except（那一档已由
``tests/test_route_value_error_status.py`` 覆盖）：用一个不经命令总线、也不
自行捕获异常的裸路由直接触发 ``_on_unhandled``。

test_app 是独立的最小 FastAPI 实例，只挂 ``app.main`` 真正线上用的异常处理器
函数对象本身（不是重新实现一遍逻辑），断言的就是线上那份代码。之所以不直接复
用 ``app.main.app`` 单例：该单例在模块末尾 ``app.mount("/", SpaStaticFiles(...))``
挂了一个吃光所有未匹配路径的 SPA 兜底，测试期追加的路由排在它后面、永远匹配不
到（同一个坑 ``tests/test_novel_import_approval.py`` 的 ``client`` fixture也是
这样绕开的）。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config as app_config
from app import db
from app import main as app_main
from app.main import _on_http_exception, _on_request_validation, _on_unhandled

_VALUE_ERROR_MESSAGE = "测试_全局未捕获_业务规则冲突_不可重复登记同一别名"
_OTHER_ERROR_MESSAGE = "测试_全局未捕获_非ValueError_不应变成409"
_ROLLBACK_PROBE_ID = "test_global_409_rollback_probe"


@pytest.fixture
def client():
    """独立最小 app：只挂 app.main 真正的三个异常处理器函数对象。"""
    test_app = FastAPI()
    test_app.add_exception_handler(RequestValidationError, _on_request_validation)
    test_app.add_exception_handler(StarletteHTTPException, _on_http_exception)
    test_app.add_exception_handler(Exception, _on_unhandled)

    @test_app.get("/__test__/value-error")
    def _raise_value_error():
        raise ValueError(_VALUE_ERROR_MESSAGE)

    @test_app.get("/__test__/other-error")
    def _raise_other_error():
        raise RuntimeError(_OTHER_ERROR_MESSAGE)

    @test_app.get("/__test__/dangling-write-value-error")
    async def _raise_after_dangling_write():
        # async def：与 _on_unhandled 共用同一个任务局部连接（get_conn() 的
        # task/thread 局部性见 app/db.py），sync def 路由体会跑在线程池的另一
        # 条线程里，get_conn() 会解析到不同连接，够不成这条回滚顺序的验证。
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO projects(id,name,created_at) VALUES(?,?,0)",
            (_ROLLBACK_PROBE_ID, "X"),
        )
        raise ValueError(_VALUE_ERROR_MESSAGE)

    # raise_server_exceptions=False：注册在 Exception/500 上的处理器由 Starlette
    # 的 ServerErrorMiddleware 承接，它在调用完 handler、正常发出响应之后仍会
    # `raise exc`（源码原话：方便测试客户端按需重新抛出）——默认 True 会让这里
    # 的 client.get() 直接把原始异常炸给测试进程，看不到 handler 产出的响应。
    with TestClient(test_app, raise_server_exceptions=False) as test_client:
        yield test_client


def _error_log_row(error_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(app_config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM error_logs WHERE id=?", (error_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None, "error_logs 里应该有这条记录"
    return row


def test_unhandled_value_error_becomes_409_with_original_chinese_message(client) -> None:
    resp = client.get("/__test__/value-error")

    assert resp.status_code == 409
    body = resp.json()
    assert _VALUE_ERROR_MESSAGE in body["detail"]
    # 与命令总线 call_guarded 的 invalid_state->409 同一落点（errors.classify
    # 里 http_status==409 分支产出的既有分类，不是新造的）。
    assert body["code"] == "CON-409"
    assert body["category"] == "状态冲突"
    error_id = body["error_id"]

    row = _error_log_row(error_id)
    assert row["http_status"] == 409
    assert row["category"] == "conflict"
    assert row["code"] == "CON-409"
    assert row["is_technical"] == 0  # 非技术类：公开文案=原始中文，不脱敏
    assert row["message"] == _VALUE_ERROR_MESSAGE
    assert row["exc_type"] == "ValueError"
    assert row["traceback"]  # 技术细节（堆栈）仍要留痕，便于诊断


def test_unhandled_non_value_error_stays_500_with_generic_message(client) -> None:
    """反向：非 ValueError 的未处理异常必须仍是 500，公开文案仍是「系统内部」
    ——证明这次改动没有把兜底路径整体放宽。"""
    resp = client.get("/__test__/other-error")

    assert resp.status_code == 500
    body = resp.json()
    assert "系统内部" in body["detail"]
    assert _OTHER_ERROR_MESSAGE not in body["detail"]  # 技术类原文不进公开文案
    assert body["code"] == "SYS"
    error_id = body["error_id"]

    row = _error_log_row(error_id)
    assert row["http_status"] == 500
    assert row["category"] == "system"
    assert row["message"] == _OTHER_ERROR_MESSAGE  # 原文仍完整落库供排查
    assert row["exc_type"] == "RuntimeError"


def test_unhandled_value_error_rolls_back_dangling_write_before_log_error(
    client, monkeypatch,
) -> None:
    """CLAUDE.md：回滚必须是异常处理器的第一条语句，排在任何日志/记录调用
    之前。用 spy 包一层 errors.log_error，断言它被调用时 get_conn() 的事务
    已经被撤销——这是确定性的调用顺序验证，不依赖锁竞争之类的计时假设。
    """
    observed: dict[str, bool] = {}
    original_log_error = app_main.errors.log_error

    def _spy_log_error(exc, **kwargs):
        observed["in_transaction_at_log_time"] = db.get_conn().in_transaction
        return original_log_error(exc, **kwargs)

    # app/errors.py 是单文件模块（不是拆包），app.main 用 `from app import
    # errors` 绑的是同一个模块对象，属性打桩在这里生效即代表处处生效。
    monkeypatch.setattr(app_main.errors, "log_error", _spy_log_error)

    resp = client.get("/__test__/dangling-write-value-error")

    assert resp.status_code == 409
    assert observed.get("in_transaction_at_log_time") is False

    # 独立连接核验半途写入确实没有落库，不是同连接读自己未提交的写入产生的
    # 错觉（同连接会"看得见"，独立连接才是可信证据）。
    independent = sqlite3.connect(app_config.DB_PATH)
    try:
        count = independent.execute(
            "SELECT COUNT(*) FROM projects WHERE id=?", (_ROLLBACK_PROBE_ID,),
        ).fetchone()[0]
    finally:
        independent.close()
    assert count == 0
