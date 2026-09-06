"""视频提交槽位先过机器水位闸：水位超标时不认领、不动供应商侧上限（2026-09-05 用户拍板）。"""
from __future__ import annotations

import pytest

from app.db import get_conn
from app.media_pipeline.scheduler import claim_video_submit_slot
from app.observability import machine_watermark


def _seed() -> None:
    conn = get_conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e1','p1',1,'generating',1)")
    conn.execute(
        """INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
                             characters,action_desc,dialogues,transition)
           VALUES('s1','e1',1,5,'中景','固定','室内','[]','人物站定','[]','硬切')"""
    )
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at)
           VALUES('v1','s1',1,'p','idem','running','{}',1)"""
    )
    conn.execute(
        """INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,lease_owner,lease_expires_at,
                            provider_operation_id,provider_create_state,provider_non_cancellable,created_at,updated_at)
           VALUES('j1','video','s1','v1','e1','p1','running','w1',9999999999,'video-create-v1','not_started',0,1,1)"""
    )
    conn.commit()


def test_claim_is_refused_with_the_watermark_reason_when_machine_is_hot(monkeypatch) -> None:
    _seed()
    monkeypatch.setattr(machine_watermark, "throttle_reason", lambda: "内存占用 88% ≥ 70%")
    claimed, reason = claim_video_submit_slot(
        job_id="j1", lease_owner="w1", episode_id="e1", project_id="p1", version_id="v1",
        operation_id="video-create-v1", amount_cny=0.0, is_auto_retake=False, conn=get_conn(),
    )
    assert claimed is False and reason == "机器水位限流：内存占用 88% ≥ 70%"
    assert not get_conn().in_transaction  # 拒绝路径已回滚，不留挂起事务
    assert get_conn().execute("SELECT provider_non_cancellable FROM jobs WHERE id='j1'").fetchone()[0] == 0


def test_claim_passes_the_gate_when_machine_is_cool(monkeypatch) -> None:
    """水位正常时闸门放行，认领继续走到后面的供应商侧上限与预算 CAS（这里的最小种子过不了
    预算 CAS，抛的是那一步的冲突错误——证明没有在水位闸被拦住）。"""
    _seed()
    monkeypatch.setattr(machine_watermark, "throttle_reason", lambda: None)
    with pytest.raises(ValueError, match="lease/CAS"):
        claim_video_submit_slot(
            job_id="j1", lease_owner="w1", episode_id="e1", project_id="p1", version_id="v1",
            operation_id="video-create-v1", amount_cny=0.0, is_auto_retake=False, conn=get_conn(),
        )
