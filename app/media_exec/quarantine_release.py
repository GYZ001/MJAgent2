"""隔离素材的放行判据（拆自 ``job_recovery``，守住 500 行文件红线）。"""
from __future__ import annotations

from pathlib import Path


def release_orphan_quarantined_versions(conn, limit: int) -> int:
    """放行「本镜没有任何可用版本、素材却真实存在」的隔离版本。

    隔离本身仍然有效——并发竞争里落败的那一版必须隔离，避免同一镜出现两个
    采用候选。判据因此从数据推导，不看隔离文案：本镜存在 succeeded 版本时
    一律不动（那才是真正的重复产出）；只有当本镜**一个可用版本都没有**、而
    隔离版的视频文件确实躺在盘上时，才说明这是成本预算退场前那套
    ``video_slot_active=0 → adoptable=False`` 机器扔掉的好素材——用户已经等了
    6 次生成却拿不到任何能用的东西，继续隔离纯粹是为已废止概念服务的拦路石。
    每镜只放行最新的一版，避免一次冒出多个候选。

    「一个可用版本都没有」必须与 ``uq_versions_active_video_shot``（每镜至多一行
    ``video_slot_active=1``）同一判据：本镜若有别的版本正占着槽位（新一轮生成已在
    排队/运行），这镜就不是孤儿，放行会在 UPDATE 上撞唯一索引。2026-09-02 计算服务器
    上就是这样：v1 隔离、v2 queued 占槽，放行 v1 抛 IntegrityError，把启动恢复整个打死。
    """
    rows = conn.execute(
        """SELECT v.id, v.shot_id, v.video_path
             FROM shot_versions v
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
            ORDER BY v.created_at DESC LIMIT ?""",
        (max(1, int(limit)),),
    ).fetchall()
    released = 0
    for row in rows:
        if not Path(str(row["video_path"])).exists():
            continue  # 台账有记录但素材已不在盘上，不能谎称可用
        changed = conn.execute(
            """UPDATE shot_versions
                  SET status='succeeded', error=NULL, video_slot_active=1
                WHERE id=? AND status='quarantined'""",
            (str(row["id"]),),
        )
        if changed.rowcount == 1:
            released += 1
    if released:
        conn.commit()
    return released
