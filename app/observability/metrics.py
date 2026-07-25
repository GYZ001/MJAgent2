"""VAL-422 可观测性：沿用 provider_calls，不引入 Prometheus。

调用 ``inc(name, **labels)`` 会写一条 ``kind=val422_metric`` 记录；
监控页 / 聚合端点可按 ``meta.metric`` 汇总。
"""
from __future__ import annotations

from typing import Any

from app import config
from app.db import log_provider_call


def inc(metric: str, *, value: int = 1, **labels: Any) -> None:
    """递增一个命名指标（写入 provider_calls，失败静默）。"""
    if not metric:
        return
    try:
        log_provider_call(
            "val422_metric",
            config.MODEL_TEXT,
            "COUNTER",
            None,
            0,
            meta={"metric": metric, "value": int(value), **labels},
        )
    except Exception:  # noqa: BLE001
        pass


def spoken_contract_audit_mode() -> str:
    """audit_only | enforce。默认 enforce（新数据强制一致）。"""
    from app.db import get_setting
    mode = (get_setting("spoken_contract_audit_mode") or "enforce").strip().lower()
    return mode if mode in {"audit_only", "enforce"} else "enforce"


def spine_structured_hard_gate() -> bool:
    """结构化 spine hard gate；关闭时 LEGACY_COVERAGE_UNCERTAIN 不阻断确认。"""
    from app.db import get_setting
    return (get_setting("spine_structured_hard_gate") or "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
