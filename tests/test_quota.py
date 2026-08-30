"""三档会员配额引擎（app/quota.py）与其接线点的硬证明。

覆盖收尾自检要求的四组证明：
1. 幂等性——同一次尝试（attempt_key）的同一个动作重复处理，只扣一次/只退一次。
2. 事务原子性——扣减写入与调用方事务共生死，调用方失败时用独立连接验证回滚。
3. 退还——按产物信号（有没有成功的视频版本）判断，不看错误码。
4. 四类配额各自的边界拦截——达到上限时被拦，响应带齐 剩余/重置时间/档位/升级路径，
   且能分清是哪一类（``detail["gate"]``）。

以及至少一条端到端接线验证（HTTP 项目数配额、直接调用 media_exec 的视频任务创建
函数），证明不是只有单元函数本身正确，而是真的接上了产品入口。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import quota, quota_addon
from app.auth.passwords import hash_password
from app.auth.principal import Principal, set_current_principal
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


def _make_user(tier: str = "free", *, is_admin: bool = False) -> str:
    conn = get_conn()
    user_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier,
               quota_period_started_at
           ) VALUES(?,?,?,?,'local','active',?,0,?,?,?)""",
        (
            user_id, f"{tier}-{user_id}", "测试账号", hash_password("pw-test-000000"),
            1 if is_admin else 0, now(), tier, now(),
        ),
    )
    conn.commit()
    return user_id


def _make_project(owner_user_id: str) -> str:
    conn = get_conn()
    project_id = new_id("proj")
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, owner_user_id) "
        "VALUES(?,?,?,?,?)",
        (project_id, "P", "created", now(), owner_user_id),
    )
    conn.commit()
    return project_id


def _make_episode(project_id: str) -> str:
    conn = get_conn()
    episode_id = new_id("ep")
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) "
        "VALUES(?,?,?,?,?)",
        (episode_id, project_id, 1, "confirmed", now()),
    )
    conn.commit()
    return episode_id


def _make_shot(episode_id: str) -> str:
    conn = get_conn()
    shot_id = new_id("shot")
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,?)",
        (shot_id, episode_id, 1, 5),
    )
    conn.commit()
    return shot_id


# ---------------------------------------------------------------------------
# 周期与档位
# ---------------------------------------------------------------------------

def test_period_index_advances_every_30_days_from_anchor():
    anchor = 1_000_000.0
    assert quota.period_index(anchor, anchor) == 0
    assert quota.period_index(anchor, anchor + quota.PERIOD_SECONDS - 1) == 0
    assert quota.period_index(anchor, anchor + quota.PERIOD_SECONDS) == 1
    assert quota.period_index(anchor, anchor + 3 * quota.PERIOD_SECONDS + 5) == 3


def test_effective_limits_admin_and_unknown_user_are_unlimited():
    conn = get_conn()
    admin_id = _make_user("free", is_admin=True)
    limits = quota.effective_limits(conn, admin_id)
    assert limits.projects is None and limits.token is None

    unlimited = quota.effective_limits(conn, "no-such-user-id")
    assert unlimited.projects is None


def test_effective_limits_match_tier_table():
    """五档，用户拍板的数值（app/quota.py::TIER_TABLE）——逐档核对四类订阅
    配额，不漏任何一档。"""
    conn = get_conn()
    for tier, expected in (
        ("free", (1, 1, 300_000.0, 60.0, 3_000_000.0)),
        ("starter", (2, 2, 600_000.0, 300.0, 6_000_000.0)),
        ("standard", (3, 3, 900_000.0, 900.0, 9_000_000.0)),
        ("pro", (6, 6, 1_800_000.0, 1800.0, 18_000_000.0)),
        ("max", (10, 10, 3_000_000.0, 3000.0, 30_000_000.0)),
    ):
        uid = _make_user(tier)
        limits = quota.effective_limits(conn, uid)
        assert (
            limits.projects, limits.concurrency, limits.token,
            limits.video_seconds, limits.image,
        ) == expected


# ---------------------------------------------------------------------------
# 幂等性
# ---------------------------------------------------------------------------

def test_charge_tokens_same_attempt_key_only_charged_once():
    conn = get_conn()
    uid = _make_user("free")
    r1 = quota.charge_tokens(conn, uid, 1000.0, attempt_key="call:1")
    r2 = quota.charge_tokens(conn, uid, 1000.0, attempt_key="call:1")
    conn.commit()
    assert r1 == {"charged": 1000.0, "idempotent_replay": False}
    assert r2 == {"charged": 0.0, "idempotent_replay": True}
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "token", pidx) == 1000.0


def test_reserve_and_refund_video_seconds_are_each_idempotent():
    conn = get_conn()
    uid = _make_user("max")
    c1 = quota.reserve_video_seconds(conn, uid, attempt_key="job:1")
    c2 = quota.reserve_video_seconds(conn, uid, attempt_key="job:1")
    assert c1 == {"charged_s": 15.0, "idempotent_replay": False}
    assert c2 == {"charged_s": 0.0, "idempotent_replay": True}

    r1 = quota.refund_video_seconds(conn, uid, attempt_key="job:1")
    r2 = quota.refund_video_seconds(conn, uid, attempt_key="job:1")
    conn.commit()
    assert r1 == {"refunded_s": 15.0, "idempotent_replay": False, "no_charge_found": False}
    assert r2 == {"refunded_s": 0.0, "idempotent_replay": True, "no_charge_found": False}
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 0.0


def test_refund_without_prior_charge_is_a_safe_noop():
    conn = get_conn()
    uid = _make_user("free")
    result = quota.refund_video_seconds(conn, uid, attempt_key="job:never-charged")
    assert result == {"refunded_s": 0.0, "idempotent_replay": False, "no_charge_found": True}


def test_charge_image_cost_idempotent_across_pool_and_token_split():
    conn = get_conn()
    uid = _make_user("free")
    limits = quota.effective_limits(conn, uid)
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    # 先把本周期的图像额度打到只剩 100，再花 300：应该 100 走图像额度、200 走 token。
    quota._record_ledger(
        conn, user_id=uid, resource="image", pidx=pidx,
        attempt_key="prefill", reason="charge", delta=limits.image - 100,
    )
    r1 = quota.charge_image_cost(conn, uid, 300.0, attempt_key="img:1")
    r2 = quota.charge_image_cost(conn, uid, 300.0, attempt_key="img:1")
    conn.commit()
    assert r1 == {"pool_charged": 100.0, "token_charged": 200.0, "idempotent_replay": False}
    assert r2 == {"pool_charged": 100.0, "token_charged": 200.0, "idempotent_replay": True}
    assert quota.usage_for(conn, uid, "token", pidx) == 200.0
    assert quota.usage_for(conn, uid, "image", pidx) == limits.image


def test_charge_image_cost_boundary_per_tier():
    """五档图像额度各自独立：满档后立刻溢出到 token，不会互相污染彼此的上限。"""
    conn = get_conn()
    for tier, cap in (
        ("free", 3_000_000.0), ("starter", 6_000_000.0), ("standard", 9_000_000.0),
        ("pro", 18_000_000.0), ("max", 30_000_000.0),
    ):
        uid = _make_user(tier)
        r1 = quota.charge_image_cost(conn, uid, cap, attempt_key=f"img-{tier}-full")
        conn.commit()
        assert r1 == {"pool_charged": cap, "token_charged": 0.0, "idempotent_replay": False}

        # 再花 1：图像额度已耗尽，整笔溢出到 token。
        r2 = quota.charge_image_cost(conn, uid, 1.0, attempt_key=f"img-{tier}-overflow")
        conn.commit()
        assert r2 == {"pool_charged": 0.0, "token_charged": 1.0, "idempotent_replay": False}

        pidx = quota.period_index(quota.period_anchor(conn, uid))
        assert quota.usage_for(conn, uid, "image", pidx) == cap
        assert quota.usage_for(conn, uid, "token", pidx) == 1.0


def test_charge_image_cost_rejected_when_both_image_and_token_exhausted():
    conn = get_conn()
    uid = _make_user("free")
    limits = quota.effective_limits(conn, uid)
    # 图像额度用满，token 额度也用满：再来一分钱的图像成本都应该被整体拒绝。
    quota.charge_image_cost(conn, uid, limits.image, attempt_key="img:fill-pool")
    conn.commit()
    quota.charge_tokens(conn, uid, limits.token, attempt_key="call:fill-token")
    conn.commit()

    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota.charge_image_cost(conn, uid, 1.0, attempt_key="img:rejected")
    detail = exc_info.value.detail
    assert detail["gate"] == "token"
    assert detail["remaining"] == 0.0

    # 拒绝时整体不落任何一笔账（既不扣图像也不扣 token）。
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "image", pidx) == limits.image
    assert quota.usage_for(conn, uid, "token", pidx) == limits.token


def test_image_quota_resets_after_30_day_period_rolls_over():
    """把周期锚点拨到 31 天前，模拟"下一个周期已经开始"，确认图像额度恢复。"""
    conn = get_conn()
    uid = _make_user("free")
    limits = quota.effective_limits(conn, uid)

    r1 = quota.charge_image_cost(conn, uid, limits.image, attempt_key="img:period0")
    conn.commit()
    assert r1 == {"pool_charged": limits.image, "token_charged": 0.0, "idempotent_replay": False}
    old_anchor = quota.period_anchor(conn, uid)
    old_pidx = quota.period_index(old_anchor)
    assert quota.usage_for(conn, uid, "image", old_pidx) == limits.image

    # 把锚点向前拨 31 天（等价于"31 天已经过去"）。
    conn.execute(
        "UPDATE users SET quota_period_started_at=? WHERE id=?",
        (old_anchor - 31 * 86400.0, uid),
    )
    conn.commit()

    new_anchor = quota.period_anchor(conn, uid)
    new_pidx = quota.period_index(new_anchor)
    assert new_pidx != old_pidx, "锚点后移 31 天后应该落进新的 30 天周期"
    assert quota.usage_for(conn, uid, "image", new_pidx) == 0.0, "新周期的图像用量应该是 0"

    # 新周期里应该能重新从图像额度里整笔扣，不再溢出到 token。
    r2 = quota.charge_image_cost(conn, uid, 1000.0, attempt_key="img:period1")
    conn.commit()
    assert r2 == {"pool_charged": 1000.0, "token_charged": 0.0, "idempotent_replay": False}


# ---------------------------------------------------------------------------
# 事务原子性——独立连接验证回滚
# ---------------------------------------------------------------------------

def test_reserve_video_seconds_rolls_back_with_caller_transaction():
    """模拟"扣减成功但任务创建失败"：同一事务里先扣 15 秒，再故意失败并回滚，
    用第二条独立连接读盘证明 ledger 里什么都没留下（不是同一连接读自己写）。
    """
    from app import config as app_config

    conn = get_conn()
    uid = _make_user("max")
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = quota.reserve_video_seconds(conn, uid, attempt_key="job:atomic-1")
        assert result["charged_s"] == 15.0
        raise RuntimeError("模拟任务创建失败（例如违反某个约束）")
    except RuntimeError:
        conn.rollback()

    independent = sqlite3.connect(app_config.DB_PATH)
    independent.row_factory = sqlite3.Row
    try:
        rows = independent.execute(
            "SELECT * FROM quota_ledger WHERE attempt_key=?", ("job:atomic-1",)
        ).fetchall()
        assert rows == [], "回滚后不应该有任何 ledger 行留在盘上"
    finally:
        independent.close()

    # 用调用方自己的连接也确认：usage 是 0，配额确实没被扣。
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 0.0


def test_project_creation_rolls_back_ledger_and_row_together_on_failure(monkeypatch):
    """项目创建路径：配额检查通过后，如果后续写入失败，项目行和（隐式）配额
    状态必须一起回滚——用独立连接验证 projects 表里没有新增行。
    """
    from app import config as app_config
    from app.domain import projects as projects_mod
    from tests.conftest import patch_projects_everywhere

    uid = _make_user("free")
    set_current_principal(Principal(user_id=uid, username="u", is_system_admin=False))
    try:
        # patch_projects_everywhere, not a bare monkeypatch.setattr(projects_mod,
        # ...): app.domain.projects is a real package (拆分后), and
        # _create_project_core (called below) resolves ingest_novel/
        # prepare_novel_bytes/validate_novel_filename/_creation_owner_user_id
        # through app/domain/projects/create.py's own module globals, not
        # through the package-level re-export this bare form would rebind --
        # see tests/test_projects_monkeypatch_guard.py.
        patch_projects_everywhere(monkeypatch, "_creation_owner_user_id", lambda: uid)

        def _boom(*_a, **_k):
            raise RuntimeError("模拟章节写入失败")

        patch_projects_everywhere(monkeypatch, "ingest_novel", lambda *_a, **_k: {
            "chapters": [{"idx": 0, "title": "t", "content": "c"}],
            "total_chars": 1,
        })
        # executemany for chapters is what we corrupt: patch conn.executemany via
        # a wrapper is fragile; simplest is to break report["chapters"] shape.
        patch_projects_everywhere(monkeypatch, "prepare_novel_bytes", lambda *_a, **_k: b"x")
        patch_projects_everywhere(monkeypatch, "validate_novel_filename", lambda name: name)

        conn = get_conn()
        before = conn.execute(
            "SELECT COUNT(*) c FROM projects WHERE owner_user_id=?", (uid,)
        ).fetchone()["c"]
        assert before == 0

        # 强行让 chapters 的 executemany 失败：塞一个不是 4 元组的畸形章节。
        patch_projects_everywhere(monkeypatch, "ingest_novel", lambda *_a, **_k: {
            "chapters": [{"idx": "not-an-int", "title": None, "content": None}],
            "total_chars": 1,
        })
        with pytest.raises(Exception):
            projects_mod._create_project_core("T", "novel.txt", b"raw-bytes")

        independent = sqlite3.connect(app_config.DB_PATH)
        independent.row_factory = sqlite3.Row
        try:
            rows = independent.execute(
                "SELECT * FROM projects WHERE owner_user_id=?", (uid,)
            ).fetchall()
            assert rows == [], "失败必须整体回滚，不留半成品项目行"
        finally:
            independent.close()
    finally:
        set_current_principal(None)


# ---------------------------------------------------------------------------
# 退还：按产物信号，不看错误码
# ---------------------------------------------------------------------------

def test_reconcile_refunds_shot_with_no_usable_video_regardless_of_error_reason():
    conn = get_conn()
    uid = _make_user("free")
    project_id = _make_project(uid)
    episode_id = _make_episode(project_id)
    shot_id = _make_shot(episode_id)

    for version_no, (reason, error_text) in enumerate((
        ("connect_timeout", "ConnectTimeout"),
        ("gate_denied", "anchor_phrase 缺失"),
        ("malformed_json", "上游返回非法 JSON"),
    ), start=1):
        job_id = new_id("job")
        version_id = new_id("v")
        conn.execute(
            "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,"
            "status,video_slot_active,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (version_id, shot_id, version_no, "p", f"idem-{reason}", "failed", 0, now()),
        )
        conn.execute(
            "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,"
            "status,video_slot_active,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "video", shot_id, version_id, episode_id, project_id,
             "failed", 0, error_text, now(), now()),
        )
        conn.commit()
        quota.reserve_video_seconds(conn, uid, attempt_key=job_id)
        conn.commit()

        refunded = quota.reconcile_video_seconds_refunds(conn, episode_id)
        assert refunded == 1, f"{reason} 应该被退还，不管具体错误原因"
        result = quota.refund_video_seconds(conn, uid, attempt_key=job_id)
        assert result["idempotent_replay"] is True  # 已经被上面的 reconcile 退过了


def test_reconcile_does_not_refund_shot_with_succeeded_video():
    conn = get_conn()
    uid = _make_user("free")
    project_id = _make_project(uid)
    episode_id = _make_episode(project_id)
    shot_id = _make_shot(episode_id)

    job_id = new_id("job")
    version_id = new_id("v")
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,"
        "status,video_slot_active,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (version_id, shot_id, 1, "p", "idem-ok", "succeeded", 0, now()),
    )
    conn.execute(
        "INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,"
        "status,video_slot_active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (job_id, "video", shot_id, version_id, episode_id, project_id,
         "succeeded", 0, now(), now()),
    )
    conn.commit()
    quota.reserve_video_seconds(conn, uid, attempt_key=job_id)
    conn.commit()

    refunded = quota.reconcile_video_seconds_refunds(conn, episode_id)
    assert refunded == 0
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 15.0


# ---------------------------------------------------------------------------
# 四类配额的边界拦截
# ---------------------------------------------------------------------------

def test_check_project_slot_blocks_at_tier_limit_with_full_detail():
    conn = get_conn()
    uid = _make_user("free")
    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota.check_project_slot(conn, uid, active_count=1)
    detail = exc_info.value.detail
    assert detail["gate"] == "projects"
    assert detail["code"] == "QUOTA_EXCEEDED_PROJECTS"
    assert detail["tier"] == "free"
    assert detail["limit"] == 1
    assert detail["remaining"] == 0
    assert detail["reset_at"] is None
    assert "入门" in detail["upgrade_path"]
    assert exc_info.value.status_code == 429

    # 没到上限不拦截。
    quota.check_project_slot(conn, uid, active_count=0)


def test_check_project_slot_blocks_at_tier_limit_for_all_tiers():
    """五档项目数上限各自独立：free=1/starter=2/standard=3/pro=6/max=10。"""
    conn = get_conn()
    for tier, limit in (
        ("free", 1), ("starter", 2), ("standard", 3), ("pro", 6), ("max", 10),
    ):
        uid = _make_user(tier)
        quota.check_project_slot(conn, uid, active_count=limit - 1)
        with pytest.raises(quota.QuotaExceeded) as exc_info:
            quota.check_project_slot(conn, uid, active_count=limit)
        assert exc_info.value.detail["limit"] == limit


def test_assert_token_capacity_blocks_at_tier_limit_for_all_tiers():
    """五档 token 上限各自独立：30/60/90/180/300 万。"""
    conn = get_conn()
    for tier, limit in (
        ("free", 300_000.0), ("starter", 600_000.0), ("standard", 900_000.0),
        ("pro", 1_800_000.0), ("max", 3_000_000.0),
    ):
        uid = _make_user(tier)
        quota.charge_tokens(conn, uid, limit, attempt_key=f"call:cap:{tier}")
        conn.commit()
        with pytest.raises(quota.QuotaExceeded) as exc_info:
            quota.assert_token_capacity(conn, uid)
        assert exc_info.value.detail["limit"] == limit
        assert exc_info.value.detail["remaining"] == 0.0


def test_check_module_concurrency_blocks_at_tier_limit():
    """五档并发上限各自独立：free=1/starter=2/standard=3/pro=6/max=10。"""
    conn = get_conn()
    for tier, limit in (
        ("free", 1), ("starter", 2), ("standard", 3), ("pro", 6), ("max", 10),
    ):
        uid = _make_user(tier)
        quota.check_module_concurrency(
            conn, uid, quota.MODULE_STORYBOARD, active_count=limit - 1
        )
        with pytest.raises(quota.QuotaExceeded) as exc_info:
            quota.check_module_concurrency(
                conn, uid, quota.MODULE_STORYBOARD, active_count=limit
            )
        assert exc_info.value.detail["gate"] == "concurrency"
        assert exc_info.value.detail["limit"] == limit


def test_patch_quota_everywhere_makes_fake_tier_table_reach_real_call_path(monkeypatch):
    """桩生效证明（app/quota.py 拆出 app/quota_tiers.py 之后的硬约束）：用
    ``tests.conftest.patch_quota_everywhere`` 把 ``TIER_TABLE`` 换成一张 free 档
    并发上限=99 的假表，真实调用路径 ``effective_limits``/
    ``check_module_concurrency`` 必须看到假表，不是只改了某一侧的绑定。

    ``app/quota.py`` 现在写 ``from app.quota_tiers import TIER_TABLE``——
    ``quota.TIER_TABLE`` 与 ``quota_tiers.TIER_TABLE`` 是两个独立绑定。实测过
    两个方向都不完整：只打 ``quota.TIER_TABLE`` 这一侧，``effective_limits``
    （定义在 ``app.quota`` 里，读的是它自己模块命名空间里的全局名）确实能看到，
    但 ``app.quota_tiers.TIER_TABLE`` 仍是真表；只打 ``quota_tiers.TIER_TABLE``
    这一侧（数据现在的「所在地」，直觉上最容易被打桩的地方），
    ``effective_limits`` 反而看不到——它读的是 ``app.quota`` 自己那份绑定，不会
    因为 ``quota_tiers`` 被重新绑定而跟着变。``patch_quota_everywhere`` 两侧一起
    打，两个方向都不留死角。
    """
    conn = get_conn()
    uid = _make_user("free")

    fake_free = quota.TierLimits("free", 1, 99, 300_000.0, 60.0, 3_000_000.0)
    fake_table = dict(quota.TIER_TABLE)
    fake_table["free"] = fake_free
    from tests.conftest import patch_quota_everywhere
    patch_quota_everywhere(monkeypatch, "TIER_TABLE", fake_table)

    limits = quota.effective_limits(conn, uid)
    assert limits.concurrency == 99, (
        "effective_limits 应该看到假表的并发上限 99（真表是 1）——如果这里失败，"
        "说明打桩没有真的命中 effective_limits 实际读取的那个绑定"
    )

    # 真实调用路径：走到假表的 99 才该被拦，98 不该拦（真表上限是 1，98 早该炸）。
    quota.check_module_concurrency(conn, uid, quota.MODULE_STORYBOARD, active_count=98)
    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota.check_module_concurrency(conn, uid, quota.MODULE_STORYBOARD, active_count=99)
    assert exc_info.value.detail["limit"] == 99


def test_assert_token_capacity_blocks_once_used_reaches_limit():
    conn = get_conn()
    uid = _make_user("free")
    quota.charge_tokens(conn, uid, 300_000.0, attempt_key="call:cap")
    conn.commit()
    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota.assert_token_capacity(conn, uid)
    detail = exc_info.value.detail
    assert detail["gate"] == "token"
    assert detail["remaining"] == 0.0
    assert detail["reset_at"] is not None
    assert detail["reset_at"] > now()


def test_reserve_video_seconds_blocks_when_it_would_exceed_limit():
    """五档视频时长上限各自独立：free=60s(4 镜)/starter=300s(20 镜)/
    standard=900s(60 镜)/pro=1800s(120 镜)/max=3000s(200 镜)。没有加量包时，
    卡到上限就该被拦（remaining 里加量包余额贡献为 0）。"""
    conn = get_conn()
    for tier, limit_s in (
        ("free", 60.0), ("starter", 300.0), ("standard", 900.0),
        ("pro", 1800.0), ("max", 3000.0),
    ):
        uid = _make_user(tier)
        n_shots = int(limit_s // 15.0)
        for i in range(n_shots):
            result = quota.reserve_video_seconds(conn, uid, attempt_key=f"job:{tier}:{i}")
            assert result["charged_s"] == 15.0
        conn.commit()
        with pytest.raises(quota.QuotaExceeded) as exc_info:
            quota.reserve_video_seconds(conn, uid, attempt_key=f"job:{tier}:overflow")
        detail = exc_info.value.detail
        assert detail["gate"] == "video_seconds"
        assert detail["limit"] == limit_s
        assert detail["used"] == limit_s
        assert detail["remaining"] == 0.0


def test_admin_user_is_never_blocked_by_any_gate():
    conn = get_conn()
    uid = _make_user("free", is_admin=True)
    quota.check_project_slot(conn, uid, active_count=999)
    quota.check_module_concurrency(conn, uid, quota.MODULE_VIDEO, active_count=999)
    quota.assert_token_capacity(conn, uid)
    for i in range(500):
        quota.reserve_video_seconds(conn, uid, attempt_key=f"admin-job:{i}")
    conn.commit()


# ---------------------------------------------------------------------------
# 端到端接线：至少证明产品入口真的接上了这套引擎
# ---------------------------------------------------------------------------

def test_http_project_creation_blocked_once_free_tier_quota_is_used(monkeypatch):
    """走真实 HTTP 入口：free 档账号已有 1 个项目时，第二次导入被 429 拦下，
    响应带齐 gate/remaining/reset_at/tier/upgrade_path；且没有新项目行落库。
    """
    client = TestClient(app)
    uid = _make_user("free")
    session_token = create_session(uid)
    headers = {**_HEADERS, "X-Manju-Session": session_token}

    _make_project(uid)  # 已经用满 free 档的 1 个项目名额

    def _post():
        return client.post(
            "/api/projects",
            headers=headers,
            data={"name": "第二个项目"},
            files={"file": ("novel.txt", "第一章\n正文正文正文。\n".encode("utf-8"), "text/plain")},
        )

    waiting = _post()
    assert waiting.status_code == 202, waiting.text
    approval_token = waiting.json()["approval_token"]

    resp = client.post(
        "/api/projects",
        headers={**headers, "X-Manju-Approval-Token": approval_token},
        data={"name": "第二个项目"},
        files={"file": ("novel.txt", "第一章\n正文正文正文。\n".encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 429, resp.text
    body = resp.json()["detail"]
    assert body["gate"] == "projects"
    assert body["code"] == "QUOTA_EXCEEDED_PROJECTS"
    assert body["tier"] == "free"
    assert body["remaining"] == 0
    assert body["reset_at"] is None
    assert "upgrade_path" in body

    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) c FROM projects WHERE owner_user_id=?", (uid,)
    ).fetchone()["c"]
    assert count == 1, "被拦截的第二次创建不能留下任何新项目行"


def test_video_preflight_job_creation_charges_15_seconds_and_respects_concurrency():
    """直接调用生产代码里创建视频任务的真实函数（不是重新实现一份等价逻辑），
    证明并发闸门 + 15 秒预扣真的接在了这条路径上。"""
    from app.media_exec import enqueue

    uid = _make_user("free")  # 并发上限 1
    project_id = _make_project(uid)
    episode_id = _make_episode(project_id)
    shot_a = _make_shot(episode_id)
    conn = get_conn()
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,?)",
        ("shot-b", episode_id, 2, 5),
    )
    conn.commit()

    result_a = enqueue._begin_video_preflight_job(shot_a, supervisor_run_id=None)
    assert result_a["acquired"] is True
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 15.0

    with pytest.raises(quota.QuotaExceeded) as exc_info:
        enqueue._begin_video_preflight_job("shot-b", supervisor_run_id=None)
    assert exc_info.value.detail["gate"] == "concurrency"

    # 第二镜没有留下任何 job 行或额外扣款——被并发闸门整体挡在事务外。
    leftover = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE shot_id='shot-b'"
    ).fetchone()["c"]
    assert leftover == 0


# ---------------------------------------------------------------------------
# 加量包：不随周期重置、消耗顺序（先订阅后加量包）、幂等
# ---------------------------------------------------------------------------

def test_grant_video_addon_seconds_increases_balance_and_is_idempotent():
    conn = get_conn()
    uid = _make_user("free")
    assert quota.addon_video_seconds_balance(conn, uid) == 0.0

    r1 = quota_addon.grant_video_addon_seconds(conn, uid, packages=2, attempt_key="order:1")
    conn.commit()
    assert r1["granted_s"] == 1200.0  # 2 包 * 600 秒/包
    assert r1["idempotent_replay"] is False
    assert quota.addon_video_seconds_balance(conn, uid) == 1200.0

    # 同一笔购买（同一个 attempt_key）重复入账只生效一次。
    r2 = quota_addon.grant_video_addon_seconds(conn, uid, packages=2, attempt_key="order:1")
    conn.commit()
    assert r2["idempotent_replay"] is True
    assert quota.addon_video_seconds_balance(conn, uid) == 1200.0  # 没有翻倍


def test_grant_video_addon_seconds_rejects_non_positive_packages():
    conn = get_conn()
    uid = _make_user("free")
    with pytest.raises(HTTPException) as exc_info:
        quota_addon.grant_video_addon_seconds(conn, uid, packages=0, attempt_key="order:bad")
    assert exc_info.value.status_code == 422


def test_addon_balance_survives_period_rollover_unlike_subscription_usage():
    """加量包买了就是买了：周期滚动后订阅视频用量清零，加量包余额纹丝不动。"""
    conn = get_conn()
    uid = _make_user("free")
    quota_addon.grant_video_addon_seconds(conn, uid, packages=1, attempt_key="order:period")
    conn.commit()
    balance_before = quota.addon_video_seconds_balance(conn, uid)
    assert balance_before == 600.0

    old_anchor = quota.period_anchor(conn, uid)
    conn.execute(
        "UPDATE users SET quota_period_started_at=? WHERE id=?",
        (old_anchor - 31 * 86400.0, uid),
    )
    conn.commit()

    assert quota.period_index(quota.period_anchor(conn, uid)) != quota.period_index(old_anchor)
    assert quota.addon_video_seconds_balance(conn, uid) == balance_before


def test_reserve_video_seconds_consumes_subscription_before_addon():
    """消耗顺序：订阅额度没用尽之前，一分钱加量包都不动——订阅额度会随周期作
    废，先花订阅、后花不会过期的加量包才不浪费。"""
    conn = get_conn()
    uid = _make_user("free")  # 60 秒上限 = 4 镜
    quota_addon.grant_video_addon_seconds(conn, uid, packages=1, attempt_key="order:seq")
    conn.commit()
    addon_before = quota.addon_video_seconds_balance(conn, uid)

    for i in range(4):
        result = quota.reserve_video_seconds(conn, uid, attempt_key=f"job:seq:{i}")
        assert result["charged_s"] == 15.0
    conn.commit()

    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 60.0
    assert quota.addon_video_seconds_balance(conn, uid) == addon_before  # 加量包分毫未动


def test_reserve_video_seconds_spills_into_addon_once_subscription_exhausted():
    """订阅用尽后，原本会被拦的一镜靠加量包放行；订阅用量不再增长，加量包按
    实际溢出部分扣减——买加量包真的能让人多产。"""
    conn = get_conn()
    uid = _make_user("free")  # 60 秒上限 = 4 镜
    for i in range(4):
        quota.reserve_video_seconds(conn, uid, attempt_key=f"job:spill:{i}")
    conn.commit()

    # 没有加量包时，第 5 镜应该被拦（且不留任何半途账）。
    with pytest.raises(quota.QuotaExceeded):
        quota.reserve_video_seconds(conn, uid, attempt_key="job:spill:blocked-probe")

    quota_addon.grant_video_addon_seconds(conn, uid, packages=1, attempt_key="order:spill")
    conn.commit()

    result = quota.reserve_video_seconds(conn, uid, attempt_key="job:spill:4")
    conn.commit()
    assert result["charged_s"] == 15.0  # 买包之后同一镜放行了

    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 60.0  # 订阅用量没再涨
    assert quota.addon_video_seconds_balance(conn, uid) == 600.0 - 15.0  # 加量包扣了这 15 秒


def test_reserve_video_seconds_blocks_when_addon_balance_also_insufficient():
    """加量包余额也不够覆盖溢出部分时，整笔仍然被拦，且订阅/加量包都不留半笔
    账（拒绝发生在任何 ledger 写入之前）。"""
    conn = get_conn()
    uid = _make_user("free")  # 60 秒订阅上限
    for i in range(4):
        quota.reserve_video_seconds(conn, uid, attempt_key=f"job:insuf:{i}")
    conn.commit()

    quota_addon.grant_video_addon_seconds(conn, uid, packages=1, attempt_key="order:insuf")
    conn.commit()
    # 先把加量包用到只剩 10 秒——不够覆盖下一镜默认的 15 秒。
    drain = quota.reserve_video_seconds(
        conn, uid, attempt_key="job:insuf:drain", seconds=590.0,
    )
    conn.commit()
    assert drain["charged_s"] == 590.0
    assert quota.addon_video_seconds_balance(conn, uid) == 10.0

    pidx = quota.period_index(quota.period_anchor(conn, uid))
    video_used_before = quota.usage_for(conn, uid, "video_seconds", pidx)

    with pytest.raises(quota.QuotaExceeded) as exc_info:
        quota.reserve_video_seconds(conn, uid, attempt_key="job:insuf:final")
    detail = exc_info.value.detail
    assert detail["gate"] == "video_seconds"
    assert "加量包" in detail["message"]

    # 拒绝时订阅、加量包都没有留下任何新账。
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == video_used_before
    assert quota.addon_video_seconds_balance(conn, uid) == 10.0


def test_refund_video_seconds_returns_addon_portion_when_charge_spans_both():
    """一次预扣如果拆到了订阅+加量包两边，退还要两边一起退，不能漏掉加量包
    那一半。"""
    conn = get_conn()
    uid = _make_user("free")  # 60 秒上限
    for i in range(3):
        quota.reserve_video_seconds(conn, uid, attempt_key=f"job:refund:{i}")
    conn.commit()  # 用掉 45 秒，订阅还剩 15 秒

    quota_addon.grant_video_addon_seconds(conn, uid, packages=1, attempt_key="order:refund")
    conn.commit()

    # 这一镜 20 秒：15 秒吃掉订阅剩余额度，5 秒溢出到加量包。
    result = quota.reserve_video_seconds(conn, uid, attempt_key="job:refund:spill", seconds=20.0)
    conn.commit()
    assert result["charged_s"] == 20.0
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 60.0
    assert quota.addon_video_seconds_balance(conn, uid) == 600.0 - 5.0

    refund = quota.refund_video_seconds(conn, uid, attempt_key="job:refund:spill")
    conn.commit()
    assert refund["refunded_s"] == 20.0
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 45.0
    assert quota.addon_video_seconds_balance(conn, uid) == 600.0  # 加量包那 5 秒也退回来了

    # 幂等：再退一次是 no-op。
    refund2 = quota.refund_video_seconds(conn, uid, attempt_key="job:refund:spill")
    assert refund2 == {"refunded_s": 0.0, "idempotent_replay": True, "no_charge_found": False}


def test_http_admin_grant_video_addon_lets_a_previously_blocked_shot_through():
    """端到端接线：走真实 HTTP 管理端点发放加量包，证明产品入口真的接上了
    ``quota_addon.grant_video_addon_seconds``，而不是只有单元函数本身正确。
    额外验证同一个 idempotency_key 重复调用只生效一次。

    2026-08-30 起该端点经 Command Bus（``quota.grant_video_addon`` 在 catalog
    里声明 ``confirmation=ALWAYS``）：首次调用不带 ``X-Manju-Approval-Token``
    只会拿到 202 + approval_token，真正执行要带着这个 token 重放——与
    ``tests/test_session_security_todolist.py::test_mkdir_requires_directory_grant``
    同一套协议，不是本端点特有。"""
    client = TestClient(app)
    admin_id = _make_user("free", is_admin=True)
    admin_headers = {**_HEADERS, "X-Manju-Session": create_session(admin_id)}

    uid = _make_user("free")  # 60 秒上限 = 4 镜
    conn = get_conn()
    for i in range(4):
        quota.reserve_video_seconds(conn, uid, attempt_key=f"job:http:{i}")
    conn.commit()
    with pytest.raises(quota.QuotaExceeded):
        quota.reserve_video_seconds(conn, uid, attempt_key="job:http:probe")

    grant_body = {"packages": 1, "idempotency_key": "order:http-1"}
    waiting = client.post(
        f"/api/system/users/{uid}/video-addons",
        headers=admin_headers,
        json=grant_body,
    )
    assert waiting.status_code == 202, waiting.text
    assert waiting.json()["status"] == "waiting_approval"
    approval_token = waiting.json()["approval_token"]

    resp = client.post(
        f"/api/system/users/{uid}/video-addons",
        headers={**admin_headers, "X-Manju-Approval-Token": approval_token},
        json=grant_body,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["seconds_granted"] == 600.0
    assert body["addon_balance_s"] == 600.0
    assert body["idempotent_replay"] is False

    # 同一个 idempotency_key 重放（哪怕不带 approval_token）直接命中 Command Bus
    # 的幂等缓存，原样回放第一次执行的完整响应体（含当时的 idempotent_replay=
    # False），不再重新调用领域函数——不是"确认闸门也挡幂等重放"，是根本没有
    # 二次执行可挡。用 addon_balance_s 与下面的余额查询共同证明没有二次发放。
    resp2 = client.post(
        f"/api/system/users/{uid}/video-addons",
        headers=admin_headers,
        json=grant_body,
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json() == body  # Command Bus 幂等缓存原样回放，不是重新执行
    assert resp2.json()["addon_balance_s"] == 600.0  # 没有翻倍
    assert quota_addon.addon_video_seconds_balance(conn, uid) == 600.0  # 独立查询同样证明只发了一次

    result = quota.reserve_video_seconds(conn, uid, attempt_key="job:http:after-grant")
    conn.commit()
    assert result["charged_s"] == 15.0  # 加量包放行了原本会被拦的这一镜
    pidx = quota.period_index(quota.period_anchor(conn, uid))
    assert quota.usage_for(conn, uid, "video_seconds", pidx) == 60.0  # 订阅用量没再涨
    assert quota.addon_video_seconds_balance(conn, uid) == 600.0 - 15.0  # 加量包扣了这 15 秒
