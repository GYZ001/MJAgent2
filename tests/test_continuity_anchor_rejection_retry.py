"""供应商拒收的是上一段锚点帧时，本镜去掉锚点重试，不按内容拒绝计数（2026-09-05 第 15 集）。

第 16/17 镜各自被拒的 content[N] 都对应第 15 镜成片取出的同一张参考帧：拒的不是本镜内容。
按「同一镜头 3 个独立任务相同拒绝」判成模型拒绝，再把下游依赖改挂到同一个锚点，会把
整条链一镜一镜全部判掉。
"""
from __future__ import annotations

import json
import sqlite3

from app import hiagent
from app.db import get_conn
from app.harness.hiagent_input_image_rejection import rejected_input_image_index
from app.media_exec.job_state import settle_terminal_poll_failure

POLL_ERROR = (
    "Error code: 400 - {\"message\":\"The request failed because the input image 'content[2]' may contain "
    "sensitive information. Request id: 0217886459998243462842069ee0828bfbcf630188a27c118fa59\","
    "\"type\":\"BadRequest\",\"code\":\"InputImageSensitiveContentDetected\",\"param\":\"\",\"request_id\":\"\"}"
)
POLL_RESPONSE = json.dumps({"id": "t1", "status": "failed", "error": {"code": "", "message": POLL_ERROR}})


def test_index_is_read_from_provider_code_and_locator() -> None:
    assert rejected_input_image_index(POLL_ERROR) == 2
    assert rejected_input_image_index(POLL_RESPONSE) == 2
    assert rejected_input_image_index(POLL_ERROR.replace("content[2]", "content[4]")) == 4


def test_other_codes_and_prose_are_not_classified() -> None:
    text_code = POLL_ERROR.replace("InputImageSensitiveContentDetected", "InputTextSensitiveContentDetected")
    assert rejected_input_image_index(text_code) is None
    assert rejected_input_image_index("The request failed because the input image 'content[2]' may contain") is None
    assert rejected_input_image_index("") is None
    assert rejected_input_image_index(POLL_ERROR.replace("'content[2]'", "the second image")) is None


def _labels() -> list[dict]:
    def label(kind: str, name: str) -> dict:
        return {"role": "reference_image", "type": kind, "entity_name": name, "label": name}
    return [label("previous_shot_frame", "第3镜"), label("previous_shot_frame", "第1镜"),
            label("previous_shot_frame", "第2镜"), label("character", "孟浩")]


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',1)")
    conn.execute("INSERT INTO episodes(id,project_id,episode_no,status,created_at) VALUES('e1','p1',1,'generating',1)")
    for no in (1, 2):
        conn.execute(
            """INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
                                 characters,action_desc,dialogues,transition)
               VALUES(?,'e1',?,5,'中景','固定','室内','[]','人物站定','[]','硬切')""",
            (f"s{no}", no),
        )
    meta = {
        "mode": "REFERENCE_IMAGE_MODE",
        "after_shot_id": "s1",
        "after_version_id": "v0",
        "reference_images": [
            {"id": "r1", "type": "previous_shot_frame", "entity_name": "第1镜", "selectedForSeedance": True},
            {"id": "r2", "type": "previous_shot_frame", "entity_name": "第2镜", "selectedForSeedance": True},
            {"id": "r3", "type": "previous_shot_frame", "entity_name": "第3镜", "selectedForSeedance": True},
            {"id": "r4", "type": "character", "entity_name": "孟浩", "selectedForSeedance": True},
        ],
        "_seedance_image_input_labels": _labels(),
    }
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,provider_task_id,image_inputs,created_at)
           VALUES('v1','s2',1,'prompt','idem','running','task-dead',?,1)""",
        (json.dumps(meta, ensure_ascii=False),),
    )
    conn.execute(
        """INSERT INTO jobs(id,kind,shot_id,version_id,episode_id,project_id,status,lease_owner,lease_expires_at,
                            after_shot_id,after_version_id,provider_operation_id,provider_create_state,
                            provider_non_cancellable,provider_submitted_at,provider_poll_required,created_at,updated_at)
           VALUES('j1','video','s2','v1','e1','p1','running','worker-1',9999999999,'s1','v0',
                  'video-create-v1','accepted',1,1,1,1,1)"""
    )
    conn.execute(
        """INSERT INTO provider_calls(ts,kind,model,status,http_status,latency_ms,response_json,operation_id)
           VALUES(1,'video_create','seedance','OK',200,10,'{"id":"task-dead"}','video-create-v1')"""
    )
    conn.commit()


def _settle(conn: sqlite3.Connection, error_text: str):
    failure = hiagent.ProviderFailure.technical(
        hiagent.ProviderFailureKind.EXECUTION_FAILED.value, retryable=True,
    )
    return settle_terminal_poll_failure(
        conn, "j1", "worker-1", shot_id="s2", version_id="v1", failure=failure, error_text=error_text,
    )


def test_rejected_anchor_frame_is_dropped_and_shot_retries_without_anchor() -> None:
    conn = get_conn()
    _seed(conn)
    # content[2] → 标签下标 1 → 第 1 镜取帧（上一段锚点）
    failure = _settle(conn, POLL_ERROR)
    assert failure.category is hiagent.ProviderFailureCategory.TECHNICAL and failure.retryable
    meta = json.loads(conn.execute("SELECT image_inputs FROM shot_versions WHERE id='v1'").fetchone()[0])
    assert [r["type"] for r in meta["reference_images"]] == ["character"]
    assert [x["type"] for x in meta["_seedance_image_input_labels"]] == ["character"]
    assert meta["continuity_degraded"] is True and meta["after_shot_id"] is None
    job = conn.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    assert job["after_shot_id"] is None and job["after_version_id"] is None
    assert job["provider_poll_required"] == 0 and job["provider_create_state"] == "not_started"
    assert conn.execute("SELECT provider_task_id FROM shot_versions WHERE id='v1'").fetchone()[0] is None
    assert conn.execute(
        "SELECT recovery_disposition FROM provider_calls WHERE operation_id='video-create-v1'"
    ).fetchone()[0] == "TASK_FAILED"


def test_rejected_library_image_keeps_content_rejection_path() -> None:
    conn = get_conn()
    _seed(conn)
    # content[4] → 标签下标 3 → 角色定妆照：不是锚点，走原有的内容拒绝计数/换新任务重试
    failure = _settle(conn, POLL_ERROR.replace("content[2]", "content[4]"))
    assert failure.category is hiagent.ProviderFailureCategory.TECHNICAL
    meta = json.loads(conn.execute("SELECT image_inputs FROM shot_versions WHERE id='v1'").fetchone()[0])
    assert len(meta["reference_images"]) == 4 and meta.get("continuity_degraded") is None
    assert conn.execute("SELECT after_shot_id FROM jobs WHERE id='j1'").fetchone()[0] == "s1"
