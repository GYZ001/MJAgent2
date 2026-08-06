from __future__ import annotations

import asyncio
import json

import pytest

from app import db
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.scene_policy import (
    normalize_scene_image_qa,
    normalize_scene_pack_qa,
    normalize_scene_prompt,
)


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-prd.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return db.get_conn()


def _seed_project(conn, project_id: str = "p") -> None:
    bible = {
        "characters": [],
        "world": {"era": "玄幻", "genre": "玄幻", "visual_style_canonical": "国风厚涂"},
        "scenes": [
            {
                "name": "萧炎卧室", "location_kind": "室内",
                "scene_canonical": "夜晚萧家旧宅卧室，木床书案与药炉位置固定，月光穿窗，冷灰安静，无人物",
            },
            {
                "name": "萧家测验广场", "location_kind": "室外",
                "scene_canonical": "白日萧家测验广场，中央测验石碑，青石地面与看台围合，阳光明亮，空间开阔",
            },
        ],
    }
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,bible_version,created_at) VALUES(?,?,?,?,?)",
        (project_id, "测试", json.dumps(bible, ensure_ascii=False), 1, db.now()),
    )
    conn.commit()


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"person_count": 1, "watermark_detected": False, "forbidden_text_detected": False,
          "space_type_matches": True}, "纯环境策略下检测到人物"),
        ({"person_count": 0, "watermark_detected": True, "forbidden_text_detected": False,
          "space_type_matches": True}, "检测到水印或 Logo"),
        ({"person_count": 0, "watermark_detected": False, "forbidden_text_detected": True,
          "space_type_matches": True}, "检测到禁止的多余文字"),
        ({"person_count": 0, "watermark_detected": False, "forbidden_text_detected": False,
          "space_type_matches": False}, "场景空间类型与锚点不符"),
    ],
)
def test_high_score_never_offsets_scene_hard_gate(facts, expected) -> None:
    qa = normalize_scene_image_qa({"overall": 0.95, "issues": [], **facts})
    assert qa["status"] == "failed"
    assert qa["hard_gate_passed"] is False
    assert qa["score_affects_pass"] is False
    assert expected in qa["hard_failures"]


def test_scene_layout_details_do_not_become_wrong_place_hard_failure() -> None:
    qa = normalize_scene_image_qa({
        "overall": 0.3,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": False,
        "issues": [
            "嵌入式售票窗为开放式大窗口，不符合封闭小型交易口",
            "远端存在开放式通道，未被连续无窗内墙完整包围",
            "未设置明确的无文字影厅入口，仍存在普通柜台",
        ],
    })

    assert qa["status"] == "warning"
    assert qa["hard_gate_passed"] is True
    assert qa["space_type_matches"] is False
    assert qa["space_type_hard_failure"] is False
    assert qa["hard_failures"] == []
    assert any("布局差异" in warning for warning in qa["warnings"])


def test_scene_wrong_place_evidence_remains_a_hard_failure() -> None:
    qa = normalize_scene_image_qa({
        "overall": 0.2,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": False,
        "issues": ["电影院门厅实际画成海堤上的半室外雨廊"],
    })

    assert qa["status"] == "failed"
    assert qa["hard_gate_passed"] is False
    assert qa["space_type_hard_failure"] is True
    assert "场景空间类型与锚点不符" in qa["hard_failures"]


def test_explicit_wrong_place_issue_overrides_conflicting_true_boolean() -> None:
    qa = normalize_scene_image_qa({
        "overall": 0.5,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": True,
        "issues": [
            "画面实际为影院走廊区域，并非预期的电影院放映室内部，"
            "未呈现放映室的核心场景元素"
        ],
    })

    assert qa["reported_space_type_matches"] is True
    assert qa["space_type_matches"] is False
    assert qa["space_type_hard_failure"] is True
    assert qa["status"] == "failed"
    assert qa["hard_gate_passed"] is False


def test_explicit_indoor_frost_is_a_material_hard_failure() -> None:
    qa = normalize_scene_image_qa({
        "overall": 0.7,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": True,
        "issues": [
            "地面及墙角的附着物为白色积雪/霜类物质，"
            "不符合要求的暖灰褐色薄层细粉尘积灰"
        ],
    })

    assert qa["forbidden_material_detected"] is True
    assert qa["status"] == "failed"
    assert qa["hard_gate_passed"] is False
    assert "场景材质出现明确禁止的积雪、冰霜或白色覆盖物" in qa["hard_failures"]

    snow_like = normalize_scene_image_qa({
        "overall": 0.6,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": True,
        "issues": ["地面出现白色水渍/积雪状物质，未使用暖灰褐色薄层细粉尘表现积灰"],
    })
    assert snow_like["forbidden_material_detected"] is True
    assert snow_like["hard_gate_passed"] is False


def test_scene_gate_requires_explicit_negative_evidence() -> None:
    qa = normalize_scene_image_qa({"overall": 0.99, "issues": []})
    assert qa["status"] == "unverified"
    assert qa["hard_gate_passed"] is False
    assert "无法确认画面无水印" in qa["uncertainties"]


def test_reverse_angle_role_and_axis_are_hard_gates_not_ssim() -> None:
    qa = normalize_scene_pack_qa(
        {
            "overall": 0.97,
            "ssim": 0.748937,
            "views": [
                {"view_role": "establishing", "view_role_matches": True},
                {"view_role": "reverse_angle", "view_role_matches": False,
                 "camera_axis_valid": False, "landmark_relation_valid": False},
            ],
        },
        required_roles=["establishing", "reverse_angle"],
        actual_roles=["establishing", "reverse_angle"],
    )
    assert qa["status"] == "failed"
    assert qa["score_affects_pass"] is False
    assert any("reverse_angle" in failure for failure in qa["hard_failures"])


def test_prompt_normalization_only_deduplicates_exact_segments() -> None:
    prompt = normalize_scene_prompt("国风厚涂。。国风厚涂", "冷灰光线；冷灰光线加强", "无水印；无水印")
    assert prompt.count("国风厚涂") == 1
    assert prompt.count("无水印") == 1
    assert "冷灰光线。冷灰光线加强" in prompt


def test_gap_scan_is_read_only_and_classifies_missing(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn)
    from app.api import scan_scene_asset_gaps

    before = {
        "jobs": conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"],
        "runs": conn.execute("SELECT COUNT(*) n FROM workflow_runs").fetchone()["n"],
        "calls": conn.execute("SELECT COUNT(*) n FROM provider_calls").fetchone()["n"],
    }
    result = scan_scene_asset_gaps("p")
    after = {
        "jobs": conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"],
        "runs": conn.execute("SELECT COUNT(*) n FROM workflow_runs").fetchone()["n"],
        "calls": conn.execute("SELECT COUNT(*) n FROM provider_calls").fetchone()["n"],
    }
    assert result["read_only"] is True
    assert result["counts"]["missing"] == 2
    assert before == after


def test_gap_scan_ignores_optional_views_when_primary_is_video_usable(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn)
    image = tmp_path / "usable-primary.jpg"
    image.write_bytes(b"usable")
    primary_qa = {
        "status": "passed", "hard_gate_passed": True, "hard_failures": [],
        "person_count": 0, "watermark_detected": False,
        "forbidden_text_detected": False, "space_type_matches": True,
    }
    group_qa = {
        "status": "failed", "hard_gate_passed": False,
        "hard_failures": ["缺少必需视角：reverse_angle"],
        "required_views": ["establishing", "reverse_angle"],
    }
    conn.execute(
        "INSERT INTO scene_references(id,project_id,scene_name,ep_start,ep_end,image_path,qa_json,"
        "pack_status,group_qa_json,created_at) VALUES('usable','p','萧炎卧室',1,NULL,?,?, 'failed',?,?)",
        (str(image), json.dumps(primary_qa), json.dumps(group_qa), db.now()),
    )
    conn.execute(
        "INSERT INTO scene_reference_views(id,scene_reference_id,view_role,image_path,qa_json,status,created_at) "
        "VALUES('usable-view','usable','establishing',?,?,'ready',?)",
        (str(image), json.dumps(primary_qa), db.now()),
    )
    conn.commit()

    from app.api import scan_scene_asset_gaps

    result = scan_scene_asset_gaps("p")
    assert [item["scene"] for item in result["items"]] == ["萧家测验广场"]
    assert result["counts"]["warning"] == 0


def test_manual_adoption_keeps_explicit_scene_hard_failure_as_warning(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn)
    image = tmp_path / "watermarked.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    artifact = repository.create_artifact(EvidenceArtifact(
        type="scene_reference", scope_type="reference_asset", scope_id="p:萧炎卧室:1",
        status="candidate", trust_level="T1", file_path=str(image),
        content={"scene_name": "萧炎卧室", "prompt": "纯环境，无水印"},
    ))
    repository.create_evaluation(artifact["id"], Evaluation(
        evaluator_type="model", evaluator_name="scene_reference_consistency_qa",
        evaluator_version="2.0.0", status="failed", hard_gate_passed=False, score=95,
        evidence={"qa": normalize_scene_image_qa({
            "overall": 0.95, "person_count": 0, "watermark_detected": True,
            "forbidden_text_detected": False, "space_type_matches": True,
        })},
    ))
    from app.scenes import adopt_scene_candidate

    result = asyncio.run(adopt_scene_candidate(
        "p", "萧炎卧室", artifact["id"], reason="已知风险仍采用",
    ))
    assert result["adopted"] is True
    assert result["gate_retry_exhausted"] is True
    assert image.exists()
    assert repository.get_artifact(artifact["id"])["status"] == "approved"
    assert conn.execute("SELECT COUNT(*) n FROM scene_references").fetchone()["n"] == 1


def _seed_ready_scene_pack(conn, tmp_path):
    group = {
        "status": "ready", "hard_gate_passed": True, "hard_failures": [],
        "policy_version": "scene-no-watermark-1.0.0",
        "required_views": ["establishing", "reverse_angle"],
    }
    old = tmp_path / "old.jpg"; old.write_bytes(b"old")
    reverse = tmp_path / "reverse.jpg"; reverse.write_bytes(b"reverse")
    conn.execute(
        "INSERT INTO scene_references(id,project_id,scene_name,ep_start,ep_end,scene_canonical,prompt,"
        "image_path,qa_json,base_scene_id,bible_version,artifact_id,pack_status,group_qa_json,created_at) "
        "VALUES('current','p','萧炎卧室',1,NULL,'卧室','旧词',?, '{}',NULL,1,NULL,'ready',?,?)",
        (str(old), json.dumps(group, ensure_ascii=False), db.now()),
    )
    for role, path in (("establishing", old), ("reverse_angle", reverse)):
        conn.execute(
            "INSERT INTO scene_reference_views(id,scene_reference_id,view_role,image_path,qa_json,status,created_at) "
            "VALUES(?,?,?,?,?,'ready',?)",
            (f"view-{role}", "current", role, str(path), json.dumps({"hard_gate_passed": True}), db.now()),
        )
    conn.commit()
    return str(old)


def test_scene_pack_accepts_descriptive_vlm_role_labels_by_input_order(tmp_path, monkeypatch) -> None:
    from app import hiagent
    from app.multiview import review_scene_pack_consistency

    first = tmp_path / "establishing.jpg"
    reverse = tmp_path / "reverse.jpg"
    first.write_bytes(b"first")
    reverse.write_bytes(b"reverse")
    monkeypatch.setattr(hiagent, "encode_image_file", lambda path: path)

    async def fake_vlm_check(images, expectation, *, call_meta=None):
        assert "view_role 只能原样输出" in expectation
        return json.dumps({
            "overall": 0.94,
            "geometry_consistency": 0.96,
            "landmark_consistency": 0.97,
            "lighting_consistency": 0.98,
            "views": [
                {
                    "view_role": "establishing shot (forward left foreground viewpoint)",
                    "overall": 0.95,
                    "view_role_matches": True,
                    "camera_axis_valid": True,
                    "landmark_relation_valid": True,
                    "space_coverage_valid": True,
                    "issues": [],
                    "hard_failures": [],
                },
                {
                    "view_role": "reverse angle shot (180-degree rotated)",
                    "overall": 0.95,
                    "view_role_matches": True,
                    "camera_axis_valid": True,
                    "landmark_relation_valid": True,
                    "space_coverage_valid": True,
                    "issues": [],
                    "hard_failures": [],
                },
            ],
            "issues": [],
            "hard_failures": [],
            "uncertainties": [],
        })

    monkeypatch.setattr(hiagent, "vlm_check", fake_vlm_check)
    qa = asyncio.run(review_scene_pack_consistency([
        {"image_path": str(first), "view_role": "establishing"},
        {"image_path": str(reverse), "view_role": "reverse_angle"},
    ], "树林场景"))

    assert qa["status"] == "ready"
    assert qa["hard_gate_passed"] is True
    assert [view["view_role"] for view in qa["views"]] == ["establishing", "reverse_angle"]
    assert all(view["status"] == "ready" for view in qa["views"])


def _strict_candidate(tmp_path):
    image = tmp_path / "candidate.jpg"; image.write_bytes(b"candidate")
    artifact = repository.create_artifact(EvidenceArtifact(
        type="scene_reference", scope_type="reference_asset", scope_id="p:萧炎卧室:1",
        status="candidate", trust_level="T1", file_path=str(image),
        content={"scene_name": "萧炎卧室", "prompt": "新候选"},
    ))
    qa = normalize_scene_image_qa({
        "overall": 0.9, "person_count": 0, "watermark_detected": False,
        "forbidden_text_detected": False, "space_type_matches": True,
    })
    repository.create_evaluation(artifact["id"], Evaluation(
        evaluator_type="model", evaluator_name="scene_reference_consistency_qa",
        evaluator_version="2.0.0", status="passed", hard_gate_passed=True, score=90,
        evidence={"qa": qa},
    ))
    return artifact, str(image)


def test_candidate_pack_qa_failure_still_adopts_when_files_ready(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch); _seed_project(conn)
    old_path = _seed_ready_scene_pack(conn, tmp_path)
    artifact, candidate_path = _strict_candidate(tmp_path)

    async def failed_group(*_args, **_kwargs):
        return {"status": "failed", "hard_gate_passed": False, "hard_failures": ["对向轴线错误"]}

    monkeypatch.setattr("app.multiview.review_scene_pack_consistency", failed_group)
    from app.scenes import adopt_scene_candidate
    result = asyncio.run(adopt_scene_candidate("p", "萧炎卧室", artifact["id"], reason="测试"))
    assert result["adopted"] is True
    current = conn.execute("SELECT * FROM scene_references WHERE ep_end IS NULL").fetchone()
    assert current["image_path"] == candidate_path
    assert current["image_path"] != old_path
    history = conn.execute("SELECT * FROM scene_references WHERE ep_end=0").fetchone()
    assert history is not None


def test_candidate_pack_success_preserves_rollback_history(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch); _seed_project(conn)
    _seed_ready_scene_pack(conn, tmp_path)
    artifact, candidate_path = _strict_candidate(tmp_path)

    async def passed_group(*_args, **_kwargs):
        return {"status": "ready", "hard_gate_passed": True, "hard_failures": [],
                "required_views": ["establishing", "reverse_angle"]}

    monkeypatch.setattr("app.multiview.review_scene_pack_consistency", passed_group)
    from app.scenes import adopt_scene_candidate
    result = asyncio.run(adopt_scene_candidate("p", "萧炎卧室", artifact["id"], reason="测试"))
    assert result["adopted"] is True
    current = conn.execute("SELECT * FROM scene_references WHERE ep_end IS NULL").fetchone()
    history = conn.execute("SELECT * FROM scene_references WHERE ep_end=0").fetchone()
    assert current["image_path"] == candidate_path
    assert history is not None and history["image_path"] != candidate_path
    assert conn.execute(
        "SELECT COUNT(*) n FROM scene_reference_views WHERE scene_reference_id=?", (history["id"],),
    ).fetchone()["n"] == 2


def test_project_context_never_falls_back_to_blocked_bible_scene_cache(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch); _seed_project(conn)
    cached = tmp_path / "blocked.jpg"; cached.write_bytes(b"blocked")
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p'").fetchone()["bible_json"])
    bible["scenes"][0]["ref_image_path"] = str(cached)
    conn.execute("UPDATE projects SET bible_json=? WHERE id='p'", (json.dumps(bible, ensure_ascii=False),))
    conn.execute(
        "INSERT INTO scene_references(id,project_id,scene_name,ep_start,ep_end,image_path,pack_status,"
        "group_qa_json,created_at) VALUES('blocked','p','萧炎卧室',1,NULL,?,'failed',?,?)",
        (str(cached), json.dumps({"status": "failed", "hard_failures": ["检测到水印"]}, ensure_ascii=False), db.now()),
    )
    conn.commit()
    from app.schemas import Bible
    from app.scenes import scene_refs_as_image_inputs
    inputs = scene_refs_as_image_inputs(
        Bible.model_validate(bible), ["萧炎卧室"], 1, project_id="p", episode_no=1,
    )
    assert inputs
    assert inputs[0][1] == "reference_image"


def test_soft_warning_adoption_requires_explicit_reason(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch); _seed_project(conn)
    image = tmp_path / "warning.jpg"; image.write_bytes(b"warning")
    artifact = repository.create_artifact(EvidenceArtifact(
        type="scene_reference", scope_type="reference_asset", scope_id="p:萧炎卧室:1",
        status="candidate", trust_level="T1", file_path=str(image),
        content={"scene_name": "萧炎卧室", "prompt": "纯环境"},
    ))
    qa = normalize_scene_image_qa({
        "overall": 0.8, "person_count": 0, "watermark_detected": False,
        "forbidden_text_detected": False, "space_type_matches": True,
        "issues": ["边缘有轻微噪点"],
    })
    repository.create_evaluation(artifact["id"], Evaluation(
        evaluator_type="model", evaluator_name="scene_reference_consistency_qa",
        evaluator_version="2.0.0", status="passed", hard_gate_passed=True, score=80,
        evidence={"qa": qa},
    ))
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)
    from app.scenes import adopt_scene_candidate
    with pytest.raises(ValueError, match="必须填写采纳理由"):
        asyncio.run(adopt_scene_candidate("p", "萧炎卧室", artifact["id"], reason=""))
    assert conn.execute("SELECT COUNT(*) n FROM scene_references").fetchone()["n"] == 0


def test_scene_prd_routes_are_registered() -> None:
    from app.api import router
    routes = {(route.path, method) for route in router.routes for method in (route.methods or set())}
    expected = {
        ("/api/projects/{project_id}/scene-bible/preview", "POST"),
        ("/api/projects/{project_id}/scene-bible/precheck", "POST"),
        ("/api/projects/{project_id}/scene-refs/precheck", "POST"),
        ("/api/projects/{project_id}/scene-refs/gaps", "GET"),
        ("/api/projects/{project_id}/scene-refs/progress", "GET"),
        ("/api/projects/{project_id}/scene-reviews", "POST"),
        ("/api/projects/{project_id}/scene-reviews/{batch_id}", "GET"),
        ("/api/projects/{project_id}/scene-reviews/{batch_id}/items/{item_id}/disposition", "POST"),
    }
    assert expected <= routes
