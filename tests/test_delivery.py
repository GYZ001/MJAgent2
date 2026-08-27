import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app import artifacts, db, delivery, downstream_authority, task_registry
from app.evidence import repository
from app.evidence import media as media_evidence
from app.harness.types import Evaluation, EvidenceArtifact
from app.orchestration import api as orchestration_api


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


def _approved_artifact(kind: str, scope_type: str, scope_id: str, *, file_path: str | None = None) -> str:
    artifact = repository.create_artifact(EvidenceArtifact(
        type=kind, scope_type=scope_type, scope_id=scope_id,
        content={
            "kind": kind,
            **({"version_id": "v"} if kind == "shot_video" else {}),
        },
        file_path=file_path, status="validated", trust_level="T2",
        contract_version="video-2.0.0" if kind == "shot_video" else "1.0.0",
    ))
    artifact = repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="file" if kind == "shot_video" else "deterministic",
        evaluator_name="video_technical_validator" if kind == "shot_video" else "test",
        evaluator_version="1",
        status="passed", hard_gate_passed=True, score=100,
    )])
    return artifact["id"]


def test_delivery_package_reaches_t5_and_feedback_preserves_snapshot(tmp_path, monkeypatch) -> None:
    from app import downstream_authority

    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery, "get_conn", lambda: conn)
    monkeypatch.setattr(orchestration_api, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery.config, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(delivery, "validate_video_file", lambda path, expected_duration_s=5: {
        "passed": True,
        "issues": [],
        "evidence": {"path": path, "duration_s": expected_duration_s},
    })
    release_authority = {
        "published_storyboard_artifact_id": "storyboard-current",
        "storyboard_production_revision_id": "revision-current",
        "storyboard_completion_certificate_id": "certificate-current",
        "release_qualification_hash": "qualification-current",
    }
    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: release_authority,
    )

    bible_id = _approved_artifact("character_bible", "project", "p")
    screenplay_id = _approved_artifact("episode_screenplay", "episode", "e")
    storyboard_id = _approved_artifact("storyboard", "episode", "e")
    video = tmp_path / "candidate.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"video-bytes")
    final_video = tmp_path / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_video.parent.mkdir(parents=True)
    final_video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"final-video")
    video_artifact_id = _approved_artifact("shot_video", "shot", "s", file_path=str(video))

    bible = {"world": {"era": "架空", "genre": "玄幻", "visual_style_canonical": "国漫"}, "characters": []}
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,bible_artifact_id,created_at) VALUES('p','P','planned',?,?,0)",
        (json.dumps(bible, ensure_ascii=False), bible_id),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) "
        "VALUES('p',1,'第一章','第一章正文')"
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,screenplay_json,screenplay_artifact_id,storyboard_artifact_id,status,created_at) "
        "VALUES('e','p',1,'E','[1]','{}',?,?,'done',0)", (screenplay_id, storyboard_id),
    )
    release_authority["published_storyboard_artifact_id"] = storyboard_id
    conn.execute(
        """UPDATE episodes
              SET published_storyboard_artifact_id=?,
                  storyboard_production_revision_id='revision-current',
                  storyboard_completion_certificate_id='certificate-current'
            WHERE id='e'""",
        (storyboard_id,),
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,characters,action_desc,dialogues,transition,adopted_version_id,storyboard_artifact_id) "
        "VALUES('s','e',1,5,'中景','固定','夜，庭院','[]','角色抬头','[]','硬切','v',?)", (storyboard_id,),
    )
    technical = json.dumps({"passed": True, "issues": [], "evidence": {"duration_s": 5}}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,qa_json,technical_validation_json,adoption_reason,artifact_id,created_at) "
        "VALUES('v','s',1,'prompt','key','succeeded',?,'{\"overall\":0.9}',?,'best candidate',?,0)",
        (str(video), technical, video_artifact_id),
    )
    conn.commit()
    video_manifest = downstream_authority.current_adopted_video_delivery_manifest(
        "e",
        conn=conn,
    )
    (final_video.parent / "episode.edit-report.json").write_text(
        json.dumps({
            "ok": True,
            "video_delivery_manifest_hash": video_manifest["manifest_hash"],
            "video_delivery_manifest": video_manifest,
            "final_video_sha256": hashlib.sha256(final_video.read_bytes()).hexdigest(),
        }),
        encoding="utf-8",
    )

    readiness = delivery.delivery_readiness("e")
    assert readiness["ready"] is True and readiness["evidence_coverage"] == 1
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    delivery.delivery_readiness("e")
    conn.set_trace_callback(None)
    writes = (
        "insert ", "update ", "delete ", "create ", "alter ", "drop ",
        "begin ", "commit", "rollback",
    )
    assert not any(
        statement.strip().lower().startswith(writes)
        for statement in statements
    )
    assert readiness["source_chapters"] == [{
        "chapter_id": 1,
        "project_id": "p",
        "chapter_idx": 1,
        "title": "第一章",
        "content_sha256": hashlib.sha256("第一章正文".encode()).hexdigest(),
    }]
    final_stale = final_video.with_suffix(".stale")
    final_stale.write_text("outdated\n", encoding="utf-8")
    outdated = delivery.delivery_readiness("e")
    assert outdated["ready"] is False
    assert any(
        item["key"] == "final_video" and item["evidence"]["outdated"] is True
        for item in outdated["blockers"]
    )
    final_stale.unlink()
    conn.execute(
        "UPDATE shot_versions SET qa_json=? WHERE id='v'",
        (json.dumps({"overall": 0.9, "contract_facts": ["no_character_duplicate_failed"]}),),
    )
    conn.commit()
    score_only = delivery.delivery_readiness("e")
    assert score_only["ready"] is True
    assert not any(item["key"] == "fatal_video_quality" for item in score_only["blockers"])
    assert not any(item["check"] == "fatal_video_quality" for item in score_only["warnings"])

    def assert_source_blocked(package_id: str) -> dict:
        blocked = delivery.delivery_readiness("e")
        source_blocker = next(
            item for item in blocked["blockers"] if item["key"] == "source_chapters"
        )
        with pytest.raises(ValueError, match="授权章节列表非空"):
            delivery.build_delivery_package("e", package_id=package_id)
        return source_blocker["evidence"]

    conn.execute("UPDATE episodes SET source_chapters='[]' WHERE id='e'")
    conn.commit()
    assert assert_source_blocked("delivery_empty_sources")["authorized_indices"] == []

    conn.execute("UPDATE episodes SET source_chapters='[99]' WHERE id='e'")
    conn.commit()
    assert assert_source_blocked("delivery_missing_source")["missing_indices"] == [99]

    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('foreign','Foreign',0)")
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) "
        "VALUES('foreign',2,'外部章节','不属于当前项目')"
    )
    conn.execute("UPDATE episodes SET source_chapters='[2]' WHERE id='e'")
    conn.commit()
    cross_project = assert_source_blocked("delivery_cross_project_source")
    assert cross_project["missing_indices"] == [2]
    assert cross_project["foreign_project_matches"] == [{
        "chapter_idx": 2,
        "project_id": "foreign",
    }]

    conn.execute("UPDATE episodes SET source_chapters='[1]' WHERE id='e'")
    conn.execute("DELETE FROM chapters WHERE project_id='p' AND idx=1")
    conn.commit()
    assert assert_source_blocked("delivery_deleted_source")["missing_indices"] == [1]
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) "
        "VALUES('p',1,'第一章','第一章正文')"
    )
    conn.commit()

    conn.execute(
        "UPDATE shot_versions SET qa_json=? WHERE id='v'",
        (json.dumps({"overall": 0.9}),),
    )
    conn.commit()
    draft = delivery.build_delivery_package(
        "e", package_id="delivery_crash_window", operation_started_at=42,
    )
    source_snapshot = json.loads(
        Path(draft["package_path"], "snapshots", "source-chapters.json").read_text(
            encoding="utf-8"
        )
    )
    assert source_snapshot["chapters"] == draft["manifest"]["source_chapters"]
    assert source_snapshot["chapters"][0]["content_sha256"] == hashlib.sha256(
        "第一章正文".encode()
    ).hexdigest()
    assert any(
        item["path"] == "snapshots/source-chapters.json"
        and item["role"] == "snapshot"
        and len(item["sha256"]) == 64
        for item in draft["manifest"]["files"]
    )
    draft_manifest = Path(draft["package_path"], "manifest.json").read_bytes()
    draft_hash = repository.get_artifact(draft["artifact_id"])["content_hash"]
    draft_archive = Path(draft["archive_path"])
    draft_archive_bytes = draft_archive.read_bytes()
    draft_archive.unlink()
    with pytest.raises(ValueError, match="缺少原 operation owner"):
        delivery.build_delivery_package(
            "e", package_id="delivery_crash_window", operation_started_at=42,
        )
    draft_archive.write_bytes(draft_archive_bytes)
    tracked_shot = next(
        item for item in draft["manifest"]["files"] if item["role"] == "shot_video"
    )
    tracked_path = Path(draft["package_path"], tracked_shot["path"])
    tracked_path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="损坏"):
        delivery.build_delivery_package(
            "e", package_id="delivery_crash_window", operation_started_at=42,
        )
    # A matching re-packed ZIP is not sufficient: draft downloads remain
    # bound to every file hash in the persisted manifest.
    delivery.atomic_zip_directory(Path(draft["package_path"]), Path(draft["archive_path"]))
    with pytest.raises(Exception, match="manifest"):
        orchestration_api.download_delivery_archive(draft["package_id"])
    tracked_path.write_bytes(video.read_bytes())
    delivery.atomic_zip_directory(Path(draft["package_path"]), Path(draft["archive_path"]))
    # Simulate a hard exit after the immutable artifact commit but before the
    # delivery_packages pointer commit.  The stable operation must reconstruct
    # the pointer without changing evidence or duplicating its file evaluation.
    conn.execute("DELETE FROM delivery_packages WHERE id=?", (draft["package_id"],))
    conn.commit()
    recovered_draft = delivery.build_delivery_package(
        "e", package_id="delivery_crash_window", operation_started_at=42,
    )
    assert recovered_draft["artifact_id"] == draft["artifact_id"]
    assert repository.get_artifact(draft["artifact_id"])["content_hash"] == draft_hash
    assert len(repository.get_evaluations(draft["artifact_id"])) == 1

    # Approval is a decision over the exact draft bytes and authority manifest.
    # It must never rebuild current live inputs into a different T5 package.
    tracked_path.write_bytes(b"tampered-after-review")
    with pytest.raises(ValueError, match="篡改"):
        delivery.approve_delivery(
            "e",
            package_id=draft["package_id"],
            decided_by="reviewer",
            decision="approve",
            reason="复验通过",
        )
    tracked_path.write_bytes(video.read_bytes())

    conn.execute("UPDATE shot_versions SET playback_rate=1.25 WHERE id='v'")
    conn.commit()
    with pytest.raises(ValueError, match="已采纳视频已漂移"):
        delivery.approve_delivery(
            "e",
            package_id=draft["package_id"],
            decided_by="reviewer",
            decision="approve",
            reason="复验通过",
        )
    assert not any(
        item["evaluator_type"] == "human"
        for item in repository.get_evaluations(draft["artifact_id"])
    )
    conn.execute("UPDATE shot_versions SET playback_rate=1 WHERE id='v'")
    conn.commit()

    package = delivery.approve_delivery(
        "e", decided_by="reviewer", decision="approve", reason="复验通过",
    )
    assert package["package_id"] == draft["package_id"]
    assert package["artifact_id"] == draft["artifact_id"]
    assert Path(draft["package_path"], "manifest.json").read_bytes() == draft_manifest
    assert repository.get_artifact(draft["artifact_id"])["status"] == "approved"
    assert package["approved_snapshot_preserved"] is True
    assert package["trust_level"] == "T5" and Path(package["archive_path"]).is_file()
    archive_bytes = Path(package["archive_path"]).read_bytes()
    Path(package["archive_path"]).write_bytes(b"tampered-approved-archive")
    with pytest.raises(Exception, match="压缩包与已审核目录不一致"):
        orchestration_api.download_delivery_archive(package["package_id"])
    Path(package["archive_path"]).write_bytes(archive_bytes)
    report_path = Path(package["package_path"], "quality-report.html")
    report_bytes = report_path.read_bytes()
    report_path.write_bytes(b"tampered-report")
    with pytest.raises(Exception, match="与已审核 manifest 不一致"):
        orchestration_api.download_delivery_report(package["package_id"])
    report_path.write_bytes(report_bytes)
    report_text = Path(package["package_path"], "quality-report.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in report_text
    shot_entry = next(item for item in package["manifest"]["files"] if item["role"] == "shot_video")
    assert shot_entry["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
    artifact_before = repository.get_artifact(package["artifact_id"])

    feedback = delivery.add_customer_feedback(
        "e", message="第二镜节奏可更紧", created_by="customer", rating=3, request_revision=True,
    )
    assert feedback["revision_run_id"]
    assert repository.get_artifact(package["artifact_id"])["content_hash"] == artifact_before["content_hash"]
    assert any(item["evaluator_name"] == "customer_feedback" for item in repository.get_evaluations(package["artifact_id"]))

    # Deleting the adopted source is an authority revocation, not merely a
    # missing-media marker.  The historical package remains auditable but is
    # no longer current or downloadable.
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    assert artifacts.delete_video_version("v") == "s"
    revoked = conn.execute(
        "SELECT delivery_artifact_id,delivery_status FROM episodes WHERE id='e'"
    ).fetchone()
    assert revoked["delivery_artifact_id"] is None
    assert revoked["delivery_status"] == "not_ready"
    assert conn.execute(
        "SELECT status FROM delivery_packages WHERE id=?", (package["package_id"],)
    ).fetchone()["status"] == "superseded"
    assert repository.get_artifact(package["artifact_id"])["status"] == "superseded"
    with pytest.raises(Exception, match="已不是当前可下载权威"):
        orchestration_api.download_delivery_archive(package["package_id"])
    with pytest.raises(Exception, match="已不是当前可下载权威"):
        orchestration_api.download_delivery_report(package["package_id"])


def test_delivery_package_id_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="非法的 package_id"):
        delivery.validate_package_id("../../etc/passwd")
    with pytest.raises(ValueError, match="非法的 package_id"):
        delivery.validate_package_id("delivery_../escape")


def test_auto_adopted_validated_video_is_current_delivery_authority(
    tmp_path, monkeypatch,
) -> None:
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence.shutil, "which", lambda _name: None)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "invalidate_episode_final", lambda _episode_id: False)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,created_at) "
        "VALUES('e','p',1,'confirmed',0)"
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s','e',1,5)"
    )
    video = tmp_path / "auto.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"auto-video")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,qa_json,created_at
           ) VALUES('v-auto','s',1,'prompt','idem','succeeded',?,'{}',0)""",
        (str(video),),
    )
    conn.commit()

    artifact = media_evidence.record_video_candidate("v-auto")
    assert artifact["status"] == "validated"
    assert media_evidence.select_best_video_candidate("s")["version_id"] == "v-auto"
    manifest = downstream_authority.current_adopted_video_delivery_manifest(
        "e", conn=conn,
    )

    assert manifest["items"][0]["artifact_id"] == artifact["id"]
    assert manifest["items"][0]["adopted_version_id"] == "v-auto"


@pytest.mark.parametrize(
    ("decision", "accepted_risk"),
    [
        (None, None),
        ("approve", None),
        ("approve_with_risk", "接受已知质量风险"),
    ],
)
def test_delivery_hard_blockers_cannot_build_or_approve(
    monkeypatch,
    decision: str | None,
    accepted_risk: str | None,
) -> None:
    monkeypatch.setattr(
        delivery,
        "delivery_readiness",
        lambda _episode_id: {
            "blockers": [
                {
                    "key": "final_video",
                    "message": "整集成片缺失或不可解码",
                },
            ],
        },
    )
    monkeypatch.setattr(
        delivery,
        "get_conn",
        lambda: (_ for _ in ()).throw(
            AssertionError("硬门禁失败后不得产生数据库或文件副作用")
        ),
    )

    with pytest.raises(ValueError, match="交付硬门禁未通过.*整集成片缺失"):
        delivery.build_delivery_package(
            "e",
            decision=decision,
            accepted_risk=accepted_risk,
        )


def test_delivery_reject_replays_exact_receipt(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery, "get_conn", lambda: conn)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,created_at) VALUES('e','p',1,0)"
    )
    artifact = repository.create_artifact(EvidenceArtifact(
        type="delivery_package",
        scope_type="episode",
        scope_id="e",
        content={"package": "draft"},
        status="candidate",
        trust_level="T3",
    ))
    conn.execute(
        "UPDATE artifacts SET status='validated' WHERE id=?",
        (artifact["id"],),
    )
    conn.execute(
        """INSERT INTO delivery_packages(
               id,episode_id,artifact_id,status,package_path,manifest_json,
               quality_report_json,known_issues,created_at
           ) VALUES('delivery_draft','e',?,'waiting_human','/tmp/draft','{}','{}','[]',0)""",
        (artifact["id"],),
    )
    conn.execute(
        "UPDATE episodes SET delivery_artifact_id=?,delivery_status='waiting_human' WHERE id='e'",
        (artifact["id"],),
    )
    conn.commit()

    first = delivery.approve_delivery(
        "e",
        package_id="delivery_draft",
        decided_by="reviewer",
        decision="reject",
        reason="证据不足",
    )
    assert first["decision"] == "reject"
    replay = delivery.approve_delivery(
        "e",
        package_id="delivery_draft",
        decided_by="reviewer",
        decision="reject",
        reason="证据不足",
    )
    assert replay == first
    assert conn.execute(
        "SELECT COUNT(*) FROM gate_decisions WHERE artifact_id=?", (artifact["id"],)
    ).fetchone()[0] == 1


def test_delivery_route_forwards_idempotency_metadata(monkeypatch) -> None:
    captured: dict = {}

    async def fake_ui_route(name: str, args: dict):
        captured.update({"name": name, "args": args})
        return {"status": "accepted"}

    monkeypatch.setattr("app.capabilities.dispatch.ui_route", fake_ui_route)
    result = asyncio.run(orchestration_api.create_delivery_package(
        "e",
        {"idempotency_key": "delivery-once", "request_id": "request-1"},
    ))

    assert result == {"status": "accepted"}
    assert captured == {
        "name": "delivery.create_package",
        "args": {
            "episode_id": "e",
            "idempotency_key": "delivery-once",
            "request_id": "request-1",
            "package_id": None,
            "reason": None,
        },
    }


def test_delivery_route_forwards_client_supplied_package_id(monkeypatch) -> None:
    """客户端重放「已校验过的交付包 id」续跑时，package_id 必须真的进 dispatch
    参数——此前 REST 包装层只读它做本地校验分支，从不转发给命令总线，handler
    重建 body 时又把它丢一次，两次丢包合起来让这个字段在命令总线路径上完全
    不可达（同幂等键的重试因此总落 sha256 确定性重算分支，而不是复用已验证 id）。
    """
    captured: dict = {}

    async def fake_ui_route(name: str, args: dict):
        captured.update({"name": name, "args": args})
        return {"status": "accepted"}

    monkeypatch.setattr("app.capabilities.dispatch.ui_route", fake_ui_route)
    result = asyncio.run(orchestration_api.create_delivery_package(
        "e",
        {
            "idempotency_key": "delivery-once",
            "request_id": "request-1",
            "package_id": "delivery_alreadyvalidated",
        },
    ))

    assert result == {"status": "accepted"}
    assert captured["args"]["package_id"] == "delivery_alreadyvalidated"


def test_delivery_route_forwards_reason(monkeypatch) -> None:
    """reason 是交付包 quality-report 里的说明性文本，继承自
    StandardCommandInput；REST 包装层此前手写 dict 重建 ui_route 参数时漏了
    这个字段（与 package_id 同一形状），任务侧 yyft_pipeline10.py 发的
    reason 因此在命令总线路径上完全不可达。
    """
    captured: dict = {}

    async def fake_ui_route(name: str, args: dict):
        captured.update({"name": name, "args": args})
        return {"status": "accepted"}

    monkeypatch.setattr("app.capabilities.dispatch.ui_route", fake_ui_route)
    result = asyncio.run(orchestration_api.create_delivery_package(
        "e",
        {
            "idempotency_key": "delivery-once",
            "reason": "readiness 门禁已全部通过，生成交付候选包",
        },
    ))

    assert result == {"status": "accepted"}
    assert captured["args"]["reason"] == "readiness 门禁已全部通过，生成交付候选包"


def test_delivery_package_decided_by_ignores_client_payload(monkeypatch) -> None:
    """decided_by 是审计相邻字段（写入 WorkflowRecorder.requested_by 与
    quality-report 的 human_decision），不接受客户端自报——即便客户端在
    body 里塞了 decided_by，实际记录的必须是已鉴权身份（这里用
    current_actor_name() 的 fallback 值验证，因为测试没有真实登录会话）。
    """
    from app.auth.principal import current_actor_name
    from app.db import get_conn

    # 不额外造隔离连接：recorder.step() 经 run_write_transaction 走的是
    # app.db 的真实按线程连接，不会看见另一份手动 monkeypatch 的内存连接
    # （试过，FOREIGN KEY 撞车）。conftest 的 autouse fixture 已经给每个
    # 测试准备好一份隔离的真实 sqlite 文件库，直接在这份库里插入夹具即可。
    conn = get_conn()
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('p2','P2','planned',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,created_at) "
        "VALUES('e2','p2',1,'E2','[]','done',0)"
    )
    conn.commit()

    async def fake_ui_route(name: str, args: dict):
        return None  # 模拟 in_handler()：走真实的 legacy 落地逻辑

    monkeypatch.setattr("app.capabilities.dispatch.ui_route", fake_ui_route)

    captured_kwargs: dict = {}

    def fake_build_delivery_package(episode_id, **kwargs):
        captured_kwargs.update(kwargs)
        return {"package_id": kwargs["package_id"], "manifest": {}, "artifact_id": "art"}

    monkeypatch.setattr(delivery, "build_delivery_package", fake_build_delivery_package)

    # 测试全局 autouse fixture（conftest._reset_capability_runtime）已经注入了一个
    # 系统管理员 Principal；直接读它此刻会解析成的值，不假设具体用户名，只断言
    # 它不等于客户端在 body 里塞的伪造值。
    expected_actor = current_actor_name()
    assert expected_actor != "spoofed-reviewer"
    result = asyncio.run(orchestration_api.create_delivery_package(
        "e2",
        {
            "idempotency_key": "spoof-check",
            "decided_by": "spoofed-reviewer",
            "reason": "真实原因",
        },
    ))

    assert result["package_id"]
    assert captured_kwargs["decided_by"] == expected_actor
    assert captured_kwargs["reason"] == "真实原因"


def test_delivery_restart_fences_lease_only_for_recovery_owner(
    tmp_path, monkeypatch,
) -> None:
    database = tmp_path / "delivery-restart.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db(reconcile_interrupted=False)
    conn = db.get_conn()
    owner, recovered = delivery.claim_delivery_package_operation(
        package_id="delivery_restart",
        episode_id="e",
        request_fingerprint="fp",
        conn=conn,
    )
    assert owner and recovered is None
    workspace = tmp_path / f".{owner}.tmp"
    conn.execute(
        """UPDATE delivery_operation_receipts
              SET workspace_path=?,promotion_phase='directory_promoted'
            WHERE package_id='delivery_restart'""",
        (str(workspace),),
    )
    conn.commit()
    lease_before = conn.execute(
        "SELECT lease_expires_at FROM delivery_operation_receipts"
    ).fetchone()[0]

    # A secondary/non-owner initialization must not steal a live operation.
    db.init_db(reconcile_interrupted=False)
    assert conn.execute(
        "SELECT lease_expires_at FROM delivery_operation_receipts"
    ).fetchone()[0] == lease_before
    with pytest.raises(ValueError, match="正在执行"):
        delivery.claim_delivery_package_operation(
            package_id="delivery_restart",
            episode_id="e",
            request_fingerprint="fp",
            allow_interrupted_takeover=True,
            conn=conn,
        )

    # The runtime-recovery owner proves the old process is gone, fences its
    # lease, and the new owner preserves abandoned path/phase evidence while
    # receiving a fresh workspace binding.
    db.init_db(reconcile_interrupted=True)
    next_owner, recovered = delivery.claim_delivery_package_operation(
        package_id="delivery_restart",
        episode_id="e",
        request_fingerprint="fp",
        allow_interrupted_takeover=True,
        conn=conn,
    )
    assert next_owner and next_owner != owner and recovered is None
    row = conn.execute(
        "SELECT * FROM delivery_operation_receipts WHERE package_id='delivery_restart'"
    ).fetchone()
    assert row["workspace_path"] == ""
    assert row["promotion_phase"] == "claimed"
    assert row["abandoned_workspace_path"] == str(workspace)
    assert row["abandoned_promotion_phase"] == "directory_promoted"
    assert row["interrupted_at"] is None
    assert row["recovery_fenced_owner"] == next_owner

    # The startup proof is one-shot. A later natural lease expiry in the same
    # process must not let a third owner treat owner B as a dead process.
    conn.execute(
        "UPDATE delivery_operation_receipts SET lease_expires_at=0 WHERE package_id='delivery_restart'"
    )
    conn.commit()
    third_owner, _ = delivery.claim_delivery_package_operation(
        package_id="delivery_restart",
        episode_id="e",
        request_fingerprint="fp",
        conn=conn,
    )
    third = conn.execute(
        "SELECT recovery_fenced_owner,interrupted_at FROM delivery_operation_receipts"
    ).fetchone()
    assert third_owner and third_owner != next_owner
    assert third["recovery_fenced_owner"] == ""
    assert third["interrupted_at"] is None
    conn.close()
    db._local.conn = None


def test_delivery_recovery_isolates_spawn_failure_and_continues(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(orchestration_api, "get_conn", lambda: conn)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,created_at) VALUES('e','p',1,0)"
    )
    for index in (1, 2):
        payload = {
            "package_id": f"delivery_recover_{index}",
            "operation_started_at": float(index),
        }
        conn.execute(
            """INSERT INTO workflow_runs(
                   id,workflow_type,scope_type,scope_id,status,input_fingerprint,
                   policy_snapshot_json,config_snapshot_json,failure_code,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                f"run_delivery_{index}",
                "delivery_package",
                "episode",
                "e",
                "PAUSED_EXTERNAL",
                f"fp-{index}",
                "{}",
                json.dumps({"recovery_payload": payload}),
                "SERVICE_RESTART",
                float(index),
            ),
        )
    conn.commit()
    calls = 0

    def selective_spawn(_kind, _key, coro, *, project_id=None):
        nonlocal calls
        calls += 1
        coro.close()
        if calls == 1:
            raise RuntimeError("event loop unavailable")
        return None

    monkeypatch.setattr(task_registry, "spawn", selective_spawn)

    assert orchestration_api.recover_delivery_tasks() == 1
    assert calls == 2
    first = conn.execute(
        "SELECT failure_message FROM workflow_runs WHERE id='run_delivery_1'"
    ).fetchone()
    assert "可重新发起" in first["failure_message"]
