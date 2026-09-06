"""模型规划漏掉的镜头按确定性默认补齐（2026-09-05 第 4 轮第 14 集：18 镜漏报第 15/16 镜，整集 VIDEO_PLAN_INVALID）。"""
from __future__ import annotations

from app.video_plan import plan_fill
from app.video_plan.models import ShotVideoGenerationPlan, VideoGenerationMode


def _item(shot_id: str, shot_no: int) -> ShotVideoGenerationPlan:
    return ShotVideoGenerationPlan(
        shot_plan_id=f"svp-{shot_no}", episode_video_plan_id="evp", plan_revision=1,
        source_storyboard_revision_id="rev", shot_id=shot_id, published_shot_id=shot_id, shot_no=shot_no,
        mode=VideoGenerationMode.REFERENCE_IMAGE_MODE, confidence=0.9, capability_snapshot_id="cap",
    )


def _rows_and_payload(n: int):
    rows = [{"id": f"shot_{i}"} for i in range(1, n + 1)]
    payload = [{"shot_id": f"SH-{i}", "database_shot_id": f"shot_{i}", "shot_no": i} for i in range(1, n + 1)]
    return rows, payload


def test_omitted_shots_are_filled_in_order_and_logged(monkeypatch) -> None:
    logged: list[dict] = []
    monkeypatch.setattr(plan_fill, "log_provider_call", lambda *a, **kw: logged.append(kw.get("meta") or {}))
    rows, payload = _rows_and_payload(4)
    plans = [_item("SH-1", 1), _item("shot_2", 2), _item("SH-4", 4)]  # 漏了第 3 镜；id 形态混用也能对上
    out = plan_fill.fill_omitted_shots(
        plans, rows, payload, plan_id="evp", plan_revision=1, revision_id="rev", snapshot_id="cap",
        asset_fingerprints={"shot_3": "fp3"}, episode_id="e1", model="m",
    )
    assert [item.shot_no for item in out] == [1, 2, 3, 4]
    filled = out[2]
    assert filled.shot_id == "shot_3" and filled.reason_codes == [plan_fill.OMITTED_SHOT_REASON]
    assert filled.mode is VideoGenerationMode.REFERENCE_IMAGE_MODE and filled.depends_on_shot_id is None
    assert filled.input_revision_fingerprints["asset_revisions"] == "fp3"
    assert logged and logged[0]["changes"][0]["shot_nos"] == [3]


def test_complete_plan_is_returned_untouched(monkeypatch) -> None:
    monkeypatch.setattr(plan_fill, "log_provider_call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该记账")))
    rows, payload = _rows_and_payload(2)
    plans = [_item("SH-1", 1), _item("SH-2", 2)]
    assert plan_fill.fill_omitted_shots(
        plans, rows, payload, plan_id="evp", plan_revision=1, revision_id="rev", snapshot_id="cap",
        asset_fingerprints={}, episode_id="e1", model="m",
    ) is plans


def test_mangled_shot_id_counts_as_covered_by_shot_no(monkeypatch) -> None:
    """第 12 轮第 24 集：模型把第 3 镜的 shot_id 写错，绑定时按 shot_no 解析回同一镜；补齐若再补一条就成 DUPLICATE_SHOT_PLAN。"""
    monkeypatch.setattr(plan_fill, "log_provider_call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该补")))
    rows, payload = _rows_and_payload(3)
    plans = [_item("SH-1", 1), _item("SH-2", 2), _item("shot_3_typo", 3)]
    out = plan_fill.fill_omitted_shots(
        plans, rows, payload, plan_id="evp", plan_revision=1, revision_id="rev", snapshot_id="cap",
        asset_fingerprints={}, episode_id="e1", model="m",
    )
    assert out is plans


def test_phantom_entry_colliding_with_exact_shot_is_dropped_and_logged(monkeypatch) -> None:
    """第 14 轮第 24 集：18 镜窗口回了 19 条，凭空多出的 id 按位置占了第 18 号，真第 18 镜被挤到第 19 号。"""
    logged: list[dict] = []
    monkeypatch.setattr(plan_fill, "log_provider_call", lambda *a, **kw: logged.append(kw.get("meta") or {}))
    rows, payload = _rows_and_payload(3)
    plans = [_item("SH-1", 1), _item("SH-2", 2), _item("shot_af6d8f9ca1f4", 3), _item("shot_3", 3)]  # 第 3 条是幻影；真第 3 镜序号已由 planner_shot_numbers 修回 3
    out = plan_fill.fill_omitted_shots(
        plans, rows, payload, plan_id="evp", plan_revision=1, revision_id="rev", snapshot_id="cap",
        asset_fingerprints={}, episode_id="e1", model="m",
    )
    assert [(item.shot_id, item.shot_no) for item in out] == [("SH-1", 1), ("SH-2", 2), ("shot_3", 3)]
    assert logged[0]["changes"] == [{"code": plan_fill.PHANTOM_SHOT_REASON, "shot_ids": ["shot_af6d8f9ca1f4"], "shot_nos": [3]}]


def test_two_exact_entries_for_one_shot_keep_the_first(monkeypatch) -> None:
    logged: list[dict] = []
    monkeypatch.setattr(plan_fill, "log_provider_call", lambda *a, **kw: logged.append(kw.get("meta") or {}))
    rows, payload = _rows_and_payload(2)
    plans = [_item("SH-1", 1), _item("shot_1", 1), _item("SH-2", 2)]
    out = plan_fill.fill_omitted_shots(
        plans, rows, payload, plan_id="evp", plan_revision=1, revision_id="rev", snapshot_id="cap",
        asset_fingerprints={}, episode_id="e1", model="m",
    )
    assert [item.shot_id for item in out] == ["SH-1", "SH-2"]
    assert logged[0]["changes"][0]["code"] == plan_fill.PHANTOM_SHOT_REASON and logged[0]["changes"][0]["shot_ids"] == ["shot_1"]
