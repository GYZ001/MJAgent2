import asyncio
import hashlib
import json
import sqlite3

from app import compiler, db, video_modes, worker
from app.schemas import Bible, Character, Shot, World


def test_visible_cast_projection_does_not_require_offscreen_listener_reaction() -> None:
    """VLM 视频质检（qa_shot/_video_qa_contract）已下线，但这条画面可见名单投影逻辑
    （project_visual_contract_to_visible_cast）仍是付费视频 prompt 编译的活路径
    （app/compiler.py 的 compile_prompt），直接对其断言，覆盖不随质检下线而丢失。"""
    shot = Shot(
        shot_no=14, duration_s=10, shot_size="中景", camera_move="固定",
        scene_setting="夜，后山悬崖",
        characters=["丙老", "甲一"], characters_visible=["丙老"],
        action_desc="丙老悬浮于戒指上方，抬手抚须，向甲一说明缘由。",
        first_frame_desc="丙老独自悬浮于戒指上方，面朝甲一方向。",
        last_frame_desc="丙老仍悬浮于戒指上方，望向甲一方向等待回应。",
        state_in="甲一质问完毕，丙老面对甲一，双方对峙持续。",
        primary_action="丙老向甲一说明缘由。",
        state_out="丙老说完，甲一沉默并转向理性思考。",
        continuity_mode="reverse_angle",
    )

    state_in, state_out, primary_action, full_action, visible_contract = (
        compiler.project_visual_contract_to_visible_cast(
            shot,
            state_in=shot.state_in,
            state_out=shot.state_out,
            primary_action=shot.primary_action,
            full_action=shot.action_desc,
            visible_names=["丙老"],
            bible_names=["丙老", "甲一"],
            continuity_mode="reverse_angle",
        )
    )
    action_desc = full_action or primary_action
    if visible_contract:
        action_desc = f"{action_desc}\n画内角色合同：{visible_contract}"

    assert "甲一沉默" not in state_out
    assert "画外甲一方向等待回应" in state_out
    assert "画内角色合同：可辨识画面人物仅限：丙老" in action_desc
    assert "甲一只作为画外叙事关系" in action_desc


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def _seed_project(conn: sqlite3.Connection) -> None:
    bible = Bible(
        characters=[Character(name="A", role="lead", appearance_canonical="black hair")],
        world=World(visual_style_canonical="anime drama style"),
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?,?,?,?)",
        ("p1", "P", "created", bible.model_dump_json(), 1.0),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        ("e1", "p1", 1, "confirmed", 1.0),
    )
    conn.execute(
        """INSERT INTO shots(
               id, episode_id, shot_no, duration_s, shot_size, camera_move, scene_setting,
               characters, action_desc, source_excerpt, narration, dialogues, transition,
               continuity_from_prev, first_frame_desc, last_frame_desc, scene_status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "s1", "e1", 1, 10, "中景", "固定", "室内",
            json.dumps(["A"]), "A坐在桌前整理文件，将散开的纸张平码在桌面。",
            "A坐到桌前，把散开的纸张逐页整理整齐。",
            None, json.dumps([]), "硬切", 0,
            "A坐在桌前，纸张散放在双手之间。",
            "同一机位，A已经把纸张整齐平码在桌面。",
            "approved",
        ),
    )
    conn.commit()


def test_enqueue_budget_reserves_full_timeline_keyframe_estimate(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 10")
    monkeypatch.setattr(video_modes, "estimated_keyframe_generation_count", lambda: 9)
    captured: dict[str, float] = {}

    def reserve(_job_id, _episode_id, estimate, _limit, *, conn=None):
        captured["estimate"] = float(estimate)
        return True

    monkeypatch.setattr(worker.media_scheduler, "reserve_budget", reserve)

    result = worker.enqueue_shot("s1")

    assert result["reused"] is False
    assert "paused_budget" not in result
    assert captured["estimate"] == (
        compiler.shot_cost_cny(10) + worker.config.IMAGE_PRICE_PER_UNIT * 9
    )


def test_enqueue_next_shot_reads_adopted_previous_prompt_and_records_lineage(
    monkeypatch,
) -> None:
    conn = _conn()
    _seed_project(conn)
    conn.execute(
        """INSERT INTO shots(
               id, episode_id, shot_no, duration_s, shot_size, camera_move,
               scene_setting, characters, action_desc, source_excerpt, narration,
               dialogues, transition, continuity_from_prev, first_frame_desc,
               last_frame_desc, scene_status
           )
           SELECT 's2', episode_id, 2, duration_s, shot_size, camera_move,
                  scene_setting, characters,
                  'A把整理好的文件推到桌面右侧。',
                  source_excerpt, narration, dialogues, transition, 1,
                  last_frame_desc,
                  '同一机位，文件已经平码在桌面右侧。',
                  scene_status
           FROM shots WHERE id='s1'"""
    )
    previous_prompt = (
        "[ONE CURRENT ACTION]\n上一镜整理文件。\n\n"
        "[END STATE | 10.0s]\nA已经把纸张整齐平码在桌面。\n\n"
        "[PERSISTENT SCENE GEOMETRY]\n桌子固定在房间中央。 "
        "--ratio 9:16 --dur 10"
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('prev-ver','s1',1,?,'prev-key','succeeded',1)""",
        (previous_prompt,),
    )
    conn.execute(
        "UPDATE shots SET adopted_version_id='prev-ver' WHERE id='s1'"
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(
        worker.media_scheduler,
        "reserve_budget",
        lambda *_args, **_kwargs: True,
    )
    captured: dict = {}

    def compile_with_previous(*_args, **kwargs):
        captured.update(kwargs)
        return "CURRENT PROMPT --ratio 9:16 --dur 10"

    monkeypatch.setattr(compiler, "compile_prompt", compile_with_previous)

    result = worker.enqueue_shot("s2")

    assert captured["previous_prompt_text"] == previous_prompt
    meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?",
        (result["version_id"],),
    ).fetchone()["image_inputs"])
    assert meta["previous_prompt_version_id"] == "prev-ver"
    assert meta["previous_prompt_fingerprint"] == hashlib.sha256(
        previous_prompt.encode("utf-8")
    ).hexdigest()
    assert meta["previous_prompt_inherited"] is True
    assert meta["video_prompt_contract_version"] == (
        compiler.VIDEO_PROMPT_CONTRACT_VERSION
    )


def test_enqueue_budget_reservation_error_keeps_recoverable_preflight(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(worker.errors, "record_and_format", lambda *_args, **_kwargs: "（测试错误）")
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 10")

    def fail_reserve(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker.media_scheduler, "reserve_budget", fail_reserve)

    result = worker.enqueue_shot("s1")

    assert result["retry_scheduled"] is True
    job = conn.execute(
        "SELECT status,version_id,video_slot_active FROM jobs"
    ).fetchone()
    assert dict(job) == {
        "status": "waiting_retry",
        "version_id": None,
        "video_slot_active": 1,
    }
    assert conn.execute("SELECT COUNT(*) FROM shot_versions").fetchone()[0] == 0


def test_enqueue_dispatch_error_keeps_job_durably_accepted(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(worker.errors, "record_and_format", lambda *_args, **_kwargs: "（测试错误）")
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 10")
    monkeypatch.setattr(
        worker.media_scheduler,
        "reserve_budget",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        worker,
        "_enqueue_for_current_status",
        lambda _job_id: (_ for _ in ()).throw(RuntimeError("queue notification failed")),
    )

    result = worker.enqueue_shot("s1")

    assert result["task_accepted"] is True
    assert result["dispatch_deferred"] is True
    job = conn.execute("SELECT status,error FROM jobs WHERE id=?", (result["job_id"],)).fetchone()
    assert job["status"] == "queued"
    assert "持久队列" in job["error"]


def test_generated_reference_gallery_cannot_change_enqueue_idempotency(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 5")

    first = worker.enqueue_shot("s1")
    assert first["reused"] is False

    refs = [
        {
            "id": "r_keep",
            "url": "data:image/jpeg;base64,keep",
            "type": "plot_key_frame",
            "source": "seedream_generated",
            "selectedForSeedance": True,
        },
        {
            "id": "r_gone",
            "url": "data:image/jpeg;base64,gone",
            "type": "plot_key_frame",
            "source": "seedream_generated",
            "selectedForSeedance": False,
            "deleted": True,
        },
    ]
    meta = {"mode": "REFERENCE_IMAGE_MODE", "reference_images": refs}
    conn.execute(
        "UPDATE shot_versions SET status='succeeded', image_inputs=? WHERE id=?",
        (json.dumps(meta, ensure_ascii=False), first["version_id"]),
    )
    assert worker._set_job(first["job_id"], "succeeded") is True
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id='s1'", (first["version_id"],))
    conn.commit()

    unchanged = worker.enqueue_shot("s1")
    assert unchanged == {"reused": True, "version_id": first["version_id"]}

    refs[1]["selectedForSeedance"] = True
    refs[1]["deleted"] = False
    edited_meta = {
        "mode": "REFERENCE_IMAGE_MODE",
        "reference_images": refs,
        "reference_gallery_revision": 123.0,
        "reference_gallery_edited": True,
        "reference_gallery_contract_override": True,
    }
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id=?",
        (json.dumps(edited_meta, ensure_ascii=False), first["version_id"]),
    )
    conn.commit()

    changed = worker.enqueue_shot("s1")
    assert changed == {"reused": True, "version_id": first["version_id"]}

    new_meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?", (changed["version_id"],)
    ).fetchone()["image_inputs"])
    assert "reference_gallery_source_version_id" not in new_meta
    assert all(ref["source"] == "seedream_generated" for ref in new_meta["reference_images"])


def test_new_portrait_revision_prevents_old_gallery_and_video_reuse(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 5")

    current = {"input_fingerprint": "portrait-old"}
    monkeypatch.setattr(
        "app.multiview.resolve_shot_asset_dependencies",
        lambda **_kwargs: dict(current),
    )
    monkeypatch.setattr(
        "app.multiview.manifest_revisions_match",
        lambda frozen, latest: frozen.get("input_fingerprint") == latest.get("input_fingerprint"),
    )

    first = worker.enqueue_shot("s1")
    old_refs = [{
        "id": "old-character",
        "url": "data:image/jpeg;base64,old",
        "type": "character",
        "source": "asset_library",
        "selectedForSeedance": True,
        "library_revision_id": "portrait-old",
    }]
    conn.execute(
        "UPDATE shot_versions SET status='succeeded', image_inputs=? WHERE id=?",
        (json.dumps({
            "mode": "REFERENCE_IMAGE_MODE",
            "reference_images": old_refs,
            "reference_manifest": dict(current),
            "reference_gallery_revision": 1,
            "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        }), first["version_id"]),
    )
    assert worker._set_job(first["job_id"], "succeeded") is True
    conn.execute(
        "UPDATE shots SET adopted_version_id=? WHERE id='s1'",
        (first["version_id"],),
    )
    conn.commit()

    current["input_fingerprint"] = "portrait-new"
    second = worker.enqueue_shot("s1")

    assert second["reused"] is False
    assert second["version_id"] != first["version_id"]
    new_meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?",
        (second["version_id"],),
    ).fetchone()["image_inputs"])
    assert "reference_images" not in new_meta
    assert "reference_gallery_source_version_id" not in new_meta


def test_reroll_does_not_reuse_gallery_containing_generated_keyframe(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 5")
    monkeypatch.setattr(
        "app.multiview.resolve_shot_asset_dependencies",
        lambda **_kwargs: {"input_fingerprint": "frozen-manifest"},
    )
    monkeypatch.setattr(
        "app.multiview.manifest_revisions_match",
        lambda frozen, latest: frozen.get("input_fingerprint") == latest.get("input_fingerprint"),
    )

    first = worker.enqueue_shot("s1")
    refs = [
        {
            "id": "character-a",
            "url": "data:image/jpeg;base64,character",
            "type": "character",
            "source": "asset_library",
            "entity_name": "A",
            "relatedCharacterIds": ["A"],
            "selectedForSeedance": True,
        },
        {
            "id": "scene",
            "url": "data:image/jpeg;base64,scene",
            "type": "scene",
            "source": "asset_library",
            "selectedForSeedance": True,
        },
        {
            "id": "keyframe-a",
            "url": "data:image/jpeg;base64,keep",
            "type": "plot_key_frame",
            "source": "seedream_generated",
            "relatedCharacterIds": ["A"],
            "selectedForSeedance": True,
        },
    ]
    conn.execute(
        "UPDATE shot_versions SET status='succeeded', image_inputs=? WHERE id=?",
        (json.dumps({
            "mode": "REFERENCE_IMAGE_MODE",
            "reference_images": refs,
            "reference_manifest": {"input_fingerprint": "frozen-manifest"},
            "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        }), first["version_id"]),
    )
    assert worker._set_job(first["job_id"], "succeeded") is True
    conn.execute(
        "UPDATE shots SET adopted_version_id=? WHERE id='s1'",
        (first["version_id"],),
    )
    conn.commit()

    rerolled = worker.enqueue_shot("s1", reroll=True)
    rerolled_meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?",
        (rerolled["version_id"],),
    ).fetchone()["image_inputs"])

    assert rerolled["version_id"] != first["version_id"]
    assert "reference_gallery_source_version_id" not in rerolled_meta
    assert "reference_images" not in rerolled_meta
    assert rerolled_meta["reference_input_policy_version"] == video_modes.REFERENCE_INPUT_POLICY_VERSION
    rerolled_prompt = conn.execute(
        "SELECT prompt_text FROM shot_versions WHERE id=?",
        (rerolled["version_id"],),
    ).fetchone()["prompt_text"]
    assert "Reference image" not in rerolled_prompt


def test_legacy_keyframe_gallery_is_not_reused_after_prompt_contract_upgrade(monkeypatch) -> None:
    """无构图合同版本的老关键帧不得继续污染新视频版本。"""
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 5")

    first = worker.enqueue_shot("s1")
    legacy_refs = [{
        "id": "legacy_keyframe",
        "url": "data:image/jpeg;base64,old",
        "type": "plot_key_frame",
        "source": "seedream_generated",
        "selectedForSeedance": True,
    }]
    conn.execute(
        "UPDATE shot_versions SET status='succeeded', image_inputs=? WHERE id=?",
        (json.dumps({
            "mode": "REFERENCE_IMAGE_MODE",
            "reference_images": legacy_refs,
        }), first["version_id"]),
    )
    assert worker._set_job(first["job_id"], "succeeded") is True
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id='s1'", (first["version_id"],))
    conn.commit()

    rerolled = worker.enqueue_shot("s1", reroll=True)
    new_meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?", (rerolled["version_id"],),
    ).fetchone()["image_inputs"])

    assert "reference_gallery_source_version_id" not in new_meta
    assert "reference_images" not in new_meta
    assert new_meta["keyframe_prompt_contract_version"] == video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION


def test_gallery_with_missing_selected_continuity_tail_is_not_technically_reusable() -> None:
    fingerprint = "shot-fingerprint"
    meta = {
        "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        "keyframe_contract_fingerprint": fingerprint,
        "reference_images": [
            {
                "type": "plot_key_frame", "url": "data:image/jpeg;base64,keyframe",
                "selectedForSeedance": True,
                "keyframe_contract_fingerprint": fingerprint,
            },
            {
                "type": "previous_shot_frame", "path": "/definitely/missing/00_previous_tail.jpg",
                "selectedForSeedance": True,
            },
        ],
    }

    assert video_modes.reference_gallery_matches_keyframe_contract(
        meta, expected_fingerprint=fingerprint,
    ) is False


def test_failed_generated_reference_gallery_is_not_reused_by_next_attempt(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 5")

    failed = worker.enqueue_shot("s1")
    refs = [{
        "id": "r_failed_video",
        "url": "data:image/jpeg;base64,still-valid",
        "type": "plot_key_frame",
        "source": "seedream_generated",
        "selectedForSeedance": True,
    }]
    conn.execute(
        "UPDATE shot_versions SET status='failed', image_inputs=? WHERE id=?",
        (json.dumps({
            "mode": "REFERENCE_IMAGE_MODE",
            "reference_images": refs,
            "keyframe_prompt_contract_version": video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION,
        }), failed["version_id"]),
    )
    assert worker._set_job(failed["job_id"], "failed") is True
    conn.commit()

    retried = worker.enqueue_shot("s1")
    retried_meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?",
        (retried["version_id"],),
    ).fetchone()["image_inputs"])

    assert "reference_gallery_source_version_id" not in retried_meta
    assert "reference_images" not in retried_meta


def test_auto_retake_count_is_persisted_on_child_video_version(monkeypatch) -> None:
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "ensure_media_trace", lambda **_kwargs: (None, None))
    monkeypatch.setattr(compiler, "compile_prompt", lambda *a, **k: "PROMPT --dur 5")

    child = worker.enqueue_shot("s1", reroll=True, auto_retake_count=1)
    meta = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id=?",
        (child["version_id"],),
    ).fetchone()["image_inputs"])

    assert meta["auto_retake_count"] == 1


def test_maybe_auto_qa_is_a_noop_and_never_enqueues_retake(monkeypatch) -> None:
    """VLM 视觉质检已下线：_maybe_auto_qa 不再调用模型、不再写 qa_json，只
    原样返回 True，且绝不因「低分」触发重抽。qa_shot 本身已删除，不再需要 monkeypatch 佯装低分。"""
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "log_provider_call", lambda *_args, **_kwargs: None)

    enqueued: list[dict] = []
    monkeypatch.setattr(
        worker,
        "enqueue_shot",
        lambda shot_id, **kwargs: enqueued.append({"shot_id": shot_id, **kwargs})
        or {"version_id": "next"},
    )
    version = worker.new_id("ver")
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status, video_path,
               image_inputs, created_at
           ) VALUES(?, 's1', 1, 'PROMPT', 'idem-retry', 'succeeded', '/tmp/v.mp4', ?, 1.0)""",
        (version, json.dumps({"auto_retake_count": 0, "reference_images": []})),
    )
    conn.commit()

    force_best = asyncio.run(worker._maybe_auto_qa({
        "id": "j-retry",
        "shot_id": "s1",
        "project_id": "p1",
        "after_shot_id": None,
    }, version, "/tmp/v.mp4"))

    assert force_best is True
    assert enqueued == []
    qa_json = conn.execute(
        "SELECT qa_json FROM shot_versions WHERE id=?", (version,),
    ).fetchone()["qa_json"]
    assert qa_json is None


def test_complete_mode_maybe_auto_qa_is_also_a_noop(monkeypatch) -> None:
    """Supervisor 完整补齐模式下同样不再调用模型或写 qa_json。"""
    conn = _conn()
    _seed_project(conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "log_provider_call", lambda *_args, **_kwargs: None)

    enqueued: list[dict] = []
    monkeypatch.setattr(
        worker,
        "enqueue_shot",
        lambda shot_id, **kwargs: enqueued.append({"shot_id": shot_id, **kwargs}) or {},
    )
    version = worker.new_id("ver")
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status, video_path,
               image_inputs, created_at
           ) VALUES(?, 's1', 1, 'PROMPT', 'idem-controlled', 'succeeded', '/tmp/v.mp4', ?, 1.0)""",
        (version, json.dumps({
            "mode": "REFERENCE_IMAGE_MODE",
            "reference_images": [{"id": "r1", "selectedForSeedance": True}],
            "auto_retake_count": 0,
        })),
    )
    conn.commit()

    force_best = asyncio.run(worker._maybe_auto_qa({
        "id": "j1",
        "shot_id": "s1",
        "project_id": "p1",
        "after_shot_id": None,
    }, version, "/tmp/v.mp4", allow_autonomous_retake=False))

    assert enqueued == []
    assert force_best is True
    qa_json = conn.execute(
        "SELECT qa_json FROM shot_versions WHERE id=?", (version,),
    ).fetchone()["qa_json"]
    assert qa_json is None
