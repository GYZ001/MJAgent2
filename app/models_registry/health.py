"""模型健康度：进程内滑动窗口 + 熔断状态机，定期落盘 ``model_health``。L2。

**为什么是"拉"不是"推"**：真实调用失败在 ``app/hiagent.py``（3130 行，行数
基线已顶满零余量，见 ``app/FILE_CONVENTIONS.toml``）里发生，把推送式健康度记录
塞进那条已经写满事故复盘注释的 ``chat()``/``_post_json()`` 重试循环，既没有
行数预算、也会给 EP-05 §11 已知陷阱 1 点名的"改共享底层原语必须跑全量"再添一处
高危改动面。好在 ``_post_json`` 已经把每次真实调用的结果**无条件**写进
``provider_calls``（``status`` ∈ OK/FAILED/TIMEOUT/INTERRUPTED/NETWORK_ERROR，
``http_status``），这份数据本身就是权威来源——本模块改为定期"拉"这张表、按
``model_ref`` 反查 ``models.id``、离线做分类与状态机推进，对 ``app/hiagent.py``
零改动。局限：若两条模型库条目共用同一个 ``model_ref`` 字符串（同一 kind 下
理论可能但生产数据未见），健康度会记到两者共享的桶里，不区分——已知、可接受
的边界情况，不是误判成"选错模型"，只是"归因粗一档"。

``call_with_failover``（见 ``routing.py``）额外走"推"路径立即更新同一份状态，
两条路径写同一个 ``_window_map()``，互相印证不冲突。

**落盘节流**：``refresh()`` 默认每 ≥5s 才真正扫表 + 落盘一次（``force=True``
绕开节流，供测试与 ``call_with_failover`` 后立即可见用），避免"每次调用都写库"
制造的写锁热点（CLAUDE.md 记录的整站冻结事故根因）。落盘用
``app.db._run_write_transaction_once``——独立连接、``BEGIN IMMEDIATE``，不占用
调用方正在持有的连接/事务。

**按 ``db.DB_PATH`` 分桶**：全部模块级状态字典都以 ``str(db.DB_PATH)`` 为第一层
key（与 ``app/models_registry/schema.py::_ensured_paths`` 同一手法），否则测试
间会共享同一份内存状态——两个测试各自的 ``provider_calls`` 都从 id=1 起跳，
上一个测试留下的"已看到 id=500"会让下一个测试的新表 1..50 全部被当成"看过"
而跳过。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app import db
from app.db import get_conn

from app.models_registry import schema

_MIN_SAMPLES = 5
_FAILURE_RATE_OPEN = 0.5
_FAILURE_RATE_DEGRADED = 0.2
_COOLDOWN_BASE_S = 60.0
_COOLDOWN_MAX_S = 600.0
_WINDOW_DECAY_AT = 50
_REFRESH_MIN_INTERVAL_S = 5.0
_SCAN_BATCH_LIMIT = 2000
_LATENCY_SAMPLE_CAP = 200


@dataclass
class _Window:
    state: str = "healthy"
    calls: int = 0
    failures: int = 0
    timeouts: int = 0
    rate_limited: int = 0
    opened_at: float | None = None
    half_open_at: float | None = None
    cooldown_s: float = _COOLDOWN_BASE_S
    last_error_code: str | None = None
    last_error_at: float | None = None
    latencies: list[int] = field(default_factory=list)


_STATE: dict[str, dict[str, _Window]] = {}
_LAST_SEEN_CALL_ID: dict[str, int] = {}
_LAST_REFRESH_AT: dict[str, float] = {}
_LAST_FLUSH_AT: dict[str, float] = {}
_DIRTY: dict[str, set[str]] = {}


def _db_key() -> str:
    return str(db.DB_PATH)


def _window_map() -> dict[str, _Window]:
    return _STATE.setdefault(_db_key(), {})


def _dirty_set() -> set[str]:
    return _DIRTY.setdefault(_db_key(), set())


def reset_for_tests(db_path: str | None = None) -> None:
    """测试专用：清空某个（默认当前）db_path 下的全部内存健康度状态。"""
    key = db_path if db_path is not None else _db_key()
    _STATE.pop(key, None)
    _LAST_SEEN_CALL_ID.pop(key, None)
    _LAST_REFRESH_AT.pop(key, None)
    _LAST_FLUSH_AT.pop(key, None)
    _DIRTY.pop(key, None)


def classify_call_status(status: str, http_status: int | None) -> str | None:
    """``provider_calls`` 一行的 (status, http_status) → 四类失败之一，或
    ``None``（成功/中间态，不计入健康度）。结构判据与
    ``app.hiagent._classify_http_error`` 用同一套状态码分段，不是关键词匹配。
    """
    upper = (status or "").upper()
    if upper in {"TIMEOUT", "INTERRUPTED", "NETWORK_ERROR"}:
        return "timeout"
    if upper != "FAILED":
        return None
    if http_status == 429:
        return "rate_limited"
    if http_status is not None and (http_status >= 500 or http_status in (401, 403)):
        return "server_error"
    if http_status is not None and 400 <= http_status < 500:
        return "content_rejected"
    return "server_error"


def effective_state(model_id: str) -> str:
    """当前状态，惰性把到期的 ``circuit_open`` 推进到 ``half_open``。"""
    win = _window_map().get(str(model_id or "").strip())
    if win is None:
        return "healthy"  # 从没见过调用：无证据说它坏，不拦
    if win.state == "circuit_open" and win.opened_at is not None:
        if time.time() - win.opened_at >= win.cooldown_s:
            win.state = "half_open"
            win.half_open_at = time.time()
            _dirty_set().add(str(model_id))
    return win.state


def is_selectable(model_id: str) -> bool:
    return effective_state(model_id) != "circuit_open"


def _decay_if_needed(win: _Window) -> None:
    if win.calls >= _WINDOW_DECAY_AT:
        win.calls //= 2
        win.failures //= 2
        win.timeouts //= 2
        win.rate_limited //= 2


def _apply_transition(win: _Window, success: bool) -> None:
    now_ts = time.time()
    if win.state == "half_open":
        if success:
            win.state, win.cooldown_s = "healthy", _COOLDOWN_BASE_S
            win.calls = win.failures = win.timeouts = win.rate_limited = 0
        else:
            win.state, win.opened_at = "circuit_open", now_ts
            win.cooldown_s = min(win.cooldown_s * 2, _COOLDOWN_MAX_S)
        return
    if win.state == "circuit_open" or win.calls < _MIN_SAMPLES:
        return
    rate = win.failures / win.calls
    if rate > _FAILURE_RATE_OPEN:
        win.state, win.opened_at = "circuit_open", now_ts
    elif rate > _FAILURE_RATE_DEGRADED:
        win.state = "degraded"
    else:
        win.state = "healthy"


def record_outcome(model_id: str, category: str | None, *, latency_ms: int | None = None) -> None:
    """``category`` 为 ``None`` 表示成功；否则是 timeout/rate_limited/
    server_error/content_rejected 之一。contract_invalid 类失败永远不会调用
    这个函数——它们在 ``_post_json`` 眼里是 status=OK 的成功传输，见模块文档。
    """
    model_id = str(model_id or "").strip()
    if not model_id:
        return
    win = _window_map().setdefault(model_id, _Window())
    _decay_if_needed(win)
    win.calls += 1
    if latency_ms is not None:
        win.latencies.append(int(latency_ms))
        if len(win.latencies) > _LATENCY_SAMPLE_CAP:
            del win.latencies[: len(win.latencies) - _LATENCY_SAMPLE_CAP]
    success = category is None
    if not success:
        win.failures += 1
        win.last_error_code, win.last_error_at = category, time.time()
        if category == "timeout":
            win.timeouts += 1
        elif category == "rate_limited":
            win.rate_limited += 1
    _dirty_set().add(model_id)
    _apply_transition(win, success)


def _model_ids_by_ref(model_refs: set[str]) -> dict[str, str]:
    if not model_refs:
        return {}
    placeholders = ",".join("?" for _ in model_refs)
    rows = get_conn().execute(
        f"SELECT id, model_ref FROM models WHERE model_ref IN ({placeholders})",
        tuple(model_refs),
    ).fetchall()
    mapping: dict[str, str] = {}
    for row in rows:
        mapping.setdefault(str(row["model_ref"]), str(row["id"]))
    return mapping


def _apply_call_rows(rows: list[Any]) -> None:
    refs = {str(row["model"] or "").strip() for row in rows}
    refs.discard("")
    ref_to_id = _model_ids_by_ref(refs)
    for row in rows:
        model_id = ref_to_id.get(str(row["model"] or "").strip())
        if not model_id:
            continue
        category = classify_call_status(row["status"], row["http_status"])
        record_outcome(model_id, category, latency_ms=row["latency_ms"])


def _percentiles(latencies: list[int]) -> tuple[int | None, int | None]:
    if not latencies:
        return None, None
    ordered = sorted(latencies)

    def pick(pct: float) -> int:
        return ordered[min(len(ordered) - 1, int(len(ordered) * pct))]

    return pick(0.5), pick(0.95)


def _flush() -> None:
    dirty = _dirty_set()
    if not dirty:
        return
    targets = list(dirty)
    dirty.clear()
    windows = _window_map()
    rows_to_write = []
    for model_id in targets:
        win = windows.get(model_id)
        if win is None:
            continue
        p50, p95 = _percentiles(win.latencies)
        rows_to_write.append((
            model_id, win.state, win.calls, win.failures, win.timeouts,
            win.rate_limited, p50, p95, win.opened_at, win.half_open_at,
            win.last_error_code, win.last_error_at, time.time(),
        ))
    if rows_to_write:
        _write_health_rows(rows_to_write)


def _write_health_rows(rows_to_write: list[tuple[Any, ...]]) -> None:
    def operation(conn: Any) -> None:
        conn.executemany(
            """INSERT INTO model_health
                   (model_id, state, window_calls, window_failures, window_timeouts,
                    window_rate_limited, p50_latency_ms, p95_latency_ms, opened_at,
                    half_open_at, last_error_code, last_error_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(model_id) DO UPDATE SET
                   state=excluded.state, window_calls=excluded.window_calls,
                   window_failures=excluded.window_failures,
                   window_timeouts=excluded.window_timeouts,
                   window_rate_limited=excluded.window_rate_limited,
                   p50_latency_ms=excluded.p50_latency_ms,
                   p95_latency_ms=excluded.p95_latency_ms,
                   opened_at=excluded.opened_at, half_open_at=excluded.half_open_at,
                   last_error_code=excluded.last_error_code,
                   last_error_at=excluded.last_error_at, updated_at=excluded.updated_at""",
            rows_to_write,
        )

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 落盘失败不阻塞选路；内存状态仍是最新，
        # 只是 model_health 表这一轮没更新，下一轮 flush 会带着累积状态重试。
        return


def refresh(*, force: bool = False) -> None:
    """扫描新的 ``provider_calls`` 行、推进状态机，节流落盘。"""
    key = _db_key()
    now_ts = time.time()
    if not force and now_ts - _LAST_REFRESH_AT.get(key, 0.0) < _REFRESH_MIN_INTERVAL_S:
        return
    _LAST_REFRESH_AT[key] = now_ts
    schema.ensure_schema()
    last_seen = _LAST_SEEN_CALL_ID.get(key, 0)
    rows = get_conn().execute(
        "SELECT id, model, status, http_status, latency_ms FROM provider_calls "
        "WHERE id > ? ORDER BY id LIMIT ?",
        (last_seen, _SCAN_BATCH_LIMIT),
    ).fetchall()
    if rows:
        _apply_call_rows(rows)
        _LAST_SEEN_CALL_ID[key] = max(int(row["id"]) for row in rows)
    if force or now_ts - _LAST_FLUSH_AT.get(key, 0.0) >= _REFRESH_MIN_INTERVAL_S:
        _flush()
        _LAST_FLUSH_AT[key] = now_ts
