"""单集视频预算授权：上限计算、增量/绝对授权与余额快照。
"""
from __future__ import annotations

import math


from app.db import now
from app.provider_task_clearance import (
    ProviderTasksNotTerminalError as ProviderTasksNotTerminalError,
    assert_provider_tasks_clearable as assert_provider_tasks_clearable,
    prepare_provider_tasks_for_clear as prepare_provider_tasks_for_clear,
)
from app.completion_grant.ledger import (
    _historical_video_liability,
    ensure_video_budget_authority_tables,
)
from app.completion_grant.models import (
    EPISODE_VIDEO_BUDGET_HARD_CAP_CNY,
    VIDEO_BUDGET_RETRY_MARGIN_MULTIPLIER,
)

def _episode_video_claimed(episode_id: str, *, conn) -> float:
    """扣款侧算作"已用"的那部分认领——与 ``reserve_provider_video_budget``
    里的 ``claimed`` 必须是同一个查询，两处漂移就会让新批的 cap 一诞生就低于
    已用额度。"""
    return float(conn.execute(
        """SELECT COALESCE(SUM(amount_cny),0) AS amount
             FROM provider_video_budget_claims
            WHERE episode_id=? AND status!='released'""",
        (episode_id,),
    ).fetchone()["amount"] or 0)


def _episode_video_budget_floor(episode_id: str, *, conn) -> tuple[float, float]:
    """已承诺的下限：返回 (baseline_cny, floor_cny)。floor 至少是
    baseline+已认领，有 authority 行时还不低于既有 cap。cap 永远不能低于
    floor，否则会撕毁已经产生的付款责任。

    两个分支都必须把 ``claimed`` 计进 floor。扣款侧一律按
    ``used = baseline + claimed`` 判断，而"没有 authority 行"的分支曾只返回
    ``baseline``——``_historical_video_liability`` 按设计只统计**没有 claim
    归属**的遗留责任（避免与 claimed 重复计），所以有认领时它正确地返回 0，
    于是 floor 也成了 0，新批的 cap 一诞生就低于已用额度：实测
    ``ep_0a70ec56e8e9`` 已有 96 元 settled 认领、authority 行缺失，新授权拿到
    cap=96 而 used 已是 96，之后每一次供应商调用都被判超限，8 个镜头全部
    ``paused_budget``，整集永久停在 WAITING_AUTHORIZATION。
    """
    current = conn.execute(
        "SELECT baseline_cny,cap_cny FROM episode_video_budget_authorities WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    claimed = _episode_video_claimed(episode_id, conn=conn)
    if current:
        baseline = float(current["baseline_cny"] or 0)
        return baseline, max(float(current["cap_cny"] or 0), baseline + claimed)
    baseline = _historical_video_liability(episode_id, conn=conn)
    return baseline, baseline + claimed


def _apply_video_budget_retry_margin(
    floor_cny: float,
    amount_cny: float,
    *,
    multiplier: float = VIDEO_BUDGET_RETRY_MARGIN_MULTIPLIER,
    hard_cap_cny: float = EPISODE_VIDEO_BUDGET_HARD_CAP_CNY,
) -> float:
    """把新批准的首轮金额按重试余量倍数计入 cap，再夹到单集硬上限——但
    永远不低于已经承诺的下限，硬上限只截「新增余量」，不撕毁既有责任。"""
    raw = floor_cny + max(0.0, float(amount_cny)) * multiplier
    return max(floor_cny, min(raw, hard_cap_cny))


def preview_episode_video_budget_authorization_cap(
    episode_id: str, amount_cny: float, *, conn,
) -> float:
    """只读预览：`authorize_episode_video_budget_increment` 对同样的
    episode_id/amount 会产出的 cap，供审批卡在真正下单前展示「授权上限」。
    必须与真实授权函数用同一套推导逻辑，不能各算各的、界面和执行对不上。
    """
    db = conn
    ensure_video_budget_authority_tables(db)
    _, floor = _episode_video_budget_floor(episode_id, conn=db)
    return round(_apply_video_budget_retry_margin(floor, amount_cny), 6)


def authorize_episode_video_budget_increment(
    episode_id: str,
    increment_cny: float,
    *,
    source: str,
    operation_id: str | None = None,
    request_fingerprint: str | None = None,
    conn,
) -> float:
    """Add one explicitly approved payable-video amount to the episode cap."""
    amount = float(increment_cny)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("视频授权额度必须是非负有限数")
    db = conn
    ensure_video_budget_authority_tables(db)
    db.execute(
        """CREATE TABLE IF NOT EXISTS video_budget_authorization_receipts(
               operation_id TEXT PRIMARY KEY,
               episode_id TEXT NOT NULL,
               request_fingerprint TEXT NOT NULL,
               increment_cny REAL NOT NULL,
               cap_after_cny REAL NOT NULL,
               source TEXT NOT NULL,
               created_at REAL NOT NULL
           )"""
    )
    if bool(operation_id) != bool(request_fingerprint):
        raise ValueError("预算授权 receipt 必须同时提供 operation_id 与 request_fingerprint")
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        if operation_id:
            receipt = db.execute(
                """SELECT episode_id,request_fingerprint,increment_cny,cap_after_cny
                     FROM video_budget_authorization_receipts WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()
            if receipt:
                if (
                    str(receipt["episode_id"]) != episode_id
                    or str(receipt["request_fingerprint"]) != request_fingerprint
                    or abs(float(receipt["increment_cny"]) - amount) > 1e-9
                ):
                    raise ValueError("预算授权 idempotency_key 已绑定不同请求")
                if owns_transaction:
                    db.commit()
                return round(float(receipt["cap_after_cny"]), 6)
        stamp = now()
        baseline, floor = _episode_video_budget_floor(episode_id, conn=db)
        cap = _apply_video_budget_retry_margin(floor, amount)
        db.execute(
            """INSERT INTO episode_video_budget_authorities(
                   episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(episode_id) DO UPDATE SET
                   cap_cny=excluded.cap_cny,source=excluded.source,
                   authorized_at=excluded.authorized_at,updated_at=excluded.updated_at""",
            (episode_id, baseline, cap, source, stamp, stamp),
        )
        if operation_id:
            db.execute(
                """INSERT INTO video_budget_authorization_receipts(
                       operation_id,episode_id,request_fingerprint,increment_cny,
                       cap_after_cny,source,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    operation_id,
                    episode_id,
                    request_fingerprint,
                    amount,
                    cap,
                    source,
                    stamp,
                ),
            )
        if owns_transaction:
            db.commit()
        return round(cap, 6)
    except Exception:
        if owns_transaction:
            db.rollback()
        raise


def authorize_episode_video_budget_absolute(
    episode_id: str,
    cap_cny: float,
    *,
    source: str,
    conn,
) -> float:
    """Persist an absolute completion-run cap without forgetting sunk liability."""
    requested = float(cap_cny)
    if not math.isfinite(requested) or requested < 0:
        raise ValueError("视频授权上限必须是非负有限数")
    db = conn
    ensure_video_budget_authority_tables(db)
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        # 这里的 requested 是调用方给出的**绝对总额**（补款路径传的就是新的总
        # 上限），所以不加不减；只保证不低于已承诺责任 floor，否则扣款侧立刻
        # 判超限。"本轮批准额度"要变成总额是调用方的事，见 grants_issue。
        baseline, floor = _episode_video_budget_floor(episode_id, conn=db)
        cap = max(requested, floor)
        stamp = now()
        db.execute(
            """INSERT INTO episode_video_budget_authorities(
                   episode_id,baseline_cny,cap_cny,source,authorized_at,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(episode_id) DO UPDATE SET
                   cap_cny=excluded.cap_cny,source=excluded.source,
                   authorized_at=excluded.authorized_at,updated_at=excluded.updated_at""",
            (episode_id, baseline, cap, source, stamp, stamp),
        )
        if owns_transaction:
            db.commit()
        return round(cap, 6)
    except Exception:
        if owns_transaction:
            db.rollback()
        raise


def episode_video_budget_snapshot(episode_id: str, *, conn) -> dict[str, float] | None:
    db = conn
    ensure_video_budget_authority_tables(db)
    row = db.execute(
        "SELECT baseline_cny,cap_cny FROM episode_video_budget_authorities WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if row is None:
        return None
    claimed = float(db.execute(
        """SELECT COALESCE(SUM(amount_cny),0) AS amount
             FROM provider_video_budget_claims
            WHERE episode_id=? AND status!='released'""",
        (episode_id,),
    ).fetchone()["amount"] or 0)
    baseline = float(row["baseline_cny"] or 0)
    cap = float(row["cap_cny"] or 0)
    return {
        "baseline_cny": baseline,
        "claimed_cny": claimed,
        "used_cny": round(baseline + claimed, 6),
        "cap_cny": cap,
        "remaining_cny": round(max(0.0, cap - baseline - claimed), 6),
    }


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


def episode_video_completion_budget_requirement(
    episode_id: str,
    *,
    conn,
) -> dict[str, float | int]:
    """Return the absolute provider cap needed for one claim per current shot."""
    from app.video_cost_model import initial_shot_generation_cost

    db = conn
    ensure_video_budget_authority_tables(db)
    release_row = db.execute(
        "SELECT published_storyboard_artifact_id FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    cost_basis = None
    if release_row and release_row["published_storyboard_artifact_id"]:
        from app.video_plan import authoritative_storyboard_plan_cost

        cost_basis = authoritative_storyboard_plan_cost(
            episode_id,
            conn=db,
        )
    snapshot = episode_video_budget_snapshot(episode_id, conn=db)
    used = float((snapshot or {}).get("used_cny") or 0)
    claimed_shot_ids = {
        str(row["shot_id"])
        for row in db.execute(
            """SELECT DISTINCT v.shot_id
                 FROM provider_video_budget_claims c
                 JOIN shot_versions v ON v.id=c.version_id
                 JOIN shots s ON s.id=v.shot_id
                WHERE c.episode_id=? AND c.status!='released'
                  AND s.episode_id=?""",
            (episode_id, episode_id),
        ).fetchall()
    }
    remaining = 0.0
    projected_first_pass = 0.0
    total_shots = 0
    for row in db.execute(
        "SELECT id,duration_s FROM shots WHERE episode_id=?",
        (episode_id,),
    ).fetchall():
        total_shots += 1
        shot_cost = initial_shot_generation_cost(
            float(row["duration_s"] or 0)
        )
        projected_first_pass += shot_cost
        if str(row["id"]) in claimed_shot_ids:
            continue
        remaining += shot_cost
    if cost_basis is not None and (
        total_shots != int(cost_basis["shot_count"])
        or abs(
            projected_first_pass
            - float(cost_basis["estimated_cost_cny"])
        ) > 1e-9
    ):
        raise ValueError(
            "视频成本投影与当前 outline/release authority 不一致"
        )
    return {
        "used_cny": round(used, 6),
        "claimed_current_shots": len(claimed_shot_ids),
        "shots_total": total_shots,
        "unclaimed_first_pass_cny": round(remaining, 6),
        "required_completion_cap_cny": round(used + remaining, 6),
    }
