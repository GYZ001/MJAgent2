"""scripts/yyft_serial10.py 的 GEN-RETRY-GRANT 分类覆盖。

背景：EP1→EP10 严格串行回归里，EP7（ep_621d93ac1231 / run_7143e7e04ab9）在后端
被另一会话重启时打断了一次流式蓝图分片调用（provider_calls id=8207，
INTERRUPTED，received_chars=0），领域层的 requires_fresh_retry_grant 门禁
（app/stages.py）正确地安全拦截，把episode 判成 failed 并落一条
error_logs.category=='generation_retry_grant' 的记录（ERR-20260823-62f248）。
门禁本身及其两步确认自愈路径（POST /screenplay -> 202 + approval_token ->
带 token 二次确认 -> 签发新 Production Grant）已经由
tests/test_screenplay_controls.py::
test_confirmed_unknown_retry_crosses_handler_api_facade_and_mints_grant 等
覆盖，本文件不重复验证门禁语义。

【2026-08-24 更新】上面这次真实事故打断的是 `screenplay_blueprint_shard`
（旧的重型「蓝图→场次分片→编译→修复回路」管线的 stage_key）。剧本台已改造为
轻量 episode_prep_pack 流程后，领域层的 `requires_fresh_retry_grant` 门禁
（app/stages.py:_BlueprintGenerationBudget，query 见 app/stages.py:7372-7374）
只认 `screenplay_blueprint_shard`/`_patch`/`_review` 三个旧 stage_key，
app/production/prep_pack.py 从不写这三个 key、也从不抛
`BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED`——在当前后端下这条门禁对新管线的
生成活动已结构性不可达（实测：对本项目 10 集调用
`_screenplay_blueprint_budget_projection` 全部 `requires_fresh_retry_grant=
False`、`revision=None`）。scripts/yyft_serial10.py 因此不再自动恢复这一类
失败——继续假装"自动重新发起首版剧本就能自愈"是在保护一条走不到的分支。
本文件相应更新：
  1. `is_retry_grant_category`（原 `is_retry_grant_recoverable`）只在「客观
     证据吻合」时命中（该识别的识别）；
  2. 面对同一时间窗内的其它失败类别、其它剧集、过期证据时仍然不命中
     （该拦的仍然拦——这部分证据判定逻辑本身未变，仍然值得测）；
  3. `cmd_run` 命中这一类别时不再进自动重试循环，直接按非限流失败停下
     （原来的「循环内自愈、超上限才停」两条用例被下面单条
     `test_cmd_run_stops_immediately_without_a_retry_loop` 取代）。
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
    assert yyft_serial10.is_retry_grant_category(
        EP7, since, db_path=ep7_fixture_db,
    ) is True


def test_call_8208_noise_does_not_leak_into_a_different_episode(
    ep7_fixture_db,
) -> None:
    """反面：8208 是无关会话的调用（无 episode_id/stage_key），不构成任何剧集
    的 GEN-RETRY-GRANT 证据 —— 对 EP7 之外的任意剧集查询都必须是 False。"""
    since = CALL_8207["ts"]
    assert yyft_serial10.is_retry_grant_category(
        "ep_some_other_episode", since, db_path=ep7_fixture_db,
    ) is False


def test_stale_evidence_before_the_attempt_window_is_not_reused(
    ep7_fixture_db,
) -> None:
    """反面：把 since 设到那条错误记录之后，代表「这是更早一次已处理过的失败」，
    不能被新一轮 start_or_resume 误当成刚发生的可恢复证据。"""
    since = ERROR_LOG_62F248["ts"] + 1.0
    assert yyft_serial10.is_retry_grant_category(
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
    assert yyft_serial10.is_retry_grant_category(
        EP7, monkeypatch_since, db_path=db.DB_PATH,
    ) is False


# ---------------------------------------------------------------------------
# 2) cmd_run：命中该分类不再自动重试，直接按非限流失败停下
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


def test_cmd_run_stops_immediately_without_a_retry_loop(
    monkeypatch, tmp_path,
) -> None:
    """GEN-RETRY-GRANT 命中时不再自动重试（2026-08-24 起该分支在
    episode_prep_pack-only 后端下结构性不可达，见模块 docstring）：cmd_run
    只标注一条诊断日志，然后照常按非限流失败停下整轮——只调用一次
    start_or_resume，不循环、不设自动恢复上限。"""
    _prep_single_episode_run(monkeypatch, tmp_path)
    starts = {"n": 0}

    def fake_start_or_resume(_name, _eid):
        starts["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "start_or_resume", fake_start_or_resume)
    monkeypatch.setattr(
        yyft_serial10, "is_retry_grant_category", lambda eid, since: True,
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
    assert starts["n"] == 1


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
        yyft_serial10, "is_retry_grant_category", lambda eid, since: False,
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
