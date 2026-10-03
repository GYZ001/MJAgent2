"""放行/收敛判据必须认「当前分镜合同」，不能把已被修订替换的旧素材冒充候选。

生产实例（proj_ca86b15ab7d7 / ep_a3c61162b4ce / 第 10 段，shot_61b2e668c1f0）：
修订本段（``identity_workspace.save_identity_candidate``）只把 succeeded 候选转
stale，漏掉了同镜一条更早的 quarantined 候选（v1，历史供应商任务晚到）；随后
``release_orphan_quarantined_versions`` 把这条属于旧合同的隔离版本当「孤儿」放
行，还把 ``video_slot_active`` 错误置 1——顶替了新合同应有的候选（内容错），
并让此后每次「修订本段」保存都被 ``_assert_idle_current`` 判定「该片段仍有视
频任务」而失败、界面无出路（拦用户）。

本文件覆盖四点：
1. 修订本段作废未被保留采用的 succeeded 与 quarantined 候选（原采用版本改为
   保留——用户 2026-10-03 拍板，见 app.evidence.identity_revision_retention）；
2. 放行只认指纹匹配当前分镜合同的隔离版本，非 2.x 镜头维持原语义；
3. 收敛存量坏数据（succeeded 但仍占槽、且没有存活 job），真在途的不动；
4. 收敛后，此前被卡住的「修订本段」保存恢复可用。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import db
from app.domain.storyboard_ops import identity_workspace as workspace
from app.media_exec import quarantine_release
from app.production.storyboard_identity_contract import identity_contract_fingerprint, stamp_identity_contract
from app.production.storyboard_speech_render import render_segment_speech
from tests.test_segment_identity_workspace import candidate_of, fixture  # noqa: F401  复用真实身份工作台夹具
from tests.test_video_stall_recovery import _conn as _base_conn, _seed


def _segment(line: str = "我一定会回来。") -> dict:
    segment = dict(
        segment_no=1, synopsis="孟浩自述", source_segment_indexes=[1], beat_ids=["B1"],
        beats=[{"beat_id": "B1", "summary": "孟浩自述", "segment_indexes": [1]}], shot_count=2, duration_s=15,
        target_model="seedance_2", degraded_capabilities=[],
        prompt_text="镜头1：远处山风。{{speech:U01}} 镜头2：山路空寂。",
        dialogue=[dict(utterance_id="U01", speaker_identity_id="bible:孟浩", line=line,
                       source_segment_index=1, delivery="offscreen_voice", delivery_kind="inner_monologue")],
        resources={"characters": [dict(identity_id="bible:孟浩", display_name="孟浩",
                                        subject_kind="character", visibility="voice_only")],
                   "scenes": [], "props": []},
    )
    render_segment_speech(segment, dialect="seedance")
    stamp_identity_contract(segment)
    return segment


def _add_shot(conn, shot_id: str, shot_no: int, *, segment: dict | None) -> None:
    """按 ``_seed`` 同样的必填列插入一个镜头；``segment`` 非空时写成 2.x 分镜包镜头。"""
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,first_frame_desc,last_frame_desc,
               source_excerpt,narration,dialogues,transition,continuity_from_prev,
               continuity_mode,shot_contract_json
           ) VALUES(?, 'e1', ?, 10, '中景', '固定', '宝阁',
                    '[]', '人物站在宝阁中央。', '人物仍在宝阁中央。', '人物仍在宝阁中央。',
                    '人物仍在宝阁中央。', NULL, '[]', '硬切', 0, 'same_scene_cut', ?)""",
        (
            shot_id, shot_no,
            json.dumps({"storyboard_pack_segment": segment}) if segment is not None else None,
        ),
    )


def _add_version(
    conn, *, vid, shot_id, no, status, slot, video_path, created_at, fingerprint="__absent__",
):
    """``fingerprint`` 默认哨兵表示「不写 image_inputs」（无证据）；显式传 None/字符串控制取值。"""
    image_inputs = None if fingerprint == "__absent__" else json.dumps(
        {"segment_identity_fingerprint": fingerprint}
    )
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
                                     video_slot_active,video_path,created_at,image_inputs)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (vid, shot_id, no, "p", f"idem-{vid}", status, slot, video_path, created_at, image_inputs),
    )


def _add_job(conn, *, job_id: str, shot_id: str, status: str) -> None:
    conn.execute(
        "INSERT INTO jobs(id,kind,shot_id,status,created_at,updated_at) VALUES(?,'video',?,?,?,?)",
        (job_id, shot_id, status, db.now(), db.now()),
    )


def _pack_conn() -> sqlite3.Connection:
    conn = _base_conn()
    _seed(conn)
    conn.executescript(db.INTEGRITY_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# 1) 修订本段同时作废 succeeded 与 quarantined 候选
# ---------------------------------------------------------------------------

def test_revision_stales_both_succeeded_and_quarantined(fixture, tmp_path):
    """v1 是修订前的采用版本：用户 2026-10-03 拍板后继续保留采用（不转
    stale），其余候选（这里的 v1b）照旧全部作废——「同时作废」只对非采用候选
    成立。"""
    conn, segment, _, _ = fixture
    late_video = tmp_path / "late-arrival.mp4"
    late_video.write_bytes(b"late-provider-result")
    # 同镜再插一条 quarantined 候选：历史供应商任务晚到，槽位早被 v1 占了，被隔离。
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
                                     video_slot_active,video_path,created_at)
           VALUES('v1b','s1',2,'p','idem-v1b','quarantined',0,?,?)""",
        (str(late_video), db.now()),
    )
    conn.commit()
    workspace.save_identity_candidate(
        conn, shot_id="s1", baseline=identity_contract_fingerprint(segment), candidate=candidate_of(segment),
    )
    rows = {
        r["id"]: (r["status"], r["video_slot_active"])
        for r in conn.execute("SELECT id,status,video_slot_active FROM shot_versions WHERE shot_id='s1'")
    }
    assert rows["v1"] == ("succeeded", 0), "修订前的采用版本保留采用，不随批量作废被转 stale"
    assert rows["v1b"] == ("stale", 0), "quarantined 候选此前不在 WHERE 范围内，会原样留在 quarantined"
    shot = conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()
    assert shot["adopted_version_id"] == "v1"


# ---------------------------------------------------------------------------
# 2) 放行只认指纹匹配当前分镜合同的隔离版本
# ---------------------------------------------------------------------------

def test_release_matches_mismatches_and_missing_evidence(tmp_path):
    conn = _pack_conn()
    current = _segment("我一定会回来。")
    other = _segment("我们后会有期。")  # 台词不同 -> 指纹不同
    assert current["identity_contract_fingerprint"] != other["identity_contract_fingerprint"]

    _add_shot(conn, "s2", 2, segment=current)  # 指纹匹配
    _add_shot(conn, "s3", 3, segment=current)  # 指纹不匹配（版本记的是旧合同 other 的指纹）
    _add_shot(conn, "s4", 4, segment=current)  # 版本没有指纹证据
    for shot_id, fp in (
        ("s2", current["identity_contract_fingerprint"]),
        ("s3", other["identity_contract_fingerprint"]),
    ):
        video = tmp_path / f"{shot_id}.mp4"
        video.write_bytes(b"x")
        _add_version(
            conn, vid=f"v-{shot_id}", shot_id=shot_id, no=1, status="quarantined", slot=0,
            video_path=str(video), created_at=1.0, fingerprint=fp,
        )
    video_s4 = tmp_path / "s4.mp4"
    video_s4.write_bytes(b"x")
    _add_version(
        conn, vid="v-s4", shot_id="s4", no=1, status="quarantined", slot=0,
        video_path=str(video_s4), created_at=1.0,  # fingerprint 缺省 = 不写 image_inputs
    )
    conn.commit()

    released = quarantine_release.release_orphan_quarantined_versions(conn, 50)
    assert released == 1
    rows = {
        r["id"]: (r["status"], r["video_slot_active"])
        for r in conn.execute("SELECT id,status,video_slot_active FROM shot_versions")
    }
    assert rows["v-s2"] == ("succeeded", 0), "指纹匹配：放行，且落在正常结算终态（不占槽）"
    assert rows["v-s3"] == ("quarantined", 0), "指纹不匹配：不放行，原样保留"
    assert rows["v-s4"] == ("quarantined", 0), "没有指纹证据：不能当匹配处理，不放行"


def test_release_still_permissive_for_legacy_shot_without_2x_contract(tmp_path):
    """非 2.x 镜头（没有 storyboard_pack_segment）没有身份合同，指纹判据不适用，维持原语义放行——
    这类镜头每次编辑都会被 stage_shot_artifact_cleanup 整表清空重建，能留到现在就没有被替代过。"""
    conn = _pack_conn()  # s1 是 _seed 缺省的旧式镜头，没有 shot_contract_json
    video = tmp_path / "v1.mp4"
    video.write_bytes(b"x")
    _add_version(conn, vid="v1", shot_id="s1", no=1, status="quarantined", slot=0,
                 video_path=str(video), created_at=1.0)
    conn.commit()
    assert quarantine_release.release_orphan_quarantined_versions(conn, 50) == 1
    row = conn.execute("SELECT status, video_slot_active FROM shot_versions WHERE id='v1'").fetchone()
    assert (row["status"], row["video_slot_active"]) == ("succeeded", 0)


# ---------------------------------------------------------------------------
# 3) 收敛存量坏数据：succeeded 却仍占槽、且没有存活 job
# ---------------------------------------------------------------------------

def test_converge_clears_slot_when_matching_and_stales_when_not(tmp_path):
    conn = _pack_conn()
    current = _segment("我一定会回来。")
    other = _segment("我们后会有期。")
    _add_shot(conn, "s2", 2, segment=current)
    _add_shot(conn, "s3", 3, segment=current)
    _add_version(conn, vid="v-s2", shot_id="s2", no=1, status="succeeded", slot=1,
                 video_path=str(tmp_path / "s2.mp4"), created_at=1.0,
                 fingerprint=current["identity_contract_fingerprint"])
    _add_version(conn, vid="v-s3", shot_id="s3", no=1, status="succeeded", slot=1,
                 video_path=str(tmp_path / "s3.mp4"), created_at=1.0,
                 fingerprint=other["identity_contract_fingerprint"])
    conn.commit()

    report = quarantine_release.converge_stray_active_slot_versions(conn, 50)
    assert report == {"stray_slot_converged": 1, "stray_slot_staled": 1}
    rows = {
        r["id"]: (r["status"], r["video_slot_active"])
        for r in conn.execute("SELECT id,status,video_slot_active FROM shot_versions")
    }
    assert rows["v-s2"] == ("succeeded", 0), "匹配当前合同：只清槽位，回到正常结算态"
    assert rows["v-s3"] == ("stale", 0), "不匹配：连同槽位一起转 stale，不能冒充当前分镜的产出"
    assert conn.execute(
        "SELECT error FROM shot_versions WHERE id='v-s3'"
    ).fetchone()["error"] == quarantine_release.STRAY_ACTIVE_SLOT_STALE_REASON


@pytest.mark.parametrize("job_status", ["running", "waiting_human", "paused"])
def test_converge_leaves_genuinely_in_flight_version_alone(tmp_path, job_status):
    conn = _pack_conn()
    current = _segment("我一定会回来。")
    _add_shot(conn, "s2", 2, segment=current)
    _add_version(conn, vid="v-s2", shot_id="s2", no=1, status="succeeded", slot=1,
                 video_path=str(tmp_path / "s2.mp4"), created_at=1.0,
                 fingerprint=current["identity_contract_fingerprint"])
    _add_job(conn, job_id="j-s2", shot_id="s2", status=job_status)
    conn.commit()

    report = quarantine_release.converge_stray_active_slot_versions(conn, 50)
    assert report == {"stray_slot_converged": 0, "stray_slot_staled": 0}
    row = conn.execute("SELECT status, video_slot_active FROM shot_versions WHERE id='v-s2'").fetchone()
    assert (row["status"], row["video_slot_active"]) == ("succeeded", 1), "真在途任务：一动不动"


# ---------------------------------------------------------------------------
# 4) 收敛后，此前被卡住的「修订本段」保存恢复可用
# ---------------------------------------------------------------------------

def test_converge_unblocks_save_identity_candidate(fixture):
    conn, segment, _, _ = fixture
    # 模拟生产坏数据：release_orphan_quarantined_versions 修复前把 v1 落成 succeeded+槽位=1。
    conn.execute("UPDATE shot_versions SET video_slot_active=1 WHERE id='v1'")
    conn.commit()
    with pytest.raises(ValueError, match="仍有视频任务"):
        workspace.save_identity_candidate(
            conn, shot_id="s1", baseline=identity_contract_fingerprint(segment), candidate=candidate_of(segment),
        )

    report = quarantine_release.converge_stray_active_slot_versions(conn, 50)
    assert report["stray_slot_converged"] + report["stray_slot_staled"] == 1
    assert conn.execute(
        "SELECT 1 FROM shot_versions WHERE shot_id='s1' AND video_slot_active=1"
    ).fetchone() is None

    result = workspace.save_identity_candidate(
        conn, shot_id="s1", baseline=identity_contract_fingerprint(segment), candidate=candidate_of(segment),
    )
    assert "artifact_id" in result
