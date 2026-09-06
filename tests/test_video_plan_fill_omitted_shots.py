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
