"""跨集视频花费快照（历史遗留台账估算，仅供审计只读展示）。

金额不再构成生成拦截（会员分档时长制，非按金额计费）：本文件曾经的上限计算
与增量/绝对授权函数（``authorize_episode_video_budget_increment``/
``authorize_episode_video_budget_absolute``/``_episode_video_budget_floor``/
``_apply_video_budget_retry_margin``/``preview_episode_video_budget_authorization_cap``/
``episode_video_completion_budget_requirement``）在消费者清零后已整体删除——见
CLAUDE.md「Retiring Features」与本次「成本预算拦截体系退场」。
``project_video_budget_snapshot`` 保留：它是纯只读的历史台账聚合，仍有测试
（``tests/test_db_migration.py``、``tests/test_legacy_video_liability_migration.py``
等，不归本轮改动）依赖其返回形状，且不参与任何拦截判断。
"""
from __future__ import annotations


from app.completion_grant.ledger import (
    _historical_video_liability,
    ensure_video_budget_authority_tables,
)


def project_video_budget_snapshot(project_id: str, *, conn) -> dict[str, float]:
    """Aggregate durable provider liability across every episode in a project.

    Claim release is the accounting boundary. Job and version outcomes only
    describe execution; they cannot return capacity after a provider call may
    have incurred a charge. Episodes without an authority row use the legacy
    liability estimator until their first grant freezes that amount as baseline.
    """
    db = conn
    ensure_video_budget_authority_tables(db)
    episodes = db.execute(
        """SELECT e.id,a.baseline_cny
             FROM episodes e
             LEFT JOIN episode_video_budget_authorities a ON a.episode_id=e.id
            WHERE e.project_id=?""",
        (project_id,),
    ).fetchall()
    baseline = 0.0
    legacy = 0.0
    for row in episodes:
        if row["baseline_cny"] is None:
            legacy += _historical_video_liability(str(row["id"]), conn=db)
        else:
            baseline += float(row["baseline_cny"] or 0)
    claimed = float(db.execute(
        """SELECT COALESCE(SUM(c.amount_cny),0) AS amount
             FROM provider_video_budget_claims c
            WHERE c.project_id=? AND c.status!='released'""",
        (project_id,),
    ).fetchone()["amount"] or 0)
    used = baseline + legacy + claimed
    return {
        "baseline_cny": round(baseline, 6),
        "legacy_cny": round(legacy, 6),
        "claimed_cny": round(claimed, 6),
        "used_cny": round(used, 6),
    }
