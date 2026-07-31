from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import db
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact
from app.scene_policy import normalize_scene_image_qa


def test_environment_only_prompt_removes_conflicting_human_clauses() -> None:
    from app.scenes import environment_only_scene_canonical, scene_ref_prompt

    canonical = "热闹露天坊市，摊位林立，人流穿梭，周围有萧家护卫巡视，色彩明快"
    cleaned = environment_only_scene_canonical(canonical)
    assert "摊位林立" in cleaned
    assert "人流" not in cleaned
    assert "护卫" not in cleaned
    prompt = scene_ref_prompt("国风厚涂", canonical, scene_name="萧家坊市")
    assert "人流穿梭" not in prompt
    assert "护卫巡视" not in prompt
    assert "无人物" in prompt
    assert "规范地点名称：萧家坊市" in prompt
    assert "不得替换成其他地点" in prompt
    assert "地点名是独立且最高优先级的场景语义输入" in prompt
    assert "即使后续场景描述没有重复某个名称限定词，也不得忽略或替换它" in prompt
    assert "画风最高优先级：必须严格保持「国风厚涂」" in prompt
    assert "不得擅自切换成与该画风冲突的真人摄影" in prompt
    assert "再次确认：地点是「萧家坊市」，画风是「国风厚涂」" in prompt


def test_environment_prompt_keeps_auditorium_seats_but_removes_people() -> None:
    from app.scenes import environment_only_scene_canonical, scene_ref_prompt

    canonical = (
        "室内电影院一号厅，空荡观众席，前排台阶，入口通道，"
        "观众等待入场，银幕白光照明"
    )
    cleaned = environment_only_scene_canonical(canonical)
    assert "空荡观众席" in cleaned
    assert "前排台阶" in cleaned
    assert "入口通道" in cleaned
    assert "观众等待入场" not in cleaned

    prompt = scene_ref_prompt("2D 动画厚涂", canonical, scene_name="一号厅")
    assert "空荡观众席" in prompt
    assert "前排台阶" in prompt
    assert "入口通道" in prompt
    assert "观众等待入场" not in prompt


def test_scene_name_semantics_remain_authoritative_when_canonical_is_generic() -> None:
    from app.scenes import scene_name_visual_constraints, scene_ref_prompt

    prompt = scene_ref_prompt(
        "2D 厚涂水彩",
        "室内夜晚，昏暗漫射光，斑驳墙壁与纵深通道",
        scene_name="电影院落灰长廊",
    )

    assert "规范地点名称：电影院落灰长廊" in prompt
    assert "建筑功能、空间类型和状态限定词" in prompt
    assert "材质状态" in prompt
    assert "不得忽略或替换" in prompt
    assert "无文字的影厅入口" in prompt
    assert "沿两侧排列的门洞或入口" in prompt
    assert "可见积灰与尘层" in prompt
    assert "掉漆、破损或污渍不能替代积灰" in prompt

    constraints = scene_name_visual_constraints("电影院落灰长廊")
    assert "影院功能证据" in constraints
    assert "长廊空间" in constraints
    assert "材质状态" in constraints


def test_auditorium_prompt_forbids_windows_and_requires_screen_seats() -> None:
    from app.scenes import scene_ref_prompt

    prompt = scene_ref_prompt(
        "2D 厚涂水彩",
        "室内夜晚，空荡座椅与前方银幕",
        scene_name="一号厅观众席",
    )

    assert "封闭遮光空间" in prompt
    assert "前方银幕、成排座椅和台阶通道" in prompt
    assert "严禁落地窗、外窗、玻璃幕墙、城市外景或室外采光" in prompt


def test_cinema_lobby_prompt_keeps_rain_outside_enclosed_interior() -> None:
    from app.scenes import scene_ref_prompt

    prompt = scene_ref_prompt(
        "2D 厚涂插画，暖黄与海蓝主色调，柔和逆光与雨雾氛围",
        "室内夜晚，复古门厅空间，充电台灯散发暖黄光晕",
        scene_name="电影院门厅",
    )

    assert "建筑内部的封闭室内大厅" in prompt
    assert "四周使用连续无窗内墙完整包围" in prompt
    assert "严禁画成临海公共走廊、半室外门廊" in prompt
    assert "严禁以海洋、海岸、海堤或室外雨幕作为空间主背景" in prompt
    assert "严禁任何对外窗、采光窗、玻璃门、落地窗或玻璃幕墙" in prompt
    assert "雨夜氛围只能用室内冷蓝环境光与暖黄灯光的对比表达" in prompt
    assert "不得出现可见雨滴、雨幕、室外景或玻璃表面" in prompt


def test_merged_corridor_is_normalized_to_dry_ticket_lobby() -> None:
    from app.scenes import scene_generation_canonical, scene_ref_prompt

    canonical = "室内夜晚，落满灰尘的长廊与售票窗，暖黄台灯光，雨夜湿润质感"
    prompt = scene_ref_prompt(
        "2D 动画厚涂",
        canonical,
        scene_name="星港电影院门厅",
    )

    normalized = scene_generation_canonical("星港电影院门厅", canonical)
    assert "雨夜湿润质感" not in normalized
    assert "落满灰尘的长廊" not in normalized
    assert "封闭室内门厅大厅" in normalized
    assert "封闭室内夜景" in normalized
    assert "内墙嵌入式封闭售票窗（带小型交易口）" in normalized
    assert "建筑内部的封闭室内大厅" in prompt
    assert "电影院门厅内部的封闭过渡长廊" not in prompt
    assert "暖灰或灰褐色的薄层细粉尘" not in prompt
    assert "嵌在连续无窗内墙中的封闭售票窗" in prompt
    assert "空白票价框或空白海报灯箱轮廓" in prompt
    assert "严禁开放柜台、独立服务台、吧台或住宅窗户" in prompt

    generated = scene_generation_canonical(
        "星港电影院门厅",
        "傍晚雨夜室内，门廊挂铜铃，售票窗亮暖黄台灯，落灰长廊入口",
    )
    assert "傍晚雨夜室内" not in generated
    assert "封闭室内夜景" in generated
    assert "实心入口内侧上方悬挂铜铃" in generated


def test_scene_hard_gate_retry_prompt_is_bounded_and_hard_failure_only() -> None:
    from app.scenes import scene_hard_gate_retry_prompt

    base = "规范地点名称：电影院长廊。影院功能证据必须可见。"
    retry = scene_hard_gate_retry_prompt(base, {
        "status": "failed",
        "hard_failures": ["场景空间类型与锚点不符"],
        "issues": [
            "未体现影院专属的吸音墙面与影厅入口",
            "室内地面错误生成积雪，必须改为可见积灰",
        ],
    })

    assert retry is not None
    assert base in retry
    assert "上一候选未通过确定性场景硬门禁" in retry
    assert "未体现影院专属的吸音墙面与影厅入口" in retry
    assert "室内地面错误生成积雪" in retry
    assert len(retry) < 1400
    edit_retry = scene_hard_gate_retry_prompt(
        base,
        {
            "status": "failed",
            "hard_failures": ["场景空间类型与锚点不符"],
            "issues": [
                "存在大面积玻璃幕墙与室外雨幕",
                "地面是白色积雪和水渍，不是暖灰积灰",
                "普通柜台无法识别为电影院售票窗",
            ],
        },
        scene_name="星港电影院门厅",
        scene_canonical="室内落灰长廊与售票窗，雨夜湿润质感",
        visual_style="2D 厚涂插画",
    )
    assert edit_retry is not None
    assert base not in edit_retry
    assert "必须重绘有问题的建筑与材质区域" in edit_retry
    assert "任何对外小窗" in edit_retry
    assert "用连续的无窗吸音内墙、实心影厅双开门与空白海报灯箱替换" in edit_retry
    assert "清除地面和柜台上的白色积雪、冰霜、水渍与反光湿痕" in edit_retry
    assert "带小型交易口的封闭售票窗" in edit_retry
    assert "封闭室内夜景" in edit_retry
    assert len(edit_retry) < 1600
    assert scene_hard_gate_retry_prompt(base, {
        "status": "warning",
        "hard_failures": [],
        "issues": ["色调略冷"],
    }) is None


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-candidate-recovery.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return db.get_conn()


def _seed_project(conn) -> None:
    bible = {
        "characters": [],
        "world": {"era": "玄幻", "genre": "玄幻", "visual_style_canonical": "国风厚涂"},
        "scenes": [{
            "name": "萧家坊市",
            "scene_canonical": "白日萧家坊市，古风木制商铺与青石长街，阳光明亮，空间开阔，纯环境无人物",
            "location_kind": "室外",
        }],
    }
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,bible_version,created_at) VALUES(?,?,?,?,?)",
        ("p", "测试", json.dumps(bible, ensure_ascii=False), 1, db.now()),
    )
    conn.commit()


def _candidate(tmp_path) -> dict:
    path = tmp_path / "candidate.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return repository.create_artifact(EvidenceArtifact(
        type="scene_reference",
        scope_type="reference_asset",
        scope_id="p:萧家坊市:1",
        status="candidate",
        trust_level="T1",
        file_path=str(path),
        content={"scene_name": "萧家坊市", "prompt": "国风厚涂纯环境坊市", "attempt": 1},
    ))


def test_hard_gate_retry_uses_failed_candidate_as_edit_seed(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    calls: list[dict] = []
    qa_results = [
        {
            "status": "failed",
            "hard_gate_passed": False,
            "hard_failures": ["场景空间类型与锚点不符"],
            "issues": ["室内场景误画成带室外雨幕的走廊"],
        },
        {
            "status": "ready",
            "hard_gate_passed": True,
            "hard_failures": [],
            "issues": [],
        },
    ]

    async def fake_generate(prompt, anchor_url=None, *, call_meta=None):
        calls.append({
            "prompt": prompt,
            "anchor_url": anchor_url,
            "attempt": (call_meta or {}).get("attempt"),
        })
        return {"b64_json": "ZmFrZS1pbWFnZQ=="}

    async def fake_save(_item, dest):
        Path(dest).write_bytes(b"fake-image")

    async def fake_review(*_args, **_kwargs):
        return qa_results.pop(0)

    artifact_no = 0

    def fake_record(*, file_path, qa, **_kwargs):
        nonlocal artifact_no
        artifact_no += 1
        if qa["status"] == "failed":
            Path(file_path).unlink(missing_ok=True)
            return {"id": None, "status": "rejected_deleted"}
        return {"id": f"art-{artifact_no}", "status": "validated"}

    monkeypatch.setattr("app.scenes._generate_scene_image", fake_generate)
    monkeypatch.setattr("app.scenes._save_image_item", fake_save)
    monkeypatch.setattr("app.scenes._review_scene_ref", fake_review)
    monkeypatch.setattr("app.scenes.record_reference_asset", fake_record)
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)

    from app.scenes import generate_scene_refs
    asyncio.run(generate_scene_refs("p", ["萧家坊市"]))

    assert [call["attempt"] for call in calls] == [1, 2]
    assert calls[0]["anchor_url"] is None
    assert calls[1]["anchor_url"].startswith("data:image/jpeg;base64,")
    assert "编辑输入候选图：必须重绘有问题的建筑与材质区域" in calls[1]["prompt"]


def _evaluation(qa: dict, *, status: str | None = None) -> Evaluation:
    hard = bool(qa.get("hard_failures"))
    return Evaluation(
        evaluator_type="model",
        evaluator_name="scene_reference_consistency_qa",
        evaluator_version=str(qa.get("policy_version") or "legacy"),
        status=status or ("failed" if hard else "passed"),
        hard_gate_passed=bool(qa.get("hard_gate_passed")) and not hard,
        score=float(qa.get("overall") or 0) * 100,
        evidence={"qa": qa},
        recovered=bool(qa.get("qa_recovered")),
    )


def test_latest_scene_qa_replaces_old_unverified_decision(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    artifact = _candidate(tmp_path)
    old = normalize_scene_image_qa({"overall": 1.0, "issues": [], "qa_recovered": True})
    repository.create_evaluation(artifact["id"], _evaluation(old, status="error"))
    fresh = normalize_scene_image_qa({
        "overall": 0.86,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": True,
    })
    repository.create_evaluation(artifact["id"], _evaluation(fresh))

    from app.scenes import _scene_candidate_qa_score, scene_candidate_gate
    gate = scene_candidate_gate(artifact["id"])
    assert gate["verified"] is True
    assert gate["state"] == "passed"
    assert _scene_candidate_qa_score(artifact["id"]) == pytest.approx(0.86)


def test_review_existing_candidate_adds_new_qa_without_regenerating(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    artifact = _candidate(tmp_path)
    qa = normalize_scene_image_qa({
        "overall": 0.91,
        "person_count": 0,
        "watermark_detected": False,
        "forbidden_text_detected": False,
        "space_type_matches": True,
    })

    async def reviewed(*_args, **_kwargs):
        return qa

    async def must_not_generate(*_args, **_kwargs):
        raise AssertionError("重验 QA 不得重新生图")

    monkeypatch.setattr("app.scenes._review_scene_ref", reviewed)
    monkeypatch.setattr("app.scenes._generate_scene_image", must_not_generate)
    from app.scenes import review_scene_candidate
    result = asyncio.run(review_scene_candidate("p", "萧家坊市", artifact["id"]))
    assert result["reviewed"] is True
    assert result["image_regenerated"] is False
    assert result["gate"]["verified"] is True
    assert len(repository.get_evaluations(artifact["id"])) == 1


def test_batch_completion_rechecks_best_old_candidate_before_generating(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    artifacts = []
    for _ in range(5):
        artifact = _candidate(tmp_path)
        old = normalize_scene_image_qa({"overall": 0.9, "issues": [], "qa_recovered": True})
        repository.create_evaluation(artifact["id"], _evaluation(old, status="error"))
        artifacts.append(artifact)

    reviewed_ids = []
    adopted_ids = []

    async def review_existing(project_id, scene_name, artifact_id):
        reviewed_ids.append(artifact_id)
        fresh = normalize_scene_image_qa({
            "overall": 0.88,
            "person_count": 0,
            "watermark_detected": False,
            "forbidden_text_detected": False,
            "space_type_matches": True,
        })
        repository.create_evaluation(artifact_id, _evaluation(fresh))
        from app.scenes import scene_candidate_gate
        return {"gate": scene_candidate_gate(artifact_id)}

    async def adopt_existing(project_id, scene_name, artifact_id, **_kwargs):
        adopted_ids.append(artifact_id)
        artifact = repository.get_artifact(artifact_id)
        return {"image_path": artifact["file_path"]}

    async def must_not_generate(*_args, **_kwargs):
        raise AssertionError("已有候选可复验时不得继续付费生图")

    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: True)
    monkeypatch.setattr("app.scenes.review_scene_candidate", review_existing)
    monkeypatch.setattr("app.scenes.adopt_scene_candidate", adopt_existing)
    monkeypatch.setattr("app.scenes._generate_scene_image", must_not_generate)

    from app.scenes import generate_scene_refs
    asyncio.run(generate_scene_refs("p", ["萧家坊市"]))

    assert len(reviewed_ids) == 1
    assert adopted_ids == reviewed_ids


def test_manual_review_adopts_only_unverified_candidate_with_audit(tmp_path, monkeypatch) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn)
    artifact = _candidate(tmp_path)
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)
    from app.scenes import manually_review_and_adopt_scene_candidate
    result = asyncio.run(manually_review_and_adopt_scene_candidate(
        "p",
        "萧家坊市",
        artifact["id"],
        confirmations={
            "person_free": True,
            "watermark_free": True,
            "forbidden_text_free": True,
            "space_type_matches": True,
        },
        reason="已放大检查四项硬门禁",
    ))
    assert result["adopted"] is True
    assert result["manual_reviewed"] is True
    evaluations = repository.get_evaluations(artifact["id"])
    review = next(row for row in evaluations if row["evaluator_name"] == "scene_candidate_human_hard_gate_review")
    assert review["evidence"]["reason"] == "已放大检查四项硬门禁"
    assert review["evidence"]["confirmations"] == {
        "person_free": True,
        "watermark_free": True,
        "forbidden_text_free": True,
        "space_type_matches": True,
    }


def test_manual_review_preserves_historical_hard_failure_as_warning(tmp_path, monkeypatch) -> None:
    _fresh_db(tmp_path, monkeypatch)
    _seed_project(db.get_conn())
    artifact = _candidate(tmp_path)
    failed = normalize_scene_image_qa({
        "overall": 0.95,
        "person_count": 0,
        "watermark_detected": True,
        "forbidden_text_detected": False,
        "space_type_matches": True,
    })
    repository.create_evaluation(artifact["id"], _evaluation(failed))
    incomplete = normalize_scene_image_qa({"overall": 0.0, "qa_recovered": True})
    repository.create_evaluation(artifact["id"], _evaluation(incomplete, status="error"))
    monkeypatch.setattr("app.multiview.scene_multiview_enabled", lambda: False)
    from app.scenes import manually_review_and_adopt_scene_candidate
    result = asyncio.run(manually_review_and_adopt_scene_candidate(
        "p", "萧家坊市", artifact["id"],
        confirmations={
            "person_free": True, "watermark_free": True,
            "forbidden_text_free": True, "space_type_matches": True,
        },
        reason="接受风险并采用当前产物",
    ))
    assert result["manual_reviewed"] is True
    assert result["image_path"] == artifact["file_path"]


def test_candidate_recovery_routes_are_registered() -> None:
    from app.api import router
    methods = {
        (route.path, method)
        for route in router.routes
        for method in (route.methods or set())
    }
    base = "/api/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}"
    assert (f"{base}/review", "POST") in methods
    assert (f"{base}/manual-review", "POST") in methods


def test_scene_candidate_review_required_finishes_task_as_partial_not_provider_failure(
    tmp_path, monkeypatch,
) -> None:
    conn = _fresh_db(tmp_path, monkeypatch)
    _seed_project(conn)
    from app.scenes import SceneCandidateReviewRequired

    async def pending_review(*_args, **_kwargs):
        raise SceneCandidateReviewRequired("候选已生成，2 张待复核")

    monkeypatch.setattr("app.scenes.generate_scene_refs", pending_review)
    from app.domain.bible_ops import _scene_refs_task
    asyncio.run(_scene_refs_task("p", None))

    project = conn.execute(
        "SELECT scene_refs_status,scene_refs_error FROM projects WHERE id='p'",
    ).fetchone()
    assert project["scene_refs_status"] == "warning"
    assert "候选已生成" in project["scene_refs_error"]
    run = conn.execute(
        "SELECT status,failure_code FROM workflow_runs "
        "WHERE workflow_type='scene_references' AND scope_id='p' ORDER BY updated_at DESC LIMIT 1",
    ).fetchone()
    assert run["status"] == "PARTIAL"
    assert run["failure_code"] == "PARTIAL_RESULT"


def test_mixed_candidate_and_pack_quality_failures_are_not_provider_outages() -> None:
    from app import hiagent
    from app.errors import ContentGenerationError
    from app.scenes import SceneCandidateReviewRequired, _scene_failures_are_quality_only

    assert _scene_failures_are_quality_only([
        SceneCandidateReviewRequired("高分候选待复核"),
        hiagent.ProviderError("多视角资产包未通过：status=failed"),
        ContentGenerationError("场景一致性检查未通过"),
    ]) is True
    assert _scene_failures_are_quality_only([
        SceneCandidateReviewRequired("高分候选待复核"),
        hiagent.ProviderError("上游图像服务 503"),
    ]) is False
