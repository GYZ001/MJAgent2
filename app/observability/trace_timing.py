"""链路节点的时间推导：纯函数，零 app.* 依赖。

从 ``app.observability.api``（2,300+ 行、已在行数棘轮基线上）拆出，不再往那个文件里加代码。
"""
from __future__ import annotations

from typing import Any


def _call_finished_at(call: dict[str, Any]) -> float | None:
    """供应商调用节点的结束时间：仍在 RUNNING 的调用没有结束时间。

    此前无条件写 ts + latency_ms/1000，而 latency_ms 只有结束后才有值，运行中恒为 0，
    于是运行中的调用被当成「开始即结束」——2026-09-15 用户在映射台等了八分钟，链路
    页把那次 450 秒没返回一个字节的调用显示成「0ms」。前端按 finished_at 为空判断
    未结束并实时显示已等待时长，这里必须留空。
    """
    if str(call.get("status") or "") == "RUNNING":
        return None
    return float(call.get("ts") or 0) + float(call.get("latency_ms") or 0) / 1000


__all__ = ["_call_finished_at"]
