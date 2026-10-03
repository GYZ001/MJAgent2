"""「修订本段」保留修订前采用版本（用户 2026-10-03 拍板）。

背景：``app/domain/storyboard_ops/identity_workspace.py::save_identity_candidate``
此前把该段 ``shots.adopted_version_id`` 无条件置 NULL，若新一轮生成连续失败
（供应商拒收等），这一段会在采用链路上彻底空缺（真实回归：第 1 集第 33 段）。
现改为：原采用版本（若有）继续采用，打上
``app.evidence.identity_revision_retention.RETAINED_AFTER_REVISION_MARKER``
标记，直到新版本被采用（人工或系统代采）为止。

本文件覆盖：
1. 标记模块的读写往返（marker roundtrip）；
2. 交付清单（strict/partial 两个 manifest 函数）把保留版本当有效交付，带
   ``retained_after_revision`` 标记，且 manifest_hash 能区分它；
3. 人工采纳新版本会把保留版本转普通 stale；
4. 系统代采在「已有可用采用版本」时本就不会换新（对所有镜头一致，不是本功能
   新增的差异）；仅当保留版本跌出候选池时才会换人，这时会被转 stale；
5. 字幕台词按版本冻结的快照取词，不读修订后的当前合同；
6. 没有原采用版本时行为不变。
"""
from __future__ import annotations

import json
import sqlite3

from app import api
from app.evidence.identity_revision_retention import (
    RETAINED_AFTER_REVISION_MARKER,
    is_retained_after_revision,
    mark_retained_after_revision,
    release_if_retained_after_revision,
    release_retained_marker_as_stale,
    retained_dialogue_snapshot,
)
from app.subtitles.episode import shot_line_specs
from tests.conftest import patch_api_everywhere
from tests.test_episode_partial_concat import _database, _version
from tests.test_segment_identity_workspace import candidate_of, fixture  # noqa: F401  复用真实身份工作台夹具
from app.production.storyboard_identity_contract import identity_contract_fingerprint
from app.domain.storyboard_ops import identity_workspace as workspace


_SNAPSHOT = {
    "dialogue": [{"utterance_id": "U01", "speaker_identity_id": "bible:孟浩",
                  "line": "修订前的旧台词。", "delivery_kind": "inner_monologue"}],
    "resources": {"characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩"}]},
}


def _conn_with_version(tmp_path, *, marker: bool) -> sqlite3.Connection:
    conn = _database((1,))
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=True)
    if marker:
        mark_retained_after_revision(conn, "v1", dialogue_snapshot=_SNAPSHOT)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1) 标记模块读写往返
# ---------------------------------------------------------------------------

def test_marker_roundtrip_and_dialogue_snapshot(tmp_path) -> None:
    conn = _conn_with_version(tmp_path, marker=False)
    assert not is_retained_after_revision(
        conn.execute("SELECT adoption_reason FROM shot_versions WHERE id='v1'").fetchone()[0]
    )
    mark_retained_after_revision(conn, "v1", dialogue_snapshot=_SNAPSHOT)
    reason = conn.execute("SELECT adoption_reason FROM shot_versions WHERE id='v1'").fetchone()[0]
    assert RETAINED_AFTER_REVISION_MARKER in reason
    assert retained_dialogue_snapshot(conn, "v1") == _SNAPSHOT
    # 其余 image_inputs 字段（若有）不会被覆盖——这里本来就没有，验证合并路径
    # 不炸即可；release 之后标记与状态一起清空。
    release_retained_marker_as_stale(conn, "v1", reason="测试替换")
    row = conn.execute("SELECT status,error FROM shot_versions WHERE id='v1'").fetchone()
    assert row["status"] == "stale" and row["error"] == "测试替换"


def test_release_if_retained_after_revision_is_a_safe_no_op(tmp_path) -> None:
    conn = _conn_with_version(tmp_path, marker=False)
    release_if_retained_after_revision(conn, None, reason="不应该发生")
    release_if_retained_after_revision(conn, "v1", reason="不是保留版本，不应该生效")
    assert conn.execute("SELECT status FROM shot_versions WHERE id='v1'").fetchone()[0] == "succeeded"


# ---------------------------------------------------------------------------
# 2) 交付清单把保留版本当有效交付，带标记，哈希能区分
# ---------------------------------------------------------------------------

def _commit_shot_video_artifact(conn, *, shot_id: str, version_id: str, path) -> str:
    from app.evidence import repository
    from app.harness.types import Evaluation, EvidenceArtifact

    artifact = repository.create_artifact(EvidenceArtifact(
        type="shot_video", scope_type="shot", scope_id=shot_id,
        content={"kind": "shot_video", "version_id": version_id},
        file_path=str(path), status="validated", trust_level="T2",
        contract_version="video-2.0.0",
    ), conn=conn)
    artifact = repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="file", evaluator_name="video_technical_validator",
        evaluator_version="1", status="passed", hard_gate_passed=True, score=100,
    )])
    conn.execute(
        "UPDATE shot_versions SET artifact_id=?,technical_validation_json=? WHERE id=?",
        (artifact["id"], json.dumps({"passed": True, "issues": []}), version_id),
    )
    return artifact["id"]


def test_manifest_includes_retained_version_with_marker_and_distinct_hash(tmp_path, monkeypatch) -> None:
    from app import downstream_authority
    from app.evidence import repository

    conn = _conn_with_version(tmp_path, marker=True)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    video_path = conn.execute("SELECT video_path FROM shot_versions WHERE id='v1'").fetchone()[0]
    _commit_shot_video_artifact(conn, shot_id="s1", version_id="v1", path=video_path)
    conn.commit()

    strict = downstream_authority.current_adopted_video_delivery_manifest("e", conn=conn)
    partial = downstream_authority.current_partial_adopted_video_delivery_manifest("e", conn=conn)
    for manifest in (strict, partial):
        item = manifest["items"][0]
        assert item["shot_no"] == 1
        assert item["retained_after_revision"] is True

    # 不带标记时同一份视频/证据链得到不同的 manifest_hash——修订前后两次交付
    # 不会被当成同一份产物。
    conn.execute("UPDATE shot_versions SET adoption_reason=NULL WHERE id='v1'")
    unmarked = downstream_authority.current_adopted_video_delivery_manifest("e", conn=conn)
    assert unmarked["items"][0]["retained_after_revision"] is False
    assert unmarked["manifest_hash"] != strict["manifest_hash"]


# ---------------------------------------------------------------------------
# 3) 人工采纳新版本把保留版本转普通 stale
# ---------------------------------------------------------------------------

def test_manual_adoption_of_new_version_releases_retained_marker(tmp_path, monkeypatch) -> None:
    conn = _conn_with_version(tmp_path, marker=True)
    new_path = tmp_path / "new.mp4"
    new_path.write_bytes(b"new-video")
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
                                     video_path,technical_validation_json,created_at)
           VALUES('v2','s1',2,'prompt','key-2','succeeded',?,?,0)""",
        (str(new_path), json.dumps({"passed": True})),
    )
    conn.commit()

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *_args: None)
    monkeypatch.setattr(api.evidence_repository, "commit_artifact", lambda *_args, **_kwargs: {})
    patch_api_everywhere(monkeypatch, "_review_write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api.worker, "invalidate_episode_final", lambda *_args: None)
    from app.evidence import media as media_evidence
    monkeypatch.setattr(media_evidence, "record_video_candidate", lambda *_args, **_kwargs: {"id": "art-v2"})

    result = api._adopt_version_core("s1", {"version_id": "v2", "reason": "新版本生成成功，人工改用新版本"})

    assert result["adopted"] == "v2"
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()[0] == "v2"
    old = conn.execute("SELECT status,adoption_reason FROM shot_versions WHERE id='v1'").fetchone()
    assert old["status"] == "stale"
    assert not is_retained_after_revision(old["adoption_reason"])


def test_readopting_the_same_retained_version_overwrites_its_own_reason_text(tmp_path, monkeypatch) -> None:
    """只调倍速、version_id 没变：这是一次新的人工确认，会按既有 adoption_reason
    语义整段覆盖该版本自己的理由文案（与 AUTO_ADOPT_REASON_MARKER 的既有先例
    一致：人工重新确认覆盖旧标记）——不属于「被新版本替换」，不触发本功能新增
    的保留->stale 转换，版本仍是 succeeded 且仍是采用指针指向的那一版。"""
    conn = _conn_with_version(tmp_path, marker=True)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *_args: None)
    monkeypatch.setattr(api.evidence_repository, "commit_artifact", lambda *_args, **_kwargs: {})
    patch_api_everywhere(monkeypatch, "_review_write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api.worker, "invalidate_episode_final", lambda *_args: None)
    from app.evidence import media as media_evidence
    monkeypatch.setattr(media_evidence, "record_video_candidate", lambda *_args, **_kwargs: {"id": "art-v1"})
    conn.execute("UPDATE shot_versions SET technical_validation_json=? WHERE id='v1'", (json.dumps({"passed": True}),))
    conn.commit()

    api._adopt_version_core("s1", {"version_id": "v1", "reason": "重新确认同一版本，调整播放倍速", "playback_rate": 1.2})

    row = conn.execute("SELECT status,adoption_reason FROM shot_versions WHERE id='v1'").fetchone()
    assert row["status"] == "succeeded"
    assert row["adoption_reason"] == "重新确认同一版本，调整播放倍速"
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()[0] == "v1"


# ---------------------------------------------------------------------------
# 4) 系统代采：保留版本不享受 sticky——池里只要有修订后的新版本就必须换人并
#    清退标记；没有别的版本时才继续沿用保留版本顶着；跌出候选池同样换人。
# ---------------------------------------------------------------------------

def test_auto_adopt_replaces_retained_version_when_new_version_is_available(tmp_path) -> None:
    """2026-10-03 修复：此前这条测试断言 ``result["version_id"] == "v1"``——
    那是旧（错误）语义，等价于『保留版本永远 sticky』，会让修订后重新生成
    成功的新版本永远不被自动采用，旧视频一直顶着而不是『新版本到位前顶着』。
    已改正为：保留版本不享受 sticky，池里有新版本就换人。"""
    import app.evidence.media as media_module

    conn = _conn_with_version(tmp_path, marker=True)
    conn.execute("UPDATE shot_versions SET technical_validation_json=? WHERE id='v1'", (json.dumps({"passed": True}),))
    new_path = tmp_path / "new.mp4"
    new_path.write_bytes(b"new-video")
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
                                     video_path,technical_validation_json,created_at)
           VALUES('v2','s1',2,'prompt','key-2','succeeded',?,?,0)""",
        (str(new_path), json.dumps({"passed": True})),
    )
    conn.commit()

    orig_get_conn = media_module.get_conn
    media_module.get_conn = lambda: conn
    try:
        result = media_module.select_best_video_candidate("s1")
    finally:
        media_module.get_conn = orig_get_conn

    assert result["version_id"] == "v2", "保留版本不享受 sticky：池里有修订后的新版本就必须换人"
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()[0] == "v2"
    old = conn.execute("SELECT status,adoption_reason FROM shot_versions WHERE id='v1'").fetchone()
    assert old["status"] == "stale", "新版本真替换保留版本后，旧版本必须转 stale，不能继续占着交付位置"
    assert not is_retained_after_revision(old["adoption_reason"]), "替换发生后保留标记必须随之清除"


def test_auto_adopt_replaces_and_releases_marker_when_retained_version_falls_out_of_pool(tmp_path) -> None:
    """保留版本后来跌出候选池（例如被别的流程重新隔离）：系统代采换成新的技术
    有效候选，旧保留版本随之转普通 stale，标记消失——与人工路径一致。"""
    import app.evidence.media as media_module

    conn = _conn_with_version(tmp_path, marker=True)
    conn.execute("UPDATE shot_versions SET status='quarantined' WHERE id='v1'")
    new_path = tmp_path / "new.mp4"
    new_path.write_bytes(b"new-video")
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
                                     video_path,technical_validation_json,created_at)
           VALUES('v2','s1',2,'prompt','key-2','succeeded',?,?,0)""",
        (str(new_path), json.dumps({"passed": True})),
    )
    conn.commit()

    orig_get_conn = media_module.get_conn
    media_module.get_conn = lambda: conn
    try:
        result = media_module.select_best_video_candidate("s1")
    finally:
        media_module.get_conn = orig_get_conn

    assert result["version_id"] == "v2"
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()[0] == "v2"
    old = conn.execute("SELECT status,adoption_reason FROM shot_versions WHERE id='v1'").fetchone()
    assert old["status"] == "stale"
    assert not is_retained_after_revision(old["adoption_reason"])


# ---------------------------------------------------------------------------
# 5) 字幕台词取版本冻结的快照，不读修订后的当前合同
# ---------------------------------------------------------------------------

def test_shot_line_specs_prefers_dialogue_snapshot_over_current_contract() -> None:
    current_contract_row = {
        "shot_contract_json": json.dumps({"storyboard_pack_segment": {
            "resources": {"characters": [{"identity_id": "bible:孟浩", "display_name": "孟浩"}]},
            "dialogue": [{"utterance_id": "U01", "speaker_identity_id": "bible:孟浩",
                          "line": "修订后的新台词。", "delivery_kind": "inner_monologue"}],
        }}),
        "dialogues": "[]",
    }
    without_snapshot = shot_line_specs(current_contract_row)
    assert without_snapshot[0].text == "修订后的新台词。"

    with_snapshot = shot_line_specs(current_contract_row, dialogue_snapshot=_SNAPSHOT)
    assert with_snapshot[0].text == "修订前的旧台词。"
    assert with_snapshot[0].speaker == "孟浩"


def test_shot_line_specs_empty_snapshot_means_no_dialogue_not_fallback() -> None:
    row = {"shot_contract_json": json.dumps({"storyboard_pack_segment": {
        "resources": {"characters": []}, "dialogue": [{"utterance_id": "U01",
        "speaker_identity_id": "x", "line": "不应该出现。"}],
    }})}
    assert shot_line_specs(row, dialogue_snapshot={"dialogue": [], "resources": {"characters": []}}) == []


# ---------------------------------------------------------------------------
# 6) 没有原采用版本时行为不变
# ---------------------------------------------------------------------------

def test_save_identity_candidate_without_prior_adoption_is_unchanged(fixture) -> None:
    conn, segment, _, _ = fixture
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id='s1'")
    conn.commit()
    result = workspace.save_identity_candidate(
        conn, shot_id="s1", baseline=identity_contract_fingerprint(segment), candidate=candidate_of(segment),
    )
    assert result["retained_version_id"] is None
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()[0] is None
    v1 = conn.execute("SELECT status,adoption_reason FROM shot_versions WHERE id='v1'").fetchone()
    assert v1["status"] == "stale"
    assert not is_retained_after_revision(v1["adoption_reason"])


# ---------------------------------------------------------------------------
# 7) 真实 Artifact 谱系回归：supersede/stale 级联不得误伤保留版本
#
# review 实测复现：``_record_identity_revision`` 建新 storyboard_shot Artifact
# 时，旧 storyboard_shot Artifact 被 supersede，``create_and_commit_artifact_
# in_transaction`` 的级联会把它的全部后代（包括保留版本的 shot_video
# Artifact，其 parent_artifact_ids 指向旧 storyboard_shot Artifact）标记
# stale，导致 downstream_authority 判它「视频 Artifact 已失效」——与
# test_manifest_includes_retained_version_with_marker_and_distinct_hash 的差异
# 是：那条测试手工插入一条不带谱系的干净 Artifact，测不出这条级联；这里用
# fixture 走 _record_identity_revision 的真实创建路径，建出会被级联命中的
# 谱系。
# ---------------------------------------------------------------------------

def _link_real_artifact_lineage(conn, segment: dict) -> str:
    """按真实数据形态补全 fixture 缺的两环：s1 的 storyboard_shot Artifact，
    以及 v1 的 shot_video Artifact（parent 指向前者）——与生产环境
    ``_record_identity_revision``/``record_video_candidate`` 建出的谱系形状
    一致。返回 shot_video Artifact id。"""
    from app.evidence import repository
    from app.harness.contracts import get_contract
    from app.harness.types import Evaluation, EvidenceArtifact

    from app.domain.storyboard_ops.mutation_primitives import _board_from_shot_rows

    saved = conn.execute("SELECT * FROM shots WHERE id='s1'").fetchone()
    shot = _board_from_shot_rows([saved], 1).shots[0]
    sb_artifact = repository.create_and_commit_artifact_in_transaction(conn, EvidenceArtifact(
        type="storyboard_shot", scope_type="storyboard_checkpoint", scope_id="ep:1",
        status="candidate", trust_level="T1", content=shot.model_dump(mode="json"),
        parent_artifact_ids=[], contract_version=get_contract("storyboard").version,
    ), [Evaluation(evaluator_type="deterministic", evaluator_name="segment_identity_revision",
                    evaluator_version=segment["identity_contract_version"], status="passed",
                    hard_gate_passed=True, score=100, evidence={})])
    conn.execute("UPDATE shots SET storyboard_artifact_id=? WHERE id='s1'", (sb_artifact["id"],))
    video_path = conn.execute("SELECT video_path FROM shot_versions WHERE id='v1'").fetchone()[0]
    video_artifact = repository.create_artifact(EvidenceArtifact(
        type="shot_video", scope_type="shot", scope_id="s1",
        status="validated", trust_level="T3", file_path=video_path,
        content={"version_id": "v1", "version_no": 1, "prompt_text": segment["prompt_text"],
                 "provider_task_id": None},
        parent_artifact_ids=[sb_artifact["id"]], contract_version="video-2.0.0",
    ), conn=conn)
    video_artifact = repository.commit_artifact(None, video_artifact["id"], [Evaluation(
        evaluator_type="file", evaluator_name="video_technical_validator", evaluator_version="1.0.0",
        status="passed", hard_gate_passed=True, score=100,
    )])
    conn.execute(
        "UPDATE shot_versions SET artifact_id=?, technical_validation_json=? WHERE id='v1'",
        (video_artifact["id"], json.dumps({"passed": True, "issues": []})),
    )
    conn.commit()
    return video_artifact["id"]


def test_revision_does_not_cascade_stale_onto_retained_video_artifact(fixture) -> None:
    conn, segment, _, _ = fixture
    video_artifact_id = _link_real_artifact_lineage(conn, segment)

    result = workspace.save_identity_candidate(
        conn, shot_id="s1", baseline=identity_contract_fingerprint(segment), candidate=candidate_of(segment),
    )
    assert result["retained_version_id"] == "v1"

    after = conn.execute(
        "SELECT status, stale_reason FROM artifacts WHERE id=?", (video_artifact_id,),
    ).fetchone()
    assert after["status"] in ("validated", "approved"), after["stale_reason"]


def test_revision_lineage_cascade_regression_keeps_partial_manifest_from_skipping_shot(fixture) -> None:
    from app import downstream_authority

    conn, segment, _, _ = fixture
    _link_real_artifact_lineage(conn, segment)

    workspace.save_identity_candidate(
        conn, shot_id="s1", baseline=identity_contract_fingerprint(segment), candidate=candidate_of(segment),
    )
    partial = downstream_authority.current_partial_adopted_video_delivery_manifest("ep", conn=conn)
    assert 1 not in (partial.get("skipped_shot_nos") or [])
    shot1_item = next(item for item in partial["items"] if item["shot_no"] == 1)
    assert shot1_item["retained_after_revision"] is True


# ---------------------------------------------------------------------------
# 8) 连续两次修订不得用「第二次修订前」覆盖已冻结的台词快照
#
# review 实测复现：若保留版本已经打过标记（第一次修订），第二次修订传入的
# dialogue_snapshot 是「第二次修订前的当前合同」——那已经是第一次修订后的新
# 台词，不是这条物理视频文件实际生成时依据的最初台词。用
# ``app.production.storyboard_dialogue_revision.revise_segment_dialogue`` 走
# 台词人工修订的合法入口（直接改 ``dialogue[].line`` 会被必保台词溯源校验拒
# 绝，走不到这里要测的分支）。
# ---------------------------------------------------------------------------

def test_second_revision_keeps_first_revisions_frozen_dialogue_snapshot(fixture) -> None:
    from app.production.storyboard_dialogue_revision import revise_segment_dialogue

    conn, segment, _, _ = fixture
    original_line = segment["dialogue"][0]["line"]

    candidate1 = revise_segment_dialogue(
        segment, {"U01": "修订一次后的台词C1。"}, reason="测试", narrator_voice_character="",
    )
    r1 = workspace.save_identity_candidate(
        conn, shot_id="s1", baseline=identity_contract_fingerprint(segment), candidate=candidate1,
    )
    assert retained_dialogue_snapshot(conn, "v1")["dialogue"][0]["line"] == original_line

    prepared1 = r1["segment"]
    candidate2 = revise_segment_dialogue(
        prepared1, {"U01": "修订两次后的台词C2。"}, reason="测试2", narrator_voice_character="",
    )
    r2 = workspace.save_identity_candidate(
        conn, shot_id="s1", baseline=identity_contract_fingerprint(prepared1), candidate=candidate2,
    )
    assert r2["retained_version_id"] == "v1"
    snapshot_after_second_revision = retained_dialogue_snapshot(conn, "v1")["dialogue"][0]["line"]
    assert snapshot_after_second_revision == original_line, (
        "v1 视频实际内容从未重新生成，字幕快照必须一直是它首次被保留时冻结的那句，"
        "不能被第二次修订的『修订前』(已经是第一次修订后的新台词) 覆盖"
    )


# ---------------------------------------------------------------------------
# 9) 生成台/成片台可见信号：public_shot_versions / episode_detail /
#    episode_mix_status 三处投影字段的真实输出
# ---------------------------------------------------------------------------

def test_public_shot_versions_projects_retained_after_revision_field(tmp_path) -> None:
    from app.domain.storyboard_ops.public_shot_versions import _public_shot_versions

    conn = _conn_with_version(tmp_path, marker=True)
    versions = _public_shot_versions(conn, "s1", include_inputs=True)
    assert versions[0]["retained_after_revision"] is True

    conn2 = _conn_with_version(tmp_path, marker=False)
    versions2 = _public_shot_versions(conn2, "s1", include_inputs=True)
    assert versions2[0]["retained_after_revision"] is False


def test_episode_detail_adopted_video_retained_after_revision_flag() -> None:
    from app.domain.storyboard_ops.episode_detail import _adopted_video_retained_after_revision

    retained_shot = {"adopted_version_id": "v1", "versions": [
        {"id": "v1", "retained_after_revision": True},
        {"id": "v2", "retained_after_revision": False},
    ]}
    assert _adopted_video_retained_after_revision(retained_shot) is True

    plain_shot = {"adopted_version_id": "v2", "versions": [
        {"id": "v1", "retained_after_revision": True},
        {"id": "v2", "retained_after_revision": False},
    ]}
    assert _adopted_video_retained_after_revision(plain_shot) is False

    no_versions_shot = {"adopted_version_id": "v1", "versions": []}
    assert _adopted_video_retained_after_revision(no_versions_shot) is False


def test_episode_mix_status_reports_retained_after_revision_per_shot(tmp_path, monkeypatch) -> None:
    from app.media_exec import concat as concat_module

    conn = _conn_with_version(tmp_path, marker=True)
    monkeypatch.setattr(concat_module, "get_conn", lambda: conn)

    status = concat_module.episode_mix_status("e")
    shot1 = next(item for item in status["shots"] if item["shot_no"] == 1)
    assert shot1["retained_after_revision"] is True
