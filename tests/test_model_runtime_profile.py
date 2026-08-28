"""模型运行画像：思考预留必须跟着**是哪个模型在跑**走。

生产根因（分镜台整集失败两次）：思考预留是一个全局常量 16384，而它服务的两
个模型行为相反——火山 seed 的 2787 次 chat 调用 ``reasoning_tokens`` 全为 0，
智谱 ``glm-5.3-flash`` 在分镜台阶段二稳定思考 30417~30839。常量对前者纯属浪
费，对后者差了近一倍，于是答案只剩 367 token，``finish_reason=length`` 整集
失败。这些用例锁的是「参数从该模型自己的观测推导」这条性质，不是某个具体
数值。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import config, hiagent, model_runtime_profile
from app.model_runtime_profile import (
    MIN_OBSERVATIONS,
    OBSERVATION_WINDOW_S,
    model_runtime_profile as load_profile,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    reset_cache()
    yield
    reset_cache()


def _conn_with_calls(rows: list[dict]) -> sqlite3.Connection:
    """建一个只含本用例所需列的 provider_calls，用真实 SQL 跑聚合。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE provider_calls(
               model TEXT, kind TEXT, ts REAL,
               first_chunk_at REAL, response_json TEXT)"""
    )
    conn.executemany(
        "INSERT INTO provider_calls(model, kind, ts, first_chunk_at, response_json) "
        "VALUES(:model, :kind, :ts, :first_chunk_at, :response_json)",
        rows,
    )
    conn.commit()
    return conn


def _call(
    *,
    model: str,
    reasoning: int | None = None,
    ts: float,
    first_chunk_at: float | None = None,
) -> dict:
    payload = "null"
    if reasoning is not None:
        payload = (
            '{"usage": {"completion_tokens_details": {"reasoning_tokens": %d}}}'
            % reasoning
        )
    return {
        "model": model,
        "kind": "chat",
        "ts": ts,
        "first_chunk_at": first_chunk_at,
        "response_json": payload,
    }


def _install(monkeypatch, rows: list[dict]) -> None:
    conn = _conn_with_calls(rows)
    monkeypatch.setattr(model_runtime_profile, "get_conn", lambda: conn)


def test_thinking_model_ceiling_comes_from_its_own_observations(monkeypatch, ):
    """观测到的思考上界必须体现在画像里，而不是被全局常量盖住。"""
    import time

    now = time.time()
    rows = [
        _call(model="glm-5.3-flash", reasoning=336, ts=now - 100)
        for _ in range(MIN_OBSERVATIONS)
    ]
    rows.append(_call(model="glm-5.3-flash", reasoning=30839, ts=now - 50))
    _install(monkeypatch, rows)

    profile = load_profile("glm-5.3-flash")
    assert profile.reasoning_ceiling == 30839


def test_reserve_follows_the_observed_ceiling(monkeypatch):
    """核心回归：预留必须覆盖该模型实测的思考量。

    修复前这里返回的是 16384（全局常量），30839 的思考把答案挤到只剩几百
    token，正是分镜台整集失败的直接原因。
    """
    import time

    now = time.time()
    rows = [
        _call(model="glm-5.3-flash", reasoning=30839, ts=now - 100)
        for _ in range(MIN_OBSERVATIONS)
    ]
    _install(monkeypatch, rows)
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "")

    assert hiagent.reasoning_token_reserve(model="glm-5.3-flash") >= 30839


def test_sparse_observations_fall_back_instead_of_claiming_zero(monkeypatch):
    """观测不足不等于「这个模型不思考」，必须回落全局默认。

    「空集合不等于无需检查」：样本少时若直接返回 0，预留会被悄悄取消，
    比没有画像更危险。
    """
    import time

    now = time.time()
    rows = [
        _call(model="fresh-model", reasoning=5, ts=now - 10)
        for _ in range(MIN_OBSERVATIONS - 1)
    ]
    _install(monkeypatch, rows)
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "")

    assert load_profile("fresh-model").reasoning_ceiling is None
    assert hiagent.reasoning_token_reserve(model="fresh-model") == (
        config.TEXT_REASONING_TOKEN_RESERVE
    )


def test_light_recent_traffic_never_tightens_the_reserve(monkeypatch):
    """近期只跑轻任务的模型，预留不得被画像收得比全局默认还紧。"""
    import time

    now = time.time()
    rows = [
        _call(model="quiet-model", reasoning=12, ts=now - 10)
        for _ in range(MIN_OBSERVATIONS + 5)
    ]
    _install(monkeypatch, rows)
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "")

    assert load_profile("quiet-model").reasoning_ceiling == 12
    assert hiagent.reasoning_token_reserve(model="quiet-model") == (
        config.TEXT_REASONING_TOKEN_RESERVE
    )


def test_model_that_never_thinks_gets_no_reserve(monkeypatch):
    """次次观测到 0 是「它不思考」的证据，预留必须真的降到 0。

    与上一条的区别不在数值高低而在证据性质：``reasoning=12`` 说明模型会思考、
    只是近期任务轻，收紧预留是拿下一次重任务冒险；而火山 seed 的 2799 次调用
    里供应商**逐次**回报思考了 0 token，此时仍留 16384，等于把它 32768 输出
    上限的一半直接扔掉——这是每次调用都在发生的确定损失。

    注意字段缺失走的是另一条路：那时 json_extract 给 NULL，样本压根进不了
    统计，观测数不足自然回落全局默认（见 sparse 用例）。
    """
    import time

    now = time.time()
    rows = [
        _call(model="d71l5c8nfdb167kligqg", reasoning=0, ts=now - 10)
        for _ in range(MIN_OBSERVATIONS + 5)
    ]
    _install(monkeypatch, rows)
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "")

    assert load_profile("d71l5c8nfdb167kligqg").reasoning_ceiling == 0
    assert hiagent.reasoning_token_reserve(model="d71l5c8nfdb167kligqg") == 0


def test_one_thinking_call_is_enough_to_restore_the_reserve(monkeypatch):
    """只要有一次真的思考过，就不算「不思考的模型」，预留回到全局默认兜底。

    这条守的是上一条的边界：把预留降到 0 的依据必须是「无一例外」，而不是
    「大多数时候是 0」。思考模型被大量关思考调用刷屏时，任何一次未关思考的
    观测都会让判据翻回保守侧。
    """
    import time

    now = time.time()
    rows = [
        _call(model="mostly-quiet", reasoning=0, ts=now - 10)
        for _ in range(MIN_OBSERVATIONS + 5)
    ]
    rows.append(_call(model="mostly-quiet", reasoning=7, ts=now - 5))
    _install(monkeypatch, rows)
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "")

    assert load_profile("mostly-quiet").reasoning_ceiling == 7
    assert hiagent.reasoning_token_reserve(model="mostly-quiet") == (
        config.TEXT_REASONING_TOKEN_RESERVE
    )


def test_operator_override_still_wins_over_observations(monkeypatch):
    """运维显式设定的预留优先级高于画像，否则就没法临时压制异常观测。"""
    import time

    now = time.time()
    rows = [
        _call(model="glm-5.3-flash", reasoning=30839, ts=now - 10)
        for _ in range(MIN_OBSERVATIONS)
    ]
    _install(monkeypatch, rows)
    monkeypatch.setattr(
        hiagent,
        "get_setting",
        lambda key: "2048" if key == "text_reasoning_token_reserve" else "",
    )

    assert hiagent.reasoning_token_reserve(model="glm-5.3-flash") == 2048


def test_stale_observations_outside_the_window_are_ignored(monkeypatch):
    """供应商会换后端、调思考策略，窗口外的旧观测不代表今天的它。"""
    import time

    now = time.time()
    rows = [
        _call(model="glm-5.3-flash", reasoning=30839, ts=now - OBSERVATION_WINDOW_S - 100)
        for _ in range(MIN_OBSERVATIONS + 5)
    ]
    _install(monkeypatch, rows)

    assert load_profile("glm-5.3-flash").reasoning_ceiling is None


def test_first_token_ceiling_resists_a_single_long_tail(monkeypatch):
    """首字上界取 p99：它会变成等待超时，一次异常不该拖住所有调用。

    与思考预留刻意相反——预留取观测最大值（高了不花钱，低了直接截断），
    首字上界取 p99（高了会让真卡死的调用干等）。
    """
    import time

    now = time.time()
    rows = [
        _call(model="slow-start", ts=now - 1000 + i, first_chunk_at=now - 1000 + i + 4.0)
        for i in range(MIN_OBSERVATIONS + 70)
    ]
    rows.append(_call(model="slow-start", ts=now - 10, first_chunk_at=now - 10 + 241.0))
    _install(monkeypatch, rows)

    ceiling = load_profile("slow-start").first_token_ceiling_s
    assert ceiling is not None
    assert ceiling < 241.0


def test_missing_model_argument_is_a_type_error():
    """漏传 model 必须当场炸，不能静默用全局默认糊过去。

    这个参数是最容易判错的那一个：seed 与 glm 的正确预留差了近两万 token，
    「不写就默认」等于把 bug 藏进调用点。
    """
    with pytest.raises(TypeError):
        hiagent.reasoning_token_reserve()  # type: ignore[call-arg]


def test_profile_is_cached_so_the_hot_path_does_not_re_aggregate(monkeypatch):
    """每次 chat 调用都去聚合一遍 provider_calls 是不可接受的热路径开销。"""
    import time

    now = time.time()
    rows = [
        _call(model="glm-5.3-flash", reasoning=1000, ts=now - 10)
        for _ in range(MIN_OBSERVATIONS)
    ]
    conn = _conn_with_calls(rows)
    calls = {"n": 0}

    def _counting_conn():
        calls["n"] += 1
        return conn

    monkeypatch.setattr(model_runtime_profile, "get_conn", _counting_conn)

    load_profile("glm-5.3-flash")
    load_profile("glm-5.3-flash")
    load_profile("glm-5.3-flash")
    assert calls["n"] == 1


def test_unreadable_observations_fall_back_rather_than_break_generation(monkeypatch):
    """画像是优化手段，读不到观测就回落默认，绝不能因此让生成失败。"""
    def _boom():
        raise sqlite3.OperationalError("no such table: provider_calls")

    monkeypatch.setattr(model_runtime_profile, "get_conn", _boom)
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "")

    assert load_profile("glm-5.3-flash").reasoning_ceiling is None
    assert hiagent.reasoning_token_reserve(model="glm-5.3-flash") == (
        config.TEXT_REASONING_TOKEN_RESERVE
    )
