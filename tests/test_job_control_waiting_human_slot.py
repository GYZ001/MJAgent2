"""转人工（waiting_human）转移必须同步释放 video_slot_active，否则额度退还
扫描（``quota.reconcile_video_seconds_refunds``，判据只看
``jobs.video_slot_active=0``）永远看不到这次尝试，预扣的 15 秒视频额度与
镜头的活动槽位都会永久泄漏——上一段取帧链路（默认关闭）一旦开启即触发。

仓库里其它 30+ 处终态/转人工转移都会清零 video_slot_active（例如
app/system_api.py 约 1862/1882 行、app/db.py 约 2173/2187/2260/2281 行、
app/media_exec/job_recovery.py 249-260/271-286 行、app/media_exec/
checkpoints.py 50-68 行），唯独 app/video_supervisor/job_control.py 的
``_reconcile_terminal_continuity_blocks``（转人工原因码
VIDEO_CHAIN_ANCHOR_BLOCKED）此前漏了这一步。
"""
from __future__ import annotations

from app import quota
from app.db import get_conn, new_id, now
from app.media_pipeline import stages as media_stages
from app.video_supervisor.job_control import _reconcile_terminal_continuity_blocks


def _seed_user_project_episode() -> tuple[str, str, str]:
    conn = get_conn()
    uid = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier,
               quota_period_started_at
           ) VALUES(?,?,?,?,'local','active',0,0,?,?,?)""",
        (uid, f"free-{uid}", "测试账号", "x", now(), "free", now()),
    )
    project_id = new_id("proj")
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at, owner_user_id) "
        "VALUES(?,?,?,?,?)",
        (project_id, "P", "created", now(), uid),
    )
    episode_id = new_id("ep")
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) "
        "VALUES(?,?,?,?,?)",
        (episode_id, project_id, 1, "confirmed", now()),
    )
    conn.commit()
    return uid, project_id, episode_id


def test_waiting_human_continuity_block_releases_slot_and_is_refundable():
    """转人工后 video_slot_active 必须清零，且额度退还扫描必须能识别到它。"""
    conn = get_conn()
    uid, project_id, episode_id = _seed_user_project_episode()

    # shot1 是上游镜：既无成功且技术校验通过的版本，也无活动任务，才会被
    # _reconcile_terminal_continuity_blocks 判定为死锁并转人工。
    shot1_id = new_id("shot")
    shot2_id = new_id("shot")
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,?)",
        (shot1_id, episode_id, 1, 5),
    )
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,?)",
        (shot2_id, episode_id, 2, 5),
    )

    version_id = new_id("v")
    job_id = new_id("job")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,
               video_slot_active,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (version_id, shot2_id, 1, "p", f"idem-{version_id}", "queued", 1, now()),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               video_slot_active,created_at,updated_at,after_shot_id,pipeline_stage
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, "video", shot2_id, version_id, episode_id, project_id, "queued",
         1, now(), now(), shot1_id, media_stages.STAGE_WAITING_CONTINUITY),
    )
    conn.commit()

    # 模拟这次尝试在入队时已预扣 15 秒视频额度（reserve_video_seconds 的正常
    # 调用时机是创建 job 的同一事务，这里用 attempt_key=job_id 补记）。
    quota.reserve_video_seconds(conn, uid, attempt_key=job_id)
    conn.commit()

    assert _reconcile_terminal_continuity_blocks(episode_id) == 1

    job = conn.execute(
        "SELECT status, reason_code, video_slot_active FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    assert job["status"] == "waiting_human"
    assert job["reason_code"] == "VIDEO_CHAIN_ANCHOR_BLOCKED"
    assert job["video_slot_active"] == 0, "转人工必须清活动槽位，否则镜头被永久锁死"

    version = conn.execute(
        "SELECT video_slot_active FROM shot_versions WHERE id=?", (version_id,),
    ).fetchone()
    assert version["video_slot_active"] == 0, "shot_versions 侧槽位同样必须清零"

    refunded = quota.reconcile_video_seconds_refunds(conn, episode_id)
    assert refunded == 1, "video_slot_active 未清零会让退还扫描永远看不到这次尝试"
    result = quota.refund_video_seconds(conn, uid, attempt_key=job_id)
    assert result["idempotent_replay"] is True
