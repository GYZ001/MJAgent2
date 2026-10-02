"""concat_receipt_status.latest_receipt_outcome 与 episode_mix_status 的接线。

2026-10-01 对抗式复查 #0/#1：mix-status 的 concat_in_progress/concat_last_error 只读
进程内存（task_registry/concat_state），后端重启后两者都会清零，而成片台
localStorage 里的合成幂等键不会。若前端只看这两个内存字段判断"本轮合成是否完成"，
重启瞬间会把"旧任务已死、从未真正完成"误判成"已完成"。这里验证 mix-status 新增的
``concat_receipt`` 字段读的是持久化的 ``concat_operation_receipts.status``，不受
进程内存重置影响。
"""
from __future__ import annotations

import sqlite3

from app import db, worker
from app.media_exec import concat_receipt_status, concat_state
from tests.conftest import patch_worker_everywhere


def _db_with_episode(episode_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES(?,'p',1,'E','confirmed',0)",
        (episode_id,),
    )
    conn.commit()
    return conn


def test_latest_receipt_outcome_none_without_table() -> None:
    """表尚未建过（这一集从没发起过合成）时只读投影，不建表、不报错。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert concat_receipt_status.latest_receipt_outcome(conn, "e") is None


def test_latest_receipt_outcome_none_without_any_row() -> None:
    conn = _db_with_episode("e")
    assert concat_receipt_status.latest_receipt_outcome(conn, "e") is None


def test_latest_receipt_outcome_tracks_running_then_failed(monkeypatch) -> None:
    conn = _db_with_episode("e")
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    owner, replay = worker.claim_concat_operation(
        idempotency_key="k1", request_fingerprint="fp", episode_id="e",
        release_authority={"a": 1}, video_delivery_manifest={"m": 1},
    )
    assert replay is None and owner
    assert concat_receipt_status.latest_receipt_outcome(conn, "e") == {"status": "running", "error": None}

    worker.release_concat_operation(
        idempotency_key="k1", request_fingerprint="fp", claim_token=owner, reason="ffmpeg 失败",
    )
    assert concat_receipt_status.latest_receipt_outcome(conn, "e") == {"status": "failed", "error": "ffmpeg 失败"}


def test_mix_status_receipt_survives_in_memory_state_reset(monkeypatch) -> None:
    """模拟后端重启：claim 之后从不触碰 concat_state（它在真实重启后也会是空的），
    mix-status 的 concat_receipt 必须仍然诚实地反映持久化的 'running'，不能让
    concat_in_progress=false 看起来像"已完成"。"""
    episode_id = "e-receipt-restart"
    concat_state.clear_error(episode_id)
    conn = _db_with_episode(episode_id)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    owner, replay = worker.claim_concat_operation(
        idempotency_key="k2", request_fingerprint="fp", episode_id=episode_id,
        release_authority={"a": 1}, video_delivery_manifest={"m": 1},
    )
    assert replay is None and owner

    status = worker.episode_mix_status(episode_id)
    assert status["concat_in_progress"] is False, "新进程里没有真实任务在跑"
    assert status["concat_last_error"] is None, "进程内存重启后本就是空的"
    assert status["concat_receipt"] == {"status": "running", "error": None}, (
        "必须读持久化 receipt，不能因为内存字段都是假而把'说不清'误判成默认值"
    )


def test_mix_status_receipt_reaches_succeeded(monkeypatch) -> None:
    episode_id = "e-receipt-succeeded"
    conn = _db_with_episode(episode_id)
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    conn.execute(
        """INSERT INTO concat_operation_receipts(
               operation_key,command,request_fingerprint,episode_id,status,
               result_json,claim_token,lease_expires_at,created_at,updated_at
           ) VALUES('delivery.concatenate:k3','delivery.concatenate','fp',?,
                     'succeeded','{}','owner',0,0,1)""",
        (episode_id,),
    )
    conn.commit()
    status = worker.episode_mix_status(episode_id)
    assert status["concat_receipt"] == {"status": "succeeded", "error": None}
