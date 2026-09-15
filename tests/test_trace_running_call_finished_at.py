"""链路视图里运行中的供应商调用节点不能带结束时间。

2026-09-15 用户在映射台等了八分钟：那次调用在供应商侧 450 秒内一个字节都没返回，
链路页却把它显示成「0ms」——调用节点的 finished_at 无条件写成 ts + latency_ms/1000，
而 latency_ms 运行中恒为 0。前端按 finished_at 为空判断未结束并实时显示已等待时长。
"""
from __future__ import annotations

from app.observability import api as observability_api

_STARTED = 1_789_461_296.0  # 2026-09-15 16:14:56 CST，第一次被读超时掐断的那次调用


def test_running_call_has_no_finished_at() -> None:
    assert observability_api._call_finished_at({"status": "RUNNING", "ts": _STARTED, "latency_ms": 0}) is None


def test_finished_call_ends_at_start_plus_latency() -> None:
    finished = observability_api._call_finished_at({"status": "OK", "ts": _STARTED, "latency_ms": 103_063})
    assert finished == _STARTED + 103.063


def test_interrupted_call_keeps_its_recorded_end() -> None:
    finished = observability_api._call_finished_at({"status": "INTERRUPTED", "ts": _STARTED, "latency_ms": 451_573})
    assert finished == _STARTED + 451.573


def test_missing_fields_do_not_crash() -> None:
    assert observability_api._call_finished_at({}) == 0.0
