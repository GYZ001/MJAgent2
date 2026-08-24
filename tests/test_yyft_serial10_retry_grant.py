"""scripts/yyft_serial10.py 的 GEN-RETRY-GRANT 自动恢复覆盖。

背景：EP1→EP10 严格串行回归里，EP7（ep_621d93ac1231 / run_7143e7e04ab9）在后端
被另一会话重启时打断了一次流式蓝图分片调用（provider_calls id=8207，
INTERRUPTED，received_chars=0），领域层的 requires_fresh_retry_grant 门禁
（app/stages.py）正确地安全拦截，把episode 判成 failed 并落一条
error_logs.category=='generation_retry_grant' 的记录（ERR-20260823-62f248）。
门禁本身及其两步确认自愈路径（POST /screenplay -> 202 + approval_token ->
带 token 二次确认 -> 签发新 Production Grant）已经由
tests/test_screenplay_controls.py::
test_confirmed_unknown_retry_crosses_handler_api_facade_and_mints_grant 等
覆盖，本文件不重复验证门禁语义，只验证新增的驱动脚本分类器与重试循环：
  1. is_retry_grant_recoverable 只在「客观证据吻合」时放行（该放行的放行）；
  2. 面对同一时间窗内的其它失败类别、其它剧集、过期证据时仍然拦下
     （该拦的仍然拦）；
  3. cmd_run 的循环把该分类接到既有的两步确认重试上，且有上限，
     不会把真实故障（一直复现）也悄悄吃掉。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import db
from scripts import yyft_serial10


# 以下三段字面量摘自真实数据库（只读查询，未做任何手工改状态）：
#   provider_calls id=8207：EP7 被打断的蓝图分片调用。
#   provider_calls id=8208：同一时间窗内、另一个无关会话/进程的调用
#     （无 stage_key、无 episode_id、无 run_id ——与 EP7 的生成完全无关，
#     只是恰好 id 相邻、恰好在失败快照时刻处于非 OK 状态，被
#     recent_failure_evidence 的粗粒度证据查询捞了进来）。
#   error_logs id=ERR-20260823-62f248：门禁拦截时落的那条 GEN-RETRY-GRANT 记录。
CALL_8207 = {
    "id": 8207,
    "ts": 1787541419.2491128,
    "kind": "chat",
    "model": "d8p318cv256o70qpgv90",
    "status": "INTERRUPTED",
    "latency_ms": 16013,
    "error": "流式请求被取消，供应商结果未知 (latency_ms=16013, received_chars=0)",
    "meta": json.dumps({
        "episode_id": "ep_621d93ac1231",
        "run_id": "run_7143e7e04ab9",
        "stage_key": "screenplay_blueprint_shard",
        "requested_max_tokens": 13824,
        "effective_max_tokens": 30208,
    }, ensure_ascii=False),
    "run_id": "run_7143e7e04ab9",
    "operation_id": "blueprint_cf05a91972179fc67d1222f93c8752a1",
    "recovery_disposition": "REQUIRES_EXPLICIT_RETRY",
    "received_chars": 0,
}
CALL_8208 = {
    "id": 8208,
    "ts": 1787541487.4110277,
    "kind": "chat",
    "model": "d8p318cv256o70qpgv90",
    "status": "RUNNING",  # 在失败快照时刻观测到的真实状态；后来才结算为 OK。
    "latency_ms": None,
    "error": None,
    "meta": json.dumps({
        "caller_function": "main",
        "caller_module": "__main__",
        "requested_max_tokens": 16,
    }, ensure_ascii=False),
    "run_id": None,
    "operation_id": "op_39be72bfdac8da013bbebaddb115f07f",
    "recovery_disposition": None,
    "received_chars": 0,
}
ERROR_LOG_62F248 = {
    "id": "ERR-20260823-62f248",
    "ts": 1787541459.79072,
    "category": "generation_retry_grant",
    "category_label": "内容生成",
    "code": "GEN-RETRY-GRANT",
    "action": "screenplay_recovery_spawn",
    "context_json": json.dumps({
        "episode_id": "ep_621d93ac1231",
        "previous_run_id": "run_7143e7e04ab9",
    }, ensure_ascii=False),
    "message": (
        "[剧本时空因果蓝图分片] [BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] "
        "上次供应商结果未知；必须先签发新的 Production Grant"
    ),
}
EP7 = "ep_621d93ac1231"


def _insert_provider_call(conn, row: dict) -> None:
    conn.execute(
        """INSERT INTO provider_calls(
               id,ts,kind,model,status,latency_ms,error,meta,run_id,
               operation_id,recovery_disposition,received_chars
           ) VALUES(:id,:ts,:kind,:model,:status,:latency_ms,:error,:meta,
                     :run_id,:operation_id,:recovery_disposition,:received_chars)""",
        row,
    )


def _insert_error_log(conn, row: dict) -> None:
    conn.execute(
        """INSERT INTO error_logs(
               id,ts,category,category_label,code,action,context_json,message
           ) VALUES(:id,:ts,:category,:category_label,:code,:action,
                     :context_json,:message)""",
        row,
    )


@pytest.fixture()
def ep7_fixture_db(tmp_path, monkeypatch):
    """A standalone sqlite file (real app schema) seeded with EP7's real evidence."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ep7-fixture.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    _insert_provider_call(conn, CALL_8207)
    _insert_provider_call(conn, CALL_8208)
    _insert_error_log(conn, ERROR_LOG_62F248)
    conn.commit()
    return tmp_path / "ep7-fixture.db"


# ---------------------------------------------------------------------------
# 1) 分类器：该放行的放行 / 该拦的仍然拦
# ---------------------------------------------------------------------------

def test_recognizes_real_ep7_gen_retry_grant_evidence(ep7_fixture_db) -> None:
    """正面：真实 call 8207 + 真实 error_logs 记录必须被判定为可自动恢复。"""
    since = CALL_8207["ts"]
    assert yyft_serial10.is_retry_grant_recoverable(
        EP7, since, db_path=ep7_fixture_db,
    ) is True


def test_call_8208_noise_does_not_leak_into_a_different_episode(
    ep7_fixture_db,
) -> None:
    """反面：8208 是无关会话的调用（无 episode_id/stage_key），不构成任何剧集
    的 GEN-RETRY-GRANT 证据 —— 对 EP7 之外的任意剧集查询都必须是 False。"""
    since = CALL_8207["ts"]
    assert yyft_serial10.is_retry_grant_recoverable(
        "ep_some_other_episode", since, db_path=ep7_fixture_db,
    ) is False


def test_stale_evidence_before_the_attempt_window_is_not_reused(
    ep7_fixture_db,
) -> None:
    """反面：把 since 设到那条错误记录之后，代表「这是更早一次已处理过的失败」，
    不能被新一轮 start_or_resume 误当成刚发生的可恢复证据。"""
    since = ERROR_LOG_62F248["ts"] + 1.0
    assert yyft_serial10.is_retry_grant_recoverable(
        EP7, since, db_path=ep7_fixture_db,
    ) is False


def test_other_failure_category_for_the_same_episode_still_blocks(
    ep7_fixture_db,
) -> None:
    """反面：同一集在同一时间窗内，如果落的是别的失败类别（例如预算触顶的
    generation_budget，真实存在的场景，见 CATEGORIES['generation_budget']），
    绝不能被这条分类器当成 GEN-RETRY-GRANT 放过 —— 真实故障必须继续停下。"""
    conn = db.get_conn()
    monkeypatch_since = CALL_8207["ts"]
    conn.execute(
        """INSERT INTO error_logs(
               id,ts,category,category_label,code,action,context_json,message
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "ERR-20260823-budget01",
            ERROR_LOG_62F248["ts"],
            "generation_budget",
            "内容生成",
            "GEN-BUDGET",
            "screenplay_generate",
            json.dumps({"episode_id": EP7}, ensure_ascii=False),
            "[BLUEPRINT_GENERATION_CALL_BUDGET] 超过全局调用上限",
        ),
    )
    conn.commit()
    # 故意不删除真正的 GEN-RETRY-GRANT 记录：这条用例要证明的是分类器认
    # category 字段本身，而不是"这集是否出现过任何错误"。用一个干净的新库，
    # 只留预算错误，才是真正的"该拦"场景。
    conn.execute("DELETE FROM error_logs WHERE id=?", (ERROR_LOG_62F248["id"],))
    conn.commit()
    assert yyft_serial10.is_retry_grant_recoverable(
        EP7, monkeypatch_since, db_path=db.DB_PATH,
    ) is False


# ---------------------------------------------------------------------------
# 2) cmd_run 循环：接入既有两步确认重试、且有上限
# ---------------------------------------------------------------------------

def _prep_single_episode_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(yyft_serial10, "EPISODES", [("EP7", EP7)])
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    monkeypatch.setattr(
        yyft_serial10, "status_of",
        lambda eid: {"screenplay_status": "failed", "active": False},
    )
    monkeypatch.setattr(yyft_serial10, "is_rate_limited", lambda eid, since: False)
    monkeypatch.setattr(
        yyft_serial10, "recent_failure_evidence", lambda eid, since: "(evidence)",
    )


def test_cmd_run_self_heals_within_the_recovery_cap(monkeypatch, tmp_path) -> None:
    """正面：GEN-RETRY-GRANT 复现次数在上限之内时，串行回归不停整轮，
    而是像既有限流分支一样自动继续（这里是重新发起首版剧本，走既有两步确认）。"""
    _prep_single_episode_run(monkeypatch, tmp_path)
    starts = {"n": 0}

    def fake_start_or_resume(_name, _eid):
        starts["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "start_or_resume", fake_start_or_resume)
    monkeypatch.setattr(
        yyft_serial10, "is_retry_grant_recoverable", lambda eid, since: True,
    )
    terminal_calls = {"n": 0}

    def fake_await_terminal(_name, _eid, **_kwargs):
        terminal_calls["n"] += 1
        if terminal_calls["n"] <= yyft_serial10.RETRY_GRANT_MAX_AUTO_RECOVERIES:
            return {
                "screenplay_status": "failed",
                "screenplay_error": "GEN-RETRY-GRANT",
            }
        return {"screenplay_status": "ready"}

    monkeypatch.setattr(yyft_serial10, "await_terminal", fake_await_terminal)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 0
    assert starts["n"] == yyft_serial10.RETRY_GRANT_MAX_AUTO_RECOVERIES + 1
    assert terminal_calls["n"] == yyft_serial10.RETRY_GRANT_MAX_AUTO_RECOVERIES + 1


def test_cmd_run_stops_for_rca_once_the_cap_is_exhausted(
    monkeypatch, tmp_path,
) -> None:
    """反面：如果 GEN-RETRY-GRANT 一直复现（真实故障，不是一次性重启），
    自动恢复必须有底线，超过上限仍然按非限流失败停下整轮、留证据给人。"""
    _prep_single_episode_run(monkeypatch, tmp_path)
    starts = {"n": 0}

    def fake_start_or_resume(_name, _eid):
        starts["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "start_or_resume", fake_start_or_resume)
    monkeypatch.setattr(
        yyft_serial10, "is_retry_grant_recoverable", lambda eid, since: True,
    )
    monkeypatch.setattr(
        yyft_serial10, "await_terminal",
        lambda _name, _eid, **_kwargs: {
            "screenplay_status": "failed",
            "screenplay_error": "GEN-RETRY-GRANT",
        },
    )

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 4
    # 上限次自动恢复 + 最初一次尝试 = 总共 cap+1 次 start_or_resume。
    assert starts["n"] == yyft_serial10.RETRY_GRANT_MAX_AUTO_RECOVERIES + 1


def test_cmd_run_does_not_auto_recover_a_non_retry_grant_failure(
    monkeypatch, tmp_path,
) -> None:
    """反面：分类器判定不是 GEN-RETRY-GRANT 时（例如真实的 schema/内容错误），
    必须走原有的立即停止路径，一次都不自动重试。"""
    _prep_single_episode_run(monkeypatch, tmp_path)
    starts = {"n": 0}

    def fake_start_or_resume(_name, _eid):
        starts["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "start_or_resume", fake_start_or_resume)
    monkeypatch.setattr(
        yyft_serial10, "is_retry_grant_recoverable", lambda eid, since: False,
    )
    monkeypatch.setattr(
        yyft_serial10, "await_terminal",
        lambda _name, _eid, **_kwargs: {
            "screenplay_status": "failed",
            "screenplay_error": "JSON 解析失败",
        },
    )

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 4
    assert starts["n"] == 1
