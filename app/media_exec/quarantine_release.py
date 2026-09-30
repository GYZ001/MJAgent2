"""隔离素材的放行判据（拆自 ``job_recovery``，守住 500 行文件红线）。"""
from __future__ import annotations

from pathlib import Path

# 修订本段（app/domain/storyboard_ops/identity_workspace.py::save_identity_candidate）与
# 2.x 生成落库（app/media_exec/enqueue_persist.py::build_base_image_meta）共用同一份
# 「本镜当前分镜身份合同」判据：``shot_versions.image_inputs.segment_identity_fingerprint``
# 是生成那一刻的合同指纹，``shots.shot_contract_json.storyboard_pack_segment.
# identity_contract_fingerprint`` 是当前合同的指纹（``stamp_identity_contract`` 写入，两者
# 用同一个 ``identity_contract_fingerprint()`` 函数计算，字段集合见该函数文档）。两者不等，
# 说明这份素材生成于一次已被后续修订替换的旧合同，不能冒充当前分镜的产出（生产实例
# ver_d967abc82f1c：修订本段后旧合同下的隔离素材被本模块放行，顶替了新合同应有的候选）。
#
# 非 2.x（没有 storyboard_pack_segment）的旧式镜头没有这份合同：它们的 shot_versions 在每次
# 编辑时会被 stage_shot_artifact_cleanup（app/artifacts.py）整表清空重建，一条版本能留到现在
# 就说明没有被任何后续编辑取代，指纹判据对它们没有意义，按原语义放行（CLAUDE.md「判据必须
# 从数据推导」：是否存在这份合同本身就是数据，不是按镜头类型枚举的白名单）。
_HAS_SEGMENT_SQL = "json_extract(s.shot_contract_json,'$.storyboard_pack_segment') IS NOT NULL"
_CURRENT_FINGERPRINT_SQL = (
    "json_extract(s.shot_contract_json,"
    "'$.storyboard_pack_segment.identity_contract_fingerprint')"
)


def release_orphan_quarantined_versions(conn, limit: int) -> int:
    """放行「本镜没有任何可用版本、素材却真实存在、且内容对得上当前分镜」的隔离版本。

    隔离本身仍然有效——并发竞争里落败的那一版必须隔离，避免同一镜出现两个
    采用候选。判据因此从数据推导，不看隔离文案：本镜存在 succeeded 版本时
    一律不动（那才是真正的重复产出）；只有当本镜**一个可用版本都没有**、而
    隔离版的视频文件确实躺在盘上、且指纹对得上本镜当前分镜合同时，才说明这是
    成本预算退场前那套 ``video_slot_active=0 → adoptable=False`` 机器扔掉的好
    素材——用户已经等了 6 次生成却拿不到任何能用的东西，继续隔离纯粹是为已
    废止概念服务的拦路石。指纹对不上（或 2.x 镜头压根没记录指纹）的隔离版本
    不放行：没有证据证明内容匹配当前分镜，不能当匹配处理（CLAUDE.md「不得
    兜底填充」）；那条隔离记录原样保留，不算错误，只是这一轮不处理。
    每镜只放行最新的一版，避免一次冒出多个候选。

    「一个可用版本都没有」必须与 ``uq_versions_active_video_shot``（每镜至多一行
    ``video_slot_active=1``）同一判据：本镜若有别的版本正占着槽位（新一轮生成已在
    排队/运行），这镜就不是孤儿，放行会在 UPDATE 上撞唯一索引。2026-09-02 计算服务器
    上就是这样：v1 隔离、v2 queued 占槽，放行 v1 抛 IntegrityError，把启动恢复整个打死。

    放行后落在 ``video_slot_active=0``——与正常结算成功版本终态一致（见
    ``app/media_exec/job_state.py::_set_job`` 终态分支同时清零 jobs/shot_versions 两侧槽位）。
    历史上这里写的是 1，把「已了结、可当候选」的版本错误标成「仍占槽、任务未完」，导致
    ``identity_workspace._assert_idle_current`` 判定「该片段仍有视频任务」，此后修订本段
    永久失败且界面无出路；``converge_stray_active_slot_versions`` 收敛遗留的旧坏数据。
    """
    rows = conn.execute(
        f"""SELECT v.id, v.shot_id, v.video_path
             FROM shot_versions v
             JOIN shots s ON s.id=v.shot_id
            WHERE v.status='quarantined'
              AND COALESCE(v.video_path,'') <> ''
              AND NOT EXISTS (
                SELECT 1 FROM shot_versions ok
                 WHERE ok.shot_id=v.shot_id
                   AND (ok.status='succeeded' OR ok.video_slot_active=1)
              )
              AND v.created_at=(
                SELECT MAX(x.created_at) FROM shot_versions x
                 WHERE x.shot_id=v.shot_id AND x.status='quarantined'
                   AND COALESCE(x.video_path,'') <> ''
              )
              AND (
                NOT ({_HAS_SEGMENT_SQL})
                OR (
                  COALESCE(json_extract(v.image_inputs,'$.segment_identity_fingerprint'),'') <> ''
                  AND json_extract(v.image_inputs,'$.segment_identity_fingerprint')
                      = {_CURRENT_FINGERPRINT_SQL}
                )
              )
            ORDER BY v.created_at DESC LIMIT ?""",
        (max(1, int(limit)),),
    ).fetchall()
    released = 0
    for row in rows:
        if not Path(str(row["video_path"])).exists():
            continue  # 台账有记录但素材已不在盘上，不能谎称可用
        changed = conn.execute(
            """UPDATE shot_versions
                  SET status='succeeded', error=NULL, video_slot_active=0
                WHERE id=? AND status='quarantined'""",
            (str(row["id"]),),
        )
        if changed.rowcount == 1:
            released += 1
    if released:
        conn.commit()
    return released


STRAY_ACTIVE_SLOT_STALE_REASON = (
    "该版本对应的分镜身份合同已被后续修订替换，系统自检时自动转为历史版本，不可采纳"
)


def converge_stray_active_slot_versions(conn, limit: int) -> dict[str, int]:
    """收敛「succeeded 且仍占槽、但没有存活 job」的历史脏数据（同批次、同 ``limit`` 节奏）。

    正常结算路径（``job_state._set_job`` 终态分支）把 ``jobs.video_slot_active`` 与
    ``shot_versions.video_slot_active`` 在同一次 UPDATE 里一起清零；按这个不变式，
    「succeeded + video_slot_active=1」只可能来自绕过该结算路径的历史脏写——2026-09-30 前的
    ``release_orphan_quarantined_versions`` 就是一例，见其文档。本函数负责收敛已经落库的
    存量坏数据，新发生的不再产生（上面的函数已改成落 0）。

    「没有存活 job」用的镜头级状态集合与 ``identity_workspace._assert_idle_current`` 的
    「仍有视频任务」判据保持一致（``queued``/``running``/``waiting_provider``/
    ``waiting_retry``/``waiting_human``/``paused``），避免两处对「是否在途」出现分歧判断，
    真正在跑的任务（哪怕租约刚好在这一刻过期、还没被 sweeper 复位）不会被本函数误收。
    指纹判据与 ``release_orphan_quarantined_versions`` 同一份，不重复注释：指纹对得上（或
    本镜没有 2.x 合同）只清槽位，回到正常结算态，继续留作候选；指纹对不上（或压根没记录）
    连同槽位一起转 ``stale``，不能悄悄冒充当前分镜的产出。
    """
    rows = conn.execute(
        f"""SELECT v.id, v.shot_id,
                  {_HAS_SEGMENT_SQL} AS has_segment,
                  COALESCE(json_extract(v.image_inputs,'$.segment_identity_fingerprint'),'') AS fp,
                  COALESCE({_CURRENT_FINGERPRINT_SQL},'') AS current_fp
             FROM shot_versions v
             JOIN shots s ON s.id=v.shot_id
            WHERE v.status='succeeded' AND v.video_slot_active=1
              AND NOT EXISTS (
                SELECT 1 FROM jobs j
                 WHERE j.shot_id=v.shot_id AND j.abandoned=0
                   AND j.status IN (
                       'queued','running','waiting_provider',
                       'waiting_retry','waiting_human','paused'
                   )
              )
            ORDER BY v.created_at LIMIT ?""",
        (max(1, int(limit)),),
    ).fetchall()
    converged = 0
    staled = 0
    for row in rows:
        matches = not row["has_segment"] or (bool(row["fp"]) and row["fp"] == row["current_fp"])
        if matches:
            changed = conn.execute(
                "UPDATE shot_versions SET video_slot_active=0 WHERE id=? AND video_slot_active=1",
                (row["id"],),
            )
        else:
            changed = conn.execute(
                """UPDATE shot_versions SET status='stale', video_slot_active=0, error=?
                    WHERE id=? AND video_slot_active=1""",
                (STRAY_ACTIVE_SLOT_STALE_REASON, row["id"]),
            )
        if changed.rowcount != 1:
            continue  # 并发已经改动这一行（比如租约恢复接管了它），这一轮跳过不强改
        # jobs 侧槽位理论上在这类坏数据里已经是 0（该 job 在隔离/结算时已清过），这里
        # 一并兜底清零只为防御式收口，不依赖它——uq_jobs_active_video_shot 保证至多一行。
        conn.execute(
            "UPDATE jobs SET video_slot_active=0 WHERE shot_id=? AND video_slot_active=1",
            (row["shot_id"],),
        )
        converged += int(matches)
        staled += int(not matches)
    if converged or staled:
        conn.commit()
    return {"stray_slot_converged": converged, "stray_slot_staled": staled}


def release_and_converge_quarantine(conn, limit: int) -> dict[str, int]:
    """``job_recovery._reconcile_stalled_video_jobs`` 的单一调用入口：同批次跑放行 +
    收敛，返回值直接喂给调用方的 report 字典。放在这里（而不是内联进
    ``job_recovery.py``）只为不把那个文件的行数/函数行数棘轮再往上推——它已经卡在
    FILE_CONVENTIONS.toml 的基线（500 行/单函数 145 行）上，不允许上调。"""
    return {
        "quarantine_released": release_orphan_quarantined_versions(conn, limit),
        **converge_stray_active_slot_versions(conn, limit),
    }
