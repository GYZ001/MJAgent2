import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app import db, delivery, task_registry
from app.evidence import repository
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
        content={"kind": kind}, file_path=file_path, status="validated", trust_level="T2",
    ))
    artifact = repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="deterministic", evaluator_name="test", evaluator_version="1",
        status="passed", hard_gate_passed=True, score=100,
    )])
    return artifact["id"]


def test_delivery_package_reaches_t5_and_feedback_preserves_snapshot(tmp_path, monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery.config, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(delivery, "validate_video_file", lambda path, expected_duration_s=5: {
        "passed": True,
        "issues": [],
        "evidence": {"path": path, "duration_s": expected_duration_s},
    })

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

    readiness = delivery.delivery_readiness("e")
    assert readiness["ready"] is True and readiness["evidence_coverage"] == 1
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
    Path(draft["archive_path"]).unlink()
    archive_recovered = delivery.build_delivery_package(
        "e", package_id="delivery_crash_window", operation_started_at=42,
    )
    assert archive_recovered["archive_recovered"] is True
    assert Path(archive_recovered["archive_path"]).is_file()
    tracked_shot = next(
        item for item in draft["manifest"]["files"] if item["role"] == "shot_video"
    )
    tracked_path = Path(draft["package_path"], tracked_shot["path"])
    tracked_path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="损坏"):
        delivery.build_delivery_package(
            "e", package_id="delivery_crash_window", operation_started_at=42,
        )
    tracked_path.write_bytes(video.read_bytes())
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
    package = delivery.approve_delivery(
        "e", decided_by="reviewer", decision="approve", reason="复验通过",
    )
    assert package["package_id"] != draft["package_id"]
    assert package["artifact_id"] != draft["artifact_id"]
    assert Path(draft["package_path"], "manifest.json").read_bytes() == draft_manifest
    assert repository.get_artifact(draft["artifact_id"])["status"] == "superseded"
    assert package["trust_level"] == "T5" and Path(package["archive_path"]).is_file()
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


def test_delivery_package_id_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="非法的 package_id"):
        delivery.validate_package_id("../../etc/passwd")
    with pytest.raises(ValueError, match="非法的 package_id"):
        delivery.validate_package_id("delivery_../escape")


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


def test_delivery_reject_can_only_be_claimed_once(monkeypatch) -> None:
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
        """INSERT INTO delivery_packages(
               id,episode_id,artifact_id,status,package_path,manifest_json,
               quality_report_json,known_issues,created_at
           ) VALUES('delivery_draft','e',?,'waiting_human','/tmp/draft','{}','{}','[]',0)""",
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
    with pytest.raises(ValueError, match="不可审核"):
        delivery.approve_delivery(
            "e",
            package_id="delivery_draft",
            decided_by="reviewer",
            decision="reject",
            reason="重复提交",
        )
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
        },
    }


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
