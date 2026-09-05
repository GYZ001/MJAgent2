"""开机自愈：把「已下载成功但还没做技术校验」的视频版本补成正规候选。

视频 job 的收尾链是 commit_result_checkpoint（版本 succeeded、文件落盘）→ run_auto_qa
→ record_video_candidate（技术校验 + 证据工件）→ adopt_and_settle_candidate。服务重启
恰好落在第一步之后时，版本留成 succeeded + technical_validation_json 为空 + 仍占着
video_slot_active：覆盖账本只认 technical.passed 的候选，看不见它；新一轮 supervisor 去
重拍又撞 uq_versions_active_video_shot，等 12 轮后转人工 → L6 → 整集 PARTIAL
（2026-09-05 我欲封天：74 个成功版本里 17 个是这种半成品，第 11/14 集因此判失败）。

这里不重拍、不丢弃：文件在盘上就补做技术校验（纯文件检查，不发模型调用），QA 记为
「未复核」（账本按 qa_recovered 降权但可采用），并释放版本/任务槽位——等价于把被打断
的收尾链走完。文件不在盘上的版本这里不动：那是另一类问题，留给账本按「无可用候选」处理。
"""
from __future__ import annotations

import json
import logging
import os

from app.db import get_conn, now
from app.evidence.media import record_video_candidate

_LOGGER = logging.getLogger(__name__)

UNVERIFIED_QA = {"status": "unverified", "qa_recovered": True, "reason": "service_restart"}


def _unvalidated_succeeded_versions(conn) -> list:
    return conn.execute(
        """SELECT v.id, v.shot_id, v.video_path, v.qa_json
             FROM shot_versions v
             JOIN shots s ON s.id=v.shot_id
             JOIN episodes e ON e.id=s.episode_id
             JOIN projects p ON p.id=e.project_id -- ALL_OWNERS: startup recovery
             -- heals every owner's interrupted video candidates after a restart
            WHERE v.status='succeeded' AND v.video_path IS NOT NULL AND v.video_path!=''
              AND (v.technical_validation_json IS NULL OR v.technical_validation_json IN ('', '{}'))
              AND p.deleted_at IS NULL
            ORDER BY v.created_at""",
    ).fetchall()


def recover_unvalidated_video_candidates() -> int:
    """返回补齐的版本数。逐版本独立提交：一个坏文件不影响其它版本收尾。"""
    conn = get_conn()
    healed = 0
    for row in _unvalidated_succeeded_versions(conn):
        path = str(row["video_path"] or "")
        if not os.path.isfile(path):
            continue
        try:
            record_video_candidate(row["id"])
        except Exception as exc:  # noqa: BLE001 单个版本校验失败只记日志，不阻断开机恢复
            _LOGGER.warning("补做技术校验失败 version=%s: %s", row["id"], exc)
            continue
        qa_json = str(row["qa_json"] or "").strip()
        conn.execute(
            """UPDATE shot_versions
                  SET video_slot_active=0,
                      qa_json=CASE WHEN ? THEN qa_json ELSE ? END
                WHERE id=?""",
            (bool(qa_json and qa_json != "{}"), json.dumps(UNVERIFIED_QA, ensure_ascii=False), row["id"]),
        )
        conn.execute(
            "UPDATE jobs SET video_slot_active=0, updated_at=? WHERE version_id=? AND status='succeeded'",
            (now(), row["id"]),
        )
        conn.commit()
        healed += 1
    return healed
