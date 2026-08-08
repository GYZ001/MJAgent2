from __future__ import annotations

import json
import sqlite3

import pytest

from app import compiler, config, db, worker
from app.schemas import Bible, Character, World


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


def _seed(conn: sqlite3.Connection, *, two_shots: bool = False) -> None:
    bible = Bible(
        characters=[
            Character(name="管事", role="配角", appearance_canonical="黑袍中年人"),
            Character(name="孟浩", role="主角", appearance_canonical="青衫青年"),
        ],
        world=World(visual_style_canonical="东方玄幻动画"),
    )
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,created_at) VALUES('p1','P','created',?,1)",
        (bible.model_dump_json(),),
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('e1','p1',1,'confirmed',1)"""
    )
    rows = [
        (
            "s1", 1, 10, "管事面色严肃，抬手指向铜镜。",
            "管事抬手警告孟浩不要放下铜镜，四周宝光仍在闪烁。",
            0, "same_scene_cut",
        ),
    ]
    if two_shots:
        rows.append((
            "s2", 2, 10, "孟浩握住铜镜，转头看向宝阁出口。",
            "孟浩握住铜镜转头望向宝阁出口，管事站在原地。",
            1, "action_continuation",
        ))
    for shot_id, shot_no, duration, action, excerpt, continuous, mode in rows:
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,shot_size,camera_move,
                   scene_setting,characters,action_desc,first_frame_desc,last_frame_desc,
                   source_excerpt,narration,dialogues,transition,continuity_from_prev,
                   continuity_mode
               ) VALUES(?, 'e1', ?, ?, '中景', '固定', '宝阁',
                        ?, ?, '人物站在宝阁中央。', '人物仍在宝阁中央。',
                        ?, NULL, '[]', '硬切', ?, ?)""",
            (
                shot_id, shot_no, duration, json.dumps(["管事", "孟浩"]),
                action, excerpt, continuous, mode,
            ),
        )
    conn.commit()


def _patch_enqueue_runtime(monkeypatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(
        worker.media_scheduler,
        "reserve_budget",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(worker, "_enqueue_for_current_status", lambda _job_id: None)


def test_embedded_source_dialogue_is_repaired_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    line = "师弟不要在入宗第一天犯下门规，此宝阁内拿起便不可放下。"
    conn.execute(
        """UPDATE shots
           SET action_desc=?, source_excerpt=?, duration_s=5
           WHERE id='s1'""",
        (
            f"管事面色骤沉，以门规警告孟浩；待修复台词信息「{line}」",
            line + "管事目光锐利地盯着孟浩。",
        ),
    )
    conn.commit()
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(
        compiler,
        "compile_prompt",
        lambda shot, *_args, **_kwargs: f"画面动作：{shot.action_desc}",
    )

    result = worker.enqueue_shot("s1")

    assert result["task_accepted"] is True
    assert result["auto_repaired"] is True
    shot = conn.execute(
        "SELECT action_desc,dialogues FROM shots WHERE id='s1'"
    ).fetchone()
    assert "待修复台词信息" not in shot["action_desc"]
    assert json.loads(shot["dialogues"]) == [{
        "speaker": "管事",
        "line": line,
        "emotion": "平静",
        "delivery": "spoken_dialogue",
    }]
    job = conn.execute(
        "SELECT status,version_id,pipeline_stage FROM jobs WHERE id=?",
        (result["job_id"],),
    ).fetchone()
    assert job["status"] == "queued"
    assert job["version_id"] == result["version_id"]
    assert job["pipeline_stage"] == "job_queued"
    assert conn.execute(
        "SELECT duration_s FROM shots WHERE id='s1'"
    ).fetchone()["duration_s"] > 5


def test_legacy_descriptive_speaker_is_not_guessed_into_route_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shot 17 同时带原文对白、空角色表和描述性临时说话人，必须一次收口。"""
    conn = _conn()
    _seed(conn)
    line = "许师姐已经到了凝气第七层，被掌教赐了风幡，没到筑基便可飞行，让人羡慕。"
    conn.execute(
        """UPDATE shots
           SET duration_s=5,shot_size='近景',characters='[]',dialogues='[]',
               action_desc=?,source_excerpt=?,shot_contract_json=?
           WHERE id='s1'""",
        (
            "绿袍修士乙站在青石空地上，双眼凹陷瞳孔泛绿，面朝孟浩四人方向，"
            f"开口感慨许师姐修为之高；待修复台词信息「{line}」。",
            line,
            json.dumps({
                "primary_action": "绿袍修士乙开口介绍许师姐的修为与风幡之事",
                "state_in": "绿袍修士乙面朝孟浩等人站在青石空地上",
                "state_out": "绿袍修士乙说完，众人仍留在青石空地上",
                "characters_visible": [],
                "audio_cast": [],
                "audio_timeline": [],
                "continuity_mode": "reverse_angle",
            }, ensure_ascii=False),
        ),
    )
    conn.commit()
    _patch_enqueue_runtime(monkeypatch, conn)

    with pytest.raises(compiler.CompileError, match="绿袍修士乙"):
        worker.enqueue_shot("s1")

    shot = conn.execute(
        "SELECT duration_s,characters,dialogues,action_desc FROM shots WHERE id='s1'"
    ).fetchone()
    assert shot["duration_s"] == 9
    assert json.loads(shot["characters"]) == ["绿袍修士乙"]
    assert json.loads(shot["dialogues"])[0]["speaker"] == "绿袍修士乙"
    assert "待修复台词信息" not in shot["action_desc"]


def test_structured_source_dialogue_expands_to_minimum_valid_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    line = "师弟不要在入宗第一天犯下门规，此宝阁内拿起便不可放下。"
    conn.execute(
        """UPDATE shots
           SET duration_s=5, action_desc='管事面色严肃，以门规警告孟浩不可放下铜镜',
               source_excerpt=?, dialogues=?
           WHERE id='s1'""",
        (
            line + "管事随后收起了笑容。",
            json.dumps([{
                "speaker": "管事",
                "line": line,
                "emotion": "平静",
                "delivery": "spoken_dialogue",
            }]),
        ),
    )
    conn.commit()
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(
        compiler,
        "compile_prompt",
        lambda *_args, **_kwargs: "安全的视频提示词",
    )

    result = worker.enqueue_shot("s1")

    assert result["task_accepted"] is True
    assert result["auto_repaired"] is True
    assert result["preflight_repair"]["repair"] == "source_dialogue_duration"
    assert result["preflight_repair"]["from_duration_s"] == 5
    assert result["preflight_repair"]["to_duration_s"] > 5
    assert conn.execute(
        "SELECT duration_s FROM shots WHERE id='s1'"
    ).fetchone()["duration_s"] == result["preflight_repair"]["to_duration_s"]


def test_location_prefixed_speaker_is_not_normalized_by_name_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    line = "此物拿起便不可放下。"
    conn.execute(
        """UPDATE shots
           SET shot_size='近景', characters=?, action_desc=?,
               source_excerpt=?, dialogues=?, shot_contract_json=?
           WHERE id='s1'""",
        (
            json.dumps(["宝阁管事"]),
            "宝阁管事面色严肃，警告孟浩不可放下铜镜。",
            line + "宝阁管事随后收起了笑容。",
            json.dumps([{
                "speaker": "宝阁管事",
                "line": line,
                "emotion": "平静",
                "delivery": "spoken_dialogue",
            }]),
            json.dumps({
                "characters_visible": ["宝阁管事"],
                "audio_cast": ["宝阁管事"],
                "audio_timeline": [],
                "continuity_mode": "same_scene_cut",
            }),
        ),
    )
    conn.commit()
    _patch_enqueue_runtime(monkeypatch, conn)

    def compile_role(shot, *_args, **_kwargs):
        if "宝阁管事" in shot.characters:
            raise compiler.CompileError(
                "角色合同残留了既不在角色圣经、也不是功能性路人的角色：['宝阁管事']"
            )
        return "安全的视频提示词"

    monkeypatch.setattr(compiler, "compile_prompt", compile_role)

    with pytest.raises(compiler.CompileError, match="宝阁管事"):
        worker.enqueue_shot("s1")

    shot = conn.execute(
        "SELECT characters,dialogues,shot_contract_json FROM shots WHERE id='s1'"
    ).fetchone()
    assert json.loads(shot["characters"]) == ["宝阁管事"]
    assert json.loads(shot["dialogues"])[0]["speaker"] == "宝阁管事"
    assert json.loads(shot["shot_contract_json"])["characters_visible"] == [
        "宝阁管事"
    ]


def test_unstructured_source_excerpt_is_scrubbed_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    excerpt = "管事抬手指向铜镜并持续警告孟浩绝对不可把它放回原处。"
    conn.execute(
        """UPDATE shots
           SET action_desc=?, source_excerpt=?, characters='[]', duration_s=5
           WHERE id='s1'""",
        (excerpt, excerpt),
    )
    conn.commit()
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(
        compiler,
        "compile_prompt",
        lambda shot, *_args, **_kwargs: f"画面动作：{shot.action_desc}",
    )

    result = worker.enqueue_shot("s1")

    job = conn.execute(
        """SELECT status,version_id,pipeline_stage,reason_code FROM jobs
           WHERE shot_id='s1'"""
    ).fetchone()
    assert job["status"] == "queued"
    assert job["version_id"] == result["version_id"]
    assert job["pipeline_stage"] == "job_queued"
    assert job["reason_code"] is None
    assert result["auto_repaired"] is True
    assert result["preflight_repair"]["repair"] == "source_excerpt_prompt_scrub"
    version = conn.execute(
        "SELECT prompt_text,image_inputs FROM shot_versions WHERE id=?",
        (result["version_id"],),
    ).fetchone()
    assert excerpt[:24] not in version["prompt_text"]
    assert "不复述小说原文" in version["prompt_text"]
    assert json.loads(version["image_inputs"])["preflight_auto_repair"]["repair"] == (
        "source_excerpt_prompt_scrub"
    )


def test_source_excerpt_failure_gets_bounded_retry_if_scrubber_cannot_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    excerpt = "管事抬手指向铜镜并持续警告孟浩绝对不可把它放回原处。"
    conn.execute(
        "UPDATE shots SET action_desc=?,source_excerpt=? WHERE id='s1'",
        (excerpt, excerpt),
    )
    conn.commit()
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(
        compiler, "compile_prompt", lambda shot, *_a, **_k: f"画面动作：{shot.action_desc}"
    )
    monkeypatch.setattr(
        worker, "ensure_source_excerpt_in_prompt", lambda prompt, _shot: prompt
    )
    monkeypatch.setattr(config, "VIDEO_PREFLIGHT_RETRY_BASE_DELAY", 1.0)

    result = worker.enqueue_shot("s1")

    job = conn.execute(
        "SELECT status,retry_count,pipeline_stage,reason_text FROM jobs WHERE shot_id='s1'"
    ).fetchone()
    assert job["status"] == "waiting_retry"
    assert job["retry_count"] == 1
    assert job["pipeline_stage"] == "preflight_retry"
    assert "自动重试" in job["reason_text"]
    assert result["task_accepted"] is True
    assert result["retry_scheduled"] is True
    assert result["job_id"]


def test_legacy_blocked_source_excerpt_job_is_reactivated_after_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(compiler, "compile_prompt", lambda *_a, **_k: "安全的视频提示词")
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,episode_id,project_id,status,error,retry_count,
               reason_code,reason_text,pipeline_stage,stage_status,created_at,updated_at
           ) VALUES(
               'legacy-source-job','video','s1','e1','p1','waiting_human',?,0,
               'VIDEO_PREFLIGHT_BLOCKED',?,'preflight_blocked','blocked',1,1
           )""",
        (
            "视频输入校验未通过：shot_no=1 最终提示词包含 source_excerpt 原文内容",
            "视频输入校验未通过：shot_no=1 最终提示词包含 source_excerpt 原文内容",
        ),
    )
    conn.commit()

    report = worker.reconcile_stalled_video_jobs()

    assert report["legacy_preflight_reactivated"] == 1
    assert report["preflight_retried"] == 1
    job = conn.execute(
        "SELECT status,version_id,pipeline_stage FROM jobs WHERE id='legacy-source-job'"
    ).fetchone()
    assert job["status"] == "queued"
    assert job["version_id"]
    assert job["pipeline_stage"] == "job_queued"


def test_transient_preflight_failure_is_retried_by_sweeper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    _patch_enqueue_runtime(monkeypatch, conn)
    calls = {"count": 0}

    def compile_once(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return "安全的视频提示词"

    monkeypatch.setattr(compiler, "compile_prompt", compile_once)
    monkeypatch.setattr(config, "VIDEO_PREFLIGHT_RETRY_BASE_DELAY", 1.0)

    accepted = worker.enqueue_shot("s1")
    assert accepted["task_accepted"] is True
    assert accepted["retry_scheduled"] is True
    waiting = conn.execute(
        "SELECT id,status,retry_count FROM jobs WHERE shot_id='s1'"
    ).fetchone()
    assert waiting["status"] == "waiting_retry"
    assert waiting["retry_count"] == 1
    conn.execute(
        "UPDATE jobs SET next_retry_at=0 WHERE id=?", (waiting["id"],)
    )
    conn.commit()

    report = worker.reconcile_stalled_video_jobs()

    assert report["preflight_retried"] == 1
    recovered = conn.execute(
        "SELECT status,version_id,pipeline_stage FROM jobs WHERE id=?",
        (waiting["id"],),
    ).fetchone()
    assert recovered["status"] == "queued"
    assert recovered["version_id"]
    assert recovered["pipeline_stage"] == "job_queued"


def test_orphaned_continuity_wait_requires_repair_without_mode_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn, two_shots=True)
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(config, "VIDEO_CONTINUITY_ORPHAN_TIMEOUT", 30.0)
    monkeypatch.setattr(
        compiler,
        "compile_prompt",
        lambda *_args, **_kwargs: "独立首帧安全提示词",
    )
    import app.continuity as continuity
    monkeypatch.setattr(continuity, "preflight_seedance_gates", lambda *_a, **_k: [])

    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('v2','s2',1,'旧连续提示词','idem-v2','queued',?,1)""",
        (json.dumps({
            "mode": "REFERENCE_IMAGE_MODE",
            "after_shot_id": "s1",
            "continuity_mode": "action_continuation",
            "reference_images": [],
        }),),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               after_shot_id,pipeline_stage,stage_started_at,created_at,updated_at
           ) VALUES(
               'j2','video','s2','v2','e1','p1','waiting_human',
               's1','waiting_continuity_anchor',1,1,1
           )"""
    )
    conn.commit()

    report = worker.reconcile_stalled_video_jobs()

    assert report["continuity_degraded"] == 0
    assert report["dependency_repair_required"] == 1
    job = conn.execute(
        "SELECT status,after_shot_id,reason_code,pipeline_stage FROM jobs WHERE id='j2'"
    ).fetchone()
    assert dict(job) == {
        "status": "waiting_human",
        "after_shot_id": "s1",
        "reason_code": "VIDEO_DEPENDENCY_REPAIR_REQUIRED",
        "pipeline_stage": "waiting_human",
    }
    version = conn.execute(
        "SELECT prompt_text,image_inputs,status FROM shot_versions WHERE id='v2'"
    ).fetchone()
    assert version["prompt_text"] == "旧连续提示词"
    assert version["status"] == "waiting_human"
    meta = json.loads(version["image_inputs"])
    assert "continuity_degraded" not in meta
    assert meta["continuity_mode"] == "action_continuation"
    assert meta["after_shot_id"] == "s1"


def test_planned_orphan_keeps_mode_and_requires_dependency_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn, two_shots=True)
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(config, "VIDEO_CONTINUITY_ORPHAN_TIMEOUT", 30.0)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('v2','s2',1,'已冻结的计划提示词','idem-v2','queued',?,1)""",
        (json.dumps({
            "mode": "VIDEO_INPUT_MODE",
            "shot_plan_id": "svp-old",
            "after_shot_id": "s1",
            "review_dependency_snapshot": {"qualification_version": "q1"},
        }),),
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,
               after_shot_id,pipeline_stage,stage_started_at,created_at,updated_at
           ) VALUES(
               'j2','video','s2','v2','e1','p1','waiting_human',
               's1','waiting_dependency',1,1,1
           )"""
    )
    conn.commit()
    enqueued: list[dict] = []
    monkeypatch.setattr(
        worker,
        "enqueue_shot",
        lambda shot_id, **kwargs: enqueued.append({"shot_id": shot_id, **kwargs}),
    )

    report = worker.reconcile_stalled_video_jobs()

    assert report["continuity_degraded"] == 0
    assert report["dependency_repair_required"] == 1
    job = conn.execute(
        "SELECT status,reason_code FROM jobs WHERE id='j2'"
    ).fetchone()
    assert dict(job) == {
        "status": "waiting_human",
        "reason_code": "VIDEO_PLAN_DEPENDENCY_REPAIR_REQUIRED",
    }
    version = conn.execute(
        "SELECT prompt_text,status FROM shot_versions WHERE id='v2'"
    ).fetchone()
    assert dict(version) == {
        "prompt_text": "已冻结的计划提示词",
        "status": "waiting_human",
    }
    assert enqueued == []


def test_legacy_jobless_preflight_issue_is_adopted_by_new_state_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    line = "师弟不要在入宗第一天犯下门规，此宝阁内拿起便不可放下。"
    conn.execute(
        """UPDATE episodes SET status='generating' WHERE id='e1'"""
    )
    conn.execute(
        "UPDATE shots SET action_desc=?, source_excerpt=?, characters='[]' WHERE id='s1'",
        (
            f"管事面色骤沉，收起笑容，以门规警告孟浩不可放下铜镜；待修复台词信息「{line}」",
            line + "管事随后收起了笑容。",
        ),
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,
               content_json,content_hash,created_at
           ) VALUES(
               'art_issue','video_shot_issue','shot','s1',1,'validated','T2',
               ?,'hash',?
           )""",
        (
            json.dumps({
                "episode_id": "e1",
                "shot_id": "s1",
                "shot_no": 1,
                "issues": [{"code": "VIDEO_PREFLIGHT_BLOCKED"}],
            }),
            worker.now(),
        ),
    )
    conn.commit()
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(
        compiler,
        "compile_prompt",
        lambda shot, *_args, **_kwargs: f"画面动作：{shot.action_desc}",
    )

    report = worker.reconcile_stalled_video_jobs()

    assert report["legacy_jobless_recovered"] == 1
    job = conn.execute(
        "SELECT status,version_id FROM jobs WHERE shot_id='s1'"
    ).fetchone()
    assert job["status"] == "queued"
    assert job["version_id"]
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM jobs WHERE shot_id='s1'"
    ).fetchone()["c"] == 1


def test_resuming_paused_preflight_shell_revalidates_instead_of_running_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    _patch_enqueue_runtime(monkeypatch, conn)
    monkeypatch.setattr(
        compiler,
        "compile_prompt",
        lambda *_args, **_kwargs: "安全的视频提示词",
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,episode_id,project_id,status,
               pipeline_stage,created_at,updated_at
           ) VALUES(
               'preflight-paused','video','s1','e1','p1','paused',
               'preflight_blocked',1,1
           )"""
    )
    conn.commit()

    result = worker.resume_episode_video_tasks("e1")

    assert result["resumed_jobs"] == 1
    job = conn.execute(
        "SELECT status,version_id,pipeline_stage FROM jobs WHERE id='preflight-paused'"
    ).fetchone()
    assert job["status"] == "queued"
    assert job["version_id"]
    assert job["pipeline_stage"] == "job_queued"


def test_reconcile_closes_redundant_preflight_after_successful_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    _seed(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES('adopted-v1','s1',1,'prompt','idem','succeeded','/tmp/v1.mp4',1)"""
    )
    conn.execute(
        "UPDATE shots SET adopted_version_id='adopted-v1' WHERE id='s1'"
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,episode_id,project_id,status,pipeline_stage,
               created_at,updated_at
           ) VALUES(
               'redundant-preflight','video','s1','e1','p1','waiting_human',
               'preflight_blocked',2,2
           )"""
    )
    conn.commit()

    report = worker.reconcile_stalled_video_jobs()

    assert report["redundant_preflight_closed"] == 1
    job = conn.execute(
        """SELECT status,cancellation_requested,reason_code
           FROM jobs WHERE id='redundant-preflight'"""
    ).fetchone()
    assert dict(job) == {
        "status": "cancelled",
        "cancellation_requested": 1,
        "reason_code": "SUPERSEDED_PREFLIGHT",
    }
