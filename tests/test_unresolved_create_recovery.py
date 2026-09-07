"""重启打断 create 留下的 unresolved 残留：同镜已有活动作业的清掉，真需要重提的一条不动。

2026-09-07 部署重启实测：15 个视频作业停在 VIDEO_PROVIDER_CREATE_UNRESOLVED、跨 11 集。其中 6 条
的镜头已另有活动作业（纯残留），9 条的镜头真的没人在跑——后者要不要冒重复提交的风险由部署方定，
本模块不替任何人决定。
"""
from __future__ import annotations

import sqlite3

from app.media_exec import unresolved_create_recovery as ucr


def _conn(monkeypatch) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE jobs(id TEXT PRIMARY KEY, shot_id TEXT, kind TEXT, status TEXT, "
        "reason_code TEXT, error TEXT, updated_at REAL)"
    )
    rows = [
        # 镜 A：残留 + 同镜在跑 → 清理
        ("j_a_stuck", "shot_a", "video", "waiting_human", ucr.UNRESOLVED_REASON, None, 1.0),
        ("j_a_live", "shot_a", "video", "running", None, None, 2.0),
        # 镜 B：残留 + 同镜已成功 → 清理
        ("j_b_stuck", "shot_b", "video", "waiting_human", ucr.UNRESOLVED_REASON, None, 3.0),
        ("j_b_done", "shot_b", "video", "succeeded", None, None, 4.0),
        # 镜 C：只有这一条 → 不动，报告给调用方
        ("j_c_stuck", "shot_c", "video", "waiting_human", ucr.UNRESOLVED_REASON, None, 5.0),
        # 镜 D：残留 + 同镜也已失败（不算活动）→ 不动
        ("j_d_stuck", "shot_d", "video", "waiting_human", ucr.UNRESOLVED_REASON, None, 6.0),
        ("j_d_failed", "shot_d", "video", "failed", None, None, 7.0),
        # 别的原因停在 waiting_human 的不归本模块管
        ("j_e_other", "shot_e", "video", "waiting_human", "SOMETHING_ELSE", None, 8.0),
    ]
    conn.executemany("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    monkeypatch.setattr(ucr, "get_conn", lambda: conn)
    return conn


def _status(conn, job_id: str) -> str:
    return conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]


def test_only_redundant_leftovers_are_cleared(monkeypatch) -> None:
    conn = _conn(monkeypatch)
    assert ucr.resolve_redundant_unresolved_creates() == 2
    assert _status(conn, "j_a_stuck") == "abandoned" and _status(conn, "j_b_stuck") == "abandoned"
    assert _status(conn, "j_c_stuck") == "waiting_human"  # 镜头真没人在跑，不替部署方决定
    assert _status(conn, "j_d_stuck") == "waiting_human"  # failed 不算活动作业
    assert _status(conn, "j_e_other") == "waiting_human"  # 别的原因不归本模块
    assert ucr.resolve_redundant_unresolved_creates() == 0  # 幂等


def test_resubmittable_jobs_are_reported_not_touched(monkeypatch) -> None:
    conn = _conn(monkeypatch)
    assert sorted(ucr.resubmittable_unresolved_jobs()) == ["j_c_stuck", "j_d_stuck"]
    assert _status(conn, "j_c_stuck") == "waiting_human"
